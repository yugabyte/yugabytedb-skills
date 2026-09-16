---
name: yb-performance-assessment
description: Start here for a broad, whole-universe YugabyteDB performance assessment — when the request is open-ended ("assess this universe for any performance issues", "is this cluster healthy?", "find any performance problems", "do a performance review", "where do I start", "something feels slow but I don't know what"). This is the triage-and-orchestration entry point: it detects the deployment/access, runs a fast cross-cutting triage across BOTH layers, then dispatches to the SQL-layer skill (yb-query-analysis) and the infrastructure/metrics skill (yb-metrics-analysis) and synthesises one ranked report. Use this instead of going straight to a single layer when the scope is the whole database/universe rather than one known symptom. For a specific known symptom (a named slow query, a specific hot node) you may go directly to the matching specialist skill.
---

# YugabyteDB performance assessment (triage + orchestration)

A YugabyteDB performance problem can live in **two layers**, and a real assessment must cover both:

| Layer | What it covers | Specialist skill |
|---|---|---|
| **SQL / session** | slow queries, scan amplification, missing indexes, retries, locks, connections, sequences, stats | [`yb-query-analysis`](../yb-query-analysis/SKILL.md) |
| **Infrastructure / metrics** | CPU, memory/OOM, disk & I/O, hotspots/skew, tablet limits, latency/throughput, connection skew | [`yb-metrics-analysis`](../yb-metrics-analysis/SKILL.md) |

The single most common assessment failure is **checking one layer and declaring done**. A "slow database" with a clean SQL layer is often an infra hotspot; a CPU-saturated node is often driven by one bad query. **You must run both tracks** unless one is genuinely unreachable (say so explicitly if so).

This skill is thin on purpose: it sequences the work and hands off. The depth lives in the two specialist skills — read their references, don't reinvent their queries here.

## Output discipline — advisory only; no changes without explicit approval

This assessment is **diagnostic and read-only by default.** Only the two triage scans and the specialists' read-only collection (catalog, `pg_stat_statements`, `pg_stat_activity`, ASH, PromQL / `/prometheus-metrics`) run without asking. The deliverable is **one ranked report of findings and recommendations for the user to review — not changes you apply.**

**Never run a statement that mutates state or disrupts the running system without the user's explicit approval** — including DDL (`CREATE`/`DROP`/`ALTER`, e.g. `ALTER DATABASE`/`ALTER SEQUENCE … SET`), session/transaction termination (`pg_terminate_backend`, `yb_cancel_transaction`), statistics resets (`pg_stat_statements_reset`, `yb_pg_stat_plans_reset`), `SET`/GUC/GFlag/runtime-config changes, restarts, manual compaction, or any `yb-ts-cli` / `kubectl` / `yba-api` write. Show the exact command and what it does, then let the user run it (or explicitly approve you running it). Default to succinct guidance.

## Step 1 — Detect deployment and access (infer; ask only if blocked)

Establish two things before querying anything:

**Deployment type** — determines the metrics source and the SQL connection method:

| Signal | Deployment | Metrics source | SQL access |
|---|---|---|---|
| YBA host mentioned, `X-AUTH-YW-API-TOKEN`, `yb-platform` namespace, universe name | **YugabyteDB Anywhere** | YBA bundled Prometheus / metrics proxy | Direct host:5433, or `kubectl exec`/port-forward on K8s — see [`references/access-yba-k8s.md`](../yb-query-analysis/references/access-yba-k8s.md) |
| "Aeon" / "YugabyteDB Managed" | **Aeon** | curated `cluster-metrics` API only (no full Prometheus) → Performance Advisor/Insights | live YSQL via allowlist; see `yb-query-analysis` → `references/aeon-access.md` |
| Podman/Docker/self-managed, own Prometheus | **Self-managed** | standalone Prometheus or direct node scrape | direct host:5433 |

**Access axes** (independent — you may have one and not the other):
- **Live SQL access?** — can you reach YSQL (`ysqlsh`/`psql` on 5433)? On YBA-K8s this means `kubectl exec` into a tserver pod or a port-forward — see [`references/access-yba-k8s.md`](../yb-query-analysis/references/access-yba-k8s.md). If you have no live SQL, fall back to paste-mode (the user runs queries and pastes output — each `yb-query-analysis` reference has a paste block).
- **Metrics access?** — can you reach a Prometheus (`:9090`) or the YBA metrics proxy? See `yb-metrics-analysis` → `references/data-sources.md`.

Record what you have. The two tracks below run against whatever access exists; note any track you cannot run.

## Step 2 — Fast cross-cutting triage: a mandatory TWO-item gate

There are **two** cheap, read-only triage scans — one per layer — and **both must be run before Step 3**. They are peers, not a sequence. Treat this as a checklist you must complete:

- [ ] **SQL triage snapshot** run (or paste-mode collected)
- [ ] **Metrics quick health check** run

> ### ⛔ Anti-pattern: "run metrics first, then SQL only if metrics reveal a query problem"
> This is the single most common — and most damaging — assessment mistake, and it is **wrong**. The SQL triage snapshot is **not** a deep dive and it is **not** conditional on the metrics results. It is the SQL-layer *equivalent* of the metrics quick health check — the very scan that *reveals* SQL-layer problems in the first place. Infra metrics are **blind** to scan amplification, missing indexes, defeated index pushdown, sequence-cache (CACHE 1) bottlenecks, retry storms, and stale stats — a cluster can show perfectly healthy CPU/memory/disk/latency while one of these silently caps throughput. So: **run the SQL triage snapshot every time, even if the metrics come back all-green.** "Metrics look fine, so I'll skip/lighten the SQL pass" = issues missed.

Distinguish clearly:
- **Step 2 (this step) = the two cheap triage *scans*** — always run both, unconditionally.
- **Step 3 (next) = the deep dives** — *those* are driven by what the two triages surfaced (a deep query/EXPLAIN investigation is warranted only if the SQL triage flagged something; a CPU/disk playbook only if the health check flagged something).

The two scans:

1. **SQL triage snapshot** — `yb-query-analysis` → [`references/triage-snapshot.sql`](../yb-query-analysis/references/triage-snapshot.sql) (workload concentration, scan amplification, retries, tail latency, sequence cache, stale stats, connections). On a YBA-managed Kubernetes universe there is no plain host:port — connect via `kubectl exec` into a `<node>-yb-tserver-N` pod (or port-forward 5433) per [`references/access-yba-k8s.md`](../yb-query-analysis/references/access-yba-k8s.md). No live SQL access at all → paste-mode (ask the user to run the snapshot and paste output).

2. **Metrics quick health check** — `yb-metrics-analysis` → [`references/issue-quick-healthcheck.md`](../yb-metrics-analysis/references/issue-quick-healthcheck.md) (the green/triage gate that routes to CPU / memory / disk / hotspot / latency / tablet-limit playbooks). On a YBA-managed Kubernetes universe, reach the metrics one of two ways — **don't give up if the first is awkward**: (a) find the YBA platform pod (it can be in a *different* kube context than the universe) with `kubectl --context <ctx> get pods -A | grep -i yugaware` and port-forward its `9090`; or (b) **simplest, no kubectl** — call the YBA metrics proxy `POST /api/v1/customers/{cid}/metrics` with the API token you already have. Use (b) whenever the port-forward is any trouble. See [`yba-api` Prometheus reference → Connecting](../yba-api/references/prometheus.md#connecting).

A finding in either layer is a lead, not a conclusion — and the *absence* of a finding in one layer is never a reason to skip the other scan.

## Step 3 — Fan out to the specialist skills

Based on what the two triages surfaced, go deep using the specialist skills — **in both layers as warranted**:

- SQL-layer leads (high scan ratio, retries, slow statements, locks, sequence/stat/connection flags) → drive with [`yb-query-analysis`](../yb-query-analysis/SKILL.md) and its routing table.
- Infra leads (CPU/mem/disk pressure, node hotspot/skew, tablet-limit pressure, latency/throughput regression, master/catalog pressure) → drive with [`yb-metrics-analysis`](../yb-metrics-analysis/SKILL.md) and its per-issue playbooks. For a **broad review** with no dominant infra lead, run its [guided workload sweep](../yb-metrics-analysis/references/workload-sweep.md) as the metrics track's deep pass — it works the expert dashboard order (workload mix → resource → master → background work → balance → hot tables → query cross-check), enumerates *all* points of interest, and shares this skill's rank-by-impact discipline; its §9 query cross-check overlaps the SQL triage you already ran, so don't repeat it.
- **Correlate across layers.** The two layers explain each other: an ASH `DiskIO`/`RPCWait` wait points at a metrics playbook; a hot node in metrics points at a query or tablet in SQL. Follow the cross-links in both directions before concluding a cause.

## Step 4 — Synthesise one ranked report (apply the assessment discipline)

Merge both tracks into a single output, governed by the **assessment discipline** in [`yb-query-analysis` SKILL.md](../yb-query-analysis/SKILL.md#assessment-discipline--breadth-first-then-depth-then-prune). The four rules apply to the *combined* set of findings across both layers:

1. **Don't hone in too quickly** — enumerate flags from *both* triages before drilling.
2. **A big primary finding does NOT license skipping the rest** — every real issue from either layer goes in the report, regardless of size or type.
3. **Run the broad pass alongside the deep dive** — confirm or dismiss each flag; "checked, healthy" is a valid result. Cover the second-tier checks too.
4. **Report by impact in tiers; prune only the negligible** — one ranked list spanning both layers: lead with the top issue, then a secondary tier listing every other real finding (SQL and infra) with its evidence.

State explicitly which tracks you ran and which (if any) you couldn't, so the reader knows the assessment's coverage.

Two ranking traps to avoid when merging the tracks:

- **Attribute findings to the right layer before ranking.** The largest single consumer of cumulative time is not automatically the headline. On a short or low-volume capture it is often an **environment/config artifact** — most commonly driver/catalog type-loading (`pg_type`, `pg_attribute`) when connection pooling is off, which can dominate recorded time simply because little application work has accumulated. Report it (enable pooling) but set it aside and re-rank the *application* queries to find the data-/schema-layer anti-pattern (scan amplification, retries, N+1, unindexed FK column). A config-layer fix is not a substitute for the schema-layer fix — and vice versa.
- **Attribute the measured time before you call anything clean.** Row counts are
  not cost. Produce an explicit breakdown of a plan's `Execution Time` into its
  storage components — YugabyteDB's DIST output gives them directly — and state
  each as a share of the total:
  `Storage Index Read Execution Time`, `Storage Table Read Execution Time`,
  `Storage Write/Flush Execution Time`, `Catalog Read Execution Time`, and the
  residual. **Rank them. The largest avoidable component is your finding.**
  You may not describe a plan as clean, optimal, or "not the problem" until that
  breakdown exists and you have named its largest component.
  - **Report the share; do not judge whether the fix is worth it.** On an
    `Index Scan`, any non-zero `Storage Table Read Execution Time` is the
    base-table fetch for columns the index does not carry — i.e. the measured
    cost of the index not being covering. State that cost as a percentage. The
    trade-off (index size, write amplification, whether a wide `text` column is
    worth carrying) is the reader's call to make with your number in hand, and
    is never a reason to leave the cost out of the report.
  - **Per-source row counts can each equal the rows returned while the query is
    still doing two storage reads.** "Index scanned 1000, table scanned 1000,
    returned 1000" is not 1:1 — it is two reads to serve one result set. Judge by
    the time split and the number of storage read requests, not by comparing one
    node's row count against the row count returned.
- **A clean execution plan does not clear a query.** Plans (and QPM per-call timings) are blind to conflict/restart retries, hot-row contention, wasted speculative writes, N+1 call counts, and lock waits — all of which live in `pg_stat_statements`/ASH, not in a plan. Never let a "plans look healthy" result from the SQL deep-dive end the assessment for a high-volume query; cross-check it against pgss retry/scan/call-count metrics before concluding "no action."

## Related skills

- [`yb-query-analysis`](../yb-query-analysis/SKILL.md) — SQL/session layer (the SQL triage + deep dives live here)
- [`yb-metrics-analysis`](../yb-metrics-analysis/SKILL.md) — infrastructure/metrics layer
- [`yba-api`](../yba-api/SKILL.md) — YBA control-plane API + bundled Prometheus connection details
- [`aeon-api`](../aeon-api/SKILL.md) — Aeon REST API (cluster metrics, lifecycle)
- For schema/DDL design review → `ysql`; for YCQL → `ycql`
