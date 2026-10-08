# YugabyteDB schema checks

Contents: 1) basic questions · 2) key choice · 3) defect catalog · 4) partitioning ·
5) judgement calls · 6) parse-time syntax · 7) per-index checklist · 8) index rollout

This file is the delta over general YSQL guidance: failure mechanisms and what reviews
commonly get wrong.

---

## 1. The basic questions

1. **What are the access patterns?** Actual SQL, with call rates. Without these, key
   choice is a guess, and the guess costs a migration to fix.
2. **Is the leading key column high-cardinality and not value-skewed?**
3. **If partitioned: does every latency-sensitive read supply the partition key?**
4. **Does each index earn its place against a numbered pattern?**

---

## 2. Key choice

|        Leading column used for        |              Choose               |
| ------------------------------------- | --------------------------------- |
| Equality only (`=`, `IN`)             | `HASH`                            |
| Ranges, `BETWEEN`, ordered pagination | `ASC` / `DESC`                    |
| Equality prefix + ordered suffix      | Hash the prefix, range the suffix |

Then check:

- **Cardinality and skew.** A hash key's space is no more granular than its distinct value
  count. Nine distinct values means ~nine hash codes no matter how many tablets exist.
  Where tenant sizes are uneven, a composite hash (`(tenant_id, order_id) HASH`)
  distributes far better than `(tenant_id) HASH` alone.
- **Monotonicity.** A monotonic leading *range* key (sequence, timestamp, UUIDv7,
  increasing text ID) sends every insert to the newest tablet. `ASC` also sorts NULLs
  last, so NULLs collect in the same place. Do not read it from `pg_stats.correlation`: YSQL
  samples rows in primary-key order, so correlation is about 1 for any range key's leading
  column and says nothing about insert order. Use the column's default (a sequence) or its
  type and name; write counters say how hot the table is, not whether inserts are ordered.
  Random IDs spread over the key range do not hot-spot.
- **Parenthesisation.** `PRIMARY KEY (a, b, c HASH)` is not `PRIMARY KEY ((a, b, c)
  HASH)`.
- **Prunability.** A composite hash group distributes by the *combination*, so it only
  prunes when a query supplies every column in the group with equality.

**Primary-key columns are implicitly `NOT NULL`** (SQL spec; `PRIMARY KEY` = `UNIQUE` +
`NOT NULL`). The nullable-hash-key problem below is a **secondary index** problem, not a
primary key one. Reviewers get this backwards regularly.

---

## 3. The defect catalog

Each entry is a mechanism, not a style preference. These are what to look for in a review.

- **Nullable leading hash column on a secondary index.** All NULLs produce one hash code.
For a hash-partitioned tablet the split point reduces to the hash code, so when the lower
bound and the median key share it, the split is refused and the tablet grows without
bound. **Diagnostic**: logs may show `TABLET_SPLIT_KEY_RANGE_TOO_SMALL` / "failed to
detect middle key". `pg_stats` will show high % of NULLs on the column. **Fix**: `WHERE
col IS NOT NULL` on the index, plus a split sized for the remaining rows. It is usually
safer to suggest to add `WHERE col IS NOT NULL` during index creation if the leading col
is nullable and there are not query patterns explicitly looking for NULLs.
**Precondition**: measure `null_frac` first — if the column is mostly non-null, this is
not your problem, but note that even 1% of 15B rows is 150M. **Caveat**: If the `IS NOT
NULL` is specified on a non-leading column or outside the `HASH` part, call that out as
that index may not be used when queries specify just the leading column.

- **Low-cardinality or value-skewed hash key.** Signature in a tablet report: most tablets
near-empty, one large. (The nullable case looks different — the others sit near the
median.) Usually the right answer is to drop the index; otherwise make it composite with a
high-cardinality column, or bucket on a different column. *Nuance:* low-cardinality `HASH`
and low-cardinality `ASC` are different problems. For a **range**-sharded index the split
key includes the hidden `ybidxbasectid` (the encoded base PK), so if the base table's PK
is hash the index can still split and distribute. It relies entirely on auto-split — you
cannot `SPLIT AT VALUES` on a hidden column — and if the base PK is range-sharded on a
monotonic column it inherits that ordering and concentrates at the tail. Treat `HASH` on
low cardinality as the hard defect and `ASC` as a ramp problem.

- **Hash sharding is not always the default.** An unannotated first key column is `HASH`
only when `yb_use_hash_splitting_by_default` is on and the relation is neither colocated nor
in a tablegroup (`pg_yb_utils.c`). Otherwise it is `ASC`. Enhanced Postgres Compatibility
Mode (EPCM) turns that setting off, so the default becomes `ASC` to match Postgres behavior. Users
completely miss this and assume `HASH` is the default and think that the table and indexes
will be HASH distributed. It is important to **explicitly** add the sharding scheme
(`HASH`/`ASC`/`DESC`) on the PRIMARY KEY to avoid surprises.

- **Monotonic leading range key on an index.** Hash-sharding the *table* does not protect
its indexes. `CREATE INDEX ... (event_ts ASC)` on a hash-PK table still concentrates
writes on the newest index range leading to a hot shard. **Fix**: a bucket-based index,
`(yb_hash_code(col) % N) ASC, col ASC`, with `SPLIT AT VALUES (1),(2)..(N-1)` pinning
buckets to different tablets — without that, N buckets are not guaranteed to land on N
tablets. Cost: any `ORDER BY col LIMIT n` needs a merge across buckets. Validate with
`EXPLAIN` before adopting.

- **Non-covering index on a hot read.** A secondary-index plan may need a further
distributed lookup to the base table: a network hop, not the local disk read it would be
on PostgreSQL. `INCLUDE` avoids that hop when the planner picks an index-only path, which
is often a material win. Do not model it as exactly one RPC per matching row, though:
batching, pushdown, locality, cache state and planner choice all vary. Audit the `INCLUDE`
list — unused payload columns still cost writes and storage.

- **Index proliferation.** Creating one index per SELECT query is an overkill as each
write will have to update all the indexes on the table and each index has its own RAFT
replication. If the query can be served by another index by adding a column to its PRIMARY
KEY, then that should be opted for.

- **Redundant indexes.** A shared first column does not make two indexes redundant. Compare
the full keys, HASH / range layout and hash group, sort order, partial-index predicates,
INCLUDE columns, and uniqueness or constraint dependencies; only an index that another one
serves in all of these respects is a candidate. Then confirm `idx_scan = 0` before dropping,
and ask the user to look at Perf Advisor for more redundant indexes.

- **Unused indexes.** It is common to create an index for a specific usecase and later the
usecase goes away. Some indexes may just lie around without being used and just add to
write latency. But there could be cases where some tables are created/populated and
indexes created for them but the tables themselves may not be active. But for those cases
where the table is active but the indexes are not used, such indexes should be suggested
to be dropped. This info should also be available in the Perf Advisor.

- **Unique index on a nullable column.** By default, NULL values are distinct, so multiple
NULL rows are permitted; use `NULLS NOT DISTINCT` when that is the intended uniqueness
semantics and supported by the target release. For Voyager, treat NULL/unique-index
conflict handling as version-sensitive: recent releases contain fixes in this area, so
verify the exact Voyager version rather than assuming a universal failure mode.

- **Hot rows and retry amplification.** A read-then-write pattern on contended rows
produces write/write conflicts and retries. If the client opens a *new connection* per
retry, contention escalates into an outage. **Important:** ask about client retry
behaviour explicitly.

- **Insufficient tablet splits.** Tables and Indexes could start with only 1 tablet
configured by the `ysql_num_tablets` gflag, which is totally fine in a normal scenario as
the tablet will auto split as it grows. But if the volume is high or it is high-growth
table, then it is important to pre-split the tables and indexes ahead of time into
multiple tablets using the `SPLIT INTO` clause. If the table has already been split, the
Index splits can be suggested based on that information. This is one of the very common
mistakes. Recommend explicit splits only for relations whose growth or ingest rate makes
the auto-split ramp itself the bottleneck; otherwise keep the default tablet count.

- **Table truncation.** During attempts to start re-migration (say using Voyager) on to an
existing database, it is natural to truncate the existing tables to get them to a clean
state. But this could void the tablet split information. So make sure the user is aware
that is important to recreate the tables from the original DDLs. The current tablet splits
can also be fetched via the `ysql_dump --schema-only --include-yb-metadata` command.

---

## 4. Partitioning

**Do you need it at all?** Sharding already distributes a large table. Partition for
retention (`DETACH` + `DROP` is the only lever that reduces tablet count), regional
placement, or lifecycle isolation — not because a table is big. Partitioning multiplies
relation and tablet count.

### The parent primary key

PostgreSQL requires a partitioned parent's `PRIMARY KEY` or `UNIQUE` constraint to contain
every partition key column. That rule is what drives the design, and it usually pushes you
one particular way.

Tables are typically partitioned by a **time column** but need uniqueness on an **id
column**. A parent PK would therefore have to be `(id, created_at)` — which does *not*
express the requirement. `(id, created_at)` unique permits the same `id` in two
partitions. So the composite parent PK buys you a constraint that doesn't match the intent
while still not giving global uniqueness.

Hence the common shape: **no PK on the parent, `PRIMARY KEY (id)` on each child**, created
separately and attached.

```sql
CREATE TABLE base_table (
    id          bigint      NOT NULL,
    created_at  timestamptz NOT NULL
) PARTITION BY RANGE (created_at);

CREATE TABLE base_table_p2027_01 (
    id          bigint      DEFAULT nextval('base_table_id_seq') NOT NULL,
    created_at  timestamptz NOT NULL,
    CONSTRAINT base_table_p2027_01_pkey PRIMARY KEY (id)
);

ALTER TABLE base_table ATTACH PARTITION base_table_p2027_01
    FOR VALUES FROM ('2027-01-01') TO ('2027-02-01');
```

**The caveat, and it is the whole caveat:** uniqueness of `id` is now enforced only
*within* each partition. Global uniqueness has to be guaranteed outside the constraint
system — by the application, or by allocating every `id` from a single shared sequence.
Write that down as an explicit design commitment, because nothing in the schema enforces
it. It also means no foreign key can reference the parent, and parent-level `ON CONFLICT
(id)` is not available.

Declare the parent PK instead only when `(id, partition_key)` genuinely *is* the
uniqueness requirement.

### Pruning is a separate question

**Fan-out.** `WHERE id = $1` with no `created_at` filter cannot prune, so the planner
probes every partition, and each probe is a network RPC. This follows from partitioning by
time and looking up by id — not from where the PK is declared.

> **Decision rule:** partition by time only if every latency-sensitive read supplies the
> partition key. If a hot path looks rows up by `id` alone, make the application pass the
> time bound.

**Screening heuristic:** flat latency — `p50 ≈ p90 ≈ p99` — on a lookup that should be a
point read suggests a fixed structural cost per call rather than contention. Divide by the
partition count and compare against a single-partition read; when they match, fan-out is
the likely cause. This is cheap and works on `pg_stat_statements` percentiles you already
have, which matters when you have no cluster access.

**Then confirm it.** A percentile shape is a pointer, not a diagnosis. Validate with
`EXPLAIN (ANALYZE, DIST)`, comparing a representative id-only lookup against one that
supplies the partition bound. Do not report fan-out on the latency shape alone.

### Mechanics

- **Nothing inherits a split.** Not partitions from parents, not indexes from tables. Each
  relation's tablet count is its own decision.
- **Verify inheritance rather than assuming it.** Hash annotations on a parent PK have
  historically not propagated correctly to children (yugabyte-db#6149: `PRIMARY KEY ((a,
  b) HASH)` landed as `(a HASH, b)`). Run `\d+` on the first child.
- **Runway and retention** need automation before go-live. Inserts into an uncreated month
  fail.
- **A partitioned table with zero partitions** fails every insert.

---

## 5. Decisions, not rules

These are judgement calls:

**Pre-splitting.** YugabyteDB splits tablets (both indexes and tables) automatically as
data grows, and also supports explicit pre-splitting at relation creation. Documented
guidance is to **default to no pre-split for average workloads** and minimise total tablet
count; pre-split when startup throughput means the auto-split ramp is itself the
bottleneck. Choose the initial count from startup throughput, topology, relation count and
expected growth — never by copying a number from an example. Note that tablet merge is not
available, so an over-split relation stays that way; confirm against the target release
before relying on either direction.

**Colocation.** Colocation reduces tablet overhead and network hops for small or related
relations, but the shared colocation tablet is one Raft group and can become a bottleneck
under disproportionate load. Judge dataset shape, aggregate throughput and IOPS, hotspot
risk, join patterns and latency targets. The colocation guide
(docs.yugabyte.com/stable/additional-features/colocation/) gives a typical case of a whole
database under 50 GB, but treat it as an anchor, not a threshold: large deployments routinely mix
colocated small relations with distributed large ones, and any table that turns
write-heavy should be uncolocated.

**Index cost.** In an ordinary non-colocated design, each index is a separate distributed
relation carrying its own write and storage cost, and each write in the transaction adds a
participant. In a colocated database, eligible indexes can be colocated too. Phrase write
amplification against the actual topology rather than as a universal law.

Similarly, co-hashing a child on its parent's key (`PRIMARY KEY ((parent_id) HASH, seq
ASC)`) gives a good query shape: one parent's rows occupy one hash range in the child —
but it does **not** make the two tables physically colocated and does not guarantee a
shared tablet leader. They stay separate relations unless colocation is explicit.

---

## 6. Syntax that fails at parse time

|                     Wrong                      |                     Right                      |
| ---------------------------------------------- | ---------------------------------------------- |
| `... WHERE x IS NOT NULL SPLIT INTO 9 TABLETS` | `... SPLIT INTO 9 TABLETS WHERE x IS NOT NULL` |
| `SPLIT INTO 9`                                 | `SPLIT INTO 9 TABLETS`                         |
| `PRIMARY KEY (a, b, c HASH)`                   | `PRIMARY KEY ((a, b, c) HASH, ...)`            |
| `yb_hash_code(t) % 9 ASC`                      | `(yb_hash_code(t) % 9) ASC`                    |

Index clause order: column list → `INCLUDE` → `SPLIT INTO` / `SPLIT AT VALUES` → `WHERE`.

`python3 scripts/yb-lint.py your.sql` catches all of these.

```sql
-- Partial hash index: split before predicate.
CREATE INDEX CONCURRENTLY idx_shopper ON events_p2027_01
    USING lsm (shopper_id HASH, created_at DESC)
    SPLIT INTO 18 TABLETS
    WHERE shopper_id IS NOT NULL;

-- Bucketed index for a monotonic column: parenthesised modulo, buckets pinned.
CREATE INDEX idx_updated ON events_p2027_01
    USING lsm ((yb_hash_code(updated_at) % 9) ASC, updated_at ASC)
    SPLIT AT VALUES ((1), (2), (3), (4), (5), (6), (7), (8));
```

Tablet counts in examples are illustrative. Derive your own from expected mature size.

---

## 7. Index review — run on every index

1. Which numbered access pattern uses it?
2. Do equality columns precede range/order columns?
3. Is the leading column high-cardinality and not skewed?
4. If the leading hash column is nullable, is there a `WHERE ... IS NOT NULL` guard?
5. Is the leading range column monotonic?
6. Redundant with another index or the PK?
7. Would `INCLUDE` make a hot read covering — and is every included column populated?
8. Is the write amplification acceptable on this table's write path?
9. Does its tablet count need explicit control at expected scale?
10. Is it used at all (Check if the table is active first)?
    (`pg_stat_user_indexes.idx_scan`)

---

## 8. Rolling out an index change

Most index failures in the field are rollout failures, not design failures.

- **Create the replacement, verify `indisvalid`, then drop the original.** Never reverse
  it. The replacement keeps the original's UNIQUE, INCLUDE, predicate and SPLIT unless the
  change is meant to remove one. YSQL has no `DROP INDEX CONCURRENTLY` (the parser rejects
  it): use `DROP INDEX`, and `ALTER TABLE ... DROP CONSTRAINT` for the index behind a UNIQUE
  constraint. A partitioned parent's index is created without `CONCURRENTLY`.
- **Clear `idle in transaction` sessions first.** One open transaction stalls `CREATE
  INDEX` and leaves it half-built. Walsenders, auto-analyze, backfill and matview backends
  are excluded from the lagging-backend count and are never the cause.
- **Set `statement_timeout` on the DDL session deliberately.** A backfill outlives the
  statement and cannot be cancelled once running.
- **Run DDL sequences on one connection.** Consecutive DDL across different connections
  can hit `duplicate key ... pg_attribute_relid_attnum_index` (GH #12449) — this affects
  Flyway, ActiveRecord, and similar tooling.
- **`ANALYZE` after bulk load or rebuild.** The CBO picks wrong plans on empty statistics.
- **No index DDL during an active Voyager migration.**
