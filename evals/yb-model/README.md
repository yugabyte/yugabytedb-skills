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

When all runs have written `final.md`, give the judge (one strong model) the answer key and
the `final.md` files, anonymised as A, B, C and so on. The judge writes `scores.json`.

Judge every run on the same deliverable: the review plus the chat message. Runs sometimes put
only one of the two in `final.md`; before judging, rebuild it from the run's `review.md` and its
chat message (the skill's `chat.md`, or the run's reply). Alternate which revision is A across
fixtures so a judge's position bias cannot favour one, and strip run paths that name a revision.
Where Docker is available, also run each revision's recommended DDL against the fixture's schema
in a scratch container: the judge cannot tell DDL that YSQL rejects.

For the engine alone, no model is involved:

```bash
python3 -m unittest discover -s skills/yb-model/scripts -p 'test_*.py'
```

That test asserts byte-identical output across runs and full recall on the answer key's
`engine_match`.
