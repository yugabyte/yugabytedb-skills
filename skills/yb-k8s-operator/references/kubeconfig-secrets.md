# Kubeconfig Secrets for YBProvider

YBA Operator's `YBProvider` CRD accepts Kubernetes Secrets containing `kubeconfig` data to connect to remote/target Kubernetes clusters. This enables multi-cluster universes where each zone can be a separate Kubernetes cluster.

A `kubeconfig` Secret must be created for every remote cluster in a provider, except the cluster hosting YugabyteDB Anywhere itself (which uses in-cluster credentials).

**Do not use `kubectl config view --raw --minify`** because this will include client authentication which YBA does not support. Service Accounts and tokens must be generated and added to the `kubeconfig` value.

## Naming Convention

Name each `kubeconfig` Secret after the target cluster it connects to, so it can be reused across multiple providers or zones:

```
<cluster-name>-kubeconfig
```

For example, a cluster named `ireland` gets a Secret called `ireland-kubeconfig`. If you need to rotate tokens without updating provider references, use a versioned suffix (e.g. `ireland-kubeconfig-1`) and update the YBProvider when switching to a new Secret.

## Secret Format

The Secret must contain a single key called `kubeconfig` whose value is a complete kubeconfig YAML file:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: ireland-kubeconfig
  namespace: yba
type: Opaque
data:
  kubeconfig: <base64-encoded kubeconfig YAML>
```

## Referencing in YBProvider

A `kubeConfigSecret` can be set at the top-level `spec.cloudInfo` (applies to all zones) or per-zone in `spec.regions[].zones[].cloudInfo` (overrides the top-level). Per-zone secrets are useful when zones span different Kubernetes clusters.

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: YBProvider
metadata:
  name: multi-cluster-provider
spec:
  cloudInfo:
    kubernetesProvider: gke
    kubernetesImageRegistry: quay.io/yugabyte/yugabyte
    # Top-level: used by all zones unless overridden
    kubeConfigSecret:
      name: ireland-kubeconfig
      namespace: yba
  regions:
    - code: eu-west-1
      zones:
        - code: eu-west-1a
          cloudInfo:
            kubeNamespace: yb
            kubernetesStorageClass: gp3
        - code: eu-west-1b
          cloudInfo:
            kubeNamespace: yb
            kubernetesStorageClass: gp3
            # Per-zone override for a different cluster
            kubeConfigSecret:
              name: frankfurt-kubeconfig
              namespace: yba
```

---

## Create a Kubeconfig Secret

This procedure creates a Service Account on the remote cluster, generates a kubeconfig using its token, and stores it as a Secret in the YBA cluster. Repeat for each remote cluster.

### 1. Set environment variables

Adjust these for each target cluster. The `SECRET_NAME` follows the naming convention above.

```bash
export CTX_TARGET=ireland              # kubectl context for the remote cluster
export CTX_YBA=london                  # kubectl context for the YBA cluster
export CLUSTER_NAME=ireland            # Label for the remote cluster
export TARGET_NAMESPACE=yb             # Namespace on CTX_TARGET for SA and RoleBinding
export YBA_NAMESPACE=yba               # Namespace on CTX_YBA for the kubeconfig Secret
export SA_NAME=yb-anywhere-kube
export SECRET_NAME=${CLUSTER_NAME}-kubeconfig
export TOKEN_DURATION=24h              # For short-lived tokens only
```

### 2. Create Namespace, Service Account, and RoleBinding on the remote cluster

```bash
kubectl --context "$CTX_TARGET" create namespace "$TARGET_NAMESPACE"

kubectl --context "$CTX_TARGET" create serviceaccount "$SA_NAME" -n "$TARGET_NAMESPACE"

kubectl --context "$CTX_TARGET" create rolebinding "${SA_NAME}-admin" \
  --clusterrole=admin \
  --serviceaccount="${TARGET_NAMESPACE}:${SA_NAME}" \
  -n "$TARGET_NAMESPACE"
```

The `admin` ClusterRole is used but scoped to `TARGET_NAMESPACE` via the RoleBinding.

### 3. Create a token

YBA does not yet support short-lived token authentication plugins (as of 2025.2.2.1), so long-lived tokens are required.

#### Option A: Short-lived token (requires auth plugin — not yet supported by YBA)

```bash
TOKEN="$(kubectl --context "$CTX_TARGET" create token "$SA_NAME" \
  -n "$TARGET_NAMESPACE" --duration=$TOKEN_DURATION)"
```

#### Option B: Long-lived token (recommended)

```bash
kubectl --context "$CTX_TARGET" apply -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: ${SA_NAME}-token
  namespace: ${TARGET_NAMESPACE}
  annotations:
    kubernetes.io/service-account.name: ${SA_NAME}
type: kubernetes.io/service-account-token
EOF

TOKEN="$(kubectl --context "$CTX_TARGET" get secret "${SA_NAME}-token" \
  -n "$TARGET_NAMESPACE" -o jsonpath='{.data.token}' | base64 -d)"
```

### 4. Extract cluster connection details

```bash
API_SERVER="$(kubectl config view --context "$CTX_TARGET" --minify \
  -o jsonpath='{.clusters[0].cluster.server}')"

CA_DATA="$(kubectl config view --raw --context "$CTX_TARGET" --minify \
  -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')"
```

If the context uses `certificate-authority` (a file path) instead of `certificate-authority-data`, base64-encode the PEM file contents and use that as `CA_DATA`.

### 5. Generate the kubeconfig file

```bash
cat > ${CTX_TARGET}-kubeconfig.yaml <<EOF
apiVersion: v1
kind: Config
clusters:
- name: ${CLUSTER_NAME}
  cluster:
    server: ${API_SERVER}
    certificate-authority-data: ${CA_DATA}
contexts:
- name: ${CLUSTER_NAME}-context
  context:
    cluster: ${CLUSTER_NAME}
    user: ${SA_NAME}
current-context: ${CLUSTER_NAME}-context
users:
- name: ${SA_NAME}
  user:
    token: ${TOKEN}
EOF
```

Verify connectivity:

```bash
kubectl --kubeconfig=${CTX_TARGET}-kubeconfig.yaml get ns ${TARGET_NAMESPACE}
```

### 6. Create the kubeconfig Secret in the YBA cluster

```bash
kubectl --context "$CTX_YBA" create secret generic "$SECRET_NAME" \
  -n "$YBA_NAMESPACE" \
  --from-file=kubeconfig=./${CTX_TARGET}-kubeconfig.yaml
```

Clean up the local kubeconfig file:

```bash
rm ${CTX_TARGET}-kubeconfig.yaml
```

---

## Rotate the Token and Kubeconfig Secret

When a token is revoked, YBA loses API access to the remote cluster. Impact during the outage:
- Health checks for nodes on that cluster will fail.
- Universe edit/update operations affecting that cluster will fail.
- **Universe availability is unaffected** — the database remains fully accessible to clients.
- YBA continues to aggregate metrics, send alerts, and manage backups for those universes.

### 1. Revoke the old token

Short-lived tokens expire automatically. Long-lived tokens are revoked by deleting their Secret from the remote cluster:

```bash
kubectl --context "$CTX_TARGET" delete secret "${SA_NAME}-token" -n "$TARGET_NAMESPACE"
```

### 2. Create a new token

Repeat step 3 above to generate a new token.

### 3. Regenerate the kubeconfig

Repeat steps 4 and 5 above with the new token.

### 4. Update or replace the kubeconfig Secret

#### Option A: Update the existing Secret in-place

```bash
kubectl --context "$CTX_YBA" create secret generic "$SECRET_NAME" \
  -n "$YBA_NAMESPACE" \
  --from-file=kubeconfig=./${CTX_TARGET}-kubeconfig.yaml \
  --dry-run=client -o yaml | kubectl --context "$CTX_YBA" apply -f -
```

YBA picks up the updated Secret immediately. No YBProvider changes needed.

#### Option B: Create a new Secret with a different name

Use a new `SECRET_NAME` (e.g. `ireland-kubeconfig-2`), then repeat step 6 to create it. Update each `YBProvider` that referenced the old Secret:

```bash
kubectl patch ybprovider <provider-name> -n <namespace> --type merge \
  -p '{"spec": {"cloudInfo": {"kubeConfigSecret": {"name": "ireland-kubeconfig-2"}}}}'
```

Delete the old Secret after all providers have been updated:

```bash
kubectl --context "$CTX_YBA" delete secret ireland-kubeconfig-1 -n "$YBA_NAMESPACE"
```

Clean up the local kubeconfig file:

```bash
rm ${CTX_TARGET}-kubeconfig.yaml
```
