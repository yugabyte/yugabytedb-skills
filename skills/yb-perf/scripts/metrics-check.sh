#!/bin/bash
# metrics-check.sh -- one read-only pass over the YugabyteDB metrics must-checks, per node.
# Usage: bash metrics-check.sh [prometheus_url]        (default http://localhost:9090)
# Only HTTP GETs to the Prometheus query API. Flags any node >= 2x the median of its peers.
#
# Scope selectors (PromQL label matchers, no braces). Defaults fit a self-managed Prometheus
# with one job per server type:
#   YB_SEL         tserver series        default: job="yb-tserver"
#   YB_MASTER_SEL  master series         default: job="yb-master"
#   YB_HOST_SEL    node_exporter series  default: none (all hosts)
#   YB_POD_SEL     cAdvisor series       default: none (all pods)
#   YB_NODE_LABEL  per-node label        default: exported_instance
# YBA (bundled Prometheus; node_prefix = universeDetails.nodePrefix, see yba-api prometheus.md):
#   YB_SEL='node_prefix="yb-dev-u1",export_type="tserver_export"' \
#   YB_MASTER_SEL='node_prefix="yb-dev-u1",export_type="master_export"' \
#   YB_HOST_SEL='node_prefix="yb-dev-u1",export_type="node_export"' bash metrics-check.sh http://<yba>:9090
# YBA on Kubernetes: also YB_POD_SEL='namespace="<ns>"' for container CPU (cAdvisor has no node_prefix).
#
# Sums of two metrics aggregate each side first: relabelled stacks add a per-metric label
# (saved_name), so `rate(a) + rate(b)` would match no series.
P="${1:-http://localhost:9090}"
TS="${YB_SEL:-job=\"yb-tserver\"}"
MS="${YB_MASTER_SEL:-job=\"yb-master\"}"
HS="${YB_HOST_SEL:-}"
PS="${YB_POD_SEL:-}"
N="${YB_NODE_LABEL:-exported_instance}"
export N

q() { curl -s --get "$P/api/v1/query" --data-urlencode "query=$1"; }
has() { q "count($1)" | grep -q '"value"'; }
show() { # title, promql
  echo "== $1"
  q "$2" | python3 -c '
import json,os,statistics,sys
try: res=json.load(sys.stdin)["data"]["result"]
except Exception: print("   (query failed)"); sys.exit()
m=lambda r: r["metric"]
rows=[(m(r).get(os.environ["N"]) or m(r).get("table_name") or m(r).get("pod") or m(r).get("pod_name") or m(r).get("name") or m(r).get("instance","?"), float(r["value"][1])) for r in res]
rows=[(k,v) for k,v in rows if v==v]
if not rows: print("   (no data -- metric absent or selector wrong; check /api/v1/label/__name__/values)"); sys.exit()
med=statistics.median([v for _,v in rows]) or 0
for k,v in sorted(rows):
    flag="  <== OUTLIER (>=2x median)" if med>0 and v>=2*med and len(rows)>2 else ""
    print(f"   {k:30} {v:14.3f}{flag}")
'
}
rate_sum() { echo "sum by ($N) (rate($1[2m]))"; }   # metric{selector}
mean_lat() { echo "$(rate_sum "$1_sum{$2}") / $(rate_sum "$1_count{$2}")"; }   # metric, selector

echo "Prometheus $P | tserver {$TS} | master {$MS} | node label $N"
show "M1a. tserver targets up (1 = up)" "up{$TS}"
show "M1b. master targets up (1 = up)" "up{$MS}"

# Relabelled (rpc_latency + service_method) or raw (handler_latency_*) naming.
if has "rpc_latency_count{$TS}"; then
  R="rpc_latency"; RS="$TS,service_type=\"TabletServerService\",service_method=\"Read\""
  W="rpc_latency"; WS="$TS,service_type=\"TabletServerService\",service_method=\"Write\""
  M="rpc_latency"; MSEL="$MS,server_type=\"yb_master\""
else
  R="handler_latency_yb_tserver_TabletServerService_Read"; RS="$TS"
  W="handler_latency_yb_tserver_TabletServerService_Write"; WS="$TS"
  M=""; MSEL=""
fi
show "M2a. tserver Read RPC mean latency (us)" "$(mean_lat "$R" "$RS")"
show "M2b. tserver Write RPC mean latency (us)" "$(mean_lat "$W" "$WS")"
# Percentiles are a gauge with a quantile label (p50 p95 p99 mean min max), not _bucket histograms.
show "M2c. tserver Write RPC p99 (us), max over 5m" "max by ($N) (max_over_time($W{$WS,quantile=\"p99\"}[5m]))"

echo "== M3. CPU per node (compare each with its own limit, not only with its peers)"
show "M3a. tserver process CPU (cpu_utime + cpu_stime, ms per s; 1000 = one core)" "$(rate_sum "cpu_utime{$TS}") + $(rate_sum "cpu_stime{$TS}")"
if has "node_cpu_seconds_total{${HS:+$HS,}mode=\"idle\"}"; then
  show "M3b. host CPU busy % (node_exporter, VM)" "100 * (1 - avg by ($N) (rate(node_cpu_seconds_total{${HS:+$HS,}mode=\"idle\"}[2m])))"
elif has "container_cpu_usage_seconds_total{${PS:+$PS,}container=\"yb-tserver\"}"; then
  show "M3b. tserver container CPU cores (cAdvisor, Kubernetes)" "sum by (pod) (rate(container_cpu_usage_seconds_total{${PS:+$PS,}container=\"yb-tserver\"}[2m]))"
elif has "container_cpu_usage{${PS:+$PS,}container_name=\"yb-tserver\"}"; then
  show "M3b. tserver container CPU cores (YBA Kubernetes recording)" "sum by (pod_name) (container_cpu_usage{${PS:+$PS,}container_name=\"yb-tserver\"})"
elif has "podman_container_cpu_seconds_total"; then
  show "M3b. container CPU cores (podman exporter)" 'rate(podman_container_cpu_seconds_total[2m]) * on (id) group_left(name) podman_container_info{name=~".*(yugabyte|tserver).*"}'
else
  echo "   M3b. (no host or container CPU metrics: node_exporter, cAdvisor or podman exporter not scraped)"
fi
# The ceiling each node can reach. A capped node shows LOW or flat CPU, not high CPU.
if has "container_spec_cpu_quota{${PS:+$PS,}container=\"yb-tserver\"}"; then
  show "M3c. tserver container CPU limit (cores)" "sum by (pod) (container_spec_cpu_quota{${PS:+$PS,}container=\"yb-tserver\"}) / sum by (pod) (container_spec_cpu_period{${PS:+$PS,}container=\"yb-tserver\"})"
  show "M3d. share of CPU periods throttled (0-1; > 0.2 = starved)" "sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{${PS:+$PS,}container=\"yb-tserver\"}[2m])) / sum by (pod) (rate(container_cpu_cfs_periods_total{${PS:+$PS,}container=\"yb-tserver\"}[2m]))"
elif has "node_cpu_seconds_total{${HS:+$HS,}mode=\"idle\"}"; then
  show "M3c. host CPU count" "count by ($N) (node_cpu_seconds_total{${HS:+$HS,}mode=\"idle\"})"
else
  echo "   M3c. CPU limit not exported here: compare a slow node's usage with its container / VM size before ruling CPU out."
fi

show "M4. WAL log_sync mean latency (us)" "$(mean_lat log_sync_latency "$TS")"

# is_raft_leader in 2025.2 is per table: its value is that table's leader count on the node. Sum it.
show "M5a. raft leaders per node" "sum by ($N) (is_raft_leader{$TS})"
show "M5b. live tablet peers per node" "sum by ($N) (ts_live_tablet_peers{$TS})"
show "M5c. tablet peers as share of the supportable limit (no row = no limit)" "sum by ($N) (ts_live_tablet_peers{$TS}) / sum by ($N) (ts_supportable_tablet_peers{$TS} > 0)"
show "M5d. master: creates + splits rejected by the tablet limit in the last 1h" "sum by ($N) (increase(create_table_too_many_tablets{$MS}[1h])) + sum by ($N) (increase(split_tablet_too_many_tablets{$MS}[1h]))"

show "M6a. max follower_lag_ms" "max by ($N) (follower_lag_ms{$TS})"
show "M6b. operations inflight" "sum by ($N) (all_operations_inflight{$TS})"
show "M6c. memory-pressure rejections /s" "sum by ($N) (rate({__name__=~\".*_memory_pressure_rejections\",$TS}[2m]))"
show "M6d. too-many-SST-files write rejections /s" "$(rate_sum "majority_sst_files_rejections{$TS}")"
show "M6e. RPC queue overflows + timed out in queue /s" "$(rate_sum "rpcs_queue_overflow{$TS}") + $(rate_sum "rpcs_timed_out_in_queue{$TS}")"

show "M7. rocksdb seek+next /s by table (top 5; load follows where each table's leaders sit -- a cause only if new or abnormal for that table)" "topk(5, sum by (table_name) (rate(rocksdb_number_db_seek{$TS}[2m])) + sum by (table_name) (rate(rocksdb_number_db_next{$TS}[2m])))"
show "M8a. rocksdb stall micros /s" "$(rate_sum "rocksdb_stall_micros{$TS}")"
show "M8b. queued (non-active) compaction tasks" "sum by ($N) (compaction_tasks{$TS,state_type=~\"Non-Active.*\"})"

if [ -n "$M" ]; then
  show "M9a. master RPC mean latency (us; followers idle = no row)" "$(mean_lat "$M" "$MSEL")"
else
  show "M9a. master RPC mean latency (us; raw names)" "sum by ($N) (rate({__name__=~\"handler_latency_yb_master_.*_sum\",$MS}[2m])) / sum by ($N) (rate({__name__=~\"handler_latency_yb_master_.*_count\",$MS}[2m]))"
fi
show "M9b. master RPC queue overflows + timed out in queue /s" "$(rate_sum "rpcs_queue_overflow{$MS}") + $(rate_sum "rpcs_timed_out_in_queue{$MS}")"

show "M10a. transaction conflicts /s" "$(rate_sum "transaction_conflicts{$TS}")"
show "M10b. waiters in the wait queue" "sum by ($N) (wait_queue_num_waiters{$TS})"
# Per-node verdict: a lead, not a conclusion. The node leading the hottest tablets routinely
# runs 2-3x its peers' Write latency in a healthy cluster; a cap, disk fault or neighbour is
# usually larger and NEW. "1h ago" compares with the same ratio an hour earlier when history exists.
echo "== Per-node verdict (Write latency and process CPU vs the median of all nodes; a lead, not a conclusion)"
LAT="$(mean_lat "$W" "$WS")"; CPU="$(rate_sum "cpu_utime{$TS}") + $(rate_sum "cpu_stime{$TS}")"
OLD="${LAT//\[2m\]/[2m] offset 1h}"
{ q "$LAT"; echo; q "$CPU"; echo; q "$OLD"; } | python3 -c '
import json,os,statistics,sys
P=sys.stdin.read().split("\n"); N=os.environ["N"]
def get(t):
    try: return {r["metric"].get(N,"?"): float(r["value"][1]) for r in json.loads(t)["data"]["result"] if float(r["value"][1])==float(r["value"][1])}
    except Exception: return {}
lat,cpu,old=(get(P[i]) if len(P)>i else {} for i in range(3))
if len(lat)<3: print("   (need 3+ nodes with Write latency)"); sys.exit()
ml=statistics.median(lat.values()); mc=statistics.median(cpu.values()) if cpu else 0
mo=statistics.median(old.values()) if len(old)>=3 else 0
for k in sorted(lat):
    lr=lat[k]/ml if ml else 0; cr=(cpu.get(k,0)/mc) if mc else float("nan")
    was=f"  (1h ago {old[k]/mo:.1f}x)" if mo and k in old else "  (no 1h history)"
    note=""
    if lr>=4:
        note=("  <== STRONG node lead (if many paths are slow): outlier without higher CPU -> CPU cap/throttle, slow disk or noisy neighbour; check its limit (M3c)"
              if cr==cr and cr<=1.2 else "  <== STRONG node lead (if many paths are slow): outlier with higher CPU -> CPU saturation; find what it serves (M5, M7)")
    elif lr>=2:
        note="  <-- elevated: common for the node leading the hottest tablets; a lead only if new AND many paths are slow"
    print(f"   {k:30} latency {lr:5.1f}x  cpu {cr:5.2f}x{was}{note}")
'
echo "== M11. app latency: if the application exports metrics, compare p99 across endpoints/activities (scope first)"
