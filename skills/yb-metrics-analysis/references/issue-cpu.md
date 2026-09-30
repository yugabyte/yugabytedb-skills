# Playbook: high CPU

> *Starting point — to be expanded.* High CPU is either **work the cluster is genuinely doing** (throughput up, queries expensive) or **overhead** (compaction, GC, context-switch churn, a hot node). Separate *whole-cluster* CPU from *per-node* CPU early — they point at different causes.

## Symptoms

- Latency climbs while throughput is flat or falling.
- One or all nodes pinned near 100%.
- CPU spikes correlate with compaction, backups, schema changes, or a traffic burst.

## Metrics to read

| Scope | Metric | Notes |
|---|---|---|
| Node (VM) | `node_cpu_usage_avg{node_prefix="<prefix>", mode="total"}` | Per-node CPU, all non-idle modes (recording rule). Raw: `100 - rate(node_cpu_seconds_total{mode="idle"}[…])*100`. **Absent on Kubernetes** — use the row below. |
| Node (k8s) | `container_cpu_usage{namespace="<ns>", container_name=~"yb-tserver\|yb-master"}` grouped `by (pod_name)` | Cores used; compare against `container_spec_cpu_quota`/`_period` for % of the pod's limit. cAdvisor metrics carry **no `node_prefix`** — scope by `namespace`/`pod_name` (see [Platform fork](finding-metrics.md#platform-fork-vm-vs-kubernetes-read-this-before-querying-cpumemorydisk)). |
| Process | `cpu_utime_irate`, `cpu_stime_irate` | Per-process user vs system CPU (tserver/master). High system % → syscalls/IO/context-switching. |
| Process | `process_user_cpu_seconds_rate`, `process_system_cpu_seconds_rate` | Same, with a `process="total_count"` rollup. |
| Context | `rpc_irate_rps` | Is CPU tracking real ops/sec? |
| Context | `involuntary_context_switches_irate`, `voluntary_context_switches_irate`, `spinlock_contention_time_irate` | Contention / scheduling pressure. |
| Context | `rocksdb_compact_*_bytes`, `rocksdb_stall_micros` | Background compaction burning CPU. |

## Aggregate vs. group vs. top-N vs. balance

- **Balance check first** (the "one node high CPU" case): is CPU evenly spread or is one node an outlier? Compute the coefficient of variation across nodes before deciding it's a capacity problem. **VM:**
  ```promql
  stddev(node_cpu_usage_avg{node_prefix="<prefix>", mode="total"})
  / avg(node_cpu_usage_avg{node_prefix="<prefix>", mode="total"})
  ```
  **Kubernetes** (cAdvisor — group by pod, no `node_prefix`):
  ```promql
  stddev(sum(container_cpu_usage{namespace="<ns>", container_name="yb-tserver"}) by (pod_name))
  / avg(sum(container_cpu_usage{namespace="<ns>", container_name="yb-tserver"}) by (pod_name))
  ```
  CoV > ~0.3 → one or few nodes are hot → this is really a [hotspot](issue-hotspots.md); `topk(1, …)` names the node/pod. Low CoV with high absolute CPU → genuine cluster-wide capacity/workload question. Realistic expectations when reading the spread: **~20% top-to-bottom variance across nodes is acceptable** (load balancers and tablet balance are never perfect); **one node at 80–100% while the rest idle is a serious problem** even when the cluster total looks fine.
- **Per-node, grouped:** read each node's level — VM `node_cpu_usage_avg{mode="total"} by (exported_instance)`; k8s `container_cpu_usage{...} by (pod_name)`.
- **Split user vs system:** group `cpu_utime_irate` and `cpu_stime_irate` separately. High *user* CPU → query/compaction compute; high *system* CPU → I/O, networking, context switches.
- **Top-N processes:** compare tserver vs master CPU per node — master should be light; a hot master suggests metadata/heartbeat load or too few masters.
- **Aggregate (fleet avg)** only to answer "do we need more cores cluster-wide?" — not for diagnosing a single hot node.

## Trends that are significant

- CPU **rising while throughput is flat/falling** → not useful work: compaction backlog, lock contention, or inefficient plans. Cross-check `rocksdb_compact_*` and `spinlock_contention_time_irate`.
- CPU **tracking `rpc_irate_rps` linearly** → honest load; the cluster is at capacity for the current workload (scale out or optimise queries).
- **Sustained >80–85% per core** (not brief spikes) → headroom gone; tail latency will suffer. High CPU inflates the **higher percentiles non-linearly** — a node at ~90% CPU shows a far worse P90/P99 than one at ~40% for the same workload — so treat high CPU as a *tail-latency cause* when triaging a P99 complaint, not just a capacity number ([latency playbook](issue-latency-throughput.md)).
- **System CPU dominating** + high context switches → I/O-bound or oversubscribed host (noisy neighbour, too many tablets per core).
- CPU spikes **aligned with backup / compaction / DDL windows** → background work; consider throttling or rescheduling.

## Confirm vs. rule out

- **Confirm "real workload":** CPU and `rpc_irate_rps` move together, evenly across nodes → add capacity or tune queries (see the [latency playbook](issue-latency-throughput.md)).
- **Confirm "compaction/background":** user CPU up, `rocksdb_compact_*_bytes` and/or `rocksdb_stall_micros` up, ops/sec flat → it's the LSM engine; see [disk & I/O playbook](issue-disk-io.md).
- **Rule out hotspot:** if only one node is hot, switch to the [hotspot playbook](issue-hotspots.md) before adding capacity.

## Where to look next

- Even, capacity-bound CPU → scale out or right-size; optimise the heaviest statement types (top-N `service_method`).
- Single hot node → [hotspots](issue-hotspots.md).
- Background-work CPU → [disk & I/O](issue-disk-io.md) (compaction tuning, SST pressure).
