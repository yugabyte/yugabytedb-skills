# Assessment checklists

Two structured checklists for proactive database review. Each item names the tool, what to check, what a healthy result looks like, and which reference to read for depth. These checklists are deliberately thin — the analysis detail lives in the referenced files.

For infrastructure metrics (CPU, memory, disk, tablet hotspots) add `yb-metrics-analysis` to this assessment. For schema design review see the `ysql` skill.

---

## Pre-production / pre-go-live checklist

Run before launching a new application or promoting a migration to production. The goal is to surface performance problems when they are still cheap to fix.

### 1. Schema sign-off — `ysql` skill
- No monotonically increasing leading key (timestamp, BIGSERIAL, UUIDv7) on a range-sharded primary key or index without a bucket or hash prefix — causes hot tablet on writes
- All columns referenced in JOIN predicates have indexes (especially foreign key columns)
- High-frequency read queries use covering indexes (`INCLUDE` columns) to achieve Index Only Scan
- No speculative single-column indexes not aligned with real query shapes
- Sequences on high-ingest paths have `CACHE ≥ 100`

Run `EXPLAIN (ANALYZE, DIST)` on the top 5–10 most critical queries and verify no Seq Scan appears on any table that will have > 100K rows.

### 2. EXPLAIN DIST on all critical query paths — `pgss-analysis.md`

For each critical query:
```sql
EXPLAIN (ANALYZE, DIST, COSTS OFF) <critical_query_with_representative_values>;
```
**Healthy:** `Index Scan` or `Index Only Scan`; `Storage Rows Scanned ≈ rows returned`; `Storage Table Read Requests` = 0 (Index Only Scan) or ≤ 1; `Storage Index Read Requests` ≤ 2.

**Flag immediately:** `Seq Scan` on any table that will grow beyond development data; `Storage Read Requests > 5` per query; planning time > 50ms (catalog pressure, too many partitions).

### 3. Load test — run for at least 30 minutes with representative traffic

**During the test — PGSS scan ratio** (`pgss-analysis.md`):
```sql
SET yb_enable_pg_stat_statements_rpc_stats = true;
SELECT left(query, 80) AS query, calls,
       round(mean_exec_time::numeric, 2) AS mean_ms,
       round((docdb_rows_scanned::numeric / NULLIF(docdb_rows_returned, 0)), 1) AS scan_ratio,
       conflict_retries
FROM pg_stat_statements
WHERE calls > 20
ORDER BY mean_exec_time DESC LIMIT 20;
```
**Healthy:** `scan_ratio ≤ 2` for indexed queries; `conflict_retries` < 5% of calls.

**During the test — ASH wait breakdown** (`ash-analysis.md`):
```sql
SELECT wait_event_type, wait_event, count(*) AS samples,
       round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct
FROM yb_active_session_history
WHERE sample_time > now() - interval '5 minutes'
GROUP BY wait_event_type, wait_event ORDER BY samples DESC LIMIT 15;
```
**Healthy:** > 70% of samples are `Cpu / OnCpu_Active`. Flag `RPCWait / ConflictResolution_ResolveConflicts` > 10% (transaction conflict design issue) or `DiskIO` > 20% (working set exceeds cache — size up or add caching).

**After test — Performance Advisor / Aeon Insights:**
- YBA: Universes → Queries → Performance Advisor → Scan
- Aeon: cluster → Performance → Insights → Scan
- Resolves: unused indexes, hot shards, connection skew, CPU skew automatically

### 4. Statistics freshness — `query-tuning.md`
After any bulk data load, run `ANALYZE` before starting the load test:
```sql
ANALYZE;  -- full database; or ANALYZE <table> for each loaded table
```
Verify with `pg_class.reltuples` (cluster-wide; `-1` = never analyzed) that all loaded tables have statistics before performance testing — not `pg_stat_user_tables.last_analyze`, whose timestamp is node-local and reads NULL for tables analyzed on another node.

Auto Analyze was introduced in **2025.1** (EA) and is GA/on-by-default from **2025.2** (when CBO is on). On 2024.x or earlier it does not exist — manual `ANALYZE` after every major load is the only option. To verify it is enabled, check the **TServer flags** — `ysql_enable_auto_analyze` is a GFlag, **not** a GUC, so `SHOW ysql_enable_auto_analyze;` errors with *"unrecognized configuration parameter"* (validated on 2025.2.1). Read it from the tserver flags endpoint instead:
```bash
# Per node (tserver web UI, default port 9000): expect ysql_enable_auto_analyze = true
curl -s http://<tserver-host>:9000/api/v1/varz | \
  grep -oE '"ysql_enable_auto_analyze[a-z_]*"[^}]*' 
# On 2025.2+ the active flags are ysql_enable_auto_analyze (+ ysql_enable_auto_analyze_infra).
# The 2025.1 flags ysql_enable_auto_analyze_service / ysql_enable_table_mutation_counter
# are superseded and read false on 2025.2 — do not treat that as "disabled".
```
If it is off (2025.2+) or you are on 2025.1, raise it as a finding: the TServer GFlag `ysql_enable_auto_analyze = true` requires a restart to set. See `query-tuning.md` for the full configuration.

### 5. Connection planning — `contention.md`
```sql
-- Check current connection usage at peak test load
SELECT state, count(*) FROM pg_stat_activity WHERE backend_type = 'client backend' GROUP BY state;
```
**Rule of thumb:** if peak test connections approach **200 per TServer node** (out of the default 300 limit), plan to enable YSQL Connection Manager before production launch. See `ysql` skill.

### 6. Terminated queries — `contention.md`
Run at the end of the load test:
```sql
SELECT query_text, termination_reason, query_end_time
FROM yb_terminated_queries ORDER BY query_end_time DESC LIMIT 10;
```
Any `SIGKILL` = OOM kill; any temp file limit breach = sort/hash spill. Either is a production risk to address before launch.

### 7. Infrastructure metrics — `yb-metrics-analysis`
After the load test, check via YBA Metrics UI or `yb-metrics-analysis` skill:
- DocDB block cache hit ratio > 95%
- CPU headroom > 40% at peak test load
- No reactor delay spikes
- Raft replication lag < 1 second under load

---

## Periodic health check

Run this against production on a regular cadence — suggested monthly for steady-state workloads, weekly for fast-growing or frequently-changed schemas.

### 1. Unused indexes (monthly)

> **YB-safe: `idx_scan` is node-local — sum it across all nodes before calling an index unused.** `pg_stat_all_indexes.idx_scan` **is** populated in YugabyteDB (YBA Performance Advisor and Aeon Insights both use `idx_scan = 0`), but each node's stats collector only counts scans that ran through *that* node. Validated on RF3 2025.2: an index scan driven through node 3 showed `idx_scan = 1` on node 3 and `0` on nodes 1 and 2 — so a single connection reports a **false "unused"** for any index used only on other nodes. Aggregate across the cluster first.

The most reliable source is **YBA Performance Advisor / Aeon Insights**, which aggregate `idx_scan` cluster-wide centrally — prefer them when available. To do it in SQL, sum `idx_scan` over every endpoint from `yb_servers()`:
```bash
# Per-node idx_scan; an index is a drop candidate only if the SUM across nodes is 0.
for h in $(ysqlsh -tAc "SELECT host FROM yb_servers()"); do
  ysqlsh -h "$h" -tAF',' -c "
    SELECT s.indexrelname, s.idx_scan,
           pg_size_pretty(pg_table_size(s.indexrelid))   -- pg_table_size, NOT pg_relation_size (see note)
    FROM pg_stat_all_indexes s
    JOIN pg_index i ON i.indexrelid = s.indexrelid
    WHERE s.schemaname NOT IN ('pg_catalog','information_schema','pg_toast')
      AND NOT i.indisprimary AND NOT i.indisunique;"
done | awk -F',' '{scan[$1]+=$2; size[$1]=$3} END{for(i in scan) if(scan[i]==0) print i, size[i]}'
```
Or, if `dblink` is installed, fan the same query across `yb_servers()` and `sum(idx_scan) … having sum = 0` in one query. Single-node fallback (only valid on a 1-tserver/dev cluster):
```sql
-- Use pg_table_size (real DocDB size), NOT pg_relation_size — see note below.
SELECT schemaname, relname AS tablename, indexrelname AS indexname,
       idx_scan,
       pg_size_pretty(pg_table_size(indexrelid)) AS index_size
FROM pg_stat_all_indexes
WHERE schemaname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  AND idx_scan = 0
ORDER BY pg_table_size(indexrelid) DESC;
```
> **YB-safe sizing:** `pg_relation_size()` returns **0 bytes** for YugabyteDB relations (it measures the PostgreSQL heap fork, which DocDB tables don't have — validated on RF3 2025.2: a 300k-row index reported `0 bytes` via `pg_relation_size` but `15 MB` via `pg_table_size`). Always use **`pg_table_size()`** (or `pg_total_relation_size()`) for index/table sizes on YB; for authoritative on-disk sizes use the DocDB tablet metrics in [`yb-metrics-analysis`](../../yb-metrics-analysis/SKILL.md) or the tserver UI.
**Action:** An index whose `idx_scan` summed **across all nodes** is 0 has not been used since the last per-node stats reset (or node restart, which zeroes the counter locally). Confirm the workload window is representative, exclude PK/unique-constraint indexes, then consider dropping it — each index adds write overhead on every INSERT/UPDATE/DELETE. Note: `pg_stat_reset()` resets all statistics **on that node** — do not run before this check; if stats or any node were recently reset/restarted, wait for a representative cycle.

### 1b. Redundant / overlapping indexes (monthly)
Distinct from *unused* indexes: two indexes can both be scanned, yet one is **redundant** because its leading columns are a prefix of another — the wider index already serves it, and the narrower one is pure write overhead.
```sql
-- Candidate redundant pairs: same table, one index's column list is a left-prefix of another's
SELECT a.indrelid::regclass            AS table_name,
       a.indexrelid::regclass          AS narrower,
       b.indexrelid::regclass          AS wider
FROM pg_index a
JOIN pg_index b
  ON a.indrelid = b.indrelid
 AND a.indexrelid <> b.indexrelid
 AND a.indnkeyatts < b.indnkeyatts
 AND (string_to_array(b.indkey::text, ' '))[1:a.indnkeyatts] = string_to_array(a.indkey::text, ' ')   -- a's key columns are b's leading prefix
ORDER BY table_name;
```
**Action:** these are *candidates* — before dropping the narrower index confirm both share the same HASH/RANGE choice, opclass, and `WHERE` predicate (a partial or differently-sharded index is not redundant). Also look for the inverse opportunity: an index whose every query adds the same constant filter (`… WHERE status = 'active'`) can be replaced by a smaller **partial** index, cutting write and storage cost. Schema/DDL changes → [`ysql`](../../ysql/SKILL.md).

### 2. PGSS scan ratio (weekly for active databases) — `pgss-analysis.md`
```sql
SET yb_enable_pg_stat_statements_rpc_stats = true;
SELECT left(query, 80) AS query, calls,
       round((docdb_rows_scanned::numeric / NULLIF(docdb_rows_returned, 0)), 1) AS scan_ratio,
       round(mean_exec_time::numeric, 2) AS mean_ms
FROM pg_stat_statements
WHERE calls > 100 AND docdb_rows_scanned > 0
ORDER BY scan_ratio DESC LIMIT 20;
```
**Flag:** `scan_ratio > 10` with `calls > 100` = a frequently-run query with a significant missing index. Investigate with EXPLAIN DIST.

### 3. Top slow queries by mean time — `pgss-analysis.md`
```sql
SELECT left(query, 80) AS query, calls,
       round(mean_exec_time::numeric, 2) AS mean_ms,
       round(yb_get_percentile(yb_latency_histogram, 99)::numeric, 2) AS p99_ms
FROM pg_stat_statements WHERE calls > 50
ORDER BY mean_exec_time DESC LIMIT 10;
```
Compare against the previous cycle's baseline. Any query newly appearing in the top 10 or with mean time significantly increased warrants investigation.

### 4. Statistics staleness — `query-tuning.md`

> **YB-safe:** drive this from `pg_class.reltuples` (cluster-wide catalog), not `pg_stat_user_tables` (node-local). `reltuples = -1` = never analyzed. Do **not** filter on `last_autoanalyze` — YB Auto Analyze records into `last_analyze`, so `last_autoanalyze` stays NULL even after it runs.
```sql
SELECT c.relname,
       c.reltuples::bigint AS est_rows,
       CASE WHEN c.reltuples = -1 THEN 'NEVER ANALYZED' ELSE 'analyzed' END AS stats_state,
       GREATEST(st.last_analyze, st.last_autoanalyze) AS last_analyze_this_node
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_stat_all_tables st ON st.relid = c.oid
WHERE c.relkind IN ('r','p') AND n.nspname NOT IN ('pg_catalog','information_schema')
ORDER BY (c.reltuples = -1) DESC, c.reltuples DESC;
```
**Action:** Tables with `est_rows = -1` (never analyzed): run `ANALYZE <table>`. For recency, remember the timestamp is node-local — a table can read NULL here yet have been analyzed on another node; confirm across `yb_servers()` (or `yb_stat_auto_analyze()` on 2025.2.3+) before concluding it is stale. See `query-tuning.md` for the cross-node query.

**If tables show `est_rows = -1` even though data was loaded**, the root cause is auto-analyze being unavailable/disabled (or ANALYZE never run) — not individual stale tables. `last_autoanalyze IS NULL` on its own is **not** evidence of this in YB (it is NULL by design). Check the version and flag instead:
- **2024.x or earlier**: auto-analyze does not exist; manual `ANALYZE` is the only option — raise as a finding and note the upgrade path.
- **2025.1**: feature exists but may be off. Enabling it uses the 2025.1 flags `ysql_enable_auto_analyze_service` (Master + TServer) **and** `ysql_enable_table_mutation_counter` (TServer), both requiring a restart.
- **2025.2+**: on by default when CBO is enabled; the flag is `ysql_enable_auto_analyze = true` (with `ysql_enable_auto_analyze_infra`). Verify with the tserver flags (`…:9000/api/v1/varz`) — the 2025.1 `_service`/`_table_mutation_counter` flags are *not* the ones in play here. If `reltuples` stays -1 on loaded tables, investigate a GFlag override or cooldown settings. See `query-tuning.md`.

### 5. Connection usage trends — `contention.md`
```sql
SELECT state, count(*) FROM pg_stat_activity
WHERE backend_type = 'client backend' GROUP BY state ORDER BY count DESC;
```
**Flag:** `idle in transaction` count growing over successive checks = application not committing cleanly; consider setting `idle_in_transaction_session_timeout = '30s'`.

### 6. Terminated queries since last check — `contention.md`
```sql
SELECT query_text, termination_reason, query_end_time
FROM yb_terminated_queries
WHERE query_end_time > now() - interval '7 days'
ORDER BY query_end_time DESC;
```
Any new SIGKILL entries need investigation — see `contention.md`.

### 7. Tablet growth — `yb-metrics-analysis`
For YBA-managed clusters: run Performance Advisor scan for "too many tablets" warning. See `yb-metrics-analysis` tablet limits playbook if flagged.
