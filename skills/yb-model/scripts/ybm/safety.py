"""Safety checks of the options the findings offer.

Each modelled option is applied alone to a copy of the schema and compared with the schema as
it is:

1. Uniqueness: each PRIMARY KEY / UNIQUE guarantee must survive, on the same or fewer columns
   and with a predicate no narrower.
2. ON CONFLICT targets: every upsert's conflict columns must still match a unique index.
3. Plans: every ranked pattern is re-planned statically; losing an access path, a point
   lookup, index order or coverage is a regression.
4. Measured use: dropping an index that pg_stat_user_indexes shows scanned is a regression.

Every option the engine can model is checked this way, one at a time, and what it would
lose is attached to the option (annotate). Nothing is amended and no option is chosen: the
reviewer and the user choose, knowing the costs, and `yb-model.py plan` checks the combination
they choose (plan.py). The one mechanical step is rendering: a sketch that drops an index a
foreign key depends on carries the key's drop and re-creation around it when its replacement
can back the key, and is withheld when nothing can.
"""

import copy
import re

from . import changes as ch_mod
from . import schema as schema_mod
from .sqltok import tokenize, rebase
from . import sqlshape

def changes_of(ddl):
    """Structured changes from hand-written DDL (tests, an older review.json). The engine's own
    findings carry theirs (Finding.changes) and are never read back from text."""
    return ch_mod.parse(ddl)


def an_partition_copies(sch, name):
    from .analyze import partition_copies
    return partition_copies(sch, name)


def apply(sch, changes):
    """The schema model after the changes (changes.Change; checks and foreign-key steps leave
    the model as it is)."""
    after = copy.deepcopy(sch)
    for ch in changes:
        if ch.kind in ("drop", "retire"):  # retire: left behind with a swapped-out table
            # Dropping a partitioned index drops every partition's copy of it.
            for n in [ch.name] + an_partition_copies(after, ch.name):
                after.indexes.pop(n, None)
        elif ch.kind == "create":
            names = set(after.indexes)
            schema_mod._parse_create_index(ch.stmt, after, 0)
            for n in set(after.indexes) - names:  # the server builds a copy on each partition
                _copy_to_partitions(after, after.indexes[n])
        elif ch.kind == "rekey":
            t = after.tables.get(ch.table)
            if t is not None:
                keys = schema_mod._parse_keys(rebase(tokenize(ch.key)))
                t.pk = schema_mod.Index(t.pk.name if t.pk else ch.table + "_pkey", ch.table,
                                        keys, unique=True, is_pk=True)
    after.resolve_modes()
    return after


def _copy_to_partitions(sch, idx):
    """CREATE INDEX on a partitioned table creates an attached copy on every partition."""
    t = sch.tables.get(idx.table)
    for part in (t.partitions if t else []):
        c = copy.deepcopy(idx)
        c.name = schema_mod.in_schema_of(part, "%s_%s" % (schema_mod.bare(part),
                                                          schema_mod.bare(idx.name)))
        c.table, c.parent = part, idx.name
        sch.indexes[c.name] = c
        _copy_to_partitions(sch, c)


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
        if i.unique and not getattr(i, "parent", None):  # a copy's guarantee is its parent's
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
    tables = arbiter_tables(sch, table)
    return any(t in tables and k == target and
               (w is None or (sh.conflict_where is not None and
                              _norm(w) == _norm(sh.conflict_where)))
               for t, k, w, _ in constraints(sch))


def arbiter_tables(sch, table):
    """The table an upsert names and, for a partition, its ancestors: a partition's copy of a
    partitioned unique index (listed on the parent) is its arbiter."""
    out, t = [table], sch.tables.get(table)
    while t is not None and t.partition_of and t.partition_of not in out:
        out.append(t.partition_of)
        t = sch.tables.get(t.partition_of)
    return out


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
                    "gap": list((best.get("binding") or {}).get("gap") or []) if best else [],
                    "after_gap": list((best.get("binding") or {}).get("after_gap") or [])
                    if best else [],
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
                if a.get("after_gap") and not b.get("after_gap"):
                    why.append("binds %s only after an unbound %s, a seek per value of it"
                               % (", ".join(a["after_gap"]), ", ".join(a["gap"])))
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
    drops, out = {}, []
    for ch in all_changes:
        if ch.kind == "drop":
            drops[ch.name] = drops.get(ch.name, 0) + 1
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


def _repoint_fks(changes, blocked):
    """The changes with each foreign key that depends on a dropped index dropped just before
    that drop and added back after it, unvalidated and then validated, as written (options
    kept)."""
    out = []
    for c in changes:
        fks = [fk for name, fk in blocked if c.kind == "drop" and c.name == name]
        for fk in fks:
            out.append(ch_mod.fk("ALTER TABLE %s DROP CONSTRAINT %s;" % (
                schema_mod.qn(fk.table), schema_mod.qi(schema_mod.bare(fk.name)))))
        out.append(c)
        for fk in fks:
            out.append(ch_mod.fk("ALTER TABLE %s ADD CONSTRAINT %s %s%s;" % (
                schema_mod.qn(fk.table), schema_mod.qi(schema_mod.bare(fk.name)), fk.clause,
                "" if re.search(r"\bNOT\s+VALID\b", fk.clause, re.I) else " NOT VALID")))
            out.append(ch_mod.fk("ALTER TABLE %s VALIDATE CONSTRAINT %s;" % (
                schema_mod.qn(fk.table), schema_mod.qi(schema_mod.bare(fk.name)))))
    return out


def costs(sch, patterns, an, ps, bundle, chs, base_map, weights):
    """What a set of changes, applied alone to the schema model, would lose. Returns (costs,
    changes): the changes with foreign-key steps around a drop whose replacement can back the
    key, or None when a foreign key depends on a dropped index nothing replaces."""
    model = ch_mod.model(chs)
    out = []
    if not model:
        return out, chs
    blocked = [(c.name, fk) for c in model if c.kind == "drop" and c.name in sch.indexes
               for fk in sch.fks_on_index(sch.indexes[c.name])] \
        if not any(c.kind == "rekey" for c in model) else []
    if blocked:
        if all(_fk_backed(apply(sch, model), fk) for _, fk in blocked):
            chs = _repoint_fks(chs, blocked)
        else:
            out += ["foreign key %s on %s depends on %s, and nothing in this option can back "
                    "it" % (fk.name, fk.table, n) for n, fk in blocked]
            chs = None
    after = apply(sch, model)
    out += ["loses the unique guarantee of %s (%s)" % (n, ", ".join(sorted(k)))
            for _, k, _, n in lost_uniqueness(sch, after)]
    out += ["%s loses its ON CONFLICT target (%s)" % (pid, ", ".join(c))
            for pid, _, c in lost_conflict_targets(sch, after, patterns)]
    out += ["%s (%s) on %s %s" % (r["pattern"], r["weight"], r["table"], r["why"])
            for r in regressions(base_map, access_map(after, patterns, an, ps["cbo"]), weights)]
    if not any(c.kind in ("create", "rekey") for c in model):
        for c in model:
            u = an.index_usage_of(sch, bundle, c.name) if c.kind == "drop" else None
            if u is not None and u["idx_scan"] > 0:
                out.append("drops %s, which was scanned %d times" % (c.name, u["idx_scan"]))
    created = [i.name for i in (_index_of(sch, c.stmt) for c in model if c.kind == "create")
               if i]
    out += ["brings back a defect: " + x for x in reintroduced(after, created, bundle, an)]
    return out, chs


def modelled(f, option, sch, an):
    """The changes that stand for one option of a finding, or None when the engine has no
    model of it: the engine's own sketch, a checked drop of a non-unique index, or nothing for
    an option that changes no schema."""
    if option["change"] == "none":
        return []
    if option.get("sketched"):
        return list(f.changes)
    if option["change"] == "drop" and f.index and f.index in sch.indexes and \
            not (sch.indexes[f.index].unique or getattr(sch.indexes[f.index], "parent", None)):
        return an._check_then_drop(f.index, sch=sch)
    return None


def annotate(sch, patterns, col, an, ps, bundle, rules):
    """Check every option the engine can model, one at a time, and attach the result to the
    option: never a change to it, and never a choice between them (the reviewer and the user
    choose; `yb-model.py plan` checks the combination they choose)."""
    from .dossier import options as options_of
    weights = {p["id"]: p["weight"] for p in patterns}
    base_map = access_map(sch, patterns, an, ps["cbo"])
    for f in col.items.values():
        if getattr(f, "disputed", None):
            continue
        checks = {}
        for o in options_of(rules.get(f.rule, {}), f.changes, guarded=f.guarded):
            chs = modelled(f, o, sch, an)
            if chs is None:
                checks[o["id"]] = {"modelled": False, "costs": []}
                continue
            found, rendered = costs(sch, patterns, an, ps, bundle, chs, base_map, weights)
            checks[o["id"]] = {"modelled": True, "costs": found}
            if o.get("sketched"):
                f.sketched_option = o["id"]
                f.ddl = rendered  # foreign-key steps added, or withheld when nothing backs it
        f.option_checks = checks
