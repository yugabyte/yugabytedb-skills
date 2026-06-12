# YBA prerequisites & cloud network discovery

Source: Yugabyte docs (stable channel). State these requirements when planning an install, and use the discovery commands to fill in the network IDs a cloud provider manifest needs.

## YBA host VM requirements

<https://docs.yugabyte.com/stable/yugabyte-platform/prepare/server-yba/>

| | Requirement |
|---|---|
| vCPU | **4 cores** |
| Memory | **8 GB** |
| Disk | **215 GB minimum.** Provision a **250 GB root volume** (or a dedicated filesystem) to leave headroom. |
| Architecture | **x86_64 only** — ARM/aarch64 is *not* supported for the YBA host (universes may still run on ARM DB nodes). |
| Data directory | YBA stores data in **`/opt/yugabyte/data`** by default. A dedicated data disk needs ≥ 200 GB free. |
| OS | AlmaLinux 8/9, RHEL 8 / 9.3+, Rocky 8/9, Oracle Linux 8, Ubuntu 20/22, Amazon Linux 2023, SLES 15 SP5. **Production: AlmaLinux 8 or RHEL 8** (AlmaLinux 8 is YBA's default image). CentOS 7 / RHEL 7 / Ubuntu 18 / Amazon Linux 2 were removed in v2.21. |
| Python | **3.10–3.12** (3.10–3.11 for v2025.2.0–2025.2.1). Both `python` and `python3` must resolve to Python 3. |
| Privileges | **sudo root** for install; YBA Installer (`yba-ctl`) also supports non-root mode. |
| License | A **license file** from Yugabyte sales is required. |
| Installer | **YBA Installer (`yba-ctl`)** is current; Replicated is legacy. No Docker requirement. |
| HA | Active-passive HA needs **two identical VMs**. |

> The **250 GB** root-volume figure is the safe provisioning target; the **215 GB** is the documented hard minimum. The separate **250 GB** number for *AWS gp3 EBS* below refers to **DB-node** data disks, not the YBA host — don't conflate them.

## Database (universe) node requirements

<https://docs.yugabyte.com/stable/deploy/checklist/> · <https://docs.yugabyte.com/stable/yugabyte-platform/prepare/server-nodes-software/>

- **Hardware:** min 2 cores / 2 GB RAM. Production: 16+ cores, 32 GB (YCQL) / 64 GB (YSQL) RAM.
- **Disks:** SSD required (ZFS and NFS not supported). **XFS** recommended, mount with `noatime`. Cloud sizing: AWS gp3 EBS ≥ **250 GB**; GCP local SSD **375 GB** or persistent SSD ≥ 250 GB.
- **Software:** Python 3.6–3.12; packages `tar`, `unzip`, `policycoreutils-python-utils`, OpenSSH server. RHEL 8 needs `DefaultLimitNOFILE=1048576` in systemd conf (reboot).
- **SSH user:** an SSH user **with sudo** is used only to *provision* nodes (installs the node-agent). After provisioning, YBA talks to the node-agent over **9070/443** and no longer needs SSH/sudo. **The SSH user cannot be named `yugabyte`.**

## Ports

<https://docs.yugabyte.com/stable/yugabyte-platform/prepare/networking/>

| Flow | Ports |
|---|---|
| YBA UI / access | 443 (HTTPS), 9090 (Prometheus) |
| DB node → YBA | 443 (node-agent) |
| YBA → DB node (provisioning) | 22 (SSH, legacy provisioning), 9070 (node-agent RPC) |
| Node ↔ node | 7100 (master RPC), 9100 (tserver RPC), 18018 (YB Controller RPC) |
| HTTP UIs / monitoring | 7000 (master), 9000 (tserver), 9300 (node exporter) |
| Client → DB | 5433 (YSQL), 9042 (YCQL) |

---

## Network discovery — get VPC / subnet / SG IDs for a provider manifest

A cloud provider resource needs: **region(s)**, **VPC/vnet**, **one subnet per availability zone**, a **security group**, and (AWS/Azure) an SSH **key pair**. GCP subnets are per-region (span all zones); AWS subnets are per-AZ (1 subnet : 1 AZ); Azure subnets are per-region too. All commands below are read-only.

### AWS (`aws ec2 describe-*` — most calls need `--region`)

```bash
# Account ID you're provisioning into
aws sts get-caller-identity --query Account --output text

# Enabled regions
aws ec2 describe-regions --query "Regions[].RegionName" --output table

# AZs in a region
aws ec2 describe-availability-zones --region us-west-2 \
  --query "AvailabilityZones[].ZoneName" --output table

# VPCs (ID + CIDR + Name)
aws ec2 describe-vpcs --region us-west-2 \
  --query "Vpcs[].{VpcId:VpcId, CIDR:CidrBlock, Name:Tags[?Key=='Name']|[0].Value}" --output table

# Subnets per AZ in a VPC  ← the AZ column maps directly to a yba zone
aws ec2 describe-subnets --region us-west-2 \
  --filters "Name=vpc-id,Values=<vpc-id>" \
  --query "Subnets[].{SubnetId:SubnetId, AZ:AvailabilityZone, CIDR:CidrBlock}" --output table

# Security groups in a VPC
aws ec2 describe-security-groups --region us-west-2 \
  --filters "Name=vpc-id,Values=<vpc-id>" \
  --query "SecurityGroups[].{GroupId:GroupId, GroupName:GroupName}" --output table

# Key pairs (name to use for SSH)
aws ec2 describe-key-pairs --region us-west-2 \
  --query "KeyPairs[].KeyName" --output table
```

### GCP (`gcloud compute …` — subnets are per-region; zones auto-discover)

```bash
gcloud config get-value project                                   # project ID
gcloud compute regions list --format="table(name)"                # regions
gcloud compute zones list --format="table(name, region)"          # zones + their region
gcloud compute networks list --format="table(name, x_gcloud_subnet_mode)"   # VPC networks

# Subnetworks per region (name + region + CIDR) for one network — use the full path in the manifest:
#   projects/<project>/regions/<region>/subnetworks/<name>
gcloud compute networks subnets list \
  --filter="network:<vpc-network>" \
  --format="table(name, region, ipCidrRange)"

gcloud compute firewall-rules list --filter="network:<vpc-network>" \
  --format="table(name, direction, allowed[].map().firewall_rule().list())"
```

### Azure (`az network …` — subnets are per-vnet/region)

```bash
az account show --query "{SubscriptionId:id, Name:name, TenantId:tenantId}" --output table
az account list-locations --query "[].name" --output table          # regions
az group list --query "[].{Name:name, Location:location}" --output table   # resource groups

# VNets (name + RG + location + CIDR)
az network vnet list \
  --query "[].{Name:name, ResourceGroup:resourceGroup, Location:location, CIDR:addressSpace.addressPrefixes[0]}" \
  --output table

# Subnets in a vnet (need the vnet name + its RG)
az network vnet subnet list --resource-group <rg> --vnet-name <vnet> \
  --query "[].{Name:name, CIDR:addressPrefix}" --output table

az network nsg list --query "[].{Name:name, ResourceGroup:resourceGroup}" --output table
```

> Azure zones are referenced as numbers (`1`,`2`,`3`) within a region, not separate network objects — one regional subnet is associated with each zone. If `addressPrefix` is null on newer API versions, query `addressPrefixes[0]` instead.

### If the CLI isn't available

If the user has no cloud CLI configured and the IDs aren't in memory or the conversation, **ask** for exactly: region(s), VPC/vnet ID, one subnet ID per availability zone, security-group ID, and (AWS/Azure) the SSH key-pair name. Do not fabricate IDs.
