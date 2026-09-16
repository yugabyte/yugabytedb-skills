# Playbook: quick health check (triage gate)

> *Start here when the symptom is unknown* — "the universe feels slow", "is everything OK?", or no symptom at all. This is a **fast, token-light gate**: pull a handful of vitals, compare each against a coarse threshold, and either (a) route to the specific playbook for whatever tripped, or (b) report "nothing obvious — green." It is deliberately shallow; depth lives in the per-issue playbooks. If the user actually wants a **deliberate broad review** (characterise the workload, enumerate every point of interest) rather than a fast gate, run the [guided workload sweep](workload-sweep.md) instead — its coverage is a superset of these vitals.

## How to run it

Fire the vitals below as **instant** queries (cheap; see the one-metric snippets in [`data-sources.md`](data-sources.md)), one per row. For each, apply both an **absolute** check (is the level bad?) and a **balance** check (is one member an outlier? — `max/avg`, see [SKILL.md](../SKILL.md#the-balance-lens-outlier-detection)). Stop expanding a vital as soon as it routes; you're triaging, not diagnosing.

Substitute `node_prefix="<prefix>"` per the [yba-api Prometheus reference](../../yba-api/references/prometheus.md#identifiers-you-need-from-the-yba-api). Queries assume the relabelled/recording-rule schema; translate names via [`finding-metrics.md`](finding-metrics.md) if scraping nodes directly.

**Detect the platform first.** Host-resource metrics differ between VM and Kubernetes universes (see [`finding-metrics.md` → Platform fork](finding-metrics.md#platform-fork-vm-vs-kubernetes-read-this-before-querying-cpumemorydisk)). The CPU/disk rows below give both forms — on **Kubernetes** the `node_*` queries return empty, so use the `container_*`/`kubelet_*` variant scoped by `namespace`/`pod_name` (no `node_prefix`).

## Vitals and routing thresholds

| # | Vital | Quick query (instant) | Trips when… | Route to |
|---|---|---|---|---|
| 0 | **Nodes reporting** | `up{node_prefix="<prefix>"}` | any `0`, or fewer series than expected nodes | a node/process is down — investigate before trusting other metrics |
| 1 | **CPU** | VM: `node_cpu_usage_avg{node_prefix="<prefix>", mode="total"}` · k8s: `sum(container_cpu_usage{namespace="<ns>", container_name="yb-tserver"}) by (pod_name)` | any node > ~80% (VM) / near its core limit (k8s), or `max/avg` > ~1.3 | [issue-cpu](issue-cpu.md) (high) / [issue-hotspots](issue-hotspots.md) (skewed) |
| 2 | **Memory pressure** | `sum(rate(leader_memory_pressure_rejections{node_prefix="<prefix>"}[5m])) by (exported_instance)` | any non-zero | [issue-memory](issue-memory.md) |
| 3 | **Throughput** | `sum(rpc_irate_rps{node_prefix="<prefix>", service_type="TabletServerService"}) by (exported_instance)` | `max/avg` > ~1.3 (skew) or unexpectedly near zero | [issue-hotspots](issue-hotspots.md) — then **decompose** by API (YSQL/YCQL `server_type`), read/write, and `table_name` before concluding / [issue-latency-throughput](issue-latency-throughput.md) |
| 4 | **Latency (p99)** | `histogram_quantile(0.99, sum(rate(rpc_latency_bucket{node_prefix="<prefix>", server_type=~"yb_ysqlserver\|yb_cqlserver"}[5m])) by (le, service_method))` | a statement type well above its baseline | [issue-latency-throughput](issue-latency-throughput.md) |
| 5 | **Disk full** | VM: `max(node_filesystem_used_bytes{node_prefix="<prefix>", mountpoint=~"/mnt/d.*"} / node_filesystem_size_bytes{...}) by (exported_instance)` · k8s: `max(kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes) by (persistentvolumeclaim)` | any node/PVC > ~75% | [issue-disk-io](issue-disk-io.md) |
| 6 | **Compaction/write stalls** | `sum(rate(rocksdb_stall_micros{node_prefix="<prefix>"}[5m])) by (exported_instance)` + `majority_sst_files_rejections` | any non-zero | [issue-disk-io](issue-disk-io.md) |
| 7 | **Connection balance & churn** | `sum(yb_ysqlserver_active_connection_total{node_prefix="<prefix>"}) by (exported_instance)` ; churn: `sum(rate(yb_ysqlserver_new_connection_total{node_prefix="<prefix>"}[5m]))` *(confirm names via discovery)* | `max/avg` > ~1.5 (skew), or a sustained new-connection rate (pool churn) | [issue-connection-skew](issue-connection-skew.md) — churn also pressures the master → [issue-master-catalog](issue-master-catalog.md) |
| 8 | **Errors / contention** | `sum(rate(glog_error_messages{node_prefix="<prefix>"}[5m]))`, `sum(rate(transaction_conflicts{node_prefix="<prefix>"}[5m]))` | rising error log rate or a conflict spike | [issue-latency-throughput](issue-latency-throughput.md) (conflicts) / inspect logs |
| 9 | **Replication & leader balance** | `max(follower_lag_ms{node_prefix="<prefix>"})` ; leaders per tserver `count(is_raft_leader{node_prefix="<prefix>", exported_instance=~".*tserver.*"} == 1) by (exported_instance)` | lag in the seconds, or leader count `max/avg` > ~1.3 | check Raft/placement (master UI, [yba-api](../../yba-api/SKILL.md)) |
| 10 | **Tablet limit** | `ts_live_tablet_peers{node_prefix="<prefix>", exported_instance=~".*tserver.*"}` ; `split_tablet_too_many_tablets{node_prefix="<prefix>"}` | `live/supportable` > ~0.8 (when `ts_supportable_tablet_peers > 0`), or `split_tablet_too_many_tablets` non-zero | [issue-tablet-limits](issue-tablet-limits.md) |
| 11 | **Master RPC load** | `sum(rpc_irate_rps{node_prefix="<prefix>", server_type="yb_master"})` | sustained well above the steady-state baseline (roughly ~5 × node count; establish the universe's own quiet baseline) | [issue-master-catalog](issue-master-catalog.md) |

The thresholds are coarse trip-wires for routing, **not** diagnoses — they're intentionally sensitive. A tripped vital means "look here next," not "this is the problem."

A slow-burn pattern the instant sweep can miss: a **single table or query degrading over time with flat data size** (a queue / soft-delete table accumulating tombstones, or wide-row update churn). It won't reliably trip a vital above — check the seek-amplification trend in [issue-disk-io](issue-disk-io.md) and the query-layer scan ratio in [`yb-query-analysis`](../../yb-query-analysis/references/pgss-analysis.md) when a user reports gradual slowdown.

## Interpreting the sweep

- **One vital tripped** → go straight to its playbook.
- **Several tripped** → they're usually related; pick the *most upstream* cause. Rough precedence: a **down node** (0) → **disk full / stalls** (5, 6) → **memory pressure** (2) → **CPU** (1) → **latency** (4). Resource exhaustion shows up as latency/CPU symptoms, so fix the resource before chasing the symptom. A **balance** trip (1/3/7 with high `max/avg`) on any vital points at [hotspots](issue-hotspots.md) or [connection skew](issue-connection-skew.md) as the common root.
- **Nothing tripped** → report green (template below) and ask the user for the specific concern (a time window, a slow query, an alert) so you can target a playbook rather than sweep again.

## "Everything looks OK" template

> Quick health check on `<universe>` over the last `<window>` — no obvious problems:
> - All `<N>` nodes reporting; CPU ≤ `<x>%` (balanced), no memory-pressure rejections.
> - Throughput `<...>` ops/sec, evenly distributed; p99 latency `<...>` ms, within normal range.
> - Disk ≤ `<x>%` on the fullest node; no write stalls or SST rejections.
> - Connections balanced; no error-log spike or replication lag.
>
> If you have a specific symptom (a slow query, an alert, a time window when it was bad), tell me and I'll dig into the matching area.

Always state the **window** you checked — a 5-minute instant sweep can miss an intermittent problem. If the user reports an issue you can't see now, widen to a range query around when it occurred (see [yba-api Prometheus reference](../../yba-api/references/prometheus.md)).
