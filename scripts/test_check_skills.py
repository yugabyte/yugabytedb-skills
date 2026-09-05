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

    def test_nested_mapping_is_skipped(self):
        fields, _, problems = self.fm(
            "---\nname: ysql\nmetadata:\n  author: x\n  version: \"1\"\ndescription: after nested\n---\n")
        self.assertIsNone(fields["metadata"])
        self.assertEqual(fields["description"], "after nested")
        self.assertEqual(problems, [])

    def test_undecodable_line_is_fm007_and_parsing_stops(self):
        fields, _, problems = self.fm("---\nname: ysql\n- not a key\ndescription: after\n---\n")
        self.assertEqual([r for r, _, _ in problems], ["FM007"])
        self.assertEqual(problems[0][1], 3)
        self.assertNotIn("description", fields)

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


if __name__ == "__main__":
    unittest.main()
