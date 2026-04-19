# YBA API recipes

Concrete request/response shapes for the most common workflows. Each recipe shows the endpoint, the minimal payload, the response shape, and any gotchas. Use the wrapper from [python-client.md](python-client.md) — variables `yba` and `cid` refer to the splatted connection dict and `customer_id` from there.

All paths assume `/api/v1/` unless explicitly v2.

## Look up an endpoint you don't already know

Don't guess. Two source-of-truth files cover everything:

- v1: <https://github.com/yugabyte/yugabyte-db/blob/master/managed/src/main/resources/v1.routes>
  - Each line is `METHOD path controller.method(arg: type, ...)`.
  - Search for the resource noun: `universes`, `providers`, `ybdb_release`, `customer_tasks`, `runtime_config`, `kms_configs`, `customer_configs`, `backups`, `dr_config`, `xcluster_configs`, `alert_configurations`, `support_bundle`, `ha_config`.
- v2: <https://github.com/yugabyte/yugabyte-db/tree/master/managed/src/main/resources/openapi>
  - Start at `paths/_index.yaml` (the master path index), then look at the per-resource files (`universe.yaml`, `release.yaml`, …).
  - Each path entry has `x-yba-api-visibility` (`preview`/`beta`/`stable`) and `x-yba-api-since` — check before relying on it.

When asked something like "How do I rotate KMS in YBA?" — `grep kms_configs` in `v1.routes` first.

## Register or log in

```python
from yba import yba_register, yba_login

# First time on a fresh YBA — creates customer + admin user, returns token
auth = yba_register(URL, code="dev", name="Admin", email=EMAIL, password=PW, verify=False)

# Subsequent logins — note this rotates the token
auth = yba_login(URL, EMAIL, PW, verify=False)

token = auth["apiToken"]
cid   = auth["customerUUID"]
```

Both responses look like:
```json
{"apiToken": "3.xxxx....", "apiTokenVersion": 2,
 "customerUUID": "0e71...", "userUUID": "4c53..."}
```

## Session / customer info

```python
yba_request(**yba, endpoint="/api/v1/session_info")
# {"customerUUID": "...", "userUUID": "...", "apiToken": null, ...}

yba_request(**yba, endpoint="/api/v1/customers")
# [{"uuid": "...", "code": "...", "name": "..."}]

yba_request(**yba, endpoint=f"/api/v1/customers/{cid}/host_info")
# Cloud / network info about where YBA itself runs
```

## Versions and YBDB releases

```python
# Version of YBA itself
yba_request(**yba, endpoint="/api/v1/app_version")  # {"version": "2025.2.x.x-bN"}

# List of YBDB releases YBA can deploy
yba_request(**yba, endpoint=f"/api/v1/customers/{cid}/ybdb_release")
```

Adding a new release is multi-step (the API does not download the package synchronously):

1. **`POST /customers/{cid}/ybdb_release/extract_metadata`** with `{"url": "<package URL>"}` → returns `{"resourceUUID": "..."}` (a metadata-extraction task).
2. **Poll** `GET /customers/{cid}/ybdb_release/extract_metadata/{resourceUUID}` until `status == "success"`. Response includes `version`, `release_type`, `platform`, `architecture`.
3. **`POST /customers/{cid}/ybdb_release`** with the extracted metadata to register the release. If a release already exists with that version, add a new artifact (architecture) instead of failing.

Package URL patterns:

| Platform | Architecture | URL pattern |
|---|---|---|
| Linux | x86_64 | `https://software.yugabyte.com/releases/<version>/yugabyte-<version>-<build>-linux-x86_64.tar.gz` |
| Linux | aarch64 | `https://software.yugabyte.com/releases/<version>/yugabyte-<version>-<build>-el8-aarch64.tar.gz` |
| Kubernetes | (none) | `https://s3.us-west-2.amazonaws.com/releases.yugabyte.com/<version>-<build>/helm/yugabyte-<short-version>.tgz` |

Note: Linux artifacts must be registered before the matching Kubernetes Helm chart for the same release.

## Providers

| Provider type | Create endpoint |
|---|---|
| AWS / GCP / Azure VM / on-prem | `POST /customers/{cid}/providers` |
| Kubernetes | `POST /customers/{cid}/providers` (with `code: "kubernetes"`) |
| Suggested Kubernetes config (auto-detect) | `GET /customers/{cid}/providers/suggested_kubernetes_config` |

```python
providers = yba_request(**yba, endpoint=f"/api/v1/customers/{cid}/providers")
# Each provider has uuid, code, name, regions[].zones[]

instance_types = yba_request(
    **yba,
    endpoint=f"/api/v1/customers/{cid}/providers/{providers[0]['uuid']}/instance_types",
)
```

Provider creation returns `{"taskUUID": "...", "resourceUUID": "<provider-uuid>"}` — pass `wait=True` to block until provisioning finishes.

The provider payload is large and tightly coupled to provider-specific details (subnets, security groups, image bundles, access keys, namespaces, kubeconfigs). The simplest way to learn the shape is to:
1. Create one through the YBA UI.
2. `GET /providers/{uuid}` to see the resulting structure.
3. Use that as a template for further providers.

## Universe lifecycle

| Action | v1 endpoint | v2 endpoint |
|---|---|---|
| List | `GET /customers/{cid}/universes` | `GET /customers/{cid}/universes` |
| Get by UUID | `GET /customers/{cid}/universes/{uuid}` | `GET /customers/{cid}/universes/{uuid}` |
| Find UUID by name | `GET /customers/{cid}/universes/find?name=<n>` | — (filter v2 list) |
| Create | `POST /customers/{cid}/universes/clusters` (call `/universe_configure` first to fill in placement) | `POST /customers/{cid}/universes` (single call, simpler payload) |
| Edit | `PUT /customers/{cid}/universes/{uuid}/clusters` | `POST /customers/{cid}/universes/{uuid}/edit` |
| Software upgrade | `POST /customers/{cid}/universes/{uuid}/upgrade/software` | `POST /customers/{cid}/universes/{uuid}/upgrade/software` |
| GFlag upgrade | `POST /customers/{cid}/universes/{uuid}/upgrade/gflags` | `POST /customers/{cid}/universes/{uuid}/upgrade/gflags` |
| Pause | `POST /customers/{cid}/universes/{uuid}/pause` | `POST /customers/{cid}/universes/{uuid}/pause` |
| Resume | `POST /customers/{cid}/universes/{uuid}/resume` | `POST /customers/{cid}/universes/{uuid}/resume` |
| Delete | `DELETE /customers/{cid}/universes/{uuid}` | `DELETE /customers/{cid}/universes/{uuid}` |

When creating universes the YSQL and YCQL passwords must be complex (lower case, upper case, number, special character) and longer than 10 characters.

### v1 cloud universe (AWS / GCP / Azure / on-prem)

Two calls: configure (fills in placement, prices, masters) then create.

```python
# Pick a provider + region/zone to deploy into
providers = yba_request(**yba, endpoint=f"/api/v1/customers/{cid}/providers")
prov = providers[0]
region = prov["regions"][0]
zones  = region["zones"]

# Minimal cluster spec — the configure call fills in everything else
spec = {
    "clusters": [{
        "clusterType": "PRIMARY",
        "userIntent": {
            "universeName": "cloud-uni",
            "provider": prov["uuid"],
            "providerType": prov["code"],            # "aws" | "gcp" | "azu" | "onprem"
            "regionList": [region["uuid"]],
            "numNodes": 3,
            "replicationFactor": 3,
            "instanceType": "c5.large",              # provider-specific; query /instance_types
            "deviceInfo": {"numVolumes": 1, "volumeSize": 100, "storageType": "GP3"},
            "ybSoftwareVersion": "2024.2.3.2-b6",
            "accessKeyCode": prov.get("allAccessKeys", [{}])[0].get("keyInfo", {}).get("keyCode"),
            "assignPublicIP": True,
            "useTimeSync": True,
            "enableYSQL": True, "enableYSQLAuth": True, "ysqlPassword": "Password123!",
            "enableYCQL": False,
            "enableNodeToNodeEncrypt": True,
            "enableClientToNodeEncrypt": True,
        },
    }],
    "currentClusterType": "PRIMARY",
    "clusterOperation": "CREATE",
}

configured = yba_request(**yba, method="POST",
    endpoint=f"/api/v1/customers/{cid}/universe_configure", payload=spec)

task = yba_request(**yba, method="POST",
    endpoint=f"/api/v1/customers/{cid}/universes/clusters",
    payload=configured, wait=True)
```

### v1 Kubernetes universe

Same shape, different `userIntent` knobs (no `instanceType`; CPU/memory expressed via overrides; `providerType: "kubernetes"`):

```python
spec = {
    "clusters": [{
        "clusterType": "PRIMARY",
        "userIntent": {
            "universeName": "k8s-uni",
            "provider": prov["uuid"],
            "providerType": "kubernetes",
            "regionList": [region["uuid"]],
            "numNodes": 3,
            "replicationFactor": 3,
            "deviceInfo": {"numVolumes": 1, "volumeSize": 100, "storageClass": "standard"},
            "masterDeviceInfo": {"numVolumes": 1, "volumeSize": 50, "storageClass": "standard"},
            "ybSoftwareVersion": "2024.2.3.2-b6",
            "enableYSQL": True, "enableYSQLAuth": True, "ysqlPassword": "Password123!",
            "enableNodeToNodeEncrypt": True,
            "enableClientToNodeEncrypt": True,
            "universeOverrides": (
                "resource:\n"
                "  master:\n    requests: {cpu: 1, memory: 2Gi}\n"
                "  tserver:\n    requests: {cpu: 2, memory: 8Gi}\n"
            ),
        },
    }],
    "currentClusterType": "PRIMARY",
    "clusterOperation": "CREATE",
}
configured = yba_request(**yba, method="POST",
    endpoint=f"/api/v1/customers/{cid}/universe_configure", payload=spec)
task = yba_request(**yba, method="POST",
    endpoint=f"/api/v1/customers/{cid}/universes/clusters",
    payload=configured, wait=True)
```

### v2 cloud universe (AWS / GCP / Azure / on-prem)

A single POST. Uses provider/region/zone UUIDs in `placement_spec`, plus an `instance_type` for cloud.

```python
v2_payload = {
  "spec": {
    "name": "cloud-uni",
    "yb_software_version": "2025.2.0.0-b131",
    "encryption_in_transit_spec": {"enable_node_to_node_encrypt": True,
                                   "enable_client_to_node_encrypt": True},
    "ysql": {"enable": True, "enable_auth": True, "password": "Password123!"},
    "ycql": {"enable": False},
    "use_time_sync": True,
    "networking_spec": {"assign_public_ip": True, "enable_ipv6": False},
    "clusters": [{
      "cluster_type": "PRIMARY",
      "num_nodes": 3,
      "replication_factor": 3,
      "node_spec": {
        "instance_type": "c5.large",
        "storage_spec": {"volume_size": 100, "num_volumes": 1, "storage_type": "GP3"},
        "dedicated_nodes": False,
      },
      "networking_spec": {"enable_exposing_service": "EXPOSED", "enable_lb": False},
      "provider_spec": {"provider": prov["uuid"], "region_list": [region["uuid"]]},
      "placement_spec": {"cloud_list": [{
        "uuid": prov["uuid"], "code": prov["code"],
        "masters_in_default_region": True,
        "region_list": [{
          "uuid": region["uuid"], "code": region["code"], "name": region["name"],
          "az_list": [{
            "uuid": zones[0]["uuid"], "name": zones[0]["name"],
            "replication_factor": 3, "num_nodes_in_az": 3,
            "leader_affinity": True,
          }],
        }],
      }]},
    }],
  },
  "arch": "x86_64",
}

task = yba_request(**yba, method="POST",
    endpoint=f"/api/v2/customers/{cid}/universes",
    payload=v2_payload, wait=True)
```

### v2 Kubernetes universe

Same shape, but `node_spec` uses `k8s_master_resource_spec` / `k8s_tserver_resource_spec` and `storage_spec` uses `storage_class` instead of `storage_type` / `instance_type`.

```python
v2_payload = {
  "spec": {
    "name": "k8s-uni",
    "yb_software_version": "2025.2.0.0-b131",
    "encryption_in_transit_spec": {"enable_node_to_node_encrypt": True,
                                   "enable_client_to_node_encrypt": True},
    "ysql": {"enable": True, "enable_auth": True, "password": "Password123!"},
    "ycql": {"enable": False},
    "use_time_sync": True,
    "networking_spec": {"assign_public_ip": True},
    "clusters": [{
      "cluster_type": "PRIMARY",
      "num_nodes": 3,
      "replication_factor": 3,
      "node_spec": {
        "storage_spec": {"volume_size": 100, "num_volumes": 1, "storage_class": "standard"},
        "tserver": {"storage_spec": {"volume_size": 100, "num_volumes": 1}},
        "master":  {"storage_spec": {"volume_size":  50, "num_volumes": 1}},
        "k8s_master_resource_spec":  {"cpu_core_count": 1, "memory_gib": 2},
        "k8s_tserver_resource_spec": {"cpu_core_count": 2, "memory_gib": 8},
        "dedicated_nodes": False,
      },
      "networking_spec": {"enable_exposing_service": "UNEXPOSED", "enable_lb": False},
      "provider_spec": {"provider": prov["uuid"], "region_list": [region["uuid"]]},
      "placement_spec": {"cloud_list": [{
        "uuid": prov["uuid"], "code": "kubernetes",
        "masters_in_default_region": True,
        "region_list": [{
          "uuid": region["uuid"], "code": region["code"], "name": region["name"],
          "az_list": [{
            "uuid": zones[0]["uuid"], "name": zones[0]["name"],
            "replication_factor": 3, "num_nodes_in_az": 3,
            "leader_affinity": True,
          }],
        }],
      }]},
    }],
  },
  "arch": "x86_64",
}

task = yba_request(**yba, method="POST",
    endpoint=f"/api/v2/customers/{cid}/universes",
    payload=v2_payload, wait=True)
```

For multi-AZ or multi-region, append more entries to `az_list` (and to the cluster's `region_list` / `placement_spec.cloud_list[].region_list`), summing the per-AZ `num_nodes_in_az` to the cluster's `num_nodes`.

**Editing a universe (v1)**: always `GET` first, mutate, then send back. Building the payload from scratch is error-prone because YBA fills in dozens of derived fields on creation that the edit endpoint expects to see again. v2 edit endpoints accept the trimmed-down spec shape used for create.

## Storage configs (for backups)

`POST /api/v1/customers/{cid}/configs` creates a storage config; `GET` lists them. The `data.*` keys vary per `name` (`S3`, `GCS`, `AZ`, `NFS`).

### AWS S3 — single bucket, IAM auth

```python
yba_request(**yba, method="POST",
    endpoint=f"/api/v1/customers/{cid}/configs",
    payload={
        "configName": "backup-s3-prod",
        "type": "STORAGE",
        "name": "S3",
        "data": {
            "BACKUP_LOCATION": "s3://my-bucket/yba-backups",
            "AWS_HOST_BASE": "s3.amazonaws.com",
            "IAM_INSTANCE_PROFILE": "true",
        },
    })
```

### AWS S3 — multi-region, with optional HTTPS proxy

When the YBA host needs to reach S3 through an outbound proxy (corporate egress, isolated VPC), include a `PROXY_SETTINGS` block. `REGION_LOCATIONS` lets a single config back universes that span regions, picking the closest bucket per region.

```python
payload = {
    "configName": "multi-region-backup",
    "customerUUID": cid,
    "type": "STORAGE",
    "name": "S3",
    "data": {
        "IAM_INSTANCE_PROFILE": "true",
        # Default location used when no region-specific override matches
        "BACKUP_LOCATION": "s3://uswest1-bucket/",
        "REGION_LOCATIONS": [
            {"REGION": "us-west-1", "LOCATION": "s3://uswest1-bucket/",
             "AWS_HOST_BASE": "s3.us-west-1.amazonaws.com"},
            {"REGION": "us-west-2", "LOCATION": "s3://uswest2-bucket/",
             "AWS_HOST_BASE": "s3.us-west-2.amazonaws.com"},
        ],
        # Optional — omit the whole PROXY_SETTINGS block if no proxy is needed.
        # Set PROXY_USERNAME / PROXY_PASSWORD to "" for unauthenticated proxies.
        "PROXY_SETTINGS": {
            "PROXY_HOST": "proxy.example.com",
            "PROXY_PORT": "3128",
            "PROXY_USERNAME": "",
            "PROXY_PASSWORD": "",
        },
    },
    "state": "Active",
}

yba_request(**yba, method="POST",
    endpoint=f"/api/v1/customers/{cid}/configs", payload=payload)
```

For access-key auth instead of IAM, replace `IAM_INSTANCE_PROFILE` with `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` inside `data`.

GCS / Azure / NFS variants use different `data.*` keys (`GCS_CREDENTIALS_JSON`, `AZURE_STORAGE_SAS_TOKEN`, etc.) — search `v1.routes` for `customer_configs` and check the API docs portal.

## Telemetry providers (log/metric export)

YBA can stream universe logs and metrics to an external sink (S3, GCS, Loki, Datadog, Splunk, AWS CloudWatch, …) via OpenTelemetry. The endpoint is `/api/v1/customers/{cid}/telemetry_provider`; the `config.type` field selects the sink.

### Create an S3 log-export provider

```python
yba_request(**yba, method="POST",
    endpoint=f"/api/v1/customers/{cid}/telemetry_provider",
    payload={
        "name": "logs-to-s3",
        "config": {
            "type": "S3",
            "region": "eu-west-1",
            "bucket": "yba-log-archive",
            "accessKey": "AKIA...",
            "secretKey": "...",
        },
        "tags": {"env": "prod"},
    })
```

`config.type` values include `S3`, `GCS`, `AWS_CLOUDWATCH`, `LOKI`, `DATA_DOG`, `SPLUNK` — each takes a different set of sibling fields. List existing providers with `GET /telemetry_provider`; delete with `DELETE /telemetry_provider/{intUUID}`. Once created, attach the provider to a universe via the universe's audit-log / query-log configuration endpoints (search `v1.routes` for `audit_log_config` / `query_log_config`).

## Universe health checks and alerts

Use this sequence to get a fresh view of a universe's health. Unless otherwise stated, **always use this process to get universe health** by invoking new health checks.

```python
uni = uuid_of_my_universe  # from /universes or /universes/find?name=...

# 1. Ensure that on-demand health check triggering is enabled - GET then PUT if not already `true`

Set universe-scoped runtime flag `yb.health.trigger_api.enabled` to `true`

# 2. Trigger an on-demand health check with GET request (queues a check; does not wait)
yba_request(**yba,
    endpoint=f"/api/v1/customers/{cid}/universes/{uni}/trigger_health_check")
# → {"timestamp": "..."}  (the timestamp the check was queued at)

# 2. Wait briefly for the check to run and report (typically 10-60 s)
import time; time.sleep(30)

# 3. Read the latest health-check results for this universe
checks = yba_request(**yba,
    endpoint=f"/api/v1/customers/{cid}/universes/{uni}/health_check")
# Returns a list of report objects (most recent last). Each report's
# `data` field holds per-node check results: ssh reachability, disk
# utilisation, clock skew, cert expiry, gflag drift, etc.
latest = checks[-1] if checks else None

# 4. Read all currently-active alerts for the customer, then filter to this universe
active_alerts = yba_request(**yba, endpoint=f"/api/v1/customers/{cid}/alerts/active")
universe_alerts = [a for a in active_alerts
                   if a.get("sourceUUID") == uni or a.get("labels", {}).get("source_uuid") == uni]

# 5. Read recent tasks for the universe for any failures
tasks = yba_request(**yba, endpoint=f"/api/v1/customers/{cid}/tasks")
universe_tasks = tasks.get(uni, [])
```

# Tasks — the API returns a dict keyed by universeUUID

Useful adjacent endpoints:

| Purpose | Endpoint |
|---|---|
| All alerts (history) | `GET /customers/{cid}/alerts` |
| Active alerts only | `GET /customers/{cid}/alerts/active` |
| One alert | `GET /customers/{cid}/alerts/{alertUUID}` |
| Page / filter alerts | `POST /customers/{cid}/alerts/page` (body: `{filter, sortBy, direction, offset, limit}`) |
| Acknowledge | `POST /customers/{cid}/alerts/{alertUUID}/acknowledge` |
| Configure per-universe alert thresholds | `POST /customers/{cid}/universes/{uni}/config_alerts` |

If `health_check` returns an empty list, the universe is too new to have any reports — wait longer after the trigger or check `GET /universes/{uni}` for `universeDetails.updateInProgress`.

## Universe metrics (Prometheus on port 9090)

YBA bundles Prometheus on `http://<yba-host>:9090` and scrapes every node, master, tserver and (on Kubernetes) container in every universe. Useful queries (YCQL/YSQL ops/sec and latency, container CPU/memory, node-exporter CPU, tablet-leader balance, xCluster lag) plus Python and PowerShell helpers and the recipe for deriving `node_prefix` and pod-name regexes from a universe API response are in [`prometheus.md`](prometheus.md). Quick example:

```python
uni = yba_request(**yba, endpoint=f"/api/v1/customers/{cid}/universes/{uni_uuid}")
prefix = uni["universeDetails"]["nodePrefix"]
query = (f'sum(avg_over_time(rpc_irate_rps{{service_type="SQLProcessor",'
         f'node_prefix="{prefix}",server_type="yb_cqlserver",'
         f'service_method=~"SelectStmt|InsertStmt|UpdateStmt|DeleteStmt|OtherStmts|Transaction"}}'
         f'[30s])) by (service_method)')
requests.get("http://yba.internal:9090/api/v1/query", params={"query": query}).json()
```

## Runtime configuration

YBA exposes a hierarchical key-value configuration store (Typesafe Config). Settings can be scoped to **global**, a **customer**, a **universe**, or a **provider**, and a request resolves the most specific scope that has a value (with optional inheritance).

| Scope | UUID to use as `:scope` |
|---|---|
| Global | `00000000-0000-0000-0000-000000000000` |
| Customer | the customer UUID |
| Universe | the universe UUID |
| Provider | the provider UUID |

All values are sent and returned as **plain text** in the request/response body. Use `Content-Type: text/plain` (not JSON) on `PUT`. Booleans are `true` / `false`, numbers are bare numerals, durations are quoted strings (`"30 seconds"`), lists are JSON-encoded strings.

### Discover available keys

```python
# All mutable runtime keys (just the names)
keys = yba_request(**yba, endpoint="/api/v1/runtime_config/mutable_keys")

# Same list with metadata: scope, type, default, description
key_info = yba_request(**yba, endpoint="/api/v1/runtime_config/mutable_key_info")

# Feature flags only
yba_request(**yba, endpoint="/api/v1/runtime_config/feature_flags")
```

`mutable_key_info` is the most useful for the assistant — it tells you the data type and which scopes (`GLOBAL`, `CUSTOMER`, `UNIVERSE`, `PROVIDER`) accept the key, so you can pick the right `:scope` UUID.

### List configurable scopes for a customer

```python
yba_request(**yba, endpoint=f"/api/v1/customers/{cid}/runtime_config/scopes")
# → {"globalScope": "...", "customerScope": "...",
#    "universeScopes": [...], "providerScopes": [...]}
```

### Read a single key (any scope)

The wrapper expects JSON responses, but the runtime-config GET returns either JSON-wrapped or `text/plain` depending on key type — easiest to use `requests` directly when the value is plain text:

```python
import requests
def rc_get(scope, key):
    r = requests.get(
        f"{yba['base_url']}/api/v1/customers/{cid}/runtime_config/{scope}/key/{key}",
        headers={"X-AUTH-YW-API-TOKEN": yba['token'], "Accept": "application/json"},
        verify=yba['verify'])
    r.raise_for_status()
    # Body is the raw value. Try JSON first, fall back to text.
    try: return r.json()
    except ValueError: return r.text

GLOBAL = "00000000-0000-0000-0000-000000000000"
rc_get(GLOBAL,        "yb.node_agent.preflight_checks.max_python_version")  # "3.11"
rc_get(universe_uuid, "yb.checks.nodes_safe_to_take_down.enabled")          # true
```

`curl` equivalent:

```bash
curl -sk -H "X-AUTH-YW-API-TOKEN: $YBA_TOKEN" \
     "$YBA_URL/api/v1/customers/$CUSTOMER_ID/runtime_config/$UNIVERSE_UUID/key/yb.checks.nodes_safe_to_take_down.enabled"
```

To see **every** key set at a scope (optionally including inherited values from broader scopes):

```bash
curl -sk -H "X-AUTH-YW-API-TOKEN: $YBA_TOKEN" \
     "$YBA_URL/api/v1/customers/$CUSTOMER_ID/runtime_config/$UNIVERSE_UUID?includeInherited=true"
```

### Set a key (PUT)

The body is the raw value as text — do not JSON-wrap it.

```python
def rc_set(scope, key, value):
    if isinstance(value, bool): value = "true" if value else "false"
    r = requests.put(
        f"{yba['base_url']}/api/v1/customers/{cid}/runtime_config/{scope}/key/{key}",
        headers={"X-AUTH-YW-API-TOKEN": yba['token'],
                 "Content-Type": "text/plain", "Accept": "application/json"},
        data=str(value), verify=yba['verify'])
    r.raise_for_status()

# Global scope
rc_set(GLOBAL, "yb.node_agent.preflight_checks.max_python_version", "3.11")

# Per-universe scope
rc_set(universe_uuid, "yb.checks.nodes_safe_to_take_down.enabled", True)

# Per-provider scope
rc_set(provider_uuid, "yb.internal.allow_unsupported_instances", False)
```

`curl` equivalents (note the `text/plain` content type and the raw value in `--data`):

```bash
# Global
curl -sk -X PUT \
  -H "X-AUTH-YW-API-TOKEN: $YBA_TOKEN" \
  -H 'Content-Type: text/plain' \
  --data '3.11' \
  "$YBA_URL/api/v1/customers/$CUSTOMER_ID/runtime_config/00000000-0000-0000-0000-000000000000/key/yb.node_agent.preflight_checks.max_python_version"

# Per-universe
curl -sk -X PUT \
  -H "X-AUTH-YW-API-TOKEN: $YBA_TOKEN" \
  -H 'Content-Type: text/plain' \
  --data 'true' \
  "$YBA_URL/api/v1/customers/$CUSTOMER_ID/runtime_config/$UNIVERSE_UUID/key/yb.checks.nodes_safe_to_take_down.enabled"
```

### Delete (revert to inherited / default)

`DELETE /customers/{cid}/runtime_config/{scope}/key/{key}` removes the override at that scope; the next read resolves to the next-broader scope or the built-in default.

### Common pitfalls

- **Wrong `Content-Type`.** Sending `application/json` on `PUT` causes the server to reject the body as malformed — always `text/plain`.
- **Setting a key at a scope that doesn't accept it.** Many keys are global-only (e.g. log levels) or universe-only (e.g. `yb.checks.*`). Check `mutable_key_info` first.
- **Forgetting that the scope is a UUID, not a name.** The customer scope UUID equals the customer UUID; the universe scope UUID equals the universe UUID; same for provider. The all-zeros UUID is the only special one.

## Backups and restores

```python
# Take a backup
task = yba_request(**yba, method="POST",
    endpoint=f"/api/v1/customers/{cid}/tables/backup",
    payload={
        "universeUUID": universe_uuid,
        "keyspace": "yugabyte",
        "backupType": "PGSQL_TABLE_TYPE",   # or YQL_TABLE_TYPE for YCQL
        "storageConfigUUID": storage_uuid,
        "timeBeforeDelete": 0,              # 0 = no auto-expiry
    },
    wait=True)

# Schedule recurring backups
yba_request(**yba, method="POST",
    endpoint=f"/api/v1/customers/{cid}/create_backup_schedule",
    payload={...})

# List backups
yba_request(**yba, endpoint=f"/api/v1/customers/{cid}/backups")

# Restore
task = yba_request(**yba, method="POST",
    endpoint=f"/api/v1/customers/{cid}/universes/{target_uuid}/backups/restore",
    payload={
        "actionType": "RESTORE",
        "storageConfigUUID": storage_uuid,
        "backupStorageInfoList": [...],
    },
    wait=True)
```

## Async tasks — polling, listing, aborting

```python
# Get a single task's status
yba_request(**yba, endpoint=f"/api/v1/customers/{cid}/tasks/{task_uuid}")

# Recent tasks (handy when the original taskUUID is lost)
yba_request(**yba, endpoint=f"/api/v1/customers/{cid}/tasks")

# Abort a running task (only works for abortable tasks)
yba_request(**yba, method="POST",
    endpoint=f"/api/v1/customers/{cid}/tasks/{task_uuid}/abort")
```

Task object key fields:

| Field | Meaning |
|---|---|
| `status` | `Created`, `Running`, `Success`, `Failure`, `Aborted` |
| `percent` | 0–100 |
| `target` | Resource name (e.g. universe name) |
| `targetUUID` | Resource UUID — for create tasks, this is the new resource's UUID |
| `details.taskDetails[]` | Per-subtask status — read this on failure to see which step failed |
| `abortable` / `retryable` | Booleans |

## Common error responses

| HTTP | YBA `error` | Meaning |
|---|---|---|
| 400 | `cannot register multiple accounts` | `/register` already used — log in instead |
| 400 | `Cannot find universe …` | Wrong UUID or wrong customer |
| 401 | `Unable To Authenticate User` | Bad / rotated token |
| 403 | `Invalid Customer UUID` | Token belongs to a different customer |
| 404 | (HTML) | Wrong path (typo) — confirm against `v1.routes` or v2 OpenAPI |
| 409 | various | Conflicting in-flight task on the same resource |
| 500 | `Connection reset` | YBA restarting / unreachable |

YBA's error body is `{"error": "..."}` for most errors but occasionally just an HTML error page (typically 404 or proxy errors). Surface the body verbatim in your exception message — it's the fastest route to the cause.

## Dry-run / "show me what this will send"

There is no built-in dry-run for state-changing endpoints. Two practical approaches:

1. Print the URL, method, and `json.dumps(payload, indent=2)` before calling the wrapper. The PowerShell module's `-WhatIf` switch does exactly this.
2. For payload validation only (without committing), some endpoints have a sibling validation endpoint, e.g. `POST /universe_configure` validates a universe spec without creating anything.
