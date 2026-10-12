# YugabyteDB metric and label names

Exact names, labels and units, limited to those current models often get wrong. "(live)" =
seen on a 2025.2.1 RF3 cluster behind a YBA-style relabelling Prometheus; "(YBA)" = YBA
convention not present on that stack. Discover anything else with
`/api/v1/label/__name__/values` and `/api/v1/series?match[]=...` rather than guessing.

## Scope and node labels

- Per-node identity after YBA-style relabelling: `exported_instance` (copied from the scrape's node label) (live).
- YBA scopes a universe with `node_prefix="<universeDetails.nodePrefix>"` plus `export_type` = `tserver_export`, `master_export`, `ysql_export`, `cql_export` or `node_export` (YBA, docs). A self-managed stack may use `job` instead.
- Kubernetes cAdvisor series carry no `node_prefix`: scope by `namespace`, group by `pod` (YBA: `pod_name`).

## RPC latency (relabelled)

- Raw `handler_latency_yb_<server>_<service>_<method>` becomes `rpc_latency` (`_sum`, `_count`, and the bare gauge) with labels `server_type`, `service_type`, `service_method` (live).
- `server_type` values: `yb_tserver`, `yb_master`, `yb_ysqlserver` (YSQL statements), `yb_cqlserver` (YCQL; `service_type="SQLProcessor"` for statements), `yb_consensus` (Raft `ConsensusService`, e.g. `UpdateConsensus`), `yb_cdc`, `yb_server`, `yb_client` (live).
- Tserver reads and writes: `rpc_latency_sum/_count{service_type="TabletServerService", service_method="Read"|"Write"}` (live).
- Percentiles: no `_bucket` series exist, so `histogram_quantile` returns nothing. The bare `rpc_latency` (raw `handler_latency_*`) is a gauge with `quantile` = `p50`, `p95`, `p99`, `mean`, `min`, `max` (live). `yb_ysqlserver` statements have no quantile series (live).
- `rpc_irate_rps` = irate of `rpc_latency_count`: ops/s per method and node (YBA recording rule; also live here).

## Per-table metrics

- Labels: `table_name`, `table_id`, `namespace_name`, `table_type` (live).
- `table_type`: `PGSQL_TABLE_TYPE` = YSQL; `YQL_TABLE_TYPE` = YCQL and system tables; `TRANSACTION_STATUS_TABLE_TYPE` = transaction status (live).
- `namespace_name` `system` and `system_postgres` = internal tables (live).
- `ql_read_latency` / `ql_write_latency` (`_sum`, `_count`): per table per tserver (live).
- `rocksdb_number_db_seek`, `_next`, `_prev`: per table (live).
- `is_raft_leader`: per table in the 2025.2 default export (no `tablet_id`); see yb-metric-facts.md. Masters export it too (live).
- `transaction_conflicts`: per table, tserver and master (live).

## Tablets, splitting and limits

- `ts_live_tablet_peers`, `ts_supportable_tablet_peers` (-1 = no limit): tserver gauges (live; docs).
- `create_table_too_many_tablets`, `split_tablet_too_many_tablets`: master counters of requests rejected by the tablet replica limit (live).
- `split_operations_inflight`: tablet splits in progress (live).
- `num_tablet_peers_undergoing_rbs`: peers in remote bootstrap (live).
- No tablet-size gauge: use `rocksdb_current_version_sst_files_size` + `intentsdb_rocksdb_current_version_sst_files_size` (live).
- `mem_tracker_server_Tablets_overhead`: memory used by per-tablet overhead (live).
- `tablet_leaders`: absent on some builds, including 2025.2.1 self-managed (live).

## Pressure, rejections and queues

- `leader_memory_pressure_rejections`, `follower_memory_pressure_rejections`, `operation_memory_pressure_rejections` (live).
- `majority_sst_files_rejections`: writes rejected because a tablet has too many SST files (live).
- `all_operations_inflight`; `rpcs_queue_overflow`, `rpcs_timed_out_in_queue` (tserver and master) (live).
- `compaction_tasks` gauge with `state_type` e.g. `Active Background`, `Non-Active Full` (non-active = queued) (live).
- `rocksdb_stall_micros`: write-stall time (live).
- `wait_queue_num_waiters`: transactions waiting on locks (live).

## YSQL server

- `yb_ysqlserver_active_connection_total` (busy backends), `yb_ysqlserver_new_connection_total` (rate = churn), `yb_ysqlserver_max_connection_total`, `yb_ysqlserver_connection_over_limit_total` (refused) (live).
- `yb_ysqlserver_CatalogCacheMisses_count` (also `service_method="CatalogCacheMisses"` under `SQLProcessor`) (live).

## Host and container CPU

- VM: node_exporter `node_cpu_seconds_total` (YBA recording rules `node_cpu_usage*` exist only where loaded).
- Kubernetes: cAdvisor `container_cpu_usage_seconds_total{container="yb-tserver"}`; YBA exposes `container_cpu_usage{container_name="yb-tserver"}` in cores (YBA).
