# YugabyteDB Performance Analysis Skills

Three complementary skills turn an AI agent into a YugabyteDB performance analyst. Together they cover the **two layers** a YSQL performance problem can live in — the **SQL / session layer** and the **infrastructure / metrics layer** — and they scale from a *single pasted artifact* (one `pg_stat_statements` row, one `EXPLAIN` plan) all the way up to a *full, live, whole-universe assessment*.

They encode the diagnostic judgement of an experienced YugabyteDB performance engineer: which signal to read first, what a number *means* on a distributed LSM-tree database (not just on PostgreSQL), how to tell a real problem from a config artifact, and — crucially — what a clean-looking plan or metric **cannot** tell you.

| Skill | Layer | Use it for |
|---|---|---|
| [`yb-performance-assessment`](../skills/yb-performance-assessment/SKILL.md) | **Orchestrator** | Open-ended reviews: *"assess this universe", "is it healthy?", "something feels slow."* Runs both layers and synthesises one ranked report. **Start here when the scope is the whole database.** |
| [`yb-query-analysis`](../skills/yb-query-analysis/SKILL.md) | **SQL / session** | A specific slow query, a regression, lock contention, a pre-production sign-off, or interpreting a `pg_stat_statements` / `EXPLAIN` / ASH export. |
| [`yb-metrics-analysis`](../skills/yb-metrics-analysis/SKILL.md) | **Infrastructure / metrics** | "One node is hotter than the others", CPU/memory/disk pressure, tablet-count limits, latency/throughput regressions — anything you'd reach for Prometheus/Grafana to answer. |

## How they fit together

```
                ┌─────────────────────────────────────--────┐
                │        yb-performance-assessment          │
                │   triage BOTH layers → one ranked report  │
                └──────────────┬─────────────┬──────────────┘
                               │             │
              SQL / session ◄──┘             └──► infrastructure / metrics
        ┌────────────────────-──────┐   ┌──────────────────────────────┐
        │     yb-query-analysis     │◄─►│      yb-metrics-analysis     │
        │  pg_stat_statements, ASH, │   │  PromQL, hotspots, CPU/mem,  │
        │  EXPLAIN DIST, locks,     │   │  disk/IO, tablet limits,     │
        │  tuning, pre-prod checks  │   │  latency/throughput, skew    │
        └─────────────────────-─────┘   └──────────────────────────────┘
```

The two specialist skills **cross-reference each other**: an ASH `DiskIO` wait points at the metrics disk playbook; a hot node in metrics points back at a query or tablet in the SQL layer. The orchestrator's core rule is that **checking one layer and declaring done is the most common assessment mistake** — a "slow database" with a clean SQL layer is often an infra hotspot, and a CPU-saturated node is often driven by one bad query.

## When to use them — scenarios

- **Pre-deployment / pre-go-live / pre-migration review.** Structured checklists validate schema sharding keys, covering indexes, sequence cache, and connection headroom, and run `EXPLAIN (ANALYZE, DIST)` over the critical query paths *before* launch — surfacing problems while they're still cheap to fix.
- **Periodic health check.** A monthly/weekly cadence over production: unused & redundant indexes, scan-ratio drift, new slow queries vs. baseline, statistics staleness, connection trends.
- **Reactive triage.** "Why is this query slow?", "the cluster feels slow", "P99 spiked after the deploy", "we're getting connection errors / OOM kills."
- **Working from exports — no live access needed.** Paste a `pg_stat_statements` dump (CSV/table), an `EXPLAIN` / `EXPLAIN (ANALYZE, DIST)` plan, QPM plan history, or an ASH/wait-event snapshot, and the skills interpret it directly. Every reference includes a **paste-mode** block telling the agent exactly what to ask you to run and paste when it has no live connection.
- **Live, hands-on analysis.** Given a connection, the agent runs a cheap read-only triage snapshot first (catalog + `pg_stat_statements` + `pg_stat_activity`, no user-table scans, safe on production), then drills into whatever it flags.

## Works for different deployment models

These skills are built to serve **both open-source and YugabyteDB-supported deployments**, and adapt to whatever access you have to a universe / database.

| Deployment | How metrics and SQL are reached |
|---|---|
| **Open-source / self-managed** (podman, Docker, bare VM, manual install) | Direct node `/prometheus-metrics` scrape (`:7000`/`:9000`/`:13000`/`:12000`) or a standalone Prometheus; YSQL on `:5433` |
| **YugabyteDB Anywhere (YBA)** | YBA bundled Prometheus / metrics proxy API + Performance Advisor; direct or proxied YSQL |
| **Kubernetes** (operator or YBA-on-K8s) | `kubectl exec` / `port-forward` into tserver pods for YSQL; platform-pod Prometheus or the metrics proxy for metrics — including when the platform lives in a *different* kube context |
| **VM / cloud clusters** | Direct host `:5433` + node Prometheus endpoints or a standalone Prometheus |
| **YugabyteDB Aeon** (managed) | SQL-layer analysis via live YSQL; metrics via the console / Insights / `cluster-metrics` API — **or**, if metrics export to your own Prometheus / Grafana Cloud / Datadog is configured, the full metrics playbooks apply against that sink |
| **Grafana / upstream Prometheus** | Query the Prometheus (or Mimir/Datadog) datasource behind your dashboards directly |

Access is treated as **two independent axes** — you might have live SQL but no metrics, metrics but no SQL, or neither (paste-mode); and you might have query access but no GFlag/restart control (common on Aeon). The skills detect what's available and degrade gracefully rather than assuming a single environment. The metrics skill also handles the different labels that can be seen across YBA or raw YugabyteDB nodes.

## Flexible across different use cases

- **Any granularity** — from interpreting one pasted row to running a 30-minute load test and ranking the whole workload by cumulative time.
- **YugabyteDB-aware, not just PostgreSQL** — reads the DocDB-specific signals (`docdb_rows_scanned`/`returned`, `docdb_read/write_rpcs`, `conflict_retries`, `read_restart_retries`, `yb_latency_histogram`, RocksDB seek/next, tablet/leader balance) that standard Postgres tooling is blind to, and explains them in distributed-storage terms.
- **Breadth-first discipline** — it enumerates *every* real finding (not just the headline), separates config-layer artifacts from schema-layer problems, and reports in impact tiers so you get both "the one thing to fix now" and "the handful worth fixing next."
- **Routes work to the right place** — schema/DDL redesign hands off to the `ysql` skill; control-plane actions (resize, GFlags) hand off to `yba-api`.

## What they can capture — at a glance

**`yb-query-analysis` (SQL / session layer):**

- Scan amplification — `docdb_rows_scanned ≫ rows_returned`: missing index, or an index defeated by a function/cast on the column (`lower(col)`, `col::bigint`), or a HASH/leading-column mismatch
- Sequence `CACHE 1` insert hotspots
- Transaction **conflict** and **read-restart** retry storms (hot rows, contention)
- **N+1 / chatty-client** patterns (visible only as a cross-query call-count ratio)
- **Unbatched writes** — single-row inserts and per-row `INSERT … ON CONFLICT` loops that should be one multi-row statement
- **Tombstone / version churn** — queue & soft-delete tables that get slower over time (latency without scan amplification)
- **Deep `LIMIT … OFFSET`** pagination (vs. keyset/seek)
- **Hash-key skew & monotonic write hotspots** (celebrity values, NULL-heavy or low-cardinality hash keys, time-series tail tablets)
- Stale statistics and **plan regressions** (with hint-based stabilisation)
- **Lock contention**, idle-in-transaction, connection exhaustion, OOM / temp-file terminations
- Over-fetch (`SELECT *` on wide rows), unindexed foreign-key columns, redundant/overlapping indexes
- Tail-latency (P99) problems hiding behind a healthy mean
- The **blind spots** a plan/QPM *cannot* show — and the cross-checks that close them

**`yb-metrics-analysis` (infrastructure / metrics layer):**

- **Node hotspots / key skew** — one tserver serving far more ops/CPU/IO than its peers
- **CPU** saturation (honest load vs. compaction vs. a hotspot)
- **Memory** pressure, OOM, memory-rejection events
- **Disk** fill and **I/O** saturation, compaction backlog / SST pressure, write stalls
- **Latency & throughput** regressions, read vs. write and per-statement-type breakdowns
- **Connection skew** across nodes, plus **concurrency sizing** (active connections per core, over-serialized vs. over-subscribed) and reconnect churn
- **Master / catalog RPC pressure** — connection churn and unprepared queries driving catalog cache misses onto the single master leader
- **Tablet limits** — too many tablets / replica-limit pressure, and oversized tablets that won't split (including split operations causing periodic tail-latency spikes)
- **Seek amplification** from tombstone/version churn (the metrics-layer view of the queue-table anti-pattern)
- A **guided expert workload sweep** — the ordered dashboard walkthrough a YSQL optimisation expert performs (read/write mix → resource → master → background work → per-node balance → hot tables → query cross-check), enumerating every point of interest before ranking by impact

## Safety — advisory by default

All three skills are **diagnostic and read-only by default**: only read-only triage and collection run without asking. The output is **succinct, ranked guidance — findings and recommended fixes for you to review, not changes the agent applies.** Any statement that mutates or disrupts the running system (DDL, session/transaction termination, statistics resets, GUC/GFlag/runtime-config changes, restarts, compaction, control-plane writes) is presented for your **explicit approval** before it runs.

## Getting started

```bash
# The orchestrator (recommended entry point for whole-universe reviews)
npx skills add yugabyte/yugabytedb-skills -s yb-performance-assessment

# Or the specialist layers directly
npx skills add yugabyte/yugabytedb-skills -s yb-query-analysis
npx skills add yugabyte/yugabytedb-skills -s yb-metrics-analysis
```

Then just describe the situation to your agent — *"assess this YugabyteDB universe for performance issues"*, *"here's a pg_stat_statements export, what's wrong?"*, *"one node is hotter than the others"* — and it will pick the right skill, run the appropriate triage, and report back.
