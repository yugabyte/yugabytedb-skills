"""The schema model read from a scratch YugabyteDB instead of parsed from the DDL text.

The reviewer's own container loads whatever DDL the bundle has; the server's catalog then says
what it actually created: key layouts (pg_index.indoption, HASH = 0x4), constraints and the
indexes behind them, foreign keys, partitions and their attached indexes, colocation and
tablegroups (yb_table_properties). Each index's canonical definition (pg_get_indexdef with
yb_format_funcs_include_yb_metadata) goes through the ordinary CREATE INDEX parser, so the
model's strings match what the rules expect. Statements the server rejects are recorded, and
the objects they define are taken from the DDL text as before.

Nothing here touches a customer cluster.
"""

import json
import os
import re
import tempfile
import time

from . import schema as schema_mod
from .sqltok import split_statements, tokenize, rebase, rel_key, in_schema_of

# One JSON document with everything the model needs, from user schemas only.
QUERY = r"""
SET yb_format_funcs_include_yb_metadata = true;
WITH rel AS (
  SELECT c.oid, c.relname, n.nspname, c.relkind
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
     AND n.nspname NOT LIKE 'pg\_%' AND c.relkind IN ('r', 'p', 'i', 'I'))
SELECT json_build_object(
  'db_colocated', yb_is_database_colocated(false),
  'tables', (SELECT json_agg(json_build_object(
      'name', r.relname, 'schema', r.nspname, 'kind', r.relkind,
      'props', (SELECT row_to_json(p) FROM yb_table_properties(r.oid) p),
      'partkey', CASE WHEN r.relkind = 'p' THEN pg_get_partkeydef(r.oid) END,
      'parent', (SELECT pc.relname FROM pg_inherits i JOIN pg_class pc ON pc.oid = i.inhparent
                  WHERE i.inhrelid = r.oid LIMIT 1),
      'parent_schema', (SELECT pn.nspname FROM pg_inherits i JOIN pg_class pc
                         ON pc.oid = i.inhparent JOIN pg_namespace pn ON pn.oid = pc.relnamespace
                        WHERE i.inhrelid = r.oid LIMIT 1),
      'bound', (SELECT pg_get_expr(c.relpartbound, c.oid) FROM pg_class c WHERE c.oid = r.oid),
      'columns', (SELECT json_agg(json_build_object(
          'name', a.attname, 'type', format_type(a.atttypid, a.atttypmod),
          'notnull', a.attnotnull, 'identity', a.attidentity <> '',
          'default', pg_get_expr(d.adbin, d.adrelid)) ORDER BY a.attnum)
          FROM pg_attribute a LEFT JOIN pg_attrdef d
            ON d.adrelid = a.attrelid AND d.adnum = a.attnum
         WHERE a.attrelid = r.oid AND a.attnum > 0 AND NOT a.attisdropped))
      ORDER BY r.nspname, r.relname) FROM rel r WHERE r.relkind IN ('r', 'p')),
  'indexes', (SELECT json_agg(json_build_object(
      'name', r.relname, 'schema', r.nspname, 'table', t.relname,
      'def', pg_get_indexdef(i.indexrelid), 'primary', i.indisprimary,
      'unique', i.indisunique,
      'constraint', (SELECT con.conname FROM pg_constraint con
                      WHERE con.conindid = i.indexrelid AND con.contype IN ('p', 'u')
                        AND con.conrelid = i.indrelid LIMIT 1),
      'parent', (SELECT pc.relname FROM pg_inherits h JOIN pg_class pc ON pc.oid = h.inhparent
                  WHERE h.inhrelid = i.indexrelid LIMIT 1),
      'parent_schema', (SELECT pn.nspname FROM pg_inherits h JOIN pg_class pc
                         ON pc.oid = h.inhparent JOIN pg_namespace pn ON pn.oid = pc.relnamespace
                        WHERE h.inhrelid = i.indexrelid LIMIT 1),
      'tablets', (SELECT p.num_tablets FROM yb_table_properties(i.indexrelid) p))
      ORDER BY r.nspname, r.relname)
      FROM rel r JOIN pg_index i ON i.indexrelid = r.oid JOIN pg_class t ON t.oid = i.indrelid),
  'fks', (SELECT json_agg(json_build_object(
      'name', con.conname, 'table', c.relname, 'ref_table', rc.relname,
      'schema', n.nspname,
      'ref_schema', (SELECT rn.nspname FROM pg_namespace rn WHERE rn.oid = rc.relnamespace),
      'cols', (SELECT json_agg(a.attname ORDER BY k.ord) FROM unnest(con.conkey)
                 WITH ORDINALITY k(attnum, ord) JOIN pg_attribute a
                 ON a.attrelid = con.conrelid AND a.attnum = k.attnum),
      'ref_cols', (SELECT json_agg(a.attname ORDER BY k.ord) FROM unnest(con.confkey)
                 WITH ORDINALITY k(attnum, ord) JOIN pg_attribute a
                 ON a.attrelid = con.confrelid AND a.attnum = k.attnum),
      'def', pg_get_constraintdef(con.oid)) ORDER BY c.relname, con.conname)
      FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid
      JOIN pg_class rc ON rc.oid = con.confrelid JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE con.contype = 'f' AND n.nspname NOT IN ('pg_catalog', 'information_schema')));
"""

# One tablet per relation unless the DDL says otherwise, also on images that predate automatic
# tablet splitting (they size an undeclared table by cores).
TSERVER_FLAGS = "ysql_num_shards_per_tserver=1,yb_num_shards_per_tserver=1"
_ERR_LINE = re.compile(r":(\d+): ERROR:\s*(.*)")
_SPLIT_INTO = re.compile(r"(?i)\bSPLIT\s+INTO\s+(\d+)\s+TABLETS\b")
_OBJ = re.compile(r'(?is)^\s*CREATE\s+(?:UNIQUE\s+)?(TABLE|INDEX)\s+(?:NONCONCURRENTLY\s+|'
                  r'CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?((?:"[^"]+"|[^\s"(.]+)\.)?'
                  r'("[^"]+"|[^\s"(]+)')


_ON = re.compile(r'(?is)\bON\s+(?:ONLY\s+)?((?:"[^"]+"|[^\s"(.]+)\.)?("[^"]+"|[^\s"(]+)')


def _ident(text):
    return text[1:-1].replace('""', '"') if text.startswith('"') else text.lower()


def one_tablet(ddl):
    """(ddl, {object: declared tablet count}). Every SPLIT INTO n TABLETS becomes one tablet:
    a large schema then loads on one node (the server caps tablet replicas per node), and the
    declared counts are kept for the model. Tablet counts change no catalog fact. A table is
    keyed as the model keys it (schema.name outside public); an index by its bare name, as
    CREATE INDEX writes it."""
    declared = {}
    for _, _, text in _statements(ddl):
        m, s = _OBJ.match(text), _SPLIT_INTO.search(text)
        if m and s:
            name = _ident(m.group(3))
            if m.group(1).upper() == "TABLE":
                name = rel_key(_ident(m.group(2)[:-1]) if m.group(2) else None, name)
            else:  # an index lives in its table's schema
                on = _ON.search(text, m.end())
                if on:
                    name = in_schema_of(rel_key(_ident(on.group(1)[:-1]) if on.group(1) else None,
                                                _ident(on.group(2))), name)
            declared[name] = int(s.group(1))
    return _SPLIT_INTO.sub("SPLIT INTO 1 TABLETS", ddl), declared


def _truthy(v):
    return str(v).strip().lower() in ("t", "true", "1", "on", "yes")


def _statements(sql):
    """[(first line, last line, text)] of each statement in `sql`."""
    out = []
    for st in split_statements(tokenize(sql)):
        a, b = st[0].pos, st[-1].pos + len(st[-1].text)
        out.append((sql.count("\n", 0, a) + 1, sql.count("\n", 0, b) + 1, sql[a:b]))
    return out


_NO_SCHEMA = re.compile(r'schema "([^"]+)" does not exist')
_NO_SEQ = re.compile(r'relation "([^"]+)" does not exist')
_COLOCATED_DUMP = "cannot set colocation_id for non-colocated table"


def _run(container, db, sql):
    """Run `sql` as a file in `db`; [(statement, error)] for each statement the server
    rejected."""
    fd, path = tempfile.mkstemp(suffix=".sql", prefix="ybm_catalog_")
    with os.fdopen(fd, "w") as fh:
        fh.write(sql)
    try:
        out = container.file_sql(path, db=db).stdout
    finally:
        os.unlink(path)
    stmts = _statements(sql)
    failed = []
    for line in out.splitlines():
        m = _ERR_LINE.search(line)
        if m:
            n = int(m.group(1))
            st = next((s for s in stmts if s[0] <= n <= s[1]), None)
            failed.append((st[2] if st else "", m.group(2).strip()))
    return failed


def _repairs(failed, ddl):
    """Statements that supply what an incomplete dump left out, judged from the server's own
    errors: a missing schema, or a sequence a column default names."""
    out = []
    for stmt, err in failed:
        m = _NO_SCHEMA.search(err)
        if m:
            out.append('CREATE SCHEMA IF NOT EXISTS "%s";' % m.group(1))
        m = _NO_SEQ.search(err)
        if m and "nextval" in stmt.lower() and m.group(1) in stmt:
            out.append("CREATE SEQUENCE IF NOT EXISTS %s;" % m.group(1))
    return sorted(set(out))


def load(bundle, container, db, hash_default=True, colocated=False):
    """Load the bundle's DDL into a new database `db`. Returns (seconds, [(text, error)] for
    each statement the server rejected, {object: declared tablet count}, info). When the
    server's errors show the dump is incomplete in a known way (a schema or sequence it never
    created, or colocation ids from a colocated database), the gap is supplied and the
    rejected statements run again; info["repairs"] says what was added."""
    from .replay import strip_dump
    ddl, declared = one_tablet(strip_dump(bundle.ddl))
    head = "" if hash_default else "SET yb_use_hash_splitting_by_default = off;\n"
    info = {"repairs": [], "colocated": bool(colocated)}
    t0 = time.time()
    container.sql("CREATE DATABASE %s WITH colocation = %s;" % (
        db, "true" if colocated else "false"), db="yugabyte")
    failed = _run(container, db, head + ddl)
    if not colocated and any(_COLOCATED_DUMP in e for _, e in failed):
        # The dump carries colocation ids: it came from a colocated database.
        container.sql("DROP DATABASE %s;" % db, db="yugabyte")
        container.sql("CREATE DATABASE %s WITH colocation = true;" % db, db="yugabyte")
        info["colocated"] = True
        info["repairs"].append("database recreated as colocated (the dump sets colocation ids)")
        failed = _run(container, db, head + ddl)
    for _ in range(3):
        fixes = _repairs(failed, ddl)
        if not fixes:
            break
        info["repairs"].extend(fixes)
        retry = [s for s, _ in failed if s]
        failed = _run(container, db, head + "\n".join(fixes) + "\n" +
                      "".join(s.rstrip().rstrip(";") + ";\n" for s in retry))
        failed = [(s, e) for s, e in failed if s not in fixes]
    return time.time() - t0, failed, declared, info


def read(container, db):
    r = container.sql(QUERY, db=db, flags=("-tA",))
    text = next((l for l in r.stdout.splitlines() if l.startswith("{")), None)
    if text is None:
        raise RuntimeError("catalog query failed: " + (r.stderr or r.stdout)[-500:])
    return json.loads(text)


def build(cat, explicit=True, failed=(), declared=None, hash_default=True):
    """A schema.Schema from the catalog document `cat`. `explicit` says whether the source DDL
    annotated its keys (a ysql_dump does); when it did not, the layouts are the scratch
    server's defaults and keep the 'sharding inferred' caveat. Objects whose statements the
    server rejected come from the DDL text, as the text parser reads them."""
    sch = schema_mod.Schema()
    sch.db_colocated = bool(cat.get("db_colocated"))
    sch.hash_default = hash_default
    for t in cat.get("tables") or []:
        name = rel_key(t.get("schema"), t["name"])  # schema.name outside public
        tb = schema_mod.Table(name)
        tb.schema = t["schema"]
        for c in t.get("columns") or []:
            default = c.get("default") or ""
            tb.cols[c["name"]] = {"type": c["type"], "notnull": bool(c["notnull"]),
                                  "sequence": bool(c["identity"]) or "nextval(" in default}
            tb.col_order.append(c["name"])
        props = t.get("props") or {}
        # A partitioned parent has no DocDB table, so its properties are all NULL: leave
        # colocation to the database, as the DDL text does.
        tb.colocated = None if props.get("is_colocated") is None else (
            bool(props["is_colocated"]) or bool(props.get("tablegroup_oid")))
        tb.tablegroup = props.get("tablegroup_oid")
        if t.get("partkey"):
            m = re.match(r"(\w+)\s*\((.*)\)", t["partkey"])
            if m:
                tb.partition_by = (m.group(1).upper(), [x.strip().strip('"')
                                                        for x in m.group(2).split(",")])
        if t.get("parent"):
            tb.partition_of = rel_key(t.get("parent_schema"), t["parent"])
            bound = t.get("bound") or ""
            if bound.strip().upper() == "DEFAULT":
                tb.is_default = True
            m = re.search(r"TO \('([^']*)'", bound)
            if m:
                tb.bound_to = m.group(1)
        n = (declared or {}).get(name)
        if n and n > 1:
            tb.split = "INTO %d" % n
        sch.tables[name] = tb
    for name, t in sch.tables.items():
        if t.partition_of and t.partition_of in sch.tables:
            sch.tables[t.partition_of].partitions.append(name)
    for i in cat.get("indexes") or []:
        tmp = schema_mod.Schema()
        st = [rebase(s) for s in split_statements(tokenize(i["def"]))]
        if not st:
            continue
        schema_mod._parse_create_index(st[0], tmp, 0, i["def"])
        key, table = rel_key(i.get("schema"), i["name"]), rel_key(i.get("schema"), i["table"])
        idx = tmp.indexes.get(key)
        if idx is None or table not in sch.tables:
            continue
        n = (declared or {}).get(key)
        if n and n > 1:  # loaded with one tablet; the DDL declared more
            idx.split, idx.split_sql = "INTO %d" % n, "SPLIT INTO %d TABLETS" % n
        elif idx.split == "INTO 1":  # one tablet: the load's setting, not a declared layout
            idx.split = idx.split_sql = None
        idx.parent = rel_key(i.get("parent_schema") or i.get("schema"), i["parent"]) \
            if i.get("parent") else None
        for k in idx.keys:
            k.explicit = explicit
        if i["primary"]:
            idx.is_pk = True
            sch.tables[table].pk = idx
        else:
            idx.constraint = bool(i.get("constraint"))
            sch.indexes[idx.name] = idx
    for t in sch.tables.values():
        if t.split and t.pk and not t.pk.split:  # as the DDL text: the key's tablets are the table's
            t.pk.split = t.split
    for f in cat.get("fks") or []:
        sch.foreign_keys.append(schema_mod.ForeignKey(
            f["name"], rel_key(f.get("schema"), f["table"]), f.get("cols") or [],
            rel_key(f.get("ref_schema"), f["ref_table"]), f.get("ref_cols"),
            f.get("def")))
    if failed:
        text = schema_mod.parse("\n".join(s + ";" for s, _ in failed if s),
                                db_colocated=sch.db_colocated, hash_default=hash_default)
        for name, t in text.tables.items():
            if name not in sch.tables:
                sch.tables[name] = t
        for name, idx in text.indexes.items():
            if name not in sch.indexes and idx.table in sch.tables:
                sch.indexes[name] = idx
        for s, err in failed:
            sch.parse_notes.append("the scratch server rejected a statement (%s); its objects "
                                   "come from the DDL text: %s" % (err[:160], s[:120]))
    sch.resolve_modes()
    return sch


def attach(bundle, image=None, container=None, db="ybm_catalog"):
    """Build the schema from a scratch server and keep it on the bundle (bundle.catalog_schema,
    bundle.catalog_info), so every pass of the review uses it."""
    from . import analyze, replay
    hash_default = analyze.planner_settings(bundle)["hash_default"]
    if container is not None:
        sch, info = schema_for(bundle, container, db, hash_default=hash_default)
    else:
        img = image or replay.find_image(bundle.version)[0] or replay.find_image(None)[0]
        if not img:
            raise RuntimeError("no local yugabytedb/yugabyte image")
        with replay.Container(img, "ybm-catalog-%d" % os.getpid(),
                              master_flags="enforce_tablet_replica_limits=false",
                              tserver_flags=TSERVER_FLAGS) as c:
            sch, info = schema_for(bundle, c, db, hash_default=hash_default)
        info["image"] = img
    bundle.catalog_schema, bundle.catalog_info = sch, info
    return sch, info


def schema_for(bundle, container, db, hash_default=True, colocated=None):
    """Load, read and build; also returns timing and the rejected statements."""
    from . import preflight
    if colocated is None:
        colocated = _truthy(bundle.meta.get("colocated", ""))
    secs, failed, declared, loaded = load(bundle, container, db, hash_default=hash_default,
                                          colocated=colocated)
    t0 = time.time()
    cat = read(container, db)
    sch = build(cat, explicit=preflight._yb_metadata(bundle), failed=failed, declared=declared,
                hash_default=hash_default)
    for r in loaded["repairs"]:
        sch.parse_notes.append("the dump was incomplete; added on the scratch server: " + r)
    return sch, {"load_seconds": round(secs, 1), "read_seconds": round(time.time() - t0, 1),
                 "repairs": loaded["repairs"], "colocated": loaded["colocated"],
                 "rejected": [{"statement": s[:300], "error": e} for s, e in failed]}
