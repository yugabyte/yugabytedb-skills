# End migration

Read this when the data phase is validated (offline) or cutover is complete (live) and the user wants to close out the migration with `yb-voyager end migration`.

Docs (fetch for the options, what the command removes, and the migration-user cleanup SQL):
- https://docs.yugabyte.com/stable/yugabyte-voyager/reference/end-migration/
- https://docs.yugabyte.com/stable/yugabyte-voyager/migrate/migrate-steps/ ("End migration" and the migration-user cleanup)

`end migration` cannot be resumed once it succeeds, and it removes whatever it does not back up. Treat it as the most destructive step in the skill.

## 1. Pre-checks

1. Offline: confirm from a fresh status read that both jobs finished, every table is done with no errors, and the validation passed.
2. Live flows: `cutover status` shows the cutovers the user wanted as completed. For fall-back or fall-forward, confirm with the user that the reverse stream is no longer needed, because ending the migration stops it.
3. `yb-voyager version` matches the progress file.
4. Every password variable the flow uses is set (without printing it). A missing one becomes a prompt that hangs a background run.
5. List the Voyager processes still running from the progress file. If any must keep running, stop and ask.

## 2. Ask about backups (mandatory)

1. Read `--help` and the end-migration page for the backup options and what each keeps.
2. Ask the user about each option and the backup directory. Tell them the size of what each would keep (`du -sh`).
3. Set every option explicitly on the command line. Never rely on a value from a config template: if a template has them preset, it decides for the user.
4. If the page says backups move files out of the export directory, tell the user the export directory cannot be reused afterwards.
5. Create the backup directory only with the user's agreement, on a filesystem with enough space.

Record each answer and the backup directory in the progress file.

## 3. What it does

Read the end-migration page for what the command removes from the export directory and cleans up on the target, source and source-replica (for example replication state). Summarise it for the user before the gate. If it offers to stop running Voyager processes, show the user the PID list and let them decide.

## 4. Run it

**GATE: the user approves this exact `end migration` command, with every backup option value, the backup directory, and the consequence that the migration cannot be resumed.**

Backup answers given in advance, or a general "carry on" earlier in the run, are inputs to this command, not approval to run it. Show the command and wait for a yes to it, even when nothing else is left to do.

1. Print the command in full (options explicit, no passwords).
2. Pass `--yes` only right after that explicit go.
3. Run it with `nohup` and a log file, record the PID, poll the PID and the log.
4. Verify: exit code 0, the backup directory holds what the user chose, and the export directory matches what the page says remains. Report both.

If it fails partway, read the page for whether it can be re-run, and re-run the same command if so. Do not delete anything by hand. If the same error repeats, quote the log line and ask the user.

## 5. Offer the clean-up the user runs

Print the exact statements and say what each changes; the user runs them with admin credentials.

1. Revert every source and target change recorded in the progress file (cluster flags, table settings, server settings), using the docs for the statements and noting any that need a restart. If the old value was not recorded, the user must look it up.
2. Drop or keep the migration user on source and target, using the docs' cleanup SQL. Dropping is irreversible and fails while it still owns objects.

Close out the progress file with the outcome and what the user chose to revert.
