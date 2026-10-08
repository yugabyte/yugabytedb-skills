# REVIEW.md — what to check when reviewing this repository

This repository contains **agent skills**, not application code. Almost every file under `skills/` is Markdown that a coding agent loads and acts on. The executable code is `scripts/check_skills.py` and its tests, and the `yb-model` skill's engine (`skills/yb-model/scripts/`, with evals under `evals/yb-model/`); see [Reviewing the yb-model engine](#reviewing-the-yb-model-engine).

That changes what a defect is. A wrong package name here is not a typo — an agent will run `pip install` on it. A parameter spelled the way a different driver spells it produces code that connects but silently loses the behaviour the user asked for. Review for **what an agent will do after reading the text**, not for prose style.

## The highest-value thing to check: factual claims against upstream sources

Most of this repo is claims about other people's software. Check them against the source, not against another file in this repo — two files in the same change agreeing with each other proves nothing.

| Claim type | Authoritative source |
| --- | --- |
| Package / coordinate names | The registry itself: PyPI, Maven Central, npm, NuGet, crates.io, RubyGems, the Go module proxy |
| Driver connection parameters, defaults, behaviour | The matching page under `docs.yugabyte.com/preview/drivers-orms/` |
| Driver API shapes (builder methods, exception classes) | The driver's own source or API docs |
| YugabyteDB SQL/DDL behaviour | `docs.yugabyte.com` |

Two failure modes are worth naming because both have happened here:

- **Asserting an inference as documented fact.** If the docs describe a package but not a behaviour, say the mechanism rather than claiming the guarantee.
- **Reporting a claim as wrong from a single source that merely omits it.** Absence from one page is not disproof — check a second page before calling something a hallucination.

For a checker change, compare fixtures against the format specification or an independent parser, and test the resulting manifest writes and CLI diagnostics. The frontmatter decoder supports a YAML subset; skipped collections are not validated. Passing tests that compare the checker with its own output cannot establish that the skill platform reads the same value. The review-fix and checker checklists in [AGENTS.md](AGENTS.md#review-fixes-verify-the-behavior-and-every-place-that-teaches-it) record the regression cases and evidence requirements.

## Conventions the checker already enforces — do not re-report

`python3 scripts/check_skills.py` runs in CI on every PR and covers: frontmatter validity and manifest/README sync, size budgets, resolution of `references/…` paths **named in `SKILL.md`** as a markdown link or a backtick-wrapped mention, **outside fenced code** (a path that appears *only* inside a fence is not resolved), sibling pointers between files in the same `references/` directory, code-fence matching, and exact version pins. If a finding is one the checker would catch, the checker will catch it. Report the rule being *wrong* if it is, not the individual instance.

Rules that regularly get misread:

- **Size warnings are expected, not failures.** `SKILL.md` over 400 lines and reference files over 600 lines warn; only over 500 lines errors. Several files sit in the warning band deliberately. CI does not run `--strict`, so warnings do not fail the build.
- **Version pins.** The rule forbids pinning a *dependency package whose newest release is the one you want*. It does not touch compatibility constraints (`>=`, `~>`, `^`) or **calendar-style** YugabyteDB releases such as `2024.2.1.0-b1`, in payloads or in commands. Two limits worth knowing before reporting one: the exemption is calendar-style only, so a 2.x release like `2.20.7.0-b1` in a pin-shaped position does warn and is meant to be baselined; and the scan deliberately covers fenced code, because that is where `pip install x==1.2.3` lives.
- **References one level deep** applies to *new* reference sets. The `yba-api`, `yba-terraform` and `yb-k8s-operator` sets already point references at their siblings and are grandfathered; `yba-terraform` does so deliberately as a two-stage workflow. Those pointers are checked by `RF003`, so a broken one is a build failure, not a review finding. Links between different skills' reference sets do not exist here — do not report them as the grandfathered case.

Known exceptions live in `.skills-lint.json`, each with a stated reason, and are printed as `IGNORED` rather than hidden. `skills/explain_plan_analyzer/` is deliberately unregistered pending a register-vs-remove decision.

## What is worth flagging

- A factual claim that the upstream source contradicts, or that no source supports.
- Guidance in an always-loaded `SKILL.md` whose caveat lives only in an on-demand `references/` file — the agent may act on the rule without ever reading the caveat.
- A rule stated in one skill that another skill's content contradicts. These skills install together from one `npx skills add`, so an agent can hold two of them at once.
- A conflict guarded in one direction only. If skill A warns about replacing B's dependency, B needs the mirror.
- Anything that would make an agent take a destructive or irreversible action without surfacing the choice to the user.
- Documentation in `AGENTS.md` that no longer matches what `scripts/check_skills.py` does. These drift, and the doc is what contributors read.

## Reviewing the yb-model engine

`yb-model` runs a deterministic Python engine and the agent reports its output verbatim, so an engine bug reaches the user as a confident finding. Beyond the points above, check:

- **The report says only what the code checked.** A sentence in `report.py`, `SKILL.md` or `references/engine.md` that claims more than the code does (a check that covers fewer cases, a guarantee that depends on an input that may be missing) is a defect.
- **Missing inputs degrade honestly.** Without pg_settings, release facts, statistics or a workload, a rule must be muted, weakened with a caveat, or made conditional; never decided by a default the customer may not run. The same holds for replay and probes: under assumed settings they may dispute a finding, not remove it.
- **No finding without its basis.** Rules that claim planner or execution behaviour cite a regress test (`check-rule-refs.py` enforces this); the rest must be arithmetic on the bundle.
- **Recommended DDL never loses a guarantee.** Uniqueness, foreign keys, ON CONFLICT targets and plans are checked by the safety pass; a change that bypasses it, or DDL an agent would run on the customer's cluster, is a defect.
- **Recommended DDL runs on YSQL.** No `DROP INDEX CONCURRENTLY` (the parser rejects it); a constraint's index is dropped with `ALTER TABLE ... DROP CONSTRAINT`; replacements are built with `schema.index_sql` so UNIQUE, INCLUDE, the predicate and SPLIT survive. A template that formats `CREATE INDEX` by hand is a defect, and so is one that prints a bare name or the `text_of` form of an expression (quoted mixed-case names lose their quotes; the `Metamorphic` variant tests catch it).
- **No destructive advice in prose.** A fix text that says to drop or rebuild something with no DDL behind it escapes the safety pass. It must never target a primary key, a unique index or a column an index references.
- **Statistics are read for what they show on YSQL.** Correlation reflects primary-key order, not insert order; aggregated per-node pg_stats can repeat a column. A rule that treats them otherwise is a defect.
- **Pitfalls stay generic and actionable.** An entry in `references/pitfalls.md` says what the reviewer sees, why, and what to do, and names no customer, table or value. A pitfall the engine can detect belongs in the engine (with a test), with the entry saying what the engine now reports.
- **The chat message comes from the engine.** `chat.md` is rendered from `review.json`; skill text that asks the model to summarise findings in its own words reintroduces the errors this removed.
- **The `AGENTS.md` rules for this skill:** no version-specific caches or generated bulk data committed, no release facts in the rules, no customer data, tests pass without the caches (`python3 -m unittest discover -s skills/yb-model/scripts -p 'test_*.py'`).

## Conventions for skill content

`AGENTS.md` holds the full authoring guide ("Writing effective skills"). In short: `SKILL.md` is loaded whole when the skill triggers, so it carries the decisions and points at `references/` for detail; the description is the only text an agent sees when choosing a skill, so it names concrete triggers; instructions are imperative and give the reason rather than shouting ALWAYS/NEVER; nothing that goes stale (dates, exact versions) belongs in the text.
