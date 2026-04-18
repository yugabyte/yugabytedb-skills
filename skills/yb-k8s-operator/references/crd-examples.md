# CRD Examples

Complete, copy-paste-ready YAML for every YugabyteDB Kubernetes Operator CRD.

## Release

### HTTP download (most common)

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: Release
metadata:
  name: "2024.2.1.0-b1"
spec:
  config:
    version: "2024.2.1.0-b1"
    downloadConfig:
      http:
        paths:
          helmChart: "https://charts.yugabyte.com/yugabyte-2024.2.1.tgz"
          x86_64: "https://software.yugabyte.com/releases/2024.2.1.0/yugabyte-2024.2.1.0-b1-linux-x86_64.tar.gz"
```

### S3 download with secret reference

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: Release
metadata:
  name: "2024.2.1.0-b1"
spec:
  config:
    version: "2024.2.1.0-b1"
    downloadConfig:
      s3:
        accessKeyId: AKIA...
        secretAccessKeySecret:
          name: s3-release-secret
          namespace: yb-platform
        paths:
          helmChart: "s3://my-releases/yugabyte-2024.2.1.tgz"
          x86_64: "s3://my-releases/yugabyte-2024.2.1.0-b1-linux-x86_64.tar.gz"
```

### GCS download with secret reference

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: Release
metadata:
  name: "2024.2.1.0-b1"
spec:
  config:
    version: "2024.2.1.0-b1"
    downloadConfig:
      gcs:
        credentialsJsonSecret:
          name: gcs-release-secret
          namespace: yb-platform
        paths:
          helmChart: "gs://my-releases/yugabyte-2024.2.1.tgz"
```

---

## YBProvider

### Single-cluster GKE provider

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: YBProvider
metadata:
  name: gke-provider
spec:
  cloudInfo:
    kubernetesProvider: gke
    kubernetesImageRegistry: quay.io/yugabyte/yugabyte
  regions:
    - code: us-west1
      zones:
        - code: us-west1-a
          cloudInfo:
            kubernetesStorageClass: yb-standard
            kubeNamespace: yb-db-nodes
        - code: us-west1-b
          cloudInfo:
            kubernetesStorageClass: yb-standard
            kubeNamespace: yb-db-nodes
        - code: us-west1-c
          cloudInfo:
            kubernetesStorageClass: yb-standard
            kubeNamespace: yb-db-nodes
```

### EKS provider with custom kubeconfig

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: YBProvider
metadata:
  name: eks-provider
spec:
  cloudInfo:
    kubernetesProvider: eks
    kubernetesImageRegistry: quay.io/yugabyte/yugabyte
    kubeConfigSecret:
      name: eks-kubeconfig
      namespace: yb-platform
  regions:
    - code: us-east-1
      zones:
        - code: us-east-1a
          cloudInfo:
            kubernetesStorageClass: gp3
            kubeNamespace: yb-db-nodes
        - code: us-east-1b
          cloudInfo:
            kubernetesStorageClass: gp3
            kubeNamespace: yb-db-nodes
        - code: us-east-1c
          cloudInfo:
            kubernetesStorageClass: gp3
            kubeNamespace: yb-db-nodes
```

### Multi-cluster provider with Istio

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: YBProvider
metadata:
  name: istio-provider
spec:
  cloudInfo:
    kubernetesProvider: gke
    kubernetesImageRegistry: quay.io/yugabyte/yugabyte
  regions:
    - code: europe-west1
      zones:
        - code: ireland
          cloudInfo:
            kubeNamespace: yb
            kubernetesStorageClass: yb-standard
            kubePodAddressTemplate: "{pod_name}.{namespace}.svc.{cluster_domain}"
            kubeConfigSecret:
              name: ireland-kubeconfig
              namespace: yba
            overrides:
              multicluster:
                createServicePerPod: true
              istioCompatibility:
                enabled: true
        - code: london
          cloudInfo:
            kubeNamespace: yb
            kubernetesStorageClass: yb-standard
            kubePodAddressTemplate: "{pod_name}.{namespace}.svc.{cluster_domain}"
            kubeConfigSecret:
              name: london-kubeconfig
              namespace: yba
            overrides:
              multicluster:
                createServicePerPod: true
              istioCompatibility:
                enabled: true
```

### Multi-cluster provider with Cilium ClusterMesh (MCS + Endpoint Sync)

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: YBProvider
metadata:
  name: cilium-mcs-provider
spec:
  cloudInfo:
    kubernetesProvider: gke
    kubernetesImageRegistry: quay.io/yugabyte/yugabyte
  regions:
    - code: europe-west1
      zones:
        - code: ireland
          cloudInfo:
            kubeNamespace: yb
            kubernetesStorageClass: yb-standard
            kubePodAddressTemplate: "{pod_name}.ireland.{service_name}.{namespace}.svc.{cluster_domain}"
            kubeConfigSecret:
              name: ireland-kubeconfig
              namespace: yba
            overrides:
              multicluster:
                createServiceExports: true
                kubernetesClusterId: ireland
        - code: frankfurt
          cloudInfo:
            kubeNamespace: yb
            kubernetesStorageClass: yb-standard
            kubePodAddressTemplate: "{pod_name}.frankfurt.{service_name}.{namespace}.svc.{cluster_domain}"
            kubeConfigSecret:
              name: frankfurt-kubeconfig
              namespace: yba
            overrides:
              multicluster:
                createServiceExports: true
                kubernetesClusterId: frankfurt
```

### Provider with cert-manager

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: YBProvider
metadata:
  name: provider-with-certs
spec:
  cloudInfo:
    kubernetesProvider: gke
    kubernetesImageRegistry: quay.io/yugabyte/yugabyte
  regions:
    - code: us-central1
      zones:
        - code: us-central1-a
          cloudInfo:
            kubernetesStorageClass: yb-standard
            kubeNamespace: yb-db-nodes
            certManagerIssuerKind: ClusterIssuer
            certManagerIssuerName: yb-ca-issuer
```

---

## YBCertificate

### Self-signed certificate

First create the secret:
```bash
kubectl create secret tls yb-self-signed-cert \
  --cert=ca.crt --key=ca.key -n yb-platform
```

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: YBCertificate
metadata:
  name: yb-tls-cert
spec:
  certType: SELF_SIGNED
  certificateSecretRef:
    name: yb-self-signed-cert
    namespace: yb-platform
```

### Cert-manager certificate

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: YBCertificate
metadata:
  name: yb-cm-cert
spec:
  certType: K8S_CERT_MANAGER
  certificateSecretRef:
    name: yb-cm-ca-secret
```

---

## YBUniverse

### Minimal universe (3-node, RF=3)

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: YBUniverse
metadata:
  name: demo-universe
spec:
  numNodes: 3
  replicationFactor: 3
  enableYSQL: true
  enableNodeToNodeEncrypt: true
  enableClientToNodeEncrypt: true
  ybSoftwareVersion: "2024.2.1.0-b1"
  deviceInfo:
    volumeSize: 100
    numVolumes: 1
    storageClass: "yb-standard"
```

### Production universe with placement, resources, and GFlags

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: YBUniverse
metadata:
  name: prod-universe
spec:
  universeName: production-db
  providerName: gke-provider
  numNodes: 6
  replicationFactor: 3
  enableYSQL: true
  enableYCQL: false
  enableNodeToNodeEncrypt: true
  enableClientToNodeEncrypt: true
  enableYSQLAuth: true
  ysqlPassword:
    secretName: ysql-credentials
  rootCA: yb-tls-cert
  ybSoftwareVersion: "2024.2.1.0-b1"
  placementInfo:
    defaultRegion: us-west1
    regions:
      - code: us-west1
        zones:
          - code: us-west1-a
            numNodes: 2
            preferred: true
          - code: us-west1-b
            numNodes: 2
            preferred: true
          - code: us-west1-c
            numNodes: 2
            preferred: true
  deviceInfo:
    volumeSize: 500
    numVolumes: 2
    storageClass: "yb-standard"
  masterDeviceInfo:
    volumeSize: 100
    numVolumes: 1
    storageClass: "yb-standard"
  gFlags:
    tserverGFlags:
      ysql_enable_packed_row: "true"
      yb_enable_read_committed_isolation: "true"
    masterGFlags:
      enable_automatic_tablet_splitting: "true"
  kubernetesOverrides:
    resource:
      master:
        requests:
          cpu: 4
          memory: 8Gi
        limits:
          cpu: 4
          memory: 8Gi
      tserver:
        requests:
          cpu: 8
          memory: 16Gi
        limits:
          cpu: 8
          memory: 16Gi
```

### Universe with YCQL enabled and authentication

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: YBUniverse
metadata:
  name: ycql-universe
spec:
  numNodes: 3
  replicationFactor: 3
  enableYSQL: true
  enableYCQL: true
  enableYCQLAuth: true
  ycqlPassword:
    secretName: ycql-credentials
  enableNodeToNodeEncrypt: true
  enableClientToNodeEncrypt: true
  ybSoftwareVersion: "2024.2.1.0-b1"
  deviceInfo:
    volumeSize: 200
    numVolumes: 1
    storageClass: "yb-standard"
```

### Universe with read replica

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: YBUniverse
metadata:
  name: universe-with-rr
spec:
  numNodes: 3
  replicationFactor: 3
  enableYSQL: true
  enableNodeToNodeEncrypt: true
  enableClientToNodeEncrypt: true
  ybSoftwareVersion: "2024.2.1.0-b1"
  deviceInfo:
    volumeSize: 200
    numVolumes: 1
  readReplica:
    numNodes: 3
    replicationFactor: 3
    deviceInfo:
      volumeSize: 200
      numVolumes: 1
```

### Universe with tolerations and node affinity

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: YBUniverse
metadata:
  name: affinity-universe
spec:
  numNodes: 3
  replicationFactor: 3
  enableYSQL: true
  enableNodeToNodeEncrypt: true
  enableClientToNodeEncrypt: true
  ybSoftwareVersion: "2024.2.1.0-b1"
  deviceInfo:
    volumeSize: 100
    numVolumes: 1
    storageClass: "yb-standard"
  kubernetesOverrides:
    resource:
      tserver:
        requests:
          cpu: 4
          memory: 8Gi
        limits:
          cpu: 4
          memory: 8Gi
    tserver:
      tolerations:
        - key: dedicated
          operator: Equal
          value: yugabyte
          effect: NoSchedule
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            - nodeSelectorTerms:
                - matchExpressions:
                    - key: node-type
                      operator: In
                      values:
                        - yugabyte-db
    master:
      tolerations:
        - key: dedicated
          operator: Equal
          value: yugabyte
          effect: NoSchedule
```

---

## StorageConfig

### S3 with secret reference (recommended)

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: StorageConfig
metadata:
  name: s3-backup-config
spec:
  config_type: STORAGE_S3
  data:
    BACKUP_LOCATION: s3://my-yb-backups/prod
    AWS_ACCESS_KEY_ID: AKIA...
  awsSecretAccessKeySecret:
    name: aws-backup-secret
    namespace: yb-platform
```

### S3 with IAM (no credentials, not suitable for multi-cluster universes)

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: StorageConfig
metadata:
  name: s3-iam-config
spec:
  config_type: STORAGE_S3
  data:
    BACKUP_LOCATION: s3://my-yb-backups/prod
    USE_IAM: true
```

### GCS with IAM

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: StorageConfig
metadata:
  name: gcs-iam-config
spec:
  config_type: STORAGE_GCS
  data:
    BACKUP_LOCATION: gs://my-yb-backups/prod
    USE_IAM: true
```

### GCS with secret reference

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: StorageConfig
metadata:
  name: gcs-backup-config
spec:
  config_type: STORAGE_GCS
  data:
    BACKUP_LOCATION: gs://my-yb-backups/prod
  gcsCredentialsJsonSecret:
    name: gcs-backup-secret
    namespace: yb-platform
```

### Azure Blob Storage

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: StorageConfig
metadata:
  name: az-backup-config
spec:
  config_type: STORAGE_AZ
  data:
    BACKUP_LOCATION: https://myaccount.blob.core.windows.net/backups
  azureStorageSasTokenSecret:
    name: azure-sas-secret
    namespace: yb-platform
```

### NFS

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: StorageConfig
metadata:
  name: nfs-backup-config
spec:
  config_type: STORAGE_NFS
  data:
    BACKUP_LOCATION: /mnt/nfs/yb-backups
```

---

## Backup

### Full YSQL backup

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: Backup
metadata:
  name: prod-backup-20240115
spec:
  backupType: PGSQL_TABLE_TYPE
  storageConfig: s3-backup-config
  universe: prod-universe
  keyspace: yugabyte
  timeBeforeDelete: 2592000000  # 30 days in milliseconds
```

### Full YCQL backup

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: Backup
metadata:
  name: ycql-backup-20240115
spec:
  backupType: YQL_TABLE_TYPE
  storageConfig: s3-backup-config
  universe: prod-universe
  keyspace: my_keyspace
```

### Incremental backup (references a base)

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: Backup
metadata:
  name: prod-incremental-20240116
spec:
  backupType: PGSQL_TABLE_TYPE
  storageConfig: s3-backup-config
  universe: prod-universe
  keyspace: yugabyte
  incrementalBackupBase: prod-backup-20240115
```

---

## BackupSchedule

### Hourly full + 15-minute incremental

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: BackupSchedule
metadata:
  name: prod-hourly-schedule
spec:
  backupType: PGSQL_TABLE_TYPE
  storageConfig: s3-backup-config
  universe: prod-universe
  keyspace: yugabyte
  schedulingFrequency: 3600000       # 1 hour in ms
  incrementalBackupFrequency: 900000 # 15 minutes in ms
  timeBeforeDelete: 604800000        # 7 days in ms
```

### Daily cron schedule with PITR

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: BackupSchedule
metadata:
  name: prod-daily-schedule
spec:
  backupType: PGSQL_TABLE_TYPE
  storageConfig: s3-backup-config
  universe: prod-universe
  keyspace: yugabyte
  cronExpression: "0 2 * * *"  # Daily at 2 AM
  enablePointInTimeRestore: true
  timeBeforeDelete: 2592000000 # 30 days in ms
```

---

## RestoreJob

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: RestoreJob
metadata:
  name: restore-from-backup
spec:
  actionType: RESTORE
  universe: prod-universe
  backup: prod-backup-20240115
  keyspace: yugabyte
```

---

## PitrConfig

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: PitrConfig
metadata:
  name: yugabyte-pitr
spec:
  name: yugabyte-pitr
  universe: prod-universe
  database: yugabyte
  tableType: YSQL
  intervalInSeconds: 86400      # snapshot every 24 hours
  retentionPeriodInSeconds: 604800  # retain 7 days
```

---

## DrConfig

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: DrConfig
metadata:
  name: prod-dr
spec:
  name: prod-dr-config
  sourceUniverse: prod-universe-east
  targetUniverse: prod-universe-west
  databases:
    - yugabyte
    - app_data
  storageConfig: s3-backup-config
```

---

## SupportBundle

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: SupportBundle
metadata:
  name: debug-bundle-20240115
spec:
  universeName: prod-universe
  collectionTimerange:
    startDate: "2024-01-14T00:00:00Z"
    endDate: "2024-01-15T12:00:00Z"
  components:
    - UniverseLogs
    - ApplicationLogs
    - OutputFiles
    - ErrorFiles
    - GFlags
    - Instance
    - YbcLogs
    - K8sInfo
```

### Minimal support bundle (all components, open-ended)

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: SupportBundle
metadata:
  name: quick-debug
spec:
  universeName: prod-universe
  collectionTimerange:
    startDate: "2024-01-15T00:00:00Z"
```
