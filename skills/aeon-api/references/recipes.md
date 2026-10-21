# Aeon API recipes

Every example assumes `AEON_API_KEY` in the environment and `BASE`, `ACCOUNT_ID`, `PROJECT_ID`,
`CLUSTER_ID` discovered as in [`auth-and-discovery.md`](auth-and-discovery.md):

```bash
set -euo pipefail
BASE="https://cloud.yugabyte.com/api/public/v1"
P="$BASE/accounts/$ACCOUNT_ID/projects/$PROJECT_ID"
AUTH=(-H "Authorization: Bearer $AEON_API_KEY" -H "Content-Type: application/json")
```

`curl -sS --fail-with-body` (curl 7.76+) exits non-zero on an HTTP error and still prints the
body, so the error's `detail` (or the HTML / XML page, see the error table in
`auth-and-discovery.md`) reaches you. Plain `-f` throws the body away.

---

## Cluster details

```bash
curl -sS --fail-with-body "${AUTH[@]}" "$P/clusters/$CLUSTER_ID" | jq '.data | {
  name: .spec.name, state: .info.state, version: .info.software_version,
  nodes: .spec.cluster_info.num_nodes, vcpu: .spec.cluster_info.node_info.num_cores,
  memory_mb: .spec.cluster_info.node_info.memory_mb, disk_gb: .spec.cluster_info.node_info.disk_size_gb,
  regions: [.spec.cluster_region_info[]? | {region: .placement_info.cloud_info.region, nodes: .placement_info.num_nodes}]}'
```

States you will see: `ACTIVE`, `PAUSED`, and transitional ones such as `QUEUED`, `UPDATING`,
`UPDATING_LB`, `PAUSING`, `RESUMING`, `DELETING` (creation adds `BOOTSTRAPPING`,
`CONFIGURING` and others); failures end in `_FAILED`. Wait on "not yet settled", not on a list
of states you expect.

### Nodes

```bash
curl -sS --fail-with-body "${AUTH[@]}" "$P/clusters/$CLUSTER_ID/nodes" \
  | jq '.data[] | {name, up: .is_node_up, master: .is_master, region: .cloud_info.region, zone: .cloud_info.zone}'
```

---

## Scale a cluster (edit)

`PUT .../clusters/{clusterId}` replaces the cluster spec, so send the **whole** current `spec`
with your change. For a single-region cluster, change `cluster_info.num_nodes` and the region's
`placement_info.num_nodes` together; for multi-region, change each region's `num_nodes` and set
`cluster_info.num_nodes` to their sum. vCPUs and disk are `cluster_info.node_info.num_cores` and
`disk_size_gb`. Disk can only grow; on AWS a disk change (including more vCPUs, which grows the
disk) blocks further disk changes for six hours. Sandbox clusters cannot be scaled or paused.

```bash
# 1. Plan: build the new spec and show the change. Nothing is sent yet.
SPEC=$(curl -sS --fail-with-body "${AUTH[@]}" "$P/clusters/$CLUSTER_ID" | jq '.data.spec')
NEW=$(jq '.cluster_info.num_nodes = 6 | .cluster_region_info[0].placement_info.num_nodes = 6' <<<"$SPEC")
diff <(jq . <<<"$SPEC") <(jq . <<<"$NEW") || true
```

Show the diff to the user. Scaling is billable: send it only once they have confirmed.

```bash
# 2. Apply, after confirmation
curl -sS --fail-with-body -X PUT "${AUTH[@]}" "$P/clusters/$CLUSTER_ID" -d "$NEW" | jq '.data.info.state'   # 202, QUEUED
```

The `202` response already reads `QUEUED`, with the old size. Then the cluster reads `QUEUED`,
`UPDATING`, `UPDATING_LB` (most of the time), and `ACTIVE`: about 10 minutes for 3 to 6 nodes
on AWS. The new nodes appear in `/nodes` before the end, and `spec.cluster_info.num_nodes`
changes only shortly before `ACTIVE`. So the edit is done when the state is `ACTIVE` **and** the
spec shows the target (or when a new `EDIT_CLUSTER` task, newer than the last one before the
PUT, has `SUCCEEDED`: see the allow-list recipe for that pattern). Treat
any state other than `ACTIVE` or one ending in `_FAILED` as still in progress, and do not send
another edit until it is done.

```python
import os, sys, time, requests
BASE = "https://cloud.yugabyte.com/api/public/v1"
H = {"Authorization": f"Bearer {os.environ['AEON_API_KEY']}", "Content-Type": "application/json"}
url = f"{BASE}/accounts/{account_id}/projects/{project_id}/clusters/{cluster_id}"

cluster = requests.get(url, headers=H, timeout=30).json()["data"]
if cluster["info"]["state"] != "ACTIVE":
    raise SystemExit(f"cluster is {cluster['info']['state']}; wait for ACTIVE")
spec = cluster["spec"]
before = spec["cluster_info"]["num_nodes"]
spec["cluster_info"]["num_nodes"] = 6
spec["cluster_region_info"][0]["placement_info"]["num_nodes"] = 6
if "--yes" not in sys.argv and input(f"Scale {spec['name']} from {before} to 6 nodes? [y/N] ").lower() != "y":
    raise SystemExit("not confirmed")
r = requests.put(url, headers=H, json=spec, timeout=60)
r.raise_for_status()                      # 202 Accepted

deadline = time.monotonic() + 3600
while True:
    c = requests.get(url, headers=H, timeout=30).json()["data"]
    state = c["info"]["state"]
    if state.endswith("_FAILED"):
        raise SystemExit(f"edit failed: {state}")
    if state == "ACTIVE" and c["spec"]["cluster_info"]["num_nodes"] == 6:
        break
    if time.monotonic() > deadline:
        raise SystemExit(f"still {state} after an hour")
    time.sleep(30)                        # QUEUED, UPDATING, UPDATING_LB, ...
```

### Pause and resume

```bash
curl -sS --fail-with-body -X POST "${AUTH[@]}" "$P/clusters/$CLUSTER_ID/pause"    # 202; QUEUED -> PAUSING -> PAUSED
curl -sS --fail-with-body -X POST "${AUTH[@]}" "$P/clusters/$CLUSTER_ID/resume"   # 202; QUEUED -> RESUMING -> ACTIVE
```

Each takes several minutes on a 3-node cluster. A loop that waits for `PAUSED` must not treat
`QUEUED` (the first state after the request) as a failure.

Pausing stops compute billing; storage is still billed.

---

## Network allow lists

Allow lists belong to the **project**. Create one, then assign it to a cluster by ID. The
cluster's assignment is the full list of IDs: a PUT replaces it, so include the IDs already
assigned. The assignment PUT answers `202` and runs an `EDIT_ALLOW_LIST` task; wait for it to
succeed before changing the cluster's access again (a second PUT while it runs fails with 500).

```bash
# List the project's allow lists
curl -sS --fail-with-body "${AUTH[@]}" "$P/allow-lists" \
  | jq '.data[] | {id: .info.id, name: .spec.name, cidrs: .spec.allow_list, clusters: .info.cluster_ids}'

# Create one (name, description and allow_list are all required)
AL_ID=$(curl -sS --fail-with-body -X POST "${AUTH[@]}" "$P/allow-lists" -d '{
  "name": "office-vpn", "description": "Corporate VPN egress", "allow_list": ["203.0.113.0/24"]
}' | jq -r '.data.info.id')

# Assign it to the cluster, keeping the lists already assigned (202: an EDIT_ALLOW_LIST task)
TASKS="$BASE/accounts/$ACCOUNT_ID/tasks?entity_id=$CLUSTER_ID&task_type=EDIT_ALLOW_LIST&limit=1"
BEFORE=$(curl -sS --fail-with-body "${AUTH[@]}" "$TASKS" | jq -r '.data[0].info.id // ""')   # the last task, if any
CURRENT=$(curl -sS --fail-with-body "${AUTH[@]}" "$P/clusters/$CLUSTER_ID/allow-lists" | jq '[.data[].info.id]')
if jq -e --arg id "$AL_ID" 'index($id)' <<<"$CURRENT" >/dev/null; then
  echo "already assigned"
else
  curl -sS --fail-with-body -X PUT "${AUTH[@]}" "$P/clusters/$CLUSTER_ID/allow-lists" \
    -d "$(jq -c --arg id "$AL_ID" '. + [$id] | unique' <<<"$CURRENT")" >/dev/null
  # Wait for the new task (not an older SUCCEEDED one), with a deadline
  STATE=""
  for _ in $(seq 60); do
    T=$(curl -sS --fail-with-body "${AUTH[@]}" "$TASKS" | jq -r '.data[0].info | "\(.id) \(.state)"')
    [ "${T% *}" != "$BEFORE" ] && STATE="${T#* }"
    case "$STATE" in SUCCEEDED|FAILED) break ;; esac
    sleep 5
  done
  [ "$STATE" = SUCCEEDED ] || { echo "allow-list task: ${STATE:-not started} after 5 minutes" >&2; exit 1; }
fi
```

Removing access is a separate step (confirm it first). A list still assigned to a cluster is not
deleted, even though the DELETE answers `200`, so unassign it, wait for that task, check, then
delete and confirm:

```bash
# Unassign $AL_ID from the cluster (the PUT keeps every other list), and wait for the new task
TASKS="$BASE/accounts/$ACCOUNT_ID/tasks?entity_id=$CLUSTER_ID&task_type=EDIT_ALLOW_LIST&limit=1"
BEFORE=$(curl -sS --fail-with-body "${AUTH[@]}" "$TASKS" | jq -r '.data[0].info.id // ""')
KEEP=$(curl -sS --fail-with-body "${AUTH[@]}" "$P/clusters/$CLUSTER_ID/allow-lists" \
  | jq -c --arg id "$AL_ID" '[.data[].info.id | select(. != $id)]')
curl -sS --fail-with-body -X PUT "${AUTH[@]}" "$P/clusters/$CLUSTER_ID/allow-lists" -d "$KEEP" >/dev/null
# ... wait for a task newer than $BEFORE to reach SUCCEEDED, as above ...

# Delete only when no cluster uses it, then check it is gone
USED=$(curl -sS --fail-with-body "${AUTH[@]}" "$P/allow-lists/$AL_ID" | jq '.data.info.cluster_ids | length')
[ "$USED" -eq 0 ] || { echo "still assigned to $USED cluster(s)" >&2; exit 1; }
curl -sS --fail-with-body -X DELETE "${AUTH[@]}" "$P/allow-lists/$AL_ID" >/dev/null
curl -sS "${AUTH[@]}" "$P/allow-lists" | jq -e --arg id "$AL_ID" 'any(.data[]; .info.id == $id) | not' >/dev/null \
  || { echo "allow list $AL_ID still exists" >&2; exit 1; }
```

- Names are unique in a project: creating a list whose name exists fails with `400 A Network
  Allow List with this name already exists`. Look the name (or the CIDR) up first and reuse the
  list, so re-runs are idempotent.
- Deleting a list that is still assigned answers `200` but leaves the list in place, which is
  why the recipe checks `info.cluster_ids` first and confirms with a GET afterwards.

---

## Backups and restore

Backups are listed and created at the **project** level, filtered or targeted by cluster;
schedules are per cluster. Every cluster starts with a system default schedule (daily, kept 8
days). An on-demand backup of a cluster with no tables is refused (`400 Backups not allowed as
there are no tables on the cluster`).

```bash
# Completed and running backups for one cluster
curl -sS --fail-with-body "${AUTH[@]}" "$P/backups?cluster_id=$CLUSTER_ID" \
  | jq '.data[] | {id: .info.id, state: .info.state, completed: .info.completed_on, bytes: .info.size_in_bytes}'

# On-demand backup (202; a FULL backup unless backup_type says otherwise)
BACKUP_ID=$(curl -sS --fail-with-body -X POST "${AUTH[@]}" "$P/backups" -d "{
  \"cluster_id\": \"$CLUSTER_ID\", \"description\": \"pre-upgrade\", \"retention_period_in_days\": 7
}" | jq -r '.data.info.id')
# poll GET $P/backups/$BACKUP_ID until .data.info.state is SUCCEEDED (QUEUED, IN_PROGRESS, FAILED);
# a small cluster's backup takes a few minutes

# Schedules
curl -sS --fail-with-body "${AUTH[@]}" "$P/clusters/$CLUSTER_ID/backup-schedules" | jq '.data'
curl -sS --fail-with-body -X POST "${AUTH[@]}" "$P/clusters/$CLUSTER_ID/backup-schedules" -d '{
  "description": "nightly", "cron_expression": "0 2 * * *", "retention_period_in_days": 14, "state": "ACTIVE"
}'

# Delete a schedule
curl -sS --fail-with-body -X DELETE "${AUTH[@]}" "$P/clusters/$CLUSTER_ID/backup-schedules/$SCHEDULE_ID"

# Restore a backup into a cluster (confirm first): 202 with the restore's ID
RESTORE_ID=$(curl -sS --fail-with-body -X POST "${AUTH[@]}" "$P/restore" \
  -d "{\"backup_id\": \"$BACKUP_ID\", \"cluster_id\": \"$CLUSTER_ID\"}" | jq -r '.data.info.id')
# then poll GET $P/restore/$RESTORE_ID until .data.info.state is SUCCEEDED (or FAILED)
```

A restore does not overwrite: if a database in the backup already exists on the target cluster,
it is refused (`400 The database '<name>' already exists in the cluster. Either rename database,
or restore to a different cluster.`). Restore into a new or emptied cluster, or rename or drop
the database first (with the owner's agreement).

---

## Cluster metrics (curated set)

`GET .../clusters/{clusterId}/metrics` takes the metric names as repeated `metrics` query
parameters and the window in **epoch seconds**. Names include `CPU_USAGE`, `DISK_USAGE_GB`,
`READ_OPS_PER_SEC`, `WRITE_OPS_PER_SEC`, `AVERAGE_READ_LATENCY_MS`, `AVERAGE_WRITE_LATENCY_MS`,
`AVERAGE_YSQL_CONNECTION_COUNT`, `YSQL_SELECT_OPS_PER_SEC` (the API reference lists all).
Optional filters: `node_name`, `region`, `node_type` (`PRIMARY`, `READ_REPLICA`),
`namespace_name`.

```bash
END=$(date +%s); START=$((END - 3600))
curl -sS --fail-with-body "${AUTH[@]}" \
  "$P/clusters/$CLUSTER_ID/metrics?metrics=CPU_USAGE&metrics=READ_OPS_PER_SEC&start_time=$START&end_time=$END" \
  | jq '.data[] | {name, labels, points: (.values | length)}'
```

Each series is `{name, labels, values: [[timestamp, value], ...]}`; a metric with no samples in
the window is left out of `data` rather than returned empty. This is not the full
Prometheus surface: for that, configure metrics export (`metrics-exporter-configs`, or the
console's Integrations).

---

## Database users (SQL, not the API)

The API has no database-user resource. Create users with SQL on the cluster, connected as the
admin user set at cluster creation, over TLS verified with the cluster CA. The host is
`info.cluster_endpoints[].host` (or `info.endpoints.<region>`); the CA comes from
`GET /connection-certificate`, which needs no key.

```bash
HOST=$(curl -sS --fail-with-body "${AUTH[@]}" "$P/clusters/$CLUSTER_ID" | jq -r '.data.info.cluster_endpoints[0].host')
curl -sS --fail-with-body "$BASE/connection-certificate" | jq -r .data > root.crt
# PGPASSWORD (admin) and REPORTING_PASSWORD are exported from a secret store. Neither goes on a
# command line, where other users could read it with ps.
psql "host=$HOST port=5433 dbname=yugabyte user=admin sslmode=verify-full sslrootcert=root.crt" \
  -v ON_ERROR_STOP=1 -f reporting.sql
```

```sql
-- reporting.sql: idempotent, re-runnable. psql reads the password from the environment itself
-- (\getenv, psql or ysqlsh 15+), so it is never in argv.
\getenv pw REPORTING_PASSWORD
\if :{?pw}
\else
  DO $$ BEGIN RAISE EXCEPTION 'REPORTING_PASSWORD is not set'; END $$;
\endif
SELECT 'CREATE ROLE reporting LOGIN' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'reporting')
\gexec
ALTER ROLE reporting WITH LOGIN PASSWORD :'pw';
GRANT SELECT ON ALL TABLES IN SCHEMA public TO reporting;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO reporting;
```

Aeon has no superuser for you to grant; the admin user does not own the `yugabyte` database or
the `public` schema, so `GRANT CONNECT` / `GRANT USAGE` on them only warn (every role already
has both through PUBLIC). The client also needs network access: an allow list holding its
address, assigned to the cluster.

---

## Python helper

```python
import os, requests

BASE = "https://cloud.yugabyte.com/api/public/v1"
H = {"Authorization": f"Bearer {os.environ['AEON_API_KEY']}", "Content-Type": "application/json"}

def call(method, path, body=None, **params):
    r = requests.request(method, f"{BASE}{path}", headers=H, json=body, params=params, timeout=60)
    if r.status_code >= 400:
        try:
            detail = r.json()["error"]["detail"]
        except (ValueError, KeyError, TypeError):
            detail = r.text[:200]          # 404s are HTML; /api/v1 401s are XML
        raise RuntimeError(f"{method} {path}: {r.status_code} {detail}")
    return r.json() if r.content else None

account_id = call("GET", "/account")["data"]["info"]["id"]
for proj in call("GET", f"/accounts/{account_id}/projects")["data"]:
    for c in call("GET", f"/accounts/{account_id}/projects/{proj['id']}/clusters")["data"]:
        print(proj["name"], c["spec"]["name"], c["info"]["state"])
```
