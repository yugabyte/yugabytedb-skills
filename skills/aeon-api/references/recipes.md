# Aeon API recipes

Ready-to-use operations. All examples assume `$AEON_TOKEN`, `$BASE`, `$ACCOUNT_ID`, `$PROJECT_ID`, `$CLUSTER_ID` are set — see [`auth-and-discovery.md`](auth-and-discovery.md).

---

## Cluster information

### Get cluster details (state, spec, version, nodes)
```bash
curl -s -H "Authorization: Bearer $AEON_TOKEN" \
  "$BASE/accounts/$ACCOUNT_ID/projects/$PROJECT_ID/clusters/$CLUSTER_ID" \
  | jq '{name: .spec.name, state: .info.state, version: .info.software_version,
         nodes: .spec.cluster_info.num_nodes, vcpu: .spec.cluster_info.node_info.num_cores,
         disk_gb: .spec.cluster_info.node_info.disk_size_gb}'
```

**Cluster states:** `ACTIVE`, `PAUSED`, `CREATING`, `MODIFYING`, `DELETING`, `FAILED`. Operations on a cluster in a transitional state return `409` — wait for `ACTIVE` before modifying.

### List nodes
```bash
curl -s -H "Authorization: Bearer $AEON_TOKEN" \
  "$BASE/accounts/$ACCOUNT_ID/projects/$PROJECT_ID/clusters/$CLUSTER_ID/nodes" \
  | jq '.data[] | {name: .name, state: .is_node_up, region: .cloud_info.region, az: .cloud_info.zone}'
```

---

## Cluster scaling (modify spec)

Scale node count or vCPU/disk. The request body only needs to include the fields you want to change alongside the required cluster identification fields. Check the Swagger for the full schema — the exact body shape varies with cluster type (single-region vs multi-region).

```bash
# Example: scale a single-region cluster to 6 nodes
curl -s -X PUT \
  -H "Authorization: Bearer $AEON_TOKEN" \
  -H "Content-Type: application/json" \
  "$BASE/accounts/$ACCOUNT_ID/projects/$PROJECT_ID/clusters/$CLUSTER_ID" \
  -d '{
    "spec": {
      "cluster_info": {
        "num_nodes": 6
      }
    }
  }' | jq '{id: .info.id, state: .info.state}'
```

After issuing a scale request, poll until the cluster returns to `ACTIVE`:
```bash
watch -n 10 'curl -s -H "Authorization: Bearer $AEON_TOKEN" \
  "$BASE/accounts/$ACCOUNT_ID/projects/$PROJECT_ID/clusters/$CLUSTER_ID" \
  | jq .info.state'
```

### Pause and resume (Dedicated clusters)
```bash
# Pause (stops billing for compute; storage continues)
curl -s -X POST -H "Authorization: Bearer $AEON_TOKEN" \
  "$BASE/accounts/$ACCOUNT_ID/projects/$PROJECT_ID/clusters/$CLUSTER_ID/pause"

# Resume
curl -s -X POST -H "Authorization: Bearer $AEON_TOKEN" \
  "$BASE/accounts/$ACCOUNT_ID/projects/$PROJECT_ID/clusters/$CLUSTER_ID/resume"
```

---

## Database users

### List database users
```bash
curl -s -H "Authorization: Bearer $AEON_TOKEN" \
  "$BASE/accounts/$ACCOUNT_ID/projects/$PROJECT_ID/clusters/$CLUSTER_ID/db_credentials" \
  | jq '.data[] | {username: .spec.username}'
```

### Create a database user
```bash
curl -s -X POST \
  -H "Authorization: Bearer $AEON_TOKEN" \
  -H "Content-Type: application/json" \
  "$BASE/accounts/$ACCOUNT_ID/projects/$PROJECT_ID/clusters/$CLUSTER_ID/db_credentials" \
  -d '{"spec": {"username": "appuser", "password": "SecurePassw0rd!"}}'
```

### Change a database user password
```bash
curl -s -X PUT \
  -H "Authorization: Bearer $AEON_TOKEN" \
  -H "Content-Type: application/json" \
  "$BASE/accounts/$ACCOUNT_ID/projects/$PROJECT_ID/clusters/$CLUSTER_ID/db_credentials/appuser" \
  -d '{"spec": {"password": "NewSecurePassw0rd!"}}'
```

---

## Network allowlists

Allowlists control which IP ranges can connect to the cluster.

### List allowlists
```bash
curl -s -H "Authorization: Bearer $AEON_TOKEN" \
  "$BASE/accounts/$ACCOUNT_ID/projects/$PROJECT_ID/clusters/$CLUSTER_ID/allow_lists" \
  | jq '.data[] | {id: .info.id, name: .spec.name, cidrs: .spec.allow_list}'
```

### Create an allowlist
```bash
curl -s -X POST \
  -H "Authorization: Bearer $AEON_TOKEN" \
  -H "Content-Type: application/json" \
  "$BASE/accounts/$ACCOUNT_ID/projects/$PROJECT_ID/clusters/$CLUSTER_ID/allow_lists" \
  -d '{
    "spec": {
      "name": "office-vpn",
      "description": "Corporate VPN egress",
      "allow_list": ["203.0.113.0/24"]
    }
  }' | jq '{id: .info.id, name: .spec.name}'
```

### Remove an allowlist
```bash
curl -s -X DELETE -H "Authorization: Bearer $AEON_TOKEN" \
  "$BASE/accounts/$ACCOUNT_ID/projects/$PROJECT_ID/clusters/$CLUSTER_ID/allow_lists/$ALLOWLIST_ID"
```

---

## Backups

### List backup schedules
```bash
curl -s -H "Authorization: Bearer $AEON_TOKEN" \
  "$BASE/accounts/$ACCOUNT_ID/projects/$PROJECT_ID/clusters/$CLUSTER_ID/backup_schedules" \
  | jq '.data[] | {id: .info.id, state: .info.state, cron: .spec.cron_expression,
                   retention_days: .spec.retention_period_in_days}'
```

### Trigger an on-demand backup
```bash
curl -s -X POST \
  -H "Authorization: Bearer $AEON_TOKEN" \
  -H "Content-Type: application/json" \
  "$BASE/accounts/$ACCOUNT_ID/projects/$PROJECT_ID/clusters/$CLUSTER_ID/backups" \
  -d '{"spec": {"backup_description": "pre-upgrade snapshot"}}' \
  | jq '{id: .info.id, state: .info.state}'
```

Poll the backup until `info.state` is `SUCCEEDED` before proceeding with any destructive operation.

### List completed backups
```bash
curl -s -H "Authorization: Bearer $AEON_TOKEN" \
  "$BASE/accounts/$ACCOUNT_ID/projects/$PROJECT_ID/clusters/$CLUSTER_ID/backups" \
  | jq '.data[] | {id: .info.id, state: .info.state, created: .info.metadata.created_on,
                   size_bytes: .spec.backup_size}'
```

---

## Cluster metrics (limited curated set)

The Aeon metrics API exposes a curated set of operational metrics — **not** the full ~2,000-metric Prometheus surface available on YBA. Available metrics include: operations per second, average latency, CPU usage, disk usage, connection counts, and similar console-chart data.

For the full Prometheus metric surface, configure metrics export: Aeon console → cluster → Settings → Metrics Export (Datadog, Grafana Cloud, or self-hosted Prometheus).

```bash
# List available metric names for a cluster
curl -s -H "Authorization: Bearer $AEON_TOKEN" \
  "$BASE/accounts/$ACCOUNT_ID/projects/$PROJECT_ID/clusters/$CLUSTER_ID/metrics" \
  | jq '[.data[].spec.metric_name] | unique'

# Query a specific metric (adjust metric_name and time range as needed)
curl -s -X POST \
  -H "Authorization: Bearer $AEON_TOKEN" \
  -H "Content-Type: application/json" \
  "$BASE/accounts/$ACCOUNT_ID/projects/$PROJECT_ID/clusters/$CLUSTER_ID/metrics" \
  -d '{
    "metrics": [
      {
        "metric_name": "YSQL_OPS_PER_SEC",
        "node_name": "all"
      }
    ],
    "start_time": "2025-06-01T00:00:00Z",
    "end_time":   "2025-06-01T01:00:00Z"
  }' | jq '.data'
```

**Note:** Exact metric names, the request body schema, and available time ranges may change between Aeon versions. Check `https://cloud.yugabyte.com/swagger` under the `cluster-metrics` section for the current contract.

---

## Python helper

A minimal Python client for use in scripts:

```python
import requests, json

TOKEN     = "your-api-key-here"
BASE      = "https://cloud.yugabyte.com/api/v1"
HEADERS   = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

def aeon_get(path):
    r = requests.get(f"{BASE}{path}", headers=HEADERS)
    r.raise_for_status()
    return r.json()

def aeon_post(path, body=None):
    r = requests.post(f"{BASE}{path}", headers=HEADERS, json=body or {})
    r.raise_for_status()
    return r.json()

def aeon_put(path, body):
    r = requests.put(f"{BASE}{path}", headers=HEADERS, json=body)
    r.raise_for_status()
    return r.json()

def aeon_delete(path):
    r = requests.delete(f"{BASE}{path}", headers=HEADERS)
    r.raise_for_status()

# Example: list all clusters across all projects
accounts = aeon_get("/accounts")["data"]
for acct in accounts:
    aid = acct["info"]["id"]
    for proj in aeon_get(f"/accounts/{aid}/projects")["data"]:
        pid = proj["info"]["id"]
        for cluster in aeon_get(f"/accounts/{aid}/projects/{pid}/clusters")["data"]:
            print(cluster["spec"]["name"], cluster["info"]["state"])
```
