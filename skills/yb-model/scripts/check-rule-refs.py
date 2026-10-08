#!/usr/bin/env python3
"""
check-rule-refs: verify that every test anchor in rules/rules.json exists in a yugabyte-db
checkout, per release tag, without checking anything out.

    python3 check-rule-refs.py --repo ~/yugabyte-db --tags v2024.2.0.0 v2025.1.0.0 v2025.2.4.0

For each rule and anchor it prints which tags contain the anchor. An anchor missing from the
newest tag means the test changed and the rule's citation (and possibly the rule) needs a
look. An anchor missing from older tags marks the release that introduced the behaviour.
Exit code 1 if any anchor is missing from the newest tag given, or if a rule that claims
planner or execution behaviour (kind "capability" or "plan") cites no test. Statistics,
workload, configuration and safety rules are arithmetic on the bundle and cite none.
"""

import argparse
import json
import os
import subprocess
import sys

REGRESS = "src/postgres/src/test/regress/expected/"


def show(repo, tag, path):
    r = subprocess.run(["git", "-C", repo, "show", "%s:%s" % (tag, path)],
                       capture_output=True, text=True, errors="replace")
    return r.stdout if r.returncode == 0 else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--tags", nargs="+", required=True, help="oldest first")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args()
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "..", "rules", "rules.json")) as fh:
        rules = json.load(fh)["rules"]
    cache, rows, bad = {}, [], 0
    uncited = sorted(rid for rid, r in rules.items()
                     if r.get("kind") in ("capability", "plan") and not r.get("test"))
    for rid in uncited:
        print("%s: claims planner behaviour (kind %s) but cites no regress test" % (
            rid, rules[rid]["kind"]), file=sys.stderr)
    bad += len(uncited)
    for rid in sorted(rules):
        for ref in rules[rid].get("test", []):
            present = []
            for tag in args.tags:
                key = (tag, ref["file"])
                if key not in cache:
                    cache[key] = show(args.repo, tag, REGRESS + ref["file"])
                    if cache[key] is None:
                        # Before 2025.1, yb.orig.X.out was yb_X.out and yb.port.X.out yb_pg_X.out.
                        old = ref["file"].replace("yb.orig.", "yb_").replace("yb.port.", "yb_pg_")
                        cache[key] = show(args.repo, tag, REGRESS + old)
                txt = cache[key]
                present.append(bool(txt) and ref["anchor"] in txt)
            rows.append({"rule": rid, "file": ref["file"], "anchor": ref["anchor"],
                         "tags": dict(zip(args.tags, present))})
            if not present[-1]:
                bad += 1
    if args.format == "json":
        print(json.dumps(rows, indent=1))
    else:
        width = max(len(t) for t in args.tags)
        print("rule    " + "  ".join(t.ljust(width) for t in args.tags) + "  file :: anchor")
        for r in rows:
            marks = "  ".join(("yes" if r["tags"][t] else "--").ljust(width) for t in args.tags)
            print("%-7s %s  %s :: %s" % (r["rule"], marks, r["file"], r["anchor"][:70]))
        print("\n%d anchor(s) missing from %s" % (bad, args.tags[-1]))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
