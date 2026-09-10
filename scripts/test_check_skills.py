#!/usr/bin/env python3
"""Tests for scripts/check_skills.py. Standard library only.

    python3 -m unittest discover -s scripts -p 'test_*.py' -v
"""
from __future__ import annotations

import json
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

    def test_flow_sequence_is_unsupported(self):
        _, _, problems = self.fm("---\ndescription: [flow, seq]\n---\n")
        self.assertTrue(any(r == "FM007" and "unsupported" in msg for r, _, msg in problems))

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


if __name__ == "__main__":
    unittest.main()
