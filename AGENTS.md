# YugabyteDB Agent Skills

This repository contains agent skills for generating correct, optimized YugabyteDB code.

## Skills

| Skill | When to Use |
|-------|-------------|
| `yugabytedb-ysql` | Writing SQL or application code targeting YugabyteDB YSQL — anti-patterns, schema design, sharding, indexes, smart drivers, transaction retry, PostgreSQL migration |
| `yugabytedb-ycql` | Writing YCQL (Cassandra-compatible API) code — partition keys, secondary indexes, TTL, batching |

## Install

```bash
# All skills
npx skills add yugabyte/yugabytedb-skills

# YSQL only
npx skills add yugabyte/yugabytedb-skills -s yugabytedb-ysql

# YCQL only
npx skills add yugabyte/yugabytedb-skills -s yugabytedb-ycql
```
