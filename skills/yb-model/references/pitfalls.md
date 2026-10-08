# Pitfalls: things to be wary of in a review

Read this before every review. Each entry says what you see, why it happens and what to do.
The engine handles some of these itself and says so in its open items; the rest need you.
Entries are generic: they never name a customer, a table or a value.

## Inputs

**A file the engine cannot read.** The engine stops (exit 2) and names the file and the cause
for:

- UTF-16 encoding (Windows exports) or other NUL bytes;
- a semicolon or tab delimiter, or a missing header row or column;
- a schema file from which no table can be read.

Copy the bundle, convert the copy (step 1c of the skill's procedure) and run again. Never
edit the customer's original files, and say in the review what you converted. A byte-order
mark at the start of a file (Excel's "CSV UTF-8") is read without help.

**A file the engine reads wrongly without noticing.** A query table pasted from a UI (box
characters, wrapped lines, a header repeated on every page) or a spreadsheet converted by
hand loads, but its statements come out partial or unparsed. The sign is far fewer patterns,
tables or statistics rows than the source holds; compare the counts in the review with the
files. Convert a copy as above.

**Truncated statement text.** UI exports and some query logs cut statements at 1024 or 4096
characters. Watch for a statement that ends mid-word, has unbalanced parentheses or ends in
`AND`. Its access pattern is incomplete. Ask for the full text, or say in the review that
findings on that pattern rest on a fragment.

**Usage counters from one node.** Each node counts only the statements that ran through it:
`pg_stat_statements`, `pg_stat_user_indexes` and `pg_stat_user_tables` all work this way. One
capture still ranks the workload fairly, but it cannot prove an index unused. The engine
says how many nodes the counters cover, and makes every drop start with a check on every
node. To remove the doubt, run `collect.sql` once per node, each into its own folder of the
bundle; the engine adds the folders up.

**A recent stats reset or restart.** The counters start at the last reset (the review shows
the postmaster start). Zero scans over a day says little about a weekly or monthly job.

**A plain `pg_dump`, an ORM migration or hand-written DDL.** Keys carry no HASH or ASC
annotation and there are no SPLIT clauses, so sharding is inferred (CFG004). Ask for
`ysql_dump --include-yb-metadata`, or run with `--catalog` so a local YugabyteDB says how it
would create each table.

**Several schemas.** A table outside `public` is named `schema.name` throughout the review.
When queries name a table without a schema and several schemas define it, the engine
resolves the name through `search_path`, as the server does, and adds an open item.
Applications with one schema per tenant set `search_path` per connection, so one statement
may run against every tenant's copy.

**An incomplete dump.** A dump may lack `CREATE SCHEMA`, sequences, or the colocated database
it came from. The text reader takes the DDL as written. `--catalog` repairs these gaps from
the server's errors and lists each repair.

**Statistics listed twice.** When nodes disagree, `pg_stats` can list one column several
times. The engine keeps one row and adds an open item; say which capture you trust if the
user knows.

**Tablet counts from one node.** `yb_local_tablets` lists only one node's tablets. Prefer the
`yb_table_properties` capture in `collect.sql`.

## Engine limits

**Index methods other than LSM** (GIN, GiST, ybgin, vector indexes) are not modelled as
access paths. Never accept a drop of such an index (WRK003) without checking the workload
for the operators it serves: `@>`, `?`, `&&`, `<->` and full-text search.

**Partition indexes.** Without `--catalog`, the engine does not see which per-partition
index is a copy of a partitioned index. Change such an index through its parent.

**Very large schemas** (more than a few hundred tables) make the safety pass slow. Review
the tables the workload touches first.

**A replay on another release** shows that release's planner. Read the version match in the
review before trusting a refuted finding.

## When something did not work as expected

Write one entry in a file named pitfall-candidates.md next to review.md. Give the
symptom, the cause you found and what you did. Keep it generic: no customer names, table names
or values. A maintainer reviews the candidates and either fixes the engine (with a test) or
adds the entry here.
