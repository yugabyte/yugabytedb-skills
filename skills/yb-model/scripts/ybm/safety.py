"""Final safety pass over the engine's own recommendations.

Every recommended DDL is applied to a copy of the schema, first one finding at a time and then
all together, and the result is compared with the schema as it is:

1. Uniqueness: each PRIMARY KEY / UNIQUE guarantee must survive, on the same or fewer columns
   and with a predicate no narrower. A change that would lose one is amended with the UNIQUE
   index that keeps it, and the amendment is checked again.
2. ON CONFLICT targets: every upsert's conflict columns must still match a unique index.
3. Plans: every ranked pattern is re-planned statically; losing an access path, a point
   lookup, index order or coverage is a regression.
4. Measured use: dropping an index that pg_stat_user_indexes shows scanned is a regression.

The pass never deletes a recommendation. It amends DDL where the fix is mechanical and
otherwise raises a SAF finding next to the recommendation it concerns.
"""

import copy
import re

from . import schema as schema_mod
from .sqltok import tokenize, split_statements, rebase, match_paren
from . import sqlshape

# The index a table swap leaves behind on the retired table (analyze._old_index_note): gone
# afterwards, but never a DROP INDEX the reader runs.
_QNAME = r'((?:\w+|"(?:[^"]|"")+")(?:\.(?:\w+|"(?:[^"]|"")+"))?)'
RETIRED = re.compile(r'--\s*%s is not created on the new table' % _QNAME)
REKEY = re.compile(r'--\s*CREATE TABLE %s \(\.\.\. PRIMARY KEY \((.+)\)\)' % _QNAME)


def _key_of(text):
    """The relation key of a possibly qualified, possibly quoted name as DDL writes it."""
    name, _ = schema_mod._name(rebase(tokenize(text)), 0)
    return name


class _Stmt(list):
    """Tokens of one statement, with the SQL text their positions refer to."""
    sql = None


def changes_of(ddl):
    """Structured changes from an engine DDL string."""
    out = []
    if not ddl:
        return out
    for line in ddl.splitlines():
        m = REKEY.search(line)
        if m:
            name = _key_of(m.group(1))
            if name and name.endswith("_new"):
                out.append(("rekey", name[:-4], m.group(2)))
        m = RETIRED.search(line)
        if m:
            out.append(("retire", _key_of(m.group(1))))
    body = "\n".join(l for l in ddl.splitlines() if not l.lstrip().startswith("--"))
    body = re.sub(r"--[^\n]*", "", body)
    for st in split_statements(tokenize(body)):
        st = rebase(st)
        if not st:
            continue
        words = [t.up for t in st[:4] if t.kind == "word"]
        up = [t.up for t in st if t.kind == "word"]
        if up[:2] == ["ALTER", "TABLE"] and "DROP" in up and "CONSTRAINT" in up:
            table, _ = schema_mod._name(st, schema_mod._skip_words(st, 2, "IF", "EXISTS", "ONLY"))
            k = next(n for n, t in enumerate(st) if t.kind == "word" and t.up == "CONSTRAINT")
            cname, _ = schema_mod._name(st, schema_mod._skip_words(st, k + 1, "IF", "EXISTS"))
            if cname:  # a constraint's index lives in its table's schema
                out.append(("drop", schema_mod.in_schema_of(table, cname)))
            continue
        if words[:1] == ["DROP"] and "INDEX" in words:
            name, _ = schema_mod._name(st, schema_mod._skip_words(
                st, 2, "CONCURRENTLY", "IF", "EXISTS"))
            if name:
                out.append(("drop", name))
        elif words[:1] == ["CREATE"] and "INDEX" in words:
            stmt = _Stmt(st)
            stmt.sql = body
            out.append(("create", stmt))
    return out


def an_partition_copies(sch, name):
    from .analyze import partition_copies
    return partition_copies(sch, name)


def apply(sch, changes):
    after = copy.deepcopy(sch)
    for ch in changes:
        if ch[0] in ("drop", "retire"):  # retire: left behind with a swapped-out table
            # Dropping a partitioned index drops every partition's copy of it.
            for n in [ch[1]] + an_partition_copies(after, ch[1]):
                after.indexes.pop(n, None)
        elif ch[0] == "create":
            schema_mod._parse_create_index(ch[1], after, 0)
        elif ch[0] == "rekey":
            t = after.tables.get(ch[1])
            if t is not None:
                keys = schema_mod._parse_keys(rebase(tokenize(ch[2])))
                t.pk = schema_mod.Index(t.pk.name if t.pk else ch[1] + "_pkey", ch[1], keys,
                                        unique=True, is_pk=True)
    after.resolve_modes()
    return after


def _norm(where):
    return re.sub(r"[()\s]+", " ", (where or "").lower()).strip()


def _keyset(idx):
    return frozenset(k.col or ("expr:" + k.expr) for k in idx.keys)


def constraints(sch):
    out = []
    for t in sch.tables.values():
        if t.pk:
            out.append((t.name, _keyset(t.pk), None, t.pk.name))
    for i in sch.indexes.values():
        if i.unique:
            out.append((i.table, _keyset(i), i.where, i.name))
    return out


def _covers(w, where, keys, nulls_conflict=False):
    """Does a unique index with predicate `w` (None: every row) check every row that one with
    predicate `where` checks, for duplicates on `keys`? Two rows whose key holds a NULL never
    conflict (unless NULLS NOT DISTINCT: `nulls_conflict`), so `col IS NOT NULL` on a key column
    changes nothing; with no OR, the predicate covers at least as much when its remaining
    conjuncts are a subset of the other's."""
    if w is None:
        return True
    a, b = _norm(w), _norm(where)
    if a == b:
        return True
    if " or " in " %s " % a or " or " in " %s " % (b or ""):
        return False
    guard = set() if nulls_conflict else {
        "%s is not null" % k.lower() for k in keys if not k.startswith("expr:")}
    return set(a.split(" and ")) - guard <= set(b.split(" and ") if b else []) - guard


def _nnd(sch, name):
    return bool(getattr(sch.indexes.get(name), "nulls_not_distinct", False))


def lost_uniqueness(before, after):
    have = constraints(after)
    lost = []
    for table, keys, where, name in constraints(before):
        nnd = _nnd(before, name)  # then the replacement must treat NULLs as equal too
        ok = any(t == table and k <= keys and (not nnd or _nnd(after, n)) and
                 _covers(w, where, keys, nnd) for t, k, w, n in have)
        if not ok:
            lost.append((table, keys, where, name))
    return lost


def has_arbiter(sch, sh):
    """Can INSERT ... ON CONFLICT in shape `sh` find its arbiter in `sch`? ON CONSTRAINT names
    a primary key or unique constraint (a unique index without a constraint does not count);
    (cols) needs a unique index on exactly those columns, a partial one only when the statement
    repeats its predicate in ON CONFLICT (cols) WHERE ..."""
    table = sh.tables[0]
    if sh.conflict_constraint:
        t = sch.tables.get(table)
        return bool(t and t.pk and t.pk.name == sh.conflict_constraint) or any(
            i.table == table and i.unique and getattr(i, "constraint", False) and
            i.name == sh.conflict_constraint for i in sch.indexes.values())
    target = frozenset(sh.conflict_cols)
    return any(t == table and k == target and
               (w is None or (sh.conflict_where is not None and
                              _norm(w) == _norm(sh.conflict_where)))
               for t, k, w, _ in constraints(sch))


def conflict_target(sh):
    """The ON CONFLICT target of `sh` as the report names it, or None when it has none."""
    if sh.kind != "insert" or not sh.tables:
        return None
    if sh.conflict_constraint:
        return ["ON CONSTRAINT " + sh.conflict_constraint]
    return sorted(sh.conflict_cols) or None


def lost_conflict_targets(before, after, patterns):
    lost = []
    for p in patterns:
        for sh in sqlshape.flatten(p["shape"]):
            target = conflict_target(sh)
            if target and has_arbiter(before, sh) and not has_arbiter(after, sh):
                lost.append((p["id"], sh.tables[0], target))
    return lost


def access_map(sch, patterns, an, cbo):
    """pattern id -> table -> access summary, as the static planner sees it."""
    out = {}
    for p in patterns:
        for sh in sqlshape.flatten(p["shape"]):
            if sh.kind == "insert":
                continue
            for table in sh.tables:
                if table not in sch.tables:
                    continue
                preds = sh.preds_for(table)
                if not preds and not sh.order:
                    continue
                ops = an._pred_index(preds)
                paths = []
                for idx in sch.indexes_on(table):
                    r = an.eval_path(idx, ops, sh, table, cbo)
                    if r["usable"]:
                        r["order_ok"] = an.order_ok(idx, ops, sh, table)
                        r["covering"], r["missing"] = an.covering(idx, sh, table,
                                                                  sch.tables[table].cols)
                    paths.append(r)
                best = an.choose(paths, limit=bool(sh.limit))
                walk = an.ordered_walk(sch, table, ops, sh) if best is None else None
                out.setdefault(p["id"], {})[table] = {
                    "path": best["index"] if best else (walk.name if walk else None),
                    "bound": best["bound"] if best else 0,
                    "point": bool(best and best["full_unique"]),
                    "order_ok": best.get("order_ok") if best else (True if walk else None),
                    "covering": bool(best and best.get("covering")),
                }
    return out


def regressions(before_map, after_map, weights):
    out = []
    for pid, tables in sorted(before_map.items(), key=lambda kv: int(kv[0][1:])):
        for table, b in sorted(tables.items()):
            a = after_map.get(pid, {}).get(table)
            if a is None:
                continue
            why = []
            if b["path"] and not a["path"]:
                why.append("loses its access path (%s) and falls back to a scan" % b["path"])
            elif b["path"] and a["path"]:
                if b["point"] and not a["point"]:
                    why.append("is no longer a point lookup")
                if a["bound"] < b["bound"]:
                    why.append("binds fewer key columns (%d -> %d)" % (b["bound"], a["bound"]))
                if b["order_ok"] is True and a["order_ok"] is False:
                    why.append("loses index order and needs a sort")
                if b["covering"] and not a["covering"]:
                    why.append("loses coverage (%s -> %s) and needs a base-table fetch"
                               % (b["path"], a["path"]))
            if why:
                out.append({"pattern": pid, "weight": weights.get(pid), "table": table,
                            "why": "; ".join(why)})
    return out


def _index_of(sch, st):
    tmp = copy.deepcopy(sch)
    before = set(tmp.indexes)
    schema_mod._parse_create_index(st, tmp, 0, getattr(st, "sql", None))
    new = [tmp.indexes[n] for n in tmp.indexes if n not in before]
    return new[0] if new else None


def _ddl_for(sch, idx, orig, splits):
    """The merged replacement `idx` of `orig`. Its SPLIT is the original's when the key layout
    is unchanged, otherwise the one a merged replacement chose for its new layout (bucketing)."""
    t = sch.tables.get(orig.table)
    same = idx.signature() == orig.signature()
    split = None if same else next((sp for sp in splits if sp), None)
    keys = None if same else idx.signature(quote=True)
    base = schema_mod.Index(orig.name, orig.table, orig.keys, unique=idx.unique,
                            include=[], where=None, split=orig.split, method=orig.method,
                            split_sql=orig.split_sql,
                            nulls_not_distinct=idx.nulls_not_distinct)
    return schema_mod.index_sql(base, idx.name, keys=keys, add_include=idx.include,
                                add_where=idx.where, split=split,
                                colocated=sch.is_colocated(orig.table),
                                partitioned=bool(t and t.partition_by)) + "\n" + \
        schema_mod.drop_sql(orig, idx.name)


def merge_replacements(sch, col):
    """Several findings may each rebuild the same index (for example a NULL guard and a
    covering INCLUDE). Applied one after the other they drop the original twice and the later
    rebuild undoes the earlier one. Merge them into one replacement on the highest-ranked
    finding: union of INCLUDE columns, conjunction of predicates, UNIQUE if any is."""
    by_target = {}
    for key in sorted(col.items):
        f = col.items[key]
        if getattr(f, "disputed", None):
            continue  # not recommended: replay disputed it
        chs = changes_of(f.ddl)
        drops = [c[1] for c in chs if c[0] == "drop"]
        creates = [c[1] for c in chs if c[0] == "create"]
        if len(drops) == 1 and len(creates) == 1:
            by_target.setdefault(drops[0], []).append((f, creates[0]))
    merged = []
    for target, items in sorted(by_target.items()):
        if len(items) < 2 or target not in sch.indexes:
            continue
        orig = sch.indexes[target]
        idxs = [_index_of(sch, st) for _, st in items]
        if any(i is None for i in idxs):
            continue
        if sch.fks_on_index(orig) and any(i.where for i in idxs):
            continue  # a partial index cannot back the foreign key: each fix is judged alone
        keys = max(idxs, key=lambda i: len(i.keys)).keys
        include = sorted({c for i in idxs for c in i.include} - {k.col for k in keys})
        wheres, seen = [], set()
        for i in idxs:
            if i.where and i.where not in seen:
                seen.add(i.where)
                wheres.append(i.where_sql or i.where)  # as written: keeps quoted names
        out = schema_mod.Index("%s_v2" % target, orig.table, keys,
                               unique=orig.unique or any(i.unique for i in idxs),
                               nulls_not_distinct=orig.nulls_not_distinct,
                               include=include,
                               where=" AND ".join("(%s)" % w for w in wheres) if wheres else None)
        rank = sorted(items, key=lambda it: (["critical", "high", "medium", "low",
                                              "info"].index(it[0].severity), it[0].rule))
        lead = rank[0][0]
        lead.ddl = _ddl_for(sch, out, orig, [i.split_sql for i in idxs
                                             if i.signature() == out.signature()])
        others = [f for f, _ in rank[1:]]
        for f in others:
            f.ddl = None
            f.fix = (f.fix or "") + (" Merged with the %s fix for %s into one replacement, so "
                                     "the index is rebuilt once." % (lead.rule, target))
        lead.fix = (lead.fix or "") + (" This replacement also carries the %s change(s), merged "
                                       "by the safety check." % ", ".join(f.rule for f in others))
        merged.append({"finding_rule": lead.rule, "object": lead.obj, "changes": 2,
                       "status": "merged", "checks": [
                           "%d fixes rebuilt %s; merged into one %sindex%s%s" % (
                               len(items), target, "UNIQUE " if out.unique else "",
                               (" INCLUDE (%s)" % ", ".join(include)) if include else "",
                               (" WHERE %s" % out.where) if out.where else "")]})
    return merged


def reintroduced(sch_after, created_names, bundle, an):
    """Defects a new index brings back: a NULL-heavy hash lead without a guard, or a
    boolean key."""
    out = []
    for n in created_names:
        idx = sch_after.indexes.get(n)
        if not idx or not idx.keys or not idx.keys[0].col:
            continue
        t = sch_after.tables.get(idx.table)
        lead = idx.keys[0]
        st = an.col_stats(bundle, sch_after, idx.table, lead.col)
        # As STA001: under UNIQUE ... NULLS NOT DISTINCT at most one row has a NULL key.
        if lead.mode == "HASH" and st and (st["null_frac"] or 0) >= an.THRESHOLDS["null_frac_flag"] \
                and not (idx.unique and idx.nulls_not_distinct) \
                and not (idx.where and re.search(r"\b%s\s+is\s+not\s+null" % re.escape(lead.col),
                                                 idx.where, re.I)):
            out.append("%s hashes %s.%s again without WHERE %s IS NOT NULL (null_frac %.2f)" % (
                n, idx.table, lead.col, lead.col, st["null_frac"]))
        if t and t.cols.get(lead.col, {}).get("type") in ("boolean", "bool") and len(idx.keys) == 1:
            out.append("%s indexes a boolean" % n)
    return out


def ddl_conflicts(all_changes):
    drops, creates, out = {}, {}, []
    for ch in all_changes:
        if ch[0] == "drop":
            drops[ch[1]] = drops.get(ch[1], 0) + 1
    for n, k in sorted(drops.items()):
        if k > 1:
            out.append("DROP INDEX %s appears %d times; the second fails" % (n, k))
    return out


def _fk_backed(after, fk):
    """Is there a unique, non-partial index (or the primary key) on exactly the columns `fk`
    references, which the key can be created against?"""
    t = after.tables.get(fk.ref_table)
    if not fk.ref_cols:
        return bool(t and t.pk)
    want = set(fk.ref_cols)
    return any(i.table == fk.ref_table and (i.unique or i.is_pk) and not i.where and
               all(k.col for k in i.keys) and {k.col for k in i.keys} == want
               for i in list(after.indexes.values()) + ([t.pk] if t and t.pk else []))


def _repoint_fks(ddl, blocked):
    """The DDL with each foreign key that depends on a dropped index dropped just before that
    drop and added back after it, unvalidated and then validated, as written (options kept)."""
    out = []
    for line in ddl.split("\n"):
        chs = changes_of(line)
        fks = [fk for name, fk in blocked if chs == [("drop", name)]]
        for fk in fks:
            out.append("ALTER TABLE %s DROP CONSTRAINT %s;" % (schema_mod.qn(fk.table),
                                                              schema_mod.qi(schema_mod.bare(fk.name))))
        out.append(line)
        for fk in fks:
            out.append("ALTER TABLE %s ADD CONSTRAINT %s %s%s;" % (
                schema_mod.qn(fk.table), schema_mod.qi(schema_mod.bare(fk.name)), fk.clause,
                "" if re.search(r"\bNOT\s+VALID\b", fk.clause, re.I) else " NOT VALID"))
            out.append("ALTER TABLE %s VALIDATE CONSTRAINT %s;" % (schema_mod.qn(fk.table),
                                                                  schema_mod.qi(schema_mod.bare(fk.name))))
    return "\n".join(out)


def check(sch, patterns, col, an, ps, bundle, Finding):
    """Run the pass; amends findings in place, adds SAF findings, returns a report."""
    weights = {p["id"]: p["weight"] for p in patterns}
    merged_rows = merge_replacements(sch, col)
    base_map = access_map(sch, patterns, an, ps["cbo"])
    report = []
    all_changes = []
    for key in sorted(col.items, key=lambda k: (k[0], k[1])):
        f = col.items[key]
        if getattr(f, "disputed", None):
            continue  # not recommended: replay disputed it
        chs = changes_of(f.ddl)
        if not chs:
            continue
        row = {"finding_rule": f.rule, "object": f.obj, "changes": len(chs), "checks": [],
               "status": "ok"}
        # A foreign key depends on the unique index it was created against: that index can
        # only be dropped after the key. Re-point the key when the change leaves an index that
        # can back it; otherwise the DDL would fail, so it is withheld. A primary-key change
        # is a table swap whose comment lines already say to move the keys around it.
        blocked = [(c[1], fk) for c in chs if c[0] == "drop" and c[1] in sch.indexes
                   for fk in sch.fks_on_index(sch.indexes[c[1]])] \
            if not any(c[0] == "rekey" for c in chs) else []
        if blocked:
            backed = apply(sch, chs)
            if all(_fk_backed(backed, fk) for _, fk in blocked):
                f.ddl = _repoint_fks(f.ddl, blocked)
                row["checks"].append("foreign key %s re-pointed: dropped before %s and added "
                                     "back NOT VALID, then validated" % (
                                         ", ".join(fk.name for _, fk in blocked),
                                         ", ".join(sorted({n for n, _ in blocked}))))
                row["status"] = "amended"
            else:
                f.fix = (f.fix or "") + (" Not applied: %s, and only a unique index without a "
                                         "predicate on exactly the referenced columns can "
                                         "back a foreign key, so the DDL is withheld." % "; ".join(
                                             "foreign key %s on %s depends on %s" % (
                                                 fk.name, fk.table, n) for n, fk in blocked))
                f.ddl = None
                row["checks"].append("withheld: %s" % "; ".join(
                    "foreign key %s depends on %s" % (fk.name, n) for n, fk in blocked))
                row["status"] = "withheld"
                report.append(row)
                continue
        after = apply(sch, chs)
        lost = lost_uniqueness(sch, after)
        if lost:
            adds = []
            for table, keys, where, name in lost:
                src = sch.indexes.get(name)
                if src is not None and src.where_sql:
                    where = src.where_sql  # as written: keeps quoted names
                cols = sorted(keys)
                iname = "%s_%s_uniq" % (table, "_".join(re.sub(r"\W+", "_", c) for c in cols))
                keycols = [("(%s)" % c[5:]) if c.startswith("expr:") else schema_mod.qi(c)
                           for c in cols]
                group = ("%s" % ", ".join("%s ASC" % c for c in keycols)
                         if sch.is_colocated(table) else "(%s) HASH" % ", ".join(keycols))
                adds.append("CREATE UNIQUE INDEX CONCURRENTLY %s ON %s (%s)%s%s;  -- added "
                            "by the safety check: keeps the uniqueness %s enforces" % (
                                schema_mod.qi(schema_mod.ident(schema_mod.bare(iname))),
                                schema_mod.qn(table),
                                group, " NULLS NOT DISTINCT" if _nnd(sch, name) else "",
                                (" WHERE %s" % where) if where else "", name))
            f.ddl = f.ddl + "\n" + "\n".join(adds)
            chs = changes_of(f.ddl)
            after = apply(sch, chs)
            still = lost_uniqueness(sch, after)
            row["checks"].append("uniqueness: %s; %s" % (
                ", ".join("%s(%s)" % (n, ", ".join(sorted(k))) for _, k, _, n in lost),
                "amended" if not still else "NOT fixed by amendment"))
            row["status"] = "amended" if not still else "warn"
            f.fix = (f.fix or "") + (" The safety check added a UNIQUE index to keep the "
                                     "guarantee of %s." % ", ".join(n for *_, n in lost))
        conf = lost_conflict_targets(sch, after, patterns)
        if conf:
            row["checks"].append("ON CONFLICT target lost for %s" % ", ".join(
                "%s (%s)" % (pid, ", ".join(c)) for pid, _, c in conf))
            row["status"] = "warn"
        regs = regressions(base_map, access_map(after, patterns, an, ps["cbo"]), weights)
        if regs:
            row["checks"].append("plans: " + "; ".join(
                "%s on %s %s" % (r["pattern"], r["table"], r["why"]) for r in regs))
            row["status"] = "warn"
        replaces = any(c[0] in ("create", "rekey") for c in chs)
        for ch in chs:
            # A replacement is judged by the plan check; a pure drop also by measured use.
            if ch[0] == "drop" and not replaces:
                u = bundle.index_usage.get(ch[1])
                if u is not None and u["idx_scan"] > 0:
                    row["checks"].append("drops %s, which was scanned %d times" % (
                        ch[1], u["idx_scan"]))
                    row["status"] = "warn"
        if row["status"] == "warn":
            worst = min((["HOT", "UNRANKED", "WARM", "COLD"].index(r["weight"] or "COLD")
                         for r in regs), default=1)
            sev = ["high", "high", "medium", "low"][worst]
            tiny = regs and all((bundle.reltuples.get(r["table"]) or 1e9) <
                                an.THRESHOLDS["tiny_rows"] for r in regs)
            if tiny:
                sev = "info"
                row["checks"].append("affected tables are under 10k rows, so the cost is small")
            col.add(Finding("SAF001", sev, "confirmed", "%s %s" % (f.rule, f.obj),
                            "Applying the recommendation for %s %s would cause: %s." % (
                                f.rule, f.obj, " | ".join(row["checks"])),
                            sorted({r["pattern"] for r in regs}), table=f.table))
            f.fix = (f.fix or "") + " See SAF001: the safety check found a side effect."
        if not row["checks"]:
            row["checks"].append("uniqueness, ON CONFLICT targets and plans unchanged")
        created = [c[1] for c in chs if c[0] == "create"]
        names = [i.name for i in (_index_of(sch, st) for st in created) if i]
        back = reintroduced(after, names, bundle, an)
        if back:
            row["checks"].append("reintroduces: " + "; ".join(back))
            row["status"] = "warn"
            col.add(Finding("SAF001", "high", "confirmed", "%s %s" % (f.rule, f.obj),
                            "The recommendation for %s %s would bring back a defect: %s." % (
                                f.rule, f.obj, "; ".join(back)), table=f.table))
        report.append(row)
        all_changes.extend(chs)

    # Everything together: catches two fixes that are each safe but not combined.
    if all_changes:
        after = apply(sch, all_changes)
        redundant = []
        combo = {"finding_rule": "ALL", "object": "all recommendations together",
                 "changes": len(all_changes), "checks": [], "status": "ok"}
        lost = lost_uniqueness(sch, after)
        if lost:
            combo["checks"].append("uniqueness lost: " + ", ".join(n for *_, n in lost))
        conf = lost_conflict_targets(sch, after, patterns)
        if conf:
            combo["checks"].append("ON CONFLICT target lost: " + ", ".join(p for p, _, _ in conf))
        combo["checks"].extend(ddl_conflicts(all_changes))
        created_all = [i.name for i in (_index_of(sch, c[1]) for c in all_changes
                                        if c[0] == "create") if i and i.name in after.indexes]
        combo["checks"].extend("reintroduces: " + x
                               for x in reintroduced(after, created_all, bundle, an))
        # A rebuilt index that another index in the final schema makes redundant.
        for n in created_all:
            a = after.indexes.get(n)
            if not a or a.unique:
                continue
            ak = [k.col for k in a.keys]
            for b in after.indexes.values():
                if b.name == n or b.table != a.table or _norm(b.where) != _norm(a.where):
                    continue
                bk = [k.col for k in b.keys]
                if len(bk) > len(ak) and bk[:len(ak)] == ak and \
                        [k.mode for k in b.keys[:len(ak)]] == [k.mode for k in a.keys]:
                    redundant.append("%s is a prefix of %s with the same predicate: drop the "
                                     "original instead of rebuilding it" % (n, b.name))
                    break
        single = {(r_pat) for row in report for c in row["checks"]
                  for r_pat in re.findall(r"\b(P\d+) on ", c)}
        regs = [r for r in regressions(base_map, access_map(after, patterns, an, ps["cbo"]),
                                       weights) if r["pattern"] not in single]
        if regs:
            combo["checks"].append("plans: " + "; ".join(
                "%s on %s %s" % (r["pattern"], r["table"], r["why"]) for r in regs))
        if redundant:
            col.add(Finding("SAF003", "low", "confirmed", "redundant rebuilds",
                            "After the recommended changes: %s." % "; ".join(redundant)))
            combo["checks"].extend("note: " + r for r in redundant)
        if any(not c.startswith("note: ") for c in combo["checks"]):
            combo["status"] = "warn"
            col.add(Finding("SAF002", "high", "confirmed", "combined recommendations",
                            "Applying all recommendations together would cause: %s." %
                            " | ".join(combo["checks"]), sorted({r["pattern"] for r in regs})))
        if combo["status"] != "warn":
            combo["checks"].insert(0, "uniqueness, ON CONFLICT targets, plans and DDL "
                                      "consistency unchanged")
        report.append(combo)
    return merged_rows + report
