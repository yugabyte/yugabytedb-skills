---
name: yb-voyager
description: Guides a full PostgreSQL to YugabyteDB migration with YugabyteDB Voyager (yb-voyager), phase by phase - setup, assessment, choosing a migration flow (offline, live, live with fall-back, live with fall-forward), schema export and the analyze-schema fix loop, data export and import, cutover and end migration - stopping for the user's approval between phases and before every irreversible step. Use when a user wants to migrate a PostgreSQL database to YugabyteDB, is in the middle of a Voyager migration and wants to continue or recover one, or asks which Voyager migration type fits them. Triggers on yb-voyager, Voyager, assess-migration, export schema, analyze-schema, import schema, export data, import data, get data-migration-report, cutover, initiate cutover, fall-back, iterative cutover, fall-forward, source-replica, archive changes, end migration, export-dir, migration assessment report. Not for writing application SQL against YugabyteDB (ysql) or reviewing an existing YugabyteDB schema (yb-model).
---

# YugabyteDB Voyager migration orchestrator

You run a migration with the user, one phase at a time. This skill owns the order, the
decisions, the gates and the safety rules. It deliberately holds no release-specific facts:
no flag names beyond the core ones, no defaults, file paths, report fields, status values or
messages. Those change between Voyager releases, so take them from the sources below every time.

You run the commands yourself; the user decides. Read-only checks and status polls need no
approval: run them and report what you found. Ask first, in the approval format below, before:

- every command that changes the source, the target or Voyager's state (the approval list);
- the first run of each Voyager command that does work (assess, export or import schema,
  export or import data, archive, finalize), even when it changes nothing in the databases. A
  re-run of the same command needs no new approval.

After writing or changing a config file, show its path and contents (never a password). Ask the
user to check the values that matter (target version, schema and table lists, export directory)
before a command uses it. Tell the user each command you start, with its PID and log. The user does only what you cannot: host restarts, managed-service and cluster settings,
`sudo`, and admin SQL when they have not given you an admin connection.

## Where facts come from

1. **What the command prints and the files it writes.** Read the output, the log and every
   report a command says it wrote. This is the ground truth for the installed version.
2. **`yb-voyager <command> --help`** for flags, accepted values and defaults.
3. **The docs page for the step**, fetched as Markdown by appending `.md` to the page URL (for
   example `https://docs.yugabyte.com/stable/yugabyte-voyager/migrate/live-migrate.md`). Each
   reference file names the pages for its phase. The docs describe the latest release.
4. **A YugabyteDB docs search tool, if connected** (the Kapa MCP server at
   `https://yugabyte.mcp.kapa.ai`) for error messages and "is X supported" questions. Confirm
   any flag it suggests with `--help`.

When the docs and the CLI disagree, the CLI wins: follow its message or `--help`, and tell the
user the docs differ. Never fill a gap from memory.

## Scope

- **PostgreSQL sources.** For any other source, check the
  [introduction](https://docs.yugabyte.com/stable/yugabyte-voyager/introduction/) for current
  support, tell the user, and do not orchestrate.
- **Bulk load from files** is a separate workflow: link
  [bulk data load](https://docs.yugabyte.com/stable/yugabyte-voyager/migrate/bulk-data-load/)
  and stop.

## Phases and gates

| # | Phase | Read | Ends with |
|---|---|---|---|
| 1 | Setup: install and version check, export dir, assessment config, passwords, assessment grants | `references/setup.md` | Voyager verified, source reachable as the migration user |
| 2 | Assess: run `assess-migration`, present the report | `references/assess-and-plan.md` | **GATE**: user has seen the findings and sizing |
| 3 | Plan: choose the flow, write the flow's config, prepare source and target | `references/assess-and-plan.md` | **GATE**: flow chosen, prep done and verified, target DB created |
| 4 | Schema: export, analyze-schema fix loop, import | `references/schema.md` | **GATE**: clean analysis, schema imported and verified |
| 5a | Data, offline flow | `references/data-offline.md` | **GATE**: data imported, finalized, row counts match |
| 5b | Data, live flows, up to cutover to target | `references/data-live.md` | **GATE** before every cutover |
| 5c | Live with fall-back: reverse sync, optional cutover to source, optional further rounds (iterative cutover) | `references/fall-back.md` | **GATE**: stay on YugabyteDB, fall back for good, or fall back and start another round |
| 5d | Live with fall-forward: source-replica, optional cutover to it | `references/fall-forward.md` | **GATE**: stay, or switch to the replica |
| 6 | End migration | `references/end-migration.md` | **GATE**: backups chosen, user confirms the end |

Read only the reference file for the current phase, plus the flow file the user chose.

`references/field-notes.md` holds what past migrations ran into: rules of thumb (for example
the change rates live migration has handled), schema traits that cause trouble, and a
symptom-to-cause table. Read it during planning, before a live data phase, and whenever a run
is slow, stuck or failing in a way its output does not explain. Present what it says as field
experience, not as documented limits.

**At every gate**, post this and wait for an explicit yes. It is shown in a code block here;
post it as rendered Markdown, not inside a code block, so it wraps. Keep each bullet to one
short line and the whole gate to one screen. Put long lists (issues, per-table counts) in a
table after it, or in a file you link.

```markdown
### Phase <n> done: <phase name>

- **Ran:** <commands, briefly>
- **Result:** <outcome in one line>
- **Evidence:** <report path, counts or status as Voyager printed them>
- **Decisions recorded:** <one per bullet, or "none">

**Next:** <phase n+1 and its first command>

**Needs from you:**
1. <an approval, information, or a step only the user can do>

Continue?
```

Silence, or a reply about something else, is not a yes.

## Progress file

Keep `<export-dir>/agent-progress.md`, and update it after every command that changes state. It
lets a new session pick up where the last one stopped:

- Voyager version recorded at setup, the source and target hosts (no passwords), the config file path
- the chosen flow and why, plus every user decision
- the current phase and the last gate passed
- schema fixes applied (file, object, change, approved by the user)
- background jobs: command, PID, log file, start time
- every change made to source or target (by you or the user), so it can be reverted at the end

**Resuming:** read the progress file, run `yb-voyager version` and compare it with the recorded
version, check which background PIDs are alive, and poll status before running anything. If the
version changed, stop and tell the user: a newer release can refuse to continue a migration
started on an older one, and the release notes say which.

## Running Voyager without a terminal

You run commands with no terminal. A command that asks a question reads the end of its input
and dies, unless you give it an input it can wait on. Handle passwords and questions this way:

- **Passwords** come only from the environment variables named on the
  [configuration-file page](https://docs.yugabyte.com/stable/yugabyte-voyager/reference/configuration-file/).
  Each of your commands runs in a fresh shell, so an `export` typed during the session does not
  carry over. The user either starts the session from a shell where they exported them, or keeps
  the exports in a file only they edit and you load it at the start of each command
  (`setup.md` §6). Check that each is set without printing it. Never write a password into a
  config file, onto a command line or into chat.
- **Questions** (continue anyway, include replicas, which target version, which option). You
  cannot foresee all of them, so:
  1. Before each command, read its `--help` and docs page for options that answer a question in
     advance (a target version, skipping read replicas, `--start-clean`). Ask the user and set
     them.
  2. Run every Voyager command except read-only ones (`version`, `--help`, reports, status) in
     the background with its input from a pipe that stays open, so an unforeseen question waits
     instead of killing the run:
     ```
     mkfifo <dir>/<name>.in
     nohup sleep 86400 > <dir>/<name>.in 2>/dev/null &   # holds the pipe open; record its PID
     nohup yb-voyager <command> --config-file <config> < <dir>/<name>.in > <dir>/<name>.log 2>&1 &
     ```
  3. Poll. When the PID is alive and the log ends in a question (for example `[Y/N]:`), show
     the user the question word for word and what each answer does. Wait for their answer, then
     send it: `echo <answer> > <dir>/<name>.in`. Never answer a question yourself.
  4. When the run ends, kill the holder PID and remove the pipe.

  Processes Voyager starts by itself (the reverse-sync processes after a cutover) do not read
  your pipe, so a question there kills them. Settle those answers before cutover
  (`references/fall-back.md`, plan-time step 6).
- **If your shell cannot do this** (the setup check in `references/setup.md` §1 found no
  `mkfifo`, or background processes die when your command ends, as in some sandboxes):
  - Commands that finish within one of your commands (assess, the grant script): run them in
    the foreground with the user's chosen answers piped in order, for example
    `printf 'Y\nN\n' | yb-voyager …`. An unforeseen question then ends the run with the
    question as its last output. Show it to the user and re-run with their answer added.
  - Long runs (export, import, archive): give the user the exact command to start in their own
    terminal, in `tmux` or `screen` if they have one so it survives a closed window. Poll its
    log and the export directory yourself, and tell them how to answer any question it asks.
  - With no shell at all, print each command and work from the output the user pastes.
- **`--yes`** answers every prompt with yes, destructive ones included. Use it only on a command
  the user has just approved, never as a default.
- **Background runs:** record each command, PID and log in the progress file. Turn off the
  progress display if `--help` offers a way. Poll rather than wait. To stop a run, `kill` its PID
  and confirm it is gone. Then fix the cause and start it again with the same command and export
  directory.
- **One run of a command per export dir.** Before starting or restarting one, check that its
  recorded PID is gone.
- **Config file:** after editing it, run the next real command and read the start of its output;
  if it rejects a key, fix the key from the message and the configuration-file page.

## Silent failures

Most Voyager problems fail loudly. These do not, so check for them explicitly:

- **Exit code 0 is not success.** A command can finish with skipped rows or failed statements.
  Read the status or report it wrote and treat any errored row, failed statement or partial
  status as not done.
- **Every file export writes must be accounted for.** If export produced something the import
  step does not apply (the docs or the analyze output say which), show it to the user.
- **Template values are not decisions.** Set every option that deletes, skips or backs up data
  explicitly after asking the user. Never run with a value only because a template had it.
- **A state that does not advance with no error** (a cutover that stays initiated, a job with a
  live PID and a silent log) usually means a paired process died or a prompt is waiting. Check
  liveness and the log before anything else.

## Never without explicit approval for that exact action

**Approval format.** Every request for approval gives the user enough context to decide without
reading the docs. Post it as rendered Markdown:

~~~markdown
**<the action, in plain words>**

- **What it is:** <one line>
- **Why now:** <what needs it>
- **What it changes:** <one change per bullet, or "nothing in the databases">
- **Undo:** <how it is reverted, or "cannot be undone">
- **Who runs it:** <you, through which connection> or <the user, and why>

```
<the exact command, with no passwords>
```

Run it? (yes / no)
~~~

Ask in this format for:

- Anything that changes the source or target server: grant scripts, server settings and the
  restarts they need, cluster flags, dropping foreign keys or disabling triggers. Show the exact
  commands and the effects the docs list. After the user's yes:
  - Run SQL steps yourself through the admin connection (`references/setup.md` §6). If the user
    gave none, print the SQL for them to run.
  - The user does restarts, managed-service parameter changes, cluster flags and anything that
    needs `sudo`. Run one of these yourself only if you already have that access and the user
    approves that exact step.
- Any `--start-clean`. It discards Voyager's state and, on some commands, deletes exported files
  or target objects. Read `--help` and the page for what it removes on that command.
- Any option that truncates tables, skips rows, overwrites on conflict or deletes archived
  changes.
- Every `initiate cutover …`. A cutover cannot be taken back.
- `end migration`. It cannot be resumed and removes whatever is not backed up.
- Upgrading or reinstalling Voyager during a migration.

## Anti-patterns

| What agents do | Why it breaks | Do this instead |
|---|---|---|
| Copy a config template and only fill source/target | Templates can ship optional sections enabled and placeholders set | Read every uncommented line and keep only what the user chose |
| Pick the migration flow from the assessment report | The flow is the user's decision | Ask the plan questions; use the report as evidence |
| Run the live-migration grants "to be safe" during setup | They change production tables | Use the assessment-level grants until the flow is chosen |
| Delete or comment out objects to get a clean analyze-schema report | Silent data or feature loss | Classify each issue; anything beyond a mechanical fix is the user's decision |
| Re-run a failed command with `--start-clean` | Wipes exported data, edits or target objects | Read the log first; check the docs for whether the command resumes |
| Treat exit code 0 as success | See "Silent failures" | Check the phase's success criteria from Voyager's own output |
| Kill a slow job after one flat poll | Progress moves in batch-sized steps | Use the stuck rule in the phase file |
| Cut over while a streaming process is down | The cutover waits with no error | Confirm every streaming process is alive first |
| Pass `--yes` everywhere to avoid hangs | It also approves destructive prompts | Remove each prompt's cause; use `--yes` only after approval |
| State a default, path or field from memory | It may have changed in this release | Read `--help`, the output or the docs page |

## Checking your own work

Before each gate, confirm the phase's success criteria from evidence you just read (what the
command printed, the files it wrote, row counts), not from an earlier poll. If evidence is
missing, say so in the gate message instead of reporting success.
