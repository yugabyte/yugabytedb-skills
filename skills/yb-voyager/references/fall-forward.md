# Live migration with fall-forward

Read this when the user chose live migration with fall-forward (a separate PostgreSQL database, the source-replica, is fed from the target after cutover). It is loaded with the live-phase files (SKILL.md's phase table); follow the live phase for everything not covered here. Everything fall-forward needs beyond that is in this file.

## Docs to fetch

- https://docs.yugabyte.com/stable/yugabyte-voyager/migrate/live-fall-forward/ for source-replica prep, `import data to source-replica`, the cutover steps and limitations.
- https://docs.yugabyte.com/stable/yugabyte-voyager/reference/yb-voyager-pg-grant-migration-permissions/ for the grant variant this flow uses.
- https://docs.yugabyte.com/stable/yugabyte-voyager/reference/data-migration/import-data/ for the `import data to source-replica` flags.
- https://docs.yugabyte.com/stable/yugabyte-voyager/reference/cutover-archive/cutover/ for the cutover options and the connector choices.

## Plan-time decisions

1. Use the source grant variant the live-fall-forward page gives; fall-forward does not write back to the source. Build its command and answers as SKILL.md's grant-script rule describes, and show it in the approval format (SKILL.md). **GATE: the user approves running the grant variant.** Then run it through the admin connection, or the user runs it if there is none.
2. Ask the user for the source-replica connection (host, port, database, user) and record it, not the password. Its password variable is named on the configuration-file page; the user sets it the same way as the source's (SKILL.md, Passwords). Ask whether you also get an admin connection to the source-replica.
3. After cutover, changes are read from YugabyteDB. Read the cutover docs page for the connector used and its version requirements. If the target does not meet them, present the alternatives and their limitations from the docs; the user decides. Cross-check column types against the connector's limitation list on the live-fall-forward page.
4. Check that the stream from YugabyteDB can reach it the way its connector connects. The default connector reconnects to the addresses `SELECT host, port FROM yb_servers()` returns, not the address in the config. From the Voyager host, run that query and test a connection to one returned `host:port`. If it is unreachable (a container hostname, a private IP behind NAT, an internal port), the export from the target dies at cutover with no node available. The user fixes the advertised address before cutover.
5. Settle prompts that appear only at cutover. The export from the target starts automatically after the forward stream has stopped, so it does not read your input pipe, and a question there (for example a warning that the bundled connector is older than the target's YugabyteDB release) kills it. Read the cutover page and the release notes for such warnings for the installed version and target release, tell the user, and record their answer. If one appears anyway, the process has died. Show the user the question, and re-run that same command with their answer.
6. Record the flow, grant variant, connector choice and the answers from steps 4–5 in the progress file.

## Prepare the source-replica

1. Follow the page's steps for the source-replica user and schema. The page says which objects to leave out until after cutover.
2. Show each command in the approval format. After the user's yes, run them through the source-replica admin connection, or the user runs them if there is none.
3. **GATE: the user confirms the source-replica schema is loaded as the page describes and the connection works.**

## Start the source-replica importer before cutover

Start `import data to source-replica` in its own background shell, with its own held-open input pipe and log (SKILL.md, "Running Voyager without a terminal"), the same export directory and the password in the environment, at the point the live-fall-forward page gives. Take the exact command from the page and ask before its first run. Record its PID.

Include it in every report poll (the report has rows for the source-replica; its imported counts must follow the target's) and in the liveness check before cutover. Restart it with the same command if it dies.

## Cutover to the target

Follow the live phase, including the step that tells the user cutover is possible. Use the cutover command and options the cutover docs page gives for fall-forward. If the CLI rejects an option, follow its message.

After cutover, read the live-fall-forward page for which processes run now. Re-identify the running Voyager processes and update their PIDs and roles in the progress file. If one dies, its log usually records the command it ran; re-run that same command with the same export directory and the passwords in the environment.

`cutover status` shows a direction for the target and one for the source-replica. Poll the source-to-target direction to completed, then finalize, validate and switch the application as in the live phase. Then confirm with `get data-migration-report` that the source-replica's counts move when the application writes to the target.

The live phase's disk and slot guidance applies to this stream too.

## Decision point: stay or switch to the source-replica

Changes keep flowing to the source-replica until the migration ends. Ask the user:

1. **Stay on the target.** Go to end migration. Ending stops the stream to the source-replica, after which there is no switch.
2. **Switch to the source-replica.**
   1. **GATE: the user confirms application writes to YugabyteDB are stopped and approves the exact `initiate cutover to source-replica` command.**
   2. Check every process is alive, then run `yb-voyager initiate cutover to source-replica` with the options the cutover docs page gives. Pass `--yes` only after the user's go.
   3. Poll `cutover status` until the source-replica direction reports completed.
   4. On the source-replica, create what was left out before cutover and re-enable what was disabled, per the page. **GATE: the user approves these statements.** Run them through the source-replica admin connection, or the user runs them if there is none.
   5. Verify row counts on the source-replica, switch the application, then go to end migration.

Record in the progress file: each cutover command and when it completed, the decision, and the row-count result.
