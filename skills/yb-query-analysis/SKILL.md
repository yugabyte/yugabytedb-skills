---
name: yb-query-analysis
description: Analyse YSQL query performance, session activity, and lock contention in YugabyteDB — and assess database health proactively (pre-production, periodic, pre-migration). Triggers on slow query, high latency, query regression, pg_stat_statements output, ASH / active session history, wait events, lock contention, idle in transaction, conflict retries, "why is this query slow", "assess my database", "health check", "pre-go-live", "is my database healthy", connection exhaustion, out of connections, OOM kill. **This skill owns query performance analysis even when the user has already pasted an EXPLAIN plan** — "here is my slow query and its plan, what should I change" belongs here, not in `explain-plan-analyzer`, which is for plan comprehension only (what a node or DIST counter means) and is used from here as a plan-reading aid. For infrastructure/Prometheus metrics (node CPU, disk, tablet hotspots) use `yb-metrics-analysis`. For YCQL use `ycql`. For schema design or Postgres migration DDL use `ysql`.
---

# YugabyteDB YSQL Query Analysis

This skill diagnoses performance at the **SQL and session layer** — from identifying slow queries and wait events to tuning plans and running structured database assessments. It works across all deployment types and at any level of access.

## Output discipline — advisory only; no changes without explicit approval

This skill is **diagnostic and read-only by default.** Only read-only investigation runs without asking — the triage snapshot, `pg_stat_statements`/`pg_stat_activity`/`pg_locks`/ASH queries, `EXPLAIN` (without side effects), and catalog reads. The default deliverable is **succinct guidance: findings and recommended fixes for the user to review — not changes you apply.**

**Never run a statement that mutates state or disrupts the running system without the user's explicit approval** — including DDL (`CREATE INDEX`, `DROP`, `ALTER`, `ALTER DATABASE`/`ALTER SEQUENCE … SET`), session/transaction termination (`pg_terminate_backend`, `yb_cancel_transaction`), statistics resets (`pg_stat_statements_reset`, `yb_pg_stat_plans_reset`), session/database `SET` of GUCs, GFlag or runtime-config changes, or restarts. This includes "diagnostic" mutations such as enabling a flag via `ALTER DATABASE … SET` or resetting stats: propose them and let the user run them (a session-scoped `SET` the user is asked to run in their own session is fine to recommend). Present the exact command and its effect; the user runs it or explicitly approves you running it.

## START HERE — run the triage snapshot first (when you have live access)

Whenever you have live SQL access and the task is "why is this slow" / "assess this database" / "something is performing poorly", your **first action** is to run the one-shot triage script, before you write any EXPLAIN or invent your own diagnostic queries:

```
ysqlsh -h <host> -p 5433 -d <database> -f references/triage-snapshot.sql
```

**On a YBA-managed Kubernetes universe** there is no plain host:port — YSQL runs inside the tserver pods. Connect via `kubectl exec` or a port-forward first; see [`references/access-yba-k8s.md`](references/access-yba-k8s.md). (For a whole-universe "assess for any performance issues" request that should also cover infrastructure/metrics, start from the [`yb-performance-assessment`](../yb-performance-assessment/SKILL.md) orchestrator, which runs this SQL triage *and* the metrics health check.)

It is a single cheap, read-only pass (catalog + `pg_stat_statements` + `pg_stat_activity` only — no user-table scans, no writes, safe on production) covering, in one shot: workload concentration by cumulative time, scan amplification, retry hotspots, tail latency, sequence cache, stale statistics, and connection state. The output tells you **which** queries and objects to drill into — it replaces the guesswork that otherwise sends you to EXPLAIN on the wrong query.

**Why this is mandatory:** the most common failure mode is to skip straight to `EXPLAIN ANALYZE` on a query you constructed yourself — which often uses a clean access path and hides the real problem. The decisive signals (scan ratio of scanned:returned rows, retry rates, where cumulative time actually goes) live in `pg_stat_statements`, not in an EXPLAIN of a hand-written query. Read the snapshot first, then EXPLAIN the queries it flags.

If `pg_stat_statements` is empty or the `docdb_*` columns are NULL, see the enablement notes at the top of [`references/pgss-analysis.md`](references/pgss-analysis.md) (enable `yb_enable_pg_stat_statements_rpc_stats`, ensure the extension is created), then re-run.

## Assessment discipline — breadth first, then depth, then prune

Four rules govern how you work through a diagnosis. They matter as much as the queries themselves:

1. **Do not hone in too quickly.** Finding one red flag is not the end of triage. A database with a slow query frequently also has stale stats, an unindexed FK, and a connection problem. Read **every** section of the triage output and enumerate **all** flags before drilling into any one of them. Tunnel-vision on the first finding is the main way significant issues get missed.

2. **A big primary finding does NOT license skipping the rest — every real issue you found gets reported, whatever it is.** This is the most important rule and the easiest to break. When a dramatic primary issue appears (e.g. a 500,000:1 scan amplification), the natural pull is to report *just that* and stop. Resist it. Any genuine problem you noticed at any point during the assessment — a missing index, `SELECT *` over-fetch, stale stats, an idle-in-transaction connection, a low-cardinality index, anything — gets included in the final report, **regardless of how small it looks next to the headline issue and regardless of what type of issue it is**. The headline finding does not absorb or excuse the others. If you saw it and it's real, it goes in the report.

3. **Run the broad pass alongside the deep dive.** When you drill into the top suspect, keep the full list in view. Confirm or dismiss each remaining flag — don't silently drop it. A short "checked, not a problem" is a valid and useful outcome for a flag. Note that the triage ranks by scan amplification + cumulative time, so issues with a *modest* scan ratio (over-fetch / `SELECT *` on wide rows, a missing range/sort index behind a full Seq Scan + Sort, a low-cardinality or leading-column-mismatch index) won't reach the top of that list — the "Second-tier checks" in [`references/pgss-analysis.md`](references/pgss-analysis.md) catch them. But the principle in rule 2 is general: report every real finding regardless of type, not only the ones on a checklist.

4. **Report by impact in tiers; prune only the genuinely negligible.** The final output must be succinct and ranked, but "succinct" means *tiered*, not *truncated*. Lead with the primary offenders (top scan amplification / cumulative time), then include a clearly-secondary tier listing **every** other real issue found — at minimum a one-line mention each with its evidence. Only omit or compress to a single line things with **negligible** impact (a query at 0.2% of total time, a cosmetically-improvable index never on a hot path). The distinction is *lower priority* (keep it, in the secondary tier) vs *negligible* (prune it). A reader should come away with both "the one thing to fix now" and "the handful of things worth fixing next" — not just the former.

   **Rank by impact, but separate layers — the biggest cumulative-time row is not automatically the headline.** Cumulative `total_exec_time` share is a starting point for ranking, not the verdict. On a short or low-volume capture it can be dominated by **environment/configuration artifacts** rather than the application workload — most commonly driver/catalog type-loading (`pg_type`, `pg_attribute`, `version()`) when connection pooling is off, which can reach 50–70 % of recorded time simply because little application work has accumulated. Treat a connection/config finding as exactly that: report it (e.g. "enable pooling"), but **do not let it occupy the primary slot or end the assessment** — set the catalog/driver rows aside and re-rank the remaining *application* queries (scan ratio, retries, call-count ratio, unindexed FK columns) to find the data-/schema-layer anti-pattern. A config-layer fix and a schema-layer fix are different findings at different layers; finding one is not finding the other. Equally, do **not** over-correct into ignoring a genuine pooling/connection problem — if pooling truly is the dominant issue at steady state, it stays in the report. The goal is correct *attribution by layer*, not suppressing either finding.

## Access detection (infer, do not ask upfront)

| Signal | Access mode |
|---|---|
| User pasted query output / EXPLAIN / CSV | Paste-only — work from the paste; ask for specific additional queries only when needed |
| Mentions "Aeon" / "YugabyteDB Managed" | → read [`references/aeon-access.md`](references/aeon-access.md) first to scope what's available |
| Mentions "YBA" / "Anywhere" / "universe" | Full SQL + Performance Advisor available |
| No deployment mentioned | Assume live YSQL access; proceed with queries |

Access is two independent axes — a user may have live SQL but no GFlag control (common on Aeon). This distinction matters for features like ASH, query diagnostics, and slow-query logging, which require GFlags. The paste-mode collection block in each reference tells you what to ask the user to run and paste when live access is absent.

## Routing by symptom / intent

Read the matching reference; do not load all of them.

| Symptom or intent | Reference |
|---|---|
| **Any live-access investigation — run this first** | [`references/triage-snapshot.sql`](references/triage-snapshot.sql) — one-shot red-flag scan; then drill into what it flags |
| "Slow query", query regression, high P99, want to know which queries are expensive | [`references/pgss-analysis.md`](references/pgss-analysis.md) — start here for any query-level investigation |
| "Indexes exist but it's still slow" | [`references/pgss-analysis.md`](references/pgss-analysis.md) — scan-ratio section; this phrase is the fingerprint of a function/cast defeating index pushdown |
| Pasted `pg_stat_statements` output | [`references/pgss-analysis.md`](references/pgss-analysis.md) — interpret the paste |
| Writes slow, unbatched inserts, `ON CONFLICT` in a loop, high multi-region write latency | [`references/pgss-analysis.md`](references/pgss-analysis.md) — write-batching (§10) and multi-region pipelining (§13) sections |
| Queue table degrading over time, soft-delete / tombstones, wide-row update churn, "many SEEKs" | [`references/pgss-analysis.md`](references/pgss-analysis.md) — read-amplification from version churn (§11); universe view in `yb-metrics-analysis` |
| Deep pagination, slow high `OFFSET` | [`references/pgss-analysis.md`](references/pgss-analysis.md) — keyset pagination (§12) |
| Wrong index chosen (GIN), `IN`/`ANY` list with `ORDER BY` sorting | [`references/query-tuning.md`](references/query-tuning.md) — SAOP merge and GIN cost-model notes |
| Slow join, `Nested Loop` with high `loops`, unbatched join, "plan looks clean but it's slow", BNL / batched nested loop, `yb_bnl_batch_size` | [`references/query-tuning.md`](references/query-tuning.md) — Batched Nested Loop joins (BNL); one DocDB RPC per outer row is a top cause of clean-looking slow plans |
| Pasted EXPLAIN plan + a performance question ("why is this slow", "what should I change") | **Stay here — this skill owns the analysis.** A supplied plan is not a substitute for triage: run the snapshot above for what the plan cannot show (statistics currency, cumulative time, scan amplification, retries), then consult `explain-plan-analyzer` for plan structure (incl. its Stage 3b join-strategy / BNL check) and return here to rank findings and recommend fixes |
| User wants to *understand* a plan — what a node, cost, `loops` or DIST counter means | `explain-plan-analyzer` skill — plan comprehension is its scope; no triage needed |
| "What is the database waiting on", ASH, wait events, hot shard | [`references/ash-analysis.md`](references/ash-analysis.md) |
| Locks, blocking, idle in transaction, connection exhaustion, OOM kill | [`references/contention.md`](references/contention.md) |
| "Force a plan", plan hint, plan regression, Auto Analyze, statistics stale | [`references/query-tuning.md`](references/query-tuning.md) |
| "Assess my database", health check, pre-production, pre-go-live, periodic review | [`references/assessment-checklists.md`](references/assessment-checklists.md) |
| Migrating from Postgres, Voyager | `ysql` skill → [`voyager.md`](../ysql/references/voyager.md) |
| On Aeon — what can I actually do? | [`references/aeon-access.md`](references/aeon-access.md) |

## First query — establish YugabyteDB version when relevant

Only needed before recommending a feature gated behind a version (ASH rss_mem_bytes, yb_query_diagnostics, QPM). Ask or run:

```sql
SELECT version();
-- Look for: YB-2025.2, YB-2024.2, YB-2.20, etc.
```

Features introduced before 2024.2 are assumed present on all clusters in this skill.
