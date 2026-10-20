---
name: ysql
description: Use when writing or reviewing SQL, schema definitions, or application code that targets YugabyteDB's PostgreSQL-compatible YSQL API (port 5433). Triggers on CREATE TABLE, indexes, connections, transactions, sharding, migrations from PostgreSQL, or any mention of YugabyteDB with SQL.
---

# YugabyteDB YSQL Best Practices

**This skill includes:**
- `references/smart-drivers.md` — connection examples for all 9 smart drivers: Python (psycopg3, psycopg2), Java (JDBC, R2DBC), Go, Node.js, C#, Rust, Ruby
- `references/retry-patterns.md` — transaction retry code in Python and Java
- `references/bnl-joins.md` — Batched Nested Loop join design implications, GUC defaults, and diagnosis
- `references/query-optimization.md` — worked example of `timestamp` vs `now()` blocking index pushdown
- `references/schema-design.md` — primary keys, hash vs range sharding, tablets, colocation, index design, geo-distribution, partitioning, size limits
- `references/voyager.md` — YugabyteDB Voyager migration assessment and schema conversion review

YugabyteDB is a distributed, PostgreSQL-compatible database (YSQL on port 5433) that is **ACID-compliant**, **highly available**, **horizontally scalable**, and supports **hash/range sharding** of tables and indexes. Every design choice should balance read efficiency, write scalability, and operational cost.

Connection URI example: `postgresql://yugabyte:yugabyte@localhost:5433/yugabyte`

Important: one connection endpoint by itself does not guarantee load balancing. Use driver-native topology/load-balance features where available, and combine with infrastructure load balancing (for example CSP/Kubernetes/Istio) when needed.

## Schema Design

Read [references/schema-design.md](references/schema-design.md) before writing or reviewing `CREATE TABLE`, `CREATE INDEX`, tablespaces, or partitioning. It covers primary key strategy, hash vs range sharding, tablet count, colocation, index design, geo-distribution, partitioning, and data type and size limits. Core rules:

- **Primary keys:** natural distributed key first; UUID second; `IDENTITY` with `PRIMARY KEY (id HASH)` when sequence-generated IDs are required. Avoid monotonically increasing leading keys (sequences, timestamps, UUIDv7) on range-sharded access paths.
- **Sharding:** `HASH` distributes writes and serves point lookups; `ASC`/`DESC` preserves order for range scans. Combine them (`PRIMARY KEY ((player_id) HASH, created_at DESC)`) for both. Hash-sharding a table does not stop a monotonic secondary index from hotspotting; use a bucketed index (`yb_hash_code(...) % N` first).
- **Tablets and colocation:** keep total tablet count low; pre-split only for high-ingest workloads. Colocate small databases; colocated tablets do not auto-split, so create hot tables `WITH (colocation = false)`.
- **Indexes:** index the query shapes in `pg_stat_statements`, not columns. Use covering (`INCLUDE`), composite (equality columns first), and partial indexes; index foreign-key child columns; verify with `EXPLAIN (ANALYZE, DIST)`.
- **Partitioning:** use it for geo-distribution or time-based lifecycle, not size management; keep partition counts low.
- **Sizes:** prefer rows under 10 MB (32 MB max) and columns of 2 MB or less; use JSONB only for truly dynamic schemas.

## Application Patterns

### Smart Drivers (Client-Side Load Balancing)
Use smart drivers for discovery of tservers and client-side connection balancing when the application can reach the discovered addresses. Enable load balancing; add topology keys when zone-aware routing is required. **Both parameter names vary by driver — use the exact pair for the driver in hand:**

| Driver | Enable load balancing | Topology keys |
| --- | --- | --- |
| psycopg2, pgx, rust-postgres, ruby-pg | `load_balance=true` | `topology_keys` |
| psycopg3 | `load_balance_hosts=true` | `topology_keys` |
| JDBC | `load-balance=true` | `topology-keys` |
| R2DBC | `loadBalanceHosts=true` | `topologyKeys` |
| node-postgres | `loadBalance: true` | `topologyKeys` |
| Npgsql | `Load Balance Hosts=true` | `Topology Keys` |

**psycopg3 topology keys restrict application connections to equally preferred placements; priority suffixes are unsupported and no cluster-wide fallback occurs for those connections.** Bootstrap and discovery control connections can contact other cluster nodes. Allow multiple zones (or a region’s `*` zone) when zone availability is required; if no permitted tserver is live, connection fails. Omit topology keys for cluster-wide balancing. For rust-postgres, opt into `fallback_to_topology_keys_only=true` only for strict placement requirements, with multiple allowed zones if needed. Read the [driver examples and failure guidance](references/smart-drivers.md) before configuring either policy.

**Python driver choice:** match the driver the codebase already uses. psycopg3 → `psycopg-yugabytedb` (import stays `psycopg`). psycopg2 → `psycopg2-yugabytedb`. Do not default to psycopg2 for new code — psycopg3 is the current driver.

**Check the environment before installing a Python smart driver.** Keep `psycopg-yugabytedb` separate from upstream `psycopg`, `psycopg-binary`, and `psycopg-c`; use system `libpq` for the fork. For psycopg2, choose one YugabyteDB variant without upstream psycopg2 packages. If a dependency such as `langchain_postgres` requires upstream psycopg3, preserve the working environment and surface the choice: separate workloads into different environments/processes, or migrate explicitly to upstream behavior. Read the [package choices and migration procedure](references/smart-drivers.md#psycopg3-traps) before changing packages or connection settings.

> **Connection examples for all 9 smart drivers (Python psycopg3/psycopg2, Java JDBC/R2DBC, Go, Node.js, C#, Rust, Ruby):** see [references/smart-drivers.md](references/smart-drivers.md)

For drivers other than psycopg3, topology keys use `cloud.region.zone[:priority]` (1=primary, 2=fallback). Use the cluster’s actual placement labels. JDBC, pgx, and node-postgres document a zone wildcard such as `aws.us-east.*:1` for region-wide routing; fetch the selected driver’s linked documentation before using wildcard or priority syntax. For R2DBC and Npgsql, use explicit zones unless wildcard support is verified in the source matching the selected package release, as described in [the driver reference](references/smart-drivers.md). Psycopg3 also supports a zone wildcard, but without a priority suffix (`aws.us-east.*`); cloud and region wildcards are rejected. See its [topology reference](https://docs.yugabyte.com/stable/develop/drivers-orms/python/yugabyte-psycopg3-reference/).

### Concurrency Control: Wait-on-Conflict

YSQL defaults to **Wait-on-Conflict** in v2024.1 and later (`enable_wait_queues=true`): conflicting transactions wait, although they can still abort after the blocker commits incompatible changes. Deadlock detection is enabled with wait queues unless explicitly disabled. Check these flags on older or customized deployments.

For highly contentious workloads, keep `wait_queue_poll_interval_ms` near its documented default; increasing it can worsen tail latency. Benchmark before reducing it, because more frequent polling adds overhead. See [concurrency control](https://docs.yugabyte.com/stable/architecture/transactions/concurrency-control/).

### Transaction Retry

`40001` (serialization failure) and `40P01` (deadlock) are still normal under write concurrency. Always implement client retries with rollback, exponential backoff, jitter, and bounded attempts (typically 3-10). Design write paths to be idempotent.

> **Retry code for Python and Java:** see [references/retry-patterns.md](references/retry-patterns.md)

### Timeouts
Choose `statement_timeout` and `idle_in_transaction_session_timeout` from the application's latency budget and legitimate query/transaction durations. The following 30s/60s values are examples, not minimums. Apply at connection/session scope when possible; database-level defaults affect more workloads.

```python
# psycopg3 (psycopg-yugabytedb)
import psycopg

conn = psycopg.connect("...",
    options="-c statement_timeout=30000 -c idle_in_transaction_session_timeout=60000")

# psycopg2 alternative (psycopg2-yugabytedb) - use one driver, not both:
# import psycopg2
# conn = psycopg2.connect("...",
#     options="-c statement_timeout=30000 -c idle_in_transaction_session_timeout=60000")
```

### Prepared Statements
Use parameter binding for query values. For reusable server-side plans, configure the driver's protocol-level prepared statements; parameterization alone does not guarantee plan reuse. YSQL Connection Manager supports protocol preparation without the stickiness caused by SQL `PREPARE`/`EXECUTE`. With PgBouncer, verify prepared-statement support for the deployed version, pooling mode, and configuration before enabling driver auto-prepare. See [Connection Manager best practices](https://docs.yugabyte.com/stable/additional-features/connection-manager-ysql/ycm-best-practices/).

### Batch Operations
Use batching/micro-batching wherever practical. Multi-row INSERT and INSERT ON CONFLICT generally outperform single-row operations by reducing network round-trips:

```sql
INSERT INTO orders (id, customer_id, total) VALUES
    (gen_random_uuid(), $1, $2),
    (gen_random_uuid(), $3, $4),
    (gen_random_uuid(), $5, $6);
```

### Bulk Loading with COPY
```sql
COPY orders FROM '/path/data.csv' WITH (FORMAT csv, HEADER, ROWS_PER_TRANSACTION 20000);
```
- `ROWS_PER_TRANSACTION` (default 20000) controls commit batch size for an eligible regular table when COPY runs standalone, outside an explicit transaction or multi-command batch. With `ROWS_PER_TRANSACTION 1000`, an error at row 3500 leaves committed rows 1–3000; the in-progress batch rolls back. Inside an explicit transaction, or for unsupported table/trigger configurations, the option is ignored with a warning. Check driver transaction mode and server warnings before relying on partial commits.
- The filename is on the connected YB-TServer. Use client-side `\copy` in ysqlsh, or the driver's COPY API, for a client-local file.
- `DISABLE_FK_CHECK` — skip foreign key checks only when referential integrity has already been verified.
- `SKIP n` — resume after partial failure by skipping already-loaded rows.
- For colocated tables, `SET yb_fast_path_for_colocated_copy = on;` enables a fast path only when COPY runs outside an explicit transaction, omits `ROWS_PER_TRANSACTION`, and the target has no triggers, rules, or foreign keys. Its atomic batch size follows `ysql_session_max_batch_size`, including index writes. See [COPY options and fast-path requirements](https://docs.yugabyte.com/stable/api/ysql/the-sql-language/statements/cmd_copy/).

### TRUNCATE Over Bulk DELETE
`DELETE` writes tombstones that add compaction work. Consider `TRUNCATE` for full cleanup only when its locking, foreign-key, and transaction behavior fits the operation. The [YSQL TRUNCATE documentation](https://docs.yugabyte.com/stable/api/ysql/the-sql-language/statements/ddl_truncate/) cautions against use in multi-step transactions or concurrently with reads/writes; use transactional `DELETE` when those semantics are required.
For time-series: `ALTER TABLE events DETACH PARTITION old_partition; DROP TABLE old_partition;`

### Sequence CACHE
```sql
CREATE SEQUENCE order_seq CACHE 100;
```
Identity columns use an implicit sequence. The effective allocation cache is at least `ysql_sequence_cache_minval` (default 100), unless that minimum is disabled; a larger sequence `CACHE` takes precedence. Tune cache size and connection-versus-server caching for the workload, and expect gaps after rollback or loss of cached values. See [sequence caching](https://docs.yugabyte.com/stable/api/ysql/the-sql-language/statements/ddl_create_sequence/).

### EXPLAIN (ANALYZE, DIST)
```sql
EXPLAIN (ANALYZE, DIST, COSTS) SELECT * FROM orders WHERE customer_id = $1;
```
Key metrics: `Storage Read Requests` (RPCs), `Storage Rows Scanned`, and scan type.

`Index Scan` is often fine when returning few rows. For larger projections, `Index Only Scan` can reduce resource use and improve latency when the index covers selected columns.

### Join Strategies: Nested Loop vs Batched Nested Loop (BNL)

YugabyteDB supports the standard PostgreSQL join methods (Nested Loop, Hash Join, Merge Join) plus a YB-specific one: **Batched Nested Loop Join (BNL)**, which batches inner-table lookups into `= ANY(ARRAY[...])` calls instead of one RPC per outer row.

> **Design implications, GUC defaults, and diagnosing a plan that should batch but doesn't:** see [references/bnl-joins.md](references/bnl-joins.md)

### Long-Running Read Snapshots
For batch jobs that need consistent reads without contention:
```sql
BEGIN TRANSACTION ISOLATION LEVEL SERIALIZABLE READ ONLY DEFERRABLE;
-- long-running queries here
COMMIT;
```
Acquiring the snapshot can wait out the configured maximum clock skew (500ms by default). This avoids read-restart errors at the cost of startup latency. Follower reads below are a separate choice for workloads that tolerate stale results. See [read-restart mitigation](https://docs.yugabyte.com/stable/architecture/transactions/read-restart-error/).

### Follower Reads (Low-Latency Stale Reads)
Allow read-only queries to use a nearby replica; lagging or unavailable followers can cause fallback to another replica or the leader:
```sql
SET yb_read_from_followers = true;
SET yb_follower_read_staleness_ms = 30000; -- 30s; check clock-skew constraints before reducing
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT * FROM analytics WHERE region = 'us-east'; -- eligible for follower reads
```
- **Must be in a READ ONLY transaction** — follower reads are silently ignored in read-write transactions.
- Staleness applies even when reading from the leader — all reads are stale by `yb_follower_read_staleness_ms`.
- When reducing staleness, satisfy the deployed release's clock-skew constraint as well as the documented heartbeat recommendation. The [2025.2 transaction manager](https://github.com/yugabyte/yugabyte-db/blob/2025.2/src/yb/yql/pggate/pg_txn_manager.cc#L333-L339) requires `yb_follower_read_staleness_ms * 1000 > 2 * max_clock_skew_usec`: with the default 500,000 microseconds of maximum clock skew, use strictly more than 1000 ms.
- Ideal for dashboards, analytics, and read replicas where sub-second freshness is not required.
- These session settings also affect later transactions; reset them before returning a connection to a pool. Follower reads do not by themselves give multiple READ COMMITTED statements one stable snapshot. See [follower-read behavior](https://docs.yugabyte.com/stable/develop/build-global-apps/follower-reads/) and [isolation interaction](https://docs.yugabyte.com/stable/architecture/transactions/read-committed/).

### yb_hash_code
Use `yb_hash_code` to chunk large READ/WRITE operations:
```sql
SELECT * FROM large_table WHERE yb_hash_code(id) BETWEEN 0 AND 5000;
SELECT * FROM large_table WHERE yb_hash_code(id) BETWEEN 5001 AND 10000;
```

Pass all HASH key columns to `yb_hash_code`, in their key order, for efficient partition-bound scans. These two ranges are only a partial example: a complete job must cover 0 through 65535 without gaps or overlaps. For range-sharded tables, chunk by key bounds; evaluate the deployed release's [parallel-query support](https://docs.yugabyte.com/stable/additional-features/parallel-query/) and confirm the actual plan with `EXPLAIN`.

### Advisory Locks

Advisory locks are available and enabled by default in v2025.1 and later; verify the advisory-lock flags for the deployed release. They use PostgreSQL's advisory-lock functions. YugabyteDB-specific considerations:
- Locks are **distributed globally** via `pg_advisory_locks` system table (not shared memory) — visible across all nodes.
- **Session stickiness required:** session-level locks are tied to the backend connection. With smart drivers or connection pooling, ensure sessions stay on the same connection.
- Prefer transaction-level advisory locks for transaction-pooled work; session-level locks need a backend retained until unlock. See [explicit locking](https://docs.yugabyte.com/stable/explore/transactions/explicit-locking/).

## PostgreSQL Migration Strategy

Apply these four levels progressively. Start with Level 1 — only advance when the workload demands it.

### Level 1: Lift and Shift (Always Do This)

- Replace unsupported features (see PG Feature Awareness table)
- Choose hash vs range sharding by access pattern; avoid range-leading monotonic keys for write-heavy paths
- Add missing indexes — in distributed environments, missing indexes cause larger performance loss than single-node
- Remove redundant indexes after comparing keys, ordering, predicates, included columns, and constraint dependencies.
- Consider partial indexes for mostly-null columns when the query predicate implies the index predicate.

### Level 2: Optimize Read Latency

- Consider covering indexes (key + `INCLUDE`) for important queries, balancing index width and write cost.
- Consider natural primary keys when they are compact, stable, and match frequent lookups.
- Covering indexes add maintenance work when their key or included columns change; balance read savings against write overhead.

### Level 3: Linear Write Scalability

- Apply modulo-bucketed indexes on timestamp columns (see "Index ordering and scalability" in [references/schema-design.md](references/schema-design.md#index-design))
- **ORDER BY LIMIT caveat:** modulo-bucketed indexes don't produce globally ordered results. Merge each bucket's output:
```sql
SELECT * FROM (
    (SELECT * FROM events WHERE (yb_hash_code(timestamp) % 3) = 0 ORDER BY timestamp DESC LIMIT 10)
    UNION ALL
    (SELECT * FROM events WHERE (yb_hash_code(timestamp) % 3) = 1 ORDER BY timestamp DESC LIMIT 10)
    UNION ALL
    (SELECT * FROM events WHERE (yb_hash_code(timestamp) % 3) = 2 ORDER BY timestamp DESC LIMIT 10)
) sub ORDER BY timestamp DESC LIMIT 10;
```

Bucket-based scan optimizations are version-dependent and were introduced as preview/early-access features in the v2025.2 series. For v2025.2.3 and later, evaluate these session settings with representative queries; the earlier `yb_max_saop_merge_streams` name is deprecated in favor of `yb_max_merge_scan_streams`. Check the deployed patch release and [bucket-based index documentation](https://docs.yugabyte.com/stable/develop/data-modeling/bucket-based-index-ysql/) before enabling them. `yb_max_merge_scan_streams`, `yb_enable_derived_saops`, and `yb_enable_derived_equalities` are defined only in the v2025.2 series and later; on an earlier release each `SET` fails with `unrecognized configuration parameter`, so the block aborts at the first missing setting rather than degrading. Confirm the deployed release before running it as a block.

```sql
ANALYZE events;
SET yb_max_merge_scan_streams = 64;
SET yb_enable_derived_saops = true;
SET yb_enable_derived_equalities = true;
SET yb_enable_cbo = on;
```

For eligible plans, the optimizer merges bucket streams, so a manual `UNION ALL` is unnecessary. Verify `Merge Sort Key`, `Merge Stream Key`, and `Merge Streams` in `EXPLAIN`; do not assume every query qualifies. Apply database-wide defaults only after validating the workload. Add a unique tie-breaker to every ordering if deterministic results among equal timestamps are required.

### Level 4: End-to-End Optimization

- Use `pg_stat_statements` for query-level workload measurements and table statistics for per-table activity when balancing indexes.
- Use CTEs to reduce client-server round-trips and push logic into the database (note: CTEs can act as optimization fences preventing push-down — test with `EXPLAIN`)
- Use both hash and range sharding where needed for non-equality lookups

## Application Design

Load-test application concurrency with representative query costs, network latency, and read/write mix. Increase in-flight work until throughput stops improving or latency exceeds the target; thread count is not a universal multiple of database cores, especially with async clients or parallel queries. Monitor database CPU, pool wait time, and p99 latency together. See [connection and cluster sizing](https://docs.yugabyte.com/stable/additional-features/connection-manager-ysql/ycm-best-practices/).

## PG Feature Awareness

| PG Feature | YugabyteDB | Use Instead |
| --- | --- | --- |
| `SERIAL` / `BIGSERIAL` PK or `PRIMARY KEY (timestamp, ...)` with pure range sharding | Monotonic values append into the newest range tablet and hotspot writes | Use `PRIMARY KEY (... HASH)` or a non-monotonic natural key pattern for range-sharded primary keys |
| `CREATE UNLOGGED TABLE` | Current YSQL accepts and ignores `UNLOGGED` for distributed tables; older releases may reject it | Regular replicated table; choose `TRUNCATE` or transactional `DELETE` for cleanup as described above |
| `EXCLUDE USING gist(...)`, or GiST/SP-GiST index plans | GiST/SP-GiST are not supported in YSQL | Redesign with supported constraints, or serialize competing validation/writes; a check-then-insert in an app or trigger alone can race |
| BRIN-only index strategies | BRIN is not supported | Use B-tree (and partial/composite indexes) based on query patterns |
| `xmin`, `xmax`, `ctid` | PostgreSQL heap/MVCC system-column behavior is unsupported for distributed YSQL tables | Explicit `version INT` column for optimistic locking |
| `MERGE INTO` | Not supported in the documented YSQL compatibility limits | `INSERT ... ON CONFLICT ... DO UPDATE SET` for upserts; explicit transactional logic for other MERGE branches |
| `PREPARE TRANSACTION` | Not implemented | Saga or outbox pattern |

`version INT` optimistic locking is valid in YugabyteDB. Compare the expected version and increment it in the same write statement, then check that one row was updated. Bind the expected value using the driver's placeholder syntax (`?` for JDBC, `%s` for psycopg).

Check [migration compatibility limits](https://docs.yugabyte.com/stable/yugabyte-voyager/known-issues/postgresql/) against the target release; some entries describe older versions and include a "Fixed In" release.

For latitude/longitude search patterns, use SQL for broad pre-filtering/range scans and perform precise geo-distance checks in the middle tier if needed.

## Production Checklist

- **DDL safety:** Use a single connection for schema changes and allow catalog propagation time; avoid relying on transactional DDL unless that mode is explicitly enabled in your version/config.
- **Table-level locking:** Enable/use explicit table-level locking patterns when your migration or operational flow requires it.
- **DDL execution modes:** If supported by your YugabyteDB version, evaluate concurrent DDL and transactional DDL features for safer online schema changes.
- **TLS:** Use `sslmode=verify-full` and the trusted CA via `sslrootcert`; add `sslcert`/`sslkey` when client-certificate authentication is required.
- **Observability:** Log retry count/delay/SQLSTATE. Retry known transient failures (`40001`, `40P01`); syntax/access-rule (`42xxx`) and integrity (`23xxx`) errors normally require correcting the operation, not blindly retrying. Monitor tablet leader distribution.
- **Optimistic locking:** `version INT` column, not system columns (`xmin`/`ctid`)
- **Connection pools:** Yugabyte's [sizing guidance](https://docs.yugabyte.com/stable/additional-features/connection-manager-ysql/ycm-best-practices/) gives 15 server connections per vCPU as a baseline maximum, not a throughput target. Load-test lower concurrency for latency-sensitive workloads and account for memory. A server-side pooler can multiplex many client connections over fewer backends.
- **Connection recycling:** Adding nodes does not move existing connections. Configure the pool's lifetime/idle recycling settings (for example, HikariCP `maxLifetime` and `idleTimeout`) so new connections can use newly discovered nodes.
- **YSQL Connection Manager:** Built-in server-side connection pooler; verify its configured listener port and client limit. Session features have different pooling costs: TEMP tables and SQL `PREPARE` make connections sticky; protocol preparation avoids that cause of stickiness. Check [setup and limitations](https://docs.yugabyte.com/stable/additional-features/connection-manager-ysql/ycm-setup/) and combine with smart drivers for topology-aware connection routing.
- **DDL migrations:** Use a single database connection for migration tools (Flyway, Active Record). DDL propagation across distributed caches can cause constraint violations with concurrent connections.
- **Extensions:** Check the deployed release's [bundled extensions and modules](https://docs.yugabyte.com/stable/explore/ysql-language-features/pg-extensions/) and their setup requirements. Examples include `pg_stat_statements`, `pgcrypto`, pgvector (SQL name `vector`), `pg_cron`, `pg_partman`, `postgres_fdw`, `pgaudit`, `pg_trgm`, `pg_hint_plan`, `uuid-ossp`, and `hstore`. SQL extensions generally need `CREATE EXTENSION`; some also need preload/configuration. `auto_explain` is a loadable module, not a `CREATE EXTENSION` target.

## Query Optimization

**`timestamp` (without time zone) vs `now()`:** `now()` is `timestamptz`; comparing it to a `timestamp` column forces an implicit conversion that blocks index-condition pushdown (visible as high `Rows Removed by Index Recheck` / `Storage Index Rows Scanned` ≫ rows returned). Fix by defining the column as `timestamptz` (preferred) or casting in the predicate (`now()::timestamp - interval …`). Worked `EXPLAIN (ANALYZE, DIST)` example: [references/query-optimization.md](references/query-optimization.md).
