# Playbook: disk space & I/O saturation / compaction pressure

> *Starting point — to be expanded.* Two related problems share this playbook: **running out of disk** (capacity) and **I/O that can't keep up** (throughput/latency, usually compaction or WAL). YugabyteDB's LSM-tree storage (DocDB/RocksDB) makes both sensitive to write-heavy and skewed workloads.

## Symptoms

- Disk filling up; risk of a read-only / crashed node when a volume hits ~100%.
- Write latency rising; `rocksdb_stall_micros` non-zero; `majority_sst_files_rejections` > 0.
- Compaction backlog: SST file count or size climbing without plateau.
- High disk-busy %, queueing, or I/O latency.
- A specific table or query getting slower over time **with no data growth** — reads stepping over tombstones / stale row versions (a queue / soft-delete table, or a wide-column update churn pattern).

## Metrics to read

| Concern | Metric | Notes |
|---|---|---|
| Capacity (VM) | `node_filesystem_used_bytes` / `node_filesystem_size_bytes` (mount `=~"/mnt/d.*"`) | % full per data volume per node. **Absent on Kubernetes.** |
| Capacity (k8s) | `kubelet_volume_stats_used_bytes` / `kubelet_volume_stats_capacity_bytes` | % full per PVC (group `by (persistentvolumeclaim)`). cAdvisor/kubelet — no `node_prefix`. See [Platform fork](finding-metrics.md#platform-fork-vm-vs-kubernetes-read-this-before-querying-cpumemorydisk). |
| Capacity | `rocksdb_current_version_sst_files_size` | Logical data size (DocDB), per node/tablet. |
| Compaction | `rocksdb_current_version_num_sst_files` | SST file count — rising = compaction falling behind. |
| Compaction | `rocksdb_compact_read_bytes`, `rocksdb_compact_write_bytes` | Compaction throughput (write amplification). |
| Compaction backlog | active vs **queued** compaction/background task gauges *(confirm exact series via discovery — grep names for `compact`/`task`)* | A pile-up of **queued (non-active)** tasks = compaction isn't keeping up; healthy = tasks active and draining. Correlate with SST file count. |
| Background work | `num_tablet_peers_undergoing_rbs` | Remote bootstraps in flight — each competes for disk bandwidth (see below). |
| Backpressure | `rocksdb_stall_micros` | Time writes were stalled by the engine. **Non-zero is bad.** |
| Backpressure | `majority_sst_files_rejections` | Writes rejected due to too many SST files. |
| I/O (node) | `node_disk_io_time_irate` | Disk-busy seconds/sec → ~1.0 means saturated. |
| I/O (node) | `node_disk_read_bytes_irate`, `node_disk_write_bytes_irate`, `node_disk_reads_irate`, `node_disk_writes_irate` | Throughput & IOPS per node. |
| WAL | `log_wal_size`, `log_sync_latency_*`, `log_append_latency_*` | WAL volume and fsync latency. |
| Cache | `rocksdb_block_cache_(hit\|miss)`, `rocksdb_bloom_filter_(checked\|useful)` | Read efficiency — low hit ratio drives read I/O. |
| Read amplification | `rocksdb_number_db_seek`, `rocksdb_number_db_next` (rate) | Internal keys stepped over per read. Rises with **tombstones / stale versions** even when data size is flat — the signature of queue/soft-delete churn or wide-row updates. *(confirm exact names via discovery)* |

## Aggregate vs. group vs. top-N vs. balance

- **Balance check:** uneven disk usage or SST count across nodes (high `stddev/avg`, see [SKILL.md](../SKILL.md#the-balance-lens-outlier-detection)) means the *data* is skewed — fix distribution (a [hotspot](issue-hotspots.md)), don't just buy disk for everyone. `topk(1, …)` names the node/tablet.
- **Capacity:** watch the **fullest** volume — the cluster is constrained by its most-full node, not the average. Never report disk as a fleet average. **VM** (group `by (exported_instance)`):
  ```promql
  max(node_filesystem_used_bytes{node_prefix="<prefix>", mountpoint=~"/mnt/d.*"}
      / node_filesystem_size_bytes{node_prefix="<prefix>", mountpoint=~"/mnt/d.*"}) by (exported_instance)
  ```
  **Kubernetes** (per PVC):
  ```promql
  max(kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes) by (persistentvolumeclaim)
  ```
  No volume metric? `rocksdb_current_version_sst_files_size by (exported_instance)` shows DocDB data size per node as a usable proxy for *relative* growth/skew (not absolute % full).
- **Compaction/SST: group `by (exported_instance)`**, then **top-N `by (tablet_id)`** to find the tablet driving the backlog.
- **I/O saturation:** `node_disk_io_time_irate` per node; ~1.0 = a disk pinned busy.
- **Project capacity:** use `predict_linear(node_filesystem_used_bytes[6h], 86400*7)` to estimate time-to-full.

## Trends that are significant

- **Disk > ~75% and rising** on any node → act before it's full (a full data volume can take a node read-only). Use the growth slope to estimate runway.
- **`rocksdb_stall_micros` > 0 or `majority_sst_files_rejections` > 0** → write path is being throttled by the storage engine *now*; compaction can't keep up.
- **SST file count climbing monotonically** while writes are steady → compaction backlog (under-provisioned I/O, too many tablets, or compaction throttled too aggressively).
- **`node_disk_io_time_irate` near 1.0 sustained** → disk saturated; everything above it (write latency, compaction, WAL sync) will degrade. Disks need **spare bandwidth for background operations** — when a **remote bootstrap** is in flight (`num_tablet_peers_undergoing_rbs` > 0), confirm the disk can carry its throughput *without starving normal traffic*; a bootstrap on an already-busy disk degrades both.
- **Block-cache hit ratio falling** + read I/O rising → working set outgrew cache/RAM (ties to the [memory playbook](issue-memory.md)).
- **WAL sync latency spikes** → slow disk fsync; affects every write's commit latency.
- **`rocksdb_number_db_seek`/`_next` rate rising while `rocksdb_current_version_sst_files_size` is flat** → reads are stepping over **tombstones / stale row versions**, not handling more data — a soft-delete/queue table or wide-row update churn. Compaction reclaims the garbage, but the durable fix is at the query/schema layer. Distinguish from a *skewed* seek rate (one node/tablet ≫ rest), which is a [hotspot](issue-hotspots.md), not churn.

## Confirm vs. rule out

- **Confirm capacity problem:** a volume trending to full → add storage / nodes, drop unused data, or check for stuck snapshots/backups holding space.
- **Confirm compaction/I/O bottleneck:** stalls/rejections + rising SST count + disk near saturated → the disk is the limiter; faster disks, more nodes, or compaction tuning.
- **Rule out skew:** if only one node's disk/SST is high, it's a [hotspot](issue-hotspots.md) — fix distribution rather than buying disk for everyone.
- **Rule out transient:** brief SST/stall spikes during a load burst or post-bulk-load compaction are expected; look for *sustained* trends.

## Where to look next

- One-node disk/I/O skew → [hotspots](issue-hotspots.md).
- Cache-miss-driven read I/O → [memory playbook](issue-memory.md).
- Compaction burning CPU → [CPU playbook](issue-cpu.md).
- Capacity/expansion actions (add nodes, resize volumes) → [yba-api](../../yba-api/SKILL.md).
- Seek amplification from tombstone/version churn (queue tables, wide-row updates) → the fix is query-/schema-side: [`yb-query-analysis`](../../yb-query-analysis/references/pgss-analysis.md) (read-amplification from version churn).
