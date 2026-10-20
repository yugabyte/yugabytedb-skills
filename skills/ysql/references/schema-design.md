# YSQL Schema Design

The SQL snippets illustrate separate schema choices, not one migration to run in sequence. Index examples assume the named tables and columns exist; hash-sharding examples use non-colocated tables.

## Primary Key Strategy

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

## Sharding: Hash vs Range
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

## Tablet Count Management

Minimize total tablet count to reduce overhead:
- Default to the database's initial tablet count for average workloads; omitting `SPLIT INTO` does not necessarily create a single tablet
- Pre-split (`SPLIT INTO`) only for high ingest/high throughput workloads where starting tablet count materially changes initial performance
- YugabyteDB auto-splits as data grows; start conservative and expand when telemetry justifies it
- Colocate small, low-access tables when practical

## Colocation

Fully colocated databases suit datasets whose storage and write workload fit a single tablet. The [colocation guide](https://docs.yugabyte.com/stable/additional-features/colocation/) gives databases below 50 GB as a typical use case, not a capacity guarantee. Colocated tablets do not auto-split; opt large or write-heavy tables out of colocation.

```sql
CREATE DATABASE myapp WITH colocation = true;
-- Tables share a colocation tablet by default, reducing distributed reads
-- Opt out large/high-throughput tables: WITH (colocation = false)
```

Set `WITH (colocation = false)` when creating a hot table. Moving an existing table out of colocation requires creating a replacement and migrating data with a planned cutover; it is not an in-place storage-option change. Preserve constraints, indexes, privileges, and dependencies when recreating the table.

## Index Design

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
- Global ordered reads need merging across buckets. Automatic merge scans depend on the release and its planner settings (`yb_max_merge_scan_streams`, formerly `yb_max_saop_merge_streams`, plus the derived-predicate settings `yb_enable_derived_saops` and `yb_enable_derived_equalities`, introduced alongside it in the v2025.2 series); inspect `EXPLAIN` for merge-stream fields rather than assuming a sort-free plan. See [bucket-based indexes](https://docs.yugabyte.com/stable/develop/data-modeling/bucket-based-index-ysql/) and the target release notes for setup

## Geo-Distribution

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

## Partitioning

If your source system is partitioned only for size management, consider wider partitions or no partitions — YugabyteDB naturally shards relations. Only use partitioning for geo-distribution or time-based data lifecycle (detach + drop old partitions instead of DELETE).

Keep partition counts low. Very fine-grained partitions (for example, daily partitions retained for years) create unnecessary tablet overhead.

If partitioning provides only marginal value for your workload, keep the model simpler.

## Data Type Guidance
- **JSONB:** Use only for truly dynamic schema scenarios. Regular columns outperform JSONB for frequent access patterns.

## Row and Column Size Guidance
- **Row size:** Prefer less than 10 MB for consistent latency; 32 MB is the documented maximum, not a latency target. See [data-modeling limits](https://docs.yugabyte.com/stable/develop/best-practices-develop/data-modeling-perf/)
- **Column size:** Target 2 MB or less per column
