# Playbook: high memory / OOM / memory-pressure rejections

> *Starting point — to be expanded.* YugabyteDB manages memory through internal **mem trackers** (a hard cap derived from RAM and `*_memory_limit` flags), not just OS RSS. The most actionable signal is not "RAM is high" — it's **whether the server is rejecting work due to memory pressure**, which is an unambiguous "you're over the limit" event.

## Symptoms

- Tserver/master RSS climbing toward the host limit; OOM-kills (process restarts, gaps in series).
- Errors / latency spikes correlated with `*_memory_pressure_rejections` > 0.
- Slow steady growth (possible leak / unbounded cache) vs. sharp spikes (load bursts, large transactions).

## Metrics to read

| Scope | Metric | Notes |
|---|---|---|
| Pressure | `leader_memory_pressure_rejections`, `follower_memory_pressure_rejections`, `operation_memory_pressure_rejections` (`*_memory_pressure_rejections`) | **The smoking gun.** Any sustained non-zero rate = over the soft/hard limit. |
| Pressure | `rpc_inbound_calls_rejected_because_memory_pressure` | RPCs shed because the server is out of memory budget. |
| Pressure | `majority_sst_files_rejections` | Writes rejected because too many SST files (memory+compaction backpressure). |
| Tracker | `mem_tracker` | Root consumption vs. the cap. |
| Tracker | `mem_tracker_RegularDB_MemTable`, `mem_tracker_IntentsDB_MemTable` (+ `*_server_PerTablet_*`) | Where the memory is going — memtables (write buffers) vs. intents (uncommitted txns). |
| Process | `yb_process_memory_kb` (`{process="total_count"}` for the node roll-up) | Per-process RSS. |
| Node (VM) | `node_memory_MemAvailable_bytes`, `node_memory_MemTotal_bytes` | Host headroom. **Absent on Kubernetes.** |
| Node (k8s) | `container_memory_working_set_bytes` / `container_memory_usage_bytes` (`container_name=~"yb-tserver\|yb-master"`) vs. `container_spec_memory_limit_bytes` | Pod RSS vs. limit. Scope by `namespace`/`pod_name` — **no `node_prefix`**; confirm the exact metric name via discovery (see [Platform fork](finding-metrics.md#platform-fork-vm-vs-kubernetes-read-this-before-querying-cpumemorydisk)). |

## Aggregate vs. group vs. top-N vs. balance

- **Balance check:** memory pressure usually shows up on one node first, so a high coefficient of variation (`stddev/avg`, see [SKILL.md](../SKILL.md#the-balance-lens-outlier-detection)) on per-node RSS / `mem_tracker` is an early hotspot signal — `topk(1, …)` names the node before rejections even start.
- **Rejections: group `by (exported_instance)`** — pressure usually hits the hottest node first, so a per-node view doubles as a hotspot signal.
  ```promql
  sum(rate(leader_memory_pressure_rejections{node_prefix="<prefix>"}[5m])) by (exported_instance)
  ```
- **RSS / mem_tracker:** group `by (exported_instance)` and compare against the limit, not in isolation. A value is only meaningful relative to the cap.
- **Breakdown:** group the `mem_tracker_*` sub-trackers to attribute growth (memtables vs intents vs block cache).
- **Top-N tablets** via `*_PerTablet_*` trackers if one tablet hoards memtable memory.
- **Aggregate** only for "total fleet memory headroom" capacity questions.

## Trends that are significant

- **Any sustained `*_memory_pressure_rejections` rate > 0** → the cluster is over budget *now*; this is the headline finding, regardless of what RSS looks like.
- **Monotonic RSS growth that never plateaus** under steady load → leak or unbounded growth (cache, connections, intents from long-running/abandoned transactions). Correlate with `mem_tracker` sub-trackers and connection counts.
- **RSS that rises and falls with load** and stays clear of the limit → healthy.
- **`IntentsDB_MemTable` climbing** → large/long uncommitted transactions piling up intents; cross-check `transaction_conflicts`, `expired_transactions`.
- **`majority_sst_files_rejections` > 0** → compaction can't keep up; ties into the [disk & I/O playbook](issue-disk-io.md).

## Confirm vs. rule out

- **Confirm over-limit:** rejection metrics non-zero → reduce memory demand (fewer tablets per node, lower per-component limits' pressure, throttle writes) or add RAM/nodes.
- **Confirm leak vs. burst:** leak = growth continues after load drops; burst = tracks load and recovers. Only the former needs a restart/upstream fix.
- **Rule out "high but fine":** RSS high but well under the limit and zero rejections → not a problem; YugabyteDB deliberately uses available memory for caches.

## Where to look next

- Over-limit on one node only → also run the [hotspot playbook](issue-hotspots.md).
- Intents-driven growth → investigate long-running transactions (application side) and conflict rates.
- SST-rejection driven → [disk & I/O playbook](issue-disk-io.md) for compaction.
- Tuning memory limits / OOM config → [yba-api](../../yba-api/SKILL.md) runtime config / gflags.
