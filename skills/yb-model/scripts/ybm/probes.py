"""Per-release verification of a rule's mechanism with a small probe.

A rule that claims how YugabyteDB executes something (not just which plan it picks) carries a
`probe` in rules/rules.json: setup SQL that builds a few thousand generated rows, named cases
(EXPLAIN (ANALYZE, DIST) of literal queries), `applies` checks (the planner took the path the
rule is about) and `holds` checks (the claimed behaviour happened). Rows are generated, not
injected, because the claims are about execution counters (rows scanned, rechecks), which
injected statistics cannot produce.

Probes run on the customer's release, in the replay container, under the customer's planner
settings, and only for rules that fired. Results go into that review only; nothing is stored.

Verdicts: holds | refuted | inconclusive (applies failed, or the probe errored).
"""

import ast
import json
import os

_RULES = None


def rules_with_probes():
    global _RULES
    if _RULES is None:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "..", "rules", "rules.json"), encoding="utf-8") as fh:
            _RULES = {k: v["probe"] for k, v in json.load(fh)["rules"].items() if v.get("probe")}
    return _RULES


# --- metrics ------------------------------------------------------------------------------

def _walk(node, out):
    out.append(node)
    for ch in node.get("Plans", []) or []:
        _walk(ch, out)
    return out


def metrics(plan_json):
    """Counters from EXPLAIN (ANALYZE, DIST, FORMAT JSON) that the checks may use."""
    top = plan_json[0]["Plan"] if isinstance(plan_json, list) else plan_json["Plan"]
    nodes = _walk(top, [])
    idx_rows = sum(n.get("Storage Index Rows Scanned", 0) or 0 for n in nodes)
    tab_rows = sum(n.get("Storage Table Rows Scanned", 0) or 0 for n in nodes)
    scans = [n for n in nodes if "Scan" in n.get("Node Type", "")]
    return {"returned": top.get("Actual Rows", 0),
            "scanned": max(idx_rows, tab_rows),
            "index_scanned": idx_rows, "table_scanned": tab_rows,
            "rechecked": sum(n.get("Rows Removed by Index Recheck", 0) or 0 for n in nodes),
            "filtered": sum(n.get("Rows Removed by Filter", 0) or 0 for n in nodes),
            "sorted": any(n.get("Node Type") in ("Sort", "Incremental Sort") for n in nodes),
            "scan": scans[0].get("Node Type") if scans else None,
            "index": scans[0].get("Index Name") if scans else None}


# --- checks: a tiny, safe expression language over case metrics -----------------------------

_OK = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.Compare,
       ast.BinOp, ast.Mult, ast.Add, ast.Sub, ast.Div, ast.Attribute, ast.Name, ast.Load,
       ast.Constant, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)


class _Case:
    def __init__(self, m):
        self.__dict__.update(m)


def parse_check(expr, cases):
    """Validate a check like 'claim.scanned >= 10 * claim.returned'."""
    tree = ast.parse(expr, mode="eval")
    for n in ast.walk(tree):
        if not isinstance(n, _OK):
            raise ValueError("not allowed in a probe check: %s (%s)" % (type(n).__name__, expr))
        if isinstance(n, ast.Name) and n.id not in cases:
            raise ValueError("unknown case %r in %s" % (n.id, expr))
        if isinstance(n, ast.Attribute) and n.attr not in METRICS:
            raise ValueError("unknown metric %r in %s" % (n.attr, expr))
    return compile(tree, "<probe>", "eval")


METRICS = ("returned", "scanned", "index_scanned", "table_scanned", "rechecked", "filtered",
           "sorted", "scan", "index")


def judge(probe, case_metrics):
    """(verdict, failed check or None)."""
    env = {k: _Case(v) for k, v in case_metrics.items()}
    names = list(probe["cases"])
    for expr in probe.get("applies", []):
        if not eval(parse_check(expr, names), {"__builtins__": {}}, env):
            return "inconclusive", "precondition failed: " + expr
    for expr in probe["holds"]:
        if not eval(parse_check(expr, names), {"__builtins__": {}}, env):
            return "refuted", expr
    return "holds", None


# --- running ------------------------------------------------------------------------------

def run(container, rule_ids, settings_sql):
    """Run the probes of `rule_ids` in a scratch database of `container` (replay.Container)."""
    probes = rules_with_probes()
    todo = [r for r in sorted(set(rule_ids)) if r in probes]
    out = {}
    if not todo:
        return out
    container.sql("CREATE DATABASE ybm_probe WITH colocation = false;", db="yugabyte")
    for rid in todo:
        p = probes[rid]
        r = container.sql("\n".join(p["setup"]) + "\n", db="ybm_probe")
        err = [l.strip() for l in (r.stdout + r.stderr).splitlines() if "ERROR:" in l]
        if err:
            out[rid] = {"verdict": "inconclusive", "detail": "setup failed: " + err[0],
                        "cases": {}}
            continue
        cases, failed = {}, None
        for name, q in p["cases"].items():
            r = container.sql(settings_sql + "EXPLAIN (ANALYZE, DIST, FORMAT JSON) %s;\n" % q,
                              db="ybm_probe", flags=("-tA",))
            txt = r.stdout.strip()
            try:
                cases[name] = metrics(json.loads(txt[txt.index("["):]) if "[" in txt else None)
            except (ValueError, TypeError, KeyError):
                failed = "case %s: %s" % (name, (r.stderr.strip() or txt)[:300])
                break
        if failed:
            out[rid] = {"verdict": "inconclusive", "detail": failed, "cases": cases}
            continue
        verdict, why = judge(p, cases)
        out[rid] = {"verdict": verdict, "detail": why, "cases": cases}
    return out


def mode_sql(mode):
    """Session settings for a stand-alone probe run: force the cost model on or off, or leave
    the image's defaults."""
    if mode == "on":
        return "SET yb_enable_cbo = on;\n"
    if mode == "off":
        return "SET yb_enable_cbo = legacy_mode;\n"
    return ""


def apply(col, plans, recon):
    """Fold probe verdicts into the findings of one review: holds -> the finding says it was
    verified on that release; refuted -> the finding is withdrawn, with the counters;
    inconclusive -> the finding says so."""
    res = (plans or {}).get("probes") or {}
    if not res:
        return
    vm = plans.get("version_match") or {}
    rel = vm.get("image_release") or plans.get("version")
    where = rel if vm.get("exact", True) else "%s (nearest image to %s)" % (
        rel, vm.get("customer_release") or vm.get("customer"))
    for key in sorted(k for k in col.items if k[0] in res):
        f, r = col.items[key], res[key[0]]
        txt = describe(key[0], r)
        if r["verdict"] == "holds":
            f.verified = "holds on %s: %s" % (where, txt)
        elif r["verdict"] == "refuted":
            if plans.get("assumed_settings"):
                # Under assumed planner settings a refutation disputes, never removes.
                f.disputed = ("the rule's probe on %s, run under assumed planner settings "
                              "(%s), did not hold: %s" % (
                                  where, ", ".join(sorted(plans["assumed_settings"])), txt))
                continue
            recon.append({"finding": f.rule, "object": f.obj, "patterns": f.patterns,
                          "outcome": "rule refuted on %s by its probe" % where,
                          "plan": "%s failed: %s" % (r["detail"], txt)})
            del col.items[key]
        else:
            f.verified = "not verified on %s: probe inconclusive (%s)%s" % (
                where, r["detail"], ("; " + txt) if txt else "")


def describe(rid, res):
    """One line for the report, from the measured counters."""
    parts = []
    for name, m in sorted(res.get("cases", {}).items()):
        parts.append("%s: %s rows scanned for %s returned%s%s" % (
            name, m["scanned"], m["returned"],
            (", %s removed by recheck" % m["rechecked"]) if m["rechecked"] else "",
            (" via %s" % m["index"]) if m["index"] else (" (%s)" % m["scan"] if m["scan"]
                                                          else "")))
    return "; ".join(parts)
