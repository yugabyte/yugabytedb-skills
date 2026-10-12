"""The reviewer's plan: one direction per area, chosen from the findings' options, checked by
the engine as one combined change.

The skill writes plan.json after reading the review's areas (review.json "dossiers"):

    {"areas": [{"area": "orders",                 # any table of the area
                "direction": "plain words: what makes most sense here, and why",
                "choices": {"F5": "partial", "F9": "keep"},   # finding id -> option id
                "questions": ["what the user must decide or confirm"]}]}

`check` renders the chosen options' sketches, applies all of them together to the schema
model, and reports what the combination would break: a lost unique guarantee or ON CONFLICT
target, a foreign key left without its index, a plan regression on a ranked pattern, an index
dropped that the counters show in use, a partition's copy dropped on its own, two choices that
change one index. It never amends a choice; a conflict goes back to the skill, which revises
the plan once, and anything still in conflict is put to the user as a decision.
"""

import json

from . import analyze, changes as ch_mod, safety
from .dossier import change_class

MEDIUM = ("critical", "high", "medium")


def context(bundle):
    """The schema model, patterns and planner settings the review was made with."""
    ps = analyze.planner_settings(bundle)
    sch = analyze.build_schema(bundle)
    bundle.align_names(sch)
    return sch, analyze.build_patterns(bundle, sch), ps


def _area_of(res, name):
    for d in res.get("dossiers") or []:
        if name in d["area"] or name == ", ".join(d["area"]):
            return d
    return None


def sketch_for(f, option, sch):
    """(changes, how) for the chosen option of finding f: the engine's sketch when that is
    the option it drafted, a checked drop for any drop option on an index, nothing for an
    option that changes no schema; None when the engine has no sketch for it."""
    if f.get("section") == "disputed":
        return None, "not offered: replay disputed this finding (see the review)"
    if option["change"] == "none":
        return [], "no schema change"
    if option.get("sketched"):
        chs = [ch_mod.Change.from_dict(c) for c in f.get("changes") or []]
        if not chs:  # withheld: a foreign key depends on an index nothing in it can back
            return None, "withheld: " + "; ".join(option.get("costs") or ["no sketch"])
        return chs, "engine sketch"
    if option["change"] == "drop" and f.get("index") and (
            f["index"] in sch.indexes):
        idx = sch.indexes[f["index"]]
        if idx.unique or idx.is_pk:
            return None, "not sketched: the index enforces a unique guarantee"
        if getattr(idx, "parent", None):
            return None, "not sketched: a partition's copy changes only through %s" % idx.parent
        return analyze._check_then_drop(f["index"], sch=sch), "drop after the every-node check"
    return None, "no engine sketch for this option"


def check(res, plan, sch, patterns, ps, bundle):
    """The plan checked against the review it was written from. An area's suggestions count
    as chosen unless the plan chooses another option for that finding."""
    by_id = {f["id"]: f for f in res["findings"]}
    out = {"areas": [], "errors": [], "conflicts": []}
    combined, owner, pure_drops = [], {}, set()

    def take(fid, oid, row, origin):
        f = by_id.get(fid)
        if f is None:
            out["errors"].append("%s: no such finding" % fid)
            return
        if f.get("section") == "disputed":
            out["errors"].append("%s is disputed by replay, so it is not offered" % fid)
            return
        opt = next((o for o in f.get("options") or [] if o["id"] == oid), None)
        if opt is None:
            out["errors"].append("%s %s has no option %r (options: %s)" % (
                fid, f["rule"], oid, ", ".join(o["id"] for o in f.get("options") or [])
                or "none"))
            return
        chs, how = sketch_for(f, opt, sch)
        row["choices"].append({"finding": fid, "rule": f["rule"], "object": f["object"],
                               "option": oid, "label": opt["label"], "change": opt["change"],
                               "how": how, "origin": origin, "costs": opt.get("costs") or [],
                               "ddl": ch_mod.render(chs) if chs else None,
                               "checked": chs is not None})
        if change_class(chs) == "drop":
            pure_drops.update(c.name for c in chs if c.kind == "drop")
        for c in chs or []:
            combined.append(c)
            name = c.table if c.kind == "rekey" else c.name if c.kind in ("drop", "retire") \
                else None
            if name:  # two choices that drop, rebuild or re-key one object
                owner.setdefault(name, set()).add(fid)

    areas = plan.get("areas")
    if not isinstance(areas, list):
        out["errors"].append('"areas" must be a list of objects')
        areas = []
    for a in areas:
        if not isinstance(a, dict):
            out["errors"].append("an area must be an object, not %r" % (a,))
            continue
        if not isinstance(a.get("choices") or {}, dict):
            out["errors"].append('%s: "choices" must map finding ids to option ids' % (
                a.get("area"),))
            a = dict(a, choices={})
        qs = a.get("questions") or []
        if isinstance(qs, str):
            qs = [qs]
        if not isinstance(qs, list) or not all(isinstance(q, str) for q in qs):
            out["errors"].append('%s: "questions" must be a list of strings' % (a.get("area"),))
            qs = [q for q in qs if isinstance(q, str)] if isinstance(qs, list) else []
        for k in ("diagnosis", "direction"):
            if not isinstance(a.get(k, ""), str):
                out["errors"].append('%s: "%s" must be a string' % (a.get("area"), k))
                a = dict(a, **{k: ""})
        name = a.get("area") if isinstance(a.get("area"), str) else str(a.get("area") or "")
        d = _area_of(res, name)
        row = {"area": d["area"] if d else [name or "(no area named)"],
               "diagnosis": a.get("diagnosis", ""), "direction": a.get("direction", ""),
               "questions": [str(q) for q in qs], "choices": [],
               "unchosen": []}
        out["areas"].append(row)
        if d is None:
            out["errors"].append("no area of the review has table %r" % name)
            continue
        chosen = dict(a.get("choices") or {})
        for fid, oid in sorted(chosen.items()):
            take(fid, str(oid), row, "choice")
        for sg in d.get("suggestions") or []:
            if sg["finding"] not in chosen:
                take(sg["finding"], sg["option"], row, "suggestion")
        decided = {c["finding"] for c in row["choices"]}
        row["unchosen"] = [x["finding"] for dc in d.get("decisions") or []
                           for x in dc["findings"] if x["finding"] not in decided]
    named = {tuple(a["area"]) for a in out["areas"]}
    for d in res.get("dossiers") or []:  # areas the plan leaves alone keep their suggestions
        if tuple(d["area"]) in named or not d.get("suggestions"):
            continue
        row = {"area": d["area"], "diagnosis": "", "direction": "", "questions": [],
               "choices": [], "unchosen": [], "suggestions_only": True}
        for sg in d["suggestions"]:
            take(sg["finding"], sg["option"], row, "suggestion")
        out["areas"].append(row)
    out["conflicts"] = conflicts(sch, patterns, ps, bundle, combined, owner, pure_drops)
    out["unchecked"] = ["%s %s (%s): %s" % (c["finding"], c["rule"], c["option"], c["how"])
                        for a in out["areas"] for c in a["choices"] if not c["checked"]]
    out["ok"] = not out["errors"] and not out["conflicts"] and not out["unchecked"]
    return out


def conflicts(sch, patterns, ps, bundle, combined, owner, pure_drops=()):
    """What the chosen changes, applied together, would break."""
    out = []
    model = ch_mod.model(combined)
    if not model:
        return out
    after = safety.apply(sch, model)
    for table, keys, where, name in safety.lost_uniqueness(sch, after):
        out.append("the unique guarantee of %s on %s (%s) is lost" % (
            name, table, ", ".join(sorted(keys))))
    for pid, table, target in safety.lost_conflict_targets(sch, after, patterns):
        out.append("%s (INSERT ... ON CONFLICT %s on %s) loses its unique index" % (
            pid, ", ".join(target), table))
    for c in model:
        if c.kind != "drop" or c.name not in sch.indexes:
            continue
        idx = sch.indexes[c.name]
        for fk in sch.fks_on_index(idx):
            if not safety._fk_backed(after, fk):
                out.append("foreign key %s on %s references %s through %s, which is dropped "
                           "and not replaced" % (fk.name, fk.table, idx.table, c.name))
        if getattr(idx, "parent", None):
            out.append("%s is a partition's copy of %s; YSQL drops it only through %s" % (
                c.name, idx.parent, idx.parent))
        u = analyze.index_usage_of(sch, bundle, c.name)
        if c.name in pure_drops and u is not None and u["idx_scan"] > 0:
            out.append("%s is dropped, but the counters show %d scans" % (c.name, u["idx_scan"]))
    for name, fids in sorted(owner.items()):
        if len(fids) > 1:
            out.append("%s each change %s: choose one change for it" % (
                " and ".join(sorted(fids)), name))
    out.extend(safety.ddl_conflicts(model))
    weights = {p["id"]: p["weight"] for p in patterns}
    # Every pattern, as the option checks use (safety.costs): what an option costs alone and
    # what the combination costs are read the same way.
    regs = safety.regressions(safety.access_map(sch, patterns, analyze, ps["cbo"]),
                              safety.access_map(after, patterns, analyze, ps["cbo"]), weights)
    for r in regs:
        out.append("%s (%s) on %s %s" % (r["pattern"], r["weight"], r["table"], r["why"]))
    return out


def render(chk):
    """plan.md: the reviewer's direction per area, the engine's check, and the sketches of the
    chosen options."""
    L = ["# Suggested direction", "",
         "Written by the reviewer from the review's findings and options. Several answers can be "
         "right, depending on how the application will grow: these are suggestions to discuss "
         "with the user, who decides, destructive options included. The engine checked the "
         "chosen options and the area's suggestions together.", ""]
    if chk["errors"]:
        L += ["**The plan has errors:** " + "; ".join(chk["errors"]) + ".", ""]
    L += ["**Engine check of the combined choices:** %s" % (
        "no conflict found (uniqueness, ON CONFLICT targets, foreign keys, partition copies, "
        "indexes in use, and the plans of ranked patterns)." if not chk["conflicts"] else
        "conflicts, to settle with the user:"), ""]
    for c in chk["conflicts"]:
        L.append("- %s" % c)
    if chk["conflicts"]:
        L.append("")
    if chk.get("unchecked"):
        L += ["Not checked, because the engine has no model of these options: put them to "
              "the user as open questions, with their trade-offs: "
              "%s." % "; ".join(chk["unchecked"]), ""]
    for a in chk["areas"]:
        L += ["## %s" % ", ".join(str(x) for x in a["area"][:6]), ""]
        if a.get("suggestions_only"):
            L += ["The plan does not discuss this area; its suggestions are included as the "
                  "engine made them.", ""]
        else:
            if a.get("diagnosis"):
                L += ["**Diagnosis:** %s" % a["diagnosis"], ""]
            L += ["**Direction:** %s" % (a["direction"] or "(none given)"), ""]
        for c in a["choices"]:
            L.append("- %s %s on %s: %s (%s%s; %s)%s" % (
                c["finding"], c["rule"], c["object"], c["label"], c["change"],
                ", the engine's suggestion" if c["origin"] == "suggestion" else "", c["how"],
                (" Known cost: %s." % "; ".join(c["costs"])) if c["costs"] else ""))
        if a["unchosen"]:
            L.append("- Not yet decided: %s" % ", ".join(a["unchosen"]))
        if a["questions"]:
            L += ["", "Questions for the user:"] + ["- %s" % q for q in a["questions"]]
        sk = [c for c in a["choices"] if c["ddl"]]
        if sk:
            L += ["", "DDL for these choices, once the user agrees to them: `python3 "
                  "scripts/yb-model.py sketch <bundle> <finding> --option <id>` (%s)." % ", ".join(
                      "%s %s" % (c["finding"], c["option"]) for c in sk)]
        L.append("")
    return "\n".join(L)


def load(path):
    with open(path) as fh:
        return json.load(fh)
