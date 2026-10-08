---
name: yb-model
description: "Offline review of an existing YugabyteDB YSQL schema and data model from evidence captured on the running database: a ysql_dump of the schema, plus pg_stats, pg_stat_statements or a query list, row counts and planner settings when available. Finds key and sharding problems (hot shards, tablet skew), unused or redundant indexes, partition and tablet-split gaps and data-quality issues, with deterministic findings and safety-checked DDL. Use when the user hands over a YugabyteDB schema dump or capture bundle and asks whether the schema or data model holds up, or why a table or tablet is hot, for example \"review this ysql_dump\" or \"audit our data model before go-live\". Not for: a slow query, or pg_stat_statements output without a schema (yb-query-analysis); node metrics such as CPU, memory or I/O (yb-metrics-analysis); an open-ended universe health check (yb-performance-assessment); designing a new schema, application code, or a PostgreSQL schema being migrated (ysql)."
---

# YugabyteDB schema review

This skill reviews an existing schema, completely, from as much evidence as the user can
give: the more of the bundle in step 1 exists, the more of the engine runs. The review runs
offline, on files; nobody needs access to the customer's cluster. If the user has only DDL,
review it and the report labels the result a structural review. If the user has no schema,
only a slow query or `pg_stat_statements` output, this is not a schema review: see
[Other skills](#other-skills).

**Scope guard.** YugabyteDB YSQL only. Do not apply these rules to plain PostgreSQL work.

## How this skill works

The findings come from a deterministic engine, `scripts/yb-model.py`. Its rules live in
`rules/rules.json`. A rule that claims planner or execution behaviour cites the yugabyte-db
regress test that pins it; statistics, workload and configuration rules are arithmetic on the
customer's own data, and their findings say which inputs they were computed from. The same
inputs give the same findings, severities, ranking, DDL and chat message, whichever model runs
the skill.

Your job is to collect the inputs, run the engine, add only the context the engine cannot know
and say what was not verified. **Do not add, drop, re-rank, re-word or re-grade findings yourself.** If you
believe a finding is wrong or something is missing, write it under *Reviewer notes* with the
finding ID, or "not in engine". Never edit the findings table by hand.

| File | Read when |
|---|---|
| `references/pitfalls.md` | Before every review, and whenever something does not work as expected. Things to be wary of. |
| `references/engine.md` | Step 4. What each output field means; how to read replay results. |
| `references/intake.md` | Step 1. What to ask for, and the exact capture commands. |
| `references/validation.md` | Step 5. Live-cluster checks the engine cannot do. |
| `references/checks.md` | Only for Reviewer notes, or when the engine cannot run. Background on mechanisms. |
| `references/critic.md` | Step 6, the optional second pass. |

## Procedure

Copy this checklist and tick it off.

```
- [ ] 0. references/pitfalls.md read
- [ ] 1. Inputs gathered into one directory
- [ ] 1b. Preflight ran; if it asked, the user said yes or no
- [ ] 1c. Any input the engine could not read converted (a copy) and named in NOTES
- [ ] 2. Engine ran (review.md, review.json and chat.md exist)
- [ ] 3. Replay ran, or the reason it did not is recorded
- [ ] 4. User context added to review.md, or the NOTES line deleted
- [ ] 5. Limitations stated
- [ ] 6. (optional) Second pass recorded under Reviewer notes
- [ ] 7. chat.md pasted as the chat message
- [ ] 8. Anything that did not work as expected written to pitfall-candidates.md
```

### 1. Gather

A complete review needs the complete bundle, so ask for it before running anything. Whoever
can connect to the database (the user, or the customer through them) runs the two capture
commands in `references/intake.md` §1 (`ysql_dump --include-yb-metadata` and
`scripts/collect.sql`): together they write every file the engine reads, namely statistics,
workload, row counts, planner settings, index usage, tablet counts and the release. Take
whatever they can give; preflight (step 1b) names what is still missing.

Make one directory (the *bundle*) and put the DDL in `schema.sql`. Copy every file the user
gave you into it unchanged:

- The `ybm_*.csv` files from `scripts/collect.sql`.
- A `pg_stat_statements` export, saved as `ybm_pss.csv`.
- A `pg_stats` export, saved as `ybm_pg_stats.csv`.
- Captures from several nodes, one subfolder each (`node1/`, `node2/`, ...). Usage counters
  are per node; the engine adds the folders up.

Exports often arrive in another shape (tab-separated, document or spreadsheet tables,
aggregates over nodes). Look at what you were given first, then write a one-off converter
into the layout above. Keep the originals, change no values, and say in the NOTES line what you
mapped and what you could not. Keep every row: if an aggregated pg_stats export lists a column
more than once (per-node variants), the engine keeps one row by a stated rule and names the
columns in its open items.

Write any query list the user gives to `queries.sql`, one statement per `;`, whether or not
there is a `pg_stat_statements` export. The engine uses both: a listed statement found in
`pg_stat_statements` is ranked from it, and one it lacks is added unranked. Keep parameters
typed the way the application sends them (for example `$2::boolean` where a parameter appears
only in `$2 IS NULL`), so replay can plan them. If, and only if, the user or their document
states that no other statement touches these tables, make the first line
`-- ybm: workload-complete`. Unused-index checks (IDX001) then run on the list, marked as
resting on that statement. Never add it on your own judgement.

The release is read from `SELECT version()` (`ybm_meta.csv`), `pg_settings`
(`server_version`) or the `ysql_dump` header. If the user states it, or it is only in some
other file, pass `--release <a.b.c.d>` to `preflight` and `review`.

### 1b. Preflight: confirm before reviewing with missing inputs

```bash
python3 <skill-dir>/scripts/yb-model.py preflight <bundle>
```

- **Exit 0:** nothing that changes the findings is missing. Go to step 2. If it printed minor
  gaps, mention them in one line.
- **Exit 3:** inputs that change the findings are missing. Show the printed text to the user
  **verbatim** (it names each missing input, what it mutes or weakens, and how to collect it)
  and ask **one yes / no question**: proceed with an incomplete review? Use the question tool
  if you have one. Then stop and wait. Never answer it yourself, never assume yes from earlier
  messages, and do not summarise or soften the list.
  - **Yes:** go to step 2 and add `--accept-missing`.
  - **No:** give the user the collect commands from the printed text (and
    `references/intake.md` §1 for the full capture), then stop. Run preflight again when
    they come back with the files.

`review` refuses to run (exit 3) without `--accept-missing` while a major input is missing.
Never pass that flag unless the user said yes in this conversation to this bundle's list.

What muting means: a rule whose inputs are missing is not evaluated, because its result would
rest on a guess (for example, no primary-key re-key from an unranked query list). The report
names the candidates it withheld. A weakened rule still runs, and each finding it makes
carries a caveat such as `sharding inferred`. The mapping lives in `rules/inputs.json`; do not
re-derive it.

### 1c. When the engine cannot read an input

If preflight or the engine exits 2 naming a file (UTF-16, another delimiter, a missing header,
no table read from the schema), or a review comes back with far fewer tables than the dump
holds, find the cause before anything else: look at the file's first bytes and lines. Then
write a one-off converter that writes a corrected copy into a new bundle directory, run again
on that copy, and say in the NOTES line what you converted and why. Never change values or
the customer's originals. `references/pitfalls.md` lists the causes seen so far. If yours is
new, record it (step 8).

### 2. Run the engine

```bash
python3 <skill-dir>/scripts/yb-model.py review <bundle>
```

Add no flags other than `--accept-missing` (step 1b) and `--release` (step 1). The engine
runs replay by itself when Docker has a `yugabytedb/yugabyte` image for the bundle's release,
and prints why when it cannot. Replay does four things:

1. Starts that version in a scratch container.
2. Injects the customer's row counts and column statistics, the way the TAQO planner tests
   do.
3. Runs `EXPLAIN` on every pattern.
4. Runs the **probe** of every fired rule that has one: a few thousand generated rows and
   `EXPLAIN (ANALYZE, DIST)`, checking that the rule's mechanism holds on that release. A
   refuted rule's finding is withdrawn; the report says which. Probes never use customer data
   and store nothing.

Never `docker pull` without asking: the image is a download of about 1 GB. If the user
agrees to the pull, run the same command again afterwards.

The engine writes `review.md`, `review.json`, `chat.md` (the chat message) and, with replay,
`plans.json` into the bundle. Exit codes:

- **0:** no findings at high or above.
- **1:** findings at high or above. This is not a failure.
- **2:** the engine failed. Show the error. If it names an input file, go to step 1c.
- **3:** inputs are missing and were not accepted. Go back to step 1b.

If the engine cannot run on this surface, say so and review by hand with
`references/checks.md`. Label the result **manual review, engine not run**.

### What the report contains

`review.md` always has these sections, in this order. Do not reorder or drop them:

1. **Headline**: the worst schema finding, its cost and the first action (engine-written).
2. **What this review is based on**: inputs, release, where each planner setting came from.
3. **Assumptions and unknowns**: missing inputs, settings that depend on how the cluster was
   deployed, features that do not exist on this release, and the **Muted checks** table
   (which rules were muted or weakened by which missing input, and the candidates withheld).
4. **Access patterns**: P1..Pn with weight, calls, time share, access path.
5. **Findings**: schema findings, ranked, each with fact, mechanism, fix and its basis (the
   regress test that pins it, or "computed from the bundle").
6. **Disputed by replay (planner settings assumed)**: findings that replay disagreed with
   while running under assumed planner settings (the bundle had no pg_settings). They are
   kept, because the customer's real settings may differ, but stay out of the recommended
   DDL and action items. Say so plainly; collecting `ybm_settings.csv` settles them.
7. **Workload hygiene (confirm with the application team)**: how the application uses the
   database (lookups that find nothing, UPDATEs that match nothing or rewrite every column,
   full-table counts, fan-out, planning time). Not schema defects; hand these to the app
   owners.
8. **What is already sound.**
9. **Recommended DDL**, after the safety pass.
10. **Safety check of the recommendations.**
11. **Validation and limitations**, including replay and the self-check.
12. **Action items**, schema first, then the application team.

### Versions

Behaviour and defaults differ between YugabyteDB releases and between deployment tools on
the same release (yugabyted and YBA new universes turn the cost model on; a manual install or
an upgraded universe keeps `legacy_mode`). The engine reads these facts from a local cache,
`rules/versions.json`, built from the yugabyte-db source for the releases being reviewed
(it is not shipped with the skill). Your job is only to report what the engine says:

- Settings come from the bundle's pg_settings. Without them, the release default is used
  only when every deployment tool agrees; otherwise the finding is conditional and the open
  items say so. Ask for `ybm_settings.csv` rather than guessing.
- A fix that needs a feature the release lacks (for example merge scan streams) is replaced
  with one that works on that release.
- A finding whose rule cites a regress test that does not exist on the customer's release
  is marked "not pinned by tests on <release>".
- When the cache lacks the customer's release, the nearest *earlier* release stands in, never
  a newer one (a newer release may already fix what the customer's still does); the open
  items name it. Replay follows the same rule: the exact image, or else the newest earlier
  image of the same release line.
- Replay on an image that is not the customer's exact release lists the differences between
  the two releases, and does not confirm rules whose tested behaviour differs. Without
  release facts for both, the differences are unknown and such a replay confirms or refutes
  nothing.
- If the open items contain `RELEASE-DATA-MISSING`, the cache has no entry for the customer's
  release (on a fresh install it is empty). If the user has a yugabyte-db checkout, run the
  `--repo` command quoted in that item with its path; it reads local files only. Otherwise
  **ask the user** whether you may fetch the release's source facts from GitHub, and run the
  `--github` command only if they agree. Then run the review again. If they decline, keep
  the review and say which release facts are unknown or come from the nearest earlier
  release.

### 3. Replay

If replay failed or was skipped, give the reason in one sentence under Validation. Without
replay, access-path findings stay `probable`. Do not upgrade them by reasoning.

### 4. Add context

The engine writes the headline (worst finding, its cost, first action) and the chat message.
Keep both as they are. Replace the `<!-- NOTES ... -->` line with at most two sentences of
context that only the user gave you, or delete the line. Do not write DDL, numbers or estimates of your own, and do not
add causes or effects (contention, latency, retries) that no finding states: every
statement, figure and claim in the review comes from the engine. Add a `## Reviewer notes` section
only when step 6 produced something or you disagree with a finding.

The report ends with a **Safety check** table: the engine applied every recommended DDL
to a copy of the schema and checked uniqueness, ON CONFLICT targets and the plans of every
ranked pattern. Rows marked `amended` already include the fix (for example an added UNIQUE
index). Never recommend DDL that bypasses a `warn` row; point the user at the SAF finding.
Replacement indexes keep the original's UNIQUE, INCLUDE, predicate and SPLIT, and drops are
plain `DROP INDEX` (YSQL rejects `DROP INDEX CONCURRENTLY`) or `ALTER TABLE ... DROP
CONSTRAINT` for a constraint's index. Do not rewrite them.

### 5. Limitations

The engine already lists the open items and what replay did. Add only facts from the user
that change the picture, for example "statistics are from staging".

### 6. Second pass (optional)

For a schema with more than a handful of tables, and when subagents are available, run the
critic in `references/critic.md` on the DDL and the pattern list only. For each item it
raises that no engine finding covers, add it under Reviewer notes as "second pass, not
verified by the engine". Do not merge these into the findings table.

### 7. Deliver

Paste `chat.md` as your chat message, unchanged. It holds the headline, the top three findings
with their fixes, the review level and, for an incomplete review, the missing inputs and the
withheld candidates, and it ends with the path to `review.md`. You may add at most two
sentences after it, under "Reviewer note:", with context the user gave you. Never restate or
summarise findings in your own words: that is where reviews pick up wrong numbers.


### 8. Record what surprised you

If any step did not work as expected (an input in an unforeseen shape, a finding you had to
correct under Reviewer notes, an error you had to work around), add one entry to
pitfall-candidates.md next to review.md: the symptom, the cause and what you did. Keep it
generic: no customer names, table names or values. Maintainers move useful entries into
`references/pitfalls.md` or fix the engine. Do this after delivering (step 7); do not mention
it in the chat message.

## Other skills

Route by what the user hands over and what they ask, not by topic. These skills live in the
same repository; point to one only if it is installed.

| The user has, and asks | Skill |
|---|---|
| A schema dump, with or without statistics and workload: does the model hold up? | this one |
| A slow query, a plan, or `pg_stat_statements` output without a schema | `yb-query-analysis` |
| Metrics: a hot node or tablet, CPU, I/O, tablet limits | `yb-metrics-analysis` |
| "Is the universe healthy?", no single symptom | `yb-performance-assessment` |
| A new schema, application code or a migration to write | `ysql` |

Hand-offs from a review:

- **Workload hygiene** findings describe how the application uses the database. Following
  them up on the running system belongs to `yb-query-analysis`.
- **Writing the migration** for a recommended change (backfill, dual writes, cut-over)
  belongs to `ysql`.
- **Reading a replayed plan** node by node belongs to `explain-plan-analyzer`.

When another skill calls this one as part of a broader assessment, hand back `review.md` as
its own section: its findings are not merged into, re-ranked or re-graded in the caller's
list, and the preflight question still goes to the user.

## Style

Write as a senior YugabyteDB architect talking to a principal engineer. Lead with the worst
problem. No praise, filler or canned conclusion. No em or en dashes. Avoid *delve, leverage,
robust, crucial, seamlessly, holistic, furthermore, moreover*. Refer to findings by ID
(`F3`) rather than restating them.
