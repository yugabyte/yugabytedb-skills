---
name: operator
description: Use when provisioning, managing, or troubleshooting YugabyteDB universes on Kubernetes via the YugabyteDB Kubernetes Operator and YugabyteDB Anywhere CRDs (YBUniverse, YBProvider, Release, Backup, StorageConfig, PitrRestore, DrConfig, etc.). Triggers on kubectl apply, Helm install of yugaware, operator CRDs, or any mention of YugabyteDB with Kubernetes.
---

# YugabyteDB Kubernetes Operator

**This skill includes:**
- `references/crd-examples.md` — complete YAML examples for every CRD
- `references/workflows.md` — end-to-end provisioning, upgrade, backup, and DR workflows
- `references/kubeconfig-secrets.md` — create and rotate kubeconfig secrets for multi-cluster providers. **Always follow this guidance; do not create generic kubeconfig secrets.**
- `references/multi-cluster-service-mesh.md` — Helm overrides and pod address templates for Istio and Cilium

**Note:** This skill covers the YBA Kubernetes Operator (commercial). It does not fully apply to the YugabyteDB OSS Kubernetes Operator, which has fewer CRDs and features.

The operator manages YBA universes declaratively via Kubernetes CRs alongside YBA, watching a configured namespace for changes.

CRD source: `https://raw.github.com/yugabyte/charts/<version>/crds/concatenated_crd.yaml`
Documentation: `https://docs.yugabyte.com/stable/yugabyte-platform/anywhere-automation/yb-kubernetes-operator/`

## Prerequisites

**Before creating any CRs:**

- [ ] `kubectl config current-context` points to the cluster running the operator.
- [ ] You are targeting the namespace from `yugaware.kubernetesOperatorNamespace` (empty string = all namespaces).
- [ ] CRDs applied at the **same version** as the Helm chart (`helm ls -A | grep yugaware`).
- [ ] If RBAC is namespace-scoped (RoleBinding only, no ClusterRoleBinding), a YBProvider CR must be created explicitly — the operator cannot auto-discover node/zone topology.
- [ ] For multi-cluster universes, kubeconfig secrets exist for each remote cluster.

**Auto-created vs explicit YBProvider:** The operator auto-creates a provider only when the universe targets the same cluster as the operator AND YBA has a ClusterRoleBinding. Create a YBProvider CR explicitly when targeting a different cluster, when RBAC is namespace-scoped, or when you need per-zone control (storage class, namespace, overrides, kubeconfig, cert-manager).

**CRD upgrades:** Apply CRDs before `helm upgrade` — mismatched CRDs cause silent field drops or validation errors:
```bash
kubectl apply -f https://raw.github.com/yugabyte/charts/<new-version>/crds/concatenated_crd.yaml
helm upgrade yba yugabytedb/yugaware --version <new-version> ...
```

## CRD Dependency Order

Create resources in this order; each must be Ready before dependents are created.

```
1. Release       — register DB software version
2. YBProvider    — define cloud/regions/zones (optional if auto-provider is available)
3. YBCertificate — (optional) TLS certificates
4. YBUniverse    — database cluster
5. StorageConfig — backup storage destination
6. Backup / BackupSchedule / RestoreJob — backup operations
7. PitrConfig    — point-in-time recovery config
8. PitrRestore   — PITR restore operation [v2026.1+]
9. DrConfig      — disaster recovery replication
10. SupportBundle — diagnostic log collection
```

## CRD Quick Reference

All CRDs use `apiVersion: operator.yugabyte.io/v1alpha1`.

### Release

Registers a YugabyteDB software version. Must reach `status.success: true` before a universe can reference it.

The CRD validation (v2026.1+) requires exactly one `helmChart` path and exactly one `x86_64` path across the combination of `http`, `s3`, and `gcs` — they can come from different sources (e.g. `http` for the Helm chart and `s3` for the tarball). In pre-2026.1 CRDs, `x86_64` was optional.

**Helm chart URL format:** The chart filename uses the 3-part chart version, not the full DB version string — `https://charts.yugabyte.com/yugabyte-<major>.<minor>.<patch>.tgz`. For example, DB version `2025.2.3.0-b149` uses chart version `2025.2.3`, so `helmChart: "https://charts.yugabyte.com/yugabyte-2025.2.3.tgz"`. Confirm chart existence with `helm search repo yugabytedb/yugabyte --versions` before authoring the CR.

**`kubernetesOperatorNamespace: ""` bug:** When the operator watches all namespaces (empty string), `ReleaseReconciler.onAdd` throws `namespace not specified` when patching the CR status. The exception fires between YBA registration and the download trigger, so:
- The release appears in YBA metadata but **packages are never downloaded** — the release shows as incomplete in YBA and cannot be used to upgrade a universe.
- Subsequent reconcile loops log `no changes found` and do not retry the download.
- Releases that **fail YBA registration** (bad URL, non-existent version) are re-processed via the update path, which does write status correctly.

**Workaround:** Set `kubernetesOperatorNamespace` to the specific namespace where universes run instead of `""`. With a namespace set, the `onAdd` status patch succeeds and the download is triggered normally.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `spec.config.version` | string | yes | e.g. `"2025.2.3.0-b149"` |
| `spec.config.downloadConfig.http.paths.helmChart` | string | yes (v2026.1+) | URL to Helm chart `.tgz` — uses 3-part chart version, not full DB version |
| `spec.config.downloadConfig.http.paths.x86_64` | string | yes (v2026.1+) | URL to DB tarball |
| `spec.config.downloadConfig.s3.accessKeyId` | string | no | S3 access key |
| `spec.config.downloadConfig.s3.secretAccessKeySecret` | object | no | `{name, namespace}` — preferred over inline secret |
| `spec.config.downloadConfig.gcs.credentialsJsonSecret` | object | no | `{name, namespace}` — preferred over inline credentials |

### YBProvider

Defines a Kubernetes cloud provider. Universes reference this by `metadata.name`.

| Field | Type | Required | Immutable | Notes |
|-------|------|----------|-----------|-------|
| `spec.cloudInfo.kubernetesProvider` | string enum | no | **yes** | `gke`, `eks`, `aks`, `openshift`, `custom` |
| `spec.cloudInfo.kubernetesImageRegistry` | string | no | no | e.g. `quay.io/yugabyte/yugabyte` |
| `spec.cloudInfo.kubeConfigSecret` | object | no | no | `{name, namespace}` — global kubeconfig secret |
| `spec.regions[].code` | string | yes | no | Region code, e.g. `us-west1` |
| `spec.regions[].zones[].code` | string | yes | no | Zone code, e.g. `us-west1-a` |
| `spec.regions[].zones[].cloudInfo.kubeNamespace` | string | no | no | Namespace for DB pods in this zone |
| `spec.regions[].zones[].cloudInfo.kubernetesStorageClass` | string | no | no | StorageClass — confirm it exists and allows volume expansion |
| `spec.regions[].zones[].cloudInfo.kubeDomain` | string | no | no | Cluster domain name [v2026.1+] |
| `spec.regions[].zones[].cloudInfo.kubePodAddressTemplate` | string | no | no | FQDN template for multi-cluster |
| `spec.regions[].zones[].cloudInfo.certManagerIssuerName` | string | no | no | Requires `certManagerIssuerKind` |
| `spec.regions[].zones[].cloudInfo.certManagerIssuerGroup` | string | no | no | Issuer group for cert-manager |
| `spec.regions[].zones[].cloudInfo.kubeConfigSecret` | object | no | no | Per-zone kubeconfig override |
| `spec.regions[].zones[].cloudInfo.overrides` | object | no | no | Free-form Helm overrides per zone |

Status: `status.state`, `status.message`.

Kubeconfig secrets must contain a single key `kubeconfig` with complete kubeconfig YAML. Use long-lived service account tokens — YBA does not support short-lived token plugins. See [references/kubeconfig-secrets.md](references/kubeconfig-secrets.md) for the full procedure.

For multi-cluster service mesh (Istio / Cilium), see [references/multi-cluster-service-mesh.md](references/multi-cluster-service-mesh.md).

### YBCertificate

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `spec.certType` | string enum | yes | `SELF_SIGNED` or `K8S_CERT_MANAGER` |
| `spec.certificateSecretRef.name` | string | yes | Secret name (`SELF_SIGNED` needs `ca.crt` + `ca.key`; `K8S_CERT_MANAGER` needs `ca.crt` only) |
| `spec.certificateSecretRef.namespace` | string | no | Defaults to CR namespace |

### YBUniverse

The primary CRD. Creates and manages a YugabyteDB database cluster.

**Core fields:**

| Field | Type | Required | Immutable | Default | Notes |
|-------|------|----------|-----------|---------|-------|
| `spec.universeName` | string | no | **yes** | — | Display name in YBA UI |
| `spec.numNodes` | integer | yes | no | — | Total tserver count; must be ≥ `replicationFactor` |
| `spec.replicationFactor` | integer | yes | **yes** | — | Typically 3 |
| `spec.ybSoftwareVersion` | string | yes | no | — | Must match a registered Release |
| `spec.providerName` | string | no | **yes** | — | YBProvider CR name |
| `spec.zoneFilter` | string array | no | **yes** | — | Filter zones used by auto-provider (only when `providerName` unset) [v2026.1+] |
| `spec.enableYSQL` | bool | no | **yes** | `true` | YSQL (PostgreSQL-compatible) |
| `spec.enableYCQL` | bool | no | **yes** | `false` | YCQL (Cassandra-compatible) |
| `spec.enableYSQLAuth` | bool | no | **yes** | `false` | Requires `ysqlPassword.secretName` |
| `spec.enableYCQLAuth` | bool | no | **yes** | `false` | Requires `ycqlPassword.secretName` |
| `spec.enableNodeToNodeEncrypt` | bool | no | **yes** | `true` | TLS between nodes |
| `spec.enableClientToNodeEncrypt` | bool | no | **yes** | `true` | TLS for client connections |
| `spec.rootCA` | string | no | **yes** | — | YBCertificate CR name |
| `spec.enableLoadBalancer` | bool | no | **yes** | `false` | Creates `Type:LoadBalancer` services |
| `spec.enableIPV6` | bool | no | **yes** | `false` | — |
| `spec.paused` | bool | no | no | `false` | Scales pods to 0 |
| `spec.useYbdbInbuiltYbc` | bool | no | no | `false` | YBC runs from the DB container image rather than installed at runtime [v2026.1+] |

Password secrets: create a Kubernetes Secret with key `ysqlPassword` or `ycqlPassword` (≥12 chars, mixed case, numbers, special chars) in the same namespace as the YBUniverse, then reference via `spec.ysqlPassword.secretName` / `spec.ycqlPassword.secretName`. Both fields are immutable once set.

**Placement (v2025.2+):**

| Field | Type | Notes |
|-------|------|-------|
| `spec.placementInfo.defaultRegion` | string | Immutable once set. Location of masters; omit to spread masters across all regions. |
| `spec.placementInfo.regions[].code` | string | Region code from provider |
| `spec.placementInfo.regions[].zones[].code` | string | Zone code from provider |
| `spec.placementInfo.regions[].zones[].numNodes` | integer | Nodes in this zone |
| `spec.placementInfo.regions[].zones[].preferred` | bool | Default `true` |

**Storage — v2026.1+: `tserverVolume` / `masterVolume` (preferred):**

Replaces `deviceInfo`/`masterDeviceInfo`; the two sets are mutually exclusive. `numVolumes` and `storageClass` are mutable when running DB ≥ 2026.1.0. Provider-level storage class takes precedence over `storageClass` here — leave the provider value empty to use this field. Once set, `tserverVolume`/`masterVolume` cannot be removed; `perAZ` within them cannot be removed once set.

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `spec.tserverVolume.volumeSize` | integer | `100` | GiB per volume |
| `spec.tserverVolume.numVolumes` | integer | `2` | Mutable with DB ≥ 2026.1.0 |
| `spec.tserverVolume.storageClass` | string | — | Mutable with DB ≥ 2026.1.0 |
| `spec.tserverVolume.perAZ` | map[string]object | — | Per-AZ overrides: `volumeSize`, `numVolumes`, `storageClass` |
| `spec.masterVolume.volumeSize` | integer | `50` | GiB per volume |
| `spec.masterVolume.numVolumes` | integer | `1` | Mutable with DB ≥ 2026.1.0 |
| `spec.masterVolume.storageClass` | string | — | Mutable with DB ≥ 2026.1.0 |
| `spec.masterVolume.perAZ` | map[string]object | — | Per-AZ overrides |

**Storage — legacy: `deviceInfo` / `masterDeviceInfo` (deprecated v2026.1+):**

| Field | Type | Immutable | Default |
|-------|------|-----------|---------|
| `spec.deviceInfo.volumeSize` | integer | no | `100` |
| `spec.deviceInfo.numVolumes` | integer | **yes** | `1` |
| `spec.deviceInfo.storageClass` | string | **yes** | — |
| `spec.masterDeviceInfo.volumeSize` | integer | no | `50` |
| `spec.masterDeviceInfo.numVolumes` | integer | **yes** | `1` |
| `spec.masterDeviceInfo.storageClass` | string | **yes** | — |

**Resource Spec (v2026.1+):** Simple CPU/memory sizing as an alternative to `kubernetesOverrides.resource`. Values are integers (cores / GiB, min 1 each). When `kubernetesOverrides.resource` is already set on an existing universe, adding these fields is accepted by Kubernetes but the operator reconciles as NO_OP — no YBA update is triggered and sizing does not change. These fields only take effect on universes created without `kubernetesOverrides.resource`.

| Field | Notes |
|-------|-------|
| `spec.tserverResourceSpec.cpu` / `.memory` | CPU cores / GiB for tserver pods |
| `spec.masterResourceSpec.cpu` / `.memory` | CPU cores / GiB for master pods |

**YBC Throttle Parameters (v2026.1+):** Tune backup/restore throughput when backups consume too many resources. `maxConcurrentUploads` and `maxConcurrentDownloads` have a hard maximum of **3** — values above this cause `Error Updating` at reconcile time (the admission webhook does not validate them).

| Field | Notes |
|-------|-------|
| `spec.ybcThrottleParameters.maxConcurrentUploads` / `maxConcurrentDownloads` | Parallel uploads/downloads per node |
| `spec.ybcThrottleParameters.perUploadNumObjects` / `perDownloadNumObjects` | Buffers per upload/download per node |
| `spec.ybcThrottleParameters.diskReadBytesPerSec` / `diskWriteBytesPerSec` | Disk I/O rate limits during backup/restore |

**GFlags:**

| Field | Notes |
|-------|-------|
| `spec.gFlags.tserverGFlags` | map[string]string |
| `spec.gFlags.masterGFlags` | map[string]string |
| `spec.gFlags.perAZ` | map[string]object with `tserverGFlags`/`masterGFlags` |

**Kubernetes overrides** (`spec.kubernetesOverrides`): free-form object passed to the Helm chart.

| Path | Notes |
|------|-------|
| `resource.master.requests.cpu` / `.memory` | e.g. `4`, `"8Gi"` |
| `resource.master.limits.cpu` / `.memory` | — |
| `resource.tserver.requests.*` / `limits.*` | Same shape as master |
| `master.affinity` / `tserver.affinity` | Pod/node affinity |
| `master.tolerations` / `tserver.tolerations` | Node tolerations |
| `master.podAnnotations` / `.podLabels` | — |
| `tserver.podAnnotations` / `.podLabels` | — |
| `master.extraEnv` / `tserver.extraEnv` | `[{name, value}]` |
| `master.secretEnv` / `tserver.secretEnv` | `[{name, secretKeyRef: {name, key}}]` [v2026.1+] |
| `master.extraVolumes` / `tserver.extraVolumes` | `[{name, persistentVolumeClaim: {claimName}}]` [v2026.1+] |
| `master.extraVolumeMounts` / `tserver.extraVolumeMounts` | `[{name, mountPath}]` [v2026.1+] |
| `tserver.serviceAccount` | KSA for IAM-based backup access |
| `serviceEndpoints` | Custom service definitions |
| `nodeSelector` | map[string]string |

**Read Replica** (`spec.readReplica`):

| Field | Required | Notes |
|-------|----------|-------|
| `numNodes` | yes | — |
| `replicationFactor` | yes | — |
| `tserverVolume` | no | v2026.1+ preferred; same shape as primary. Cannot be removed once set. |
| `tserverResourceSpec` | no | `{cpu, memory}` [v2026.1+] |
| `deviceInfo` | no | Deprecated v2026.1+; mutually exclusive with `tserverVolume` |
| `placementInfo` | no | Same shape as primary `placementInfo` |

Status: `status.universeState` (`Ready`, `Creating`, `Editing`, `Deleting`), `status.sqlEndpoints`, `status.cqlEndpoints`, `status.resourceUUID`. `status.actions` (v2026.1+): array of `{action_type, message, taskUUID, status}` where status is `queued`, `running`, or `failed`.

### StorageConfig

Defines a backup storage destination. Referenced by Backup, BackupSchedule, and DrConfig.

| Field | Type | Required | Immutable | Notes |
|-------|------|----------|-----------|-------|
| `spec.config_type` | string enum | yes | **yes** | `STORAGE_S3`, `STORAGE_GCS`, `STORAGE_AZ`, `STORAGE_NFS` |
| `spec.name` | string | no | **yes** | Display name |
| `spec.data.BACKUP_LOCATION` | string | yes | **yes** | e.g. `s3://bucket/path` |
| `spec.data.AWS_ACCESS_KEY_ID` | string | no | no | S3 |
| `spec.data.USE_IAM` | bool | no | no | IAM access for S3/GCS/AZ |
| `spec.data.AWS_HOST_BASE` | string | no | no | S3-compatible endpoint |
| `spec.data.PATH_STYLE_ACCESS` | bool | no | no | S3-compatible path-style |
| `spec.data.SIGNING_REGION` | string | no | no | For private S3 endpoints |
| `spec.awsSecretAccessKeySecret` | object | no | no | `{name, namespace}` — preferred over `data.AWS_SECRET_ACCESS_KEY`. Secret key: `awsSecretAccessKey`. **Secret must exist before applying the CR** — if absent, the CR is created with empty `resourceUUID` and all subsequent updates fail with "Invalid UUID string" until the CR is deleted and recreated. |
| `spec.gcsCredentialsJsonSecret` | object | no | no | `{name, namespace}` — preferred over `data.GCS_CREDENTIALS_JSON`. Secret key: `gcsCredentialsJson`. Same pre-existence requirement. |
| `spec.azureStorageSasTokenSecret` | object | no | no | `{name, namespace}` — preferred over `data.AZURE_STORAGE_SAS_TOKEN`. Secret key: `azureStorageSasToken`. Same pre-existence requirement. |

### Backup

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `spec.backupType` | string enum | yes | `PGSQL_TABLE_TYPE` (YSQL) or `YQL_TABLE_TYPE` (YCQL) |
| `spec.universe` | string | yes | YBUniverse CR name |
| `spec.storageConfig` | string | yes | StorageConfig CR name |
| `spec.keyspace` | string | yes | Database/keyspace name |
| `spec.timeBeforeDelete` | integer | no | Auto-delete after N ms |
| `spec.tableByTableBackup` | bool | no | — |
| `spec.sse` | bool | no | Server-side encryption |
| `spec.incrementalBackupBase` | string | no | Base Backup CR name for incremental |

### BackupSchedule

Must set either `schedulingFrequency` or `cronExpression`.

| Field | Type | Required | Immutable | Notes |
|-------|------|----------|-----------|-------|
| `spec.backupType` | string enum | yes | **yes** | `PGSQL_TABLE_TYPE` or `YQL_TABLE_TYPE` |
| `spec.universe` | string | yes | **yes** | YBUniverse CR name |
| `spec.storageConfig` | string | yes | **yes** | StorageConfig CR name |
| `spec.keyspace` | string | yes | **yes** | Database/keyspace name |
| `spec.name` | string | no | **yes** | Display name |
| `spec.schedulingFrequency` | integer | conditional | no | ms between full backups (min 3600000) |
| `spec.cronExpression` | string | conditional | no | Cron schedule for full backups |
| `spec.incrementalBackupFrequency` | integer | no | no | ms between incremental backups |
| `spec.timeBeforeDelete` | integer | no | **yes** | Auto-delete after N ms |
| `spec.tableByTableBackup` | bool | no | **yes** | — |
| `spec.enablePointInTimeRestore` | bool | no | **yes** | Default `false` |

Auto-deletes when owning universe is deleted.

### RestoreJob

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `spec.actionType` | string enum | yes | Must be `RESTORE` |
| `spec.universe` | string | yes | Target YBUniverse CR name |
| `spec.backup` | string | yes | Source Backup CR name |
| `spec.keyspace` | string | no | Keyspace override |

### PitrConfig

**`kubernetesOperatorNamespace: ""` bug:** `PitrConfigReconciler` passes an empty namespace when looking up the referenced YBUniverse, causing a permanent `Unable to fetch YBUniverse` loop. PitrConfig will never be created when the operator watches all namespaces. Set `kubernetesOperatorNamespace` to a specific namespace to work around this.

| Field | Type | Required | Immutable | Default | Notes |
|-------|------|----------|-----------|---------|-------|
| `spec.universe` | string | yes | **yes** | — | YBUniverse CR name |
| `spec.name` | string | yes | no | — | Display name |
| `spec.tableType` | string enum | yes | no | — | `YSQL` or `YCQL` |
| `spec.database` | string | yes | no | — | Database/keyspace name |
| `spec.intervalInSeconds` | integer | no | no | `86400` | Snapshot interval |
| `spec.retentionPeriodInSeconds` | integer | no | no | `604800` | Must be > `intervalInSeconds` |

### PitrRestore (v2026.1+)

All fields are required and immutable. To restore to a different time, create a new CR.

| Field | Type | Notes |
|-------|------|-------|
| `spec.universe` | string | YBUniverse CR name |
| `spec.pitrConfig` | string | PitrConfig CR name |
| `spec.restoreTime` | string (date-time) | ISO 8601, e.g. `"2024-02-09T15:30:00Z"`. Must be within the retention window. |

Status: `status.message`, `status.taskUUID`.

### DrConfig

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `spec.name` | string | yes | Display name |
| `spec.sourceUniverse` | string | yes | Source YBUniverse CR name |
| `spec.targetUniverse` | string | yes | Target YBUniverse CR name; set to `""` to trigger failover |
| `spec.databases[]` | string array | yes (min 1) | Database names to replicate |
| `spec.storageConfig` | string | yes | StorageConfig CR name |
| `spec.paused` | bool | no | Default `false` — pause/resume replication [v2026.1+] |

**sourceUniverse / targetUniverse mutations (v2026.1+):** The webhook allows only these change patterns; any other combination is rejected:
- **Switchover** — swap source and target.
- **Failover** — source ← old target, target ← `""`.
- **Restart/change replica** — source unchanged, target ← new universe name.

Pre-2026.1: both fields were fully immutable.

### SupportBundle

**Plural resource name is `support-bundles`** (hyphenated) — `kubectl get support-bundles -A`, not `kubectl get supportbundle`.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `spec.universeName` | string | yes | YBUniverse CR name |
| `spec.collectionTimerange.startDate` | string (date-time) | yes | ISO 8601 |
| `spec.collectionTimerange.endDate` | string (date-time) | no | Defaults to now |
| `spec.components[]` | string enum array | no | `UniverseLogs`, `ApplicationLogs`, `OutputFiles`, `ErrorFiles`, `CoreFiles`, `GFlags`, `Instance`, `ConsensusMeta`, `TabletMeta`, `YbcLogs`, `K8sInfo` |

Status: `status.status` (`generating`, `ready`, `failed`), `status.access` (download URL).

## Immutable Fields

Attempting to change an immutable field is rejected by the admission webhook — delete and recreate the CR (data loss for YBUniverse).

- **YBUniverse:** `universeName`, `replicationFactor`, `providerName`, `zoneFilter`, `enableYSQL`, `enableYCQL`, `enableYSQLAuth`, `ysqlPassword`, `enableYCQLAuth`, `ycqlPassword`, `enableNodeToNodeEncrypt`, `enableClientToNodeEncrypt`, `rootCA`, `enableLoadBalancer`, `enableIPV6`, `deviceInfo.numVolumes`, `deviceInfo.storageClass`, `masterDeviceInfo.numVolumes`, `masterDeviceInfo.storageClass`. `tserverVolume`/`masterVolume` cannot be removed once set; `perAZ` within them cannot be removed once set; `numVolumes`/`storageClass` within them are mutable with DB ≥ 2026.1.0.
- **YBProvider:** `cloudInfo.kubernetesProvider`
- **StorageConfig:** `config_type`, `name`, `data.BACKUP_LOCATION`
- **DrConfig (pre-2026.1):** `sourceUniverse`, `targetUniverse` fully immutable. See DrConfig section for v2026.1+ rules.
- **PitrConfig:** `universe`
- **PitrRestore (v2026.1+):** `universe`, `pitrConfig`, `restoreTime` — all immutable.
- **BackupSchedule:** `backupType`, `universe`, `storageConfig`, `keyspace`, `name`, `tableByTableBackup`, `timeBeforeDelete`, `enablePointInTimeRestore`

## What Can Be Changed After Creation

- **Scale:** `spec.numNodes` and `placementInfo` zone counts.
- **Upgrade:** `spec.ybSoftwareVersion` (register the new Release first).
- **GFlags:** `spec.gFlags.tserverGFlags` / `masterGFlags`.
- **Volume size:** Increase `volumeSize` on any volume field (never decrease).
- **Volume count / storage class (v2026.1+, DB ≥ 2026.1.0):** `tserverVolume.numVolumes`, `storageClass`, per-AZ overrides.
- **Resource spec (v2026.1+):** `tserverResourceSpec`, `masterResourceSpec`.
- **YBC throttle (v2026.1+):** `ybcThrottleParameters`.
- **Kubernetes overrides:** Resource requests/limits, affinity, tolerations, annotations, labels, secretEnv, extraVolumes.
- **Pause/resume:** `spec.paused` on YBUniverse; `spec.paused` on DrConfig (v2026.1+).
- **DR switchover / failover (v2026.1+):** See DrConfig mutation rules.
- **Backup schedule frequency:** `schedulingFrequency`, `cronExpression`, `incrementalBackupFrequency`.

## Anti-Patterns

| Anti-Pattern | Consequence | Do Instead |
|-------------|-------------|------------|
| Creating CRs in a namespace the operator is not watching | CRs silently ignored | Check `yugaware.kubernetesOperatorNamespace` in Helm values |
| CRD version doesn't match operator version | Fields silently dropped or validation errors | Apply CRDs from same version as Helm chart before upgrading |
| Omitting a YBProvider when RBAC is namespace-scoped | Universe creation fails | Check for ClusterRoleBinding; if only RoleBindings exist, create an explicit YBProvider |
| Creating a YBUniverse before its Release is `success: true` | Creation fails or hangs | Wait for `kubectl get release <version>` to show `Downloaded: true` |
| Putting credentials inline in StorageConfig `spec.data` | Secrets visible in CR YAML | Use `awsSecretAccessKeySecret` / `gcsCredentialsJsonSecret` / `azureStorageSasTokenSecret` |
| Deleting an incremental backup individually | Breaks the backup chain | Delete the base full backup (cascades to all incrementals) |
| Mixing `tserverVolume` and `deviceInfo` (or `masterVolume` and `masterDeviceInfo`) | Rejected by validation | Use one set; they are mutually exclusive |
| `ybcThrottleParameters.maxConcurrentUploads` or `maxConcurrentDownloads` > 3 | `Error Updating` at reconcile time — admission webhook does not validate this | Keep both values ≤ 3 |
| Applying `awsSecretAccessKeySecret` / `gcsCredentialsJsonSecret` / `azureStorageSasTokenSecret` before the referenced secret exists | StorageConfig CR created with empty `resourceUUID`; all subsequent updates fail with "Invalid UUID string" until CR is deleted and recreated | Create the secret first, then apply the StorageConfig CR |
| `kubernetesOperatorNamespace: ""` with Release or PitrConfig CRs | Release: packages never downloaded (namespace error interrupts `onAdd` between YBA registration and download trigger); PitrConfig: universe lookup fails permanently — config never created | Set `kubernetesOperatorNamespace` to the specific namespace where universes run |
| Changing DrConfig source/target in an unsupported pattern (v2026.1+) | Rejected by validation | Only switchover (swap), failover (target → `""`), or restart (source unchanged, new target) |
| PitrRestore `restoreTime` outside the retention window | Task fails | Verify the timestamp is within the PitrConfig retention period |
| Using short-lived tokens in kubeconfig secrets | Token expires silently | Use long-lived service account tokens (`kubernetes.io/service-account-token` Secret) |
| Kubeconfig secret missing the `kubeconfig` key | Provider fails to connect | Secret must contain exactly one key named `kubeconfig` |

## Installation

```bash
kubectl apply -f https://raw.github.com/yugabyte/charts/<version>/crds/concatenated_crd.yaml

helm install yba yugabytedb/yugaware \
  --version <version> --namespace yb-platform \
  --set yugaware.kubernetesOperatorEnabled=true \
  --set yugaware.kubernetesOperatorNamespace='<operator-namespace>' \
  --set yugaware.defaultUser.enabled=true \
  --set yugaware.defaultUser.username='<username>' \
  --set yugaware.defaultUser.email='<email>' \
  --set yugaware.defaultUser.password='<password>'
```

Upgrade (enable operator, watch all namespaces):
```bash
kubectl apply -f https://raw.github.com/yugabyte/charts/<version>/crds/concatenated_crd.yaml
helm upgrade yba yugabytedb/yugaware --version <version> --namespace yb-platform \
  --reset-then-reuse-values \
  --set yugaware.kubernetesOperatorEnabled=true \
  --set yugaware.kubernetesOperatorNamespace=''
```

## Current Limitations

- No multi-cluster universes.
- No software upgrade rollback.
- No xCluster replication (use DrConfig for DR).
- Read Replica defined in CRD but not fully supported via the operator.
- No encryption-at-rest.
- TLS config is immutable after creation; only self-signed supported.
- PitrRestore CR requires v2026.1+. Earlier versions must use the YBA UI or API for PITR restore.

## Workflow Summaries

> **Detailed step-by-step workflows with full YAML:** see [references/workflows.md](references/workflows.md)
> **Complete YAML examples for every CRD:** see [references/crd-examples.md](references/crd-examples.md)

### Provision a Universe
1. Install YBA with operator enabled; apply CRDs.
2. Create `Release` CR → wait for `status.success: true`.
3. Create `YBProvider` CR (if needed).
4. Optionally create `YBCertificate`.
5. Create `YBUniverse` → monitor with `kubectl get ybuniverse -w` until `Ready`.

### Upgrade DB Software
1. Create new `Release` CR → wait for `status.success: true`.
2. Edit `spec.ybSoftwareVersion` on the YBUniverse → monitor rolling upgrade.

### Backups
- **One-time:** `StorageConfig` → `Backup`
- **Scheduled:** `StorageConfig` → `BackupSchedule` (set `schedulingFrequency` or `cronExpression`)
- **Incremental:** set `incrementalBackupFrequency` on schedule, or `incrementalBackupBase` on a Backup
- **Restore from backup:** `RestoreJob` with `actionType: RESTORE`

### PITR Restore (v2026.1+)
1. Ensure `PitrConfig` exists for the target database.
2. Create `PitrRestore` CR with `universe`, `pitrConfig`, and `restoreTime`.
3. To restore to a different time, create a new `PitrRestore` CR.

### Disaster Recovery
- **Setup:** Create `StorageConfig` → `DrConfig` with source/target universes and database list.
- **Pause/resume:** Toggle `spec.paused` on the DrConfig (v2026.1+).
- **Switchover:** Swap `sourceUniverse`/`targetUniverse`.
- **Failover:** Set `sourceUniverse` ← old target, `targetUniverse` ← `""`.
- **Restart replication:** Keep `sourceUniverse` unchanged, set `targetUniverse` to new universe.
