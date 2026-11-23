---
name: aeon-api
description: Use when scripting, automating, or querying YugabyteDB Aeon (YugabyteDB Managed cloud) via its REST API — including listing, scaling, pausing and resuming clusters, configuring network allow lists, reading cluster metrics, managing backups and restores, working with VPC peering or private endpoints, and creating database users on an Aeon cluster (with SQL, as the API has no endpoint for them). Triggers on YugabyteDB Aeon API, YugabyteDB Managed API, cloud.yugabyte.com/api, Aeon REST, automate Aeon, scale Aeon cluster programmatically, Aeon metrics via API, Aeon backup via API, Aeon allowlist API, Aeon database user, Aeon read-only user. Does NOT cover the YBA (YugabyteDB Anywhere) self-managed API — use `yba-api` for that. Does NOT cover performance diagnosis (SQL or metrics layer) — use `yb-perf` for that.
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

## Endpoints

Every path is under `https://cloud.yugabyte.com/api/public/v1`; `P` is
`/accounts/{accountId}/projects/{projectId}`. Use only these, or ones the API reference lists.

| Task | Call | Answer |
|---|---|---|
| Account ID | `GET /account` | `data.info.id` |
| Projects | `GET /accounts/{accountId}/projects` | `data[].id`, `data[].name` |
| Find a cluster | `GET P/clusters?name=<name>` | `data[].info.id`, `.info.state`, `.spec` |
| Cluster details | `GET P/clusters/{clusterId}` | `data.spec`, `data.info` |
| Scale / edit | `PUT P/clusters/{clusterId}` with the whole `spec` | `202` |
| Pause / resume | `POST P/clusters/{clusterId}/pause` (`/resume`), no body | `202` |
| Nodes | `GET P/clusters/{clusterId}/nodes` | `data[].name`, `.is_node_up` |
| Metrics | `GET P/clusters/{clusterId}/metrics?metrics=CPU_USAGE&start_time=<epoch s>&end_time=<epoch s>` | `data[].values` |
| Allow lists (project) | `GET` / `POST P/allow-lists`, `DELETE P/allow-lists/{id}` | `data[].info.id`, `.spec.allow_list`. DELETE of a list still assigned to a cluster answers `200` and deletes nothing: unassign it first, then confirm with a GET |
| Cluster's allow lists | `GET` / `PUT P/clusters/{clusterId}/allow-lists`, body `["<id>", ...]` | PUT `202`, replaces the set |
| Backups | `GET P/backups?cluster_id=`, `POST P/backups` `{"cluster_id": ...}`, `GET P/backups/{id}` | POST `202` |
| Backup schedules | `GET` / `POST P/clusters/{clusterId}/backup-schedules` | |
| Restore | `POST P/restore` `{"backup_id": ..., "cluster_id": ...}`, `GET P/restore/{id}` | POST `202`, `data.info.id` |
| Tasks | `GET /accounts/{accountId}/tasks?entity_id={clusterId}&task_type=EDIT_ALLOW_LIST` | newest first, `data[].info.state` |
| Cluster CA certificate | `GET /connection-certificate` (no key needed) | `data`: PEM |

There is no endpoint for database users, gflags, or per-node operations other than the above.

## Rules

- **Discover, don't hard-code IDs.** Resolve account, project and cluster IDs by name from the
  API (`?name=` filters exist on list calls).
- **Edits replace the whole spec.** `PUT .../clusters/{clusterId}` takes a complete
  `ClusterSpec` (`name`, `cluster_info`, `software_info`, `cluster_region_info`, ...). GET the
  cluster, change it, PUT the whole `spec` back. A partial body, or one wrapped in
  `{"spec": ...}`, is rejected with `400` naming the missing fields. The node count is in two
  places: change `cluster_info.num_nodes` and each region's `placement_info.num_nodes` together
  (for several regions, the regions must add up to `cluster_info.num_nodes`).
- **Writes are asynchronous; know what "done" means.** Edit, pause, resume, delete, restore and
  assigning allow lists return `202`. Do not start another change until the last one is done:
  the API answers 400 or 500.
  - Edit (scale): done when `info.state` is `ACTIVE` **and** the spec shows the target. The
    state reads `QUEUED`, `UPDATING`, `UPDATING_LB` first, and the spec changes only near the end.
  - Pause / resume: done when `info.state` is `PAUSED` / `ACTIVE`.
  - Allow-list assignment: the cluster state does not change, so only its `EDIT_ALLOW_LIST` task
    shows when it is done. Tasks are newest first and an older `SUCCEEDED` one is usually there:
    note `data[0].info.id` for that `task_type` **before** the PUT, then wait for a newer task to
    reach `SUCCEEDED` (or `FAILED`). The same applies to any wait on a task.
  - Treat any state that is not settled and does not end in `_FAILED` as still in progress, and
    give every wait a deadline.
- **Error bodies are not always JSON.** A wrong path answers an HTML 404; `/api/v1/...` (the
  old, non-public base) answers an XML 401 that looks like a bad key.
- **Confirm before destructive or billable changes** (scale, pause, delete, restore, removing
  an allow list from a cluster) and show the planned request first.

## References

- [`references/auth-and-discovery.md`](references/auth-and-discovery.md): key handling, curl /
  Python / PowerShell patterns, ID discovery, pagination, errors, task polling.
- [`references/recipes.md`](references/recipes.md): cluster details and scaling, pause and
  resume, nodes, allow lists, backups and restore, metrics, database users (SQL).

## What the API covers and what it doesn't

**Covered:**
- Cluster lifecycle: create, edit (scale nodes, vCPUs, disk), pause, resume, delete
- Network allow lists (project-level, assigned to clusters by ID)
- Backups (on-demand, schedules, list) and restore
- VPCs, VPC peering and private service endpoints
- Cluster metrics (a curated set; see `recipes.md`) and metrics export configuration
- Alert rules and notifications, account users and roles, tasks

**Not covered: alternatives**
- **Database users and roles** (YSQL/YCQL): not an API resource. Create them with SQL, connected
  as the admin user set at cluster creation: follow
  [`recipes.md` § Database users](references/recipes.md#database-users-sql-not-the-api) (an
  idempotent script, the password from the environment rather than the command line, TLS
  `verify-full` with the CA from `GET /connection-certificate`, and an allow list for the client).
- **GFlags / server flags**: not exposed by the API. Raise a support ticket.
- **Full Prometheus metric surface**: configure metrics export (Datadog, Grafana Cloud,
  Prometheus and others) in the console or via `metrics-exporter-configs`.
- **Performance diagnosis** (SQL or metrics layer): connect via YSQL and use the `yb-perf` skill.
- **Performance Advisor / Insights**: console only.
