# End migration

Read this when the data phase is validated (offline) or cutover is complete (live) and the user wants to close out the migration with `yb-voyager end migration`.

Docs (fetch for the flag table and the ybvoyager user cleanup SQL):
- https://docs.yugabyte.com/stable/yugabyte-voyager/reference/end-migration/ for flags.
- https://docs.yugabyte.com/stable/yugabyte-voyager/migrate/migrate-steps/ ("End migration" and "Delete the ybvoyager user") for the flow and the ownership-transfer SQL.

`end migration` cannot be resumed or continued once it succeeds, and it deletes whatever it does not back up. Treat it as the most destructive step in the skill.

## 1. Pre-checks

1. Offline flows: confirm from the progress file and a fresh `yb-voyager import data status --export-dir <export-dir> --output-format json` that both jobs exited, every table is `DONE`, `errored_count` is 0, and the data-phase validation passed. If anything is unfinished, go back; do not rely on the command's own prompt to catch it.
2. Live, fall-back and fall-forward flows: run `yb-voyager cutover status --config-file <config>` and require `COMPLETED`. For fall-back or fall-forward, also confirm with the user whether the reverse stream is still needed, because ending the migration drops it.
3. Check that `yb-voyager version` matches the value in the progress file.
4. Confirm the passwords are exported without echoing them: `TARGET_DB_PASSWORD`, plus `SOURCE_DB_PASSWORD` for live flows, plus `SOURCE_REPLICA_DB_PASSWORD` for fall-forward. When one is unset the command asks for it with a hidden TTY read, and a background run blocks forever. Do not put passwords in the config or on a command line.
5. List the voyager processes still running (PIDs from the progress file). If the user has other voyager commands on this export-dir that must keep running, stop and ask.

## 2. Ask about backups (mandatory)

`end migration` requires all four backup flags and refuses to run without them.

**Docs gap:** the docs table does not mark the four flags as required, and the shipped config template's `end-migration` section sets all four to `false`. Running with the template untouched deletes schema, assessment, data, logs, reports and metadata from the export-dir with no backup. Set each flag explicitly after asking; do not leave template defaults in place.

Ask the user about each one and the backup directory:

1. `backup-schema-files`: keep the exported schema (and any fixes made to it).
2. `backup-data-files`: keep the exported snapshot data. Without it the only copy of the exported data is deleted. It uses disk, so tell the user the size of `<export-dir>/data` first.
3. `save-migration-reports`: keep the assessment, analyze-schema and data-migration reports, including the status JSON files under `reports/`.
4. `backup-log-files`: keep Voyager's logs, which matter if anything needs diagnosing later.
5. `backup-dir`: required when any flag is true, and the directory must already exist. Create it only with the user's agreement, on a filesystem with enough free space.

Backups move files (`mv`) instead of copying them, so the export-dir no longer holds them afterwards. If the user might need to re-import, say so now: after this step the export-dir cannot be reused.

Record each answer and the backup-dir in the progress file before running.

## 3. What it does

In order: saves reports, moves schema files, moves the `.sql` data files and `data/errors`, clears Voyager's state tables on the target (and on the source and source-replica for live flows), drops the replication slot and publication for live flows, moves logs, then removes the schema, assessment, data, logs, reports, temp and metadata directories from the export-dir. For an offline PostgreSQL migration nothing is left on the source to clean up.

If other voyager commands are running it asks whether to continue by stopping them, then signals them. Show the user the PID list from the pre-checks and let them answer that prompt, unless the gate below covers it.

## 4. Run it

**GATE: the user approves this exact `end migration` command, with the four backup flag values, the backup-dir, and the consequence that the migration cannot be resumed afterwards.**

1. Print the command in full (flags set explicitly, no passwords). Use the config file the flow already uses with the values in its `end-migration` section, or pass them as flags.
2. Pass `--yes` only right after that explicit go, because a non-TTY run hangs on the y/n prompt. Do not add it to a config.
3. Launch with `nohup` and a log file as in the data phase, record the PID, and poll the PID and the log.
4. Verify: the command exited 0, the backup-dir holds the items the user chose, and the export-dir no longer holds the removed directories. Report both lists.

If it fails partway, re-run the same command; it is meant to be re-run. Do not start over or delete anything by hand. If the same error repeats, quote the log line from the end-migration log (`yb-voyager-end-migration.log` under the export-dir or backup-dir `logs/`) and ask the user.

## 5. Offer the clean-up the user runs

The agent offers these and prints the exact statements; the user runs them with admin credentials. Say what each one changes.

1. Target cluster gflags set for the migration: revert the ones the user chose to change for Voyager (listed in the progress file). This may need a rolling restart.
2. Source `REPLICA IDENTITY`: live flows set it to FULL on every table. Offer statements to restore the previous setting; the user must know or look up the old value, since Voyager does not record it.
3. Source `wal_level`: set it back from `logical` only if nothing else needs it. This needs a source restart.
4. The `ybvoyager` user on source and target: the docs give the ownership reassignment and drop SQL. Fetch it, print it, and wait. Dropping the user is irreversible and fails while it still owns objects.

Close out the progress file with the end-migration outcome and what the user chose to revert.
