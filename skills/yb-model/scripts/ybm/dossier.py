"""Per-area dossiers: the findings on related objects in one place, what any change there must
keep true, and where findings pull in different directions.

An area is the tables a change on one of them can reach: partitions and their parent, and
tables linked by a foreign key. For each area the engine lists, deterministically:

- the findings on its tables and indexes, in plain words, with the options each rule offers;
- what must stay true: unique guarantees, the index each foreign key and each ON CONFLICT
  target relies on, the indexes ranked patterns read through (and how), partition copies, and
  indexes the usage counters show in use;
- tensions: a sketch that drops, rebuilds or re-keys something another entry relies on, and
  several findings that would change one object in different ways.

The engine does not choose between options: an additive option that checks clean is offered
as a suggestion for the user to confirm, and anything that drops, rebuilds or re-keys
something is always a decision.
"""

from . import changes as ch_mod
from . import schema as schema_mod
from . import sqlshape

RANKED = ("HOT", "WARM", "COLD", "UNRANKED")  # every pattern, as the option checks use
# Rebuilds that change which rows the index holds or how they are laid out: the patterns that
# read through the original may lose its order, its point lookups or the index altogether.
# A predicate also narrows a unique guarantee.
LAYOUT = {"partial", "bucket", "hash", "lead_with_other", "composite", "separate_hot_value",
          "full_index", "reorder"}
NARROWS = {"partial", "separate_hot_value"}


def change_class(changes):
    """What a finding's sketch does: rekey, rebuild (create then drop), add, drop, or None."""
    kinds = {c.kind for c in changes or []}
    if "rekey" in kinds:
        return "rekey"
    if "create" in kinds and "drop" in kinds:
        return "rebuild"
    if "create" in kinds:
        return "add"
    if "drop" in kinds:
        return "drop"
    return None


def options(rule, changes, guarded=False, checks=None, sketched=None, exclude=()):
    """The rule's options, the one the engine sketched marked (the first of the sketch's
    change class), each with what checking it alone found (safety.annotate): `checked` when
    the engine could model it, and its `costs`. An index that enforces a unique guarantee is
    never offered a drop. `sketched` names the modelled option when its DDL was withheld.
    `exclude`: option ids that do not apply to this finding's object (a primary key is
    re-keyed, an index rebuilt)."""
    cls = change_class(changes)
    out, marked = [], False
    for o in rule.get("options") or []:
        if (guarded and o["change"] == "drop") or o["id"] in exclude:
            continue
        o = dict(o)
        o["sketched"] = o["id"] == sketched if sketched else bool(
            cls and not marked and o["change"] == cls)
        marked = marked or o["sketched"]
        c = (checks or {}).get(o["id"])
        o["checked"] = bool(c and c["modelled"])
        o["costs"] = list(c["costs"]) if c else []
        out.append(o)
    return out


def _areas(sch):
    """Union-find over tables: partition links and foreign keys."""
    parent = {t: t for t in sch.tables}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def join(a, b):
        if a in parent and b in parent:
            parent[find(a)] = find(b)
    for t in sch.tables.values():
        if t.partition_of:
            join(t.name, t.partition_of)
    for fk in sch.foreign_keys:
        join(fk.table, fk.ref_table)
    groups = {}
    for t in sch.tables:
        groups.setdefault(find(t), []).append(t)
    return {t: sorted(groups[find(t)]) for t in sch.tables}


def _index_named(sch, name):
    if name in sch.indexes:
        return sch.indexes[name]
    return next((t.pk for t in sch.tables.values() if t.pk and t.pk.name == name), None)


def must_keep(sch, tables, patterns, an, ps, bundle):
    """What any change on these tables must keep true, as {kind, object, text}."""
    from . import safety
    tables = set(tables)
    out = []
    for table, keys, where, name in safety.constraints(sch):
        if table in tables:
            out.append({"kind": "unique", "object": name, "text": "%s keeps (%s) unique%s" % (
                name, ", ".join(sorted(k[5:] if k.startswith("expr:") else k for k in keys)),
                (" where %s" % where) if where else "")})
    for idx in list(sch.indexes.values()) + [t.pk for t in sch.tables.values() if t.pk]:
        if idx.table not in tables:
            continue
        for fk in sch.fks_on_index(idx):
            out.append({"kind": "fk", "object": idx.name, "text": "foreign key %s on %s "
                        "references %s through %s" % (fk.name, fk.table, idx.table, idx.name)})
    for p in patterns:
        for sh in sqlshape.flatten(p["shape"]):
            target = safety.conflict_target(sh)
            if not target or sh.tables[0] not in tables:
                continue
            arb = _arbiter(sch, sh)
            if arb:
                out.append({"kind": "arbiter", "object": arb, "text": "%s (INSERT ... ON "
                            "CONFLICT %s) decides conflicts with %s" % (
                                p["id"], ", ".join(target), arb)})
    amap = safety.access_map(sch, [p for p in patterns if p["weight"] in RANKED], an, ps["cbo"])
    for pid, tabs in sorted(amap.items(), key=lambda kv: int(kv[0][1:])):
        weight = next(p["weight"] for p in patterns if p["id"] == pid)
        for table, a in sorted(tabs.items()):
            if table not in tables or not a["path"]:
                continue
            how = [w for w, on in (("point lookup", a["point"]), ("in index order",
                                                                  a["order_ok"] is True),
                                   ("covering", a["covering"])) if on]
            out.append({"kind": "access", "object": a["path"], "text": "%s (%s) reads %s "
                        "through %s%s" % (pid, weight, table, a["path"],
                                          (" (%s)" % ", ".join(how)) if how else "")})
    for idx in sch.indexes.values():
        if idx.table not in tables or idx.parent:
            continue
        copies = an.partition_copies(sch, idx.name)
        if copies:
            out.append({"kind": "partition", "object": idx.name, "text": "%s has a copy on "
                        "%d partition(s); YSQL changes them only through %s" % (
                            idx.name, len(copies), idx.name)})
        u = an.index_usage_of(sch, bundle, idx.name)
        if u is not None and u["idx_scan"] > 0:
            out.append({"kind": "used", "object": idx.name, "text": "%s was scanned %d times "
                        "on the nodes captured" % (idx.name, u["idx_scan"])})
    return out


def _arbiter(sch, sh):
    """The unique index or constraint an ON CONFLICT statement relies on, or None."""
    from . import safety
    table = sh.tables[0]
    if sh.conflict_constraint:
        return sh.conflict_constraint if safety.has_arbiter(sch, sh) else None
    target = frozenset(sh.conflict_cols)
    tables = safety.arbiter_tables(sch, table)
    for t, k, w, n in safety.constraints(sch):
        if t in tables and k == target and (w is None or (
                sh.conflict_where is not None and safety._norm(w) == safety._norm(
                    sh.conflict_where))):
            return n
    return None


def tensions(findings, keep):
    """Sketches that remove what an entry relies on, and objects several findings would change
    in different ways."""
    out = []
    by_obj = {}
    for e in keep:
        by_obj.setdefault(e["object"], []).append(e)
    touched = {}
    for f in findings:
        chs = [ch_mod.Change.from_dict(c) for c in f.get("changes") or []]
        cls = change_class(chs)
        opt = next((o["id"] for o in f.get("options") or [] if o.get("sketched")), None)
        if cls == "rebuild":  # the replacement keeps what it does not change
            kinds = {"fk", "arbiter", "partition"} | (
                {"access", "used"} if opt in LAYOUT else set()) | (
                {"unique"} if opt in NARROWS else set())
        else:
            kinds = {"unique", "fk", "arbiter", "access", "partition", "used"}
        removed = [c.name for c in chs if c.kind in ("drop", "retire")]
        for name in removed:
            touched.setdefault(name, []).append((f, cls))
            relied = [e for e in by_obj.get(name, []) if e["kind"] in kinds]
            if relied:
                out.append("%s %s's sketch %s %s, which %s: %s. Check that the option you "
                           "choose keeps %s." % (
                               f["id"], f["rule"], {"drop": "drops", "rebuild": "replaces",
                                                    "rekey": "retires"}.get(cls, "changes"),
                               name, "other entries rely on" if len(relied) > 1 else
                               "something relies on", "; ".join(e["text"] for e in relied),
                               "them" if len(relied) > 1 else "it"))
        if f.get("index") and not removed:
            touched.setdefault(f["index"], []).append((f, cls))
    for name, fs in sorted(touched.items()):
        if len({f["id"] for f, _ in fs}) > 1:
            out.append("%s all concern %s, and their options change it in different ways: "
                       "decide them together, usually with one change to %s." % (
                           ", ".join("%s %s" % (f["id"], f["rule"]) for f, _ in fs), name, name))
    return out


def _object(f):
    return f.get("index") or f.get("table") or f["object"]


def suggestions_and_decisions(fs):
    """Split an area's findings. A suggestion: the only finding on its object, whose engine
    option only adds something (a new index) and was checked alone with no cost found; it is
    offered with its reasons, for the user to confirm. A decision: everything else with
    options: several findings on one object, or an option that drops, rebuilds or re-keys
    something (the user's call, however clean it checks), or one with a cost or no check. A
    note: a finding with no options (no schema change to choose)."""
    by_obj = {}
    for f in fs:
        if f.get("options"):
            by_obj.setdefault(_object(f), []).append(f)
    sugg, dec, notes = [], [], []
    for f in fs:
        if not f.get("options"):
            notes.append({"finding": f["id"], "rule": f["rule"], "object": f["object"],
                          "about": f["title"], "suggestion": f.get("fix")})
    for obj, group in sorted(by_obj.items()):
        o = next((o for o in group[0]["options"] if o.get("sketched")), None) \
            if len(group) == 1 else None
        if o is not None and o["change"] == "add" and o["checked"] and not o["costs"]:
            f = group[0]
            sugg.append({"finding": f["id"], "rule": f["rule"], "object": obj,
                         "about": f["title"], "option": o["id"], "label": o["label"],
                         "why": " ".join(x for x in (f["fact"], f.get("fix") or "") if x),
                         "check_first": o["check_first"],
                         "others": [x["label"] for x in f["options"] if x is not o]})
            continue
        dec.append({"object": obj, "findings": [
            {"finding": f["id"], "rule": f["rule"], "about": f["title"], "fact": f["fact"],
             "options": [{"id": x["id"], "label": x["label"], "change": x["change"],
                          "checked": x["checked"], "costs": x["costs"]}
                         for x in f["options"]]} for f in group]})
    return sugg, dec, notes


def build(sch, patterns, findings, rules, bundle, an, ps):
    """One dossier per area that has a finding, most severe first; findings tied to no table
    go to a last, general entry."""
    areas = _areas(sch)
    owner = {}
    for f in findings:
        if f.get("section") == "disputed":
            continue
        t = f.get("table") or (_index_named(sch, f["index"]).table
                               if f.get("index") and _index_named(sch, f["index"]) else None)
        key = tuple(areas[t]) if t in areas else ()
        owner.setdefault(key, []).append(f)
    sev = ["critical", "high", "medium", "low", "info"]
    out = []
    for key, fs in owner.items():
        notes = []
        for f in fs:
            notes.append({"finding": f["id"], "rule": f["rule"], "severity": f["severity"],
                          "object": f["object"], "about": f["title"], "fact": f["fact"],
                          "options": [o["label"] + (" (sketched)" if o.get("sketched") else "")
                                      for o in f.get("options") or []]})
        keep = must_keep(sch, key, patterns, an, ps, bundle) if key else []
        sugg, dec, other = suggestions_and_decisions(fs)
        out.append({"area": list(key) or ["(no single table)"],
                    "worst": min((sev.index(f["severity"]) for f in fs), default=4),
                    "findings": notes, "suggestions": sugg, "decisions": dec, "notes": other,
                    "must_keep": [e["text"] for e in keep],
                    "tensions": tensions(fs, keep) if key else []})
    out.sort(key=lambda d: (d["worst"], d["area"] == ["(no single table)"], d["area"]))
    for d in out:
        d["worst"] = sev[d["worst"]]
    return out
