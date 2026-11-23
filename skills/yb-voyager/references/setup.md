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
6. Check how your shell runs Voyager (SKILL.md, "Running Voyager without a terminal"):
   - `command -v mkfifo` finds it;
   - a background process outlives the command that started it. Run `nohup sleep 300 >/dev/null 2>&1 & echo $!`, then in a separate later command `kill -0 <PID> && echo alive`, then `kill <PID>`.

   Record both results in the progress file. If either fails, use the fallback in that section.

## 2. Dependencies

1. Read the install page's dependency list for this OS and check each tool with its version command. A client tool older than the page requires is a failure even when the source server is older.
2. If the install page says to source an environment file or update `PATH`, do it in every new shell: each Bash call you make starts a fresh one. Verify each tool resolves with `which`.
3. Package installs need `sudo` and a network. Hand those commands to the user.

## 3. Docker

If the user runs Voyager in Docker, read the install page's Docker section for how config files, flags and host paths reach the container. Mount every host path the config references. If the config file cannot be used that way, pass full CLI flags on every command and note it in the progress file.

The input pipe reaches Voyager only if the container gets standard input. Use `docker run -i` without `-t`: with `-t`, Docker refuses a pipe with "the input device is not a TTY". If the install's wrapper script passes `-it`, tell the user and use a copy of it with `-i` only.

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

Show the user the file's path and contents, and ask them to check the source connection, schema list and target version before assessment uses it.

## 6. Passwords

Each of your commands runs in a fresh shell, so an `export` the user types during the session does not reach later commands. Ask the user which way your shell gets the source password variable named on the configuration-file page:

- **From the environment:** they export it, then start this session from that shell.
- **From a file:** they put the `export` line in a file only they can read (`chmod 600`), outside the export directory, and give you its path. Start each command that needs a password with `. <file> &&` so it is set in that same shell. Never print or open the file.

Once the flow is chosen, the plan phase lists every password the flow needs, set the same way.

**Admin connection (optional).** Prep, grants and clean-up need a privileged account. Ask the user whether you should run these SQL steps yourself, after their approval of each one. If yes, they give you the host, port and admin user, with the password in its own variable, provided either way above. Use it only for approved admin steps, through `PGPASSWORD="$VAR" psql`. Suggest a separate admin role for the migration that they drop at the end. If they say no, print each admin statement for them to run. Record the choice (never the password) in the progress file.

1. Check it is set without printing it: `test -n "$VAR" && echo set`. Load the file first in the same command if that is the route.
2. Reuse it for `psql` checks through `PGPASSWORD="$VAR"`, which keeps it out of the argument list.
3. If neither way works for the user, they run the Voyager commands themselves, and you guide them from the output they paste (never a password).

## 7. Assessment-level grants

Assessment needs the offline (non-live) grant level only. The live variants change production tables, so they wait until the flow is chosen.

1. Create the migration user first if it does not exist (migrate-steps page, "Prepare the source database"). The user chooses its password and sets it in the source password variable first, never in chat. Create the user through the admin connection with psql reading the password from the environment, so it is on no command line:
   ```
   PGPASSWORD="$<admin variable>" psql -h <host> -p <port> -U <admin user> -d <database> <<'SQL'
   \getenv pw <source password variable>
   CREATE ROLE <migration user> LOGIN PASSWORD :'pw';
   SQL
   ```
   `\getenv` needs psql 15 or newer.
2. Fetch the grant-script page and build the offline-level command for the user's database, user and schema list.
3. Show both commands and the effects the page lists for that level. After the user's yes, run them through the admin connection, or print them for the user if there is none.

## 8. Read-only checks

Run as the migration user through `psql -Atc`:

| Check | Query |
|---|---|
| Connectivity | `SELECT 1` |
| Server version | `SHOW server_version`, compared with the supported versions on the introduction page |
| Schema access | a `SELECT` from one table per schema in the list |

Do not turn off Voyager's own permission checks: the first database command reports missing grants, and that report is how you confirm the grants worked.

**GATE: the user confirms install, export directory, assessment config and grants are done before the first database command.**
