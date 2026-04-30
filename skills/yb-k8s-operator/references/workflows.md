# End-to-End Workflows

Step-by-step workflows for common YugabyteDB Kubernetes Operator tasks.

## 1. Provision a New Universe from Scratch

### Step 1: Install CRDs and YBA

```bash
# Apply CRDs, replacing version as needed
kubectl apply -f https://raw.github.com/yugabyte/charts/2025.2.2/crds/concatenated_crd.yaml

# Install YBA with operator enabled
helm install yba yugabytedb/yugaware \
  --version 2025.2.2 \
  --namespace yb-platform --create-namespace \
  --set yugaware.kubernetesOperatorEnabled=true \
  --set yugaware.kubernetesOperatorNamespace='' \
  --set rbac.enabled=true \
  --set yugaware.defaultUser.enabled=true \
  --set yugaware.defaultUser.username='admin' \
  --set yugaware.defaultUser.email='admin@example.com' \
  --set yugaware.defaultUser.password='SecureP@ss123' \
  --wait --timeout 1200s
```

`yugaware.defaultUser` need not be set in secure environments - it only creates a job which invokes an API request to complete initial registration for the default administrative user. If it is excluded the registration can be completed separately after YBA is deployed:

```bash
curl -k -X POST https://yba-yugaware-ui/api/register \
  -H "Content-Type: application/json" \
  -d '{
    "code": "dev",
    "name": "admin",
    "email": "admin@example.com",
    "password": "SecureP@ss123"
  }'
```

### Step 2: Verify YBA is running

```bash
kubectl get pods -n yb-platform
# Wait for all pods to be Running and Ready
```

### Step 3: Register a DB software release

Always verify a release, including its build number, on the YugabyteDB Helm chart by checking for `appVersion`: `https://charts.yugabyte.com/index.yaml`

```yaml
# release.yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: Release
metadata:
  name: "2024.2.1.0-b1"
  namespace: yb-operator
spec:
  config:
    version: "2024.2.1.0-b1"
    downloadConfig:
      http:
        paths:
          helmChart: "https://charts.yugabyte.com/yugabyte-2024.2.1.tgz"
          x86_64: "https://software.yugabyte.com/releases/2024.2.1.0/yugabyte-2024.2.1.0-b1-linux-x86_64.tar.gz"
```

```bash
kubectl apply -f release.yaml
# Wait for download to complete
kubectl get release "2024.2.1.0-b1" -n yb-operator
# Verify: Downloaded = true
```

### Step 4: Create a provider

```yaml
# provider.yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: YBProvider
metadata:
  name: my-provider
  namespace: yb-operator
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

In a multi-cluster environment ensure that kubeconfig secrets are created with long-lived Service Account tokens for authentication. **Follow `kubeconfig-secrets.md` to create a valid kubeconfig Secret per cluster.** Always create these secrets or create a script that the user can use to do so.

```bash
kubectl apply -f provider.yaml
kubectl get ybprovider my-provider -n yb-operator
# Wait for State to be Ready
```

### Step 5: Create the universe

```yaml
# universe.yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: YBUniverse
metadata:
  name: my-universe
  namespace: yb-operator
spec:
  providerName: my-provider
  numNodes: 3
  replicationFactor: 3
  enableYSQL: true
  enableNodeToNodeEncrypt: true
  enableClientToNodeEncrypt: true
  ybSoftwareVersion: "2024.2.1.0-b1"
  placementInfo:
    defaultRegion: us-west1
    regions:
      - code: us-west1
        zones:
          - code: us-west1-a
            numNodes: 1
          - code: us-west1-b
            numNodes: 1
          - code: us-west1-c
            numNodes: 1
  deviceInfo:
    volumeSize: 100
    numVolumes: 1
    storageClass: "yb-standard"
  kubernetesOverrides:
    resource:
      master:
        requests:
          cpu: 2
          memory: 4Gi
        limits:
          cpu: 2
          memory: 4Gi
      tserver:
        requests:
          cpu: 4
          memory: 8Gi
        limits:
          cpu: 4
          memory: 8Gi
```

```bash
kubectl apply -f universe.yaml
kubectl get ybuniverse my-universe -n yb-operator -w
# Wait for STATE = Ready (can take 10-20 minutes)
```

Do not adjust the master volume size in the `masterDeviceInfo` below 50. Only adjust the t server volume size `deviceInfo` if requested.

Exclude `defaultRegion` if the region does not include enough zones to host three master pods (one per zone).

Always ensure that the YugabyteDB software release build number is the latest valid value for the requested version.

Always ensure that the `ysqlpassword` and / or `ycqlpassword` Secrets exist in the `ybUniverse` resource's namespace.

Ensure that the pod request and limits are within the available resources of the nodes in their zones' clusters.

### Step 6: Connect to the database

```bash
# Get YSQL endpoints
kubectl get ybuniverse my-universe -n yb-operator -o jsonpath='{.status.sqlEndpoints}'

# Connect via port-forward or LoadBalancer
kubectl port-forward svc/yb-tserver-service 5433:5433 -n yb-db-nodes
psql -h 127.0.0.1 -p 5433 -U yugabyte
```

---

## 2. Upgrade DB Software Version

### Step 1: Register the new release

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: Release
metadata:
  name: "2025.2.2.1-b1"
  namespace: yb-operator
spec:
  config:
    version: "2025.2.2.1-b1"
    downloadConfig:
      http:
        paths:
          helmChart: "https://charts.yugabyte.com/yugabyte-2025.2.2.tgz"
          x86_64: "https://software.yugabyte.com/releases/2025.2.2.1/yugabyte-2025.2.2.1-b1-linux-x86_64.tar.gz"
```

```bash
kubectl apply -f new-release.yaml
kubectl get release "2025.2.2.1-b1" -n yb-operator
# Wait for Downloaded = true
```

### Step 2: Update the universe version

```bash
kubectl patch ybuniverse my-universe -n yb-operator --type merge \
  -p '{"spec": {"ybSoftwareVersion": "2025.2.2.1-b1"}}'
```

### Step 3: Monitor the rolling upgrade

```bash
kubectl get ybuniverse my-universe -n yb-operator -w
# STATE transitions: Ready → Editing → Ready
```

---

## 3. Scale a Universe

### Add nodes

```bash
kubectl patch ybuniverse my-universe -n yb-operator --type merge \
  -p '{"spec": {"numNodes": 6}}'
```

With placement info, also update zone node counts:
```bash
kubectl patch ybuniverse my-universe -n yb-operator --type merge \
  -p '{"spec": {"numNodes": 6, "placementInfo": {"regions": [{"code": "us-west1", "zones": [{"code": "us-west1-a", "numNodes": 2}, {"code": "us-west1-b", "numNodes": 2}, {"code": "us-west1-c", "numNodes": 2}]}]}}}'
```

### Expand storage

```bash
kubectl patch ybuniverse my-universe -n yb-operator --type merge \
  -p '{"spec": {"deviceInfo": {"volumeSize": 200}}}'
```

Volume size can only be increased, never decreased.

---

## 4. Set Up Backups with Scheduled Incremental

### Step 1: Create storage config

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: StorageConfig
metadata:
  name: s3-backup
  namespace: yb-operator
spec:
  config_type: STORAGE_S3
  data:
    BACKUP_LOCATION: s3://my-yb-backups/prod
    USE_IAM: true
```

```bash
kubectl apply -f storage-config.yaml
kubectl get storageconfig s3-backup -n yb-operator
# Verify success: true
```

### Step 2: Create backup schedule

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: BackupSchedule
metadata:
  name: prod-schedule
  namespace: yb-operator
spec:
  backupType: PGSQL_TABLE_TYPE
  storageConfig: s3-backup
  universe: my-universe
  keyspace: yugabyte
  schedulingFrequency: 86400000       # 24 hours in ms
  incrementalBackupFrequency: 3600000 # 1 hour in ms
  timeBeforeDelete: 604800000         # 7 days in ms
```

```bash
kubectl apply -f backup-schedule.yaml
kubectl get backupschedule prod-schedule -n yb-operator
```

### Step 3: Verify backups are being created

```bash
kubectl get backups -n yb-operator
```

---

## 5. Restore from Backup

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: RestoreJob
metadata:
  name: restore-prod
  namespace: yb-operator
spec:
  actionType: RESTORE
  universe: my-universe
  backup: prod-schedule-XXXXXXXXXX-full-YYYY-MM-DD-HH-MM-SS
  keyspace: yugabyte
```

```bash
kubectl apply -f restore-job.yaml
kubectl get restorejob restore-prod -n yb-operator
```

---

## 6. Configure Disaster Recovery

### Prerequisites
- Two universes in `Ready` state
- A StorageConfig for backup transport

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: DrConfig
metadata:
  name: prod-dr
  namespace: yb-operator
spec:
  name: prod-dr
  sourceUniverse: prod-east
  targetUniverse: prod-west
  databases:
    - yugabyte
    - app_data
  storageConfig: s3-backup
```

```bash
kubectl apply -f dr-config.yaml
kubectl get drconfig prod-dr -n yb-operator
```

---

## 7. Enable Point-in-Time Recovery

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: PitrConfig
metadata:
  name: yugabyte-pitr
  namespace: yb-operator
spec:
  name: yugabyte-pitr
  universe: my-universe
  database: yugabyte
  tableType: YSQL
  intervalInSeconds: 900        # snapshot every 15 minutes
  retentionPeriodInSeconds: 86400  # retain 24 hours
```

```bash
kubectl apply -f pitr-config.yaml
kubectl get pitrconfig yugabyte-pitr -n yb-operator
```

---

## 8. Collect a Support Bundle

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: SupportBundle
metadata:
  name: debug-bundle
  namespace: yb-operator
spec:
  universeName: my-universe
  collectionTimerange:
    startDate: "2024-01-15T00:00:00Z"
  components:
    - UniverseLogs
    - ApplicationLogs
    - ErrorFiles
    - GFlags
    - K8sInfo
    - YbcLogs
```

```bash
kubectl apply -f support-bundle.yaml
# Monitor until ready
kubectl get supportbundle debug-bundle -n yb-operator -w
# Download when status.status = ready
kubectl get supportbundle debug-bundle -n yb-operator -o jsonpath='{.status.access}'
```

---

## 9. Update GFlags (Live Configuration Change)

```bash
kubectl patch ybuniverse my-universe -n yb-operator --type merge \
  -p '{"spec": {"gFlags": {"tserverGFlags": {"ysql_enable_packed_row": "true", "yb_enable_read_committed_isolation": "true"}}}}'
```

Monitor the rolling restart:
```bash
kubectl get ybuniverse my-universe -n yb-operator -w
```

---

## 10. Pause and Resume a Universe

### Pause (scales pods to 0)

```bash
kubectl patch ybuniverse my-universe -n yb-operator --type merge \
  -p '{"spec": {"paused": true}}'
```

### Resume

```bash
kubectl patch ybuniverse my-universe -n yb-operator --type merge \
  -p '{"spec": {"paused": false}}'
```

---

## 11. Configure TLS with Self-Signed Certificates

### Step 1: Create the TLS secret

```bash
# Generate a CA (or use an existing one)
openssl genrsa -out ca.key 2048
openssl req -x509 -new -key ca.key -sha256 -days 3650 -out ca.crt \
  -subj "/CN=YugabyteDB CA"

# Create the Kubernetes secret
kubectl create secret tls yb-ca-cert \
  --cert=ca.crt --key=ca.key -n yb-operator
```

### Step 2: Create the YBCertificate CR

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: YBCertificate
metadata:
  name: yb-tls
  namespace: yb-operator
spec:
  certType: SELF_SIGNED
  certificateSecretRef:
    name: yb-ca-cert
    namespace: yb-operator
```

### Step 3: Reference in the universe

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: YBUniverse
metadata:
  name: secure-universe
  namespace: yb-operator
spec:
  enableNodeToNodeEncrypt: true
  enableClientToNodeEncrypt: true
  rootCA: yb-tls
  # ... rest of spec
```

---

## 12. Delete a Universe and Clean Up

Deleting a universe automatically deletes associated BackupSchedules (via owner references).

```bash
# Delete the universe (triggers cleanup)
kubectl delete ybuniverse my-universe -n yb-operator

# Manually clean up remaining resources if needed
kubectl delete backups --all -n yb-operator
kubectl delete storageconfig s3-backup -n yb-operator
kubectl delete release "2024.2.1.0-b1" -n yb-operator
kubectl delete ybprovider my-provider -n yb-operator
```

---

## Monitoring Cheat Sheet

```bash
# Watch universe state transitions
kubectl get ybuniverse -n yb-operator -w

# Check all YugabyteDB resources
kubectl get all -l app=yugabyte -n yb-operator

# View universe details and status
kubectl describe ybuniverse my-universe -n yb-operator

# Check operator logs
kubectl logs -n yb-platform -l app=yugaware -c yugabyte-k8s-operator -f

# List all CRD instances
kubectl get releases,ybproviders,ybcertificates,ybuniverses,storageconfigs,backups,backupschedules,restorejobs,pitrconfigs,drconfigs,support-bundles -n yb-operator
```
