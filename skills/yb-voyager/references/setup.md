# Setup: install, export directory, stage-1 config, assessment grants

Read this at the start of a migration, before any `yb-voyager` command that touches a database. It ends at a working install, an export directory, a stage-1 config and a source user that can be assessed.

Docs this file relies on (fetch the page for the exact commands; this file only adds orchestration):

- Install, dependencies, upgrade, Docker: https://docs.yugabyte.com/stable/yugabyte-voyager/install-yb-voyager/
- Export directory, source user, config file step: https://docs.yugabyte.com/stable/yugabyte-voyager/migrate/migrate-steps/
- Config keys, precedence, password env vars: https://docs.yugabyte.com/stable/yugabyte-voyager/reference/configuration-file/
- Grant script: https://docs.yugabyte.com/stable/yugabyte-voyager/reference/yb-voyager-pg-grant-migration-permissions/
- Release notes (latest version, breaking changes): https://docs.yugabyte.com/stable/yugabyte-voyager/release-notes/

## 1. Install and version check

1. Run `yb-voyager version` and read `VERSION=`. If the command is missing, send the user to the install page for their OS. The user runs the `sudo` steps; do not run them yourself.
2. Fetch the release-notes page and read the newest version heading. The docs describe only the latest stable release, so an older install means the docs and the CLI can disagree.
3. If the install is older than the latest, ask the user whether to upgrade. Upgrade now, before the first migration command. Never upgrade mid-migration: a release marked "Breaking change" in the release notes refuses to continue export directories created by older versions.
4. If the export directory already holds a migration (a `metainfo/` directory exists), do not upgrade. Run the next command on the installed version; it prints a note on a version mismatch and refuses when the directory predates a breaking change. When it refuses, the choices are to continue with the version that started the migration or to start a new export directory. Both are the user's call.
5. Record `VERSION` in the progress file. Re-run `yb-voyager version` at the start of each phase and stop if it changed.
6. Verify: `yb-voyager version` prints `VERSION=` and the value matches the progress file.

Use `yb-voyager <command> --help` for any flag whose existence depends on the version (for example `--skip-pre-checks`, JSON output flags). Do not assume a flag from a docs page.

## 2. Dependencies and shell traps

- The install page lists the required Java and PostgreSQL client tools; check each with `java -version` and `pg_dump --version`. A client older than the page requires is a failure even when the source server is older, because the client tool is what runs.
- A source-built install writes `~/.yb-voyager.rc`. Source it in every new shell; each Bash call you make starts a fresh shell.
- On macOS, after `brew install` of the PostgreSQL client, the new binaries are not on `PATH` until the user updates it and restarts the terminal. Verify with `which pg_dump`.
- Package installs need `sudo` and a network. Hand the commands to the user.

## 3. Docker caveat

The Docker wrapper script (Method 1 on the install page) cannot use a config file and drops shorthand flags such as `-e`. This skill drives everything through a config file, so with Docker pick one:

- Method 2 on the install page: a container with the export directory and the config file mounted. Mount every host path the config references (SSL certificates, the export directory).
- Full CLI flags on every command, no config file. Say so in the progress file; every later reference assumes `--config-file`.

**Docs gap:** the configuration-file page tells every user to pass `--config-file` and never mentions that the Docker wrapper ignores it.

## 4. Export directory

1. Ask the user for an absolute path, one per migration, outside any git repository. Check free space with `df`; it must hold at least the source database size (see the install page for the exact ratio).
2. Create it with `mkdir -p`. Stage 1 (assessment) and stage 2 (the flow's config) deliberately share it, because later commands read the assessment output from it.
3. Never reuse a directory for another source database and never `rm -rf` one to start over. It holds the migration state; the supported cleanup is `end migration`, which is gated in SKILL.md.
4. Record the path in the progress file.

## 5. Stage-1 config

Stage 1 exists to run `assess-migration` before the flow is chosen. Write only these keys, in one file next to the export directory (for example `<export-dir>/../<name>-stage1.yaml`):

```yaml
export-dir: /abs/path/to/export-dir
source:
  db-type: postgresql
  db-host: <host>
  db-port: 5432
  db-name: <database>
  db-schema: <schema1,schema2>
  db-user: ybvoyager
assess-migration:
  target-db-version: <A.B.C.D>
```

Why each choice:

- No `target:` block. `assess-migration` never connects to the target, so the target is not needed yet.
- `assess-migration.target-db-version`: ask the user for their real target YugabyteDB version. Without it the command stops on a Y/N prompt and hangs a background run. The default assesses against the newest release, which hides issues that exist on an older cluster.
- No `db-password`. Passwords come only from environment variables (next section).
- Replica prompt: with a PostgreSQL primary and neither flag, Voyager discovers replicas and prompts. `primary-only` is a CLI flag only (the config validator rejects it), so pass `--primary-only` on the command line, or set `source.read-replica-endpoints` when the user wants replicas assessed.
- Config validation is strict: an unknown key, section or misplaced key is a hard error. `yes` and `start-clean` are CLI flags, not config keys.
- `assess-migration` suppresses the "Using config file" line, so its absence proves nothing. A rejected config fails the command with an "Invalid keys" or "Invalid sections" message.

**Docs gap:** the migrate-steps config snippet omits `assess-migration:` and shows `target-db-version` as optional with a default; without the key the run prompts.

## 6. Passwords

1. Ask the user to export `SOURCE_DB_PASSWORD` in the shell that runs `yb-voyager` (and `TARGET_DB_PASSWORD` / `SOURCE_REPLICA_DB_PASSWORD` when those phases arrive). A missing password turns into a terminal prompt, which hangs a background run.
2. Check each with `test -n "$SOURCE_DB_PASSWORD" && echo set`. Never print the value, put it in a config file or a command line, or ask for it in chat.
3. If the variable is not set in your shell, the user must start the session with it exported, or run the command themselves. Do not work around it.
4. Use the same variable for `psql` checks: `PGPASSWORD="$SOURCE_DB_PASSWORD" psql ...` keeps the value out of the argument list.

## 7. Assessment-level grants

Assessment needs the offline grant level only. Do not run the live variants yet: they change the production source (the flow-specific prep table in the planning phase, see SKILL.md).

1. Ask the user for the source database user that will run the migration (`ybvoyager`) and the schema list. Use the same schema list in `source.db-schema`.
2. Print the user's version of the command below for them to run with admin credentials. The user runs it; you do not hold the admin password.

```bash
psql -h <host> -d <source-database> -U <admin-user> \
  -v voyager_user='ybvoyager' -v schema_list='<schema1,schema2>' \
  -v is_live_migration=0 -v is_live_migration_fall_back=0 \
  -f /opt/yb-voyager/guardrails-scripts/yb-voyager-pg-grant-migration-permissions.sql
```

3. Effects to state to the user: the offline level grants read access only and does not change table ownership, replica identity or `wal_level`. On RDS the script may print permission-denied notices for some catalog tables; the docs say to ignore them.
4. Create the `ybvoyager` role first if it does not exist (docs: "Prepare the source database"). The user runs that too.
5. Verify with the read-only checks below.

**Docs gap:** the grant-script page calls `<database>` the "target YugabyteDB database"; `-d` is the source PostgreSQL database.

## 8. Read-only verification queries

Run as `ybvoyager` through `PGPASSWORD="$SOURCE_DB_PASSWORD" psql -h <host> -U ybvoyager -d <db> -Atc "<query>"`:

| Check | Query | Expect |
|---|---|---|
| Connectivity | `SELECT 1` | `1` |
| Server version | `SHOW server_version` | a supported major version (introduction page) |
| Logical decoding (live flows) | `SHOW wal_level` | `logical`; anything else means the user's restart step is pending |
| Replication role (live flows) | `SELECT rolreplication FROM pg_roles WHERE rolname='ybvoyager'` | `t` (on RDS check membership of `rds_replication` instead) |
| Replica identity (live flows) | `SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind='r' AND n.nspname = ANY(string_to_array('<schemas>',',')) AND c.relreplident <> 'f'` | zero rows after the live grant |

The docs give no "did the grants work" step. A first `assess-migration` run also checks permissions (guardrails) and prints which are missing; leave `run-guardrails-checks` at its default so it keeps doing that.

**GATE: the user confirms install, export directory, stage-1 config and assessment grants are done before the first database command.**
