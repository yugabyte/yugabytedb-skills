---
name: ycql
description: Use when writing or reviewing CQL code, schema definitions, or application code that targets YugabyteDB's Cassandra-compatible YCQL API (port 9042). Triggers on YCQL tables, partition keys, TTL, batching, or any mention of YugabyteDB with Cassandra/CQL.
---

# YugabyteDB YCQL Best Practices

YCQL is YugabyteDB's Cassandra-compatible API (port 9042). It provides global secondary indexes with strong consistency (ACID) — a key advantage over Apache Cassandra.

## Schema Design

### Partition Keys and Clustering Columns
Design partition keys for even data distribution and clustering columns for efficient range scans within a partition:

```sql
CREATE TABLE orders (
    customer_id UUID,
    order_date TIMESTAMP,
    order_id UUID,
    total DECIMAL,
    PRIMARY KEY ((customer_id), order_date DESC, order_id)
) WITH CLUSTERING ORDER BY (order_date DESC, order_id ASC);
```

- **Partition key** (`customer_id`): Determines tablet placement. Choose for even distribution.
- **Clustering columns** (`order_date`, `order_id`): Determine sort order within a partition.

### Global Secondary Indexes
YCQL secondary indexes in YugabyteDB are global and strongly consistent (ACID), unlike Cassandra's local indexes:

```sql
CREATE INDEX idx_orders_date ON orders (order_date);
```

### Covering Indexes
Use the `INCLUDE` clause to serve queries directly from the index without a table lookup:

```sql
CREATE INDEX idx_orders_customer ON orders (customer_id) INCLUDE (total, order_date);
```

### Unique Indexes
```sql
CREATE UNIQUE INDEX idx_users_email ON users (email);
```

## Data Types

- **JSONB:** Supported for schema-less data, but use only for truly dynamic values. Regular columns outperform JSONB for frequent access patterns.
- **Counter increments:** YugabyteDB supports integer increment/decrement with CAS operations in a single Raft round-trip (vs 4 in Apache Cassandra).
- **Collections:** Design for small datasets. Large collections significantly impact performance.

## Size Limits

- **Columns:** Keep in the 2 MB range or less
- **Rows:** Keep in the 32 MB range or less

## TTL (Time-to-Live)

Automatic data expiration at table, row, or column level:

```sql
-- Table-level TTL
CREATE TABLE events (
    id UUID PRIMARY KEY,
    data TEXT
) WITH default_time_to_live = 86400;  -- 24 hours

-- Row-level TTL on insert
INSERT INTO events (id, data) VALUES (uuid(), 'event data') USING TTL 3600;

-- Column-level TTL
UPDATE events USING TTL 7200 SET data = 'updated' WHERE id = ?;
```

**Note:** TTL is not supported for transactional tables.

## Consistency Levels

YugabyteDB YCQL supports only two consistency levels: `QUORUM` (default) and `ONE`. Writes are always strongly consistent (`QUORUM`). Use `ONE` for follower reads (stale, lower-latency reads from nearest replica).

```java
// Default: QUORUM (strong consistency)
Statement stmt = SimpleStatement.newInstance("SELECT * FROM orders WHERE customer_id = ?", id)
    .setConsistencyLevel(ConsistencyLevel.ONE); // Read from nearest replica (may be stale)
```

## Lightweight Transactions (Atomic Read-Modify-Write)

`IF EXISTS` / `IF NOT EXISTS` operations are much faster than in Apache Cassandra — 1 Raft round-trip vs 4 LWT round-trips:

```sql
-- Atomic insert-if-not-exists
INSERT INTO users (id, email, name) VALUES (?, ?, ?) IF NOT EXISTS;

-- Atomic conditional update
UPDATE accounts SET balance = ? WHERE id = ? IF balance >= ?;
```

## Query Optimization

### Prepared Statements (Always Use)
Prepared statements enable partition-aware routing — the driver calculates the partition hash and sends the query directly to the correct tablet leader:

```java
PreparedStatement ps = session.prepare("SELECT * FROM orders WHERE customer_id = ?");
BoundStatement bs = ps.bind(customerId);
session.execute(bs);
```

### Batching
Batch operations send all operations in a single RPC call:

```java
BatchStatement batch = BatchStatement.newInstance(DefaultBatchType.UNLOGGED);
batch = batch.add(ps1.bind(...));
batch = batch.add(ps2.bind(...));
session.execute(batch);
```

Use batching to group operations that target the same partition for best performance.

### Connection Pooling
Use a single cluster object to manage connections. Typically 1–2 connections per YB-TServer is sufficient for 64–128 threads. The driver handles token-aware routing automatically when using prepared statements.

### Retry Policy
Default retry policy retries once on certain failures. For write-heavy workloads, configure a custom retry policy with backoff to handle transient tablet leader changes during load balancing.

## Large Table Operations

Use `partition_hash` to parallelize scans across tablets:

```sql
SELECT * FROM large_table WHERE partition_hash(id) >= 0 AND partition_hash(id) < 5000;
SELECT * FROM large_table WHERE partition_hash(id) >= 5000 AND partition_hash(id) < 10000;
```

## TRUNCATE vs DELETE

`TRUNCATE` is much faster than `DELETE`. DELETE inserts markers (tombstones) that require compaction. Use `TRUNCATE` for full-table cleanup.

## Memory Configuration

For YCQL-only deployments, set `--use_memory_defaults_optimized_for_ysql=false` on **yb-master** to avoid reserving memory for the PostgreSQL layer.
