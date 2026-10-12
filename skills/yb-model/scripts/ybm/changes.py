"""Structured changes behind every recommendation.

A finding's modelled option is a list of Change objects, and DDL is rendered from them only
when asked for (`yb-model.py sketch`). The option checks, foreign-key re-pointing, the plan
check and the self-check read the structure, never the engine's own SQL text: string
conventions (comment markers, "-- first" lines) were where one fix kept breaking another.

Kinds, and what each does to the schema model (safety.apply):

    create  a new index (stmt: its tokens; name: the index)  -> added
    drop    an index or the UNIQUE constraint behind it       -> removed, with partition copies
    check   the read-only idx_scan check run before a drop    -> nothing
    rekey   a primary-key change by table swap (comment)      -> the table's primary key
    retire  an index left behind on the swapped-out table     -> removed
    fk      foreign-key DROP / ADD NOT VALID / VALIDATE       -> nothing
"""

import re

from . import schema as schema_mod
from .sqltok import tokenize, split_statements, rebase

MODEL = ("create", "drop", "rekey", "retire")  # kinds that change the schema model


class Change:
    __slots__ = ("kind", "sql", "name", "table", "key", "_stmt")

    def __init__(self, kind, sql, name=None, table=None, key=None):
        self.kind, self.sql, self.name, self.table, self.key = kind, sql, name, table, key
        self._stmt = None

    @property
    def stmt(self):
        """The CREATE INDEX statement's tokens (create only), read from its own one-statement
        text. The tokenizer skips comments and keeps string literals whole, so a trailing
        comment goes and a '--' inside a literal stays."""
        if self._stmt is None and self.kind == "create":
            st = _Stmt(rebase(next(iter(split_statements(tokenize(self.sql))), [])))
            st.sql = self.sql
            self._stmt = st
        return self._stmt

    def as_tuple(self):
        if self.kind == "rekey":
            return ("rekey", self.table, self.key)
        if self.kind == "create":
            return ("create", self.stmt)
        return (self.kind, self.name)

    def to_dict(self):
        return {k: getattr(self, k) for k in ("kind", "sql", "name", "table", "key")
                if getattr(self, k) is not None}

    @classmethod
    def from_dict(cls, d):
        return cls(d["kind"], d["sql"], d.get("name"), d.get("table"), d.get("key"))

    def __repr__(self):
        return "Change(%s, %r)" % (self.kind, self.name or self.table or self.sql[:40])


class _Stmt(list):
    """Tokens of one statement, with the SQL text their positions refer to."""
    sql = None


def render(changes):
    """The DDL text of a list of changes, or None when there is none."""
    return "\n".join(c.sql for c in changes) if changes else None


def model(changes):
    """The changes that alter the schema model (not checks, notes or foreign-key steps)."""
    return [c for c in changes if c.kind in MODEL]


# --- constructors ----------------------------------------------------------------------------
def create(sql, name=None):
    """A new index; `name` is the index it creates (bare, in its table's schema)."""
    return Change("create", sql, name=schema_mod.ident(schema_mod.bare(name)) if name else None)


def drop(idx, after=None):
    """Remove an index: an Index (a constraint's index is dropped with ALTER TABLE ... DROP
    CONSTRAINT) or a name (DROP INDEX)."""
    if isinstance(idx, str):
        return Change("drop", schema_mod.drop_index_sql(idx, after), name=idx)
    return Change("drop", schema_mod.drop_sql(idx, after), name=idx.name)


def replace(orig, new_name, **index_args):
    """Create the derived replacement of `orig`, then drop the original (YSQL rejects DROP
    INDEX CONCURRENTLY, so the drop waits until the replacement is valid)."""
    return [create(schema_mod.index_sql(orig, new_name, **index_args), new_name),
            drop(orig, new_name)]


def check(sql):
    return Change("check", sql)


def rekey(table, key, sql):
    return Change("rekey", sql, table=table, key=key)


def retire(name, sql):
    return Change("retire", sql, name=name)


def fk(sql):
    return Change("fk", sql)


# --- reading hand-written DDL ----------------------------------------------------------------
_QNAME = r'((?:\w+|"(?:[^"]|"")+")(?:\.(?:\w+|"(?:[^"]|"")+"))?)'
RETIRED = re.compile(r'--\s*%s is not created on the new table' % _QNAME)
REKEY = re.compile(r'--\s*CREATE TABLE %s \(\.\.\. PRIMARY KEY \((.+)\)\)' % _QNAME)


def _key_of(text):
    name, _ = schema_mod._name(rebase(tokenize(text)), 0)
    return name


def parse(ddl):
    """Changes from DDL written by hand (tests, or an older review.json without structured
    changes). The engine never reads its own output this way."""
    out = []
    if not ddl:
        return out
    for line in ddl.splitlines():
        m = REKEY.search(line)
        if m:
            name = _key_of(m.group(1))
            if name and name.endswith("_new"):
                out.append(Change("rekey", line, table=name[:-4], key=m.group(2)))
        m = RETIRED.search(line)
        if m:
            out.append(Change("retire", line, name=_key_of(m.group(1))))
    # The tokenizer skips comments and keeps positions in ddl: each statement's text runs to
    # the end of the line that holds its semicolon, so a trailing comment stays with it.
    for st in split_statements(tokenize(ddl)):
        st = rebase(st)
        if not st:
            continue
        words = [t.up for t in st[:4] if t.kind == "word"]
        up = [t.up for t in st if t.kind == "word"]
        end = st[-1].pos + len(st[-1].text)
        semi = ddl.find(";", end)
        eol = ddl.find("\n", semi if semi != -1 else end)
        text = ddl[st[0].pos:eol if eol != -1 else len(ddl)].strip()
        if up[:2] == ["ALTER", "TABLE"] and "DROP" in up and "CONSTRAINT" in up:
            table, _ = schema_mod._name(st, schema_mod._skip_words(st, 2, "IF", "EXISTS", "ONLY"))
            k = next(n for n, t in enumerate(st) if t.kind == "word" and t.up == "CONSTRAINT")
            cname, _ = schema_mod._name(st, schema_mod._skip_words(st, k + 1, "IF", "EXISTS"))
            if cname:  # a constraint's index lives in its table's schema
                out.append(Change("drop", text, name=schema_mod.in_schema_of(table, cname)))
            continue
        if words[:1] == ["DROP"] and "INDEX" in words:
            name, _ = schema_mod._name(st, schema_mod._skip_words(
                st, 2, "CONCURRENTLY", "IF", "EXISTS"))
            if name:
                out.append(Change("drop", text, name=name))
        elif words[:1] == ["CREATE"] and "INDEX" in words:
            c = Change("create", text)
            stmt = _Stmt(st)
            stmt.sql = ddl
            c._stmt = stmt
            out.append(c)
    return out
