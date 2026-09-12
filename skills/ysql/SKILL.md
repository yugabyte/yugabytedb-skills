---
name: ysql
description: Use when writing or reviewing SQL, schema definitions, or application code that targets YugabyteDB's PostgreSQL-compatible YSQL API (port 5433). Triggers on CREATE TABLE, indexes, connections, transactions, sharding, migrations from PostgreSQL, or any mention of YugabyteDB with SQL.
---

# YugabyteDB YSQL Best Practices

**This skill includes:**
- `references/smart-drivers.md` — connection examples for all 9 smart drivers: Python (psycopg3, psycopg2), Java (JDBC, R2DBC), Go, Node.js, C#, Rust, Ruby
- `references/retry-patterns.md` — transaction retry code in Python and Java

YugabyteDB is a distributed, PostgreSQL-compatible database (YSQL on port 5433) that is **ACID-compliant**, **highly available**, **horizontally scalable**, and supports **hash/range sharding** of tables and indexes. Every design choice should balance read efficiency, write scalability, and operational cost.

Connection URI example: `postgresql://yugabyte:yugabyte@localhost:5433/yugabyte`

Important: one connection endpoint by itself does not guarantee load balancing. Use driver-native topology/load-balance features where available, and combine with infrastructure load balancing (for example CSP/Kubernetes/Istio) when needed.

## Schema Design

The SQL snippets illustrate separate schema choices, not one migration to run in sequence. Index examples assume the named tables and columns exist; hash-sharding examples use non-colocated tables.

### Primary Key Strategy

Prefer natural primary keys when they are stable and well distributed. Surrogate keys are often useful for legacy integration and interoperability, but they are not automatically the best primary lookup shape for distributed systems.

Avoid monotonically increasing leading key values for range-sharded access paths (sequences, timestamps, UUIDv7, lexicographically increasing text IDs).

**Option 1: Natural key with hash+range shape** — best when the domain already provides a stable key:
```sql
CREATE TABLE order_lines (
    order_id UUID,
    line_id INT,
    sku TEXT,
    qty INT,
    PRIMARY KEY ((order_id) HASH, line_id ASC)
);
```
This keeps all lines for one order together while distributing different orders evenly.

**Option 2: UUID surrogate key** — best when natural keys are unavailable:
```sql
CREATE TABLE orders (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    customer_id UUID NOT NULL,
    total DECIMAL(12,2),
    created_at TIMESTAMPTZ DEFAULT now()
);
```
UUIDv4-style randomness distributes writes evenly.

**Option 3: IDENTITY with HASH sharding** — best when sequential IDs are required:
```sql
CREATE TABLE tickets (
    id BIGINT GENERATED ALWAYS AS IDENTITY,
    title TEXT,
    PRIMARY KEY (id HASH)
);
```

This warning is specifically about using a single hashed tenant key (for example, `PRIMARY KEY ((tenant_id) HASH, ...)`) when tenant sizes are highly uneven. For large-tenant workloads, use a composite hash key (for example, `PRIMARY KEY ((tenant_id, order_id) HASH, ...)`), which distributes load much better.

**Decision guide:** natural distributed key first; UUID second; IDENTITY + HASH when numeric, sequence-generated IDs are required. Sequence caching and concurrent transactions mean these IDs are neither gapless nor ordered by commit time.

### Sharding: Hash vs Range
The primary schema difference from PostgreSQL is the choice between hash and range sharding on primary keys and indexes (`HASH` / `ASC` / `DESC`).

**Range sharding** (`ASC` / `DESC`):
- Preserves key order for range scans and `ORDER BY ... LIMIT`
- Monotonic inserts (`NOW()`, sequences, UUIDv7, increasing text IDs) append to the newest range tablet and create hot shards/uneven tablet sizes

```sql
-- Hash PK distributes table writes; plain ASC timestamp index can still hotspot on monotonic inserts
CREATE TABLE sensor_events (
    event_id UUID DEFAULT gen_random_uuid(),
    sensor_id UUID NOT NULL,
    event_ts TIMESTAMPTZ NOT NULL,
    payload JSONB,
    PRIMARY KEY (event_id HASH)
);

CREATE INDEX idx_sensor_events_ts ON sensor_events (event_ts ASC);
```

Important: hash-sharding a table does not automatically prevent hotspotting in secondary indexes. A monotonic index key (for example `event_ts ASC`/`DESC`) can still concentrate writes on the newest index range.

**Hash sharding** (`HASH`):
- Distributes distinct hash keys across tablets; skewed traffic to one hash key can still hotspot
- Efficient point queries and multi-row lookups on the same hash
- Does not preserve ordering on the hash key
- Secondary indexes can still hotspot if their leading key is monotonic
- Use hash+range combinations when you need both write distribution and ordered retrieval

```sql
-- Hash distributes writes; range key preserves per-player order
CREATE TABLE player_transactions (
    player_id UUID,
    created_at TIMESTAMPTZ,
    txn_id UUID DEFAULT gen_random_uuid(),
    payload JSONB,
    PRIMARY KEY ((player_id) HASH, created_at DESC, txn_id ASC)
);
```

### Tablet Count Management

Minimize total tablet count to reduce overhead:
- Default to the database's initial tablet count for average workloads; omitting `SPLIT INTO` does not necessarily create a single tablet
- Pre-split (`SPLIT INTO`) only for high ingest/high throughput workloads where starting tablet count materially changes initial performance
- YugabyteDB auto-splits as data grows; start conservative and expand when telemetry justifies it
- Colocate small, low-access tables when practical

### Colocation

Fully colocated databases suit datasets whose storage and write workload fit a single tablet. The [colocation guide](https://docs.yugabyte.com/stable/additional-features/colocation/) gives databases below 50 GB as a typical use case, not a capacity guarantee. Colocated tablets do not auto-split; opt large or write-heavy tables out of colocation.

```sql
CREATE DATABASE myapp WITH colocation = true;
-- Tables share a colocation tablet by default, reducing distributed reads
-- Opt out large/high-throughput tables: WITH (colocation = false)
```

Set `WITH (colocation = false)` when creating a hot table. Moving an existing table out of colocation requires creating a replacement and migrating data with a planned cutover; it is not an in-place storage-option change. Preserve constraints, indexes, privileges, and dependencies when recreating the table.

### Index Design

In YugabyteDB, secondary-index scans can require additional storage requests to fetch base-table columns. These requests may cross nodes, and multiple row fetches can be batched; there is not necessarily one remote RPC per row. Design indexes from your actual query workload and measure storage requests:

```sql
-- PG15-based YSQL: prioritize by total execution time
SELECT queryid, calls, total_exec_time, mean_exec_time
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 20;

-- Then validate plan quality and rows scanned
EXPLAIN (ANALYZE, DIST, COSTS) SELECT ...;  -- check for Seq Scan, Storage Read Requests, Storage Rows Scanned
```

Older PG11-based YSQL exposes `total_time` and `mean_time` instead. Inspect the installed view (`\d pg_stat_statements`) before adapting monitoring queries; use the [query statistics reference](https://docs.yugabyte.com/stable/launch-and-manage/monitor-and-alert/query-tuning/pg-stat-statements/) for the target release.

**Covering indexes** — when appropriate, `INCLUDE` columns needed by the query that are not already available from the index, allowing an Index Only Scan to avoid base-table fetches:
```sql
-- Fetching device_type requires base-table access
CREATE INDEX idx_sessions_account_seen ON sessions (account_id, seen_at DESC);
SELECT account_id, seen_at, device_type
FROM sessions
WHERE account_id = $1
ORDER BY seen_at DESC
LIMIT 50;

-- GOOD for read-heavy query: Index Only Scan candidate
CREATE INDEX idx_sessions_account_seen_cover
ON sessions (account_id, seen_at DESC) INCLUDE (device_type);
```

Tradeoff: indexing frequently updated columns increases write amplification. Favor covering indexes on read-heavy, low-update columns.

**Composite indexes** — for multi-column queries. Column order matters: equality columns first, range columns last:
```sql
-- Better distributed example for YugabyteDB
CREATE INDEX idx_player_txn_player_created
ON player_transactions (player_id HASH, created_at DESC);

-- Uses index:
-- WHERE player_id = $1 AND created_at > now() - interval '7 days'
-- WHERE player_id = $1
-- Does not use index efficiently:
-- WHERE created_at > now() - interval '7 days'  -- missing equality on player_id

-- Covering variant:
CREATE INDEX idx_player_txn_cover
ON player_transactions (player_id HASH, created_at DESC) INCLUDE (payload);
```

Index your queries, not your columns. Avoid speculative single-column indexes that are never used by real query shapes.

**Partial indexes** — only index rows that queries actually need. Smaller index = less storage, faster writes, faster scans:
```sql
-- Only active users (soft-delete pattern)
CREATE INDEX idx_users_active_email ON users (email) WHERE deleted_at IS NULL;

-- Only pending orders (completed orders rarely queried by status)
CREATE INDEX idx_orders_pending ON orders (created_at DESC) WHERE status = 'pending';

-- Only non-null values (high null_frac columns)
CREATE INDEX idx_orders_shipped ON orders (shipped_at) WHERE shipped_at IS NOT NULL;

-- Check null fraction to decide: SELECT attname, null_frac FROM pg_stats WHERE tablename = 'orders';
```

**Foreign key indexes** — index the referencing (child) columns when parent updates/deletes or child lookups need them. Without a suitable existing index, checking or cascading a parent deletion can scan the child table. A compatible existing composite index may already serve this purpose:
```sql
CREATE INDEX idx_orders_customer ON orders (customer_id);
```

**Index overhead** — maintaining indexes adds storage writes and can add RPCs. Don't add indexes speculatively; verify queries need them via `pg_stat_statements` and `EXPLAIN (ANALYZE, DIST)`. A shared first column does not make two indexes redundant: compare the full keys, hash/range layout, sort order, predicates, included columns, and uniqueness or constraint dependencies before removing one.

**Index ordering and scalability**

How do you preserve ordered reads while scaling monotonic inserts on an ASC/DESC key?
Use a low-cardinality bucket prefix:

```sql
CREATE INDEX idx_events_bucket_ts
ON events ((yb_hash_code(timestamp) % 3) ASC, timestamp ASC)
SPLIT AT VALUES ((1), (2));
```

Bucket design recommendations:
- Start bucket count from expected write throughput (not only node count); increase as needed
- For unique index/PK designs, `yb_hash_code(...)` inputs should be a subset of unique key columns
- `SPLIT AT VALUES` is optional but recommended for predictable initial distribution
- Keep bucket expression first in the index key
- Bucketing by timestamp distributes distinct timestamp values; many rows sharing one timestamp still hit one bucket
- Global ordered reads need merging across buckets. Automatic merge scans depend on the release and its planner settings (`yb_max_merge_scan_streams`, formerly `yb_max_saop_merge_streams`, plus derived-predicate settings in earlier releases); inspect `EXPLAIN` for merge-stream fields rather than assuming a sort-free plan. See [bucket-based indexes](https://docs.yugabyte.com/stable/develop/data-modeling/bucket-based-index-ysql/) and the target release notes for setup

### Geo-Distribution

**Tablespaces — pin data to regions:**
The cluster must have tservers whose cloud/region/zone labels satisfy every placement block.
```sql
CREATE TABLESPACE us_east_ts WITH (
    replica_placement = '{"num_replicas": 3, "placement_blocks": [
        {"cloud": "aws", "region": "us-east-1", "zone": "us-east-1a", "min_num_replicas": 1},
        {"cloud": "aws", "region": "us-east-1", "zone": "us-east-1b", "min_num_replicas": 1},
        {"cloud": "aws", "region": "us-east-1", "zone": "us-east-1c", "min_num_replicas": 1}
    ]}'
);

CREATE TABLESPACE eu_west_ts WITH (
    replica_placement = '{"num_replicas": 3, "placement_blocks": [
        {"cloud": "aws", "region": "eu-west-1", "zone": "eu-west-1a", "min_num_replicas": 1},
        {"cloud": "aws", "region": "eu-west-1", "zone": "eu-west-1b", "min_num_replicas": 1},
        {"cloud": "aws", "region": "eu-west-1", "zone": "eu-west-1c", "min_num_replicas": 1}
    ]}'
);
```

**Row-level geo-partitioning:**
```sql
CREATE TABLE orders (
    id UUID DEFAULT gen_random_uuid(),
    region TEXT DEFAULT yb_server_region(),
    customer_id UUID,
    PRIMARY KEY (id, region)
) PARTITION BY LIST (region);

CREATE TABLE orders_us PARTITION OF orders FOR VALUES IN ('us-east-1') TABLESPACE us_east_ts;
CREATE TABLE orders_eu PARTITION OF orders FOR VALUES IN ('eu-west-1') TABLESPACE eu_west_ts;
```

`yb_server_region()` uses the connected tserver's region. Supply `region` explicitly when data placement follows the customer's region instead; an insert for a region without a partition fails. Include the region in lookup predicates to permit partition pruning. Create each partition's secondary indexes in its regional tablespace as well; a regional base table alone does not place its secondary indexes there. See [geo-partitioning](https://docs.yugabyte.com/stable/explore/multi-region-deployments/row-level-geo-partitioning/).

### Partitioning

If your source system is partitioned only for size management, consider wider partitions or no partitions — YugabyteDB naturally shards relations. Only use partitioning for geo-distribution or time-based data lifecycle (detach + drop old partitions instead of DELETE).

Keep partition counts low. Very fine-grained partitions (for example, daily partitions retained for years) create unnecessary tablet overhead.

If partitioning provides only marginal value for your workload, keep the model simpler.

### Data Type Guidance
- **JSONB:** Use only for truly dynamic schema scenarios. Regular columns outperform JSONB for frequent access patterns.

### Row and Column Size Guidance
- **Row size:** Prefer less than 10 MB for consistent latency; 32 MB is the documented maximum, not a latency target. See [data-modeling limits](https://docs.yugabyte.com/stable/develop/best-practices-develop/data-modeling-perf/)
- **Column size:** Target 2 MB or less per column

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
conn = psycopg.connect("...",
    options="-c statement_timeout=30000 -c idle_in_transaction_session_timeout=60000")

# psycopg2 (psycopg2-yugabytedb)
conn = psycopg2.connect("...",
    options="-c statement_timeout=30000 -c idle_in_transaction_session_timeout=60000")
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
SET yb_follower_read_staleness_ms = 30000; -- 30s; recommended lower bound: 2x raft heartbeat (1000ms with defaults)
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT * FROM analytics WHERE region = 'us-east'; -- eligible for follower reads
```
- **Must be in a READ ONLY transaction** — follower reads are silently ignored in read-write transactions.
- Staleness applies even when reading from the leader — all reads are stale by `yb_follower_read_staleness_ms`.
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

- Apply modulo-bucketed indexes on timestamp columns (see Index ordering and scalability above)
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

Bucket-based scan optimizations are version-dependent and were introduced as preview/early-access features in the v2025.2 series. For v2025.2.3 and later, evaluate these session settings with representative queries; the earlier `yb_max_saop_merge_streams` name is deprecated in favor of `yb_max_merge_scan_streams`. Check the deployed patch release and [bucket-based index documentation](https://docs.yugabyte.com/stable/develop/data-modeling/bucket-based-index-ysql/) before enabling them.

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
