# Query tuning

Tools for stabilising and improving query execution plans once a problem query has been identified. Use after `pgss-analysis.md` has identified a candidate and `EXPLAIN (ANALYZE, DIST)` has confirmed the plan shape.

For **index and schema changes** (covering indexes, sharding key redesign, colocation) see the `ysql` skill — that skill owns DDL patterns. This reference covers plan-level controls and statistics maintenance.

---

## pg_hint_plan — forcing a specific execution plan

Pre-installed and enabled by default on all YugabyteDB deployments. Allows hints in SQL comments or persistent hints stored in a table. Use when:
- The planner chooses a Seq Scan and you know an index exists and would be faster
- A join is using Hash Join but `YBBatchedNL` would reduce cross-node RPCs
- A plan regression occurred and the previous plan shape is known

### Inline hints (current statement only)
```sql
-- Force an index scan (replace table and index names as appropriate)
/*+ IndexScan(orders orders_customer_status_idx) */
EXPLAIN (ANALYZE, DIST) SELECT * FROM orders WHERE customer_id = $1 AND status = 'PENDING';

-- Force index-only scan (requires covering index)
/*+ IndexOnlyScan(orders orders_customer_cover_idx) */
SELECT customer_id, status, created_at FROM orders WHERE customer_id = $1;

-- Force a batched nested loop join (efficient for distributed many-to-one)
/*+ Leading(o c) YBBatchedNL(o c) */
SELECT o.id, c.name FROM orders o JOIN customers c ON o.customer_id = c.id
WHERE o.status = 'PENDING';

-- Prevent a sequential scan
/*+ NoSeqScan(large_table) */
SELECT * FROM large_table WHERE status = 'active';

-- Force join order and method together
/*+ Leading(fact dim1 dim2) HashJoin(fact dim1) YBBatchedNL(fact dim2) */
SELECT ... FROM fact JOIN dim1 ... JOIN dim2 ...;
```

**Available hint types:**

| Category | Hints |
|---|---|
| Scan | `SeqScan`, `IndexScan`, `IndexOnlyScan`, `BitmapScan`, `NoSeqScan`, `NoIndexScan`, `NoIndexOnlyScan` |
| Join method | `NestLoop` (classic — **unbatched**), `HashJoin`, `MergeJoin`, `YBBatchedNL` (distributed batch — preferred for YugabyteDB many-to-one joins) |
| Join order | `Leading(t1 t2 t3)` — left-to-right = outer-to-inner |
| Planner config | `Set(enable_hashjoin off)`, `Set(enable_seqscan off)`, `Set(yb_bnl_batch_size 1024)` |

⚠️ **`NestLoop` and `YBBatchedNL` are different join methods from 2.21 / 2024.2 onward.** `NestLoop(a b)` forces the classic one-RPC-per-outer-row join; only `YBBatchedNL(a b)` forces the batched one. The pre-2.21 idiom — `NestLoop(a b)` together with `Set(yb_bnl_batch_size 1024)` — now yields an **unbatched** plan, so hints copied from older runbooks silently regress. See "Batched Nested Loop joins" below.

### Persistent hints (apply to all matching queries from all connections)
```sql
-- Store a hint (norm_query_string uses $1, $2 placeholders — same format as pg_stat_statements.query)
INSERT INTO hint_plan.hints (norm_query_string, application_name, hints)
VALUES (
    'SELECT id, status, created_at FROM orders WHERE customer_id = $1 AND status = $2',
    '',   -- empty = match any application; or specify app name to scope it
    'IndexOnlyScan(orders orders_customer_status_cover_idx)'
);

-- View all stored hints
SELECT * FROM hint_plan.hints ORDER BY id;

-- Remove a hint
DELETE FROM hint_plan.hints WHERE norm_query_string LIKE '%orders%';
```

**Finding the correct `norm_query_string`:** copy `pg_stat_statements.query` for the target query — it already uses `$1`, `$2` placeholder format and is normalized the same way.

### Verifying a hint is being applied
```sql
SET pg_hint_plan.debug_print = on;
SET pg_hint_plan.message_level = debug;
-- Then run the query — hint application is logged to the PostgreSQL log
```

---

## Query Plan Management (QPM)

⚠️ **Available from YugabyteDB 2025.2 (EA). Requires a GFlag change + TServer restart. On Aeon: support-only.**

QPM records the execution plan history for each query (identified by `queryid` from `pg_stat_statements`). When a plan regresses — the planner starts choosing a worse plan after a schema change, statistics update, or software upgrade — QPM can surface what the previous plan was and generate the `pg_hint_plan` hint needed to restore it.

**Enable (TServer GFlag, restart required):**
```
yb_pg_stat_plans_track = all     -- options: none | top | all (default: none pre-2026)
```

**Workflow after enabling:**
1. QPM builds plan history automatically as queries execute
2. When you detect a regression (via `pg_stat_statements` mean time increasing), look up the plan history for that `queryid`
3. The plan history reveals when the plan changed — correlate with schema changes, `ANALYZE`, or upgrades
4. Use `pg_hint_plan` to enforce the previously known-good plan

QPM's plan history is stored in internal system tables (exact view names may change during EA). The primary value is the correlation — "plan changed at time X" — rather than direct query access to history.

---

## Auto Analyze — keeping planner statistics current

Stale statistics cause the planner to make wrong cardinality estimates, which leads to wrong plan choices (wrong join strategy, missing index usage). This is a common cause of plan regressions after bulk loads or schema changes.

### Check statistics staleness

> **YB-safe pattern (read this first).** In YugabyteDB the `pg_stat_user_tables`/`pg_stat_all_tables` analyze columns are **node-local** — a table analyzed on another node reads back NULL here — and **Auto Analyze records into `last_analyze`, not `last_autoanalyze`** (validated on RF3 2025.2: after auto-analyze, `last_analyze` + `analyze_count` were set while `last_autoanalyze` stayed NULL). So never key off `last_autoanalyze` alone, and never trust a single node's timestamp. Drive the check from **`pg_class.reltuples`**, which is the cluster-wide catalog value (identical on every node): `reltuples = -1` means the table was **never analyzed**.

```sql
-- Primary signal: pg_class.reltuples (cluster-wide). last_analyze here is this
-- node's local record only — see the cross-node query below for recency.
SELECT c.relname,
       c.reltuples::bigint                            AS est_rows,
       CASE WHEN c.reltuples = -1 THEN 'NEVER ANALYZED' ELSE 'analyzed' END AS stats_state,
       GREATEST(st.last_analyze, st.last_autoanalyze) AS last_analyze_this_node,
       now() - GREATEST(st.last_analyze, st.last_autoanalyze) AS age_this_node
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_stat_all_tables st ON st.relid = c.oid
WHERE c.relkind IN ('r','p')
  AND n.nspname NOT IN ('pg_catalog','information_schema')
ORDER BY (c.reltuples = -1) DESC, age_this_node DESC NULLS FIRST
LIMIT 20;
```

**Flag if:** `est_rows = -1` (never analyzed — planner blind), or the analyze timestamp is more than a few hours old on an active table **once you have confirmed it across all nodes** (a single node's NULL is not proof — see below).

**Confirming recency across all nodes.** Because the timestamp is node-local, a table that looks "never analyzed" from one connection may have been analyzed on another node. Two ways to check reliably:
- **2025.2.3+**: use the cluster-aware `yb_stat_auto_analyze()` function.
- **Any version**: iterate the endpoints from `yb_servers()` and take the max, e.g. loop in your shell:
  ```bash
  for h in $(ysqlsh -tAc "SELECT host FROM yb_servers()"); do
    ysqlsh -h "$h" -tAc "SELECT '$h', relname, GREATEST(last_analyze, last_autoanalyze)
                         FROM pg_stat_all_tables WHERE relname='<table>';"
  done
  ```
  (Or, if `dblink` is installed, fan the same query across `yb_servers()` hosts and `max()` the result in one query.) The table has fresh stats if **any** node reports a recent timestamp.

### Manual ANALYZE (always self-service, no GFlags needed)
```sql
-- Analyze a specific table immediately
ANALYZE orders;

-- Verbose output (shows which columns were analyzed, statistics)
ANALYZE VERBOSE orders;

-- Analyze the whole database (run during a maintenance window — can take minutes on large schemas)
ANALYZE;
```

Run `ANALYZE` manually after:
- A large bulk load or `COPY` operation
- A schema change (new column, changed constraint)
- A YugabyteDB version upgrade (optimizer statistics format may change)
- Before running the first performance test on a new dataset

### Auto Analyze configuration

Auto Analyze was introduced as Early Access in **2025.1** and is GA and enabled by default from **2025.2** (when CBO is on). It does not exist on 2024.x or earlier — on those clusters manual `ANALYZE` is the only option. `ysql_enable_auto_analyze` is a **GFlag, not a GUC** — `SHOW ysql_enable_auto_analyze;` errors with *"unrecognized configuration parameter"* (validated on 2025.2.1). Verify it from the tserver flags endpoint instead: `curl -s http://<tserver>:9000/api/v1/varz | grep ysql_enable_auto_analyze` (expect `true`).

**TServer GFlags (restart required to change):**
```
ysql_enable_auto_analyze = true              -- default true in 2025.2+ (the flag in play on 2025.2)
ysql_enable_auto_analyze_infra = true        -- default true in 2025.2+
ysql_auto_analyze_scale_factor = 0.1         -- trigger at 10% row changes (default)
ysql_auto_analyze_threshold = 50             -- minimum row mutations before eligible
ysql_auto_analyze_min_cooldown_per_table = 10s
ysql_auto_analyze_max_cooldown_per_table = 24h
```
On **2025.1** the enabling flags are instead `ysql_enable_auto_analyze_service` (Master + TServer) and `ysql_enable_table_mutation_counter` (TServer); on 2025.2 these read `false` and are superseded — do not mistake that for "auto-analyze disabled".

**Trigger formula:** `mutations_since_analyze > threshold + scale_factor × reltuples` (uses `pg_class.reltuples`).

> **Confirming Auto Analyze actually ran (YB quirk).** Validated on RF3 2025.2: after Auto Analyze fires, it populates `pg_stat_all_tables.last_analyze` and `analyze_count` — **not** `last_autoanalyze`/`autoanalyze_count`, which stay NULL/0 — and only on the node where it ran. So `last_autoanalyze IS NULL` is **not** evidence it didn't run. To confirm, check that `pg_class.reltuples` updated (cluster-wide) and/or that `GREATEST(last_analyze, last_autoanalyze)` is recent on **some** node (loop `yb_servers()`); on 2025.2.3+ use `yb_stat_auto_analyze()`.

On Aeon: Auto Analyze is managed by Yugabyte and likely enabled. Manual `ANALYZE` is always self-service.

### Statistics and covering index relationship

If EXPLAIN shows an `Index Scan` with high `Storage Rows Scanned` (rows scanned >> rows returned), the issue is usually a **missing covering index** rather than stale statistics. After running `ANALYZE`, if the plan still shows the inefficient shape, the fix is to add `INCLUDE (col1, col2)` to the existing index. See the `ysql` skill for the DDL pattern.

---

## Tuning planner settings for CBO (Cost-Based Optimizer)

YugabyteDB 2025.2+ ships improvements to the cost-based optimizer. If you're seeing poor join plans on a recent version:

```sql
-- Enable CBO enhancements (session-level first, then promote to database if confirmed helpful)
SET yb_enable_cbo = on;
SET yb_max_saop_merge_streams = 64;
SET yb_enable_derived_saops = true;
SET yb_enable_derived_equalities = true;

-- After ANALYZE, check if plans improve:
EXPLAIN (ANALYZE, DIST) <your query>;

-- If confirmed: promote to database level
ALTER DATABASE <dbname> SET yb_enable_cbo = on;
ALTER DATABASE <dbname> SET yb_max_saop_merge_streams = 64;
ALTER DATABASE <dbname> SET yb_enable_derived_saops = true;
ALTER DATABASE <dbname> SET yb_enable_derived_equalities = true;
```

These are GUC parameters (set via SQL, not GFlags) — self-service on all deployment types including Aeon.

---

## Batched Nested Loop joins (BNL) — the unbatched-join anti-pattern

A plain `Nested Loop` re-queries the inner table once per outer row, and on YugabyteDB each of those is a **separate DocDB RPC**. Batched Nested Loop (BNL) collapses up to `yb_bnl_batch_size` outer keys into one `= ANY(ARRAY[…])` lookup, turning N round-trips into N/batch_size. This is frequently the difference between a "clean-looking" plan and an acceptably fast one.

**Fingerprint in `EXPLAIN (ANALYZE, DIST)`:**

| | Unbatched (🔴) | Batched (✅) |
|---|---|---|
| Join node | `Nested Loop` | `YB Batched Nested Loop Join` |
| Inner `Index Cond` | `(c = outer.c)` | `(c = ANY (ARRAY[outer.c, $1, $2, …]))` |
| Inner `loops` | `N` (= outer rows) | `1` (or `ceil(N / batch_size)`) |
| `Storage Read Requests` | scales with outer rows | roughly outer_rows / batch_size |

On plain `EXPLAIN` (no `ANALYZE`) there is no `loops` — judge on the node type and `Index Cond` form alone. A plain `Nested Loop` doing per-row index lookups is a finding **even when every node estimates `rows=1`**; narrow predicates produce `rows=1` estimates that hide the fan-out, and `cost=` does not model RPC latency.

**Controls:**
```sql
SHOW yb_bnl_batch_size;    -- 1 = BNL OFF. Default 1 on ≤2.20; 1024 from 2.21 / 2024.2
SHOW yb_enable_batchednl;  -- planner's use of BNL

SET yb_bnl_batch_size = 1024;   -- 128–1024; lower for low-latency single-region, 1024 across regions
SET yb_enable_batchednl = on;

-- Prove the win before changing anything globally:
EXPLAIN (ANALYZE, DIST, COSTS OFF) /*+ YBBatchedNL(o c) */ SELECT …;
EXPLAIN (ANALYZE, DIST, COSTS OFF) /*+ NestLoop(o c) */    SELECT …;  -- the unbatched comparison
```

**Why BNL may not be chosen** — work through in this order:
1. `yb_bnl_batch_size = 1` (the default on 2.20 and earlier) or `yb_enable_batchednl = off`.
2. **Stale statistics** — if the planner estimates ~1 outer row, batching looks pointless. `ANALYZE` both tables and re-plan. This is the most common cause on a correctly-configured cluster.
3. **No index on the inner join column** — BNL still needs an index to batch lookups against; an inner Seq Scan can't be batched.
4. **Non-batchable join condition** — an expression/function wrapping the *inner* column, or a non-equality operator, can't become `= ANY(ARRAY[…])`. Symptom: the condition sits in `Join Filter` instead of the inner `Index Cond`. Rewrite so the raw inner column is the equality target.

⚠️ **`SET enable_nestloop = off` does not disable BNL on 2.21+.** BNL became its own join strategy and its dependency on `enable_nestloop` was removed — that flag now governs only the classic unbatched join. So `YB Batched Nested Loop Join` nodes surviving `enable_nestloop = off` is expected, and that flag is not a valid way to test "without nested loops" on YB (use `yb_bnl_batch_size = 1`).

**Prefer batching over Hash Join.** A Hash Join reads the entire inner relation across all its tablets to build the hash table; a batched index lookup fetches only the keys needed. Hash Join is still the right answer when the inner side is a small lookup/dimension table scanned once and probed by a large outer side.

---

## `IN` / `= ANY(...)` with `ORDER BY` — SAOP merge

A `WHERE col IN ($1,$2,…)` (a ScalarArrayOpExpr / "SAOP") combined with `ORDER BY col LIMIT n` can be served straight from an index in sorted order — but only when YugabyteDB merges the per-element index scans into one ordered stream. Without that merge the planner runs the array elements as separate scans and adds a `Sort` (or over-reads to satisfy the `LIMIT`). **Fingerprint:** `EXPLAIN` shows a `Sort` above an index scan on the `IN` column, on a query that supplies an `IN`/`= ANY` list and an `ORDER BY` on the same column.

```sql
SET yb_enable_cbo = on;
SET yb_max_saop_merge_streams = 64;   -- merge up to N array elements into one ordered index scan
SET yb_enable_derived_saops = true;
```
These are the CBO GUCs above — this is the specific anti-pattern they remove. Promote with `ALTER DATABASE … SET …` once confirmed on a session.

---

## GIN index chosen (or skipped) wrongly

GIN indexes (`jsonb`, array, full-text, `pg_trgm`) do not yet have a complete cost model, so the planner can mis-price them — choosing a GIN scan where a btree/range index is cheaper, or ignoring a GIN index that would help. If `EXPLAIN` shows a surprising GIN choice (or refusal) on a hot query, force the intended path rather than fighting the estimate:

```sql
/*+ IndexScan(t t_btree_idx) */     -- force the btree;  or  /*+ NoIndexScan(t t_gin_idx) */
SELECT … ;
```
Persist via `hint_plan.hints` (above) if it recurs. Always re-test after `ANALYZE` first — stale statistics amplify the mis-costing and may be the real cause.
