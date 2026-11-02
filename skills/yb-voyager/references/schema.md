# Schema phase (PostgreSQL source)

Read this when the migration reaches the schema phase: export schema, the analyze loop, import schema, target verification, finalize and detect-drift.

Contents:
1. Export schema
2. Snapshot before any edit
3. Analyze loop
4. Import schema
5. Verify against the target
6. Finalize schema (data phase)
7. Detect drift
8. Traps

Docs to fetch (base `https://docs.yugabyte.com/stable/yugabyte-voyager`; append `.md` to a page URL for plain text):
- `/reference/schema-migration/export-schema/`, `/analyze-schema/`, `/import-schema/`, `/finalize-schema-post-data-import/`, `/detect-drift/` for flags and config keys.
- `/known-issues/postgresql/` for the fix recipe of each analyze issue.

Record each gate decision and every schema edit in the progress file (SKILL.md).

## 1. Export schema

Run `yb-voyager assess-migration` first and finish its review. A bare `export schema` assesses on its own but does not apply the colocation and sharding recommendations in that run, so the assessment would be unreviewed and unapplied.

1. Confirm the source user can use every schema in `--source-db-schema` and that the assessment is done.
2. Run `export schema` with the flags from the docs. Do not pass `--run-guardrails-checks false` (it hides missing permissions) or `--disable-schema-snapshot-capture` (detect-drift needs the snapshot).
3. Open `<export-dir>/reports/schema_optimization_report.html` and summarise it for the user. Export applies colocation and sharding changes to `schema/tables/table.sql` without asking, so the user must see what changed.
4. List `<export-dir>/schema/`. If `uncategorized.sql` exists, show its contents to the user: Voyager never imports it, so its DDL is silently missing from the target unless the user applies it by hand.
5. Note the backup copies Voyager leaves next to the schema files it rewrote (the original pg_dump output). Edit the live file named in the analyze report's `FilePath`, never the backup.

**GATE: user accepts the optimization report and decides what to do with `uncategorized.sql`**

**Docs gap:** no page names the report paths, `uncategorized.sql`, `failed.sql` or the backup files. Take file names from the analyze report's `FilePath` field instead of guessing.

Re-running `export schema` is a no-op ("Schema is already exported"). `--start-clean` wipes `schema/` and every manual edit; use it only when the user wants a fresh export and has a snapshot (section 2).

## 2. Snapshot before any edit

Copy `<export-dir>/schema/` to a sibling directory (for example `<export-dir>/schema-snapshot-<n>`) before the first edit, and again before each import attempt. Diff against it to show the user exactly what changed per round. The snapshot is the recovery path, so a fresh export is never needed to undo an edit.

## 3. Analyze loop

Ask the user for the exact target YugabyteDB version (read it from the cluster if reachable) and pass it as `--target-db-version`. The flag hides issues fixed in that version, so the default (latest stable) makes an older target look cleaner than it is.

Per round:
1. Run `analyze-schema --output-format json` plus the target version. Use a single format so there is one file to parse; the command overwrites it each run and is safe to repeat.
2. Read `<export-dir>/reports/schema_analysis_report.json`. Top level: `Summary` (with `Notes` and `DatabaseObjects`) and `Issues`. Per issue, use `IssueType`, `ObjectType`, `ObjectName`, `Reason`, `SqlStatement`, `FilePath`, `Suggestion` and `DocsLink` (maps to a `/known-issues/postgresql/` anchor).
3. Fetch the `DocsLink` anchor for each issue and classify it (below).
4. Present a table: object, reason, file, class, proposed edit as a diff. Link the anchor; do not paste the docs workaround.
5. Apply only what the user approved, edit the file named in `FilePath`, then re-run from step 1.

Exit criterion: the fresh JSON has `Issues` empty and `Summary.Notes` has no `uncategorized.sql` note. The exit code does not signal this (the command exits 0 with issues present). Confirm the report timestamp is newer than the last edit.

Classes:
- **A, mechanical:** schema-only, no behaviour change (for example a primary key moved into `CREATE TABLE` for a partitioned table, unsupported storage parameters removed, `UNLOGGED` to logged, `%TYPE` to an explicit type). Propose the batch, apply after one user approval.
- **B, user decision:** the application or data changes (exclusion or deferrable constraints, constraint triggers, generated columns, `WITH CHECK OPTION`, advisory locks, unsupported index types, extensions, case-sensitive schema names, policies on roles that do not exist on the target, foreign tables needing a server). Draft options, state what the user gives up, and wait. The performance recommendations (hotspot, missing foreign-key index, redundant index, partial index) are also B: apply only on opt-in because they change which queries use an index.
- **C, no workaround:** table inheritance on a version without support, large objects, unsupported datatypes, two-phase commit, `LISTEN/NOTIFY`, `REFERENCING` triggers and similar. Stop and tell the user the options: exclude the object, change the source, change the target version, or accept skipped data.

Delete no table, column or index to reach a green report; that trades the user's data for a clean report. Entries the known-issues page marks "workaround not required" or "N/A" are not fix items.

If the flow is live (decided after assessment), also check the user's datatypes against the live-migration unsupported list on the known-issues page, because the offline analyze run does not flag them.

**GATE: user approves each round's edits (class A as a batch, each class B decision, class C escalation)**

## 4. Import schema

Pre-checks, each one confirmed before running:
1. The latest analyze report is clean (section 3). Import does not block on outstanding issues, so the agent is the only gate.
2. `uncategorized.sql` has been handled or the user knowingly deferred it.
3. Target DB name and user are confirmed; the user is a superuser (a non-superuser triggers an interactive "continue anyway" prompt that hangs a background run).
4. Colocation matches the assessment. If the assessment recommended colocated tables and the target database is not colocated, import prompts "continue without colocation". Check the database setting first and ask the user; do not answer with `-y` blindly.
5. Roles, extensions and foreign-table servers that the schema depends on exist on the target. Voyager migrates none of them.
6. The target database is empty of the migrated objects, or the user knows what is there.

**GATE: user approves creating objects on the target database**

Run `import schema` once. The first run creates tables, indexes, triggers, functions and views; it skips foreign-key and other not-valid constraints for later, and creates materialized views empty. Voyager retries "missing dependency" failures at the end of the run, so ordering errors often resolve themselves: do not reorder files first.

**Docs gap:** the import-schema page describes a second run that creates indexes and triggers after data import, and recommends `--post-snapshot-import`. That is outdated: indexes and triggers come from the first run, and the post-data step is `finalize-schema-post-data-import` (section 6).

Failure recovery:
1. Read the error and the failing statement. Import is not transactional: objects before the failure stay created, and the run stops at the first failure by default.
2. Fix the schema file or the target prerequisite (missing role, extension). Snapshot first.
3. Re-run with `--ignore-exist true`. It skips only "already exists" errors. An object you edited that already exists keeps its old definition on the target, so drop that object on the target (user approval, exact `DROP` shown) and re-run.
4. Use `--object-type-list` to re-import one type after fixing it.
5. Use `--continue-on-error true` only with explicit user approval. It exits 0 even when statements fail and produces a partial schema. After the run, check for `<export-dir>/schema/failed.sql`, show every statement in it to the user, and treat the schema as not imported until it is empty or the user accepts each failure. The next run overwrites that file, so copy it first.

`import schema --start-clean` runs `DROP SCHEMA <name> CASCADE` for each schema in the export, usually including `public`. That deletes every object in it, imported data and non-Voyager objects included, and cannot be undone. Offer it only before any data import, list the schemas that will be dropped, and never combine it with `-y`.

**GATE: user approves `--start-clean` or `--continue-on-error`, naming the schemas or statements affected**

**Docs gap:** `import schema --start-clean` is missing from the import-schema reference page.

## 5. Verify against the target

1. Confirm `schema/failed.sql` does not exist.
2. Connect to the target with `psql` (password from `TARGET_DB_PASSWORD`) and count tables, indexes, views, functions, sequences and triggers per schema from `pg_class`, `pg_proc` and `pg_trigger`.
3. Compare with `Summary.DatabaseObjects` in the last analyze report and with `uncategorized.sql`. Report any difference to the user.

## 6. Finalize schema (data phase)

`finalize-schema-post-data-import` creates the constraints that import schema skipped and, with `--refresh-mviews true`, populates the materialized views. It does not create indexes or triggers. Run it from the data phase, not here: offline flows once `import data status` shows every table done; live flows during cutover to the target, after `cutover status` reports COMPLETED. Earlier runs can fail while data is still arriving. Fetch the finalize page for flags; the same `--ignore-exist` and `failed.sql` rules from section 4 apply.

## 7. Detect drift

`detect-drift` is a tech preview, read-only, and compares tables and columns only. It does not see index, constraint, view or function changes, so report "no table or column drift" and never "no schema drift". Recommend it before cutover in live flows and whenever the user says the source schema changed.

1. Export schema and export data must have kept the schema snapshot (no `--disable-schema-snapshot-capture`). Otherwise the command fails rather than reporting no drift.
2. Run it with `--source-db-schema` (required) and the password in `SOURCE_DB_PASSWORD`. If `--table-list` is used and `public` is not among the schemas, qualify every entry as `schema.table`.
3. Read `<export-dir>/reports/drift_analysis_report.json`. Relay each change with its severity and the report's corrective step. Stop at any "Breaks the migration: unrecoverable" entry and ask the user.
4. The report writes no DDL. Any DDL to fix the target is a user-approved step.

Fetch `/reference/schema-migration/detect-drift/` ("Reading the report") for the severity meanings.

From a config file, uncomment the `schema-detect-drift` section that the config templates ship; it takes `output-format`, `table-list`, `exclude-table-list`, `object-type-list`, `exclude-object-type-list` and `log-level`, and the source connection comes from the `source` section.

## 8. Traps

| Trap | Why it breaks | Do this instead |
|---|---|---|
| `export schema --start-clean` after edits | Wipes `schema/` and all edits | Snapshot, then ask the user |
| Editing the backup file | Voyager imports the live file | Edit the file in `FilePath` |
| Reading exit code as "clean" | Analyze exits 0 with issues | Check `Issues` is empty |
| Default `--target-db-version` | Hides issues fixed in newer versions | Pass the real target version |
| `--ignore-exist` after editing a created object | Old definition stays on the target | Drop the object with approval, then re-run |
| Ignoring `uncategorized.sql` | Its DDL never reaches the target | Show it, get a decision |
| Dropping an object for a green report | Silent data or feature loss | Escalate as class B or C |
| Fixing `rule.sql` views | Known harmless pg_dump layout | Leave as is |
