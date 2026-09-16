# Contention, locks, and connection analysis

This reference covers four related problems: **lock contention** (blocking query chains), **transaction conflicts** (distributed write-write conflicts), **connection exhaustion** (running out of YSQL backends), and **abnormal query termination** (OOM kills, temp file limits).

---

## pg_stat_activity — session visibility

`pg_stat_activity` shows one row per backend process in real time. By default the app user sees only their own sessions. To see all sessions, the user needs the `pg_monitor` role:
```sql
GRANT pg_monitor TO <app_user>;  -- requires superuser; on Aeon this is support-only
```

### Find long-running active queries
```sql
SELECT pid, datname, usename, application_name,
       state,
       left(query, 120) AS query,
       now() - query_start AS duration,
       wait_event_type, wait_event
FROM pg_stat_activity
WHERE state = 'active'
  AND now() - query_start > interval '1 minute'
ORDER BY duration DESC;
```

### Find idle-in-transaction sessions (lock holders and connection leaks)
```sql
SELECT pid, usename, application_name,
       state,
       left(query, 120) AS last_query,
       now() - xact_start AS txn_age,
       wait_event_type, wait_event,
       client_addr
FROM pg_stat_activity
WHERE state LIKE 'idle in transaction%'
ORDER BY txn_age DESC;
```
An `idle in transaction` session has opened a transaction and stopped executing — it may be holding locks and consuming a connection slot. Application-side causes: missing COMMIT/ROLLBACK, exception thrown before cleanup, connection pool misconfiguration.

### Connection count by state
```sql
SELECT state,
       count(*) AS connections
FROM pg_stat_activity
WHERE backend_type = 'client backend'
GROUP BY state
ORDER BY connections DESC;
```

---

## Connection exhaustion

The default per-TServer YSQL connection limit is **300** (`ysql_max_connections` GFlag). When this is reached, new connections are rejected with `FATAL: sorry, too many clients already`.

**Diagnose:**
```sql
-- Current usage vs limit
SELECT count(*) AS total,
       count(*) FILTER (WHERE state = 'active')              AS active,
       count(*) FILTER (WHERE state = 'idle')                AS idle,
       count(*) FILTER (WHERE state LIKE 'idle in transaction%') AS idle_in_txn
FROM pg_stat_activity
WHERE backend_type = 'client backend';
```

**Immediate relief — propose terminating long idle-in-transaction sessions (destructive — get explicit approval first).** This rolls back the matched transactions and drops their connections. **Do not run it unprompted:** first list the candidates for the user (the `SELECT` below without the `pg_terminate_backend` wrapper), show who/what would be killed, and run it only once they confirm.
```sql
-- Review candidates first (read-only):
--   SELECT pid, usename, application_name, now() - xact_start AS txn_age, left(query,80)
--   FROM pg_stat_activity WHERE state = 'idle in transaction' AND now() - xact_start > interval '5 minutes';
-- Then, ONLY after the user approves, terminate them:
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE state = 'idle in transaction'
  AND now() - xact_start > interval '5 minutes';
```

**Check for a timeout safety net:**
```sql
SHOW idle_in_transaction_session_timeout;
-- '0' means no timeout — sessions can hold open indefinitely
-- Recommend: SET idle_in_transaction_session_timeout = '30s' at session or database level
```

**Prevention — YSQL Connection Manager:**
The built-in connection pooler multiplexes many client connections onto fewer server backends. Enable it when peak connections approach the limit. See `ysql` skill for details and caveats (supports prepared statements and TEMP tables unlike PgBouncer; on Aeon available via Settings → Connection Pooling).

---

## pg_locks — blocking chain analysis

### Tune visibility (reduce noise)
```sql
-- Only show transactions held for > 2 seconds (default 0 = show everything)
SET yb_locks_min_txn_age = 2000;
SET yb_locks_max_transactions = 100;  -- limit rows
```

### Find the full blocking chain
```sql
SELECT bl.pid                       AS blocked_pid,
       left(bq.query, 120)          AS blocked_query,
       bq.state                     AS blocked_state,
       now() - bq.xact_start        AS blocked_txn_age,
       kl.pid                       AS blocking_pid,
       left(kq.query, 120)          AS blocking_query,
       kq.state                     AS blocking_state,
       now() - kq.xact_start        AS blocking_txn_age
FROM pg_locks bl
JOIN pg_stat_activity bq ON bq.pid = bl.pid
JOIN pg_locks kl ON kl.transactionid = bl.transactionid AND kl.granted
JOIN pg_stat_activity kq ON kq.pid = kl.pid
WHERE NOT bl.granted
ORDER BY blocked_txn_age DESC;
```

### Inspect YugabyteDB distributed lock details
The `ybdetails` JSONB column adds distributed context not in standard PostgreSQL:
```sql
SELECT pid,
       locktype,
       granted,
       ybdetails->>'tablet_id'      AS tablet_id,
       ybdetails->>'transactionid'  AS yb_txn_uuid,
       ybdetails->'blocked_by'      AS blocked_by_txn_ids,
       waitend
FROM pg_locks
WHERE ybdetails IS NOT NULL
ORDER BY waitend ASC NULLS LAST;
```

The `yb_txn_uuid` from `ybdetails->>'transactionid'` is the YugabyteDB distributed transaction UUID — different from the PostgreSQL PID.

### Cancel a blocking transaction (destructive — requires explicit user approval)

Identify the blocker for the user (from the blocking-chain query above) and **propose** one of the options below; run it only once the user confirms which transaction/backend to cancel. Prefer the YugabyteDB-native one for distributed transactions:
```sql
-- Cancel via YugabyteDB distributed transaction UUID (from ybdetails above)
-- Rolls back the distributed transaction cleanly
SELECT yb_cancel_transaction('xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx');

-- Kill the PostgreSQL backend (more aggressive — use if yb_cancel_transaction unavailable)
SELECT pg_terminate_backend(<blocking_pid>);
```

---

## Transaction conflicts

Distributed write-write conflicts are normal in YugabyteDB but should be rare. When they spike, they show up in `pg_stat_statements` and ASH:

**From PGSS — find high-conflict queries:**
```sql
SELECT queryid,
       left(query, 120) AS query,
       calls,
       conflict_retries,
       round(conflict_retries::numeric / NULLIF(calls, 0), 3) AS conflict_rate,
       read_restart_retries,
       round(read_restart_retries::numeric / NULLIF(calls, 0), 3) AS restart_rate
FROM pg_stat_statements
WHERE (conflict_retries > 0 OR read_restart_retries > 0) AND calls > 10
ORDER BY conflict_retries DESC
LIMIT 20;
```

**Threshold:** `conflict_rate > 0.05` (5% of calls retry) warrants investigation.

**Common causes of write-write conflicts:**
- Hot rows: multiple transactions updating the same row simultaneously (e.g. incrementing a counter, updating a status column on a small key space)
- Too-large transactions touching many rows
- Missing COMMIT causing long-lived transactions holding intents

**ASH confirmation:** If `RPCWait / ConflictResolution_ResolveConflicts` dominates in `ash-analysis.md`, it confirms the same picture.

**Remediation options:**
- Redesign to avoid shared mutable state (e.g. use append-only event log, not counter updates)
- Reduce transaction scope (commit more frequently, touch fewer rows per transaction)
- Use `SELECT ... FOR UPDATE` with appropriate isolation to explicit order competing writers rather than relying on retry
- Check `yb_transaction_priority_lower_bound` / `upper_bound` if priority-based conflict resolution is appropriate

---

## yb_terminated_queries — abnormal termination

Holds up to 1,000 recent abnormal query terminations per node (in-memory circular buffer — not persisted across restarts, not cluster-aggregated).

```sql
-- Recent abnormal terminations
SELECT query_text,
       termination_reason,
       query_start_time,
       query_end_time,
       query_end_time - query_start_time AS duration
FROM yb_terminated_queries
ORDER BY query_end_time DESC
LIMIT 20;

-- OOM kills only (OS killed the backend due to memory pressure)
SELECT query_text, termination_reason, query_end_time
FROM yb_terminated_queries
WHERE termination_reason LIKE '%SIGKILL%'
ORDER BY query_end_time DESC;

-- Temp file limit breaches (query generated too much sort/hash spill)
SELECT query_text, termination_reason, query_end_time
FROM yb_terminated_queries
WHERE termination_reason LIKE '%temp%'
ORDER BY query_end_time DESC;
```

**Termination reasons:**
- `SIGKILL` — OS OOM killer terminated the backend; the query was consuming too much memory
- `SIGSEGV` — backend crash (PostgreSQL bug or corrupted data)
- Temp file size exceeded `temp_file_limit` — query generated more sort/hash spill than allowed

**For SIGKILL:** run `EXPLAIN (ANALYZE, DIST)` on the offending query to find hash joins or sorts on large datasets. Add indexes to eliminate them, or increase `work_mem` carefully (it multiplies per active session).

**For temp file limit:** either the limit is too low for legitimate sort workloads (`SET temp_file_limit = '2GB'` — session-level, always self-service), or the query needs an index to avoid the sort.

---

## Paste-mode — what to ask the user to collect

**Session state snapshot:**
```sql
SELECT state,
       count(*) AS connections,
       count(*) FILTER (WHERE now() - xact_start > interval '1 minute') AS old_txns
FROM pg_stat_activity
WHERE backend_type = 'client backend'
GROUP BY state ORDER BY connections DESC;
```

**Blocking chain (if locks suspected):**
```sql
SET yb_locks_min_txn_age = 1000;
SELECT bl.pid AS blocked_pid, left(bq.query, 80) AS blocked_query,
       kl.pid AS blocking_pid, left(kq.query, 80) AS blocking_query,
       now() - bq.xact_start AS blocked_age
FROM pg_locks bl
JOIN pg_stat_activity bq ON bq.pid = bl.pid
JOIN pg_locks kl ON kl.transactionid = bl.transactionid AND kl.granted
JOIN pg_stat_activity kq ON kq.pid = kl.pid
WHERE NOT bl.granted;
```

**Recent terminations:**
```sql
SELECT query_text, termination_reason, query_end_time
FROM yb_terminated_queries ORDER BY query_end_time DESC LIMIT 10;
```
