---
name: aeon-api
description: Use when scripting, automating, or querying YugabyteDB Aeon (YugabyteDB Managed cloud) via its REST API — including listing and scaling clusters, managing database users, configuring network allowlists, reading cluster metrics, managing backups, and working with VPC peering or private endpoints. Triggers on YugabyteDB Aeon API, YugabyteDB Managed API, cloud.yugabyte.com/api, Aeon REST, automate Aeon, scale Aeon cluster programmatically, Aeon metrics via API, Aeon backup via API, Aeon allowlist API. Does NOT cover the YBA (YugabyteDB Anywhere) self-managed API — use `yba-api` for that. Does NOT cover SQL-layer performance analysis — use `yb-query-analysis` for that.
---

# YugabyteDB Aeon REST API

The Aeon REST API allows programmatic management of YugabyteDB Managed cloud clusters. It is a distinct API from the YBA (YugabyteDB Anywhere) API — different base URL, different auth mechanism, different resource model.

**Base URL:** `https://cloud.yugabyte.com/api/v1`

**API reference / Swagger UI:** `https://cloud.yugabyte.com/swagger` — always check this for the current endpoint list; the API evolves with Aeon releases.

## Authentication

All requests require an **API key** set as a Bearer token:

```
Authorization: Bearer <api-key>
```

Generate an API key: Aeon console → top-right profile → API Keys → Create API Key. Keys are scoped to your account and do not expire unless revoked.

## Resource hierarchy

```
Account (accountId)
  └── Project (projectId)
        └── Cluster (clusterId)
              ├── Nodes
              ├── Backups
              ├── Database users
              ├── Network allowlists
              └── Metrics
```

Most endpoints follow the pattern:
`/api/v1/accounts/{accountId}/projects/{projectId}/clusters/{clusterId}/...`

Discover your IDs in [`references/auth-and-discovery.md`](references/auth-and-discovery.md).

## References

- [`references/auth-and-discovery.md`](references/auth-and-discovery.md) — obtaining API key, discovering accountId/projectId/clusterId, curl and Python/PowerShell patterns, pagination
- [`references/recipes.md`](references/recipes.md) — ready-to-use operations: cluster info, scaling, node list, database users, allowlists, backups, metrics

## What the API covers vs what it doesn't

**Covered by the Aeon API:**
- Cluster lifecycle (create, modify spec/scale, pause, resume, delete)
- Database user management (create, list, change password)
- Network allowlists (add/remove IP ranges)
- Backup schedules and on-demand backups; restore
- VPC peering and private endpoint configuration
- Cluster metrics (curated set — see `recipes.md`)
- Alerts configuration

**Not covered — alternatives:**
- Full Prometheus metrics surface → configure metrics export in Aeon console (Settings → Metrics Export to Datadog / Grafana Cloud / Prometheus)
- SQL-layer query analysis → connect via YSQL and use `yb-query-analysis` skill
- Performance Advisor / Insights → Aeon console only (no current API endpoint)
- GFlag changes → support ticket (not exposed via API)
