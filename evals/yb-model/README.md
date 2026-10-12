# yb-model evals

These evals compare two revisions of the skill (for example `main` and a change under review)
across models on the same evidence bundle. We score each run against an answer key.

## Layout

```
fixtures/<name>/
  bundle/            what the skill under test sees: schema.sql + ybm_*.csv (collect.sql output)
  answer-key.json    planted or known defects, with "engine_match" and "judge" text, plus traps
  build.sh           optional: rebuilds bundle/ on a scratch container (synthetic fixtures)
  setup.sql, load.sql, gen_workload.py   optional: inputs to build.sh (the workload is generated)
judge.md             grading brief for the blind judge
prepare.sh           creates isolated run directories
```

Only `bundle/` is copied into a run directory. The model under test never sees the answer
key, setup SQL or workload.

## Recall per mechanism

```bash
python3 evals/yb-model/recall.py [fixture ...]
```

Runs the engine on every fixture with an answer key (`fixtures/*`, and `fixtures-private/*`
when present) and prints each defect as hit, miss or judge-only, and each trap as ok or
violated, then a summary by rule. In an answer key, `engine_match` lists what the engine must
emit (a finding by `rule`, pinned by `object_contains`, `table` or `fact_contains`); `engine_absent` on a trap lists what it must
not: a finding, review text (`section_contains`), or a pattern it must not model as a point
lookup (`point_query_contains`). A defect with no `engine_match` is one the engine has no rule
for; the judge grades it. With few reviewed cases, read the table, not a percentage, and plant
each mechanism in a synthetic fixture as well (`keygap` plants CAP014) so that one customer's
schema never stands for a rule's recall. A private fixture's answer key may name its bundle
folder with `"bundle"`.

## Recorded server answers

`fixtures/gen` exercises the measured pass's sample generator on a synthetic schema and holds
`recorded.json` once recorded, every SQL call one live `review --replay --measure` sent and the
server's answer (`YBM_RECORD=<file>` records one; release-specific, so built locally and
git-ignored, and the test skips without it). `test_ybm.py` (class Recorded) serves it back, runs the
whole replay and measured pass offline, and checks properties every sample must have: each
table loads with its constraints, the sample has the planned shape, each read finds rows, the
run is deterministic. When the engine's SQL changes the test fails with "call not in the
recording": record again (the fixture's README has the command).

## Adding a real-world fixture

1. Capture against the customer database (read-only):

   ```bash
   ysqlsh -h <host> -U <user> -d <db> -f skills/yb-model/scripts/collect.sql
   ysql_dump -h <host> -U <user> -d <db> --schema-only --include-yb-metadata > schema.sql
   ```

2. Put the files in `fixtures-private/<name>/bundle/`. That folder is ignored by git: a
   customer's schema, statistics and workload are never committed, and nothing derived from
   them goes into a commit (see `AGENTS.md`). Only synthetic fixtures, such as `ecommerce`,
   live in `fixtures/`.
3. Write `answer-key.json` from what the case actually established: the ticket, the RCA,
   what the customer changed. List traps too: things a review should not claim.

## Running a comparison

```bash
evals/yb-model/prepare.sh <fixture> <run-root>   # creates <run-root>/<fixture>/<skill>-<model>/bundle
```

Then, for each `<skill>-<model>` directory, start one agent with the model under test. Use
the prompt in `judge.md` §Runner, with SKILL_DIR pointing at the worktree for that skill
version:

- `git worktree add ../yugabytedb-skills-base main` for the base revision.
- This checkout for the change.

Each run's deliverable is the review, the suggested direction (`plan.md`, when the skill's
step 4b ran) and the chat message. When all runs have written `final.md`, give the judge (one strong model) the answer key and
the `final.md` files, anonymised as A, B, C and so on. The judge writes `scores.json`.

Judge every run on the same deliverable: the review, the suggested direction (`plan.md`, when
step 4b ran) and the chat message. Runs sometimes leave a part out of `final.md`; before
judging, rebuild it from the run's `review.md`, its `plan.md` and its chat message (the
skill's `chat.md`, or the run's reply). Alternate which revision is A across
fixtures so a judge's position bias cannot favour one, and strip run paths that name a revision.
Where Docker is available, also run each revision's modelled options (review.json `ddl`, as
`yb-model.py sketch` would print them) against the fixture's schema in a scratch container:
the judge cannot tell DDL that YSQL rejects.

For the engine alone, no model is involved:

```bash
python3 -m unittest discover -s skills/yb-model/scripts -p 'test_*.py'
```

That test asserts byte-identical output across runs and full recall on the answer key's
`engine_match`.
