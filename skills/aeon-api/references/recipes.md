# Aeon API recipes

Every example assumes `AEON_API_KEY` in the environment and `BASE`, `ACCOUNT_ID`, `PROJECT_ID`,
`CLUSTER_ID` discovered as in [`auth-and-discovery.md`](auth-and-discovery.md):

```bash
BASE="https://cloud.yugabyte.com/api/public/v1"
P="$BASE/accounts/$ACCOUNT_ID/projects/$PROJECT_ID"
AUTH=(-H "Authorization: Bearer $AEON_API_KEY" -H "Content-Type: application/json")
```

---

## Cluster details

```bash
curl -sf "${AUTH[@]}" "$P/clusters/$CLUSTER_ID" | jq '.data | {
  name: .spec.name, state: .info.state, version: .info.software_version,
  nodes: .spec.cluster_info.num_nodes, vcpu: .spec.cluster_info.node_info.num_cores,
  memory_mb: .spec.cluster_info.node_info.memory_mb, disk_gb: .spec.cluster_info.node_info.disk_size_gb,
  regions: [.spec.cluster_region_info[]? | {region: .placement_info.cloud_info.region, nodes: .placement_info.num_nodes}]}'
```

States you will see: `ACTIVE`, `PAUSED`, and transitional ones such as `QUEUED`, `PROVISIONING`,
`UPDATING`, `PAUSING`, `RESUMING`, `DELETING`; failures end in `_FAILED`.

### Nodes

```bash
curl -sf "${AUTH[@]}" "$P/clusters/$CLUSTER_ID/nodes" \
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
SPEC=$(curl -sf "${AUTH[@]}" "$P/clusters/$CLUSTER_ID" | jq '.data.spec')
NEW=$(jq '.cluster_info.num_nodes = 6 | .cluster_region_info[0].placement_info.num_nodes = 6' <<<"$SPEC")
diff <(jq . <<<"$SPEC") <(jq . <<<"$NEW")          # show the change; confirm before sending
curl -sf -X PUT "${AUTH[@]}" "$P/clusters/$CLUSTER_ID" -d "$NEW" | jq '.data.info.state'   # 202
```

Then poll `info.state` until `ACTIVE` (see `wait_for_state` in `auth-and-discovery.md`). Do not
send another edit while the cluster is `UPDATING`.

```python
import os, requests
BASE = "https://cloud.yugabyte.com/api/public/v1"
H = {"Authorization": f"Bearer {os.environ['AEON_API_KEY']}", "Content-Type": "application/json"}
url = f"{BASE}/accounts/{account_id}/projects/{project_id}/clusters/{cluster_id}"

cluster = requests.get(url, headers=H, timeout=30).json()["data"]
if cluster["info"]["state"] != "ACTIVE":
    raise SystemExit(f"cluster is {cluster['info']['state']}; wait for ACTIVE")
spec = cluster["spec"]
spec["cluster_info"]["num_nodes"] = 6
spec["cluster_region_info"][0]["placement_info"]["num_nodes"] = 6
r = requests.put(url, headers=H, json=spec, timeout=60)
r.raise_for_status()                      # 202 Accepted; poll until ACTIVE
```

### Pause and resume

```bash
curl -sf -X POST "${AUTH[@]}" "$P/clusters/$CLUSTER_ID/pause"    # 202; QUEUED -> PAUSING -> PAUSED
curl -sf -X POST "${AUTH[@]}" "$P/clusters/$CLUSTER_ID/resume"   # 202; QUEUED -> RESUMING -> ACTIVE
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
curl -sf "${AUTH[@]}" "$P/allow-lists" \
  | jq '.data[] | {id: .info.id, name: .spec.name, cidrs: .spec.allow_list, clusters: .info.cluster_ids}'

# Create one (name, description and allow_list are all required)
AL_ID=$(curl -sf -X POST "${AUTH[@]}" "$P/allow-lists" -d '{
  "name": "office-vpn", "description": "Corporate VPN egress", "allow_list": ["203.0.113.0/24"]
}' | jq -r '.data.info.id')

# Assign it to the cluster, keeping the lists already assigned (202: an EDIT_ALLOW_LIST task)
CURRENT=$(curl -sf "${AUTH[@]}" "$P/clusters/$CLUSTER_ID/allow-lists" | jq '[.data[].info.id]')
curl -sf -X PUT "${AUTH[@]}" "$P/clusters/$CLUSTER_ID/allow-lists" \
  -d "$(jq -c --arg id "$AL_ID" '. + [$id] | unique' <<<"$CURRENT")"

# Wait for the task before any further change
until [ "$(curl -sf "${AUTH[@]}" "$BASE/accounts/$ACCOUNT_ID/tasks?entity_id=$CLUSTER_ID&task_type=EDIT_ALLOW_LIST&limit=1" \
          | jq -r '.data[0].info.state')" = SUCCEEDED ]; do sleep 10; done

# Delete an allow list: unassign it from every cluster first (and wait for those tasks)
curl -sf -X DELETE "${AUTH[@]}" "$P/allow-lists/$AL_ID"
```

- Names are unique in a project: creating a list whose name exists fails with `400 A Network
  Allow List with this name already exists`. Look the name (or the CIDR) up first and reuse the
  list, so re-runs are idempotent.
- Deleting a list that is still assigned answers `200` but leaves the list in place: check
  `info.cluster_ids` is empty first, and confirm with a GET afterwards.

---

## Backups and restore

Backups are listed and created at the **project** level, filtered or targeted by cluster;
schedules are per cluster. Every cluster starts with a system default schedule (daily, kept 8
days). An on-demand backup of a cluster with no tables is refused (`400 Backups not allowed as
there are no tables on the cluster`).

```bash
# Completed and running backups for one cluster
curl -sf "${AUTH[@]}" "$P/backups?cluster_id=$CLUSTER_ID" \
  | jq '.data[] | {id: .info.id, state: .info.state, completed: .info.completed_on, bytes: .info.size_in_bytes}'

# On-demand backup (202; a FULL backup unless backup_type says otherwise)
BACKUP_ID=$(curl -sf -X POST "${AUTH[@]}" "$P/backups" -d "{
  \"cluster_id\": \"$CLUSTER_ID\", \"description\": \"pre-upgrade\", \"retention_period_in_days\": 7
}" | jq -r '.data.info.id')
# poll GET $P/backups/$BACKUP_ID until .data.info.state is SUCCEEDED (QUEUED, IN_PROGRESS, FAILED);
# a small cluster's backup takes a few minutes

# Schedules
curl -sf "${AUTH[@]}" "$P/clusters/$CLUSTER_ID/backup-schedules" | jq '.data'
curl -sf -X POST "${AUTH[@]}" "$P/clusters/$CLUSTER_ID/backup-schedules" -d '{
  "description": "nightly", "cron_expression": "0 2 * * *", "retention_period_in_days": 14, "state": "ACTIVE"
}'

# Delete a schedule
curl -sf -X DELETE "${AUTH[@]}" "$P/clusters/$CLUSTER_ID/backup-schedules/$SCHEDULE_ID"

# Restore a backup into a cluster (confirm first)
curl -sf -X POST "${AUTH[@]}" "$P/restore" -d "{\"backup_id\": \"$BACKUP_ID\", \"cluster_id\": \"$CLUSTER_ID\"}"
# then poll GET $P/restore/$RESTORE_ID until .data.info.state is SUCCEEDED
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
curl -sf "${AUTH[@]}" \
  "$P/clusters/$CLUSTER_ID/metrics?metrics=CPU_USAGE&metrics=READ_OPS_PER_SEC&start_time=$START&end_time=$END" \
  | jq '.data[] | {name, labels, points: (.values | length)}'
```

Each series is `{name, labels, values: [[timestamp, value], ...]}`; a metric with no samples in
the window is left out of `data` rather than returned empty. This is not the full
Prometheus surface: for that, configure metrics export (`metrics-exporter-configs`, or the
console's Integrations).

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
