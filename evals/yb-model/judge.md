# Eval briefs

## Runner

Give this to the model under test, with SKILL_DIR, BUNDLE and RUN_DIR filled in:

> You are being evaluated on following an agent skill exactly. The skill is installed at
> SKILL_DIR. Read SKILL_DIR/SKILL.md first and follow its procedure, reading its references
> and running its scripts from SKILL_DIR as it instructs.
>
> The user's request is: "Can you review this YugabyteDB schema and workload? Everything we
> captured is in BUNDLE (schema.sql from ysql_dump plus CSV exports of pg_stats,
> pg_stat_statements, pg_settings, index/table usage, reltuples and tablet counts)."
>
> Rules: read only files under SKILL_DIR and BUNDLE. The user is not available; where the
> skill says to ask, state the assumption and proceed. Do not connect to any database. Use
> Docker only if the skill instructs it, and never pull images. Create files only under
> RUN_DIR. When done, write RUN_DIR/final.md with both parts of the deliverable: first the
> whole review.md as you left it, then the chat message you would send. Reply with that path.

## Judge

Give this to one strong model, with the answer key, the bundle and the anonymised `final.md`
files. The answer key is a floor, not a ceiling: a human or synthetic key is never complete,
so the judge verifies extra findings against the bundle and credits them.

> You grade YugabyteDB schema reviews. Each review is labelled with a letter; you do not know
> which skill or model wrote it. Score substance, not style or length.
>
> **Key coverage.** For each defect in `defects`: found = 2 (right object, mechanism and a
> usable fix), partial = 1, missed = 0 (or contradicted).
>
> **Beyond the key.** The key is incomplete. For each finding not in the key, check it
> against the bundle files yourself. Credit +1 for each that is correct, material (it would
> change what the team does) and not a restatement of a key item, up to +8 per review. Record
> each in `extra_valid` with the evidence you checked. Wrong extra claims go in `extra_wrong`
> at -1 each.
>
> **Safety of recommendations.** For every recommended change (DDL or a described change),
> check whether applying it would lose something that holds today. Subtract 3 for each:
> - a PRIMARY KEY or UNIQUE guarantee, including a re-key that only enforces uniqueness on
>   a wider column set;
> - an ON CONFLICT target that an upsert in the workload relies on;
> - an access path, index order or coverage that a listed query uses, without a stated
>   replacement;
> - a partial-index predicate (for example `deleted_at IS NULL`);
> - DDL that would fail as written.
>
> List each under `safety_losses` with the statement and what it loses. A loss the review
> itself flags and corrects is not penalised.
>
> **Other penalties:** -2 per trap; -1 per invented number.
>
> Output JSON per letter: `defects`, `extra_valid`, `extra_wrong`, `safety_losses`, `traps`,
> `invented`, `key_score` (sum of defects), `extra_score`, `score` = key_score + extra_score
> - len(extra_wrong) - 3*len(safety_losses) - 2*len(traps) - invented, and `notes` quoting
> the text behind each non-zero item.
