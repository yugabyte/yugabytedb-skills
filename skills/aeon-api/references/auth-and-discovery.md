# Aeon API — authentication and ID discovery

## API key

Generate in the Aeon console: profile icon (top-right) → **API Keys** → **Create API Key**.

The key is shown once on creation — store it securely. It does not expire unless manually revoked. It grants access to all resources in your account.

## Base request pattern

```bash
# macOS / Linux
AEON_TOKEN="your-api-key-here"
BASE="https://cloud.yugabyte.com/api/v1"

curl -s -H "Authorization: Bearer $AEON_TOKEN" \
     -H "Content-Type: application/json" \
     "$BASE/<path>"
```

```powershell
# Windows PowerShell
$Token = "your-api-key-here"
$Base  = "https://cloud.yugabyte.com/api/v1"
$Headers = @{ Authorization = "Bearer $Token"; "Content-Type" = "application/json" }

Invoke-RestMethod -Uri "$Base/<path>" -Headers $Headers
```

```python
# Python (requests)
import requests

TOKEN = "your-api-key-here"
BASE  = "https://cloud.yugabyte.com/api/v1"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

def get(path):
    r = requests.get(f"{BASE}{path}", headers=HEADERS)
    r.raise_for_status()
    return r.json()
```

## Discovering your account ID

Your `accountId` is fixed per account. Find it via:

```bash
curl -s -H "Authorization: Bearer $AEON_TOKEN" \
  "https://cloud.yugabyte.com/api/v1/accounts" | jq '.data[].info.id'
```

```python
accounts = get("/accounts")
account_id = accounts["data"][0]["info"]["id"]
print(account_id)  # e.g. "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

It also appears in the Aeon console URL when you navigate to account settings.

## Listing projects

```bash
curl -s -H "Authorization: Bearer $AEON_TOKEN" \
  "$BASE/accounts/$ACCOUNT_ID/projects" | jq '.data[] | {id: .info.id, name: .spec.name}'
```

```python
projects = get(f"/accounts/{account_id}/projects")
for p in projects["data"]:
    print(p["info"]["id"], p["spec"]["name"])
```

Most accounts have one project. The `projectId` is needed for all cluster operations.

## Listing clusters and finding clusterId

```bash
curl -s -H "Authorization: Bearer $AEON_TOKEN" \
  "$BASE/accounts/$ACCOUNT_ID/projects/$PROJECT_ID/clusters" \
  | jq '.data[] | {id: .info.id, name: .spec.name, state: .info.state}'
```

```python
clusters = get(f"/accounts/{account_id}/projects/{project_id}/clusters")
for c in clusters["data"]:
    print(c["info"]["id"], c["spec"]["name"], c["info"]["state"])
```

## Pagination

List endpoints return paginated responses. Check for a `next_page_token` in the response and pass it as `?page_token=<token>` to get the next page:

```python
def get_all(path):
    results, page_token = [], None
    while True:
        url = path + (f"?page_token={page_token}" if page_token else "")
        data = get(url)
        results.extend(data.get("data", []))
        page_token = data.get("next_page_token")
        if not page_token:
            break
    return results
```

## Error handling

| HTTP status | Meaning |
|---|---|
| `200` | Success |
| `400` | Bad request — check the request body structure |
| `401` | Invalid or missing API token |
| `403` | Token valid but insufficient permissions for the operation |
| `404` | Resource not found — check accountId/projectId/clusterId |
| `409` | Conflict — cluster is in a transitional state; wait and retry |
| `422` | Unprocessable — validation error; response body contains details |

Long-running operations (cluster create, scale, restore) return a task/operation ID in the response. Poll the cluster state (`GET .../clusters/{clusterId}`) until `info.state` returns `ACTIVE`.

## Swagger reference

The full endpoint catalogue with request/response schemas is at:
`https://cloud.yugabyte.com/swagger`

Use it to discover endpoints not covered in `recipes.md` and to verify exact request body schemas, which change between Aeon versions.
