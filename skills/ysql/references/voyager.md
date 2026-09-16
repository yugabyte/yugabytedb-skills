# YugabyteDB Voyager — migration assessment and schema conversion

Voyager is Yugabyte's open-source migration tool. It handles the full migration lifecycle: assessment, schema export/import, and data export/import. This reference covers **assessment** and **schema conversion review** — the steps that determine what needs to change before moving a PostgreSQL (or Oracle/MySQL) database to YugabyteDB.

Install: `pip install yb-voyager` or use the Docker image. Full installation guide: https://docs.yugabyte.com/preview/yugabyte-voyager/install-yb-voyager/

---

## Step 1: assess-migration

Run the assessment against the **source** database before touching any schema. It generates a report without making any changes.

```bash
yb-voyager assess-migration \
  --source-db-type   postgresql \
  --source-db-host   <source-host> \
  --source-db-port   5432 \
  --source-db-user   <user> \
  --source-db-password <password> \
  --source-db-name   <dbname> \
  --source-db-schema public \
  --assessment-dir   ./assessment-output
```

For Oracle: `--source-db-type oracle`. For multiple schemas: comma-separate in `--source-db-schema`.

The assessment reads the source schema and statistics — it does **not** read table data.

### Assessment report

The report is generated at `./assessment-output/assessmentReport.html` (and `.json` for programmatic use). Key sections:

**Migration complexity:** `Easy` / `Medium` / `Hard` — based on the count and severity of unsupported features. This is a rough guide; always read the details.

**Unsupported features:** Objects and features that cannot be migrated as-is. These are blockers — they must be resolved before migration. Common examples:

| Feature | YugabyteDB status | Action |
|---|---|---|
| GiST / SP-GiST indexes | Not supported | Replace with B-tree; move geo-search to application layer or use pgvector |
| BRIN indexes | Not supported | Replace with B-tree composite or partial index |
| EXCLUDE constraints using GiST | Not supported | Implement constraint at application layer |
| `MERGE INTO` statement | Not supported | Replace with `INSERT ... ON CONFLICT DO UPDATE` |
| `PREPARE TRANSACTION` (2PC) | Not supported | Use saga or outbox pattern |
| `CREATE UNLOGGED TABLE` | Silently accepted but has no effect | Table will be replicated; remove `UNLOGGED` |
| Advisory locks | Distributed globally via system table (not shared memory) | Works, but session-level locks require sticky connections — review connection pooling |
| Stored procedures with `COMMIT`/`ROLLBACK` inside | Limited support | Refactor or test carefully |

**Migration caveats (non-blocking but requiring review):**

| Item | What to check |
|---|---|
| Sequences | Default `CACHE 1` in PostgreSQL → hotspot on high-ingest paths; change to `CACHE 100` or higher |
| Triggers | Complex triggers work but add cross-node RPC on write paths — review performance impact |
| Materialized views | Supported; `REFRESH MATERIALIZED VIEW` acquires a lock |
| Partitioned tables | Supported; review partition count — overly fine-grained partitions create tablet overhead |
| Full-text search (`tsvector`, `to_tsvector`) | Supported; GIN indexes on `tsvector` columns supported |
| `JSONB` with GIN indexes | GIN on JSONB: supported for `@>` and `?` operators; `jsonb_path_ops` supported |
| `SERIAL` / `BIGSERIAL` PKs | These are sequences — see sequence caveats above; also risk range-shard hotspot |
| `xmin`, `xmax`, `ctid` system columns | Unreliable in YugabyteDB — replace optimistic locking with explicit `version INT` column |

---

## Step 2: export and review the schema

```bash
yb-voyager export schema \
  --source-db-type   postgresql \
  --source-db-host   <source-host> \
  --source-db-port   5432 \
  --source-db-user   <user> \
  --source-db-password <password> \
  --source-db-name   <dbname> \
  --source-db-schema public \
  --export-dir       ./export-output
```

Converted DDL is written to `./export-output/schema/`. Review each file before importing to YugabyteDB.

---

## Step 3: schema conversion review checklist

Work through these in order after running `export schema`. Each item links to the relevant section in the main `ysql` SKILL.md for design guidance.

### 3a. Primary key and sharding strategy

Voyager defaults to **hash sharding** for most tables. Review each table:

1. **Tables where hash sharding is wrong:** Tables that are primarily queried with range predicates on the PK (`WHERE created_at BETWEEN ...`, `WHERE id > ? ORDER BY id LIMIT 100`) need `ASC`/`DESC` sharding — but **only if the PK is non-monotonic** or pre-split. A monotonic range-sharded PK (timestamp, sequence) will hotspot on writes.

2. **Tables where range sharding + a bucket is right:** High-ingest time-series tables where range queries are also needed. See `ysql` skill → Index ordering and scalability.

3. **Multi-tenant tables:** Ensure `tenant_id` is the first hash component: `PRIMARY KEY ((tenant_id) HASH, ...)`. For large tenants with uneven sizes, use a composite hash: `PRIMARY KEY ((tenant_id, entity_id) HASH, ...)`.

### 3b. Secondary indexes

Review every exported `CREATE INDEX` statement:

- **ASC/DESC index on a monotonic column** (timestamp, `created_at`, BIGSERIAL): will hotspot on writes — add a hash bucket prefix or redesign as described in `ysql` skill
- **Missing FK indexes:** Voyager may not add indexes on referencing (child) foreign key columns — scan the DDL for `FOREIGN KEY` references and verify each has a matching index
- **Speculative single-column indexes:** Remove indexes that don't match any real query shape in the application
- **Duplicate indexes:** Two indexes starting with the same column — remove the narrower one

### 3c. Sequences

For every `CREATE SEQUENCE` and `GENERATED ALWAYS AS IDENTITY` column:
```sql
-- After import, set appropriate cache on each sequence
ALTER SEQUENCE <seq_name> CACHE 100;
-- Or specify at creation:
CREATE SEQUENCE order_seq CACHE 100;
```
`IDENTITY` columns already default to cache 100 in YugabyteDB. Explicit sequences default to cache 1 unless changed.

### 3d. Partitioned tables

If the source schema uses date/time partitioning with many fine-grained partitions (e.g. daily partitions retained for years):
- Each partition creates tablets — multiply partition count × replication factor
- Consider wider partitions (monthly instead of daily) or no partitioning if YugabyteDB's native sharding makes it unnecessary
- Only partition for lifecycle management (detach + drop old partitions instead of DELETE) or geo-distribution

### 3e. Colocation (small databases)

If the database is small (< 300 GB total) with low sustained write throughput, consider creating it with colocation enabled:
```sql
CREATE DATABASE myapp WITH colocation = true;
```
JOINs between colocated tables become local reads. Opt out any table that becomes write-heavy: `CREATE TABLE hot_table (...) WITH (colocation = false)`.

---

## Step 4: import schema and verify

```bash
yb-voyager import schema \
  --target-db-host     <yb-host> \
  --target-db-port     5433 \
  --target-db-user     yugabyte \
  --target-db-password <password> \
  --target-db-name     <dbname> \
  --export-dir         ./export-output
```

After import, verify with `EXPLAIN (ANALYZE, DIST)` on a sample of the top queries (using representative data) before proceeding to data migration. Any Seq Scan on a table that will have > 100K rows is a schema issue to fix now — not after data is loaded.

---

## Data migration (overview)

After schema is confirmed:
```bash
# Export data from source
yb-voyager export data --source-db-type postgresql ... --export-dir ./export-output

# Import to YugabyteDB
yb-voyager import data --target-db-host <yb-host> ... --export-dir ./export-output
```

Run `ANALYZE` on all tables after data import, before performance testing:
```sql
ANALYZE;
```

For live cutover (minimal downtime) using change-data-capture:
```bash
yb-voyager export data-from-target ...  # CDC-based live migration
```

Refer to the Voyager documentation for the full live migration workflow: https://docs.yugabyte.com/preview/yugabyte-voyager/migrate/
