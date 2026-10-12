"""Self-check: every DDL sketch must fix what it was drafted for, and nothing else.

Sketches are alternatives (several findings may sketch different changes to one index), so
they are not applied all together. They are grouped into batches whose sketches touch
different objects; each batch is applied to a copy of the schema, which is reviewed again
statically. Each finding whose sketch was applied must be gone after its batch, and no new
schema finding at medium or above may appear (workload and plan findings describe the
workload as measured and are not re-checked). A batch that adds one is re-run one sketch at
a time to name the sketch responsible; one that only the combination brings is reported for
the batch.

It needs no answer key, so it runs on every fixture, including private customer bundles. The
combination the reviewer chooses is checked by `yb-model.py plan`.
"""

import re

from . import analyze, safety
from . import schema as schema_mod
from .changes import Change


def _subject(f):
    m = re.search(r"\(([^()]*)\)\s*$", f["object"] or "")
    return (f["rule"], f.get("table"), m.group(1) if m else None)


def _changes(res):
    out = []
    for f in res["findings"]:
        if "changes" in f:
            out.extend(Change.from_dict(c) for c in f["changes"])
        else:  # a review.json from before findings carried structured changes
            out.extend(safety.changes_of(f.get("ddl")))
    return out


def _serious(res):
    return {_subject(f) for f in res["findings"]
            if f["severity"] in ("critical", "high", "medium")}


def _bare(name):
    """An index key's name without its schema, as the engine splits keys (schema.bare)."""
    return schema_mod.bare(name or "").strip('"')


def _replacements(fs):
    """Replacement index name -> the index it replaces, for sketches that create one index and
    drop another (read from the changes' names, never from their SQL)."""
    out = {}
    for f in fs:
        made = [c.get("name") for c in f.get("changes") or [] if c["kind"] == "create"]
        gone = [c["name"] for c in f.get("changes") or [] if c["kind"] == "drop"]
        if len(made) == 1 and made[0] and len(gone) == 1:
            out[made[0].strip('"')] = _bare(gone[0])  # a created name is bare already
    return out


def _as_original(obj, names):
    for new, old in names.items():
        obj = re.sub(r"(?<![\w$])%s(?![\w$])" % re.escape(new), old, obj)
    return obj


def _touched(f):
    """The objects a sketch removes or re-keys: two sketches that share one are alternatives."""
    return {c.get("name") or c.get("table") for c in f.get("changes") or []
            if c["kind"] in ("drop", "retire", "rekey")}


def batches(findings):
    """Greedy grouping of sketches so that no batch holds two that touch one object."""
    out = []
    for f in findings:
        t = _touched(f)
        for b in out:
            if not (b["touched"] & t):
                b["findings"].append(f)
                b["touched"] |= t
                break
        else:
            out.append({"findings": [f], "touched": set(t)})
    return [b["findings"] for b in out]


def _apply(sch0, fs):
    return safety.apply(sch0, [c for f in fs for c in _changes({"findings": [f]})])


def check(bundle, final=None):
    sch0 = analyze.build_schema(bundle)
    r1 = final if final is not None else analyze.run(bundle, schema_override=sch0)
    # Every default option the review offers is re-checked, those with a known cost included
    # (a problem one brings is reported with its cost). Disputed findings are not offered.
    fixed = [f for f in r1["findings"] if f.get("ddl") and f.get("section") not in (
        "hygiene", "disputed")]
    # The re-runs are static: findings that replay or a probe disputed or refuted (removed
    # from the review, listed under reconciliation) come back in them and are not new.
    before = {(f["rule"], f["object"]) for f in r1["findings"]} | {
        (r["finding"], r["object"]) for r in r1.get("reconciliation") or []}
    if r1.get("replay"):  # a replayed plan's access may have replaced the model's (path gaps)
        r0 = analyze.run(bundle, schema_override=sch0)
        before |= {(f["rule"], f["object"]) for f in r0["findings"]}
    # Measurements (workload counters, replayed plans) describe the workload before the
    # change and cannot be re-measured on a simulated schema.
    measured = ("WRK", "PLN")  # workload and replayed-plan findings

    def new_serious(r, fs):
        """New findings at medium or above, by object: the same rule on a new index counts. A
        finding that moved to a replacement (X_nn for X) is the original finding, not a new one."""
        names = _replacements(fs)
        return sorted(((f["rule"], f.get("table"), f["object"]) for f in r["findings"]
                       if f["severity"] in ("critical", "high", "medium") and
                       (f["rule"], _as_original(f["object"], names)) not in before and
                       not f["rule"].startswith(measured)), key=str)

    problems, runs = [], 0
    groups = batches(fixed)
    for fs in groups:
        r2 = analyze.run(bundle, schema_override=_apply(sch0, fs))
        runs += 1
        # By object, as new_serious compares: a finding that moved to its replacement (X_nn
        # for X) is still there; another finding of the rule on the table is not this one.
        names = _replacements(fs)
        after = {(f["rule"], _as_original(f["object"], names)) for f in r2["findings"]}
        for f in fs:
            if (f["rule"], f["object"]) in after:
                problems.append("not resolved by its own sketch: %s %s" % (f["rule"],
                                                                         f["object"]))
        batch_new = new_serious(r2, fs)
        if batch_new:
            named = set()
            for f in fs:  # every sketch of the batch, alone, to name the one responsible
                r = analyze.run(bundle, schema_override=_apply(sch0, [f])) if len(fs) > 1 \
                    else r2
                runs += len(fs) > 1
                costs = [c for o in f.get("options") or [] if o.get("sketched")
                         for c in o.get("costs") or []]
                for s in new_serious(r, [f] if len(fs) > 1 else fs):
                    named.add(s)
                    problems.append("introduced by the sketch for %s %s: %s on %s (%s)%s" % (
                        (f["rule"], f["object"]) + s + (
                            ("; a known cost of that option: %s" % "; ".join(costs))
                            if costs else "",)))
            for s in batch_new:
                if s not in named:  # only the combination brings it
                    problems.append("introduced when the sketches for %s are applied together: "
                                    "%s on %s (%s)" % ((", ".join(
                                        "%s %s" % (f["rule"], f["object"]) for f in fs),) + s))
    return {"round1": len(r1["findings"]), "fixed_with_ddl": len(fixed),
            "batches": len(groups), "runs": runs, "problems": sorted(set(problems))}
