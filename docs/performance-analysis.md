# YugabyteDB Performance Analysis Skills

One short skill, `yb-perf`, covers both layers a YSQL performance problem can live in. It
lists only what is different on YugabyteDB -- current models already know how to diagnose a
slow PostgreSQL system -- plus the rules that stop the common wrong conclusions, and it ships a
read-only script per layer that runs all of that layer's checks in one call.

| Layer | Must-checks | Use it for |
|---|---|---|
| SQL / session | [`yb-perf`](../skills/yb-perf/SKILL.md) S1-S9 | A slow query or endpoint, an `EXPLAIN (ANALYZE, DIST)` plan, a `pg_stat_statements` or ASH export, lock contention and retries, "is the database the cause?". |
| Infrastructure / metrics | [`yb-perf`](../skills/yb-perf/SKILL.md) M1-M11 | Universe health, node hotspots, leader or tablet skew, latency or throughput read from Prometheus / YBA metrics. |

For any slowness question the skill scopes the complaint first (one path slow, or many
unrelated paths at once), then runs both checks, whichever it can reach: a slow database with a
clean SQL layer is often an infrastructure hotspot, and a saturated node is often driven by one
bad query. Each layer is blind to causes the other sees.

## Why short

An evaluation against known-root-cause scenarios on a local RF3 universe found that the earlier
long methodology skills added no diagnostic accuracy for current frontier models and cost
40-47% more tokens, while the must-check skills now merged into `yb-perf` did as well or
better in every comparison. Its reference files hold only YugabyteDB facts that a closed-book probe showed at least two of
four current models getting wrong or unsure; facts most models already know are left out.

## What it runs

- `yb-perf/scripts/must-check.sql` -- retries per call, long transactions and lock waits
  (sampled, with `yb_locks_min_txn_age = 0`), per-connection warm-up, scan amplification,
  p99/p50, node count, never-analysed tables, unindexed foreign keys, abnormal terminations.
- `yb-perf/scripts/ash.sql` -- Active Session History wait breakdown, top queries, hot tablets, zone split.
- `yb-perf/scripts/all-nodes.sh` -- runs either SQL file on every node, since `pg_stat_statements`,
  `pg_stat_activity`, `pg_locks` and ASH are per node.
- `yb-perf/scripts/metrics-check.sh` -- the metrics checks in one Prometheus pass,
  on YBA (`node_prefix` / `export_type` selectors) or a self-managed Prometheus.

## Safety

The skill is diagnostic and read-only by default. Anything that changes the system (DDL,
cancelling transactions, statistics resets, GUC / GFlag changes) is proposed for your approval,
not run. `EXPLAIN ANALYZE` executes its statement, so the skill never runs it on a write.

## Getting started

```bash
npx skills add yugabyte/yugabytedb-skills -s yb-perf
```
