# REVIEW.md — what to check when reviewing this repository

This repository contains **agent skills**, not application code. Every file under `skills/` is Markdown that a coding agent loads and acts on. The only executable code is `scripts/check_skills.py` and its tests.

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

## Conventions the checker already enforces — do not re-report

`python3 scripts/check_skills.py` runs in CI on every PR and covers: frontmatter validity and manifest/README sync, size budgets, reference-link resolution, code-fence matching, and exact version pins. If a finding is one the checker would catch, the checker will catch it. Report the rule being *wrong* if it is, not the individual instance.

Rules that regularly get misread:

- **Size warnings are expected, not failures.** `SKILL.md` over 400 lines and reference files over 600 lines warn; only over 500 lines errors. Several files sit in the warning band deliberately. CI does not run `--strict`, so warnings do not fail the build.
- **Version pins.** The rule forbids pinning a *dependency package whose newest release is the one you want*. It does not touch compatibility constraints (`>=`, `~>`, `^`) or **calendar-style** YugabyteDB releases such as `2024.2.1.0-b1`, in payloads or in commands. Two limits worth knowing before reporting one: the exemption is calendar-style only, so a 2.x release like `2.20.7.0-b1` in a pin-shaped position does warn and is meant to be baselined; and the scan deliberately covers fenced code, because that is where `pip install x==1.2.3` lives.
- **References one level deep** applies to *new* reference sets. The `yba-api` and `yba-terraform` sets cross-link each other today and are grandfathered; `yba-terraform` does so deliberately as a two-stage workflow.

Known exceptions live in `.skills-lint.json`, each with a stated reason, and are printed as `IGNORED` rather than hidden. `skills/explain_plan_analyzer/` is deliberately unregistered pending a register-vs-remove decision.

## What is worth flagging

- A factual claim that the upstream source contradicts, or that no source supports.
- Guidance in an always-loaded `SKILL.md` whose caveat lives only in an on-demand `references/` file — the agent may act on the rule without ever reading the caveat.
- A rule stated in one skill that another skill's content contradicts. These skills install together from one `npx skills add`, so an agent can hold two of them at once.
- A conflict guarded in one direction only. If skill A warns about replacing B's dependency, B needs the mirror.
- Anything that would make an agent take a destructive or irreversible action without surfacing the choice to the user.
- Documentation in `AGENTS.md` that no longer matches what `scripts/check_skills.py` does. These drift, and the doc is what contributors read.

## Conventions for skill content

`AGENTS.md` holds the full authoring guide ("Writing effective skills"). In short: `SKILL.md` is loaded whole when the skill triggers, so it carries the decisions and points at `references/` for detail; the description is the only text an agent sees when choosing a skill, so it names concrete triggers; instructions are imperative and give the reason rather than shouting ALWAYS/NEVER; nothing that goes stale (dates, exact versions) belongs in the text.
