# Finding metrics by name (and YBA's relabelling)

YugabyteDB nodes export hundreds of metrics with overlapping naming. The single biggest source of confusion is that **the name a metric has in a query is often not the name the node exported** — YugabyteDB Anywhere (and any Prometheus that copies its config, like podman-yugabyte) rewrites metrics at scrape time via `metric_relabel_configs`, and adds derived series via recording rules. This guide is the decoder ring between the **raw** names (what you see scraping a node directly) and the **relabelled** names (what you query in YBA/Prometheus).

> Source of truth for the relabelling: YBA's Prometheus config, faithfully replicated in
> [`podman-yugabyte/tools/prometheus/prometheus.yaml`](https://github.com/yugabyte/podman-yugabyte/blob/main/tools/prometheus/prometheus.yaml)
> (`metric_relabel_configs`) and
> [`recording-rules-raw.yml`](https://github.com/yugabyte/podman-yugabyte/blob/main/tools/prometheus/recording-rules-raw.yml).
> If in doubt, read those files for the exact regexes.

## Step 0: discover, don't guess

Whatever the source, list what actually exists before building queries:

```bash
# All metric names (optionally scoped to a universe)
curl -sG "http://<prom>:9090/api/v1/label/__name__/values" \
  --data-urlencode 'match[]={node_prefix="<prefix>"}' | jq -r '.data[]' | sort | grep -i <substring>

# All label values a metric carries (e.g. which service_methods exist)
curl -sG "http://<prom>:9090/api/v1/series" \
  --data-urlencode 'match[]=rpc_latency_count{node_prefix="<prefix>"}' | jq '.data[0]'
```

Scraping a node directly instead? `curl -s http://<node>:9000/prometheus-metrics | grep -i <substring>`.

## The relabelling rules, in plain terms

YBA applies these `metric_relabel_configs` to **master and tserver** scrapes (and the label-copy rules to all YB jobs):

| Raw input | Becomes | Why it matters |
|---|---|---|
| `node` label | copied to **`exported_instance`** | `exported_instance` is the per-node identity you group/top-N by. The raw `node` label may also still be present. |
| `__address__` (`host:port`) | host part → **`node_prefix`** | In real YBA `node_prefix` is the **universe** prefix (`universeDetails.nodePrefix`); in the podman replica it's the scrape host. Either way it's the universe/scope selector. |
| `__name__` (every metric) | copied to **`saved_name`** *before* any rename | Lets you still select a metric by its **original** name after it's been renamed. Many YBA dashboard panels filter on `saved_name`. |
| `handler_latency_yb_<server>_<service>_<method>` | metric renamed to **`rpc_latency`** (keeping `_sum`/`_count`), and split into labels: **`server_type`**, **`service_type`**, **`service_method`** | This is the big one — see below. |

### The `handler_latency_*` → `rpc_latency` transform

Raw, on a node:

```
handler_latency_yb_tserver_TabletServerService_Read_count{...}   12345
handler_latency_yb_tserver_TabletServerService_Read_sum{...}     6789000
handler_latency_yb_cqlserver_SQLProcessor_SelectStmt_count{...}  999
```

After YBA relabelling, in Prometheus:

```
rpc_latency_count{server_type="yb_tserver",  service_type="TabletServerService", service_method="Read",       saved_name="handler_latency_yb_tserver_TabletServerService_Read_count", ...}
rpc_latency_sum{  server_type="yb_tserver",  service_type="TabletServerService", service_method="Read",       ...}
rpc_latency_count{server_type="yb_cqlserver",service_type="SQLProcessor",        service_method="SelectStmt", ...}
```

So the regex `handler_latency_(yb_[^_]*)_([^_]*)_([^_]*)(_sum|_count)?` maps:
- group 1 → `server_type` (`yb_tserver`, `yb_master`, `yb_cqlserver`, `yb_ysqlserver`)
- group 2 → `service_type` (`TabletServerService`, `SQLProcessor`, `ConsensusService`, `RaftServer`, …)
- group 3 → `service_method` (the RPC / statement, e.g. `Read`, `Write`, `SelectStmt`, `UpdateConsensus`)
- the metric itself → `rpc_latency` (a histogram: `_sum`, `_count`, and `_bucket` for quantiles)

**Practical consequence:** if you want RPC/statement latency or throughput in a relabelled source, query **`rpc_latency`** filtered by those split labels — *not* `handler_latency_*` (which won't exist there). If you're scraping a node directly, it's the reverse: only `handler_latency_*` exists.

## Recording rules (derived metrics that exist only when configured)

A Prometheus with YBA's recording rules also exposes pre-computed series. The ones you'll reach for most:

| Recorded metric | Definition | Use |
|---|---|---|
| `rpc_irate_rps` | `irate(rpc_latency_count[…])` | **Ops/sec** per service_method / node — the throughput workhorse. |
| `node_cpu_usage`, `node_cpu_usage_avg{mode="total"}` | irate of `node_cpu_seconds_total` | Per-core / per-node CPU without writing the `1 - idle` math each time. |
| `node_disk_*_irate`, `node_network_*_irate` | irate of node-exporter counters | Disk/network throughput per node. |
| `node_filesystem_used_bytes` | `node_filesystem_size_bytes - node_filesystem_free_bytes` | Disk used, normalised across node-exporter versions. |
| `cpu_utime_irate`, `cpu_stime_irate` | irate of `cpu_utime` / `cpu_stime` | Per-process (tserver/master) user/system CPU. |
| `process_user_cpu_seconds_rate`, `process_system_cpu_seconds_rate` | rate of `yb_process_cpu_seconds_total{type=…}` | Per-process CPU, with a `process="total_count"` rollup. |
| `process_io_read_rate`, `process_io_write_rate` | rate of `yb_process_io_kb_total{type=…}` | Per-process I/O. |
| `yb_process_memory_kb{process="total_count"}` | `sum without(process)` of per-process RSS | Total YB process memory per node. |

**These do not exist on a raw node scrape or a plain Prometheus.** If a query referencing one returns empty, either the recording rules aren't loaded (compute the underlying `irate`/`rate` yourself) or you're on the wrong source.

## Platform fork: VM vs. Kubernetes (read this before querying CPU/memory/disk)

**Host-resource metrics differ completely between VM and Kubernetes universes — and the two use different scope labels.** This is the #1 cause of "my query returns nothing." Verified against a live k8s YBA universe.

- **YugabyteDB process & DocDB metrics** (`rpc_latency`, `rpc_irate_rps`, `mem_tracker_*`, `rocksdb_*`, `is_raft_leader`, `*_memory_pressure_rejections`, `cpu_utime`/`cpu_stime`, `yb_process_*`) — **identical on both platforms.** Scope by `node_prefix`; per-node identity is `exported_instance`.
- **Host CPU / memory / disk / network** — platform-specific:

| Resource | VM universe (node-exporter) | Kubernetes universe (cAdvisor / kubelet) |
|---|---|---|
| Scope label | `node_prefix`, group by `exported_instance` | **`namespace`**, group by **`pod_name`** (cAdvisor metrics carry **no `node_prefix`**) |
| CPU | `node_cpu_usage_avg{mode="total"}`, `node_cpu_seconds_total` | `container_cpu_usage{container_name="yb-tserver"}` (cores), or `container_cpu_usage_seconds_total` |
| Memory | `node_memory_MemAvailable_bytes`, `node_memory_MemTotal_bytes` | `container_memory_working_set_bytes` / `container_memory_usage_bytes` *(confirm exact name via discovery)* |
| Disk space | `node_filesystem_used_bytes` / `_size_bytes` | `kubelet_volume_stats_used_bytes` / `kubelet_volume_stats_capacity_bytes` (PVC); or YB's `host_storage_mount_space_free_bytes` |
| Disk I/O | `node_disk_*_irate` | `container_fs_reads_bytes_total` / `container_fs_writes_bytes_total` |

On Kubernetes, the `node_*` (node-exporter) series and the `node_cpu_usage*`/`node_filesystem_used_bytes` **recording rules simply do not exist** — querying them returns empty. Use the container/kubelet column instead, scoped by `namespace`/`pod_name`.

**Detect the platform** before picking resource queries: if `container_cpu_usage` exists for your universe's namespace it's Kubernetes; if `node_cpu_seconds_total{node_prefix=...}` exists it's a VM. The k8s `namespace` is typically the same string as `node_prefix`, but confirm — and build the `pod_name=~...` regex from the universe's pods (see [yba-api Prometheus reference](../../yba-api/references/prometheus.md#identifiers-you-need-from-the-yba-api)).

### `tablet_leaders` may be absent — derive leader balance from `is_raft_leader`

The `tablet_leaders` gauge is **not exported on all builds** (absent on the verified k8s universe). The portable way to get per-tserver leader count is to count `is_raft_leader` (a per-tablet 0/1 gauge that exists everywhere):

```promql
count(is_raft_leader{node_prefix="<prefix>", exported_instance=~".*tserver.*"} == 1) by (exported_instance)
```

Use this instead of `tablet_leaders` for placement-balance checks. **Filter to tservers** (`exported_instance=~".*tserver.*"`) — `is_raft_leader` also covers masters (each the leader of its own 1 Raft group), which otherwise inflate a `max/avg` balance ratio. Verified: on a balanced 3-tserver universe the tserver-only ratio was 1.01, but ~1.34 when masters were included.

## Raw ↔ relabelled quick reference

| Concept | Raw (direct node scrape) | Relabelled (YBA / podman Prometheus) |
|---|---|---|
| RPC / statement latency | `handler_latency_yb_*_*_*{_sum,_count,_bucket}` | `rpc_latency{server_type,service_type,service_method}` |
| RPC / statement throughput | derive: `irate(handler_latency_..._count[…])` | `rpc_irate_rps{…}` (recording rule) |
| Per-node identity | `node`, `exported_instance` (if set) | `exported_instance` |
| Universe / scope selector | `node_prefix` (only if relabelled) | `node_prefix` |
| Original name after rename | — | `saved_name` |
| Node CPU | node-exporter `node_cpu_seconds_total` | `node_cpu_usage_avg{mode="total"}` etc. |
| Process CPU | `cpu_utime`, `cpu_stime`, `yb_process_cpu_seconds_total` | same + `*_irate` / `*_rate` recording rules |
| Process memory | `mem_tracker*`, `yb_process_memory_kb` | same + `yb_process_memory_kb{process="total_count"}` |
| Tablet leaders | `tablet_leaders` | `tablet_leaders` (unchanged) |
| RocksDB internals | `rocksdb_*` | `rocksdb_*` (unchanged) |

## Metric families worth knowing (names are stable across sources)

These keep their raw names through relabelling — only the latency/RPC family and the derived rates change.

- **RPC / API layer:** `rpc_latency*` (relabelled) / `handler_latency_*` (raw), `rpc_inbound_calls_created`, `rpc_inbound_calls_rejected_because_memory_pressure`.
- **Query layer (split by API):** filter `rpc_latency` / `rpc_irate_rps` on `server_type` — **`yb_ysqlserver`** (YSQL) vs **`yb_cqlserver`** (YCQL) — to separate the two query APIs. One is frequently idle; always check which API the traffic is in before diagnosing. Note that YCQL/`YQL_TABLE_TYPE` also backs **internal** system tables (most notably `system.cdc_state` for CDC), so storage-layer load can exist on YCQL-type tables even when *user* YCQL query ops are zero. Also: `ql_read_latency*`, `ql_write_latency*`, `write_lock_latency*`, `*_operations_inflight`.
- **Storage / RocksDB:** `rocksdb_current_version_num_sst_files`, `rocksdb_current_version_sst_files_size`, `rocksdb_compact_*_bytes`, `rocksdb_stall_micros`, `rocksdb_db_*_micros_*` (per-tablet read/write op counts), `rocksdb_block_cache_(hit|miss|add)`, `rocksdb_bloom_filter_(checked|useful)`, **`rocksdb_number_db_(seek|next|prev)`** — the sharpest per-tablet **read/seek skew** signal for hotspot hunting.
- **Memory:** `mem_tracker`, `mem_tracker_RegularDB_MemTable`, `mem_tracker_IntentsDB_MemTable` (and `*_server_PerTablet_*` variants), `*_memory_pressure_rejections`, `majority_sst_files_rejections`.
- **Raft / replication:** `is_raft_leader`, `follower_lag_ms`, `ts_live_tablet_peers`, `log_wal_size`, `log_*_latency_*`, `async_replication_committed_lag_micros`, `async_replication_sent_lag_micros`, `xcluster_consumer_replication_error_count`.
- **Transactions:** `transaction_conflicts`, `expired_transactions`.
- **Node / OS (node-exporter):** `node_cpu_seconds_total`, `node_memory_*`, `node_filesystem_*`, `node_disk_*`, `node_network_*`.
- **YSQL connection manager:** `ysql_conn_mgr_*` (pooling), often only meaningful when the built-in pooler is enabled.

### Per-table labels — prefer names over IDs

On YBA/Prometheus, the storage/RocksDB metrics carry human-readable identity labels in addition to the raw IDs:

| Label | Example | Use |
|---|---|---|
| `table_name` | `cdc_state`, `orders` | Group/top-N by this to **name the hot table directly** — far more useful than `table_id`. |
| `table_type` | `PGSQL_TABLE_TYPE` (YSQL), `YQL_TABLE_TYPE` (YCQL / internal system tables) | Tells you which API/engine owns the table. |
| `namespace_name` | `system`, `system_postgres`, `<your_db>` | Distinguishes **user** tables from **internal** ones (`system`, `system_postgres`). |
| `table_id` / `tablet_id` | hex id | Fall back to these only when no name label is present. |

So `sum(rate(rocksdb_number_db_seek[5m])) by (table_name, table_type, namespace_name, exported_instance)` names the hot table, says whether it's YSQL or YCQL, and whether it's a user or system table — in one query. (If `table_name` is absent — e.g. raw node scrape — only `table_id`/`tablet_id` are available.)

When a name you expect is missing, it is almost always one of: (a) wrong source (raw vs relabelled), (b) a recording rule that isn't loaded, or (c) the metric is gated behind `priority_regex` on the scrape. Re-run the discovery query in Step 0 before concluding it doesn't exist.
