#!/usr/bin/env python3
"""
recall: what the engine finds against every answer key, per mechanism.

    python3 evals/yb-model/recall.py [fixture ...]

Reads fixtures/<name>/answer-key.json (bundle in fixtures/<name>/bundle) and, when present,
fixtures-private/<name>/answer-key.json (bundle at its "bundle" path, default bundle/). Runs the
static engine (no replay) on each bundle and prints, per answer-key item: hit, miss, or
judge-only (no engine_match: a defect the engine has no rule for, graded by the judge), and
per trap: ok or violated (engine_absent matched). The summary groups by mechanism (the rule an
item is matched by), not by fixture, so one customer cannot stand for a rule's recall. Private
fixtures print item ids and rule ids only.

    python3 evals/yb-model/recall.py --server [--out ledger.json] [fixture ...]

The server path, as a review runs with a scratch server: the schema from the server's catalog,
every pattern replayed, the measured pass. Each item is graded on both paths, and each hit on
the server path says what it rests on: plan (a replayed plan confirmed it), measured, catalog
(no pattern: a schema or statistics fact), model (patterns planned, nothing confirmed it), or
unplanned (a pattern of it did not plan). A ledger per fixture counts patterns that did not
plan (and the share of HOT ones), gap records by layer, replay outcomes, the measured pass's
yield and the time the server path took. An answer key may name "release" and "image".
"""

import argparse
import glob
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "skills", "yb-model", "scripts"))

from ybm import analyze, inputs, report  # noqa: E402
from ybm import server as server_mod  # noqa: E402


def fixtures(names):
    out = []
    for root in ("fixtures", "fixtures-private"):
        for key in sorted(glob.glob(os.path.join(HERE, root, "*", "answer-key.json"))):
            d = os.path.dirname(key)
            name = os.path.basename(d)
            if names and name not in names:
                continue
            with open(key, encoding="utf-8") as fh:
                ak = json.load(fh)
            bundle = os.path.join(d, ak.get("bundle", "bundle"))
            if os.path.isdir(bundle):
                out.append((name, root == "fixtures-private", bundle, ak))
    return out


def matches(m, res, md):
    """An engine_match / engine_absent item: a finding (rule, with object_contains, table or
    fact_contains to pin it), text in the
    review (section_contains), or a pattern whose query holds a fragment and that the engine
    models as a point lookup (point_query_contains)."""
    findings = res["findings"]
    if "section_contains" in m:
        return m["section_contains"] in md
    if "point_query_contains" in m:
        return any(a.get("point") for p in res["patterns"] if m["point_query_contains"]
                   in p["query"] for a in p.get("access", []))
    return any(f["rule"] == m["rule"] and m.get("object_contains", "") in f["object"] and
               (m.get("table") is None or f.get("table") == m["table"]) and
               m.get("fact_contains", "") in (f.get("fact") or "")
               for f in findings)


def grade(ak, res, md):
    """[(item id, kind, rules, verdict, matched finding dicts)] for one answer key."""
    out = []
    for item in ak.get("defects", []):
        em = item.get("engine_match") or []
        if not em:
            out.append((item["id"], "defect", ["-"], "judge-only", []))
            continue
        hits = [f for m in em for f in matching(m, res)]
        verdict = "hit" if any(matches(m, res, md) for m in em) else "miss"
        out.append((item["id"], "defect", sorted({m.get("rule", "-") for m in em}), verdict,
                    hits))
    for item in ak.get("traps", []):
        ea = item.get("engine_absent") or []
        verdict = ("violated" if any(matches(m, res, md) for m in ea) else "ok") \
            if ea else "judge-only"
        out.append((item["id"], "trap", ["-"], verdict, []))
    return out


def matching(m, res):
    """The findings an engine_match item names (none for review-text or point-lookup items)."""
    if "rule" not in m:
        return []
    return [f for f in res["findings"] if f["rule"] == m["rule"] and
            m.get("object_contains", "") in f["object"] and
            (m.get("table") is None or f.get("table") == m["table"]) and
            m.get("fact_contains", "") in (f.get("fact") or "")]


def resting_on(fs, unplanned, kinds_of):
    """What a hit on the server path rests on: facts (a catalog, statistics or workload rule),
    plan (a replayed plan showed it; "plan, assumed settings" when the settings were assumed),
    measured, model (its patterns planned and nothing confirmed it; "model, plan disagrees"
    when the planner chose another path), unplanned (a pattern of it did not plan), text (a
    review-text item)."""
    if not fs:
        return "text"
    found = set()
    for f in fs:
        conf, rp = f.get("confidence") or "", f.get("replay") or ""
        if kinds_of.get(f["rule"]) not in ("capability", "plan", "measured"):
            found.add("facts")
        elif "replay" in conf:
            found.add("plan, assumed settings" if "assumed" in conf else "plan")
        elif "measured" in conf:
            found.add("measured")
        elif set(f.get("patterns") or []) & unplanned:
            found.add("unplanned")
        elif rp.startswith("planner chose another path") or f.get("disputed"):
            found.add("model, plan disagrees")
        else:
            found.add("model")
    for k in ("plan", "plan, assumed settings", "measured", "facts", "model", "unplanned",
              "model, plan disagrees"):
        if k in found:
            return k


def server_run(bundle_dir, ak, fallback_image=None):
    """(review result, ledger) on the server path, or (None, reason)."""
    from ybm import probes, replay
    b = inputs.load(bundle_dir, release=ak.get("release"))
    img = ak.get("image") or replay.find_image(b.version)[0] or fallback_image
    if not img:
        return None, "no local image for release %s" % b.version
    t0 = time.time()

    def probe_rules(bb):  # as the review decides them, on the catalog's schema
        return sorted({f["rule"] for f in analyze.run(bb)["findings"]} &
                      set(probes.rules_with_probes()))
    plans = replay.run(b, image=img, probe_rules=probe_rules, measure=True, catalog=True)
    if plans.get("catalog_error"):  # the review falls back to the DDL text; so does this
        print("recall: catalog unavailable: %s" % plans["catalog_error"], file=sys.stderr)
    res = analyze.run(b, plans=plans)
    took = round(time.time() - t0)
    weight = {p["id"]: p.get("weight") for p in res["patterns"]}
    failed = {e["id"] for e in plans.get("patterns") or [] if not e.get("plan")}
    hot = [p for p, w in weight.items() if w == "HOT"]
    layers, outcomes = {}, {}
    for g in res.get("gaps") or []:
        layers[g["layer"]] = layers.get(g["layer"], 0) + 1
    for r in res.get("reconciliation") or []:
        o = r["outcome"].split(" on ")[0]
        outcomes[o] = outcomes.get(o, 0) + 1
    ms = res.get("measured") or {}
    done = [m for m in ms.get("patterns") or [] if "returned" in m]
    ledger = {
        "image": img, "seconds": took, "schema": "catalog" if getattr(
            b, "catalog_info", None) else "text",
        "tables": {"catalog": len(getattr(getattr(b, "catalog_schema", None), "tables", {})
                                  or {}), "replay": plans.get("tables_loaded")},
        "indexes": {"catalog": len(getattr(getattr(b, "catalog_schema", None), "indexes", {})
                                   or {}), "replay": plans.get("indexes_loaded")},
        "catalog_rejected": len((getattr(b, "catalog_info", None) or {}).get("rejected") or []),
        "access": {"plan": sum(1 for p in res["patterns"] for a in p.get("access", [])
                               if a.get("source") == "plan"),
                   "model_planned": sorted("%s %s" % (p["id"], a["table"])
                                           for p in res["patterns"] if p["id"] not in failed
                                           for a in p.get("access", [])
                                           if a.get("source") == "model")},
        "plans": {e["id"]: sorted("%s %s" % (sc["node"], sc["index"] or "")
                                  for sc in server_mod.scans(e["plan"]))
                  for e in plans.get("patterns") or [] if e.get("plan")},
        "ddl_errors": (plans.get("ddl_errors") or {}).get("count"),
        "repairs": len((plans.get("preparation") or {}).get("repairs") or []),
        "patterns": len(weight), "failed": sorted(failed), "hot": len(hot),
        "hot_failed": len(set(hot) & failed), "gaps": layers, "replay_outcomes": outcomes,
        "measured_tables": "%d/%d" % (sum(1 for v in (ms.get("tables") or {}).values()
                                          if v.get("measured")), len(ms.get("tables") or {})),
        "measured_reads": len(done),
        "meas001": sum(1 for f in res["findings"] if f["rule"] == "MEAS001"),
        "corroborated": sum(1 for f in res["findings"] if "measured" in
                            (f.get("confidence") or "") and f["rule"] != "MEAS001"),
    }
    return res, ledger


def disagreements(bundles, out, image=None, release=None):
    """Per bundle: the server ledger, and the findings (rule, object) only the static path
    makes and only the server path makes."""
    report_ = {}
    for n, path in enumerate(bundles, 1):
        try:
            static = analyze.run(inputs.load(path, release=release))
        except inputs.InputError as e:  # refused as input on both paths alike
            report_[path] = {"skipped": "input refused: %s" % str(e)[:120]}
            print("bundle-%d: input refused on both paths" % n)
            continue
        sres = None
        for attempt in (1, 2):  # a scratch server that times out once gets one more try
            try:
                sres, ledger = server_run(path, {"release": release} if release else {}, image)
                break
            except Exception as e:
                ledger = "server path failed: %s" % str(e)[:200]
        if sres is None:
            report_[path] = {"skipped": ledger}
            print("bundle-%d: skipped: %s" % (n, ledger))
            if out:
                with open(out, "w", encoding="utf-8") as fh:
                    json.dump(report_, fh, indent=1, sort_keys=True)
            continue
        a = {(f["rule"], f["object"]) for f in static["findings"]}
        b = {(f["rule"], f["object"]) for f in sres["findings"]}
        report_[path] = dict(ledger, static_only=sorted(a - b), server_only=sorted(b - a))
        print("bundle-%d: static-only %d, server-only %d, %s" % (
            n, len(a - b), len(b - a), json.dumps({k: ledger[k] for k in (
                "schema", "patterns", "hot", "hot_failed", "gaps", "seconds")}, sort_keys=True)))
        if out:  # as it goes: a later failure keeps what was measured
            with open(out, "w", encoding="utf-8") as fh:
                json.dump(report_, fh, indent=1, sort_keys=True)
    return 0


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*")
    ap.add_argument("--server", action="store_true", help="also grade the server path")
    ap.add_argument("--out", help="write the ledger (JSON) here")
    ap.add_argument("--image", help="the image for a bundle no local image matches (the "
                                     "review labels a newer image as such)")
    ap.add_argument("--release", help="the release of the --bundle bundles (when they do "
                                       "not say)")
    ap.add_argument("--bundle", action="append", default=[],
                    help="a bundle with no answer key: the server ledger and the findings the "
                         "two paths disagree on (implies --server; repeatable)")
    args = ap.parse_args(argv[1:])
    if args.bundle:
        return disagreements(args.bundle, args.out, args.image, args.release)
    rows, by_rule, ledgers = [], {}, {}
    for n, (name, private, bundle, ak) in enumerate(fixtures(args.names), 1):
        shown = "private-%d" % n if private else name  # a private name may name the case
        res = analyze.run(inputs.load(bundle, release=ak.get("release")))
        graded = grade(ak, res, report.render(res))
        sgraded = {}
        if args.server:
            sres, ledger = server_run(bundle, ak)
            if sres is None:
                ledger = {"skipped": ledger}
            else:
                unplanned = set(ledger["failed"])
                kinds_of = {r: d.get("kind") for r, d in analyze.load_rules().items()}
                sg = grade(ak, sres, report.render(sres))
                sgraded = {i: (v, resting_on(fs, unplanned, kinds_of)) for i, _, _, v, fs in sg}
                ledger["hits"] = {i: [{"rule": f["rule"], "confidence": f.get("confidence"),
                                       "replay": (f.get("replay") or "")[:160],
                                       "disputed": f.get("disputed")} for f in fs]
                                  for i, _, _, v, fs in sg if v == "hit"}
            ledgers[shown] = dict(ledger, shown=shown)
        for item, kind, rules, verdict, _ in graded:
            sv = sgraded.get(item) or ("skipped", "-")
            rows.append((shown, item, kind, ",".join(rules), verdict) +
                        ((sv[0], sv[1] if sv[0] == "hit" else "-") if args.server else ()))
            if kind == "defect":
                for r in rules:
                    by_rule.setdefault(r, []).append(verdict)
    head = ("fixture", "item", "kind", "rule", "verdict") + (
        ("server", "rests on") if args.server else ())
    fmt = "%-14s %-6s %-7s %-18s %-10s" + (" %-10s %s" if args.server else "")
    print(fmt % head)
    for r in rows:
        print(fmt % r)
    print("\nby mechanism:")
    for rule, vs in sorted(by_rule.items()):
        print("  %-14s %d hit, %d miss, %d judge-only" % (
            rule, vs.count("hit"), vs.count("miss"), vs.count("judge-only")))
    if args.server:
        print("\nledger (server path):")
        for name, lg in ledgers.items():
            print("  %s: %s" % (lg["shown"], json.dumps(
                {k: v for k, v in lg.items() if k not in ("shown", "hits", "plans")},
                sort_keys=True)))
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump({"rows": rows, "ledgers": ledgers}, fh, indent=1, sort_keys=True)
    bad = [r for r in rows if r[4] in ("miss", "violated") or
           (args.server and len(r) > 5 and r[5] in ("miss", "violated"))]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
