# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

YugabyteDB Agent Skills — a collection of reusable AI agent skills (delivered as Markdown files) for generating correct, optimized YugabyteDB code. Published to the Claude Plugin Marketplace and compatible with Claude Code, Cursor, GitHub Copilot, Windsurf, Gemini, and any tool supporting the [skills.sh](https://skills.sh) ecosystem.

**This is a documentation-only repository.** There is no build system, test suite, or application code.

## Repository Structure

```
skills/
  yugabytedb-ysql/
    SKILL.md                  # PostgreSQL-compatible YSQL API skill (port 5433)
    references/               # Detailed code examples (progressive disclosure)
      smart-drivers.md        # Connection examples for Python, Java, Go, Node.js
      retry-patterns.md       # Transaction retry code in Python and Java
  yugabytedb-ycql/
    SKILL.md                  # Cassandra-compatible YCQL API skill (port 9042)
.claude-plugin/
  marketplace.json            # Claude Plugin Marketplace metadata (version, plugin definitions)
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
npx skills add yugabyte/yugabytedb-skills              # All skills
npx skills add yugabyte/yugabytedb-skills -s yugabytedb-ysql   # YSQL only
npx skills add yugabyte/yugabytedb-skills -s yugabytedb-ycql   # YCQL only
```
