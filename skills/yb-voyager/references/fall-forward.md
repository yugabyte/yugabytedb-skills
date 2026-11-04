# Live migration with fall-forward

Read this when the user chose live migration with fall-forward (a separate PostgreSQL database, the source-replica, is fed from the target after cutover). It adds to the live reference; follow that file for everything not covered here.

## Docs to fetch

- https://docs.yugabyte.com/stable/yugabyte-voyager/migrate/live-fall-forward/ for source-replica prep, `import data to source-replica`, the cutover steps and limitations.
- https://docs.yugabyte.com/stable/yugabyte-voyager/reference/data-migration/import-data/ for the `import data to source-replica` flags.
- https://docs.yugabyte.com/stable/yugabyte-voyager/reference/cutover-archive/cutover/ for the cutover options.

## Plan-time decisions

1. Use the source grant variant the live-fall-forward page gives; fall-forward does not write back to the source.
2. Ask the user for the source-replica connection details and record them (not the password). The user exports the source-replica password variable named on the configuration-file page.
3. After cutover, changes are read from YugabyteDB: cross-check column types and the target version against the connector requirements on the live-fall-forward and cutover pages.

## Prepare the source-replica

The user prepares the source-replica; it is a separate PostgreSQL the agent does not administer.

1. Follow the page's steps for the source-replica user and schema. The page says which objects to leave out until after cutover.
2. Print each command and its effect; the user runs the ones that need admin credentials.
3. **GATE: the user confirms the source-replica schema is loaded as the page describes and the connection works.**

## Start the source-replica importer before cutover

Start `import data to source-replica` by hand in its own background shell, with its own log, the same export directory and the password in the environment, at the point the live-fall-forward page gives. Take the exact command from the page. Record its PID.

Include it in every report poll (the report has rows for the source-replica; its imported counts must follow the target's) and in the liveness check before cutover. Restart it with the same command if it dies.

## Cutover to the target

Follow the live reference, using the cutover command and options the cutover reference gives for fall-forward. If the CLI rejects an option, follow its message.

After cutover, read the live-fall-forward page for which processes run now. Re-identify the running Voyager processes and update their PIDs and roles in the progress file. If one dies, re-run the command its log records.

`cutover status` shows a direction for the target and one for the source-replica. Poll the source-to-target direction to completed, then finalize and validate as in the live reference.

## Optional: cutover to the source-replica

1. Ask the user whether to move the application to the source-replica.
2. **GATE: the user confirms application writes to YugabyteDB are stopped and approves `initiate cutover to source-replica`.**
3. Check the reverse processes are alive, then run `yb-voyager initiate cutover to source-replica` with the options the cutover reference gives. Pass `--yes` only after the user's go.
4. Poll `cutover status` until the source-replica direction reports completed.
5. On the source-replica, create what was left out before cutover and re-enable what was disabled, per the page. **GATE: the user approves these statements and runs them with admin credentials.**
6. Verify row counts on the source-replica, switch the application, then go to end migration.
