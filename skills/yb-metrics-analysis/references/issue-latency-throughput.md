# Playbook: latency & throughput regressions

> *Starting point — to be expanded.* "It got slower." This playbook separates **latency** (how long each op takes) from **throughput** (how many ops/sec), reads them by statement type, and decides whether the cause is the query layer, the storage layer, replication, or simply load. The canonical YugabyteDB latency pattern is **rate of the histogram `_sum` ÷ rate of its `_count`** for averages, and `histogram_quantile` over `_bucket` for tails.

## Symptoms

- p99 (or average) latency up; throughput down or capped.
- A specific statement type (Select/Insert/Update/Transaction) regressed.
- Regression aligns with a deploy, schema change, traffic shift, or another resource issue.

## Metrics to read

| Concern | Metric | Notes |
|---|---|---|
| Throughput | `rpc_irate_rps{server_type=~"yb_cqlserver\|yb_ysqlserver"}` | Query ops/sec by `service_method`. Raw: `irate(handler_latency_..._count[…])`. |
| Throughput | `rpc_irate_rps{service_type="TabletServerService"}` | Read/write path ops/sec at the storage layer. |
| Latency (avg) | `rpc_latency_sum` / `rpc_latency_count` | Average latency — see pattern below. Raw: `handler_latency_*_{sum,count}`. |
| Latency (tail) | `rpc_latency_bucket` | p99/p999 via `histogram_quantile`. |
| Query layer | `ql_read_latency*`, `ql_write_latency*`, `write_lock_latency*` | DocDB read/write & lock-wait latency. |
| Inflight | `*_operations_inflight` | Queue depth / concurrency saturation. |
| Contention | `transaction_conflicts`, `expired_transactions` | Retries inflating latency. |
| Replication | `follower_lag_ms`, `async_replication_committed_lag_micros` | Raft / xCluster lag affecting commit latency or read freshness. |

## The latency query patterns

**Average over the window** (rate of sum ÷ rate of count — never `rate(_sum)` alone):
```promql
sum(rate(rpc_latency_sum{node_prefix="<prefix>", server_type="yb_ysqlserver",
      service_method=~"SelectStmt|InsertStmt|UpdateStmt|DeleteStmt|Transaction"}[5m])) by (service_method)
/
sum(rate(rpc_latency_count{node_prefix="<prefix>", server_type="yb_ysqlserver",
      service_method=~"SelectStmt|InsertStmt|UpdateStmt|DeleteStmt|Transaction"}[5m])) by (service_method)
```

**p99 tail** (sum the bucket rates by `le` first):
```promql
histogram_quantile(0.99,
  sum(rate(rpc_latency_bucket{node_prefix="<prefix>", server_type="yb_ysqlserver",
        service_method=~"Select.*|Insert.*"}[5m])) by (le, service_method))
```

## Aggregate vs. group vs. top-N vs. balance

- **Group `by (service_method)`** first — "latency is up" is meaningless until you know *which* statement type. Selects and writes have different root causes.
- **Average vs. tail:** always look at both. A stable average with a blown-out p99 means a *subset* of requests is slow (a hot tablet, contention, GC pause) — pivot to [hotspots](issue-hotspots.md).
- **Per-node check:** group latency `by (exported_instance)` to see whether it's cluster-wide or one node (→ [hotspot](issue-hotspots.md)).
- **Balance check (outliers, high *and* low):** a single method/node/table standing apart is a clue even when the average is fine. `topk(1, …)` finds the slowest; **`bottomk(1, …)` finds an anomalously *fast* one** — e.g. one table with surprisingly low latency often means it's barely being queried (traffic skew elsewhere) or hitting a cache the others miss, not that it's healthy. Use the CoV idiom from [SKILL.md](../SKILL.md#the-balance-lens-outlier-detection) over the grouped latency to quantify the spread.
- **Throughput context:** never read latency without throughput beside it — rising latency *with* rising ops/sec is just load; rising latency with flat/falling ops/sec is a real regression.
- **Aggregate** total ops/sec only for the "are we hitting a ceiling?" question.

## Trends that are significant

- **p99 ≫ average and diverging** → tail problem: contention, a hot tablet, or GC/compaction pauses — not a uniform slowdown. High CPU is also a tail cause in its own right — it inflates the higher percentiles non-linearly (a ~90%-CPU node has a far worse P90 than a ~40% one) → [CPU playbook](issue-cpu.md).
- **Periodic, recurring percentile spikes** → check whether they line up with **tablet-split operations on large tablets** (`outstanding_tablet_splits`, `split_operations_inflight` — see [issue-tablet-limits](issue-tablet-limits.md)) or other scheduled background work (compaction, backups).
- **Latency up while ops/sec flat or down** → genuine regression; correlate with a deploy/DDL or with CPU/disk/memory playbooks.
- **Throughput plateaus while offered load keeps rising** + `*_operations_inflight` growing → saturation/queueing; the cluster is at capacity.
- **One `service_method` regressed, others fine** → query-/plan-specific (missing index, plan change) → drop to query-level analysis (EXPLAIN / plan inspection) for that statement.
- **`transaction_conflicts` / `expired_transactions` rising** with write latency → contention/retry storm, not raw slowness.
- **`follower_lag_ms` / replication lag up** → commit latency or stale-read issues from a struggling follower or network.

## Confirm vs. rule out

- **Confirm load (not regression):** latency and throughput rise together, evenly across nodes → capacity; scale or optimise the heaviest `service_method`.
- **Confirm query regression:** a single statement type's latency jumped while volume didn't → plan/index change; analyse the query.
- **Confirm contention:** write latency up with conflicts/expired txns up → application access pattern / hot row.
- **Rule out "everything is slow":** if a downstream resource is exhausted (CPU/disk/memory), fix that first — latency is a symptom, not the cause. Check the other playbooks.

## Where to look next

- Per-statement query issues → query/plan analysis and the [ysql](../../ysql/SKILL.md) / [ycql](../../ycql/SKILL.md) skills.
- Tail driven by one node/tablet → [hotspots](issue-hotspots.md).
- Resource-driven latency → [CPU](issue-cpu.md), [memory](issue-memory.md), [disk & I/O](issue-disk-io.md).
