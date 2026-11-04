# Schema phase (PostgreSQL source)

Read this when the migration reaches the schema phase: export schema, the analyze loop, import schema, target verification, finalize and detect-drift.

Docs to fetch (base `https://docs.yugabyte.com/stable/yugabyte-voyager`; append `.md` for plain text):
- `/reference/schema-migration/export-schema/`, `/analyze-schema/`, `/import-schema/`, `/finalize-schema-post-data-import/`, `/detect-drift/` for flags, keys and the files each command writes.
- `/known-issues/postgresql/` for the workaround for each analyze issue.

Record each gate decision and every schema edit in the progress file.

## 1. Export schema

1. Finish the assessment and its review first (assess-and-plan). Export uses the assessment's recommendations.
2. Run `export schema`. Keep Voyager's permission checks and any schema-snapshot option on, because later steps (detect-drift) depend on them; check `--help` for options that would turn either off.
3. Read what export printed and list the export directory's schema folder. Show the user:
   - any report export wrote about changes it applied (for example colocation or sharding), with a summary of each change;
   - any file the docs or the later analyze output say is not imported. Its contents never reach the target unless the user applies them by hand.
4. If export kept backup copies of files it rewrote, edit only the live file the analyze report points to.

**GATE: the user accepts export's changes and decides what to do with anything that will not be imported.**

A re-run of `export schema` may do nothing once the schema is exported. `--start-clean` wipes the exported schema and every manual edit; use it only with approval and a snapshot (section 2).

## 2. Snapshot before any edit

Copy the exported schema folder to a sibling directory before the first edit and before each import attempt. Diff against it to show the user each round's changes. It is also the recovery path for a bad edit.

## 3. Analyze loop

Ask the user for the exact target YugabyteDB version and pass it to `analyze-schema` (see `--help`). The default may hide issues that exist on an older target.

Per round:
1. Run `analyze-schema` with JSON output if offered, and read the report file it names.
2. For each issue, read its fields (object, reason, statement, file, suggestion, docs link) and fetch the linked known-issues entry.
3. Classify it (below) and present a table: object, reason, file, class, proposed edit as a diff. Link the docs entry; do not paste the workaround.
4. Apply only what the user approved, to the file the issue names, then re-run.

Exit: a fresh report lists no issues and no notes about objects left out. The exit code does not tell you this; read the report, and confirm it is newer than your last edit.

Classes:
- **A, mechanical:** schema-only, no behaviour change, and the known-issues entry gives the workaround. Propose the batch; apply after one approval.
- **B, user decision:** the workaround changes behaviour or data, or is optional (performance recommendations). Draft options, state what the user gives up, and wait.
- **C, no workaround:** the known-issues entry has none. Stop and give the options: exclude the object, change the source, change the target version, or accept the gap.

Never delete a table, column or index to reach a clean report. Entries the known-issues page marks as needing no workaround are not fix items.

For a live flow, also check the user's datatypes against the live-migration limitations on the known-issues page; an offline analyze run may not flag them.

**GATE: the user approves each round's edits (class A as a batch, each class B decision, class C escalation).**

## 4. Import schema

Pre-checks, each confirmed before running:
1. The latest analyze report is clean. Import does not block on outstanding issues.
2. Files that will not be imported are handled or knowingly deferred.
3. Target database and user are confirmed, and the user has the privileges the import-schema page requires (a missing privilege can trigger a prompt that hangs a background run).
4. Colocation matches the assessment. If import asks about colocation, show the user; never answer it for them.
5. Roles, extensions and other dependencies the schema needs exist on the target. Check the docs for what Voyager does not migrate.
6. The target is empty of the migrated objects, or the user knows what is there.

**GATE: the user approves creating objects on the target.**

Run `import schema`. Read its output and the import-schema page for which objects it creates now and which wait for finalize.

Failure recovery:
1. Read the error and the failing statement. Assume objects created before the failure stay created.
2. Fix the schema file or the missing target prerequisite. Snapshot first.
3. Re-run, using the options `--help` offers to skip objects that already exist or to import one object type. An edited object that already exists keeps its old definition on the target: drop it there first (user approval, exact `DROP` shown).
4. An option to continue past errors needs explicit approval. Afterwards, find the list of failed statements the command reports, show every one, and treat the schema as not imported until each is fixed or accepted.

`import schema --start-clean` can drop target schemas and everything in them. Read `--help` and the page for what it removes, list the affected schemas, offer it only before any data import, and never combine it with `--yes`.

**GATE: the user approves any `--start-clean` or continue-past-errors run, with the schemas or statements affected named.**

## 5. Verify against the target

1. Confirm no statements are listed as failed.
2. Count tables, indexes, views, functions, sequences and triggers per schema on the target from the catalog (`pg_class`, `pg_proc`, `pg_trigger`).
3. Compare with the object counts in the last analyze report and with any files left out. Report every difference.

## 6. Finalize schema (data phase)

`finalize-schema-post-data-import` creates what import schema deferred; the page lists what that is. Run it from the data phase: offline once every table is imported, live after cutover to the target completes. The same recovery rules as section 4 apply.

## 7. Detect drift

`schema detect-drift` is read-only. Read its page for what it compares and its maturity, and report only what it covers (for example "no table or column drift", never "no schema drift" if it skips other objects). Recommend it before cutover in live flows and whenever the user says the source schema changed.

1. It needs the schema snapshot from export; if that was turned off, it fails.
2. Run it with the source password set, then read the report file it names. Relay each change with its severity and the corrective step. Stop and ask on anything it calls unrecoverable.
3. It writes no DDL. Any fix on the target is a user-approved step.

## 8. Traps

| Trap | Why it breaks | Do this instead |
|---|---|---|
| `export schema --start-clean` after edits | Wipes all edits | Snapshot, then ask |
| Editing a backup copy | Voyager imports the live file | Edit the file the report names |
| Reading exit code as "clean" | Analyze succeeds with issues present | Read the report |
| Default target version | Hides issues on older targets | Pass the real version |
| Skipping existing objects after editing one | The old definition stays | Drop it with approval, then re-run |
| Ignoring files that are not imported | Their DDL never reaches the target | Show them, get a decision |
| Dropping an object for a clean report | Silent data or feature loss | Escalate as class B or C |
