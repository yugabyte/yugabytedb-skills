"""The server as oracle: read what the scratch YugabyteDB says about a statement, and compare it
with what the engine's own model predicted.

The engine keeps its Python models (sqlshape, binding_map, the measured pass's binder) as
predictions. Where replay ran, the server's answer for the same input is read here and used,
and every disagreement is recorded as a gap: {layer, pattern, table, index, model, server}.

The server's deparsed conditions (EXPLAIN VERBOSE: Index Cond, Filter, Storage Filter, Storage
Index Filter, Recheck Cond) are a closed, fully parenthesised grammar, so they are read with a
small reader of their own, not with sqlshape (the model under check). An atom it cannot read is
kept as residue, never guessed.
"""

import re

COND_KEYS = ("Index Cond", "Recheck Cond", "Filter", "Storage Filter", "Storage Index Filter",
             "Remote Filter")
FILTER_KEYS = ("Filter", "Storage Filter", "Storage Index Filter", "Remote Filter")
_CAST = r"(?:::[a-z_][\w ]*(?:\[\])?)"
_REF = r"\(?(?:(?P<qual>[A-Za-z_][\w$]*|\"(?:[^\"]|\"\")+\")\.)?(?P<col>[A-Za-z_][\w$]*|\"(?:[^\"]|\"\")+\")\)?%s?" % _CAST
_OPS = {"=": "eq", "<>": "ne", "<": "range", ">": "range", "<=": "range", ">=": "range",
        "~~": "like", "~~*": "like", "!~~": "ne", "!~~*": "ne"}


def _strip(s):
    s = s.strip()
    while s.startswith("(") and _closing(s, 0) == len(s) - 1:
        s = s[1:-1].strip()
    return s


def _closing(s, i):
    """Index of the parenthesis closing s[i], skipping quoted text."""
    depth, k, q = 0, i, None
    while k < len(s):
        ch = s[k]
        if q:
            if ch == q:
                if k + 1 < len(s) and s[k + 1] == q:
                    k += 2
                    continue
                q = None
        elif ch in "'\"":
            q = ch
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
            if depth == 0:
                return k
        k += 1
    return -1


def _split(s, word):
    """Split s on top-level ` word ` (AND / OR), outside parentheses and quotes."""
    parts, depth, q, start, k = [], 0, None, 0, 0
    w = " %s " % word
    while k < len(s):
        ch = s[k]
        if q:
            if ch == q:
                if k + 1 < len(s) and s[k + 1] == q:
                    k += 2
                    continue
                q = None
        elif ch in "'\"":
            q = ch
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif depth == 0 and s[k:k + len(w)].upper() == w:
            parts.append(s[start:k])
            start = k + len(w)
            k = start
            continue
        k += 1
    parts.append(s[start:])
    return [p.strip() for p in parts if p.strip()]


def _split_commas(s):
    parts, depth, q, cur = [], 0, None, ""
    for ch in s:
        if q:
            cur += ch
            if ch == q:
                q = None
            continue
        if ch in "'\"":
            q = ch
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
            continue
        cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return parts


def constants(expr):
    """column -> the constants an expression of the server's (a CHECK, a partition constraint)
    compares it with: equality, IN lists and range bounds. These are schema text, not data."""
    out = {}
    for a, _ in atoms(read(expr or "")):
        if "col" not in a:
            continue
        vals = [a["value"]] if a.get("kind") == "literal" else \
            list(a["value"]) if a.get("kind") == "literals" else []
        if a.get("kind") == "literal" and a.get("sym") in ("<", "<=") and a["op"] == "range":
            vals = []  # an upper bound: the value itself may be outside (exclusive bound)
        for v in vals:
            if v not in out.setdefault(a["col"], []):
                out[a["col"]].append(v)
    return {c: v for c, v in out.items() if v}


def scrub(value):
    """A gap record's text with its constants replaced: quoted literals by '?', numbers by N.
    Gap records describe shapes (columns, operators, counts), never values: every one goes
    through this before it leaves the engine."""
    if isinstance(value, str):
        out = re.sub(r"'(?:[^']|'')*'", "'?'", value)
        # A server error's row values ("Failing row contains (...)", "Key (a)=(...)").
        out = re.sub(r"(Failing row contains|\)=)\s*\(.*", r"\1 (...)", out)
        return re.sub(r"(?<![\w$.:])-?\d+(?:\.\d+)?(?![\w.])", "N", out)
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    return value


# Fields of a record that name things the engine itself chose (layers, ids, schema objects,
# counts): kept. Everything else is text that may carry a value, and is scrubbed.
_NAMES = ("layer", "pattern", "table", "index", "param", "constraint", "id", "kind")


def scrub_record(rec):
    """A gap record, or the measured pass's result, as it may leave the engine: its free text
    scrubbed (scrub), its names and numbers kept."""
    if isinstance(rec, dict):
        return {k: (v if k in _NAMES or not isinstance(v, (str, list, dict)) else
                    scrub_record(v) if isinstance(v, dict) else scrub(v))
                for k, v in rec.items()}
    return scrub(rec)


def _name(raw):
    return raw[1:-1].replace('""', '"') if raw.startswith('"') else raw.lower()


def _operand(text):
    """('param', n) | ('params', [n..]) | ('literal', text) | ('column', col) | ('other', text)."""
    t = text.strip()
    m = re.fullmatch(r"\(?\$(\d+)\)?%s?" % _CAST, t)
    if m:
        return "param", int(m.group(1))
    m = re.fullmatch(r"('(?:[^']|'')*')%s?" % _CAST, t)
    if m:
        return "literal", m.group(1)
    m = re.fullmatch(r"-?\d+(?:\.\d+)?%s?" % _CAST, t)
    if m:
        return "literal", "'%s'" % re.match(r"-?\d+(?:\.\d+)?", t).group(0)
    m = re.fullmatch(r"(?:ANY|ALL)\s*\((.*)\)", t, re.S)
    if m:
        inner = m.group(1).strip()
        ps = [int(x) for x in re.findall(r"\$(\d+)", inner)]
        if ps:
            return "params", ps
        lit = re.fullmatch(r"('(?:[^']|'')*')%s?" % _CAST, inner)
        if lit:
            return "array", lit.group(1)
        arr = re.fullmatch(r"ARRAY\[(.*)\]%s?" % _CAST, inner, re.S)
        if arr:  # ARRAY['a'::text, 'b'::text]: each element a literal
            items = [_operand(x) for x in _split_commas(arr.group(1))]
            if items and all(k == "literal" for k, _ in items):
                return "literals", [v for _, v in items]
        return "other", inner
    m = re.fullmatch(_REF, t)
    if m:
        return "column", _name(m.group("col"))
    m = re.fullmatch(r"\(?([a-z_][\w$]*)\((.*)\)\)?%s?" % _CAST, t, re.S)
    if m and re.fullmatch(_REF, m.group(2).strip()):  # f(col), e.g. yb_hash_code(t.id)
        return "expr", "%s(%s)" % (m.group(1), _name(re.fullmatch(_REF, m.group(2).strip())
                                                       .group("col")))
    return "other", t


def statement(query):
    """The statement to hand the server, and the rewrites made to get it: a query wrapped in
    COPY (...) TO is the query; a three-part name (database.schema.table) loses its database,
    which YSQL refuses ("cross-database references are not implemented")."""
    q = query.strip().rstrip(";")
    notes = []
    m = re.match(r"(?is)^COPY\s*\(", q)
    if m:
        end = _closing(q, m.end() - 1)
        if end > 0:
            q = q[m.end():end].strip()
            notes.append("COPY (query) TO: the query")
    ident = r'(?:[A-Za-z_][\w$]*|"(?:[^"]|"")+")'
    three = re.compile(r"(?<![\w$.\"])%s\.(%s\.%s)" % (ident, ident, ident))
    outside = re.split(r"('(?:[^']|'')*')", q)
    for k in range(0, len(outside), 2):
        new = three.sub(r"\1", outside[k])
        if new != outside[k]:
            outside[k] = new
            if "database.schema.table: schema.table" not in notes:
                notes.append("database.schema.table: schema.table")
    return "".join(outside), notes


def atom(text):
    """One comparison: {col, op, value_kind, value}, a test on a parameter {param, op}, or
    {residue: text}."""
    a = _strip(text)
    m = re.fullmatch(r"\(?\$(\d+)\)?%s? IS (NOT )?NULL" % _CAST, a)
    if m:
        return {"param": int(m.group(1)), "op": "notnull" if m.group(2) else "isnull"}
    m = re.fullmatch(r"(.+?) IS (NOT )?NULL", a, re.S)
    if m:
        k, v = _operand(m.group(1))
        if k == "column":
            return {"col": v, "op": "notnull" if m.group(2) else "isnull", "kind": None,
                    "value": None}
        return {"residue": a}
    m = re.fullmatch(r"ROW\((.*?)\)\s*(<=|>=|<|>|=|<>)\s*ROW\((.*)\)", a, re.S)
    if m:
        cols = [_operand(x)[1] for x in m.group(1).split(",")]
        return {"cols": cols, "op": "rowcmp" if m.group(2) != "=" else "eq", "kind": "row",
                "sym": m.group(2), "value": m.group(3)}
    # the operator at depth 0: the first one outside parentheses and quotes
    depth, q, k = 0, None, 0
    while k < len(a):
        ch = a[k]
        if q:
            if ch == q:
                q = None
        elif ch in "'\"":
            q = ch
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif depth == 0 and ch == " ":
            m = re.match(r" (=|<>|<=|>=|<|>|~~\*|~~|!~~\*|!~~) ", a[k:])
            if m:
                lhs, op, rhs = a[:k], m.group(1), a[k + len(m.group(0)):]
                lk, lv = _operand(lhs)
                rk, rv = _operand(rhs)
                if lk not in ("column", "expr") and rk in ("column", "expr"):  # $1 < col
                    lk, lv, rk, rv = rk, rv, lk, lv
                    op = {"<": ">", ">": "<", "<=": ">=", ">=": "<="}.get(op, op)
                if lk == "expr":  # a function of a column: no key column, its parameters known
                    return {"expr": lv, "op": _OPS[op], "sym": op, "kind": rk, "value": rv}
                if lk != "column":
                    return {"residue": a}
                kind = rk
                opname = _OPS[op]
                if rk in ("params", "array", "literals") and op == "=":
                    opname = "in"
                if rk == "column":
                    opname = "join"
                    # both sides are columns, never swapped: keep their text for qualifiers
                    return {"col": lv, "op": opname, "sym": op, "kind": kind, "value": rv,
                            "text_l": lhs, "text_r": rhs}
                return {"col": lv, "op": opname, "sym": op, "kind": kind, "value": rv}
        k += 1
    # No comparison the binding codes know: any other operator between a column and an operand
    # (@>, &&, ?, @@ ...) is one atom, op "other" with its symbol. Never more grammar: what the
    # server put it in (Index Cond or a filter) says whether the index serves it.
    gen = _generic(a)
    if gen:
        return gen
    return {"residue": a}


_OP_TOKEN = re.compile(r" ([-+*/<>=~!@#%^&|`?]+) ")


def _generic(a):
    depth, q = 0, None
    for k, ch in enumerate(a):
        if q:
            if ch == q:
                q = None
        elif ch in "'\"":
            q = ch
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif depth == 0 and ch == " ":
            m = _OP_TOKEN.match(a, k)
            if not m:
                continue
            lk, lv = _operand(a[:k])
            rk, rv = _operand(a[m.end():])
            if lk == "column":
                return {"col": lv, "op": "other", "sym": m.group(1), "kind": rk, "value": rv}
            if rk == "column":
                return {"col": rv, "op": "other", "sym": m.group(1), "kind": lk, "value": lv}
            return None
    return None


def read(cond):
    """A deparsed condition as a tree: ("and", [..]) | ("or", [..]) | atom dict."""
    s = _strip(cond or "")
    if not s:
        return ("and", [])
    ors = _split(s, "OR")
    if len(ors) > 1:
        return ("or", [read(x) for x in ors])
    ands = _split(s, "AND")
    if len(ands) > 1:
        return ("and", [read(x) for x in ands])
    if s.upper().startswith("NOT "):
        return {"residue": s}
    return atom(s)


def atoms(tree):
    """Every atom of a tree, with whether it sits under an OR."""
    out = []

    def walk(t, under_or):
        if isinstance(t, dict):
            out.append((t, under_or))
        else:
            for c in t[1]:
                walk(c, under_or or t[0] == "or")
    walk(tree, False)
    return out


def conjuncts(tree):
    """The atoms a tree ANDs together at its top level (an OR group is not one)."""
    if isinstance(tree, dict):
        return [tree]
    if tree[0] == "and":
        out = []
        for c in tree[1]:
            if isinstance(c, dict):
                out.append(c)
            elif c[0] == "and":
                out.extend(conjuncts(c))
        return out
    return []


# --- the plan's binding of predicates to key columns ----------------------------------------

def _walk(node, out):
    out.append(node)
    for ch in node.get("Plans", []) or []:
        _walk(ch, out)
    return out


def scans(plan_json):
    """Scan nodes of a plan: {table, alias, index, node, index_cond: [trees], filters: [trees],
    bitmap: bool}. Bitmap index scans under one heap scan are folded into it."""
    top = plan_json[0]["Plan"] if isinstance(plan_json, list) else plan_json["Plan"]
    nodes = _walk(top, [])
    out = []
    for n in nodes:
        t = n.get("Node Type", "")
        if t in ("Bitmap Index Scan",):
            continue
        if "Scan" not in t or not n.get("Relation Name"):
            continue
        entry = {"table": n.get("Relation Name"), "alias": n.get("Alias"),
                 "schema": n.get("Schema"), "node": t, "index": n.get("Index Name"),
                 "index_cond": [], "filters": [], "bitmap": "Bitmap" in t}
        if n.get("Index Cond"):
            entry["index_cond"].append(read(n["Index Cond"]))
        for k in FILTER_KEYS:
            if n.get(k):
                entry["filters"].append(read(n[k]))
        if entry["bitmap"]:
            kids = [c for c in _walk(n, [])[1:] if c.get("Node Type") == "Bitmap Index Scan"]
            entry["index"] = kids[0].get("Index Name") if kids else None
            entry["arms"] = [read(c.get("Index Cond")) for c in kids
                             if c.get("Index Name") == entry["index"]]
            entry["index_cond"] = []
        out.append(entry)
    return out


def server_map(scan, index):
    """The plan's binding of `index`'s key columns, in the codes binding_map uses: eq, in,
    range, or_eq (every BitmapOr arm binds it), op (another operator the index's operator
    class serves, in the Index Cond), filter (named only in a filter), none."""
    filtered = {a["col"] for f in scan["filters"] for a, _ in atoms(f) if "col" in a}

    def seek(a):  # a join condition in an Index Cond is an equality seek on each outer row;
        # any other operator there is served by the index's operator class (code "op")
        return ({"join": "eq", "other": "op"}.get(a["op"], a["op"]),
                str(a.get("value", a.get("text_r"))))
    if scan.get("bitmap"):
        arms = [{a["col"]: seek(a) for a in conjuncts(t) if "col" in a}
                for t in scan.get("arms") or []]
    else:
        arms = [{a["col"]: seek(a) for t in scan["index_cond"]
                 for a in conjuncts(t) if "col" in a}]
    cols = []
    for k in index.keys:
        c = k.col or None  # as the plan reader names it: a quoted identifier keeps its case
        ops = [arm.get(c) for arm in arms] if c else []
        if c and ops and all(o and o[0] in ("eq", "in", "range", "op") for o in ops):
            if len(arms) == 1 or len(set(ops)) == 1:
                code = ops[0][0]  # one arm, or every arm binds the same value
            else:
                code = "or_eq" if all(o[0] == "eq" for o in ops) else "in"
        elif c and (c in filtered or any(o for o in ops)):
            code = "filter"
        else:
            code = "none"
        cols.append({"col": k.label, "code": code})
    return cols


_SEEK = ("eq", "in", "range", "op")


def compare_maps(model_cols, server_cols):
    """Key columns the model and the server bind differently: the model says the column is
    sought (eq / in / range) and the plan only filters it, or the other way round. A column
    the model binds in every OR arm (or_eq) is bound only when the planner builds a BitmapOr;
    either answer agrees with it, and the server's settles it."""
    out = []
    for m, s in zip(model_cols, server_cols):
        mk, sk = m["code"], s["code"]
        if mk == "or_eq":
            continue
        if (mk in _SEEK) != (sk in _SEEK + ("or_eq",)):
            out.append({"col": m["col"], "model": mk, "server": sk})
    return out


def params(plan_json, table=None):
    """$n -> (col, op) from every condition of a plan (scan nodes of `table` when given), and
    column -> literals compared with it by equality or IN."""
    pmap, lits = {}, {}
    for sc in scans(plan_json):
        if table and sc["table"] != table:
            continue
        trees = sc["index_cond"] + sc["filters"] + list(sc.get("arms") or [])
        for t in trees:
            for a, _ in atoms(t):
                if a.get("kind") == "row":  # ROW(a, b) < ROW($3, $4): each $n to its column
                    for c, v in zip(a["cols"], a["value"].split(",")):
                        k, n = _operand(v)
                        if k == "param":
                            pmap.setdefault(n, (c, a["sym"]))
                    continue
                if "expr" in a and a.get("kind") == "param":
                    pmap.setdefault(a["value"], ("expr:" + a["expr"], a["sym"]))
                    continue
                if "col" not in a:
                    continue
                if a["kind"] == "param":
                    pmap.setdefault(a["value"], (a["col"], a["sym"] if a["op"] in (
                        "eq", "range") else a["op"]))
                elif a["kind"] == "params":
                    for k, n in enumerate(a["value"]):
                        pmap.setdefault(n, (a["col"], "in:%d" % k))
                elif a["kind"] == "literal" and a["op"] in ("eq", "in"):
                    if a["value"] not in lits.setdefault(a["col"], []):
                        lits[a["col"]].append(a["value"])
    return pmap, lits


JOIN_KEYS = ("Hash Cond", "Merge Cond", "Join Filter", "Index Cond", "Recheck Cond", "Filter",
             "Storage Filter", "Storage Index Filter")


def _ref(text):
    """(qualifier, column) of a column reference, or None."""
    m = re.fullmatch(_REF, text.strip())
    if not m:
        return None
    return (_name(m.group("qual")) if m.group("qual") else None, _name(m.group("col")))


def joins(plan_json):
    """Pairs ((table, col), (table, col)) a plan compares by equality across two relations
    (hash, merge and nested-loop join conditions), tables resolved through the plan's aliases."""
    top = plan_json[0]["Plan"] if isinstance(plan_json, list) else plan_json["Plan"]
    nodes = _walk(top, [])
    alias = {}
    for n in nodes:
        if n.get("Relation Name"):
            alias[(n.get("Alias") or n["Relation Name"]).lower()] = n["Relation Name"]
    out = []
    for n in nodes:
        for key in JOIN_KEYS:
            if not n.get(key):
                continue
            for a, _ in atoms(read(n[key])):
                if a.get("op") != "join" or a.get("sym") != "=":
                    continue
                left, right = _ref(a["text_l"]), _ref(a["text_r"])
                if not left or not right or not left[0] or not right[0]:
                    continue
                lt, rt = alias.get(left[0]), alias.get(right[0])
                if lt and rt and (lt, left[1]) != (rt, right[1]):
                    pair = tuple(sorted([(lt, left[1]), (rt, right[1])]))
                    if pair not in out:
                        out.append(pair)
    return out


def residue(plan_json):
    """Condition atoms the reader could not read (a gap in this reader, recorded)."""
    out = []
    for sc in scans(plan_json):
        for t in sc["index_cond"] + sc["filters"] + list(sc.get("arms") or []):
            out.extend(a["residue"] for a, _ in atoms(t) if "residue" in a)
    return out
