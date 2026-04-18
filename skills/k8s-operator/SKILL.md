---
name: operator
description: Use when provisioning, managing, or troubleshooting YugabyteDB universes on Kubernetes via the YugabyteDB Kubernetes Operator and YugabyteDB Anywhere CRDs (YBUniverse, YBProvider, Release, Backup, StorageConfig, etc.). Triggers on kubectl apply, Helm install of yugaware, operator CRDs, or any mention of YugabyteDB with Kubernetes.
---

# YugabyteDB Kubernetes Operator

**This skill includes:**
- `references/crd-examples.md` — complete YAML examples for every CRD
- `references/workflows.md` — end-to-end provisioning, upgrade, backup, and DR workflows
- `references/kubeconfig-secrets.md` — create and rotate kubeconfig secrets for multi-cluster providers (`ybprovider` resources). **Do not create generic kubeconfig secrets, always follow this guidance**
- `references/multi-cluster-service-mesh.md` — Helm overrides and pod address templates for Istio and Cilium

**Note** that this skill works with the YBA Kubernetes Operator, a commercial product. It does not fully apply to the YugabyteDB OSS Kubernetes Operator, which has fewer CRDs and features.

The YugabyteDB Kubernetes Operator manages YugabyteDB Anywhere (YBA) universes declaratively through Kubernetes Custom Resources. It installs alongside YBA and watches a configured namespace for CR changes.

CRD source: `https://raw.github.com/yugabyte/charts/<version>/crds/concatenated_crd.yaml`
Documentation: `https://docs.yugabyte.com/stable/yugabyte-platform/anywhere-automation/yb-kubernetes-operator/`
YugabyteDB Helm charts: `https://charts.yugabyte.com/index.yaml`

## Prerequisites: Context, Namespace, RBAC, and CRD Version

Before creating any CRs, verify you are targeting the right cluster, namespace, and that CRDs match the operator version.

### 1. Identify the operator namespace

All `yb*` CRs must be created in the namespace the operator is watching. Check the Helm release values to find it:

```bash
# Find the YBA Helm release
helm ls -A | grep yugaware

# Check which namespace the operator watches and RBAC settings
helm get values <release-name> -n <yba-namespace> -a | grep -E 'kubernetesOperator|rbac'
```

Key values to look for:

| Helm Value | Meaning |
|---|---|
| `yugaware.kubernetesOperatorEnabled` | Must be `true` |
| `yugaware.kubernetesOperatorNamespace` | The namespace the operator watches for CRs — create all CRs here. If this value is an empty string the operator is watching all namespaces. |
| `rbac.create` | Whether the chart created RBAC resources |

Verify your kubectl context and namespace before applying any CR:

```bash
kubectl config current-context
kubectl get ybuniverses -n <operator-namespace>  # should succeed without errors
```

### 2. Auto-created vs explicit YBProvider

The operator auto-creates a default YBProvider when **all** of the following are true:
- The universe targets the **same cluster** as the operator.
- YBA's service account has a **ClusterRoleBinding** with permissions to read node information cluster-wide.

A YBProvider CR **must be created explicitly** when:
- The universe targets a **different cluster** (multi-cluster / multi-cloud).
- YBA's RBAC is **namespace-scoped** (RoleBinding instead of ClusterRoleBinding) — the operator cannot discover node/zone topology automatically.
- You need to control zone-level settings (storage class, namespace, overrides, kubeconfig, cert-manager).

Check whether RBAC is cluster-scoped or namespace-scoped:

```bash
# Cluster-scoped: look for ClusterRoleBinding referencing the YBA service account
kubectl get clusterrolebinding -o json | \
  jq -r '.items[] | select(.subjects[]?.name == "<yba-service-account>") | .metadata.name'

# Namespace-scoped: look for RoleBinding in the YBA namespace
kubectl get rolebinding -n <yba-namespace> -o json | \
  jq -r '.items[] | select(.subjects[]?.name == "<yba-service-account>") | .metadata.name'
```

If only RoleBindings exist (no ClusterRoleBinding), the operator cannot auto-create a provider — you must create a YBProvider CR.

### 3. CRD version must match the operator version

The CRDs are specific to the operator and must be updated whenever the operator is upgraded. Mismatched CRDs can cause silent field drops or validation errors.

**Verify CRD version against YBA:**

```bash
# Get the installed YBA / operator version
helm ls -A | grep yugaware
# Example output: yba  yb-platform  1  ...  deployed  yugaware-2025.2.2  2025.2.2.1-b1

# Check when CRDs were last applied
kubectl get crd ybuniverses.operator.yugabyte.io -o jsonpath='{.metadata.annotations.meta\.helm\.sh/release-name}{" "}{.metadata.annotations.meta\.helm\.sh/release-namespace}{"\n"}'

# Compare CRD fields against the expected version
kubectl get crd ybuniverses.operator.yugabyte.io -o jsonpath='{.metadata.creationTimestamp}'
```

The CRD YAML URL contains the version — always match it to your Helm chart version:

```
https://raw.github.com/yugabyte/charts/<version>/crds/concatenated_crd.yaml
```

When upgrading, apply CRDs **before** running `helm upgrade`:

```bash
kubectl apply -f https://raw.github.com/yugabyte/charts/<new-version>/crds/concatenated_crd.yaml
helm upgrade yba yugabytedb/yugaware --version <new-version> ...
```

### 4. Quick preflight checklist

Before creating any CR, confirm:

- [ ] `kubectl config current-context` points to the cluster running the operator.
- [ ] You are targeting the namespace from `yugaware.kubernetesOperatorNamespace`.
- [ ] CRDs have been applied at the same version as the Helm chart.
- [ ] If RBAC is namespace-scoped, a YBProvider CR exists (or will be created before the universe).
- [ ] For multi-cluster universes, kubeconfig secrets exist for each remote cluster.

## CRD Dependency Order

Always create resources in this order. A resource that references another must be created after its dependency is Ready.

```
1. Release          — register the DB software version (download must succeed before universe can use it)
2. YBProvider       — define cloud type, regions, zones, storage classes, namespaces
3. YBCertificate    — (optional) configure TLS certificates before universe creation
4. YBUniverse       — create the database universe (references Release version, provider name, optional rootCA)
5. StorageConfig    — configure backup storage destination (S3, GCS, Azure, NFS)
6. Backup           — take a one-time backup (references universe + storage config)
7. BackupSchedule   — schedule recurring backups (references universe + storage config)
8. RestoreJob       — restore from a backup (references universe + backup)
9. PitrConfig       — configure point-in-time recovery (references universe)
10. DrConfig        — configure disaster recovery replication (references two universes + storage config)
11. SupportBundle   — collect diagnostic logs (references universe)
```

Resources at the same level (e.g. StorageConfig and PitrConfig) can be created in parallel once their dependencies exist.

## CRD Quick Reference

All CRDs use `apiVersion: operator.yugabyte.io/v1alpha1`.

### Release

Registers a YugabyteDB software version for use by universes. Must reach `success: true` before a universe can reference it.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `spec.config.version` | string | yes | Semver version string, e.g. `"2024.2.1.0-b1"` |
| `spec.config.downloadConfig` | object | yes | Exactly one of `http`, `s3`, or `gcs` |
| `spec.config.downloadConfig.http.paths.helmChart` | string | yes (http) | URL to the Helm chart `.tgz` |
| `spec.config.downloadConfig.http.paths.x86_64` | string | no | URL to the DB tarball |

Status: `status.success` (bool), `status.message` (string).

### YBProvider

Defines a Kubernetes cloud provider with regions, zones, and per-zone settings. Universes reference this by `metadata.name`.

| Field | Type | Required | Immutable | Notes |
|-------|------|----------|-----------|-------|
| `spec.cloudInfo.kubernetesProvider` | string enum | no | **yes** | `gke`, `eks`, `aks`, `openshift`, `custom` |
| `spec.cloudInfo.kubernetesImageRegistry` | string | no | no | e.g. `quay.io/yugabyte/yugabyte` |
| `spec.cloudInfo.kubeConfigSecret` | object | no | no | `{name, namespace}` — secret with key `kubeconfig` |
| `spec.regions[]` | array | yes (min 1) | no | List-map keyed by `code` |
| `spec.regions[].code` | string | yes | no | Region code, e.g. `us-west1` |
| `spec.regions[].zones[]` | array | yes (min 1) | no | List-map keyed by `code` |
| `spec.regions[].zones[].code` | string | yes | no | Zone code, e.g. `us-west1-a` |
| `spec.regions[].zones[].cloudInfo.kubeNamespace` | string | no | no | Namespace for DB pods in this zone |
| `spec.regions[].zones[].cloudInfo.kubernetesStorageClass` | string | no | no | StorageClass for volumes. **Confirm that StorageClass exists in cluster and allows volume expansion** |
| `spec.regions[].zones[].cloudInfo.kubePodAddressTemplate` | string | no | no | FQDN template for multi-cluster |
| `spec.regions[].zones[].cloudInfo.certManagerIssuerName` | string | no | no | Requires matching `certManagerIssuerKind` |
| `spec.regions[].zones[].cloudInfo.overrides` | object | no | no | Free-form Helm overrides per zone |

Status: `status.state` (string), `status.message` (string).

**Kubeconfig secrets** — required for every remote cluster (not needed for the cluster hosting YBA itself). The secret must contain a single key called `kubeconfig` with a complete kubeconfig YAML value. Name the secret after the target cluster (e.g. `ireland-kubeconfig`) so it can be reused across providers. Can be set at the top-level `spec.cloudInfo.kubeConfigSecret` (all zones) or per-zone (overrides top-level). YBA does not yet support short-lived token plugins — use long-lived service account tokens.

> **Full procedure for creating and rotating kubeconfig secrets for multi-cluster providers:** see [references/kubeconfig-secrets.md](references/kubeconfig-secrets.md)

**Multi-cluster service mesh** — when zones span multiple Kubernetes clusters connected via a service mesh, each zone needs Helm overrides in `cloudInfo.overrides` and a `kubePodAddressTemplate` for cross-cluster pod DNS resolution. Istio requires `createServicePerPod` + `istioCompatibility`; Cilium ClusterMesh with MCS requires `createServiceExports` + `kubernetesClusterId` (varies per zone). The pod address template may also vary per zone depending on the mesh.

> **Overrides and templates for Istio and Cilium:** see [references/multi-cluster-service-mesh.md](references/multi-cluster-service-mesh.md)

### YBCertificate

Configures TLS certificates for encryption in transit. Referenced by the universe's `spec.rootCA` field.

| Field | Type | Required | Immutable | Notes |
|-------|------|----------|-----------|-------|
| `spec.certType` | string enum | yes | no | `SELF_SIGNED` or `K8S_CERT_MANAGER` |
| `spec.certificateSecretRef.name` | string | yes | no | Secret name (non-empty) |
| `spec.certificateSecretRef.namespace` | string | no | no | Defaults to CR namespace |

Secret contents: `SELF_SIGNED` requires both `ca.crt` and `ca.key`; `K8S_CERT_MANAGER` requires only `ca.crt`.

### YBUniverse

The primary CRD. Creates and manages a YugabyteDB database cluster.

**Core fields:**

| Field | Type | Required | Immutable | Default | Notes |
|-------|------|----------|-----------|---------|-------|
| `spec.universeName` | string | no | **yes** | — | Display name in YBA UI |
| `spec.numNodes` | integer | yes | no | — | Total tserver count; must be >= `replicationFactor` |
| `spec.replicationFactor` | integer | yes | **yes** | — | Data replication count (typically 3) |
| `spec.ybSoftwareVersion` | string | yes | no | — | Must match a registered Release version |
| `spec.providerName` | string | no | **yes** | — | Name of a YBProvider CR |
| `spec.enableYSQL` | bool | no | **yes** | `true` | Enable PostgreSQL-compatible API |
| `spec.enableYCQL` | bool | no | **yes** | `false` | Enable Cassandra-compatible API |
| `spec.enableYSQLAuth` | bool | no | **yes** | `false` | Requires `ysqlPassword.secretName` |
| `spec.enableYCQLAuth` | bool | no | **yes** | `false` | Requires `ycqlPassword.secretName` |
| `spec.enableNodeToNodeEncrypt` | bool | no | **yes** | `true` | TLS between DB nodes |
| `spec.enableClientToNodeEncrypt` | bool | no | **yes** | `true` | TLS for client connections |
| `spec.rootCA` | string | no | **yes** | — | Name of a YBCertificate CR |
| `spec.enableLoadBalancer` | bool | no | **yes** | `false` | Creates `Type:LoadBalancer` services |
| `spec.enableIPV6` | bool | no | **yes** | `false` | — |
| `spec.paused` | bool | no | no | `false` | Scales pods to 0 when true |

**Placement (optional, available v2025.2+):**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `spec.placementInfo.defaultRegion` | string | no (immutable once set) | Location of masters. The region must have at least 3  numNodes across its zones, or this field be excluded from the resource to spread master pods across all regions. Must match a region code if specified. |
| `spec.placementInfo.regions[].code` | string | yes | Region code from provider |
| `spec.placementInfo.regions[].zones[].code` | string | yes | Zone code from provider |
| `spec.placementInfo.regions[].zones[].numNodes` | integer | yes | Nodes in this zone |
| `spec.placementInfo.regions[].zones[].preferred` | bool | no | Default `true` |

**Storage:**

| Field | Type | Required | Immutable | Default | Notes |
|-------|------|----------|-----------|---------|-------|
| `spec.deviceInfo.volumeSize` | integer | no | no | `100` | GB per volume (tserver) |
| `spec.deviceInfo.numVolumes` | integer | no | **yes** | `1` | Volumes per tserver |
| `spec.deviceInfo.storageClass` | string | no | **yes** | — | Kubernetes StorageClass |
| `spec.masterDeviceInfo.volumeSize` | integer | no | no | `50` | GB per volume (master) |
| `spec.masterDeviceInfo.numVolumes` | integer | no | **yes** | `1` | Volumes per master |
| `spec.masterDeviceInfo.storageClass` | string | no | **yes** | — | Kubernetes StorageClass |

**GFlags:**

| Field | Type | Notes |
|-------|------|-------|
| `spec.gFlags.tserverGFlags` | map[string]string | Tserver config flags |
| `spec.gFlags.masterGFlags` | map[string]string | Master config flags |
| `spec.gFlags.perAZ` | map[string]object | Per-AZ overrides with `tserverGFlags`/`masterGFlags` |

**Kubernetes overrides** (`spec.kubernetesOverrides`): free-form object passed to the Helm chart. Common sub-fields:

| Path | Type | Notes |
|------|------|-------|
| `resource.master.requests.cpu` | string/int | e.g. `2` or `"2"` |
| `resource.master.requests.memory` | string/int | e.g. `"8Gi"` |
| `resource.master.limits.cpu` | string/int | — |
| `resource.master.limits.memory` | string/int | — |
| `resource.tserver.requests.*` | string/int | Same structure as master |
| `resource.tserver.limits.*` | string/int | — |
| `master.affinity` | object | Pod/node affinity rules |
| `tserver.affinity` | object | Pod/node affinity rules |
| `master.tolerations` | array | Node tolerations |
| `tserver.tolerations` | array | Node tolerations |
| `master.podAnnotations` | map[string]string | — |
| `tserver.podAnnotations` | map[string]string | — |
| `master.podLabels` | map[string]string | — |
| `tserver.podLabels` | map[string]string | — |
| `master.extraEnv` | array of `{name, value}` | — |
| `tserver.extraEnv` | array of `{name, value}` | — |
| `tserver.serviceAccount` | string | KSA for IAM-based backup access |
| `serviceEndpoints` | array | Custom service definitions |
| `nodeSelector` | map[string]string | Node selector |

**Read Replica** (`spec.readReplica`): optional second cluster for read scaling.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `spec.readReplica.numNodes` | integer | yes | — |
| `spec.readReplica.replicationFactor` | integer | yes | — |
| `spec.readReplica.deviceInfo` | object | yes | Same shape as `spec.deviceInfo` |
| `spec.readReplica.placementInfo` | object | no | Same shape as `spec.placementInfo` |

Status: `status.universeState` (string: `Ready`, `Creating`, `Editing`, `Deleting`), `status.sqlEndpoints` (array), `status.cqlEndpoints` (array).

**Password secrets** — when `enableYSQLAuth` or `enableYCQLAuth` is true, create a Kubernetes Secret with a `ysqlPassword` or `ycqlPassword` key, then reference it:
```yaml
spec:
  enableYSQLAuth: true
  ysqlPassword:
    secretName: my-ysql-password
```
Ensure that the password is at least 12 characters and includes numbers, upper case letters, lower case letters and special character. Put the password in the same namespace as the `ybUniverse` resource.

### StorageConfig

Defines a backup storage destination. Referenced by Backup, BackupSchedule, and DrConfig CRs.

| Field | Type | Required | Immutable | Notes |
|-------|------|----------|-----------|-------|
| `spec.config_type` | string enum | yes | **yes** | `STORAGE_S3`, `STORAGE_GCS`, `STORAGE_AZ`, `STORAGE_NFS` |
| `spec.name` | string | no | **yes** | Display name |
| `spec.data.BACKUP_LOCATION` | string | yes | **yes** | Bucket path, e.g. `s3://bucket/path` |
| `spec.data.AWS_ACCESS_KEY_ID` | string | no | no | For S3 |
| `spec.data.AWS_SECRET_ACCESS_KEY` | string | no | no | Deprecated — use `awsSecretAccessKeySecret` |
| `spec.data.USE_IAM` | bool | no | no | IAM-based access for S3/GCS |
| `spec.data.GCS_CREDENTIALS_JSON` | string | no | no | Deprecated — use `gcsCredentialsJsonSecret` |
| `spec.data.AZURE_STORAGE_SAS_TOKEN` | string | no | no | Deprecated — use `azureStorageSasTokenSecret` |
| `spec.data.AWS_HOST_BASE` | string | no | no | S3-compatible endpoint |
| `spec.data.PATH_STYLE_ACCESS` | bool | no | no | For S3-compatible |
| `spec.data.SIGNING_REGION` | string | no | no | For private S3 endpoints |
| `spec.awsSecretAccessKeySecret` | object | no | no | `{name, namespace}` — preferred over inline. The referred secret must have a value in key `awsSecretAccessKey` |
| `spec.gcsCredentialsJsonSecret` | object | no | no | `{name, namespace}` — preferred over inline. The referred secret must have a value in key `gcsCredentialsJson` |
| `spec.azureStorageSasTokenSecret` | object | no | no | `{name, namespace}` — preferred over inline. The referred secret must have a value in key `azureStorageSasToken` |

### Backup

Takes a one-time backup of a universe keyspace.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `spec.backupType` | string enum | yes | `PGSQL_TABLE_TYPE` (YSQL) or `YQL_TABLE_TYPE` (YCQL) |
| `spec.universe` | string | yes | Name of a YBUniverse CR |
| `spec.storageConfig` | string | yes | Name of a StorageConfig CR |
| `spec.keyspace` | string | yes | Database/keyspace name |
| `spec.timeBeforeDelete` | integer | no | Auto-delete after N milliseconds (min 0) |
| `spec.tableByTableBackup` | bool | no | Backup tables individually |
| `spec.sse` | bool | no | Server-side encryption |
| `spec.incrementalBackupBase` | string | no | Base Backup CR name for incremental |

### BackupSchedule

Schedules recurring backups. Must set either `schedulingFrequency` or `cronExpression`.

| Field | Type | Required | Immutable | Notes |
|-------|------|----------|-----------|-------|
| `spec.backupType` | string enum | yes | **yes** | `PGSQL_TABLE_TYPE` or `YQL_TABLE_TYPE` |
| `spec.universe` | string | yes | **yes** | Name of a YBUniverse CR |
| `spec.storageConfig` | string | yes | **yes** | Name of a StorageConfig CR |
| `spec.keyspace` | string | yes | **yes** | Database/keyspace name |
| `spec.schedulingFrequency` | integer | conditional | no | Milliseconds between full backups (min 3600000 = 1hr) |
| `spec.cronExpression` | string | conditional | no | Cron schedule for full backups |
| `spec.incrementalBackupFrequency` | integer | no | no | Milliseconds between incremental backups (min 0) |
| `spec.timeBeforeDelete` | integer | no | **yes** | Auto-delete after N ms |
| `spec.tableByTableBackup` | bool | no | **yes** | — |
| `spec.enablePointInTimeRestore` | bool | no | **yes** | Enable PITR from scheduled backups |

Auto-deletes when owning universe is deleted.

### RestoreJob

Restores data from an existing Backup CR.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `spec.actionType` | string enum | yes | Must be `RESTORE` |
| `spec.universe` | string | yes | Target YBUniverse CR name |
| `spec.backup` | string | yes | Source Backup CR name |
| `spec.keyspace` | string | no | Keyspace override |

### PitrConfig

Configures point-in-time recovery for a database.

| Field | Type | Required | Immutable | Default | Notes |
|-------|------|----------|-----------|---------|-------|
| `spec.universe` | string | yes | **yes** | — | YBUniverse CR name |
| `spec.name` | string | yes | no | — | Config display name |
| `spec.tableType` | string enum | yes | no | — | `YSQL` or `YCQL` |
| `spec.database` | string | yes | no | — | Database/keyspace name |
| `spec.intervalInSeconds` | integer | no | no | `86400` | Snapshot interval (min 0) |
| `spec.retentionPeriodInSeconds` | integer | no | no | `604800` | Retention window (min 0) |

Validation: `retentionPeriodInSeconds` must be > `intervalInSeconds`.

### DrConfig

Configures disaster recovery replication between two universes.

| Field | Type | Required | Immutable | Notes |
|-------|------|----------|-----------|-------|
| `spec.name` | string | yes | no | DR config display name |
| `spec.sourceUniverse` | string | yes | **yes** | Source YBUniverse CR name |
| `spec.targetUniverse` | string | yes | **yes** | Target YBUniverse CR name (must differ from source) |
| `spec.databases[]` | string array | yes (min 1) | no | Database names to replicate (non-empty strings) |
| `spec.storageConfig` | string | yes | no | StorageConfig CR name |

### SupportBundle

Collects diagnostic logs and metadata from a universe.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `spec.universeName` | string | yes | YBUniverse CR name |
| `spec.collectionTimerange.startDate` | string (date-time) | yes | ISO 8601, e.g. `2024-01-15T00:00:00Z` |
| `spec.collectionTimerange.endDate` | string (date-time) | no | Defaults to now |
| `spec.components[]` | string enum array | no | Subset of: `UniverseLogs`, `ApplicationLogs`, `OutputFiles`, `ErrorFiles`, `CoreFiles`, `GFlags`, `Instance`, `ConsensusMeta`, `TabletMeta`, `YbcLogs`, `K8sInfo` |

Status: `status.status` (enum: `generating`, `ready`, `failed`), `status.access` (download URL).

## Immutable Fields

Many fields are locked after initial creation. Attempting to change an immutable field causes a validation error. Key immutable fields:

- **YBUniverse:** `universeName`, `replicationFactor`, `providerName`, `enableYSQL`, `enableYCQL`, `enableYSQLAuth`, `enableYCQLAuth`, `enableNodeToNodeEncrypt`, `enableClientToNodeEncrypt`, `rootCA`, `enableLoadBalancer`, `enableIPV6`, `deviceInfo.numVolumes`, `deviceInfo.storageClass`, `masterDeviceInfo.numVolumes`, `masterDeviceInfo.storageClass`
- **YBProvider:** `cloudInfo.kubernetesProvider`
- **StorageConfig:** `config_type`, `name`, `data.BACKUP_LOCATION`
- **DrConfig:** `sourceUniverse`, `targetUniverse`
- **PitrConfig:** `universe`
- **BackupSchedule:** `backupType`, `universe`, `storageConfig`, `keyspace`, `tableByTableBackup`, `timeBeforeDelete`, `enablePointInTimeRestore`, `name`

## What Can Be Changed After Creation

- **Scale:** Change `spec.numNodes` (and `placementInfo` zone counts) to add/remove tservers.
- **Upgrade:** Change `spec.ybSoftwareVersion` to trigger a rolling upgrade (register the new Release first).
- **GFlags:** Update `spec.gFlags.tserverGFlags` / `masterGFlags` for live config changes.
- **Volume size:** Increase `spec.deviceInfo.volumeSize` or `spec.masterDeviceInfo.volumeSize` (never decrease).
- **Kubernetes overrides:** Update resource requests/limits, affinity, tolerations, annotations, labels.
- **Pause/resume:** Toggle `spec.paused`.
- **Backup schedule frequency:** Update `schedulingFrequency`, `cronExpression`, or `incrementalBackupFrequency`.

## Anti-Patterns

| Anti-Pattern | Consequence | Do Instead |
|-------------|-------------|------------|
| Creating CRs in a namespace the operator is not watching | CRs are ignored silently — nothing happens | Check `yugaware.kubernetesOperatorNamespace` in Helm values |
| Using CRDs from a different version than the operator | Fields silently dropped or validation errors | Always apply CRDs from the same version as the Helm chart before upgrading |
| Omitting a YBProvider when RBAC is namespace-scoped | Universe creation fails — operator cannot discover topology | Check for ClusterRoleBinding; if only RoleBindings exist, create an explicit YBProvider |
| Creating a YBUniverse before its Release is `success: true` | Universe creation fails or hangs | Wait for `kubectl get release <version>` to show `Downloaded: true` |
| Setting `replicationFactor` > `numNodes` | Rejected by validation | Ensure `replicationFactor <= numNodes` |
| Trying to change immutable fields via `kubectl edit` | Admission webhook rejects the update | Delete and recreate the CR (data loss for universes) |
| Putting credentials directly in StorageConfig `spec.data` | Secrets visible in CR YAML | Use `awsSecretAccessKeySecret` / `gcsCredentialsJsonSecret` / `azureStorageSasTokenSecret` |
| Deleting an incremental backup individually | Breaks the backup chain | Delete the base full backup (cascades to all incrementals) |
| Creating DrConfig with `sourceUniverse == targetUniverse` | Rejected by validation | Use two distinct universe names |
| Setting `PitrConfig.retentionPeriodInSeconds <= intervalInSeconds` | Rejected by validation | Retention must be strictly greater than interval |
| Missing `schedulingFrequency` and `cronExpression` on BackupSchedule | Rejected by validation | Set at least one; `schedulingFrequency` minimum is 3600000 ms |
| Using short-lived tokens in kubeconfig secrets | YBA lacks auth plugin support (as of 2025.2.2.1); token expires silently | Use long-lived service account tokens (`kubernetes.io/service-account-token` Secret) |
| Creating a kubeconfig secret without the `kubeconfig` key | Provider fails to connect to the remote cluster | Secret must contain exactly one key named `kubeconfig` |

## Installation

Apply CRDs then install YBA with operator enabled:

```bash
kubectl apply -f https://raw.github.com/yugabyte/charts/<version>/crds/concatenated_crd.yaml

helm install yba yugabytedb/yugaware \
  --version <version> \
  --namespace yb-platform \
  --set yugaware.kubernetesOperatorEnabled=true \
  --set yugaware.kubernetesOperatorNamespace='<operator-namespace>' \
  --set yugaware.defaultUser.enabled=true \
  --set yugaware.defaultUser.username='<username>' \
  --set yugaware.defaultUser.email='<email>' \
  --set yugaware.defaultUser.password='<password>'
```

To upgrade an existing YBA:
```bash
kubectl apply -f https://raw.github.com/yugabyte/charts/<version>/crds/concatenated_crd.yaml

helm upgrade yba yugabytedb/yugaware --version <version> \
  --set yugaware.kubernetesOperatorEnabled=true \
  --set yugaware.kubernetesOperatorNamespace='<operator-namespace>'
```

Verify:
```bash
kubectl get pods -n <yba-namespace>
kubectl get pods -n <operator-namespace>
```

## Inspecting CRDs

Use `kubectl explain` to explore any CRD field at runtime:

```bash
kubectl explain ybuniverse.spec
kubectl explain ybuniverse.spec.gFlags
kubectl explain ybprovider.spec.regions
kubectl explain backupschedules.spec
```

## Current Limitations

- Single Kubernetes cluster only — no multi-cluster universes.
- No software upgrade rollback.
- No xCluster replication support (use DrConfig for DR).
- Read Replica is defined in the CRD but not fully supported via the operator.
- Encryption-at-rest is not supported.
- Only self-signed encryption in transit; editing TLS config after creation is not supported.
- PITR supports only declarative operations (create, update databases, delete) — imperative restore from PITR is not yet available.

## Workflow Summaries

> **Detailed step-by-step workflows with full YAML:** see [references/workflows.md](references/workflows.md)
> **Complete YAML examples for every CRD:** see [references/crd-examples.md](references/crd-examples.md)

### Provision a New Universe

1. Apply CRDs and install YBA with operator enabled.
2. Create a `Release` CR for the desired DB version. Wait for `status.success: true`.
3. Create a `YBProvider` CR defining regions, zones, storage classes.
4. (Optional) Create a `YBCertificate` CR for TLS.
5. Create the `YBUniverse` CR referencing the provider name and software version.
6. Monitor with `kubectl get ybuniverse -w` until `STATE` is `Ready`.

### Upgrade DB Software

1. Create a new `Release` CR for the target version.
2. Wait for `status.success: true`.
3. `kubectl edit ybuniverse <name>` — change `spec.ybSoftwareVersion` to the new version.
4. Monitor rolling upgrade with `kubectl get ybuniverse -w`.

### Set Up Backups

1. Create a `StorageConfig` CR pointing to your bucket.
2. For one-time: create a `Backup` CR referencing the universe and storage config.
3. For scheduled: create a `BackupSchedule` CR with frequency or cron expression.
4. For incremental: set `incrementalBackupFrequency` on the schedule, or `incrementalBackupBase` on a manual Backup.

### Restore from Backup

1. Create a `RestoreJob` CR with `actionType: RESTORE`, referencing the target universe and source backup.

### Configure Disaster Recovery

1. Ensure two universes exist and are `Ready`.
2. Create a `StorageConfig` CR.
3. Create a `DrConfig` CR with source/target universe names, database list, and storage config.
