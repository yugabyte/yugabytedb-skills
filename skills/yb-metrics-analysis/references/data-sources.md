# Choosing a metrics source

Before querying anything, decide *where* the metrics come from. The three sources expose **different metric names and labels**, so this choice determines how every later query is written. Prefer them in this order.

```
YugabyteDB Anywhere  >  standalone Prometheus / PromQL  >  direct node scrape
   (pre-labelled,         (whatever the operator           (raw names, no YBA
    pre-cooked API)        configured)                      relabelling at all)
```

> **On YugabyteDB Aeon, it depends on metrics export.** Aeon's *native* API exposes only a small curated metrics set — not the full Prometheus surface these guides query. For un-exported Aeon, use its console / **Performance Advisor / Insights** and the `cluster-metrics` API (via [`aeon-api`](../../aeon-api/SKILL.md)). **But if Aeon (or YBA) metrics are exported to your own Prometheus / Grafana Cloud / Datadog, this skill *does* apply** — point at that sink and use case 2. See [case 4](#4-yugabytedb-aeon-and-exported-metrics-grafana-cloud--datadog--your-own-prometheus) and [SKILL.md → Scope](../SKILL.md#scope-where-this-skill-applies-and-where-it-doesnt).

## 1. YugabyteDB Anywhere (preferred when the universe is YBA-managed)

If the universe was created or is managed by YBA, **always start here.** YBA runs a bundled Prometheus that scrapes every master, tserver, ysql/ycql server, ybc, and node-exporter in every managed universe, and applies a consistent relabelling scheme (see [`finding-metrics.md`](finding-metrics.md)). It gives you two surfaces:

- **Bundled Prometheus** at `http://<yba-host>:9090` — full PromQL, range queries, label discovery. This is what you want for ad-hoc analysis. No auth by default; reachable only from the YBA host's network (port-forward the `yugaware` pod's `9090` on Kubernetes).
- **YBA metrics proxy** at `POST /api/v1/customers/{cid}/metrics` — auth-gated, takes *dashboard metric names* (not raw PromQL) and returns the same series the YBA UI panels render. Use it when you cannot reach `:9090` directly (firewall / k8s-internal), when the platform pod is hard to find, or when you specifically want parity with the UI.

> **On Kubernetes, discover the pods before assuming names.** The YBA platform often lives in a *different kube context (and cluster)* than the universe's nodes. Find them explicitly:
> ```bash
> kubectl config get-contexts                                   # the platform may be in another context
> kubectl --context <ctx> get pods -A | grep -i yugaware        # YBA platform pod (Prometheus is a sidecar in it; namespace varies — often 'default')
> kubectl --context <ctx> -n <ns> port-forward pod/<yugaware-pod> 9090:9090 &
> kubectl --context <ctx> get pods -A | grep yb-tserver         # universe tserver pods (for pod_name/namespace selectors)
> ```
> **If the port-forward is any trouble — or you're a lower-capability model — skip it and use the metrics proxy API with the token (no kubectl needed).** That is the most reliable path for quick triage. Full discovery + port-forward + proxy commands: [`yba-api` Prometheus reference → Connecting](../../yba-api/references/prometheus.md#connecting).

Full connection details, the universe-scoping labels (`node_prefix`, k8s `pod_name`/`namespace`), Python/PowerShell query helpers, and a catalogue of ready PromQL live in the **[`yba-api` Prometheus reference](../../yba-api/references/prometheus.md)**. Read that for the mechanics; this skill focuses on *what to query and how to interpret it*.

**How to tell it's YBA-managed:** the operator mentions YBA / YugabyteDB Anywhere / "the platform"; there is a YBA host or `yb-platform` namespace; or the universe appears under `GET /api/v1/customers/{cid}/universes`.

## 2. Standalone Prometheus / PromQL (no YBA, but Prometheus exists)

Self-managed deployments often run their own Prometheus scraping the YugabyteDB nodes — for example the [podman-yugabyte](https://github.com/yugabyte/podman-yugabyte) `tools/prometheus` setup, a Helm-deployed kube-prometheus-stack, or a hand-rolled `prometheus.yml`. PromQL works exactly as with YBA, **but the labels and metric names depend entirely on that instance's `metric_relabel_configs`.**

Two sub-cases:

- **It replicates YBA's relabelling** (e.g. podman-yugabyte deliberately copies YBA's `metric_relabel_configs` and recording rules). Then all the relabelled names in this skill apply as-is: `rpc_latency`, `server_type`, `service_type`, `service_method`, `saved_name`, `exported_instance`, `rpc_irate_rps`, the `node_*_irate` rules, etc.
- **It does *not* relabel** (a plain scrape). Then you only have the **raw** YugabyteDB names — `handler_latency_yb_tserver_TabletServerService_Read`, etc. — and none of the split labels or recording rules. Translate using [`finding-metrics.md`](finding-metrics.md).

**Always confirm which case you are in** by listing metric names and labels (`/api/v1/label/__name__/values`, `/api/v1/series`) before writing queries. Do not assume the relabelled schema just because it's Prometheus.

Find the endpoint: ask the operator, check `prometheus.yaml`/`docker-compose.yaml`/Helm values, or look for a Grafana datasource. Then query `http://<prom-host>:9090/api/v1/...` like any Prometheus.

> **Grafana is a view, not a source.** A Grafana dashboard sits on top of a Prometheus (or Mimir/Datadog) datasource — query *that* datasource, not Grafana. Locating the datasource is often the fastest way to discover the Prometheus URL; if you can only reach Grafana, its datasource-proxy API (`/api/datasources/proxy/<id>/api/v1/query`) or the **Explore** tab runs PromQL against the same backend.

### Retrieving one metric (Mac & Windows)

For a *current* value, use an **instant** query (`/api/v1/query`) — far cheaper than a range query, and extract only the label + value so nothing else lands in context. Works against YBA's bundled `:9090` and any standalone Prometheus. (For range queries, history, and ready-made Python/PowerShell helpers, use the [`yba-api` Prometheus reference](../../yba-api/references/prometheus.md) rather than duplicating it here.)

```bash
# macOS / Linux — per-node ops/sec, one line per node
curl -s "http://PROM:9090/api/v1/query" \
  --data-urlencode 'query=sum(rpc_irate_rps{node_prefix="P",service_type="TabletServerService"}) by (exported_instance)' \
  | jq -r '.data.result[] | "\(.metric.exported_instance) \(.value[1])"'
```

```powershell
# Windows PowerShell — same (irm sends $Body as the query string for GET)
$q = 'sum(rpc_irate_rps{node_prefix="P",service_type="TabletServerService"}) by (exported_instance)'
(irm "http://PROM:9090/api/v1/query" -Body @{ query = $q }).data.result |
  ForEach-Object { '{0} {1}' -f $_.metric.exported_instance, $_.value[1] }
```

## 3. Direct node scrape (last resort — no Prometheus at all)

Every YugabyteDB master and tserver exposes a Prometheus-format endpoint you can scrape with a single HTTP GET. Use this only when there is no Prometheus to query — it gives you an **instant snapshot, not history**, so you cannot see trends without polling and diffing yourself.

| Component | Endpoint |
|---|---|
| Master | `http://<node>:7000/prometheus-metrics` |
| TServer | `http://<node>:9000/prometheus-metrics` |
| YSQL server | `http://<node>:13000/prometheus-metrics` |
| YCQL server | `http://<node>:12000/prometheus-metrics` |

These return **raw, un-relabelled** names: `handler_latency_yb_tserver_TabletServerService_Read{...}`, `rocksdb_*`, `mem_tracker_*`, `tablet_leaders`, etc. There is **no** `rpc_latency`, no `server_type`/`service_type`/`service_method` split, no `rpc_irate_rps` — those are products of relabelling and recording rules that only exist in a Prometheus configured to create them. See [`finding-metrics.md`](finding-metrics.md) for the raw→relabelled mapping.

### Retrieving one metric (Mac & Windows)

The endpoint emits **thousands** of lines — never read the whole thing into context. Always (1) append `?show_help=false` to drop the `# HELP`/`# TYPE` comment lines, (2) filter to the one metric you want, and (3) cap the output. The filter `^name[ {]` anchors to the metric name so `tablet_leaders` doesn't also match `tablet_leaders_*`.

```bash
# macOS / Linux — one metric from one tserver
curl -s "http://NODE:9000/prometheus-metrics?show_help=false" | grep -E '^tablet_leaders[ {]' | head -20
```

```powershell
# Windows PowerShell — same
(iwr "http://NODE:9000/prometheus-metrics?show_help=false" -UseBasicParsing).Content -split "`n" |
  Select-String '^tablet_leaders[ {]' | Select-Object -First 20
```

`priority_regex=<regex>` narrows the payload server-side to a metric family before you even filter (YBA uses it) — handy when grabbing one of the `rocksdb_*` or `handler_latency_*` groups. Node-exporter (`:9100/metrics`) takes the same `grep`/`Select-String` treatment but has no `show_help` param.

To approximate rates/trends from snapshots, scrape the same endpoint twice a known interval apart and compute the delta yourself, or — better — point a throwaway Prometheus at the nodes and fall back to case 2.

Notes:
- For node-level CPU/memory/disk you also need **node-exporter** (`:9100/metrics`); the database endpoints expose process-level (`yb_process_*`, `cpu_utime`/`cpu_stime`, `mem_tracker_*`) and RocksDB metrics, not host-level ones.
- The `/prometheus-metrics` endpoint supports `?priority_regex=...&show_help=false` params (YBA uses these to trim the payload to the metrics its dashboards need); usually you can ignore them for ad-hoc curls.

## 4. YugabyteDB Aeon, and exported metrics (Grafana Cloud / Datadog / your own Prometheus)

Aeon (managed cloud) does not expose the full Prometheus surface these playbooks assume. Which route applies depends on whether **metrics export** has been configured:

- **No export — use Aeon's own surfaces.** The console **Metrics** tab and the AI-driven **Performance Advisor / Insights** are the first stop. For programmatic access, the public REST **`cluster-metrics`** endpoint returns a small curated set (ops/sec, average latency, CPU, disk) — see the [`aeon-api`](../../aeon-api/SKILL.md) skill. The per-node / per-tablet / RocksDB metrics most playbooks here rely on are **not** available this way, so treat Aeon-native metrics as a coarse health check and drop to the SQL layer ([`yb-query-analysis`](../../yb-query-analysis/SKILL.md)) for depth.
- **Export configured — treat it as case 2.** Both **Aeon and YBA** can stream their full metrics to a third-party sink (Prometheus, Grafana Cloud, Datadog, …). If that exists you have a real Prometheus-compatible surface: point at it and use **case 2**. Caveats — the exporter may rename or drop labels, and the sink's query layer varies (Grafana Cloud / Mimir is PromQL-compatible; Datadog uses its own query language), so **verify metric names with a label/series listing first**, exactly as in case 2.

This is the path that lets the skill serve **commercial Aeon deployments and any Grafana/Datadog-based observability stack** — through the exported Prometheus surface rather than the database directly.

## Decision summary

| You have… | Use | Metric schema you'll see |
|---|---|---|
| A YBA-managed universe | YBA bundled Prometheus `:9090` (proxy API if `:9090` unreachable) | Relabelled (this skill's default) |
| A self-managed Prometheus (or one behind Grafana) | That Prometheus | Depends — verify with `/api/v1/series`; relabelled iff it copies YBA's config |
| No Prometheus | Direct `:7000`/`:9000`/`:9100` scrape | Raw names only; snapshot, no history |
| Aeon **or** YBA metrics **exported** to Prometheus / Grafana Cloud / Datadog | That sink — as case 2 | Depends on the exporter; verify names first |
| Aeon, no export | Aeon console / Insights; `cluster-metrics` API (`aeon-api`) | Small curated set only — coarse health check |

Once the source is chosen, go to [`finding-metrics.md`](finding-metrics.md) to locate metrics by name, then to the matching issue playbook.
