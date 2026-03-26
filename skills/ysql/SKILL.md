---
name: ysql
description: Use when writing or reviewing SQL, schema definitions, or application code that targets YugabyteDB's PostgreSQL-compatible YSQL API (port 5433). Triggers on CREATE TABLE, indexes, connections, transactions, sharding, migrations from PostgreSQL, or any mention of YugabyteDB with SQL.
---

# YugabyteDB YSQL Best Practices

**This skill includes:**
- `references/smart-drivers.md` — connection examples for Python, Java, Go, Node.js
- `references/retry-patterns.md` — transaction retry code in Python and Java

YugabyteDB is a distributed, PostgreSQL-compatible database (YSQL on port 5433) that is **ACID-compliant**, **highly available**, **horizontally scalable**, and supports **hash/range sharding** of tables and indexes. **Every SQL operation may involve network RPCs between nodes.** Patterns free in single-node PostgreSQL (heap fetches, sequence increments) become expensive or impossible.

Connection: `postgresql://yugabyte:yugabyte@localhost:5433/yugabyte`

## Anti-Patterns — Never Use These PostgreSQL Features

| Feature | Why It Fails on YugabyteDB | Use Instead |
|---------|---------------------------|-------------|
| `SERIAL` / `BIGSERIAL` PK or `PRIMARY KEY (timestamp, ...)` with range sharding | Monotonic values → all inserts go to the same tablet → write hotspot | See "Primary Key Strategy" below |
| `CREATE UNLOGGED TABLE` | Silently ignored — all tables are Raft-replicated, `UNLOGGED` keyword accepted but has no effect | Regular table + `TRUNCATE` after processing |
| `EXCLUDE USING gist(...)` | GiST indexes not supported | App-level validation or triggers |
| `xmin`, `xmax`, `ctid` | Unreliable — DocDB uses HLC-based MVCC, not heap tuple headers | Explicit `version INT` column for optimistic locking |
| `USING gist/brin/spgist` | Only B-tree, Hash, GIN, and vector (pgvector) indexes supported | GIN for trigram/FTS: `USING gin (col gin_trgm_ops)` |
| `MERGE INTO` | Not supported | `INSERT ... ON CONFLICT ... DO UPDATE SET` |
| `PREPARE TRANSACTION` | Not implemented | Saga or outbox pattern |
## Schema Design

### Primary Key Strategy

The core problem: in a distributed database, all inserts to a monotonic (auto-increment) key go to the **same tablet** → write hotspot. Choose your PK based on the workload:

**Option 1: UUID** — best when IDs don't need to be human-readable or sequential:
```sql
CREATE TABLE orders (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    customer_id UUID NOT NULL,
    total DECIMAL(12,2),
    created_at TIMESTAMPTZ DEFAULT now()
);
```
UUIDs from `gen_random_uuid()` are random → writes distribute evenly across tablets with any sharding strategy.

**Option 2: IDENTITY with HASH sharding** — best when you need sequential, human-readable IDs:
```sql
CREATE TABLE tickets (
    id BIGINT GENERATED ALWAYS AS IDENTITY,
    title TEXT,
    PRIMARY KEY (id HASH)  -- HASH distributes monotonic values across tablets
);
```
The `HASH` keyword makes YugabyteDB hash the sequential value for tablet placement. Point lookups by ID still work efficiently.

**Option 3: Natural key** — best when a well-distributed business key already exists:
```sql
CREATE TABLE tenant_orders (
    tenant_id UUID,
    order_id UUID,
    data JSONB,
    PRIMARY KEY (tenant_id, order_id)  -- tenant_id distributes writes; order_id sorts within tenant
);
```

**Decision guide:** Use UUID when no natural key exists and human-readability doesn't matter. Use IDENTITY + HASH when you need auto-increment IDs (ticket numbers, invoice numbers). Use natural keys when the domain provides a well-distributed key (tenant_id, user_id).

### Sharding: Hash vs Range

The primary schema difference from PostgreSQL is the choice between hash and range sharding on primary keys and indexes (`HASH` / `ASC` / `DESC`).

**Range sharding** (`ASC` / `DESC`):
- Perfect for ordered queries and range scans (`BETWEEN`, `>`, `<`)
- Leads to hot shards when the key is monotonic since all inserts go to the same tablet

```sql
-- Range sharding for ordered queries
CREATE TABLE player_scores (
    score      BIGINT,
    player_id  UUID,
    username   TEXT,
    PRIMARY KEY (score ASC, player_id ASC)
);
```

**Hash sharding** (`HASH`):
- Perfect for even data distribution across nodes
- Efficient point queries and multi-row lookups on the same hash
- Limits range queries on the hashed column

```sql
-- Hash sharding for even data distribution across nodes
CREATE TABLE user_events (
    event_id UUID DEFAULT gen_random_uuid(),
    user_id  BIGINT,
    event_ts TIMESTAMPTZ,
    payload  JSONB,
    PRIMARY KEY (event_id HASH)
);
```


### Tablet Count Management

Minimize total tablet count to reduce overhead:
- **Colocation:** Group small tables into shared tablets via `CREATE DATABASE ... WITH colocation = true`
- **SPLIT INTO:** Control tablet count explicitly: `CREATE TABLE ... SPLIT INTO 8 TABLETS`
- **Automatic splitting:** YugabyteDB can auto-split tablets as data grows; start with fewer tablets for small tables

### Colocation

Small databases (under ~50 GB) with many tables waste resources when each table gets its own tablets. Colocation places all tables in shared tablets — JOINs between them are local:

```sql
CREATE DATABASE myapp WITH colocation = true;
-- All tables colocated by default; JOINs between them are local
-- Opt out large/high-throughput tables: WITH (colocation = false)
```
Avoid colocating tables that receive disproportionately high load — they will hotspot the shared tablet.

```sql
-- Explicit tablet splits
CREATE TABLE large_events (...) SPLIT INTO 16 TABLETS;
```

### Index Design

In YugabyteDB every index miss or heap fetch is a **network RPC to another node** — not a local disk read. This makes index strategy far more impactful than in single-node PostgreSQL. Always design indexes from your actual query workload:

```sql
-- Find queries that need indexes: high calls + high mean_time + high Storage Read Requests
EXPLAIN (ANALYZE, DIST, COSTS) SELECT ...;  -- check for Seq Scan, Storage Read Requests
SELECT query, calls, mean_time FROM pg_stat_statements ORDER BY mean_time * calls DESC;
```

**Covering indexes** — the single most important YugabyteDB index pattern. INCLUDE all columns your query SELECTs to eliminate heap fetch RPCs:
```sql
-- BAD: index finds rows, then heap fetch for name/email = extra RPCs per row
CREATE INDEX idx_users_status ON users (status);
SELECT status, name, email FROM users WHERE status = 'active';

-- GOOD: Index Only Scan — all data served from index, zero heap fetches
CREATE INDEX idx_users_status ON users (status) INCLUDE (name, email);
```
Every column not in the index forces a cross-node RPC to fetch the full row. If `EXPLAIN (ANALYZE, DIST)` shows `Storage Read Requests` much higher than rows returned, you need a covering index.

**Composite indexes** — for multi-column queries. Column order matters: equality columns first, range columns last:
```sql
-- Efficient: status (=) first, created_at (range) second
CREATE INDEX idx_orders_status_date ON orders (status, created_at DESC);

-- Uses index: WHERE status = 'pending' AND created_at > '2024-01-01'
-- Uses index: WHERE status = 'pending' (leftmost prefix)
-- DOES NOT use index: WHERE created_at > '2024-01-01' alone

-- Add INCLUDE for covering:
CREATE INDEX idx_orders_covering ON orders (status, created_at DESC) INCLUDE (customer_id, total);
```
Avoid separate single-column indexes for queries that filter on multiple columns — in distributed DB, combining two indexes is much more expensive than one composite index.

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

**Hotspot prevention** — for high-write timestamp indexes:
```sql
-- BEFORE: hotspot on timestamp-leading index
CREATE INDEX ON events (timestamp DESC);

-- AFTER: perfect write distribution across 3 buckets
CREATE INDEX ON events (
    (yb_hash_code(timestamp) % 3) ASC,
    timestamp DESC
) SPLIT AT VALUES ((0), (1), (2));
```
The modulo value determines node-placement buckets (can be increased later with a new index). A hotspot is acceptable if that object's throughput stays within a single node's capability.

**Natural primary keys** — when the domain provides a unique, well-distributed key, using it as PK avoids an extra surrogate column and index. The primary key IS the data, so point lookups need zero heap fetches:
```sql
CREATE TABLE account_balances (
    account_id UUID,
    currency TEXT,
    balance DECIMAL(18,2),
    updated_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (account_id, currency)
);
```

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

YugabyteDB defaults to **Wait-on-Conflict** — transactions queue on contention instead of aborting, matching PostgreSQL semantics. Distributed deadlock detection is active automatically. Combine with `lock_timeout` to bound wait time.

### Transaction Retry

`40001` (serialization failure) and `40P01` (deadlock) occur under write concurrency. With **Wait-on-Conflict** (default) they are rare; with **Fail-on-Conflict** they are **common**. Always ROLLBACK before retry. Exponential backoff + jitter. Bounded retries (3–10). Design for idempotency.

> **Retry code for Python and Java:** see [references/retry-patterns.md](references/retry-patterns.md)

### Timeouts (Always Set)
```python
conn = psycopg2.connect("...",
    options="-c statement_timeout=30000 -c idle_in_transaction_session_timeout=60000")
```

### Prepared Statements
Use protocol-level prepared statements (parameterized queries) rather than explicit `PREPARE`/`EXECUTE`. Protocol-level prepared statements are preferred because they maintain connection flexibility with server-side pooling (PgBouncer, YSQL Connection Manager).

### Batch Operations
Use multi-row INSERT and INSERT ON CONFLICT for superior throughput over individual row operations, since it reduces the number of network round-trips:
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
Identity columns (`GENERATED ALWAYS AS IDENTITY`) use an implicit sequence with CACHE 100. For high-throughput inserts, set `ysql_sequence_cache_minval` on the tserver or create an explicit sequence with larger CACHE.

### EXPLAIN (ANALYZE, DIST)
```sql
EXPLAIN (ANALYZE, DIST, COSTS) SELECT * FROM orders WHERE customer_id = $1;
```
Key metrics: `Storage Read Requests` (RPCs, lower=better), `Storage Rows Scanned`, `Index Only Scan` (ideal) vs `Seq Scan` (bad for large tables).


### Long-Running Read Snapshots
For batch jobs that need consistent reads without contention:
```sql
BEGIN TRANSACTION ISOLATION LEVEL SERIALIZABLE READ ONLY DEFERRABLE;
-- long-running queries here
COMMIT;
```

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

### Parallel Scans for Large Tables
Use `yb_hash_code` to parallelize large SELECT or DELETE operations across tablets:
```sql
SELECT * FROM large_table WHERE yb_hash_code(id) BETWEEN 0 AND 5000;
SELECT * FROM large_table WHERE yb_hash_code(id) BETWEEN 5001 AND 10000;
```

### Advisory Locks

All PostgreSQL advisory lock functions work. YugabyteDB-specific considerations:
- Locks are **distributed globally** via `pg_advisory_locks` system table (not shared memory) — visible across all nodes.
- **Session stickiness required:** session-level locks are tied to the backend connection. With smart drivers or connection pooling, ensure sessions stay on the same connection.

## PostgreSQL Migration Strategy

Apply these four levels progressively. Start with Level 1 — only advance when the workload demands it.

### Level 1: Lift and Shift (Always Do This)

- Replace unsupported features (see Anti-Patterns table above)
- Switch to range sharding by default; use hash sharding for sequential PKs
- Add missing indexes — in distributed environments, missing indexes cause larger performance loss than single-node
- Remove redundant indexes (two indexes on the same column are common in production)
- Use partial indexes for high null_frac columns

### Level 2: Optimize Read Latency

- Add covering indexes with all columns used by the query (key + `INCLUDE`)
- Use natural primary keys for read-heavy tables
- Index-only scans can slightly slow down updates if indexed columns are frequently modified — balance read optimization against write overhead

### Level 3: Linear Write Scalability

- Apply modulo-bucketed indexes on timestamp columns (see Hotspot Prevention above)
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

### Level 4: End-to-End Optimization

- Analyze read/write ratio per object to balance indexing strategy using `pg_stat_statements`
- Use CTEs to reduce client-server round-trips and push logic into the database (note: CTEs can act as optimization fences preventing push-down — test with `EXPLAIN`)
- Use both hash and range sharding where needed for non-equality lookups

## Application Design

The application must not be a bottleneck — it must issue concurrency equal to the number of YugabyteDB cores:

- **Read-only workloads:** up to 8 concurrent threads per YugabyteDB core
- **Write-only workloads** (per-statement commits): 0.5–2 threads per YugabyteDB core
- A 4-thread application cannot saturate a 12-core YugabyteDB universe

## Production Checklist

- **DDL safety:** Avoid DDL in transactions (not transactional by default). Single connection. Allow catalog propagation time.
- **TLS:** `sslmode=verify-full` + `sslrootcert`, `sslcert`, `sslkey` in connection string
- **Observability:** Log retry count/delay/SQLSTATE. Differentiate transient (40001, 40P01) vs terminal (42xxx, 23xxx). Monitor tablet leader distribution.
- **Optimistic locking:** `version INT` column, not system columns (`xmin`/`ctid`)
- **Connection pools:** Baseline: up to 15 server connections per vCPU; use 10 or fewer per vCPU for latency-sensitive workloads. Client pools may multiplex over fewer server connections.
- **Connection recycling:** When adding nodes, existing connections don't auto-route to new instances. Configure `maxLifetime` and `idleTimeout` to recycle connections periodically.
- **YSQL Connection Manager:** Built-in server-side connection pooler. Listens on port 5433, supports up to 10k client connections by default. Unlike PgBouncer, supports `SET` statements, `TEMP` tables, and prepared statements. Combine with smart drivers for topology-aware routing.
- **DDL migrations:** Use a single database connection for migration tools (Flyway, Active Record). DDL propagation across distributed caches can cause constraint violations with concurrent connections.
- **Extensions:** Pre-bundled — `pg_stat_statements` (enabled by default), `pgcrypto`, `pgvector`, `pg_cron`, `pg_partman`, `postgres_fdw`, `pgAudit`, `pg_trgm`, `pg_hint_plan`, `auto_explain`, `uuid-ossp`, `hstore`. Enable others with `CREATE EXTENSION <name>;`
