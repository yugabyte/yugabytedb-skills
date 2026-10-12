"""Sample generation for the measured pass, run on the scratch server.

Four algorithms, each defined once (engine.md §7):

A. Value constructor. A column's values are the images of 0, 1, 2, ... under ybm_value(type, i),
   a server function that walks pg_type: an enum gives its labels, a domain its base type's
   values (the cast enforces the domain's CHECK), an array or a composite its element values,
   a base type a value of its category. A value is admitted when every member of its
   component (B) takes it as a value of its own type, and is new under each member type's own
   equality (a cast that truncates, as to varchar(n), or two spellings of one number yield one
   value); it is kept as written, so every member casts it. The constants the workload or the schema compares
   a column with come first. Bounded types (boolean, enum) give fewer values, and that count
   is their size.
B. Equality graph. Columns that must hold the same values are one component: a foreign key's
   child and parent columns, the two sides of every join condition in the replayed plans. A
   component has one value list (its members' constants first); each member takes the first k
   values of it, a foreign key's child no more than its parent, so a child's values are its
   parent's and join sides overlap by construction. The tables generated are the measured ones
   and, transitively, the tables their foreign keys reference, loaded parents first. Each unique
   column set gets an injective member where one is free.
C. Predicate-filtered load. The table keeps every constraint it has, and its own predicates,
   as the server deparses them, decide what loads. First each column's values are narrowed by
   the predicates that read only that column (a one-column CHECK; the partition bound when the
   partition key is one column), evaluated by the server on the values. Then candidate rows
   (four or more per sample row: the same key values; copy 0 is the sample row, later copies
   shift the other columns by hashed offsets) go to a scratch table, and per key the first
   candidate that satisfies every predicate loads: CHECK expressions, the partition bound (for
   a partitioned table, any partition's, unless one is DEFAULT), and foreign keys as "exists in
   the parent". Conflicts on unique columns are skipped. Each loss is recorded: rows rejected
   per predicate; a one-column CHECK no made value satisfies, or a CHECK the server cannot
   evaluate on the candidates, is dropped; a constraint the load still violates, named by the
   server, is dropped and the load retried; a table no candidate fits is not measured.
D. Round-trip verifier. The server checks the sample against the plan it was made from, and
   the measured plan against the replayed one. Each loaded table's columns are counted
   (distinct values, NULLs; exact counts, so no statistics change the plans) and compared with
   what the plan meant to make (a table the predicates thinned is not compared: its loss is
   already recorded). After the run, a measured plan that uses a scan the replayed plan does
   not (the planner chose differently on the sample) is a plan flip: its counters describe a
   plan the customer would not run, so they explain nothing and raise nothing. A read that
   returns no row is recorded too. Each is a gap (gen.shape, gen.plan_flip, gen.empty).

Values are a pure function of (type, i), after the constants the workload's statements and the
schema compare columns with: the sample is deterministic for (bundle, image). Statistics give
only its shape; the customer's data never enters it, and only those constants come from the
customer's own text (they never leave in a gap record: server.scrub).
"""

import json

from .sqltok import qi, qn, split_key

BUDGET = 4000          # rows per hash prefix
PREFIXES = 2           # target + background
MAX_PREFIXES = 8       # at most, when the workload binds more hash key values than PREFIXES
DEFAULT_ND = 8         # distinct values of a key column when pg_stats has none
OTHER_ND = 100         # distinct values of other columns when pg_stats has none
POOL = 64              # values made per component at least, for narrow() to choose from
COPIES = 4             # candidate rows per sample row, for the load's predicates to choose from
CANDIDATES = 64        # candidate rows at least, however small the table

# A. The value constructor and its domain table, installed in the scratch database.
GEN_SQL = r"""
CREATE OR REPLACE FUNCTION ybm_value(t oid, i int) RETURNS text AS $f$
DECLARE r record; n int; e text;
BEGIN
  SELECT typtype, typcategory, typbasetype, typelem, typname, typrelid INTO r
    FROM pg_type WHERE oid = t;
  IF NOT FOUND THEN RETURN NULL; END IF;
  IF r.typtype = 'd' THEN RETURN ybm_value(r.typbasetype, i); END IF;
  IF r.typtype = 'e' THEN
    SELECT count(*) INTO n FROM pg_enum WHERE enumtypid = t;
    IF n = 0 THEN RETURN NULL; END IF;
    RETURN (SELECT enumlabel::text FROM pg_enum WHERE enumtypid = t
             ORDER BY enumsortorder OFFSET (i % n) LIMIT 1);
  END IF;
  IF r.typcategory = 'A' AND r.typelem <> 0 THEN
    e := ybm_value(r.typelem, i);
    RETURN CASE WHEN e IS NULL THEN NULL
                ELSE '{"' || replace(replace(e, '\', '\\'), '"', '\"') || '"}' END;
  END IF;
  IF r.typtype = 'c' THEN
    SELECT '(' || string_agg(COALESCE('"' || replace(replace(ybm_value(a.atttypid, i), '\',
             '\\'), '"', '\"') || '"', ''), ',' ORDER BY a.attnum) || ')' INTO e
      FROM pg_attribute a WHERE a.attrelid = r.typrelid AND a.attnum > 0
       AND NOT a.attisdropped;
    RETURN e;
  END IF;
  IF r.typcategory = 'N' THEN RETURN i::text; END IF;
  -- Strings of lengths 2 to 9 (i's digits, padded on the left with v; distinct: dropping the
  -- leading v's gives i back), so a length CHECK finds values.
  IF r.typcategory = 'S' THEN
    RETURN CASE WHEN length(i::text) >= 1 + i % 9 THEN 'v' || i
                ELSE lpad(i::text, 1 + i % 9, 'v') END;
  END IF;
  IF r.typcategory = 'B' THEN RETURN CASE WHEN i % 2 = 0 THEN 'true' ELSE 'false' END; END IF;
  IF r.typname = 'date' THEN RETURN (date '2026-01-01' + i)::text; END IF;
  IF r.typname IN ('time', 'timetz') THEN
    RETURN (time '00:00' + (i % 86400) * interval '1 second')::text;
  END IF;
  IF r.typcategory = 'D' THEN
    RETURN (timestamp '2026-01-01' + i * interval '1 minute')::text;
  END IF;
  IF r.typcategory = 'T' THEN RETURN i || ' seconds'; END IF;
  IF r.typname = 'uuid' THEN RETURN md5(i::text)::uuid::text; END IF;
  IF r.typname IN ('json', 'jsonb') THEN RETURN '{"k": ' || i || '}'; END IF;
  IF r.typname = 'bytea' THEN RETURN '\x' || md5(i::text); END IF;
  IF r.typname = 'hstore' THEN RETURN 'k=>' || i; END IF;
  IF r.typcategory = 'I' THEN
    RETURN '10.' || (i / 65536) % 256 || '.' || (i / 256) % 256 || '.' || i % 256;
  END IF;
  RETURN NULL;
END $f$ LANGUAGE plpgsql STABLE;

CREATE TABLE IF NOT EXISTS ybm_dom (comp int PRIMARY KEY, vals text[], reps text[]);

-- v admitted to a component whose members have types ts (keyexpr[k] casts $1 to the k-th):
-- NULL when some member's type refuses it; the value already admitted that equals it under
-- some member's own equality (ybm_seen_k holds the admitted values as that type; a type
-- without equality compares by its text form); otherwise v, admitted when there is room.
CREATE OR REPLACE FUNCTION ybm_admit(v text, keyexpr text[], room boolean) RETURNS text AS $f$
DECLARE k int; hit text;
BEGIN
  FOR k IN 1 .. cardinality(keyexpr) LOOP
    BEGIN
      EXECUTE 'SELECT ' || keyexpr[k] USING v;
    EXCEPTION WHEN others THEN RETURN NULL;
    END;
  END LOOP;
  FOR k IN 1 .. cardinality(keyexpr) LOOP
    EXECUTE format('SELECT raw FROM ybm_seen_%s WHERE x = %s', k, keyexpr[k]) INTO hit USING v;
    IF hit IS NOT NULL THEN RETURN hit; END IF;
  END LOOP;
  IF NOT room THEN RETURN NULL; END IF;
  FOR k IN 1 .. cardinality(keyexpr) LOOP
    EXECUTE format('INSERT INTO ybm_seen_%s VALUES (%s, $1)', k, keyexpr[k]) USING v;
  END LOOP;
  RETURN v;
END $f$ LANGUAGE plpgsql;

-- comp's values: the constants first, then ybm_value(ts[1], 0..), each admitted (ybm_admit)
-- by every member's type, kept as written so every member casts it; at most size. reps: for
-- each constant, the value that holds it (NULL when no member type takes it or there was no
-- room). A bounded type stops when its values repeat.
CREATE OR REPLACE FUNCTION ybm_domain(comp int, ts oid[], ms int[], size int, lits text[])
RETURNS int AS $f$
DECLARE vals text[] := '{}'; reps text[] := '{}'; keyexpr text[] := '{}'; v text; r text;
        first text; i int := 0; k int; tries int := size * 2 + 16;
BEGIN
  FOR k IN 1 .. cardinality(ts) LOOP
    EXECUTE format('DROP TABLE IF EXISTS ybm_seen_%s', k);
    BEGIN
      EXECUTE format('CREATE TEMP TABLE ybm_seen_%s (x %s UNIQUE, raw text)', k,
                     format_type(ts[k], ms[k]));
      keyexpr := keyexpr || format('$1::%s', format_type(ts[k], ms[k]));
    EXCEPTION WHEN others THEN
      EXECUTE format('CREATE TEMP TABLE ybm_seen_%s (x text UNIQUE, raw text)', k);
      keyexpr := keyexpr || format('($1::%s)::text', format_type(ts[k], ms[k]));
    END;
  END LOOP;
  FOREACH v IN ARRAY coalesce(lits, '{}'::text[]) LOOP
    r := CASE WHEN v IS NULL THEN NULL
              ELSE ybm_admit(v, keyexpr, cardinality(vals) < size) END;
    reps := reps || r;
    IF r = v AND NOT v = ANY(vals) THEN
      vals := vals || v;
    END IF;
  END LOOP;
  WHILE cardinality(vals) < size AND i < tries LOOP
    v := ybm_value(ts[1], i);
    EXIT WHEN v IS NULL OR v = first;
    IF i = 0 THEN first := v; END IF;
    i := i + 1;
    IF ybm_admit(v, keyexpr, true) = v AND NOT v = ANY(vals) THEN
      vals := vals || v;
    END IF;
  END LOOP;
  FOR k IN 1 .. cardinality(ts) LOOP
    EXECUTE format('DROP TABLE IF EXISTS ybm_seen_%s', k);
  END LOOP;
  DELETE FROM ybm_dom WHERE ybm_dom.comp = ybm_domain.comp;
  INSERT INTO ybm_dom VALUES (comp, vals, reps);
  RETURN cardinality(vals);
END $f$ LANGUAGE plpgsql;
"""


class _UF:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            lo, hi = sorted([ra, rb])  # deterministic representative
            self.parent[hi] = lo


def generation_set(facts, measured):
    """B: the measured tables and, transitively, the tables their foreign keys reference, in
    load order (a table after every table it references), and the foreign keys dropped to break
    cycles: (order, cyclic) as lists of (schema, name) keys and fk dicts."""
    fks = [fk for fk in facts.get("fks") or []]
    want = set(measured)
    changed = True
    while changed:
        changed = False
        for fk in fks:
            child, parent = (fk["schema"], fk["table"]), (fk["ref_schema"], fk["ref_table"])
            if child in want and parent not in want:
                want.add(parent)
                changed = True
    edges = {t: set() for t in want}
    for fk in fks:
        child, parent = (fk["schema"], fk["table"]), (fk["ref_schema"], fk["ref_table"])
        if child in want and parent in want and child != parent:
            edges[child].add(parent)
    order, done, cyclic = [], set(), []
    def visit(t, stack):
        if t in done:
            return
        if t in stack:
            return
        stack.add(t)
        for p in sorted(edges[t]):
            if p in stack:
                cyclic.extend(fk for fk in fks if (fk["schema"], fk["table"]) == t and
                              (fk["ref_schema"], fk["ref_table"]) == p)
                continue
            visit(p, stack)
        stack.discard(t)
        done.add(t)
        order.append(t)
    for t in sorted(want):
        visit(t, set())
    return order, cyclic


def components(facts, tables, joins=()):
    """B: (table key, column) -> component id, and the foreign-key edges (child slot, parent
    slot) among `tables`. Members are unified across foreign keys and join conditions."""
    uf = _UF()
    names = {t[1]: t for t in tables}
    fk_edges = []
    for fk in facts.get("fks") or []:
        child, parent = (fk["schema"], fk["table"]), (fk["ref_schema"], fk["ref_table"])
        if child in tables and parent in tables:
            for c, r in zip(fk["cols"], fk["ref_cols"]):
                uf.union((child, c), (parent, r))
                fk_edges.append(((child, c), (parent, r)))
    for (lt, lc), (rt, rc) in joins:
        lk, rk = names.get(lt), names.get(rt)
        if lk and rk:
            uf.union((lk, lc), (rk, rc))
    ids, out = {}, {}
    ftab = {(t["schema"], t["name"]): t for t in facts.get("tables") or []}
    for t in sorted(tables):
        for c in (ftab.get(t) or {}).get("columns") or []:
            root = uf.find((t, c["name"]))
            out[(t, c["name"])] = ids.setdefault(root, len(ids))
    return out, fk_edges


def _key(table):
    s, n = split_key(table)
    return (s or "public", n)


def _quote(raw):
    return "'%s'" % raw.replace("'", "''")


def unquote(lit):
    """A literal's text: 'it''s' -> it's, 42 -> 42."""
    return lit[1:-1].replace("''", "'") if lit.startswith("'") else lit


def _nd(bundle, table, col, rows):
    st = bundle.stats.get((table, col))
    if not st or st.get("n_distinct") in (None, 0):
        return None
    nd = st["n_distinct"]
    if nd < 0:
        return int(-nd * rows) if rows else None
    return int(nd)


def plan_table(sch, bundle, table, facts_table, literals, budget=BUDGET, comp_of=None,
               uniques=()):
    """The sample's planned shape for one table, or (None, reason). literals: column -> raw
    constants the workload or the schema compares it with. Each column gets a component (its
    value list, materialised on the server), a role (how a row picks its value: p hash prefix,
    r:k the k-th range key digit, n injective, m spread) and a planned size; the sizes become
    actual once the server has made the values (finalize)."""
    t = sch.tables.get(table)
    if t is None or facts_table is None:
        return None, "not in the scratch database"
    rows_full = bundle.reltuples.get(table)
    # No primary key: rows by the row id, one flat run of the budget (or the table's size).
    hk = [k.col for k in t.pk.hash_cols if k.col] if t.pk else []
    rk = [k.col for k in t.pk.keys if k.col and k.col not in hk] if t.pk else []
    flat = None if t.pk else max(1, min(int(rows_full or budget), budget))
    d = {}
    for c in rk:
        nd = _nd(bundle, table, c, rows_full) or DEFAULT_ND
        d[c] = max(len(literals.get(c, [])), min(nd, budget))
    def product():
        p = 1
        for c in rk:
            p *= d[c]
        return p
    while product() > budget:
        c = max(rk, key=lambda x: d[x])
        floor = max(2, len(literals.get(c, [])))
        if d[c] <= floor:
            break
        d[c] = max(floor, d[c] // 2)
    # One prefix per hash key value the workload binds (an IN list on the key), within bounds.
    prefixes = min(MAX_PREFIXES, max([PREFIXES] + [len(literals.get(c, [])) for c in hk])) \
        if hk else 1
    n_rows = flat or product() * prefixes
    # One injective member per unique column set that is not already unique by the key's
    # cross product: the first of its columns with no key role.
    injective, unmet = set(), []
    keyed = set(hk) | set(rk)
    for cols_u in uniques:
        if keyed and set(cols_u) >= keyed:
            continue  # contains the whole primary key: unique by the cross product
        free = [c for c in cols_u if c not in keyed]
        if free:
            injective.add(free[0])
        else:
            unmet.append(list(cols_u))
    cols = {c["name"]: c for c in facts_table["columns"]}
    key = _key(table)
    shape = {}
    comp = 0
    for c in t.col_order:
        fc = cols.get(c)
        if fc is None or fc.get("generated"):
            continue  # the server computes a generated column
        if c in hk:
            size, role = max(prefixes, len(literals.get(c, []))), "p"
        elif c in rk:
            size, role = d[c], "r:%d" % rk.index(c)
        elif c in injective:
            size, role = n_rows, "n"
        else:
            nd = _nd(bundle, table, c, rows_full)
            size, role = max(1, min(nd or OTHER_ND, n_rows)), "m"
        st = bundle.stats.get((table, c)) or {}
        cid = (comp_of or {}).get((key, c), comp)
        shape[c] = {"type": fc["type"], "typoid": int(fc["typoid"]), "typmod": int(fc["typmod"]),
                    "notnull": bool(fc["notnull"]) or c in hk or c in rk, "comp": cid,
                    "role": role, "size": size, "literals": list(literals.get(c, [])),
                    "null_frac": 0.0 if fc["notnull"] or c in hk or c in rk
                    else float(st.get("null_frac") or 0.0), "domain": None}
        comp += 1
    return {"table": table, "range_cols": rk, "prefixes": prefixes, "shape": shape,
            "unique_unmet": unmet, "flat": flat}, None


def settle(plans, fk_edges, keys):
    """B across tables: every member of a component carries the component's constants (so each
    member's first values include them), and a foreign key's child takes no more values than
    its parent. keys: table -> (schema, name)."""
    by_slot = {(keys[t], c): s for t, p in plans.items() for c, s in p["shape"].items()}
    lits = {}
    for slot, s in sorted(by_slot.items()):
        for v in s["literals"]:
            if v not in lits.setdefault(s["comp"], []):
                lits[s["comp"]].append(v)
    for s in by_slot.values():
        s["literals"] = list(lits.get(s["comp"], []))
        s["size"] = max(s["size"], len(s["literals"]))
    for child, parent in fk_edges:
        cs, ps = by_slot.get(child), by_slot.get(parent)
        if cs and ps:
            cs["size"] = min(cs["size"], ps["size"])


def materialize(container, db, plans):
    """A: make every component's values on the server (one call per component, one read-back
    for all) and give each column its domain (raw text). Returns {table: reason} for tables a
    key or NOT NULL column of which has no value."""
    comps, types = {}, {}
    for t in sorted(plans):  # one call per component: the largest member decides its size
        for c, s in sorted(plans[t]["shape"].items()):
            have = comps.get(s["comp"])
            if have is None or s["size"] > have["size"]:
                comps[s["comp"]] = s
            types.setdefault(s["comp"], set()).add((s["typoid"], s["typmod"]))
    calls = []
    for cid, s in sorted(comps.items()):
        lits = "ARRAY[%s]::text[]" % ", ".join(_quote(v) for v in s["literals"]) \
            if s["literals"] else "'{}'::text[]"
        ts = [(s["typoid"], s["typmod"])] + sorted(types[cid] - {(s["typoid"], s["typmod"])})
        calls.append("SELECT ybm_domain(%d, ARRAY[%s]::oid[], ARRAY[%s]::int[], %d, %s);" % (
            cid, ", ".join(str(t) for t, _ in ts), ", ".join(str(m) for _, m in ts),
            max(s["size"], POOL), lits))
    container.sql(GEN_SQL + "\n".join(calls) + "\n", db=db)
    r = container.sql("SELECT json_object_agg(comp, json_build_array(vals, reps)) FROM ybm_dom;",
                      db=db, flags=("-tA",))
    txt = (r.stdout or "").strip()
    doms = {int(k): v for k, v in json.loads(txt[txt.index("{"):]).items()} if "{" in txt \
        else {}
    failed = {}
    for table, p in plans.items():
        for c, s in p["shape"].items():
            vals, reps = doms.get(s["comp"]) or [[], []]
            s["pool"] = vals or []  # the component's values, for narrow()
            s["reps"] = list(reps or [])  # per constant: the value that holds it, or None
            s["domain"] = s["pool"][:s["size"]]  # this member's first values
            if not s["domain"]:
                if s["notnull"]:
                    failed[table] = "column %s: the server could not make a value of type %s" \
                        % (c, s["type"])
                s["domain"] = None  # a nullable column with no value stays NULL
    return failed


def unary_predicates(facts, key):
    """C, node consistency: {column: [(constraint, sql)]} for the table's predicates that read
    one column (constraint: the CHECK's name; None for a partition bound):
    a CHECK over one column, and the partition bound when the partition key is one column (a
    partition's own; for a partitioned table, any partition's unless one is DEFAULT)."""
    tables = {(t["schema"], t["name"]): t for t in facts.get("tables") or []}
    t = tables.get(key) or {}
    out = {}
    for c in t.get("checks") or []:
        if len(c.get("cols") or []) == 1:
            out.setdefault(c["cols"][0], []).append((c["name"], c["expr"]))
    def keycols(x):  # a partition's bound reads its own key and its ancestors' keys
        cols, seen = set(), set()
        while x and x["name"] not in seen:
            seen.add(x["name"])
            parent = tables.get((x["schema"], x.get("parent"))) if x.get("parent") else None
            if parent is None:
                break
            cols |= set(parent.get("partkey") or [None])
            x = parent
        return cols
    if t.get("partition_constraint"):
        cols = keycols(t)
        if len(cols) == 1 and None not in cols:
            out.setdefault(cols.pop(), []).append((None, t["partition_constraint"]))
    kids = [x for k, x in sorted(tables.items()) if x.get("parent") == key[1] and
            k[0] == key[0]]
    keys = set(t.get("partkey") or [None]) | set().union(*[keycols(x) for x in kids] or [set()])
    if kids and len(keys) == 1 and None not in keys and not any(x.get("is_default")
                                                                 for x in kids):
        parts = [x["partition_constraint"] for x in kids if x.get("partition_constraint")]
        if parts:
            out.setdefault(keys.pop(), []).append((None, "(%s)" % " OR ".join(parts)))
    return out


def narrow(container, db, plans, facts, keys):
    """C, node consistency: each column keeps the values of its component that its one-column
    predicates admit, evaluated by the server on the values themselves, then its first `size`
    of them. A CHECK no made value satisfies is dropped (the values cannot be found by
    enumeration; the statistics, not the rule, shape the reads) and recorded; a partition bound
    no value satisfies leaves the table unmeasured. Returns ({table: reason}, [gap], [(table
    key, constraint)] to drop). A predicate the server cannot evaluate this way is left to the
    load's filter."""
    failed, gaps, drops = {}, [], []
    next_comp = 1 + max([s["comp"] for p in plans.values() for s in p["shape"].values()] or [0])
    for table, plan in sorted(plans.items()):
        for col, preds in sorted(unary_predicates(facts, keys[table]).items()):
            s = plan["shape"].get(col)
            if not s or not s.get("pool"):
                continue
            admitted = s["pool"]
            for con, p in preds:
                r = container.sql(
                    "SELECT json_agg(ybm_v ORDER BY ybm_o) FROM (SELECT ybm_v, ybm_o, "
                    "ybm_v::%s AS %s FROM unnest(ARRAY[%s]::text[]) WITH ORDINALITY "
                    "u(ybm_v, ybm_o)) x WHERE (%s) IS NOT FALSE;" % (
                        s["type"], qi(col), ", ".join(_quote(v) for v in admitted), p),
                    db=db, flags=("-tA",))
                txt = (r.stdout or "").strip()
                try:
                    if "ERROR:" in (r.stderr or "") + txt:
                        raise ValueError
                    left = json.loads(txt) if txt else []  # none admitted: NULL, empty
                except ValueError:
                    gaps.append({"layer": "gen.narrow", "table": table, "model": col,
                                 "server": ((r.stderr or "") + txt).strip()[:200]})
                    continue
                if left:
                    admitted = left
                elif con:
                    gaps.append({"layer": "gen.check_dropped", "table": table, "model": con,
                                 "server": "no value made for %s satisfies %s" % (col, p)})
                    drops.append((keys[table], con))
                else:
                    failed[table] = "column %s: no value made satisfies the partition bounds" \
                        % col
                    break
            next_comp = _own_values(container, db, s, admitted, next_comp)
    return failed, gaps, drops


def _own_values(container, db, s, values, next_comp):
    """Give a column its own value list (a new component, on the server too) when it differs
    from its component's; returns the next free component id."""
    if values != s["pool"]:
        s["comp"] = next_comp
        next_comp += 1
        container.sql("INSERT INTO ybm_dom VALUES (%d, ARRAY[%s]::text[]);" % (
            s["comp"], ", ".join(_quote(v) for v in values)), db=db)
    s["pool"] = values
    s["domain"] = values[:s["size"]] or None
    return next_comp


def resettle(container, db, plans, fk_edges, keys):
    """B again after C's narrowing: a foreign key's child keeps only values its parent holds,
    parents first, so narrowing one member of a component cannot leave a child value with no
    parent row."""
    by_slot = {(keys[t], c): s for t, p in plans.items() for c, s in p["shape"].items()}
    plan_of = {(keys[t], c): p for t, p in plans.items() for c in p["shape"]}
    next_comp = 1 + max([s["comp"] for s in by_slot.values()] or [0])
    changed = True
    while changed:  # a chain of keys settles from its root; each pass only removes values
        changed = False
        for child, parent in fk_edges:
            cs, ps = by_slot.get(child), by_slot.get(parent)
            if not cs or not ps or not cs.get("pool") or child == parent:
                continue
            held = set(loaded_values(plan_of[parent], parent[1]))  # what parent rows hold
            kept = [v for v in cs["pool"] if v in held]
            if kept != cs["pool"]:
                next_comp = _own_values(container, db, cs, kept, next_comp)
                changed = True


def loaded_values(plan, col):
    """The values of `col` that sample rows hold: a hash column's first `prefixes`, a range or
    injective column's own, another column's first values-per-prefix (load_sql's roles). Valid
    before and after finalize."""
    s = plan["shape"][col]
    dom = s["domain"] or []
    if s["role"] == "p":
        return dom[:plan["prefixes"]]
    if s["role"].startswith("r:") or s["role"] == "n":
        return dom
    per = plan.get("flat") or 1
    for rc in plan["range_cols"] if not plan.get("flat") else ():
        per *= max(1, len(plan["shape"][rc]["domain"] or []))
    return dom[:per]


def unloaded_literals(plan):
    """D: {column: (bound values loaded, bound values)} for the columns some of whose bound
    values no sample row holds. A bound value is the value that holds a constant (reps, from
    the server); a constant no member type takes is not a value at all and is not counted."""
    out = {}
    for c, s in plan["shape"].items():
        bound = {r for r in s.get("reps") or [] if r is not None}
        if not bound or not s["domain"]:
            continue
        held = len(bound & set(loaded_values(plan, c)))
        if held < len(bound):
            out[c] = (held, len(bound))
    return out


def finalize(plan):
    """The shape from the actual value counts: range sizes, rows per prefix, rows."""
    sizes = []
    for c in plan["range_cols"]:
        s = plan["shape"][c]
        sizes.append(min(s["size"], len(s["domain"] or [])) or 1)
    per = plan.get("flat") or 1
    for x in sizes:
        per *= x
    plan["range_sizes"] = sizes
    plan["per_prefix"] = per
    plan["rows"] = per * plan["prefixes"]
    for c, s in plan["shape"].items():
        if s["role"] == "p" and s["domain"]:
            plan["prefixes"] = min(plan["prefixes"], len(s["domain"]))
    plan["rows"] = per * plan["prefixes"]
    return plan


def predicates(facts, key, plans_keys, dropped_fks=()):
    """C: the table's own predicates, as [(label, constraint, sql)] (constraint: the name to
    drop if the server cannot evaluate it; None for a partition bound): CHECK expressions, the
    partition
    constraint (a partition's own; for a partitioned table, any partition's unless one is
    DEFAULT), and foreign keys to loaded tables as "exists in the parent"."""
    tables = {(t["schema"], t["name"]): t for t in facts.get("tables") or []}
    t = tables.get(key) or {}
    out = []
    for c in t.get("checks") or []:
        out.append(("check " + c["name"], c["name"], c["expr"]))
    if t.get("partition_constraint"):
        out.append(("partition " + key[1], None, t["partition_constraint"]))
    kids = [x for k, x in sorted(tables.items()) if x.get("parent") == key[1] and
            k[0] == key[0]]
    if kids and not any(x.get("is_default") for x in kids):
        parts = [x["partition_constraint"] for x in kids if x.get("partition_constraint")]
        if parts:
            out.append(("partitions of " + key[1], None, "(%s)" % " OR ".join(parts)))
    dropped = {fk["name"] for fk in dropped_fks}
    for fk in facts.get("fks") or []:
        if (fk["schema"], fk["table"]) != key or fk["name"] in dropped:
            continue
        parent = (fk["ref_schema"], fk["ref_table"])
        if parent not in plans_keys:
            continue
        cols = ", ".join(qi(c) for c in fk["cols"])
        out.append(("foreign key " + fk["name"], fk["name"],
                    "(%s OR (%s) IN (SELECT %s FROM %s))" % (
            " OR ".join("%s IS NULL" % qi(c) for c in fk["cols"]), cols,
            ", ".join(qi(c) for c in fk["ref_cols"]),
            qn(fk["ref_table"] if parent[0] == "public" else "%s.%s" % parent))))
    return out


def load_sql(plan, preds, cand="ybm_cand"):
    """C: [drop, create candidates, filtered insert]. The candidates are the sample rows
    repeated (COPIES, more for a small table): every copy keeps the key values; copy 0 is the
    sample row, later copies shift the other columns. Per key, the first candidate the
    predicates admit loads."""
    cols, exprs, joins = [], [], []
    sizes = plan["range_sizes"]
    n_rows = plan["rows"]
    copies = max(COPIES, -(-CANDIDATES // n_rows))
    for j, (c, s) in enumerate(plan["shape"].items()):
        if not s["domain"]:
            continue
        n = len(s["domain"])
        alias = "d%d" % s["comp"]
        if alias not in [j.split()[-1] for j in joins]:
            joins.append("(SELECT vals FROM ybm_dom WHERE comp = %d) %s" % (s["comp"], alias))
        base = "(g.n %% %d)" % n_rows
        copy = "(g.n / %d)" % n_rows
        if s["role"] == "p":
            idx = "(%s / %d)" % (base, plan["per_prefix"])
        elif s["role"].startswith("r:"):
            k = int(s["role"][2:])
            div = 1
            for x in sizes[k + 1:]:
                div *= x
            idx = "((%s %% %d) / %d)" % (base, plan["per_prefix"], div)
        elif s["role"] == "n":
            idx = "g.n"
        else:  # cycles within each prefix; copy 0 is the sample row, copy k > 0 shifts each
            # column by its own hashed offset, so the copies pair different values of two
            # columns (a multi-column CHECK finds a pair that holds)
            idx = ("(%s %% %d + CASE WHEN %s = 0 THEN 0 ELSE hashtext(%s || ':%d')::bigint "
                   "& 2147483647 END)" % (base, plan["per_prefix"], copy, copy, j))
        e = "%s.vals[(%s) %% %d + 1]::%s" % (alias, idx, n, s["type"])
        if s["null_frac"] > 0:  # exactly that fraction of rows, spread evenly; each column
            # rotated differently so two columns' NULLs do not fall on the same rows
            k = "((%s * 7919 + %d) %% %d)" % (base, j * 104729, n_rows)
            e = "CASE WHEN floor((%s + 1) * %r) > floor(%s * %r) THEN NULL ELSE %s END" % (
                k, s["null_frac"], k, s["null_frac"], e)
        cols.append(qi(c))
        exprs.append("%s AS %s" % (e, qi(c)))
    where = " AND ".join("(%s) IS NOT FALSE" % p for _, _, p in preds) or "true"
    return [
        "DROP TABLE IF EXISTS %s;" % cand,
        "CREATE TABLE %s AS SELECT g.n AS ybm_n, %s FROM generate_series(0, %d) AS g(n)%s;"
        % (cand, ", ".join(exprs), n_rows * copies - 1,
           "".join(" CROSS JOIN " + j for j in joins)),
        "INSERT INTO %s (%s) OVERRIDING SYSTEM VALUE SELECT %s FROM (SELECT DISTINCT ON "
        "(ybm_n %% %d) * FROM %s WHERE %s ORDER BY ybm_n %% %d, ybm_n) c ON CONFLICT DO NOTHING;"
        % (qn(plan["table"]), ", ".join(cols), ", ".join("c.%s" % c for c in cols), n_rows,
           cand, where, n_rows)]


def rejects_sql(preds, cand="ybm_cand"):
    """One query: how many candidates each predicate rejects (JSON)."""
    if not preds:
        return None
    return "SELECT json_build_object(%s) FROM %s;" % (", ".join(
        "%s, count(*) FILTER (WHERE (%s) IS FALSE)" % (_quote(label), p)
        for label, _, p in preds), cand)


SHAPE_TOLERANCE = 2.0  # a distinct count within this factor of the plan's is the plan's
NULL_TOLERANCE = 0.1   # and a null fraction within this much


def expected_shape(plan):
    """D: column -> (distinct values, null fraction) the plan meant the loaded sample to have."""
    out = {}
    for c, s in plan["shape"].items():
        if not s["domain"]:
            continue
        n = len(s["domain"])
        if s["role"] == "p":
            nd = min(n, plan["prefixes"])
        elif s["role"].startswith("r:"):
            nd = plan["range_sizes"][int(s["role"][2:])]
        elif s["role"] == "n":
            nd = min(n, plan["rows"])
        else:
            nd = min(n, plan["per_prefix"])
        # only the rows that are not NULL hold values
        nd = min(nd, -(-plan["rows"] * (1000 - int(s["null_frac"] * 1000)) // 1000))
        out[c] = (nd, s["null_frac"])
    return out


def verify_shape(container, db, plan, key):
    """D: count each column's distinct values and NULLs in the loaded table (exact counts, not
    ANALYZE: statistics would change the plans being measured when the bundle has none to
    inject) and compare them with the plan's. Returns [gap] (layer gen.shape); one gap without
    a column when the server gave no counts."""
    exp = expected_shape(plan)
    if not exp:
        return []
    cols = sorted(exp)
    r = container.sql("SELECT json_build_object('rows', count(*), %s) FROM %s;" % (
        ", ".join("%s, json_build_array(count(DISTINCT %s::text), count(*) - count(%s))" % (
            _quote(c), qi(c), qi(c)) for c in cols), qn(plan["table"])), db=db, flags=("-tA",))
    txt = (r.stdout or "").strip()
    try:
        got = json.loads(txt[txt.index("{"):]) if "{" in txt else None
    except ValueError:
        got = None
    if not got:
        return [{"layer": "gen.shape", "table": plan["table"], "model": "column counts",
                 "server": ((r.stderr or "") + txt).strip()[:200] or "no result"}]
    rows = max(got.get("rows") or 0, 1)
    gaps = []
    for c in cols:
        nd, nf = exp[c]
        actual, nulls = got[c]
        off = {}
        if not nd / SHAPE_TOLERANCE <= actual <= nd * SHAPE_TOLERANCE:
            off.update(distinct=actual, planned_distinct=nd)
        if abs(nulls / rows - nf) > NULL_TOLERANCE + 1.0 / rows:
            off.update(null_frac=round(nulls / rows, 2), planned_null_frac=round(nf, 2))
        if off:  # counts as numbers: they are the sample's shape, not values
            gaps.append(dict({"layer": "gen.shape", "table": plan["table"], "model": c}, **off))
    return gaps


def plan_flip(replayed, measured):
    """D: the scans (node, index) the measured plan uses that the replayed plan does not, or
    []. A partitioned read may scan fewer partitions once values are bound, so only a scan the
    replay never chose counts."""
    from . import server
    def sig(plan_json):
        return {(s["node"], s["index"]) for s in server.scans(plan_json)}
    try:
        return sorted(sig(measured) - sig(replayed), key=str)
    except (KeyError, IndexError, TypeError):
        return []


def sort_domains(container, db, plans, columns):
    """The values of each (table, column) in `columns`, in the order of the column's type, as
    the server sorts them: shape["sorted"]. A range read binds its bounds from it."""
    by_table = {}
    for t, c in columns:
        s = (plans.get(t) or {}).get("shape", {}).get(c)
        if s and s["domain"]:
            by_table.setdefault(t, []).append(c)
    for t, cols in sorted(by_table.items()):
        shape = plans[t]["shape"]
        r = container.sql("SELECT json_build_object(%s);" % ", ".join(
            "%s, (SELECT json_agg(v ORDER BY v::%s) FROM unnest(ARRAY[%s]::text[]) v)" % (
                _quote(c), shape[c]["type"], ", ".join(_quote(v) for v in shape[c]["domain"]))
            for c in sorted(cols)), db=db, flags=("-tA",))
        txt = (r.stdout or "").strip()
        try:
            got = json.loads(txt[txt.index("{"):]) if "{" in txt else {}
        except ValueError:
            got = {}
        for c in cols:
            if got.get(c):
                shape[c]["sorted"] = got[c]


def neutral_values(container, db, types):
    """type name -> the server's value 0 of that type (ybm_value), for parameters no condition
    places."""
    types = sorted({t for t in types if t})
    if not types:
        return {}
    r = container.sql("SELECT json_object_agg(t, ybm_value(t::regtype::oid, 0)) FROM "
                      "unnest(ARRAY[%s]::text[]) t;" % ", ".join(_quote(t) for t in types),
                      db=db, flags=("-tA",))
    txt = (r.stdout or "").strip()
    try:
        return json.loads(txt[txt.index("{"):]) if "{" in txt else {}
    except ValueError:
        return {}
