-- must-check.sql -- one read-only pass over the YugabyteDB-specific must-checks (SKILL.md S1-S5, S8-S9),
-- plus context, tail latency, unindexed foreign keys and abnormal terminations.
-- Takes ~1.5 s (five spaced samples of pg_stat_activity / pg_locks).
-- Run once: ysqlsh -h <host> -p 5433 -U <user> -d <db> -f must-check.sql
-- Reads catalog / pg_stat_* views only. The two SETs change this session only, no data.
-- EXPLAIN ANALYZE executes the statement -- never run it on INSERT/UPDATE/DELETE (or any write);
-- use plain EXPLAIN, or an equivalent SELECT.
-- pg_stat_statements, pg_stat_activity, pg_locks and yb_terminated_queries are per node:
-- run on every tserver for full coverage (all-nodes.sh). The pg_class / pg_constraint reads are cluster-wide.
\pset pager off
SET statement_timeout = '30s';
SET yb_locks_min_txn_age = 0;   -- show ALL lock holders/waiters, not only transactions >= 1 s (the default)

\echo '== 0. Context: version, pgss DocDB column switches (NULL = GUC not in this release), pgss window =='
SELECT version();
SELECT current_setting('yb_enable_pg_stat_statements_rpc_stats', true)     AS rpc_stats,
       current_setting('yb_enable_pg_stat_statements_docdb_metrics', true) AS docdb_metrics;
\echo '   rpc_stats off in the workload sessions => docdb_*_rpcs AND docdb_rows_scanned/returned stay empty, so S5 cannot run'
\echo '   (a SET here does not fix that; ALTER DATABASE|ROLE ... SET reaches only new sessions -- pooled connections must reconnect).'
SELECT stats_reset AS pgss_last_reset, now() - stats_reset AS window_covered FROM pg_stat_statements_info;

\echo '== S1. Retries per call (contention, or lock waits under Read Committed). Fast mean + retries = waiting =='
SELECT left(query, 80) AS query, calls,
       round(mean_exec_time::numeric, 2) AS mean_ms, round(max_exec_time::numeric, 1) AS max_ms,
       round(conflict_retries::numeric / NULLIF(calls, 0), 2) AS conflict_retries_per_call,
       round(read_restart_retries::numeric / NULLIF(calls, 0), 2) AS read_restarts_per_call
FROM pg_stat_statements
WHERE calls >= 5 AND (conflict_retries + read_restart_retries) > 0.1 * calls
ORDER BY (conflict_retries + read_restart_retries) DESC LIMIT 10;

\echo '== S2+S3. Five samples ~0.25 s apart: (a) transactions much older than their current statement (locks held across other work); (b) ungranted locks. Zero rows in every sample is INCONCLUSIVE for short transactions -- trust check S1 (retries) and app latency over an empty sample. =='
SELECT 'activity 1' AS s, pid, state, wait_event_type, wait_event,
       round(extract(epoch FROM now() - xact_start) * 1000) AS txn_age_ms,
       round(extract(epoch FROM now() - query_start) * 1000) AS stmt_age_ms,
       left(query, 60) AS current_or_last_query
FROM pg_stat_activity
WHERE xact_start IS NOT NULL AND pid <> pg_backend_pid() AND backend_type = 'client backend'
  AND now() - xact_start > interval '20 ms' AND now() - xact_start > 2 * (now() - query_start)
ORDER BY xact_start LIMIT 8;
SELECT 'locks 1' AS s, relation::regclass AS rel, mode, granted, ybdetails->'blocked_by' AS blocked_by
FROM pg_locks WHERE NOT granted LIMIT 8;
SELECT pg_sleep(0.25) \g /dev/null
SELECT 'activity 2' AS s, pid, state, wait_event_type, wait_event,
       round(extract(epoch FROM now() - xact_start) * 1000) AS txn_age_ms,
       round(extract(epoch FROM now() - query_start) * 1000) AS stmt_age_ms,
       left(query, 60) AS current_or_last_query
FROM pg_stat_activity
WHERE xact_start IS NOT NULL AND pid <> pg_backend_pid() AND backend_type = 'client backend'
  AND now() - xact_start > interval '20 ms' AND now() - xact_start > 2 * (now() - query_start)
ORDER BY xact_start LIMIT 8;
SELECT 'locks 2' AS s, relation::regclass AS rel, mode, granted, ybdetails->'blocked_by' AS blocked_by
FROM pg_locks WHERE NOT granted LIMIT 8;
SELECT pg_sleep(0.25) \g /dev/null
SELECT 'activity 3' AS s, pid, state, wait_event_type, wait_event,
       round(extract(epoch FROM now() - xact_start) * 1000) AS txn_age_ms,
       round(extract(epoch FROM now() - query_start) * 1000) AS stmt_age_ms,
       left(query, 60) AS current_or_last_query
FROM pg_stat_activity
WHERE xact_start IS NOT NULL AND pid <> pg_backend_pid() AND backend_type = 'client backend'
  AND now() - xact_start > interval '20 ms' AND now() - xact_start > 2 * (now() - query_start)
ORDER BY xact_start LIMIT 8;
SELECT 'locks 3' AS s, relation::regclass AS rel, mode, granted, ybdetails->'blocked_by' AS blocked_by
FROM pg_locks WHERE NOT granted LIMIT 8;
SELECT pg_sleep(0.25) \g /dev/null
SELECT 'activity 4' AS s, pid, state, wait_event_type, wait_event,
       round(extract(epoch FROM now() - xact_start) * 1000) AS txn_age_ms,
       round(extract(epoch FROM now() - query_start) * 1000) AS stmt_age_ms,
       left(query, 60) AS current_or_last_query
FROM pg_stat_activity
WHERE xact_start IS NOT NULL AND pid <> pg_backend_pid() AND backend_type = 'client backend'
  AND now() - xact_start > interval '20 ms' AND now() - xact_start > 2 * (now() - query_start)
ORDER BY xact_start LIMIT 8;
SELECT 'locks 4' AS s, relation::regclass AS rel, mode, granted, ybdetails->'blocked_by' AS blocked_by
FROM pg_locks WHERE NOT granted LIMIT 8;
SELECT pg_sleep(0.25) \g /dev/null
SELECT 'activity 5' AS s, pid, state, wait_event_type, wait_event,
       round(extract(epoch FROM now() - xact_start) * 1000) AS txn_age_ms,
       round(extract(epoch FROM now() - query_start) * 1000) AS stmt_age_ms,
       left(query, 60) AS current_or_last_query
FROM pg_stat_activity
WHERE xact_start IS NOT NULL AND pid <> pg_backend_pid() AND backend_type = 'client backend'
  AND now() - xact_start > interval '20 ms' AND now() - xact_start > 2 * (now() - query_start)
ORDER BY xact_start LIMIT 8;
SELECT 'locks 5' AS s, relation::regclass AS rel, mode, granted, ybdetails->'blocked_by' AS blocked_by
FROM pg_locks WHERE NOT granted LIMIT 8;

\echo '== S4. Per-connection warm-up: plan time >> exec time, or the same text split across entries =='
SELECT left(query, 80) AS query, calls,
       round(mean_plan_time::numeric, 2) AS mean_plan_ms, round(mean_exec_time::numeric, 2) AS mean_exec_ms
FROM pg_stat_statements
WHERE calls >= 5 AND mean_plan_time > 3 * GREATEST(mean_exec_time, 0.05)
ORDER BY mean_plan_time * calls DESC LIMIT 10;
SELECT left(query, 80) AS query, count(*) AS pgss_entries, sum(calls) AS calls
FROM pg_stat_statements WHERE calls >= 5 GROUP BY query HAVING count(*) > 1 LIMIT 10;
SELECT count(*) FILTER (WHERE now() - backend_start < interval '5 s') AS backends_younger_than_5s,
       count(*) AS client_backends
FROM pg_stat_activity WHERE backend_type = 'client backend';

\echo '== S5. Scan amplification (rows scanned in storage per row returned) =='
SELECT CASE WHEN coalesce(current_setting('yb_enable_pg_stat_statements_rpc_stats', true), 'on') <> 'on'
            THEN 'S5 NOT MEASURABLE: rpc_stats is off here, so docdb_rows_scanned is likely empty -- an empty result below is a gap, not a pass'
            ELSE 'S5 measurable if rpc_stats was also on in the workload sessions' END AS s5_status;
\echo '   shape = AGG (count/sum/avg/min/max/GROUP BY): rows is ~1 per call, so scanned_per_row is meaningless --'
\echo '   compare scanned_per_call with the table row count; a full count(*) scanning every row is not a finding.'
SELECT left(query, 80) AS query, calls, rows, docdb_rows_scanned,
       round(docdb_rows_scanned::numeric / GREATEST(rows, 1), 0) AS scanned_per_returned_row,
       round(docdb_rows_scanned::numeric / NULLIF(calls, 0), 0) AS scanned_per_call,
       CASE WHEN query ~* '\m(count|sum|avg|min|max)\s*\(' OR query ~* '\mgroup\s+by\M'
            THEN 'AGG' END AS shape,
       round(mean_exec_time::numeric, 2) AS mean_ms
FROM pg_stat_statements
WHERE calls >= 5 AND docdb_rows_scanned > 10 * GREATEST(rows, 1)
ORDER BY docdb_rows_scanned DESC LIMIT 10;

\echo '== Tail latency: p99/p50 > 10 = occasional very slow runs (retries, hot tablet, cold cache) behind a healthy mean =='
SELECT left(query, 80) AS query, calls,
       round(yb_get_percentile(yb_latency_histogram, 50)::numeric, 2) AS p50_ms,
       round(yb_get_percentile(yb_latency_histogram, 99)::numeric, 2) AS p99_ms,
       round((yb_get_percentile(yb_latency_histogram, 99) /
              NULLIF(yb_get_percentile(yb_latency_histogram, 50), 0))::numeric, 1) AS p99_p50
FROM pg_stat_statements
WHERE calls >= 20 AND yb_latency_histogram IS NOT NULL
ORDER BY yb_get_percentile(yb_latency_histogram, 99) DESC NULLS LAST LIMIT 10;

\echo '== S8. Cluster membership (compare with the expected node count) =='
SELECT count(*) AS nodes_serving, string_agg(host, ', ' ORDER BY host) AS hosts FROM yb_servers();

\echo '== S9. Tables never analysed (cluster-wide pg_class.reltuples: -1 on PG15-based 2025.1+; 0 on PG11-based 2024.x, where 0 can also be an empty table).'
\echo '   n_live_tup / last_*analyze are per node, and Auto Analyze writes last_analyze, not last_autoanalyze =='
SELECT n.nspname AS schema, c.relname, c.reltuples::bigint AS est_rows
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p')
  AND (c.reltuples = -1 OR (c.reltuples = 0 AND current_setting('server_version_num')::int < 150000))
  AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%'
ORDER BY 1, 2 LIMIT 20;

\echo '== Unindexed foreign keys: parent DELETE/UPDATE full-scans the child (invisible to an insert-only workload) =='
SELECT c.conrelid::regclass AS child_table, a.attname AS fk_column, c.confrelid::regclass AS parent_table
FROM pg_constraint c
JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
WHERE c.contype = 'f'
  AND NOT EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid = c.conrelid AND i.indkey[0] = c.conkey[1])
ORDER BY 1 LIMIT 20;

\echo '== Abnormal terminations on this node (OOM kill, crash, temp_file_limit) =='
SELECT query_end_time, termination_reason, left(query_text, 80) AS query
FROM yb_terminated_queries ORDER BY query_end_time DESC LIMIT 10;

\echo '== Not covered here: S6 (tombstones: rocksdb seek/next metrics, M7 in metrics-check.sh) and S7 (range keys: check the schema). =='
