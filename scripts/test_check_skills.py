#!/usr/bin/env python3
"""Tests for scripts/check_skills.py. Standard library only.

    python3 -m unittest discover -s scripts -p 'test_*.py' -v
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import check_skills
from check_skills import Checker


class FrontmatterTests(unittest.TestCase):
    def fm(self, text: str):
        return Checker.frontmatter(text)

    def test_plain_scalars_and_line_numbers(self):
        fields, where, problems = self.fm("---\nname: ysql\ndescription: Use when writing SQL.\n---\nbody\n")
        self.assertEqual(fields, {"name": "ysql", "description": "Use when writing SQL."})
        self.assertEqual(where, {"name": 2, "description": 3})
        self.assertEqual(problems, [])

    def test_folded_block_scalar_clips_to_one_newline(self):
        fields, _, problems = self.fm("---\nname: ysql\ndescription: >\n  Use when writing SQL\n  for YugabyteDB.\n---\n")
        self.assertEqual(fields["description"], "Use when writing SQL for YugabyteDB.\n")
        self.assertEqual(problems, [])

    def test_literal_block_scalar_with_strip(self):
        fields, _, problems = self.fm("---\ndescription: |-\n  line one\n  line two\n---\n")
        self.assertEqual(fields["description"], "line one\nline two")
        self.assertEqual(problems, [])

    def test_folded_block_scalar_keep_and_blank_line(self):
        fields, _, _ = self.fm("---\ndescription: >+\n  a\n\n  b\n\n---\n")
        self.assertEqual(fields["description"], "a\nb\n\n")

    def test_double_quoted_name(self):
        fields, _, problems = self.fm('---\nname: "ysql"\n---\n')
        self.assertEqual(fields["name"], "ysql")
        self.assertEqual(problems, [])

    def test_single_quoted_with_doubled_quote(self):
        fields, _, _ = self.fm("---\ndescription: 'it''s here'\n---\n")
        self.assertEqual(fields["description"], "it's here")

    def test_double_quoted_escapes(self):
        fields, _, _ = self.fm('---\ndescription: "a \\"q\\" b\\tc \\u00e9"\n---\n')
        self.assertEqual(fields["description"], 'a "q" b\tc \u00e9')

    def test_plain_comment_is_stripped_but_hash_inside_a_token_is_kept(self):
        fields, _, _ = self.fm('---\ndescription: Triggers on "TA-#####", port 5433 # trailing comment\n---\n')
        self.assertEqual(fields["description"], 'Triggers on "TA-#####", port 5433')

    def test_plain_continuation_lines_fold_with_a_space(self):
        fields, _, _ = self.fm("---\ndescription: first part\n  second part\n---\n")
        self.assertEqual(fields["description"], "first part second part")

    def test_missing_terminator_is_fm001_reported_once(self):
        fields, _, problems = self.fm(
            "---\nname: ysql\ndescription: x\n\n# Heading\nBody paragraph without a terminator.\nmore prose\n")
        self.assertEqual([r for r, _, _ in problems if r == "FM001"], ["FM001"])
        self.assertLessEqual(sum(1 for r, _, _ in problems if r == "FM007"), 1)
        self.assertEqual(fields["name"], "ysql")

    def test_prose_body_above_a_thematic_break_is_fm007(self):
        # Terminator missing, body reaches a `---` thematic break. The break is
        # the terminator (that is how frontmatter is delimited), and the heading
        # and prose above it do not decode as `key: value`, so FM007 flags it.
        text = ("---\nname: ysql\ndescription: x\n\n"
                "# YugabyteDB YSQL Best Practices\n\n"
                "Some intro prose without a colon.\n\n---\nmore body\n")
        _, _, problems = self.fm(text)
        self.assertIn("FM007", [r for r, _, _ in problems])

    def test_key_shaped_body_absorbed_is_caught_by_fm008(self):
        # Nothing structural separates this from a block that really carries
        # those keys, so shape cannot decide it. The spec field set can: neither
        # `Status` nor `Accept` is an Agent Skills frontmatter field.
        text = ("---\nname: ysql\ndescription: x\n\n"
                "Status: ready\nAccept: application/json\n\n---\nmore body\n")
        _, _, problems = self.fm(text)
        flagged = {msg.split("'")[1] for r, _, msg in problems if r == "FM008"}
        self.assertEqual(flagged, {"Status", "Accept"})

    def test_block_scalar_then_absorbed_body_key_is_fm008(self):
        # The block-scalar path: `>` consumes the blank line, so `Status` lands in
        # the fields. FM008 is what catches it.
        _, _, problems = self.fm(
            "---\nname: demo\ndescription: >\n  Real description.\n\nStatus: ready\n---\n# Body\n")
        self.assertIn("FM008", [r for r, _, _ in problems])

    def test_nested_value_then_absorbed_body_key_is_fm008(self):
        # yb-rag-langchain/SKILL.md ships this `metadata:` shape.
        _, _, problems = self.fm(
            "---\nname: demo\nmetadata:\n  tags: a\n\nStatus: ready\n---\n# Body\n")
        self.assertIn("FM008", [r for r, _, _ in problems])

    def test_blank_line_between_keys_is_valid_yaml_and_accepted(self):
        # The converse of the FM008 cases and the reason shape cannot decide them:
        # identical structure, but every key is a spec field, so this is valid
        # frontmatter and must not be reported as unclosed.
        fields, _, problems = self.fm(
            "---\nname: x\nmetadata:\n  tags: a\n\ndescription: Use when the shape is valid.\n---\n")
        self.assertEqual(problems, [])
        self.assertEqual(fields["description"], "Use when the shape is valid.")

    def test_unknown_top_level_key_is_fm008(self):
        _, _, problems = self.fm("---\nname: x\ndescription: d\nauthor: someone\n---\n")
        self.assertTrue(any(r == "FM008" and "author" in msg for r, _, msg in problems), problems)

    def test_flow_collections_are_skipped_not_rejected(self):
        # `allowed-tools: [Read, Grep]` is valid YAML in a field the spec defines.
        # Rejecting it would also set decode_failed and stand down the manifest
        # checks, so a stale description would go unreported behind a parser error.
        for value in ("[Read, Grep]", "{author: x}"):
            with self.subTest(value=value):
                _, _, problems = self.fm(
                    "---\nname: x\ndescription: Use when checking flow collections parse.\n"
                    "allowed-tools: " + value + "\n---\n")
                self.assertEqual(problems, [])

    def test_anchors_and_tags_are_still_rejected(self):
        _, _, problems = self.fm("---\nname: x\ndescription: d\nmetadata: &anchor\n---\n")
        self.assertTrue(any(r == "FM007" for r, _, _ in problems), problems)

    def test_optional_spec_fields_are_accepted(self):
        _, _, problems = self.fm(
            "---\nname: x\ndescription: d\nlicense: Apache-2.0\ncompatibility: Requires git\n"
            "allowed-tools: Read\nmetadata:\n  author: y\n---\n")
        self.assertEqual(problems, [])

    def test_blank_line_interior_to_a_nested_block_is_kept(self):
        # The converse: a blank line followed by more indented content is part of
        # the nested block, so the block is still properly terminated.
        fields, _, problems = self.fm(
            "---\nname: demo\nmetadata:\n  a: 1\n\n  b: 2\ndescription: after\n---\n")
        self.assertEqual(problems, [])
        self.assertIsNone(fields["metadata"])
        self.assertEqual(fields["description"], "after")

    def test_duplicate_key_is_fm007(self):
        fields, _, problems = self.fm("---\nname: first\nname: second\ndescription: d\n---\n")
        self.assertTrue(any(r == "FM007" and "duplicate" in msg for r, _, msg in problems),
                        problems)

    def test_nested_mapping_is_skipped(self):
        fields, _, problems = self.fm(
            "---\nname: ysql\nmetadata:\n  author: x\n  version: \"1\"\ndescription: after nested\n---\n")
        self.assertIsNone(fields["metadata"])
        self.assertEqual(fields["description"], "after nested")
        self.assertEqual(problems, [])

    def test_undecodable_line_is_fm007_but_the_block_is_still_closed(self):
        # A malformed line inside an otherwise terminated block is flagged, and
        # the scan continues so the real terminator is found (no spurious FM001).
        # Callers gate on `problems`, so the parsed values are not trusted here.
        fields, _, problems = self.fm("---\nname: ysql\n- not a key\ndescription: after\n---\n")
        self.assertEqual([r for r, _, _ in problems], ["FM007"])
        self.assertEqual(problems[0][1], 3)

    def test_comment_lines_inside_the_block_are_legal(self):
        # A comment used to fail KEY_LINE and land as FM007, which sets
        # decode_failed and stands down the value checks and the fixer.
        fm, _, problems = Checker.frontmatter(
            "---\n# which skill this is\nname: demo\n"
            "description: Demo. Use when checking comments. Triggers on demo.\n---\n")
        self.assertEqual(problems, [])
        self.assertEqual(fm.get("name"), "demo")

    def test_column_zero_block_sequence_is_skipped_not_rejected(self):
        # YAML lets a block sequence sit at the parent key's indentation. This
        # used to fail KEY_LINE on "- Read" and report FM007, which sets
        # decode_failed and silences the manifest checks and the fixer.
        fm, _, problems = Checker.frontmatter(
            "---\nname: demo\nallowed-tools:\n- Read\n- Grep\n"
            "description: Demo. Use when checking sequences. Triggers on demo.\n---\n")
        self.assertEqual(problems, [])
        self.assertEqual(fm.get("name"), "demo")
        self.assertIn("description", fm)

    def test_column_zero_sequence_does_not_swallow_the_terminator(self):
        fm, _, problems = Checker.frontmatter(
            "---\nallowed-tools:\n- Read\n---\n\n- a body bullet\n")
        self.assertEqual([r for r, _, _ in problems if r == "FM007"], [])
        self.assertNotIn("a body bullet", str(fm))

    def test_flow_sequence_is_skipped_like_a_block_collection(self):
        # Not decoded, but not an error either: it is a structured value, the same
        # case as a block mapping. The value comes back None and nothing is raised.
        fields, _, problems = self.fm("---\ndescription: [flow, seq]\n---\n")
        self.assertEqual(problems, [])
        self.assertIsNone(fields["description"])

    def test_unterminated_quote_is_fm007(self):
        _, _, problems = self.fm('---\ndescription: "never closed\n---\n')
        self.assertTrue(any(r == "FM007" for r, _, _ in problems))

    def test_no_frontmatter(self):
        self.assertEqual(self.fm("# Title\n"), ({}, {}, []))

    def test_colon_without_space_is_not_a_key(self):
        _, _, problems = self.fm("---\nname:ysql\n---\n")
        self.assertTrue(any(r == "FM007" for r, _, _ in problems))


class FenceTests(unittest.TestCase):
    def fmap(self, text: str):
        return Checker.fence_map(text.split("\n"))

    def test_four_backtick_block_may_contain_three_backtick_lines(self):
        inside, unclosed = self.fmap("````\n```\ncode\n````\ntext")
        self.assertIsNone(unclosed)
        self.assertEqual(inside, [True, True, True, True, False])

    def test_unclosed_fence_reports_its_opening_line(self):
        _, unclosed = self.fmap("text\n```\ncode\n")
        self.assertEqual(unclosed, 2)

    def test_tilde_fence_may_contain_backticks(self):
        inside, unclosed = self.fmap("~~~\n```\n~~~\ntext")
        self.assertIsNone(unclosed)
        self.assertEqual(inside, [True, True, True, False])

    def test_shorter_closer_does_not_close(self):
        _, unclosed = self.fmap("````\n```\n")
        self.assertEqual(unclosed, 1)

    def test_info_string_opens_a_fence(self):
        inside, unclosed = self.fmap("```python\nx = 1\n```\n")
        self.assertIsNone(unclosed)
        self.assertEqual(inside[:3], [True, True, True])

    def test_backtick_in_info_string_is_not_a_fence(self):
        inside, unclosed = self.fmap("``` a`b\ntext")
        self.assertIsNone(unclosed)
        self.assertEqual(inside, [False, False])

    def test_closer_with_trailing_text_is_content(self):
        inside, unclosed = self.fmap("```\ncode\n``` trailing\nmore\n```\n")
        self.assertIsNone(unclosed)
        self.assertEqual(inside[2:4], [True, True])

    def test_outside_fences_uses_the_same_matching(self):
        outside = [line for _, line in Checker.outside_fences("a\n````\n```\nb\n````\nc")]
        self.assertEqual(outside, ["a", "c"])


class RepoTests(unittest.TestCase):
    """End-to-end checks on a temporary repository with one skill."""

    SKILL = '''---
name: "demo-skill"
description: >
  Demo skill for the checker's own tests. Use when verifying that folded
  descriptions and nested code fences are handled. Triggers on "demo".
---

# Demo

````markdown
```sql
SELECT 1;
```
````

Done.
'''
    DESCRIPTION = ("Demo skill for the checker's own tests. Use when verifying that folded "
                   'descriptions and nested code fences are handled. Triggers on "demo".')

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="check-skills-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "skills" / "demo-skill").mkdir(parents=True)
        (self.root / ".claude-plugin").mkdir()
        self.skill = self.root / "skills" / "demo-skill" / "SKILL.md"
        self.skill.write_text(self.SKILL, encoding="utf-8")
        self.manifest = self.root / ".claude-plugin" / "marketplace.json"
        self.write_manifest(self.DESCRIPTION)
        (self.root / "README.md").write_text("|`demo-skill`|Demo|\n\nnpx skills add x/y -s demo-skill\n")
        (self.root / "AGENTS.md").write_text("skills/\n  demo-skill/\n")

    def write_manifest(self, description: str) -> None:
        manifest = {"name": "demo", "owner": {"name": "x"}, "metadata": {}, "plugins": [
            {"name": "demo-skill", "description": description, "source": "./", "strict": False,
             "skills": ["./skills/demo-skill"]}]}
        self.manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    def manifest_description(self) -> str:
        return json.loads(self.manifest.read_text(encoding="utf-8"))["plugins"][0]["description"]

    def check(self) -> Checker:
        checker = Checker(self.root)
        checker.run()
        return checker

    @staticmethod
    def rules(checker: Checker, level: str | None = None) -> list[str]:
        return sorted(f.rule for f in checker.findings
                      if not f.ignored and (level is None or f.level == level))

    def test_clean_repo_has_no_findings(self):
        self.assertEqual(self.rules(self.check()), [])

    def test_single_quoted_name_is_accepted(self):
        self.skill.write_text(self.SKILL.replace('name: "demo-skill"', "name: 'demo-skill'", 1))
        self.assertEqual(self.rules(self.check()), [])

    def test_fix_descriptions_writes_the_folded_text(self):
        self.write_manifest("stale")
        self.assertEqual(self.rules(self.check(), "ERROR"), ["MP004"])
        self.assertEqual(Checker(self.root).fix_descriptions(), 1)
        self.assertEqual(self.manifest_description(), self.DESCRIPTION)
        self.assertEqual(self.rules(self.check()), [])

    def test_missing_terminator_is_an_error_and_blocks_the_fixer(self):
        lines = self.SKILL.split("\n")
        del lines[[i for i, l in enumerate(lines) if l.strip() == "---"][1]]
        self.skill.write_text("\n".join(lines))
        self.write_manifest("stale")
        errors = self.rules(self.check(), "ERROR")
        self.assertIn("FM001", errors)
        self.assertLessEqual(errors.count("FM007"), 1)
        self.assertEqual(Checker(self.root).fix_descriptions(), 0)
        self.assertEqual(self.manifest_description(), "stale")

    def test_empty_frontmatter_block_is_reported_as_empty_not_missing(self):
        # A present-but-empty block must not be reported as "no YAML frontmatter
        # block": the block exists, it just has no name/description.
        self.skill.write_text("---\n---\n\n# Demo\n")
        checker = self.check()
        msgs = [f.msg for f in checker.findings if f.rule in ("FM001", "FM002")]
        self.assertTrue(any("empty" in m for m in msgs), msgs)
        self.assertFalse(any("no YAML frontmatter block" in m for m in msgs), msgs)

    def test_file_with_no_frontmatter_is_still_fm001(self):
        self.skill.write_text("# Demo only\n")
        msgs = [f.msg for f in self.check().findings if f.rule == "FM001"]
        self.assertTrue(any("no YAML frontmatter block" in m for m in msgs), msgs)

    def test_name_mismatch_is_fm003_and_mp003(self):
        self.skill.write_text(self.SKILL.replace('name: "demo-skill"', "name: other-name", 1))
        self.assertEqual(self.rules(self.check(), "ERROR"), ["FM003", "MP003"])

    def test_unclosed_fence_is_md001_at_the_opening_line(self):
        self.skill.write_text(self.SKILL + "\n```python\nprint(1)\n")
        md001 = [f for f in self.check().findings if f.rule == "MD001"]
        self.assertEqual(len(md001), 1)
        self.assertEqual(md001[0].line, self.SKILL.count("\n") + 2)

    def test_placeholder_is_reported_outside_fences_only(self):
        self.skill.write_text(self.SKILL.replace("Done.", "TODO finish") + "\n```\nTODO inside code\n```\n")
        md003 = [f for f in self.check().findings if f.rule == "MD003"]
        self.assertEqual([f.line for f in md003], [self.SKILL.count("\n")])

    def test_npm_compatibility_ranges_are_not_flagged(self):
        # The documented rule allows ^ and ~ ranges; the package.json pattern used
        # to match them, contradicting it.
        self.skill.write_text(self.SKILL + '\n```json\n{"version": "^8.11.0"}\n```\n')
        self.assertNotIn("VP001", self.rules(self.check(), "WARN"))

    def test_npm_exact_pin_still_warns(self):
        self.skill.write_text(self.SKILL + '\n```json\n{"version": "8.11.0"}\n```\n')
        self.assertIn("VP001", self.rules(self.check(), "WARN"))

    def test_version_pin_warns(self):
        self.skill.write_text(self.SKILL + "\n```bash\npip install psycopg==3.2.1\n```\n")
        self.assertIn("VP001", self.rules(self.check(), "WARN"))

    def test_unregistered_skill_is_mp001(self):
        other = self.root / "skills" / "other"
        other.mkdir()
        (other / "SKILL.md").write_text(
            "---\nname: other\ndescription: Use when testing another skill; this text is long enough.\n---\n# Other\n")
        self.assertIn("MP001", self.rules(self.check(), "ERROR"))

    def test_ignore_baseline_marks_the_finding_as_ignored(self):
        self.write_manifest("stale")
        (self.root / ".skills-lint.json").write_text(
            json.dumps({"ignore": [{"rule": "MP004", "reason": "test baseline"}]}))
        checker = self.check()
        self.assertEqual(self.rules(checker), [])
        self.assertTrue(any(f.rule == "MP004" and f.ignored == "test baseline" for f in checker.findings))

    def with_description(self, description_yaml: str) -> str:
        body = self.SKILL.split("---\n", 2)[2]
        return f'---\nname: "demo-skill"\ndescription: {description_yaml}\n---\n' + body

    def test_invalid_escape_in_description_is_an_error_and_blocks_the_fixer(self):
        self.skill.write_text(self.with_description(
            '"Demo skill with a bad \\q escape. Use when verifying that invalid YAML is rejected."'))
        self.write_manifest("stale")
        self.assertEqual(self.rules(self.check(), "ERROR"), ["FM007"])
        self.assertEqual(Checker(self.root).fix_descriptions(), 0)
        self.assertEqual(self.manifest_description(), "stale")

    def test_unquoted_colon_space_in_description_is_an_error(self):
        self.skill.write_text(self.with_description(
            "Demo skill for SQL: review. Use when verifying that unquoted mapping syntax is rejected."))
        self.assertEqual(self.rules(self.check(), "ERROR"), ["FM007"])

    def test_stale_backtick_reference_mention_is_rf001(self):
        # A backtick-wrapped reference path that does not resolve (the "This skill
        # includes" list shape) must be RF001, even though it is not link syntax.
        self.skill.write_text(self.SKILL.replace(
            "# Demo\n", "# Demo\n\n- `references/does-not-exist.md` — missing\n"))
        self.assertIn("RF001", self.rules(self.check(), "ERROR"))

    def test_broken_frontmatter_does_not_emit_mp004(self):
        # With FM001/FM007 present, name/desc are untrustworthy: skip the manifest
        # value-comparison checks (and their misleading --fix-descriptions advice).
        lines = self.SKILL.split("\n")
        del lines[[i for i, l in enumerate(lines) if l.strip() == "---"][1]]
        self.skill.write_text("\n".join(lines))
        self.write_manifest("stale-does-not-match")
        errors = self.rules(self.check(), "ERROR")
        self.assertIn("FM001", errors)
        self.assertNotIn("MP004", errors)
        self.assertNotIn("MP003", errors)

    def test_baseline_entry_without_reason_does_not_crash_or_suppress(self):
        # An entry with a rule but no reason used to raise KeyError in add() on the
        # first matching finding, losing the whole report. It must be reported as
        # CFG001 and must not suppress the finding it names.
        self.write_manifest("stale")
        (self.root / ".skills-lint.json").write_text(
            json.dumps({"ignore": [{"rule": "MP004"}]}))
        checker = self.check()
        errors = self.rules(checker, "ERROR")
        self.assertIn("CFG001", errors)
        self.assertIn("MP004", errors)
        self.assertFalse(any(f.rule == "MP004" and f.ignored for f in checker.findings))

    def test_reference_mention_inside_a_fence_is_not_rf001(self):
        # An illustrative `references/<topic>.md` path inside a fenced block is an
        # example, not a pointer: it must not fail CI.
        self.skill.write_text(self.SKILL + "\n```markdown\n- `references/example.md` — illustration\n```\n")
        self.assertNotIn("RF001", self.rules(self.check(), "ERROR"))

    def test_unknown_key_does_not_suppress_manifest_sync_or_the_fixer(self):
        # FM008 says nothing about whether name/description decoded, so one typo'd
        # key must not silence MP004 or turn --fix-descriptions into a no-op.
        self.skill.write_text(self.SKILL.replace(
            'name: "demo-skill"', 'name: "demo-skill"\nauthor: someone'))
        self.write_manifest("stale")
        errors = self.rules(self.check(), "ERROR")
        self.assertIn("FM008", errors)
        self.assertIn("MP004", errors)
        self.assertEqual(Checker(self.root).fix_descriptions(), 1)

    def test_decode_failure_does_suppress_value_checks(self):
        # The converse, and what the docstring promises: when the block did not
        # decode, the checks that read name/description stand down.
        lines = self.SKILL.split("\n")
        del lines[[i for i, l in enumerate(lines) if l.strip() == "---"][1]]
        self.skill.write_text("\n".join(lines))
        self.write_manifest("stale")
        errors = self.rules(self.check(), "ERROR")
        self.assertIn("FM001", errors)
        for suppressed in ("FM003", "MP003", "MP004"):
            self.assertNotIn(suppressed, errors)
        self.assertEqual(Checker(self.root).fix_descriptions(), 0)

    def test_duplicate_name_does_not_also_raise_a_misleading_fm003(self):
        # FM007 already says the key is duplicated; comparing whichever value came
        # last against the directory would be a second, misleading error.
        self.skill.write_text(self.SKILL.replace(
            'name: "demo-skill"', 'name: "demo-skill"\nname: something-else'))
        errors = self.rules(self.check(), "ERROR")
        self.assertIn("FM007", errors)
        self.assertNotIn("FM003", errors)

    def test_indented_opener_is_reported_as_no_block_not_empty_block(self):
        self.skill.write_text("  ---\nname: demo-skill\n  ---\n\n# Demo\n")
        msgs = [f.msg for f in self.check().findings if f.rule in ("FM001", "FM002")]
        self.assertTrue(any("no YAML frontmatter block" in m for m in msgs), msgs)

    def test_baseline_entry_without_rule_is_cfg001(self):
        (self.root / ".skills-lint.json").write_text(
            json.dumps({"ignore": [{"path": "skills/x", "reason": "no rule key"}]}))
        self.assertIn("CFG001", self.rules(self.check(), "ERROR"))

    def test_cli_exit_codes_and_github_format(self):
        script = Path(check_skills.__file__)
        ok = subprocess.run([sys.executable, str(script), "--root", str(self.root)],
                            capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stdout)
        self.assertIn("0 error(s), 0 warning(s), 0 ignored", ok.stdout)
        self.write_manifest("stale")
        bad = subprocess.run([sys.executable, str(script), "--root", str(self.root), "--format", "github"],
                             capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("::error file=.claude-plugin/marketplace.json,title=MP004::", bad.stdout)


class StrictYamlTests(unittest.TestCase):
    """Input that PyYAML rejects must not reach the manifest."""

    def fm(self, text: str):
        return Checker.frontmatter(text)

    def test_unknown_escape_is_rejected(self):
        fields, _, problems = self.fm('---\ndescription: "bad \\q escape"\n---\n')
        self.assertTrue(any(r == "FM007" and "invalid escape" in msg for r, _, msg in problems), problems)
        self.assertIsNone(fields["description"])

    def test_hex_and_unicode_escapes_are_decoded(self):
        fields, _, problems = self.fm('---\ndescription: "\\x41\\u00e9\\U0001F600 \\\\ \\" \\t"\n---\n')
        self.assertEqual(problems, [])
        self.assertEqual(fields["description"], 'A\u00e9\U0001F600 \\ " \t')

    def test_yaml_specific_escapes_decode_to_their_codepoints(self):
        # \N \_ \L \P must decode to U+0085, U+00A0, U+2028, U+2029 — not to a
        # plain space or the empty string. The table writes them as \u escapes so
        # this stays reviewable; this test is the guard.
        for esc, codepoint in (("N", 0x0085), ("_", 0x00A0), ("L", 0x2028), ("P", 0x2029)):
            with self.subTest(escape=esc):
                fields, _, problems = self.fm('---\ndescription: "a\\' + esc + 'b"\n---\n')
                self.assertEqual(problems, [])
                self.assertEqual(fields["description"], "a" + chr(codepoint) + "b")

    def test_short_hex_escape_is_rejected(self):
        _, _, problems = self.fm('---\ndescription: "\\x4"\n---\n')
        self.assertTrue(any("invalid escape" in msg for _, _, msg in problems), problems)

    def test_plain_scalar_with_colon_space_is_rejected(self):
        _, _, problems = self.fm("---\ndescription: Use for SQL: review and more\n---\n")
        self.assertTrue(any(r == "FM007" and "': '" in msg for r, _, msg in problems), problems)

    def test_plain_scalar_ending_with_colon_is_rejected(self):
        _, _, problems = self.fm("---\ndescription: Use for SQL review:\n---\n")
        self.assertTrue(any(r == "FM007" for r, _, _ in problems), problems)

    def test_plain_scalar_starting_with_sequence_indicator_is_rejected(self):
        _, _, problems = self.fm("---\ndescription: - not a scalar\n---\n")
        self.assertTrue(any(r == "FM007" for r, _, _ in problems), problems)

    def test_colon_without_a_following_space_is_fine(self):
        fields, _, problems = self.fm("---\ndescription: Connect to host:5433 for YSQL\n---\n")
        self.assertEqual(problems, [])
        self.assertEqual(fields["description"], "Connect to host:5433 for YSQL")


class FenceIndentationTests(unittest.TestCase):
    """Indented code is not a fence; fences inside list items are."""

    def fmap(self, text: str):
        return Checker.fence_map(text.split("\n"))

    def test_four_space_indented_backticks_are_indented_code(self):
        inside, unclosed = self.fmap("Example:\n\n    ```\n    code\n\nAfter.")
        self.assertIsNone(unclosed)
        self.assertEqual(inside, [False] * 6)

    def test_three_space_indented_fence_is_a_fence(self):
        inside, unclosed = self.fmap("   ```\ncode\n   ```\ntext")
        self.assertIsNone(unclosed)
        self.assertEqual(inside, [True, True, True, False])

    def test_fence_inside_a_list_item(self):
        inside, unclosed = self.fmap("- item\n\n  ```bash\n  run\n  ```\n- next")
        self.assertIsNone(unclosed)
        self.assertEqual(inside, [False, False, True, True, True, False])

    def test_fence_inside_a_nested_list_item_indented_four_spaces(self):
        inside, unclosed = self.fmap("- a\n  - b\n\n    ```\n    code\n    ```\nend")
        self.assertIsNone(unclosed)
        self.assertEqual(inside, [False, False, False, True, True, True, False])

    def test_four_space_indented_backticks_inside_a_fence_do_not_close_it(self):
        inside, unclosed = self.fmap("```\n    ```\ncode\n```\ntext")
        self.assertIsNone(unclosed)
        self.assertEqual(inside, [True, True, True, True, False])

    def test_tab_indented_backticks_are_not_a_fence(self):
        inside, unclosed = self.fmap("text\n\n\t```\n\tcode\n")
        self.assertIsNone(unclosed)
        self.assertFalse(any(inside))


class ManifestShapeTests(unittest.TestCase):
    """A plugin entry may name more than one directory; none may be dropped."""

    def build(self, skills: list[str]):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        for name in skills:
            (tmp / "skills" / name).mkdir(parents=True)
            (tmp / "skills" / name / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: Demo skill for the checker's own "
                f"tests. Use when verifying manifest shapes. Triggers on {name}.\n---\n\n# {name}\n",
                encoding="utf-8")
        (tmp / ".claude-plugin").mkdir()
        return tmp

    def run_checker(self, root: Path):
        c = Checker(root)
        c.run()
        return {f.rule for f in c.findings}

    def test_second_directory_in_one_entry_is_not_reported_unregistered(self):
        tmp = self.build(["alpha", "beta"])
        (tmp / ".claude-plugin" / "marketplace.json").write_text(json.dumps(
            {"plugins": [{"name": "alpha",
                          "description": "Demo skill for the checker's own tests. Use when "
                                         "verifying manifest shapes. Triggers on alpha.",
                          "skills": ["./skills/alpha", "./skills/beta"]}]}), encoding="utf-8")
        rules = self.run_checker(tmp)
        self.assertNotIn("MP001", rules, "the second directory was treated as unregistered")
        self.assertIn("MP005", rules, "a multi-skill entry should be reported")

    def test_multi_skill_entry_reports_only_mp005_not_a_half_comparison(self):
        # The entry's single name/description cannot be compared against two
        # skills, and --fix-descriptions writes only skills[0], so MP003/MP004
        # here would be advice the fixer cannot act on.
        tmp = self.build(["alpha", "beta"])
        (tmp / ".claude-plugin" / "marketplace.json").write_text(json.dumps(
            {"plugins": [{"name": "alpha",
                          "description": "Demo skill for the checker's own tests. Use when "
                                         "verifying manifest shapes. Triggers on alpha.",
                          "skills": ["./skills/alpha", "./skills/beta"]}]}), encoding="utf-8")
        rules = self.run_checker(tmp)
        self.assertIn("MP005", rules)
        self.assertEqual(rules & {"MP003", "MP004"}, set(),
                         "the entry was compared against a skill it does not name")

    def test_entry_without_a_name_reports_instead_of_crashing(self):
        tmp = self.build(["alpha"])
        (tmp / ".claude-plugin" / "marketplace.json").write_text(json.dumps(
            {"plugins": [{"description": "Demo skill for the checker's own tests. Use when "
                                         "verifying manifest shapes. Triggers on alpha.",
                          "skills": ["./skills/alpha"]}]}), encoding="utf-8")
        self.assertIn("MP003", self.run_checker(tmp))

    def test_nameless_entry_pointing_at_a_missing_directory_does_not_crash(self):
        # MP002's branch is only reached when the directory is absent, so the
        # earlier nameless-entry test never executed it.
        tmp = self.build(["alpha"])
        (tmp / ".claude-plugin" / "marketplace.json").write_text(json.dumps(
            {"plugins": [{"description": "Demo skill for the checker's own tests. Use when "
                                         "verifying manifest shapes. Triggers on alpha.",
                          "skills": ["./skills/alpha"]},
                         {"skills": ["./skills/gone"]}]}), encoding="utf-8")
        self.assertIn("MP002", self.run_checker(tmp))

    def test_entry_with_null_skills_does_not_crash(self):
        tmp = self.build(["alpha"])
        (tmp / ".claude-plugin" / "marketplace.json").write_text(json.dumps(
            {"plugins": [{"name": "alpha",
                          "description": "Demo skill for the checker's own tests. Use when "
                                         "verifying manifest shapes. Triggers on alpha.",
                          "skills": ["./skills/alpha"]},
                         {"name": "broken", "skills": None}]}), encoding="utf-8")
        # The point is that run() completes: a null "skills" must not raise
        # before the findings already gathered can be reported.
        rules = self.run_checker(tmp)
        self.assertIsInstance(rules, set)
        self.assertNotIn("MP002", rules)

    def test_one_directory_per_entry_does_not_raise_mp005(self):
        tmp = self.build(["alpha"])
        (tmp / ".claude-plugin" / "marketplace.json").write_text(json.dumps(
            {"plugins": [{"name": "alpha",
                          "description": "Demo skill for the checker's own tests. Use when "
                                         "verifying manifest shapes. Triggers on alpha.",
                          "skills": ["./skills/alpha"]}]}), encoding="utf-8")
        self.assertNotIn("MP005", self.run_checker(tmp))


class ReferenceLinkingTests(unittest.TestCase):
    """A reference counts as linked only when the path it names resolves."""

    def build(self, body: str, ref_names: list[str]):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        skill = tmp / "skills" / "alpha"
        (skill / "references").mkdir(parents=True)
        for name in ref_names:
            (skill / "references" / name).write_text(f"# {name}\n", encoding="utf-8")
        (skill / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: Demo skill for the checker's own tests. Use when "
            "verifying reference linking. Triggers on alpha.\n---\n\n" + body, encoding="utf-8")
        (tmp / ".claude-plugin").mkdir()
        (tmp / ".claude-plugin" / "marketplace.json").write_text(json.dumps(
            {"plugins": [{"name": "alpha",
                          "description": "Demo skill for the checker's own tests. Use when "
                                         "verifying reference linking. Triggers on alpha.",
                          "skills": ["./skills/alpha"]}]}), encoding="utf-8")
        c = Checker(tmp)
        c.run()
        return c.findings

    def test_broken_link_does_not_mark_a_same_named_real_file_as_linked(self):
        # The link names references/old/notes.md, which does not exist; the real
        # references/notes.md is mentioned nowhere. Keying `linked` by basename
        # used to let the broken link satisfy the real file's RF002.
        findings = self.build("See [notes](references/old/notes.md).", ["notes.md"])
        self.assertIn("RF001", {f.rule for f in findings})
        self.assertIn("RF002", {f.rule for f in findings},
                      "a broken link suppressed the unlinked-reference warning")

    def test_dot_slash_relative_link_resolves_and_counts_as_linked(self):
        findings = self.build("See [notes](./references/notes.md).", ["notes.md"])
        self.assertEqual({f.rule for f in findings} & {"RF001", "RF002"}, set(),
                         "a ./references/ link was neither resolved nor counted")

    def test_broken_dot_slash_link_is_rf001(self):
        findings = self.build("See [gone](./references/gone.md).", ["notes.md"])
        self.assertIn("RF001", {f.rule for f in findings})

    def test_broken_sibling_reference_is_rf003(self):
        # RF001 reads SKILL.md only, so a reference pointing at a renamed
        # sibling is invisible to it; nothing else would catch the break.
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        skill = tmp / "skills" / "alpha"
        (skill / "references").mkdir(parents=True)
        (skill / "references" / "a.md").write_text(
            "# a\n\nSee [b](b.md) and `gone.md`.\n", encoding="utf-8")
        (skill / "references" / "b.md").write_text("# b\n", encoding="utf-8")
        (skill / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: Demo skill for the checker's own tests. Use when "
            "verifying sibling references. Triggers on alpha.\n---\n\n"
            "`references/a.md` and `references/b.md`.\n", encoding="utf-8")
        (tmp / ".claude-plugin").mkdir()
        (tmp / ".claude-plugin" / "marketplace.json").write_text(json.dumps(
            {"plugins": [{"name": "alpha",
                          "description": "Demo skill for the checker's own tests. Use when "
                                         "verifying sibling references. Triggers on alpha.",
                          "skills": ["./skills/alpha"]}]}), encoding="utf-8")
        c = Checker(tmp)
        c.run()
        rf003 = [f for f in c.findings if f.rule == "RF003"]
        self.assertEqual(len(rf003), 1, [f.msg for f in rf003])
        self.assertIn("gone.md", rf003[0].msg)

    def test_working_link_marks_the_file_as_linked(self):
        findings = self.build("See [notes](references/notes.md).", ["notes.md"])
        self.assertEqual({f.rule for f in findings} & {"RF001", "RF002"}, set())


class StructureTreeTests(unittest.TestCase):
    """AG001 reads the structure tree, not the whole of AGENTS.md."""

    DOC = ("# AGENTS\n\n## Repository Structure\n\n```\nskills/\n  ysql/\n"
           "    SKILL.md\n```\n\n## Installation\n\n`npx skills add … -s ycql/`\n")

    def test_only_the_fenced_tree_is_searched(self):
        tree = Checker.structure_tree(self.DOC)
        self.assertIn("ysql/", tree)
        self.assertNotIn("ycql/", tree, "a mention outside the tree was included")

    def test_missing_section_falls_back_to_the_whole_document(self):
        doc = "# AGENTS\n\nNo structure section here, but ysql/ is named.\n"
        self.assertEqual(Checker.structure_tree(doc), doc)

    def test_missing_fence_falls_back_to_the_whole_document(self):
        doc = "## Repository Structure\n\nskills/ysql/ in prose, no fence.\n"
        self.assertEqual(Checker.structure_tree(doc), doc)


class DocumentationDriftTests(unittest.TestCase):
    """AGENTS.md documents what the checker does; these fail when they diverge.

    Every round of review on this repository has turned up at least one place
    where the prose and the code disagreed after a behaviour change. The prose
    is what contributors read, so the disagreement is the defect. These assert
    the parts that can be compared mechanically.
    """

    ROOT = Path(__file__).resolve().parent.parent

    @classmethod
    def setUpClass(cls) -> None:
        cls.agents = (cls.ROOT / "AGENTS.md").read_text(encoding="utf-8")
        cls.review = (cls.ROOT / "REVIEW.md").read_text(encoding="utf-8")
        cls.source = (cls.ROOT / "scripts" / "check_skills.py").read_text(encoding="utf-8")

    def row(self, group: str) -> str:
        """The `| <group> | … |` row of the rules table in AGENTS.md."""
        for line in self.agents.split("\n"):
            if line.startswith(f"| {group} |"):
                return line
        self.fail(f"AGENTS.md has no '{group}' row in the rules table")

    def test_rule_codes_named_in_docs_are_codes_the_checker_emits(self):
        emitted = set(re.findall(r'"([A-Z]{2,4}\d{3})"', self.source))
        self.assertTrue(emitted, "no rule codes found in check_skills.py")
        cited = set(re.findall(r"\b([A-Z]{2,4}\d{3})\b", self.agents + self.review))
        self.assertEqual(cited - emitted, set(),
                         "docs name rule codes the checker cannot emit")

    def test_size_budgets_in_agents_md_match_the_code(self):
        row = self.row("Size")
        for value in (check_skills.SKILL_MD_MAX_LINES,
                      check_skills.SKILL_MD_WARN_LINES,
                      check_skills.SKILL_MD_WARN_WORDS,
                      check_skills.REFERENCE_WARN_LINES):
            self.assertIn(str(value), row,
                          f"the Size row does not mention the budget {value}")

    def test_size_budgets_in_review_md_match_the_code(self):
        for value in (check_skills.SKILL_MD_MAX_LINES,
                      check_skills.SKILL_MD_WARN_LINES,
                      check_skills.REFERENCE_WARN_LINES):
            self.assertIn(str(value), self.review,
                          f"REVIEW.md does not mention the budget {value}")

    def test_frontmatter_limits_in_agents_md_match_the_code(self):
        row = self.row("Frontmatter")
        for value in (check_skills.NAME_MAX,
                      check_skills.DESCRIPTION_MAX,
                      check_skills.DESCRIPTION_MIN):
            self.assertIn(str(value), row,
                          f"the Frontmatter row does not mention the limit {value}")

    def test_spec_fields_listed_in_agents_md_match_the_code(self):
        row = self.row("Frontmatter")
        listed = re.search(r"spec defines \(([^)]*)\)", row)
        self.assertIsNotNone(listed, "the Frontmatter row does not list the spec fields")
        names = set(re.findall(r"`([^`]+)`", listed.group(1)))
        self.assertEqual(names, set(check_skills.SPEC_FIELDS))

    def test_pin_shape_count_in_agents_md_matches_the_code(self):
        row = self.row("Versions")
        words = {"three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}
        found = re.search(r"\b(%s)\b pin shapes" % "|".join(words), row)
        self.assertIsNotNone(found, "the Versions row does not count the pin shapes")
        self.assertEqual(words[found.group(1)], len(check_skills.PIN_PATTERNS))


if __name__ == "__main__":
    unittest.main()
