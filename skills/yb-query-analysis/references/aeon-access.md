# YugabyteDB Aeon — SQL-layer access guide

Aeon (YugabyteDB Managed) is a fully managed service. This means GFlag changes require a support ticket, direct node access is unavailable, and the Prometheus surface is limited. This reference is the single authoritative guide to what is and isn't possible for SQL-layer performance analysis on Aeon. For Aeon REST API automation (cluster scaling, metrics export, backup management) see the `aeon-api` skill.

## Access model: two independent axes

Before recommending any action, determine where it falls:

| Axis | What it covers | On Aeon |
|---|---|---|
| **SQL-readable** | Any `SELECT` against catalog views, stat views, EXPLAIN, manual ANALYZE | Self-service — any user with appropriate role |
| **GFlag / infrastructure** | TServer/Master GFlag changes, TServer restart, log file access, node shell | Support-only or unavailable |

A user may have full SQL access (Axis A) but no GFlag control (Axis B). This is the normal Aeon state. Always check Axis B requirements before recommending a feature.

## Self-service — no support ticket needed

### SQL catalog and stat views
```sql
-- pg_stat_statements: requires pg_monitor role for full cross-session visibility
-- Verify access:
SELECT count(*) FROM pg_stat_statements;
-- If zero rows with a live workload, check role: SHOW session_authorization;

-- Enable DocDB columns (GUC, not GFlag — always self-service)
SET yb_enable_pg_stat_statements_rpc_stats = true;
-- Or persist:
ALTER DATABASE <dbname> SET yb_enable_pg_stat_statements_rpc_stats = true;

-- pg_stat_activity: own session always visible; other sessions need pg_monitor role
SELECT count(*) FROM pg_stat_activity;

-- pg_locks
SELECT count(*) FROM pg_locks;

-- pg_stat_user_tables, pg_stat_all_indexes (for health check queries)
-- NOTE: these are node-local in YB — analyze timestamps and idx_scan reflect
-- only the node you connect to. Use pg_class.reltuples (cluster-wide) for
-- "has this table been analyzed", and aggregate idx_scan across nodes before
-- calling an index unused. On Aeon, Insights already aggregates these centrally.
SELECT count(*) FROM pg_stat_user_tables;
```

If `pg_monitor` role is needed but not granted, ask the Aeon database admin user to grant it:
```sql
GRANT pg_monitor TO <app_user>;  -- requires the Aeon admin user (admin-level DB user, not cloud console)
```

### EXPLAIN and EXPLAIN DIST
Always self-service. Run on the YSQL connection:
```sql
EXPLAIN (ANALYZE, DIST, COSTS OFF) <query>;
```

### Manual ANALYZE
Always self-service:
```sql
ANALYZE <tablename>;
ANALYZE;  -- full database
```

### Planner GUC settings (not GFlags)
Set at session or database level without any support involvement:
```sql
SET yb_enable_cbo = on;
SET yb_max_saop_merge_streams = 64;
SET yb_enable_derived_saops = true;
SET yb_enable_derived_equalities = true;
ALTER DATABASE <dbname> SET yb_enable_cbo = on;
```

### Aeon Console features
All available without support:
- **Slow Queries UI:** cluster → Performance → Queries → Slow Queries — backed by `pg_stat_statements`; shows P25/P50/P90/P95/P99 latency histogram, call count, mean time. YSQL only.
- **Aeon Insights:** cluster → Performance → Insights → Scan — runs Performance Advisor. Flags: unused indexes, schema/sharding mismatches, connection skew, query load skew, CPU skew/usage. Run after ≥ 30 minutes of representative workload.
- **Metrics UI:** cluster → Performance → Metrics — ops/sec, avg latency, CPU, memory, disk usage. Limited compared to YBA's full Prometheus surface but covers operational basics.
- **Connection Pooling:** cluster → Settings → Connection Pooling — enables YSQL Connection Manager self-service. Default: 10 client connections per server connection.

### Active Session History (ASH) — verify access first
ASH is enabled by default in recent Aeon versions. Verify before assuming:
```sql
SELECT count(*) FROM yb_active_session_history;
```
- **Returns rows → self-service.** Proceed with `ash-analysis.md` queries.
- **Returns 0 consistently even under load** → may be a permissions issue or ASH not enabled; try granting `pg_monitor` first, then test again.
- **`relation does not exist`** → ASH is not enabled on this cluster. File a support ticket to enable `ysql_yb_enable_ash = true` (TServer GFlag, restart required).

Note: ASH data on Aeon is per-node and may not be visible across all nodes from a single connection. The Aeon console does not currently surface an ASH UI, so SQL queries are the only interface.

---

## Support-only — requires a support ticket

These features require GFlag changes that Yugabyte must apply. When recommending them, clearly state this and suggest the user open a ticket at support.yugabyte.com with the specific GFlag and value requested.

| Feature | GFlag required | Restart? | Notes |
|---|---|---|---|
| Enable ASH if not on | `ysql_yb_enable_ash = true` | Yes | Only needed if `yb_active_session_history` relation doesn't exist |
| yb_query_diagnostics ⚠️ 2025.2+ | `ysql_yb_enable_query_diagnostics = true` | Yes | Diagnostic bundle for a specific query |
| Slow query logging | `ysql_log_min_duration_statement = <ms>` | No | Logs to TServer log files — user cannot access logs on Aeon |
| auto_explain | `shared_preload_libraries = auto_explain` | Yes | Logs plans — user cannot access logs on Aeon |
| Query Plan Management ⚠️ 2025.2+ | `yb_pg_stat_plans_track = all` | Yes | Plan history capture |
| ASH sampling interval change | `ysql_yb_ash_sampling_interval_ms = <ms>` | Yes | |
| Increase max connections | `ysql_max_connections = <n>` | Yes | Consider Connection Pooling (self-service) first |
| Any other TServer/Master GFlag | — | Varies | |

**Important:** `ysql_log_min_duration_statement` and `auto_explain` are useful on Aeon in principle, but since Yugabyte manages the log files and Aeon users cannot SSH to nodes, log output is not directly accessible. Request these only if Yugabyte support can extract relevant log snippets as part of a support case.

---

## Not available on Aeon

- **Direct node access** (SSH, shell commands)
- **yb-ts-cli / yb-admin** CLI tools
- **Full Prometheus / PromQL surface** — only the curated metrics set is available via the Aeon metrics API and console (ops/sec, avg latency, CPU, disk, connections). For the full ~2,000-metric Prometheus surface, customers can configure metrics export to Datadog, Grafana Cloud, or a self-hosted Prometheus via cluster → Settings → Metrics Export.
- **TServer/Master log files** directly — request via support if needed
- **GFlag changes** without support involvement (see above)

---

## Aeon tier differences

Aeon has multiple tiers (Sandbox, Dedicated). Key differences affecting this skill:
- **Sandbox clusters:** may have reduced feature availability and no SLA; not suitable for performance testing
- **Dedicated clusters:** full feature set as described above; Performance Advisor and Insights are available

When a feature works on Dedicated but the user is on Sandbox, recommend upgrading to Dedicated for performance work.

---

## Recommended analysis sequence on Aeon

1. **Start with Aeon Insights** — run a scan to get automated recommendations before manual SQL work
2. **Slow Queries UI** — identify top queries by P99 latency or mean time without writing SQL
3. **`pg_stat_statements` via SQL** — for deeper column-level analysis not available in the UI (scan ratio, retry counts)
4. **`EXPLAIN (ANALYZE, DIST)`** — for the specific slow queries identified
5. **ASH** (if available) — for wait event breakdown on specific queries
6. **Support ticket** — for anything requiring GFlag changes (ASH enable, query diagnostics, log access)
