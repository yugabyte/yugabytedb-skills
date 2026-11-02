# Assess and plan: assessment, flow choice, stage-2 config, prep

Read this after setup is done (stage-1 config, assessment-level grants, passwords exported) and before any schema work. It covers running `assess-migration`, reading the report, choosing the flow with the user, writing the flow's config, and the prep the user runs on source and target.

Contents:

1. Pre-assess checks
2. Run the assessment
3. Read and present the report
4. PK-less table check
5. Flow decision
6. Choices that lock in
7. Stage-2 config
8. Flow-specific prep
9. Target database, colocation, sizing
10. Gates

Docs (fetch for commands, flags and limitation lists), all under https://docs.yugabyte.com/stable/yugabyte-voyager/:

- `migrate/assess-migration/`: assessment guide
- `reference/assess-migration/`: assessment flags and config keys
- `introduction/`: the flows and the target version floor per flow
- `migrate/live-migrate/`, `migrate/live-fall-back/`, `migrate/live-fall-forward/`: per-flow prep and "Limitations"

## 1. Pre-assess checks

Assessment accuracy depends on the source state. Check, then tell the user what you found. Run the queries as `ybvoyager` with `PGPASSWORD="$SOURCE_DB_PASSWORD" psql -Atc`.

1. `pg_stat_statements`: `SELECT extname FROM pg_extension WHERE extname='pg_stat_statements'` and `SHOW shared_preload_libraries`. Without it, query-level findings are limited and the run prompts "continue anyway". Enabling it needs a restart, so the user decides whether to enable it or continue without.
2. Statistics: `SELECT count(*) FROM pg_stat_user_tables WHERE last_analyze IS NULL AND last_autoanalyze IS NULL`. Some performance recommendations need `ANALYZE` to have run. `ANALYZE` writes statistics on the production source, so ask before running it (first gate below).
3. Workload: IOPS are captured for the capture interval (default 120 seconds). On an idle source the report shows zero IOPS and sizing is meaningless, and the command prompts "continue anyway". Ask the user to have the typical workload running during the capture. Do not answer that prompt yourself.
4. Source type: this skill covers PostgreSQL sources only (SKILL.md routes the rest).

## 2. Run the assessment

1. Confirm the passwords are exported (`test -n "$SOURCE_DB_PASSWORD"`, never echoed).
2. Run in the background so the session stays usable, with a log and a PID:

```bash
nohup yb-voyager assess-migration --config-file <stage1.yaml> --primary-only \
  > <export-dir>/../assess.log 2>&1 &
echo $! > <export-dir>/../assess.pid
```

When the user wants read replicas assessed, drop `--primary-only` and set `source.read-replica-endpoints` in the config. Do not add `--yes`: it answers every prompt with yes, including the zero-IOPS and no-objects ones, which the user must see.

3. Poll the log. The run is healthy while the PID is alive and the log grows, and it lasts at least the capture interval. It is hung when the PID is alive and the log ends in a question (`[Y/N]`, "continue anyway", "include these replicas"). Kill it, show the user the question, fix the cause (set the key, enable the extension) and re-run.
4. Success is exit code 0 and a non-empty `<export-dir>/assessment/reports/migration_assessment_report.json` (an `.html` sits beside it). Check with `test -s`. The "generated report" line goes to the log file under `<export-dir>/logs/`, not to the console.
5. A re-run in the same export directory fails with "assessment metadata or reports files already exist". `--start-clean` deletes the previous assessment: copy the old report aside and ask first.
6. Record the report path and the PID outcome in the progress file.

**Docs gap:** the docs say the report path "is printed on the console" and never give it, and omit that a re-run needs `--start-clean`. Use the path above.

## 3. Read and present the report

The JSON has no flow recommendation, despite the guide's wording. You gather evidence; the user chooses. Show each field verbatim, then your reading:

| Field | Use |
|---|---|
| `VoyagerVersion` | Must equal the progress-file version |
| `MigrationComplexity`, `MigrationComplexityExplanation` | Show the rating and explanation as written. `NOT AVAILABLE` means no rating, not low |
| `Sizing.SizingRecommendation`: `NumNodes`, `VCPUsPerInstance`, `MemoryPerInstance`, `ColocatedTables`, `ShardedTables`, `ColocatedReasoning`, `EstimatedTimeInMinForImport` | Cluster size, colocation set, import time estimate |
| `Sizing.FailureReasoning` | Non-empty means sizing failed: report "no sizing", not zero nodes |
| `AssessmentIssues[]`: `Category`, `Type`, `Name`, `Impact`, `ObjectName`, `SqlStatement`, `DocsLink`, `MinimumVersionsFixedIn` | Group by `Category` (`unsupported_features`, `unsupported_datatype`, `unsupported_query_constructs`, `unsupported_plpgsql_objects`, `migration_caveats`), then by `Impact` (Level-1 to Level-3) |
| `TableIndexStats` | Per-table size and read/write IOPS: write rate and size drive the offline-versus-live question |

Present sizing with its assumptions: RF3, 4 GiB of memory per core, and the baseline version named in the assessment guide's overview. State this every time; the numbers do not adjust to other settings.

Two evidence rules for the flow decision:

- Issues whose `Type` starts with `UNSUPPORTED_DATATYPE_LIVE_MIGRATION` mean those columns break live, fall-back and fall-forward. Show them before asking about flows. The fix is a schema change, which is the user's decision.
- An issue with `Type` `SAVEPOINT_USAGE` ties to the fall-back and fall-forward limitation about savepoints. Fetch the flow page's Limitations for the version floor and compare it with `yb-voyager version`.

Write the complexity rating, the count of live-blocking issues and the sizing into the progress file.

## 4. PK-less table check

The report's missing-primary-key finding is narrower than the live-flow limitation ("tables without a primary key are not supported"), so run your own check before offering any live flow:

```sql
SELECT n.nspname, c.relname
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE c.relkind IN ('r','p') AND n.nspname = ANY(string_to_array('<schemas>',','))
  AND NOT EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conrelid=c.oid AND k.contype='p');
```

Any row rules out the live flows for that table until the user adds a key or accepts offline. List the rows for the user. Also ask whether partitions are created dynamically (pg_partman, cron): adding or dropping partitions during a live migration is not supported.

## 5. Flow decision

Ask in this order, one question at a time, and let the user answer:

1. PostgreSQL version, and whether the source is RDS, Aurora, Cloud SQL, Azure or standalone (live prep differs).
2. Can the application stay offline for the whole export plus import? Compare with `EstimatedTimeInMinForImport` and the `TableIndexStats` sizes.
3. Is a way back to the old source needed after cutover?
4. If yes: may Voyager write to the source (fall-back), or will the user provide a third database (fall-forward)?
5. Target: self-managed or Aeon, and the YugabyteDB version. The introduction page's target table sets version floors per flow and connector.

| Downtime allowed | Rollback after cutover | Flow | Cost to state |
|---|---|---|---|
| Whole export plus import; writes can freeze | Restore from backup | Offline | None on the source; simplest |
| Minutes | None | Live | `wal_level=logical` (source restart), replication slot grows on the source until cutover or end of migration, every table needs a primary key, no live-blocking datatypes |
| Minutes | Back to the original source | Live + fall-back | Live costs, plus Voyager writes into the source (write grants), PG below 15 needs extra handling at cutover, "may alter your source" warning |
| Minutes | To a separate database; source untouched | Live + fall-forward | Live costs, plus a third PostgreSQL database with a hand-created schema |

Never pick for the user. Offer bulk data load only when the user says the data is already in CSV or TEXT files (SKILL.md routes it). Fetch the fall-back page before offering fall-back or fall-forward: it covers connector choice and its limits.

## 6. Choices that lock in

Settle these before stage 2. Each is hard or impossible to change in a started export directory:

- Fall-back or not: decide at plan time. The source grants and the cutover flag both depend on it.
- Grant ownership variant (live flows): changes production table ownership or role grants (section 8).
- Schema list and any table or exclude list: fixed once data export starts. Changing them means a new export directory.
- Export type (snapshot only or snapshot and changes): fixed per export directory.
- Colocation: decided at `CREATE DATABASE`.
- Special characters in schema or table names: unsupported in every flow. List any you find and let the user decide.
- The replication slot holds WAL on the source from the start of live export. Pausing between phases costs source disk.

Record the choices in the progress file.

## 7. Stage-2 config

Do not hand-write the full config: the templates carry hundreds of commented lines. The docs tell users to copy `offline-migration.yaml` whatever the flow; use the flow's own template.

1. List `/opt/yb-voyager/config-templates/` and copy the chosen flow's template (`offline-migration.yaml`, `live-migration.yaml`, `live-migration-with-fall-back.yaml` or `live-migration-with-fall-forward.yaml`) to a new file next to the stage-1 file. Use the local copy, which matches the installed version, not the GitHub link in the docs.
2. Carry over `export-dir` (the same directory), the `source:` values, `assess-migration.target-db-version`, and the `target:` values from the user (host, port, db-name, user; the password stays in the environment). For fall-forward also fill `source-replica:`.
3. Leave `target.db-schema` commented. For PostgreSQL sources schema names carry over, and the configuration-file reference limits that key to MySQL and Oracle sources.
4. Comment out `control-plane-type` and the whole `yugabyted-control-plane:` block, unless the user wants the yugabyted UI and gave a real connection string.
5. Live flows: `archive-changes.archive-dir` ships as an uncommented placeholder. Set it to a real directory outside the export directory with free space, or comment it out if the user will not run archiving.
6. Check the keys that encode the flow: `export-data-from-source.export-type` is `snapshot-and-changes` for live flows and snapshot-only for offline, and `initiate-cutover-to-target.prepare-for-fall-back` is `true` only for fall-back. Leave other keys alone unless the user asks.
7. Keep passwords, `yes` and `start-clean` out of the file.
8. Verify: `grep -nE '^(control-plane-type|yugabyted-control-plane)' <file>` prints nothing, `grep -n 'archive-dir' <file>` shows a real path or a commented line, and `grep -n 'password' <file>` shows only commented or empty values. The first command that loads the file (export schema) validates every key and fails on an unknown one before changing anything.
9. Record the stage-2 file path in the progress file.

**Docs gap:** the templates ship `control-plane-type: yugabyted` and the `archive-dir` placeholder uncommented although described as optional, and a copied template then points at a yugabyted that does not exist and fails.

## 8. Flow-specific prep (the user runs it)

Every row changes a production system. Print the exact command or setting and its effects, then wait for the user to say it is done. The command text lives on the flow's docs page ("Prepare the source database", "Prepare the target database"); this table says what differs.

| Item | Offline | Live | Live + fall-back | Live + fall-forward |
|---|---|---|---|---|
| Grant script `is_live_migration` | 0 | 1 | 1 | 1 |
| `is_live_migration_fall_back` | 0 | 0 | 1 | 0 |
| `replication_group` variable | not needed | required | required | required |
| Source `wal_level=logical` (RDS: `rds.logical_replication=1`) | no | yes, restart or reboot | yes | yes |
| Source effects | none beyond grants | `REPLICA IDENTITY FULL` on every table in the schema list, replication role, `CREATE` on the database, ownership change or owner-role grants | live effects plus write grants on tables and sequences | same as live |
| Extra database | none | none | none | source-replica database with the schema created by hand (no indexes or triggers yet) and a privileged user |
| Target | superuser user; colocated DB if recommended | cluster flags that disable query-layer retries, read-committed off; revert after migration | live items plus ports 7100 and 9100 reachable from the Voyager host (Aeon: replication-role setting) | live items |

Rules for the live variants:

- The grant script asks "Enter choice (1/2)" for how the migration user gets table ownership. The user runs it in their own `psql` session so they can answer. Recommend choice 2 (grant each original owner role to the migration user). Choice 1 transfers ownership of every table in the schema list to the replication group, which can break applications that rely on owner privileges and has no documented undo. If the script is run without a terminal it fails at the prompt, after it has already set `REPLICA IDENTITY FULL` and the replication role, so a failed run still changed the source: record it as changed.
- Re-running the script at the live level after the assessment-level run is the documented path. A user who prepared without the fall-back variant and later wants fall-back re-runs it with `is_live_migration_fall_back=1` before cutover.
- Fall-back on PostgreSQL below 15: ask the major version now. The script cannot grant the replica-role setting there. The fall-back page's "Cutover to the target" section covers the superuser or trigger and foreign-key handling; fetch it and let the user choose.
- Record every source and target change (wal_level, gflags, replica identity, ownership, replication role) in the progress file, because `end migration` offers to revert them (SKILL.md).
- Verify with the read-only queries from setup: `SHOW wal_level` is `logical`, the replication role is present, and the replica-identity query returns zero rows.

**Docs gap:** the live pages show the grant command without mentioning the interactive ownership prompt, and do not say that assessment-level grants (`is_live_migration=0`) are not enough for a live flow.

## 9. Target database, colocation, sizing

1. Read `Sizing.SizingRecommendation.ColocatedTables`. When non-empty, the target database must be created colocated, and that cannot be changed afterwards.
2. Ask the user to create the target database (`CREATE DATABASE <name> WITH COLOCATION = true`) or to confirm an existing one. The user runs it, because it acts on their cluster. Do this after the user has seen the report.
3. Verify: `SELECT yb_is_database_colocated();` on the target returns `t` when colocation was recommended.
4. Never answer the "continue without colocation?" prompt of `import schema` for the user, and never use `--yes` there.
5. Voyager never applies sizing. Show `NumNodes`, `VCPUsPerInstance` and `MemoryPerInstance` with the section 3 caveats, and ask the user to resize or confirm before data import.

**Docs gap:** the assessment guide creates the colocated database after the report while the flow pages create it before assessment. Follow the guide's order. A report with no colocated tables needs no colocated database.

## 10. Gates

**GATE: the user approves running `ANALYZE` on the source, or declines it, before assessment.**

**GATE: the user reviews the assessment report (complexity, live-blocking issues, PK-less tables, sizing) and states the flow.**

**GATE: the user approves the stage-2 config file, the grant script variant and ownership choice, and the source and target prep commands.**

**GATE: the user confirms the prep is done and the target database (colocated if recommended) exists.**
