# Live migration: data phase, cutover, restart

Read this for every live flow (plain live, fall-back, fall-forward): it covers the long-running processes, polling, cutover and recovery. Also load the fall-back or fall-forward reference when the user chose one of those (SKILL.md routes to them).

Contents:
1. Docs to fetch
2. Process topology
3. Ordered steps
4. Polling
5. Liveness
6. Archive changes and disk
7. Cutover
8. Finalize
9. Restart rules
10. Where fall-back and fall-forward differ

## 1. Docs to fetch

Fetch https://docs.yugabyte.com/stable/yugabyte-voyager/migrate/live-migrate/ for the command lines and flags of each step, and https://docs.yugabyte.com/stable/yugabyte-voyager/reference/cutover-archive/cutover/ and https://docs.yugabyte.com/stable/yugabyte-voyager/reference/cutover-archive/archive-changes/ for the cutover and archive flag tables. This file adds only order, gates and failure handling.

**Docs gap:** the cutover reference lists `--prepare-for-fall-back` as optional, omits the hidden `--yes`, and writes the status as "NOT INITIATED". Use the facts in sections 4 and 7 below.

## 2. Process topology

Run each long-running command in its own background shell with `nohup`, its own log file and the same `--export-dir`. Record each PID and log path in the progress file. Pass `--disable-pb true` so the log is parseable.

| Process | Command | Ends |
|---|---|---|
| Exporter | `export data from source --export-type snapshot-and-changes` | at cutover (snapshot first, then change streaming) |
| Importer | `import data to target` | at cutover, after draining |
| Archiver (optional) | `archive changes --policy ...` | when the migration ends |

Short-lived commands: `get data-migration-report`, `cutover status`, `schema detect-drift`, `initiate cutover ...`, `finalize-schema-post-data-import`, `end migration`.

The importer may start straight after import schema. It prints "Waiting for export data from source to start..." until the exporter is up, which is healthy.

## 3. Ordered steps

1. Confirm the preconditions with the user: PG source with `wal_level=logical` (needs a source restart), the grant script run in the variant that matches the chosen flow, target prepared, schema imported. The user runs the source-mutating steps; print the command and its effects, then wait.
2. Start the exporter. Do not change `--table-list` on a later restart: the table set is locked after the first run.
3. Start the importer. Do not pass `--table-list` or `--exclude-table-list` to it in live mode (rejected).
4. Decide on archive changes (section 6) and start it if chosen.
5. Poll the report (section 4) until every table's snapshot is imported and the change lag is near zero and stays there.
6. Run `yb-voyager schema detect-drift` (tech preview; optional but recommended). Read a clean result as "no table or column drift": it compares tables and columns only. Fetch https://docs.yugabyte.com/stable/yugabyte-voyager/reference/schema-migration/detect-drift/ for the output and correction steps. Apply target-side corrections only with the user's approval.
7. **GATE: the user confirms application writes to the source are stopped and the exporter's export rate is 0.**
8. Check liveness (section 5). Both the exporter and importer must be alive.
9. Initiate cutover (section 7), then poll `cutover status` until the `source → target` row is `COMPLETED`.
10. Run finalize (section 8), validate row counts with the user, then switch the application.
11. Verify: `cutover status` shows `COMPLETED`, the exporter and importer have exited (plain live), and finalize exited 0. Record the outcome in the progress file.

## 4. Polling

Run the report in the foreground with the passwords exported:

```
yb-voyager get data-migration-report --export-dir <dir> --output-format json
```

The command prints only "Data migration report is written to ..." and writes `<export-dir>/reports/data-migration-report.json`. It overwrites that file on every run, so copy it first if you need history.

**Docs gap:** the docs never give this path, the fields, or the fact that the command connects to the databases.

- It connects to the databases: export `TARGET_DB_PASSWORD`, plus `SOURCE_DB_PASSWORD` for fall-back and `SOURCE_REPLICA_DB_PASSWORD` for fall-forward. Without them it prompts and a background run hangs.
- It works only for `snapshot-and-changes` migrations, and only after the exporter has initialised the migration.
- The file is an array with one row per table per `db_type` (`source`, `target`, and `source-replica` for fall-forward). Fields: `table_name`, `db_type`, `exported_snapshot_rows`, `imported_snapshot_rows`, `errored_imported_snapshot_rows`, `exported_inserts`, `exported_updates`, `exported_deletes`, `imported_inserts`, `imported_updates`, `imported_deletes`, `final_row_count`. `iteration_number` appears only in iteration reports.

Lag maths per table:
- The snapshot is complete when the target row's `imported_snapshot_rows` equals the source row's `exported_snapshot_rows` and `errored_imported_snapshot_rows` is 0. A non-zero errored count is a stop: show it to the user.
- Change lag is (source `exported_inserts + exported_updates + exported_deletes`) minus (target `imported_inserts + imported_updates + imported_deletes`). Two polls a minute apart with lag 0 and unchanged exported counts mean the stream is caught up.

"Remaining events" and the export rate are not in the JSON. They appear only in the process stdout or log (the progress display; hence `--disable-pb true`). Read the tail of the exporter and importer logs for them. After a restart or server state change, events can repeat and the report counts can exceed expectation; verify with row counts and tell the user, and do not treat a count mismatch alone as failure.

`cutover status` prints a text table with columns `CUTOVER DIRECTION | STATUS | REQUESTED AT` and has no JSON output. The statuses are `NOT_INITIATED`, `INITIATED` and `COMPLETED`. Direction cells read `source → target`, `target → source` (fall-back) and `target → source-replica` (fall-forward), with the arrow character. Match on the row's direction, not its line position. After an iterative cutover the output has one block per iteration.

## 5. Liveness

Each voyager command holds `<export-dir>/.<command>Lockfile.lck`, which contains its PID. A PID that is no longer running while the status is not `COMPLETED` is a dead process. Check with `kill -0 <pid>`. The Debezium JVM is a child of the exporter; judge liveness by the voyager PID, not the JVM.

A process that is alive but silent for several polls: read the log tail before acting, and report it to the user rather than restarting it.

## 6. Archive changes and disk

Change segments accumulate under the export dir, and the source's replication slot retains WAL while the exporter is down or lagging. Both can fill a disk.

1. Check free space on the export-dir filesystem with `df` at each poll during a long snapshot-and-changes phase.
2. Ask the user to choose the policy. `archive` copies processed segments to `--archive-dir` (separate storage with enough space) and then removes them. `delete` removes processed segments once usage passes the threshold (default 70%) and makes them unrecoverable.
3. **GATE: the user approves `--policy delete`, or supplies a real archive directory for `--policy archive`.**
4. Start `archive changes` only after change streaming has started (it errors before that), against the parent export-dir.

Every live config template ships `archive-changes.policy: archive` with the literal `archive-dir: <path-to-archive-dir>` uncommented. Replace it with a real path, or comment the block out, before using the config file; the placeholder makes the command fail or archive to a bogus path.

Tell the user that an exporter left down lets the source slot retain WAL until the source disk fills. Restart the exporter promptly. If the migration is abandoned, the user drops the slot on the source.

## 7. Cutover

All `initiate cutover` commands only set a flag; the work happens in the running processes. Re-running one after it was initiated does nothing. The commands prompt "Are you sure (y/n)". The hidden `--yes` (`-y`) skips the prompt: pass it only right after the user's explicit go for that exact command, because a non-TTY run otherwise hangs.

Pre-checks before initiating:
1. Application writes to the source are stopped (the gate in section 3).
2. Both the exporter and importer PIDs are alive. If the exporter is dead, the cutover is accepted but stays `INITIATED` with no error, and the importer keeps running silently. Restart the exporter (same command), then poll `cutover status` again.
3. Export `SOURCE_DB_PASSWORD`. `initiate cutover to target` runs the schema drift check itself and needs the source password for it. If it finds drift and the run is non-interactive, the cutover is not started. Resolve the drift with the user, or pass `--skip-pre-checks schema_drift` only with their approval.

Plain live (no rollback):

```
yb-voyager initiate cutover to target --export-dir <dir> --prepare-for-fall-back false
```

`--prepare-for-fall-back` is mandatory for every migration that is not fall-forward; the command exits with `missing required flag` without it. Do not add other flags.

Then poll `cutover status`:
- `INITIATED` for a long time: a paired process is probably dead. Voyager reports no error for this, so check section 5.
- `COMPLETED`: the exporter and importer have exited on their own. Move to finalize.

## 8. Finalize

After cutover is `COMPLETED`, run `yb-voyager finalize-schema-post-data-import`. Add `--refresh-mviews true` only if the schema has materialized views and the user wants them refreshed. It adds the NOT VALID constraints and refreshes views, so run it before final row counts and before the application switch. Fetch the live-migrate page above for the exact flags. `end migration` is a separate gate owned by SKILL.md.

## 9. Restart rules

1. Restart any dead live command by re-running the same command with the same `--export-dir` and arguments. It resumes from saved state, and the exporter resumes from the slot.
2. Do not use `--start-clean` on a live command once streaming has started: it wipes state and leaves the source slot to be handled by hand. **GATE: the user approves any `--start-clean` on a live migration, after hearing what it discards.**
3. Supply the passwords through the environment on every restart; a background process has no usable prompt.
4. After a restart, re-check liveness and the report before continuing.

## 10. Where fall-back and fall-forward differ

- Fall-back: the grant variant is chosen at plan time, cutover uses `--prepare-for-fall-back true`, and the processes switch roles at cutover.
- Fall-forward: a fourth process, `import data to source-replica`, is started by hand before cutover, and the cutover flag is not set to true.
