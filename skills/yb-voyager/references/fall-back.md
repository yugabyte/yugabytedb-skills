# Live migration with fall-back

Read this when the user chose live migration with fall-back (target changes stream back to the source after cutover). It adds to the shared live reference; follow that file's steps and gates for everything not covered here.

## Docs to fetch

- https://docs.yugabyte.com/stable/yugabyte-voyager/migrate/live-fall-back/ for the commands, target preparation, and the trigger/FK PL/pgSQL for the PG<15 branch.
- https://docs.yugabyte.com/stable/yugabyte-voyager/reference/yb-voyager-pg-grant-migration-permissions/ for the grant script.
- https://docs.yugabyte.com/stable/yugabyte-voyager/reference/cutover-archive/cutover/ for the cutover flags and the YugabyteDB Connector vs gRPC connector choice.

## Plan-time decisions

1. Choose fall-back before the grant script runs. The fall-back variant (`-v is_live_migration=1 -v is_live_migration_fall_back=1`) adds INSERT/UPDATE/DELETE grants that the reverse stream needs, and the CLI first learns about fall-back only at cutover. **Docs gap:** no page says the choice must come first. If the user ran the plain live variant and later wants fall-back, they must re-run the script with the fall-back variant before cutover.
2. The user runs the grant script: it is interactive (ownership strategy prompt) and needs a privileged account. Print the command and its effects (REPLICA IDENTITY FULL on the listed schemas, a REPLICATION role, CREATE on the database), then wait. **GATE: the user approves running the fall-back grant variant.**
3. Check the target version. The default YugabyteDB Connector for `export data from target` needs a stable release at or above the minimum named on the cutover reference page; preview releases are rejected. If the target does not qualify, `--use-yb-grpc-connector true` is the alternative, and the user must decide whether the gRPC connector's limitations and port access are acceptable. It is not supported on Aeon.
4. Cross-check column types against the connector-specific limitation list on the live-fall-back page, because after cutover the changes are read from YugabyteDB.
5. Record the flow, grant variant and connector choice in the progress file.

## Before cutover: PG<15 branch

Run `SHOW server_version_num` on the source and tell the user which branch applies.

- PG 15 or newer: the live-fall-back page's PG<15 steps do not apply. The docs say nothing more, so tell the user that and proceed.
- PG below 15: the reverse stream needs foreign keys and triggers out of the way. Offer the two options from the docs and let the user pick:
  - Make the voyager user a superuser so `session_replication_role` can be set per connection. Least invasive, needs a privileged account.
  - Disable triggers and drop all foreign keys on the source schemas with the docs' PL/pgSQL. Destructive on the source.
- **GATE: the user approves the chosen option and runs it themselves (admin credentials).**
- If they dropped the FKs, record in the progress file that FKs must be restored after fall-back.

## Cutover to the target

Follow steps 1-9 of the live reference, with these differences. The cutover command is:

```
yb-voyager initiate cutover to target --export-dir <dir> --prepare-for-fall-back true
```

Add `--use-yb-grpc-connector true` only if the user chose it. Export `SOURCE_DB_PASSWORD`: the drift check and the later reverse import need it.

After the command, the running processes switch roles by exec (the process replaces itself, so the PID, terminal and log file stay the same):
- the exporter's PID now runs `import data to source`;
- the importer's PID now runs `export data from target`.

**Docs gap:** the docs say the commands "transform" but not that the PID and log are reused. Update the progress file: the PID labelled exporter is now the source importer, and the PID labelled importer is now the target exporter. Watch the same two logs.

If either process dies after this point, read the end of its log: the line `Starting import data to source with command:` or `Starting export data from target with command:` holds the exact command to re-run. Re-run it with the same export-dir. The `import data to source` restart needs `SOURCE_DB_PASSWORD` in the environment; the exec inherited it, a fresh shell will not have it.

`cutover status` for the `source → target` row reaches `COMPLETED` only when the reverse stream (export data from target and import data to source) has started, not merely when the forward drain finishes. So `COMPLETED` means reverse streaming is up. Verify with `get data-migration-report` (with `SOURCE_DB_PASSWORD` and `TARGET_DB_PASSWORD`) that reverse counts begin to move when the application writes to the target.

Then run finalize as in the live reference, validate, and switch the application to the target.

**Docs gap:** the live-fall-back page omits the warning that a stopped exporter makes the source slot retain WAL; apply the disk guidance from the live reference to the reverse stream too. Archive changes remains optional; the docs do not say whether `--policy delete` is safe for the reverse stream, so prefer `archive` here.

## Decision point: stay or fall back

Reverse streaming keeps running until the migration ends. Ask the user to choose:

1. Stay on the target. Hand over to the end-migration step in SKILL.md. `end migration` stops the reverse streaming and removes the source-side state, after which there is no fall-back.
2. Fall back to the source.
   1. **GATE: the user confirms YugabyteDB application writes are stopped and approves `initiate cutover to source`.**
   2. Check both reverse PIDs are alive, then run `yb-voyager initiate cutover to source --export-dir <dir>`. Pass the hidden `--yes` only after the user's go. Do not add `--restart-data-migration-source-target`.
   3. Poll `cutover status` until the `target → source` row is `COMPLETED`; the reverse processes exit.
   4. Restore on the source what the PG<15 branch removed. If the FKs were dropped, they are recreated from the pure schema dump in `<export-dir>/temp/schema.sql`; re-enable the triggers. **Docs gap:** this pointer is buried inside a SQL comment on the page. The user runs these statements with admin credentials: print them and wait. **GATE: the user approves the FK/trigger restore statements.**
   5. Verify row counts on the source, switch the application back, then go to the end-migration step.

## Iterative cutover

Repeating fall-back and cutover in rounds exists for PostgreSQL fall-back migrations (https://docs.yugabyte.com/stable/yugabyte-voyager/reference/iterative-cutover/). This skill does not orchestrate it; point the user at that page.
