# Playbook: tablet limits / too many tablets (and oversized tablets that won't split)

> *Starting point — to be expanded.* Two related tablet-count problems live here:
> 1. **Too many tablets** — a tserver hosts more tablet *peers* than its memory/CPU can comfortably back, hitting (or approaching) the enforced replica limit. Symptoms: `CREATE TABLE`/index fails with *"would cause the total running tablet replica count to exceed the safe system maximum"*, automatic splits get rejected, or per-tablet memory overhead crowds out the block cache.
> 2. **Oversized tablets that won't split** — a tablet has grown past the split threshold but isn't being split (splitting disabled, blocked by the replica limit, or an unsupported table type), so it stays a single large hotspot-prone shard.
>
> The replica limit is computed **per node from its memory/CPU spec and flags** — read it from a metric where possible, or derive it from the spec. Verified metric names against a live YBA universe.

## Symptoms

- DDL errors: *"The requested number of tablet replicas would cause the total running tablet replica count to exceed the safe system maximum."*
- Automatic tablet splits stop happening; `split_tablet_too_many_tablets` increments.
- High `mem_tracker_server_Tablets_overhead` — per-tablet overhead eating the tserver memory budget (ties to the [memory playbook](issue-memory.md)).
- One tablet far larger than the rest and not splitting; uneven load on the node that leads it ([hotspots](issue-hotspots.md)).
- Many small tablets from over-presplitting, lots of tables/indexes, or **CDC** (each stream adds tablet peers — this universe had hundreds of `..._PerTablet_CDC_*` trackers).

## Metrics to read

| Concern | Metric | Notes |
|---|---|---|
| Current count | `ts_live_tablet_peers` | Live tablet peers hosted **per tserver** (group `by (exported_instance)`). |
| The limit | `ts_supportable_tablet_peers` | The **computed limit** per tserver. **`-1` = limit not enforced** (the enabling flag is off) — see below; don't divide by it. |
| Utilization | `ts_live_tablet_peers / ts_supportable_tablet_peers` | Only meaningful when supportable `> 0`. >~0.8 = approaching the cap. |
| Overhead memory | `mem_tracker_server_Tablets_overhead`, `mem_tracker_server_Tablets_overhead_PerTablet*` | Memory consumed by per-tablet overhead — the resource the limit protects. |
| Split backlog | `tablet_split_candidates`, `outstanding_tablet_splits` | Master-side: tablets identified to split / splits in flight. |
| Split blocked by limit | `split_tablet_too_many_tablets` | **Splits rejected because the replica limit would be exceeded** — the smoking gun linking the two problems. Master-side. |
| Split activity | `automatic_split_manager_time`, `split_operations_inflight`, `ts_split_op_apply` | Is the split manager running / applying splits? |
| Tablet size | `rocksdb_current_version_sst_files_size` (+ `intentsdb_rocksdb_current_version_sst_files_size`) `by (table_name, tablet_id)` | **No dedicated "tablet size" gauge exists** — on-disk SST size per tablet is the proxy. Sum RegularDB + IntentsDB for the logical size. |
| Tablet health | `yb_node_leaderless_tablet_count`, `yb_node_underreplicated_tablet_count`, `num_tablet_peers_undergoing_rbs` | Side-effects of churn / over-limit. |

> **Scope note:** `ts_*_tablet_peers` and `mem_tracker_*` are **tserver** metrics; `tablet_split_candidates` / `outstanding_tablet_splits` / `split_tablet_too_many_tablets` are reported by the **master** (split orchestration). Filter `exported_instance` accordingly.

## How the limit is computed (and what to do when the metric says `-1`)

The per-node tablet-replica limit is driven by flags, not a fixed number:

- **`enforce_tablet_replica_limits`** (master) — turns the guardrail on. Default **on in v2024.2+**, off before. When off, `ts_supportable_tablet_peers` reports **`-1`** (unlimited) — exactly what the verified universe showed.
- **`tablet_overhead_size_percentage`** (tserver) — percentage of tserver memory reserved for per-tablet overhead. **This is what actually sizes the limit; if it's `0` the limit is effectively unlimited even with enforcement on.**
- **`tablet_replicas_per_gib_limit`** (tserver) — replicas allowed per GiB of that overhead budget.

Roughly: `limit per tserver ≈ (tserver_memory × tablet_overhead_size_percentage%) × tablet_replicas_per_gib_limit_per_GiB`, summed across tservers for the universe cap. **Prefer reading `ts_supportable_tablet_peers` directly** rather than recomputing.

**If `ts_supportable_tablet_peers` is `-1` (limit not enforced):** you can't compute utilization from metrics. Either (a) enable the guardrail (`enforce_tablet_replica_limits` + a non-zero `tablet_overhead_size_percentage`) so the metric populates and DDL is protected, or (b) estimate against the node spec — a common rule of thumb is to keep total tablet peers per tserver in proportion to its vCPUs (oversubscribing tablets-per-core drives context-switch and memory overhead; see [issue-cpu](issue-cpu.md), [issue-memory](issue-memory.md)). The master UI's **Universe Summary** also shows *Active Tablet-Peers* vs *Tablet Peer Limit*.

## Aggregate vs. group vs. balance

- **The limit is per-node, so always group `by (exported_instance)`.** A cluster total can look fine while one tserver is at its cap — and the busiest node hits the limit first.
  ```promql
  # Tablet-peer utilization per tserver (guard against the -1 sentinel)
  ts_live_tablet_peers{node_prefix="<prefix>", exported_instance=~".*tserver.*"}
  / (ts_supportable_tablet_peers{node_prefix="<prefix>", exported_instance=~".*tserver.*"} > 0)
  ```
- **Balance check:** skew in `ts_live_tablet_peers` across tservers (high `max/avg`, see [SKILL.md](../SKILL.md#the-balance-lens-outlier-detection)) means tablets aren't evenly placed — the heavy node approaches the cap while others have headroom.
- **Oversized tablets — top-N by size:**
  ```promql
  topk(10, sum(rocksdb_current_version_sst_files_size{node_prefix="<prefix>"}
              + intentsdb_rocksdb_current_version_sst_files_size{node_prefix="<prefix>"}) by (table_name, tablet_id))
  ```
  Compare the top values against `tablet_force_split_threshold_bytes` (default 100 GiB; low/high-phase thresholds 128 MiB / 10 GiB) — anything well over threshold that isn't a split candidate is **stuck**.

## Trends that are significant

- **`ts_live_tablet_peers / ts_supportable_tablet_peers` > ~0.8 and rising** → approaching the cap; DDL and splits will start failing. Act before 1.0.
- **`split_tablet_too_many_tablets` non-zero** → splits are *already* being rejected by the limit — the two problems have merged; you must add capacity or remove tablets before splitting can resume.
- **A tablet's SST size above the force-split threshold while `tablet_split_candidates` ignores it** → split is **blocked**, not pending — check the prevent-conditions below.
- **`mem_tracker_server_Tablets_overhead` a large share of the tserver mem budget** → too many tablets regardless of the count limit; block-cache/memtable space is being squeezed ([memory playbook](issue-memory.md)).
- **Tablet count climbing without data growth** → over-presplitting, runaway table/index creation, or CDC stream proliferation.
- **Split operations on large tablets lining up with periodic latency-percentile spikes** → the splits themselves are the tail-latency cause; see [issue-latency-throughput](issue-latency-throughput.md).

## Confirm vs. rule out

- **Confirm "too many tablets":** `ts_live_tablet_peers` near `ts_supportable_tablet_peers` on one or more tservers, and/or `split_tablet_too_many_tablets` > 0, and/or DDL erroring with the "safe system maximum" message.
- **Confirm "oversized & stuck":** a tablet's SST size ≫ the applicable phase threshold, it's not in `tablet_split_candidates`/`outstanding_tablet_splits`, and a known blocker applies. Splits are prevented when: **the replica limit would be exceeded** (`split_tablet_too_many_tablets`), `enable_automatic_tablet_splitting=false`, **colocated** tables, tables under **xCluster** replication (during bootstrap), tables using **TTL file expiration**, range-partitioned **index** tables during restore, or a backup/PITR/index-backfill is in progress.
- **Rule out (healthy):** `ts_supportable_tablet_peers` is `-1` *and* tablet counts are modest for the node spec, no DDL errors, tablets under threshold → not a limit problem (but consider enabling the guardrail proactively).
- **Rule out (it's a hotspot, not a count problem):** one node hot but tablet counts balanced and under limit → [hotspots](issue-hotspots.md), not tablet limits.

## Where to look next

- **Too many tablets:** scale out (more tservers spreads the per-node count), drop unused tables/secondary indexes, reduce per-table presplit (`ysql_num_shards_per_tserver` / `num_tablets` at create time), and review **CDC** stream count — each stream multiplies tablet peers.
- **Raise the ceiling deliberately:** increase `tablet_overhead_size_percentage` only with the memory to back it, or add RAM/nodes — see [`yba-api`](../../yba-api/SKILL.md) for editing gflags and resizing.
- **Stuck oversized tablet:** clear the blocker (free up under the replica limit, finish the backup/restore, or manually split via the master admin API), then let post-split compaction settle.
- **Schema-side prevention:** presplit large tables sensibly and choose shard counts up front — see the [`ysql`](../../ysql/SKILL.md) / [`ycql`](../../ycql/SKILL.md) skills.
