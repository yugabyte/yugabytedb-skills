# Validation

Contents: A) query pack · B) reading the results · C) config baseline · D) sign-off gate

`scripts/collect.sql` already captures A1, A4, A6, A7 and A8, plus settings and version.
`yb-model.py review --replay` already runs EXPLAIN on every pattern against injected
statistics. Use this file for the live-cluster checks that remain: `EXPLAIN (ANALYZE,
DIST)` on the target, parse-checking recommended DDL, and the sign-off gate.

Everything here is read-only on the customer's cluster. **Never run recommended DDL there**
(it includes `DROP INDEX`, primary-key swaps and index rebuilds). Parse-check it on a scratch
instance of the same release, such as the replay container, or give it to the user to run.

---

## A. Query pack

Hand these to the user, or run them yourself with permission, showing each query as you
run it.

```sql
-- A1. Statistics for every column on a table (sampled). On YSQL, correlation is agreement
--     with the primary key's order, not insert order.
SELECT tablename, attname, null_frac, n_distinct, avg_width,
correlation, most_common_freqs, most_common_vals
FROM pg_stats WHERE schemaname='public'
ORDER BY tablename, attname;

-- A2. Exact null fraction for a decisive column (pg_stats is sampled)
-- Do not run this query if the table is very large, stick with pg_stats data
SELECT count(*) FILTER (WHERE <col> IS NULL)::numeric / NULLIF(count(*),0) AS null_frac
FROM public.<table>;

-- A3. Value skew on a suspected low-cardinality column
-- Do not run this query if the table is very large, stick with pg_stats data
SELECT <col>, count(*) FROM public.<table> GROUP BY 1 ORDER BY 2 DESC LIMIT 20;

-- A4. Index usage. Note the system's uptime so a zero isn't misread.
SELECT schemaname, relname, indexrelname, idx_scan, idx_tup_read, idx_tup_fetch
FROM pg_stat_user_indexes WHERE schemaname='public' ORDER BY idx_scan ASC;

-- A5. Unused non-unique, non-primary indexes, with drop statements
SELECT relname AS table_name, indexrelname AS index_name,
       pg_size_pretty(pg_relation_size(i.indexrelid)) AS index_size,
       'DROP INDEX IF EXISTS ' || quote_ident(schemaname) || '.'
         || quote_ident(indexrelname) || ';' AS drop_cmd
FROM pg_stat_user_indexes s JOIN pg_index i ON s.indexrelid = i.indexrelid
WHERE idx_scan = 0 AND NOT i.indisprimary AND NOT i.indisunique
ORDER BY pg_relation_size(i.indexrelid) DESC;

-- A6. Tablet counts per relation, cluster-wide from the master catalog. Trust this, not the
-- DDL file. (yb_local_tablets lists only the tablets with a peer on the node you are on.)
SELECT n.nspname, c.relname, (yb_table_properties(c.oid)).num_tablets
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'i', 'm') AND n.nspname NOT IN ('pg_catalog', 'information_schema')
ORDER BY 3 DESC;

-- A7. Relation and index sizes
SELECT relname,
       pg_size_pretty(pg_total_relation_size(c.oid)) AS total,
       pg_size_pretty(pg_indexes_size(c.oid))        AS indexes
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='public' AND c.relkind='r'
ORDER BY pg_total_relation_size(c.oid) DESC;

-- A8. Top queries
SELECT queryid, calls, mean_exec_time, max_exec_time, rows, query
FROM pg_stat_statements ORDER BY calls DESC LIMIT 100;

-- A9. Sharding annotations as actually created (a pg_dump won't show PK annotations)
\d+ public.<table>

-- A10. Partition list and bounds
SELECT c.relname, pg_get_expr(c.relpartbound, c.oid)
FROM pg_class c JOIN pg_inherits i ON i.inhrelid=c.oid
WHERE i.inhparent = 'public.<parent>'::regclass ORDER BY 1;

-- A11. Publication membership. Do NOT trust pg_publication_tables under
--      publish_via_partition_root — it collapses to the root.
SELECT p.pubname, c.relname FROM pg_publication_rel pr
JOIN pg_publication p ON p.oid=pr.prpubid JOIN pg_class c ON c.oid=pr.prrelid;

-- A12. Replica identity per table
SELECT relname, relreplident FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='public' AND relkind='r';

-- A13. Sessions that would stall a CREATE INDEX (run before any index DDL)
SELECT pid, state, xact_start, application_name, left(query, 80)
FROM pg_stat_activity
WHERE xact_start IS NOT NULL AND state LIKE 'idle in transaction%'
ORDER BY xact_start;

-- A14. Plan with distributed metrics
EXPLAIN (ANALYZE, DIST, DEBUG, COSTS, VERBOSE) <query>;

-- A15. Get the full DDL with tablet splits
ysql_dump --schema-only --include-yb-metadata
```

---

## B. Reading the results

**Plans.** Look for `Append` over multiple children (partition pruning failed); `Index
Scan` where you expected `Index Only Scan` (index isn't covering); `Seq Scan` on a table
you indexed. Compare actual DocDB RPC counts against your predicted participant count.

**Low-cardinality test data lies.** A `Seq Scan` at 50k rows may be a correct choice and
tells you nothing about production. Re-run at scale before concluding.

**Tablet reports.** Falling skew on a growing table means splitting works; static skew
means it doesn't. A single tablet spanning `0000..ffff` at the base of a tombstone tree
means the relation started as one tablet. Tombstoned rows with `sst_GB = 0` are post-split
parents — they still hold metadata and count against the budget.

**Partial indexes.** Run `yb_index_check` after significant load, especially where the
write path uses `ON CONFLICT`.

**When to scale instead of redesign.** Sustained TServer CPU above ~65–70%; p99 creeping
up despite healthy application code; disk IOPS or throughput saturation; flush/compaction
backlogs or write throttling. Scale up while nodes are small (below ~16 vCPU), then out.
Production YSQL at scale wants 16+ cores and 64 GB+ per node.

---

## C. Settings — verify, do not prescribe

Server flags, PG parameters, backfill tuning, CPU/RAM thresholds and batch sizes are
**version- and topology-specific**. They belong to the operational runbook for the target
release, not to a portable data-modeling prompt. Do not hand a customer a gflag list from
memory.

What is safe to recommend as a category, then verify against the target version:

- **Observability before go-live, not after an incident.** `statement_timeout`,
  `log_min_duration_statement`, ASH enabled, `ybtop` available. Once a backend is
  OOM-killed the offending query is often unrecoverable: `pg_stat_statements` records only
  completed queries, OS OOM logs carry no query text, and ASH has the query id but not the
  text.
- **Sequence caching** where identity allocation is on a hot path. Expect large ID gaps;
  that is correct behaviour, not a defect.
- **A smart driver plus a connection pool**, with the pool configured to recycle
  connections so that newly added nodes actually receive traffic.
- **Protocol-level prepared statements** rather than explicit `PREPARE`/`EXECUTE`, which
  makes connections sticky and defeats server-side pooling.
- **Bounded retries with backoff**, and a client that does not open a new connection per
  retry.
- **Index backfill tuning** for very large tables, set for the maintenance window only.

For concrete values, defer to the official `ysql` skill and the target release's docs, and
say which version you checked.

## D. Sign-off

Tick what was done; list everything unticked under Limitations in the deliverable. A review
without measurements is a structural review and must say so.

- [ ] Preconditions measured: null fractions and cardinalities for every hash-key and
      leading-index column.
- [ ] `pg_stat_user_indexes` reviewed before any index drop.
- [ ] `EXPLAIN (ANALYZE, DIST)` run on every access pattern; plans match expectations.
- [ ] Tablet counts verified with `yb_table_properties` (A6), not read from the DDL.
- [ ] Every recommended DDL statement parse-checked on a scratch instance of the customer's
      release (never on the customer's cluster); version recorded.
- [ ] Tablet budget arithmetic done against node count and RAM, tombstones included.
- [ ] `ANALYZE` run; plans re-checked without legacy hints.
- [ ] Publications and `REPLICA IDENTITY` verified, if CDC is in scope.
- [ ] Partition runway and retention schedule confirmed, if partitioned.
- [ ] Index rollout plan written, with an owner for clearing idle-in-transaction sessions.
- [ ] Limitations section written: what you could not verify, and why.
