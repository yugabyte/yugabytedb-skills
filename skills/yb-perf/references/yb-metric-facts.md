# YugabyteDB metric behaviour and sources

What metrics mean and where to read them, limited to facts current models often get wrong.
"(live)" = checked on a 2025.2.1 RF3 cluster; "(docs)" = YugabyteDB docs.

## Reading the series

- `is_raft_leader` (2025.2 default export): one series per table per node whose value is the number of that table's tablets the node leads. Leaders per node = `sum by (exported_instance)`; never `count(is_raft_leader == 1)` (live).
- RPC percentiles are gauges with a `quantile` label, not histograms: take `max_over_time` of the `quantile="p99"` series; `rate(_sum)/rate(_count)` gives the mean (live).
- Tablet size has no gauge: sum regular and intents SST sizes per `tablet_id` (live).
- Tombstones show as `rocksdb_number_db_seek` / `next` per operation rising while rows scanned stay flat.

## Tablet limits and splitting

- Limit = `tablet_replicas_per_gib_limit` (default 1462, live) x GiB of tablet-overhead memory (`tablet_overhead_size_percentage` of tserver memory), and/or `tablet_replicas_per_core_limit` (0 = off, live).
- `enforce_tablet_replica_limits` (master) default differs by version: true in 2024.2 / 2025.1 docs, false on 2025.2.1 (live). Check the flag before calling the limit enforced.
- A rejected CREATE TABLE says "...would cause the total running tablet replica count to exceed the safe system maximum" (docs).
- Master UI `:7000` > Tablet Servers > Universe Summary shows Active Tablet-Peers and Tablet Peer Limit (docs).
- Automatic split low phase: `tablet_split_low_phase_size_threshold_bytes` 128 MiB (live).

## Sources

- YBA bundles Prometheus on `:9090`; on Kubernetes it runs as a container in the YBA (`yugaware`) pod. Scope a universe with `node_prefix` = `universeDetails.nodePrefix` from `GET /api/v1/customers/{cUUID}/universes/{uUUID}`; if the `yba-api` skill is installed, its Prometheus reference covers connection and auth.
- YBA REST proxy for dashboard metrics: `POST /api/v1/customers/{customerUUID}/metrics`.
- A node's `/prometheus-metrics` accepts `priority_regex=<regex>` (used by YBA; narrows the payload server-side but is not an exact name filter -- server-level metrics still come back, live) and `show_help=false`; raw names only, no relabelling.
- Aeon's public REST API has only a small curated cluster-metrics set (ops/s, average latency, CPU, disk); full metrics need Aeon metrics export.
