# Assess and plan: assessment, flow choice, flow config, prep

Read this after setup is done and before any schema work. It covers running `assess-migration`, presenting the report, choosing the flow with the user, writing the flow's config, and the prep the user runs on source and target.

Docs (fetch for commands, keys, report contents and limitations), all under https://docs.yugabyte.com/stable/yugabyte-voyager/:

- `migrate/assess-migration/`: assessment guide, prerequisites, how to read the report
- `reference/assess-migration/`: flags and config keys
- `introduction/`: the flows and the supported versions per flow
- `migrate/live-migrate/`, `migrate/live-fall-back/`, `migrate/live-fall-forward/`: per-flow prep and "Limitations"

## 1. Pre-assess checks

1. Read the assessment guide's prerequisites. For each one that is a property of the source (an extension, collected statistics, a running workload), check it with a read-only query and tell the user what you found.
2. Anything that changes the source (enabling an extension, a restart, running `ANALYZE`) is the user's decision. **GATE: the user approves or declines each change before assessment.**
3. If the guide says metrics are sampled during the run, ask the user to have the typical workload running then. An idle source gives meaningless sizing.

## 2. Run the assessment

1. Confirm the source password variable is set (without printing it).
2. Run `yb-voyager assess-migration --config-file <assessment config>` in the background with `nohup`, a log file and the PID recorded. Do not add `--yes`: its prompts warn about conditions the user must see.
3. Poll: healthy while the PID is alive and the log grows. If the log ends in a question, stop the run, show the user the question, fix the cause and re-run.
4. Success: exit code 0, and the report files the run names in its output or log exist and are non-empty (`test -s`). Record their paths.
5. If a re-run in the same export directory refuses, read the message. Removing the previous assessment needs `--start-clean`: copy the old report aside and ask first.

## 3. Present the report

Read the JSON report. The flow choice is the user's; you present evidence. Show, as written:

- the complexity rating and its explanation (an absent or "not available" rating is no rating, not a low one);
- every issue, grouped by its category and impact, with the docs link it carries;
- the sizing recommendation with the assumptions the assessment guide states (if sizing failed, say "no sizing", not zero);
- per-table size and write rate, which drive the offline-versus-live question.

Flag any issue the report ties to live migration, and check every issue against the Limitations section of each flow you may offer. Record the rating, the live-blocking issue count and the sizing in the progress file.

## 4. Check the flow limitations on the source

For every Limitation on a live-flow page that is a property of the source schema (for example tables without a primary key, unsupported datatypes, partitions created during the migration), check it yourself with a read-only query. The report may not flag all of them. List the matches for the user.

## 5. Flow decision

Ask in this order, one question at a time:

1. PostgreSQL version, and whether the source is managed (RDS, Aurora, Cloud SQL, Azure) or self-hosted.
2. Can the application stay offline for the whole export plus import? Compare with the report's import estimate and table sizes.
3. Is a way back to the old source needed after cutover?
4. If yes: may Voyager write to the source (fall-back), or will the user provide a separate database (fall-forward)?
5. Target: self-managed or Aeon, and its version. Check the introduction page's version table for the flows you may offer.

| Downtime allowed | Rollback after cutover | Flow |
|---|---|---|
| Whole export plus import | Restore from backup | Offline |
| Minutes | None | Live |
| Minutes | Back to the original source | Live with fall-back |
| Minutes | To a separate database; source untouched | Live with fall-forward |

For each flow you offer, state its costs from its docs page (source settings and restarts, what it writes to the source, extra databases, limitations). Add the plan-time checks from `field-notes.md` §1 that apply to the user's source, write rate and flow (for example a peak change rate above what live migration has commonly handled, or schema traits that broke past live migrations), labelled as field experience. Never pick for the user. Offer bulk load only when the data is already in files.

## 6. Choices that lock in

Settle these before the flow config. Check the flow page for anything else that cannot change once export starts:

- the flow itself, including fall-back or not: grants and cutover options depend on it;
- schema, table and exclude lists;
- colocation, which is fixed when the target database is created;
- names with special characters, which the docs' limitations may rule out.

Record the choices in the progress file.

## 7. Flow config

1. Find the config templates the install page says ship with the installed version, and copy the chosen flow's template next to the assessment config. Use the local copy, not one from GitHub.
2. Carry over the export directory, the source values and the target version, and fill the target (and, for fall-forward, source-replica) connection from the user. Passwords stay in the environment.
3. Read every uncommented line. Keep what the flow and the user's choices need; comment out optional sections the user did not ask for; replace every placeholder with a real value or comment it out.
4. Check the keys that encode the flow (export type, fall-back option) against the flow page.
5. Keep passwords, `--yes` and `--start-clean` out of the file.
6. Verify: no placeholder or password value is left (`grep -n '<' <file>`, `grep -n -i password <file>`). The first command that loads the file validates it.
7. Record the file path in the progress file.

## 8. Flow-specific prep (the user runs it)

Every item changes a production system.

1. Fetch the flow page's "Prepare the source database" and "Prepare the target database" sections, and the grant-script page.
2. For each step, print the exact command for the user's setup and the effects the docs list, then wait for the user to say it is done.
3. The grant script may ask questions (for example how the migration user gets table ownership). The user runs it in their own terminal so they can answer. Explain each option's effect from the docs and recommend the least invasive one. If a run stopped partway, treat the source as already changed.
4. Ask the source major version: the fall-back page may have version-specific steps.
5. Record every change (server settings, grants, table settings, ownership, cluster flags) in the progress file, so it can be reverted at the end.
6. Verify each change with a read-only query (for example `SHOW wal_level` for logical decoding).

## 9. Target database and sizing

1. If the report recommends colocated tables, the target database must be created colocated, and that cannot change later. The user creates it (or confirms an existing one) after seeing the report. Verify colocation with the query the docs give.
2. Never answer an `import schema` prompt about colocation for the user, and never use `--yes` there.
3. Voyager never resizes the cluster. Show the sizing with its assumptions and ask the user to resize or confirm before data import.

## 10. Gates

**GATE: the user approves or declines each source change before assessment.**

**GATE: the user reviews the report and the limitation checks, and states the flow.**

**GATE: the user approves the flow config, the grant variant and its options, and the prep commands.**

**GATE: the user confirms the prep is done and the target database exists.**
