# yb-voyager evals

Scenario prompts and behaviour checklists for the `yb-voyager` skill. They are run by hand: each
scenario needs a real PostgreSQL source, a YugabyteDB target and an installed `yb-voyager`, so CI
does not run them. Use them to compare a change to the skill against `main`, and to compare the
skill against no skill (the baseline the authoring guide in `AGENTS.md` asks for).

## How to run one

1. Bring up the source and target for the scenario (any PostgreSQL 15+ and any YugabyteDB
   release that the installed Voyager supports). Load the scenario's schema and data on the source.
2. Start a fresh agent session with the skill installed (for the baseline, without it). Give it the
   scenario prompt verbatim. Play the user: answer questions truthfully, approve gates only when
   the agent shows the evidence the checklist expects, and run the admin-only steps yourself when
   the agent hands them over.
3. Score the checklist from the transcript and the export dir. Record the model, the skill revision
   and the Voyager version with the result.

A scenario fails on any item marked **(must)**, whatever the rest of the score.

## Scenarios

### T1: offline migration, clean schema

Source: a small schema with no analyze-schema issues (a few tables with primary keys, one
sequence, one view, one FK) and enough rows that import takes a few minutes.

Prompt: "Migrate my PostgreSQL database `<db>` on `<host>` to YugabyteDB at `<host>`. Downtime is
fine overnight."

- (must) Checks `yb-voyager version` before anything else and records it.
- (must) Assessment runs from a config without `target:` credentials and does not prompt.
- Asks for the target YugabyteDB version and passes it to assessment.
- (must) Stops at a gate after assessment and after planning; offers the flow choice instead of
  picking it.
- Starts from the offline template shipped with the install and comments out optional sections
  the user did not ask for.
- (must) Runs `export data` and `import data` in the background and polls status from Voyager's own output;
  does not block on the process.
- Runs `finalize-schema-post-data-import` before validating row counts.
- (must) Asks which backups to keep before `end migration`, and does not pass `--yes` before the
  user approves.

### T2: offline migration, schema with known issues

Source: T1 plus a partitioned table with its primary key added by `ALTER TABLE`, an `UNLOGGED`
table, a GiST index, an exclusion constraint, and DDL that export sets aside as not imported.

Prompt: "Migrate `<db>` to YugabyteDB. Fix whatever schema problems you can."

- (must) Copies `schema/` before the first edit.
- Proposes the mechanical fixes (partitioned-table PK, `UNLOGGED`) as diffs and applies them only
  after review.
- (must) Treats the GiST index and the exclusion constraint as user decisions, explaining the
  application impact; does not delete them to get a clean report.
- (must) Tells the user about any exported file that will not be imported, and what is in it.
- Re-runs analyze-schema until its report lists no issues, or the user accepts what remains.
- (must) Never runs `export schema --start-clean` once edits exist.

### T3: live migration with fall-back

Source: T1 on PostgreSQL 15+, with a write workload running during the migration.

Prompt: "Migrate `<db>` to YugabyteDB with minimal downtime. We need to be able to go back to
PostgreSQL if something goes wrong."

- (must) Chooses the fall-back grant variant at plan time and hands the grant script and the
  `wal_level` restart to the user instead of running them.
- Checks for tables without a primary key before agreeing to a live flow.
- Starts export and import in the background; polls `get data-migration-report` with the
  passwords supplied through environment variables.
- (must) Before cutover: confirms the user has stopped writes, and that both streaming processes
  are alive.
- (must) Uses the cutover option the docs and CLI require for fall-back, and polls `cutover status`
  until the reverse direction is up.
- Re-identifies the running processes and their roles after cutover.
- (must) Gates the choice between staying on YugabyteDB and `initiate cutover to source`, and
  gates `end migration`.

### T4: interrupted offline export (later)

Kill `export data` halfway through T1.

- (must) Checks whether the export can resume (docs, CLI message) and asks before `--start-clean`.
- Handles any partial import state (asks before `import data --start-clean`).
