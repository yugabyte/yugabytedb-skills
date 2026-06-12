---
name: yba-terraform
description: Use when writing, generating, or troubleshooting Terraform that drives YugabyteDB Anywhere (YBA) via the `yugabyte/yba` provider — installing YBA on a VM, registering the first customer, and creating cloud (AWS/GCP/Azure) or on-prem providers, storage configs, universes, backups, schedules, and restores. Also covers the upstream cloud Terraform (IAM user / service account / service principal) that grants YBA its node-deployment permissions, and the CLI commands to discover the VPC/subnet/security-group network info those manifests need. Triggers on `yba_installer`, `yba_customer_resource`, `yba_aws_provider`/`yba_gcp_provider`/`yba_azure_provider`, `yba_universe`, `yba_backup`, `source = "yugabyte/yba"`, or any mention of the YBA Terraform provider. Does NOT cover the YBA REST API directly (use the yba-api skill) or YugabyteDB Aeon.
---

# YugabyteDB Anywhere (YBA) Terraform Provider

This skill generates correct Terraform for the **`yugabyte/yba`** provider — the official provider that installs and drives a self-managed YugabyteDB Anywhere control plane.

- **Registry:** <https://registry.terraform.io/providers/yugabyte/yba/latest/docs>
- **Source:** <https://github.com/yugabyte/terraform-provider-yba> (branch `main`)
- **Current version:** `1.0.0` (Early Access). Requires YBA stable `>= 2024.2.0.0` or preview `>= 2.23.1.0`. **Kubernetes universes are not supported by this provider** — for those use the `yb-k8s-operator` skill.

It is **not** about:
- The **YBA REST API** directly — use the `yba-api` skill (this provider calls that API for you).
- **YugabyteDB Aeon** (managed DBaaS) or **YSQL/YCQL** database access.

## Reference files (read before generating non-trivial manifests)

- `references/prereqs-and-network.md` — YBA host VM requirements (CPU/RAM/**disk**/OS/Python/ports), DB-node requirements, and the **aws/gcloud/az CLI commands** to discover VPC IDs, subnets-per-zone, security groups, and regions. Read this first when sizing a VM or filling in network IDs.
- `references/cloud-iam-setup.md` — Terraform (using the `aws`/`google`/`azurerm` providers, **not** the yba provider) that creates the **IAM user / service account / service principal** YBA uses to deploy nodes, with the exact delegated permissions from the Yugabyte docs. Read this when the user needs to grant YBA cloud access.
- `references/install-bootstrap.md` — the `yba_installer` and `yba_customer_resource` resources and the **two-provider (unauthenticated → authenticated) pattern**. Read this when installing YBA or wiring up the API token.
- `references/providers-universe.md` — full HCL for `yba_aws_provider` / `yba_gcp_provider` / `yba_azure_provider` / `yba_onprem_provider`, the four storage-config resources, `yba_universe`, and `yba_backup` / `yba_backup_schedule` / `yba_restore`. Read this when generating any provider, universe, or backup resource.

## The end-to-end workflow

A complete "from nothing to a running universe with backups" deployment is **two Terraform stages** because the API token does not exist until YBA is installed and the customer is registered.

```
┌─ Stage 0 (optional, upstream cloud TF) ── references/cloud-iam-setup.md
│   aws_iam_user / google_service_account / azuread_application
│   → the credentials YBA will use to deploy nodes
│
├─ Stage 1 (unauthenticated yba provider) ── references/install-bootstrap.md
│   yba_installer          → install YBA on an existing VM over SSH
│   yba_customer_resource  → register first customer; OUTPUTS api_token + cuuid
│
└─ Stage 2 (authenticated yba provider, api_token from stage 1) ── references/providers-universe.md
    yba_aws_provider / yba_gcp_provider / yba_azure_provider   (network info from references/prereqs-and-network.md)
    yba_s3_storage_config / yba_gcs_storage_config / ...
    yba_universe
    yba_backup_schedule / yba_backup / yba_restore
```

Run Stage 1 and Stage 2 as separate `apply`s (or separate root modules / `-target`), because the `api_token` produced by `yba_customer_resource` must be known before the authenticated provider can be configured. Passing a provider `api_token` that is computed in the same apply is fragile — split the state.

## Provider configuration (the two-provider pattern)

```hcl
terraform {
  required_providers {
    yba = {
      source  = "yugabyte/yba"
      version = "~> 1.0"
    }
  }
}

# Stage 1: install + customer registration need NO token.
provider "yba" {
  alias = "unauthenticated"
  host  = var.yba_host   # IP or DNS, optionally :port
}

# Stage 2: everything else needs the customer API token.
provider "yba" {
  host      = var.yba_host
  api_token = var.yba_api_token   # from yba_customer_resource.<name>.api_token
}
```

The provider block accepts exactly three arguments — all optional, all env-var backed:

| Argument | Env var (preferred → legacy) | Notes |
|---|---|---|
| `host` | `YBA_HOST` → `YB_HOST` | IP/DNS, optionally with `:port`. |
| `api_token` | `YBA_API_TOKEN` / `YBA_API_KEY` → `YB_API_KEY` | Customer API token. Omit for the unauthenticated provider. |
| `enable_https` | `YBA_ENABLE_HTTPS` → `YB_ENABLE_HTTPS` | **`true` by default.** Set `false` for HTTP-only YBA (then put the HTTP port in `host`). |

There is **no `api_key` provider argument** — `YBA_API_KEY` is only an env-var alias for `api_token`. With env vars set, the authenticated block can be empty (`provider "yba" {}`).

## Resource cheat-sheet (use the per-cloud resources, not the deprecated generics)

| Purpose | Resource | Notes |
|---|---|---|
| Install YBA on a VM | `yba_installer` | SSH-based; unauthenticated provider. |
| Register first customer | `yba_customer_resource` | Outputs `api_token`, `cuuid`. `destroy` is a no-op. |
| Cloud provider | `yba_aws_provider`, `yba_gcp_provider`, `yba_azure_provider` | **Prefer these.** `yba_cloud_provider` is deprecated (removed in v2.0.0). |
| On-prem / VM provider | `yba_onprem_provider` + `yba_onprem_node_instance` | v1.0.0 has known on-prem issues — see reference file. |
| Backup storage | `yba_s3_storage_config`, `yba_gcs_storage_config`, `yba_azure_storage_config`, `yba_nfs_storage_config` | `yba_storage_config_resource` is deprecated. |
| Universe | `yba_universe` | References provider via `provider`, `region_list`, plus `yba_provider_key` + `yba_release_version` data sources. |
| Backups | `yba_backup` (on-demand), `yba_backup_schedule` (cron/frequency) | `yba_backups` is deprecated → use `yba_backup_schedule`. |
| Restore | `yba_restore` | Uses `backup_storage_info { keyspace = ... }` (singular). |

Useful data sources: `yba_provider_key`, `yba_release_version`, `yba_universe_schema` (keyspace/table discovery for backups), `yba_storage_configs`, `yba_provider_regions`, `yba_backup_info`.

## Getting the network info a cloud provider needs

`yba_aws_provider` / `yba_azure_provider` need **VPC/vnet IDs, subnet IDs per zone, and a security group**; `yba_gcp_provider` needs the **network and a regional subnet** (GCP zones auto-discover). Before generating a provider manifest:

1. **Check memory / the conversation** for already-known VPC, subnet, region, and project/subscription IDs.
2. If unknown, **run the discovery CLI commands** in `references/prereqs-and-network.md` (`aws ec2 describe-subnets …`, `gcloud compute networks subnets list …`, `az network vnet subnet list …`) and read the IDs from the output.
3. If the user has no cloud CLI configured and the info isn't in memory, **ask the user** for the region(s), VPC/vnet ID, subnet ID per availability zone, and security-group ID — list exactly what's missing rather than inventing placeholder IDs that will fail at apply.

Never invent real-looking IDs (`vpc-0123…`, `subnet-…`). Use clearly-templated placeholders (`<aws-vpc-id>`) only when explicitly producing a template the user will fill in.

## Prerequisites (always state these when planning an install)

The YBA **host VM** (official minimums, from the Yugabyte docs):

- **4 vCPU, 8 GB RAM, x86_64 only** (ARM is not supported for the YBA host).
- **Disk: 215 GB minimum.** Provision a **250 GB root volume (or a dedicated filesystem mounted at YBA's data dir, default `/opt/yugabyte/data`)** to leave headroom — a too-small root volume is the most common install failure.
- A supported Linux OS (AlmaLinux 8/9, RHEL 8/9, Rocky 8/9, Oracle Linux 8, Ubuntu 20/22, Amazon Linux 2023, SLES 15 SP5). **AlmaLinux 8 / RHEL 8 recommended for production.**
- **Python 3.10–3.12**, with both `python` and `python3` resolving to Python 3.
- A **YBA license file** from Yugabyte, and **sudo access** for the SSH user (YBA Installer also supports non-root mode).

Full host + DB-node requirements and the port list are in `references/prereqs-and-network.md`.

## Anti-patterns

| Anti-pattern | Consequence | Do instead |
|---|---|---|
| Configuring the authenticated provider with an `api_token` computed in the same apply | Provider config can't depend on a not-yet-known value; flaky/failed apply | Split into Stage 1 (install + customer) and Stage 2 (everything else); pass the token between states/modules. |
| Using `yba_cloud_provider` / `yba_storage_config_resource` / `yba_backups` | Deprecated; removed in v2.0.0 | Use `yba_aws_provider`/`…`, the per-backend `*_storage_config`, and `yba_backup_schedule`. |
| Adding a nested `zones {}` block to `yba_gcp_provider` | Schema error — GCP has no per-zone block | GCP: `regions { code, shared_subnet }`; zones auto-discover. AWS/Azure use `zones { code, subnet }`. |
| Putting AMI IDs in `region_overrides` for GCP/Azure | Wrong field | AWS uses `image_bundles.details.region_overrides` (map); GCP/Azure use `details.global_yb_image` (string). Or use `yba_managed_image_bundles`. |
| Hard-coding a 215 GB / smaller root volume | Install runs out of space mid-deploy | Provision **250 GB**; YBA data lives in `/opt/yugabyte/data`. |
| Using `keyspaces` on `yba_restore` | Schema error | Restore uses repeatable `backup_storage_info { keyspace = ... }` (singular); backups/schedules use `keyspaces` (list). |
| `device_info` with `storage_type = "GP3"` but no `disk_iops`/`throughput` | **Passes `validate`, fails `plan`** — GP3 requires both (IO1/IO2 require `disk_iops`) | Set `disk_iops` and `throughput` for GP3 (baseline 3000 / 125). See the `device_info` table in `providers-universe.md`. |
| Trusting a clean `terraform validate` | The yba provider enforces many cross-field rules only at plan time | Always run `terraform plan` — `validate` checks schema shape, not the provider's conditional requirements. |
| Granting YBA broad cloud admin | Over-privileged credentials | Apply the scoped policy/role in `references/cloud-iam-setup.md` (AWS JSON policy, GCP `roles/compute.admin`, Azure Network + VM Contributor). |
| Committing state with `api_token`/`password` in plaintext | Credential leak — token does not expire | Use an encrypted remote backend; mark vars `sensitive`. |
| Expecting `terraform destroy` to remove the customer | `yba_customer_resource` destroy is a no-op (no delete-customer API) | Understand it only drops from state; the YBA customer persists. |
