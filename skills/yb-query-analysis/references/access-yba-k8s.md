# Connecting to YSQL on a YBA-managed Kubernetes universe

The triage snapshot and every SQL query in this skill need a YSQL connection (`ysqlsh`/`psql` on port 5433). On a YugabyteDB Anywhere (YBA)–managed **Kubernetes** universe you don't have a plain host:port — YSQL runs inside the tserver pods. There are two ways in; both assume your `kubectl` context already points at the cluster hosting the universe (the user will usually have set this).

## Identify the universe's pods and namespace

The tserver pods are named `<node>-yb-tserver-<N>` and live in the universe's namespace(s). If the universe is in a non-default kube context, pass `--context <ctx>` on every `kubectl` call (the universe may live in a different context than your current one — check with `kubectl config current-context`). Find the pods:

```bash
# All tserver pods across namespaces (pick the ones for your universe by name) —
# match by name; it's the most robust (label keys vary by chart version).
kubectl --context <ctx> get pods -A | grep yb-tserver
# or, if you know the namespace:
kubectl --context <ctx> get pods -n <namespace> | grep yb-tserver
# Label alternative (note the FULL key, not bare app=): -l app.kubernetes.io/name=yb-tserver
```

If you have the YBA API token, the authoritative source is the universe API response — `universeDetails.nodeDetailsSet[].nodeName` (the pod base name) and `.cloudInfo.kubernetesNamespace`. See [`yba-api` Prometheus reference](../../yba-api/references/prometheus.md#identifiers-you-need-from-the-yba-api) for the exact derivation; the same identifiers scope the metrics queries.

## Option A — `kubectl exec` into a tserver pod (simplest for ad-hoc triage)

`ysqlsh` ships inside the tserver container and connects to the local node's YSQL automatically:

```bash
# Run the triage snapshot by piping it into ysqlsh inside the pod
kubectl --context <ctx> exec -i -n <namespace> <node>-yb-tserver-0 -c yb-tserver -- \
  ysqlsh -d <database> < skills/yb-query-analysis/references/triage-snapshot.sql

# Or an interactive session
kubectl --context <ctx> exec -it -n <namespace> <node>-yb-tserver-0 -c yb-tserver -- ysqlsh -d <database>

# Single query
kubectl --context <ctx> exec -i -n <namespace> <node>-yb-tserver-0 -c yb-tserver -- \
  ysqlsh -d <database> -c "SELECT version();"
```

Notes:
- The container is `-c yb-tserver`. Inside the pod, `ysqlsh` defaults to the local tserver's host and port 5433 — no `-h` needed.
- For an authenticated universe, pass `-U <user>` and set `PGPASSWORD` in the env, or use the credentials YBA stores (see `yb-k8s-operator` → `references/kubeconfig-secrets.md` for retrieving universe secrets).
- This pins you to one node's PG backend. **`pg_stat_statements` is per-node in YugabyteDB** (each tserver's PostgreSQL keeps its own), so the snapshot reflects the workload that ran through *this* node — run your workload and read the snapshot through the same pod for consistency. To reset/aggregate across all nodes, use the YBA "slow queries" feature (it fans `pg_stat_statements_reset()` and collection across every node). The **catalog** (`pg_class`/`reltuples`, `pg_sequences`, `pg_stats`, index lists) is cluster-wide — read it from any pod. But the **`pg_stat_*` collector views** (`pg_stat_user_tables`/`pg_stat_all_tables` analyze timestamps and counters, `pg_stat_all_indexes.idx_scan`, `pg_stat_activity`, `pg_locks`) are **node-local**, as are ASH and other diagnostics — they reflect only the node you land on. For a cluster-wide view of those, query each pod (loop over `yb_servers()`) and aggregate.

## Option B — port-forward 5433 to your machine (use your local psql tooling)

```bash
kubectl --context <ctx> port-forward -n <namespace> <node>-yb-tserver-0 5433:5433
# then, in another shell, against localhost:
psql -h localhost -p 5433 -U yugabyte -d <database> -f skills/yb-query-analysis/references/triage-snapshot.sql
```

Use this when you want local tooling (psql `\copy`, scripts, the saved `.sql` files) rather than the in-pod `ysqlsh`. Keep the port-forward running in a background process for the duration of the assessment.

## Which to use

- **Quick triage / one-off queries** → Option A (`kubectl exec`), no extra process to manage.
- **A longer session, local scripts, or piping result files around** → Option B (port-forward).

Either way, the queries themselves are identical to any other deployment — only the connection wrapper differs. Once connected, return to [SKILL.md → START HERE](../SKILL.md) and run the triage snapshot.
