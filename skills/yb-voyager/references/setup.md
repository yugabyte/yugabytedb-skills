# Setup: install, export directory, assessment config, assessment grants

Read this at the start of a migration, before any `yb-voyager` command that touches a database. It ends at a working install, an export directory, an assessment config and a source user that can be assessed.

Docs (fetch each page for the exact commands, keys and values; this file only adds order and gates):

- Install, dependencies, upgrade, Docker: https://docs.yugabyte.com/stable/yugabyte-voyager/install-yb-voyager/
- Export directory, source user, config file step: https://docs.yugabyte.com/stable/yugabyte-voyager/migrate/migrate-steps/
- Config keys, precedence, password environment variables: https://docs.yugabyte.com/stable/yugabyte-voyager/reference/configuration-file/
- Grant script: https://docs.yugabyte.com/stable/yugabyte-voyager/reference/yb-voyager-pg-grant-migration-permissions/
- Release notes: https://docs.yugabyte.com/stable/yugabyte-voyager/release-notes/

## 1. Install and version check

1. Run `yb-voyager version`. If the command is missing, send the user to the install page for their OS. The user runs any step that needs `sudo`.
2. Fetch the release notes and compare the newest version with the installed one. The docs describe the latest release, so an older install can disagree with them.
3. If the install is older, ask the user whether to upgrade now, before the first migration command. Never upgrade mid-migration.
4. If the export directory already holds a migration, do not upgrade. If the installed version refuses to continue it, the choices are the version that started it or a new export directory. Both are the user's call.
5. Record the version in the progress file and re-check it at the start of each phase.

## 2. Dependencies

1. Read the install page's dependency list for this OS and check each tool with its version command. A client tool older than the page requires is a failure even when the source server is older.
2. If the install page says to source an environment file or update `PATH`, do it in every new shell: each Bash call you make starts a fresh one. Verify each tool resolves with `which`.
3. Package installs need `sudo` and a network. Hand those commands to the user.

## 3. Docker

If the user runs Voyager in Docker, read the install page's Docker section for how config files, flags and host paths reach the container. Mount every host path the config references. If the config file cannot be used that way, pass full CLI flags on every command and note it in the progress file.

## 4. Export directory

1. Ask the user for an absolute path, one per migration, outside any git repository. Check free space with `df` against the sizing guidance on the install page.
2. Create it with `mkdir -p`. Assessment and the later flow config share it.
3. Never reuse it for another source database and never `rm -rf` it to start over. The supported cleanup is `end migration`.
4. Record the path in the progress file.

## 5. Assessment config

The assessment runs before the flow is chosen, so start with a small config for it alone, next to the export directory. Take key names from the configuration-file page or from the template shipped with the install; hold only:

- the export directory;
- the source connection (type, host, port, database, schema list, migration user), with no password;
- the assessment section, with the user's real target YugabyteDB version set explicitly. The default assesses against the newest release, which hides issues on an older cluster.

No target section yet: assessment does not connect to the target. If the source has read replicas, ask the user whether to assess them and set the matching option (configuration-file page or `--help`), so the run does not stop to ask.

## 6. Passwords

1. Ask the user to export the source password variable named on the configuration-file page, in the shell that runs `yb-voyager`. Target and replica passwords follow in later phases.
2. Check it is set without printing it: `test -n "$VAR" && echo set`.
3. If it is not set in your shell, the user starts the session with it exported or runs the command themselves.
4. Reuse it for `psql` checks through `PGPASSWORD="$VAR"`, which keeps it out of the argument list.

## 7. Assessment-level grants

Assessment needs the offline (non-live) grant level only. The live variants change production tables, so they wait until the flow is chosen.

1. Create the migration user first if it does not exist (migrate-steps page, "Prepare the source database"). The user runs it.
2. Fetch the grant-script page and build the offline-level command for the user's database, user and schema list. Print it; the user runs it with admin credentials.
3. Tell the user the effects the page lists for that level, then wait.

## 8. Read-only checks

Run as the migration user through `psql -Atc`:

| Check | Query |
|---|---|
| Connectivity | `SELECT 1` |
| Server version | `SHOW server_version`, compared with the supported versions on the introduction page |
| Schema access | a `SELECT` from one table per schema in the list |

Do not turn off Voyager's own permission checks: the first database command reports missing grants, and that report is how you confirm the grants worked.

**GATE: the user confirms install, export directory, assessment config and grants are done before the first database command.**
