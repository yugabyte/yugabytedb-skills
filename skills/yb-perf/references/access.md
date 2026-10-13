# Reaching YSQL, and working without it

## YBA-managed Kubernetes universe

Tserver pods are `<node>-yb-tserver-<N>` (label `app.kubernetes.io/name=yb-tserver`); the namespace is
`universeDetails.nodeDetailsSet[].cloudInfo.kubernetesNamespace` in the YBA universe API. `ysqlsh` in a pod
reaches the local node without `-h`. Add `--context <ctx>` if the universe is not in the current context.

```bash
# run from the yb-perf skill directory (scripts/ is relative to it)
kubectl --context <ctx> get pods -A | grep yb-tserver          # find namespace and pods
kubectl --context <ctx> exec -i -n <ns> <node>-yb-tserver-0 -c yb-tserver -- \
  ysqlsh -U <user> -d <db> -f - < scripts/must-check.sql
kubectl --context <ctx> port-forward -n <ns> <node>-yb-tserver-0 5433:5433   # or use local tools
scripts/all-nodes.sh --kube <ns> --context <ctx> -f scripts/must-check.sql  # every pod
```

Each pod is one node: per-node views (pgss, `pg_stat_activity`, `pg_locks`, ASH) show only its share.

## YugabyteDB Aeon

GFlags are not self-service on Aeon: they need a support ticket (usually a restart).

| Self-service (SQL) | Support ticket (GFlag) | Not available |
|---|---|---|
| `pg_stat_statements`, `pg_stat_activity`, `pg_locks` (other sessions need `pg_monitor`) | Enable ASH if the relation is missing: `ysql_yb_enable_ash` | TServer / master log files (ask support for extracts) |
| `EXPLAIN (ANALYZE, DIST)`, manual `ANALYZE` | `yb_query_diagnostics`: `ysql_yb_enable_query_diagnostics` (2025.2+) | Full Prometheus surface: the public API's cluster-metrics endpoint is a small curated set; configure metrics export instead |
| GUCs incl. `yb_enable_pg_stat_statements_rpc_stats` via `ALTER DATABASE ... SET` (new sessions only: pooled connections must reconnect) | Query Plan Management: `yb_pg_stat_plans_track` (later than 2025.2.1; check `SHOW`) | |
| ASH, if `SELECT count(*) FROM yb_active_session_history` works | Slow-statement logging, `auto_explain` (logs are not user-visible) | |
| Console: Slow Queries, Insights (Performance Advisor: cluster > Performance > Insights, unverified), Metrics, Connection Pooling (default 10 client connections per server connection, docs) | Connection limits, ASH sampling interval, any other GFlag | |

## Paste mode (no live access)

Ask the user to run this on each node and paste the output (empty `docdb_*` columns mean
rpc_stats was off in the workload's sessions):

```sql
SELECT queryid, left(query, 120) AS query, calls,
       round(mean_exec_time::numeric, 2) AS mean_ms, round(total_exec_time::numeric, 0) AS total_ms,
       round(max_exec_time::numeric, 2) AS max_ms,
       round(yb_get_percentile(yb_latency_histogram, 50)::numeric, 2) AS p50_ms,
       round(yb_get_percentile(yb_latency_histogram, 99)::numeric, 2) AS p99_ms,
       conflict_retries, read_restart_retries, docdb_rows_scanned, docdb_rows_returned, docdb_read_rpcs
FROM pg_stat_statements WHERE calls > 10 ORDER BY mean_exec_time DESC LIMIT 50;
```

## CSV exports (pgss, ASH, query-diagnostics bundles)

An export is the whole view in arbitrary order (ASH: one row per sample), so never read it
head-first. Load it into a scratch table on any YugabyteDB (a local one is fine) and run the
scripts' queries against it:

```sql
CREATE TEMP TABLE pgss_import (LIKE pg_stat_statements);
\copy pgss_import FROM 'export.csv' WITH (FORMAT csv, HEADER true)
CREATE TEMP TABLE ash_import (LIKE yb_active_session_history);
\copy ash_import FROM 'active_session_history.csv' WITH (FORMAT csv, HEADER true)
```

Off YugabyteDB, create the table from the export's header row instead. Load every node's file
into the one table (add a `source_node` column) before aggregating.
