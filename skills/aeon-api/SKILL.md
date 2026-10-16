---
name: aeon-api
description: Use when scripting, automating, or querying YugabyteDB Aeon (YugabyteDB Managed cloud) via its REST API — including listing, scaling, pausing and resuming clusters, configuring network allow lists, reading cluster metrics, managing backups and restores, and working with VPC peering or private endpoints. Triggers on YugabyteDB Aeon API, YugabyteDB Managed API, cloud.yugabyte.com/api, Aeon REST, automate Aeon, scale Aeon cluster programmatically, Aeon metrics via API, Aeon backup via API, Aeon allowlist API. Does NOT cover the YBA (YugabyteDB Anywhere) self-managed API — use `yba-api` for that. Does NOT cover performance diagnosis (SQL or metrics layer) — use `yb-perf` for that.
---

# YugabyteDB Aeon REST API

The Aeon REST API manages YugabyteDB Aeon (managed cloud) clusters programmatically. It is a
distinct API from YBA (YugabyteDB Anywhere): different base URL, auth and resource model.

**Base URL:** `https://cloud.yugabyte.com/api/public/v1`. The `/public/` segment is required:
`/api/v1/...` is not the public API (it answers 404 without a key, and an XML 401 with one).

**API reference:** https://api-docs.yugabyte.com/docs/managed-apis/9u5yqnccbe8lk-yugabyte-db-managed-rest-api
(overview: https://docs.yugabyte.com/stable/yugabyte-cloud/managed-automation/managed-api/).
Check it for endpoints not covered here; do not guess paths.

## Authentication

Every request sends an **API key** as a Bearer token:

```
Authorization: Bearer <api-key>
```

Create one in the Aeon console: **Security → Access Control → API Keys** (it is shown once). A
key can be given an expiry and a role, and can be restricted to IP allow lists. Read it from an
environment variable or secret store; never hard-code it or print it.

## Resource hierarchy

```
Account (accountId)            GET /account   (the account the key belongs to)
  └── Project (projectId)      GET /accounts/{accountId}/projects
        ├── Clusters           .../projects/{projectId}/clusters/{clusterId}
        │     ├── nodes, metrics, backup-schedules, allow-lists (assigned IDs), pause, resume
        ├── Allow lists        .../projects/{projectId}/allow-lists        (project-level)
        ├── Backups, restore   .../projects/{projectId}/backups, /restore  (filter by cluster_id)
        └── VPCs, peerings     .../projects/{projectId}/vpcs, /vpc-peerings
```

Path segments use **hyphens** (`allow-lists`, `backup-schedules`), never underscores. JSON
bodies use snake_case fields. Discovery of IDs, pagination and errors:
[`references/auth-and-discovery.md`](references/auth-and-discovery.md).

## Rules

- **Discover, don't hard-code IDs.** Resolve account, project and cluster IDs by name from the
  API (`?name=` filters exist on list calls).
- **Edits replace the whole spec.** `PUT .../clusters/{clusterId}` takes a complete
  `ClusterSpec` (`name`, `cluster_info`, `software_info`, `cluster_region_info`, ...). GET the
  cluster, change the field, PUT the whole `spec` back. A partial body, or one wrapped in
  `{"spec": ...}`, is rejected with `400` naming the missing fields.
- **Writes are asynchronous.** Edit, pause, resume, delete, restore and assigning allow lists
  return `202`. Poll `GET .../clusters/{clusterId}` until `info.state` is `ACTIVE` (or
  `PAUSED`), or follow the task with
  `GET /accounts/{accountId}/tasks?entity_id={clusterId}&task_type=...` until `SUCCEEDED`. Do not
  start another change while one is running: the API answers 400 or 500.
- **Error bodies are not always JSON.** A wrong path answers an HTML 404; `/api/v1/...` (the
  old, non-public base) answers an XML 401 that looks like a bad key.
- **Confirm before destructive or billable changes** (scale, pause, delete, restore, removing
  an allow list from a cluster) and show the planned request first.

## References

- [`references/auth-and-discovery.md`](references/auth-and-discovery.md): key handling, curl /
  Python / PowerShell patterns, ID discovery, pagination, errors, task polling.
- [`references/recipes.md`](references/recipes.md): cluster details and scaling, pause and
  resume, nodes, allow lists, backups and restore, metrics.

## What the API covers and what it doesn't

**Covered:**
- Cluster lifecycle: create, edit (scale nodes, vCPUs, disk), pause, resume, delete
- Network allow lists (project-level, assigned to clusters by ID)
- Backups (on-demand, schedules, list) and restore
- VPCs, VPC peering and private service endpoints
- Cluster metrics (a curated set; see `recipes.md`) and metrics export configuration
- Alert rules and notifications, account users and roles, tasks

**Not covered: alternatives**
- **Database users and roles** (YSQL/YCQL): not an API resource. Connect to the cluster and use
  SQL (`CREATE ROLE ... LOGIN PASSWORD ...`); the admin credentials are set at cluster creation.
- **GFlags / server flags**: not exposed by the API. Raise a support ticket.
- **Full Prometheus metric surface**: configure metrics export (Datadog, Grafana Cloud,
  Prometheus and others) in the console or via `metrics-exporter-configs`.
- **Performance diagnosis** (SQL or metrics layer): connect via YSQL and use the `yb-perf` skill.
- **Performance Advisor / Insights**: console only.
