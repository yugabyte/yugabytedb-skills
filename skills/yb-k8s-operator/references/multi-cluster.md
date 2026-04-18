# Multi-Cluster Service Mesh Configuration

When YugabyteDB universe zones span multiple Kubernetes clusters connected via a service mesh, the YBProvider must include per-zone Helm overrides and a `kubePodAddressTemplate` so that YBA and the DB nodes can resolve cross-cluster pod addresses.

Each service mesh has different requirements. Choose the section that matches your mesh.

## Istio Multi-Cluster

Istio multi-cluster service mesh requires per-pod Kubernetes Services and Istio compatibility mode.

**Per-zone overrides:**

```yaml
overrides:
  multicluster:
    createServicePerPod: true
  istioCompatibility:
    enabled: true
```

**Pod address template** (same for all zones):

```
{pod_name}.{namespace}.svc.{cluster_domain}
```

**Full provider example:**

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: YBProvider
metadata:
  name: istio-multi-cluster
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

---

## Cilium ClusterMesh with MCS API and Endpoint Sync (Recommended)

When Cilium ClusterMesh is configured with the Multi-Cluster Services (MCS) API and endpoint sync enabled, use `createServiceExports` and set `kubernetesClusterId` to identify each cluster. The pod address template includes the cluster name and varies per zone.

**Per-zone overrides** (cluster name varies per zone):

```yaml
overrides:
  multicluster:
    createServiceExports: true
    kubernetesClusterId: <cluster-name>
```

**Pod address template** (cluster-specific — varies per zone):

```
{pod_name}.<cluster-name>.{service_name}.{namespace}.svc.{cluster_domain}
```

Replace `<cluster-name>` with the Cilium cluster name for that zone (e.g. `ireland`, `frankfurt`).

**Full provider example:**

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

---

## Cilium ClusterMesh with Global Services (Not Recommended)

When MCS API and endpoint sync are **not** enabled, Cilium ClusterMesh can still work using Global Services. This approach requires manual service annotation after the universe is created and is not recommended for new deployments.

**Per-zone overrides:**

```yaml
overrides:
  multicluster:
    createServicePerPod: true
```

**Pod address template** (same for all zones):

```
{pod_name}.{namespace}.svc.{cluster_domain}
```

**Post-creation manual step** — after the universe reaches `Ready` state, annotate each per-pod Service in every remote cluster for Global Services support:

```bash
kubectl --context <cluster-context> annotate service <service-name> \
  -n <namespace> \
  service.cilium.io/global=true \
  service.cilium.io/global-sync-endpoint-slices=true
```

This must be repeated for every per-pod Service created by the Helm chart across all clusters in the universe. New Services created during scale-out operations also need these annotations.

**Full provider example:**

```yaml
apiVersion: operator.yugabyte.io/v1alpha1
kind: YBProvider
metadata:
  name: cilium-global-svc-provider
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
```

---

## Quick Reference

| Service Mesh | Override Key | Pod Address Template | Per-Zone Template? | Post-Create Steps |
|---|---|---|---|---|
| Istio | `createServicePerPod: true`, `istioCompatibility.enabled: true` | `{pod_name}.{namespace}.svc.{cluster_domain}` | No (same for all) | None |
| Cilium MCS + Endpoint Sync | `createServiceExports: true`, `kubernetesClusterId: <name>` | `{pod_name}.<name>.{service_name}.{namespace}.svc.{cluster_domain}` | **Yes** (cluster name varies) | None |
| Cilium Global Services | `createServicePerPod: true` | `{pod_name}.{namespace}.svc.{cluster_domain}` | No (same for all) | Annotate Services manually |
