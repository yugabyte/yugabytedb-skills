"""Measured pass: run the workload's reads on a small generated sample and report what DocDB did.

The rules predict; this pass measures. generate.py makes, for each table that single-table reads
touch, a sample shaped by the bundle's statistics, holding none of the customer's data (only
the constants the workload's statements bind, so each read finds its rows). Full-scale
statistics are injected after the load, so the planner chooses the plan it would choose on the
customer's table; the plan then runs on the sample with EXPLAIN (ANALYZE, DIST, DEBUG), its
parameters bound to the sample's values (by the replayed plan where it says which column each
one binds). The pass reports counters and their ratios per row returned, never times.

A read that seeks far more than it asks for, or scans far more rows than it returns, is an
anomaly. analyze.run reports each anomaly no finding on that pattern explains as MEAS001,
"unexplained, measured": a lead for a closer look, which ends in a rule or a pitfalls.md entry.
"""

import json
import re

from . import probes
from .sqltok import qi, qn

# A read needs about one seek per value it asks for (an IN list element, an OR arm); more than
# `seeks_over_asked` times that, past `seeks_floor`, means DocDB seeks for values the query did
# not name (a skip scan over a key gap, for one). Rows scanned are judged per row returned.
THRESHOLDS = {"seeks_over_asked": 4.0, "seeks_floor": 20, "scanned_per_row": 10.0,
              "scanned_floor": 100}
_INT_BOUNDS = {"smallint": 15, "int2": 15, "integer": 31, "int": 31, "int4": 31, "bigint": 63,
               "int8": 63}


def _quote(raw):
    return "'%s'" % raw.replace("'", "''")


def _array_item(raw):
    return '"%s"' % raw.replace("\\", "\\\\").replace('"', '\\"')


def unquote(lit):
    """A literal's text: 'it''s' -> it's, 42 -> 42."""
    return lit[1:-1].replace("''", "'") if lit.startswith("'") else lit


# What the generator needs from the scratch database, after the customer's DDL is loaded there:
# per table its columns (type oid and typmod for the value constructor, NOT NULL, identity,
# generated), CHECK expressions and, for a partition, its partition constraint (both as the
# server deparses them); unique column sets; foreign keys. User schemas only.
FACTS = r"""
WITH rel AS (
  SELECT c.oid, c.relname, n.nspname, c.relkind, c.relispartition
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg\_%'
     AND c.relkind IN ('r', 'p'))
SELECT json_build_object(
  'tables', (SELECT json_agg(json_build_object(
      'name', r.relname, 'schema', r.nspname, 'kind', r.relkind,
      'parent', (SELECT pc.relname FROM pg_inherits i JOIN pg_class pc ON pc.oid = i.inhparent
                  WHERE i.inhrelid = r.oid LIMIT 1),
      'partition_constraint', CASE WHEN r.relispartition
                                   THEN pg_get_partition_constraintdef(r.oid) END,
      'is_default', COALESCE((SELECT pg_get_expr(c.relpartbound, c.oid) = 'DEFAULT'
                                FROM pg_class c WHERE c.oid = r.oid), false),
      'partkey', (SELECT json_agg(a.attname ORDER BY k.ord) FROM pg_partitioned_table pt
                    CROSS JOIN unnest(pt.partattrs::int2[]) WITH ORDINALITY k(attnum, ord)
                    LEFT JOIN pg_attribute a ON a.attrelid = pt.partrelid AND a.attnum = k.attnum
                   WHERE pt.partrelid = r.oid),
      'checks', (SELECT json_agg(json_build_object('name', con.conname,
                   'expr', pg_get_expr(con.conbin, con.conrelid),
                   'cols', (SELECT json_agg(a.attname) FROM unnest(con.conkey) k(attnum)
                              JOIN pg_attribute a ON a.attrelid = con.conrelid
                               AND a.attnum = k.attnum)) ORDER BY con.conname)
                   FROM pg_constraint con WHERE con.conrelid = r.oid AND con.contype = 'c'),
      'columns', (SELECT json_agg(json_build_object(
          'name', a.attname, 'typoid', a.atttypid, 'typmod', a.atttypmod,
          'type', format_type(a.atttypid, a.atttypmod), 'notnull', a.attnotnull,
          'identity', a.attidentity <> '', 'generated', a.attgenerated <> '')
          ORDER BY a.attnum)
          FROM pg_attribute a WHERE a.attrelid = r.oid AND a.attnum > 0
           AND NOT a.attisdropped))
      ORDER BY r.nspname, r.relname) FROM rel r),
  'uniques', (SELECT json_agg(json_build_object(
      'table', t.relname, 'schema', n.nspname, 'primary', i.indisprimary,
      'cols', (SELECT json_agg(a.attname ORDER BY k.ord) FROM unnest(i.indkey)
                 WITH ORDINALITY k(attnum, ord) JOIN pg_attribute a
                 ON a.attrelid = i.indrelid AND a.attnum = k.attnum)) ORDER BY t.relname)
      FROM pg_index i JOIN pg_class t ON t.oid = i.indrelid
      JOIN pg_namespace n ON n.oid = t.relnamespace
     WHERE i.indisunique AND n.nspname NOT IN ('pg_catalog', 'information_schema')
       AND n.nspname NOT LIKE 'pg\_%' AND NOT EXISTS (SELECT 1 FROM unnest(i.indkey) k(a)
                                                     WHERE k.a = 0)),
  'fks', (SELECT json_agg(json_build_object(
      'name', con.conname, 'table', c.relname, 'schema', n.nspname, 'ref_table', rc.relname,
      'ref_schema', rn.nspname,
      'cols', (SELECT json_agg(a.attname ORDER BY k.ord) FROM unnest(con.conkey)
                 WITH ORDINALITY k(attnum, ord) JOIN pg_attribute a
                 ON a.attrelid = con.conrelid AND a.attnum = k.attnum),
      'ref_cols', (SELECT json_agg(a.attname ORDER BY k.ord) FROM unnest(con.confkey)
                 WITH ORDINALITY k(attnum, ord) JOIN pg_attribute a
                 ON a.attrelid = con.confrelid AND a.attnum = k.attnum))
      ORDER BY c.relname, con.conname)
      FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid
      JOIN pg_namespace n ON n.oid = c.relnamespace JOIN pg_class rc ON rc.oid = con.confrelid
      JOIN pg_namespace rn ON rn.oid = rc.relnamespace
     WHERE con.contype = 'f' AND con.conislocal
       AND n.nspname NOT IN ('pg_catalog', 'information_schema')));
"""


def read_facts(container, db):
    """The generator's facts (FACTS) from the scratch database, or None when the server
    returned none."""
    r = container.sql(FACTS, db=db, flags=("-tA",))
    txt = (r.stdout or "").strip()
    try:
        return json.loads(txt[txt.index("{"):]) if "{" in txt else None
    except ValueError:
        return None


def schema_constants(facts):
    """(schema, table) -> column -> constants from the table's CHECK expressions and, for a
    partition, its partition constraint, credited to the root table (rows load through it)."""
    from . import server
    out = {}
    tables = {(t["schema"], t["name"]): t for t in (facts or {}).get("tables") or []}
    def root(key):
        seen = set()
        while tables.get(key, {}).get("parent") and key not in seen:
            seen.add(key)
            key = (key[0], tables[key]["parent"])
        return key
    for key, t in sorted(tables.items()):
        exprs = [c["expr"] if isinstance(c, dict) else c for c in t.get("checks") or []] + (
            [t["partition_constraint"]] if t.get("partition_constraint") else [])
        for e in exprs:
            for c, vs in server.constants(e).items():
                lst = out.setdefault(root(key), {}).setdefault(c, [])
                for v in vs:
                    if v not in lst:
                        lst.append(v)
    return out


_LIT = r"('(?:[^']|'')*'|(?<![\w$])-?\d+(?:\.\d+)?)"  # not the digits of $3
_COL = r'(?:[A-Za-z_][\w$]*\.)?("?[A-Za-z_][\w$]*"?)'
_OPS = r"(=|<>|!=|>=|<=|>|<)"


def _unq(c):
    return c[1:-1].replace('""', '"') if c.startswith('"') else c.lower()


def literals(query):
    """column -> the literal values a query compares it with (equality, IN lists)."""
    out = {}
    q = re.sub(r"--[^\n]*", " ", query)
    for m in re.finditer(r"%s\s*=\s*%s" % (_COL, _LIT), q):
        out.setdefault(_unq(m.group(1)), []).append(m.group(2))
    for m in re.finditer(r"%s\s+IN\s*\(([^()]*)\)" % _COL, q, re.I):
        for lit in re.findall(_LIT, m.group(2)):
            out.setdefault(_unq(m.group(1)), []).append(lit)
    return out


def params(query):
    """$n -> (column, operator) for the parameters a query compares a column with, and the
    LIMIT / OFFSET parameters as ("limit", None)."""
    out = {}
    q = re.sub(r"'(?:[^']|'')*'", "''", query)
    for m in re.finditer(r"%s\s*%s\s*\$(\d+)" % (_COL, _OPS), q):
        out.setdefault(int(m.group(3)), (_unq(m.group(1)), m.group(2)))
    for m in re.finditer(r"\$(\d+)(?:::[\w ]+)?\s*%s\s*%s" % (_OPS, _COL), q):
        flip = {">": "<", "<": ">", ">=": "<=", "<=": ">="}
        out.setdefault(int(m.group(1)), (_unq(m.group(3)), flip.get(m.group(2), m.group(2))))
    for m in re.finditer(r"%s\s+IN\s*\(([^()]*)\)" % _COL, q, re.I):
        for k, item in enumerate(m.group(2).split(",")):  # position among all elements
            p = re.search(r"\$(\d+)", item)
            if p:
                out.setdefault(int(p.group(1)), (_unq(m.group(1)), "in:%d" % k))
    for m in re.finditer(r"%s\s*=\s*ANY\s*\(\s*\$(\d+)" % _COL, q, re.I):
        out.setdefault(int(m.group(2)), (_unq(m.group(1)), "any"))
    for m in re.finditer(r"\b(LIMIT|OFFSET)\s+\$(\d+)", q, re.I):
        out.setdefault(int(m.group(2)), (m.group(1).lower(), None))
    return out


def bind(query, plans_by_table, tables, server_params=None, types=None, neutrals=None):
    """Parameter values for EXECUTE: the sample's target value (its first) for equality, the
    next values for an IN list, for a range a lower bound a quarter and an upper bound three
    quarters into the values in the type's order (shape "sorted"), 100 for LIMIT. Where the
    replayed plan says which column a parameter binds (server_params: $n -> (col, op)), that
    answer is used; the text reading is the fallback. A parameter the server types as an array
    is bound as an array literal; one no condition places gets a neutral value of its type."""
    vals = {}
    mapping = {n: v for n, v in params(query).items()
               if not (server_params and n in server_params)}
    for n, (col, op) in (server_params or {}).items():
        mapping[n] = (col, op)
    for n, (col, op) in sorted(mapping.items()):
        if col in ("limit", "offset"):
            vals[n] = "100" if col == "limit" else "0"
            continue
        s = next((plans_by_table[t]["shape"][col] for t in tables
                  if t in plans_by_table and col in plans_by_table[t]["shape"]), None)
        if s is None or not s["domain"]:
            continue  # a function of a column, or no column: bound by type below
        if types and n <= len(types) and types[n - 1].endswith("[]") and \
                not s["type"].endswith("[]"):
            op = "any"  # an array of the column's values (an array column takes its own)
        dom = s["domain"]
        if op and op.startswith("in:"):
            vals[n] = _quote(dom[min(int(op[3:]), len(dom) - 1)])
        elif op == "any":  # an array literal of the element, coerced to the column's array
            vals[n] = _quote("{%s}" % _array_item(dom[0]))
        elif op in (">", ">="):  # bounds a quarter and three quarters into the values in
            # the type's order, so a read bounded on both sides still finds rows
            srt = s.get("sorted") or dom
            vals[n] = _quote(srt[len(srt) // 4])
        elif op in ("<", "<="):
            srt = s.get("sorted") or dom
            vals[n] = _quote(srt[min(len(srt) - 1, (3 * len(srt)) // 4)])
        else:
            vals[n] = _quote(dom[0])
    for n, t in enumerate(types or [], 1):
        if n not in vals:
            vals[n] = neutral(t, (mapping.get(n) or (None, None))[1], neutrals)
    return vals


def neutral(typ, op=None, neutrals=None):
    """A value of a type that leaves a predicate as wide as it can be: an integer type's bound
    for a range, otherwise the server's value 0 of the type (neutrals: type -> raw)."""
    bits = _INT_BOUNDS.get((typ or "").strip().lower())
    if bits is not None:
        low, high = -(2 ** bits), 2 ** bits - 1
        return "'%d'" % (low if op in (">", ">=") else high if op in ("<", "<=") else 0)
    raw = (neutrals or {}).get(typ)
    return "NULL" if raw is None else _quote(raw)


def asked(query):
    """How many seeks a read asks for by itself: one, plus one per IN-list element and per OR
    arm (each can need its own seek)."""
    q = re.sub(r"'(?:[^']|'')*'", "''", query)
    n = 1
    for m in re.finditer(r"\bIN\s*\(([^()]*)\)", q, re.I):
        n += len([x for x in m.group(1).split(",") if x.strip()])
    n += len(re.findall(r"\bOR\b", q, re.I))
    return n


def anomalies(m):
    """The thresholds a measurement crosses: seeks beyond what the read asks for, or rows
    scanned per row returned."""
    out = []
    ret, seeks, scanned = m.get("returned") or 0, m.get("seeks"), m.get("scanned") or 0
    th = THRESHOLDS
    if seeks is not None and seeks >= th["seeks_floor"] and \
            seeks > th["seeks_over_asked"] * m.get("asked", 1):
        out.append("seeks")
    if scanned >= th["scanned_floor"] and scanned > th["scanned_per_row"] * max(ret, 1):
        out.append("scanned")
    return out


def compare_params(query, server_params):
    """Where the text reading of a query's parameters disagrees with the replayed plan's:
    [{"param", "model", "server"}]."""
    out = []
    model = params(query)
    def norm(v):
        col, op = v
        op = {"eq": "=", None: "=", "any": "in", "ne": "<>", "!=": "<>"}.get(op, op)
        # (= ANY($n) is an IN list; the server reads <> and != alike)
        return (col, op.split(":")[0] if op else op)
    for n, sv in sorted((server_params or {}).items()):
        mv = model.get(n)
        if mv is None or norm(mv) != norm(sv):
            out.append({"param": n, "model": list(mv) if mv else None, "server": list(sv)})
    return out


def _drop_constraint(container, db, key, name):
    container.sql("ALTER TABLE %s DROP CONSTRAINT IF EXISTS %s;" % (
        qn(key[1] if key[0] == "public" else "%s.%s" % key), qi(name)), db=db)


def _errors(r):
    return [l.strip() for l in ((r.stdout or "") + (r.stderr or "")).splitlines()
            if "ERROR:" in l]


def _json(r):
    txt = (r.stdout or "").strip()
    try:
        return json.loads(txt[txt.index("{"):txt.rindex("}") + 1])
    except ValueError:
        return None


def _load(container, db, facts, key, plan, loaded_keys, cyclic, gaps):
    """C: load one table's sample through its own predicates. Returns (rows loaded, reason).
    A predicate the server cannot evaluate on the candidates, and a constraint the load still
    violates (the server names it), go one at a time, each recorded; then the load retries."""
    from . import generate
    table = plan["table"]
    container.sql("ALTER TABLE %s DISABLE TRIGGER USER;" % qn(table), db=db)
    # A foreign key to the table itself has no predicate: the server checks it at the end of
    # the statement, against the rows the statement wrote.
    preds = generate.predicates(facts, key, loaded_keys - {key}, cyclic)
    r = container.sql("\n".join(generate.load_sql(plan, [])[:2]), db=db)
    if _errors(r):
        return 0, "candidates failed: " + _errors(r)[0][:200]
    kept = []
    for label, con, p in preds:
        r = container.sql("SELECT count(*) FROM ybm_cand WHERE (%s) IS FALSE;" % p, db=db,
                          flags=("-tA",))
        if _errors(r):
            gaps.append({"layer": "gen.predicate", "table": table, "model": label,
                         "constraint": con, "server": _errors(r)[0][:200]})
            if con:
                _drop_constraint(container, db, key, con)
            continue
        kept.append((label, con, p))
    for _ in range(4):
        r = container.sql(generate.load_sql(plan, kept)[2], db=db)
        err = _errors(r)
        if not err:
            break
        m = re.search(r'constraint "([^"]+)"', err[0])
        if not m:
            return 0, "load failed: " + err[0][:200]
        gaps.append({"layer": "gen.constraint_dropped", "table": table, "model": m.group(1),
                     "server": err[0][:200]})
        _drop_constraint(container, db, key, m.group(1))
        kept = [k for k in kept if k[1] != m.group(1)]
    else:
        return 0, "load failed: " + err[0][:200]
    r = container.sql("SELECT count(*) FROM %s;" % qn(table), db=db, flags=("-tA",))
    try:
        loaded = int((r.stdout or "0").strip().splitlines()[-1])
    except (ValueError, IndexError):
        loaded = 0
    if loaded < plan["rows"]:
        rej = _json(container.sql(generate.rejects_sql(kept), db=db, flags=("-tA",))) \
            if kept else None
        gaps.append({"layer": "gen.filtered", "table": table, "model": plan["rows"],
                     "server": loaded, "rejects": {k: v for k, v in (rej or {}).items() if v}})
    container.sql("DROP TABLE IF EXISTS ybm_cand;", db=db)
    if not loaded:
        return 0, "no candidate row satisfies the table's predicates"
    return loaded, None


def run(container, bundle, sch, patterns, set_sql, inject_sql, ddl_sql, budget=None,
        replayed=None, colocated=False, head=""):
    """Generate samples (generate.py) and measure the single-table reads of `patterns` in a
    scratch database of `container` (replay.Container). `replayed`: replay's entries (plan,
    param_types), whose conditions say which literal and parameter goes to which column.
    Returns {"patterns": [...], "tables": {...}, "gaps": [...]}."""
    from . import generate, server
    from .generate import generation_set
    from .sqltok import rel_key
    budget = budget or generate.BUDGET
    by_id = {e["id"]: e for e in replayed or [] if e.get("plan")}
    reads = [p for p in patterns if p["kind"] == "select" and len(p["tables"]) == 1]
    by_table = {}
    for p in reads:
        by_table.setdefault(p["tables"][0], []).append(p)
    out = {"budget": budget, "patterns": [], "tables": {}, "gaps": [],
           "joins": [p["id"] for p in patterns if p["kind"] == "select" and
                     len(p["tables"]) > 1]}  # not measured: named in the review
    if not reads:
        return out
    srv = {}  # pattern id -> (server params, server literals)
    for p in reads:
        e = by_id.get(p["id"])
        if e:
            srv[p["id"]] = server.params(e["plan"], p["tables"][0].split(".")[-1])
            for g in compare_params(p["query"], srv[p["id"]][0]):
                out["gaps"].append(dict(g, layer="bind.param", pattern=p["id"]))
    db = "ybm_measure"
    # The replay database's preparation and layout, so tables load as the replayed plans saw
    # them.
    from . import catalog
    try:
        failed, _ = catalog.prepare(container, db, ddl_sql, colocated, head)
    except RuntimeError as e:  # an optional pass: record why, keep the replay's plans
        out["gaps"].append({"layer": "gen.ddl", "model": "the measured database",
                            "errors": 1, "server": str(e)[:200]})
        return out
    if failed:  # tables that did not create are named later as not in the scratch database
        out["gaps"].append({"layer": "gen.ddl", "model": "the replayed DDL",
                            "errors": len(failed), "server": failed[0][1][:200]})
    facts = read_facts(container, db)
    if not facts:  # the catalog is the generator's only source: without it nothing is measured
        for table in by_table:
            out["tables"][table] = {"measured": False,
                                    "reason": "the scratch server's catalog could not be read"}
        out["gaps"].append({"layer": "gen.facts", "model": "FACTS",
                            "server": "no result from the catalog query"})
        return out
    consts = schema_constants(facts)
    ftables = {(t["schema"], t["name"]): t for t in facts.get("tables") or []}
    # B: the measured tables and the tables their foreign keys reference, parents first; one
    # component per set of columns that must share values (foreign keys, join conditions).
    measured_keys = {generate._key(t): t for t in by_table}
    order, cyclic = generation_set(facts, measured_keys)
    # C: a foreign key that closes a cycle cannot hold for rows loaded parents first; it goes,
    # recorded. Every other constraint stays and decides which candidate rows load.
    for fk in cyclic:
        out["gaps"].append({"layer": "gen.fk_cycle", "table": fk["table"], "model": fk["cols"],
                            "constraint": fk["name"],
                            "server": "%s(%s)" % (fk["ref_table"], ", ".join(fk["ref_cols"]))})
        _drop_constraint(container, db, (fk["schema"], fk["table"]), fk["name"])
    joins = [j for e in by_id.values() for j in server.joins(e["plan"])]
    comp_of, fk_edges = generate.components(facts, set(order), joins)
    uniques = {}
    for u in facts.get("uniques") or []:
        if not u.get("primary"):
            uniques.setdefault((u["schema"], u["table"]), []).append(u["cols"])
    plans, keys = {}, {}
    for key in order:
        table = measured_keys.get(key) or rel_key(None if key[0] == "public" else key[0],
                                                  key[1])
        lits = {}  # column -> raw constants: the plan's, the text's, the schema's
        for p in by_table.get(table, []):
            sources = [srv.get(p["id"], ({}, {}))[1], literals(p["query"])]
            for src in sources:
                for c, vs in src.items():
                    for v in vs:
                        if unquote(v) not in lits.setdefault(c, []):
                            lits[c].append(unquote(v))
        for c, vs in consts.get(key, {}).items():
            for v in vs:
                if unquote(v) not in lits.setdefault(c, []):
                    lits[c].append(unquote(v))
        plan, why = generate.plan_table(sch, bundle, table, ftables.get(key), lits, budget,
                                        comp_of, uniques.get(key, []))
        if plan is None:
            out["tables"][table] = {"measured": False, "reason": why}
            continue
        for cols_u in plan["unique_unmet"]:
            out["gaps"].append({"layer": "gen.unique", "table": table, "model": cols_u,
                                "server": "no free column to make the set unique"})
        plans[table], keys[table] = plan, key
    generate.settle(plans, fk_edges, keys)
    failed = generate.materialize(container, db, plans) if plans else {}
    if plans:
        narrowed, gaps, drops = generate.narrow(container, db, plans, facts, keys)
        generate.resettle(container, db, plans, fk_edges, keys)
        failed.update({t: why for t, why in narrowed.items() if t not in failed})
        out["gaps"].extend(gaps)
        for key, con in drops:
            _drop_constraint(container, db, key, con)
            ft = ftables.get(key) or {}  # gone: the load's filter no longer applies it
            ft["checks"] = [c for c in ft.get("checks") or [] if c["name"] != con]
    plans_by_table = {}
    for table in [t for k in order for t, kk in keys.items() if kk == k]:
        plan = plans[table]
        if table in failed:
            out["tables"][table] = {"measured": False, "reason": failed[table]}
            continue
        generate.finalize(plan)
        for c, (held, n) in sorted(generate.unloaded_literals(plan).items()):
            out["gaps"].append({"layer": "gen.literals", "table": table, "model": c,
                                "held": held, "bound": n})
        loaded, why = _load(container, db, facts, keys[table], plan,
                            {keys[t] for t in plans_by_table}, cyclic, out["gaps"])
        if not loaded:
            out["tables"][table] = {"measured": False, "reason": why}
            continue
        planned, plan["rows"] = plan["rows"], loaded
        plans_by_table[table] = plan
        out["tables"][table] = {"measured": True, "rows": loaded, "planned_rows": planned,
                                "referenced_only": table not in by_table,
                                "per_prefix": plan["per_prefix"],
                                "range_cols": plan["range_cols"],
                                "range_sizes": plan["range_sizes"]}
    if not plans_by_table:
        return out
    ranged = set()
    for p in reads:
        sp = srv.get(p["id"], ({}, {}))[0]
        for n, (col, op) in list(params(server.statement(p["query"])[0]).items()) + \
                list(sp.items()):
            if op in (">", ">=", "<", "<="):
                ranged.add((p["tables"][0], col))
    generate.sort_domains(container, db, plans_by_table, ranged)
    neutrals = generate.neutral_values(container, db, [t for e in by_id.values()
                                                       for t in e.get("param_types") or []])
    # D: the sample as loaded, against the plan it was made from.
    for table, plan in plans_by_table.items():
        if plan["rows"] == out["tables"][table]["planned_rows"]:
            out["gaps"].extend(generate.verify_shape(container, db, plan, keys[table]))
    # Full-scale statistics after the load: the planner chooses as on the customer's table.
    container.sql(inject_sql, db=db)
    for p in reads:
        table = p["tables"][0]
        if table not in plans_by_table:
            continue
        q, rewrites = server.statement(p["query"])
        e = by_id.get(p["id"]) or {}
        vals = bind(q, plans_by_table, [table], srv.get(p["id"], ({}, {}))[0],
                    e.get("param_types"), neutrals)
        n = max([int(x) for x in re.findall(r"\$(\d+)", re.sub(r"'(?:[^']|'')*'", "", q))]
                or [0])
        entry = {"id": p["id"], "table": table, "weight": p["weight"]}
        if rewrites:
            entry["rewrites"] = rewrites
        typed = [i for i in range(1, n + 1) if i in vals and i not in
                 (srv.get(p["id"], ({}, {}))[0]) and i not in params(q)]
        if typed:
            entry["bound_by_type"] = typed
        missing = [i for i in range(1, n + 1) if i not in vals]
        if missing:
            entry["error"] = "could not bind $%s to a sample value" % ", $".join(
                str(i) for i in missing)
            out["patterns"].append(entry)
            continue
        name = "ybm_m_%s" % p["id"].lower()
        body = ("PREPARE %s AS %s;\nEXPLAIN (ANALYZE, DIST, DEBUG, FORMAT JSON) EXECUTE %s(%s);"
                "\nDEALLOCATE %s;" % (name, q, name, ", ".join(vals[i] for i in range(1, n + 1)),
                                      name)) if n else \
            "EXPLAIN (ANALYZE, DIST, DEBUG, FORMAT JSON) %s;" % q
        r = container.sql(set_sql + body + "\n", db=db, flags=("-tA",))
        txt = r.stdout
        try:
            plan_json = json.loads(txt[txt.index("["):txt.rindex("]") + 1])
            m = probes.metrics(plan_json)
            flip = generate.plan_flip(e["plan"], plan_json) if e.get("plan") else []
        except (ValueError, KeyError, TypeError):
            entry["error"] = ((r.stderr or "").strip() or txt.strip())[:200]
            out["patterns"].append(entry)
            continue
        entry.update({k: m.get(k) for k in ("returned", "scanned", "seeks", "nexts",
                                            "read_requests", "filtered", "sorted", "scan",
                                            "index")})
        ret = max(m.get("returned") or 0, 1)
        entry["seeks_per_row"] = round(m["seeks"] / ret, 2) if m.get("seeks") is not None \
            else None
        entry["scanned_per_row"] = round((m.get("scanned") or 0) / ret, 2)
        entry["asked"] = asked(q)
        entry["anomalies"] = anomalies(dict(m, asked=entry["asked"]))
        if flip:  # D: not the plan the customer runs; its counters explain nothing
            entry["plan_flip"] = ["%s %s" % (n, i or "") for n, i in flip]
            out["gaps"].append({"layer": "gen.plan_flip", "table": table, "pattern": p["id"],
                                "model": "replayed plan", "server": entry["plan_flip"]})
        if not m.get("returned"):
            out["gaps"].append({"layer": "gen.empty", "table": table, "pattern": p["id"],
                                "model": "bound to sample values", "server": "0 rows"})
        out["patterns"].append(entry)
    return out
