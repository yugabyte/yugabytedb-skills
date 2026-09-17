# pg_stat_statements analysis

`pg_stat_statements` is the primary SQL-layer performance tool — one row per normalized query, accumulating across all executions since last reset. YugabyteDB extends the standard PostgreSQL view with DocDB-layer RPC counts, row-scan metrics, a latency histogram, and retry counters.

## Enabling DocDB columns

The DocDB-specific columns (`docdb_*`) are present in the schema but populated only when enabled. This is a runtime change — no restart required:

```sql
-- Enable for the current session
SET yb_enable_pg_stat_statements_rpc_stats = true;

-- Enable cluster-wide (persists across connections, no restart)
ALTER DATABASE yugabyte SET yb_enable_pg_stat_statements_rpc_stats = true;
```

On Aeon: this is a GUC, not a GFlag, so it is self-service via `SET` or `ALTER DATABASE`. Verify: `SHOW yb_enable_pg_stat_statements_rpc_stats;`

## Column reference

**Standard columns (always present):**

| Column | Notes |
|---|---|
| `queryid` | Normalized query fingerprint — use to join with ASH (`query_id`), hint_plan.hints, and yb_query_diagnostics |
| `query` | Normalized text with `$1`, `$2` placeholders |
| `calls` | Total executions since last reset |
| `mean_exec_time` | Mean execution time in ms (column named `mean_time` on older builds pre-2.18) |
| `total_exec_time` | Cumulative ms |
| `min_exec_time` / `max_exec_time` | Best/worst single execution |
| `rows` | Total rows returned or affected |

**YugabyteDB-specific columns:**

| Column | Notes |
|---|---|
| `yb_latency_histogram` | JSONB bucketed latency distribution — use `yb_get_percentile()` for P50/P90/P99 |
| `docdb_rows_scanned` | Rows read at DocDB storage layer (requires rpc_stats enabled) |
| `docdb_rows_returned` | Rows passed back up from DocDB (requires rpc_stats enabled) |
| `docdb_read_rpcs` | DocDB read RPC round-trips per query total (requires rpc_stats enabled) |
| `docdb_write_rpcs` | DocDB write RPC round-trips per query total (requires rpc_stats enabled) |
| `catalog_wait_time` | Total ms waiting on catalog/metadata operations |
| `conflict_retries` | Transaction conflict retry count |
| `read_restart_retries` | Read restart retry count (clock skew, MVCC) |
| `total_retries` | All retries combined |

## Core analysis queries

> **First, run the one-shot triage:** [`triage-snapshot.sql`](triage-snapshot.sql) bundles the highest-value checks below (workload concentration, scan ratio, retries, tail latency, sequence cache, stale stats, connections) into a single read-only pass. Run it first; the queries below are for drilling into whatever it flags. Don't skip the triage and hand-write an EXPLAIN — the snapshot tells you *which* query to EXPLAIN.

The individual queries below are organised so you can go deeper on any one triage signal. Workload concentration (by cumulative `total_exec_time`) and scan ratio are the two that catch the most problems; start there unless the triage points elsewhere.

### 0. Sequence hotspot detection (INSERT-heavy workloads)

Run this **before** any other analysis when the workload includes INSERTs or UPSERTs. Sequence CACHE 1 is a frequent YugabyteDB-specific bottleneck that does not appear in EXPLAIN plans and is invisible to scan-ratio analysis — it shows up only as elevated mean INSERT time and high `docdb_write_rpcs` per call.

```sql
-- Check sequence cache settings for all sequences in this database
SELECT sequencename, cache_size
FROM pg_sequences
ORDER BY cache_size ASC;
-- CACHE 1 = one RPC per nextval(); every INSERT to a table with a DEFAULT nextval()
-- serialises on the sequence tablet leader under concurrent load.
-- CACHE 100 or higher amortises the RPC cost across many values.
```

If any sequence has `cache_size = 1` and is used as a DEFAULT on a table that receives concurrent inserts, flag it immediately:

```sql
-- Confirm which tables use the low-cache sequence
SELECT a.attrelid::regclass AS table_name,
       a.attname            AS column_name,
       pg_get_expr(ad.adbin, ad.adrelid) AS default_expr
FROM pg_attrdef ad
JOIN pg_attribute a ON a.attrelid = ad.adrelid AND a.attnum = ad.adnum
WHERE pg_get_expr(ad.adbin, ad.adrelid) ILIKE '%nextval%';
```

Corroborate via INSERT latency in pgss — sequence CACHE 1 produces high `mean_exec_time` on INSERT statements with relatively few rows:

```sql
SELECT queryid,
       left(query, 100) AS query,
       calls,
       round(mean_exec_time::numeric, 2) AS mean_ms,
       round(docdb_write_rpcs::numeric / NULLIF(calls, 0), 1) AS write_rpcs_per_call
FROM pg_stat_statements
WHERE query ILIKE '%INSERT%'
  AND calls > 10
ORDER BY mean_exec_time DESC
LIMIT 10;
```

**Signal:** `write_rpcs_per_call > 2` on a single-row INSERT, combined with `cache_size = 1`, confirms the sequence hotspot. The fix:

```sql
ALTER SEQUENCE <seq_name> CACHE 100;
-- Or set cluster-wide minimum (no restart on YugabyteDB):
-- yb-ts-cli --tserver_flags ysql_sequence_cache_minval=100
-- Or via GFlag ysql_sequence_cache_minval (restart required if set at startup)
```

> **Note on CACHE state:** `pg_sequences.cache_size` reflects the *current DDL state*, not the running workload. Always check this **before** the workload runs — a previous AI or DBA session may have already altered the sequence. If INSERT latency looks normal but you suspect a prior CACHE 1 problem, look for monotonically-incrementing IDs that start far above 1 (wasted cache blocks from restarts under CACHE 1) or check `pg_stat_activity` during a live load for sessions blocked on the sequence tablet.

### 1. Top queries by mean execution time
```sql
SELECT queryid,
       left(query, 100) AS query,
       calls,
       round(mean_exec_time::numeric, 2)  AS mean_ms,
       round(total_exec_time::numeric, 2) AS total_ms,
       round(max_exec_time::numeric, 2)   AS max_ms
FROM pg_stat_statements
WHERE calls > 10
ORDER BY mean_exec_time DESC
LIMIT 20;
```

### 2. P50 / P99 latency (requires yb_latency_histogram)
```sql
SELECT queryid,
       left(query, 100) AS query,
       calls,
       round(yb_get_percentile(yb_latency_histogram, 50)::numeric, 2)  AS p50_ms,
       round(yb_get_percentile(yb_latency_histogram, 99)::numeric, 2)  AS p99_ms,
       round((yb_get_percentile(yb_latency_histogram, 99) /
              NULLIF(yb_get_percentile(yb_latency_histogram, 50), 0))::numeric, 1) AS p99_p50_ratio
FROM pg_stat_statements
WHERE calls > 100 AND yb_latency_histogram IS NOT NULL
ORDER BY p99_ms DESC NULLS LAST
LIMIT 20;
```
**Interpret:** `p99_p50_ratio > 10` means occasional very slow executions — typical of conflict retries or hot-shard hits. A ratio near 1 means consistent performance. High P99 with low mean = tail latency problem, not throughput.

### 3. DocDB scan efficiency (requires rpc_stats enabled)
```sql
SELECT queryid,
       left(query, 100) AS query,
       calls,
       docdb_rows_scanned,
       docdb_rows_returned,
       round((docdb_rows_scanned::numeric / NULLIF(docdb_rows_returned, 0)), 1) AS scan_ratio,
       round(docdb_read_rpcs::numeric / NULLIF(calls, 0), 1)                    AS rpcs_per_call
FROM pg_stat_statements
WHERE docdb_rows_scanned > 0 AND calls > 10
ORDER BY scan_ratio DESC NULLS LAST
LIMIT 20;
```
**Interpret:**
- `scan_ratio = 1` → ideal; storage returning exactly what is needed (covering index, good predicate push-down)
- `scan_ratio 2–10` → some filtering at storage — check for partial indexes or tighten predicates
- `scan_ratio > 10` → table/index scan far exceeds rows needed; strong indicator of missing or unused index
- `scan_ratio > 1000` on a query that returns few rows → an index almost certainly **exists but is being bypassed**. This is the classic "indexes exist but it's still slow" case.
- `rpcs_per_call > 2` → multiple DocDB round-trips per query; often a missing covering index forcing a heap fetch (2 RPCs: index + table) or an N+1 pattern (many parameterized child queries each doing 1 RPC). See [Section 8](#8-cross-query-n1--chatty-client-detection) for the cross-query call-count check — the per-query scan ratio alone does not reveal N+1.

**When `scan_ratio` is huge but a matching index exists — the usual causes (check the query text, not the schema):**
- **Function wrapper on the indexed column**: `WHERE lower(email) = ...` cannot use a plain index on `email`. Fix with a matching expression index — `CREATE INDEX ON t (lower(email))` — or remove the wrapper.
- **Type cast on the indexed column**: `WHERE account_no::bigint = $1` (or an implicit cast from a mismatched bind parameter type) defeats the index on `account_no`. Fix the column/parameter types so no cast lands on the column, or index the cast expression.
- **Leading-column mismatch on a composite/HASH index**: `(region HASH, created_at)` cannot serve a `created_at`-only predicate — the hash-partitioned leading column needs an equality predicate first.
- **`LIKE 'prefix%'` against a HASH index**: HASH indexes serve equality only; range/prefix needs an ASC/DESC (range) index.
- **HASH-sharded partial index asked to serve an `ORDER BY`/range**: a partial index (`… WHERE status = 'active'`) with a HASH leading key still serves equality only — the partial predicate narrows the set but can't supply ordering. Give the partial index a range (ASC/DESC) key. Its small row set may also land on one tablet — confirm any resulting write hotspot in [`yb-metrics-analysis`](../../yb-metrics-analysis/references/issue-hotspots.md).

The decisive move is to look at the **predicate as written in `pg_stat_statements.query`** for the high-ratio row, not at the table's index list. The index can be perfect and still unused because the predicate doesn't match its expression.

> **Metrics-layer corroboration — rows/calls vs seeks/nexts.** `rows / calls` establishes how many rows each query *should* produce; the per-table `rocksdb_number_db_seek`/`rocksdb_number_db_next` rates show what the storage layer is actually doing to produce them. A `SELECT … WHERE column = …` whose table shows a **high next count** is scanning the *table*, not an index — a missing/unused index even before the scan-ratio math. Sustained high `rocksdb_number_db_prev` is its own flag (backwards iteration that wants a `DESC` index). See the [`yb-metrics-analysis` workload sweep §8.2/§9](../../yb-metrics-analysis/references/workload-sweep.md) for the metrics side of this cross-check.

**Zero-cardinality index — an index that exists but provides no benefit:**
If an index exists on a column yet the query still scans all rows, check whether the column actually has distinct values:
```sql
SELECT n_distinct, null_frac, most_common_vals
FROM pg_stats
WHERE schemaname = 'public' AND tablename = '<table>' AND attname = '<column>';
```
`n_distinct = 1` (or close to 1) means nearly every row has the same value — the index has no selectivity and the planner correctly avoids it. A common cause: bulk-inserting all rows in **a single transaction** with a `DEFAULT now()` timestamp column. `now()` returns the transaction start time, so every row gets the identical value. Fix: change the column default to `clock_timestamp()` (wall clock at insert time, not transaction start time) and reload the data.

> **Time-series / monotonic-key PK — read vs write tradeoff (don't invert it).** For an append-heavy table, a **range** PK that *leads* with a monotonically increasing column (`(event_ts ASC, …)`, a bigserial, UUIDv7) funnels every insert to the single tail tablet — a **write hotspot** that caps ingest no matter how many nodes you have. The fix is to **hash on a dimension** and keep time as the secondary sort: `PRIMARY KEY (device_id HASH, event_ts ASC)` — writes fan out across tablets, each device's series stays range-scannable. The tradeoff: a `device_id`-only *range* (`BETWEEN`) or a global `event_ts`-only range scan then needs a secondary index. **Do not "fix" a `(device_id HASH, event_ts ASC)` table by reverting it to a leading monotonic range key** — that reintroduces the write hotspot. Weigh write distribution (usually dominant for ingest) against the specific range-scan pattern; see the [`ysql`](../../ysql/SKILL.md) skill → "Sharding: Hash vs Range" and the [`yb-metrics-analysis` hotspot playbook](../../yb-metrics-analysis/references/issue-hotspots.md).

> **Hash-key value skew — a HASH key that still hotspots.** Hash sharding distributes by `yb_hash_code` of the hash column(s), so the spread is only as even as the *values*. Three variants concentrate rows (and traffic) on one tablet despite a HASH key: a **high-frequency value** (one dominant key — a "celebrity" tenant/account/status), a **NULL-heavy** hash column (every NULL hashes identically), or a **low-cardinality leading column** whose few values let a monotonic secondary column recreate a tail hotspot within each bucket. Detect at the SQL layer with `pg_stats` on the hash column — `most_common_vals`/`most_common_freqs` (a dominant value), `null_frac` (→ 1), `n_distinct` (low) — and confirm the resulting hot tablet in [`yb-metrics-analysis`](../../yb-metrics-analysis/references/issue-hotspots.md). Fix: add entropy to the hash key (make it composite with a higher-cardinality column), or exclude/relocate the dominant value (e.g. a partial index that omits it). Schema redesign is owned by [`ysql`](../../ysql/SKILL.md).

### 4. Transaction conflict and retry analysis
```sql
SELECT queryid,
       left(query, 100) AS query,
       calls,
       conflict_retries,
       read_restart_retries,
       round(conflict_retries::numeric   / NULLIF(calls, 0), 3) AS conflict_rate,
       round(read_restart_retries::numeric / NULLIF(calls, 0), 3) AS restart_rate
FROM pg_stat_statements
WHERE (conflict_retries > 0 OR read_restart_retries > 0) AND calls > 10
ORDER BY conflict_retries DESC
LIMIT 20;
```
**Interpret:** `conflict_rate > 0.1` (more than 10% of calls retry at least once) is worth investigating. High `conflict_retries` → write-write contention; high `read_restart_retries` → clock skew or long-running read snapshots. See `contention.md` for next steps.

### 5. Total workload breakdown (what is consuming the most cumulative time)
```sql
SELECT queryid,
       left(query, 100) AS query,
       calls,
       round(total_exec_time::numeric, 0)  AS total_ms,
       round(mean_exec_time::numeric, 2)   AS mean_ms,
       round(100.0 * total_exec_time / sum(total_exec_time) OVER (), 1) AS pct_total
FROM pg_stat_statements
WHERE calls > 5
ORDER BY total_exec_time DESC
LIMIT 20;
```
Use this alongside mean-time ranking — a query called 1M times with 2ms mean can consume more total time than a 200ms query called 1K times.

## Second-tier checks — real issues the scan-ratio ranking misses

The scan-amplification query (#3) finds the biggest offenders, but several common, real problems produce only a *modest* scan ratio (≈1–2) and so never rise to the top of that list. **Run these before concluding an assessment** — they are exactly what a triage-by-scan-ratio pass overlooks. Surface them as a secondary tier in your report (see "Assessment discipline" rule 3 in SKILL.md).

### 6. Over-fetch — `SELECT *` / wide-column projection
A query whose predicate is fine (scan ratio ≈1) but which hauls back large columns it doesn't need (a `text`/`jsonb` blob, etc.). The cost is bytes moved per row, not rows scanned, so it hides from scan-ratio ranking.
```sql
SELECT queryid,
       left(query, 90) AS query,
       calls,
       rows,
       round(mean_exec_time::numeric, 2)                              AS mean_ms,
       round(docdb_rows_scanned::numeric / GREATEST(rows,1), 1)       AS scan_ratio,
       round(docdb_read_rpcs::numeric / NULLIF(calls,0), 1)           AS rpcs_per_call
FROM pg_stat_statements
WHERE query ILIKE 'SELECT %*%FROM%'        -- SELECT * shapes
  AND calls > 5
ORDER BY mean_exec_time DESC
LIMIT 15;
```
**Interpret:** a `SELECT *` with a low `scan_ratio` but high `mean_ms` (relative to a projected-column version of the same query) is dragging unneeded columns. Confirm by checking the table for wide columns (`avg_width` is in bytes):
```sql
SELECT attname, avg_width, n_distinct
FROM pg_stats
WHERE schemaname = 'public' AND tablename = '<table>'
ORDER BY avg_width DESC NULLS LAST
LIMIT 10;
```
A column with a large `avg_width` (a `text`/`jsonb` blob of hundreds of bytes) sitting at the top is the over-fetch culprit — every `SELECT *` row pays that cost. **Fix:** project only needed columns; for a large, rarely-read column, vertical-partition it into a side table keyed by the same PK and join only when required.

### 7. Missing range/sort index — full scan + in-memory sort
A `WHERE col > …` / `BETWEEN` / `ORDER BY col LIMIT n` that does a `Seq Scan` then sorts. The scan ratio may look unremarkable (the query genuinely touches many rows) but it's still a full scan where a range (ASC/DESC) index gives an index scan and avoids the sort.
```sql
SELECT queryid,
       left(query, 90) AS query,
       calls,
       rows,
       round(mean_exec_time::numeric, 2)  AS mean_ms,
       round(total_exec_time::numeric, 0) AS total_ms
FROM pg_stat_statements
WHERE (query ILIKE '%ORDER BY%LIMIT%'
       OR query ~* 'WHERE[^;]*(>|<|>=|<=|BETWEEN)')
  AND calls > 5
ORDER BY total_exec_time DESC
LIMIT 15;
```
**Confirm** the suspects with `EXPLAIN (ANALYZE, DIST, COSTS OFF)` — look for `Seq Scan` followed by `Sort` (often with `Sort Method: external merge  Disk: …` if it spills). **Fix:** add a range index on the sort/range column, e.g. `CREATE INDEX ON t (created_at DESC)`; add `INCLUDE (…)` covering columns to also kill any heap fetch. Remember a HASH-prefixed composite index (`(region HASH, created_at DESC)`) cannot serve a `created_at`-only range/sort — it needs an equality predicate on the hash column first.

### 8. Cross-query N+1 / chatty-client detection

The N+1 pattern is **invisible to per-query analysis**. Each child query is individually fast (sub-millisecond is common), so it never rises to the top of `mean_exec_time` or even `total_exec_time` rankings when inspected in isolation. The signal is a **call-count ratio across two queries**: a parent query that returns N rows, followed by N individual child lookups parameterized on values from the parent result.

**Run this after the workload breakdown (section 5) to check for call-count outliers:**
```sql
-- Rank all queries by call count; look for large gaps between adjacent rows
SELECT
    left(query, 100)                                                    AS query,
    calls,
    round(mean_exec_time::numeric, 3)                                   AS mean_ms,
    round(total_exec_time::numeric, 0)                                  AS total_ms,
    round(docdb_read_rpcs::numeric / NULLIF(calls, 0), 1)               AS rpcs_per_call
FROM pg_stat_statements
WHERE calls > 2
  AND query NOT ILIKE '%pg_stat_statements%'   -- exclude self-referential monitoring queries
  AND query NOT ILIKE '%pg_type%'              -- exclude driver type-loading (see note below)
ORDER BY calls DESC
LIMIT 30;
```

**What to look for:**
- One query has `calls` significantly higher than another related query — a **ratio > 10× warrants checking**; ratios of 50–500× are a strong N+1 signal.
- The high-call query has a parameterized lookup shape: `WHERE table.fk_col = $1`
- The WHERE clause value type matches a column in the SELECT list of the low-call "parent" query

**Confirming the relationship — compute the ratio explicitly:**
```sql
-- Example: compare parent and child call counts side-by-side
SELECT
    left(query, 80) AS query,
    calls,
    round(100.0 * calls / max(calls) OVER (), 1) AS pct_max_calls,
    round(mean_exec_time::numeric, 3) AS mean_ms,
    round(total_exec_time::numeric, 0) AS total_ms
FROM pg_stat_statements
WHERE calls > 2
ORDER BY calls DESC
LIMIT 20;
```

**Classic N+1 fingerprint in pgss:**
```
Parent: SELECT order_id FROM orders ORDER BY placed_at DESC LIMIT 500
        → calls=2,  mean_ms=140,  total_ms=280

Child:  SELECT item_id, sku, qty FROM order_items WHERE order_id = $1
        → calls=500, mean_ms=0.5,  total_ms=255

Ratio:  500 / 2 = 250  ← N+1 (N=500 per parent execution)
Total work hidden by mean: 250ms child work vs 140ms parent — the "fast" query is actually the bottleneck
```

**Fix: replace N child queries with one batch fetch**
```sql
-- Option A: JOIN (one query for parent + all children)
SELECT o.order_id, oi.item_id, oi.sku, oi.qty
FROM orders o
JOIN order_items oi ON oi.order_id = o.order_id
ORDER BY o.placed_at DESC
LIMIT 500;

-- Option B: batch lookup — collect parent IDs, then one parameterized batch
SELECT item_id, sku, qty
FROM order_items
WHERE order_id = ANY($1::bigint[]);
-- Pass all N order_ids from the parent query as a single array parameter
```

**Covering index + N+1 is a two-level problem.** If the child query also has `rpcs_per_call = 2` (missing covering index), fix *both*: eliminate the N+1 first (one batch query), then add `INCLUDE (sku, qty)` to the index so the batch query itself is an Index Only Scan.

---

**Driver type-loading / connection-pooling variant (looks similar, different fix):**

A catalog query `SELECT t.oid, t.typname, t.typelem ... FROM pg_type ...` appearing in pgss with very high `calls` is the pgjdbc (or psycopg2) type-cache query — it fires once per **new physical connection**. It is not an N+1; the fix is not batching.

```sql
-- Detect driver type-loading storm: pg_type calls >> application query calls
SELECT
    left(query, 60) AS query,
    calls,
    round(mean_exec_time::numeric, 1) AS mean_ms,
    round(total_exec_time::numeric, 0) AS total_ms,
    round(100.0 * total_exec_time / sum(total_exec_time) OVER (), 1) AS pct_total
FROM pg_stat_statements
ORDER BY calls DESC LIMIT 5;
```

**Signal:** `pg_type` in top-3 by `calls` with `total_exec_time` > 20% of all DB time. Each new connection = one type-load query. `calls / application_query_calls` ≈ new-connections-per-query-call (should be ~0 with pooling, not ~10).

**Fix:** deploy connection pooling — YSQL Connection Manager (`enable_ysql_conn_mgr=true` GFlag, built into YBA-managed clusters), PgBouncer in transaction mode, or HikariCP/c3p0 in the application. With a pool of long-lived connections, `pg_type` drops to near-zero calls.

> **⚠️ Do not let driver/catalog noise become "the primary finding" and crowd out the schema-layer issue.** This is a real failure mode: on a short, low-volume capture (a benchmark warm-up, a freshly-reset `pg_stat_statements`, a connection-churny test harness), the one-off catalog/type-load queries (`pg_type`, `pg_attribute`, `version()`, composite-type loads) can legitimately add up to **50–70 % of total recorded time** — simply because little *application* work has accumulated yet. That percentage is an **environment/configuration signal** (no pooling), **not** an application query anti-pattern. Report it as a connection-pooling recommendation, then **explicitly set these catalog/driver queries aside and re-rank the remaining *application* queries** — by scan ratio (#3), retries (#4), and call-count ratio (#8) — to find the data-/schema-layer problem the scenario is actually about. A connection-pooling fix and a missing-index/contention fix are different layers; finding the first does **not** mean you have found the second. If after excluding the catalog noise an application query still has a high scan ratio, an unindexed foreign-key column, a retry rate, or an N+1 ratio, **that** is the headline, and the pooling note is secondary.

To re-rank application work with the catalog/driver noise excluded:
```sql
SELECT left(query, 80) AS query, calls,
       round(mean_exec_time::numeric, 2)  AS mean_ms,
       round(total_exec_time::numeric, 0) AS total_ms,
       round(docdb_rows_scanned::numeric / GREATEST(rows,1), 1) AS scan_ratio,
       conflict_retries
FROM pg_stat_statements
WHERE calls > 5
  AND query NOT ILIKE '%pg_type%'
  AND query NOT ILIKE '%pg_attribute%'
  AND query NOT ILIKE '%pg_catalog%'
  AND query NOT ILIKE '%information_schema%'
  AND query NOT ILIKE '%version()%'
  AND query NOT ILIKE '%pg_stat_statements%'
ORDER BY total_exec_time DESC
LIMIT 15;
```

---

**Diagnostic query isolation:**
When running `COUNT(*)`, `ANALYZE`, `EXPLAIN`, or other investigation commands, those statements appear in pgss and can be confused with the application workload. Before flagging a query for high call count or high scan ratio, confirm it originated from application traffic, not from your own diagnostic session. Queries you ran during the investigation will match your `pg_stat_statements.userid`.

### 9. Unindexed foreign-key column — a latent issue pgss ranking won't surface

A foreign key whose **child** (referencing) column has no index is a real anti-pattern, but it is **invisible to a workload that only inserts** — it bites later, on `DELETE`/`UPDATE` of a *parent* row (each one full-scans the child to enforce the constraint) and on any join filtering by the FK column. So the capture window may show nothing alarming (low scan ratio, low time) even though the schema is wrong. This is a schema check, not a pgss-ranking check — **run it during any assessment regardless of what the time/scan ranking shows**, and report a missing child index as a finding on its own merits (see "Assessment discipline" rule 2 in SKILL.md — every real issue is reported, not only the headline).

```sql
-- Foreign keys whose leading referencing column has no matching index on the child table
SELECT c.conrelid::regclass AS child_table,
       a.attname           AS fk_column,
       confrelid::regclass AS parent_table
FROM pg_constraint c
JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
WHERE c.contype = 'f'
  AND NOT EXISTS (
    SELECT 1 FROM pg_index i
    WHERE i.indrelid = c.conrelid
      AND i.indkey[0] = c.conkey[1]      -- FK column is the leading index column
  )
ORDER BY child_table;
```
**Any row here = a child table that will full-scan on parent delete/update and on FK-column joins.** Fix: `CREATE INDEX ON <child_table> (<fk_column>);` (plain index — the child column does not need to mirror the parent's HASH/RANGE choice; an index that *leads* with the FK column is what the constraint check and the join both need). Weigh it by the real workload: critical if parents are ever deleted/updated or joined on the FK; lower priority for an append-only child, but still worth flagging.

### 10. Unbatched writes — a write RPC per row

Writes that should be batched cost one DocDB round-trip per row instead of amortising them across a set. This is a *write*-path cost, so scan-ratio analysis misses it — and it has **two distinct signatures that need two different checks**. Run both; the second is the one most people miss.

**(a) One statement that fans out to many write RPCs** — a multi-row statement whose operations aren't buffered, or a single write touching many rows/tablets. Signal: high `docdb_write_rpcs` *per call* relative to rows written.

```sql
SELECT queryid, left(query, 80) AS query, calls, rows,
       round(mean_exec_time::numeric, 2)                    AS mean_ms,
       round(docdb_write_rpcs::numeric / NULLIF(calls,0), 1) AS write_rpcs_per_call
FROM pg_stat_statements
WHERE docdb_write_rpcs > 0
  AND query ~* '^\s*(INSERT|UPDATE|DELETE)'
  AND calls > 5
ORDER BY (docdb_write_rpcs::numeric / NULLIF(calls,0)) DESC
LIMIT 15;
```

**(b) Many single-row statements run in a loop** — the `BEGIN; insert…on conflict; insert…on conflict; …; COMMIT;` pattern. Each call is only ~1 RPC, so query (a) **will not flag it**. The giveaway is **call count, not per-call RPCs**: all N statements collapse to one normalized `queryid` with very high `calls`, ~1 `rows` per call, and a low mean — the write-path analogue of the §8 call-count check. Rank write shapes by volume:

```sql
SELECT queryid, left(query, 80) AS query, calls,
       round(rows::numeric / NULLIF(calls,0), 1)             AS rows_per_call,
       round(total_exec_time::numeric, 0)                    AS total_ms,
       round(mean_exec_time::numeric, 3)                     AS mean_ms,
       round(docdb_write_rpcs::numeric / NULLIF(calls,0), 1) AS write_rpcs_per_call
FROM pg_stat_statements
WHERE query ~* '^\s*(INSERT|UPDATE|DELETE)'
  AND calls > 100
ORDER BY calls DESC
LIMIT 15;
```
A single-row write shape (`rows_per_call ≈ 1`) with high `calls` and a large share of `total_ms` is a per-row write loop — every row paid a separate client↔server round-trip even inside one transaction. `INSERT … ON CONFLICT DO UPDATE` is the common form; its conflict-check read makes each round-trip a read-modify-write, so N statements = N read-write RPCs. A `BEGIN; COMMIT;` pair count ≈ `calls / N` (one transaction per N rows) confirms the loop.

> **Do NOT clear this as "efficient" because the per-call mean is low — that is exactly the trap.** A single-row upsert at `mean_exec_time` ≈ 0.5 ms called 4 million times is **~2,000 s of cumulative DocDB work** and routinely the #1 or #2 consumer of `total_exec_time` on the node. Judge a write loop by **`total_exec_time` share and `calls`, never by per-call mean** — `mean_exec_time` ranking and `write_rpcs_per_call` both make it look fine. The moment you see a single-row `INSERT … ON CONFLICT` / `UPDATE` with `calls` in the millions and a top `total_ms` share, flag it as unbatched writes and recommend the multi-row rewrite below, regardless of how small the mean looks. Calling it "efficient" is the single most common mistake on this pattern.

**Fix (both cases):**
- Collapse N single-row writes into **one multi-row statement** — `INSERT … VALUES (…),(…),…`, or `INSERT … SELECT * FROM unnest($1::…[], …)`, adding `ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v` for upserts. N round-trips → 1.
- If a multi-row statement *still* shows `write_rpcs_per_call` ≈ rows → operations aren't being buffered to DocDB; raise `ysql_session_max_batch_size` or check the rows aren't being forced into separate flushes.
- If the rows don't need to be atomic *together*, drop the explicit `BEGIN/COMMIT` as well — one autocommit multi-row statement also sheds the distributed-transaction overhead. If they do, the single multi-row statement is still atomic.
- Rule out the **sequence hotspot** (section 0): high write RPCs on a single-row INSERT backed by a `CACHE 1` sequence is *that* problem, not statement batching.
- In a **multi-region** deployment each round-trip also pays the cross-region RTT — see section 13.

### 11. DocDB read amplification from tombstone / version churn

A query that is **selective and uses a matching index yet has a `mean_exec_time` many times higher than a single-row op should cost** may be stepping over **dead versions / tombstones** rather than doing real work. Two causes:
- **Queue / soft-delete tables:** heavy `DELETE`/status-`UPDATE` churn leaves tombstones a scan must seek past to reach live rows — the classic "job queue that degrades." A `… ORDER BY <key> LIMIT 1` head-peek or a single-row `DELETE` of the head walks every tombstone ahead of the first live row.
- **Wide-row updates:** updating many columns writes a new row version each time; reads then seek past the stale versions.

> **The signal is LATENCY, not scan amplification — this is a YugabyteDB-specific subtlety.** Tombstones and obsolete MVCC versions are stepped over **inside the RocksDB iterator**; YugabyteDB does **not** count them in `docdb_rows_scanned`. So this pattern shows `scan_ratio` (`docdb_rows_scanned`/`docdb_rows_returned`) **≈ 1** — the scan-amplification check (section 3) and the triage snapshot will **not** flag it. The fingerprint is instead: a trivially selective query (PK-ordered `LIMIT 1`, single-row `DELETE`) with a **high `mean_exec_time`**, **`scan_ratio ≈ 1`**, and **zero `conflict_retries`/`read_restart_retries`** (rules out contention). Rank queue/selective ops by latency, not scan ratio:

```sql
-- Selective single-row-ish ops that are unexpectedly slow with NO scan amplification and NO retries
SELECT queryid, left(query, 80) AS query, calls,
       round(rows::numeric / NULLIF(calls,0), 1)                          AS rows_per_call,
       round(docdb_rows_scanned::numeric / NULLIF(docdb_rows_returned,0),1) AS scan_ratio,
       round(mean_exec_time::numeric, 2)                                  AS mean_ms,
       conflict_retries, read_restart_retries
FROM pg_stat_statements
WHERE calls > 100
  AND (query ILIKE '%LIMIT%' OR query ~* '^\s*DELETE')
  AND conflict_retries = 0 AND read_restart_retries = 0
ORDER BY mean_exec_time DESC LIMIT 15;
```
A selective op (`rows_per_call` small, `scan_ratio ≈ 1`, no retries) with a `mean_ms` far above a point-read on a churned queue/soft-delete table is the tell. Confirm at the **metrics layer** (the decisive evidence): `rocksdb_number_db_next`/`rocksdb_number_db_seek` climbing on the table while `docdb_rows_scanned ≈ 1` and SST size is flat → [`yb-metrics-analysis`](../../yb-metrics-analysis/references/issue-disk-io.md) (seek-amplification). Note `EXPLAIN (ANALYZE, DIST)` run *after* the workload pauses may look clean — compaction reclaims the tombstones, so trust the pgss `mean_exec_time` accumulated during load over a one-shot EXPLAIN.

**Fix:** for queue tables, stop scanning the tombstoned head — `SELECT … FOR UPDATE SKIP LOCKED` plus a **partial index** on the "pending" predicate so workers seek straight to live rows, and let compaction reclaim tombstones (schedule it if needed); or move off the delete-from-head FIFO pattern. For wide-update churn, split the volatile columns into a side table and prefer a **stable natural key** for the PK/covering index — frequently-updated key columns multiply versions and prevent an efficient Index Only Scan. Schema redesign → [`ysql`](../../ysql/SKILL.md).

### 12. Deep `LIMIT … OFFSET` pagination

`OFFSET n` scans and discards the first `n` rows on every page, so page latency grows linearly with depth — page 1,000 reads ~1,000× the rows of page 1 for the same result size. Cheap at low offsets, expensive in the tail.

```sql
SELECT queryid, left(query, 90) AS query, calls,
       round(mean_exec_time::numeric, 2)  AS mean_ms,
       round(total_exec_time::numeric, 0) AS total_ms
FROM pg_stat_statements
WHERE query ~* 'OFFSET' AND calls > 5
ORDER BY total_exec_time DESC LIMIT 15;
```
**Fix:** switch to **keyset (seek) pagination** — carry the last row's sort key and use `WHERE (sort_col, id) > ($1, $2) ORDER BY sort_col, id LIMIT n` against a matching range index. Constant cost per page regardless of depth.

### 13. Multi-region statement latency — round-trips that should be pipelined

In a geo-distributed deployment each separately-issued statement pays the cross-region RTT to the tablet leader; a transaction that fires N sequential statements (a per-row insert loop, or read-then-write chains) multiplies that RTT by N. The pgss signal is high `mean_exec_time` on otherwise-trivial writes whose cost is *latency, not work* — `docdb_rows_scanned` and `write_rpcs_per_call` are low — typically inside a multi-statement transaction.

**Fix:** collapse the round-trips — batch writes into one multi-row statement (section 10); fold a read-then-write chain into a single **CTE** (`WITH upd AS (UPDATE … RETURNING …) INSERT … SELECT … FROM upd`); or co-locate the latency-critical tables' leaders in one region. Confirm the cross-region wait at the universe level via ASH topology ([`ash-analysis.md`](ash-analysis.md)) or the [`yb-metrics-analysis`](../../yb-metrics-analysis/references/issue-latency-throughput.md) latency playbook.

---

## EXPLAIN DIST — getting the execution plan for a candidate query

Once a query is identified via PGSS, get its plan with YugabyteDB's distributed metrics:

```sql
EXPLAIN (ANALYZE, DIST, COSTS OFF) <your_query_here>;
```

**Key DIST output fields:**

| Field | What it tells you |
|---|---|
| `Planning Time` | ms for planner to generate the plan — high (> 50ms) suggests catalog pressure |
| `Storage Read Execution Time` | ms waiting for DocDB read responses — the dominant source of latency for indexed reads |
| `Storage Table Read Requests` | RPC round-trips to TServer for table (heap) fetches — should be 0 for Index Only Scan |
| `Storage Index Read Requests` | RPC round-trips for index reads — 1 per query is normal; > 3 is high |
| `Storage Rows Scanned` | Rows examined at DocDB — should be close to rows returned for a good plan |
| `Catalog Read Requests` | Master metadata lookups per execution — high for complex DDL or many partitions |

**Plan node interpretation:**

| Node type | Meaning |
|---|---|
| `Seq Scan` on a large table | Missing index — check `docdb_rows_scanned >> rows` in PGSS |
| `Index Scan` | Using index but still fetching heap for non-covered columns |
| `Index Only Scan` | All needed columns in index — most efficient; no heap fetch |
| `YB Batched Nested Loop` | YugabyteDB distributed join — batches inner lookups; efficient for many-to-one joins. Confirm it's actually batching: inner `Index Cond` should read `= ANY (ARRAY[outer.col, $1, …])` with `loops=1` |
| `Nested Loop` (plain, **not** batched) | 🔴 One DocDB RPC per outer row — inner `Index Cond: (col = outer.col)` with `loops=N`. Common cause of "clean plan, slow query" on YB. See `references/query-tuning.md` → "Batched Nested Loop joins (BNL)" |
| `Hash Join` on large tables | Memory-intensive; reads the whole inner relation over RPC. If slow, check for missing index on join column — and consider whether a batched index lookup would beat it |

**Action:** `Seq Scan` on a large table → add index; `Index Scan` with high `Storage Table Read Requests` → add covering index with `INCLUDE`; plain `Nested Loop` with `loops` ≫ 1 → check `yb_bnl_batch_size` / `ANALYZE` the tables so BNL is chosen; high `Catalog Read Requests` → check partition count or DDL complexity.

## yb_query_diagnostics — deep-dive bundle

⚠️ **Available from YugabyteDB 2025.2 (EA). Requires a GFlag change + TServer restart. On Aeon: support-only.**

When PGSS and EXPLAIN together don't explain the slow behaviour (e.g. it's intermittent, bind-variable-dependent, or involves complex schema interactions), `yb_query_diagnostics` captures a bundle over a time window: bind variables, sampled EXPLAIN plans, schema details for referenced tables, PGSS snapshot, and ASH data.

**Enable (restart required):**
```
TServer GFlag: ysql_yb_enable_query_diagnostics = true
```

**Usage:**
```sql
-- Find the queryid
SELECT queryid, left(query, 100) FROM pg_stat_statements
WHERE query ILIKE '%orders%' ORDER BY mean_exec_time DESC LIMIT 5;

-- Start a 2-minute diagnostic bundle, capturing EXPLAIN for 10% of executions,
-- and bind variables for executions > 100ms
SELECT yb_query_diagnostics(
    query_id                       => <queryid>,
    diagnostics_interval_sec       => 120,
    explain_sample_rate            => 10,
    explain_analyze                => true,
    explain_dist                   => true,
    bind_var_query_min_duration_ms => 100
);

-- Check status (In Progress / Completed / Failed)
SELECT * FROM yb_query_diagnostics_status;

-- Cancel if needed
SELECT yb_cancel_query_diagnostics(query_id => <queryid>);
```

Output files written per-node to: `<pg_data>/query_diagnostics/<query_id>/<random>/`
Contains: `constants_and_bind_variables.csv`, `pg_stat_statements.csv`, `schema_details.txt`, `active_session_history.csv`, `explain_plan.txt`. Manual cleanup required — no auto-expiry.

**Limitations:** one bundle per queryid at a time; max 100 concurrent cluster-wide; explain plans > 16,384 chars not captured; per-node only (not aggregated).

## Resetting statistics

```sql
-- Reset all statistics (do this before a controlled load test, not during production incident)
SELECT pg_stat_statements_reset();

-- Reset for one query only (leaves all others intact)
SELECT pg_stat_statements_reset(0, 0, <queryid>);
```

## Paste-mode — what to ask the user to collect

When you don't have live database access, ask the user to run the following and paste the output. Include a note to enable DocDB columns first if they haven't:

```sql
-- Step 1: enable DocDB columns (no restart, safe to run)
SET yb_enable_pg_stat_statements_rpc_stats = true;

-- Step 2: top 50 queries by mean time with all diagnostic columns
SELECT queryid,
       left(query, 120) AS query,
       calls,
       round(mean_exec_time::numeric, 2)  AS mean_ms,
       round(total_exec_time::numeric, 0) AS total_ms,
       round(max_exec_time::numeric, 2)   AS max_ms,
       round(yb_get_percentile(yb_latency_histogram, 50)::numeric, 2)  AS p50_ms,
       round(yb_get_percentile(yb_latency_histogram, 99)::numeric, 2)  AS p99_ms,
       conflict_retries,
       read_restart_retries,
       docdb_rows_scanned,
       docdb_rows_returned,
       docdb_read_rpcs
FROM pg_stat_statements
WHERE calls > 10
ORDER BY mean_exec_time DESC
LIMIT 50;
```

If `docdb_*` columns return NULL, the rpc_stats flag wasn't applied — interpret without them and ask the user to re-run with `SET yb_enable_pg_stat_statements_rpc_stats = true` in the same session before the SELECT.

## Working from a CSV / file export (not a pre-ranked paste)

A handed-over file — `\copy pg_stat_statements TO '…csv' CSV HEADER`, a dashboard export, a query-diagnostics `pg_stat_statements.csv` — is **not** the tidy `LIMIT 50` paste above. It is the *whole* statements table in **arbitrary order** (insertion / `queryid` order), and on a busy node that is thousands to tens of thousands of rows. **Do not read it head-first.** Reading the top of the file analyses an arbitrary slice in non-cost order and will silently miss the most expensive queries — the exact mistake the rest of this skill exists to prevent. Two safe paths:

1. **Preferred — load into a scratch table and reuse the queries above.** Then every ranking and the DocDB-column reasoning applies unchanged:
   ```sql
   CREATE TEMP TABLE pgss_import (LIKE pg_stat_statements INCLUDING ALL);  -- or a column list matching the export header
   \copy pgss_import FROM '/path/export.csv' WITH (FORMAT csv, HEADER true);
   -- now run the scan-ratio (#3), retries (#4), call-count (#8) queries against pgss_import instead of pg_stat_statements
   ```
   A local PostgreSQL works just as well as the cluster — this is pure data, no YugabyteDB needed to *read* it.

2. **If reading the file directly** (no scratch DB available): get the size first (`wc -l`), then sort and take the top N on the columns that matter **before** loading any of it into context — never the file's own order. Rank on **both** `total_exec_time` (cumulative offenders) **and** `mean_exec_time` (per-call offenders), and keep the `docdb_rows_scanned`/`docdb_rows_returned` columns so you can still compute scan ratio. A single sort by one column is not enough — the cumulative-time leader and the per-call leader are usually different rows (see the "rank by impact, separate layers" discipline in `SKILL.md`).

Either way, the same interpretation rules apply once ranked: set aside catalog/driver noise (#8 warning), separate config-layer from schema-layer findings, and report every real issue in tiers — not just the headline.
