"""Small SQL tokenizer shared by the DDL and query parsers. Standard library only.

It is not a SQL parser. It produces a flat token list with parenthesis depth so the callers
can find clauses at the top level of a statement. Comments are dropped; string, dollar-quoted
and quoted-identifier tokens are kept intact.
"""

import re

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_NUM = re.compile(r"\d+(\.\d+)?([eE][+-]?\d+)?")
_PARAM = re.compile(r"\$\d+")
_DOLLAR_TAG = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")
_OPS = ("::", "<=", ">=", "<>", "!=", "||", "->>", "->", "#>>", "#>", "~~*", "!~~", "~~",
        "@>", "<@", "&&", "=", "<", ">", "+", "-", "*", "/", "%", "~", "^", "|", "&", "@", "#")


class Tok:
    __slots__ = ("kind", "text", "depth", "pos")

    def __init__(self, kind, text, depth, pos):
        self.kind = kind  # word | qident | str | num | param | op | ( | ) | , | ; | . | [ | ]
        self.text = text
        self.depth = depth
        self.pos = pos

    @property
    def up(self):
        return self.text.upper() if self.kind == "word" else self.text

    @property
    def ident(self):
        """Identifier value: lower-cased unless quoted."""
        if self.kind == "qident":
            return self.text[1:-1].replace('""', '"')
        if self.kind == "word":
            return self.text.lower()
        return None

    def __repr__(self):
        return "Tok(%s,%r,%d)" % (self.kind, self.text, self.depth)


def tokenize(sql):
    toks, i, n, depth = [], 0, len(sql), 0
    while i < n:
        c = sql[i]
        if c.isspace():
            i += 1
            continue
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j == -1 else j
            continue
        if sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        if c == "'" or ((c in "eEbBxXnN") and i + 1 < n and sql[i + 1] == "'"):
            start = i
            if c != "'":
                i += 1
            j = i + 1
            while j < n:
                if sql[j] == "\\" and c in "eE":
                    j += 2
                    continue
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            toks.append(Tok("str", sql[start:j + 1], depth, start))
            i = j + 1
            continue
        if c == '"':
            j = i + 1
            while j < n:
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':
                        j += 2
                        continue
                    break
                j += 1
            toks.append(Tok("qident", sql[i:j + 1], depth, i))
            i = j + 1
            continue
        if c == "$":
            m = _PARAM.match(sql, i)
            if m:
                toks.append(Tok("param", m.group(0), depth, i))
                i = m.end()
                continue
            m = _DOLLAR_TAG.match(sql, i)
            if m:
                tag = m.group(0)
                j = sql.find(tag, m.end())
                j = n if j == -1 else j + len(tag)
                toks.append(Tok("str", sql[i:j], depth, i))
                i = j
                continue
        m = _WORD.match(sql, i)
        if m:
            toks.append(Tok("word", m.group(0), depth, i))
            i = m.end()
            continue
        m = _NUM.match(sql, i)
        if m:
            toks.append(Tok("num", m.group(0), depth, i))
            i = m.end()
            continue
        if c == "(":
            toks.append(Tok("(", c, depth, i))
            depth += 1
            i += 1
            continue
        if c == ")":
            depth = max(0, depth - 1)
            toks.append(Tok(")", c, depth, i))
            i += 1
            continue
        if c in ",;.[]":
            toks.append(Tok(c, c, depth, i))
            i += 1
            continue
        for op in _OPS:
            if sql.startswith(op, i):
                toks.append(Tok("op", op, depth, i))
                i += len(op)
                break
        else:
            toks.append(Tok("op", c, depth, i))
            i += 1
    return toks


def split_statements(toks):
    """Split a token list on top-level semicolons."""
    out, cur = [], []
    for t in toks:
        if t.kind == ";" and t.depth == 0:
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        out.append(cur)
    return out


def rebase(toks):
    """Return a copy of toks with depth relative to the first token's depth."""
    if not toks:
        return []
    base = min(t.depth for t in toks)
    return [Tok(t.kind, t.text, t.depth - base, t.pos) for t in toks]


def match_paren(toks, i):
    """toks[i] is '('; return the index of its matching ')'."""
    d = toks[i].depth
    for j in range(i + 1, len(toks)):
        if toks[j].kind == ")" and toks[j].depth == d:
            return j
    return len(toks) - 1


def split_top(toks, sep=","):
    """Split tokens on separators at the lowest depth present."""
    if not toks:
        return []
    d = min(t.depth for t in toks)
    parts, cur = [], []
    for t in toks:
        if t.kind == sep and t.depth == d:
            parts.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        parts.append(cur)
    return [p for p in parts if p]


# Reserved key words (PostgreSQL's "reserved" and "reserved, can be function or type"): as a
# column or table name they must be quoted, which is how ysql_dump writes them.
_RESERVED = frozenset("""all analyse analyze and any array as asc asymmetric authorization binary
both case cast check collate collation column concurrently constraint create cross
current_catalog current_date current_role current_schema current_time current_timestamp
current_user default deferrable desc distinct do else end except false fetch for foreign freeze
from full grant group having ilike in initially inner intersect into is isnull join lateral
leading left like limit localtime localtimestamp natural not notnull null offset on only or
order outer overlaps placing primary references returning right select session_user similar
some symmetric system_user table tablesample then to trailing true union unique user using
variadic verbose when where window with""".split())
_PLAIN = re.compile(r"^[a-z_][a-z0-9_$]*$")


def qi(name):
    """An identifier as SQL: bare when it is a plain lower-case name, quoted otherwise (mixed
    case, special characters or a reserved word), so engine DDL names the same column."""
    if _PLAIN.match(name) and name not in _RESERVED:
        return name
    return '"%s"' % name.replace('"', '""')


def rel_key(qual, name):
    """The engine's name for a relation: bare in schema public (or when unqualified), and
    schema.name in any other schema, so same-named tables in two schemas stay apart."""
    return name if not qual or qual == "public" else "%s.%s" % (qual, name)


def split_key(key):
    """(schema or None, bare name) of a relation key."""
    if key and "." in key:
        s, n = key.split(".", 1)
        return s, n
    return None, key


def bare(key):
    return split_key(key)[1]


def qn(key):
    """A relation key as SQL: schema-qualified outside public, each part quoted as needed."""
    s, n = split_key(key)
    return (qi(s) + "." if s else "") + qi(n)


def in_schema_of(table_key, name):
    """An index or constraint name as a key in its table's schema (they live there)."""
    if not name or "." in name:
        return name
    return rel_key(split_key(table_key)[0], name)


def expr_of(toks):
    """Normalised text of an expression that is also valid SQL: as text_of, except that a
    quoted name keeps its quotes where it needs them (mixed case, a reserved word), so "Email"
    and email stay distinct and the text can be written into DDL."""
    return text_of(toks, quote=True)


def text_of(toks, quote=False):
    """Normalised text of a token run, used to compare expressions."""
    out = []
    for t in toks:
        if t.kind == "word":
            out.append(t.text.lower())
        elif t.kind == "qident":
            out.append(qi(t.ident) if quote else t.ident)
        else:
            out.append(t.text)
    s = " ".join(out)
    s = re.sub(r"\s*([().,:\[\]])\s*", r"\1", s)
    return s


def is_word(t, *words):
    return t is not None and t.kind == "word" and t.up in words


# Words after which a minus sign belongs to the number (x = -1, LIMIT -1), not a subtraction.
_BEFORE_VALUE = {"select", "where", "and", "or", "not", "when", "then", "else", "between",
                 "limit", "offset", "values", "set", "returning", "in", "is", "like", "ilike"}
# Type names that continue over more than one word.
_TYPE_WORDS = {"double": ("precision",), "character": ("varying",), "bit": ("varying",),
               "timestamp": ("with", "without", "time", "zone"),
               "time": ("with", "without", "time", "zone")}


def _skip_type(toks, j):
    """toks[j] starts a type name after '::'; return the index after it."""
    name = toks[j].ident
    j += 1
    while j + 1 < len(toks) and toks[j].kind == "." and toks[j + 1].kind in ("word", "qident"):
        name = toks[j + 1].ident
        j += 2
    while j < len(toks) and toks[j].kind == "word" and toks[j].ident in _TYPE_WORDS.get(name, ()):
        j += 1
    if j < len(toks) and toks[j].kind == "(":
        j = match_paren(toks, j) + 1
    while j + 1 < len(toks) and toks[j].kind == "[" and toks[j + 1].kind == "]":
        j += 2
    return j


def fingerprint(sql):
    """The statement with every constant and parameter as ?, casts on them and a leading minus
    dropped, IN and VALUES lists collapsed to one item, and identifiers lower-cased.
    pg_stat_statements shows constants as $n, so a query from a list and the same statement
    there have the same fingerprint."""
    toks = tokenize(sql)
    out, i, n = [], 0, len(toks)
    while i < n:
        t = toks[i]
        if t.kind in ("str", "num", "param") or is_word(t, "TRUE", "FALSE"):
            if out and out[-1] == "-" and (len(out) == 1 or out[-2] in ("(", ",") or
                                           out[-2] in _OPS or out[-2] in _BEFORE_VALUE):
                out.pop()
            out.append("?")
            i += 1
            while i + 1 < n and toks[i].kind == "op" and toks[i].text == "::" and \
                    toks[i + 1].kind in ("word", "qident"):
                i = _skip_type(toks, i + 1)
            continue
        out.append(t.ident if t.kind in ("word", "qident") else t.text)
        i += 1
    s = " ".join(out)
    s = re.sub(r"\( \?(?: , \?)+ \)", "( ? )", s)
    return re.sub(r"\( \? \)(?: , \( \? \))+", "( ? )", s)
