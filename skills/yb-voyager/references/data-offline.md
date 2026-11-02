# Offline data migration: export, import, validate

Read this for phase 5 of an offline flow (PostgreSQL source to YugabyteDB): running `export data` and `import data` unattended, polling them, recovering from failures, and validating the result.

Contents: launch, poll status, done/failed/stuck, recovery, knobs to leave alone, finalize and validate.

Docs (fetch for exact flags and examples; this file covers only orchestration):
- https://docs.yugabyte.com/stable/yugabyte-voyager/migrate/migrate-steps/ for the export data, import data, finalize and verify steps.
- https://docs.yugabyte.com/stable/yugabyte-voyager/reference/data-migration/export-data/ and https://docs.yugabyte.com/stable/yugabyte-voyager/reference/data-migration/import-data/ for every flag.
- https://docs.yugabyte.com/stable/yugabyte-voyager/reference/performance/ for tuning, which the user decides.

Live, fall-back and fall-forward flows do not use this file: `export data status` and `import data status` refuse to run there and point at `get data-migration-report`.

## 1. Launch both jobs in the background

A foreground call blocks the session, and a prompt in a background job hangs it forever, so prepare before launching.

1. Confirm the passwords are exported (`test -n "$SOURCE_DB_PASSWORD"` and `test -n "$TARGET_DB_PASSWORD"`, without echoing them). Without them the command prompts on a TTY and the job hangs with a live PID and a silent log.
2. Confirm nothing else runs on this export-dir. Voyager takes a lock per command, and a second launch errors out instead of queueing. Check the PIDs in the progress file before relaunching.
3. Launch `export data`, then `import data`, each with the config file the earlier phases used, `--disable-pb true` (the progress bar floods a captured log) and output redirected to a file in the migration directory:

```bash
nohup yb-voyager export data --config-file <config> --disable-pb true \
  > <migration-dir>/export-data.out 2>&1 &
echo $!
```

   Use the same pattern for `import data` (output to `import-data.out`). Do not use `screen` or `tmux`, because the agent cannot read them.
4. Record each PID, the `.out` path and the launch time in the progress file.
5. Verify: wait a few seconds, confirm the PID is alive (`kill -0 <pid>`) and that the `.out` file or `<export-dir>/logs/yb-voyager-export-data.log` has grown. A live PID with an empty log suggests a prompt; see "Failed" below.

Voyager's own logs are `<export-dir>/logs/yb-voyager-export-data.log` and `<export-dir>/logs/yb-voyager-import-data.log`. Read these first when something looks wrong.

**Import may start early.** `import data` waits for the export to finish and logs "Waiting for snapshot data export to complete...". Starting both together is fine, and that message is healthy, not a hang. `import data status` is the exception: it errors with "cannot run `import data status` before data export is done" until the export ends.

**Docs gap:** the docs never say how to run these unattended, where the logs are, or how to tell done from failed; this file is the source for that.

## 2. Poll status

Run these read-only commands every few minutes (scale to data size):

```bash
yb-voyager export data status --export-dir <export-dir> --output-format json
yb-voyager import data status --export-dir <export-dir> --output-format json
```

**Docs gap:** `--output-format json` writes a file and prints only "... report is written to <path>" to stdout. Read the file after each call; each call overwrites it.

- `<export-dir>/reports/export-data-status-report.json`: array of `{table_name, status, exported_count}`. Status values: `EXPORTING`, `DONE`, `NOT_STARTED`, and the hyphen spelling `NOT-STARTED` (partitioned-table roll-up). Accept both spellings.
- `<export-dir>/reports/import-data-status-report.json`: array of `{table_name, file_name (omitted for offline), status, total_count, imported_count, errored_count, percentage_complete}`. Status values: `NOT_STARTED`, `MIGRATING`, `DONE`, `DONE_WITH_ERRORS`. Counts are rows.

Partitioned tables roll leaf status up to the root, so a missing root or leaf row is not a failure.

## 3. Done, failed or stuck

A status file is a snapshot and cannot tell done from failed from stuck. Combine it with the process state.

**Done (export):** the export PID has exited, every row is `DONE`, and the log tail shows completion. Prefer the exit code over the status file when you can read it; otherwise treat an error line in the log tail as failure.

**Done (import):** the import PID has exited cleanly, every table is `DONE`, and every `errored_count` is 0. `DONE_WITH_ERRORS` is not success: stop and ask the user, because rows were skipped.

**Failed:** the PID is gone with a non-zero exit, or a table sits below 100% after the process exited. Read the last 50 lines of the matching log under `<export-dir>/logs/` and quote the exact error line to the user. Rejected rows land in `<export-dir>/data/errors/` only when the user chose `error-policy-snapshot: stash-and-continue`. Check these known causes before reporting:
- Lock waits on export: another session holds a conflicting lock (DDL, `LOCK TABLE`), because export with parallel jobs needs table locks. Report "something else is running DDL on the source"; do not raise parallelism.
- "Sending too long RPC message" or a row-size error: point the user at the docs section on large row sizes.
- Replication or CDC detected on the target: surface it to the user; do not bypass it with `skip-replication-checks`.
- Guardrail failure at start: fix the missing permission or table (re-run `import schema` for a missing table, then a plain re-run). Do not set `run-guardrails-checks` to false.
- A silent import hang when `adaptive-parallelism-max` was set below the default parallel jobs: check `yb-voyager version` against https://docs.yugabyte.com/stable/yugabyte-voyager/release-notes/ before diagnosing.

**Stuck:** the PID is alive and the sum of `imported_count` (or `exported_count`) is unchanged across 3 polls spaced a few minutes apart. Counts move in steps because batches are large (default 20000 rows or about 200 MB), so one flat poll means nothing. Before acting, read the log for a repeating retry or a node-health message, and check for a blocked prompt (empty log, live PID). Report the evidence to the user. Do not kill a job on a single flat poll.

Retry a failed command blindly at most once; after that report and wait.

## 4. Recovery

Offline `export data` cannot resume: re-running it after an interruption fails with "data directory is not empty, use --start-clean flag ...". `import data` can resume.

**Docs gap:** the docs describe `--start-clean` for export but never say a plain re-run fails.

**Export recovery:**
1. Stop and read the log to learn why it failed.
2. Explain to the user that the only restart is `export data --start-clean`, which deletes `<export-dir>/data` and Voyager's export state (the source is untouched), and that any partial import becomes invalid and needs `import data --start-clean` too.
3. **GATE: the user approves `export data --start-clean` (and the matching import `--start-clean` if an import had started).**
4. Append `--start-clean true` to that one run only; it is not a config key, so it cannot stay set. Add `-y` only after the approval, because without it a completed export prompts on stdin and the background job hangs.
5. Verify: the PID is alive and `export-data-status-report.json` shows tables moving to `EXPORTING`.

**Import recovery:** re-run the identical command with no extra flag. Completed tables are skipped ("Already imported tables: ...") and a partly loaded table resumes at batch granularity. "All the tables are already imported, nothing left to import" means it was complete.

**Import `--start-clean` trap:** it clears Voyager's import state, then, if target tables hold rows, asks whether to continue without truncating. Answering yes re-ingests in upsert mode, which duplicates rows in tables without a primary key. Use it only when a fresh export forces it or the user wants a full reload.
- `--truncate-tables true` (valid only with `--start-clean true`) truncates every table in scope, including FK-related siblings. It is irreversible.
- **GATE: the user approves `import data --start-clean`, with the target tables that will be truncated or upserted into named.** Pass `-y` only after that approval.

Do not edit table lists to work around a failure (Voyager guards them between runs), and do not edit files under `<export-dir>/data`, because hand edits void the status counts. `error-policy-snapshot: stash-and-continue` and `on-primary-key-conflict: IGNORE` skip rows silently, so offer them only as a user decision with that consequence stated.

Do not upgrade the Voyager binary mid-migration; a release can refuse to continue a migration started on an older one. When resuming, compare `yb-voyager version` with the value in the progress file.

## 5. Knobs to leave alone

Leave these at their defaults unless the user asks or a diagnosed cause points at one: `adaptive-parallelism`, `adaptive-parallelism-max`, `batch-size`, `export-data.parallel-jobs`, `run-guardrails-checks`, `skip-replication-checks`, `enable-upsert`, `use-public-ip`, `target-endpoints`, `beta-fast-data-export`.

- `import-data.parallel-jobs` conflicts with adaptive parallelism (on by default): setting both is a hard error. **Docs gap:** the import reference example passes `--parallel-jobs 12` alone, which the CLI rejects. Do not copy it; a fixed value also requires disabling adaptive parallelism, and that is the user's call. `adaptive-parallelism-max` likewise needs adaptive parallelism enabled.
- If the target cluster looks like the bottleneck, ask the user. Do not switch to `aggressive`: the docs allow it only when nothing else runs on the cluster.

## 6. Finalize, then validate

Order matters: finalize first, then count, then end migration.

1. **GATE: the user approves `yb-voyager finalize-schema-post-data-import`.** Tell them it can take long and Voyager cannot undo it; it creates the NOT VALID constraints and can refresh materialized views. Fetch https://docs.yugabyte.com/stable/yugabyte-voyager/reference/schema-migration/finalize-schema-post-data-import/ for flags. Use this command, not the deprecated `import schema --post-snapshot-import`.
2. Compare row counts, read-only, over psql:
   - Source `SELECT count(*)` per table against `exported_count`. Run it on a quiet source; if the source kept changing during export, mismatches are expected.
   - `imported_count` equals `total_count` and `errored_count` is 0 for every table.
   - Target `SELECT count(*)` per table equals the source count.
3. Check sequences on the target. Voyager restores only sequences owned by or attached to migrated columns. For every other sequence, compare `last_value` with the source and give the user the `setval` statement to run if they differ.
4. If the source schema may have changed during the run, suggest `yb-voyager schema detect-drift` (tech preview; compares tables and columns only).
5. Verify and report one row per table: source count, target count, errored count, plus any sequence mismatch. Record "data validated" in the progress file.

**GATE: the user confirms the validation result and agrees to move on to end migration.**

Optional and user-driven: https://docs.yugabyte.com/stable/yugabyte-voyager/reference/compare-performance/ needs a workload run on both databases, so offer it and do not run it. If the source is flat files, use the bulk-data-load page; this skill does not orchestrate it.
