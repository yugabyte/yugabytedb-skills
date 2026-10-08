"""Preflight: which inputs the bundle lacks, and what the review mutes or weakens without them.

The definitions (labels, consequences, muted rules and stages, how to collect) live in
rules/inputs.json; this module only detects each input. The same bundle always gives the same
list, so every model asks the user the same question.
"""

import json
import os
import re

_DEFS = None


def defs():
    global _DEFS
    if _DEFS is None:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "..", "rules", "inputs.json"), encoding="utf-8") as fh:
            _DEFS = json.load(fh)["inputs"]
    return _DEFS


def _yb_metadata(bundle):
    """False when the dump looks like a plain pg_dump: no SPLIT clauses and some primary key
    without a HASH / ASC / DESC annotation (the same test as CFG004)."""
    if re.search(r"\bSPLIT\s+(INTO|AT)\b", bundle.ddl, re.I):
        return True
    from . import schema as schema_mod
    sch = schema_mod.parse(bundle.ddl)
    pks = [t.pk for t in sch.tables.values() if t.pk and not t.partition_of]
    return all(any(k.explicit for k in pk.keys) for pk in pks)


DETECT = {
    "workload": lambda b: bool(b.pss) or bool((b.queries_sql or "").strip()),
    "pss": lambda b: bool(b.pss),
    "pg_stats": lambda b: bool(b.stats),
    "reltuples": lambda b: bool(b.reltuples),
    "version": lambda b: bool(b.version),
    "settings": lambda b: bool(b.settings),
    "yb_metadata": _yb_metadata,
    "index_usage": lambda b: bool(b.index_usage),
    "table_usage": lambda b: bool(b.table_usage),
    "tablets": lambda b: bool(b.tablets),
    "docdb_stats": lambda b: any(r.get("docdb_rows_scanned") not in (None, 0.0) or
                                 r.get("docdb_read_rpcs") not in (None, 0.0) for r in b.pss),
    "plan_time": lambda b: any(r.get("total_plan_time") is not None for r in b.pss),
    "capture_date": lambda b: bool(b.meta.get("captured_at")),
}


def assess(bundle):
    """{missing: [input defs], present: [ids], muted: {rule: [input ids]},
    weakened: {rule: [(caveat, scope)]}, stages: [(stage, input id)], needs_confirmation}"""
    have = {k: bool(f(bundle)) for k, f in DETECT.items()}
    missing = []
    for d in defs():
        if have.get(d["id"], True):
            continue
        dep = d.get("skip_if_missing")
        if dep and not have.get(dep, True):
            continue  # covered by the broader missing input
        missing.append(d)
    muted, weakened, stages = {}, {}, []
    for d in missing:
        for r in d.get("mutes", []):
            muted.setdefault(r, []).append(d["id"])
        w = d.get("weakens")
        if w:
            for r in w["rules"]:
                weakened.setdefault(r, []).append((w["caveat"], w.get("scope")))
        for s in d.get("stages", []):
            stages.append((s, d["id"]))
    for d in missing:
        dc = d.get("if_declared_complete")
        if dc and getattr(bundle, "declared_complete", False):
            for r in dc["unmute"]:
                if muted.get(r) == [d["id"]]:
                    del muted[r]
                    weakened.setdefault(r, []).append((dc["caveat"], None))
    for r in muted:
        weakened.pop(r, None)
    return {"missing": missing, "present": sorted(k for k, v in have.items() if v),
            "muted": muted, "weakened": weakened, "stages": stages,
            "needs_confirmation": any(d["tier"] == "major" for d in missing),
            "declared_complete": bool(getattr(bundle, "declared_complete", False)),
            "replay_possible": have["workload"] and have["version"]}


def summary(pf):
    """The JSON-safe part of assess(), for review.json."""
    return {"missing": [{"id": d["id"], "tier": d["tier"], "label": d["label"],
                         "without": d["without"],
                         "mutes": [r for r in d.get("mutes", []) if r in pf["muted"]],
                         "weakens": (d.get("weakens") or {}).get("rules", []),
                         "declared_complete_runs": [
                             r for r in (d.get("if_declared_complete") or {}).get("unmute", [])
                             if pf.get("declared_complete")],
                         "caveat": (d.get("weakens") or {}).get("caveat"),
                         "stages": d.get("stages", []), "collect": d["collect"]}
                        for d in pf["missing"]],
            "present": pf["present"], "needs_confirmation": pf["needs_confirmation"]}


def render_prompt(pf, replay_note=None):
    """The text the skill shows the user, verbatim, before asking yes / no."""
    major = [d for d in pf["missing"] if d["tier"] == "major"]
    minor = [d for d in pf["missing"] if d["tier"] == "minor"]
    L = []
    w = L.append
    if not pf["missing"] and not replay_note:
        return "yb-model preflight: all inputs present. The review can run in full."
    if major:
        w("yb-model preflight: this review would be INCOMPLETE and may be suboptimal. "
          "%d input(s) that change the findings are missing." % len(major))
    else:
        w("yb-model preflight: the main inputs are present; some minor ones are missing.")
    for title, group in (("Missing, changes the findings", major),
                         ("Missing, minor", minor)):
        if not group:
            continue
        w("")
        w("%s:" % title)
        for n, d in enumerate(group, 1):
            w("")
            w("%d. %s" % (n, d["label"]))
            w("   Without it: %s" % d["without"])
            dc = d.get("if_declared_complete")
            mutes = [r for r in d.get("mutes", []) if r in pf["muted"]]
            if mutes:
                w("   Muted (not evaluated): %s" % ", ".join(mutes))
            if dc and pf.get("declared_complete"):
                w("   Declared complete: %s still run, marked '%s'." % (
                    ", ".join(dc["unmute"]), dc["caveat"]))
            elif dc:
                w("   Note: %s" % dc["note"])
            if d.get("weakens"):
                w("   Weakened (findings marked '%s'): %s" % (d["weakens"]["caveat"],
                                                            ", ".join(d["weakens"]["rules"])))
            if d.get("stages"):
                w("   Skipped or reduced: %s" % "; ".join(d["stages"]))
            w("   To collect: %s" % d["collect"])
    if replay_note:
        w("")
        w("Replay: %s" % replay_note)
    if major:
        w("")
        w("Proceed with the review anyway? (yes / no)")
        w("  yes: the review runs with the items above muted or weakened, and the report lists "
          "them.")
        w("  no:  collect the missing inputs with the commands above, then run again.")
    return "\n".join(L)
