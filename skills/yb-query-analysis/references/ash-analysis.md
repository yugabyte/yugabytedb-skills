# Active Session History (ASH) analysis

ASH samples active database sessions at regular intervals (default: every 1 second) and stores each sample — wait event, query ID, component, and topology context — in a per-node circular in-memory buffer. It answers the question "what was the database actually doing (and waiting on) at a point in time?" rather than the cumulative picture that `pg_stat_statements` gives.

## Enabling and verifying

ASH is enabled by default in YugabyteDB 2025.2+. On older builds, it may need to be enabled.

**Verify access first:**
```sql
SELECT count(*) FROM yb_active_session_history;
```
- Returns a count → ASH is enabled and accessible; proceed
- Returns 0 → may just be no activity; generate some load and retry
- `permission denied` → need `pg_monitor` role: `GRANT pg_monitor TO <your_user>;` (requires superuser to grant)
- `relation "yb_active_session_history" does not exist` → ASH is not enabled; requires GFlag change

**Enable (TServer GFlag, restart required — on Aeon: support ticket):**
```
ysql_yb_enable_ash = true                   -- master switch (default true in 2025.2+)
ysql_yb_ash_sampling_interval_ms = 1000     -- sampling frequency in ms
ysql_yb_ash_sample_size = 500               -- max samples captured per interval
ysql_yb_ash_circular_buffer_size = 256      -- MiB per node (range: 32–1024)
```

**Important:** ASH data is per-node and in-memory only. It is not aggregated cluster-wide. Querying `yb_active_session_history` on a YSQL connection returns samples from that connection's local TServer node. For cluster-wide analysis, query from multiple nodes or use ybtop if available. The buffer size (default ~256 MiB) determines how far back history extends — on a busy cluster this may be only minutes.

## Wait event → fix routing table

The most common use of ASH is to identify what class of problem is occurring. Match the dominant `wait_event_type` and `wait_event` to the appropriate next step:

| wait_event_type | wait_event | Likely cause | Next step |
|---|---|---|---|
| `Cpu` | `OnCpu_Active` | Query is CPU-bound: large sort, hash join, missing index causing full scan | PGSS: check `docdb_rows_scanned` ratio; EXPLAIN DIST for Seq Scan — see `pgss-analysis.md` |
| `RPCWait` | `ConflictResolution_ResolveConflicts` | Transaction write-write conflicts causing retry storms | PGSS: `conflict_retries` column; check for hot rows / small key space — see `contention.md` |
| `RPCWait` | `Raft_WaitingForReplication` | Raft replication backpressure — TServer overloaded or a replica falling behind | `yb-metrics-analysis`: latency/throughput playbook; check `follower_lag_ms` metric |
| `RPCWait` | `Raft_ApplyingEdits` | Raft log apply pressure from high write throughput | `yb-metrics-analysis`: Raft metrics; consider write batching in application |
| `WaitOnCondition` | `MVCC_WaitForSafeTime` | Clock skew too high for MVCC safe-time advancement, or follower reads misconfigured | `yb-metrics-analysis`: clock skew metric; check `yb_follower_read_staleness_ms` setting |
| `WaitOnCondition` | `Rpc_Done` | Generic RPC wait — waiting for a remote call to complete | Identify the `wait_event_component` (TServer vs YSQL) to narrow further |
| `DiskIO` | `RocksDB_NewIterator` | DocDB block cache miss — working set exceeds cache size | `yb-metrics-analysis`: disk-io playbook; check `rocksdb_block_cache_hit` vs `_miss` |
| `LWLock` | (various) | YSQL lightweight lock contention | `contention.md`: `pg_stat_activity` blocking chain |
| `Network` | (various) | Network-layer RPC delay | `yb-metrics-analysis`: check cross-AZ latency, node connectivity |

**Healthy baseline:** On a well-tuned cluster under normal load, the majority (> 70%) of ASH samples should be `Cpu / OnCpu_Active` — meaning the database is doing actual work, not waiting. If `RPCWait` or `DiskIO` dominate, something is wrong.

## Key analysis queries

### Quick: what is the cluster waiting on right now?
```sql
SELECT wait_event_type,
       wait_event,
       wait_event_component,
       count(*) AS samples,
       round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct
FROM yb_active_session_history
WHERE sample_time > now() - interval '5 minutes'
GROUP BY wait_event_type, wait_event, wait_event_component
ORDER BY samples DESC
LIMIT 20;
```

### CPU-bound vs wait-bound split
```sql
SELECT
    CASE WHEN wait_event_type = 'Cpu' THEN 'On CPU'
         ELSE 'Waiting (' || wait_event_type || ')'
    END AS activity_class,
    count(*) AS samples,
    round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct
FROM yb_active_session_history
WHERE sample_time > now() - interval '10 minutes'
GROUP BY activity_class
ORDER BY samples DESC;
```

### Drill into a specific slow query (join with PGSS queryid)
```sql
-- Get queryid from pgss-analysis.md first
SELECT wait_event_type, wait_event, count(*) AS samples
FROM yb_active_session_history
WHERE query_id = <queryid>
  AND sample_time > now() - interval '30 minutes'
GROUP BY wait_event_type, wait_event
ORDER BY samples DESC;
```

### Hot shard detection (tablet with most TServer activity)
```sql
SELECT wait_event_aux AS tablet_id,
       count(*) AS samples
FROM yb_active_session_history
WHERE wait_event_component = 'TServer'
  AND sample_time > now() - interval '10 minutes'
  AND wait_event_aux IS NOT NULL
GROUP BY tablet_id
ORDER BY samples DESC
LIMIT 10;
```
Then identify the table behind the hot tablet:
```sql
-- Run on the same node where the hot tablet lives
SELECT t.table_name, t.namespace_name, t.tablet_id
FROM yb_local_tablets t
WHERE t.tablet_id = '<tablet_id_from_above>';
```
A hot tablet on a write-heavy table often means a sharding key hotspot (monotonic PK, low-cardinality key). See `ysql` skill for sharding design guidance.

### Load distribution by topology (cloud / region / zone)
```sql
SELECT s.cloud, s.region, s.zone,
       count(*) AS samples,
       round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct
FROM yb_active_session_history ash
JOIN yb_servers() s ON ash.top_level_node_id::uuid = s.uuid
WHERE ash.sample_time > now() - interval '10 minutes'
GROUP BY s.cloud, s.region, s.zone
ORDER BY samples DESC;
```
Useful for confirming even load distribution across zones and detecting cross-AZ hotspots.

### Activity by client application
```sql
SELECT client_node_ip,
       count(*) AS samples
FROM yb_active_session_history
WHERE sample_time > now() - interval '10 minutes'
GROUP BY client_node_ip
ORDER BY samples DESC
LIMIT 10;
```

## Version note

⚠️ The `rss_mem_bytes` column in `yb_active_session_history` was added in **YugabyteDB 2025.2**. On earlier versions, omit it from queries. Use it to identify memory-heavy sessions during OOM investigations.

## Paste-mode — what to ask the user to collect

When you don't have live database access, ask for two pastes:

**1. Overall wait breakdown (last 30 minutes):**
```sql
SELECT wait_event_type, wait_event, wait_event_component,
       count(*) AS samples,
       round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct
FROM yb_active_session_history
WHERE sample_time > now() - interval '30 minutes'
GROUP BY wait_event_type, wait_event, wait_event_component
ORDER BY samples DESC
LIMIT 30;
```

**2. Hot tablets (TServer component):**
```sql
SELECT wait_event_aux AS tablet_id, count(*) AS samples
FROM yb_active_session_history
WHERE wait_event_component = 'TServer'
  AND sample_time > now() - interval '30 minutes'
  AND wait_event_aux IS NOT NULL
GROUP BY tablet_id
ORDER BY samples DESC
LIMIT 10;
```

If `yb_active_session_history` returns an error, see the enabling section above and note whether this requires a support ticket on their deployment.

## Working from a raw ASH file export

A handed-over ASH file — `active_session_history.csv` from a query-diagnostics bundle, or `\copy yb_active_session_history TO '…csv'` — is **not** the aggregated paste above. It is the **raw sample stream: one row per active session, per sampling interval (default 1s)**. On a busy node that is hundreds of thousands to millions of rows for a 30-minute window, in **time order**, not grouped by wait event.

This is a sharper trap than a `pg_stat_statements` export, in two ways:

1. **Never read it head-first.** The first rows are just the earliest few seconds on one node — not a representative or ranked view. (`Read` defaults to the first 2000 lines, which here is a meaningless time-slice.)
2. **A top-N of raw rows is also useless** — unlike pgss, where each row is already a per-statement aggregate, an ASH row is a single sample. The signal *only* exists after you **aggregate** (count samples `GROUP BY wait_event`, compute the percentage breakdown). Sorting raw samples and taking the top N tells you nothing.

So there is effectively **one correct path**: load into a scratch table and run the aggregation queries from this reference against it.
```sql
CREATE TEMP TABLE ash_import (LIKE yb_active_session_history);
\copy ash_import FROM '/path/active_session_history.csv' WITH (FORMAT csv, HEADER true);
-- now run the wait-breakdown and hot-tablet queries above against ash_import instead of yb_active_session_history
```
A local PostgreSQL is fine — this is pure sample data, no YugabyteDB needed to read it. **Note the per-node caveat** (see "Enabling and verifying" above): each export covers one node's buffer, so if you were given several files, load them all into the one table (add a `source_node` column if you need to tell them apart) before aggregating — otherwise the breakdown reflects a single node, not the cluster.
