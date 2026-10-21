# Aeon API: authentication and ID discovery

## API key

Create in the Aeon console: **Security → Access Control → API Keys → Create API Key**. Choose a
role (a key can do only what its role allows), an expiry (or never expire), and optionally
restrict the key to IP allow lists. The key is shown once; store it in a secret store and
revoke it if lost.

Scripts read the key from the environment (here `AEON_API_KEY`) and never print or log it.

## Base request pattern

```bash
# macOS / Linux
: "${AEON_API_KEY:?set AEON_API_KEY}"
BASE="https://cloud.yugabyte.com/api/public/v1"

curl -sS --fail-with-body -H "Authorization: Bearer $AEON_API_KEY" \
     -H "Content-Type: application/json" \
     "$BASE/<path>"
```

```powershell
# Windows PowerShell
$Base    = "https://cloud.yugabyte.com/api/public/v1"
$Headers = @{ Authorization = "Bearer $env:AEON_API_KEY"; "Content-Type" = "application/json" }

Invoke-RestMethod -Uri "$Base/<path>" -Headers $Headers
```

```python
# Python (requests)
import os, requests

BASE    = "https://cloud.yugabyte.com/api/public/v1"
HEADERS = {"Authorization": f"Bearer {os.environ['AEON_API_KEY']}",
           "Content-Type": "application/json"}

def get(path, **params):
    r = requests.get(f"{BASE}{path}", headers=HEADERS, params=params, timeout=30)
    r.raise_for_status()
    return r.json()
```

## Discovering your account ID

`GET /account` (singular) returns the account the key belongs to:

```bash
ACCOUNT_ID=$(curl -sS --fail-with-body -H "Authorization: Bearer $AEON_API_KEY" "$BASE/account" | jq -r '.data.info.id')
```

```python
account_id = get("/account")["data"]["info"]["id"]
```

There is no `GET /accounts` list.

## Listing projects

```bash
curl -sS --fail-with-body -H "Authorization: Bearer $AEON_API_KEY" "$BASE/accounts/$ACCOUNT_ID/projects" \
  | jq '.data[] | {id, name}'
```

```python
projects = get(f"/accounts/{account_id}/projects")["data"]
project_id = projects[0]["id"]          # each item also carries .info.id and .spec.name
```

## Finding a cluster by name

```bash
CLUSTER_ID=$(curl -sS --fail-with-body -H "Authorization: Bearer $AEON_API_KEY" \
  "$BASE/accounts/$ACCOUNT_ID/projects/$PROJECT_ID/clusters?name=orders-prod" \
  | jq -r '[.data[] | select(.spec.name == "orders-prod")][0].info.id // empty')
[ -n "$CLUSTER_ID" ] || { echo "no cluster named orders-prod in project $PROJECT_ID" >&2; exit 1; }
```

```python
clusters = get(f"/accounts/{account_id}/projects/{project_id}/clusters", name="orders-prod")["data"]
if not clusters:
    raise SystemExit("no cluster named orders-prod")
cluster_id = clusters[0]["info"]["id"]   # .spec.name, .info.state, .info.software_version
```

List calls also filter by `state`, `cloud`, `region` and `tier`.

## Pagination

List responses carry `_metadata.continuation_token`; pass it back as `continuation_token` (with
an optional `limit`) until it is null:

```python
def get_all(path, **params):
    items, token = [], None
    while True:
        page = get(path, **params, **({"continuation_token": token} if token else {}))
        items += page.get("data", [])
        token = (page.get("_metadata") or {}).get("continuation_token")
        if not token:
            return items
```

## Errors and asynchronous operations

| Status | Meaning |
|---|---|
| `200` | Read succeeded |
| `202` | Accepted: an edit, pause, resume, delete or restore has started |
| `400` | Bad request: the detail names the fields, e.g. `{"error": {"detail": "API Input Validation Failure, Error: ClusterSpec.name must not be null", "status": 400}}` |
| `401` | JSON `Invalid JWT` or `Request does not contain valid authorization token`: a bad or missing key. An **XML** `AuthenticationRequired` page means the path is not the public API (`/api/v1/...` instead of `/api/public/v1/...`), not a bad key |
| `403` | The key's role does not allow this operation, or the key is IP-restricted |
| `404` | An **HTML** page: the path does not exist (check `/api/public/v1` and hyphenated segments). JSON `Cannot find cluster with ID '...'` (or backup, project): the ID is wrong. An unknown allow-list ID answers `400` instead |
| `500` | `Unexpected internal error`: seen when changing a cluster while a task on it is still running; wait for the task and retry |

Error bodies are not always JSON: read `error.detail` when the body parses, otherwise report the
status and the start of the text.

Long-running operations return `202`; the cluster reads `QUEUED` at once, then transitional
states (`UPDATING`, `UPDATING_LB`, `PAUSING`, `RESUMING`, `DELETING`, ...). This helper waits
for a **state** and so suits pause (`want=("PAUSED",)`) and resume. An edit is done only when the
spec also shows the change (it updates near the end), so pass `done` for a scale, as below; an
allow-list assignment does not change the state at all: wait on its task (`recipes.md`).

```python
import time

def wait_for_state(account_id, project_id, cluster_id, want=("ACTIVE",), done=None, timeout_s=3600):
    """Wait until the cluster's state is in `want` and, if given, done(cluster) is true."""
    path = f"/accounts/{account_id}/projects/{project_id}/clusters/{cluster_id}"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        cluster = get(path)["data"]
        state = cluster["info"]["state"]
        if state.endswith("_FAILED"):
            raise RuntimeError(f"cluster entered {state}")
        if state in want and (done is None or done(cluster)):
            return cluster
        time.sleep(30)
    raise TimeoutError(f"cluster not settled in {want} after {timeout_s}s")

# A scale to 6 nodes: ACTIVE alone is not enough
# wait_for_state(a, p, c, done=lambda cl: cl["spec"]["cluster_info"]["num_nodes"] == 6)
```

Tasks for a cluster: `GET /accounts/{accountId}/tasks?entity_id={clusterId}`.

## Reference

https://api-docs.yugabyte.com/docs/managed-apis/9u5yqnccbe8lk-yugabyte-db-managed-rest-api has
every endpoint with its request and response schema. Verify a path there before using one not
listed in this skill.
