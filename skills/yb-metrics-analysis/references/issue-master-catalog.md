# Playbook: master / catalog RPC pressure

> *Starting point — to be expanded.* The YB-Master serves cluster metadata — most visibly the **catalog reads** (`GetTableSchema`-type calls) every YSQL backend needs to plan queries. Unlike tserver traffic, this load lands on the **single master leader** and **does not scale out with node count** — adding tservers adds heartbeat baseline but no extra catalog-serving capacity. Sustained master RPC load above baseline is therefore a scaling risk in its own right *and* a hidden latency tax on the queries paying for the lookups.

## Symptoms

- Master RPC rate well above its steady-state baseline, or climbing with connection churn rather than with data traffic.
- First-query-after-connect latency much worse than steady-state latency (catalog cache warming per new connection).
- High `catalog_wait_time` in `pg_stat_statements`; latency spikes correlated with reconnect storms or deploys.
- Master leader node showing CPU out of proportion to the (normally light) master role.

## Metrics to read

| Concern | Metric | Notes |
|---|---|---|
| Total master load | `sum(rpc_irate_rps{node_prefix="<prefix>", server_type="yb_master"})` | The headline. Raw: `irate(handler_latency_yb_master_*_count[…])`. |
| What the load is | same, `by (service_method)` | Separates the **heartbeat baseline** (`TSHeartbeat`) from **catalog reads** (`GetTableSchema`, `GetTableLocations`, `GetTabletLocations`, `ListTables`, …). |
| Who serves it | same, `by (exported_instance)` | Nearly all of it lands on the **master leader** — expect one hot series; that's the design, not a skew bug. |
| Driver: connection churn | `rate(yb_ysqlserver_new_connection_total[5m])` *(confirm name via discovery)* | Each **new** connection's first queries do catalog cache lookups → misses → master reads. |
| SQL-side corroboration | `pg_stat_statements.catalog_wait_time` | Time queries spent waiting on catalog/metadata — see [`yb-query-analysis` pgss reference](../../yb-query-analysis/references/pgss-analysis.md). |
| Leader CPU | master process CPU (`cpu_utime_irate` etc. on the master leader) | Confirms the pressure is biting. |

## Baseline vs. excess

- **Steady-state baseline is proportional to node count** — a rough figure is **~5 × the number of nodes** in RPCs/sec (heartbeats plus housekeeping). Establish *your* universe's quiet-period baseline rather than trusting the constant.
- **Excess above baseline** is almost always catalog traffic, driven by (in rough order of frequency):
  1. **Connections reconnecting** — a fresh backend's first queries populate its catalog cache; a churning pool (min/idle too low, no pooling, reconnect loops) pays this constantly. Cross-check [issue-connection-skew](issue-connection-skew.md) → churn.
  2. **Unprepared queries** — statements that are never prepared can do catalog lookups on **every execution**.
  3. The rare **prepared query that still does catalog lookups** each time.
  4. **Parallel queries** — each parallel worker performs its own catalog lookups.

## Aggregate vs. group vs. balance

- **Aggregate** the total first (is it above baseline at all?), then **group `by (service_method)`** — heartbeats scaling with node count are healthy; catalog reads scaling with *query or connection* volume are the problem.
- The balance lens mostly does **not** apply to the master leader itself (one leader serving everything is by design). It *does* apply to the **drivers**: uneven new-connection rate across nodes points at the client/pool responsible.
- Trend over a deploy/restart window: a sawtooth of catalog reads aligned with pool recycling is the churn signature.

## Confirm vs. rule out

- **Confirm churn-driven:** catalog-read rate tracks the new-connections rate → fix pooling (raise pool min/idle so connections are long-lived; smart-driver/pool distribution per [issue-connection-skew](issue-connection-skew.md)).
- **Confirm unprepared-query-driven:** connections are stable but catalog reads track query throughput → find the offenders via `pg_stat_statements` (`catalog_wait_time`, driver not using prepared statements / server-side prepared statements disabled) → [`yb-query-analysis`](../../yb-query-analysis/SKILL.md).
- **Rule out (healthy):** master RPCs ≈ baseline and dominated by `TSHeartbeat` → no action; a single hot master *instance* alone is not a finding.

## Where to look next

- Pooling / connection lifecycle fixes → [issue-connection-skew](issue-connection-skew.md) and the [`ysql` smart-drivers reference](../../ysql/references/smart-drivers.md).
- Per-query catalog cost and prepared-statement usage → [`yb-query-analysis` pgss reference](../../yb-query-analysis/references/pgss-analysis.md).
- Catalog-cache tuning (e.g. preload gflags) is version-dependent — verify options against the docs for the running version and route any GFlag change through [`yba-api`](../../yba-api/SKILL.md) with explicit user approval.
