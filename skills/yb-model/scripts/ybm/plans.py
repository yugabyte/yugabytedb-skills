"""Read replayed EXPLAIN (FORMAT JSON) plans and reconcile them with static findings.

Static access-path findings (CAP*) predict a plan shape. When a replayed plan exists for the
pattern, the prediction is either confirmed (the finding is kept and marked confirmed by
replay) or refuted (the finding is removed and listed under reconciliation, with the plan
node that contradicted it). Plan-only findings (PLN*) are added for shapes the static rules
did not predict.
"""

import re

from .sqltok import in_schema_of, rel_key

SCAN_NODES = ("Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan",
              "YB Bitmap Table Scan", "Bitmap Index Scan", "YB Seq Scan")


def walk(node, parent=None, out=None):
    if out is None:
        out = []
    out.append((node, parent))
    for ch in node.get("Plans", []) or []:
        walk(ch, node, out)
    return out


def summarize(plan_json, sch):
    """Flatten a plan into comparable facts per base table."""
    root = plan_json[0]["Plan"] if isinstance(plan_json, list) else plan_json["Plan"]
    nodes = walk(root)
    facts = {"scans": [], "sort_under_limit": False, "appends": [], "nodes": []}
    for n, parent in nodes:
        nt = n.get("Node Type", "")
        facts["nodes"].append(nt)
        rel = n.get("Relation Name")
        if rel:  # the plan names relations bare; key them as the schema model does
            rel = rel_key(n["Schema"], rel) if n.get("Schema") else (
                sch.rel(None, rel) if hasattr(sch, "rel") else rel)
        if nt in SCAN_NODES and rel:
            base = rel
            t = sch.tables.get(rel)
            if t is not None and t.partition_of:
                base = t.partition_of
            facts["scans"].append({"node": nt, "relation": rel, "table": base,
                                   "index": in_schema_of(rel, n.get("Index Name")),
                                   "index_cond": n.get("Index Cond"),
                                   "rows": n.get("Plan Rows"),
                                   "storage_filter": n.get("Storage Filter") or
                                   n.get("Storage Index Filter") or n.get("Remote Filter")})
        if nt == "Sort" and parent is not None and parent.get("Node Type") == "Limit":
            facts["sort_under_limit"] = True
        if nt in ("Append", "Merge Append"):
            kids = [c.get("Relation Name") for c in n.get("Plans", []) or []]
            facts["appends"].append(len(kids))
    facts["nodes"] = sorted(set(facts["nodes"]))
    return facts


def partial_hash_scan(scan, sch):
    """An Index Scan whose Index Cond does not bind every hash column of its index. The cost
    model can choose it, but DocDB cannot locate the rows by hash and reads the whole index
    (the CAP001 probe checks this on the customer's release)."""
    idx = sch.indexes.get(scan.get("index")) or next(
        (t.pk for t in sch.tables.values() if t.pk and t.pk.name == scan.get("index")), None)
    if idx is None or not idx.hash_cols or not scan.get("index_cond"):
        return False
    cond = scan["index_cond"].lower()
    return not all(re.search(r"\b%s\b" % re.escape(k.col), cond) for k in idx.hash_cols
                   if k.col)


def _scans_for(facts, table):
    return [s for s in facts["scans"] if s["table"] == table]


def reconcile(plans, patterns, col, sch, rules, th, recon, wshift, shift, Finding):
    by_pat = {}
    for p in plans.get("patterns", []):
        if p.get("plan"):
            by_pat[p["id"]] = summarize(p["plan"], sch)
    weights = {p["id"]: p["weight"] for p in patterns}
    rows_inj = plans.get("reltuples", {})
    pats_by_id = {p["id"]: p for p in patterns}

    for p in patterns:
        f = by_pat.get(p["id"])
        p["plan_facts"] = f
        if f is None:
            continue
        p["plan_summary"] = "; ".join(
            "%s %s%s" % (s["node"], s["relation"], (" using " + s["index"]) if s["index"] else "")
            for s in f["scans"])

    vm = plans.get("version_match") or {}
    drifted = set((vm.get("drift") or {}).get("rules", []))
    # On an image that is not the customer's release, confirming or refuting needs the
    # differences between the two releases. Without release facts they are unknown.
    drift_unknown = not vm.get("exact", True) and not vm.get("drift_known", True)
    # Planner settings the bundle did not supply were assumed. A plan under assumed settings
    # may confirm a finding, but never removes one: the finding moves to "disputed".
    assumed = sorted(plans.get("assumed_settings") or {})
    image = vm.get("image_release") or plans.get("version")
    label = "confirmed (replay)" if vm.get("exact", True) else \
        "confirmed (replay on %s, nearest earlier release to %s)" % (image, vm.get("customer"))
    if assumed:
        label += "; planner settings assumed"
    # What a replay on a different release shows without known differences is that release's
    # behaviour, not necessarily the customer's.
    caveat = "replayed on %s; differences to %s unknown" % (image, vm.get("customer")) \
        if drift_unknown else None
    expect_seq = ("CAP001", "CAP002", "CAP003", "CAP040", "CAP031")
    for key in list(col.items.keys()):
        fnd = col.items[key]
        if not fnd.rule.startswith("CAP") or not fnd.table:
            continue
        verdicts = []
        for pid in fnd.patterns:
            facts = by_pat.get(pid)
            if facts is None:
                continue
            scans = _scans_for(facts, fnd.table)
            if fnd.rule in expect_seq:
                ok = any(s["node"] in ("Seq Scan", "YB Seq Scan") or
                         (fnd.rule == "CAP001" and partial_hash_scan(s, sch))
                         for s in scans) if scans else None
                if fnd.rule == "CAP040" and any("Bitmap" in s["node"] for s in scans):
                    ok = False
            elif fnd.rule in ("CAP010", "CAP011"):
                ok = facts["sort_under_limit"] or "Sort" in facts["nodes"] or \
                    "Incremental Sort" in facts["nodes"]
            elif fnd.rule == "CAP020":
                ok = any(s["node"] == "Index Scan" and s["index"] == fnd.index for s in scans)
                if not ok and not any(s["index"] == fnd.index for s in scans):
                    ok = None  # planner chose a different path; neither confirmed nor refuted
            elif fnd.rule == "CAP021":
                # The partial index the query cannot prove: confirmed while no scan uses it
                # (whatever path the planner took instead), refuted when the planner uses it.
                ok = not any(s["index"] == fnd.index for s in scans) if scans else None
            elif fnd.rule == "CAP030":
                ok = bool(facts["appends"]) and max(facts["appends"]) > 1
            else:
                ok = None
            verdicts.append((pid, ok, scans))
        if not verdicts:
            continue
        if drift_unknown:
            fnd.replay = ("not used: replay ran on %s, not the customer's %s, and the "
                          "differences between the two releases are unknown (no release facts "
                          "cached)" % (plans.get("version"), vm.get("customer")))
            continue
        if fnd.rule in drifted:
            fnd.replay = ("not used: this rule's tested behaviour differs between %s and the "
                          "replay image %s" % (vm.get("customer_release"),
                                               vm.get("image_release")))
            continue
        if any(v is True for _, v, _ in verdicts):
            fnd.confidence = label
            fnd.replay = "; ".join("%s: %s" % (pid, _scan_txt(sc)) for pid, v, sc in verdicts
                                   if v is True)
        elif all(v is False for _, v, _ in verdicts):
            plan_txt = "; ".join("%s: %s" % (pid, _scan_txt(sc)) for pid, _, sc in verdicts)
            if assumed:
                fnd.disputed = ("replay under assumed planner settings (%s) chose a plan that "
                                "contradicts this finding: %s" % (", ".join(assumed), plan_txt))
                fnd.replay = "disputed: " + plan_txt
                continue
            recon.append({"finding": fnd.rule, "object": fnd.obj, "patterns": fnd.patterns,
                          "outcome": "refuted by replay", "plan": plan_txt})
            del col.items[key]
        else:
            fnd.replay = "planner chose another path: " + "; ".join(
                "%s: %s" % (pid, _scan_txt(sc)) for pid, _, sc in verdicts)

    # Plan-only findings. Each fires because no static finding predicted the plan, so its fix
    # says what the planner chose rather than pointing at a finding that does not exist.
    for pid, facts in sorted(by_pat.items(), key=lambda kv: int(kv[0][1:])):
        w = weights.get(pid, "UNRANKED")
        for s in facts["scans"]:
            rows = rows_inj.get(s["relation"]) or rows_inj.get(s["table"])
            if s["node"] in ("Seq Scan", "YB Seq Scan") and rows and rows >= th["large_rows"]:
                if not any(k[0] in ("CAP001", "CAP002", "CAP003", "CAP040", "CAP021", "CAP031")
                           and pid in v.patterns and v.table == s["table"]
                           for k, v in col.items.items()):
                    col.add(Finding("PLN001", shift("high", wshift[w]), label,
                                    "%s scan of %s" % (pid, s["relation"]),
                                    "%s: replayed plan scans all of %s (~%d rows injected)%s." % (
                                        pid, s["relation"], rows,
                                        (", storage filter %s" % s["storage_filter"])
                                        if s["storage_filter"] else ""),
                                    [pid], table=s["table"],
                                    fix=_plan_fix("PLN001", pid, _scans_for(facts, s["table"]),
                                                  col)))
        if facts["sort_under_limit"] and not any(
                k[0] in ("CAP010", "CAP011") and pid in v.patterns for k, v in col.items.items()):
            col.add(Finding("PLN002", shift("medium", wshift[w]), label,
                            "%s sort" % pid, "%s: replayed plan sorts before LIMIT." % pid, [pid],
                            fix=_plan_fix("PLN002", pid, facts["scans"], col)))
        if facts["appends"] and max(facts["appends"]) > 1 and not any(
                k[0] == "CAP030" and pid in v.patterns for k, v in col.items.items()):
            col.add(Finding("PLN003", shift("high", wshift[w]), label,
                            "%s append" % pid, "%s: replayed plan appends %d partitions." % (
                                pid, max(facts["appends"])), [pid],
                            fix=_plan_fix("PLN003", pid, facts["scans"], col)))
    # Patterns the release rejected. Untyped parameters are a replay artefact (the application's
    # driver sends typed parameters), not a defect.
    artefacts = []
    for p in plans.get("patterns", []):
        err = (p.get("error") or "").split("ERROR:", 1)[-1].strip()
        if p.get("plan") or not err:
            continue
        if re.search(r"could not determine data type of parameter|"
                     r"operator is not unique|inconsistent types deduced", err):
            artefacts.append("%s: %s" % (p["id"], err))
            continue
        hit = [v for k, v in col.items.items() if k[0] == "CAP060" and p["id"] in v.patterns]
        if hit and "ON CONFLICT" in err:
            for v in hit:
                v.confidence = label
                v.replay = "%s fails on replay: %s" % (p["id"], err)
            continue
        col.add(Finding("PLN005", shift("high", wshift[weights.get(p["id"], "UNRANKED")]),
                        label, "%s error" % p["id"], "%s fails on replay: %s" % (p["id"], err),
                        [p["id"]]))
    for f in col.items.values():
        replayed = f.rule.startswith("PLN") or (f.rule == "CAP060" and f.replay)
        if replayed and caveat and caveat not in f.caveats:
            f.caveats.append(caveat)
        if f.rule.startswith("PLN") and assumed and "planner settings assumed" not in f.caveats:
            f.caveats.append("planner settings assumed")
    return {"version": plans.get("version"), "mode": plans.get("mode"),
            "unplannable": artefacts, "drift_unknown": drift_unknown,
            "disputed": sorted(f.rule + " " + f.obj for f in col.items.values()
                               if getattr(f, "disputed", None)),
            "probes": {k: {"verdict": v["verdict"], "detail": v["detail"]}
                       for k, v in (plans.get("probes") or {}).items()},
            "version_match": vm, "assumed_settings": plans.get("assumed_settings"),
            "ddl_errors": (plans.get("ddl_errors") or {}).get("count"),
            "inject_errors": (plans.get("inject_errors") or {}).get("count"),
            "settings": plans.get("settings"), "errors": plans.get("errors", []),
            "planned": sorted(by_pat, key=lambda x: int(x[1:])),
            "failed": [p["id"] for p in plans.get("patterns", []) if not p.get("plan")]}


def _scan_txt(scans):
    if not scans:
        return "no scan of the table"
    return ", ".join("%s%s" % (s["node"], (" using " + s["index"]) if s["index"] else "")
                     for s in scans)


# The action of each plan-only finding: what to do about a plan nothing predicted.
_PLAN_ACTION = {
    "PLN001": "Check whether an index can serve this pattern's predicates.",
    "PLN002": "Lead an index with the equality columns and follow with the ORDER BY columns in "
              "the requested direction.",
    "PLN003": "Add a predicate on the partition key so the planner can prune.",
}
# analyze.run folds these into the Measured line of the finding that shares their pattern and
# drops them, so a fix that named one would point at a finding the report does not have.
_FOLDED = ("WRK001", "WRK002")
_MAX_LISTED = 4   # scans, and other findings, that a fix spells out


def _scan_phrase(scans):
    """_scan_txt for a sentence. A scan the plan repeats (one per partition under an unpruned
    Append) is said once with a count, and only the first few kinds are spelled out."""
    kinds = {}
    for s in scans:
        k = _scan_txt([s])
        kinds[k] = kinds.get(k, 0) + 1
    parts = [k if n == 1 else "%s x%d" % (k, n) for k, n in kinds.items()]
    more = len(parts) - _MAX_LISTED
    return ", ".join(parts[:_MAX_LISTED]) + ((" and %d more" % more) if more > 0 else "")


def _plan_fix(rule, pid, scans, col):
    """The fix of a plan-only finding: the action, what the planner did, then which other
    findings the pattern has. The rules.json text can only say
    "see X", and X is by construction the finding that does not exist."""
    used = ("uses " + _scan_phrase(scans)) if scans else "has no scan of a named table"
    # The action leads: the report's headline quotes a fix's first sentence as "First action".
    parts = [_PLAN_ACTION[rule], "No static finding predicted this: the replayed plan for %s "
                                 "%s." % (pid, used)]
    # Plan-only findings are left out: they are what is being explained, and the PLN001s of
    # an unpruned partitioned table would each list the others.
    others = sorted((v.rule, v.obj) for v in col.items.values()
                    if pid in v.patterns and not v.rule.startswith("PLN")
                    and v.rule not in _FOLDED)
    if others:
        more = len(others) - _MAX_LISTED
        parts.append("Other findings on %s: %s%s." % (
            pid, ", ".join("%s (%s)" % o for o in others[:_MAX_LISTED]),
            (", and %d more" % more) if more > 0 else ""))
    return " ".join(parts)
