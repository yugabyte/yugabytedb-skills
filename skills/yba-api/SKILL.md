---
name: yba-api
description: Use when scripting, automating, or troubleshooting the YugabyteDB Anywhere (YBA) REST API — the control-plane API for self-managed YBA instances that creates and manages providers, releases, universes, backups, KMS, and DR. Triggers on `/api/v1/`, `/api/v2/`, `X-AUTH-YW-API-TOKEN`, `apiToken`, `customerUUID`, `taskUUID`, `Invoke-YbaRequest`, registration of a new YBA instance, or any mention of the YBA API. Does NOT cover YugabyteDB Aeon (use the Aeon API skill) or database access via YSQL/YCQL (use those skills).
---

# YugabyteDB Anywhere REST API

This skill covers the **YBA control-plane REST API** — the HTTP API exposed by a self-managed YugabyteDB Anywhere (YBA) instance for managing providers, releases, universes, backups, KMS, alerts, HA, RBAC, and DR.

It is **not** about:
- **YugabyteDB Aeon** — fully-managed cloud DBaaS, different API surface.
- **YSQL / YCQL** — the database APIs (PostgreSQL- and Cassandra-compatible) on ports 5433 / 9042.

**Reference docs:**
- Overview: <https://docs.yugabyte.com/stable/yugabyte-platform/anywhere-automation/anywhere-api/>
- Interactive reference: <https://api-docs.yugabyte.com/docs/yugabyte-platform/f10502c9c9623-yugabyte-db-anywhere-api-overview>
- v1 endpoint registry (Play routes): <https://github.com/yugabyte/yugabyte-db/blob/master/managed/src/main/resources/v1.routes>
- v2 OpenAPI spec: <https://github.com/yugabyte/yugabyte-db/tree/master/managed/src/main/resources/openapi> (entry point: `paths/_index.yaml`)

**This skill includes:**
- `references/python-client.md` — minimal Python wrapper (`yba_request`) plus splat-style usage. Read this when writing or generating Python code.
- `references/powershell-client.md` — standalone PowerShell wrapper (`Invoke-YbaRequest`, `Get-YbaApiToken`, `Register-YbaInstance`, `Get-YbaCustomerId`) that does **not** require the `powershell-yba` module. Read this when the user is on PowerShell or has no Python available.
- `references/recipes.md` — concrete request/response shapes for register/login, providers, releases, **v1 and v2 universe creation (cloud and Kubernetes)**, **storage configs (incl. multi-region S3 with HTTPS proxy)**, **telemetry providers**, **universe health checks and alerts**, **runtime configuration at all scopes**, backups/restore, and async tasks. Read this when crafting an unfamiliar request.
- `references/prometheus.md` — querying the Prometheus instance bundled with YBA on port 9090 for universe metrics (YCQL/YSQL ops/sec, latency histograms, container CPU/memory on Kubernetes, node-exporter CPU on cloud, tablet-leader balance, xCluster lag). Includes Python and PowerShell helpers and the `node_prefix` / pod-regex derivation from a universe API response. Read this when the user wants metrics, dashboards or charts.

## v1 vs v2

YBA exposes two API versions side-by-side. Both are mounted under `/api/` on the same host and share the same `X-AUTH-YW-API-TOKEN` header.

| | **v1 (`/api/v1/...`)** | **v2 (`/api/v2/...`)** |
|---|---|---|
| Coverage | **Comprehensive** — every YBA capability is reachable here. | **Partial and growing.** Initially focused on universes, upgrades, RBAC, releases, telemetry. |
| Request/response models | Tightly coupled to internal Java/Play models — many optional fields, deep nesting, fields that the server fills in for you, deprecated artefacts. | Decoupled, hand-curated OpenAPI schemas — simpler, flatter shapes; what you send maps cleanly to what gets stored. |
| Definition source | `managed/src/main/resources/v1.routes` (Play routes file) | `managed/src/main/resources/openapi/` (OpenAPI 3) |
| Versioning | Stable. Old endpoints are kept but may be marked deprecated. | Endpoints carry `x-yba-api-visibility` (`preview`, `beta`, `stable`, `deprecated`) and `x-yba-api-since`. |
| When to prefer | Anything not yet in v2 (most providers, KMS, HA, alerts, runtime config). | New universe creation/edit/upgrade flows where it exists — the payload is much smaller. |

**Default rule:** prefer v2 if the endpoint exists; fall back to v1 for everything else. Both can be used in the same script and against the same token.

## Authentication

All authenticated requests use a single header:

```
X-AUTH-YW-API-TOKEN: <api-token>
Accept: application/json
Content-Type: application/json
```

There are three ways to obtain an API token. Pick the first one that applies.

### 1. Reuse a previously retrieved token

Tokens **do not expire** — they remain valid until explicitly rotated. If the user already has one, use it directly. Do not log it; treat it like a password. Common storage:

```bash
export YBA_URL=https://yba.example.com
export YBA_TOKEN=...        # never echo, never commit
export YBA_CUSTOMER_ID=...  # optional, can be looked up (see below)
```

### 2. Log in with email + password (existing instance)

```
POST /api/v1/api_login
{"email": "<email>", "password": "<password>"}
```

Returns:
```json
{"apiToken": "...", "apiTokenVersion": 2, "customerUUID": "...", "userUUID": "..."}
```

Note that this **rotates** the API token — any token previously issued for this user becomes invalid. To rotate explicitly without logging in again, `PUT /api/v1/customers/{cUUID}/api_token`.

### 3. Register a brand-new instance

A newly provisioned YBA has no users. The first call must be:

```
POST /api/v1/register?generateApiToken=1
{"code": "dev|stg|prd", "name": "<full name>", "email": "<email>", "password": "<password>"}
```

This creates the first customer (tenant) and admin user, and returns the same `{apiToken, customerUUID, userUUID, ...}` shape as login. Subsequent registration attempts return `cannot register multiple accounts` — handle that by falling back to login.

## Customer (tenant) ID

Almost every endpoint is scoped to a customer / tenant: `/api/v1/customers/{cUUID}/...`. YBA today is effectively single-tenant per instance, so this UUID is constant for the lifetime of the instance.

```
GET /api/v1/customers   →   [{"uuid": "...", "code": "...", "name": "..."}]
```

**Look it up once and cache it for the session.** The Python wrapper in `references/python-client.md` does this in a module-level dict keyed by base URL.

## Async tasks

Many state-changing endpoints (create universe, edit universe, software upgrade, create provider, take backup, …) return immediately with a task handle:

```json
{"taskUUID": "f3...", "resourceUUID": "..."}
```

Poll the task until it terminates:

```
GET /api/v1/customers/{cUUID}/tasks/{taskUUID}
```

The task is finished when one of these is true:
- `percent == 100` → success
- `status` is one of `Aborted`, `Failure` → failed (read `details.taskDetails[*]` for the failed sub-step)

Recommended polling: every 5 to 10 seconds with an overall timeout of 10 minutes for most universe operations (longer for software upgrades or large backups). Treat the absence of a `taskUUID` field as a synchronous response — return it directly.

## Endpoint discovery

Two authoritative sources, in this order:

1. **Interactive reference** at <https://api-docs.yugabyte.com/docs/yugabyte-platform/> — readable, has request/response examples, lets you filter by tag.
2. **Source of truth in the repo** — use these when the docs are stale or you need an exact field name:
   - v1: search the [`v1.routes`](https://github.com/yugabyte/yugabyte-db/blob/master/managed/src/main/resources/v1.routes) file for the resource (e.g. `universes`, `providers`, `ybdb_release`, `customer_tasks`). Each line is `METHOD path controller.method(...)`.
   - v2: search [`openapi/paths/_index.yaml`](https://github.com/yugabyte/yugabyte-db/tree/master/managed/src/main/resources/openapi/paths) — entry point that includes per-resource path files. Schemas live under `openapi/components/schemas/`.

When asked to "use the YBA API to do X", grep the routes file for the resource name first rather than guessing.

## Common parameters (cheat sheet)

These four travel together on every request. Every helper in the Python wrapper and every PowerShell function takes them as a unit.

| Param | Notes |
|---|---|
| `base_url` | e.g. `https://yba.example.com` — no trailing `/api`. |
| `api_token` | Sent as `X-AUTH-YW-API-TOKEN`. |
| `customer_id` | Looked up once and cached per `base_url`. |
| `verify_certificate` | YBA installations on private CAs are common — be ready to set this to `False`. When false, also suppress urllib3 warnings. |

## Python: minimum-token usage

Keep request code tiny so the AI assistant spends tokens on the payload, not on plumbing. The full wrapper is in `references/python-client.md`; the irreducible minimum is:

```python
import requests, time, urllib3

def yba_request(base_url, endpoint, token, *, customer_id=None, method="GET",
                payload=None, params=None, verify=True, wait=False, wait_timeout=600):
    if not verify: urllib3.disable_warnings()
    h = {"Accept": "application/json", "Content-Type": "application/json",
         "X-AUTH-YW-API-TOKEN": token}
    url = f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"
    r = requests.request(method, url, headers=h, json=payload, params=params,
                         timeout=30, verify=verify)
    r.raise_for_status()
    data = r.json()
    if not wait or "taskUUID" not in data or not customer_id:
        return data
    task_url = f"{base_url.rstrip('/')}/api/v1/customers/{customer_id}/tasks/{data['taskUUID']}"
    start = time.time()
    while time.time() - start < wait_timeout:
        t = requests.get(task_url, headers=h, timeout=5, verify=verify).json()
        if t.get("percent") == 100 or t.get("status") in ("Aborted", "Failure"):
            return t
        time.sleep(2)
    return t
```

Usage pattern (splat the connection args once, then forget them):

```python
yba = dict(base_url="https://yba.example.com", token=TOKEN, verify=False)
customer_id = yba_request(**yba, endpoint="/api/v1/customers")[0]["uuid"]
yba["customer_id"] = customer_id

universes = yba_request(**yba, endpoint=f"/api/v1/customers/{customer_id}/universes")
```

Splatting lets the assistant write subsequent calls as one-liners — no repeated headers, no repeated URL building, no repeated cert handling.

> **Concrete recipes (provider create, release import, universe create, backup, task polling) are in [references/recipes.md](references/recipes.md). Read that file before generating non-trivial payloads — many endpoints have non-obvious required fields.**

## PowerShell

When the user is working in PowerShell, use the standalone wrapper in [`references/powershell-client.md`](references/powershell-client.md) — `Invoke-YbaRequest`, `Get-YbaApiToken`, `Register-YbaInstance`, `Get-YbaCustomerId`. It is a single self-contained `.ps1` block, requires only PowerShell 7+, and mirrors the Python wrapper's shape (splatted connection args, `taskUUID` polling via `-Wait`, customer-ID cache).

If the user already has the [`powershell-yba`](https://github.com/dmrnft/powershell-yba) module installed, prefer its richer per-resource cmdlets (`Get-YbaUniverse`, `New-YbaRelease`, `New-YbaKubernetesProvider`, …) over raw `Invoke-YbaRequest` — they handle the v1 payload shaping that is otherwise tedious. Both surfaces accept `-BaseUrl`, `-ApiToken` (SecureString), `-CustomerId`, `-SkipCertificateCheck` so splatting is interchangeable.

## Anti-patterns

| Anti-Pattern | Consequence | Do Instead |
|---|---|---|
| Hard-coding `customerUUID` from another environment | 403 / `Invalid Customer UUID` | Look it up once via `GET /api/v1/customers` and cache. |
| Treating the response of an async POST as the final result | Code thinks the universe exists immediately when in fact a task is still running and may fail | Detect `taskUUID` and poll `/customers/{cUUID}/tasks/{taskUUID}` until `percent == 100` or `status in (Aborted, Failure)`. |
| Calling `POST /api_login` on every request | Each login rotates the token, breaking concurrent scripts | Log in once, store the token, reuse it. |
| Sending a v1 universe payload to a v2 endpoint (or vice versa) | 400 with cryptic schema error | Check the source: v1 in `v1.routes`, v2 in `openapi/paths/`. The shapes are not interchangeable. |
| Echoing or logging `apiToken` | Token leak — token does not expire on its own | Treat as a password; redact in WhatIf / dry-run output. |
| Skipping cert verification globally without saying so | Hidden MITM exposure | Require an explicit `verify=False` / `-SkipCertificateCheck` and surface that in any logging. |
| Ignoring the `error` field in non-2xx JSON responses | Generic "request failed" with no detail | YBA returns `{"error": "..."}` on most errors — surface it in exception messages. |
| Building universe JSON from scratch for v1 | Missed required nested fields, edit fails silently or partially | Read the existing universe (`GET /universes/{id}`), mutate the relevant fields, POST back to the appropriate edit endpoint. Or use v2 if available. |
| Using `GET /universes` then string-matching on name | Loads every universe into memory | Use `GET /api/v1/customers/{cUUID}/universes/find?name=<name>` to resolve a name to a UUID. |
| Polling tasks faster than 1 Hz | Hammers YBA; some endpoints rate-limit | 1–2 s polling interval is the recommended default. |

## Quick workflow: register → token → first call

```bash
# 1. Register (only succeeds the first time)
curl -sk -X POST "$YBA_URL/api/v1/register?generateApiToken=1" \
  -H 'Content-Type: application/json' \
  -d '{"code":"dev","name":"Admin","email":"admin@example.com","password":"<pw>"}'
# →  {"apiToken":"...","customerUUID":"...","userUUID":"..."}

# 2. Or log in (existing instance — rotates the token)
curl -sk -X POST "$YBA_URL/api/v1/api_login" \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@example.com","password":"<pw>"}'

# 3. Use the token
curl -sk "$YBA_URL/api/v1/customers/$CUSTOMER_ID/universes" \
  -H "X-AUTH-YW-API-TOKEN: $YBA_TOKEN"
```

For anything more elaborate than this, switch to the Python wrapper.
