# Pitfalls: known gaps and things to be wary of

Read this before every review, together with your own list (`yb-model.py pitfall list` shows
both). Each entry says what you see, why it happens and what to do. Entries are generic: they
never name a customer, a table or a value.

Every entry carries the engine's status:

- **handled**: the engine deals with it; the entry says how, so you can check.
- **flagged**: the engine detects it and says so in the review's open items.
- **not detected**: a known gap. The engine does not see it, so you have to.

An entry never claims more than the engine does. A handled or flagged entry names the test
that pins the behaviour. A gap can stay here as `not detected` until the engine fixes it.

## Inputs

### input-unreadable: A file the engine cannot read
Engine: handled (test: UnreadableInputs)

The engine stops (exit 2) and names the file and the cause for: UTF-16 encoding (Windows
exports) or other NUL bytes; a semicolon or tab delimiter; a missing header row; a missing
column the file needs (for example `idx_scan` in the index usage file, `query` and `calls`
in the pg_stat_statements file, `key` and `value` in ybm_meta.csv); a schema file from which
no table can be read. An empty capture file is not unreadable: psql creates the file before
its query runs, so a query that failed during capture (pg_stat_statements not installed, for
example) leaves it empty, and the engine treats that input as missing, with an open item. Copy the
bundle, convert the copy (step 1c of the skill's procedure) and run again. Never edit the
customer's original files, and say in the review what you converted. A byte-order mark at the
start of a file (Excel's "CSV UTF-8") is read without help.

### input-misread: A file the engine reads wrongly without noticing
Engine: not detected

A query table pasted from a UI (box characters, wrapped lines, a header repeated on every
page) or a spreadsheet converted by hand loads, but its statements come out partial or
unparsed. The sign is far fewer patterns, tables or statistics rows than the source holds;
compare the counts in the review with the files. Convert a copy as above.

### truncated-statements: Truncated statement text
Engine: not detected

UI exports and some query logs cut statements at 1024 or 4096 characters. Watch for a
statement that ends mid-word, has unbalanced parentheses or ends in `AND`. Its access pattern
is incomplete, and findings on it rest on a fragment. Ask for the full text, or say so under
Reviewer notes with the finding IDs.

### usage-one-node: Usage counters from one node
Engine: flagged (test: NodeCoverage)

Each node counts only the statements that ran through it: `pg_stat_statements`,
`pg_stat_user_indexes` and `pg_stat_user_tables` all work this way. One capture still ranks the
workload fairly, but it cannot prove an index unused. The engine says how many nodes the
counters cover, and makes every drop start with a check on every node. To remove the doubt,
run `collect.sql` once per node, each into its own folder of the bundle; the engine adds the
folders up.

### node-label-loopback: Per-node captures taken over localhost or a socket
Engine: flagged (test: NodeCoverage.test_loopback_labels_fall_back_to_the_folder)

`collect.sql` labels each capture with the server address it connected to. Run on each host
over `localhost` or a Unix socket, every node reports the same loopback label. The engine then
names each capture by its folder and adds the folders up, but nothing shows the folders are
different nodes (a folder may repeat another's), so on a cluster of more than one node the
counters never count as complete and every drop keeps its check on every node. Capture each
node over its own address to remove the doubt.

### stats-reset: A recent stats reset or restart
Engine: not detected (the review shows the postmaster start time)

The counters start at the last reset or restart. Zero scans over a day says little about a
weekly or monthly job. Compare the start time with the jobs the user describes.

### plain-dump: A plain pg_dump, an ORM migration or hand-written DDL
Engine: flagged (CFG004) (test: Pitfalls.test_a_plain_dump_is_flagged_and_counted)

Keys carry no HASH or ASC annotation and there are no SPLIT clauses, so sharding is inferred.
Ask for `ysql_dump --include-yb-metadata`, or review with a scratch server (replay, or
`--catalog`) so a local YugabyteDB says how it would create each table.

### several-schemas: Several schemas
Engine: flagged (test: Schemas)

A table outside `public` is named `schema.name` throughout the review. When a query names a
table without a schema, the engine resolves it through `search_path` as the server does, and
adds an open item when several schemas define the name or when only schemas off the search
path do. A name that one such schema defines is read as that schema's table (the server would
not find it under that search path); one that several define is left out.
Applications with one schema per tenant set `search_path` per connection, so one statement may
run against every tenant's copy.

### incomplete-dump: An incomplete dump
Engine: handled (with a scratch server; static only: not detected) (test: CatalogSchema.test_repairs_supply_what_an_incomplete_dump_left_out)

A dump may lack `CREATE SCHEMA`, sequences, or the colocated database it came from. The text
reader takes the DDL as written. With a scratch server (replay, measure or `--catalog`), the
one preparation repairs these gaps from the server's errors and lists each repair.

### stats-duplicates: Statistics listed twice
Engine: flagged (test: DuplicateStats)

When nodes disagree, `pg_stats` can list one column several times. The engine keeps one row
and adds an open item; say which capture you trust if the user knows.

### tablet-counts-one-node: Tablet counts from one node
Engine: flagged (findings stay probable) (test: TabletCounts)

`yb_local_tablets` lists only one node's tablets. Prefer the `yb_table_properties` capture in
`collect.sql`.

## Engine limits

### partitioned-index-usage: Indexes on partitioned tables
Engine: handled (test: PartitionIndexCopies)

An index on a partitioned table has a copy on every partition, and YSQL changes the copies only
through the parent. Usage counters never list the parent: its scans are counted on the copies.
The engine reads the copies from `ALTER INDEX ... ATTACH PARTITION` in a dump (or from the
catalog when a scratch server runs), writes DDL for the parent only, and judges the parent's use from
all of its copies. Hand-written DDL without those statements leaves each copy standalone.

### non-lsm-indexes: Index methods other than LSM
Engine: not detected

GIN, GiST, ybgin and vector indexes are not modelled as access paths. With a scratch server
the replayed plan shows such an index serving a read, and the review takes that access from the
plan; without one, and for every option, the engine's own model does not see it. Never accept a
drop of such an index (WRK003) without checking the workload for the operators it serves: `@>`,
`?`, `&&`, `<->` and full-text search.

### key-gap: Equality on a key column after an unbound one
Engine: handled (test: KeyGap)

A pattern binds the hash columns and the first range columns of a key, skips one, and binds a
later one: `WHERE h = $1 AND a = $2 AND c = $3` on a key `(h HASH, a, b, c)`. The plan reads
well (an Index Scan, `c` in its Index Cond, rows scanned equal to rows returned), but DocDB seeks
under every distinct value of the skipped column, so the cost grows with that count and stays
hidden until it reaches thousands. The access table prints each pattern's key binding map, and
CAP014 reports the gap with its options; its probe counts seeks on the customer's release. Two
limits: a column every OR arm binds (`or=` in the map) is treated as bound, which holds when
the planner turns the arms into a BitmapOr and not otherwise; and without pg_stats the number of
distinct gap values is unknown, so the finding cannot say how many seeks a read costs.

### measured-sample: What the measured pass can and cannot show
Engine: handled (test: MeasuredPass)

The sample is small and shaped by the statistics, so it shows how DocDB executes a read (seeks
and rows scanned per row returned), not how long it takes on the customer's cluster. Its
shape is an estimate: without `pg_stats` every key column gets 8 values, and literals the
queries bind can push a hash prefix past the row budget (the review gives the actual size).
Rows a CHECK, partition bound or foreign key rejects are dropped from the sample, and a
one-column CHECK no generated value satisfies is removed; a table no candidate row fits (a
multi-column CHECK, say) is not measured; all are recorded (`gaps`, layers `gen.*`). Join reads
are not measured; the review names them, and the tables it could not fill. A MEAS001 finding
is a lead: confirm it with `EXPLAIN (ANALYZE, DIST, DEBUG)` on the customer's data before
treating it as a defect.

### large-schemas: Very large schemas
Engine: not detected

More than a few hundred tables make the safety pass slow. Review the tables the workload
touches first.

### plans-without-statistics: Plans chosen without the customer's statistics
Engine: handled (test: ReplayAuthority.test_without_statistics_the_plan_corroborates_and_the_model_decides)

Without `pg_stats` or table sizes the planner chooses on defaults, and may scan a table or
take the primary key where an index built for the query serves it. Such a plan does not decide
the access: the engine's model does, its access-path findings stay probable, and each pattern
where the plan chose differently is listed under Validation (`path` gaps, the model's access
taken). Ask for `pg_stats` and table sizes, or check those plans on the customer's cluster.
Under assumed planner settings alone, the plan still decides, labelled.

### options-judged-by-the-model: Options are checked by the engine's model, not tried
Engine: not detected

The option checks, the per-area "must stay true" lists and the combined-plan check work out
each pattern's access with the engine's own model, even where the review's access came from
the replayed plan (the patterns listed under Validation as `path` gaps). For those patterns an
option's costs can be missing (an option that drops the index the plan reads) or extra; check
them against the plan. The fixpoint self-check re-runs without the plans, so it cannot
re-check a finding that only the plan's access raised.

The safety pass decides whether an option breaks another pattern by re-planning in Python, not
by applying the option on the scratch server. It can be wrong where its predicate reasoning is:
a unique partial index replaced by one with a different `WHERE` (a predicate that implies the
old one is not recognised, and a negated or `BETWEEN` predicate can hide a real loss of
uniqueness), and `INSERT ... ON CONFLICT` whose target is an expression (`ON CONFLICT
(lower(e))`) or carries a `WHERE` that implies the index's predicate (CAP060 may say it has no
arbiter). Before recommending an option that drops or replaces a unique or partial index, run
each upsert on the table under the new DDL in a scratch database.

Until option checks also use the replayed plans, treat an option's costs as approximate
wherever the review lists a `path` gap for the patterns it touches, and confirm them with the
plan (or EXPLAIN on the cluster) before presenting them as settled.

### ordered-walk-on-the-plan: An ordered index walk the plan takes
Engine: not detected

When the replayed plan reads an index in ORDER BY order and filters until LIMIT (no seek on
the filter), the review does not raise CAP012 for it, and the pattern's `path` gap says the
model would use no index. Read the plan of such a pattern (`plans.json`): an Index Scan with no
Index Cond under a Limit is that walk.

### statement-shapes: Statement shapes come from the engine's SQL reader
Engine: not detected

The plan supplies each pattern's access and key binding, but which tables a statement touches,
its predicates and its `ON CONFLICT` target come from the engine's own SQL reader. Forms it
reads poorly: `INSERT ... SELECT ... ON CONFLICT` (the conflict may not be tied to its table),
`ON CONFLICT ON CONSTRAINT`, and statements over several tables. A predicate the reader cannot
parse is recorded as a gap (layer `bind.reader`); check those patterns by hand.

### static-only: Reviews without a scratch server
Engine: not detected

Without Podman and a local release image the schema comes from the DDL text, and that path is
frozen: its known misreadings are not fixed. It reads an expression hash key such as
`((lower(e), t) HASH)` as one key; misses tablegroups and colocation that only a dump's
colocation clauses show (HASH or SPLIT advice for colocated tables); keeps `SPLIT INTO 1
TABLETS` on rebuilt indexes; treats partition copies of an index without `ATTACH PARTITION` as
standalone; and does not supply what an incomplete dump leaves out. Prefer a review with a
scratch server; otherwise check these by hand.

### missing-checks: Checks the engine does not make
Engine: not detected

- A foreign key whose referencing columns lead no index (each parent delete or key update scans
  the child): look for it in the catalog by hand.
- SPL001 sizes tablets from row counts, not bytes: wide rows need more tablets than it says.
- yb-lint YB021 says every NULL shares one hash code even for `UNIQUE ... NULLS NOT DISTINCT`,
  where that is the intended behaviour.
- A missing release cache (`versions.json`) is not listed among missing inputs; the review's
  version match says the differences between releases are unknown.

### replay-other-release: A replay on another release
Engine: flagged (test: ReplayAuthority)

A replay shows the planner of the image's release. When it differs from the customer's and
the differences are unknown, replay and probes neither confirm nor refute; read the version
match in the review before trusting a refuted finding.

## When something did not work as expected

Check `yb-model.py pitfall list` first: if an entry covers it, follow it. Otherwise record it
in your own list after delivering the review:

```bash
python3 scripts/yb-model.py pitfall add <bundle> --id <short-id> --title "<what you saw>" \
    --status "not detected" --body "<why it happens, and what you did>"
```

The command refuses an entry that names a table, index, schema or database from the bundle
(plain or schema-qualified, three characters or more) or one of its node addresses, so
describe it generically. It cannot recognise values or column names: leave those out yourself. Your list lives in `~/.yb-model/pitfalls.md` (or
`$YBM_HOME`), outside the skill, so updates do not overwrite it. `yb-model.py pitfall export`
prints your entries for a pull request against this file, where a maintainer adds them here or
fixes the engine with a test.
