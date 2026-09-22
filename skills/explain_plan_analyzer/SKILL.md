---
name: explain-plan-analyzer
description: 'Explains how to READ a PostgreSQL or YugabyteDB EXPLAIN / EXPLAIN (ANALYZE, DIST) plan — what a node type, cost, row estimate, loop count, filter or DIST storage counter means, and how the plan tree executes. Use for plan comprehension only: "what does this node mean", "walk me through this plan", "what is a YB Batched Nested Loop Join", "how do I read Storage Read Requests", "is loops per-row or total". NOT for performance work — for "why is this query slow", "make this faster", index/tuning recommendations, or investigating a slow or regressed query, use `yb-query-analysis`, which owns query performance analysis and draws on this skill for plan structure. That applies even when a plan has already been pasted: a supplied plan does not make it a plan-reading task.'
---

# Explain Plan Analyzer

> ## ⚠️ Routing: is this a plan-reading question or a performance question?
>
> This skill is the **plan-reading reference**. It explains what a plan says and which shapes are known problems.
>
> **If the user's goal is performance — "why is this slow", "make it faster", tuning or index advice, a regression, or an open-ended "review this and recommend changes" — use `yb-query-analysis` instead.** Being handed a plan does not make it a plan-reading task; it is the most common way a performance question arrives. That skill runs a mandatory triage snapshot first (statistics currency, `pg_stat_statements` cumulative time, scan amplification, retries) — context a plan alone cannot supply, and without which plan-only conclusions are frequently wrong. Hand off, then use the stages below for the plan-structure part of its investigation.
>
> **Stay here** when the user wants to understand the plan itself: what a node or counter means, how the tree executes, why the planner shows a given cond. Answer that directly.
>
> Either way, never present a plan-only reading as a performance verdict — see Stage 0.

When working through a plan, take these stages in order: parse → identify issues → recommend fixes → present clearly.

---

## Stage 0: What a plan CANNOT tell you (read before concluding "healthy")

A plan describes *how one execution accesses data* — not workload cost. A clean shape (Index Scan, point lookup) with a tiny per-call time does **not** mean the query is fine. The cost that matters is per-call time × `calls`: a 0.003 ms statement called 1M times, where most calls do a wasted read-modify-write, is a dominant workload problem that still presents as "fast point lookup." Before clearing any query, check these plan blind spots:

| Blind spot | Why the plan misses it | Where the evidence is |
|---|---|---|
| **Transaction conflict / retry storms** | The winning attempt's plan looks uncontended; retries aren't replayed | `pg_stat_statements` conflict / read-restart retries; ASH `ConflictResolution` waits |
| **Hot-row / hot-tablet contention** | A point lookup is "optimal" per execution; contention is emergent across concurrent calls | pgss latency tail, ASH, tablet metrics |
| **Wasted conditional work** | `UPDATE … WHERE state = $x` shows a clean scan even when it matches nothing — the no-op still costs a read-modify-write | pgss `calls` ≫ rows written; a `Storage Filter` (not `Index Cond`) on a high-call statement |
| **N+1 / chatty client** | Each child plan is individually perfect; the problem is the call count across queries | pgss `calls` ratios between parent and child shapes |
| **Lock waits / idle-in-transaction** | Waiting holds no plan node | `pg_stat_activity`, `pg_locks` |

When working from plans (or QPM) alone, conclude only that *the access path* is healthy — never that the query is. Pair any all-clear with the blind spots left unchecked, and route to the `yb-query-analysis` skill ([`pgss-analysis.md`](../yb-query-analysis/references/pgss-analysis.md), [`contention.md`](../yb-query-analysis/references/contention.md)) for the `pg_stat_statements` cross-check.

**This applies even when you DO find an issue.** Finding a 🔴 seq scan does not discharge the obligation — it is not evidence that the blind spots above are absent, only that you were not looking at them. A confident, correct diagnosis that never says which layers went uninspected reads as a complete assessment when it is a single-layer one, and the reader has no way to know what is still unchecked. State the limits of your evidence whether the verdict is "healthy", "one clear problem", or anything in between.

---

## Stage 0b — Attribute the measured time before concluding

**Attribute the measured time before you call anything clean.** Row counts are
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

## Stage 1: Understand the Plan Type

First determine what you're working with:
- **EXPLAIN only** — has cost estimates but no actual timing or row counts
- **EXPLAIN ANALYZE** — has both estimates *and* actuals (actual time=, actual rows=, loops=)
- **EXPLAIN (ANALYZE, BUFFERS)** — also includes I/O hit/miss data

**For YugabyteDB, ask for `EXPLAIN (ANALYZE, DIST)`** (optionally `, DEBUG`). `DIST` exposes the distributed storage layer — `Storage Read Requests`, `Storage Rows Scanned`, `Storage Table/Index Read Requests`, `Storage Write Requests`. These, not `BUFFERS` (which reflects little in YB's architecture), are the signals that matter. `Storage Rows Scanned` ≫ rows returned means the predicate is applied in DocDB after scanning many rows — the distributed equivalent of a wasteful scan, even when the node label says "Index Scan."

Note the plan type at the start of your response, as it affects how confident you can be about findings.

**On EXPLAIN-only plans, verify statistics rather than inferring them.** Stale statistics are the root cause behind many bad plan shapes (wrong join strategy, ignored index), but the usual tell — estimated vs actual rows diverging — **cannot be seen without `ANALYZE`**. Uniform `rows=1` estimates are equally consistent with genuinely selective predicates and with a planner that has no statistics at all. So don't treat plausible-looking estimates as evidence the planner is well informed: recommend the statistics check as a matter of course whenever the plan shape is questionable. `yb-query-analysis` → `references/triage-snapshot.sql` (§6) has the YB-safe query — driven from cluster-wide `pg_class.reltuples`, where `-1` means never analyzed, because `pg_stat_user_tables` analyze timestamps are node-local.

---

## Stage 2: Parse Key Node Attributes

For each node, extract where present: node type (Seq Scan, Index Scan, Nested Loop, **YB Batched Nested Loop Join**, Hash Join, Merge Join, Sort, Aggregate, …), relation name, estimated `rows`, `actual rows`/`loops` (ANALYZE), `cost`, `actual time` (ANALYZE), the predicate (`filter` / `index cond` / `recheck cond` / `join filter`), `rows removed by filter`, and buffers (BUFFERS). These are standard PostgreSQL plan fields. **On YugabyteDB also capture the `DIST` storage counters** (read requests, rows scanned) per node — they are the distributed cost.

Two attributes carry extra weight on YugabyteDB and are easy to skim past — always record them explicitly:
- **`loops=N` on the inner side of any join.** In YB each loop is typically its own DocDB RPC, so `loops` is a direct read on network round-trips. Remember `actual time` and `actual rows` on a looped node are reported **per loop** — multiply by `loops` for the true cost.
- **The exact form of the inner `Index Cond`.** `(col = outer.col)` is a single-key, per-row lookup. `(col = ANY (ARRAY[outer.col, $1, $2, …]))` is a *batched* lookup. This one syntactic difference is how you tell whether batching is active (see Stage 3b).

---

## Stage 3: Seq Scan Analysis (Highest Priority)

Seq Scans are the first and most critical thing to evaluate.

### Significance Thresholds

Flag a Seq Scan as **significant (🔴 Critical)** if ANY of these apply:
| Condition | Reason |
|---|---|
| Estimated rows > 10,000 | Large table being fully scanned |
| Actual rows > 10,000 (if ANALYZE) | Confirmed large scan |
| Node is inside a Nested Loop with loops > 10 | Cost multiplies: even 500-row scans become expensive |
| "Rows removed by filter" > 50,000 | Massive discard ratio — filter could become an index |
| Seq Scan cost > 30% of total plan cost | Dominates the query |

Flag a Seq Scan as **minor (🟡 Low priority)** if:
- Estimated rows < 1,000 AND not inside a loop
- The table is clearly a small config/lookup table
- Query total cost is already very low (< 100)

**For YugabyteDB**: lower the threshold to 1,000 rows for 🔴 Critical. A Seq Scan reads every row across **all tablets** over RPC from the DocDB storage layer — there is no local sequential-disk advantage, and the work fans out to every tablet in the table. Confirm the real cost with `Storage Rows Scanned` under `DIST`, and mention this explicitly.

### What to Report for Each Significant Seq Scan

1. **Table name** and estimated / actual row count
2. **Filter condition** — this is your index candidate
3. **Rows removed by filter** if present — high discard = strong index opportunity
4. **Loop count** if inside a Nested Loop
5. **Specific index recommendation** based on the filter

**Index recommendation format:**
```sql
-- If filter is: (status = 'pending')
CREATE INDEX ON orders (status);

-- If filter is: (user_id = $1 AND created_at > $2)
CREATE INDEX ON orders (user_id, created_at);

-- If most rows have status='completed' and you only query 'pending':
CREATE INDEX ON orders (status) WHERE status != 'completed';
```

---

## Stage 3b: Join Strategy Analysis (YugabyteDB — as important as Seq Scans)

On a single node a Nested Loop is cheap in-memory iteration. On YugabyteDB each loop's inner side is usually a **network RPC to a DocDB tablet**, so an unbatched Nested Loop turns row count into round-trip count. The cost model doesn't capture this, which makes it a leading cause of "clean plan, slow query" — check it even when no Seq Scan is present.

Classify **every** join level (plans commonly batch some joins and not others):

| What you see | Verdict |
|---|---|
| `YB Batched Nested Loop Join`, inner `Index Cond: (c = ANY (ARRAY[outer.c, $1, …]))`, `loops=1` | ✅ Batching active — up to `yb_bnl_batch_size` keys per RPC |
| `Nested Loop` (plain), inner `Index Cond: (c = outer.c)`, `loops=N` (N > 10) | 🔴 Unbatched — N sequential RPCs |
| `Nested Loop` (plain), inner **Seq Scan**, `loops=N` | 🔴 Worst case — a full distributed scan per outer row |
| `YB Batched Nested Loop Join` but inner `loops` ≫ 1 | 🟡 Batching chosen but batch size is low |
| `Hash Join` with a large inner side | 🟡 Reads the *whole* inner relation over RPC; often worse than a batched index lookup for many-to-one joins. A small lookup/dimension table scanned once is legitimate — flag as fine |

The tell for batching is always `= ANY (ARRAY[…])`. Multi-column joins use a `ROW(...)` form — `(ROW(a,b) = ANY (ARRAY[ROW(outer.a, outer.b), …]))` is batched; don't misread it as unbatched. A bare `(a = outer.a AND b = outer.b)` is unbatched.

**On EXPLAIN without ANALYZE, judge on shape, not estimates.** With no `loops`, use node type and `Index Cond` form alone: a plain `Nested Loop` doing per-row index lookups is a finding **even when every node estimates `rows=1`**. Narrow predicates produce `rows=1` throughout, hiding fan-out that appears at real cardinality, and `cost=` does not model RPC latency. Report it as a shape-level risk and ask for `EXPLAIN (ANALYZE, DIST)` — don't clear the plan because the estimates look tiny.

**Why batching wasn't chosen**, in likelihood order: stale statistics (planner estimates ~1 outer row, so batching looks pointless — the most common cause on a correctly configured cluster); `yb_bnl_batch_size = 1` (the default on 2.20 and earlier) or `yb_enable_batchednl = off`; no index on the inner join column; or a non-batchable join condition — an expression wrapping the *inner* column, or a non-equality operator, can't become `= ANY(ARRAY[…])` and shows up in `Join Filter` instead of the inner `Index Cond`. (Residual conditions in `Join Filter` are normal for multi-column joins; it only matters when the *selective* condition is stuck there while the `Index Cond` stays broad.)

Note that **`SET enable_nestloop = off` does not disable BNL on 2.21+** — BNL is its own strategy there, so BNL nodes surviving that flag is expected, and the hints differ (`NestLoop(...)` = unbatched, `YBBatchedNL(...)` = batched).

Confirming a fix: inner `loops` collapses toward `1`, the `Index Cond` gains `ANY (ARRAY[…])`, and **`Storage Read Requests` under `DIST` drops by roughly the batch factor** — that RPC count is the metric to quote. For the GUCs, hints and remediation steps, see `yb-query-analysis` → [`query-tuning.md`](../yb-query-analysis/references/query-tuning.md) ("Batched Nested Loop joins").

---

## Stage 4: Secondary Issue Checks

After seq scans, scan for these standard PostgreSQL signals (rough order of impact). They behave the same in YugabyteDB unless noted:

| Signal in plan | Means | Fix |
|---|---|---|
| Est. vs actual rows differ > 10× (ANALYZE) | Stale/insufficient statistics; > 100× can flip the join strategy | `ANALYZE table;` — `CREATE STATISTICS` for correlated columns |
| Nested Loop, large outer side + inner Seq Scan / expensive lookup | Cost = outer_rows × inner_cost_per_loop | Index the join column. **PostgreSQL:** test with `SET enable_nestloop = off`. **YugabyteDB:** go to Stage 3b instead — `enable_nestloop` does not control BNL, and batching usually beats the Hash Join that flag pushes you toward |
| Hash node shows `Batches: N` (N > 1) | Hash table spilled `work_mem` | Raise `work_mem` (session-test first) |
| `Sort Method: external merge` / `external sort` | Sort spilled to disk | Raise `work_mem` |
| High "Rows Removed by Filter" on an Index Scan | Index not selective; rows discarded post-scan | Composite index including the filter column |
| High `Rows Removed by Index Recheck` (Bitmap Heap Scan) | Lossy bitmap pages | Raise `work_mem` so the bitmap stays exact |
| ANALYZE total time > 1,000 ms for a "simple" query | Slow even with no single dominant node | Investigate cumulatively |

---

## Stage 5: Output Format

Use this structure when you are analysing a plan — either as the plan-structure step of a `yb-query-analysis` investigation, or when the user explicitly asked what is wrong with a plan. For a plain comprehension question ("what does this node mean") just answer it; don't impose the full report.

Structure your response exactly as follows:

### Plan Type
State whether this is EXPLAIN, EXPLAIN ANALYZE, or EXPLAIN (ANALYZE, BUFFERS), and note any limitations this places on the analysis.

### Plan Overview
1–2 sentences: what the query does, overall shape (joins, aggregations, etc.), and total cost or execution time if available.

### 🔴 Critical Issues
One section per issue. For each:
- **Issue title** (e.g., "Seq Scan on `orders` — 450,000 rows")
- What it is and why it matters
- Specific fix with SQL if applicable

If no critical issues: say "No critical issues found."

### 🟡 Secondary Issues
Brief bullets for lower-priority findings (row estimate mismatches, memory spills, etc.)

If none: omit this section.

### ✅ Recommended Actions
Numbered list, ordered by expected impact:
1. Most impactful fix (usually the biggest seq scan index)
2. Second fix
3. ...

Keep this list actionable — real SQL commands where possible.

### 🔍 Not checked (evidence limits)

**Required — include this section in every plan analysis, including ones that found critical issues.** One or two lines naming the layers this analysis did not see and what could therefore still be wrong. When working from a plan alone, that means at minimum `pg_stat_statements` (call counts, retries, scan-vs-returned ratios) and any cluster/infrastructure metrics, plus the Stage 0 blind spots most relevant to this query shape. Route onward to `yb-query-analysis` for the pgss cross-check.

Example: *"Not checked: pg_stat_statements — so this query's `calls`, and whether the workload's dominant cost lies elsewhere, are unknown; and cluster metrics — so contention, hot tablets and node-level degradation are unruled-out. The finding above is a plan-level access-path defect only."*

Omit this section only when the request was a plain comprehension question rather than an analysis.

---

## Guidelines

- **Always explain why** something is a problem, not just that it is
- **Be specific**: name the table, show the filter, write the actual CREATE INDEX statement
- If the plan is truncated, say so and analyze what's available
- Do not recommend indexes that are already being used (already showing Index Scan in the plan)
- If the **access path** looks healthy, confirm *that* — but scope the claim to what a plan can prove ("the access path is efficient", "no plan-level issue"), never an unqualified "the query is healthy" when working from a plan (or QPM) alone. Pair any all-clear with the Stage 0 blind spots left unchecked.
- **Always state what you did not check (Stage 5 "Not checked"), including when you found a clear problem.** The other guidance here scopes the *all-clear* path; this one is unconditional. A plan-only analysis that names a root cause and stops looks indistinguishable from a full assessment — say which layers were never inspected, every time.
- For EXPLAIN-only (no ANALYZE): caveat your findings with "based on planner estimates"
- If the user mentions this is YugabyteDB, apply the lower thresholds, ask for `DIST` output, and add YB-specific context
- **Always classify the join nodes on a YugabyteDB plan (Stage 3b), even when no Seq Scan is present.** An all-Index-Scan plan with an unbatched `Nested Loop` is a genuine 🔴 finding; "no seq scans, low cost" is not an all-clear on YB. Quote `loops`, the inner `Index Cond` form, and `Storage Read Requests` as the evidence.
- **YugabyteDB conditional write (`Storage Filter` on a high-call UPDATE/DELETE):** the predicate appearing as a `Storage Filter` (not `Index Cond`) means every call does a read-modify-write even when it matches nothing — wasted work at high `calls` (the job-queue "claim" anti-pattern). Flag it despite sub-millisecond per-call time; recommend the pgss check (`calls` vs writes, scan-vs-returned ratio) and the app-level fix (`SELECT … FOR UPDATE SKIP LOCKED`, or a partial index on the filtered column so workers find available rows directly).
