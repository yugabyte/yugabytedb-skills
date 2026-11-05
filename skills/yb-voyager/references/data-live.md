# Live migration: data phase, cutover, restart

Read this for every live flow (plain live, fall-back, fall-forward): it covers the long-running processes, polling, cutover and recovery. Also load the fall-back or fall-forward reference when the user chose one of those.

## 1. Docs to fetch

- https://docs.yugabyte.com/stable/yugabyte-voyager/migrate/live-migrate/ for the commands and flags of each step.
- https://docs.yugabyte.com/stable/yugabyte-voyager/reference/cutover-archive/cutover/ and https://docs.yugabyte.com/stable/yugabyte-voyager/reference/cutover-archive/archive-changes/ for cutover, cutover status and archiving.

This file adds only order, gates and failure handling.

## 2. Processes

Run each long-running command in its own background shell with `nohup`, its own log and the same export directory. Record each PID and log path. Turn off the progress display if `--help` offers it.

| Process | Command | Ends |
|---|---|---|
| Exporter | `export data` with the export type the live page gives | at cutover |
| Importer | `import data` | at cutover, after draining |
| Archiver (optional) | `archive changes` | when the migration ends |

The importer may start before the exporter; if it says it is waiting, that is expected.

## 3. Ordered steps

1. Confirm with the user that the flow's source and target prep is done and the schema is imported. The user runs anything that changes the source.
2. Start the exporter. Treat its table list as fixed once it has run.
3. Start the importer, with only the flags the live page gives for it.
4. Decide on archiving (section 6).
5. Poll (section 4) until the snapshot is imported for every table and the change lag is near zero and stays there.
6. Run `schema detect-drift` (optional, recommended; see the schema reference). Apply target-side fixes only with the user's approval.
7. **GATE: the user confirms application writes to the source are stopped and the exporter shows no new changes.**
8. Check liveness (section 5).
9. Initiate cutover (section 7), then poll `cutover status` until the source-to-target direction reports completed.
10. Run finalize, validate row counts with the user, then switch the application.
11. Verify: cutover status is completed, the exporter and importer exited (plain live), finalize succeeded. Record the outcome.

## 4. Polling

Run `yb-voyager get data-migration-report` in the foreground, with JSON output if offered, and read the file it names. It connects to the databases, so every password variable the flow uses must be set, or it prompts and hangs.

The report gives exported and imported counts per table, per database. From it:
- The snapshot is complete for a table when its imported snapshot count equals the exported one and its error count is 0. A non-zero error count is a stop: show it to the user.
- Change lag is the exported change events minus the imported ones. Two polls a minute apart with zero lag and no new exported events mean the stream is caught up.

Rates and remaining-event figures may appear only in the process logs; read their tail. After a restart, events can repeat and counts can overshoot: verify with row counts before calling it a failure.

`cutover status` shows each cutover direction and its state. Match on the direction, not the line position, and read the state values as printed.

## 5. Liveness

Check each recorded PID with `kill -0 <pid>`. A gone PID while its work is unfinished is a dead process. Judge liveness by the `yb-voyager` PID, not any child JVM. A process that is alive but silent for several polls: read its log and report to the user rather than restarting it.

## 6. Archiving and disk

Change files accumulate in the export directory, and the source's replication slot retains WAL while the exporter is down or lagging. Either can fill a disk.

1. Check free space with `df` at each poll.
2. Ask the user to choose an archiving policy from the archive-changes page. A policy that deletes files makes them unrecoverable.
3. **GATE: the user approves a deleting policy, or gives a real archive directory.**
4. Start `archive changes` when the page says it can run, against the same export directory.

If the flow config holds archiving placeholders, replace them or comment them out before use. Tell the user that a stopped exporter lets the source retain WAL until its disk fills, and that an abandoned migration leaves a slot the user must drop.

## 7. Cutover

`initiate cutover` commands request a cutover; the running processes do the work. They prompt for confirmation: pass `--yes` only right after the user's go for that exact command.

Pre-checks:
1. Application writes to the source are stopped (the gate in section 3).
2. Every streaming process is alive. If one is dead, the cutover can wait indefinitely without an error. Restart it with the same command first.
3. The cutover may run pre-checks that connect to the source; set every password variable the flow uses. If a pre-check reports a problem, resolve it with the user; skip a pre-check only with their approval.

Take the cutover command and its required options for the chosen flow from the cutover reference and the flow page. If the CLI rejects the docs' form, follow its message.

Then poll `cutover status`. Long in the requested state: check liveness. Completed: move to finalize.

## 8. Finalize

After cutover completes, run `finalize-schema-post-data-import` (schema reference), before final row counts and before the application switch. `end migration` is a separate gate.

## 9. Restart rules

1. Restart a dead live command by re-running the same command with the same export directory. Read the live page for how it resumes.
2. Do not use `--start-clean` on a live command once streaming has started. **GATE: the user approves any `--start-clean` on a live migration, after hearing what it discards (`--help`, docs).**
3. Set the passwords in the environment on every restart.
4. After a restart, re-check liveness and the report.

## 10. Where fall-back and fall-forward differ

The flow files cover the extra processes, the cutover options and what happens after cutover. Read the one the user chose.
