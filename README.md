# YugabyteDB Agent Skills

Empower your AI agents and automation systems with native YugabyteDB expertise. This curated collection of structured skills enables developer tools to perform intelligent, production-grade database operations with full contextual awareness.

## Getting Started

Install all available skills at once:

```bash
npx skills add yugabyte/yugabytedb-skills
```

Or pick only what you need:

```bash
# PostgreSQL-compatible YSQL API
npx skills add yugabyte/yugabytedb-skills -s ysql

# Cassandra-compatible YCQL API
npx skills add yugabyte/yugabytedb-skills -s ycql

# YugabyteDB Kubernetes Operator
npx skills add yugabyte/yugabytedb-skills -s yb-k8s-operator

# YugabyteDB Anywhere REST API
npx skills add yugabyte/yugabytedb-skills -s yba-api

# YugabyteDB Anywhere Terraform provider
npx skills add yugabyte/yugabytedb-skills -s yba-terraform

# RAG / semantic search with LangChain on YugabyteDB
npx skills add yugabyte/yugabytedb-skills -s yb-rag-langchain

# YugabyteDB Aeon (managed cloud) REST API
npx skills add yugabyte/yugabytedb-skills -s aeon-api

# Performance must-checks: slow app or database, slow queries, EXPLAIN plans, locks, ASH, Prometheus / YBA metrics
npx skills add yugabyte/yugabytedb-skills -s yb-perf

# YSQL schema review: sharding, indexes, partitions, statistics, workload (deterministic engine)
npx skills add yugabyte/yugabytedb-skills -s yb-model
```

## Available Skills

|Skill|Description|
|-|-|
|`ysql`|SQL and application development against YugabyteDB's PostgreSQL-compatible API — covers anti-patterns, schema design (sharding, indexes, geo-distribution), smart drivers, transaction retries, batching, PostgreSQL migration strategies, and production readiness checklists |
|`ycql`|YCQL (Cassandra-compatible API) development — covers partition keys, clustering columns, global secondary indexes, prepared statements, batching, TTL management, and memory configuration |
|`yb-k8s-operator`|YugabyteDB Kubernetes Operator - deploying and managing YugabyteDB universes on Kubernetes through the YugabyteDB Anywhere or YugabyteDB (OSS) Kubernetes Operators|
|`yba-api`|YugabyteDB Anywhere REST API — using the YugabyteDB Anywhere REST API to create, observe, manage and deploy YugabyteDB universes in a range of infrastructure topologies and clouds|
|`yba-terraform`|YugabyteDB Anywhere Terraform provider (`yugabyte/yba`) — installing YBA on a VM, registering the first customer, and creating cloud (AWS/GCP/Azure) or on-prem providers, storage configs, universes, backups, schedules and restores, plus the upstream cloud IAM and network discovery those manifests need|
|`yb-rag-langchain`|RAG, semantic search, and hybrid (vector + full-text) search on YugabyteDB with LangChain — covers `PGVectorStore` setup, `ybhnsw` / GIN index creation, hybrid search configuration, metadata filtering, and combining relational predicates with vector queries|
|`aeon-api`|YugabyteDB Aeon (managed cloud) REST API — scripting and automating cluster lifecycle, database users, network allowlists, backups, VPC peering/private endpoints, and cluster metrics|
|`yb-perf`|Performance must-checks — the YugabyteDB-specific traps behind a slow app or database, raised latency or a regressed p99, across the SQL layer (slow queries, EXPLAIN (ANALYZE, DIST) plans, pg_stat_statements, lock contention, ASH waits) and the metrics layer (node hotspots, leader and tablet skew, CPU / disk / memory, tablet limits, rejections, master load from Prometheus / YBA), with read-only one-call check scripts and the evidence required before naming a cause|
|`yb-model`|Offline review of an existing YSQL schema and data model from a captured bundle — sharding, primary keys, indexes, partitions, tablet splits and data quality checked against the schema dump, `pg_stats`, `pg_stat_statements` or a query list and the rest of the evidence by a deterministic rules engine (Python, standard library), with plan replay on the customer's release, a safety pass over its own DDL, and a workload-hygiene section for the application team|

## Learn More

- [YugabyteDB Documentation](https://docs.yugabyte.com/)
- [YugabyteDB Blog](https://www.yugabyte.com/blog)
- [skills.sh](https://skills.sh/) — The Open Agent Skills Ecosystem

## License

This project is licensed under the [Apache License 2.0](LICENSE).