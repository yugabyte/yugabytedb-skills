# Playbook: connection skew

> *Starting point — to be expanded.* Connection skew is **client connections unevenly spread across nodes**: one tserver holds far more SQL/CQL connections than its peers. Because every connection consumes CPU, memory, and a backend slot, a skewed node becomes a de-facto [hotspot](issue-hotspots.md) even when the *data* is perfectly distributed. This is a pure **balance-lens** problem (see [SKILL.md](../SKILL.md#the-balance-lens-outlier-detection)); YugabyteDB's [Performance Advisor](https://docs.yugabyte.com/stable/yugabyte-platform/alerts-monitoring/performance-advisor/) flags it when one node carries ~50%+ more connections than the others.

## Symptoms

- One node shows higher CPU / memory / ops than the rest, but tablet leaders and data size are balanced.
- Application errors like "too many clients" / connection refused while total capacity looks unused — connections piled onto one node hit `max_connections` there first.
- A single app server or a non-balancing client/proxy funnels traffic to one endpoint.

## Metrics to read

| Layer | Metric | Notes |
|---|---|---|
| YSQL | `yb_ysqlserver_active_connection_total` | Active backends per node. *(Confirm exact name via discovery — see [`finding-metrics.md`](finding-metrics.md); connection metric names vary by version.)* |
| YSQL | `yb_ysqlserver_connection_total` / `*_new_connection_total` | Cumulative / new connections — `rate()` shows churn (connect-storm vs. steady pool). |
| YCQL | `rpc_connections_alive{service_type=...}` | Live client connections at the RPC layer. |
| Pooler | `ysql_conn_mgr_*` (e.g. `ysql_conn_mgr_num_pools`, `ysql_conn_mgr_max_client_connections`) | Only when the **built-in YSQL connection manager** is enabled. Exact series vary by version — **enumerate via discovery** (`/api/v1/label/__name__/values` → grep `conn_mgr`) rather than assuming names. |
| Context | `node_cpu_usage_avg`, `rpc_irate_rps` (per node) | Confirm the connection-heavy node is also the resource-heavy one. |

## Aggregate vs. group vs. balance

- **Balance is the whole point.** Group connections `by (exported_instance)` and quantify the spread:
  ```promql
  # Outlier ratio — Performance Advisor's rule of thumb: > ~1.5 (a node 50%+ above the mean) is skewed.
  max(sum(yb_ysqlserver_active_connection_total{node_prefix="<prefix>"}) by (exported_instance))
  /
  avg(sum(yb_ysqlserver_active_connection_total{node_prefix="<prefix>"}) by (exported_instance))
  ```
  `topk(1, …)` names the overloaded node; coefficient of variation (`stddev/avg`) works too.
- **Don't aggregate to a total** for skew — a healthy total with a lopsided distribution is exactly the failure this playbook catches.
- **Churn vs. steady:** `rate()` on the cumulative connection counter separates a connection *storm* (no pooling, reconnect loop) from a stable-but-skewed pool.

## Concurrency sizing — is the *level* of active connections right?

Beyond skew, judge the **total active** connections against the cluster's cores (per-node vCPUs × nodes), calibrated by the workload's read/write mix (establish it first — [workload sweep §1.1](workload-sweep.md#11-throughput-by-statement-type--the-readwrite-mix)):

- A **write-heavy** workload can saturate at **~1 active connection per core**.
- The most **read-heavy** workload (with a little write) tops out around **4–8 active connections per core**.
- Well **below** these → **over-serialized**: the app isn't offering enough concurrency to use the cluster (throughput capped by the client, not the database).
- Well **above** → **over-subscribed**: backends queue, latency inflates. Aim for the sweet spot between the two.

**New-connection churn has a second cost besides the reconnect itself:** each fresh backend's first queries do catalog cache lookups → catalog cache misses → added latency *and* load on the single master leader. A sustained `rate(*_new_connection_total)` means the pool's **min/idle size is too low** — fix the pool, and check the master side in [issue-master-catalog](issue-master-catalog.md).

## Trends that are significant

- **Sustained `max/avg` > ~1.5 across nodes** (transient skew during a deploy or rolling restart is normal — let it settle).
- **Total connections approaching per-node `max_connections`** on the hot node while others sit idle → imminent connection-refused errors.
- **High `*_new_connection_total` rate** → no/poor pooling; each request reconnects (expensive, and amplifies skew).
- **Connections at/over the limit** — `yb_ysqlserver_connection_over_limit_total` rising, or active near `yb_ysqlserver_max_connection_total` → backends exhausted on that node; with the connection manager enabled, a rising `ysql_conn_mgr_*` queue/pool series says the same.
- Connection skew **tracking** CPU/ops skew on the same node → connections are the *cause* of that node's load.

## Confirm vs. rule out

- **Confirm skew is the root cause:** the connection-heavy node is also the CPU/ops-heavy one, *and* tablet leaders / data are balanced → fix client distribution, not the schema.
- **Rule out (it's data skew, not connections):** connections are even but ops/CPU are not → this is a [hotspot](issue-hotspots.md), not connection skew.
- **Find the why:** is a YugabyteDB **smart driver** in use (it load-balances connections across nodes), or a plain PostgreSQL driver pointed at one host? Is a proxy/LB in front pinning to one backend? Is the client setting topology keys that collapse onto one zone?

## Where to look next

- Enable client-side load balancing with a **YugabyteDB smart driver** (`load-balance=true`, topology keys) — see [`ysql` smart-drivers reference](../../ysql/references/smart-drivers.md).
- Add/right-size **connection pooling**, or enable the **built-in YSQL connection manager**, to bound and balance backends.
- Put a balancing endpoint (or all node addresses) in the connection string instead of a single host.
- The resulting resource imbalance on the hot node → [issue-hotspots](issue-hotspots.md), [issue-cpu](issue-cpu.md), [issue-memory](issue-memory.md).
