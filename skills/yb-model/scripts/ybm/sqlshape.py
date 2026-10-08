"""Extract the access shape of a query: which columns of which tables are filtered, joined,
ordered and projected. Deterministic and deliberately conservative: anything it cannot
classify is recorded as 'other' rather than guessed.
"""

from .sqltok import (tokenize, split_statements, match_paren, split_top, text_of, expr_of,
                     is_word, rebase, rel_key)

CLAUSES = ("SELECT", "FROM", "WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "OFFSET", "FETCH",
           "FOR", "UNION", "INTERSECT", "EXCEPT", "RETURNING", "SET", "VALUES", "USING",
           "WINDOW")
JOIN_WORDS = ("JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "NATURAL", "OUTER", "LATERAL")
RANGE_OPS = ("<", ">", "<=", ">=")
FLIP = {"<": ">", ">": "<", "<=": ">=", ">=": "<="}
NOT_COLS = {"true", "false", "null", "now", "current_timestamp", "current_date", "interval",
            "and", "or", "not", "case", "when", "then", "else", "end", "as", "distinct",
            "count", "sum", "min", "max", "avg", "coalesce", "exists", "in", "is", "like",
            "between", "cast", "extract", "from", "over", "partition", "by", "asc", "desc",
            "limit", "select", "where", "localtimestamp", "current_user", "default"}


class Pred:
    """op: eq | in | range | prefix | isnull | notnull | ne | like | rowcmp | or | other"""

    def __init__(self, table, col, op, expr=None):
        self.table = table
        self.col = col
        self.op = op
        self.expr = expr

    def to_dict(self):
        return {"table": self.table, "col": self.col, "op": self.op, "expr": self.expr}


class Shape:
    def __init__(self, kind):
        self.kind = kind            # select | insert | update | delete | utility
        self.refs = []              # [(alias, table)] for real tables (CTEs excluded)
        self.preds = []             # [Pred]
        self.joins = []             # [(t1, c1, t2, c2)]
        self.order = []             # [(table, col, dir)]
        self.limit = False
        self.select_all = set()     # tables projected with * or t.*
        self.select_cols = set()    # (table, col)
        self.group_by = False
        self.aggregate = False      # select list has an aggregate (count, sum, ...)
        self.set_cols = set()       # update targets
        self.conflict_cols = []
        self.conflict_where = None       # ON CONFLICT (cols) WHERE pred, as text_of writes it
        self.conflict_constraint = None  # ON CONFLICT ON CONSTRAINT name
        self.subshapes = []
        self.unresolved = []        # column names that could not be tied to a table
        self.copy = False           # COPY (SELECT ...) TO: a bulk export, not a lookup

    @property
    def tables(self):
        return sorted({t for _, t in self.refs})

    def preds_for(self, table):
        return [p for p in self.preds if p.table == table]

    def to_dict(self):
        return {"kind": self.kind, "tables": self.tables,
                "preds": [p.to_dict() for p in self.preds],
                "joins": [list(j) for j in self.joins],
                "order": [list(o) for o in self.order], "limit": self.limit,
                "select_all": sorted(self.select_all),
                "select_cols": sorted([list(x) for x in self.select_cols]),
                "group_by": self.group_by, "aggregate": self.aggregate, "set_cols": sorted(self.set_cols),
                "conflict_cols": self.conflict_cols,
                **({"conflict_where": self.conflict_where} if self.conflict_where else {}),
                **({"conflict_constraint": self.conflict_constraint}
                   if self.conflict_constraint else {}),
                "subshapes": [s.to_dict() for s in self.subshapes],
                "unresolved": sorted(set(self.unresolved))}


def _clause_positions(st, kind="select"):
    pos = {}
    for k, t in enumerate(st):
        if kind != "delete" and is_word(t, "USING"):
            continue
        if t.depth == 0 and t.kind == "word" and t.up in CLAUSES and t.up not in pos:
            if t.up in ("ORDER", "GROUP") and not (k + 1 < len(st) and is_word(st[k + 1], "BY")):
                continue
            pos[t.up] = k
    return pos


def _clause(st, pos, name):
    if name not in pos:
        return []
    start = pos[name] + 1
    if name in ("ORDER", "GROUP"):
        start += 1
    nxt = [v for v in pos.values() if v > pos[name]]
    end = min(nxt) if nxt else len(st)
    return st[start:end]


class _Ctx:
    def __init__(self, schema, shape, ctes):
        self.schema = schema
        self.shape = shape
        self.ctes = ctes
        self.alias = {}   # alias -> table (None for CTE/subquery)

    def rel(self, qual, name):
        """The table key a query's (qualifier, name) refers to."""
        if self.schema is not None and hasattr(self.schema, "rel"):
            return self.schema.rel(qual, name)
        return rel_key(qual, name)

    def table_cols(self, table):
        t = self.schema.tables.get(table) if self.schema else None
        return t.cols if t else None

    def resolve(self, qual, col):
        if qual is not None:
            if qual in self.alias:
                return self.alias[qual]
            return None
        cands = []
        for a, t in self.alias.items():
            if t is None:
                continue
            cols = self.table_cols(t)
            if cols is None or col in cols:
                cands.append(t)
        cands = sorted(set(cands))
        if len(cands) == 1:
            return cands[0]
        if not cands and len({t for t in self.alias.values() if t}) == 1:
            return [t for t in self.alias.values() if t][0]
        self.shape.unresolved.append(col)
        return None


def _colref(toks):
    """Return (qualifier, column) if toks is exactly a column reference, possibly cast."""
    core = toks
    if len(core) >= 3 and core[-2].kind == "op" and core[-2].text == "::":
        core = core[:-2]
    if len(core) == 1 and core[0].ident is not None and core[0].kind in ("word", "qident") \
            and core[0].ident not in NOT_COLS:
        return None, core[0].ident
    if len(core) == 3 and core[1].kind == "." and core[0].ident and core[2].ident:
        return core[0].ident, core[2].ident
    return None


def _colrefs_in(toks):
    """All column-looking references in a token run, as (qualifier, col)."""
    out = []
    k = 0
    while k < len(toks):
        t = toks[k]
        if t.ident is not None and t.kind in ("word", "qident"):
            if k + 2 < len(toks) and toks[k + 1].kind == "." and toks[k + 2].ident is not None:
                if toks[k + 2].kind == "op":
                    k += 2
                    continue
                out.append((t.ident, toks[k + 2].ident))
                k += 3
                continue
            nxt = toks[k + 1] if k + 1 < len(toks) else None
            prev = toks[k - 1] if k > 0 else None
            if (nxt is None or nxt.kind != "(") and t.ident not in NOT_COLS and \
                    not (prev is not None and is_word(prev, "AS")) and \
                    not (prev is not None and prev.kind == "op" and prev.text == "::"):
                out.append((None, t.ident))
        k += 1
    return out


def _has_colref(toks):
    return any(True for _ in _colrefs_in(toks))


def _split_and(toks):
    """Split a boolean expression on top-level AND, keeping BETWEEN x AND y together."""
    if not toks:
        return []
    d = min(t.depth for t in toks)
    parts, cur, between = [], [], False
    for t in toks:
        if t.depth == d and is_word(t, "BETWEEN"):
            between = True
        if t.depth == d and is_word(t, "AND"):
            if between:
                between = False
            else:
                parts.append(cur)
                cur = []
                continue
        cur.append(t)
    if cur:
        parts.append(cur)
    return parts


def _strip_parens(toks):
    while toks and toks[0].kind == "(" and match_paren(toks, 0) == len(toks) - 1:
        toks = rebase(toks[1:-1])
    return toks


def _add_pred(ctx, qual, col, op, expr=None):
    table = ctx.resolve(qual, col) if col else None
    if col and table is None:
        return
    ctx.shape.preds.append(Pred(table, col, op, expr))


def _expr_pred(ctx, toks, op):
    refs = _colrefs_in(toks)
    tables = {ctx.resolve(q, c) for q, c in refs}
    tables.discard(None)
    if len(tables) == 1:
        ctx.shape.preds.append(Pred(tables.pop(), None, op, expr_of(_unqualify(toks))))


def _unqualify(toks):
    out, k = [], 0
    while k < len(toks):
        if k + 2 < len(toks) and toks[k].ident and toks[k + 1].kind == "." and toks[k + 2].ident:
            out.append(toks[k + 2])
            k += 3
            continue
        out.append(toks[k])
        k += 1
    return out


def _conjunct(ctx, toks):
    toks = _strip_parens(rebase(toks))
    if not toks:
        return
    d = 0
    if any(t.depth == d and is_word(t, "AND") for t in toks) and \
            not any(t.depth == d and is_word(t, "BETWEEN") for t in toks):
        for p in _split_and(toks):
            _conjunct(ctx, p)
        return
    if any(t.depth == d and is_word(t, "OR") for t in toks):
        for q, c in _colrefs_in(toks):
            _add_pred(ctx, q, c, "or")
        return
    if is_word(toks[0], "NOT"):
        for q, c in _colrefs_in(toks):
            _add_pred(ctx, q, c, "ne")
        return
    if is_word(toks[0], "EXISTS"):
        return
    # Locate the operator at depth 0.
    for k, t in enumerate(toks):
        if t.depth != 0:
            continue
        if is_word(t, "IS"):
            left = toks[:k]
            neg = k + 1 < len(toks) and is_word(toks[k + 1], "NOT")
            isnull = any(is_word(x, "NULL") for x in toks[k + 1:])
            op = ("notnull" if neg else "isnull") if isnull else "other"
            cr = _colref(left)
            if cr:
                _add_pred(ctx, cr[0], cr[1], op)
            return
        if is_word(t, "NOT") and k + 1 < len(toks) and is_word(toks[k + 1], "IN", "LIKE",
                                                                   "ILIKE", "BETWEEN"):
            cr = _colref(toks[:k])
            if cr:
                _add_pred(ctx, cr[0], cr[1], "ne")
            return
        if is_word(t, "IN"):
            left = toks[:k]
            cr = _colref(left)
            if cr:
                _add_pred(ctx, cr[0], cr[1], "in")
            elif left and left[0].kind == "(":
                for q, c in _colrefs_in(left):
                    _add_pred(ctx, q, c, "rowcmp")
            return
        if is_word(t, "BETWEEN"):
            cr = _colref(toks[:k])
            if cr:
                _add_pred(ctx, cr[0], cr[1], "range")
            else:
                _expr_pred(ctx, toks[:k], "range")
            return
        if is_word(t, "LIKE", "ILIKE") or (t.kind == "op" and t.text in ("~~", "~~*")):
            cr = _colref(toks[:k])
            right = toks[k + 1:]
            if cr:
                op = "like"
                if is_word(t, "LIKE") and len(right) == 1 and right[0].kind == "str":
                    lit = right[0].text.strip("'")
                    if lit and lit[0] not in "%_":
                        op = "prefix"
                _add_pred(ctx, cr[0], cr[1], op)
            return
        if t.kind == "op" and t.text in ("=", "<", ">", "<=", ">=", "<>", "!="):
            left, right = toks[:k], toks[k + 1:]
            op = t.text
            any_arr = right and is_word(right[0], "ANY", "SOME")
            lcr, rcr = _colref(left), _colref(right)
            if lcr and rcr:
                lt, rt = ctx.resolve(*lcr), ctx.resolve(*rcr)
                if op == "=" and lt and rt and lt != rt:
                    ctx.shape.joins.append((lt, lcr[1], rt, rcr[1]))
                    ctx.shape.preds.append(Pred(lt, lcr[1], "join"))
                    ctx.shape.preds.append(Pred(rt, rcr[1], "join"))
                return
            if not lcr and rcr and not _has_colref(left):
                lcr, left, right = rcr, right, left
                op = FLIP.get(op, op)
            row = _row_of_cols(left)
            if row and not _has_colref(right):
                # (a, b) < ($1, $2): a row comparison. The planner lists it as an index
                # condition, but DocDB applies it as a recheck, not a seek bound (CAP013), so
                # it binds nothing. An equality binds every column.
                if op == "=":
                    for q, c in row:
                        _add_pred(ctx, q, c, "eq")
                elif op in RANGE_OPS:
                    for q, c in row:
                        _add_pred(ctx, q, c, "rowcmp")
                else:
                    for q, c in row:
                        _add_pred(ctx, q, c, "ne")
                return
            if lcr and not _has_colref(right if not any_arr else right[1:]):
                if op == "=":
                    _add_pred(ctx, lcr[0], lcr[1], "in" if any_arr else "eq")
                elif op in RANGE_OPS:
                    _add_pred(ctx, lcr[0], lcr[1], "range")
                else:
                    _add_pred(ctx, lcr[0], lcr[1], "ne")
                return
            if lcr and _has_colref(right):
                # col = expression(other cols): treat as a join-ish predicate on lcr only.
                _add_pred(ctx, lcr[0], lcr[1], "other")
                return
            if not lcr and _has_colref(left) and not _has_colref(right):
                _expr_pred(ctx, left, "eq" if op == "=" else
                           ("range" if op in RANGE_OPS else "ne"))
                return
            return
        if t.kind == "op" and t.text in ("@>", "<@", "&&", "?", "->>", "->"):
            for q, c in _colrefs_in(toks):
                _add_pred(ctx, q, c, "other")
            return
    for q, c in _colrefs_in(toks):
        _add_pred(ctx, q, c, "other")


def _row_of_cols(toks):
    """[(qualifier, col), ...] when toks is a parenthesised list of plain column references."""
    if len(toks) < 5 or toks[0].kind != "(" or toks[-1].kind != ")":
        return None
    parts, cur, depth = [], [], 0
    for t in toks[1:-1]:
        if t.kind == "(":
            depth += 1
        elif t.kind == ")":
            depth -= 1
        if t.kind == "," and depth == 0:
            parts.append(cur)
            cur = []
        else:
            cur.append(t)
    parts.append(cur)
    refs = [_colref(p) for p in parts]
    return refs if len(refs) > 1 and all(refs) else None


def _parse_from(ctx, toks):
    """Register FROM items and harvest JOIN ... ON predicates."""
    on_parts = []
    items, cur = [], []
    k = 0
    while k < len(toks):
        t = toks[k]
        if t.depth == 0 and (t.kind == "," or is_word(t, *JOIN_WORDS)):
            if cur:
                items.append(cur)
            cur = []
            k += 1
            continue
        if t.depth == 0 and is_word(t, "ON"):
            if cur:
                items.append(cur)
            cur = []
            e = k + 1
            while e < len(toks) and not (toks[e].depth == 0 and (toks[e].kind == "," or
                                                                  is_word(toks[e], *JOIN_WORDS))):
                e += 1
            on_parts.append(toks[k + 1:e])
            k = e
            continue
        if t.depth == 0 and is_word(t, "USING") and k + 1 < len(toks) and toks[k + 1].kind == "(":
            if cur:
                items.append(cur)
            cur = []
            k = match_paren(toks, k + 1) + 1
            continue
        cur.append(t)
        k += 1
    if cur:
        items.append(cur)
    for it in items:
        it = rebase(it)
        if not it:
            continue
        if it[0].kind == "(":
            j = match_paren(it, 0)
            inner = rebase(it[1:j])
            if inner and is_word(inner[0], "SELECT", "WITH", "VALUES"):
                ctx.shape.subshapes.append(_analyze_tokens(inner, ctx.schema, ctx.ctes))
            alias = it[j + 1].ident if j + 1 < len(it) and not is_word(it[j + 1], "AS") else \
                (it[j + 2].ident if j + 2 < len(it) else None)
            if alias:
                ctx.alias[alias] = None
            continue
        if it[0].ident is None:
            continue
        qual, name, ptr = _qualparts(it, 0)
        if ptr < len(it) and it[ptr].kind == "(":
            continue  # set-returning function
        alias = name
        if name not in ctx.ctes or qual:
            name = ctx.rel(qual, name)
        if ptr < len(it) and is_word(it[ptr], "AS"):
            ptr += 1
        if ptr < len(it) and it[ptr].ident and it[ptr].kind in ("word", "qident") and \
                not is_word(it[ptr], "TABLESAMPLE", "WHERE"):
            alias = it[ptr].ident
        if name in ctx.ctes:
            ctx.alias[alias] = None
            continue
        ctx.alias[alias] = name
        ctx.shape.refs.append((alias, name))
    for p in on_parts:
        for c in _split_and(rebase(p)):
            _conjunct(ctx, c)


def _subqueries(ctx, toks):
    k = 0
    while k < len(toks):
        if toks[k].kind == "(" and k + 1 < len(toks) and is_word(toks[k + 1], "SELECT", "WITH"):
            j = match_paren(toks, k)
            ctx.shape.subshapes.append(_analyze_tokens(rebase(toks[k + 1:j]), ctx.schema,
                                                       ctx.ctes))
            k = j + 1
            continue
        k += 1


def _strip_subqueries(toks):
    out, k = [], 0
    while k < len(toks):
        if toks[k].kind == "(" and k + 1 < len(toks) and is_word(toks[k + 1], "SELECT", "WITH"):
            k = match_paren(toks, k) + 1
            continue
        out.append(toks[k])
        k += 1
    return out


def _where(ctx, toks):
    _subqueries(ctx, toks)
    toks = _strip_subqueries(toks)
    for c in _split_and(rebase(toks)):
        _conjunct(ctx, c)


def _analyze_tokens(st, schema, ctes=None):
    ctes = set(ctes or ())
    subs = []
    # WITH name AS ( ... ), ...
    if st and is_word(st[0], "WITH"):
        k = 1
        if is_word(st[k], "RECURSIVE"):
            k += 1
        while k < len(st):
            name = st[k].ident
            k += 1
            if k < len(st) and st[k].kind == "(":
                k = match_paren(st, k) + 1
            if k < len(st) and is_word(st[k], "AS"):
                k += 1
            k = _skip_mat(st, k)
            if k < len(st) and st[k].kind == "(":
                j = match_paren(st, k)
                ctes.add(name)
                inner = rebase(st[k + 1:j])
                subs.append((inner))
                k = j + 1
            if k < len(st) and st[k].kind == ",":
                k += 1
                continue
            break
        st = rebase(st[k:])
    if not st:
        return Shape("utility")
    if is_word(st[0], "COPY") and len(st) > 2 and st[1].kind == "(" and \
            is_word(st[2], "SELECT", "WITH"):
        inner = _analyze_tokens(rebase(st[2:match_paren(st, 1)]), schema, ctes)
        inner.copy = True
        return inner
    head = st[0].up if st[0].kind == "word" else ""
    kind = {"SELECT": "select", "INSERT": "insert", "UPDATE": "update", "DELETE": "delete",
            "VALUES": "select", "TABLE": "select"}.get(head, "utility")
    shape = Shape(kind)
    ctx = _Ctx(schema, shape, ctes)
    for inner in subs:
        shape.subshapes.append(_analyze_tokens(inner, schema, ctes))
    if kind == "utility":
        return shape
    pos = _clause_positions(st, kind)
    if kind == "select":
        _parse_from(ctx, _clause(st, pos, "FROM"))
        sel = _clause(st, pos, "SELECT")
        _projection(ctx, sel)
    elif kind == "update":
        k = 1
        k = _skip_words(st, k, "ONLY")
        name, alias, k = _qualname(st, k, ctx)
        if k < len(st) and is_word(st[k], "AS"):
            k += 1
        if k < len(st) and st[k].kind in ("word", "qident") and not is_word(st[k], "SET"):
            alias = st[k].ident
        ctx.alias[alias] = name
        shape.refs.append((alias, name))
        if "FROM" in pos:
            _parse_from(ctx, _clause(st, pos, "FROM"))
        for part in split_top(_clause(st, pos, "SET")):
            if part and part[0].ident:
                shape.set_cols.add(part[0].ident)
    elif kind == "delete":
        k = 1
        if is_word(st[k], "FROM"):
            k += 1
        k = _skip_words(st, k, "ONLY")
        name, alias, k = _qualname(st, k, ctx)
        if k < len(st) and is_word(st[k], "AS"):
            k += 1
        if k < len(st) and st[k].kind in ("word", "qident") and not is_word(st[k], "WHERE",
                                                                            "USING"):
            alias = st[k].ident
        ctx.alias[alias] = name
        shape.refs.append((alias, name))
        if "USING" in pos:
            _parse_from(ctx, _clause(st, pos, "USING"))
    elif kind == "insert":
        k = 1
        if is_word(st[k], "INTO"):
            k += 1
        name, alias, k = _qualname(st, k, ctx)
        ctx.alias[alias] = name
        shape.refs.append((alias, name))
        for k2, t in enumerate(st):
            if t.depth != 0 or not is_word(t, "CONFLICT") or k2 + 1 >= len(st):
                continue
            if st[k2 + 1].kind == "(":
                j = match_paren(st, k2 + 1)
                shape.conflict_cols = [p[0].ident for p in split_top(st[k2 + 2:j])
                                       if p and p[0].ident]
                if j + 1 < len(st) and is_word(st[j + 1], "WHERE"):  # a partial arbiter
                    e = next((m for m in range(j + 2, len(st))
                              if st[m].depth == 0 and is_word(st[m], "DO")), len(st))
                    shape.conflict_where = text_of(st[j + 2:e])
            elif is_word(st[k2 + 1], "ON") and k2 + 3 < len(st) and \
                    is_word(st[k2 + 2], "CONSTRAINT"):
                shape.conflict_constraint = st[k2 + 3].ident
        sel = next((k2 for k2, t in enumerate(st) if t.depth == 0 and is_word(t, "SELECT")), -1)
        if sel != -1:
            shape.subshapes.append(_analyze_tokens(rebase(st[sel:]), schema, ctes))
        return shape
    _where(ctx, _clause(st, pos, "WHERE"))
    if "GROUP" in pos:
        shape.group_by = True
    for part in split_top(_clause(st, pos, "ORDER")):
        part = rebase(part)
        direction = "DESC" if any(is_word(t, "DESC") for t in part if t.depth == 0) else "ASC"
        core = [t for t in part if not (t.depth == 0 and is_word(t, "ASC", "DESC", "NULLS",
                                                                  "FIRST", "LAST"))]
        cr = _colref(core)
        if cr:
            tbl = ctx.resolve(*cr)
            shape.order.append((tbl, cr[1], direction))
        else:
            shape.order.append((None, expr_of(core), direction))
    shape.limit = "LIMIT" in pos or "FETCH" in pos
    return shape


def _skip_mat(st, k):
    while k < len(st) and is_word(st[k], "NOT", "MATERIALIZED"):
        k += 1
    return k


def _skip_words(st, k, *words):
    while k < len(st) and is_word(st[k], *words):
        k += 1
    return k


def _qualparts(st, k):
    """(qualifier or None, name, next index) of a possibly qualified name at st[k]."""
    parts = [st[k].ident]
    k += 1
    while k + 1 < len(st) and st[k].kind == "." and st[k + 1].ident:
        parts.append(st[k + 1].ident)
        k += 2
    return (parts[-2] if len(parts) > 1 else None), parts[-1], k


def _qualname(st, k, ctx=None):
    """(table key, bare name, next index): the key the schema knows the table by."""
    qual, name, k = _qualparts(st, k)
    return (ctx.rel(qual, name) if ctx is not None else rel_key(qual, name)), name, k


def _projection(ctx, sel):
    sel = _strip_subqueries(sel)
    for k, t in enumerate(sel):
        if is_word(t, "COUNT", "SUM", "AVG", "MIN", "MAX", "BOOL_AND", "BOOL_OR",
                   "ARRAY_AGG", "STRING_AGG") and k + 1 < len(sel) and sel[k + 1].kind == "(":
            ctx.shape.aggregate = True
    for item in split_top(sel):
        item = rebase(item)
        if len(item) == 1 and item[0].kind == "op" and item[0].text == "*":
            for a, t in ctx.alias.items():
                if t:
                    ctx.shape.select_all.add(t)
            continue
        if len(item) == 3 and item[1].kind == "." and item[2].kind == "op" and item[2].text == "*":
            t = ctx.alias.get(item[0].ident)
            if t:
                ctx.shape.select_all.add(t)
            continue
        for q, c in _colrefs_in(item):
            t = ctx.resolve(q, c) if (q is not None or _known_col(ctx, c)) else None
            if t:
                ctx.shape.select_cols.add((t, c))


def _known_col(ctx, col):
    for t in ctx.alias.values():
        if t and ctx.table_cols(t) and col in ctx.table_cols(t):
            return True
    return False


def analyze(sql, schema=None):
    toks = tokenize(sql)
    stmts = split_statements(toks)
    if not stmts:
        return Shape("utility")
    return _analyze_tokens(rebase(stmts[0]), schema)


def flatten(shape):
    out = [shape]
    for s in shape.subshapes:
        out.extend(flatten(s))
    return out
