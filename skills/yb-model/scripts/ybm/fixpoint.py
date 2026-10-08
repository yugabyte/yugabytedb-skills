"""Fixpoint check: the engine must agree with itself after its own advice is applied.

1. Start from the review as reported (its final findings and DDL, after replay and probes),
   or, without one, a static review of the schema.
2. Apply every recommended DDL (the same model the safety pass uses).
3. Review the changed schema statically (replay cannot re-measure a simulated schema). Each
   schema finding that carried DDL must be gone, no new schema finding at medium or above may
   appear (workload, plan and safety findings describe the workload as measured and are not
   re-checked), and the safety pass must have nothing to warn about.
4. Apply the second round's DDL and review again: no new schema finding at medium or above.

It needs no answer key, so it runs on every fixture, including private customer bundles.
"""

import re

from . import analyze, safety


def _subject(f):
    m = re.search(r"\(([^()]*)\)\s*$", f["object"] or "")
    return (f["rule"], f.get("table"), m.group(1) if m else None)


def _changes(res):
    out = []
    for f in res["findings"]:
        out.extend(safety.changes_of(f.get("ddl")))
    return out


def _serious(res):
    return {_subject(f) for f in res["findings"]
            if f["severity"] in ("critical", "high", "medium")}


def check(bundle, final=None):
    sch0 = analyze.build_schema(bundle)
    r1 = final if final is not None else analyze.run(bundle, schema_override=sch0)
    if final is not None:  # disputed findings are not recommended, so their DDL is not applied
        r1 = dict(r1, findings=[f for f in r1["findings"] if f.get("section") != "disputed"])
    sch1 = safety.apply(sch0, _changes(r1))
    r2 = analyze.run(bundle, schema_override=sch1)
    sch2 = safety.apply(sch1, _changes(r2))
    r3 = analyze.run(bundle, schema_override=sch2)

    problems = []
    fixed = [f for f in r1["findings"] if f.get("ddl") and f.get("section") != "hygiene"]
    after = {_subject(f) for f in r2["findings"]}
    for f in fixed:
        if _subject(f) in after:
            problems.append("not resolved by its own DDL: %s %s" % (f["rule"], f["object"]))
    before = {(_s[0], _s[1]) for _s in (_subject(f) for f in r1["findings"])}
    # Measurements (workload counters, replayed plans) describe the workload before the
    # change and cannot be re-measured on a simulated schema.
    measured = ("WRK", "PLN", "SAF")
    for s in sorted(_serious(r2), key=str):
        if (s[0], s[1]) not in before and not s[0].startswith(measured):
            problems.append("introduced by the fixes: %s on %s (%s)" % s)
    for row in r2.get("safety") or []:
        if row["status"] == "warn":
            problems.append("second round safety warning: %s %s: %s" % (
                row["finding_rule"], row["object"], "; ".join(row["checks"])))
    new3 = _serious(r3) - _serious(r2)
    for s in sorted(new3, key=str):
        problems.append("not idempotent: round 3 adds %s on %s (%s)" % s)
    return {"round1": len(r1["findings"]), "round2": len(r2["findings"]),
            "round3": len(r3["findings"]), "fixed_with_ddl": len(fixed),
            "problems": problems}
