# Guided workload sweep (expert YSQL universe review)

> *Use this when the goal is a deliberate, broad review* — "characterise this workload", "review the universe's performance", "walk the metrics like an expert would" — rather than a single named symptom. It encodes the ordered walkthrough an experienced YSQL optimisation engineer performs over the YBA metrics dashboard (Overall → Outlier Nodes → Container → Outlier Tables → Queries), expressed as source-agnostic PromQL so it works against any source from [`data-sources.md`](data-sources.md).
>
> **Relationship to the [quick health check](issue-quick-healthcheck.md):** the health check is a *fast triage gate* (trip-wires → route or report green). This sweep is the *thorough ordered review*: it first characterises the workload, then walks every area collecting **points of interest**. Both feed the same per-issue playbooks. For "is it OK?" run the health check; for "review it properly" run this sweep (the health check's vitals are a subset, so you need not run both).

## Discipline: enumerate first, drill later

This sweep exists to **broadly identify all potential issues, then prioritise by impact** — the same rule as [`yb-performance-assessment`](../../yb-performance-assessment/SKILL.md). Work the sections **in order** and keep a running **findings ledger**: for each section record what you saw (including "checked, healthy"). **Do not stop at the first interesting signal and rabbit-hole into it** — a finding here is a lead to route to a playbook *after* the sweep completes, ranked against the other findings. Early sections deliberately establish context (read/write mix, concurrency) that changes how later sections are interpreted.

Queries assume the relabelled schema and `node_prefix="<prefix>"` scoping; translate via [`finding-metrics.md`](finding-metrics.md) and detect the VM-vs-Kubernetes platform fork before the resource sections. Connection metric names vary by version — **confirm via discovery** before trusting a `yb_ysqlserver_*connection*` name.

## 1. Characterise the workload (Overall tab → YSQL)

Everything later is interpreted through what you learn here.

### 1.1 Throughput by statement type — the read/write mix

```promql
sum(rpc_irate_rps{node_prefix="<prefix>", server_type="yb_ysqlserver", service_type="SQLProcessor",
    service_method=~"SelectStmt|InsertStmt|UpdateStmt|DeleteStmt"}) by (service_method)
```

Record the select vs insert/update/delete ratio and classify the workload **read-heavy ↔ write-heavy**. This framing drives the concurrency rules in §1.3 and the saturation expectations everywhere else (a write-heavy workload saturates at far fewer active connections than a read-heavy one).

### 1.2 Average latency per statement type

```promql
sum(rate(rpc_latency_sum{node_prefix="<prefix>", server_type="yb_ysqlserver", service_type="SQLProcessor"}[5m])) by (service_method)
/ sum(rate(rpc_latency_count{node_prefix="<prefix>", server_type="yb_ysqlserver", service_type="SQLProcessor"}[5m])) by (service_method)
```

This is the cost of "the main thing that's happening" — **averages hide outliers**, so do *not* conclude anything about tails here; tail problems surface in §7–§8 and via percentiles ([issue-latency-throughput](issue-latency-throughput.md)).

### 1.3 Connections — active is the one that matters

- **Total connections** (`yb_ysqlserver_connection_total` — confirm name): context only.
- **Active connections** (`yb_ysqlserver_active_connection_total` — confirm name): the real concurrency driver. Judge against a **per-core rule of thumb** (cores = per-node vCPUs × nodes):
  - **Write-heavy** workloads can saturate at **~1 active connection per core**.
  - The most **read-heavy** workloads (with a little write) top out around **4–8 active connections per core**.
  - Well below these → the workload is **over-serialized** (not enough concurrency to use the cluster); well above → **over-subscribed** (queueing, inflated latency). You're looking for the sweet spot, calibrated by the §1.1 mix.
- **New connections/sec** (`rate(yb_ysqlserver_new_connection_total[5m])` — confirm name): a **sustained** new-connection rate means the client pool's min/idle size is too low, so the app keeps opening fresh connections. Each new connection triggers **catalog cache lookups → catalog cache misses → added latency and load on the single master leader** — note it here and check §3. Routes: [issue-connection-skew](issue-connection-skew.md) (churn/pooling), [issue-master-catalog](issue-master-catalog.md) (master-side effect).

## 2. Resource (Overall tab)

### 2.1 CPU level

VM: `node_cpu_usage_avg{node_prefix="<prefix>", mode="total"}`; k8s: `sum(container_cpu_usage{namespace="<ns>", container_name="yb-tserver"}) by (pod_name)` (see the [platform fork](finding-metrics.md#platform-fork-vm-vs-kubernetes-read-this-before-querying-cpumemorydisk)).

High CPU **inflates the higher latency percentiles non-linearly** — a node at ~90% CPU has a much higher P90 than one at ~40%, so treat high CPU as a *tail-latency cause*, not just a capacity number. Route: [issue-cpu](issue-cpu.md).

### 2.2 Disk capacity and I/O headroom

`node_filesystem_used_bytes / node_filesystem_size_bytes` (k8s: `kubelet_volume_stats_*`) for capacity; `node_disk_read_bytes_irate`, `node_disk_write_bytes_irate`, `node_disk_io_time_irate` for I/O. The question at this stage is **headroom**: disks need spare bandwidth for background operations (compactions, bootstraps, backups), not just current traffic. Route: [issue-disk-io](issue-disk-io.md).

### 2.3 Remote bootstraps in flight

`num_tablet_peers_undergoing_rbs` — if a remote bootstrap is **ongoing**, correlate with `node_disk_*_irate`: it competes for disk bandwidth, so confirm there's enough to carry it **without starving normal traffic**. Route: [issue-disk-io](issue-disk-io.md).

## 3. Master (Overall tab)

```promql
sum(rpc_irate_rps{node_prefix="<prefix>", server_type="yb_master"})           # total
sum(rpc_irate_rps{node_prefix="<prefix>", server_type="yb_master"}) by (service_method)  # what it is
```

Master RPC load is served by the **single master leader** and **does not scale out** — anything above baseline is a scaling risk. Expect a steady state roughly proportional to node count (**~5 × number of nodes** as a rough figure, mostly heartbeats). Excess above baseline is usually **catalog traffic** (`GetTableSchema`-type methods): connections reconnecting (a connection's first queries do catalog cache lookups), **unprepared queries** doing catalog lookups on every execution, the rare prepared query that does the same, and **parallel queries**. Corroborate on the SQL side with pg_stat_statements `catalog_wait_time`. Route: [issue-master-catalog](issue-master-catalog.md).

## 4. Tablet server / background work (Overall tab)

- **Compactions — active vs queued.** A backlog of **queued (non-active)** background/compaction tasks means compaction isn't keeping up; healthy is work *active* and draining. Confirm the exact active/queued task series via discovery (grep names for `compact`/`task`), and correlate with `rocksdb_compact_read_bytes` / `rocksdb_compact_write_bytes` and `rocksdb_current_version_num_sst_files` (the backlog signal). Route: [issue-disk-io](issue-disk-io.md).
- **Tablet splitting.** `tablet_split_candidates`, `outstanding_tablet_splits`, `split_operations_inflight` (master-side). Split operations on **large tablets** can cause **periodic latency-percentile spikes** — check whether split activity lines up with recurring P99 bumps. Routes: [issue-tablet-limits](issue-tablet-limits.md), [issue-latency-throughput](issue-latency-throughput.md).

## 5. Per-node evenness (Outlier Nodes tab)

Re-run every §1 metric grouped `by (exported_instance)` (the YBA "Outlier Nodes" top-10 view). Each node should be **even across all SQL statement types, total connections, active connections, and new connections**. Quantify with the [balance lens](../SKILL.md#the-balance-lens-outlier-detection) (`max/avg`, CoV) rather than eyeballing. Uneven = a skew lead: connections → [issue-connection-skew](issue-connection-skew.md); ops → [issue-hotspots](issue-hotspots.md).

## 6. Per-pod / per-node resource balance (Container tab)

CPU per pod/node (`container_cpu_usage{container_name="yb-tserver"} by (pod_name)` on k8s; `node_cpu_usage_avg by (exported_instance)` on VMs), quantified with `max/avg` or `stddev/avg`. Realistic expectations: **~20% top-to-bottom variance is acceptable** (load balancers and tablet balance are never perfect); **one node at 80–100% CPU while the rest idle is a serious problem** even when cluster totals look fine. Route: [issue-hotspots](issue-hotspots.md), [issue-cpu](issue-cpu.md).

## 7. Transaction conflicts

`rate(transaction_conflicts[5m])` (correlate with `expired_transactions`). A highly contended workload drives slowness and **elevated tail percentiles** — a rising conflict counter is often the explanation for a bad P99 that §1.2's averages hid. Routes: [issue-latency-throughput](issue-latency-throughput.md); SQL-side contention analysis → [`yb-query-analysis` contention](../../yb-query-analysis/references/contention.md).

## 8. Hot objects (Outlier Tables tab) — where most performance findings live

### 8.1 Top-10 read and top-10 write objects

```promql
topk(10, sum(rate(ql_read_latency_count{node_prefix="<prefix>"}[5m]))  by (table_name))
topk(10, sum(rate(ql_write_latency_count{node_prefix="<prefix>"}[5m])) by (table_name))
```

These name the highest-throughput objects — the usual home of performance problems. For **each** top object check two things: (a) load is **evenly spread across all nodes** (regroup `by (table_name, exported_instance)`), and (b) **no high latency** on any node (`rate(_sum)/rate(_count)` per node).

### 8.2 LSM read work per hot object — seek / next / prev

```promql
sum(rate(rocksdb_number_db_seek{node_prefix="<prefix>"}[5m])) by (table_name, exported_instance)
# likewise rocksdb_number_db_next, rocksdb_number_db_prev
```

Seeks and nexts are **the cost of producing each row** — the sharpest read-work signal:

- **Seeks** per hot object should look **reasonable for the rows the queries expect** (cross-check §9.2). High seeks ⇒ an expensive access path or an index doing too much work.
- **Nexts** similarly low relative to rows returned.
- **Prev traffic in steady state is a red flag** — occasional `prev` is fine, but sustained high-throughput backwards iteration should be redesigned into **seek + next** (typically a missing `DESC` index or a backwards range scan). Route: [issue-hotspots](issue-hotspots.md), then query/index work in [`yb-query-analysis`](../../yb-query-analysis/references/pgss-analysis.md).

### 8.3 Write-lock latency — the write-hotspot tell

`write_lock_latency` (per table): average via `rate(_sum)/rate(_count)`. **Microseconds = fine. Milliseconds — especially double/triple-digit ms — = a write hotspot on that object.** Likely root causes: a low-cardinality hash index, a NULL-heavy hash column, a monotonic range index (or a low-cardinality column leading a monotonic range column), UUIDv7, timestamps, lexicographically-ordered hex, sequences / generally-increasing values. Route: [issue-hotspots](issue-hotspots.md) (monotonic-key write hotspot), schema fix via [`ysql`](../../ysql/SKILL.md).

### 8.4 Correlate hot objects back to nodes / tablets / CPU

For each top-10 object: is it reading/writing from **all** nodes? If a node isn't participating, decide which of two causes: the table **doesn't have enough tablets** to cover the nodes (size-based split needed), or it has enough tablets but the data **can't split / isn't using them**. Cross-check `count(is_raft_leader==1) by (exported_instance)`, `ts_live_tablet_peers`, and the node's CPU against §6. Routes: [issue-hotspots](issue-hotspots.md), [issue-tablet-limits](issue-tablet-limits.md).

## 9. Queries cross-check (Queries tab / pg_stat_statements)

This section corroborates the metrics story against actual per-query cost — run it via [`yb-query-analysis`](../../yb-query-analysis/references/pgss-analysis.md) (live or paste-mode):

1. **Throughput ↔ latency corroboration:** `mean_exec_time`, `calls`, `rows` for the top statements should line up with the §1 dashboard read/write throughput and latency.
2. **Rows per query:** `rows / calls` establishes how many rows each query *should* produce — the yardstick for the §8.2 seek/next counts.
3. **Index usage:** correlate per-table seeks/nexts with the query shape. A `SELECT … WHERE column = …` with a **high next count on the table** is scanning the *table*, not an index ⇒ index likely missing or unused. General principle: do the seeks/nexts match what the query should need?
4. **Scan amplification:** `docdb_rows_scanned` vs `docdb_rows_returned` vs `calls` (requires `yb_enable_pg_stat_statements_rpc_stats=true`). A selective query returning ~1 row that isn't aggregating but scans many rows has an insufficient index condition, falling back to storage-side filtering — the scan-ratio check in [pgss-analysis.md](../../yb-query-analysis/references/pgss-analysis.md).

## 10. Synthesise — rank by impact, then drill

Close the ledger and apply the assessment discipline ([`yb-performance-assessment`](../../yb-performance-assessment/SKILL.md#step-4--synthesise-one-ranked-report-apply-the-assessment-discipline)): every real finding from any section goes in the report; rank by impact; "checked, healthy" is a valid entry; only *then* take the top findings into their per-issue playbooks for confirmation and remediation guidance. All remediation remains **advisory** per [SKILL.md](../SKILL.md#output-discipline--advisory-only-no-changes-without-explicit-approval).
