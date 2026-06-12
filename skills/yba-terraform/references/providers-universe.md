# Stage 2 — cloud/on-prem providers, storage configs, universes, backups

All resources here run against the **authenticated** `yba` provider (token from `install-bootstrap.md`). Fill subnet/VPC/region IDs from `prereqs-and-network.md`; grant the credentials from `cloud-iam-setup.md`.

Always prefer the per-cloud resources below over the deprecated `yba_cloud_provider`, `yba_storage_config_resource`, and `yba_backups`.

---

## Cloud providers

### AWS — `yba_aws_provider`

```hcl
resource "yba_aws_provider" "aws" {
  name              = "aws-us-west-2"
  access_key_id     = var.yba_aws_access_key_id       # or: use_iam_instance_profile = true
  secret_access_key = var.yba_aws_secret_access_key

  regions {
    code              = "us-west-2"
    vpc_id            = "<aws-vpc-id>"
    security_group_id = "<aws-sg-id>"
    zones {
      code   = "us-west-2a"
      subnet = "<subnet-id-a>"      # one subnet per AZ
    }
    zones {
      code   = "us-west-2b"
      subnet = "<subnet-id-b>"
    }
    zones {
      code   = "us-west-2c"
      subnet = "<subnet-id-c>"
    }
  }

  # SSH keypair: let YBA manage one, or supply your own:
  # ssh_keypair_name        = "my-keypair"
  # ssh_private_key_content = file("~/.ssh/my-keypair.pem")

  # Image: YBA-managed (it picks the AMI) ...
  yba_managed_image_bundles {
    arch           = "x86_64"        # or aarch64
    use_as_default = true
  }
  # ... OR a custom AMI per region:
  # image_bundles {
  #   name           = "custom-x86"
  #   use_as_default = true
  #   details {
  #     arch             = "x86_64"
  #     ssh_user         = "ec2-user"
  #     ssh_port         = 22
  #     region_overrides = { "us-west-2" = "<ami-id>" }   # map: region → AMI
  #   }
  # }

  air_gap_install = false
}
```

`regions` → `code` (req), `vpc_id`, `security_group_id`, ≥1 `zones`. `zones` → `code` (req), `subnet` (req), `secondary_subnet` (opt). Read-only: `id`, `regions[*].uuid`, `access_key_code`. Other args: `use_iam_instance_profile`, `hosted_zone_id` (Route53), `set_up_chrony`, `ntp_servers`.

#### Multi-region AWS (dynamic blocks)

For more than one region, drive the `regions`/`zones` blocks from a variable instead of repeating them. The universe then spans every region via `regions[*].uuid` (see the universe section below). This same pattern applies to `yba_azure_provider` (which also has nested `zones`); for `yba_gcp_provider`, drop the inner `dynamic "zones"` since GCP has no zones block.

```hcl
variable "regions" {
  type = map(object({
    vpc_id            = string
    security_group_id = string
    zones             = map(string) # az_name => subnet_id (use private subnets for DB nodes)
  }))
  # e.g. { "eu-west-2" = { vpc_id = "vpc-…", security_group_id = "sg-…",
  #         zones = { "eu-west-2a" = "subnet-…", "eu-west-2b" = "subnet-…" } }, ... }
}

resource "yba_aws_provider" "multiregion" {
  name              = "aws-eu-multiregion"
  access_key_id     = var.aws_access_key_id
  secret_access_key = var.aws_secret_access_key

  dynamic "regions" {
    for_each = var.regions
    content {
      code              = regions.key
      vpc_id            = regions.value.vpc_id
      security_group_id = regions.value.security_group_id
      dynamic "zones" {
        for_each = regions.value.zones
        content {
          code   = zones.key
          subnet = zones.value
        }
      }
    }
  }

  yba_managed_image_bundles {
    arch           = "x86_64"
    use_as_default = true
  }
  air_gap_install = false
}
```

Discover the VPC/subnet/SG IDs with the `aws ec2 describe-*` commands in `prereqs-and-network.md`. Reuse a security group YBA already created for an existing universe (named `yba-universe-*`) if one exists, or omit `security_group_id` to let YBA create one per region.

### GCP — `yba_gcp_provider`

GCP has **no nested `zones` block** — zones auto-discover. Subnet is a full resource path.

```hcl
resource "yba_gcp_provider" "gcp" {
  name        = "gcp-us-west1"
  credentials = file("${path.module}/yba-gcp-sa.json")   # or: use_host_credentials = true
  project_id  = var.project_id
  network     = "<vpc-network>"                            # or create_vpc = true / use_host_vpc = true

  regions {
    code          = "us-west1"
    shared_subnet = "projects/${var.project_id}/regions/us-west1/subnetworks/<subnet>"
    # instance_template = "<template>"   # optional
  }

  yba_managed_image_bundles {
    arch           = "x86_64"            # GCP managed bundles: x86_64 only
    use_as_default = true
  }
  # Custom image instead: image_bundles { details { global_yb_image = "projects/.../global/images/<img>" ... } }

  # yb_firewall_tags = "yb-server,yb-client"
  air_gap_install = false
}
```

`regions` → `code` (req), `shared_subnet` (subnet path or `"default"`), `instance_template` (opt). VPC: `network` / `create_vpc` / `use_host_vpc` / `shared_vpc_project_id`.

### Azure — `yba_azure_provider`

```hcl
resource "yba_azure_provider" "azure" {
  name            = "azure-eastus"
  client_id       = var.azure_client_id
  client_secret   = var.azure_client_secret      # omit + use_managed_identity = true for MI
  tenant_id       = var.azure_tenant_id
  subscription_id = var.azure_subscription_id
  resource_group  = var.azure_resource_group

  regions {
    code = "eastus"
    vnet = "<vnet-name>"
    zones {
      code   = "eastus-1"
      subnet = "<subnet-name>"
    }
    zones {
      code   = "eastus-2"
      subnet = "<subnet-name>"
    }
    zones {
      code   = "eastus-3"
      subnet = "<subnet-name>"
    }
  }

  yba_managed_image_bundles {
    arch           = "x86_64"
    use_as_default = true
  }
  # Custom image: image_bundles { details { global_yb_image = "<azure-image-resource-uri>" ... } }

  air_gap_install = false
}
```

`regions` → `code` (req), `vnet`, `security_group_id`, `resource_group`, `network_resource_group`, ≥1 `zones`. `zones` → `code` (req), `subnet` (req), `secondary_subnet` (opt). Optional top-level: `network_subscription_id`, `network_resource_group`, `hosted_zone_id` (Private DNS Zone).

### On-prem / VM — `yba_onprem_provider` (+ `yba_onprem_node_instance`)

> **v1.0.0 has known on-prem issues.** If you already manage on-prem with the provider, pin `version = "~> 0.1"`. Otherwise register nodes with the standalone `yba_onprem_node_instance` rather than inline `node_instances {}`.

```hcl
resource "yba_onprem_provider" "onprem" {
  name                     = "onprem-dc1"
  ssh_user                 = "ybadmin"            # sudo for provisioning; not "yugabyte"
  ssh_keypair_name         = "onprem-key"
  ssh_private_key_content  = file("~/.ssh/onprem-key.pem")
  skip_provisioning        = true
  passwordless_sudo_access = true

  regions {
    code = "dc1"
    zones { code = "dc1-rack1" }
    zones { code = "dc1-rack2" }
  }

  instance_types {
    instance_type_code = "xlarge"
    num_cores          = 16
    mem_size_gb        = 64
    volume_size_gb     = 500
  }
}

resource "yba_onprem_node_instance" "node1" {
  provider_name = yba_onprem_provider.onprem.name
  instance_type = "xlarge"          # matches instance_type_code above
  ip            = "10.0.0.11"
  region        = "dc1"
  zone          = "dc1-rack1"
}
```

---

## Backup storage configs

Each returns a read-only `config_uuid` referenced by backups/restores. Common args: `name`, `backup_location`, optional `region_locations {}`.

```hcl
resource "yba_s3_storage_config" "s3" {
  name              = "s3-backups"
  backup_location   = "s3://my-bucket/yugabyte-backups"
  access_key_id     = var.yba_aws_access_key_id     # or use_iam_instance_profile = true
  secret_access_key = var.yba_aws_secret_access_key
  # MinIO/Ceph: aws_host_base = "minio.example.com:9000", path_style_access = true
}

resource "yba_gcs_storage_config" "gcs" {
  name            = "gcs-backups"
  backup_location = "gs://my-bucket/yugabyte-backups"
  credentials     = file("${path.module}/yba-gcs-sa.json")   # or use_gcp_iam = true
}

resource "yba_azure_storage_config" "az" {
  name            = "azure-backups"
  backup_location = "https://<account>.blob.core.windows.net/<container>"
  sas_token       = var.azure_sas_token                       # or use_azure_iam = true
}

resource "yba_nfs_storage_config" "nfs" {
  name            = "nfs-backups"
  backup_location = "/mnt/nfs/yugabyte-backups"
  # nfs_bucket = "yugabyte_backup"   # default
}
```

---

## Universe — `yba_universe`

Links to the provider via `provider` (the provider's `id`), `region_list` (region UUIDs), and two companion data sources for the access key and software version.

```hcl
data "yba_provider_key" "key" {
  provider_id = yba_aws_provider.aws.id
}

data "yba_release_version" "release" {
  depends_on = [yba_aws_provider.aws]
}

resource "yba_universe" "prod" {
  clusters {
    cluster_type = "PRIMARY"          # or "ASYNC" for read replicas
    user_intent {
      universe_name      = "prod-us-west"
      provider           = yba_aws_provider.aws.id
      region_list        = yba_aws_provider.aws.regions[*].uuid
      num_nodes          = 3
      replication_factor = 3
      instance_type      = "c5.xlarge"

      device_info {
        num_volumes  = 1
        volume_size  = 250            # GB; ≥250 for production AWS gp3
        storage_type = "GP3"          # AWS: GP2/GP3/IO1/IO2 · GCP: Persistent/Scratch/Hyperdisk_*
        disk_iops    = 3000           # REQUIRED for GP3/IO1/IO2 (gp3 baseline 3000)
        throughput   = 125            # REQUIRED for GP3 (MB/s; gp3 baseline 125)
      }

      enable_ysql         = true
      enable_node_to_node_encrypt   = true
      enable_client_to_node_encrypt = true
      use_time_sync       = true

      yb_software_version = data.yba_release_version.release.id
      access_key_code     = data.yba_provider_key.key.id
    }

    # Optional: masters on dedicated nodes (omit inner fields to inherit tserver sizing)
    # dedicated_masters {}
  }

  communication_ports {}              # empty = YBA defaults
}
```

For **GCP/Azure** universes, swap the two data-source `provider_id`/`depends_on` and the `provider`/`region_list` references to `yba_gcp_provider.gcp` / `yba_azure_provider.azure`.

**Multi-region placement:** `region_list = <provider>.regions[*].uuid` spreads the universe across every region on the provider. `num_nodes` is the *total* across all regions, so a 3-region **RF3** universe with `num_nodes = 3` places one node per region (survives a full region loss); `num_nodes = 9` gives three per region. YBA balances nodes across the regions and their zones automatically.

`user_intent` required: `universe_name`, `provider`, `region_list`, `num_nodes`, `replication_factor`, `instance_type`, `device_info`, `yb_software_version`. Common optional: `access_key_code`, `assign_public_ip`, `enable_ysql_auth` (+`ysql_password`), `enable_ycql`, `instance_tags` (map), `specific_gflags {}`, `dedicated_masters {}`, `image_bundle_uuid`, `preferred_region`.

**`device_info` block** — `num_volumes` (req), `volume_size` GB (req), `storage_type` (req for cloud). Conditionally-required fields the provider enforces **at plan time** (so `terraform validate` passes but `plan` errors):

| `storage_type` | Also required |
|---|---|
| AWS `GP3` | `disk_iops` **and** `throughput` (MB/s) — gp3 baseline 3000 / 125 |
| AWS `IO1`, `IO2` | `disk_iops` |
| AWS `GP2` | neither (don't set `disk_iops`/`throughput`) |
| GCP `Hyperdisk_Balanced`/`Hyperdisk_Extreme`, Azure `UltraSSD_LRS` | `disk_iops` (+ `throughput` on balanced/ultra) |
| GCP `Persistent`/`Scratch` | neither |

> **`terraform validate` is not enough.** The yba provider validates many cross-field rules (the GP3 iops/throughput pair above, YSQL/YCQL must have at least one enabled, password rules when auth is on) only during `plan`/`apply`. Always run `terraform plan` to confirm a manifest is complete — a clean `validate` can still fail at plan.

---

## Backups, schedules, restore

Backups/schedules use `keyspaces` (a **list**; `[]` = full universe). `backup_type` = `PGSQL_TABLE_TYPE` (YSQL) or `YQL_TABLE_TYPE` (YCQL). `storage_config_uuid` is the storage config's `config_uuid`.

```hcl
# On-demand backup of one database
resource "yba_backup" "adhoc" {
  universe_uuid       = yba_universe.prod.id
  storage_config_uuid = yba_s3_storage_config.s3.config_uuid
  keyspaces           = ["app_db"]          # [] = full universe
  backup_type         = "PGSQL_TABLE_TYPE"
  time_before_delete  = "168h"              # 7-day retention
}

# Scheduled backups — frequency (Go duration, min "1h") XOR cron_expression
resource "yba_backup_schedule" "daily" {
  universe_uuid       = yba_universe.prod.id
  storage_config_uuid = yba_s3_storage_config.s3.config_uuid
  keyspaces           = ["app_db"]
  schedule_name       = "daily-2am"
  backup_type         = "PGSQL_TABLE_TYPE"
  cron_expression     = "0 2 * * *"         # or: frequency = "24h"
  time_before_delete  = "720h"              # 30-day retention
  # incremental_backup_frequency = "1h"
}

# Restore — note backup_storage_info { keyspace = ... } is SINGULAR & repeatable
resource "yba_restore" "to_staging" {
  universe_uuid       = yba_universe.staging.id
  storage_config_uuid = yba_s3_storage_config.s3.config_uuid

  backup_storage_info {
    storage_location = "<storage-location-from-backup>"   # see data.yba_backup_info
    keyspace         = "app_db"
    backup_type      = "PGSQL_TABLE_TYPE"
    # new_owner = "app_user"   # optional remap
  }
}
```

To find the `storage_location` for a restore, use the `yba_backup_info` data source (by `backup_uuid`, or by `universe_uuid`/`universe_name` with a date range). Use `yba_universe_schema` to discover keyspace/table names/UUIDs.
