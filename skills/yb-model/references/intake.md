# Intake for a review

## 1. Capture commands

Give the user these two commands, for whoever can connect to the application database
(often the customer). Run them from an empty directory; both are read-only.

```bash
ysqlsh -h <host> -U <user> -d <db> -f <skill-dir>/scripts/collect.sql
ysql_dump -h <host> -U <user> -d <db> --schema-only --include-yb-metadata > schema.sql
```

That directory is the bundle. It covers Round 1 and Round 3 below, plus the release,
settings, index usage and tablet counts.

`pg_stat_statements` and the index and table usage counters count only the statements that
ran through the node `collect.sql` connects to. One run ranks the workload fairly but cannot
prove an index unused. For that, run `collect.sql` once on every node, each into its own
subfolder of the bundle (`node1/`, `node2/`, ...); the engine adds them up and records which
nodes they cover. `pitfalls.md` lists other input traps (encodings, truncated statements,
several schemas, recent stats resets). Files in other shapes are fine: SKILL.md step 1 says
how to convert them. Ask the rounds below only for what the bundle cannot
contain: SLOs, growth, client retry behaviour and CDC.

## 2. Question rounds

Ask in rounds, at most three questions per message. ★ marks must-haves. Accept partial
answers and turn the rest into labelled open items; never block the review on the
questionnaire. Never ask for something already provided or measurable from a reachable
cluster.

## Round 1: artifacts (ask for these, not descriptions)

1. ★ The schema as actually created: `ysql_dump --schema-only --include-yb-metadata`
   (validation §A15). A plain `pg_dump` hides PK sharding annotations and tablet splits.
2. ★ Top queries from `pg_stat_statements` by calls and by total time, anything run more
   than ~10 times a day, with **full query text** (§A8). Truncated `WHERE` clauses block
   analysis.
3. ★ `pg_stats` for the schema (§A1).

Useful if available: `pg_stat_user_indexes` with system uptime (§A4), live tablet counts
(§A6), relation sizes (§A7), known slow queries, and two tablet reports several days apart.
One report shows the distribution; two show the mechanism, because you see what split and
what did not.

## Round 2: shape and scale

1. ★ YugabyteDB version, RF, node count and size, regions and AZs. Is Enhanced PG
   Compatibility Mode on? (It changes the default sharding to `ASC`.)
2. ★ Read:write ratio and peak QPS, now and at target.
3. ★ Largest and fastest-growing tables: rows and bytes per day, retention window.
4. Latency SLO (p50, p99) and which patterns are user-facing versus batch.
5. Confirm the query list is complete, including batch jobs, admin tools, exports and
   analytics.

## Round 3: key column statistics

The round people skip, and the one that decides whether a hash key works. For every PK
column, hash-key column and leading index column:

1. ★ Null fraction (matters for **secondary index** keys; PK columns are implicitly
   `NOT NULL`).
2. ★ Distinct value count, and the share held by the most common value.
3. ★ Monotonic? Sequence, timestamp, ULID, UUIDv7.

These are measurable. Run §A1 to §A3 yourself with permission or hand them over. `pg_stats`
is sampled, so for a decisive column get an exact figure if the table is small enough.

## Round 4: write path (ask only what the DDL and queries leave open)

1. Hot rows: counters, per-tenant aggregates, status flags with many writers?
2. What does the client do on a retriable error: reuse the connection or open a new one?
   Is there a retry cap? Smart driver or plain PG driver, and which pooler?
3. Are rows updated after first write? Is re-ingest idempotent for child rows too?
4. CDC in scope? Which tables, which sink, are full before-images needed?
5. Partition maintenance script, if partitioned. Read it: `PARTITION OF` without
   `SPLIT INTO` is a finding.

## Running the interview

- If the user is short on time, take Round 1 and start. Carry the rest as open items.
- Don't re-ask what you can compute. The DDL already tells you the sharding annotations.
- "Normal OLTP workload" and "a few thousand QPS" are not inputs. Push once, then record
  the gap.
- Push back on `SELECT *` in access patterns; it decides whether an index can be covering.
