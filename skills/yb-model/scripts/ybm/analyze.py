"""Deterministic review engine. Same inputs, same findings, in the same order.

Every threshold lives in THRESHOLDS so a reviewer can see and argue with it. Nothing here calls
a model; the skill's job is to run this, then explain the output.
"""

import importlib.util
import copy
import json
import math
import os
import re

from . import schema as schema_mod
from . import sqlshape

SEV = ["critical", "high", "medium", "low", "info"]

THRESHOLDS = {
    "hot_cum_share": 0.80,      # patterns covering the first 80% of analysable time are HOT ...
    "hot_max": 25,              # ... capped at 25 patterns
    "hot_calls_rank": 10,       # the 10 most-called patterns are HOT regardless of time
    "warm_share": 0.001,        # >= 0.1% of analysable time or calls is WARM, otherwise COLD
    "null_frac_flag": 0.01,     # STA001 fires at >= 1% NULLs on a hash lead ...
    "null_frac_high": 0.10,     # ... high at >= 10% or >= null_rows_high rows
    "null_rows_high": 1e6,
    "null_rows_min": 100,       # ... and ignored below 100 NULL rows
    "ndistinct_high": 100,      # STA002 high below 100 distinct hash values ...
    "ndistinct_medium": 1000,   # ... medium below 1000
    "mcv_medium": 0.10,         # STA003 medium when one value holds >= 10% of rows ...
    "mcv_high": 0.30,           # ... high at >= 30%
    "corr_monotonic": 0.90,     # STA004 at |correlation| >= 0.9 on a range lead
    "readamp_ratio": 10.0,      # WRK001 at rows_scanned / rows_returned >= 10 ...
    "readamp_min_scanned": 1000.0,  # ... and >= 1000 rows scanned per call
    "rpcs_per_call": 10.0,      # WRK002 at >= 10 read RPCs per call
    "write_hot_indexes": 4,     # WRK004 at >= 4 secondary indexes on a write-HOT table
    "large_rows": 1e5,          # PLN001 Seq Scan matters from 100k rows
    "tiny_rows": 1e4,           # < 10k rows: access-path findings drop two levels
    "small_rows": 1e6,          # < 1M rows: access-path findings drop one level
    "split_rows": 1e7,          # SPL001: >= 10M rows still on one tablet
    "runway_days": 120,         # PRT001: last RANGE partition ends within 120 days
    "mcv_rows_low": 1e5,        # STA003 low when one value holds >= 100k rows on a hash code,
    "mcv_rows_medium": 1e7,     # ... medium at >= 10M (one hash code is never split across
    "mcv_rows_high": 1e8,       # ... tablets), high at >= 100M
    "all_null": 0.999,          # STA006: column is (almost) entirely NULL
    "zero_rows_calls": 10000,   # WRK005: a SELECT with >= 10k calls ...
    "miss_rate_rows": 0.05,     # ... returning <= 0.05 rows per call (>= 95% of calls miss)
    "plan_share": 0.30,         # WRK006: planning >= 30% of plan + execution time
    "dml_match_rate": 0.5,      # WRK009: UPDATE/DELETE changing <= 0.5 rows per call
    "fanout_rows": 1000,        # WRK007: SELECT returning >= 1000 rows per call
    "include_all_cols": 8,      # CAP020: SELECT * is coverable by INCLUDE on tables this narrow
    "sentinel_share": 0.05,     # STA007: one value >= 5% of an otherwise near-unique column,
    "near_unique_nd": -0.3,     # ... where "near-unique" is n_distinct below -0.3
    "date_sentinel_share": 0.01,  # STA007: a placeholder date holding >= 1% of rows
    "well_distributed_nd": 100000,  # "sound": a hash lead with >= 100k distinct values
    "corr_unordered": 0.3,      # "sound": |correlation| < 0.3 on a range lead
    "min_ordered_nd": 100,      # STA004 ignores columns with < 100 distinct values
    "fanout_factor": 3,         # WRK007 names skew when rows per call >= 3x the expected
}

MONOTONIC_NAME = re.compile(r"^(.*_)?(created|updated|inserted|modified|materialized|"
                            r"processed|logged|occurred|event)_(at|on|time|ts)$")
CREATION_COL = re.compile(r"^(id|created_at|created_on|inserted_at|creation_time)$")
SOFT_DELETE = re.compile(r"^(.*_)?(deleted|archived|expired|cancell?ed|discarded)_at$")

WEIGHT_SHIFT = {"HOT": 0, "WARM": 1, "COLD": 2, "UNRANKED": 0}
# Caveat on a finding whose only patterns are listed statements pg_stat_statements lacks.
LISTED_UNRANKED = "pattern unranked (listed, not in pg_stat_statements)"


def version_tuple(v):
    """2.20.1.0 -> (2, 20, 1, 0); 2025.2.4.0 -> (2025, 2, 4, 0). Calendar releases sort after 2.x."""
    import re as _re
    return tuple(int(x) for x in _re.findall(r"\d+", str(v))[:4])


def shift(sev, n):
    return SEV[min(len(SEV) - 1, max(0, SEV.index(sev) + n))]


def load_rules():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "..", "rules", "rules.json"), encoding="utf-8") as fh:
        return json.load(fh)["rules"]


def _load_linter():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location("yb_lint", os.path.join(here, "yb-lint.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _truthy(v):
    return str(v).strip().lower() in ("on", "true", "t", "1", "yes")


# --- settings ---------------------------------------------------------------------------

def planner_settings(bundle):
    """Planner settings that change what the rules conclude.

    Order of trust: the bundle's pg_settings (measured), then the customer's release defaults
    from rules/versions.json when every deployment tool agrees, otherwise unknown with the
    per-tool values recorded so findings can be stated conditionally."""
    from . import versions
    s = bundle.settings
    tag, tag_note = versions.resolve(bundle.version)
    out = {"cbo": None, "cbo_setting": None, "bitmap": None, "merge_streams": None,
           "hash_default": True, "colocated": None, "rpc_stats": None,
           "release": tag, "release_note": tag_note, "sources": {}, "conditional": {},
           "unavailable": []}

    def from_release(name):
        v, src, per = versions.setting(tag, name)
        if v is None and per and set(per.values()) != {None}:
            out["conditional"][name] = per
        if tag and not versions.available(tag, name):
            out["unavailable"].append(name)
        out["sources"][name] = src if v is not None else (
            "unknown: " + src if name in out["conditional"] else src)
        return v

    def get(name):
        if name in s:
            out["sources"][name] = "pg_settings"
            return s[name]
        return from_release(name)

    cbo = get("yb_enable_cbo")
    if cbo is not None:
        out["cbo_setting"] = "yb_enable_cbo=%s" % cbo
        out["cbo"] = str(cbo).lower() == "on"
    elif "yb_enable_cbo" not in out["conditional"]:
        a = get("yb_enable_base_scans_cost_model")
        b = get("yb_enable_optimizer_statistics")
        if a is not None and b is not None:
            out["cbo_setting"] = "yb_enable_base_scans_cost_model=%s, " \
                                 "yb_enable_optimizer_statistics=%s" % (a, b)
            out["cbo"] = _truthy(a) and _truthy(b)
    bm = get("yb_enable_bitmapscan")
    if bm is not None:
        out["bitmap"] = _truthy(bm) and _truthy(s.get("enable_bitmapscan", "on"))
    ms = get("yb_max_merge_scan_streams")
    if ms is not None:
        try:
            out["merge_streams"] = int(float(ms))
        except ValueError:
            pass
    hd = get("yb_use_hash_splitting_by_default")
    if hd is not None:
        out["hash_default"] = _truthy(hd)
    if "colocated" in bundle.meta:
        out["colocated"] = _truthy(bundle.meta["colocated"])
    rs = get("yb_enable_pg_stat_statements_rpc_stats")
    if rs is not None:
        out["rpc_stats"] = _truthy(rs)
    out["unavailable"] = sorted(set(out["unavailable"]))
    return out


# --- findings ---------------------------------------------------------------------------

class Finding:
    def __init__(self, rule, severity, confidence, obj, fact, patterns=None, fix=None,
                 evidence=None, table=None, index=None, ddl=None):
        self.rule = rule
        self.severity = severity
        self.confidence = confidence
        self.obj = obj
        self.fact = fact
        self.patterns = list(patterns or [])
        self.fix = fix
        self.evidence = evidence
        self.table = table
        self.index = index
        self.replay = None
        self.ddl = ddl
        self.measured = None
        self.caveats = []
        self.verified = None
        self.disputed = None  # why replay or a probe under assumed settings disagreed

    def key(self):
        return (self.rule, self.obj)

    def to_dict(self, rules, version=None, release=None, unavailable=()):
        from . import versions
        r = rules.get(self.rule, {})
        fix = self.fix or r.get("fix")
        if not self.fix and set(r.get("requires", [])) & set(unavailable):
            fix = r.get("fix_if_unavailable", fix)
        conf = self.confidence
        pin = None
        if r.get("test") and release:
            pin = versions.pinned(release, self.rule)
            if pin is False:
                conf += "; not pinned by tests on %s" % release
        # Planner-behaviour rules cite the regress test that pins them. The others are
        # arithmetic on the bundle's own statistics and workload, so no test can pin them.
        basis = "regress test" if r.get("test") else "computed from the bundle"
        return {"rule": self.rule, "severity": self.severity, "confidence": conf,
                "caveats": list(self.caveats), "pinned_on_release": pin, "basis": basis,
                "section": "disputed" if self.disputed else r.get("section", "schema"),
                "disputed": self.disputed,
                "object": self.obj, "table": self.table, "index": self.index,
                "patterns": self.patterns, "title": r.get("title", self.rule),
                "fact": self.fact, "mechanism": r.get("mechanism"),
                "fix": fix, "evidence_needed": self.evidence,
                "test_refs": r.get("test", []), "replay": self.replay, "ddl": self.ddl,
                "measured": self.measured, "verified": self.verified,
                "observed": versions.observations(self.rule)}


class Collector:
    def __init__(self, muted=()):
        self.items = {}
        self.muted = set(muted)
        self.withheld = {}   # muted rule -> candidate objects it would have reported

    def add(self, f):
        k = f.key()
        if f.rule in self.muted:
            self.withheld.setdefault(f.rule, set()).add(f.obj)
            return
        if k in self.items:
            old = self.items[k]
            if SEV.index(f.severity) < SEV.index(old.severity):
                old.severity = f.severity
                old.fact = f.fact
            for p in f.patterns:
                if p not in old.patterns:
                    old.patterns.append(p)
        else:
            self.items[k] = f

    def sorted(self):
        def pkey(f):
            nums = [int(p[1:]) for p in f.patterns if p[1:].isdigit()]
            return min(nums) if nums else 10 ** 6
        out = sorted(self.items.values(),
                     key=lambda f: (SEV.index(f.severity), pkey(f), f.rule, f.obj))
        for f in out:
            f.patterns.sort(key=lambda p: int(p[1:]) if p[1:].isdigit() else 10 ** 6)
        return out


# --- workload ---------------------------------------------------------------------------

def _is_catalog(q):
    ql = q.lower()
    return bool(re.search(r"\b(pg_catalog|information_schema|pg_stat|pg_class|pg_namespace|"
                          r"pg_attribute|pg_settings|yb_local_tablets|pg_database|pg_type)\b", ql))


def _listed(bundle):
    """The statements of queries.sql, in file order."""
    from .sqltok import tokenize, split_statements
    out = []
    for st in split_statements(tokenize(bundle.queries_sql or "")):
        out.append(bundle.queries_sql[st[0].pos:st[-1].pos + len(st[-1].text)])
    return out


def build_patterns(bundle, schema, dropped=None, listed=None):
    """Access patterns. With pg_stat_statements, `time_share` / `call_share` are shares of all
    captured statements, as the report quotes them. HOT / WARM / COLD rank the analysable
    statements among themselves, so session, catalog and other-schema statements do not move
    the weights. Statements that are not analysed are appended to `dropped` with the reason.

    A query list (queries.sql) is merged with pg_stat_statements: a listed statement that
    pg_stat_statements also has (same fingerprint) is ranked from it; one it lacks is added
    after the ranked patterns as UNRANKED, since its traffic is unknown. `listed` receives
    {"matched": n, "added": [pattern ids]}."""
    pats = []
    if bundle.pss:
        rows = []
        for r in bundle.pss:
            if _is_catalog(r["query"]):
                reason = "catalog or monitoring query"
            else:
                shape = sqlshape.analyze(r["query"], schema)
                tabs = sorted({t for s in sqlshape.flatten(shape) for t in s.tables
                               if t in schema.tables})
                if shape.kind == "utility":
                    reason = "utility or session statement"
                elif not tabs:
                    reason = "no table of the schema"
                else:
                    rows.append((r, shape, tabs))
                    continue
            if dropped is not None:
                dropped.append((reason, r))
        # fsum: exact, so totals do not drift between Python versions.
        all_ms = math.fsum(r["total_ms"] for r in bundle.pss) or 1.0
        all_calls = math.fsum(r["calls"] for r in bundle.pss) or 1.0
        tot_ms = math.fsum(r["total_ms"] for r, _, _ in rows) or 1.0
        tot_calls = math.fsum(r["calls"] for r, _, _ in rows) or 1.0
        rows.sort(key=lambda x: (-x[0]["total_ms"], -x[0]["calls"], x[0]["queryid"]))
        by_calls = sorted(rows, key=lambda x: (-x[0]["calls"], x[0]["queryid"]))
        call_rank = {id(x[0]): i for i, x in enumerate(by_calls)}
        cum = 0.0
        for i, (r, shape, tabs) in enumerate(rows):
            share = round(r["total_ms"] / tot_ms, 6)    # among analysable statements
            cshare = round(r["calls"] / tot_calls, 6)
            if (cum < THRESHOLDS["hot_cum_share"] and i < THRESHOLDS["hot_max"]) or \
                    call_rank[id(r)] < THRESHOLDS["hot_calls_rank"]:
                w = "HOT"
            elif share >= THRESHOLDS["warm_share"] or cshare >= THRESHOLDS["warm_share"]:
                w = "WARM"
            else:
                w = "COLD"
            cum += share
            pats.append({"id": "P%d" % (i + 1), "source": "pg_stat_statements",
                         "queryid": r["queryid"], "query": r["query"], "calls": r["calls"],
                         "total_ms": r["total_ms"], "mean_ms": r["mean_ms"], "rows": r["rows"],
                         "time_share": round(r["total_ms"] / all_ms, 6),
                         "call_share": round(r["calls"] / all_calls, 6), "weight": w,
                         "kind": shape.kind, "tables": tabs, "shape": shape, "pss": r})
    if bundle.queries_sql:
        from .sqltok import fingerprint
        known = {fingerprint(r["query"]) for r in bundle.pss}
        matched, added = 0, []
        for q in _listed(bundle):
            if known and fingerprint(q) in known:
                matched += 1
                continue
            shape = sqlshape.analyze(q, schema)
            tabs = sorted({t for s in sqlshape.flatten(shape) for t in s.tables
                           if t in schema.tables})
            pid = "P%d" % (len(pats) + 1)
            added.append(pid)
            pats.append({"id": pid, "source": "queries.sql", "queryid": None,
                         "query": q, "calls": None, "total_ms": None, "mean_ms": None,
                         "rows": None, "time_share": None, "call_share": None,
                         "weight": "UNRANKED", "kind": shape.kind, "tables": tabs,
                         "shape": shape, "pss": None})
        if listed is not None:
            listed.update(matched=matched, added=added)
    return pats


# --- access paths -----------------------------------------------------------------------

BIND_EQ = ("eq", "join")
BIND_ANY_EQ = ("eq", "in", "join")
BIND_RANGE = ("range", "prefix")


def _keyid(k):
    return k.col if k.col else "expr:" + k.expr


def _pred_index(preds):
    ops = {}
    for p in preds:
        kid = p.col if p.col else ("expr:" + p.expr if p.expr else None)
        if kid is None:
            continue
        ops.setdefault(kid, set()).add(p.op)
    return ops


def _implied(where, ops):
    """Can the planner prove the query implies this partial-index predicate?"""
    if not where:
        return True
    # The predicate as text_of normalises it: key words lower case, a quoted name as written.
    parts = [p.strip().strip("()").strip() for p in re.split(r"\band\b", where)]
    for part in parts:
        m = re.match(r"^([A-Za-z_][\w$]*)\s+is\s+not\s+null$", part)
        if m:
            col_ops = ops.get(m.group(1), set())
            if col_ops & {"eq", "in", "join", "range", "prefix", "notnull"}:
                continue
            return False
        # A soft-delete style predicate (deleted_at IS NULL) is implied when the query repeats it.
        m = re.match(r"^([A-Za-z_][\w$]*)\s+is\s+null$", part)
        if m and "isnull" in ops.get(m.group(1), set()):
            continue
        return False
    return True


def eval_path(idx, ops, shape, table, cbo):
    res = {"index": idx.name, "is_pk": idx.is_pk, "unique": idx.unique, "usable": False,
           "bound": 0, "status": None, "full_unique": False, "skip_scan": False,
           "hash_bound": None, "hash_missing": [], "in_on_hash": False}
    if idx.method not in ("lsm", "hash"):
        res["status"] = "not_modelled"
        return res
    if idx.where and not _implied(idx.where, ops):
        res["status"] = "partial_not_implied"
        res["would_bind"] = _bind_count(idx, ops)
        return res
    hk = idx.hash_cols
    pos = 0
    yhc = ops.get("expr:yb_hash_code(%s)" % ",".join(k.sql for k in hk), set()) if hk else set()
    if hk and yhc & set(BIND_ANY_EQ + BIND_RANGE):
        res.update(usable=True, status="ok", bound=len(hk), hash_bound=True, yb_hash_code=True)
        return res
    if hk:
        bound = [k for k in hk if ops.get(_keyid(k), set()) & set(BIND_ANY_EQ)]
        ranged = [k for k in hk if ops.get(_keyid(k), set()) & set(BIND_RANGE)]
        res["in_on_hash"] = any("in" in ops.get(_keyid(k), set()) and
                                not ops.get(_keyid(k), set()) & set(BIND_EQ) for k in hk)
        if len(bound) < len(hk):
            if bound or ranged:
                res["status"] = "hash_unbound"
                res["hash_missing"] = [k.label for k in hk if k not in bound]
            else:
                res["status"] = "unrelated"
            return res
        res["hash_bound"] = True
        pos = len(hk)
        res["bound"] = pos
    for k in idx.keys[pos:]:
        o = ops.get(_keyid(k), set())
        if o & set(BIND_ANY_EQ):
            res["bound"] += 1
            continue
        if o & set(BIND_RANGE):
            res["bound"] += 1
        break
    if res["bound"] == 0:
        later = any(ops.get(_keyid(k), set()) & set(BIND_ANY_EQ + BIND_RANGE)
                    for k in idx.keys[1:])
        # DocDB can skip-scan a range-led key on a later bound column in both planner modes
        # (checked by evals/yb-model/oracle.py; results stay in a local cache).
        if later and not hk:
            res["usable"] = True
            res["skip_scan"] = True
            res["status"] = "skip_scan"
            return res
        res["status"] = "unrelated"
        return res
    res["usable"] = True
    res["status"] = "ok"
    res["full_unique"] = idx.unique and res["bound"] == len(idx.keys) and all(
        ops.get(_keyid(k), set()) & set(BIND_EQ) for k in idx.keys)
    return res


def _bind_count(idx, ops):
    n = 0
    for k in idx.keys:
        if ops.get(_keyid(k), set()) & set(BIND_ANY_EQ + BIND_RANGE):
            n += 1
        else:
            break
    return n


def order_ok(idx, ops, shape, table):
    """True if the index returns rows in the pattern's ORDER BY order for this table."""
    if not shape.order or any(t != table for t, _, _ in shape.order):
        return None
    want = [(c, d) for t, c, d in shape.order if not ("eq" in ops.get(c, set()))]
    if not want:
        return True
    if idx.unique and idx.keys and all("eq" in ops.get(_keyid(k), set()) for k in idx.keys):
        return True  # at most one row
    hk = idx.hash_cols
    pos = 0
    if hk:
        if not all(ops.get(_keyid(k), set()) & set(BIND_EQ) for k in hk):
            return False
        pos = len(hk)
    keys = idx.keys[pos:]
    directions = set()
    wi = 0
    for k in keys:
        if wi >= len(want):
            break
        col, d = want[wi]
        if k.col == col:
            directions.add(d == k.mode)
            wi += 1
            continue
        if ops.get(_keyid(k), set()) & set(BIND_EQ):
            continue  # bound to one value: does not disturb order
        return False
    if wi < len(want):
        return False
    return len(directions) <= 1


def covering(idx, shape, table, tcols):
    if idx.is_pk:
        return True, []
    have = set(idx.key_names) | set(idx.include)
    if table in shape.select_all:
        # SELECT * is covered only when the index carries every column of the table.
        if tcols and set(tcols) <= have:
            return True, []
        return False, ["*"]
    need = {c for t, c in shape.select_cols if t == table}
    need |= {p.col for p in shape.preds if p.table == table and p.col}
    need |= {c for t, c, _ in shape.order if t == table}
    have = set(idx.key_names) | set(idx.include)
    missing = sorted(c for c in need if c not in have and (not tcols or c in tcols))
    return not missing, missing


def ordered_walk(sch, table, ops, shape):
    """With no index to seek on, a planner can still walk a range-led index in ORDER BY order
    and stop at LIMIT, filtering rows as it goes. Returns that index or None."""
    if not shape.order or not shape.limit:
        return None
    for idx in sch.indexes_on(table):
        if idx.method not in ("lsm", "hash") or idx.hash_cols or idx.where:
            continue
        if order_ok(idx, ops, shape, table) is True:
            return idx
    return None


def choose(paths, limit=False):
    """The path the planner is expected to take. With ORDER BY ... LIMIT, a path that returns
    rows in order stops after LIMIT rows (at most LIMIT base-table fetches), so it beats a
    covering path that must read and sort every match."""
    usable = [p for p in paths if p["usable"]]
    if not usable:
        return None
    cov = lambda p: -int(p.get("covering", False))  # noqa: E731
    ordr = lambda p: -int(bool(p.get("order_ok")))  # noqa: E731
    usable.sort(key=lambda p: (-int(p["full_unique"]), -p["bound"]) +
                ((ordr(p), cov(p)) if limit else (cov(p), ordr(p))) +
                (int(p["skip_scan"]), -int(p["is_pk"]), p["index"]))
    return usable[0]


# --- main -------------------------------------------------------------------------------

def table_rows(bundle, schema, table):
    t = schema.tables.get(table)
    if table in bundle.reltuples and bundle.reltuples[table] >= 0:
        v = bundle.reltuples[table]
        if v > 0 or not (t and t.partitions):
            return v
    if t and t.partitions:
        vals = [bundle.reltuples.get(p) for p in t.partitions]
        vals = [v for v in vals if v is not None and v >= 0]
        if vals:
            return sum(vals)
    return None


def size_shift(rows):
    if rows is None:
        return 0
    if rows < THRESHOLDS["tiny_rows"]:
        return 2
    if rows < THRESHOLDS["small_rows"]:
        return 1
    return 0


def col_stats(bundle, schema, table, col):
    st = bundle.stats.get((table, col))
    if st:
        return st
    t = schema.tables.get(table)
    if t and t.partition_of:
        return bundle.stats.get((t.partition_of, col))
    if t and t.partitions:
        for p in sorted(t.partitions):
            if (p, col) in bundle.stats:
                return bundle.stats[(p, col)]
    return None


def n_distinct_abs(st, rows):
    nd = st.get("n_distinct")
    if nd is None:
        return None
    if nd >= 0:
        return nd
    if rows:
        return -nd * rows
    return None


def _rekey_comment(tname, key, colocated, steps="Create the replacement, copy, swap names",
                   fks=()):
    """A primary-key change, which YSQL cannot make in place, as comment lines. The safety pass
    reads the CREATE TABLE line, so names are written as in DDL; a colocated table takes no
    SPLIT clause. Foreign keys that reference the table follow the old one through a swap, so
    they are named for moving."""
    return ("-- %s: a primary key cannot be changed in place. %s:\n-- CREATE TABLE %s (... "
            "PRIMARY KEY (%s))%s;%s" % (
                tname, steps, schema_mod.qn(tname + "_new"), key,
                "" if colocated else " SPLIT INTO <n> TABLETS",
                ("\n-- foreign keys that reference %s (%s) must be dropped before the swap and "
                 "added back after it" % (tname, ", ".join("%s on %s" % (fk.name, fk.table)
                                                          for fk in fks))) if fks else ""))


def _usage_complete(bundle):
    return getattr(bundle, "usage_complete", False)


def _usage_scope(bundle):
    """Which nodes the usage counters cover, in words."""
    n, total = len(set(getattr(bundle, "captures", []) or [])) or 1, \
        getattr(bundle, "cluster_nodes", None)
    if total is None:
        return "%d node%s of a cluster of unknown size" % (n, "" if n == 1 else "s")
    return "%d of %d node%s" % (n, total, "" if total == 1 else "s")


def _check_then_drop(name, ddl=None):
    """A DROP INDEX preceded by the read-only check to run on every node first (each node's
    pg_stat_user_indexes counts only the scans that went through it)."""
    return ("-- first, on every node: SELECT idx_scan FROM pg_stat_user_indexes WHERE "
            "indexrelname = '%s';  drop only if 0 on all of them\n%s" % (
                schema_mod.bare(name), ddl or schema_mod.drop_index_sql(name)))


def partition_copies(sch, name):
    """Indexes the server created on partitions as copies of partitioned index `name`, at any
    depth (catalog mode only: Index.parent)."""
    out, todo = [], [name]
    while todo:
        n = todo.pop()
        kids = sorted(i.name for i in sch.indexes.values() if getattr(i, "parent", None) == n)
        out.extend(kids)
        todo.extend(kids)
    return out


def _old_index_note(name):
    """After a table swap the old index belongs to the retired table: the new table never had
    it, and dropping the old table drops it. A DROP INDEX for it would be wrong at any point
    (before the swap it removes a live access path, and any foreign key built on it blocks
    it), so it is a note, not DDL."""
    return ("-- %s is not created on the new table; it goes when the old table is dropped "
            "after the swap" % schema_mod.qn(name))


def search_path(bundle):
    """Schemas an unqualified table name resolves in, from pg_settings search_path (the
    server default "$user", public without it). "$user" names a schema per login role, which
    the bundle does not know, so it is skipped."""
    raw = bundle.settings.get("search_path") or '"$user", public'
    out = [p.strip().strip('"') for p in raw.split(",")]
    return [p for p in out if p and p != "$user"] or ["public"]


def build_schema(bundle):
    ps = planner_settings(bundle)
    if getattr(bundle, "catalog_schema", None) is not None:  # read from a scratch server
        sch = copy.deepcopy(bundle.catalog_schema)
        sch.search_path = search_path(bundle)
        bundle.align_names(sch)
        return sch
    sch = schema_mod.parse(bundle.ddl, db_colocated=ps["colocated"],
                           hash_default=ps["hash_default"])
    sch.search_path = search_path(bundle)
    bundle.align_names(sch)
    return sch


def run(bundle, plans=None, schema_override=None):
    from . import versions as versions_mod
    from . import preflight as preflight_mod
    rules = load_rules()
    ps = planner_settings(bundle)
    sch = schema_override if schema_override is not None else build_schema(bundle)
    bundle.align_names(sch)
    # Rules whose inputs are missing are muted, not guessed (rules/inputs.json).
    pf = preflight_mod.assess(bundle)
    col = Collector(muted=pf["muted"])
    sound, open_items, recon = [], [], []

    # Inputs that are missing are open items, never guesses.
    if not bundle.pss and not bundle.queries_sql:
        open_items.append("No pg_stat_statements and no queries.sql: access patterns unknown, "
                          "so no access-path findings and no traffic weighting.")
    elif not bundle.pss:
        open_items.append("No pg_stat_statements: patterns from queries.sql are UNRANKED; "
                          "severity is not traffic-weighted.")
    dups = getattr(bundle, "stats_duplicates", None) or []
    if dups:
        open_items.append(
            "pg_stats lists %d column(s) more than once (for example per-node variants of an "
            "aggregated export): %s. One row was kept for each (the one reported by the most "
            "nodes when the file says, otherwise the first); findings on these columns rest on "
            "that row." % (len(dups), ", ".join("%s.%s (%d rows)" % d for d in dups[:8]) +
                           (" ..." if len(dups) > 8 else "")))
    if not bundle.stats:
        open_items.append("No pg_stats: null fraction, cardinality, skew and monotonicity of "
                          "key columns are unmeasured; stats rules fall back to DDL hints.")
    if not bundle.reltuples:
        open_items.append("No reltuples: table sizes unknown; access-path severity is not "
                          "size-adjusted.")
    if not bundle.settings:
        open_items.append("No pg_settings: cost-model, bitmap-scan and merge-scan settings "
                          "unknown; findings that depend on them are conditional.")
    rels = bundle.release_sources()
    for r, where, text in rels:
        if r is None:
            open_items.append("Release: %s says %r, which names no YugabyteDB release (a "
                              "PostgreSQL source?); not used." % (where, text[:80]))
    if len({r for r, _, _ in rels if r}) > 1:
        open_items.append("Release sources disagree: %s. The review uses %s, from the first "
                          "source in this order: the user, SELECT version(), "
                          "pg_settings server_version, the ysql_dump header." % (
                              "; ".join("%s from %s" % (r, w) for r, w, _ in rels if r),
                              bundle.version))
    if ps["release_note"]:
        open_items.append("Release: " + ps["release_note"] + ".")
    if bundle.version and ps["release"] != bundle.version and \
            versions_mod.vt(ps["release"] or "0")[:4] != versions_mod.vt(bundle.version)[:4]:
        build = ("Build it with `yb-model.py update-versions --repo <yugabyte-db checkout> "
                 "--release %s` (local, no network) or `yb-model.py update-versions --github "
                 "--release %s` (fetches about 25 source files of that release from "
                 "github.com; ask the user first), then review again." % (bundle.version,
                                                                          bundle.version))
        if not versions_mod.has_table():
            open_items.append("RELEASE-DATA-MISSING: no release facts have been built on this "
                              "machine yet (rules/versions.json is a local cache), so setting "
                              "defaults, feature availability and test pinning for %s are "
                              "unknown. %s" % (bundle.version, build))
        elif ps["release"]:
            open_items.append("RELEASE-DATA-MISSING: %s is not in the local release cache, so "
                              "release facts come from the nearest earlier release, %s. %s" % (
                                  bundle.version, ps["release"], build))
        else:
            open_items.append("RELEASE-DATA-MISSING: %s is older than every release in the "
                              "local cache, so its release facts are unknown (a newer release "
                              "is never used in its place). %s" % (bundle.version, build))
    for name, per in sorted(ps["conditional"].items()):
        open_items.append("%s is not in the bundle and its default on %s depends on how the "
                          "cluster was deployed (%s). Findings that depend on it are "
                          "conditional; collect pg_settings to settle it." % (
                              name, ps["release"], "; ".join("%s: %s" % (k, v)
                                                            for k, v in sorted(per.items()))))
    cov = versions_mod.oracle_coverage(ps["release"])
    if ps["release"] and cov is None and versions_mod.has_observations():
        near = versions_mod.oracle_nearest(ps["release"])
        open_items.append("The planner model has not been checked by the oracle on %s%s; "
                          "access-path findings rely on its regress-test citations." % (
                              ps["release"], (" (nearest checked: %s)" % near) if near else ""))
    if ps["unavailable"]:
        open_items.append("Not available on %s: %s. Fixes that would need them are replaced by "
                          "release-appropriate alternatives." % (
                              ps["release"], ", ".join(ps["unavailable"])))
    for note in sch.parse_notes:
        open_items.append("DDL parse: " + note)

    dropped, listed = [], {}
    patterns = build_patterns(bundle, sch, dropped, listed)
    for label, folder in getattr(bundle, "duplicate_captures", []):
        open_items.append("The capture in %s is from node %s, which another folder already "
                          "holds; it was not added a second time." % (folder, label))
    if (bundle.pss or bundle.index_usage or bundle.table_usage) and not _usage_complete(bundle):
        open_items.append(
            "Workload and usage counters cover %s: pg_stat_statements and pg_stat_user_indexes "
            "/ _tables count only the statements that ran through the node they are read on. "
            "Pattern ranking holds when connections are spread across nodes; an index with no "
            "scans may still be used through another node, so drops start with a check on "
            "every node. Running collect.sql once per node, each into its own folder of the "
            "bundle, removes this." % _usage_scope(bundle))
    for name, (hit, others) in sorted(sch.ambiguous.items()):
        open_items.append(
            "Queries name %s without a schema, and %s define it: they were read as %s, the "
            "first on the search path (%s)%s. If the application sets search_path per "
            "connection (one schema per tenant, for example), the same statements may run "
            "against %s too." % (
                name, ", ".join([hit] + others), hit, ", ".join(sch.search_path),
                "" if bundle.settings.get("search_path") else
                ", the server default; pg_settings did not include search_path",
                ", ".join(others)))
    if bundle.pss and listed:
        add = listed["added"]
        n = listed["matched"] + len(add)
        open_items.append(
            ("queries.sql lists %d statement(s): %d found in pg_stat_statements and ranked "
             "from it, %d not found (%s) and reviewed UNRANKED, weighted as if hot because "
             "their traffic is unknown (new, rare, not run since the statistics were reset, or "
             "outside the rows captured)." % (
                 n, listed["matched"], len(add), ", ".join(add[:12]) +
                 (" ..." if len(add) > 12 else ""))) if add else
            "All %d statement(s) in queries.sql are in pg_stat_statements and are ranked from "
            "it." % n)
    if dropped:
        all_ms = math.fsum(r["total_ms"] for r in bundle.pss) or 1.0
        by = {}
        for reason, r in dropped:
            n, ms = by.get(reason, (0, 0.0))
            by[reason] = (n + 1, ms + r["total_ms"])
        open_items.append("%d of %d pg_stat_statements entries (%.1f%% of statement time) are "
                          "not analysed as access patterns: %s. Shares quoted in this review "
                          "are of all statement time." % (
                              len(dropped), len(bundle.pss),
                              100 * math.fsum(r["total_ms"] for _, r in dropped) / all_ms,
                              "; ".join("%s: %d (%.1f%%)" % (k, n, 100 * ms / all_ms)
                                        for k, (n, ms) in sorted(by.items()))))
    chosen_by = {}   # index name -> [pattern ids]

    for pat in patterns:
        w = pat["weight"]
        pat["access"] = []
        for sh in sqlshape.flatten(pat["shape"]):
            if sh.unresolved:
                open_items.append("%s: could not tie column(s) %s to a table" %
                                  (pat["id"], ", ".join(sorted(set(sh.unresolved)))))
            for table in sh.tables:
                if table not in sch.tables:
                    continue
                preds = sh.preds_for(table)
                if sh.kind == "insert":
                    continue
                t = sch.tables[table]
                tcols = t.cols
                rows = table_rows(bundle, sch, table)
                ops = _pred_index(preds)
                filt = [p for p in preds if p.op != "join"]
                paths = []
                for idx in sch.indexes_on(table):
                    r = eval_path(idx, ops, sh, table, ps["cbo"])
                    if r["usable"]:
                        r["order_ok"] = order_ok(idx, ops, sh, table)
                        cov, miss = covering(idx, sh, table, tcols)
                        r["covering"], r["missing"] = cov, miss
                    paths.append((idx, r))
                best = choose([r for _, r in paths], limit=bool(sh.limit))
                walk = ordered_walk(sch, table, ops, sh) if best is None else None
                acc = {"table": table, "rows": rows,
                       "path": best["index"] if best else (walk.name if walk else None),
                       "walk": bool(walk),
                       "bound": best["bound"] if best else 0,
                       "point": bool(best and best["full_unique"]),
                       "order_ok": best.get("order_ok") if best else None,
                       "covering": best.get("covering") if best else None,
                       "skip_scan": bool(best and best["skip_scan"])}
                pat["access"].append(acc)
                if best:
                    chosen_by.setdefault(best["index"], []).append(pat["id"])
                if walk:
                    chosen_by.setdefault(walk.name, []).append(pat["id"])
                if not preds and not sh.order:
                    continue
                sev_shift = WEIGHT_SHIFT[w] + size_shift(rows)
                pid = [pat["id"]]
                size_txt = (" (~%s rows)" % _fmt(rows)) if rows is not None else ""
                # A partial index that would bind more key columns than the chosen path, rejected
                # only because the query does not imply its predicate.
                if best is not None:
                    better = [(i, r) for i, r in paths if r["status"] == "partial_not_implied"
                              and (r.get("would_bind") or 0) > best["bound"]]
                    if better:
                        i, r = better[0]
                        col.add(Finding("CAP021", shift("medium", sev_shift), "probable",
                                        "%s on %s" % (i.name, table),
                                        "%s could use %s, which binds %d key column(s) to the "
                                        "chosen %s's %d, but its predicate (%s) is not implied by "
                                        "the query%s." % (
                                            pat["id"], i.name, r["would_bind"], best["index"],
                                            best["bound"], i.where, size_txt),
                                        pid, table=table, index=i.name))
                # A keyset cursor written only as a row comparison: the next key column after
                # the bound prefix is constrained by ROW(...) < ROW(...) and nothing DocDB can
                # seek on, so every page re-reads the rows before its cursor.
                bidx = sch.indexes.get(best["index"]) if best else None
                if bidx is not None and best["bound"] < len(bidx.keys):
                    nxt = bidx.keys[best["bound"]]
                    o = ops.get(_keyid(nxt), set())
                    rc = [p.col for p in preds if p.op == "rowcmp"]
                    if rc and rc[0] == nxt.col and not o & set(BIND_ANY_EQ + BIND_RANGE):
                        col.add(Finding("CAP013", shift("medium", sev_shift), "probable",
                                        "%s on %s" % (bidx.name, table),
                                        "%s pages through %s with a row comparison on (%s); "
                                        "%s has no plain bound, so DocDB scans from the "
                                        "start of the %s key and rechecks every row before "
                                        "the cursor%s." % (
                                            pat["id"], bidx.name, ", ".join(rc), nxt.col,
                                            "hash" if best["hash_bound"] else "leading",
                                            size_txt),
                                        pid, table=table, index=bidx.name,
                                        fix="Keep the row comparison and add a plain bound "
                                            "on its first column in the same direction: AND "
                                            "%s <= $cursor (when paging with <) or AND %s >= "
                                            "$cursor (with >). DocDB seeks on the plain "
                                            "bound; the row comparison then only breaks "
                                            "ties within that value." % (rc[0], rc[0])))
                if walk is not None and preds and filt:
                    cols_txt = ", ".join(sorted({p.col or p.expr for p in filt}))
                    col.add(Finding("CAP012", shift("medium", sev_shift), "probable",
                                    "%s on %s" % (walk.name, table),
                                    "%s has no index to seek on its filter (%s), so the planner "
                                    "walks %s in ORDER BY order and filters rows until LIMIT is "
                                    "met%s. Cost depends on how many rows match: few matches "
                                    "means most of the index is read." % (
                                        pat["id"], cols_txt, walk.name, size_txt),
                                    pid, table=table, index=walk.name,
                                    fix="Index the filter column(s) with the ORDER BY columns "
                                        "after them (equality columns HASH, then the order "
                                        "columns), so the query seeks and reads only the LIMIT "
                                        "rows."))
                elif best is None and preds:
                    only_join = not filt
                    hash_unb = [(i, r) for i, r in paths if r["status"] == "hash_unbound"]
                    partial = [(i, r) for i, r in paths if r["status"] == "partial_not_implied"
                               and r.get("would_bind")]
                    or_cols = sorted({p.col for p in filt if p.op == "or" and p.col})
                    sarg = [p for p in filt if p.col and p.op in ("eq", "in", "range",
                                                                    "prefix", "isnull",
                                                                    "notnull")]
                    if only_join:
                        jc = sorted({p.col for p in preds if p.op == "join"})
                        col.add(Finding("CAP031", shift("medium", sev_shift), "probable",
                                        "%s(%s)" % (table, ", ".join(jc)),
                                        "%s joins %s on %s, and no index on %s is bound by "
                                        "those columns%s." % (pat["id"], table, ", ".join(jc),
                                                              table, size_txt),
                                        pid, table=table))
                    elif hash_unb:
                        i, r = hash_unb[0]
                        bound = [k for k in i.hash_cols if k.label not in r["hash_missing"]]
                        rest = [k for k in i.keys if k not in bound]
                        newkey = "(%s) HASH%s" % (", ".join(k.sql for k in bound), "".join(
                            ", %s %s" % (k.sql, "ASC" if k.mode == "HASH" else k.mode)
                            for k in rest))
                        ranged_hash = any(ops.get(_keyid(k), set()) & set(BIND_RANGE)
                                          for k in i.hash_cols)
                        if ranged_hash:
                            hc = ", ".join(k.sql for k in i.hash_cols)
                            c1fix = ("A range on a hash column cannot seek. For chunked scans or "
                                     "exports, split the work on yb_hash_code(%s) ranges "
                                     "(0..65535), which seek; otherwise bind every hash column "
                                     "with equality." % hc)
                            c1ddl = None
                        elif i.is_pk:
                            c1fix = ("Re-key %s as PRIMARY KEY (%s): same uniqueness, and the "
                                     "pattern then binds the whole hash group." % (table, newkey))
                            c1ddl = _rekey_comment(table, newkey, sch.is_colocated(table),
                                                   fks=sch.fks_referencing(table))
                        else:
                            c1fix = "Index the bound columns as the hash group: (%s)." % newkey
                            c1ddl = "CREATE INDEX CONCURRENTLY %s ON %s (%s);" % (
                                schema_mod.qi(schema_mod.ident("%s_%s" % (
                                    schema_mod.bare(table), "_".join(
                                        re.sub(r"\W+", "_", k.label).strip("_")
                                        for k in bound)))),
                                schema_mod.qn(table), newkey)
                        col.add(Finding("CAP001", shift("high", sev_shift), "probable",
                                        "%s on %s" % (i.name, table),
                                        "%s filters %s but does not bind %s of %s's hash group "
                                        "[%s] with equality; no other index serves it%s." % (
                                            pat["id"], table, ", ".join(r["hash_missing"]),
                                            i.name, ", ".join(k.label for k in i.hash_cols),
                                            size_txt),
                                        pid, table=table, index=i.name, fix=c1fix, ddl=c1ddl))
                    elif partial:
                        i, r = partial[0]
                        col.add(Finding("CAP021", shift("medium", sev_shift), "probable",
                                        "%s on %s" % (i.name, table),
                                        "%s could use %s, but its predicate (%s) is not implied "
                                        "by the query%s." % (pat["id"], i.name, i.where,
                                                             size_txt),
                                        pid, table=table, index=i.name))
                    elif or_cols and not sarg:
                        if ps["bitmap"] and all(any(ix.keys and ix.keys[0].col == c for ix in
                                                    sch.indexes_on(table)) for c in or_cols):
                            pass
                        else:
                            col.add(Finding("CAP040", shift("medium", sev_shift), "probable",
                                            "%s(%s)" % (table, ", ".join(or_cols)),
                                            "%s filters %s with OR over %s; bitmap scans %s%s." % (
                                                pat["id"], table, ", ".join(or_cols),
                                                ("do not exist on %s" % ps["release"])
                                                if "yb_enable_bitmapscan" in ps["unavailable"]
                                                else "are " + {True: "on", False: "off",
                                                               None: "unknown"}[ps["bitmap"]],
                                                size_txt),
                                            pid, table=table))
                    elif not sarg:
                        desc = ", ".join(sorted({"%s %s" % (p.col or p.expr, p.op)
                                                 for p in filt}))
                        exprs = sorted({p.expr for p in filt if p.expr and p.op in ("eq", "in")})
                        cfix, cddl = None, None
                        if exprs:
                            e = exprs[0]
                            m_ = re.match(r'^(lower|upper)\(([a-z_][a-z0-9_]*|"(?:[^"]|"")+")\)$', e)
                            ecol = m_ and (m_.group(2)[1:-1].replace('""', '"')
                                           if m_.group(2).startswith('"') else m_.group(2))
                            uniq = m_ and any(ix.unique and ix.keys and ix.keys[0].col == ecol
                                              for ix in sch.indexes_on(table))
                            proj = sorted({c for tb, c in sh.select_cols if tb == table} |
                                          {x.col for x in preds if x.col})
                            if table in sh.select_all:
                                proj = [c for c in t.col_order]
                            inc = [c for c in proj if c in t.cols]
                            cddl = "CREATE %sINDEX CONCURRENTLY %s ON %s (%s)%s;" % (
                                "UNIQUE " if uniq else "", schema_mod.qi(schema_mod.ident(
                                    "%s_%s" % (schema_mod.bare(table), re.sub(
                                        r"[^a-z0-9]+", "_", e.lower()).strip("_")))),
                                schema_mod.qn(table),
                                ("(%s) ASC" if sch.is_colocated(table) else "(%s) HASH") % e,
                                (" INCLUDE (%s)" % ", ".join(schema_mod.qi(c) for c in inc))
                                if inc else "")
                            cfix = ("Add an expression index that matches the query text exactly "
                                    "(%s)." % e)
                            if uniq:
                                cfix += (" The existing unique index on %s is case-sensitive while "
                                         "the lookup is not, so values differing only in case can "
                                         "coexist; making the expression index UNIQUE closes that "
                                         "(check for existing case duplicates first), after which "
                                         "the plain unique index can be dropped." % ecol)
                        col.add(Finding("CAP003", shift("medium", sev_shift), "probable",
                                        "%s(%s)" % (table, desc),
                                        "%s filters %s only with non-seekable predicates "
                                        "(%s)%s." % (pat["id"], table, desc, size_txt),
                                        pid, table=table, fix=cfix, ddl=cddl))
                    else:
                        cols_txt = ", ".join(sorted({p.col or p.expr for p in sarg}))
                        col.add(Finding("CAP002", shift("high", sev_shift), "probable",
                                        "%s(%s)" % (table, cols_txt),
                                        "%s filters %s on %s and no index leads with any of "
                                        "them%s." % (pat["id"], table, cols_txt, size_txt),
                                        pid, table=table))
                if best is not None:
                    idx = sch.indexes.get(best["index"]) or t.pk
                    if best.get("order_ok") is False:
                        in_hash = best["in_on_hash"]
                        if in_hash and sh.limit and not (ps["merge_streams"] or 0) > 0:
                            col.add(Finding("CAP011", shift("medium", sev_shift), "probable",
                                            "%s on %s" % (best["index"], table),
                                            "%s uses IN on the hash key of %s with ORDER BY %s "
                                            "and LIMIT; %s." % (
                                                pat["id"], best["index"],
                                                ", ".join("%s %s" % (c, d) for _, c, d in sh.order),
                                                ("merge scan streams do not exist on %s" %
                                                 ps["release"])
                                                if "yb_max_merge_scan_streams" in ps["unavailable"]
                                                else "yb_max_merge_scan_streams is %s" % (
                                                    ps["merge_streams"]
                                                    if ps["merge_streams"] is not None
                                                    else "unknown")),
                                            pid, table=table, index=best["index"]))
                        elif not in_hash:
                            col.add(Finding("CAP010", shift("medium", sev_shift), "probable",
                                            "%s on %s" % (best["index"], table),
                                            "%s orders by %s%s but its access path %s [%s] "
                                            "cannot return rows in that order." % (
                                                pat["id"],
                                                ", ".join("%s %s" % (c, d) for _, c, d in sh.order),
                                                " with LIMIT" if sh.limit else "",
                                                best["index"], idx.signature() if idx else "?"),
                                            pid, table=table, index=best["index"]))
                    if sh.kind == "select" and not best["covering"] and not best["is_pk"]:
                        miss = best["missing"]
                        fix = ("Project only the needed columns; SELECT * cannot be covered."
                               if miss == ["*"] else
                               "CREATE INDEX ... INCLUDE (%s) replacing %s, if the columns are "
                               "small and stable." % (", ".join(miss), best["index"]))
                        ddl = None
                        if miss == ["*"] and idx is not None and 0 < len(tcols) <= \
                                THRESHOLDS["include_all_cols"]:
                            rest = [c for c in t.col_order if c not in idx.key_names]
                            miss = rest
                            fix = ("The table has only %d columns: INCLUDE (%s) makes %s covering "
                                   "even for SELECT *, then drop the original." % (
                                       len(tcols), ", ".join(rest), best["index"]))
                        if miss != ["*"] and idx is not None:
                            new = schema_mod.ident(idx.name + "_cov")
                            ddl = schema_mod.index_sql(
                                idx, new, table, add_include=miss,
                                colocated=sch.is_colocated(table),
                                partitioned=bool(sch.tables[table].partition_by)) + "\n" + \
                                schema_mod.drop_sql(idx, new)
                            if idx.unique:
                                fix += (" Keep it UNIQUE: the original enforces a constraint, so "
                                        "drop it only after the replacement is valid.")
                        col.add(Finding("CAP020", shift("medium", sev_shift + (1 if best["full_unique"]
                                                                                else 0)),
                                        "probable", "%s on %s" % (best["index"], table),
                                        "%s reads %s via %s, which lacks %s." % (
                                            pat["id"], table, best["index"],
                                            "every column (SELECT *)" if miss == ["*"]
                                            else ", ".join(miss)),
                                        pid, fix=fix, table=table, index=best["index"], ddl=ddl))
                # Partition pruning.
                if t.partition_by and (preds or sh.kind in ("update", "delete")):
                    pcols = t.partition_by[1]
                    pruned = all(ops.get(c, set()) & {"eq", "in", "range", "prefix", "join"}
                                 for c in pcols)
                    if not pruned:
                        n = len(t.partitions)
                        col.add(Finding("CAP030", shift("high", WEIGHT_SHIFT[w]), "probable",
                                        table, "%s reads %s without a predicate on its partition "
                                        "key (%s); %s partitions are probed." % (
                                            pat["id"], table, ", ".join(pcols),
                                            n if n else "all"),
                                        pid, table=table))
        # Workload measurements.
        r = pat["pss"]
        if r and r["calls"]:
            calls = r["calls"]
            sc, rt = r.get("docdb_rows_scanned"), r.get("docdb_rows_returned")
            agg = any(x.aggregate for x in sqlshape.flatten(pat["shape"]))
            if sc is not None and rt is not None and sc > 0 and not agg:
                ratio = sc / max(rt, 1.0)
                if ratio >= THRESHOLDS["readamp_ratio"] and \
                        sc / calls >= THRESHOLDS["readamp_min_scanned"]:
                    col.add(Finding("WRK001", shift("high", WEIGHT_SHIFT[w]), "confirmed",
                                    pat["id"], "%s: DocDB scanned %s rows per call and returned "
                                    "%s (ratio %.0f:1) over %s calls." % (
                                        pat["id"], _fmt(sc / calls), _fmt(rt / calls), ratio,
                                        _fmt(calls)), [pat["id"]]))
            rpc = r.get("docdb_read_rpcs")
            if rpc is not None and pat["kind"] == "select" and \
                    rpc / calls >= THRESHOLDS["rpcs_per_call"]:
                col.add(Finding("WRK002", shift("medium", WEIGHT_SHIFT[w]), "confirmed",
                                pat["id"], "%s: %.1f DocDB read RPCs per call over %s calls." % (
                                    pat["id"], rpc / calls, _fmt(calls)), [pat["id"]]))

    # --- statistics rules ------------------------------------------------------------
    write_weight = {}
    for pat in patterns:
        for sh in sqlshape.flatten(pat["shape"]):
            if sh.kind in ("insert", "update", "delete"):
                for t in sh.tables:
                    cur = write_weight.get(t)
                    order = ["HOT", "UNRANKED", "WARM", "COLD"]
                    if cur is None or order.index(pat["weight"]) < order.index(cur):
                        write_weight[t] = pat["weight"]
    for tname in sorted(sch.tables):
        t = sch.tables[tname]
        if t.partition_by and not t.pk and not sch.indexes_on(tname):
            continue
        rows = table_rows(bundle, sch, tname)
        coloc = sch.is_colocated(tname)
        tu = bundle.table_usage.get(tname, {})
        writes_measured = (tu.get("n_tup_ins") or 0) + (tu.get("n_tup_upd") or 0) > 0
        for idx in sch.indexes_on(tname, own_only=True):
            if not idx.keys or not idx.keys[0].col:
                continue
            lead = idx.keys[0]
            st = col_stats(bundle, sch, tname, lead.col)
            if lead.mode == "HASH" and not coloc:
                # Under UNIQUE ... NULLS NOT DISTINCT a NULL key conflicts: at most one NULL row.
                if not idx.is_pk and not (idx.unique and idx.nulls_not_distinct):
                    nullable = not t.cols.get(lead.col, {}).get("notnull", False)
                    guarded = bool(idx.where) and \
                        re.search(r"\b%s\s+is\s+not\s+null" % re.escape(lead.col), idx.where)
                    if st is not None and not guarded:
                        nf = st["null_frac"] or 0.0
                        null_rows = nf * rows if rows else None
                        if nf >= THRESHOLDS["null_frac_flag"] and \
                                (null_rows is None or null_rows >= THRESHOLDS["null_rows_min"]):
                            sev = "high" if (nf >= THRESHOLDS["null_frac_high"] or
                                             (null_rows or 0) >= THRESHOLDS["null_rows_high"]) \
                                else "medium"
                            sev = shift(sev, 1 if size_shift(rows) == 2 else 0)
                            col.add(Finding("STA001", sev, "confirmed",
                                            "%s(%s)" % (idx.name, lead.col),
                                            "%s hashes %s.%s; null_frac %.3f%s." % (
                                                idx.name, tname, lead.col, nf,
                                                (" (~%s NULL rows on one hash code)" %
                                                 _fmt(null_rows)) if null_rows else ""),
                                            table=tname, index=idx.name,
                                            fix="Replace %s with a partial index WHERE %s IS NOT "
                                                "NULL: create it, verify indisvalid, then drop "
                                                "the original. Patterns that use it must carry "
                                                "AND %s IS NOT NULL (or an equality on %s)." % (
                                                    idx.name, lead.col, lead.col, lead.col),
                                            ddl=_replace_ddl(
                                                idx, "_nn", tname, t, coloc,
                                                add_where="%s IS NOT NULL" %
                                                schema_mod.qi(lead.col))))
                    elif st is None and nullable and not guarded:
                        col.add(Finding("STA001", "low", "probable",
                                        "%s(%s)" % (idx.name, lead.col),
                                        "%s hashes %s.%s, which is declared nullable; null_frac "
                                        "not measured." % (idx.name, tname, lead.col),
                                        table=tname, index=idx.name,
                                        evidence="pg_stats null_frac for %s.%s" % (tname, lead.col)))
                if st is not None and len(idx.hash_cols) == 1:
                    nd = n_distinct_abs(st, rows)
                    big = rows is None or rows >= THRESHOLDS["tiny_rows"]
                    if nd is not None and big and nd < THRESHOLDS["ndistinct_medium"]:
                        sev = "high" if nd < THRESHOLDS["ndistinct_high"] else "medium"
                        sev = shift(sev, size_shift(rows))
                        col.add(Finding("STA002", sev, "confirmed",
                                        "%s(%s)" % (idx.name, lead.col),
                                        "%s hashes %s.%s, which has ~%s distinct values%s." % (
                                            idx.name, tname, lead.col, _fmt(nd),
                                            (" over ~%s rows" % _fmt(rows)) if rows else ""),
                                        table=tname, index=idx.name, fix=_key_fix(idx, tname)))
                    elif nd is not None and nd >= THRESHOLDS["well_distributed_nd"] and \
                            (st["null_frac"] or 0) < \
                            THRESHOLDS["null_frac_flag"] and \
                            (not st["mcf"] or st["mcf"][0] < THRESHOLDS["mcv_medium"]):
                        sound.append("%s: hash lead %s.%s is well distributed (~%s distinct, "
                                     "null_frac %.3f%s)." % (
                                         idx.name if not t.partition_of else
                                         "%s partitions" % t.partition_of,
                                         t.partition_of or tname, lead.col, _fmt(nd)
                                         if not t.partition_of else "every partition >" +
                                         _fmt(10 ** int(len(str(int(nd))) - 1)),
                                         st["null_frac"] or 0,
                                         (", top value %.1f%%" % (100 * st["mcf"][0]))
                                         if st["mcf"] else ""))
                if st is not None and st["mcf"] and len(idx.hash_cols) == 1:
                    top = st["mcf"][0]
                    big = rows is None or rows >= THRESHOLDS["tiny_rows"]
                    if big and top >= THRESHOLDS["mcv_medium"]:
                        sev = "high" if top >= THRESHOLDS["mcv_high"] else "medium"
                        sev = shift(sev, size_shift(rows))
                        col.add(Finding("STA003", sev, "confirmed",
                                        "%s(%s)" % (idx.name, lead.col),
                                        "%s hashes %s.%s; its most common value holds %.1f%% of "
                                        "rows." % (idx.name, tname, lead.col, 100 * top),
                                        table=tname, index=idx.name, fix=_key_fix(idx, tname)))
            elif lead.mode in ("ASC", "DESC") and not coloc and not idx.is_pk and t.pk and \
                    t.pk.keys and t.pk.keys[0].mode == "HASH" and \
                    not _few_values(t, lead.col, st, rows, idx) and \
                    (re.search(r"timestamp|date", t.cols.get(lead.col, {}).get("type", "")) or
                     MONOTONIC_NAME.match(lead.col)):
                nd = n_distinct_abs(st, rows) if st else None
                col.add(Finding("STA004", "medium", "probable", "%s(%s)" % (idx.name, lead.col),
                                "%s leads with %s.%s %s (%s%s) on a hash-sharded table; new values "
                                "arrive at one end of the range, so inserts concentrate on the "
                                "edge tablet of this index. (pg_stats correlation cannot show "
                                "this on a hash-sharded table.)" % (
                                    idx.name, tname, lead.col, lead.mode,
                                    t.cols.get(lead.col, {}).get("type", "?"),
                                    (", ~%s distinct" % _fmt(nd)) if nd else ""),
                                table=tname, index=idx.name,
                                fix="Bucket the index so inserts spread, or HASH it if no range "
                                    "query needs it; drop it if nothing uses it.",
                                ddl=_bucket_ddl(idx, tname, t, lead.col)))
            elif lead.mode in ("ASC", "DESC") and not coloc and \
                    not _few_values(t, lead.col, st, rows, idx):
                # YSQL's ANALYZE fetches its sample in ybctid order, so pg_stats.correlation is
                # agreement with the primary key's order: about 1 for the key's own leading
                # column whatever the insert order, and noise on a hash-sharded table. Insert
                # order needs a sequence default, a timestamp type or name, or measured writes;
                # random identifiers spread over the type's range are evidence against it.
                range_table = bool(t.pk and t.pk.keys and t.pk.keys[0].mode in ("ASC", "DESC"))
                if range_table and st is not None and st.get("correlation") is not None:
                    corr = st["correlation"]
                    ww = write_weight.get(tname)
                    hint = _insert_ordered_hint(t, lead.col)
                    pk_lead = t.pk.keys[0].col == lead.col
                    pk_hint = _insert_ordered_hint(t, t.pk.keys[0].col)
                    # Correlation helps only for a secondary range index, and only as agreement
                    # with a primary key that is itself insert-ordered.
                    if not hint and not pk_lead and pk_hint and \
                            abs(corr) >= THRESHOLDS["corr_monotonic"]:
                        hint = "correlation %.2f with the primary key, which is insert-ordered " \
                               "(%s)" % (corr, pk_hint)
                    fires = bool(hint) and not _random_ids(
                        st, t.cols.get(lead.col, {}).get("type", ""))
                    if fires:
                        sev = "high" if ww in ("HOT", "UNRANKED") or (not ww and writes_measured) \
                            else "medium"
                        sev = shift(sev, size_shift(rows))
                        if idx.is_pk:
                            s4fix = ("Rebuild %s with PRIMARY KEY ((%s) HASH) if access by %s is "
                                     "equality. A primary key cannot contain an expression, so a "
                                     "bucketed range key needs a stored bucket column." % (
                                         tname, lead.col, lead.col))
                            s4ddl = _rekey_comment(tname, "(%s) HASH" % schema_mod.qi(lead.col),
                                                   coloc, fks=sch.fks_referencing(tname))
                        else:
                            s4fix = ("Replace %s with a bucketed index so inserts spread over N "
                                     "tablets; ORDER BY %s then merges N buckets." % (idx.name,
                                                                                       lead.col))
                            s4ddl = _bucket_ddl(idx, tname, t, lead.col)
                        seen_writes = bool(ww or writes_measured)
                        why = hint
                        col.add(Finding("STA004", sev,
                                        "confirmed" if seen_writes else "probable",
                                        "%s(%s)" % (idx.name, lead.col),
                                        "%s leads with %s.%s %s, which looks insert-ordered (%s)%s."
                                        % (idx.name, tname, lead.col, lead.mode, why,
                                           (", and the table is written (%s)" % (
                                               ("%s write pattern" % ww) if ww else
                                               "pg_stat_user_tables")) if seen_writes else
                                           "; write activity was not captured, so this applies "
                                           "only if rows are still inserted in that order"),
                                        table=tname, index=idx.name, fix=s4fix, ddl=s4ddl))
            if idx.unique and not idx.is_pk and st is not None and (st["null_frac"] or 0) > 0 \
                    and not idx.where and not idx.nulls_not_distinct:
                col.add(Finding("STA005", "info", "confirmed", "%s(%s)" % (idx.name, lead.col),
                                "%s is unique on %s.%s, which is %.1f%% NULL." % (
                                    idx.name, tname, lead.col, 100 * st["null_frac"]),
                                table=tname, index=idx.name))
        # Missing key statistics are open items.
        if bundle.stats:
            for idx in sch.indexes_on(tname, own_only=True):
                if idx.keys and idx.keys[0].col and \
                        col_stats(bundle, sch, tname, idx.keys[0].col) is None and \
                        not t.partition_by:
                    open_items.append("No pg_stats row for %s.%s (lead of %s); run ANALYZE %s."
                                      % (tname, idx.keys[0].col, idx.name, tname))

    # --- index usage and write amplification ----------------------------------------
    unused_idx = []
    for tname in sorted(sch.tables):
        secondary = [i for i in sch.indexes_on(tname, own_only=True) if not i.is_pk]
        ww = write_weight.get(tname)
        if ww == "HOT" and len(secondary) >= THRESHOLDS["write_hot_indexes"]:
            col.add(Finding("WRK004", "medium", "confirmed" if bundle.pss else "probable",
                            tname, "%s is written by HOT patterns and carries %d secondary "
                            "indexes (%s)." % (tname, len(secondary),
                                               ", ".join(i.name for i in secondary)),
                            table=tname))
        for i in secondary:
            u = bundle.index_usage.get(i.name)
            if u is not None and u["idx_scan"] == 0 and not i.unique:
                every = _usage_complete(bundle)
                col.add(Finding("WRK003", "medium" if ww else "low",
                                "confirmed" if every else "probable", i.name,
                                "%s on %s has idx_scan = 0 since stats reset%s%s." % (
                                    i.name, tname,
                                    (" (postmaster start %s)" % bundle.meta["postmaster_start"])
                                    if bundle.meta.get("postmaster_start") else "",
                                    "" if every else " on %s" % _usage_scope(bundle)),
                                table=tname, index=i.name,
                                fix="Confirm no batch or periodic job needs it, then drop it." if
                                every else "pg_stat_user_indexes counts only the scans that ran "
                                "through the node it is read on: check idx_scan on every node, "
                                "and confirm no batch or periodic job needs it, before dropping "
                                "it.",
                                ddl=schema_mod.drop_index_sql(i.name) if every else
                                _check_then_drop(i.name)))
            elif (bundle.pss or bundle.declared_complete) and i.name not in chosen_by and \
                    not i.unique and \
                    not (u is not None and u["idx_scan"] > 0) and \
                    any(tname in p["tables"] for p in patterns):
                unused_idx.append(i.name)

    # --- partition runway --------------------------------------------------------------
    cap = (bundle.meta.get("captured_at") or "")[:10]
    for tname in sorted(sch.tables):
        t = sch.tables[tname]
        if not (t.partition_by and t.partition_by[0] == "RANGE" and t.partitions):
            continue
        kids = [sch.tables[p] for p in t.partitions if p in sch.tables]
        if any(k.is_default for k in kids):
            continue
        bounds = sorted(k.bound_to[:10] for k in kids if k.bound_to and
                        re.match(r"^\d{4}-\d{2}-\d{2}", k.bound_to))
        if not bounds:
            continue
        last = bounds[-1]
        if not cap:
            open_items.append("%s: last RANGE partition ends %s; capture date unknown, so runway "
                              "not computed." % (tname, last))
            continue
        import datetime as _dt
        days = (_dt.date.fromisoformat(last) - _dt.date.fromisoformat(cap)).days
        if days <= THRESHOLDS["runway_days"]:
            col.add(Finding("PRT001", "high" if days <= 0 else "medium", "confirmed", tname,
                            "%s has no DEFAULT partition and its last partition ends %s, %d days "
                            "after capture (%s)." % (tname, last, days, cap), table=tname))

    if unused_idx:
        col.add(Finding("IDX001", "low", "probable", "indexes without a pattern",
                        "No ranked pattern uses these %d secondary indexes as an access path: %s." % (
                            len(unused_idx), ", ".join(unused_idx)),
                        evidence="idx_scan from pg_stat_user_indexes over a full business cycle; "
                                 "queries outside the captured top statements"))

    # --- column-level data quality ----------------------------------------------------
    never_set, rare, markers, in_use = [], [], [], []
    if bundle.stats:
        for tname in sorted(sch.tables):
            t = sch.tables[tname]
            rows = table_rows(bundle, sch, tname)
            if rows is not None and rows < THRESHOLDS["tiny_rows"]:
                continue
            for (tb, c), st in sorted(bundle.stats.items()):
                nf = st["null_frac"] or 0
                if tb != tname or nf < THRESHOLDS["all_null"] or SOFT_DELETE.match(c):
                    continue
                name = "%s.%s" % (tname, c)
                deps = sch.dependents(tname, c)
                if deps:  # an index reads or tests it, so the application uses it
                    in_use.append("%s (%s)" % (name, ", ".join("%s %s" % d for d in deps[:2])))
                elif re.search(r"timestamp|date", t.cols.get(c, {}).get("type", "")):
                    markers.append(name)  # NULL until the event happens
                elif nf < 1.0:
                    rare.append(("%s (~%s non-NULL rows)" % (name, _fmt((1 - nf) * rows)))
                                if rows else name)
                else:
                    never_set.append(name)
            for (tb, c), st in sorted(bundle.stats.items()):
                if tb != tname or not st["mcf"]:
                    continue
                vals = None
                try:
                    from .inputs import parse_pg_array
                    vals = parse_pg_array(st["mcv"])
                except Exception:
                    vals = None
                ndv = st.get("n_distinct")
                if not vals and ndv is not None and ndv < THRESHOLDS["near_unique_nd"] and \
                        st["mcf"][0] >= THRESHOLDS["sentinel_share"]:
                    col.add(Finding("STA007", "low", "probable", "%s.%s" % (tname, c),
                                    "%s.%s is otherwise near-unique (n_distinct %.2f) yet one "
                                    "value holds %.1f%% of rows: likely a placeholder." % (
                                        tname, c, ndv, 100 * st["mcf"][0]), table=tname,
                                    evidence="SELECT %s, count(*) FROM %s GROUP BY 1 ORDER BY 2 "
                                             "DESC LIMIT 3" % (c, tname)))
                if vals and re.match(r"^(0001-01-01|1970-01-01|1900-01-01|9999-12-31)", vals[0]) \
                        and st["mcf"][0] >= THRESHOLDS["date_sentinel_share"]:
                    col.add(Finding("STA007", "low", "confirmed", "%s.%s" % (tname, c),
                                    "%s.%s: %.1f%% of rows hold the sentinel value %s." % (
                                        tname, c, 100 * st["mcf"][0], vals[0]), table=tname))
        if never_set:
            def _some(xs):
                return "; ".join(xs[:6]) + (" ..." if len(xs) > 6 else "")
            left_out = (["%d referenced by an index, so in use: %s" % (len(in_use), _some(in_use))]
                        if in_use else []) + \
                (["%d event timestamps, which stay NULL until the event happens" % len(markers)]
                 if markers else []) + \
                (["%d that hold data: %s" % (len(rare), _some(rare))] if rare else [])
            col.add(Finding("STA006", "low", "confirmed", "never-set columns",
                            "%d column(s) had no non-NULL value in the sampled rows (soft-delete "
                            "timestamps excluded): %s.%s" % (
                                len(never_set), ", ".join(never_set),
                                (" Not counted, although at least %.1f%% NULL: %s." % (
                                    100 * THRESHOLDS["all_null"], "; ".join(left_out)))
                                if left_out else "")))
        # Tables with no statistics at all.
        nostats = sorted(t for t in sch.tables if not sch.tables[t].partition_by and
                         not any(tb == t for tb, _ in bundle.stats) and
                         not (bundle.reltuples.get(t) is not None and bundle.reltuples[t] == 0))
        if nostats:
            used = sorted({t for p in patterns for t in p["tables"]} & set(nostats))
            col.add(Finding("CFG005", "medium" if used else "low", "confirmed", "statistics",
                            "No pg_stats rows for %d table(s): %s%s. The cost model plans them "
                            "blind." % (len(nostats), ", ".join(nostats),
                                        ("; ranked patterns read %s" % ", ".join(used))
                                        if used else ""),
                            fix="Run ANALYZE on these tables and confirm auto-analyze is enabled "
                                "(ysql_enable_auto_analyze) on the cluster."))
    # Boolean-keyed indexes: at most two key values whatever the table size.
    for iname in sorted(sch.indexes):
        idx = sch.indexes[iname]
        t = sch.tables.get(idx.table)
        if not t or not idx.keys or not idx.keys[0].col:
            continue
        typ = t.cols.get(idx.keys[0].col, {}).get("type", "")
        if idx.where and re.search(r"\b%s\b" % re.escape(idx.keys[0].col), idx.where):
            continue  # a partial index on the flag value is the shape this rule recommends
        if typ in ("boolean", "bool") and len(idx.keys) == 1:
            col.add(Finding("STA008", "medium", "confirmed", iname,
                            "%s indexes %s.%s, a boolean: at most two key values%s." % (
                                iname, idx.table, idx.keys[0].col,
                                " (and NULL)" if not t.cols[idx.keys[0].col].get("notnull") else ""),
                            table=idx.table, index=iname,
                            ddl=schema_mod.drop_index_sql(iname)))
    # Absolute skew: one value with many rows on one hash code, below the share thresholds.
    for tname in sorted(sch.tables):
        rows = table_rows(bundle, sch, tname)
        if not rows:
            continue
        for idx in sch.indexes_on(tname, own_only=True):
            if not idx.keys or not idx.keys[0].col or idx.keys[0].mode != "HASH" or \
                    len(idx.hash_cols) != 1 or sch.is_colocated(tname):
                continue
            st = col_stats(bundle, sch, tname, idx.keys[0].col)
            if not st or not st["mcf"]:
                continue
            top = st["mcf"][0]
            n_top = top * rows
            if top < THRESHOLDS["mcv_medium"] and n_top >= THRESHOLDS["mcv_rows_low"]:
                sev = "high" if n_top >= THRESHOLDS["mcv_rows_high"] else \
                    "medium" if n_top >= THRESHOLDS["mcv_rows_medium"] else "low"
                col.add(Finding("STA003", sev, "confirmed", "%s(%s)" % (idx.name, idx.keys[0].col),
                                "%s hashes %s.%s; its most common value holds %.2f%% of rows, "
                                "~%s rows on one hash code%s." % (
                                    idx.name, tname, idx.keys[0].col, 100 * top, _fmt(n_top),
                                    ", which can never be split across tablets"
                                    if sev != "low" else ""),
                                table=tname, index=idx.name,
                                fix=_key_fix(idx, tname) or
                                "Confirm the dominant value is expected; if it keeps growing, "
                                "make the hash group composite with a second column the "
                                "pattern binds."))

    # --- workload hygiene: how the application uses the database ------------------------
    for p in patterns:
        r = p["pss"]
        if not r or not r["calls"]:
            continue
        calls, rows = r["calls"], r["rows"] or 0.0
        shapes = sqlshape.flatten(p["shape"])
        top = p["shape"]
        # DML that mostly matches nothing.
        if p["kind"] in ("update", "delete") and calls >= THRESHOLDS["zero_rows_calls"] and \
                rows / calls <= THRESHOLDS["dml_match_rate"]:
            col.add(Finding("WRK009", shift("medium", WEIGHT_SHIFT[p["weight"]]), "confirmed",
                            p["id"], "%s (%s on %s) changed %s rows over %s calls: %s of calls "
                            "match no row." % (
                                p["id"], p["kind"].upper(), ", ".join(p["tables"]), _fmt(rows),
                                _fmt(calls), "%.0f%%" % (100 * (1 - rows / calls))),
                            [p["id"]], table=p["tables"][0] if p["tables"] else None))
        # UPDATE that rewrites key or creation-time columns.
        if p["kind"] == "update" and top.tables:
            t = top.tables[0]
            bound = {x.col for x in top.preds_for(t) if x.op == "eq" and x.col}
            pinned = sorted(c for c in top.set_cols if c in bound)
            created = sorted(c for c in top.set_cols if CREATION_COL.match(c) and c not in bound)
            if pinned or created:
                why = []
                if pinned:
                    why.append("%s, which its WHERE clause already pins" % ", ".join(pinned))
                if created:
                    why.append("%s, which should not change after insert" % ", ".join(created))
                col.add(Finding("WRK010", shift("low", WEIGHT_SHIFT[p["weight"]]), "confirmed",
                                p["id"], "%s on %s sets %s." % (p["id"], t, "; and ".join(why)),
                                [p["id"]], table=t))
        # Full-table aggregates (monitoring counts).
        for sh in shapes:
            if not sh.aggregate or not sh.tables:
                continue
            t = sh.tables[0]
            rows_t = table_rows(bundle, sch, t)
            if not [x for x in sh.preds_for(t) if x.op != "join"] and rows_t and \
                    rows_t >= THRESHOLDS["small_rows"]:
                col.add(Finding("WRK008", "low", "confirmed", "%s %s" % (p["id"], t),
                                "%s aggregates all of %s (~%s rows) with no filter: %s calls, "
                                "mean %s ms." % (p["id"], t, _fmt(rows_t), _fmt(calls),
                                                 _fmt(r["mean_ms"])), [p["id"]], table=t))
                break
        # Fan-out: rows per call far above what the column's cardinality predicts.
        if p["kind"] == "select" and not top.copy and not any(x.aggregate for x in shapes) and \
                rows / calls >= THRESHOLDS["fanout_rows"] and top.tables:
            t = top.tables[0]
            eqs = [x for x in top.preds_for(t) if x.op == "eq" and x.col]
            expected = None
            if len(eqs) == 1:
                st = col_stats(bundle, sch, t, eqs[0].col)
                rows_t = table_rows(bundle, sch, t)
                nd = n_distinct_abs(st, rows_t) if st else None
                if nd and rows_t:
                    expected = rows_t / nd
            col.add(Finding("WRK007", shift("medium", WEIGHT_SHIFT[p["weight"]]), "confirmed",
                            p["id"], "%s returns %s rows per call from %s%s." % (
                                p["id"], _fmt(rows / calls), t,
                                ("; the column's cardinality predicts ~%s, so calls concentrate "
                                 "on the heaviest values" % _fmt(expected))
                                if expected and rows / calls >= THRESHOLDS["fanout_factor"] *
                                expected else
                                ("; ~%s expected from cardinality" % _fmt(expected))
                                if expected else ""),
                            [p["id"]], table=t))
    if bundle.stats and any(st["mcf"] and not st["mcv"] for st in bundle.stats.values()):
        open_items.append("pg_stats has most_common_freqs without most_common_vals: placeholder "
                          "values can be detected (STA007) but not named. collect.sql exports "
                          "the values in full.")

    # --- primary key never read while a unique secondary carries every lookup -----------------
    for tname in sorted(sch.tables):
        t = sch.tables[tname]
        if not t.pk or t.partition_by:
            continue
        reads = [(p, a) for p in patterns for a in p.get("access", [])
                 if a["table"] == tname and p["kind"] == "select"]
        if not reads or any(a["path"] == t.pk.name for _, a in reads):
            continue
        paths = {a["path"] for _, a in reads}
        if len(paths) != 1:
            continue
        idx = sch.indexes.get(paths.pop())
        if idx is None or not idx.unique or idx.where or not all(k.col for k in idx.keys):
            continue
        if not all(t.cols.get(k.col, {}).get("notnull") for k in idx.keys):
            continue  # a primary key needs NOT NULL columns
        if all(a["covering"] for _, a in reads):
            continue
        old = ", ".join(k.label for k in t.pk.keys)
        newkey = idx.signature(quote=True)
        col.add(Finding("CAP051", "medium", "probable", tname,
                        "No ranked read of %s uses its primary key (%s); every one (%s) goes "
                        "through the unique index %s and then fetches the row." % (
                            tname, old, ", ".join(p["id"] for p, _ in reads), idx.name),
                        [p["id"] for p, _ in reads], table=tname, index=idx.name,
                        fix="Make %s's key (%s) the primary key and keep the old key unique as "
                            "a secondary index; lookups become single primary-key reads." % (
                                idx.name, newkey),
                        ddl=_rekey_comment(tname, newkey, sch.is_colocated(tname),
                                           "Create, copy, swap", sch.fks_referencing(tname)) +
                            "\nCREATE UNIQUE INDEX CONCURRENTLY %s ON %s (%s);  -- after the "
                            "swap: the old key stays unique\n%s" % (
                                schema_mod.qi(schema_mod.ident(schema_mod.bare(tname) +
                                                               "_old_pk")),
                                schema_mod.qn(tname), schema_mod.key_group(
                                    [k.sql for k in t.pk.keys], sch.is_colocated(tname), None),
                                _old_index_note(idx.name))))

    # --- ON CONFLICT targets ---------------------------------------------------------
    from . import safety as _saf
    uniq = _saf.constraints(sch)
    for p in patterns:
        for sh in sqlshape.flatten(p["shape"]):
            if not _saf.conflict_target(sh) or sh.tables[0] not in sch.tables or \
                    _saf.has_arbiter(sch, sh):
                continue
            table = sh.tables[0]
            if sh.conflict_constraint:
                t = sch.tables[table]
                names = sorted(([t.pk.name] if t.pk else []) + [
                    i.name for i in sch.indexes.values()
                    if i.table == table and i.unique and getattr(i, "constraint", False)])
                col.add(Finding("CAP060", "high", "confirmed", "%s ON CONFLICT ON CONSTRAINT %s"
                                % (table, sh.conflict_constraint),
                                "%s: INSERT ... ON CONFLICT ON CONSTRAINT %s on %s, but %s has "
                                "no constraint of that name (constraints: %s); a unique index "
                                "that backs no constraint cannot be named there." % (
                                    p["id"], sh.conflict_constraint, table, table,
                                    ", ".join(names) or "none"),
                                [p["id"]], table=table,
                                fix="Name one of %s's constraints, or target the columns with "
                                    "ON CONFLICT (columns)." % table))
                continue
            target = frozenset(sh.conflict_cols)
            near = sorted("%s (%s)" % (n, ", ".join(sorted(k))) for t, k, w, n in uniq
                          if t == table and k & target)
            partial = [n for t, k, w, n in uniq if t == table and k == target and w is not None]
            if partial:
                # A partial unique index on exactly these columns is the arbiter only when the
                # statement repeats its predicate.
                idx = sch.indexes[partial[0]]
                pred = idx.where_sql or idx.where
                col.add(Finding("CAP060", "high", "confirmed", "%s ON CONFLICT (%s)" % (
                                    table, ", ".join(sh.conflict_cols)),
                                "%s: INSERT ... ON CONFLICT (%s) on %s; the unique index on "
                                "exactly those columns, %s, is partial (WHERE %s) and the "
                                "statement does not repeat its predicate." % (
                                    p["id"], ", ".join(sh.conflict_cols), table, idx.name, pred),
                                [p["id"]], table=table,
                                fix="Repeat the index predicate in the statement: ON CONFLICT "
                                    "(%s) WHERE %s." % (", ".join(sh.conflict_cols), pred)))
                continue
            fix60, ddl60 = _conflict_fix(sch, bundle, table, sh.conflict_cols, target, uniq)
            col.add(Finding("CAP060", "high", "confirmed", "%s ON CONFLICT (%s)" % (
                                table, ", ".join(sh.conflict_cols)),
                            "%s: INSERT ... ON CONFLICT (%s) on %s, but no unique index or "
                            "constraint has exactly those columns%s." % (
                                p["id"], ", ".join(sh.conflict_cols), table,
                                ("; nearest: " + "; ".join(near)) if near else ""),
                            [p["id"]], table=table, fix=fix60, ddl=ddl60))

    # --- workload shape -------------------------------------------------------------
    zero = [p for p in patterns if p["kind"] == "select" and not p["shape"].copy and p["calls"] and
            p["calls"] >= THRESHOLDS["zero_rows_calls"] and p["rows"] is not None and
            p["rows"] / p["calls"] <= THRESHOLDS["miss_rate_rows"] and
            not any(s.aggregate for s in sqlshape.flatten(p["shape"]))]
    for p in zero:
        col.add(Finding("WRK005", shift("medium", WEIGHT_SHIFT[p["weight"]]), "confirmed", p["id"],
                        "%s returned %s rows over %s calls (%s): %s." % (
                            p["id"], _fmt(p["rows"]), _fmt(p["calls"]), ", ".join(p["tables"]),
                            "no call found anything" if p["rows"] == 0 else
                            "at least %s%% of calls find nothing" % (
                                ("%.1f" % (math.floor(1000 * (1 - p["rows"] / p["calls"])) / 10)
                                 ).rstrip("0").rstrip("."))), [p["id"]],
                        fix="Confirm with the application why this lookup always misses; cache "
                            "the negative result or skip the call."))
    planned = [(p, p["pss"].get("total_plan_time")) for p in patterns
               if p["pss"] and p["pss"].get("total_plan_time") is not None and p["total_ms"]]
    heavy = [(p, pt) for p, pt in planned
             if pt / (pt + p["total_ms"]) >= THRESHOLDS["plan_share"] and
             p["weight"] in ("HOT", "WARM") and p["kind"] in ("select", "update", "delete")]
    if heavy:
        tot_plan = math.fsum(pt for _, pt in planned)
        tot_exec = math.fsum(p["total_ms"] for p, _ in planned)
        col.add(Finding("WRK006", "medium", "confirmed", "planning time",
                        "Planning is %.0f%% of plan+execution time across ranked patterns; >= %.0f%% "
                        "on HOT/WARM patterns %s." % (
                            100 * tot_plan / (tot_plan + tot_exec), 100 * THRESHOLDS["plan_share"],
                            ", ".join(p["id"] for p, _ in heavy[:12]) +
                            (" ..." if len(heavy) > 12 else "")),
                        [p["id"] for p, _ in heavy],
                        fix="Use protocol-level prepared statements with plan caching in the "
                            "driver/ORM (for ActiveRecord: prepared_statements enabled), and check "
                            "pg_stat_statements plans vs calls after the change."))

    # --- primary key not serving its table's lookups ---------------------------------------
    for tname in sorted(sch.tables):
        t = sch.tables[tname]
        if not t.pk or t.partition_by:
            continue
        reads = [(p, a) for p in patterns for a in p.get("access", [])
                 if a["table"] == tname and p["kind"] == "select" and
                 p["weight"] in ("HOT", "WARM", "UNRANKED")]
        if len(reads) == 0:
            continue
        paths = {a["path"] for _, a in reads}
        if len(paths) != 1:
            continue
        path = paths.pop()
        idx = sch.indexes.get(path)
        if idx is None or idx.is_pk or idx.unique or not idx.keys or not idx.keys[0].col:
            continue  # a unique index enforces a constraint and stays regardless
        if any(a["covering"] for _, a in reads):
            continue
        lead = idx.keys[0].col
        if any(k.col == lead for k in t.pk.keys):
            continue
        order_cols = []
        for p, _ in reads:
            for tb, c, d in p["shape"].order:
                if tb == tname and c not in order_cols and c != lead:
                    order_cols.append(c)
        coloc = sch.is_colocated(tname)
        newkey = schema_mod.key_group([schema_mod.qi(c) for c in [lead] + order_cols + [
            k.col for k in t.pk.keys if k.col and k.col not in order_cols]], coloc)
        col.add(Finding("CAP050", "medium", "probable", tname,
                        "Every ranked read of %s (%s) looks it up by %s through %s and fetches "
                        "the row from the base table, while the primary key is (%s)." % (
                            tname, ", ".join(p["id"] for p, _ in reads), lead, path,
                            ", ".join(k.label for k in t.pk.keys)),
                        [p["id"] for p, _ in reads], table=tname, index=path,
                        fix="Re-key the table as PRIMARY KEY (%s): the same rows stay unique, "
                            "lookups by %s become primary-key reads in key order, and %s can be "
                            "dropped." % (newkey, lead, path),
                        ddl=_rekey_comment(tname, newkey, coloc, "Create, copy, swap",
                                           sch.fks_referencing(tname)) +
                            "\n" + _old_index_note(path)))

    # --- tablet splits ---------------------------------------------------------------
    if bundle.tablets:
        for tname in sorted(sch.tables):
            t = sch.tables[tname]
            if sch.is_colocated(tname) or t.partition_by:
                continue
            rels = [(tname, table_rows(bundle, sch, tname))]
            rels += [(i.name, bundle.reltuples.get(i.name, rels[0][1]))
                     for i in sch.indexes_on(tname, own_only=True) if not i.is_pk]
            single = [(n, r) for n, r in rels if r is not None and r >= THRESHOLDS["split_rows"]
                      and bundle.tablets.get(n, 0) == 1]
            if single:
                ww = write_weight.get(tname)
                local = not bundle.tablets_cluster_wide
                col.add(Finding("SPL001", "medium" if ww == "HOT" else "low",
                                "probable" if local else "confirmed", tname,
                                "Still on one tablet at >= %s rows: %s.%s" % (
                                    _fmt(THRESHOLDS["split_rows"]),
                                    ", ".join("%s (~%s rows)" % (n, _fmt(r)) for n, r in single),
                                    " (tablet count from yb_local_tablets, which lists only the "
                                    "tablets on one node; re-collect with collect.sql for "
                                    "cluster-wide counts)" if local else ""),
                                table=tname))
    if bundle.pss:
        since = bundle.meta.get("postmaster_start")
        open_items.append("pg_stat_statements and pg_stat_user_indexes cover activity since the "
                          "last stats reset%s; confirm the window includes batch and periodic jobs, "
                          "or reset with pg_stat_statements_reset() and recapture over a defined "
                          "window (for example a load test)."
                          % ((" (server start %s)" % since) if since else ""))

    # --- configuration ----------------------------------------------------------------
    if ps["cbo"] is False:
        col.add(Finding("CFG001", "medium", "confirmed", "planner",
                        "Planner settings: %s." % ps["cbo_setting"]))
    range_default = (not ps["hash_default"]) or sch.db_colocated
    if range_default:
        affected = []
        for tname in sorted(sch.tables):
            t = sch.tables[tname]
            if sch.is_colocated(tname):
                continue
            for idx in sch.indexes_on(tname):
                if idx.keys and not idx.keys[0].explicit:
                    affected.append(idx.name)
        if affected:
            col.add(Finding("CFG002", "medium", "confirmed", "default sharding",
                            "Unannotated first key columns resolve to ASC here (%s) on "
                            "non-colocated relations: %s." % (
                                "colocated database" if sch.db_colocated else
                                "yb_use_hash_splitting_by_default=off", ", ".join(affected))))
    pks = [t.pk for t in sch.tables.values() if t.pk and not t.partition_of]
    unannotated = sorted(pk.table for pk in pks if not any(k.explicit for k in pk.keys))
    plain_dump = not re.search(r"\bSPLIT\s+(INTO|AT)\b", bundle.ddl, re.I)
    if unannotated and plain_dump:
        col.add(Finding("CFG004", "high", "confirmed", "primary key sharding",
                        "%d of %d primary keys carry no HASH/ASC annotation and the dump has no "
                        "SPLIT clauses, so it was not taken with --include-yb-metadata. Their "
                        "sharding is inferred (%s for the first column here), not observed: %s." % (
                            len(unannotated), len(pks),
                            "HASH" if ps["hash_default"] and not sch.db_colocated else "ASC",
                            ", ".join(unannotated[:15]) + (" ..." if len(unannotated) > 15 else "")),
                        fix="Re-capture with ysql_dump --schema-only --include-yb-metadata and "
                            "annotate every primary key explicitly (HASH / ASC / DESC) in the "
                            "migrations so the intent survives any default change."))
    if bundle.pss and all(r.get("docdb_rows_scanned") in (None, 0.0) for r in bundle.pss):
        col.add(Finding("CFG003", "info", "confirmed", "pg_stat_statements",
                        "No docdb_* statement statistics present."))

    # --- linter -----------------------------------------------------------------------
    lint_rows = []
    try:
        lint = _load_linter()
        lf = lint.lint(bundle.ddl)
        superseded = {"YB021", "YB026"}
        # yb-lint reads PKs only inside CREATE TABLE; dumps add them with ALTER TABLE.
        if any(t.pk for t in sch.tables.values()):
            superseded.add("YB020")
        # A dump without yb metadata has no SPLIT clauses at all; CFG004 covers that once.
        if not re.search(r"\bSPLIT\s+(INTO|AT)\b", bundle.ddl, re.I):
            superseded.add("YB012")
        if bundle.stats:
            superseded.add("YB022")
        if sch.db_colocated or not ps["hash_default"]:
            superseded |= {"YB022"}
        for f in lf:
            row = {"rule": f.rule, "severity": f.severity, "line": f.line,
                   "message": f.message, "fix": f.fix,
                   "superseded": f.rule in superseded}
            lint_rows.append(row)
            if row["superseded"]:
                continue
            if f.rule == "YB020":
                tm = re.match(r"^(\w+):", f.message)
                if tm and sch.tables.get(tm.group(1)) and sch.tables[tm.group(1)].pk:
                    row["superseded"] = True
                    continue
            sev = {"error": "high", "warn": "medium", "info": "info"}[f.severity]
            col.add(Finding("LINT-" + f.rule, sev, "confirmed", "line %d" % f.line, f.message,
                            fix=f.fix))
        lint_ok = True
    except Exception as e:  # the linter is a separate script; report, never hide
        open_items.append("yb-lint.py failed: %s" % e)
        lint_ok = False

    # --- replay reconciliation ------------------------------------------------------
    replay_summary = None
    if plans:
        from . import plans as plans_mod
        replay_summary = plans_mod.reconcile(plans, patterns, col, sch, rules, THRESHOLDS,
                                             recon, WEIGHT_SHIFT, shift, Finding)

    if plans:
        from . import probes as probes_mod
        probes_mod.apply(col, plans, recon)

    # --- measurements corroborate access-path findings ---------------------------------
    for key in sorted(k for k in col.items if k[0] in ("WRK001", "WRK002")):
        w = col.items[key]
        hosts = [f for f in col.items.values() if (f.rule.startswith("CAP") or
                 f.rule.startswith("PLN")) and set(w.patterns) & set(f.patterns)]
        if hosts:
            for h in hosts:
                h.measured = ((h.measured + " ") if h.measured else "") + w.fact
                if h.confidence == "probable":
                    h.confidence = "confirmed (measured)"
                elif "replay" in h.confidence and "measured" not in h.confidence:
                    h.confidence = "confirmed (replay, measured)"
                if SEV.index(w.severity) < SEV.index(h.severity):
                    h.severity = w.severity
            del col.items[key]

    # --- sound decisions ------------------------------------------------------------
    flagged = {f.index for f in col.items.values() if f.index}
    for pat in patterns:
        if pat["weight"] not in ("HOT", "UNRANKED"):
            continue
        for a in pat["access"]:
            if a["path"] in flagged or any(pat["id"] in f.patterns and f.table == a["table"]
                                           for f in col.items.values()):
                continue
            if a["point"]:
                sound.append("%s: point lookup on %s via %s." % (pat["id"], a["table"], a["path"]))
            elif a["path"] and a["covering"] and a["order_ok"] in (True, None) and \
                    not a["skip_scan"] and a["path"] in sch.indexes:
                sound.append("%s: %s is served by %s without a base-table fetch%s." % (
                    pat["id"], a["table"], a["path"],
                    " and in ORDER BY order" if a["order_ok"] else ""))

    # Covering advice is moot for an index that should go (boolean / low-cardinality) or that a
    # primary-key re-key replaces.
    doomed = {f.index for f in col.items.values()
              if f.rule in ("STA002", "STA008", "CAP050", "CAP051") and f.index}
    for k in [k for k, f in col.items.items() if f.rule == "CAP020" and f.index in doomed]:
        del col.items[k]
    _fold_partition_copies(col, sch)
    # An index whose defect would be fixed by a rebuild, but which no ranked pattern uses and
    # pg_stat_user_indexes does not show scanned, is cheaper to drop than to rebuild. A
    # partitioned index is used when any partition's copy of it is (queries hit the copies).
    dropped = {}
    ranked = sorted(col.items.values(), key=lambda f: (SEV.index(f.severity), f.rule, f.obj))
    for f in ranked:
        if f.rule not in ("STA001", "STA002", "STA003", "STA004") or not f.index:
            continue
        if f.index in dropped:
            f.ddl = None
            f.fix = "Dropping %s (see the %s finding) resolves this too." % (f.index,
                                                                          dropped[f.index])
            continue
        idx = sch.indexes.get(f.index)
        names = [f.index] + partition_copies(sch, f.index)
        if idx is None or idx.unique or idx.is_pk or any(n in chosen_by for n in names):
            continue
        if not bundle.pss:
            continue
        u = bundle.index_usage.get(f.index)
        if any((bundle.index_usage.get(n) or {}).get("idx_scan", 0) > 0 for n in names):
            continue
        measured = u is not None and _usage_complete(bundle)
        f.ddl = (schema_mod.drop_index_sql(f.index) if measured else _check_then_drop(f.index))
        dropped[f.index] = f.rule
        f.fix = ("No ranked pattern uses %s%s, so drop it rather than rebuild it. Rebuild "
                 "only if a reader outside the captured workload needs it: %s" % (
                     f.index, " and idx_scan is 0 on every node" if measured else
                     " (idx_scan not captured on every node: check it first)", f.fix or ""))
    # A finding that drops an index outright settles every other finding on that index: their
    # rebuilds would recreate what is being dropped. Every bare DROP starts with an idx_scan check
    # unless pg_stat_user_indexes showed 0 scans.
    ranked = sorted(col.items.values(), key=lambda f: (SEV.index(f.severity), f.rule, f.obj))
    drops = {}
    for f in ranked:
        if f.index and f.ddl and _pure_drop(f.ddl, f.index) and f.index not in drops:
            drops[f.index] = f
    for f in ranked:
        d = drops.get(f.index)
        if d is not None and d is not f and f.ddl:
            f.ddl = None
            f.fix = "Dropping %s (see the %s finding) resolves this too." % (f.index, d.rule)
    for iname, f in sorted(drops.items()):
        u = bundle.index_usage.get(iname)
        if (u is None or u["idx_scan"] > 0 or not _usage_complete(bundle)) and \
                "idx_scan" not in f.ddl:
            f.ddl = _check_then_drop(iname, f.ddl)
    from . import safety as safety_mod
    import sys as _sys
    safety_report = safety_mod.check(sch, patterns, col, _sys.modules[__name__], ps, bundle,
                                     Finding)
    flagged_all = {f.index for f in col.items.values() if f.index}
    sound = [x for x in sound if x.split(":")[0] not in flagged_all]
    findings = col.sorted()
    listed_only = set(listed.get("added", [])) if bundle.pss else set()
    for f in findings:
        for caveat, scope in pf["weakened"].get(f.rule, []):
            if scope == "inferred_keys" and not _key_inferred(sch, f):
                continue
            if caveat not in f.caveats:
                f.caveats.append(caveat)
        if f.patterns and set(f.patterns) <= listed_only and LISTED_UNRANKED not in f.caveats:
            f.caveats.append(LISTED_UNRANKED)
    for n, f in enumerate(findings, 1):
        f.fid = "F%d" % n
    basis_of = {f.rule: _basis_inputs(f.rule, rules, bundle) for f in findings}
    out = {
        "tool": "yb-model analyze",
        "inputs": bundle.present,
        "version": bundle.version,
        "version_source": bundle.version_source,
        "settings": ps,
        "thresholds": THRESHOLDS,
        "lint_ran": lint_ok,
        "schema": sch.to_dict(),
        "patterns": [_pat_out(p) for p in patterns],
        "findings": [dict(id=f.fid, basis_inputs=basis_of[f.rule],
                          **f.to_dict(rules, bundle.version, ps["release"], ps["unavailable"]))
                     for f in findings],
        "sound": sorted(set(sound)),
        "open_items": sorted(set(open_items)),
        "lint": lint_rows,
        "replay": replay_summary,
        "probe_rules": sorted(probes_all()),
        "reconciliation": recon,
        "safety": safety_report,
        "preflight": dict(preflight_mod.summary(pf),
                          muted={r: {"inputs": ids,
                                     "withheld": sorted(col.withheld.get(r, ()))}
                                 for r, ids in sorted(pf["muted"].items())},
                          stages=[{"stage": s, "input": i} for s, i in pf["stages"]]),
    }
    return out


def probes_all():
    from . import probes
    return probes.rules_with_probes()


def _fold_partition_copies(col, sch):
    """An index the server created on a partition as a copy of a partitioned index (the
    catalog's pg_inherits: Index.parent) changes only through that parent; YSQL refuses DROP
    INDEX on it ("cannot drop index ... because index ... requires it"). A finding on a copy
    joins the same rule's finding on the parent, which rebuilds every copy; with none there it
    keeps its fact, points at the parent and carries no DDL."""
    idx = dict(sch.indexes)
    for t in sch.tables.values():
        if t.pk:
            idx[t.pk.name] = t.pk
    parent_of = {n: getattr(i, "parent", None) for n, i in idx.items()
                 if getattr(i, "parent", None)}
    if not parent_of:
        return
    for n in list(parent_of):  # sub-partitions: fold into the top-level index
        seen = {n}
        while parent_of[n] in parent_of and parent_of[n] not in seen:
            seen.add(parent_of[n])
            parent_of[n] = parent_of[parent_of[n]]
    host = {(f.rule, f.index): f for f in col.items.values() if f.index}
    copies = {}
    for key, f in list(col.items.items()):
        parent = parent_of.get(f.index)
        if not parent:
            continue
        h = host.get((f.rule, parent))
        if h is None:
            f.ddl = None
            f.fix = ("%s is the copy of partitioned index %s on partition %s; YSQL changes it "
                     "only through %s, which rebuilds every partition's copy. %s" % (
                         f.index, parent, f.table, parent, f.fix or "")).strip()
            continue
        copies.setdefault(id(h), (h, []))[1].append(f.index)
        for p in f.patterns:
            if p not in h.patterns:
                h.patterns.append(p)
        if SEV.index(f.severity) < SEV.index(h.severity):
            h.severity = f.severity
        del col.items[key]
    for h, names in copies.values():
        h.fact += (" The partitions' copies of %s (%s) show the same; rebuilding %s rebuilds "
                   "them." % (h.index, ", ".join(sorted(names)), h.index))


def _pure_drop(ddl, index):
    """DDL that only drops the index (optionally after the idx_scan check), as opposed to a
    rebuild or a re-key that drops it because something replaces it."""
    lines = [l.strip() for l in ddl.splitlines() if l.strip()]
    return bool(lines) and index in ddl and all(
        re.match(r"(?i)DROP\s+INDEX\b", l) or re.match(r"(?i)ALTER\s+TABLE\s+.*\bDROP\s+"
                                                        r"CONSTRAINT\b", l) or
        l.startswith("-- first") and "idx_scan" in l for l in lines)


# What each rule family reads, for its "Basis" line; a rule in rules.json may say "uses".
BASIS_USES = [("LINT-", ["schema"]), ("SAF", ["recommended", "schema"]),
              ("STA", ["schema", "pg_stats", "reltuples"]), ("IDX", ["schema", "workload"]),
              ("WRK003", ["schema", "index_usage"]), ("WRK", ["pss", "table_usage"]),
              ("SPL", ["schema", "reltuples", "tablets"]), ("CFG004", ["schema"]),
              ("CFG005", ["schema", "pg_stats"]), ("CFG", ["settings"]),
              ("PRT", ["schema", "capture_date"])]
INPUT_NAMES = {"schema": "the schema DDL", "pg_stats": "pg_stats", "reltuples": "row counts",
               "pss": "pg_stat_statements", "queries": "the query list",
               "index_usage": "index usage counters", "table_usage": "table write counters",
               "settings": "pg_settings", "tablets": "tablet counts",
               "capture_date": "the capture date", "recommended": "the recommended DDL"}


def _basis_inputs(rule, rules, bundle):
    """The inputs a rule read that this bundle has, by name, for the report's Basis line."""
    uses = rules.get(rule, {}).get("uses") or next(
        (u for pfx, u in BASIS_USES if rule.startswith(pfx)), ["schema"])
    have = {"schema": True, "pg_stats": bool(bundle.stats), "reltuples": bool(bundle.reltuples),
            "pss": bool(bundle.pss), "queries": bool((bundle.queries_sql or "").strip()),
            "index_usage": bool(bundle.index_usage), "table_usage": bool(bundle.table_usage),
            "settings": bool(bundle.settings), "tablets": bool(bundle.tablets),
            "capture_date": bool(bundle.meta.get("captured_at")), "recommended": True}
    out = []
    for u in uses:
        for x in (("pss", "queries") if u == "workload" else (u,)):
            if have.get(x) and INPUT_NAMES[x] not in out:
                out.append(INPUT_NAMES[x])
    return out


def _conflict_fix(sch, bundle, table, cols, target, uniq):
    """(fix, ddl) for an ON CONFLICT target that no unique index matches exactly (the planner
    needs an exact column-set match). A unique index on a subset already makes the target
    unique, so the statement should name that subset; otherwise a unique index on the target,
    hashed on a NOT NULL, high-cardinality column, because NULLs never conflict."""
    subset = sorted((len(k), n, k) for t, k, w, n in uniq if t == table and w is None and
                    k < target)
    if subset:
        _, name, k = subset[0]
        return ("Change the statement to ON CONFLICT (%s). %s already makes (%s) unique, so "
                "(%s) is unique too; the statement then takes the same conflicts, plus inserts "
                "that today fail on %s with a unique violation." % (
                    ", ".join(sorted(k)), name, ", ".join(sorted(k)), ", ".join(cols), name),
                None)
    t = sch.tables[table]
    rows = table_rows(bundle, sch, table)

    def score(c):
        st = col_stats(bundle, sch, table, c)
        nd = n_distinct_abs(st, rows) if st else None
        return (not t.cols.get(c, {}).get("notnull", False), -(nd or 0), cols.index(c))
    order = sorted(cols, key=score)
    nullable = [c for c in cols if not t.cols.get(c, {}).get("notnull", False)]
    name = schema_mod.ident("%s_%s_uniq" % (schema_mod.bare(table), "_".join(cols)))
    coloc = sch.is_colocated(table)
    ddl = "CREATE UNIQUE INDEX CONCURRENTLY %s ON %s (%s);" % (
        schema_mod.qi(name), schema_mod.qn(table),
        schema_mod.key_group([schema_mod.qi(c) for c in order], coloc))
    fix = ("Create a unique index on exactly (%s)%s, or change the target to the columns of an "
           "existing unique index." % (
               ", ".join(cols), " (range keys: the table is colocated)" if coloc else
               ", hashed on %s so rows spread over tablets" % order[0]))
    if nullable:
        fix += (" %s may be NULL, and NULLs never conflict, so a row with NULL there is "
                "inserted again instead of updated: declare %s NOT NULL first." % (
                    ", ".join(nullable), "it" if len(nullable) == 1 else "them"))
    return fix, ddl


def _key_fix(idx, tname):
    """Fix for a skewed or low-cardinality hash key that is also a guarantee: a primary key or
    unique index is never dropped, only rebuilt on the same columns."""
    cols = ", ".join(k.label for k in idx.keys)
    if idx.is_pk:
        return ("%s is the primary key of %s, so it cannot be dropped. A different hash group "
                "means rebuilding the table with a new primary key on the same columns (%s), which "
                "keeps them unique; consider it only if the hot patterns also bind a "
                "high-cardinality column that can join the hash group." % (idx.name, tname, cols))
    if idx.unique:
        return ("%s enforces uniqueness on (%s), so do not drop it. To change its hash group, "
                "create a UNIQUE index on the same columns with the new hash group, then drop this "
                "one; worth it only if the hot patterns bind the added column." % (idx.name, cols))
    return None


_INT_DOMAIN = {"bigint": 2 ** 63, "int8": 2 ** 63, "integer": 2 ** 31, "int": 2 ** 31,
               "int4": 2 ** 31, "smallint": 2 ** 15, "int2": 2 ** 15}


def _random_ids(st, typ):
    """Unique integers spread evenly over most of their type's positive range: random
    identifiers, which arrive anywhere in the key space rather than at one end."""
    dom = _INT_DOMAIN.get((typ or "").split("(")[0].strip())
    if not dom or not st or (st.get("n_distinct") or 0) > -0.9:
        return False
    from .inputs import parse_pg_array
    try:
        vals = sorted(float(x) for x in (parse_pg_array(st.get("hist")) or []))
    except ValueError:
        return False
    if len(vals) < 10 or vals[-1] - vals[0] < 0.5 * dom:
        return False
    n, span = len(vals) - 1, vals[-1] - vals[0]
    return max(abs((v - vals[0]) / span - i / n) for i, v in enumerate(vals)) <= 0.1


def _insert_ordered_hint(t, col):
    """Why a column's values would arrive in increasing order, or None."""
    c = t.cols.get(col, {})
    typ = c.get("type", "")
    if c.get("sequence"):
        return "it takes its value from a sequence"
    if re.search(r"timestamp|date", typ):
        return "a %s column" % typ
    if "uuid" not in typ and (MONOTONIC_NAME.match(col) or CREATION_COL.match(col)):
        return "its name suggests creation order"
    return None


def _replace_ddl(idx, suffix, tname, t, colocated, **change):
    """Create the derived replacement, then drop the original (YSQL rejects DROP INDEX
    CONCURRENTLY)."""
    new = schema_mod.ident(idx.name + suffix)
    return schema_mod.index_sql(idx, new, tname, colocated=colocated,
                                partitioned=bool(t.partition_by), **change) + "\n" + \
        schema_mod.drop_sql(idx, new)


def _bucket_ddl(idx, tname, t, col, buckets=16):
    """A bucketed replacement: the bucket leads, every original key column follows, and each
    bucket starts its own tablet. UNIQUE, INCLUDE and the predicate are carried over."""
    return _replace_ddl(idx, "_bkt", tname, t, False,
                        keys="(yb_hash_code(%s) %% %d) ASC, %s" % (
                            schema_mod.qi(col), buckets, idx.signature(quote=True)),
                        split="SPLIT AT VALUES (%s)" % ", ".join("(%d)" % b
                                                                 for b in range(1, buckets)))


def _few_values(t, col, st, rows, idx):
    """A column that cannot be insertion-ordered in a meaningful way: a boolean, one with fewer
    than 100 distinct values, or one a partial index pins with its WHERE clause."""
    if t.cols.get(col, {}).get("type", "") in ("boolean", "bool"):
        return True
    if idx.where and re.search(r"\b%s\s*=" % re.escape(col), idx.where):
        return True
    nd = n_distinct_abs(st, rows) if st else None
    return nd is not None and nd < THRESHOLDS["min_ordered_nd"]


def _key_inferred(sch, f):
    """True when the finding's index (or, without one, its table's primary key) leads with a
    column whose sharding the DDL does not state."""
    idx = sch.indexes.get(f.index) if f.index else None
    if idx is None and f.table in sch.tables:
        idx = sch.tables[f.table].pk
    return bool(idx and idx.keys and not idx.keys[0].explicit)


def _pat_out(p):
    d = {k: v for k, v in p.items() if k not in ("shape", "pss")}
    d["shape"] = p["shape"].to_dict()
    if p["pss"]:
        d["docdb"] = {k: v for k, v in p["pss"].items() if k.startswith("docdb_")}
    return d


def _fmt(n):
    if n is None:
        return "?"
    n = float(n)
    for unit, div in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(n) >= div:
            return ("%.1f%s" % (n / div, unit)).replace(".0" + unit, unit)
    if n == int(n) or abs(n) >= 100:
        return "%d" % round(n)
    return "%.1f" % n
