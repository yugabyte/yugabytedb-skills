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
# Frontmatter: `key: value` or `key:` at column 0 (YAML requires whitespace after the colon).
KEY_LINE = re.compile(r"^([A-Za-z_][\w-]*):(?:[ \t]+(.*))?$")
BLOCK_HEADER = re.compile(r"^([|>])([1-9+-]{0,2})[ \t]*(#.*)?$")
# A flow collection is a structured value this subset does not decode — the same
# situation as a block mapping/sequence, so it is skipped rather than reported.
# `allowed-tools: [Read, Grep]` is the natural YAML for a field the spec defines.
FLOW_COLLECTION_START = ("[", "{")
# A block sequence entry. YAML lets these sit at the parent key's indentation,
# so they are part of the value even when they start at column 0.
SEQUENCE_ITEM = re.compile(r"^- (?!-)|^-$")
# Anchors, aliases, tags and reserved indicators: exotic enough in frontmatter
# that seeing one almost certainly means a mistake.
UNSUPPORTED_SCALAR_START = ("&", "*", "!", "%", "@", "`")
# Top-level frontmatter fields the Agent Skills specification defines. A key
# outside this set is either a typo or body text absorbed because the block is
# missing its closing --- (FM008); per the spec, extra data belongs in metadata.
SPEC_FIELDS = frozenset({"name", "description", "license", "compatibility",
                         "allowed-tools", "metadata"})
DOUBLE_ESCAPES = {
    # Escapes YAML defines for double-quoted scalars. The last four are written
    # as \u escapes on purpose: as literal characters they are invisible in a
    # diff and unreviewable.
    "0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n",
    "v": "\v", "f": "\f", "r": "\r", "e": "\x1b", " ": " ", '"': '"',
    "/": "/", "\\": "\\",
    "N": "\u0085",  # NEL, next line
    "_": "\u00a0",  # NBSP, non-breaking space
    "L": "\u2028",  # LS, line separator
    "P": "\u2029",  # PS, paragraph separator
}
# CommonMark fenced code: a run of 3+ backticks or tildes, then an optional info string.
# Indentation is checked separately (at most 3 spaces relative to the enclosing list item).
FENCE_OPEN = re.compile(r"^ *(`{3,}|~{3,})(.*)$")
# List item marker with its content indent: leading spaces, marker, spaces before the content.
LIST_ITEM = re.compile(r"^( *)([-*+]|\d{1,9}[.)])( +)(?=\S)")
# A markdown link to a reference, with or without the "./" a relative link may
# carry. Both forms must resolve and both must count as linking the file.
REF_LINK = re.compile(r"\]\(\.?/?(references/[^)#\s]+)")
# A backtick-wrapped reference path, e.g. `references/foo.md` in the "This skill
# includes" list. RF001 checks these resolve too: a bare mention of a renamed
# file tells the agent up front to open a path that does not exist.
REF_MENTION = re.compile(r"`(references/[^`\s]+\.md)`")
# A bare `<file>.md` naming a file beside the one it appears in, as a markdown
# link or a backtick mention. Used only inside references/, where a pointer to a
# sibling is invisible from SKILL.md and so escapes RF001.
SIBLING_REF = re.compile(r"\]\(((?:\./)?[\w-]+\.md)(?:#[^\s)]*)?\)|`([\w-]+\.md)`")
PLACEHOLDER = re.compile(r"\{\{[^}]*\}\}|\bTBD\b|\bFIXME\b|\bTODO\b|lorem ipsum", re.I)
USAGE_HINT = re.compile(r"\buse (when|this skill|for)\b|\btriggers?\b|\bwhen\b", re.I)

# Dependency pins we do not want in skills (they go stale). Product release
# numbers are exempted by PRODUCT_RELEASE below, but only calendar-style ones
# (2024.2.1.0-b1) and anywhere on the line, not just in example payloads — a 2.x
# release in a pin-shaped position still warns and is meant to be baselined.
PIN_PATTERNS = [
    re.compile(r"pip install\s+[\w\-\[\],]+==\s*\d"),
    re.compile(r"<version>\s*\d[^<]*</version>"),
    re.compile(r'^\s*[\w\-]+\s*=\s*"\d+\.\d+[^"]*"\s*(#.*)?$'),   # Cargo / Terraform
    re.compile(r":\d+\.\d+(\.\d+)?[\w.-]*-yb-\d"),                # Maven coordinate
    # package.json: an exact pin only. `^`/`~` are compatibility ranges, which
    # this rule deliberately allows, so they must not match here.
    re.compile(r'"version"\s*:\s*"\d+\.\d+\.\d+'),
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
        self._manifest_cache: dict | None = None
        self._manifest_failed = False

    # ---- infrastructure ------------------------------------------------------
    def _load_ignores(self) -> list[dict]:
        cfg = self.root / ".skills-lint.json"
        if not cfg.exists():
            return []
        def invalid(message: str) -> None:
            self.findings.append(Finding("CFG001", "ERROR", ".skills-lint.json", None, message))

        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            invalid(f"cannot read ignore configuration: {exc}")
            return []
        if not isinstance(data, dict) or not isinstance(data.get("ignore", []), list):
            invalid("ignore configuration must be an object with an 'ignore' array")
            return []
        valid = []
        for e in data.get("ignore", []):
            if not isinstance(e, dict):
                invalid(f"ignore entry {e!r} must be an object")
                continue
            ok = True
            for key in ("rule", "reason"):
                if not isinstance(e.get(key), str) or not e[key].strip():
                    invalid(f"ignore entry {e!r} needs a non-empty string '{key}'")
                    ok = False
            for key in ("path", "match"):
                if key in e and not isinstance(e[key], str):
                    invalid(f"ignore entry {e!r}: '{key}' must be a string")
                    ok = False
            # An invalid entry must not reach add(): it cannot justify suppressing
            # a finding, and one with no rule would match nothing anyway.
            if ok:
                valid.append(e)
        return valid

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
            ignored = e.get("reason")
            break
        self.findings.append(Finding(rule, level, rel, line, msg, ignored))

    @staticmethod
    def read(path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def _load_manifest(self) -> dict:
        """Validate the JSON shapes shared by the checker and the writer."""
        if self._manifest_cache is not None:
            return self._manifest_cache
        path = self.root / ".claude-plugin" / "marketplace.json"

        def invalid(message: str) -> None:
            self._manifest_failed = True
            self.add("MP006", "ERROR", ".claude-plugin/marketplace.json", None, message)

        try:
            data = json.loads(self.read(path)) if path.exists() else {"plugins": []}
        except (ValueError, OSError) as exc:
            invalid(f"cannot read marketplace manifest: {exc}")
            data = {"plugins": []}
        if not isinstance(data, dict) or not isinstance(data.get("plugins", []), list):
            invalid("marketplace manifest must be an object with a 'plugins' array")
            data = {"plugins": []}
        valid = []
        registered: set[Path] = set()
        for index, plugin in enumerate(data.get("plugins", [])):
            if not isinstance(plugin, dict):
                invalid(f"plugin entry {index} must be an object")
                continue
            if any(key in plugin and not isinstance(plugin[key], str) for key in ("name", "description")):
                invalid(f"plugin entry {index}: name and description must be strings when present")
                continue
            paths = plugin.get("skills")
            if paths is not None and (not isinstance(paths, list)
                                      or any(not isinstance(p, str) or not p.strip() for p in paths)):
                invalid(f"plugin entry {index}: skills must be an array of non-empty paths or null")
                continue
            for entry in paths or []:
                resolved = (self.root / entry).resolve()
                if resolved in registered:
                    invalid(f"skill directory is registered more than once: {entry}")
                registered.add(resolved)
            valid.append(plugin)
        self._manifest_cache = dict(data, plugins=valid)
        return self._manifest_cache

    # ---- frontmatter: a YAML subset, standard library only -------------------
    @staticmethod
    def frontmatter(text: str) -> tuple[dict[str, str | None], dict[str, int], list[tuple[str, int | None, str]]]:
        """Parse the leading `--- ... ---` block.

        Returns (fields, key line numbers, problems). Values are decoded the way a
        YAML parser sees them: plain scalars (comment stripped, continuation lines
        folded), 'single' and "double" quoted scalars, and `|` / `>` block scalars
        with `-` / `+` chomping. Nested mappings and sequences are skipped (value
        None). Problems are (rule, line, message): FM001 when the block has no
        closing `---`; FM007 for a line or value this subset cannot decode and
        for a duplicate key (YAML rejects those); FM008 for a top-level key the
        Agent Skills specification does not define.

        The block runs from the opener to the FIRST line that is exactly `---`,
        which is how frontmatter is delimited — blank lines inside it are legal
        YAML and are kept. Nothing structural distinguishes a block whose author
        forgot the terminator (so body text was absorbed) from one that really
        does carry those keys: both are `key`, `key`, indented, blank, `key`,
        `---`. Rather than guess from shape, this parser matches the delimiters
        the way the platform's own parser does, and FM008 catches the absorbed
        case semantically — body lines like `Status: ready` are not spec fields.

        Callers must not act on values from a block reporting FM001 or FM007 —
        those are the decode failures, and the values are then unreliable. FM008
        is not one: an unknown key leaves `name` and `description` decoded, so it
        must not stand down the checks that read them.
        """
        lines = text.split("\n")
        # Both delimiters sit at column 0; an indented `---` is content, not a
        # delimiter, so rstrip (not strip) is the right comparison here.
        if not lines or lines[0].rstrip() != "---":
            return {}, {}, []
        # The terminator, and the bound every sub-parser must respect so that a
        # block scalar or nested mapping cannot run past it into the body.
        term = next((j for j in range(1, len(lines)) if lines[j].rstrip() == "---"), None)
        end = term if term is not None else len(lines)
        fields: dict[str, str | None] = {}
        where: dict[str, int] = {}
        problems: list[tuple[str, int | None, str]] = []
        first_bad: tuple[int, str] | None = None
        i = 1
        while i < end:
            stripped = lines[i].strip()
            if not stripped or stripped.startswith("#"):
                # Blank and comment lines are legal inside the block. Treating a
                # comment as undecodable would set decode_failed and stand down
                # the value checks over input YAML reads without complaint.
                i += 1
                continue
            m = KEY_LINE.match(lines[i])
            if not m:
                # Flag it, but keep scanning: a malformed line inside an otherwise
                # properly terminated block should not stop the rest being read.
                if first_bad is None:
                    first_bad = (i + 1, stripped[:60])
                i += 1
                continue
            key = m.group(1)
            if key in fields:
                # YAML rejects duplicate mapping keys; silently taking the last one
                # would let FM003/MP003 judge a value the platform may not use.
                problems.append(("FM007", i + 1, f"duplicate frontmatter key '{key}'"))
            where[key] = i + 1
            value, i, err = Checker._yaml_value(lines, i, m.group(2), end)
            if err:
                problems.append(("FM007", where[key], f"'{key}': {err}"))
            fields[key] = value
        if first_bad is not None:
            problems.append(("FM007", first_bad[0], f"cannot parse frontmatter line: {first_bad[1]}"))
        for key, line_no in where.items():
            if key not in SPEC_FIELDS:
                problems.append(("FM008", line_no,
                                 f"'{key}' is not an Agent Skills frontmatter field "
                                 f"(expected one of: {', '.join(sorted(SPEC_FIELDS))}). "
                                 f"If this is body text, the block is missing its closing ---"))
        if term is None:
            problems.append(("FM001", len(text.splitlines()) or 1,
                             "frontmatter block is not closed (no terminating ---)"))
        return fields, where, problems

    @staticmethod
    def _yaml_value(lines: list[str], i: int, rest: str | None,
                    end: int | None = None) -> tuple[str | None, int, str | None]:
        """Decode the value after `key:` on lines[i]. Returns (value, next line index, error).

        `end` bounds the scan at the frontmatter terminator so a nested mapping,
        block scalar or continuation cannot run past it into the body.
        """
        end = len(lines) if end is None else end
        rest = (rest or "").lstrip()
        if rest.startswith(("'", '"')):
            # Trailing whitespace can be escaped inside a continued quotation.
            return Checker._quoted_scalar(lines, i, rest, end)
        rest = rest.rstrip()
        if not rest or rest.startswith("#"):
            first = i + 1
            while first < end and (not lines[first].strip() or lines[first].lstrip().startswith("#")):
                first += 1
            if first < end and lines[first].startswith(" "):
                content = lines[first].lstrip()
                quoted_key = False
                if content.startswith(("'", '"')):
                    close = Checker._closing_quote(content, content[0])
                    quoted_key = close is not None and bool(re.match(r":(?:[ \t]|$)", content[close + 1:]))
                structured = (SEQUENCE_ITEM.match(content) or content.startswith(FLOW_COLLECTION_START)
                              or quoted_key or (re.search(r":(?:[ \t]|$)", content)
                                                and not content.startswith(("'", '"'))))
                if not structured:
                    # A scalar can start below its key; indentation alone does
                    # not make the value a mapping or sequence.
                    return Checker._yaml_value(lines, first, content, end)
            # Null, or a nested mapping / sequence on the following lines. Blank
            # lines inside the block are legal. A block sequence may sit at the
            # key's own indentation ("allowed-tools:" then "- Read" at column 0),
            # which YAML allows and which must be skipped like any other
            # structured value rather than failing KEY_LINE as undecodable.
            j, nested = i + 1, False
            while j < end and (not lines[j].strip() or lines[j][0] in " \t"
                               or SEQUENCE_ITEM.match(lines[j])):
                nested = nested or bool(lines[j].strip())
                j += 1
            return (None if nested else ""), j, None
        m = BLOCK_HEADER.match(rest)
        if m:
            return Checker._block_scalar(lines, i, m.group(1), m.group(2), end)
        if rest.startswith(("|", ">")):
            return None, i + 1, "invalid block scalar header"
        if rest[0] in FLOW_COLLECTION_START:
            # Skip it, and any continuation lines, the way a block collection is
            # skipped: the value is structured, not undecodable.
            j = i + 1
            while j < end and (not lines[j].strip() or lines[j][0] in " \t"):
                j += 1
            return None, j, None
        if rest[0] in UNSUPPORTED_SCALAR_START or rest[:2] in ("- ", "? ", ": ") or rest in ("-", "?", ":"):
            return None, i + 1, f"unsupported YAML syntax: {rest[:40]}"
        value = Checker._strip_comment(rest)
        j = i + 1
        comment_ended = value != rest
        while j < end and not comment_ended:
            next_line = j
            while next_line < end and not lines[next_line].strip():
                next_line += 1
            if (next_line == end or lines[next_line][0] not in " \t"
                    or lines[next_line].strip().startswith("#")):
                break
            # One physical break folds to a space; empty lines preserve paragraph breaks.
            value += ("\n" * (next_line - j) if next_line > j else " ")
            continuation = lines[next_line].strip()
            decoded = Checker._strip_comment(continuation)
            value += decoded
            comment_ended = decoded != continuation
            j = next_line + 1
        if re.search(r":(?:[ \t]|$)", value):
            return None, j, "plain scalar contains ': ' (YAML reads it as a nested mapping); quote the value"
        # Avoid turning implicitly typed YAML into a different string in the
        # manifest. Quote ambiguous tokens rather than choosing a YAML schema.
        if (value.lower() in {"null", "~", "true", "false", "yes", "no", "on", "off",
                              ".nan", ".inf", "+.inf", "-.inf"}
                or re.fullmatch(r"[+-]?(?:[0-9][0-9_a-fA-FxXoObBeE.+:/ -]*|\.[0-9][0-9_eE+-]*)", value)):
            return None, j, "plain scalar may have a non-string YAML type; quote the value"
        return value, j, None

    @staticmethod
    def _strip_comment(s: str) -> str:
        m = re.search(r"(^|[ \t])#", s)
        return (s[: m.start()] if m else s).rstrip()

    @staticmethod
    def _quoted_scalar(lines: list[str], i: int, rest: str,
                       end: int | None = None) -> tuple[str | None, int, str | None]:
        end = len(lines) if end is None else end
        q, buf, j = rest[0], rest, i
        while True:
            close = Checker._closing_quote(buf, q)
            if close is not None:
                break
            j += 1
            if j >= end:
                return None, i + 1, "unterminated quoted string"
            buf += "\n" + lines[j]
        tail = buf[close + 1:].strip()
        if tail and not tail.startswith("#"):
            return None, j + 1, f"unexpected text after closing quote: {tail[:40]}"
        body = Checker._fold_quoted(buf[1:close], q)
        if q == "'":
            return body.replace("''", "'"), j + 1, None
        value, err = Checker._decode_double_quoted(body)
        return value, j + 1, err

    @staticmethod
    def _fold_quoted(body: str, quote: str) -> str:
        """Fold physical line breaks before decoding escapes, preserving escaped spaces."""
        out: list[str] = []
        k = 0
        while k < len(body):
            if quote == '"' and body[k] == "\\":
                if body[k + 1:k + 2] == "\n":
                    # An escaped break joins lines without a separator. Further
                    # empty lines still contribute their own line breaks.
                    k += 2
                    while k < len(body) and body[k] in " \t":
                        k += 1
                    while k < len(body) and body[k] == "\n":
                        out.append("\n")
                        k += 1
                        while k < len(body) and body[k] in " \t":
                            k += 1
                else:
                    out.append(body[k:k + 2])
                    k += 2
                continue
            folded = re.match(r"[ \t]*\n[ \t]*(?:\n[ \t]*)*", body[k:])
            if folded:
                breaks = folded.group().count("\n")
                out.append(" " if breaks == 1 else "\n" * (breaks - 1))
                k += len(folded.group())
            else:
                out.append(body[k])
                k += 1
        return "".join(out)

    @staticmethod
    def _closing_quote(buf: str, q: str) -> int | None:
        k = 1
        while k < len(buf):
            c = buf[k]
            if q == "'" and c == "'":
                if buf[k + 1:k + 2] == "'":
                    k += 2
                    continue
                return k
            if q == '"':
                if c == "\\":
                    k += 2
                    continue
                if c == '"':
                    return k
            k += 1
        return None

    @staticmethod
    def _decode_double_quoted(body: str) -> tuple[str | None, str | None]:
        """Decode YAML double-quoted escapes; an escape YAML does not define is an error."""
        out: list[str] = []
        k = 0
        while k < len(body):
            c = body[k]
            if c != "\\":
                out.append(c)
                k += 1
                continue
            esc = body[k + 1:k + 2]
            width = {"x": 2, "u": 4, "U": 8}.get(esc)
            if width:
                digits = body[k + 2:k + 2 + width]
                if len(digits) != width or any(d not in "0123456789abcdefABCDEF" for d in digits):
                    return None, f"invalid escape \\{esc}{digits} in double-quoted string"
                codepoint = int(digits, 16)
                if codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
                    return None, f"invalid Unicode scalar \\{esc}{digits}"
                out.append(chr(codepoint))
                k += 2 + width
            elif esc in DOUBLE_ESCAPES or esc == "\t":
                out.append(DOUBLE_ESCAPES.get(esc, esc))
                k += 2
            else:
                return None, f"invalid escape \\{esc} in double-quoted string (YAML does not define it)"
        return "".join(out), None

    @staticmethod
    def _block_scalar(lines: list[str], i: int, style: str, indicators: str,
                      end: int | None = None) -> tuple[str | None, int, str | None]:
        end = len(lines) if end is None else end
        if (sum(c.isdigit() for c in indicators) > 1
                or sum(c in "+-" for c in indicators) > 1):
            return None, i + 1, "invalid block scalar indicators"
        chomp = "-" if "-" in indicators else "+" if "+" in indicators else ""
        indent = next((int(c) for c in indicators if c.isdigit()), None)
        if indent is None:
            # Infer indentation before processing empty lines: spaces beyond
            # this indentation are literal scalar content, even on blank lines.
            for line in lines[i + 1:end]:
                if line.strip():
                    indent = len(line) - len(line.lstrip(" "))
                    break
        raw: list[str] = []
        j = i + 1
        while j < end:
            line = lines[j]
            if not line.strip():
                raw.append(line[indent:] if indent else "")
                j += 1
                continue
            lead = len(line) - len(line.lstrip(" "))
            if indent is None:
                if lead == 0:
                    break
                indent = lead
            if lead == 0 or lead < indent:
                break
            raw.append(line[indent:])
            j += 1
        trailing = 0
        while raw and raw[-1] == "":
            raw.pop()
            trailing += 1
        if style == "|":
            text = "\n".join(raw)
        else:  # folded: single breaks become spaces, blank lines and more-indented lines keep breaks
            text, breaks, prev_more = "", 0, False
            for line in raw:
                if not line:
                    breaks += 1
                    continue
                more = line[0] in " \t"
                if not text:
                    text = "\n" * breaks + line
                elif breaks:
                    text += "\n" * (breaks + int(more or prev_more)) + line
                elif more or prev_more:
                    text += "\n" + line
                else:
                    text += " " + line
                breaks, prev_more = 0, more
        if text and chomp != "-":
            text += "\n" * (1 + trailing if chomp == "+" else 1)
        elif not text and chomp == "+":
            text = "\n" * trailing
        return text, j, None

    # ---- fenced code blocks (CommonMark matching) ----------------------------
    @staticmethod
    def fence_map(lines: list[str]) -> tuple[list[bool], int | None]:
        """Return (inside flag per line, line number of an unclosed opening fence or None).

        CommonMark rules that matter for skill files: a fence may be indented at most
        3 spaces relative to the enclosing list item (4 or more is indented code, not a
        fence; a tab counts as 4); it closes only on a line with the same character
        (backtick or tilde), at least the opening length and nothing else; a backtick
        fence's info string may not contain backticks. Delimiter lines count as inside.
        """
        inside = [False] * len(lines)
        open_char, open_len, open_line = "", 0, None
        items: list[int] = []  # content indent of the open list items, innermost last
        for idx, line in enumerate(lines):
            expanded = line.expandtabs(4)
            indent = len(expanded) - len(expanded.lstrip(" "))
            if open_line is not None:
                inside[idx] = True
                m = FENCE_OPEN.match(expanded)
                if (m and indent - (items[-1] if items else 0) <= 3 and m.group(1)[0] == open_char
                        and len(m.group(1)) >= open_len and not m.group(2).strip()):
                    open_line = None
                continue
            if not expanded.strip():
                continue
            while items and indent < items[-1]:
                items.pop()
            base = items[-1] if items else 0
            # List markers may share the opening fence's line, including nested
            # markers ("- - ```"). Inspect their content, not the original line.
            content = expanded[base:]
            while True:
                li = LIST_ITEM.match(content)
                if not li or len(li.group(1)) > 3:
                    break
                gap = len(li.group(3))
                base += len(li.group(1)) + len(li.group(2)) + (1 if gap >= 5 else gap)
                items.append(base)
                content = expanded[base:]
            relative_indent = len(content) - len(content.lstrip(" "))
            m = FENCE_OPEN.match(content)
            if m and relative_indent <= 3 and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                open_char, open_len, open_line = m.group(1)[0], len(m.group(1)), idx + 1
                inside[idx] = True
        return inside, open_line

    @staticmethod
    def outside_fences(text: str):
        """Yield (line_no, line) for lines that are not inside a code fence."""
        lines = text.split("\n")
        inside, _ = Checker.fence_map(lines)
        for i, line in enumerate(lines, start=1):
            if not inside[i - 1]:
                yield i, line

    # ---- the checks ----------------------------------------------------------
    def run(self) -> None:
        skills_dir = self.root / "skills"
        readme = self.read(self.root / "README.md") if (self.root / "README.md").exists() else ""
        agents = self.structure_tree(
            self.read(self.root / "AGENTS.md") if (self.root / "AGENTS.md").exists() else "")

        manifest = self._load_manifest()
        # Index every directory an entry registers, not just the first: missing
        # the rest would report them as unregistered (MP001), which is wrong.
        # The name/description sync below is defined for one skill per entry, so
        # an entry listing more than one is reported rather than half-checked.
        plugin_dirs: dict[Path, dict] = {}
        for p in manifest.get("plugins", []):
            paths = p.get("skills") or []
            for path in paths:
                plugin_dirs.setdefault((self.root / path).resolve(), p)
            if len(paths) > 1:
                self.add("MP005", "ERROR", ".claude-plugin/marketplace.json", None,
                         f"plugin '{p.get('name')}' registers {len(paths)} skill directories; "
                         "this marketplace uses one skill per entry, which is what the "
                         "name and description sync (MP003/MP004) compares")

        skill_dirs = sorted(d for d in skills_dir.iterdir() if d.is_dir()) if skills_dir.exists() else []
        names_seen: dict[str, str] = {}

        for d in skill_dirs:
            self.check_skill(d, plugin_dirs, readme, agents, names_seen)

        # manifest entries pointing at missing directories
        for p in manifest.get("plugins", []):
            # `or []` rather than a default: "skills": null is what a hand-edited
            # manifest produces, and iterating None raises before any finding is
            # reported. Same reason `name` is read with .get() here.
            for s in p.get("skills") or []:
                if not (self.root / s).is_dir():
                    self.add("MP002", "ERROR", ".claude-plugin/marketplace.json", None,
                             f"plugin '{p.get('name')}' points at missing directory {s}")

    @staticmethod
    def structure_tree(agents: str) -> str:
        """The fenced block under AGENTS.md's "Repository Structure" heading.

        AG001 asks whether the tree lists a skill directory. Searching the whole
        document instead would let any other mention of `<dir>/` — a prose
        reference, an install line — stand in for the tree entry. Falls back to
        the whole document when the section or its fence is missing, so a
        reshaped AGENTS.md weakens the check rather than passing everything.
        """
        heading = re.search(r"^#{2,} +Repository Structure *$", agents, re.M)
        if not heading:
            return agents
        fence = re.search(r"^(`{3,}|~{3,})[^\n]*\n(.*?)^\1",
                          agents[heading.end():], re.M | re.S)
        return fence.group(2) if fence else agents

    def check_skill(self, d: Path, plugin_dirs: dict, readme: str, agents: str, names_seen: dict) -> None:
        skill_md = d / "SKILL.md"
        rel_dir = f"skills/{d.name}"
        if not skill_md.is_file():
            self.add("FM001", "ERROR", rel_dir, None, "SKILL.md is missing")
            return
        text = self.read(skill_md)
        fm, where, problems = self.frontmatter(text)

        # -- frontmatter
        for rule, line_no, msg in problems:
            self.add(rule, "ERROR", skill_md, line_no, msg)
        bad_lines = {line_no for _, line_no, _ in problems}
        # Only a decode failure makes the values untrustworthy. FM008 (an unknown
        # key) says nothing about whether name/description decoded, so it must not
        # suppress the checks that read them.
        decode_failed = any(rule in ("FM001", "FM007") for rule, _, _ in problems)
        if not fm and not problems:
            # Distinguish "no block at all" from "block present but empty" — the
            # fix differs, and frontmatter() returns the same empty result for both.
            # Match frontmatter()'s own column-0 test so an indented `---` is
            # reported as "no block" rather than "empty block".
            if text.split("\n", 1)[0].rstrip() == "---":
                self.add("FM002", "ERROR", skill_md, 1,
                         "frontmatter block is empty (needs 'name' and 'description')")
            else:
                self.add("FM001", "ERROR", skill_md, 1, "no YAML frontmatter block (--- ... ---)")
        name = (fm.get("name") or "").strip() or None
        desc = (fm.get("description") or "").strip() or None
        if fm and not name and where.get("name") not in bad_lines:
            self.add("FM002", "ERROR", skill_md, 1, "frontmatter has no 'name'")
        if fm and not desc and where.get("description") not in bad_lines:
            self.add("FM002", "ERROR", skill_md, 1, "frontmatter has no 'description'")
        if name and not decode_failed:
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
        if desc and not decode_failed:
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
        plugin = plugin_dirs.get(d.resolve())
        if plugin is None:
            self.add("MP001", "ERROR", rel_dir, None,
                     "not registered in .claude-plugin/marketplace.json")
        elif len(plugin.get("skills") or []) > 1:
            # MP005 already reports this entry's shape, once, against the
            # manifest. Its single name and description cannot be compared
            # against several skill directories, and --fix-descriptions skips
            # ambiguous entries, so MP003/MP004 here would be advice the fixer
            # cannot act on and RD001/RD002 would repeat one name per directory.
            pass
        else:
            # A hand-edited entry may be missing "name"; MP003 then reports the
            # mismatch instead of the run dying on a KeyError and losing every
            # other finding.
            plugin_name = plugin.get("name") or "<entry has no name>"
            # name/desc are only trustworthy when the frontmatter decoded cleanly;
            # a block with FM001/FM007 problems must be fixed first, so skip the
            # value-comparison checks (and their "run --fix-descriptions" advice,
            # which the fixer would decline anyway) rather than act on garbage.
            # An unknown key (FM008) does not make them untrustworthy.
            if not decode_failed:
                if name and plugin_name != name:
                    self.add("MP003", "ERROR", ".claude-plugin/marketplace.json", None,
                             f"plugin name '{plugin_name}' != frontmatter name '{name}' ({rel_dir})")
                if desc and plugin.get("description", "").strip() != desc:
                    self.add("MP004", "ERROR", ".claude-plugin/marketplace.json", None,
                             f"description for '{plugin_name}' differs from SKILL.md frontmatter "
                             f"(run with --fix-descriptions)")
            # -- README coverage (only for registered skills)
            if f"|`{plugin_name}`|" not in readme.replace(" ", ""):
                self.add("RD001", "ERROR", "README.md", None,
                         f"no 'Available Skills' table row for `{plugin_name}`")
            if not re.search(r"\bnpx[ \t]+skills[ \t]+add[^\n]*[ \t]-s[ \t]+"
                             + re.escape(plugin_name) + r"(?=[\s`]|$)", readme):
                self.add("RD002", "WARN", "README.md", None,
                         f"no 'npx skills add ... -s {plugin_name}' install line")

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
        linked: set[Path] = set()
        # Skip fenced code: a `references/<topic>.md` path shown inside a fence is
        # an illustration, not a pointer, and must not fail CI. MD003 filters the
        # same way.
        for i, line in self.outside_fences(text):
            for m in REF_LINK.finditer(line):
                target = m.group(1)
                if not (d / target).is_file():
                    self.add("RF001", "ERROR", skill_md, i, f"link target does not exist: {target}")
                else:
                    # Track the resolved file, so another directory's file with
                    # the same basename cannot hide an unlinked reference.
                    linked.add((d / target).resolve())
            # Backtick-wrapped mentions (e.g. the "This skill includes" list) count
            # as references too — a stale one points the agent at a missing file
            # even though it is not markdown-link syntax.
            for m in REF_MENTION.finditer(line):
                target = m.group(1)
                if not (d / target).is_file():
                    self.add("RF001", "ERROR", skill_md, i, f"referenced path does not exist: {target}")
                else:
                    linked.add((d / target).resolve())
        if refs_dir.is_dir():
            for f in sorted(refs_dir.glob("*.md")):
                # `linked` already holds both forms a reference can take, so a
                # bare substring test on the whole file adds nothing but false
                # negatives: any longer path ending in this name would match.
                if f.resolve() not in linked:
                    self.add("RF002", "WARN", f, None,
                             "reference file is never linked or mentioned from SKILL.md (agents will not find it)")
                # A reference file pointing at a sibling is grandfathered in a
                # few sets, but the target still has to exist: a rename here is
                # invisible from SKILL.md, so nothing else would catch it.
                for i, line in self.outside_fences(self.read(f)):
                    for m in SIBLING_REF.finditer(line):
                        target = (m.group(1) or m.group(2)).lstrip("./")
                        if target != f.name and not (refs_dir / target).is_file():
                            self.add("RF003", "ERROR", f, i,
                                     f"sibling reference does not exist: {target}")
                self.check_markdown(f, is_skill_md=False)

        self.check_markdown(skill_md, is_skill_md=True, text=text)

    def check_markdown(self, path: Path, is_skill_md: bool, text: str | None = None) -> None:
        text = text if text is not None else self.read(path)
        lines = text.split("\n")
        n_lines = len(text.splitlines())
        _, unclosed = self.fence_map(lines)
        if unclosed:
            self.add("MD001", "ERROR", path, unclosed,
                     "code fence opened here is never closed (the closing fence needs the same "
                     "character and at least the opening length)")
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
        manifest = self._load_manifest()
        if self._manifest_failed:
            return 0  # never rewrite a partially validated manifest
        changed = 0
        for p in manifest.get("plugins", []):
            if len(p.get("skills") or []) != 1:
                continue
            skill_md = self.root / p["skills"][0] / "SKILL.md"
            if not skill_md.is_file():
                continue
            fm, _, problems = self.frontmatter(self.read(skill_md))
            if any(rule in ("FM001", "FM007") for rule, _, _ in problems):
                continue  # never write a value the parser could not decode. An
                          # unknown key (FM008) leaves description decodable, so
                          # it must not silently turn this into a no-op.
            desc = (fm.get("description") or "").strip()
            if desc and p.get("description", "").strip() != desc:
                p["description"] = desc
                changed += 1
        if changed:
            manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return changed


# ---- reporting -----------------------------------------------------------------
def report(findings: list[Finding], fmt: str) -> None:
    def escape(value: str, *, property_value: bool = False) -> str:
        value = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        return value.replace(":", "%3A").replace(",", "%2C") if property_value else value

    order = {"ERROR": 0, "WARN": 1}
    for f in sorted(findings, key=lambda x: (x.ignored is not None, order[x.level], x.path, x.line or 0)):
        loc = f"{f.path}:{f.line}" if f.line else f.path
        if fmt == "github" and not f.ignored:
            kind = "error" if f.level == "ERROR" else "warning"
            line_attr = f",line={f.line}" if f.line else ""
            print(f"::{kind} file={escape(f.path, property_value=True)}{line_attr},title={f.rule}::{escape(f.msg)}")
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
