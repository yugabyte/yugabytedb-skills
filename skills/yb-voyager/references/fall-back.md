# Live migration with fall-back

Read this when the user chose live migration with fall-back (target changes stream back to the source after cutover). It adds to the live reference; follow that file for everything not covered here.

## Docs to fetch

- https://docs.yugabyte.com/stable/yugabyte-voyager/migrate/live-fall-back/ for the commands, source and target prep, version-specific steps and limitations.
- https://docs.yugabyte.com/stable/yugabyte-voyager/reference/yb-voyager-pg-grant-migration-permissions/ for the fall-back grant variant.
- https://docs.yugabyte.com/stable/yugabyte-voyager/reference/cutover-archive/cutover/ for the cutover options and the connector choices.

## Plan-time decisions

1. Choose fall-back before the grant script runs: the fall-back variant grants the write access the reverse stream needs. If the user ran the plain live variant and now wants fall-back, they re-run the script with the fall-back variant before cutover.
2. The user runs the grant script (it may ask questions and needs a privileged account). Print the command and the effects the page lists, then wait. **GATE: the user approves running the fall-back grant variant.**
3. Read the cutover reference for the connector used to read changes from YugabyteDB after cutover and its version requirements. If the target does not meet them, present the alternatives and their limitations from the docs; the user decides.
4. Cross-check column types against the connector's limitation list on the live-fall-back page.
5. Record the flow, grant variant and connector choice in the progress file.

## Before cutover: version-specific source steps

Run `SHOW server_version_num` on the source and read the live-fall-back page for steps that depend on the PostgreSQL version (for example how foreign keys and triggers are handled while changes are applied back to the source).

- If the page offers options, present each with its effect on the source and let the user pick.
- **GATE: the user approves the chosen option and runs it themselves (admin credentials).**
- Record anything removed or disabled on the source, so it is restored after fall-back.

## Cutover to the target

Follow the live reference, with the cutover command and fall-back option the cutover reference gives for this flow, plus the connector option only if the user chose it. Set every password variable the flow uses.

After cutover, the processes take reverse roles: changes now flow from YugabyteDB to the source. Read the live-fall-back page for which commands run now. Re-identify the running Voyager processes (`ps`, the logs) and update the PIDs and their roles in the progress file. If one dies, its log usually records the command it ran; re-run that same command with the same export directory and the passwords in the environment.

Read the cutover reference for what a completed source-to-target cutover means for this flow. Then confirm with `get data-migration-report` that reverse counts move when the application writes to the target.

Then finalize, validate and switch the application as in the live reference. The live reference's disk and slot guidance applies to the reverse stream too. Prefer an archiving policy that keeps files unless the docs say deleting is safe for the reverse stream.

## Decision point: stay or fall back

Reverse streaming keeps running until the migration ends. Ask the user:

1. **Stay on the target.** Go to end migration. Ending stops the reverse stream, after which there is no fall-back.
2. **Fall back to the source.**
   1. **GATE: the user confirms application writes to YugabyteDB are stopped and approves `initiate cutover to source`.**
   2. Check every reverse process is alive, then run `yb-voyager initiate cutover to source` with the options the cutover reference gives. Pass `--yes` only after the user's go. Do not add the iterative-cutover option; this skill does not orchestrate iterations.
   3. Poll `cutover status` until the target-to-source direction reports completed.
   4. Restore on the source whatever the version-specific step removed, using the live-fall-back page. The user runs the statements. **GATE: the user approves the restore statements.**
   5. Verify row counts on the source, switch the application back, then go to end migration.

## Iterative cutover

Repeated rounds of fall-back and cutover are documented at https://docs.yugabyte.com/stable/yugabyte-voyager/reference/iterative-cutover/. This skill does not orchestrate them; point the user there.
