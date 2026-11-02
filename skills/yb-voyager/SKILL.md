---
name: yb-voyager
description: Guides a full PostgreSQL to YugabyteDB migration with YugabyteDB Voyager (yb-voyager), phase by phase - setup, assessment, choosing a migration flow (offline, live, live with fall-back, live with fall-forward), schema export and the analyze-schema fix loop, data export and import, cutover and end migration - stopping for the user's approval between phases and before every irreversible step. Use when a user wants to migrate a PostgreSQL database to YugabyteDB, is in the middle of a Voyager migration and wants to continue or recover one, or asks which Voyager migration type fits them. Triggers on yb-voyager, Voyager, assess-migration, export schema, analyze-schema, import schema, export data, import data, get data-migration-report, cutover, initiate cutover, fall-back, fall-forward, source-replica, archive changes, end migration, export-dir, migration assessment report. Not for writing application SQL against YugabyteDB (ysql) or reviewing an existing YugabyteDB schema (yb-model).
---

# YugabyteDB Voyager migration orchestrator

You run a migration with the user, one phase at a time. The Voyager docs own the commands, flags
and procedures; this skill owns the order, the decisions, the gates, and the traps an agent hits
running Voyager unattended. Fetch the docs for every step. Do not work from memory: Voyager
changes often.

## Scope

- **PostgreSQL sources only.** The docs list MySQL and Oracle migration as deprecated, with
  support ended. For those sources, stop: point the user to the
  [introduction](https://docs.yugabyte.com/stable/yugabyte-voyager/introduction/) and Yugabyte
  Support, and do not orchestrate.
- **Bulk load from CSV/text files** (`import data file`) is a separate workflow. Link
  [bulk data load](https://docs.yugabyte.com/stable/yugabyte-voyager/migrate/bulk-data-load/)
  and stop.
- **Iterative cutover** (repeated fall-back rounds) exists, but this skill does not orchestrate
  it. If the user asks for it, link
  [iterative cutover](https://docs.yugabyte.com/stable/yugabyte-voyager/reference/iterative-cutover/)
  and say it is lightly documented.

## Where facts come from

Check in this order, and prefer the higher source when two disagree:

1. **`yb-voyager <command> --help`** for flags and defaults. It matches the installed binary;
   the docs describe only the latest release.
2. **The docs page for the step**, fetched as Markdown by appending `.md` to the page URL (for
   example `https://docs.yugabyte.com/stable/yugabyte-voyager/migrate/live-migrate.md`). Each
   reference file below names the pages for its phase.
3. **A YugabyteDB docs search tool, if one is connected** (the Kapa MCP server at
   `https://yugabyte.mcp.kapa.ai`). Use it for error messages, "is X supported" questions and
   anything the pages above do not answer. Its answers can mix releases, so confirm any flag it
   suggests with `--help` before running it.

Lines marked `**Docs gap:**` in this skill cover places where the docs were wrong or silent when
this skill was written. If the fetched docs now say otherwise, follow the docs.

## Phases and gates

| # | Phase | Read | Ends with |
|---|---|---|---|
| 1 | Setup: install and version check, export dir, stage-1 config, passwords, assessment grants | `references/setup.md` | Voyager verified, source reachable as the migration user |
| 2 | Assess: run `assess-migration`, present the report | `references/assess-and-plan.md` | **GATE**: user has seen complexity, issues and sizing |
| 3 | Plan: choose the flow, write the full config, prepare source and target for that flow | `references/assess-and-plan.md` | **GATE**: flow chosen, prep done by the user, target DB created |
| 4 | Schema: export, analyze-schema fix loop, import | `references/schema.md` | **GATE**: clean analysis, schema imported and verified |
| 5a | Data, offline flow | `references/data-offline.md` | **GATE**: data imported, finalized, row counts match |
| 5b | Data, live flows, up to cutover to target | `references/data-live.md` | **GATE** before every cutover |
| 5c | Live with fall-back: reverse sync, optional cutover to source | `references/fall-back.md` | **GATE**: stay on YugabyteDB, or fall back |
| 5d | Live with fall-forward: source-replica, optional cutover to it | `references/fall-forward.md` | **GATE**: stay, or switch to the replica |
| 6 | End migration | `references/end-migration.md` | **GATE**: backups chosen, user confirms the end |

Read only the reference file for the current phase, plus the flow file the user chose.

**At every gate**, post this and wait for an explicit yes:

```
Phase <n> done: <what ran, outcome in one or two lines>
Evidence: <report path / counts / status>
Decisions recorded: <any>
Next: <phase n+1 and its first command>
Needs from you: <approvals, actions you must run yourself, info>
Continue?
```

Silence, or a reply about something else, is not a yes.

## Progress file

Keep `<export-dir>/agent-progress.md`, and update it after every command that changes state. It
lets a new session pick up where the last one stopped:

- Voyager version recorded at setup, the source and target hosts (no passwords), and the config file path
- the chosen flow and why, plus every user decision (grant-script ownership option, backups, colocation)
- the current phase and the last gate passed
- schema fixes applied (file, object, change, approved by the user)
- background jobs: command, PID, log file, start time
- source and target changes the user made (grants, `wal_level`, gflags), so they can be reverted at the end

**Resuming:** read the progress file, run `yb-voyager version` and compare it with the recorded
version, check which background PIDs are alive, and poll status (see the phase file) before
running anything. If the version changed, stop and tell the user: the release notes list
upgrades that cannot continue an in-flight migration.

## Running Voyager without a terminal

You usually run commands with no TTY. Any Voyager prompt then hangs or aborts the run, so remove
each prompt before it happens:

- **Passwords:** the user exports `SOURCE_DB_PASSWORD`, `TARGET_DB_PASSWORD` and (fall-forward)
  `SOURCE_REPLICA_DB_PASSWORD` in the environment that runs Voyager. Check with
  `test -n "$TARGET_DB_PASSWORD"` without printing the value. Never write a password into the
  config file or onto a command line: both end up on disk.
- **Confirmations:** `--yes` answers every prompt with yes, including destructive ones. Pass it
  only on a command the user has just approved, never as a default. It is a CLI flag only (not
  allowed in the config file). On `initiate cutover` it is accepted but hidden from `--help`.
- **Long-running commands** (`assess-migration`, `export data`, `import data`,
  `archive changes`) run in the background with `nohup`. Redirect output to a log file under the
  export dir, add `--disable-pb true` where the command accepts it, and record the PID in the
  progress file. Poll status at a regular interval rather than waiting on the process.
- **One command per export dir at a time.** Voyager holds a lock file per command in the export
  dir. Before starting or restarting a command, check whether its PID is still running.
- **Config file:** Voyager rejects unknown keys. After editing it, run the next real command and
  read the first lines of its output for a config error.

## Never without explicit approval for that exact action

Each of these destroys data or state, or changes the user's databases. Explain the effect, then ask:

- Anything that changes the source or target server: the grant script (live variants set
  `REPLICA IDENTITY FULL` and may change table ownership), `wal_level` and the restart it needs,
  target gflags, dropping FKs or disabling triggers. **The user runs these** with admin
  credentials; you print the exact commands and what each does.
- Any `--start-clean`, on `assess-migration`, `export schema` (erases manual schema edits),
  `import schema` (drops the target schemas with `CASCADE`), `export data` or `import data`.
- `import data --truncate-tables`, `error-policy-snapshot: stash-and-continue`,
  `on-primary-key-conflict: IGNORE`: the first deletes data, the other two skip rows silently.
- `archive changes --policy delete`.
- Every `initiate cutover …`. A cutover cannot be taken back.
- `end migration`. It cannot be resumed, and deletes whatever in the export dir is not backed up.
- Upgrading or reinstalling Voyager during a migration.

## Anti-patterns

| What agents do | Why it breaks | Do this instead |
|---|---|---|
| Copy a config template and only fill source/target | The templates enable a local yugabyted control plane and other placeholder sections | Follow the stage-2 procedure in `references/assess-and-plan.md` |
| Pick the migration flow from the assessment report | The report has no flow recommendation | Ask the user the plan questions; use the report as evidence |
| Run the live-migration grants "to be safe" during setup | They change production tables (replica identity, ownership) | Assessment needs only the offline grants; run live grants after the flow is chosen |
| Delete or comment out objects to get a clean analyze-schema report | Silent data or feature loss | Classify each issue; anything beyond a mechanical fix is the user's decision |
| Re-run a failed command with `--start-clean` | Wipes exported data, edits or target schemas | Read the log first; most commands resume by re-running unchanged |
| Treat exit code 0 as success | Some runs exit 0 with errors (`failed.sql`, `DONE_WITH_ERRORS`) | Check the success criteria in the phase file |
| Kill a slow import after one flat poll | Progress moves in batch-sized steps | Use the stuck rule in the phase file |
| Cut over while a streaming process is down | The cutover stalls, then errors | Confirm every streaming PID is alive first |
| Pass `--yes` everywhere to avoid hangs | It also approves destructive prompts | Remove each prompt's cause (env vars, explicit flags); use `--yes` only after approval |

## Checking your own work

Before each gate, confirm the phase's success criteria from evidence you just read (status JSON,
report files, row counts, `cutover status`), not from what a command printed earlier. If evidence
is missing, say so in the gate message instead of reporting success.
