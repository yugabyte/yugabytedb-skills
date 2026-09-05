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

`scripts/check_skills.py` (standard library only) validates every skill and runs in CI on every pull request (`.github/workflows/skills-lint.yml`). Run it locally from the repo root before opening a PR:

```bash
python3 scripts/check_skills.py                     # report; exit 1 on errors
python3 scripts/check_skills.py --strict            # warnings fail too
python3 scripts/check_skills.py --fix-descriptions  # sync marketplace.json descriptions from SKILL.md
```

What it enforces (errors fail CI, warnings annotate the PR):

| Group | Rules |
| --- | --- |
| Frontmatter | present; `name` and `description` present; `name` equals the directory name, is kebab-case and at most 64 chars; `description` at most 1024 chars (warn if under 60 chars or it never says when to use the skill) |
| Manifest | every `skills/*/` directory is registered in `marketplace.json`; every entry's directory exists; manifest `name` and `description` equal the frontmatter |
| README | every registered skill has an Available Skills row (error) and an `npx skills add … -s <name>` line (warn) |
| Size | `SKILL.md` over 500 lines is an error; over 400 lines or 4000 words is a warning; a reference file over 600 lines is a warning |
| References | every `references/…` link resolves (error); every reference file is linked or mentioned from `SKILL.md` (warn) |
| Markdown | balanced code fences (error); trailing newline; `{{…}}`, TODO, TBD, FIXME outside code (warn) |
| Versions | pinned dependency versions (`pip install x==1.2.3`, `<version>1.2.3</version>`, `crate = "1.2.3"`) are warned — name the coordinate and resolve the latest release at generation time. YugabyteDB product release numbers in example payloads are allowed |
| Docs | the structure tree in this file mentions every skill directory (warn) |

Known exceptions live in `.skills-lint.json`. Every entry needs a `reason`, and ignored findings are still printed as `IGNORED` so they stay visible.