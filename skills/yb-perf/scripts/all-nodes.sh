#!/usr/bin/env bash
# all-nodes.sh -- run a read-only .sql file or one statement on every YSQL node and prefix each
# output line with the node. pg_stat_statements, pg_stat_activity, pg_locks, ASH and
# yb_terminated_queries are per node, so one connection sees only one node's share.
#
# Usage:
#   all-nodes.sh [mode] (-f file.sql | -c 'SQL') [-- extra ysqlsh args, e.g. -v window='30 minutes']
# Modes (pick one):
#   (default)        discover hosts with yb_servers() through the seed connection (PGHOST/PGPORT/
#                    PGUSER/PGDATABASE/PGPASSWORD, default localhost:5433 yugabyte/yugabyte), then
#                    connect to each host:port directly. YB_HOST_MAP rewrites unreachable hosts:
#                    YB_HOST_MAP='yugabytedb-1=localhost:5433,yugabytedb-2=localhost:5434'
#   --exec 'CMD'     run CMD per discovered host with {host} replaced, SQL on stdin (CMD is split
#                    on spaces, no quoting inside it), e.g.
#                    --exec 'podman exec -i -e PGPASSWORD yb-{host} ysqlsh -h {host} -U yugabyte'
#   --kube NS [--context CTX]
#                    no discovery: kubectl exec ysqlsh in every *yb-tserver-* pod of namespace NS
#                    (container yb-tserver; ysqlsh there reaches the local node without -h).
# Limitation: yb_servers() returns the addresses the nodes advertise to each other. From outside
# that network (containers, Kubernetes, private subnets) they may not resolve -- use YB_HOST_MAP,
# --exec or --kube. Two map entries pointing at the same host:port query the same node twice.
set -euo pipefail

mode=direct exec_tpl="" kube_ns="" kube_ctx="" sql_file="" sql_stmt=""
while (($#)); do
  case "$1" in
    -f) sql_file="$2"; shift 2 ;;
    -c) sql_stmt="$2"; shift 2 ;;
    --exec) mode="exec"; exec_tpl="$2"; shift 2 ;;
    --kube) mode="kube"; kube_ns="$2"; shift 2 ;;
    --context) kube_ctx="$2"; shift 2 ;;
    --) shift; break ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1 (see --help)" >&2; exit 2 ;;
  esac
done
extra=("$@")
if [[ -n "$sql_file" && -n "$sql_stmt" ]] || [[ -z "$sql_file" && -z "$sql_stmt" ]]; then
  echo "give exactly one of -f file.sql or -c 'SQL'" >&2; exit 2
fi
[[ -z "$sql_file" || -r "$sql_file" ]] || { echo "cannot read $sql_file" >&2; exit 2; }

export PGHOST="${PGHOST:-localhost}" PGPORT="${PGPORT:-5433}" PGUSER="${PGUSER:-yugabyte}" \
       PGDATABASE="${PGDATABASE:-yugabyte}" PGPASSWORD="${PGPASSWORD:-yugabyte}"

sql_input() { if [[ -n "$sql_file" ]]; then cat "$sql_file"; else printf '%s\n;\n' "$sql_stmt"; fi; }
prefix() { local tag="$1"; while IFS= read -r line; do printf '[%s] %s\n' "$tag" "$line"; done; }

run_one() {  # $1 = tag, rest = command that reads SQL on stdin
  local tag="$1"; shift
  if ! sql_input | "$@" 2>&1 | prefix "$tag"; then echo "[$tag] FAILED: $*" >&2; fi
}

if [[ "$mode" == kube ]]; then
  kc=(kubectl); [[ -n "$kube_ctx" ]] && kc+=(--context "$kube_ctx")
  pods=$("${kc[@]}" get pods -n "$kube_ns" -o name | sed 's#^pod/##' | grep 'yb-tserver' || true)
  [[ -n "$pods" ]] || { echo "no yb-tserver pods in namespace $kube_ns" >&2; exit 1; }
  for pod in $pods; do
    run_one "$pod" "${kc[@]}" exec -i -n "$kube_ns" "$pod" -c yb-tserver -- \
      ysqlsh -U "$PGUSER" -d "$PGDATABASE" -X -f - "${extra[@]+"${extra[@]}"}"
  done
  exit 0
fi

servers=$(ysqlsh -X -tA -F ' ' -c "SELECT host, port FROM yb_servers() ORDER BY host")
[[ -n "$servers" ]] || { echo "yb_servers() returned nothing via $PGHOST:$PGPORT" >&2; exit 1; }

mapped() {  # YB_HOST_MAP lookup (plain loop: macOS ships bash 3.2, no associative arrays)
  local p pairs; IFS=',' read -r -a pairs <<< "${YB_HOST_MAP:-}"
  for p in "${pairs[@]+"${pairs[@]}"}"; do [[ "${p%%=*}" == "$1" ]] && { echo "${p#*=}"; return; }; done
  echo "$1:$2"
}

while read -r host port; do
  if [[ "$mode" == exec ]]; then
    read -r -a cmd <<< "${exec_tpl//\{host\}/$host}"
    run_one "$host" "${cmd[@]}" -X -f - "${extra[@]+"${extra[@]}"}"
  else
    target=$(mapped "$host" "$port")
    run_one "$host" ysqlsh -X -h "${target%:*}" -p "${target##*:}" -f - "${extra[@]+"${extra[@]}"}"
  fi
done <<< "$servers"
