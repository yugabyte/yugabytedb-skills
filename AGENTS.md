# AGENTS.md

This file provides guidance to AI agents when working with yugabyteDB.

## Project Overview

YugabyteDB Agent Skills — a collection of reusable AI agent skills (delivered as Markdown files) for deploying, managing and developing for YugabyteDB, a Postgres-compatible distributed SQL database. Published to the Claude Plugin Marketplace and compatible with Claude Code, Cursor, GitHub Copilot, Windsurf, Gemini, and any tool supporting the [skills.sh](https://skills.sh) ecosystem.

**This is a documentation-only repository.** There is no build system or application code. The only automation is `scripts/check_skills.py`, a static checker for the skill files that runs in CI — see [Static checks](#static-checks).

## Repository Structure

```
skills/
  ysql/
    SKILL.md                  # PostgreSQL-compatible YSQL API skill (port 5433)
    references/               # Detailed code examples (progressive disclosure)
      smart-drivers.md        # Connection examples for Python, Java, Go, Node.js
      retry-patterns.md       # Transaction retry code in Python and Java
  ycql/
    SKILL.md                  # Cassandra-compatible YCQL API skill (port 9042)
  yb-k8s-operator/
    SKILL.md                  # YugabyteDB Kubernetes Operator skill
    references/
      crd-examples.md         # Example Custom Resources
      kubeconfig-secrets.md   # Guidance for kubeconfig Secrets to allow Operator to manage remote clusters
      multi-cluster.md        # Guidance and examples specific to multi-cluster topologies with service meshes (e.g. Istio, Cilium, MCS)
      workflows.md            # Step-by-step instructions for common deployment scenarios
  yba-api/
    SKILL.md                  # YugabyteDB Anywhere REST API skill (auth, v1 vs v2, async tasks)
    references/
      python-client.md        # Minimal Python wrapper plus splat-style usage patterns
      powershell-client.md    # Standalone PowerShell wrapper (no module install required)
      recipes.md              # Endpoint cheat-sheet: providers, releases, v1/v2 universe creation (cloud + k8s), storage configs (S3 multi-region + proxy), telemetry providers, health checks + alerts, runtime config (all scopes), backups, tasks
      prometheus.md           # Querying the Prometheus instance bundled with YBA on :9090 — useful PromQL for ops/sec, latency, container CPU/memory, node-exporter CPU, tablet leaders, xCluster lag, plus Python/PowerShell helpers
  yb-rag-langchain/
    SKILL.md                  # RAG / semantic search / hybrid search on YugabyteDB with LangChain (PGVectorStore, ybhnsw, hybrid search, metadata filtering)
  yba-terraform/
    SKILL.md                  # YBA Terraform provider (yugabyte/yba): two-stage workflow, provider config, prereqs, anti-patterns
    references/
      prereqs-and-network.md   # YBA host/DB-node requirements + ports, and aws/gcloud/az CLI commands to discover VPC/subnet/SG/region info
      cloud-iam-setup.md       # Upstream aws/google/azurerm Terraform creating YBA's IAM user / service account / service principal with the docs' delegated permissions
      install-bootstrap.md     # Stage 1: yba_installer (install over SSH) + yba_customer_resource (first customer, outputs api_token)
      providers-universe.md    # Stage 2: yba_aws/gcp/azure/onprem_provider, *_storage_config, yba_universe, yba_backup/backup_schedule/restore
.claude-plugin/
  marketplace.json            # Claude Plugin Marketplace metadata (version, plugin definitions)
.skills-lint.json             # Known exceptions for the static checks (each needs a reason)
scripts/
  check_skills.py             # Static checks for skills (frontmatter, manifest/README sync, size, links, version pins)
  test_check_skills.py        # Tests for the checker (unittest, standard library only)
.github/workflows/
  skills-lint.yml             # Runs scripts/check_skills.py on every pull request and on main
```

## Skill File Format

Each skill folder has a `SKILL.md` with YAML frontmatter as the entry point. Skills can also include `references/` for detailed code examples:

```markdown
---
name: skill-name
description: One-line description used for skill discovery and matching.
---

# Skill content (Markdown)
```

The `name` and `description` fields in frontmatter must stay in sync with the corresponding entry in `.claude-plugin/marketplace.json`.

## Adding or Modifying Skills

- Each skill lives in its own directory under `skills/` with `SKILL.md` as the entry point.
- Use `references/` for detailed code examples that SKILL.md points to (progressive disclosure).
- When adding a new skill, also register it in `.claude-plugin/marketplace.json` under the `plugins` array.
- Skills should include: anti-patterns with alternatives, schema/design patterns with SQL/code examples, and operational guidance.
- Keep skills self-contained — each skill folder should be independently useful without requiring the other.
- Before writing or changing skill content, read [Writing effective skills](#writing-effective-skills) below.

## Writing effective skills

The [static checks](#static-checks) catch structural problems. The rules here cover what a checker cannot: whether the skill changes what the agent produces. They condense Anthropic's skill-authoring guide, the Agent Skills specification and the `skill-creator` skill (links at the end of this section), plus the conventions of this repository.

### Start from a failure, not from a topic

1. Run a representative task (design a schema, wire a driver, plan a deployment) with a capable model and **no** skill loaded. Record what it gets wrong or leaves out.
2. Write only the content that fixes those failures. The model already knows PostgreSQL, Cassandra, Kubernetes, Terraform and the language ecosystems — document what is specific to YugabyteDB.
3. Re-run the same task with the skill loaded and compare. Keep the task prompts: they are the skill's regression tests. Repeat with every model the skill is expected to serve — guidance that is enough for a large model can be too thin for a small one.

### Description — the only text the agent sees before choosing a skill

- Say what the skill does **and** when to use it, in the third person. Pattern used here: `<what it covers>. Use when <situations>. Triggers on <words the user is likely to type>.`
- List concrete triggers: product names, commands, file types, error codes, ports (`5433`, `9042`), API and resource names. The agent matches the request against every installed skill's description; a vague description means the skill is never loaded. Under-triggering is the common failure — list more triggers rather than fewer.
- Describe the user's situation, not the skill's structure or history.
- Keep the existing noun-phrase names (`ysql`, `yb-k8s-operator`, `yba-api`); consistency across the collection matters more than the form.
- `.claude-plugin/marketplace.json` must carry the identical description (`python3 scripts/check_skills.py --fix-descriptions`).

### Body — a table of contents, not a manual

- `SKILL.md` is loaded whole once the skill triggers, so every line competes with the user's task. The checker warns at 400 lines and fails at 500; most skills should be shorter.
- Lead with the decisions that differ from the upstream technology (YSQL: sharding, smart drivers, retries; the operator: CRD shapes, multi-cluster networking). No introductions, no definitions the model already has.
- Move long code, per-language variants and endpoint catalogues into `references/<topic>.md`, linked directly from `SKILL.md`. One level deep only — a reference file must not point to another reference file. Put a contents list at the top of any reference longer than about 100 lines so a partial read still shows its scope.
- Split references by domain (`smart-drivers.md`, `retry-patterns.md`), not by size, so the agent loads only the file the task needs.

### Instructions the agent can act on

- Imperative sentences. One term per concept throughout (`tserver`, not `tserver` / `node` / `server` interchangeably).
- Give the reason in one clause instead of capitalised ALWAYS / NEVER; the reason lets the agent handle the case the rule did not foresee.
- Match specificity to risk. Fragile or irreversible steps (DDL migrations, upgrades, IAM setup) get one exact command and "do not add flags". Design choices get one default plus the condition for the alternative. Do not list five valid libraries — name one and say when to use another.
- Show input → output examples where style matters (schema shapes, connection strings, report layouts). One concrete example beats a paragraph of description.
- For multi-step procedures, give a numbered checklist the agent can copy and tick off, and end it with a verification step (`EXPLAIN (ANALYZE, DIST)`, `kubectl get …`, `terraform plan`) so mistakes surface before the user sees them.
- Keep the anti-patterns table — `what people do | why it breaks on YugabyteDB | do this instead`. It is the highest-value section in most skills here.

### Content that goes stale

- No pinned dependency versions. Name the package or coordinate (`psycopg-yugabytedb`, `com.yugabyte:r2dbc-postgresql`) and tell the agent to resolve that package's latest release from its registry at generation time. Do not describe the version format: the YugabyteDB drivers are separate packages, and registries differ — PyPI, NuGet and RubyGems publish plain version numbers, while Maven, npm, Go and crates.io use the upstream version plus a `-yb-N` qualifier — so a rule that filters on a suffix rejects valid releases. YugabyteDB release numbers in example payloads are fine.
- No dates or "before / after version X" branches in the main text. Superseded guidance goes into a collapsed "Old patterns" block or is deleted.
- Anything the agent must look up live (technical advisories, release notes, docs pages) gets the URL and an instruction to fetch it — never a memorised copy.

### Before opening the PR

1. `python3 scripts/check_skills.py --strict` passes.
2. The description has been tested: a fresh session picks this skill for the target request and ignores it for a neighbouring one (YSQL vs YCQL, YBA API vs Terraform).
3. The regression task from "Start from a failure" produces better output with the skill than without, and the PR says how it was checked.

Sources: [Skill authoring best practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices) (Anthropic), [Agent Skills specification](https://agentskills.io/specification), [skill-creator](https://github.com/anthropics/skills/tree/main/skills/skill-creator) (anthropics/skills).

## Installation Commands (for reference)

```bash
npx skills add yugabyte/yugabytedb-skills                    # All skills
npx skills add yugabyte/yugabytedb-skills -s ysql            # YSQL only
npx skills add yugabyte/yugabytedb-skills -s ycql            # YCQL only
npx skills add yugabyte/yugabytedb-skills -s yb-k8s-operator # Kubernetes Operator skill only
npx skills add yugabyte/yugabytedb-skills -s yba-api         # YBA REST API skill only
npx skills add yugabyte/yugabytedb-skills -s yb-rag-langchain # RAG / LangChain skill only
npx skills add yugabyte/yugabytedb-skills -s yba-terraform   # YBA Terraform provider skill only
```

## Static checks

`scripts/check_skills.py` (standard library only) validates every skill and runs in CI on every pull request (`.github/workflows/skills-lint.yml`), together with its tests in `scripts/test_check_skills.py`. Run both locally from the repo root before opening a PR:

```bash
python3 scripts/check_skills.py                     # report; exit 1 on errors
python3 scripts/check_skills.py --strict            # warnings fail too
python3 scripts/check_skills.py --fix-descriptions  # sync marketplace.json descriptions from SKILL.md
python3 -m unittest discover -s scripts -p 'test_*.py'   # the checker's own tests (run after changing it)
```

What it enforces (errors fail CI, warnings annotate the PR):

| Group | Rules |
| --- | --- |
| Frontmatter | present and closed with `---`; values decoded as YAML scalars (plain, quoted, `|` / `>` block) — anything else is an error; `name` and `description` present; `name` equals the directory name, is kebab-case and at most 64 chars; `description` at most 1024 chars (warn if under 60 chars or it never says when to use the skill) |
| Manifest | every `skills/*/` directory is registered in `marketplace.json`; every entry's directory exists; manifest `name` and `description` equal the frontmatter |
| README | every registered skill has an Available Skills row (error) and an `npx skills add … -s <name>` line (warn) |
| Size | `SKILL.md` over 500 lines is an error; over 400 lines or 4000 words is a warning; a reference file over 600 lines is a warning |
| References | every `references/…` link resolves (error); every reference file is linked or mentioned from `SKILL.md` (warn) |
| Markdown | unclosed code fence (error; CommonMark matching — a fence is indented at most 3 spaces relative to its list item, and a closing fence uses the same character and at least the opening length, so a four-backtick block may contain three-backtick examples and four-space-indented code is not a fence); trailing newline; `{{…}}`, TODO, TBD, FIXME outside code (warn) |
| Versions | pinned dependency versions (`pip install x==1.2.3`, `<version>1.2.3</version>`, `crate = "1.2.3"`) are warned — name the coordinate and resolve the latest release at generation time. YugabyteDB product release numbers in example payloads are allowed |
| Docs | the structure tree in this file mentions every skill directory (warn) |

Known exceptions live in `.skills-lint.json`. Every entry needs a `reason`, and ignored findings are still printed as `IGNORED` so they stay visible.