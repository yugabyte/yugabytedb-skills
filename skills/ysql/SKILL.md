---
name: ysql
description: Use when writing or reviewing SQL, schema definitions, or application code that targets YugabyteDB's PostgreSQL-compatible YSQL API (port 5433). Triggers on CREATE TABLE, indexes, connections, transactions, sharding, migrations from PostgreSQL, or any mention of YugabyteDB with SQL.
---

# YugabyteDB YSQL Best Practices

**This skill includes:**
- `references/smart-drivers.md` — connection examples for Python, Java, Go, Node.js
- `references/retry-patterns.md` — transaction retry code in Python and Java

YugabyteDB is a distributed, PostgreSQL-compatible database (YSQL on port 5433) that is **ACID-compliant**, **highly available**, **horizontally scalable**, and supports **hash/range sharding** of tables and indexes. Every design choice should balance read efficiency, write scalability, and operational cost.

Connection URI example: `postgresql://yugabyte:yugabyte@localhost:5433/yugabyte`

Important: one connection endpoint by itself does not guarantee load balancing. Use driver-native topology/load-balance features where available, and combine with infrastructure load balancing (for example CSP/Kubernetes/Istio) when needed.

## Schema Design

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

**Decision guide:** natural distributed key first; UUID second; IDENTITY + HASH when business process requires ordered IDs.

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
- Perfect for even data distribution across nodes
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
- Default to no pre-splitting for average workloads
- Pre-split (`SPLIT INTO`) only for high ingest/high throughput workloads where starting tablet count materially changes initial performance
- YugabyteDB auto-splits as data grows; start conservative and expand when telemetry justifies it
- Colocate small, low-access tables when practical

### Colocation

Fully colocated databases are typically best for smaller deployments (roughly under 300 GB) with low sustained write throughput. If any table becomes write-heavy, uncolocate that table.

```sql
CREATE DATABASE myapp WITH colocation = true;
-- All tables colocated by default; JOINs between them are local
-- Opt out large/high-throughput tables: WITH (colocation = false)
```

Avoid colocating tables that receive disproportionately high load; explicitly uncolocate those hot tables.

### Index Design

In YugabyteDB every index miss or main table fetch is a **network RPC to another node** — not a local disk read. This makes index strategy far more impactful than in single-node PostgreSQL. Always design indexes from your actual query workload:

```sql
-- Prioritize by total_time first
SELECT queryid, calls, total_time, mean_time
FROM pg_stat_statements
ORDER BY total_time DESC
LIMIT 20;

-- Then validate plan quality and rows scanned
EXPLAIN (ANALYZE, DIST, COSTS) SELECT ...;  -- check for Seq Scan, Storage Read Requests, Storage Rows Scanned
```

**Covering indexes** — the single most important YugabyteDB index pattern. When appropriate, `INCLUDE` all columns your query SELECTs to eliminate main table fetch RPCs:
```sql
-- BAD for read-heavy query: requires heap fetch on every row
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

**Foreign key indexes** — always index the referencing (child) column. Without it, JOINs and CASCADE deletes trigger distributed sequential scans:
```sql
CREATE INDEX idx_orders_customer ON orders (customer_id);
```

**Index overhead** — each index adds write RPCs (data is written to index tablets too). Don't add indexes speculatively; verify queries need them via `pg_stat_statements` and `EXPLAIN (ANALYZE, DIST)`. Remove redundant indexes (two indexes starting with the same column).

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
- This design scales writes while still supporting ordered reads via merge patterns

### Geo-Distribution

**Tablespaces — pin data to regions:**
```sql
CREATE TABLESPACE us_east_ts WITH (
    replica_placement = '{"num_replicas": 3, "placement_blocks": [
        {"cloud": "aws", "region": "us-east-1", "zone": "us-east-1a", "min_num_replicas": 1},
        {"cloud": "aws", "region": "us-east-1", "zone": "us-east-1b", "min_num_replicas": 1},
        {"cloud": "aws", "region": "us-east-1", "zone": "us-east-1c", "min_num_replicas": 1}
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

### Partitioning

If your source system is partitioned only for size management, consider wider partitions or no partitions — YugabyteDB naturally shards relations. Only use partitioning for geo-distribution or time-based data lifecycle (detach + drop old partitions instead of DELETE).

Keep partition counts low. Very fine-grained partitions (for example, daily partitions retained for years) create unnecessary tablet overhead.

If partitioning provides only marginal value for your workload, keep the model simpler.

### Data Type Guidance
- **JSONB:** Use only for truly dynamic schema scenarios. Regular columns outperform JSONB for frequent access patterns.

### Row and Column Size Limits
- **Row size:** Keep in the 32 MB range or less for consistent latency
- **Column size:** Target 2 MB or less per column

## Application Patterns

### Smart Drivers (Client-Side Load Balancing)
Without smart drivers, the application connects to a single address — it has no awareness of cluster topology and cannot distribute connections across nodes. Always use `load_balance=true` and `topology_keys` for zone-aware routing.

> **Connection examples for Python, Java, Go, Node.js:** see [references/smart-drivers.md](references/smart-drivers.md)

Topology keys format: `cloud.region.zone:priority` (1=primary, 2=fallback, `*`=wildcard).

### Concurrency Control: Wait-on-Conflict

YugabyteDB defaults to **Wait-on-Conflict** — transactions queue on contention instead of immediate aborts, matching PostgreSQL semantics. Distributed deadlock detection is active automatically.

For highly contentious workloads, reducing `wait_queue_poll_interval_ms` can improve lock-wait responsiveness.

### Transaction Retry

`40001` (serialization failure) and `40P01` (deadlock) are still normal under write concurrency. Always implement client retries with rollback, exponential backoff, jitter, and bounded attempts (typically 3-10). Design write paths to be idempotent.

> **Retry code for Python and Java:** see [references/retry-patterns.md](references/retry-patterns.md)

### Timeouts (Always Set)
Set `statement_timeout` to at least 30 seconds, or about 2-3x your longest expected query in that context. Apply at connection/session scope when possible; database-level defaults are broader and should be used deliberately.

```python
conn = psycopg2.connect("...",
    options="-c statement_timeout=30000 -c idle_in_transaction_session_timeout=60000")
```

### Prepared Statements
Use protocol-level prepared statements (parameterized queries) rather than explicit `PREPARE`/`EXECUTE`. Protocol-level prepared statements are preferred because they maintain connection flexibility with server-side pooling (PgBouncer, YSQL Connection Manager).

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
- `ROWS_PER_TRANSACTION` (default 20000) — controls commit batch size. With `ROWS_PER_TRANSACTION 1000`, if error occurs at row 3500, committed batches (rows 1–3000) persist; the in-progress batch rolls back.
- `DISABLE_FK_CHECK` — skip foreign key checks for faster loads.
- `SKIP n` — resume after partial failure by skipping already-loaded rows.
- For colocated tables: `SET yb_fast_path_for_colocated_copy = on;` for faster ingestion.

### TRUNCATE Over Bulk DELETE
`DELETE` writes tombstones → compaction pressure. Use `TRUNCATE` for full cleanup.
For time-series: `ALTER TABLE events DETACH PARTITION old_partition; DROP TABLE old_partition;`

### Sequence CACHE
```sql
CREATE SEQUENCE order_seq CACHE 100;
```
Identity columns (`GENERATED ALWAYS AS IDENTITY`) already use an implicit sequence cache of 100 by default. Keep default settings for most workloads; tune explicit sequence cache or the tserver-level `ysql_sequence_cache_minval` flag only for sustained high-ingest patterns.

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
The Below is also useful for long-running jobs where stable reads and reduced cross-AZ chatter are priorities.

### Follower Reads (Low-Latency Stale Reads)
Route read-only queries to the nearest replica instead of the tablet leader:
```sql
SET yb_read_from_followers = true;
SET yb_follower_read_staleness_ms = 30000; -- 30s max staleness (min: 2× raft heartbeat = 1000ms)
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;
SELECT * FROM analytics WHERE region = 'us-east'; -- served from closest replica
```
- **Must be in a READ ONLY transaction** — follower reads are silently ignored in read-write transactions.
- Staleness applies even when reading from the leader — all reads are stale by `yb_follower_read_staleness_ms`.
- Ideal for dashboards, analytics, and read replicas where sub-second freshness is not required.

### yb_hash_code
Use `yb_hash_code` to chunk large READ/WRITE operations:
```sql
SELECT * FROM large_table WHERE yb_hash_code(id) BETWEEN 0 AND 5000;
SELECT * FROM large_table WHERE yb_hash_code(id) BETWEEN 5001 AND 10000;
```

Use this for chunking high-cardinality HASH columns.
For Range columns which need PQ, use PostgreSQL parallel query features where appropriate (`Parallel Seq Scan`, `Parallel Index Scan`, `Parallel Append`) to push true parallelism into query execution.

### Advisory Locks

All PostgreSQL advisory lock functions work. YugabyteDB-specific considerations:
- Locks are **distributed globally** via `pg_advisory_locks` system table (not shared memory) — visible across all nodes.
- **Session stickiness required:** session-level locks are tied to the backend connection. With smart drivers or connection pooling, ensure sessions stay on the same connection.

## PostgreSQL Migration Strategy

Apply these four levels progressively. Start with Level 1 — only advance when the workload demands it.

### Level 1: Lift and Shift (Always Do This)

- Replace unsupported features (see PG Feature Awareness table)
- Choose hash vs range sharding by access pattern; avoid range-leading monotonic keys for write-heavy paths
- Add missing indexes — in distributed environments, missing indexes cause larger performance loss than single-node
- Remove redundant indexes (two indexes on the same column are common in production)
- Use partial indexes for high null_frac columns

### Level 2: Optimize Read Latency

- Add covering indexes with all columns used by the query (key + `INCLUDE`)
- Use natural primary keys for read-heavy tables
- Index-only scans can slightly slow down updates if indexed columns are frequently modified — balance read optimization against write overhead

### Level 3: Linear Write Scalability

- Apply modulo-bucketed indexes on timestamp columns (see Index ordering and scalability above)
- **ORDER BY LIMIT caveat:** modulo-bucketed indexes don't produce globally ordered results. Merge each bucket's output:
```sql
SELECT * FROM (
    SELECT * FROM events WHERE (yb_hash_code(timestamp) % 3) = 0 ORDER BY timestamp DESC LIMIT 10
    UNION ALL
    SELECT * FROM events WHERE (yb_hash_code(timestamp) % 3) = 1 ORDER BY timestamp DESC LIMIT 10
    UNION ALL
    SELECT * FROM events WHERE (yb_hash_code(timestamp) % 3) = 2 ORDER BY timestamp DESC LIMIT 10
) sub ORDER BY timestamp DESC LIMIT 10;
```

Where available in your YugabyteDB version, evaluate planner settings that provide transparent merge / IN-list pushdown for bucketed lookups: Use Version 2025.2.Latest

```sql
ANALYZE events;
SET yb_max_saop_merge_streams = 64;
SET yb_enable_derived_saops = true;
SET yb_enable_derived_equalities = true;
SET yb_enable_cbo = on;

ALTER DATABASE yugabyte SET yb_max_saop_merge_streams = 64;
ALTER DATABASE yugabyte SET yb_enable_derived_saops = true;
ALTER DATABASE yugabyte SET yb_enable_derived_equalities = true;
ALTER DATABASE yugabyte SET yb_enable_cbo = on;
```

UNION ALL View is not required with saop enabled

### Level 4: End-to-End Optimization

- Analyze read/write ratio per object to balance indexing strategy using `pg_stat_statements`
- Use CTEs to reduce client-server round-trips and push logic into the database (note: CTEs can act as optimization fences preventing push-down — test with `EXPLAIN`)
- Use both hash and range sharding where needed for non-equality lookups

## Application Design

The application must not be a bottleneck — it must issue concurrency equal to the number of YugabyteDB cores:

- **Read-only workloads:** up to 8 concurrent threads per YugabyteDB core
- **Write-only workloads** (per-statement commits): 0.5–2 threads per YugabyteDB core
- A 4-thread application cannot saturate a 12-core YugabyteDB universe

## PG Feature Awareness

| PG Feature | YugabyteDB | Use Instead |
| --- | --- | --- |
| `SERIAL` / `BIGSERIAL` PK or `PRIMARY KEY (timestamp, ...)` with pure range sharding | Monotonic values append into the newest range tablet and hotspot writes | Use `PRIMARY KEY (... HASH)` or a non-monotonic natural key pattern for range-sharded primary keys |
| `CREATE UNLOGGED TABLE` | Silently ignored — all tables are Raft-replicated, `UNLOGGED` keyword accepted but has no effect | Regular table + `TRUNCATE` after processing |
| `EXCLUDE USING gist(...)`, or GiST/SP-GiST index plans | GiST/SP-GiST are not supported in YSQL | Use app-level validation, triggers, or alternate schema design |
| BRIN-only index strategies | BRIN is not supported | Use B-tree (and partial/composite indexes) based on query patterns |
| `xmin`, `xmax`, `ctid` | Unreliable — DocDB uses HLC-based MVCC, not heap tuple headers | Explicit `version INT` column for optimistic locking |
| `MERGE INTO` | Not supported | `INSERT ... ON CONFLICT ... DO UPDATE SET` |
| `PREPARE TRANSACTION` | Not implemented | Saga or outbox pattern |

`version INT` optimistic locking is valid in YugabyteDB. Keep it explicit in SQL (`WHERE version = ?`) and increment version in the same write statement.

For latitude/longitude search patterns, use SQL for broad pre-filtering/range scans and perform precise geo-distance checks in the middle tier if needed.

## Production Checklist

- **DDL safety:** Use a single connection for schema changes and allow catalog propagation time; avoid relying on transactional DDL unless that mode is explicitly enabled in your version/config.
- **Table-level locking:** Enable/use explicit table-level locking patterns when your migration or operational flow requires it.
- **DDL execution modes:** If supported by your YugabyteDB version, evaluate concurrent DDL and transactional DDL features for safer online schema changes.
- **TLS:** `sslmode=verify-full` + `sslrootcert`, `sslcert`, `sslkey` in connection string
- **Observability:** Log retry count/delay/SQLSTATE. Differentiate transient (40001, 40P01) vs terminal (42xxx, 23xxx). Monitor tablet leader distribution.
- **Optimistic locking:** `version INT` column, not system columns (`xmin`/`ctid`)
- **Connection pools:** Baseline: up to 15 server connections per vCPU; use 10 or fewer per vCPU for latency-sensitive workloads. Client pools may multiplex over fewer server connections.
- **Connection recycling:** When adding nodes, existing connections don't auto-route to new instances. Configure `maxLifetime` and `idleTimeout` to recycle connections periodically.
- **YSQL Connection Manager:** Built-in server-side connection pooler. Listens on port 5433, supports up to 10k client connections by default. Unlike PgBouncer, supports `SET` statements, `TEMP` tables, and prepared statements. Combine with smart drivers for topology-aware routing.
- **DDL migrations:** Use a single database connection for migration tools (Flyway, Active Record). DDL propagation across distributed caches can cause constraint violations with concurrent connections.
- **Extensions:** Pre-bundled — `pg_stat_statements` (enabled by default), `pgcrypto`, `pgvector`, `pg_cron`, `pg_partman`, `postgres_fdw`, `pgAudit`, `pg_trgm`, `pg_hint_plan`, `auto_explain`, `uuid-ossp`, `hstore`. Enable others with `CREATE EXTENSION <name>;`
