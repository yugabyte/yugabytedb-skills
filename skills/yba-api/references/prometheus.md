# Prometheus metrics for YBA universes

YBA bundles a Prometheus instance that scrapes every node, pod, master, tserver, ybc and node-exporter in every universe it manages. There are three ways to read those metrics — pick one based on what you need.

| Surface | Endpoint | When to use |
|---|---|---|
| **Bundled Prometheus** (recommended for ad-hoc queries) | `http://<yba-host>:9090/api/v1/...` | Full PromQL, range queries, label discovery. **No auth** by default — protect at the network layer. |
| **YBA metrics proxy** | `POST /api/v1/customers/{cid}/metrics` (uses the API token) | Replicates the YBA dashboard panels by name (`metrics: ["tserver_rpcs_per_sec", ...]`). Auth-gated, returns the dashboard's own pre-cooked queries. Use for parity with the UI. |
| **YBA's own scrape endpoint** | `GET /prometheus_metrics` (no `/api/v1` prefix) | YBA process metrics only (not universe metrics). Useful for monitoring YBA itself. |

The rest of this file covers the bundled-Prometheus path, which is what the queries the user is most likely to write target.

## Connecting

Default URL: `http://<yba-host>:9090` — HTTP, no auth, **only** reachable from the YBA host's network. If YBA is on Kubernetes, port-forward the `yb-platform-yugaware` pod's `9090` container port (the Prometheus container is a sidecar in the same pod).

```bash
# VM / installer
curl "http://yba.internal:9090/api/v1/query?query=up"

# Kubernetes (port-forward locally)
kubectl -n yb-platform port-forward svc/yba-yugaware-ui 9090:9090
curl "http://localhost:9090/api/v1/query?query=up"
```

The two endpoints you need:

- `GET /api/v1/query?query=<promql>&time=<rfc3339>` — instant query at a single point in time.
- `GET /api/v1/query_range?query=<promql>&start=<rfc3339>&end=<rfc3339>&step=<seconds>s` — range query over a window.

Response shape (range query):

```json
{"status": "success",
 "data": {
   "resultType": "matrix",
   "result": [
     {"metric": {"service_method": "SelectStmt", ...},
      "values": [[1716998400, "12.34"], [1716998460, "13.07"], ...]}
   ]
 }}
```

Each `values[i]` is `[unix_seconds, "string_value"]`.

## Identifiers you need from the YBA API

PromQL filters reference labels that map to YBA concepts. Pull them from the YBA API and substitute them into the query.

| PromQL label | Where it comes from |
|---|---|
| `node_prefix` | `GET /api/v1/customers/{cid}/universes/{uni}` → `universeDetails.nodePrefix`. Looks like `yb-<env>-<universe-name>-<n>` (cloud) or `ybmwam-<id>-<region>-<az>-<...>` (k8s). |
| `pod_name=~...` (k8s) | `universeDetails.nodeDetailsSet[].nodeName` from the same response, joined with `\|` and turned into a regex. The tserver pods follow `<node>-yb-tserver-N` and master pods `<node>-yb-master-N`. |
| `namespace=~...` (k8s) | `universeDetails.nodeDetailsSet[].cloudInfo.kubernetesNamespace` (deduplicated, joined with `\|`). |
| `service_type` | Component being scraped: `SQLProcessor` (YSQL/YCQL), `TabletServerService` (read/write paths), `ConsensusService`, `RaftServer`, etc. |
| `server_type` | `yb_cqlserver`, `yb_ysqlserver`, `yb_tserver`, `yb_master`. |
| `service_method` | RPC method name — varies per `service_type`. |

A tiny Python helper to assemble these from a universe response:

```python
def universe_promql_labels(universe: dict) -> dict:
    d = universe["universeDetails"]
    nodes = d.get("nodeDetailsSet", [])
    return {
        "node_prefix": d.get("nodePrefix"),
        "namespaces":  sorted({n["cloudInfo"].get("kubernetesNamespace")
                               for n in nodes if n.get("cloudInfo", {}).get("kubernetesNamespace")}),
        "tserver_pods": [f"{n['nodeName']}-yb-tserver-(.*)" for n in nodes if n.get("nodeName")],
        "master_pods":  [f"{n['nodeName']}-yb-master-(.*)"  for n in nodes if n.get("nodeName")],
    }
```

## Useful queries

All queries below assume `node_prefix` is the value you fetched from the universe API response. Substitute it at the call site (use Python f-strings or `string.Template` rather than gluing strings).

### YCQL operations per second, by statement type

```promql
sum(avg_over_time(rpc_irate_rps{
    service_type="SQLProcessor",
    node_prefix="yb-dev-mwam-914787231",
    server_type="yb_cqlserver",
    service_method=~"SelectStmt|InsertStmt|UpdateStmt|DeleteStmt|OtherStmts|Transaction"
}[30s])) by (service_method)
```

For YSQL, swap `server_type="yb_cqlserver"` for `server_type="yb_ysqlserver"` and the same `service_method` regex applies.

### YCQL latency (microseconds), by statement type

A canonical "average over the window" pattern: divide the rate of the histogram's `_sum` by the rate of its `_count`.

```promql
(
  avg(rate(rpc_latency_sum{
    service_type="SQLProcessor",
    node_prefix="yb-dev-mwam-914787231",
    server_type="yb_cqlserver",
    service_method=~"SelectStmt|InsertStmt|UpdateStmt|DeleteStmt|OtherStmts|Transaction"
  }[30s])) by (service_method)
)
/
(
  avg(rate(rpc_latency_count{
    service_type="SQLProcessor",
    node_prefix="yb-dev-mwam-914787231",
    server_type="yb_cqlserver",
    service_method=~"SelectStmt|InsertStmt|UpdateStmt|DeleteStmt|OtherStmts|Transaction"
  }[30s])) by (service_method)
)
```

For p99 instead of average:

```promql
histogram_quantile(0.99,
  sum(rate(rpc_latency_bucket{
    service_type="SQLProcessor", node_prefix="yb-dev-mwam-914787231",
    server_type="yb_cqlserver", service_method=~"Select.*|Insert.*"
  }[1m])) by (le, service_method)
)
```

### Container CPU (Kubernetes universe), as a percentage of one core

```promql
avg(avg_over_time(container_cpu_usage{
    container_name="yb-tserver",
    namespace=~"default|default|default",
    pod_name=~"ybmwam-914787-urope-west2-a-uamw-yb-tserver-(.*)|ybmwam-914787-urope-west2-b-vamw-yb-tserver-(.*)|ybmwam-914787-urope-west2-c-wamw-yb-tserver-(.*)"
}[30s])) * 100
```

Build the `pod_name=~...` regex from `universe_promql_labels()["tserver_pods"]` joined with `|`. The `namespace=~...` list is the deduped set of namespaces the universe spans. For master CPU, swap `container_name="yb-master"` and the master-pod regex.

### Container memory (Kubernetes), as bytes

```promql
sum(container_memory_usage{
    container_name="yb-tserver",
    namespace=~"default",
    pod_name=~"<tserver-pod-regex>"
}) by (pod_name)
```

### CPU on cloud nodes (node-exporter), as a percentage

For non-Kubernetes universes the `container_*` series do not exist; use `node_cpu` from node-exporter:

```promql
100 - avg(rate(node_cpu{
    node_prefix="yb-dev-mwam-914787231",
    mode="idle"
}[1m])) by (exported_instance) * 100
```

### Disk space used per node

```promql
sum(node_filesystem_size{
    node_prefix="yb-dev-mwam-914787231",
    mountpoint=~"/mnt/d.*"
}
 - node_filesystem_free{
    node_prefix="yb-dev-mwam-914787231",
    mountpoint=~"/mnt/d.*"
}) by (exported_instance)
```

### Tablet leader count per tserver (placement balance)

```promql
sum(tablet_leaders{node_prefix="yb-dev-mwam-914787231"}) by (exported_instance)
```

If counts are very uneven, the universe is leader-imbalanced — relevant when you've just resized or added zones.

### WAL replication lag (xCluster / read-replica)

```promql
max(async_replication_committed_lag_micros{node_prefix="yb-dev-mwam-914787231"}) by (table_id) / 1e6
```

Returns committed lag per replicated table in seconds.

## Discovering metric and label names

Two endpoints are invaluable when writing a new query:

```bash
# All metric names matching a substring
curl -sG "http://yba:9090/api/v1/label/__name__/values" --data-urlencode 'match[]={node_prefix="yb-dev-mwam-914787231"}' \
  | jq '.data[]' | grep -i latency

# All values a label takes for a given metric
curl -sG "http://yba:9090/api/v1/series" --data-urlencode 'match[]=rpc_latency_count{node_prefix="yb-dev-mwam-914787231"}' \
  | jq '.data[].service_method' | sort -u
```

Use these instead of guessing label names — the YugabyteDB tserver/master surface has hundreds of metrics with overlapping naming conventions.

## Python helper

Composes range queries against the bundled Prometheus and returns a flat list of `(labels, [(timestamp, value), ...])` tuples.

```python
import datetime as dt
import requests

def prom_range(prom_url: str, query: str, *, minutes: int = 60, step_s: int = 30,
               verify: bool = True) -> list[tuple[dict, list[tuple[dt.datetime, float]]]]:
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(minutes=minutes)
    r = requests.get(
        f"{prom_url.rstrip('/')}/api/v1/query_range",
        params={"query": query,
                "start": start.isoformat(timespec="seconds"),
                "end":   end.isoformat(timespec="seconds"),
                "step":  f"{step_s}s"},
        timeout=30, verify=verify,
    )
    r.raise_for_status()
    body = r.json()
    if body.get("status") != "success":
        raise RuntimeError(f"Prometheus error: {body}")
    return [
        (s["metric"], [(dt.datetime.fromtimestamp(t, dt.timezone.utc), float(v))
                       for t, v in s["values"]])
        for s in body["data"]["result"]
    ]
```

Composing a query for a specific universe:

```python
universe = yba_request(**yba, endpoint=f"/api/v1/customers/{cid}/universes/{uni}")
prefix   = universe["universeDetails"]["nodePrefix"]

ycql_qps = (
  f'sum(avg_over_time(rpc_irate_rps{{'
  f'service_type="SQLProcessor", node_prefix="{prefix}", '
  f'server_type="yb_cqlserver", '
  f'service_method=~"SelectStmt|InsertStmt|UpdateStmt|DeleteStmt|OtherStmts|Transaction"'
  f'}}[30s])) by (service_method)'
)

for labels, points in prom_range("http://yba.internal:9090", ycql_qps, minutes=15):
    print(labels["service_method"], points[-1])
```

## PowerShell helper

```powershell
function Invoke-YbaPromQuery
{
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)] [System.Uri]$PrometheusUrl,   # e.g. http://yba.internal:9090
        [Parameter(Mandatory)] [String]$Query,
        [Int]$Minutes = 60,
        [Int]$StepSeconds = 30,
        [Switch]$SkipCertificateCheck
    )
    $end   = (Get-Date -AsUTC)
    $start = $end.AddMinutes(-$Minutes)
    $url = "$($PrometheusUrl.AbsoluteUri.TrimEnd('/'))/api/v1/query_range"
    $params = @{
        query = $Query
        start = $start.ToString('yyyy-MM-ddTHH:mm:ssZ')
        end   = $end.ToString('yyyy-MM-ddTHH:mm:ssZ')
        step  = "${StepSeconds}s"
    }
    $qs = ($params.GetEnumerator() | ForEach-Object {
        "{0}={1}" -f [uri]::EscapeDataString($_.Key), [uri]::EscapeDataString($_.Value)
    }) -join '&'
    $r = Invoke-RestMethod -Uri "${url}?${qs}" -Method Get -TimeoutSec 30 `
            -SkipCertificateCheck:$SkipCertificateCheck
    if ($r.status -ne 'success') { throw "Prometheus error: $($r | ConvertTo-Json -Compress)" }
    foreach ($s in $r.data.result)
    {
        [pscustomobject]@{
            metric = $s.metric
            values = $s.values | ForEach-Object {
                [pscustomobject]@{
                    timestamp = ([System.DateTimeOffset]::FromUnixTimeSeconds([long]$_[0])).UtcDateTime
                    value     = [double]$_[1]
                }
            }
        }
    }
}
```

Usage:

```powershell
$universe = Invoke-YbaRequest @Yba -Endpoint "/api/v1/customers/$($Yba.CustomerId)/universes/$uni"
$prefix   = $universe.universeDetails.nodePrefix

$query = @"
sum(avg_over_time(rpc_irate_rps{service_type="SQLProcessor", node_prefix="$prefix",
server_type="yb_cqlserver", service_method=~"SelectStmt|InsertStmt|UpdateStmt|DeleteStmt|OtherStmts|Transaction"}[30s])) by (service_method)
"@

Invoke-YbaPromQuery -PrometheusUrl 'http://yba.internal:9090' -Query $query -Minutes 15
```

## YBA proxy alternative (auth-gated, dashboard-equivalent)

If you cannot reach `:9090` directly (firewall / Kubernetes-internal), YBA fronts a pre-cooked metrics API at `POST /api/v1/customers/{cid}/metrics`. It accepts a list of *dashboard metric names* (not raw PromQL) and returns the same shape the YBA UI panels render.

```python
yba_request(**yba, method="POST",
    endpoint=f"/api/v1/customers/{cid}/metrics",
    payload={
        "start": int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=15)).timestamp()),
        "end":   int(dt.datetime.now(dt.timezone.utc).timestamp()),
        "metrics": ["cql_server_rpc_per_second", "cql_sql_latency"],
        "nodePrefix": prefix,
    })
```

Discover the available metric names via `GET /api/v1/metrics_list` or by inspecting the `metrics` payload that the YBA UI sends from the browser dev tools. Use this surface only when you need parity with the UI; for any new analysis use direct PromQL on `:9090`.

## Anti-patterns

| Anti-Pattern | Consequence | Do Instead |
|---|---|---|
| Hard-coding `node_prefix` from a different universe | Empty results | Always read it from `GET /universes/{uni}` → `universeDetails.nodePrefix`. |
| Building the Kubernetes pod regex by hand | Misses pods after edits / scale-out | Derive from `universeDetails.nodeDetailsSet[].nodeName`. |
| Using `rate(...)` over windows shorter than 2× the scrape interval | Empty or noisy series (default scrape is 10–30 s) | Use windows of `30s` or longer; `1m` is a safe default. |
| Querying the dashboard proxy when you actually need PromQL | Forced to use UI's metric names; no ad-hoc filters | Query `:9090` directly. |
| Exposing `:9090` publicly | Prometheus has no auth | Keep it on a private network, or front it with the YBA proxy / a reverse proxy. |
