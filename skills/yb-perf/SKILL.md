---
name: yb-perf
description: Checks the YugabyteDB-specific performance traps that general database and monitoring knowledge misses, and the evidence required before concluding. Use when an app, site or database on YugabyteDB is slow, latency is up, p99 regressed, someone asks "is the database the cause?", or a universe needs a health check or performance review - and for the specifics - a slow query, an EXPLAIN (ANALYZE, DIST) plan, pg_stat_statements, lock contention or retries, ASH wait events, a node hotspot, leader or tablet skew, CPU, disk or memory pressure, tablet limits, Prometheus / YBA metrics. Covers both the SQL layer and the metrics layer, with read-only one-call check scripts, and works with whatever access exists - live SQL, Prometheus / YBA metrics, or pasted output - before naming a root cause or ruling the database out.
---

# YugabyteDB performance: must-checks

You already know how to diagnose a slow Postgres-style system and read a monitoring stack.
This skill adds only what is different on YugabyteDB, plus the rules that stop the common
wrong conclusions. It covers two layers: SQL / session (S rows) and metrics (M rows).

## Process (any slowness question)

1. **Scope first: one path slow, or many unrelated paths slow at once?** Use the app's
   per-endpoint / activity latency if available, else the spread across `pg_stat_statements`.
   **Many at once** → node, quorum or resource. **One path** → a query: find the statements
   that path runs. The top `total_exec_time` query is often a healthy batch job.
   **This gate decides between layers:** a node problem slows every path that touches that
   node. If the complaint is one path and other paths run at normal latency, the cause is on
   that path (its statements, its connections, its client), and any node finding is secondary.
2. **Run the one-call checks in the order the scope sets**, from this skill's directory,
   before any hand-written query. They are cheap and read-only, and each layer is blind to
   causes the other sees (metrics miss scan amplification, connection churn / catalog warm-up,
   unindexed FK cascades; SQL misses a CPU-starved node and leader skew).
   - **One path slow** → `scripts/must-check.sql` first, and explain that path from its
     statements, connections and client. Run `scripts/metrics-check.sh` only if the path's
     statements don't explain it, and treat a node finding as secondary unless other paths
     are slow too.
   - **Many paths slow, or metrics are all you have** → `scripts/metrics-check.sh` first, then
     `scripts/must-check.sql` (if reachable) to rule out a query driving the node.
   - **Scope unknown** → ask, or find it (app per-activity latency, the pgss spread) before
     choosing. If a layer is unreachable, say so in the report. With pasted evidence, apply
     the checks to what you have.
   - SQL: run it once, as one call, with `ysqlsh -f <path>` or the session wrapper you were given.
   - Metrics: `bash <path>/metrics-check.sh http://<prometheus>:9090`. On YBA set the scope first:
     `YB_SEL='node_prefix="<prefix>",export_type="tserver_export"'` and
     `YB_MASTER_SEL='node_prefix="<prefix>",export_type="master_export"'` (header of the script
     has the rest). It prints per-node tables, flags any node at 2× or more its peers' median,
     and ends with a per-node verdict.
3. **Drill into the layer the evidence points at; confirm with the other** (conflicts in
   metrics → retries / ASH in SQL; a uniform SQL latency rise → the per-node M rows).
4. **Rank causes by the latency the user feels, not by database time spent.** Waits (row
   locks, conflict retries, queueing) are **excluded** from `pg_stat_statements` timings, so a
   1 ms statement can be the 500 ms problem.
5. **Break every latency, CPU or queue metric down per node before reading an aggregate.** On
   RF3, one bad node is diluted about 3× in a cluster average. Compare each node with its
   peers; the outlier is the finding.
6. **When the complaint is broad (many paths slow) and one node is a large, new latency
   outlier, explain that node first.** A node leading the hottest tablets routinely runs 2-3×
   its peers in a healthy cluster, so a modest ratio is a lead, not a cause, and never outranks
   a SQL finding that explains the path the user named. Name the outlier's ceiling:
   CPU limit, disk, or a noisy neighbour. Low CPU on the slow node does **not** rule out CPU:
   a capped node cannot use more, so compare its usage with its limit (M3c, or the container /
   VM size). A busy table on that node is the cause only if its load is new or abnormal for that
   table -- table load sits where the table's leaders are, and the same load on a healthy node
   would be fast.
7. **Before writing "no database-side cause" or "infrastructure is healthy", list which
   must-checks you ran and what each showed.** Any check you couldn't run is an open gap, not
   a clean result. A node that is up is not a node that is healthy.

## Must-checks: SQL / session

| # | Check | Where | Means |
|---|---|---|---|
| S1 | `conflict_retries` / `read_restart_retries` per call > 0.1 | pgss | Contention, or **lock waits under Read Committed** (a wait becomes a retry). A fast mean with retries is waiting, not fast. |
| S2 | Transaction age ≫ current statement time; `idle in transaction` | `pg_stat_activity` | Something holds locks across other work. Look at what the same transaction does after its first write. |
| S3 | `pg_locks` shows nothing | `pg_locks` | **Not proof of no blocking.** It hides transactions younger than `yb_locks_min_txn_age` (default 1 s). `SET yb_locks_min_txn_age = 0` in your session (changes no data), and sample more than once. |
| S4 | Same statement text in two pgss rows; or `mean_plan_time` ≫ `mean_exec_time` on a point query | pgss | **Per-connection catalog warm-up**: the app opens a connection per request. Confirm with one fresh-session timing. Fix: connection pooling, not the query. |
| S5 | `docdb_rows_scanned` / `rows` ≫ 1; or `Storage Rows Scanned` = 2× rows returned | pgss / `EXPLAIN (ANALYZE, DIST)` | Scan amplification (missing index or blocked pushdown), or a non-covering index (index + table read). For an aggregate (`count(*)`, `GROUP BY`) compare scanned-per-call with the table's row count instead. **`docdb_rows_*` are filled only while `yb_enable_pg_stat_statements_rpc_stats` was on in the workload's sessions (off by default on 2025.2): if it was off, S5 is a gap, not a pass** -- use `EXPLAIN (ANALYZE, DIST)` on a read. |
| S6 | Latency high but scan ratio ≈ 1 (measured -- see S5) on a queue-like or delete-heavy table | pgss + M7 | **Tombstones** from deletes: YugabyteDB counts live rows, so the plan looks clean. |
| S7 | `PRIMARY KEY (ts ASC ...)` or another range key on a monotonic value | schema | All inserts hit the last range tablet. More tablets won't fix it; a hash-prefixed key will. |
| S8 | `yb_servers()` count, `follower_lag_ms`, uniform latency rise across unrelated statements | SQL / M1, M6 | Node or infrastructure event, not a query defect. Don't invent a SQL cause; find the node in the M rows. |
| S9 | `pg_class.reltuples = -1` (PG15-based 2025.1+); `reltuples = 0` on PG11-based 2024.x and earlier, where 0 can also be an empty table | catalog | Never analysed: the planner is guessing. Cluster-wide; `pg_stat_user_tables` timestamps are per node. |

## Must-checks: metrics (per node)

| # | Check | Means |
|---|---|---|
| M1 | Targets up, tserver and master | Fewer than expected = a node is down. |
| M2 | **Tserver Read / Write RPC latency** (`rpc_latency{service_method="Read"\|"Write"}`): mean and p99 | One node ≫ peers = that node is slow (CPU-starved, disk, noisy neighbour). |
| M3 | **CPU per node, against its limit**: process, then host or container, then the limit / throttling | A node pinned at a lower ceiling than its peers is throttled or undersized. Low usage on the slow node does not clear it (step 6). |
| M4 | WAL sync latency | Disk / fsync problem on that node. |
| M5 | Leaders, tablet peers, share of the tablet limit, limit rejections | Leader skew concentrates load; peers near the limit block creates and splits. |
| M6 | Follower lag, `all_operations_inflight`, memory-pressure / SST / RPC-queue rejections | Lag is often a symptom; rejections are the cause. |
| M7 | RocksDB seek/next per op, per `table_name` | High seek/next for flat row counts = tombstones (delete-heavy tables; S6). Heavy load on a table is not itself a finding (step 6). |
| M8 | `rocksdb_stall_micros`, queued compactions | Write stalls from compaction debt. |
| M9 | Master RPC latency and queue rejections | Catalog / DDL / heartbeat pressure on the master leader. |
| M10 | Transaction conflicts, wait-queue waiters | Contention: confirm in SQL (S1 retries, `scripts/ash.sql`). |
| M11 | App metrics, if exported: per-endpoint or activity latency | Confirms the scope (step 1) from the user's side. |

## Safety

- Read-only by default. Anything that changes the system (DDL, cancelling transactions,
  statistics resets, GUC / GFlag changes) is proposed for approval, not run.
- `EXPLAIN ANALYZE` executes the statement -- never run it on INSERT/UPDATE/DELETE (or any
  write); use plain `EXPLAIN`, or an equivalent `SELECT`.

## Scripts and references

- `scripts/must-check.sql` -- run first for SQL: S1-S5, S8, S9 plus context (version, pgss window), p99/p50, unindexed foreign keys and abnormal terminations.
- `scripts/metrics-check.sh` -- run first for metrics: M1-M10 in one Prometheus pass (read-only HTTP GETs; Read and Write mean and p99), YBA or self-managed selectors. M11 (app metrics) is a reminder only: check it yourself.
- `scripts/ash.sql` -- when time goes to waits: ASH wait breakdown, top `query_id`, hot tablets, zone split (`-v window='30 minutes'`).
- `scripts/all-nodes.sh` -- pgss, `pg_stat_activity`, `pg_locks` and ASH are per node: run either SQL file on every node (`--help` for host mapping, `--exec`, `--kube`).
- [references/yb-facts.md](references/yb-facts.md) -- read before relying on a column, GUC, default, version gate or wait-event name: pgss, stats, locks, EXPLAIN, hints, query diagnostics, ASH.
- [references/access.md](references/access.md) -- read when there is no direct `ysqlsh`: YBA Kubernetes, Aeon, paste mode, CSV exports.
- [references/metric-names.md](references/metric-names.md) -- read before writing PromQL beyond the script: exact metric and label names, YBA vs self-managed labels.
- [references/yb-metric-facts.md](references/yb-metric-facts.md) -- read when interpreting a metric value: leader counts, percentiles, tablet limits and splitting, metric sources (YBA, node endpoint, Aeon).
- YBA's Prometheus listens on port 9090 of the YBA host; `node_prefix` is the universe's `universeDetails.nodePrefix` (YBA API `GET /api/v1/customers/{cUUID}/universes/{uUUID}`). For more, if the `yba-api` skill is installed, see its Prometheus reference.

## Report

- One primary cause tied to the complaint, with its evidence (per-node evidence, naming the
  node or component, when it is an infrastructure cause).
- Then secondary findings, explicitly subordinated.
- Then the must-checks you ran (S and M), what each showed, and the gaps: checks or whole
  layers you couldn't reach.
