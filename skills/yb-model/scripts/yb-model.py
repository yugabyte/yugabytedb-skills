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
    python3 yb-model.py sketch  <bundle|review.json> <finding> [--option id]  # chosen DDL
    python3 yb-model.py plan    <bundle> plan.json      # check the reviewer's choices together
    python3 yb-model.py pitfall list | export | add <bundle> --id ID --title T --body B

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
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ybm import analyze, inputs, report  # noqa: E402


def _replay(bundle, args):
    from ybm import probes, replay

    def probe_rules(b):  # probes run only for rules that fire on this bundle's schema
        fired = {f["rule"] for f in analyze.run(b)["findings"]}
        return sorted(fired & set(probes.rules_with_probes()))
    # A server runs: the schema model is its catalog, read from the replay's own database.
    return replay.run(bundle, image=args.image, mode=args.mode, keep=args.keep,
                      probe_rules=probe_rules, measure=getattr(args, "measure", False),
                      catalog=True)


def _replay_note(bundle, pf, args=None):
    """Why replay will not run, or None when it will."""
    if "workload" not in pf["present"]:
        return "muted: no workload to replay"
    if "version" not in pf["present"]:
        return "muted: release unknown, so no image can be chosen"
    from ybm import replay
    try:
        img, exact = replay.find_image(bundle.version, getattr(args, "image", None))
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
        p.add_argument("--measure", action="store_true",
                       help="with replay: run the single-table reads on a small generated "
                            "sample shaped by the statistics and report what DocDB did "
                            "(seeks and rows scanned per row returned)")
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
    p = sub.add_parser("sketch", help="print the DDL of a finding's chosen option, with what "
                                      "checking it found")
    p.add_argument("review", help="the bundle (its review.json) or a review.json")
    p.add_argument("finding", help="finding id, e.g. F3")
    p.add_argument("--option", help="the chosen option's id (default: the engine's)")
    p.add_argument("--bundle", help="the bundle the review was made from (default: the "
                                    "directory review.json is in)")
    p.add_argument("--catalog", action="store_true", help="the review was made with --catalog")
    p.add_argument("--image", help="image for --catalog")
    release_arg(p)
    p = sub.add_parser("plan", help="check the reviewer's plan.json (one direction per area, "
                                     "chosen from the options) as one combined change; writes "
                                     "plan.md")
    p.add_argument("bundle")
    p.add_argument("plan", help="plan.json written from the review's areas")
    p.add_argument("--review", help="review.json (default: <bundle>/review.json)")
    p.add_argument("--out", help="where plan.md and plan-check.json go (default: the bundle)")
    p.add_argument("--catalog", action="store_true", help="the review was made with --catalog")
    p.add_argument("--image", help="image for --catalog")
    release_arg(p)
    p = sub.add_parser("pitfall", help="known gaps: list the skill's and your own, add one of "
                                       "yours, or export yours for a pull request")
    p.add_argument("action", choices=["list", "add", "export"])
    p.add_argument("bundle", nargs="?",
                   help="for add: the bundle the entry came from; its names are refused")
    p.add_argument("--id")
    p.add_argument("--title")
    p.add_argument("--status", default="not detected",
                   choices=["handled", "flagged", "not detected"])
    p.add_argument("--body")
    args = ap.parse_args()
    stated = getattr(args, "stated_release", None)
    if stated and not inputs.parse_release(stated):
        print("yb-model: --release needs a YugabyteDB release such as 2025.2.3.0, not %r"
              % stated, file=sys.stderr)
        return 2

    if args.cmd == "update-versions":
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

    if args.cmd == "sketch":
        from ybm import plan as plan_mod
        if os.path.isdir(args.review):
            path = os.path.join(args.review, "review.json")
        elif args.review.endswith(".sql"):  # a one-file bundle: the review sits beside it
            path = os.path.join(os.path.dirname(os.path.abspath(args.review)), "review.json")
        else:
            path = args.review
        try:
            with open(path) as fh:
                res = json.load(fh)
        except (OSError, ValueError) as e:
            print("yb-model: cannot read %s: %s" % (path, e), file=sys.stderr)
            return 2
        f = next((x for x in res["findings"] if x.get("id") == args.finding), None)
        opts = (f or {}).get("options") or []
        o = next((o for o in opts if (o["id"] == args.option if args.option
                                      else o.get("sketched"))), None)
        if f is None or o is None:
            print("yb-model: %s has no %s in %s%s" % (
                args.finding, "option %r" % args.option if args.option else "modelled option",
                path, (" (options: %s)" % ", ".join(x["id"] for x in opts)) if opts else ""),
                file=sys.stderr)
            return 2
        sch = None
        if not o.get("sketched") and o["change"] != "none":
            src = args.bundle or (args.review if os.path.isdir(args.review) or
                                  args.review.endswith(".sql")
                                  else os.path.dirname(os.path.abspath(path)))
            try:  # a drop option is modelled against the schema, as the review was
                sb = inputs.load(src, release=stated)
                if not sb.ddl.strip():
                    raise inputs.InputError("no schema in %s (pass --bundle)" % src)
                if args.catalog:
                    from ybm import catalog
                    catalog.attach(sb, image=args.image)
                sch = plan_mod.context(sb)[0]
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError,
                    inputs.InputError) as e:  # attach: a failed, slow or garbled load
                print("yb-model: cannot read the bundle: %s" % e, file=sys.stderr)
                return 2
        chs, how = plan_mod.sketch_for(f, o, sch)
        if not chs:
            print("yb-model: no DDL for %s %s: %s" % (f["id"], o["id"], how), file=sys.stderr)
            return 2
        from ybm.changes import render
        print("-- %s %s on %s: %s." % (f["id"], f["rule"], f["object"], o["label"]))
        print("-- Check first: %s." % o["check_first"])
        print("-- Checked alone: %s." % ("; ".join(o["costs"]) if o.get("costs") else
                                         "no cost found" if o.get("checked") else "not checked"))
        print("-- Checked with the other choices: `yb-model.py plan`. Parse-check it on a scratch "
              "instance of the customer's release, never on the customer's cluster.")
        print(render(chs))
        return 0

    if args.cmd == "plan":
        from ybm import plan as plan_mod
        base = args.bundle if os.path.isdir(args.bundle) else \
            os.path.dirname(os.path.abspath(args.bundle))
        rpath = args.review or os.path.join(base, "review.json")
        try:
            with open(rpath) as fh:
                res = json.load(fh)
            pl = plan_mod.load(args.plan)
            bundle = inputs.load(args.bundle, release=stated)
        except (OSError, ValueError, inputs.InputError) as e:
            print("yb-model: cannot read the plan, the review or the bundle: %s" % e,
                  file=sys.stderr)
            return 2
        if not isinstance(pl, dict):
            print("yb-model: plan.json must hold an object with \"areas\"", file=sys.stderr)
            return 2
        if args.catalog:
            from ybm import catalog
            try:
                catalog.attach(bundle, image=args.image)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as e:
                print("yb-model: cannot load the schema into the scratch server: %s" % e,
                      file=sys.stderr)
                return 2
        sch, patterns, ps = plan_mod.context(bundle)
        chk = plan_mod.check(res, pl, sch, patterns, ps, bundle)
        out = args.out or base
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "plan-check.json"), "w") as fh:
            json.dump(chk, fh, indent=1, sort_keys=True)
        with open(os.path.join(out, "plan.md"), "w") as fh:
            fh.write(plan_mod.render(chk))
        state = ("%d error(s) in the plan" % len(chk["errors"]) if chk["errors"] else
                 "; ".join(x for x in (
                     "%d conflict(s)" % len(chk["conflicts"]) if chk["conflicts"] else "",
                     "%d choice(s) not checked" % len(chk["unchecked"]) if chk["unchecked"]
                     else "") if x) + " for the user to settle"
                 if chk["conflicts"] or chk["unchecked"] else "no conflict")
        chat = os.path.join(out, "chat.md")
        if os.path.exists(chat):  # one engine-written line pointing at the direction
            with open(chat) as fh:
                lines = [l for l in fh.read().splitlines()
                         if not l.startswith("Suggested direction: ")]
            at = next((i for i, l in enumerate(lines) if l.startswith("Full review: ")),
                      len(lines))
            lines.insert(at, "Suggested direction: %s (engine check: %s)" % (
                os.path.join(out, "plan.md"), state))
            with open(chat, "w") as fh:
                fh.write("\n".join(lines) + "\n")
        print("plan: %d area(s); %s; wrote plan.md" % (len(chk["areas"]), state))
        for e in chk["errors"] + chk["conflicts"] + chk["unchecked"]:
            print("  - %s" % e)
        return 2 if chk["errors"] else (1 if chk["conflicts"] or chk["unchecked"] else 0)

    if args.cmd == "pitfall":
        from ybm import pitfalls
        if args.action == "list":
            print(pitfalls.listing())
            return 0
        if args.action == "export":
            print(pitfalls.export() or "No entries of yours that the skill does not have.")
            return 0
        if not args.bundle or not os.path.exists(args.bundle):
            print("yb-model: pitfall add needs the bundle the entry came from, so its names "
                  "can be kept out", file=sys.stderr)
            return 2
        terms = pitfalls.raw_terms(args.bundle)
        try:
            terms |= pitfalls.private_terms(inputs.load(args.bundle))
        except inputs.InputError:
            pass  # unreadable as a bundle: the names in its CREATE statements still count
        try:
            path = pitfalls.add(args.id, args.title, args.status, args.body, terms)
        except pitfalls.Refused as e:
            print("yb-model: not added: %s." % e, file=sys.stderr)
            return 2
        print("Added %s to %s." % (args.id, path))
        return 0

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
        if args.cmd in ("review", "preflight"):
            from ybm import pitfalls
            pitfalls.record_hits(["input-unreadable"])
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
        note = _replay_note(bundle, pf, args)
        if args.json:
            print(json.dumps(dict(preflight.summary(pf), replay=note), indent=1))
        else:
            print(preflight.render_prompt(pf, note))
        return 3 if pf["needs_confirmation"] else 0
    if args.cmd == "review" and pf["needs_confirmation"] and not args.accept_missing:
        print(preflight.render_prompt(pf, _replay_note(bundle, pf, args)))
        print("\nyb-model: review not run. Show the text above to the user and ask yes / no; "
              "only on yes, run again with --accept-missing.", file=sys.stderr)
        return 3

    plans = None
    if getattr(args, "plans", None):
        with open(args.plans) as fh:
            plans = json.load(fh)
    auto = False
    if args.cmd == "review" and plans is None and not args.no_replay and not args.replay:
        from ybm import replay as _rp
        try:
            auto = pf["replay_possible"] and bool(_rp.find_image(bundle.version,
                                                                 args.image)[0])
        except Exception:
            auto = False
        if not pf["replay_possible"]:
            print("yb-model: replay muted: %s." % _replay_note(bundle, pf, args),
                  file=sys.stderr)
        elif not auto:
            print("yb-model: replay skipped: no Docker or no local yugabytedb/yugabyte image for "
                  "version %s." % (bundle.version or "unknown"), file=sys.stderr)
    if args.cmd == "replay" or (args.cmd == "review" and (args.replay or auto) and plans is None):
        try:
            plans = _replay(bundle, args)
            if plans.get("catalog_error"):
                print("yb-model: catalog unavailable, using the DDL text: %s" %
                      plans["catalog_error"], file=sys.stderr)
        except Exception as e:
            print("yb-model: replay failed: %s" % e, file=sys.stderr)
            if args.cmd == "replay" or getattr(args, "replay", False):
                return 2
            plans = None
        if args.cmd == "replay":
            print(json.dumps(plans, indent=1, sort_keys=True))
            return 0

    # With no workload there is nothing to replay, but a scratch server still reads the schema
    # from its catalog when the release and a local image are known (--no-replay opts out).
    catalog_only = (args.cmd == "review" and plans is None and not args.no_replay and
                    "workload" not in pf["present"] and "version" in pf["present"])
    if catalog_only:
        from ybm import replay as _rp
        try:
            catalog_only = bool(_rp.find_image(bundle.version, args.image)[0])
        except Exception:
            catalog_only = False
    if (getattr(args, "catalog", False) or catalog_only) and \
            getattr(bundle, "catalog_schema", None) is None:
        # --catalog without a replay (a replay reads the catalog from its own database)
        from ybm import catalog
        try:
            catalog.attach(bundle, image=args.image)
        except Exception as e:
            print("yb-model: catalog unavailable, using the DDL text: %s" % e, file=sys.stderr)

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
        from ybm import pitfalls
        pitfalls.record_hits(res.get("pitfalls"))
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
