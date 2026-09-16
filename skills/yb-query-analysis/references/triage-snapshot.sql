-- =============================================================================
-- triage-snapshot.sql — one-shot red-flag scan for YugabyteDB YSQL
-- =============================================================================
-- PURPOSE
--   A single, cheap, read-only pass that surfaces the most common SQL-layer
--   problems WITHOUT running EXPLAIN or touching user-table data. Run this FIRST,
--   before constructing any of your own diagnostic queries. It tells you WHICH
--   queries and objects deserve a closer look — it does not replace the deeper
--   analysis in the reference files.
--
-- USAGE
--   ysqlsh -h <host> -p 5433 -d <database> -f triage-snapshot.sql
--   (or from a psql session:  \i triage-snapshot.sql )
--
-- COST
--   All queries read catalog / pg_stat_statements / pg_stat_activity only.
--   No user-table scans, no writes. Safe to run on production.
--
-- READING THE OUTPUT
--   Each section prints a heading and a small result set. A section with zero
--   rows is a clean signal for that check. Do NOT stop at the first red flag —
--   read every section, list all flags, then rank by impact. See the
--   "Assessment discipline" block in SKILL.md.
-- =============================================================================

\pset pager off
\timing off

-- Enable DocDB RPC columns for this session so scan/RPC metrics populate.
-- (No restart; session-scoped. To persist: ALTER DATABASE <db> SET ... = true;)
SET yb_enable_pg_stat_statements_rpc_stats = true;

\echo
\echo ============================================================
\echo  0. CONTEXT — version, rpc_stats, pgss reset age
\echo ============================================================
SELECT version();
SHOW yb_enable_pg_stat_statements_rpc_stats;
SELECT stats_reset AS pgss_last_reset,
       now() - stats_reset AS window_covered
FROM pg_stat_statements_info;

\echo
\echo ============================================================
\echo  0. CLUSTER MEMBERSHIP — is every expected node still serving?
\echo ============================================================
SELECT count(*)                                   AS nodes_serving,
       string_agg(host, ', ' ORDER BY host)        AS hosts_serving
FROM yb_servers();
-- INTERPRET: compare nodes_serving against the universe's EXPECTED node count.
-- yb_servers() alone cannot tell you that 2 is wrong -- you must know the expected
-- count, so ask the user if you do not already know it.
--
-- Fewer rows than expected = a node is not serving. STOP and investigate that
-- before attributing latency to any query. A uniform latency rise across
-- structurally unrelated statements, with clean plans and normal scan ratios, is
-- the signature of a cluster-level event, not a query defect -- and no schema,
-- index or statistics change will fix it.
--
-- This mirrors vital 0 ("Nodes reporting") in yb-metrics-analysis'
-- issue-quick-healthcheck.md, which is metrics-only. This section is the SQL-side
-- equivalent, so an engagement with SQL access but no metrics can still see it.

\echo
\echo ============================================================
\echo  1. WORKLOAD CONCENTRATION — top queries by cumulative impact
\echo     (ranked by total_exec_time; only queries that are >= 1%% of
\echo      total workload time AND have run enough to be meaningful)
\echo ============================================================
-- Impact-based filtering, not an arbitrary call count:
--   * pct_total >= 1.0  -> the query is a non-trivial share of all time spent
--   * calls >= 5        -> floor to drop one-off / noise statements
-- A query consuming <1%% of total time is almost never worth tuning first,
-- even if its mean looks high. Rank by where the database actually spends time.
WITH w AS (
  SELECT queryid, query, calls, total_exec_time, mean_exec_time, max_exec_time,
         sum(total_exec_time) OVER () AS grand_total
  FROM pg_stat_statements
  WHERE calls >= 5
)
SELECT left(query, 70)                                   AS query,
       calls,
       round(mean_exec_time::numeric, 2)                 AS mean_ms,
       round(total_exec_time::numeric, 0)                AS total_ms,
       round((100.0 * total_exec_time / NULLIF(grand_total,0))::numeric, 1) AS pct_total,
       round(max_exec_time::numeric, 2)                  AS max_ms
FROM w
WHERE (100.0 * total_exec_time / NULLIF(grand_total,0)) >= 1.0
ORDER BY total_exec_time DESC
LIMIT 10;
-- INTERPRET: the top 1-3 rows are where tuning effort pays off. A flat
-- distribution (no query > a few %) means no single hotspot — look at schema /
-- infrastructure instead. A single query at 50%+ is your prime suspect.

\echo
\echo ============================================================
\echo  2. SCAN AMPLIFICATION — storage rows scanned vs returned
\echo     (the decisive signal for missing index / blocked pushdown)
\echo ============================================================
-- This catches function-wrapped or cast predicates that defeat an existing
-- index (e.g. lower(col)=..., col::bigint=...). Latency alone misses these on
-- a small/idle cluster; the scan ratio does not.
SELECT left(query, 70)                                                   AS query,
       calls,
       rows,
       docdb_rows_scanned,
       round(docdb_rows_scanned::numeric / GREATEST(rows, 1), 0)         AS scanned_per_row,
       round(docdb_rows_scanned::numeric / NULLIF(calls, 0), 0)          AS scanned_per_call,
       CASE WHEN query ~* '\m(count|sum|avg|min|max)\s*\(' OR query ~* '\mgroup\s+by\M'
            THEN 'AGG' END                                               AS shape,
       round(docdb_read_rpcs::numeric / NULLIF(calls,0), 1)              AS rpcs_per_call,
       round(mean_exec_time::numeric, 2)                                 AS mean_ms
FROM pg_stat_statements
WHERE calls >= 5 AND docdb_rows_scanned > 0
ORDER BY docdb_rows_scanned::numeric / GREATEST(rows, 1) DESC
LIMIT 10;
-- INTERPRET: scanned_per_row ~1 is ideal. > 100 on a selective query = a full
-- scan where an index seek was possible. > 1000 almost always means a function
-- wrapper / type cast on the indexed column, or no index at all.
-- rpcs_per_call > 2 on a single-row result = N+1 or missing covering index.
--
-- shape = 'AGG' marks a row as an aggregate/GROUP BY. Those rows are EXPECTED
-- to sort to the top of this section and are almost never findings -- read the
-- note below before reporting one.
--
-- AGGREGATES: read scanned_per_call, NOT scanned_per_row. For COUNT/SUM/AVG or
-- any GROUP BY, `rows` is ~1 per call however much was legitimately scanned, so
-- scanned_per_row is meaningless for them and will ALWAYS look catastrophic --
-- it just approximates the table's row count. Judge an aggregate by
-- scanned_per_call against the table's actual row count: roughly equal is a
-- normal full aggregate, not amplification. `SELECT count(*) FROM t` scanning
-- every row of t is arithmetic, not a defect. scanned_per_row is the right
-- measure only for queries that RETURN the rows they matched.
-- Because this section is ordered by scanned_per_row, unfiltered aggregates
-- sort to the top; expect them there and do not report them as findings.
-- If this section is EMPTY but you expect load, rpc_stats was off when the
-- queries ran — re-run the workload after the SET above, then re-check.

\echo
\echo ============================================================
\echo  3. RETRY HOTSPOTS — conflict and read-restart retries
\echo ============================================================
SELECT left(query, 70)                                          AS query,
       calls,
       conflict_retries,
       read_restart_retries,
       round(conflict_retries::numeric   / NULLIF(calls,0), 3)  AS conflict_rate,
       round(read_restart_retries::numeric / NULLIF(calls,0), 3) AS restart_rate
FROM pg_stat_statements
WHERE (conflict_retries > 0 OR read_restart_retries > 0) AND calls >= 5
ORDER BY (conflict_retries + read_restart_retries) DESC
LIMIT 10;
-- INTERPRET: conflict_rate > 0.1 = write-write contention (hot row) -> see
-- contention.md. restart_rate > 0.1 = clock skew or long read snapshots.
-- Empty = no transaction contention recorded. Good.

\echo
\echo ============================================================
\echo  4. TAIL LATENCY — p99/p50 ratio (needs yb_latency_histogram)
\echo ============================================================
SELECT left(query, 70)                                                   AS query,
       calls,
       round(yb_get_percentile(yb_latency_histogram, 50)::numeric, 2)    AS p50_ms,
       round(yb_get_percentile(yb_latency_histogram, 99)::numeric, 2)    AS p99_ms,
       round((yb_get_percentile(yb_latency_histogram, 99) /
              NULLIF(yb_get_percentile(yb_latency_histogram, 50),0))::numeric, 1) AS p99_p50
FROM pg_stat_statements
WHERE calls >= 20 AND yb_latency_histogram IS NOT NULL
ORDER BY yb_get_percentile(yb_latency_histogram, 99) DESC NULLS LAST
LIMIT 10;
-- INTERPRET: p99_p50 > 10 = occasional very slow runs (retries, hot shard,
-- cold cache) hiding behind a healthy mean. A near-1 ratio is consistent.

\echo
\echo ============================================================
\echo  5. SEQUENCE CACHE — CACHE 1 serialises every nextval()
\echo ============================================================
SELECT sequencename, cache_size
FROM pg_sequences
WHERE cache_size < 100
ORDER BY cache_size ASC;
-- INTERPRET: any row here = a sequence that costs one RPC per value under
-- concurrent inserts. Fix: ALTER SEQUENCE <name> CACHE 100.  Empty = good.
-- NOTE: cache_size reflects current DDL, not history. The cluster GFlag
-- ysql_sequence_cache_minval can force a higher floor regardless of DDL.

\echo
\echo ============================================================
\echo  6. STALE / MISSING STATISTICS — tables needing ANALYZE
\echo ============================================================
-- YB-SAFE: driven from pg_class.reltuples, which is the cluster-wide catalog
-- value (identical on every node) — NOT pg_stat_user_tables, whose analyze
-- columns are node-local. reltuples = -1 means the table has NEVER been
-- analyzed (planner is flying blind). The last_analyze here is this node's
-- local record only; see the cross-node note below.
SELECT c.relname,
       c.reltuples::bigint                     AS est_rows,
       CASE WHEN c.reltuples = -1 THEN 'NEVER ANALYZED' ELSE 'analyzed' END AS stats_state,
       GREATEST(st.last_analyze, st.last_autoanalyze) AS last_analyze_this_node
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_stat_all_tables st ON st.relid = c.oid
WHERE c.relkind IN ('r','p')
  AND n.nspname NOT IN ('pg_catalog','information_schema')
ORDER BY (c.reltuples = -1) DESC, c.reltuples DESC
LIMIT 15;
-- INTERPRET: est_rows = -1 / stats_state = NEVER ANALYZED => planner may be
-- working from wrong cardinality. Fix: ANALYZE <table>. Especially important
-- right after a bulk load.
-- YB CAVEATS (validated on RF3 2025.2): (1) last_analyze_this_node is
-- NODE-LOCAL — a table analyzed on another node shows NULL here even though it
-- was analyzed; to judge recency reliably, check every node from yb_servers()
-- and take the max, or on 2025.2.3+ use yb_stat_auto_analyze(). (2) YB Auto
-- Analyze records into last_analyze, NOT last_autoanalyze (which stays NULL) —
-- never test last_autoanalyze alone. reltuples above is cross-node reliable and
-- is the signal to trust in this single-pass snapshot.

\echo
\echo ============================================================
\echo  7. CONNECTIONS — state mix and idle-in-transaction
\echo ============================================================
SELECT state,
       count(*) AS conns,
       round(extract(epoch FROM max(now() - state_change)), 0) AS oldest_in_state_s
FROM pg_stat_activity
WHERE backend_type = 'client backend'
GROUP BY state
ORDER BY conns DESC;
-- INTERPRET: a growing 'idle in transaction' count = app not committing; holds
-- locks and MVCC snapshots. Set idle_in_transaction_session_timeout. Total
-- client backends approaching ~200/node (of 300 default) -> plan Connection
-- Manager. See contention.md.

\echo
\echo ============================================================
\echo  TRIAGE COMPLETE. List every flagged section before drilling in.
\echo
\echo  This snapshot ranks by SCAN AMPLIFICATION + cumulative time. It does
\echo  NOT surface issues with a modest scan ratio. Before concluding, run the
\echo  "Second-tier checks" in pgss-analysis.md (#6 over-fetch / SELECT * on
\echo  wide rows, #7 missing range/sort index) and report any real findings as
\echo  a secondary tier — do not stop at the top scan-ratio offenders alone.
\echo
\echo  Drill-down: pgss-analysis.md (queries), contention.md (locks/conns),
\echo  ash-analysis.md (waits), query-tuning.md (stats/plans).
\echo ============================================================
