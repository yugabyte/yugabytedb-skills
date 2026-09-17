# Playbook: hotspots / key skew

> *Starting point — to be expanded.* A hotspot is **uneven load across nodes or tablets**: one tserver serves far more ops/sec, CPU, or I/O than its peers because the data or traffic for some keys isn't evenly distributed. The defining move in this playbook is **never aggregating away the per-node / per-tablet dimension** — a cluster average hides exactly the imbalance you're hunting.

## Symptoms

- One node's CPU, ops/sec, or disk is much higher than the others while totals look "fine."
- Tail latency (p99) is bad even though average throughput is modest.
- A single tablet or table dominates reads/writes.
- Common causes: low-cardinality or monotonic partition keys (timestamps, sequences), `ASC`/`HASH` mismatch, a "celebrity" key, or a range-sharded table with an append-only access pattern.

## Metrics to read

| Layer | Metric | What it shows |
|---|---|---|
| Query (by API) | `rpc_irate_rps{server_type="yb_ysqlserver"}` vs `{server_type="yb_cqlserver"}` | **YSQL vs YCQL** query ops/sec. Split by API first — they have separate clients and root causes, and one is often idle. |
| Storage | `rpc_irate_rps{service_type="TabletServerService", service_method=~"Read\|Write"}` | Read/write path ops the tablets serve. **Split Read vs Write** — they skew for different reasons (reads → hot key/range; writes → leader placement). |
| Seek skew | `rate(rocksdb_number_db_seek{exported_instance=~".*tserver.*"}[5m])` (and `_next`, `_prev`) | **RocksDB read work per tablet** — the sharpest signal for a *read* hotspot. Group by `exported_instance`, then by `table_name`. Seeks/nexts should look reasonable for the rows the queries return; **sustained high-throughput `prev` in steady state is a red flag** — backwards iteration that should be redesigned as seek + next (typically a missing `DESC` index). Occasional `prev` is fine. |
| Hot objects | `ql_read_latency*`, `ql_write_latency*` (per table) | `topk(10, sum(rate(ql_read_latency_count[5m])) by (table_name))` (and `_write_`) names the **highest-throughput objects** — the usual home of perf findings. For each: even across nodes, and no high latency on any node. |
| Write hotspot | `write_lock_latency*` (per table) | Average via `rate(_sum)/rate(_count)`. **Microseconds = fine; milliseconds (especially double/triple-digit ms) = a write hotspot on that object.** |
| Tablet ops skew | `rate(rocksdb_db_get_micros_count[…])`, `rate(rocksdb_db_write_micros_count[…])` | Per-tablet read/write op counts — group `by (table_name, tablet_id, exported_instance)` for top-N. |
| Balance | `count(is_raft_leader{exported_instance=~".*tserver.*"} == 1) by (exported_instance)` | Leader count per tserver. Filter to tservers — masters inflate it. (`tablet_leaders` is a shortcut but **absent on some builds**; `is_raft_leader` works everywhere — see [finding-metrics](finding-metrics.md#tablet_leaders-may-be-absent--derive-leader-balance-from-is_raft_leader).) |
| Balance | `ts_live_tablet_peers` | Peer count per tserver |
| Storage size | `rocksdb_current_version_sst_files_size`, `rocksdb_current_version_num_sst_files` | Data size per node/table/tablet |
| Resource | per-node CPU (`node_cpu_usage_avg` VM / `container_cpu_usage` k8s), disk-busy | Confirm the hot node is also resource-heavy |

> **Use the per-table labels.** On YBA/Prometheus these storage metrics carry `table_name`, `table_type` (`PGSQL_TABLE_TYPE` = YSQL, `YQL_TABLE_TYPE` = YCQL/internal), and `namespace_name` — far more actionable than the raw `table_id`/`tablet_id`. Group by `table_name` to name the culprit directly.

## Balance vs. group vs. top-N — **this is the whole game**

This playbook *is* the balance lens (see [SKILL.md](../SKILL.md#the-balance-lens-outlier-detection)). The "one tablet high ops/sec" case is exactly a high-spread grouping.

- **Group `by (exported_instance)`** and compare the *spread*, not the sum. A hotspot is visible as one series sitting well above the rest.
  ```promql
  sum(rpc_irate_rps{node_prefix="<prefix>", service_type="TabletServerService"}) by (exported_instance)
  ```
- **Quantify the skew** so it's not eyeballed — max-vs-mean ratio across nodes (≈1.0 is balanced, ≫1 is skewed); the coefficient of variation (`stddev/avg`) works on any grouping label:
  ```promql
  max(sum(rpc_irate_rps{node_prefix="<prefix>", service_type="TabletServerService"}) by (exported_instance))
  /
  avg(sum(rpc_irate_rps{node_prefix="<prefix>", service_type="TabletServerService"}) by (exported_instance))
  ```
- **Decompose the skew before concluding — a high node-level ratio is only the start.** Peel it apart in this order; each step narrows the cause and avoids a wrong diagnosis:
  1. **Layer:** is the skew in the **query** layer (`rpc_irate_rps{server_type=~"yb_ysqlserver|yb_cqlserver"}`) or the **storage** layer (`service_type="TabletServerService"`)? Balanced query ops + skewed storage ops ⇒ it's *data/tablet* skew, not application/connection routing.
  2. **API:** split YSQL (`yb_ysqlserver`) vs YCQL (`yb_cqlserver`). One is often idle. Note YCQL/`YQL_TABLE_TYPE` tables also back **internal** activity (e.g. CDC's `system.cdc_state`), so storage-layer load can exist even when *user* YCQL ops are zero.
  3. **Read vs write:** `service_method=~"Read|Write"` — reads concentrate on hot key/range; writes track leader placement.
  4. **Table:** group the **seek-skew** metric by `table_name` to name the culprit:
     ```promql
     topk(10, sum(rate(rocksdb_number_db_seek{node_prefix="<prefix>", exported_instance=~".*tserver.*"}[5m])) by (table_name, table_type, exported_instance))
     ```
- **Quantify which member is the outlier** with `topk(1, …)` / the `max/avg` ratio at each level (node, then tablet/table).
- **Do NOT** answer "is there a hotspot?" with a `sum()`/`avg()` over the whole cluster — that averages the hot node into the cool ones and hides it. Equally, don't stop at "node X is hot" — decompose to the table.

> **Worked example (real universe).** Node-level `TabletServerService` ops showed `max/avg ≈ 1.97` — looked like a serious hotspot. Decomposing: YSQL query ops were *balanced* (≈2,400/node, YCQL idle) and leaders were balanced (1.01) — so not an application or placement problem. The skew was all **reads**: one node ran 10,144 seeks/s vs 1,149 and 240. Grouping seeks `by (table_name)` pinned **89% to `system.cdc_state`** — the internal CDC checkpoint table, polled hard by CDC connectors on the node that leads its tablets. Verdict: benign internal CDC bookkeeping, *not* user key skew. Without the decomposition the headline ratio would have been misread as an application hotspot.

## Trends that are significant

- A **persistent** per-node max/mean ratio > ~1.3–1.5 on ops/sec, CPU, or SST size (transient skew during rebalancing or after add-node is normal — let it settle).
- One node's ops/sec line consistently 2×+ the median while leader counts are roughly even → traffic/key skew (not placement skew).
- Uneven leader count (`count(is_raft_leader==1) by (exported_instance)`) → **placement/leader imbalance** (different root cause: recent resize, AZ failure, or a stuck load balancer — not a key-design problem).
- A single `table_name`/`tablet_id` accounting for a large share of seek/write rate → hot tablet. If it's a **user** table (`PGSQL_TABLE_TYPE`/`YQL_TABLE_TYPE` in a user namespace) → likely a key-design issue. If it's a **system** table (e.g. `system.cdc_state`, `system.transactions`) → internal/background activity, not your schema.
- **`write_lock_latency` in the milliseconds** on a hot object → write hotspot. The usual key-design culprits: a **low-cardinality hash index**, a **NULL-heavy hash column**, a **monotonic range index** (or a low-cardinality column leading a monotonic range column), **UUIDv7**, timestamps, lexicographically-ordered hex, sequences / generally-increasing values. Schema fix via [`ysql`](../../ysql/SKILL.md); SQL-side detection in [`yb-query-analysis` pgss reference](../../yb-query-analysis/references/pgss-analysis.md).
- **A hot object not participating on all nodes** — a top-10 table reading/writing on only a subset of nodes. Decide which cause: the table **doesn't have enough tablets** to cover the nodes (needs a size-based split), or it has enough tablets but the data **can't split / isn't using them** (key design concentrates traffic). Cross-check tablet placement (`ts_live_tablet_peers`, leader counts) and [issue-tablet-limits](issue-tablet-limits.md).

## Confirm vs. rule out

- **Confirm user key skew:** storage ops uneven *and* leaders roughly even *and* one or few **user** tables/tablets dominate the seek/write top-N. The hot table is the suspect — inspect its partition/primary key design.
- **Rule out (it's internal, not your schema):** the dominant table is a **system** table — most commonly `system.cdc_state` when CDC is enabled (the CDC pollers hammer whichever node leads its tablets), or `system.transactions` under heavy distributed-txn load. Tune/scale CDC or accept it; don't touch user schema.
- **Rule out (it's placement, not keys):** leader count itself is uneven → it's a balancing problem; check the load balancer state and recent topology changes rather than the schema.
- **Rule out (it's the query layer, not data):** query ops (`server_type=...`) are skewed while storage ops are even → it's connection/application routing → see [connection skew](issue-connection-skew.md), not data skew.
- **Rule out (it's just one slow node):** CPU/disk high on one node across *all* tablets uniformly → suspect hardware/noisy-neighbour, not key distribution.
- **Rule out (it's churn, not skew):** a high seek *rate* that is **even across nodes and tablets** (low `max/avg`) is not a hotspot — it's reads stepping over tombstones / stale versions (a queue / soft-delete table, or wide-row update churn). See [disk & I/O](issue-disk-io.md) seek-amplification, and fix it at the query/schema layer via [`yb-query-analysis`](../../yb-query-analysis/references/pgss-analysis.md).

## Where to look next

- Hot table → revisit sharding: hash-shard instead of range, add entropy to a monotonic key, or split a hot range. See the [`ysql`](../../ysql/SKILL.md) / [`ycql`](../../ycql/SKILL.md) skills for partition-key design.
- Placement imbalance → check leader balancing / load-balancer status via the [`yba-api`](../../yba-api/SKILL.md) skill or master UI.
- Correlate the hot node's CPU with the [CPU playbook](issue-cpu.md) and its I/O with the [disk & I/O playbook](issue-disk-io.md).
