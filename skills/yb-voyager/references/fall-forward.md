# Live migration with fall-forward

Read this when the user chose live migration with fall-forward (a second PostgreSQL, the source-replica, is fed from the target after cutover). It adds to the shared live reference; follow that file's steps and gates for everything not covered here.

## Docs to fetch

- https://docs.yugabyte.com/stable/yugabyte-voyager/migrate/live-fall-forward/ for source-replica preparation, the `import data to source-replica` command and the cutover steps.
- https://docs.yugabyte.com/stable/yugabyte-voyager/reference/data-migration/import-data/ for the `import data to source-replica` flags.
- https://docs.yugabyte.com/stable/yugabyte-voyager/reference/cutover-archive/cutover/ for the cutover flags.

## Plan-time decisions

1. The source grant variant is the plain live one (`is_live_migration=1`, `is_live_migration_fall_back=0`); fall-forward does not write back to the source.
2. Ask the user for the source-replica connection details and record them (not the password) in the progress file. Export `SOURCE_REPLICA_DB_PASSWORD`.
3. As with fall-back, the target is read through CDC after cutover. Cross-check column types against the connector limitation list on the live-fall-forward page, and check the target version against the connector minimum on the cutover reference page.

## Prepare the source-replica

The user prepares the source-replica; this is a separate PostgreSQL the agent does not administer.

1. Create a superuser `ybvoyager_ff` on the source-replica, as the docs show.
2. Create the schema on the source-replica without indexes and triggers: they slow the bulk apply and are added after cutover. Use the docs' export/import schema steps for the source-replica. The agent prints the commands and effects; the user runs the ones that need admin credentials.
3. **GATE: the user confirms the source-replica schema is loaded without indexes or triggers and the connection works.**
4. **Docs gap:** the docs show a `source-replica.db-type` key in the config snippet, but it is not an allowed key; leave it out, or the config file is rejected.

## Start the source-replica importer before cutover

`import data to source-replica` is not started automatically. Start it by hand in its own background shell, with its own log and the same export-dir, while the exporter and importer are running (live reference step 4). It applies the snapshot and then the change events to the source-replica. Take the exact command and flags from the live-fall-forward page, with `SOURCE_REPLICA_DB_PASSWORD` in the environment. Record its PID in the progress file.

Check it as part of every report poll: the report has a `source-replica` row per table, and its `imported_*` counts must follow the target's. Include it in the liveness check before cutover. Restart it with the same command if it dies.

## Cutover to the target

Follow steps 1-9 of the live reference, using:

```
yb-voyager initiate cutover to target --export-dir <dir>
```

Do not pass `--prepare-for-fall-back true`: the CLI rejects it once fall-forward is enabled. Passing `false` is also unnecessary here, since the flag is mandatory only for non-fall-forward migrations.

At cutover, the importer's PID switches to `export data from target` by exec (same PID and log). The source-replica importer keeps running and is not restarted. Update the progress file's PID labels. If the target exporter dies, re-run the command printed in its log (`Starting export data from target with command:`).

`cutover status` shows `source → target` and `target → source-replica` rows. Poll the first to `COMPLETED`, then run finalize and validate as in the live reference.

## Optional: cutover to the source-replica

1. Ask the user whether to move the application to the source-replica.
2. **GATE: the user confirms YugabyteDB application writes are stopped and approves `initiate cutover to source-replica`.**
3. Check the target exporter and the source-replica importer are alive, then run `yb-voyager initiate cutover to source-replica --export-dir <dir>`. Pass the hidden `--yes` only after the user's go.
4. Poll `cutover status` until the `target → source-replica` row is `COMPLETED`; both processes exit.
5. On the source-replica, create the indexes, triggers and constraints that were left out, and re-enable foreign keys and check constraints. The docs give the steps. **GATE: the user approves these statements and runs them with admin credentials.**
6. Verify row counts on the source-replica, switch the application, then go to the end-migration step in SKILL.md.
