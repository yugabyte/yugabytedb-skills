# Offline data migration: export, import, validate

Read this for the data phase of an offline flow: running `export data` and `import data` unattended, polling them, recovering from failures, and validating the result.

Docs (fetch for commands, flags, status commands and examples):
- https://docs.yugabyte.com/stable/yugabyte-voyager/migrate/migrate-steps/ for export data, import data, finalize and verify.
- https://docs.yugabyte.com/stable/yugabyte-voyager/reference/data-migration/export-data/ and https://docs.yugabyte.com/stable/yugabyte-voyager/reference/data-migration/import-data/ for every flag and how to get status.
- https://docs.yugabyte.com/stable/yugabyte-voyager/reference/performance/ for tuning, which the user decides.

## 1. Launch both jobs in the background

1. Confirm the source and target password variables are set, without printing them. A missing one becomes a prompt that hangs the job.
2. Confirm no other run of the same command is active on this export directory (PIDs in the progress file).
3. Launch `export data`, then `import data`, each with the flow's config file, output redirected to a log file, progress display off if `--help` offers it:

```bash
nohup yb-voyager export data --config-file <config> > <migration-dir>/export-data.out 2>&1 &
echo $!
```

   Do not use `screen` or `tmux`; you cannot read them.
4. Record each PID, log path and launch time in the progress file.
5. Verify after a few seconds: the PID is alive (`kill -0 <pid>`) and the log is growing. A live PID with an empty log suggests a prompt.

Find where Voyager writes its own logs from the command output or the docs, and read those first when something looks wrong. If import says it is waiting for export, that is expected; read the docs before treating it as a hang.

## 2. Poll status

Every few minutes (scale to data size), run the status commands the docs give for offline export and import. Prefer JSON output; read the file the command says it wrote, since it may print only a path. Each call overwrites it.

Read the status values and counts per table as the file gives them. Partitioned tables may roll up into a parent row, so a missing child row is not a failure by itself.

## 3. Done, failed or stuck

A status file is a snapshot. Combine it with the process state.

**Done:** the PID exited with code 0, every table shows a finished status, and every error count is 0. A finished-with-errors status is not success: stop and ask the user, because rows were skipped.

**Failed:** the PID is gone with a non-zero exit, or a table is unfinished after the process exited. Quote the exact error line from the log, look it up in the docs or the docs search tool, and report it. Do not work around a safety check (permission, replication, guardrail) by turning it off.

**Stuck:** the PID is alive and the total count has not moved across 3 polls spaced a few minutes apart. Counts move in batches, so one flat poll means nothing. Read the log for repeating retries or a waiting prompt, then report the evidence. Do not kill a job on a single flat poll.

Retry a failed command at most once without diagnosis; after that, report and wait.

## 4. Recovery

Read the docs for whether each command resumes. If a plain re-run refuses and the CLI says a fresh start is needed, that is a gate:

1. Explain what `--start-clean` removes on that command (`--help`, docs) and what it invalidates downstream (a restarted export invalidates a partial import).
2. **GATE: the user approves each `--start-clean` run, with the files or target tables affected named.**
3. Pass `--start-clean` on that run only; add `--yes` only after the approval.

Options that truncate target tables, skip rows or overwrite on conflict are user decisions with their consequence stated. Do not edit table lists or files in the export directory to work around a failure. Do not upgrade Voyager mid-migration.

## 5. Tuning

Leave performance and parallelism options at their defaults unless the user asks or a diagnosed cause points at one. Some options conflict with each other; read `--help` and the performance page, and let the user decide.

## 6. Finalize, then validate

Order matters: finalize, then count, then end migration. A count taken before finalize (for example while the finalize gate is open) is a progress check, not the validation: finalize changes what the target holds and can fail, so repeat the counts after it succeeds and report those.

1. **GATE: the user approves `yb-voyager finalize-schema-post-data-import`.** Tell them what it creates (finalize page) and that Voyager cannot undo it.
2. Compare row counts, read-only, over `psql`:
   - source `SELECT count(*)` per table against the exported count (on a quiet source; changes during export make mismatches expected);
   - imported count equals the total and the error count is 0 for every table;
   - target `SELECT count(*)` per table equals the source count.
3. Compare every sequence's `last_value` on source and target, and give the user the `setval` statement for any mismatch.
4. If the source schema may have changed during the run, suggest `schema detect-drift` (schema reference).
5. Report one row per table (source count, target count, errors) plus sequence mismatches. Record "data validated" in the progress file.

**GATE: the user confirms the validation and agrees to move on to end migration.**

Optional and user-driven: https://docs.yugabyte.com/stable/yugabyte-voyager/reference/compare-performance/ needs a workload on both databases, so offer it and do not run it.
