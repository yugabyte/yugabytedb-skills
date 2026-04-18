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
```

## Available Skills

|Skill|Description|
|-|-|
|`ysql`|SQL and application development against YugabyteDB's PostgreSQL-compatible API — covers anti-patterns, schema design (sharding, indexes, geo-distribution), smart drivers, transaction retries, batching, PostgreSQL migration strategies, and production readiness checklists |
|`ycql`|YCQL (Cassandra-compatible API) development — covers partition keys, clustering columns, global secondary indexes, prepared statements, batching, TTL management, and memory configuration |
|`k8s-operator`|YugabyteDB Kubernetes Operator - deploying and managing YugabyteDB universes on Kubernetes through the YugabyteDB Anywhere or YugabyteDB (OSS) Kubernetes Operators|

## Learn More

- [YugabyteDB Documentation](https://docs.yugabyte.com/)
- [YugabyteDB Blog](https://www.yugabyte.com/blog)
- [skills.sh](https://skills.sh/) — The Open Agent Skills Ecosystem

## License

This project is licensed under the [Apache License 2.0](LICENSE).