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

For each pattern and the index it reads through, the engine records a *key binding map*: how
the pattern constrains each key column in key order (equality, IN list, range, equality in
every OR arm, filter only, or not at all). From it come the bound prefix DocDB seeks on, the
gap after it, and the columns bound after the gap, which DocDB reaches by seeking under every
value of the gap. The access table prints the map; `review.json` holds it under
`access.binding`. An OR is a disjunction of its arms (OR binds looser than AND); a column every
arm binds with equality is bound arm by arm only when the planner turns the arms into a
BitmapOr, so the map shows it apart (`or=`).

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

Rules marked `"section": "hygiene"` in `rules.json` (WRK005 to WRK010) describe how the
application uses the database. The report lists them under *Workload hygiene*, keeps them out
of the headline, and words their fixes for the application team.

*Pinned by* names the regress output file and a fixed string in it. To read the test, open
`src/postgres/src/test/regress/expected/<file>` in yugabyte-db at the customer's release tag
and search for the string. Only rules that claim planner or execution behaviour (`CAP`,
`PLN`) cite tests, and `scripts/check-rule-refs.py` fails if one does not. `STA`, `WRK`, `CFG`,
`SPL` and lint rules are arithmetic on the bundle; their *Basis* line names the inputs
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

With replay, the access a pattern takes on each table is the replayed plan's: the index it
scans (a partition's scan counts for its partitioned table, its index copy for the parent's
index) and the plan's own key binding, or a sequential scan. The access-path rules (CAP001,
CAP002, CAP003, CAP012, CAP013, CAP014, CAP020, CAP021, CAP031, CAP040, CAP050, CAP051) read
that access, so their findings describe what the plan does:

- **The plan took the access:** the finding is `confirmed (replay)` (labelled with the
  replay image when it is the nearest earlier release, and "planner settings assumed" when
  the bundle had no pg_settings: the plan still stands) and shows each pattern's scan.
- **The model would have chosen otherwise:** the model's choice is not reported as a finding;
  the disagreement is recorded as a `path` gap (review.json gaps), so it stays auditable.
- **The plan was chosen without the customer's statistics (`pg_stats` or table sizes):** it
  corroborates only. The model's choice is the access, findings stay `probable`, and each
  pattern where the plan chose differently is listed under Validation (`path` gaps).
- **The pattern did not plan, or replay ran on another release with unknown differences (or
  a difference in an access rule's tested behaviour):** the model's choice is the access, and
  the finding stays `probable`.

The rules about what a plan does with the rows it reads (CAP010 and CAP011, a sort; CAP030,
partitions appended) are still checked against the plan: matched, confirmed; contradicted
with settings known, removed and listed; contradicted with settings assumed, disputed (the
*Disputed by replay* section). Probes follow the same rule. Plans chosen without statistics
only agree (the finding stays probable) or dispute, and raise no plan-only finding (PLN001 to
PLN003).

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
unverified). On an image of another release whose differences to the customer's are unknown,
or that changed the rule's tested behaviour, a probe neither confirms nor refutes: the finding
stays and says so. Under assumed planner settings a refutation only disputes the finding. Without replay, the finding says its mechanism is unverified on the release.
Nothing is stored: a new release is checked by running the review on it.
`yb-model.py probe --image <img>` runs the probes alone.

## 5. Checking options

Before the report is written, `ybm/safety.py` checks every option the engine can model, one
at a time: its own sketch of an option, a drop of a non-unique index (after the every-node
check), and options that change no schema. Each is applied alone to a model of the schema and
compared with the original; what it would lose is shown with the option as its costs. Options
are alternatives, so they are not applied together here; the combination the reviewer and
the user choose is checked by `yb-model.py plan`. Nothing is amended and nothing is chosen:

- Every PRIMARY KEY and UNIQUE guarantee must survive, on the same or fewer columns and with
  a predicate that is no narrower. `col IS NOT NULL` on a key column does not narrow it: rows
  whose key holds a NULL never conflict, unless the index is NULLS NOT DISTINCT, which the
  replacement must then keep. An option that would lose one carries that cost. No drop is
  offered for a primary key or a unique index. STA002/STA003 on one say in their text what
  each option they offer would do to the guarantee (composite makes only the wider key unique;
  STA003's separate_hot_value leaves the dominant value's rows out); those options are not
  modelled, and they are decisions, never suggestions.
- A foreign key depends on the unique index (or primary key) it was created against, and that
  index cannot be dropped while the key exists. When a fix drops one, the pass re-points the
  key: it is dropped just before the index and added back as written, `NOT VALID` and then
  validated, provided the option leaves a unique index without a predicate on exactly the
  referenced columns (this is how its DDL is written, not a change to it); otherwise its DDL
  is withheld and the option says why in its costs. A primary-key change
  (a table swap) lists the foreign keys to move in its comment lines.
- Every `INSERT ... ON CONFLICT` must still find its arbiter: a unique index on exactly its
  columns (a partial one only when the statement repeats the predicate), or for `ON CONFLICT
  ON CONSTRAINT name` the primary key or unique constraint of that name.
- Every ranked pattern is planned again. Losing an access path, a point lookup, index order
  or coverage is a regression.
- A pure `DROP INDEX` of an index that `pg_stat_user_indexes` shows was scanned is a cost.
- A new index that brings back a defect (a NULL-heavy hash lead, a boolean key) is a cost.

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
options as a lower-case one. A primary-key change, which YSQL cannot make in place, is
written as comment lines (`-- CREATE TABLE ..._new (... PRIMARY KEY (...))`); a colocated
table gets range keys and no SPLIT clause. A new index goes through `schema.new_index_sql`: no
CONCURRENTLY on a partitioned table, and no unique index there without its partition key.
Every modelled option is held as structured changes (review.json `changes`: create, drop,
check, rekey, retire, fk), and DDL is rendered from them only when asked for; the checks, the
plan and the self-check work on the structure. No finding settles another: two findings on
one object keep their own options, and the area makes them one decision.

### Options, suggestions and decisions

A rule states why something matters and offers options (`options` in `rules/rules.json`:
label, when it fits, the trade-off, what to check first, and the kind of change: none, add,
rebuild, drop, rekey, column; every rule has `keep`). A finding's own text gives reasons; it
never instructs a change or carries a statement. The engine models one option per finding by
default, the first of its sketch's kind (`sketched`), plus the drop and no-change options it
can model, and checks each (`checked`, `costs`).

`review.json` `dossiers` groups findings by area (tables joined by partitioning or a foreign
key). Each area holds:

- **suggestions**: the only finding on its object, whose modelled option only adds something
  (a new index) and has no cost; anything that drops, rebuilds or re-keys is never one; offered
  with its reasons, and taken into a plan unless the plan chooses otherwise;
- **decisions**: every other finding with options: an object several findings name, or a
  modelled option that drops, rebuilds or re-keys something (however clean it checks), has a
  cost, or could not be checked; the reviewer consolidates them (SKILL.md step 4b) and the
  user decides;
- **notes**: findings with no options;
- what any change there must keep true (unique guarantees, the index each foreign key and ON
  CONFLICT target relies on, the indexes ranked patterns read through and how, partition
  copies, indexes the counters show in use), and its tensions.

The review document prints no DDL. `yb-model.py sketch <bundle> <finding> --option <id>`
prints it for a chosen option, with what checking it found.

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

Replay picks the exact image, or else the newest earlier image of the same release line,
or else the oldest newer one of that line (`--image` names one outright). A non-exact image's
labels say whether it is an earlier or a newer release, and its differences from the
customer's release apply as above (unknown differences confirm nothing). It sets the customer's effective planner settings explicitly, so the
container launcher's defaults never apply: from the bundle, else the release default, else
(no release facts) the image's own compiled defaults, recorded as assumed. It records the
drift between the two releases and does not let replay confirm a rule whose tested behaviour
differs between them; without release facts for both, the drift is unknown and a replay on a
different image confirms and refutes nothing.

## 7. Self-checks

- **The server as oracle** (with replay): the engine's own models are predictions and the
  replayed plan is the answer. `server.py` reads the plan's deparsed conditions (Index Cond,
  filters, BitmapOr arms) with a reader of its own and gives, per pattern, the plan's key
  binding map for the index it chose; that binding is the pattern's access (above), and each
  disagreement with the model's binding is a `bind.pred` gap (counted under Validation); a
  different index or a scan is a `path` gap, listed there by pattern. Replay also records each statement's
  parameter types (`pg_prepared_statements`); the measured pass binds literals and parameters
  by the plan, and a parameter no condition places gets a neutral value of its type. A
  statement the server cannot prepare as written is handed over rewritten (`COPY (q) TO` is q;
  `database.schema.table` is `schema.table`). Every disagreement and rewrite is recorded in
  `review.json` `gaps` (layers `bind.pred`, `bind.param`, `bind.reader`, `statement`), and the
  review counts them under Validation.
- **Measured pass** (`review --replay --measure`): in the replay container, every single-table
  read is run on a small sample generated from the bundle's statistics, never from the
  customer's data (the only customer text in it is the constants the workload's statements
  bind): two hash prefixes (one per hash key value the workload binds, up to 8),
  the primary key's range columns as a cross product sized by `pg_stats` distinct counts (8
  when unknown) within a row budget per prefix, the values the queries bind present where the
  sample can hold them (the review names any it cannot, `gen.literals`), other columns to
  their distinct count and null fraction. Values come from the server's own types
  (`generate.py`, algorithm A), each in the type's own text form; columns a foreign key or a
  join condition links share values (B). The table keeps every constraint: a column's values
  are first narrowed by its one-column CHECKs and partition bounds, evaluated by the server
  (a foreign key's child then keeps only values its parent holds), and then each sample row
  is the first of several candidates that satisfies the table's CHECKs, partition bounds and
  foreign keys (C). A one-column CHECK no generated value satisfies is dropped; a table no
  candidate row fits (a multi-column CHECK, say) is not measured. These, a predicate the
  server cannot evaluate on candidates, a foreign key that closes a cycle, and rows lost to a
  predicate are recorded in `gaps` (layers `gen.*`). The server then checks the round trip
  (D): each fully loaded table's columns are counted (distinct values and NULLs, exactly, so
  no statistics change the plans) and compared with the plan's (`gen.shape`); a measured plan
  that uses a scan the replayed plan never chose is a plan flip (`gen.plan_flip`), whose
  counters raise and explain nothing; a read that returns no row is `gen.empty`. Full-scale
  statistics are injected after the load, when the bundle has them, so the planner chooses as
  on the customer's table; the plan then runs with `EXPLAIN (ANALYZE, DIST, DEBUG)`. A read
  that seeks far more than it asks for (about one seek per IN element or OR arm) or scans far
  more rows than it returns is an anomaly; one no finding on that pattern explains becomes
  MEAS001, "unexplained, measured", a lead rather than a diagnosis. Join reads and tables no
  candidate row fits are not measured, and the review names them. Gap records are scrubbed
  as they leave the engine: quoted constants, numbers and server-reported row values are
  replaced (`server.scrub`).

- **Fixpoint** (`yb-model.py fixpoint`, also run by `review`): starting from the review as
  reported (after replay and probes), apply each finding's default modelled option, those
  with a known cost included, to a model of the schema and review it again statically
  (disputed findings are not offered, so their options are not applied). The re-runs are
  static, so a finding that replay or a probe disputed or refuted is not counted as new when
  it comes back, and a problem a costly option brings is reported with its cost. Options
  are alternatives, so they are applied in batches that touch different objects. Each
  finding must be gone after its batch, and no new schema finding at medium or above may
  appear; a batch that adds one is re-run one option at a time to name it, and one that only
  the combination brings is reported for the batch. Workload and plan findings describe the
  workload as measured and are not re-checked.
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

## 9. The catalog as the schema model

Whenever a scratch server runs (replay, `--measure`; a review with no workload when the
release and a local image are known; `--catalog` on its own), the
schema model is read from a scratch YugabyteDB the reviewer runs locally (Docker or Podman, a
local `yugabytedb/yugabyte` image, never pulled), not from the DDL text. The DDL text model
serves only static-only reviews and is frozen (AGENTS.md). The
bundle's DDL is loaded there and the model is read back from the catalog: key layouts
(`pg_index.indoption`), the constraint behind each index, foreign keys, partitions and their
attached indexes, colocation and tablegroups (`yb_table_properties`). Each index's canonical
definition (`pg_get_indexdef`) goes through the ordinary CREATE INDEX reader. The rules are
the same in both modes.

- With `--catalog` on its own, every object is created with one tablet, so a large schema
  loads on one node. The replay database (the catalog on the server path) loads the DDL as
  declared, with the master's tablet replica limit lifted. Either way the declared `SPLIT INTO
  n` counts are kept for the model and counts the load chose are dropped.
- One preparation (`catalog.prepare`) makes every scratch database (catalog, replay, measured
  pass): when the server's errors show the dump is incomplete in a known way (a schema or a
  sequence a column default names, colocation ids from a colocated database), the gap is
  supplied and the statements run again; each repair is listed in the review.
- A statement the server still rejects has no object in the model: it is listed with the
  server's message, and a pattern that reads a table it would have made is listed as not
  planned for that reason, not as a failing statement.
- Nothing connects to a customer cluster.

## 10. What the engine does not do

`pitfalls.md` lists the known gaps and the traps reviewers have met, each with the engine's
status (`handled`, `flagged`, `not detected`), and what to do about each.

- Measure latency or tablet sizes on the live cluster.
- Judge client retry behaviour, CDC, retention, or erasure.
- Parse every SQL construct. Unresolved columns are listed as open items, and statements
  that are not analysed as access patterns are counted in one (by reason, with their share
  of statement time).

These go under Limitations, or into Reviewer notes with `references/checks.md` as the
reference.
