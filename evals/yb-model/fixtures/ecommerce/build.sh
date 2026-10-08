#!/usr/bin/env bash
# Rebuild this fixture's bundle/ from setup.sql, load.sql and gen_workload.py on a scratch
# YugabyteDB container. Usage: ./build.sh [image]   (default: newest local yugabytedb/yugabyte)
set -euo pipefail
cd "$(dirname "$0")"
IMG="${1:-$(docker images --format '{{.Repository}}:{{.Tag}}' yugabytedb/yugabyte | sort | tail -1)}"
C=ybm-fixture-$$
SKILL=../../../../skills/yb-model
docker run -d --name "$C" "$IMG" bin/yugabyted start --background=false --ui=false >/dev/null
trap 'docker rm -f "$C" >/dev/null' EXIT
for i in $(seq 1 100); do docker exec "$C" bash -c 'bin/ysqlsh -h $(hostname) -tAc "select 1"' >/dev/null 2>&1 && break; sleep 3; done
q() { docker exec -i "$C" bash -c "bin/ysqlsh -h \$(hostname) -X -q -v ON_ERROR_STOP=1 -d shop $*"; }
docker exec "$C" bash -c 'bin/ysqlsh -h $(hostname) -X -q -c "CREATE DATABASE shop"'
q < setup.sql
q < load.sql
q -c "'CREATE EXTENSION IF NOT EXISTS pg_stat_statements; SELECT pg_stat_statements_reset();'" >/dev/null
python3 gen_workload.py | q >/dev/null
rm -rf bundle && mkdir bundle
docker exec "$C" mkdir -p /tmp/bundle
docker cp "$SKILL/scripts/collect.sql" "$C:/tmp/bundle/collect.sql"
docker exec "$C" bash -c 'cd /tmp/bundle && /home/yugabyte/bin/ysqlsh -h $(hostname) -X -q -d shop -f collect.sql'
docker exec "$C" bash -c '/home/yugabyte/postgres/bin/ysql_dump -h $(hostname) -d shop --schema-only --include-yb-metadata > /tmp/bundle/schema.sql'
docker exec "$C" rm /tmp/bundle/collect.sql
docker cp "$C:/tmp/bundle/." bundle/
ls -la bundle
# Present the data as production-sized: multiply reltuples and n_live_tup by SCALE (default
# 100). Column statistics stay as measured; negative n_distinct values scale with row count.
# Sequence positions in schema.sql are not scaled; a careful reviewer can still notice that.
python3 - "${SCALE:-100}" <<'PY'
import csv, sys
scale = float(sys.argv[1])
rows = list(csv.DictReader(open("bundle/ybm_reltuples.csv")))
for r in rows:
    if float(r["reltuples"]) > 0:
        r["reltuples"] = str(int(float(r["reltuples"]) * scale))
w = csv.DictWriter(open("bundle/ybm_reltuples.csv", "w", newline=""), fieldnames=list(rows[0]))
w.writeheader(); w.writerows(rows)
rows = list(csv.DictReader(open("bundle/ybm_table_usage.csv")))
for r in rows:
    if r.get("n_live_tup") and float(r["n_live_tup"]) > 0:
        r["n_live_tup"] = str(int(float(r["n_live_tup"]) * scale))
w = csv.DictWriter(open("bundle/ybm_table_usage.csv", "w", newline=""), fieldnames=list(rows[0]))
w.writeheader(); w.writerows(rows)
PY
