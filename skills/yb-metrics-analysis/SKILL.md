---
name: yb-metrics-analysis
description: Retrieve and analyse YugabyteDB universe metrics to identify and explain operational issues — a quick health-check gate, a guided workload sweep (read/write mix, active-connections-per-core sizing, hot-table review), and playbooks for hotspots, high CPU, high memory, I/O saturation, latency/throughput regressions, connection skew, master/catalog RPC pressure, and tablet limits. Use when a user asks why a universe is slow, unbalanced, or unhealthy (or simply "is it OK?"); wants a workload/universe metrics review or to inspect tserver/master/node metrics; mentions Prometheus, PromQL, the YBA metrics dashboard, node hotspots, uneven connections, connection churn or pool sizing, catalog cache misses, OOMs, memory pressure rejections, CPU saturation, compaction/SST pressure, or too many tablets. Covers picking a metrics source (YugabyteDB Anywhere → Prometheus/PromQL → direct node scrape), finding metrics, and per-issue playbooks. Not for YugabyteDB Aeon (limited metrics API) — prefer Performance Advisor there.
---

# YugabyteDB metrics analysis

Help an operator pull the right metrics from a YugabyteDB universe and turn them into a diagnosis. This skill is about **observability-driven triage**: given a symptom (or no symptom — "is this universe healthy?"), choose a metrics source, locate the relevant series by name, query them at the right aggregation, and read the trends that confirm or rule out a specific class of problem.

## Output discipline — advisory only; no changes without explicit approval

This skill is **read-only**: it queries metrics (PromQL, the YBA metrics proxy, `/prometheus-metrics` scrapes) and interprets them. The default deliverable is **succinct guidance — a diagnosis and recommended remediation for the user to review, not changes you apply.** Every remediation here (scale out, resize, drop unused tables/indexes, edit GFlags/runtime config, raise tablet/overhead limits, trigger compaction, restart) is **routed to [`yba-api`](../yba-api/SKILL.md) or the operator and requires the user's explicit approval** — never apply one yourself. Present the exact action and its effect; let the user authorize it.

It complements the [`yba-api`](../yba-api/SKILL.md) skill (control-plane automation): for raw Prometheus connection and PromQL plumbing against a YBA-bundled Prometheus, the [`yba-api` Prometheus reference](../yba-api/references/prometheus.md) is the canonical low-level guide — this skill links to it rather than repeating it.

**Related skills:**
- **Whole-universe performance assessment** (open-ended "assess this cluster for any performance issues" — runs this metrics track *and* the SQL track, then synthesises) → start from [`yb-performance-assessment`](../yb-performance-assessment/SKILL.md). Use it when the scope is the whole universe rather than a specific metrics symptom.
- **SQL-layer query analysis** (slow queries, pg_stat_statements, ASH, locks, query tuning, pre-production assessment) → [`yb-query-analysis`](../yb-query-analysis/SKILL.md)
- **YugabyteDB Aeon REST API** (cluster scaling, backups, allowlists, metrics API) → [`aeon-api`](../aeon-api/SKILL.md)

## Scope: where this skill applies (and where it doesn't)

This skill targets **self-managed YugabyteDB** and **YugabyteDB Anywhere (YBA)–managed** universes — anywhere you can reach a Prometheus (bundled or standalone) or the nodes' `/prometheus-metrics` endpoints directly. YugabyteDB exposes on the order of **~2,000** metrics there; the playbooks here assume you can query that full surface.

- **Not for YugabyteDB Aeon (managed cloud).** Aeon offers a metrics surface — a `cluster-metrics` endpoint in its public REST API and a metrics-export integration to third-party tools (Datadog, Grafana Cloud, Prometheus, etc.) — but the directly queryable API exposes only a **small curated set** (ops/sec, average latency, CPU usage, disk usage, and similar), nowhere near the full Prometheus metric set this skill relies on. For Aeon: use the Aeon console Metrics tab, Aeon Insights, or configure metrics export — and note that **if metrics export to a Prometheus / Grafana Cloud / Datadog sink is configured (on Aeon or YBA), the playbooks here _do_ apply against that sink** (see [`references/data-sources.md`](references/data-sources.md) case 4). For SQL-layer analysis on Aeon see `yb-query-analysis` → `references/aeon-access.md`. For the Aeon REST API (including cluster metrics) see the `aeon-api` skill.

- **Prefer Performance Advisor when it's available.** YugabyteDB offer [**Performance Advisor**](https://docs.yugabyte.com/stable/yugabyte-platform/alerts-monitoring/performance-advisor/) (in YBA) and the related AI-driven [**Insights / Performance Advisor in Aeon**](https://docs.yugabyte.com/preview/yugabyte-cloud/cloud-monitor/cloud-advisor/) that perform automated, AI-driven analysis far beyond ad-hoc metric queries — correlating anomalies across the cluster and flagging concrete issues like unused indexes, hot shards, connection skew and query-level load contributors. For YugabyteDB **customers**, that product is usually the faster and more thorough first stop. This skill is for when you need raw-metric triage *outside* of (or to complement) Performance Advisor — e.g. self-managed clusters, ad-hoc investigations, or going deeper than a recommendation.

## Workflow

Work through these stages in order. Each stage has a dedicated guide; read the guide before acting.

1. **Pick a data source** → [`references/data-sources.md`](references/data-sources.md)
   Prefer **YugabyteDB Anywhere** if the universe is YBA-managed (it owns a pre-labelled Prometheus and a pre-cooked metrics API). Fall back to a **standalone Prometheus / PromQL** endpoint if there is one. Only **scrape node endpoints directly** (`/prometheus-metrics` on each master/tserver) when no Prometheus exists. The guide explains how to reach each.

2. **Find the metrics by name** → [`references/finding-metrics.md`](references/finding-metrics.md)
   YugabyteDB exports hundreds of raw metrics, and **YBA rewrites many of them at scrape time** (the `handler_latency_*` → `rpc_latency` rename, the `server_type` / `service_type` / `service_method` label split, `saved_name`, `exported_instance`, `node_prefix`, and recording rules like `rpc_irate_rps`). Whether a metric exists under its raw name or its relabelled name depends on which source you chose in stage 1. This guide is the decoder ring.

3. **Run the per-issue playbook** → pick the guide matching the symptom. Two entry modes when there is no single named symptom — both feed the same playbooks below:
   - **Unknown symptom, fast answer** ("is it healthy?", "feels slow", no specifics) → the quick health check, a trip-wire gate that routes to a playbook or reports green.
   - **Deliberate broad review** ("characterise this workload", "review the universe", an expert-style walkthrough) → the **guided workload sweep** — [`references/workload-sweep.md`](references/workload-sweep.md). It works the YBA dashboard order (workload mix → resource → master → background work → per-node balance → hot tables → query cross-check), collecting *all* points of interest before drilling, then ranks by impact.

   - **Quick health check / unknown issue (start here)** → [`references/issue-quick-healthcheck.md`](references/issue-quick-healthcheck.md)
   - **Hotspots / key skew** (one node hotter than the rest) → [`references/issue-hotspots.md`](references/issue-hotspots.md)
   - **High CPU** → [`references/issue-cpu.md`](references/issue-cpu.md)
   - **High memory / OOM / memory-pressure rejections** → [`references/issue-memory.md`](references/issue-memory.md)
   - **Disk space & I/O saturation / compaction pressure** → [`references/issue-disk-io.md`](references/issue-disk-io.md)
   - **Latency & throughput regressions** → [`references/issue-latency-throughput.md`](references/issue-latency-throughput.md)
   - **Connection skew / churn & sizing** (one node holds far more client connections; too few/too many active connections; reconnect storms) → [`references/issue-connection-skew.md`](references/issue-connection-skew.md)
   - **Master / catalog RPC pressure** (master load above baseline; catalog cache misses from connection churn or unprepared queries loading the single master leader) → [`references/issue-master-catalog.md`](references/issue-master-catalog.md)
   - **Tablet limits / too many tablets** (replica-limit pressure, oversized tablets that won't split) → [`references/issue-tablet-limits.md`](references/issue-tablet-limits.md)

   Each playbook follows the same shape: **symptoms → which metrics to read (usually several, correlated) → aggregate vs. group/top-N vs. balance → trends that are significant → how to confirm vs. rule out → where to look next.**

   **A diagnosis rarely rests on one metric.** Symptoms are ambiguous (high CPU could be honest load, compaction, or a hotspot), so each playbook lists *several* metrics and the next step is usually to correlate them and follow the cross-links to other playbooks. Check the obvious metric, then the ones that confirm *why*, before concluding.

## The one principle that runs through every playbook

**How you aggregate decides what you can see.** The same metric answers different questions depending on grouping. There are four lenses — pick by the question, and usually apply more than one:

| Question | Lens | Example |
|---|---|---|
| "Is the cluster as a whole healthy?" | **aggregate**: `sum()` / `avg()` across all nodes | total ops/sec, fleet-average CPU |
| "Is *one member* the problem?" (skew/hotspot) | **compare**: group `by (<label>)` and read each series | per-node ops/sec, per-node CPU |
| "Which *table / tablet / query* is worst?" | **top-N**: `topk()` `by (table_id)` / `by (tablet_id)` / `by (service_method)` | hottest tablets, slowest statement type |
| "Is the load *balanced*, or is one member an outlier?" | **balance**: spread of a grouped series — see below | one node high CPU, one tablet high ops/sec, one table oddly low latency |

A cluster-wide average hides a hotspot; a per-tablet top-N buries an overall trend. State the question first, then choose the lens.

### The balance lens (outlier detection)

Many problems are **imbalance** — the totals look fine but one member of a group sits far from the rest. Quantify the spread instead of eyeballing the legend; an outlier exists when the dispersion across a grouping label is large. The label can be *any* dimension: `exported_instance` (nodes), `tablet_id`, `table_id`, `service_method`.

```promql
# Coefficient of variation across the group (unitless, scale-free). > ~0.3 = worth investigating.
stddev(<grouped_expr>) / avg(<grouped_expr>)

# Outlier ratio — how far the worst member is above the mean. ~1.0 balanced; >> 1 skewed.
max(<grouped_expr>) / avg(<grouped_expr>)

# Surface the offending member (high OR low — a low outlier matters too, e.g. one table much faster/slower).
topk(1, <grouped_expr>)      # highest
bottomk(1, <grouped_expr>)   # lowest
```

where `<grouped_expr>` is your metric reduced to one series per member, e.g. `sum(rpc_irate_rps{node_prefix="<prefix>"}) by (exported_instance)`. A high coefficient of variation is itself the finding — then `topk(1, …)` / `bottomk(1, …)` names the node/table/tablet to chase. The hotspot playbook lives or dies on this lens; the others use it to decide *cluster-wide vs. one-member* before recommending a fix.

## Conventions used across the guides

- PromQL examples assume the **relabelled** schema (what YBA's Prometheus and the podman-yugabyte replica produce). If you are scraping nodes directly, translate names per [`references/finding-metrics.md`](references/finding-metrics.md).
- Selectors that scope to a universe (`node_prefix=...`, k8s `pod_name=~...` / `namespace=~...`) are described once in the [`yba-api` Prometheus reference](../yba-api/references/prometheus.md#identifiers-you-need-from-the-yba-api); examples here use a placeholder `node_prefix="<prefix>"`.
- Always confirm a metric and its labels exist before building on it (`/api/v1/label/__name__/values`, `/api/v1/series`) rather than trusting a name from memory — the surface changes across YugabyteDB versions.

> The per-issue playbooks are intentionally concise starting points and will be expanded over time. When a playbook does not cover a symptom, fall back to stage 2 (discover metric names) and the four-lens principle above — most novel issues are found by checking a handful of related metrics and looking for the one that is out of balance.
