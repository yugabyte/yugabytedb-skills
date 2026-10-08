#!/usr/bin/env python3
"""
yb-model: deterministic YugabyteDB YSQL schema review.

    python3 yb-model.py preflight <bundle>      # what is missing; exit 3 if the user must confirm
    python3 yb-model.py review  <bundle> [--accept-missing] [--no-replay] [--image IMG] [--mode customer|on|off]
                                         [--out DIR] [--release R]
        (replay runs automatically when Docker has a yugabytedb/yugabyte image for the
        bundle's version; --no-replay skips it)
    python3 yb-model.py analyze <bundle> [--plans plans.json]      # JSON to stdout
    python3 yb-model.py replay  <bundle> [--image IMG] [--mode ...] # plans JSON to stdout
    python3 yb-model.py report  <analysis.json>                     # Markdown to stdout

<bundle> is a directory written by collect.sql plus schema.sql (and optionally queries.sql),
or a single .sql file. `review` runs analyze, optionally replay, and writes review.json,
review.md and chat.md (the chat message) into --out (default: the bundle directory).

The release is read from SELECT version() (ybm_meta.csv), pg_settings server_version or the
ysql_dump header. --release (on every command that takes a bundle) states it when the user
gives it, and takes precedence.

`review` refuses to run (exit 3) while a major input is missing, until the user has said yes
to the preflight prompt and the skill passes --accept-missing.

Exit codes: 0 ok, 1 findings at high or above, 2 bad invocation or tool failure,
3 inputs missing and not accepted.
Python 3.8+, standard library only. Replay needs Docker and a local yugabytedb/yugabyte image.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ybm import analyze, inputs, report  # noqa: E402


def _replay(bundle, args):
    from ybm import probes, replay
    # Probes run only for rules that fire on this bundle; a quick static pass decides which.
    fired = {f["rule"] for f in analyze.run(bundle)["findings"]}
    return replay.run(bundle, image=args.image, mode=args.mode, keep=args.keep,
                      probe_rules=sorted(fired & set(probes.rules_with_probes())))


def _replay_note(bundle, pf):
    """Why replay will not run, or None when it will."""
    if "workload" not in pf["present"]:
        return "muted: no workload to replay"
    if "version" not in pf["present"]:
        return "muted: release unknown, so no image can be chosen"
    from ybm import replay
    try:
        img, exact = replay.find_image(bundle.version)
    except Exception:
        return "will not run: Docker is not available"
    if not img:
        return ("will not run: no local yugabytedb/yugabyte image for %s (pulling one is about "
                "1 GB; ask separately)" % bundle.version)
    return None if exact else ("will run on %s, the nearest local image to %s; differences "
                               "are reported" % (img, bundle.version))


def main():
    ap = argparse.ArgumentParser(description="Deterministic YugabyteDB YSQL schema review.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def release_arg(p):
        p.add_argument("--release", dest="stated_release", metavar="RELEASE",
                       help="the customer's YugabyteDB release (e.g. 2025.2.3.0) when the user "
                            "states it; it takes precedence over SELECT version(), pg_settings "
                            "server_version and the ysql_dump header")

    for name in ("review", "analyze", "replay"):
        p = sub.add_parser(name)
        p.add_argument("bundle")
        release_arg(p)
        p.add_argument("--image", help="yugabytedb/yugabyte:<tag> to replay on")
        p.add_argument("--mode", choices=["customer", "on", "off"], default="customer",
                       help="planner mode for replay: the customer's settings, or force the "
                            "cost model on or off")
        p.add_argument("--keep", action="store_true", help="keep the replay container")
        p.add_argument("--plans", help="plans.json from a previous replay")
        p.add_argument("--catalog", action="store_true",
                       help="build the schema from a scratch YugabyteDB (a local container "
                            "the reviewer runs): load the DDL there and read the catalog")
        if name == "review":
            p.add_argument("--replay", action="store_true",
                           help="require replay (fail loudly if it cannot run)")
            p.add_argument("--no-replay", action="store_true",
                           help="skip replay even when Docker and a matching image exist")
            p.add_argument("--out")
            p.add_argument("--accept-missing", action="store_true",
                           help="run although preflight lists missing inputs (only after the "
                                "user said yes to the preflight prompt)")
    p = sub.add_parser("preflight", help="list missing inputs and what they mute; exit 3 when "
                                         "the user must confirm before a review")
    p.add_argument("bundle")
    p.add_argument("--json", action="store_true")
    release_arg(p)
    p = sub.add_parser("update-versions",
                       help="build or refresh the local release cache (rules/versions.json) from "
                            "yugabyte-db; never committed")
    p.add_argument("--repo", help="local yugabyte-db checkout (all releases, or --release)")
    p.add_argument("--github", action="store_true",
                   help="fetch from github.com (network; ask the user first)")
    p.add_argument("--release", action="append")
    p = sub.add_parser("probe", help="run rule probes on a local image (no customer data)")
    p.add_argument("--image", required=True, help="local yugabytedb/yugabyte:<tag>")
    p.add_argument("--rule", action="append", help="rule id (default: every rule with a probe)")
    p.add_argument("--mode", choices=["on", "off", "default"], default="default")
    p = sub.add_parser("fixpoint", help="apply the engine's own DDL and check it converges")
    p.add_argument("bundle")
    release_arg(p)
    p = sub.add_parser("report")
    p.add_argument("analysis")
    args = ap.parse_args()
    stated = getattr(args, "stated_release", None)
    if stated and not inputs.parse_release(stated):
        print("yb-model: --release needs a YugabyteDB release such as 2025.2.3.0, not %r"
              % stated, file=sys.stderr)
        return 2

    if args.cmd == "update-versions":
        import subprocess
        cmd = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "extract-version-data.py")]
        if args.repo:
            cmd += ["--repo", args.repo]
        if args.github:
            cmd += ["--github"]
        for r in args.release or []:
            cmd += ["--release", r]
        return subprocess.call(cmd)

    if args.cmd == "probe":
        from ybm import probes, replay
        ids = args.rule or sorted(probes.rules_with_probes())
        with replay.Container(args.image, "ybm-probe-%d" % os.getpid()) as c:
            res = probes.run(c, ids, probes.mode_sql(args.mode))
        for rid in sorted(res):
            r = res[rid]
            print("%s %s%s: %s" % (rid, r["verdict"], (" (%s)" % r["detail"]) if r["detail"]
                                   else "", probes.describe(rid, r)))
        return 0 if all(r["verdict"] == "holds" for r in res.values()) else 1

    if args.cmd == "fixpoint":
        from ybm import fixpoint
        try:
            bundle = inputs.load(args.bundle, release=stated)
        except inputs.InputError as e:
            print("yb-model: cannot read the bundle: %s." % e, file=sys.stderr)
            return 2
        out = fixpoint.check(bundle)
        print(json.dumps(out, indent=1))
        return 1 if out["problems"] else 0

    if args.cmd == "report":
        with open(args.analysis) as fh:
            print(report.render(json.load(fh)))
        return 0

    if not os.path.exists(args.bundle):
        print("yb-model: no such bundle: %s" % args.bundle, file=sys.stderr)
        return 2
    try:
        bundle = inputs.load(args.bundle, release=stated)
    except inputs.InputError as e:
        print("yb-model: cannot read the bundle: %s." % e, file=sys.stderr)
        return 2
    if not bundle.ddl.strip():
        print("yb-model: no DDL found in %s (expected schema.sql)" % args.bundle, file=sys.stderr)
        return 2
    if not analyze.build_schema(bundle).tables:
        print("yb-model: no table could be read from %s; it may not be a schema dump, or be in "
              "another encoding or format. Convert a copy of it and run again." % ", ".join(
                  os.path.basename(p) for p in bundle.ddl_files), file=sys.stderr)
        return 2

    from ybm import preflight
    pf = preflight.assess(bundle)
    if args.cmd == "preflight":
        note = _replay_note(bundle, pf)
        if args.json:
            print(json.dumps(dict(preflight.summary(pf), replay=note), indent=1))
        else:
            print(preflight.render_prompt(pf, note))
        return 3 if pf["needs_confirmation"] else 0
    if args.cmd == "review" and pf["needs_confirmation"] and not args.accept_missing:
        print(preflight.render_prompt(pf, _replay_note(bundle, pf)))
        print("\nyb-model: review not run. Show the text above to the user and ask yes / no; "
              "only on yes, run again with --accept-missing.", file=sys.stderr)
        return 3

    if getattr(args, "catalog", False):
        from ybm import catalog
        try:
            catalog.attach(bundle, image=args.image)
        except Exception as e:
            print("yb-model: catalog unavailable, using the DDL text: %s" % e, file=sys.stderr)

    plans = None
    if getattr(args, "plans", None):
        with open(args.plans) as fh:
            plans = json.load(fh)
    auto = False
    if args.cmd == "review" and plans is None and not args.no_replay and not args.replay:
        from ybm import replay as _rp
        try:
            auto = pf["replay_possible"] and bool(_rp.find_image(bundle.version)[0])
        except Exception:
            auto = False
        if not pf["replay_possible"]:
            print("yb-model: replay muted: %s." % _replay_note(bundle, pf), file=sys.stderr)
        elif not auto:
            print("yb-model: replay skipped: no Docker or no local yugabytedb/yugabyte image for "
                  "version %s." % (bundle.version or "unknown"), file=sys.stderr)
    if args.cmd == "replay" or (args.cmd == "review" and (args.replay or auto) and plans is None):
        try:
            plans = _replay(bundle, args)
        except Exception as e:
            print("yb-model: replay failed: %s" % e, file=sys.stderr)
            if args.cmd == "replay":
                return 2
            plans = None
        if args.cmd == "replay":
            print(json.dumps(plans, indent=1, sort_keys=True))
            return 0

    res = analyze.run(bundle, plans=plans)
    if getattr(bundle, "catalog_info", None):
        res["schema_source"] = dict(bundle.catalog_info, source="scratch server catalog")
    if args.cmd == "review":
        # Self-check: apply the engine's own DDL and confirm the review converges.
        from ybm import fixpoint
        try:
            res["fixpoint"] = fixpoint.check(bundle, final=res)
        except Exception as e:  # never hide a self-check failure
            res["fixpoint"] = {"problems": ["self-check failed to run: %s" % e]}
    if args.cmd == "analyze":
        print(json.dumps(res, indent=1, sort_keys=True))
    else:
        out = args.out or (args.bundle if os.path.isdir(args.bundle) else
                           os.path.dirname(os.path.abspath(args.bundle)))
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "review.json"), "w") as fh:
            json.dump(res, fh, indent=1, sort_keys=True)
        if plans is not None:
            with open(os.path.join(out, "plans.json"), "w") as fh:
                json.dump(plans, fh, indent=1, sort_keys=True)
        md = report.render(res)
        with open(os.path.join(out, "review.md"), "w") as fh:
            fh.write(md)
        with open(os.path.join(out, "chat.md"), "w") as fh:
            fh.write(report.render_chat(res, os.path.join(out, "review.md")))
        sev = {}
        for f in res["findings"]:
            sev[f["severity"]] = sev.get(f["severity"], 0) + 1
        print("yb-model: %d finding(s) %s; %d pattern(s); replay %s." % (
            len(res["findings"]), json.dumps(sev, sort_keys=True), len(res["patterns"]),
            "ran" if res.get("replay") else "not run"))
        print("wrote %s, %s and %s (the chat message: paste it unchanged)" % (
            os.path.join(out, "review.md"), os.path.join(out, "review.json"),
            os.path.join(out, "chat.md")))
    worst = min((analyze.SEV.index(f["severity"]) for f in res["findings"]), default=99)
    return 1 if worst <= analyze.SEV.index("high") else 0


if __name__ == "__main__":
    sys.exit(main())
