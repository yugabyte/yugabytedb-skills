# Live migration with fall-back

Read this when the user chose live migration with fall-back (target changes stream back to the source after cutover). It adds to the live reference; follow that file for everything not covered here.

## Docs to fetch

- https://docs.yugabyte.com/stable/yugabyte-voyager/migrate/live-fall-back/ for the commands, source and target prep, version-specific steps and limitations.
- https://docs.yugabyte.com/stable/yugabyte-voyager/reference/yb-voyager-pg-grant-migration-permissions/ for the fall-back grant variant.
- https://docs.yugabyte.com/stable/yugabyte-voyager/reference/cutover-archive/cutover/ for the cutover options and the connector choices.

## Plan-time decisions

1. Choose fall-back before the grant script runs: the fall-back variant grants the write access the reverse stream needs. If the user ran the plain live variant and now wants fall-back, they re-run the script with the fall-back variant before cutover.
2. Build the fall-back grant variant's command and its answers as `assess-and-plan.md` §8 step 3 describes. Show the command and the effects the page lists. **GATE: the user approves running the fall-back grant variant.** Then run it through the admin connection, or the user runs it if there is none.
3. Read the cutover reference for the connector used to read changes from YugabyteDB after cutover and its version requirements. If the target does not meet them, present the alternatives and their limitations from the docs; the user decides.
4. Cross-check column types against the connector's limitation list on the live-fall-back page.
5. Check that the reverse stream can reach YugabyteDB the way its connector connects. The default connector reconnects to the addresses `SELECT host, port FROM yb_servers()` returns, not the address in the config. From the Voyager host, run that query and test a connection to one returned `host:port`. If it is unreachable (a container hostname, a private IP behind NAT, an internal port), the reverse exporter dies at cutover with no node available. The user fixes the advertised address before cutover.
6. Settle prompts that appear only at cutover. The reverse processes start automatically after the forward stream has stopped, so a prompt there (for example a warning that the bundled connector is older than the target's YugabyteDB release, asking to continue) hangs or kills a background run at the worst moment. Read the cutover page and the release notes for such warnings for the installed version and target release, tell the user, and record their answer. If one appears anyway, the reverse exporter has died. Show the user the prompt, and re-run that same command with their answer.
7. Record the flow, grant variant, connector choice and the answers from steps 5–6 in the progress file.

## Before cutover: version-specific source steps

Run `SHOW server_version_num` on the source and read the live-fall-back page for steps that depend on the PostgreSQL version (for example how foreign keys and triggers are handled while changes are applied back to the source).

- If the page offers options, present each with its effect on the source and let the user pick.
- **GATE: the user approves the chosen option's exact statements.** Run them through the admin connection, or the user runs them if there is none.
- Record anything removed or disabled on the source, so it is restored after fall-back.

## Cutover to the target

Follow the live reference, with the cutover command and fall-back option the cutover reference gives for this flow, plus the connector option only if the user chose it. Set every password variable the flow uses.

After cutover, the processes take reverse roles: changes now flow from YugabyteDB to the source. Read the live-fall-back page for which commands run now. Re-identify the running Voyager processes (`ps`, the logs) and update the PIDs and their roles in the progress file. If one dies, its log usually records the command it ran; re-run that same command with the same export directory and the passwords in the environment.

Read the cutover reference for what a completed source-to-target cutover means for this flow. Then confirm with `get data-migration-report` that reverse counts move when the application writes to the target.

Then finalize, validate and switch the application as in the live reference. The live reference's disk and slot guidance applies to the reverse stream too. Prefer an archiving policy that keeps files unless the docs say deleting is safe for the reverse stream.

## Decision point: stay or fall back

Reverse streaming keeps running until the migration ends. Ask the user:

1. **Stay on the target.** Go to end migration. Ending stops the reverse stream, after which there is no fall-back.
2. **Fall back to the source.** First ask whether this fall-back is final or the start of another round (next section), because that choice is made on the cutover command itself.
   1. **GATE: the user confirms application writes to YugabyteDB are stopped and approves the exact `initiate cutover to source` command, including whether it starts another round.**
   2. Check every reverse process is alive, then run `yb-voyager initiate cutover to source` with the options the cutover reference gives, plus the restart option only if the user chose another round. Pass `--yes` only after the user's go.
   3. Poll `cutover status` until the target-to-source direction reports completed.
   4. Restore on the source whatever the version-specific step removed, using the live-fall-back page. **GATE: the user approves the restore statements.** Run them through the admin connection, or the user runs them if there is none.
   5. Verify row counts on the source and switch the application back. Then go to end migration if the fall-back was final, or continue with the next section.

## Iterative cutover (repeated rounds)

Docs: https://docs.yugabyte.com/stable/yugabyte-voyager/reference/iterative-cutover/ for the commands, the options and how `end migration` closes the rounds. Check `initiate cutover to source --help` for the restart option; if the installed version lacks it, iterative cutover is not available there, so tell the user.

With iterative cutover, a fall-back starts a new round instead of ending the migration. After the cutover to source completes, Voyager streams the source's changes to YugabyteDB again (changes only, no new snapshot), so the user can fix what went wrong and cut over to YugabyteDB again later. Round 0 is the original migration; each fall-back with the restart option starts the next round.

1. **Choose at the fall-back gate.** Explain both options before the cutover-to-source command:
   - *Final fall-back* (no restart option): the migration ends on the source; going to YugabyteDB again means a new migration from the start.
   - *Another round* (restart option set): the source-to-target stream continues and the user must later cut over again or end the migration. The source keeps a replication slot and Voyager keeps running for as long as the round lasts (live reference §6 on disk and WAL applies).

   Put the choice on the command line, with the restart option set explicitly to true or false, so the command the user approves shows it. Leave it out of the config file: a config value decides for every later fall-back without the user seeing it, and some flow templates ship with the option already set.
2. **After the cutover to source completes, the next round starts by itself.** Do not start export or import by hand. Re-identify the running Voyager processes (`ps`, the logs), check each is alive, and record them in the progress file under the round number, with their roles. If no forward process started, read the logs and `cutover status` before doing anything; never start a new migration in the same export directory.
3. **Check the round kept the user's settings.** Compare the options the new round's processes show in their logs or command lines with those recorded for round 0 (table lists, import options such as partitioning or parallelism). Past releases dropped some import options in later rounds. If one is missing, stop and tell the user; restart a process to fix it only with their approval.
4. **Run the round like the live phase.** Poll `get data-migration-report` (with the per-round breakdown if `--help` offers it) and use the live reference's polling, liveness, stuck and restart rules. The schema freeze still holds: if the fix for what caused the fall-back is a schema change, tell the user that streaming does not carry DDL and read the docs before changing the schema on either side.
5. **Cut over to YugabyteDB again.** Follow the live reference §3 steps 6 to 12 and its §7 pre-checks. Ask whether this round should keep fall-back available:
   - *Keep it*: cut over with the fall-back option again. Repeat this file from "Before cutover: version-specific source steps", then the decision point, for this round.
   - *Drop it*: this is the last cutover; after it there is no way back to the source.
   **GATE: the user approves the exact `initiate cutover to target` command, with their fall-back choice for this round stated.**
6. **End.** `end migration` closes every round. Before its gate, read the end-migration page for how it handles rounds and tell the user what it will clean up.

Record in the progress file, per round: its number, each cutover command and when it completed, the fall-back choice, and the row-count result.
