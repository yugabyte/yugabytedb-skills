# YugabyteDB Agent Skills

Reusable AI agent skills for generating correct, optimized YugabyteDB code. Compatible with Claude Code, Cursor, GitHub Copilot, Windsurf, Gemini, and any tool supporting the [skills.sh](https://skills.sh) ecosystem.

## Install

### All skills

```bash
npx skills add yugabyte/skills
```

### YSQL only (PostgreSQL-compatible API)

```bash
npx skills add yugabyte/skills -s yugabytedb-ysql
```

### YCQL only (Cassandra-compatible API)

```bash
npx skills add yugabyte/skills -s yugabytedb-ycql
```

## Skills

| Skill | When to Use |
|-------|-------------|
| `yugabytedb-ysql` | Writing SQL or application code targeting YugabyteDB's PostgreSQL-compatible API — anti-patterns, schema design (sharding, indexes, geo-distribution), smart drivers, transaction retry, batching, PostgreSQL migration strategy, production checklist |
| `yugabytedb-ycql` | Writing YCQL (Cassandra-compatible API) code — partition keys, clustering columns, global secondary indexes, prepared statements, batching, TTL, memory configuration |

## References

- [Lift-and-Shift of High Write-Throughput Apps](https://www.yugabyte.com/blog/lift-and-shift-high-write-throughput-apps/)
- [YugabyteDB Documentation](https://docs.yugabyte.com/)
- [skills.sh](https://skills.sh/) — The Open Agent Skills Ecosystem
