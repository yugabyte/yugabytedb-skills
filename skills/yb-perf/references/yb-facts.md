# YugabyteDB SQL facts for performance work

One line per fact where YugabyteDB differs from PostgreSQL, limited to facts current models
often get wrong. "(live)" = re-checked on a 2025.2.1 RF3 cluster; "(docs)" = YugabyteDB docs.

## pg_stat_statements

- YBA's Slow Queries view collects and resets pgss across all nodes of a universe (unverified); a direct query sees one node.
- Timings exclude waits (row locks, retries, queueing): a fast `mean_exec_time` can still be the slow path.
- `yb_get_percentile()` over the latency histogram returns milliseconds, like `mean_exec_time` (live).
- `pg_stat_statements.track_planning` defaults on (off in PostgreSQL), so `mean_plan_time` is filled (live).
- `docdb_rows_scanned` counts rows read in DocDB storage.
- `conflict_retries`, `read_restart_retries`, `total_retries` (all retries): per-statement counts. Under Read Committed a lock wait shows up as a retry, not as exec time.
- `yb_enable_pg_stat_statements_rpc_stats` gates `docdb_rows_scanned` / `_returned`, `docdb_read_rpcs` / `_write_rpcs`, `docdb_read_operations` / `_write_operations`, `docdb_wait_time` (DocDB waits) and `catalog_wait_time` (catalog reads); off by default on 2025.2.1 (live).
- It is a runtime GUC, not a GFlag: `ALTER DATABASE|ROLE ... SET`, no restart, self-service on Aeon.
- `yb_enable_pg_stat_statements_docdb_metrics` gates `docdb_seeks` / `_nexts` / `_prevs`, `docdb_read_time` / `_write_time`, `docdb_obsolete_rows_scanned`; neither the GUC nor the columns exist on 2025.2.1 (live).
- Both GUCs default on from v2025.2.5.0 / v2026.1.1.0.
- Your own diagnostic statements land in pgss too; exclude them before ranking.
- A `pg_type` / `pg_attribute` query with very high `calls` is the driver's type load on each new connection: a pooling problem, not an N+1.

## Scope across nodes

- `pg_locks` and ASH are node-local; the catalog (`pg_class.reltuples`, `pg_stats`, `pg_index`) is cluster-wide.
- An index is unused only if `idx_scan` summed over all nodes is 0 and no node restarted since.

## Statistics and sizes

- Auto Analyze sets `last_analyze` / `analyze_count`, never `last_autoanalyze`, and only on the node where it ran (live). Never test `last_autoanalyze` alone.
- Auto Analyze: EA in 2025.1 (`ysql_enable_auto_analyze_service` on masters and tservers + `ysql_enable_table_mutation_counter` on tservers, docs); GA and on by default from 2025.2 with CBO; absent on 2024.x.
- 2025.2 also needs tserver flag `ysql_enable_auto_analyze_infra` = true (live default).
- Trigger: mutations > threshold + `ysql_auto_analyze_scale_factor` (0.1) x `reltuples`; per-table cooldown 10 s minimum, 24 h maximum (live).
- `yb_stat_auto_analyze()` (cluster-aware) is 2025.2.3+; absent on 2025.2.1 (live).
- `pg_relation_size()` returns 0 for YugabyteDB relations; use `pg_table_size()` or `pg_total_relation_size()` (live).
- `ysql_sequence_cache_minval` (GFlag, default 100, live) floors sequence caching: `CACHE 1` still caches 100 values unless the flag is 0.

## Locks, retries and terminations

- `pg_locks` hides transactions younger than `yb_locks_min_txn_age` (default 1 s, live) and caps output at `yb_locks_max_transactions` (16, live). `SET yb_locks_min_txn_age = 0` in your session.
- `pg_locks.ybdetails->'blocked_by'` lists the blocking transaction ids; `pg_locks` also has `waitend` next to `waitstart` (live).
- `yb_cancel_transaction('<uuid>')` rolls back one distributed transaction; destructive, propose and get approval first.
- `yb_terminated_queries` (`query_end_time`, `termination_reason`, `query_text`): up to 1,000 per node, in memory, lost on restart. `termination_reason` SIGKILL = OOM kill.
- High `read_restart_retries` points at clock skew or long read snapshots.

## EXPLAIN (ANALYZE, DIST)

- `ANALYZE` executes the statement; never on a write -- use plain `EXPLAIN` or an equivalent `SELECT`.
- A predicate shown as `Storage Filter` (not `Index Cond`) on a high-call UPDATE/DELETE: every call is a read-modify-write even when it matches nothing.
- Batched join node: `YB Batched Nested Loop Join`; inner `Index Cond: (c = ANY (ARRAY[...]))` with `loops=1`, multi-column `ROW(a, b) = ANY (ARRAY[ROW(o.a, o.b), ...])`. Batching cuts Storage Read Requests by about the batch factor.
- Batch size default was 1 (off) on 2.20 and earlier. From 2.21 / 2024.2, `enable_nestloop = off` does not disable BNL.
- Plain `EXPLAIN` has no `loops`; judge the join by node type and `Index Cond` form.
- Tombstones and obsolete versions are not counted in `docdb_rows_scanned` / `Storage Rows Scanned`, so the plan can look clean (scan ratio about 1). Evidence: latency plus `rocksdb_number_db_seek` / `_next` (and `_prev`) per operation.

## Planner settings and pg_hint_plan

- Comment hints work by default. Hint table `norm_query_string` matches exactly: `?` where literals are inlined, `$n` for bound parameters (verified on 2026.1.1.0).
- `pg_hint_plan.yb_use_query_id_for_hinting` (default off, live) makes the hint table match on query id.
- From 2.21 / 2024.2 `NestLoop(a b)` forces the unbatched join; only `YBBatchedNL(a b)` forces the batched one.
- `yb_max_saop_merge_streams` (default 0 = off, live) merges per-element index scans of an `IN` list into one ordered stream for `ORDER BY ... LIMIT`.
- `yb_enable_derived_saops` and `yb_enable_derived_equalities` are off by default on 2025.2.1 (live).

## yb_query_diagnostics and Query Plan Management

- EA in 2025.2. TServer GFlag `ysql_yb_enable_query_diagnostics` + restart (Aeon: support ticket).
- `yb_query_diagnostics(query_id => ..., diagnostics_interval_sec => 300 default, bind_var_query_min_duration_ms => 10 default)` (live).
- Writes per node to `<pg_data>/query_diagnostics/<query_id>/<random>/` (docs; the function returns the path): `constants_and_bind_variables.csv`, `pg_stat_statements.csv`, `schema_details.txt`, `active_session_history.csv`, `explain_plan.txt`. Never cleaned up automatically; at most 100 bundles at once (docs).
- Query Plan Management: `yb_pg_stat_plans_track` = `none | top | all`; absent on 2025.2.1 (live), so later than 2025.2.1 -- check `SHOW`.

## Active Session History (ASH)

- TServer GFlag `ysql_yb_enable_ash`, true by default in 2025.2+ (live). In-memory per-node circular buffer, never persisted or aggregated.
- Defaults (live): 500 samples per interval (`yb_ash_sample_size`), 256 MiB buffer (`yb_ash_circular_buffer_size`) -- history may cover only minutes on a busy node.
- `wait_event_component`: `YSQL`, `TServer`, `YCQL` (live). `query_id` joins `pg_stat_statements.queryid`. Per-sample memory column is `pss_mem_bytes` (live 2025.2.1).
- `wait_event_aux` on TServer samples is a truncated 15-char tablet id (live): join `yb_local_tablets.tablet_id LIKE wait_event_aux || '%'` on the node hosting it, never `=`.
- Wait events (type, live): `ConflictResolution_ResolveConficts` (spelled so; RPCWait) = write-write conflicts; `Raft_WaitingForReplication` (RPCWait) = replication backpressure; `Raft_ApplyingEdits` (Cpu); `MVCC_WaitForSafeTime` (WaitOnCondition) = clock skew or follower-read settings; `Rpc_Done` (WaitOnCondition) = response waiting for the reactor; `RocksDB_NewIterator` (DiskIO) = block cache misses; `OnCpu_Active` (Cpu) = on CPU.
- Under ordinary load a 2025.2.1 cluster showed mostly `RPCWait` / `TableRead`, not `Cpu` / `OnCpu_Active` (live); do not treat that alone as a finding.
