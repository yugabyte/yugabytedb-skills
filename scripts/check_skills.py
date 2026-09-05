#!/usr/bin/env python3
"""Static checks for the yugabytedb-skills repository.

Standard library only. Run from the repository root:

    python3 scripts/check_skills.py                   # human-readable report
    python3 scripts/check_skills.py --format github   # GitHub Actions annotations
    python3 scripts/check_skills.py --strict          # warnings also fail
    python3 scripts/check_skills.py --fix-descriptions
        # rewrite marketplace.json descriptions from SKILL.md frontmatter

Exit status is 1 when any ERROR is found (or any WARN with --strict).

Known exceptions live in .skills-lint.json at the repo root:

    {"ignore": [{"rule": "MP001", "path": "skills/some_skill", "reason": "why"}]}

Each entry needs "rule" and "reason"; "path" (prefix of the finding's path)
and "match" (substring of the message) narrow it. Use "match" for rules that
report against shared files (AGENTS.md, marketplace.json, README.md).
Ignored findings are still printed (as IGNORED) so they stay visible.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# ---- budgets -----------------------------------------------------------------
SKILL_MD_MAX_LINES = 500      # hard limit: move detail into references/
SKILL_MD_WARN_LINES = 400     # approaching the limit
SKILL_MD_WARN_WORDS = 4000    # roughly 5k tokens; long lines hide size
REFERENCE_WARN_LINES = 600    # a reference file is loaded whole when used
NAME_MAX = 64                 # Agent Skills frontmatter limits
DESCRIPTION_MAX = 1024
DESCRIPTION_MIN = 60

KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
FENCE = re.compile(r"^\s*```")
REF_LINK = re.compile(r"\]\((references/[^)#\s]+)")
PLACEHOLDER = re.compile(r"\{\{[^}]*\}\}|\bTBD\b|\bFIXME\b|\bTODO\b|lorem ipsum", re.I)
USAGE_HINT = re.compile(r"\buse (when|this skill|for)\b|\btriggers?\b|\bwhen\b", re.I)

# Dependency pins we do not want in skills (they go stale). Product release
# numbers such as 2024.2.1.0-b1 inside example payloads are allowed.
PIN_PATTERNS = [
    re.compile(r"pip install\s+[\w\-\[\],]+==\s*\d"),
    re.compile(r"<version>\s*\d[^<]*</version>"),
    re.compile(r'^\s*[\w\-]+\s*=\s*"\d+\.\d+[^"]*"\s*(#.*)?$'),   # Cargo / Terraform
    re.compile(r":\d+\.\d+(\.\d+)?[\w.-]*-yb-\d"),                # Maven coordinate
    re.compile(r'"version"\s*:\s*"[\^~]?\d+\.\d+\.\d+'),           # package.json
]
PRODUCT_RELEASE = re.compile(r"\b20\d\d\.\d+(\.\d+){0,2}(-b\d+)?\b")
# IPv4 addresses and CIDR blocks look like versions to the patterns above.
IP_LIKE = re.compile(r'"\d{1,3}(\.\d{1,3}){3}(/\d{1,2})?"')


@dataclass
class Finding:
    rule: str
    level: str            # ERROR | WARN
    path: str
    line: int | None
    msg: str
    ignored: str | None = None


class Checker:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.findings: list[Finding] = []
        self.ignores = self._load_ignores()

    # ---- infrastructure ------------------------------------------------------
    def _load_ignores(self) -> list[dict]:
        cfg = self.root / ".skills-lint.json"
        if not cfg.exists():
            return []
        data = json.loads(cfg.read_text(encoding="utf-8"))
        entries = data.get("ignore", [])
        for e in entries:
            if not e.get("reason"):
                self.findings.append(Finding("CFG001", "ERROR", ".skills-lint.json", None,
                                             f"ignore entry {e!r} has no reason"))
        return entries

    def add(self, rule: str, level: str, path: Path | str, line: int | None, msg: str) -> None:
        rel = str(Path(path).relative_to(self.root)) if isinstance(path, Path) else path
        ignored = None
        for e in self.ignores:
            if e.get("rule") != rule:
                continue
            if e.get("path") and not rel.startswith(e["path"]):
                continue
            if e.get("match") and e["match"] not in msg:
                continue
            ignored = e["reason"]
            break
        self.findings.append(Finding(rule, level, rel, line, msg, ignored))

    @staticmethod
    def read(path: Path) -> str:
        return path.read_text(encoding="utf-8")

    @staticmethod
    def frontmatter(text: str) -> tuple[dict[str, str], dict[str, int]]:
        """Return (fields, line numbers). Only top-level `key: value` lines."""
        lines = text.split("\n")
        if not lines or lines[0].strip() != "---":
            return {}, {}
        fields: dict[str, str] = {}
        where: dict[str, int] = {}
        for i, line in enumerate(lines[1:], start=2):
            if line.strip() == "---":
                break
            m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
            if m:
                fields[m.group(1)] = m.group(2).strip()
                where[m.group(1)] = i
        return fields, where

    @staticmethod
    def outside_fences(text: str):
        """Yield (line_no, line) for lines that are not inside a code fence."""
        inside = False
        for i, line in enumerate(text.split("\n"), start=1):
            if FENCE.match(line):
                inside = not inside
                continue
            if not inside:
                yield i, line

    # ---- the checks ----------------------------------------------------------
    def run(self) -> None:
        skills_dir = self.root / "skills"
        manifest_path = self.root / ".claude-plugin" / "marketplace.json"
        readme = self.read(self.root / "README.md") if (self.root / "README.md").exists() else ""
        agents = self.read(self.root / "AGENTS.md") if (self.root / "AGENTS.md").exists() else ""

        manifest = json.loads(self.read(manifest_path)) if manifest_path.exists() else {"plugins": []}
        plugins = {p["name"]: p for p in manifest.get("plugins", [])}
        plugin_dirs = {Path(p["skills"][0]).name: p for p in manifest.get("plugins", []) if p.get("skills")}

        skill_dirs = sorted(d for d in skills_dir.iterdir() if d.is_dir()) if skills_dir.exists() else []
        names_seen: dict[str, str] = {}

        for d in skill_dirs:
            self.check_skill(d, plugin_dirs, readme, agents, names_seen)

        # manifest entries pointing at missing directories
        for p in manifest.get("plugins", []):
            for s in p.get("skills", []):
                if not (self.root / s).is_dir():
                    self.add("MP002", "ERROR", ".claude-plugin/marketplace.json", None,
                             f"plugin '{p['name']}' points at missing directory {s}")

    def check_skill(self, d: Path, plugin_dirs: dict, readme: str, agents: str, names_seen: dict) -> None:
        skill_md = d / "SKILL.md"
        rel_dir = f"skills/{d.name}"
        if not skill_md.exists():
            self.add("FM001", "ERROR", rel_dir, None, "SKILL.md is missing")
            return
        text = self.read(skill_md)
        lines = text.split("\n")
        fm, where = self.frontmatter(text)

        # -- frontmatter
        if not fm:
            self.add("FM001", "ERROR", skill_md, 1, "no YAML frontmatter block (--- ... ---)")
        name = fm.get("name")
        desc = fm.get("description")
        if fm and not name:
            self.add("FM002", "ERROR", skill_md, 1, "frontmatter has no 'name'")
        if fm and not desc:
            self.add("FM002", "ERROR", skill_md, 1, "frontmatter has no 'description'")
        if name:
            if name != d.name:
                self.add("FM003", "ERROR", skill_md, where.get("name"),
                         f"frontmatter name '{name}' != directory '{d.name}'")
            if not KEBAB.match(name):
                self.add("FM004", "ERROR", skill_md, where.get("name"),
                         f"name '{name}' is not kebab-case (a-z, 0-9, hyphens)")
            if len(name) > NAME_MAX:
                self.add("FM004", "ERROR", skill_md, where.get("name"),
                         f"name is {len(name)} chars (max {NAME_MAX})")
            if name in names_seen:
                self.add("DUP001", "ERROR", skill_md, where.get("name"),
                         f"name '{name}' is also used by {names_seen[name]}")
            names_seen[name] = rel_dir
        if desc:
            if len(desc) > DESCRIPTION_MAX:
                self.add("FM005", "ERROR", skill_md, where.get("description"),
                         f"description is {len(desc)} chars (max {DESCRIPTION_MAX})")
            elif len(desc) < DESCRIPTION_MIN:
                self.add("FM005", "WARN", skill_md, where.get("description"),
                         f"description is only {len(desc)} chars; say what it covers and when to use it")
            if not USAGE_HINT.search(desc):
                self.add("FM006", "WARN", skill_md, where.get("description"),
                         "description never says when to use the skill (no 'Use when' / 'Triggers on')")

        # -- manifest sync
        plugin = plugin_dirs.get(d.name)
        if plugin is None:
            self.add("MP001", "ERROR", rel_dir, None,
                     "not registered in .claude-plugin/marketplace.json")
        else:
            if name and plugin["name"] != name:
                self.add("MP003", "ERROR", ".claude-plugin/marketplace.json", None,
                         f"plugin name '{plugin['name']}' != frontmatter name '{name}' ({rel_dir})")
            if desc and plugin.get("description", "").strip() != desc:
                self.add("MP004", "ERROR", ".claude-plugin/marketplace.json", None,
                         f"description for '{plugin['name']}' differs from SKILL.md frontmatter "
                         f"(run with --fix-descriptions)")
            # -- README coverage (only for registered skills)
            if f"|`{plugin['name']}`|" not in readme.replace(" ", ""):
                self.add("RD001", "ERROR", "README.md", None,
                         f"no 'Available Skills' table row for `{plugin['name']}`")
            if f"-s {plugin['name']}" not in readme:
                self.add("RD002", "WARN", "README.md", None,
                         f"no 'npx skills add ... -s {plugin['name']}' install line")

        # -- AGENTS.md structure tree
        if agents and d.name + "/" not in agents:
            self.add("AG001", "WARN", "AGENTS.md", None,
                     f"repository structure does not mention {rel_dir}/")

        # -- size budget
        n_lines = len(text.splitlines())
        n_words = len(text.split())
        if n_lines > SKILL_MD_MAX_LINES:
            self.add("SZ001", "ERROR", skill_md, n_lines,
                     f"SKILL.md is {n_lines} lines (max {SKILL_MD_MAX_LINES}); move detail into references/")
        elif n_lines > SKILL_MD_WARN_LINES:
            self.add("SZ002", "WARN", skill_md, n_lines,
                     f"SKILL.md is {n_lines} lines; approaching the {SKILL_MD_MAX_LINES}-line limit")
        if n_words > SKILL_MD_WARN_WORDS:
            self.add("SZ004", "WARN", skill_md, None,
                     f"SKILL.md is ~{n_words} words (~{int(n_words * 1.3)} tokens); consider references/")

        # -- references: links resolve, files are linked
        refs_dir = d / "references"
        linked: set[str] = set()
        for i, line in enumerate(lines, start=1):
            for m in REF_LINK.finditer(line):
                target = m.group(1)
                linked.add(Path(target).name)
                if not (d / target).exists():
                    self.add("RF001", "ERROR", skill_md, i, f"link target does not exist: {target}")
        if refs_dir.is_dir():
            for f in sorted(refs_dir.glob("*.md")):
                if f.name not in linked and f.name not in text:
                    self.add("RF002", "WARN", f, None,
                             "reference file is never linked or mentioned from SKILL.md (agents will not find it)")
                self.check_markdown(f, is_skill_md=False)

        self.check_markdown(skill_md, is_skill_md=True, text=text)

    def check_markdown(self, path: Path, is_skill_md: bool, text: str | None = None) -> None:
        text = text if text is not None else self.read(path)
        lines = text.split("\n")
        n_lines = len(text.splitlines())
        fences = [i for i, l in enumerate(lines, start=1) if FENCE.match(l)]
        if len(fences) % 2:
            self.add("MD001", "ERROR", path, fences[-1], "unbalanced code fence (odd number of ``` lines)")
        if text and not text.endswith("\n"):
            self.add("MD002", "WARN", path, n_lines, "file does not end with a newline")
        if not is_skill_md and n_lines > REFERENCE_WARN_LINES:
            self.add("SZ003", "WARN", path, n_lines,
                     f"reference file is {n_lines} lines (soft limit {REFERENCE_WARN_LINES}); consider splitting")
        for i, line in self.outside_fences(text):
            if PLACEHOLDER.search(line):
                self.add("MD003", "WARN", path, i, f"placeholder text left in prose: {line.strip()[:80]}")
        for i, line in enumerate(lines, start=1):
            if "{{<" in line or PRODUCT_RELEASE.search(line) or IP_LIKE.search(line):
                continue
            if any(p.search(line) for p in PIN_PATTERNS):
                self.add("VP001", "WARN", path, i,
                         "hardcoded dependency version; name the coordinate and resolve the latest release "
                         f"from the registry at generation time: {line.strip()[:80]}")

    # ---- fixer ---------------------------------------------------------------
    def fix_descriptions(self) -> int:
        manifest_path = self.root / ".claude-plugin" / "marketplace.json"
        manifest = json.loads(self.read(manifest_path))
        changed = 0
        for p in manifest.get("plugins", []):
            if not p.get("skills"):
                continue
            skill_md = self.root / p["skills"][0] / "SKILL.md"
            if not skill_md.exists():
                continue
            fm, _ = self.frontmatter(self.read(skill_md))
            desc = fm.get("description")
            if desc and p.get("description", "").strip() != desc:
                p["description"] = desc
                changed += 1
        if changed:
            manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return changed


# ---- reporting -----------------------------------------------------------------
def report(findings: list[Finding], fmt: str) -> None:
    order = {"ERROR": 0, "WARN": 1}
    for f in sorted(findings, key=lambda x: (x.ignored is not None, order[x.level], x.path, x.line or 0)):
        loc = f"{f.path}:{f.line}" if f.line else f.path
        if fmt == "github" and not f.ignored:
            kind = "error" if f.level == "ERROR" else "warning"
            line_attr = f",line={f.line}" if f.line else ""
            print(f"::{kind} file={f.path}{line_attr},title={f.rule}::{f.msg}")
        else:
            level = f"IGNORED({f.level})" if f.ignored else f.level
            tail = f"  [ignored: {f.ignored}]" if f.ignored else ""
            print(f"{level:<14} {f.rule:<6} {loc}\n{'':14} {f.msg}{tail}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".", help="repository root (default: .)")
    ap.add_argument("--format", choices=["text", "github"], default="text")
    ap.add_argument("--strict", action="store_true", help="treat warnings as errors")
    ap.add_argument("--fix-descriptions", action="store_true",
                    help="rewrite marketplace.json descriptions from SKILL.md frontmatter, then re-check")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    checker = Checker(root)
    if args.fix_descriptions:
        n = checker.fix_descriptions()
        print(f"fix-descriptions: updated {n} manifest description(s)\n")
    checker.run()

    live = [f for f in checker.findings if not f.ignored]
    errors = sum(1 for f in live if f.level == "ERROR")
    warns = sum(1 for f in live if f.level == "WARN")
    ignored = len(checker.findings) - len(live)
    report(checker.findings, args.format)
    print(f"\n{errors} error(s), {warns} warning(s), {ignored} ignored")
    return 1 if errors or (args.strict and warns) else 0


if __name__ == "__main__":
    sys.exit(main())
