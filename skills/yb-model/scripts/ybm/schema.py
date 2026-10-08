"""DDL model: tables, keys and indexes as YugabyteDB will actually create them.

Sharding defaults follow the server rule (pg_yb_utils.c, YbSortOrdering): an unannotated
first key column is HASH only when yb_use_hash_splitting_by_default is on and the relation is
neither colocated nor in a tablegroup; otherwise ASC. Every other unannotated column is ASC.
"""

import hashlib
import re

from .sqltok import (tokenize, split_statements, match_paren, split_top, text_of, expr_of,
                     is_word, qi, rebase, rel_key, split_key, bare, qn, in_schema_of)

IDENT_MAX = 63  # PostgreSQL truncates longer identifiers silently


def key_group(cols, colocated, hashed=1):
    """Key columns (as SQL) for a new index: the first `hashed` columns (all with None) form the
    hash group and the rest are ascending; on a colocated relation every key is ascending,
    because YSQL rejects hash keys there ("cannot colocate hash partitioned index")."""
    if colocated:
        return ", ".join("%s ASC" % c for c in cols)
    n = len(cols) if hashed is None else hashed
    return ", ".join(["(%s) HASH" % ", ".join(cols[:n])] + ["%s ASC" % c for c in cols[n:]])


def ident(name):
    """A generated identifier that fits the 63-byte limit: a longer one keeps a stable hash
    suffix instead of being cut by the server (which could collide with an existing name)."""
    if len(name) <= IDENT_MAX:
        return name
    return "%s_%s" % (name[:IDENT_MAX - 9], hashlib.sha1(name.encode()).hexdigest()[:8])


def index_sql(orig, name, table=None, keys=None, add_include=(), add_where=None, split=None,
              colocated=False, partitioned=False):
    """CREATE INDEX for a replacement of `orig`, derived from it so that nothing is lost by
    omission: UNIQUE (with NULLS NOT DISTINCT), INCLUDE, the predicate (as written), the method
    and the SPLIT clause are carried over, and a rule states only what it changes.

    keys: a new key list (SQL), or None to keep the original layout. add_include: columns to
    cover. add_where: a predicate AND-ed with the original. split: the SPLIT clause for a new key
    layout (bucketing); with the original layout the original clause is kept. A colocated
    relation takes no SPLIT, and a partitioned parent cannot build an index CONCURRENTLY."""
    same_keys = keys is None
    include = list(orig.include) + sorted(set(add_include) - set(orig.include))
    where = orig.where_sql or orig.where
    if add_where:
        where = "(%s) AND (%s)" % (where, add_where) if where else add_where
    clause = None
    if colocated:
        clause = None
    elif split is not None:
        clause = split
    elif same_keys:
        clause = orig.split_sql or ("SPLIT %s TABLETS" % orig.split
                                    if orig.split and orig.split.startswith("INTO") else None)
    return "CREATE %sINDEX %s%s ON %s%s (%s)%s%s%s%s;" % (
        "UNIQUE " if orig.unique else "", "" if partitioned else "CONCURRENTLY ",
        qi(ident(bare(name))), qn(table or orig.table),
        (" USING %s" % orig.method) if orig.method not in (None, "lsm") else "",
        keys or orig.signature(quote=True),
        (" INCLUDE (%s)" % ", ".join(qi(c) for c in include)) if include else "",
        " NULLS NOT DISTINCT" if orig.unique and getattr(orig, "nulls_not_distinct", False)
        else "", (" " + clause) if clause else "", (" WHERE %s" % where) if where else "")


def drop_sql(idx, after=None):
    """Remove an index: DROP INDEX, or ALTER TABLE ... DROP CONSTRAINT when the index backs a
    UNIQUE constraint (DROP INDEX on it fails: "cannot drop index ... because constraint ...
    requires it", pinned in yb.port.create_index.out)."""
    if getattr(idx, "constraint", False):
        return "ALTER TABLE %s DROP CONSTRAINT %s;%s" % (
            qn(idx.table), qi(bare(idx.name)), ("  -- after %s is valid" % after) if after else "")
    return drop_index_sql(idx.name, after)


def drop_index_sql(name, after=None):
    """DROP INDEX as YSQL accepts it: the grammar rejects DROP INDEX CONCURRENTLY (gram.y,
    parser_ybc_not_support), so no CONCURRENTLY here."""
    return "DROP INDEX %s;%s" % (qn(name), ("  -- after %s is valid" % after) if after else "")


class KeyCol:
    def __init__(self, col=None, expr=None, mode=None):
        self.col = col      # column name for a plain column key
        self.expr = expr    # normalised expression text for an expression key
        self.mode = mode    # HASH | ASC | DESC | None (unannotated; resolved later)
        self.explicit = mode is not None

    @property
    def label(self):
        return self.col if self.col else "(%s)" % self.expr

    @property
    def sql(self):
        """The key as DDL: a quoted name where needed, an expression as written."""
        return qi(self.col) if self.col else "(%s)" % self.expr

    def to_dict(self):
        return {"col": self.col, "expr": self.expr, "mode": self.mode,
                "explicit": self.explicit}


class Index:
    def __init__(self, name, table, keys, unique=False, include=None, where=None,
                 split=None, is_pk=False, method="lsm", line=0, split_sql=None, where_sql=None,
                 constraint=False, nulls_not_distinct=False):
        self.name = name
        self.table = table
        self.keys = keys
        self.unique = unique
        self.include = include or []
        self.where = where      # normalised predicate text or None
        self.split = split      # "INTO n" | "AT VALUES" | None
        self.is_pk = is_pk
        self.method = method
        self.line = line
        self.split_sql = split_sql  # the SPLIT clause exactly as written, or None
        self.where_sql = where_sql  # the predicate exactly as written, or None
        self.constraint = constraint  # backs a UNIQUE constraint: dropped with ALTER TABLE
        self.nulls_not_distinct = nulls_not_distinct  # UNIQUE ... NULLS NOT DISTINCT

    @property
    def hash_cols(self):
        out = []
        for k in self.keys:
            if k.mode != "HASH":
                break
            out.append(k)
        return out

    @property
    def range_cols(self):
        return self.keys[len(self.hash_cols):]

    @property
    def key_names(self):
        return [k.col for k in self.keys if k.col]

    def signature(self, quote=False):
        """The key layout, as text (quote=False) or as DDL (quote=True)."""
        lab = (lambda k: k.sql) if quote else (lambda k: k.label)
        if self.hash_cols:
            h = "(%s) HASH" % ", ".join(lab(k) for k in self.hash_cols)
            rest = ["%s %s" % (lab(k), k.mode) for k in self.range_cols]
            return ", ".join([h] + rest)
        return ", ".join("%s %s" % (lab(k), k.mode) for k in self.keys)

    def to_dict(self):
        return {"name": self.name, "table": self.table, "unique": self.unique,
                "is_pk": self.is_pk, "method": self.method, "signature": self.signature(),
                "keys": [k.to_dict() for k in self.keys], "include": self.include,
                "where": self.where, "split": self.split}


class Table:
    def __init__(self, name, line=0):
        self.name = name
        self.cols = {}          # name -> {"type": str, "notnull": bool, "sequence": bool}
        self.col_order = []
        self.pk = None          # Index
        self.partition_by = None  # (method, [cols])
        self.partition_of = None
        self.partitions = []
        self.bound_to = None    # upper bound literal of a RANGE partition ("2027-01-01 ...")
        self.is_default = False
        self.colocated = None   # True | False | None (inherit database)
        self.tablegroup = None
        self.split = None
        self.line = line

    def to_dict(self):
        return {"name": self.name, "columns": [{"name": c, **self.cols[c]} for c in self.col_order],
                "pk": self.pk.to_dict() if self.pk else None,
                "partition_by": self.partition_by, "partition_of": self.partition_of,
                "partitions": sorted(self.partitions), "bound_to": self.bound_to,
                "is_default": self.is_default, "colocated": self.colocated,
                "tablegroup": self.tablegroup, "split": self.split}


class ForeignKey:
    def __init__(self, name, table, cols, ref_table, ref_cols, clause):
        self.name = name            # the constraint's name
        self.table = table          # the referencing table
        self.cols = cols
        self.ref_table = ref_table
        self.ref_cols = ref_cols    # None: the referenced table's primary key
        self.clause = clause        # FOREIGN KEY (...) REFERENCES ... [options], as written


class Schema:
    def __init__(self):
        self.tables = {}
        self.indexes = {}
        self.foreign_keys = []
        self.db_colocated = False
        self.hash_default = True
        self.parse_notes = []
        self.search_path = ["public"]  # schemas an unqualified name in a query is looked up in
        self.ambiguous = {}  # unqualified name -> (chosen key, [other keys]) seen in queries

    # --- lookups -------------------------------------------------------------
    def table(self, name):
        return self.tables.get(name)

    def rel(self, qual, name):
        """The key of the table a query names: as qualified, else the first schema on the
        search path that has it (as the server resolves it), else the one schema that has it.
        A name that several schemas define is recorded in self.ambiguous."""
        if qual:
            return rel_key(qual, name)
        same = sorted(k for k in self.tables if split_key(k)[1] == name)
        hit = next((rel_key(s, name) for s in self.search_path
                    if rel_key(s, name) in self.tables), None)
        if hit is None and len(same) == 1:
            hit = same[0]
        if hit is None:
            return name
        if len(same) > 1:
            self.ambiguous[name] = (hit, [k for k in same if k != hit])
        return hit

    def fks_on_index(self, idx):
        """Foreign keys that may depend on `idx`: a foreign key is created against a unique,
        non-partial index (or the primary key) on exactly the columns it references, and that
        index cannot be dropped while the key exists."""
        if not (idx.unique or idx.is_pk) or idx.where or not all(k.col for k in idx.keys):
            return []
        keys = {k.col for k in idx.keys}
        t = self.tables.get(idx.table)
        pk = {k.col for k in t.pk.keys} if t and t.pk else set()
        return [fk for fk in self.foreign_keys if fk.ref_table == idx.table and
                (set(fk.ref_cols) if fk.ref_cols else pk) == keys]

    def fks_referencing(self, table):
        return [fk for fk in self.foreign_keys if fk.ref_table == table]

    def indexes_on(self, table, own_only=False):
        """Access paths for a table. A partitioned parent with no index of its own is read
        through its partitions, so it borrows the first partition's paths (deduplicated by
        shape) for access-path analysis."""
        out = []
        t = self.tables.get(table)
        if t and t.pk:
            out.append(t.pk)
        out.extend(sorted((i for i in self.indexes.values() if i.table == table),
                          key=lambda i: i.name))
        if own_only or not t or not t.partitions:
            return out
        have = {i.signature() for i in out}
        for part in sorted(t.partitions):
            for i in self.indexes_on(part, own_only=True):
                if i.signature() not in have:
                    have.add(i.signature())
                    out.append(i)
        return out

    def dependents(self, table, col):
        """Indexes on `table` (the primary key included) whose key, INCLUDE list or predicate
        references `col`. Dropping the column drops every one of them."""
        pat = re.compile(r"\b%s\b" % re.escape(col))
        out = []
        for idx in self.indexes_on(table, own_only=True):
            if any(k.col == col or (k.expr and pat.search(k.expr)) for k in idx.keys):
                out.append((idx.name, "key"))
            elif col in idx.include:
                out.append((idx.name, "INCLUDE"))
            elif idx.where and pat.search(idx.where):
                out.append((idx.name, "predicate"))
        return out

    def is_colocated(self, table):
        t = self.tables.get(table)
        if t is None:
            return self.db_colocated
        if t.partition_of and t.colocated is None:
            return self.is_colocated(t.partition_of)
        if t.colocated is not None:
            return t.colocated
        return self.db_colocated

    def resolve_modes(self):
        """Fill in unannotated key modes the way the server does."""
        def fix(idx, table):
            t = self.tables.get(table)
            range_default = (not self.hash_default) or self.is_colocated(table) or \
                bool(t and t.tablegroup)
            for pos, k in enumerate(idx.keys):
                if k.mode is None:
                    if pos == 0 and not range_default and idx.method == "lsm":
                        k.mode = "HASH"
                    else:
                        k.mode = "ASC"
            # A column after a range column can never be hash.
            seen_range = False
            for k in idx.keys:
                if k.mode != "HASH":
                    seen_range = True
                elif seen_range:
                    k.mode = "ASC"
        for t in self.tables.values():
            if t.pk:
                fix(t.pk, t.name)
        for i in self.indexes.values():
            fix(i, i.table)

    def to_dict(self):
        return {"db_colocated": self.db_colocated, "hash_default": self.hash_default,
                "tables": [self.tables[k].to_dict() for k in sorted(self.tables)],
                "indexes": [self.indexes[k].to_dict() for k in sorted(self.indexes)],
                "parse_notes": self.parse_notes}


# --- parsing ---------------------------------------------------------------------------

def _name(toks, i):
    """Read a possibly qualified name at toks[i]; return (relation key, next_index): the bare
    name in schema public, schema.name elsewhere (sqltok.rel_key)."""
    if i >= len(toks) or toks[i].ident is None:
        return None, i
    parts = [toks[i].ident]
    i += 1
    while i + 1 < len(toks) and toks[i].kind == "." and toks[i + 1].ident is not None:
        parts.append(toks[i + 1].ident)
        i += 2
    return rel_key(parts[-2] if len(parts) > 1 else None, parts[-1]), i


def _skip_words(toks, i, *words):
    while i < len(toks) and is_word(toks[i], *words):
        i += 1
    return i


def _parse_keys(toks):
    """Parse the inside of a key column list into [KeyCol]."""
    keys = []
    for part in split_top(toks):
        part = rebase(part)
        mode = None
        # Trailing annotations.
        tail = [t for t in part if t.depth == 0]
        for t in tail:
            if is_word(t, "HASH", "ASC", "DESC"):
                mode = t.up
        if part[0].kind == "(":
            j = match_paren(part, 0)
            inner = part[1:j]
            items = split_top(inner)
            simple = all(len(x) == 1 and x[0].ident is not None for x in items)
            if simple and (len(items) > 1 or mode == "HASH"):
                for x in items:
                    keys.append(KeyCol(col=x[0].ident, mode=mode or "HASH"))
                continue
            if simple and len(items) == 1:
                keys.append(KeyCol(col=items[0][0].ident, mode=mode))
                continue
            keys.append(KeyCol(expr=expr_of(inner), mode=mode))
            continue
        if part[0].ident is not None and (len(part) == 1 or part[1].kind != "("):
            keys.append(KeyCol(col=part[0].ident, mode=mode))
            continue
        # Bare function expression, e.g. lower(email).
        stop = len(part)
        for k, t in enumerate(part):
            if t.depth == 0 and is_word(t, "HASH", "ASC", "DESC", "NULLS", "COLLATE"):
                stop = k
                break
        keys.append(KeyCol(expr=expr_of(part[:stop]), mode=mode))
    return keys


def _split_clause(toks, i):
    if i < len(toks) and is_word(toks[i], "SPLIT"):
        if i + 1 < len(toks) and is_word(toks[i + 1], "INTO") and i + 2 < len(toks):
            return "INTO %s" % toks[i + 2].text
        return "AT VALUES"
    return None


def _bounds(toks, t):
    """Record FOR VALUES ... TO ('x') and DEFAULT on a partition."""
    for k, x in enumerate(toks):
        if x.depth == 0 and is_word(x, "DEFAULT") and k > 0 and \
                (is_word(toks[k - 1], "OF") or toks[k - 1].ident is not None) and \
                any(is_word(y, "PARTITION") for y in toks[:k]):
            if k + 1 >= len(toks) or not toks[k + 1].kind == "(":
                t.is_default = True
        if x.depth == 0 and is_word(x, "TO") and k + 1 < len(toks) and toks[k + 1].kind == "(":
            j = match_paren(toks, k + 1)
            lit = [y for y in toks[k + 2:j] if y.kind == "str"]
            if lit:
                t.bound_to = lit[0].text.strip("'")


def _find_top(toks, word, start=0):
    for k in range(start, len(toks)):
        if toks[k].depth == 0 and is_word(toks[k], word):
            return k
    return -1


def _parse_create_table(st, schema, line, sql=None):
    i = 1
    i = _skip_words(st, i, "GLOBAL", "LOCAL", "TEMP", "TEMPORARY", "UNLOGGED")
    if not is_word(st[i] if i < len(st) else None, "TABLE"):
        return
    i += 1
    if is_word(st[i], "IF"):
        i += 3
    name, i = _name(st, i)
    if not name:
        return
    t = schema.tables.get(name) or Table(name, line)
    schema.tables[name] = t
    if i < len(st) and is_word(st[i], "PARTITION") and is_word(st[i + 1], "OF"):
        parent, i = _name(st, i + 2)
        t.partition_of = parent
        _bounds(st, t)
        if parent in schema.tables:
            schema.tables[parent].partitions.append(name)
        else:
            schema.parse_notes.append("partition %s declared before parent %s" % (name, parent))
    body_cols = []
    if i < len(st) and st[i].kind == "(":
        j = match_paren(st, i)
        body_cols = split_top(st[i + 1:j])
        i = j + 1
    for el in body_cols:
        el = rebase(el)
        head = el[0]
        cons = None
        if is_word(head, "CONSTRAINT"):
            cons = el[1].ident if len(el) > 1 else None
            el = el[2:]
            head = el[0] if el else None
        if head is None:
            continue
        if is_word(head, "PRIMARY") and len(el) > 2 and el[2].kind == "(":
            j = match_paren(el, 2)
            t.pk = Index(in_schema_of(name, cons) if cons else name + "_pkey", name,
                         _parse_keys(el[3:j]), unique=True, is_pk=True, line=line)
            continue
        nnd, m = _nulls_clause(el, 1) if is_word(head, "UNIQUE") else (False, 1)
        if is_word(head, "UNIQUE") and len(el) > m and el[m].kind == "(":
            j = match_paren(el, m)
            iname = in_schema_of(name, cons) if cons else "%s_%s_key" % (
                name, "_".join(text_of([x]) for x in el[m + 1:j] if x.ident))
            schema.indexes[iname] = Index(iname, name, _parse_keys(el[m + 1:j]), unique=True,
                                          line=line, constraint=True, nulls_not_distinct=nnd)
            continue
        if is_word(head, "FOREIGN") and len(el) > 2 and el[2].kind == "(":
            e = match_paren(el, 2)
            fcols = [p[0].ident for p in split_top(el[3:e]) if p and p[0].ident]
            r = e + 1
            if r < len(el) and is_word(el[r], "REFERENCES"):
                ref, rcols, m = _references(el, r)
                schema.foreign_keys.append(ForeignKey(
                    cons or "%s_%s_fkey" % (name, "_".join(fcols)), name, fcols, ref, rcols,
                    _raw(sql, el[:m])))
            continue
        if is_word(head, "CHECK", "EXCLUDE", "LIKE"):
            continue
        if head.ident is None:
            continue
        cname = head.ident
        typ_toks, k = [], 1
        while k < len(el) and not (el[k].depth == 0 and is_word(
                el[k], "NOT", "NULL", "DEFAULT", "PRIMARY", "UNIQUE", "CHECK", "REFERENCES",
                "CONSTRAINT", "GENERATED", "COLLATE")):
            typ_toks.append(el[k])
            k += 1
        rest = [x.up for x in el[k:] if x.depth == 0 and x.kind == "word"]
        # Filled from a sequence: serial types, DEFAULT nextval(...), or an identity column.
        sequence = text_of(typ_toks) in ("serial", "bigserial", "smallserial", "serial2",
                                         "serial4", "serial8") or \
            ("DEFAULT" in rest and any(x.kind == "word" and x.up == "NEXTVAL" for x in el[k:])) or \
            ("GENERATED" in rest and "IDENTITY" in rest)
        notnull = False
        for a, b in zip(rest, rest[1:]):
            if a == "NOT" and b == "NULL":
                notnull = True
        if "PRIMARY" in rest:
            notnull = True
            mode = None
            for x in el[k:]:
                if is_word(x, "HASH", "ASC", "DESC"):
                    mode = x.up
            t.pk = Index(name + "_pkey", name, [KeyCol(col=cname, mode=mode)], unique=True,
                         is_pk=True, line=line)
        r = next((m for m in range(k, len(el)) if el[m].depth == 0 and
                  is_word(el[m], "REFERENCES")), None)
        if r is not None:
            ref, rcols, m = _references(el, r)
            fname = el[r - 1].ident if r >= 2 and is_word(el[r - 2], "CONSTRAINT") else None
            schema.foreign_keys.append(ForeignKey(
                fname or "%s_%s_fkey" % (bare(name), cname), name, [cname], ref, rcols,
                "FOREIGN KEY (%s) %s" % (qi(cname), _raw(sql, el[r:m]))))
        if "UNIQUE" in rest:
            iname = "%s_%s_key" % (name, cname)
            u = rest.index("UNIQUE")
            schema.indexes[iname] = Index(iname, name, [KeyCol(col=cname)], unique=True,
                                          line=line, constraint=True,
                                          nulls_not_distinct=rest[u + 1:u + 4] ==
                                          ["NULLS", "NOT", "DISTINCT"])
        if cname not in t.cols:
            t.col_order.append(cname)
        t.cols[cname] = {"type": text_of(typ_toks), "notnull": notnull, "sequence": sequence}
    if t.pk:
        for k in t.pk.keys:
            if k.col in t.cols:
                t.cols[k.col]["notnull"] = True
    # Trailing clauses.
    k = _find_top(st, "PARTITION", i)
    if k != -1 and k + 2 < len(st) and is_word(st[k + 1], "BY"):
        method = st[k + 2].up
        if k + 3 < len(st) and st[k + 3].kind == "(":
            j = match_paren(st, k + 3)
            cols = [p[0].ident for p in split_top(st[k + 4:j]) if p and p[0].ident]
            t.partition_by = (method, cols)
    k = _find_top(st, "WITH", i)
    if k != -1 and k + 1 < len(st) and st[k + 1].kind == "(":
        j = match_paren(st, k + 1)
        opts = text_of(st[k + 2:j]).replace(" ", "")
        if "colocation=false" in opts or "colocated=false" in opts:
            t.colocated = False
        elif "colocation=true" in opts or "colocated=true" in opts:
            t.colocated = True
    k = _find_top(st, "TABLEGROUP", i)
    if k != -1:
        t.tablegroup = st[k + 1].ident
    k = _find_top(st, "SPLIT", i)
    if k != -1:
        t.split = _split_clause(st, k)
    if t.pk and t.pk.keys:
        t.pk.split = t.split


def _raw(sql, toks):
    """The original text of a token run, or None without the source."""
    if sql is None or not toks:
        return None
    return sql[toks[0].pos:toks[-1].pos + len(toks[-1].text)]


def _references(toks, r):
    """(referenced table, referenced columns or None, index after the clause) for REFERENCES at
    r with its options (ON DELETE / ON UPDATE actions, MATCH, DEFERRABLE, INITIALLY)."""
    ref, m = _name(toks, r + 1)
    cols = None
    if m < len(toks) and toks[m].kind == "(":
        e = match_paren(toks, m)
        cols = [p[0].ident for p in split_top(toks[m + 1:e]) if p and p[0].ident]
        m = e + 1
    while m < len(toks) and toks[m].depth == 0:
        if is_word(toks[m], "ON") and m + 2 < len(toks):
            m += 2  # ON DELETE | ON UPDATE
            if is_word(toks[m], "NO", "SET"):
                m += 1
            m += 1
            if m < len(toks) and toks[m].kind == "(":  # SET NULL (cols)
                m = match_paren(toks, m) + 1
        elif is_word(toks[m], "MATCH", "INITIALLY"):
            m += 2
        elif is_word(toks[m], "DEFERRABLE"):
            m += 1
        elif is_word(toks[m], "NOT") and m + 1 < len(toks) and is_word(toks[m + 1], "DEFERRABLE"):
            m += 2
        else:
            break
    return ref, cols, m


def _nulls_clause(toks, i):
    """(nulls_not_distinct, index after the clause) for an optional NULLS [NOT] DISTINCT at i."""
    if i < len(toks) and is_word(toks[i], "NULLS"):
        if i + 2 < len(toks) and is_word(toks[i + 1], "NOT") and is_word(toks[i + 2], "DISTINCT"):
            return True, i + 3
        return False, i + 2
    return False, i


def _parse_create_index(st, schema, line, sql=None):
    i = 1
    unique = False
    if is_word(st[i], "UNIQUE"):
        unique = True
        i += 1
    if not is_word(st[i], "INDEX"):
        return
    i += 1
    i = _skip_words(st, i, "CONCURRENTLY", "NONCONCURRENTLY")
    if is_word(st[i], "IF"):
        i += 3
    iname = None
    if not is_word(st[i], "ON"):
        iname, i = _name(st, i)
    if not is_word(st[i], "ON"):
        return
    i += 1
    i = _skip_words(st, i, "ONLY")
    tname, i = _name(st, i)
    iname = in_schema_of(tname, iname)  # an index lives in its table's schema
    method = "lsm"
    if is_word(st[i], "USING"):
        method = st[i + 1].text.lower()
        i += 2
    if i >= len(st) or st[i].kind != "(":
        return
    j = match_paren(st, i)
    keys = _parse_keys(st[i + 1:j])
    i = j + 1
    include, where, split, split_sql, where_sql = [], None, None, None, None
    k = _find_top(st, "INCLUDE", i)
    if k != -1 and st[k + 1].kind == "(":
        e = match_paren(st, k + 1)
        include = [p[0].ident for p in split_top(st[k + 2:e]) if p and p[0].ident]
    k = _find_top(st, "NULLS", i)
    nnd = k != -1 and _nulls_clause(st, k)[0]
    k = _find_top(st, "SPLIT", i)
    if k != -1:
        split = _split_clause(st, k)
        w = _find_top(st, "WHERE", k)
        split_sql = _raw(sql, st[k:w if w != -1 else len(st)])
    k = _find_top(st, "WHERE", i)
    if k != -1:
        end = len(st)
        s2 = _find_top(st, "SPLIT", k)
        if s2 != -1:
            end = s2
        where = text_of(st[k + 1:end])
        where_sql = _raw(sql, st[k + 1:end])
    if not iname:
        iname = "%s_%s_idx" % (tname, "_".join(kc.col or "expr" for kc in keys))
    if method not in ("lsm", "btree", "hash"):
        # gin / ybgin / ybhnsw and friends are not modelled as key-ordered access paths.
        schema.parse_notes.append("index %s uses %s; not modelled for scan analysis"
                                  % (iname, method))
    schema.indexes[iname] = Index(iname, tname, keys, unique=unique, include=include,
                                  where=where, split=split, method=method if method != "btree"
                                  else "lsm", line=line, split_sql=split_sql,
                                  where_sql=where_sql, nulls_not_distinct=nnd)


def _parse_alter_table(st, schema, line, sql=None):
    i = 2
    i = _skip_words(st, i, "IF", "EXISTS", "ONLY")
    tname, i = _name(st, i)
    if not tname:
        return
    # ysql_dump attaches sequences after the table: ALTER COLUMN c SET DEFAULT nextval(...),
    # or ALTER COLUMN c ADD GENERATED ... AS IDENTITY.
    k = _find_top(st, "COLUMN", i)
    t = schema.tables.get(tname)
    if k != -1 and t is not None and k > 0 and is_word(st[k - 1], "ALTER") and \
            k + 1 < len(st) and st[k + 1].ident in t.cols:
        words = [x.up for x in st[k + 2:] if x.kind == "word"]
        if ("DEFAULT" in words and "NEXTVAL" in words) or "IDENTITY" in words:
            t.cols[st[k + 1].ident]["sequence"] = True
    k = _find_top(st, "ATTACH", i)
    if k != -1 and is_word(st[k + 1], "PARTITION"):
        child, _ = _name(st, k + 2)
        if child in schema.tables:
            schema.tables[child].partition_of = tname
            _bounds(st[k:], schema.tables[child])
        if tname in schema.tables and child not in schema.tables[tname].partitions:
            schema.tables[tname].partitions.append(child)
        return
    k = _find_top(st, "ADD", i)
    if k == -1:
        return
    j = k + 1
    cname = None
    if is_word(st[j], "CONSTRAINT"):
        cname = st[j + 1].ident
        j += 2
    if is_word(st[j], "PRIMARY") and st[j + 2].kind == "(":
        e = match_paren(st, j + 2)
        t = schema.tables.get(tname)
        if t:
            t.pk = Index(in_schema_of(tname, cname) if cname else tname + "_pkey", tname,
                         _parse_keys(st[j + 3:e]), unique=True, is_pk=True, line=line)
            for kc in t.pk.keys:
                if kc.col in t.cols:
                    t.cols[kc.col]["notnull"] = True
    elif is_word(st[j], "UNIQUE") and st[_nulls_clause(st, j + 1)[1]].kind == "(":
        nnd, m = _nulls_clause(st, j + 1)
        e = match_paren(st, m)
        iname = in_schema_of(tname, cname) if cname else "%s_key" % tname
        schema.indexes[iname] = Index(iname, tname, _parse_keys(st[m + 1:e]), unique=True,
                                      line=line, constraint=True, nulls_not_distinct=nnd)
    elif is_word(st[j], "FOREIGN") and is_word(st[j + 1], "KEY") and st[j + 2].kind == "(":
        e = match_paren(st, j + 2)
        fcols = [p[0].ident for p in split_top(st[j + 3:e]) if p and p[0].ident]
        if e + 1 < len(st) and is_word(st[e + 1], "REFERENCES"):
            ref, rcols, m = _references(st, e + 1)
            schema.foreign_keys.append(ForeignKey(
                cname or "%s_%s_fkey" % (bare(tname), "_".join(fcols)), tname, fcols, ref, rcols,
                _raw(sql, st[j:m])))
    elif is_word(st[j], "UNIQUE") and is_word(st[j + 1], "USING") and is_word(st[j + 2], "INDEX"):
        # How ysql_dump writes a unique constraint (pg_dump.c): the index first, then the
        # constraint over it. The index is renamed to the constraint's name.
        src, _ = _name(st, j + 3)
        src = in_schema_of(tname, src)
        idx = schema.indexes.pop(src, None)
        if idx is not None:
            idx.name = in_schema_of(tname, cname) if cname else src
            idx.constraint = True
            schema.indexes[idx.name] = idx


def parse(sql, db_colocated=None, hash_default=True):
    schema = Schema()
    schema.hash_default = hash_default
    # psql meta-commands (backslash lines such as if/connect) are not SQL and would glue
    # onto the next statement.
    sql = "\n".join("" if ln.lstrip().startswith("\\") else ln for ln in sql.splitlines())
    toks = tokenize(sql)
    for st in split_statements(toks):
        st = rebase(st)
        if not st:
            continue
        line = sql.count("\n", 0, st[0].pos) + 1
        try:
            if is_word(st[0], "CREATE"):
                if any(is_word(x, "DATABASE") for x in st[1:3]):
                    txt = text_of(st).replace(" ", "")
                    if "colocation=true" in txt or "colocated=true" in txt:
                        schema.db_colocated = True
                elif any(is_word(x, "TABLE") for x in st[1:4]):
                    _parse_create_table(st, schema, line, sql)
                elif any(is_word(x, "INDEX") for x in st[1:3]):
                    _parse_create_index(st, schema, line, sql)
            elif is_word(st[0], "ALTER") and len(st) > 1 and is_word(st[1], "TABLE"):
                _parse_alter_table(st, schema, line, sql)
        except IndexError:
            schema.parse_notes.append("could not parse statement at line %d" % line)
    if db_colocated is not None:
        schema.db_colocated = db_colocated
    # Partitions inherit columns and PK shape from the parent when they declare none.
    for t in schema.tables.values():
        if t.partition_of and t.partition_of in schema.tables:
            parent = schema.tables[t.partition_of]
            if not t.cols:
                t.cols = dict(parent.cols)
                t.col_order = list(parent.col_order)
            if t.pk is None and parent.pk is not None:
                t.pk = Index(t.name + "_pkey", t.name,
                             [KeyCol(k.col, k.expr, k.mode) for k in parent.pk.keys],
                             unique=True, is_pk=True, line=t.line)
    schema.resolve_modes()
    return schema
