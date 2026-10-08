# Reading the engine output

Contents: 1) severity and confidence · 2) pattern weight · 3) rule families · 4) replay ·
5) what the engine does not do

## 1. Severity and confidence

Severity is the rule's base severity from `rules/rules.json`, shifted down by fixed amounts:

- **Traffic weight:** one level for a WARM pattern, two for a COLD one.
- **Table size:** one level below 1M rows, two below 10k rows.

Thresholds are in `THRESHOLDS` in `scripts/ybm/analyze.py` and are printed in `review.json`.

| Confidence | Meaning |
|---|---|
| `confirmed` | Follows from a measurement (`pg_stats`, `pg_stat_statements`, index usage) or from DDL text. |
| `confirmed (replay)` | Static prediction matched by the plan on a scratch cluster of the customer's version with their statistics injected. |
| `confirmed (measured)` | Static prediction corroborated by DocDB counters for the same pattern (rows scanned vs returned, read RPCs per call). |
| `probable` | Static prediction only. The *Evidence needed* line says what closes it. |

Two models agreeing is never a reason to upgrade `probable`.

## 2. Pattern weight

With `pg_stat_statements`, patterns are ranked by total execution time:

- **HOT:** the patterns that cover the first 80% of total time (at most 25 of them), plus
  the 10 most-called patterns.
- **WARM:** at least 0.1% of time or calls.
- **COLD:** everything else.

The ranking is among the statements the engine can analyse. Catalog and monitoring queries,
session and utility statements (`SET`, `COMMIT`, ...) and statements on tables outside the
schema are not analysed; an open item lists how many there are and their share of time, and
every share the report quotes (pattern table, headline) is a share of *all* statement time.

A query list (`queries.sql`) is used with or without `pg_stat_statements`. Each listed
statement is matched to `pg_stat_statements` by fingerprint (constants and parameters as `?`,
casts on them dropped, IN and VALUES lists collapsed). A match is ranked from
`pg_stat_statements`; a statement it lacks is added after the ranked patterns as UNRANKED, and
findings resting only on such statements carry the caveat
`pattern unranked (listed, not in pg_stat_statements)`. UNRANKED patterns weigh like HOT ones,
because their traffic is unknown. Without `pg_stat_statements`, every pattern is UNRANKED and
severity is not traffic-weighted.

## 3. Rule families

| Prefix | Source | Example |
|---|---|---|
| `CAP` | Planner capability; independent of data | Hash group not fully bound, ORDER BY not served, non-covering index, no partition pruning |
| `STA` | `pg_stats` | NULLs on a hash lead, low-cardinality or skewed hash key, monotonic range lead |
| `WRK` | `pg_stat_statements`, `pg_stat_user_*` | Unused index, too many indexes on a write-hot table |
| `SPL` | Tablet counts | Large relation still on one tablet |
| `CFG` | `pg_settings` | Cost model off, default sharding is ASC |
| `PLN` | Replayed plans only | Seq Scan on a large table that no static rule predicted; the fix says what the planner chose and names the pattern's other findings |
| `LINT-YB*` | `yb-lint.py` | Clause order, missing `TABLETS` keyword, redundant prefix index |
| `SAF` | Safety pass over the engine's own DDL | A fix that would lose a uniqueness guarantee, an ON CONFLICT target, or a plan |

Rules marked `"section": "hygiene"` in `rules.json` (WRK005 to WRK010) describe how the
application uses the database. The report lists them under *Workload hygiene*, keeps them out
of the headline, and words their fixes for the application team.

*Pinned by* names the regress output file and a fixed string in it. To read the test, open
`src/postgres/src/test/regress/expected/<file>` in yugabyte-db at the customer's release tag
and search for the string. Only rules that claim planner or execution behaviour (`CAP`,
`PLN`) cite tests, and `scripts/check-rule-refs.py` fails if one does not. `STA`, `WRK`, `CFG`,
`SPL`, `SAF` and lint rules are arithmetic on the bundle; their *Basis* line names the inputs
that rule family reads and the bundle has (for example "the schema DDL, pg_stats and row
counts"; lint findings: the schema DDL only).

**What statistics can show on YSQL.** ANALYZE fetches its sample in ybctid order, so
`pg_stats.correlation` is agreement with the primary key's order, not with insert order: it is
about 1 for the leading column of a range-sharded key whatever the inserts did, and noise on a
hash-sharded table. STA004 therefore needs a sequence default (including one attached later by
`ALTER COLUMN ... SET DEFAULT nextval` or an identity) or a time-typed or creation-named column;
for a secondary index on a range-sharded table, correlation counts only when the primary key
itself is insert-ordered. Measured writes raise its severity and confidence but never fire it
alone. Unique integers spread evenly over most of their
type's range are random identifiers and never fire it. STA006 lists only columns with no
non-NULL value in the sample; columns an index references (key, INCLUDE or predicate), event
timestamps and columns that hold some data are named as left out. A pg_stats export that lists
a column twice (per-node variants) keeps one row, the one reported by most nodes when the file
says, otherwise the first, and the open items name the columns.

## 4. Replay

`plans.json` holds one `EXPLAIN (FORMAT JSON)` per pattern. Plans are generic
(`plan_cache_mode = force_generic_plan`), so parameters are costed with default selectivity
rather than one specific value. Settings are copied from the customer's `pg_settings`
unless `--mode on|off` forces the cost model.

During reconciliation, each static access-path finding is checked against the replayed plan
of its patterns:

- **Matched:** the finding becomes `confirmed (replay)`.
- **Contradicted, settings known:** the finding is removed and listed under *Findings
  removed because the replayed plan contradicted them*. Read that list: it is where the
  static rules are weakest.
- **Contradicted, settings assumed:** when the bundle has no pg_settings, replay runs under
  assumed planner settings (the release default, or the image's compiled default when no
  release facts are cached; the report lists them). A contradiction then only *disputes* the
  finding: it moves to the *Disputed by replay* section, out of the recommended DDL and action
  items, and stays visible. A false positive is preferred to losing a real problem on a guess.
  Probes follow the same rule.
- **Planner chose a different path:** the finding keeps `probable` and shows the chosen path.

Replay plans are estimates. Injected statistics do not reproduce cache state, network
latency, tablet leadership or contention.

### Rule probes

Some rules claim how YugabyteDB *executes* a plan, not which plan it picks (CAP001: a partial
hash key is a full scan; CAP012: an ordered walk with a filter reads until LIMIT matches;
CAP013: a row-comparison cursor is rechecked, not sought). Injected statistics cannot test
these, because empty tables give zero execution counters, so each such rule carries a `probe`
in `rules.json`. A probe has setup SQL that generates a few thousand rows, literal queries
(`claim`, usually a `control` with the fix), `applies` checks (the planner took the path the
rule is about) and `holds` checks over the measured counters.

During replay, the engine runs the probes of the rules that fired, in the same container (the
customer's release, or the newest earlier image in its line) under the same planner settings.
Verdicts: **holds** (the finding names the release and the counters), **refuted** (the
finding is withdrawn and listed with the counters), **inconclusive** (the finding says
unverified). Without replay, the finding says its mechanism is unverified on the release.
Nothing is stored: a new release is checked by running the review on it.
`yb-model.py probe --image <img>` runs the probes alone.

## 5. Safety pass

Before the report is written, `ybm/safety.py` applies each finding's DDL to a copy of the
schema, alone and then all together, and compares the result with the original:

- Every PRIMARY KEY and UNIQUE guarantee must survive, on the same or fewer columns and with
  a predicate that is no narrower. `col IS NOT NULL` on a key column does not narrow it: rows
  whose key holds a NULL never conflict, unless the index is NULLS NOT DISTINCT, which the
  replacement must then keep. If a fix would lose one, the pass adds the UNIQUE
  index that keeps it, and marks the row `amended`. Statistics rules never suggest dropping a primary
  key or a unique index; their fix is a re-key on the same columns.
- A foreign key depends on the unique index (or primary key) it was created against, and that
  index cannot be dropped while the key exists. When a fix drops one, the pass re-points the
  key: it is dropped just before the index and added back as written, `NOT VALID` and then
  validated, provided the fix leaves a unique index without a predicate on exactly the
  referenced columns; otherwise the DDL is withheld and the fix says why. A primary-key change
  (a table swap) lists the foreign keys to move in its comment lines.
- Every `INSERT ... ON CONFLICT` must still find its arbiter: a unique index on exactly its
  columns (a partial one only when the statement repeats the predicate), or for `ON CONFLICT
  ON CONSTRAINT name` the primary key or unique constraint of that name.
- Every ranked pattern is planned again. Losing an access path, a point lookup, index order
  or coverage is a regression.
- A pure `DROP INDEX` of an index that `pg_stat_user_indexes` shows was scanned is flagged.

How the DDL is written: a replacement index is derived from the original
(`schema.index_sql`), so UNIQUE (with NULLS NOT DISTINCT), INCLUDE, the predicate as written,
the method and the SPLIT clause are carried over and a rule states only what it changes (a bucketed replacement keeps
every original key after the bucket). Drops are `DROP INDEX` (YSQL rejects `DROP INDEX
CONCURRENTLY`, gram.y) or `ALTER TABLE ... DROP CONSTRAINT` for the index of a UNIQUE
constraint (ysql_dump writes one as its `CREATE UNIQUE INDEX` followed by `ADD CONSTRAINT ...
UNIQUE USING INDEX`); a partitioned parent builds without CONCURRENTLY. A CAP060 fix names an existing
unique index on a subset of the ON CONFLICT columns when there is one (the statement should
target it), and otherwise hashes the new unique index on a NOT NULL, high-cardinality column.
Names are written as YSQL needs them (quoted when mixed case, special or reserved) and an
expression keeps the quotes of the names in it, so a quoted mixed-case schema gets the same
recommendations as a lower-case one. A primary-key change, which YSQL cannot make in place, is
written as comment lines (`-- CREATE TABLE ..._new (... PRIMARY KEY (...))`) that the safety
pass reads; a colocated table gets range keys and no SPLIT clause.

Side effects that cannot be amended mechanically become `SAF001`, or `SAF002` when they only
appear once the fixes are combined.

## 6. Releases and deployment tools

No release number or release behaviour is written by hand in the engine or the rules. Release
facts live in two generated files. Both are local caches: they are built on the machine that
runs reviews, for the releases being reviewed, and are never committed (`.gitignore`):

- `rules/versions.json`, built by `scripts/extract-version-data.py` from the yugabyte-db
  source of every release tag (local checkout via `git show`, or GitHub for one release).
  Per release: planner setting defaults (compiled default, overridden by the tserver's
  PG-flag default), server flag defaults, whether each rule's test citation exists, and the
  planner settings each deployment tool injects, discovered by scanning `bin/yugabyted`
  (default path vs the Enhanced PG Compatibility path) and the YBA universe-creation code.
- `rules/observations.json`, written by `evals/yb-model/oracle.py`: which releases and
  planner modes the static model was checked on, and per-rule observations such as how
  often the planner chose a partial hash-key Index Scan and how many rows it really read.

The customer's release comes from the first of these that names one: `--release` (the user
stated it), `SELECT version()` (`ybm_meta.csv`), `pg_settings` `server_version`, the
`ysql_dump` header (`Dumped from database version`, not the client's `Dumped by` line). The
report names the source. Sources that disagree, and a value that names no YugabyteDB release
(a PostgreSQL dump), are open items.

The engine resolves the customer's release to the newest table entry at or below it, then:

- fills settings missing from the bundle only when every deployment profile agrees;
- records a setting as conditional, with each profile's value, when they differ;
- lists settings absent on the release and uses a rule's `fix_if_unavailable` when a fix
  `requires` one of them;
- marks findings whose rule is not pinned by a test on that release;
- says when the planner model has not been checked by the oracle on that release;
- emits `RELEASE-DATA-MISSING` with the `update-versions` commands when the release is not
  cached (`--repo` reads a local checkout; `--github` needs the user's agreement).

When the customer's release is not cached, the nearest **earlier** cached release stands in,
from any release line; a newer release never does, because it may already fix what the
customer's release still does. A release older than everything cached has no stand-in.
GitHub-built entries record a deployment tool whose overrides could not be checked as
*unverified*, which makes the settings it might change conditional.

Replay picks the exact image, or else the newest earlier image of the same release line
(never a newer one). It sets the customer's effective planner settings explicitly, so the
container launcher's defaults never apply: from the bundle, else the release default, else
(no release facts) the image's own compiled defaults, recorded as assumed. It records the
drift between the two releases and does not let replay confirm a rule whose tested behaviour
differs between them; without release facts for both, the drift is unknown and a replay on a
different image confirms and refutes nothing.

## 7. Self-checks

- **Fixpoint** (`yb-model.py fixpoint`, also run by `review`): starting from the review as
  reported (its final findings and DDL, after replay and probes; disputed findings excluded),
  apply every recommended DDL to a copy of the schema and review it again statically. Each
  schema finding that carried DDL must be gone and no new schema finding at medium or above
  may appear; a second round must add none either. Workload, plan and safety findings
  describe the workload as measured and are not re-checked.
- **Planner oracle** (`evals/yb-model/oracle.py`, maintainer tool): generates hundreds of
  seeded schema and query cases, plans them on a real YugabyteDB with injected statistics,
  and compares the planner's choices with the engine's static model. Capability
  disagreements are engine bugs or version differences.

## 8. Missing inputs: preflight and muting

`preflight.py` detects each input listed in `rules/inputs.json`; that file says, per input,
what the review loses without it. Three effects:

- **Muted rule:** not evaluated, because it would conclude from a guess. A candidate the rule
  matched anyway is recorded as *withheld* and named in the report, so the user sees what
  collecting the input would settle. Example: without pg_stat_statements, a query list is
  unranked and may be partial, so CAP050 / CAP051 (re-key or swap a primary key, the most
  invasive DDL the engine writes) are withheld and the lighter CAP020 covering fix stands.
- **Weakened rule:** runs, and every finding it makes carries a caveat in its confidence
  (`pattern unranked`, `table size unknown`, `no pg_stats`, `planner settings not captured`,
  `sharding inferred`). `sharding inferred` applies only to findings on keys the DDL does not
  annotate.
- **Stage:** a workflow step skipped or reduced, for example replay without a release or
  workload, or drop-instead-of-rebuild without pg_stat_statements.

A query list whose first line is `-- ybm: workload-complete` (the user states nothing else
touches these tables) re-enables IDX001, the coverage-based unused-index check, with the caveat
`workload declared complete`. Duplicate and prefix-redundant indexes (linter YB052 / YB051) are
structural and run with or without a workload.

Usage counters are per node: `pg_stat_statements` and `pg_stat_user_indexes` / `_tables`
count only what ran through the node they are read on. Captures from several nodes, one
folder each, are added up (a node captured twice counts once; row estimates are not added).
Until every node is in (`collect.sql` records the node and the cluster size), an index with
no scans is not proven unused: WRK003 is probable, every drop starts with a check on every
node, and an open item says which nodes the counters cover.

Names in the bundle files are matched to the schema exactly, as the catalog spells them (a
quoted name keeps its case, so `"Orders"` keeps its statistics). A name that differs from the
schema's only in case, as in a hand-made file, is matched to the one schema name it equals.

Inputs are *major* (they change findings) or *minor*. A major gap makes `preflight` exit 3 and
`review` refuse to run until `--accept-missing`, which the skill passes only after the user
says yes. When a broader input is missing, the narrower one is not listed again (no
pg_stat_statements implies no DocDB columns). Muting is applied in `analyze` regardless of the
flag, so `analyze`, `review` and the fixpoint self-check all see the same rule set.

## 9. Catalog mode (experimental)

`--catalog` builds the schema model from a scratch YugabyteDB the reviewer runs locally (Docker
or Podman, a local `yugabytedb/yugabyte` image, never pulled), not from the DDL text. The
bundle's DDL is loaded there and the model is read back from the catalog: key layouts
(`pg_index.indoption`), the constraint behind each index, foreign keys, partitions and their
attached indexes, colocation and tablegroups (`yb_table_properties`). Each index's canonical
definition (`pg_get_indexdef`) goes through the ordinary CREATE INDEX reader. The rules are
the same in both modes.

- Every object is created with one tablet, so a large schema loads on one node; the declared
  `SPLIT INTO n` counts are kept for the model.
- When the server's errors show the dump is incomplete in a known way (a schema or a sequence
  it never created, colocation ids from a colocated database), the gap is supplied and the
  statements run again; each repair is listed in the review's notes.
- A statement the server still rejects is listed, and its objects come from the DDL text.
- Nothing connects to a customer cluster.

## 10. What the engine does not do

`pitfalls.md` lists the traps reviewers have met in practice and what to do about each.

- Measure latency or tablet sizes on the live cluster.
- Judge client retry behaviour, CDC, retention, or erasure.
- Parse every SQL construct. Unresolved columns are listed as open items, and statements
  that are not analysed as access patterns are counted in one (by reason, with their share
  of statement time).

These go under Limitations, or into Reviewer notes with `references/checks.md` as the
reference.
