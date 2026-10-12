"""Tests for the yb-model engine. Standard library only.

    python3 -m unittest discover -s skills/yb-model/scripts -p 'test_*.py' -v
"""

import csv
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# Reviews count pitfall hits in $YBM_HOME; tests (and the CLIs they start) never touch the
# reviewer's own.
os.environ["YBM_HOME"] = tempfile.mkdtemp(prefix="ybm-home-")

from ybm import (analyze, catalog, changes, fixpoint, generate, inputs, measure,  # noqa: E402
                 pitfalls, plan, plans, preflight, probes, replay, report, safety, schema,
                 server, sqlshape, versions)
from ybm.sqltok import expr_of, fingerprint, split_statements, tokenize  # noqa: E402

FIXTURE = os.path.join(HERE, "..", "..", "..", "evals", "yb-model", "fixtures", "ecommerce",
                       "bundle")
RULES = os.path.join(HERE, "..", "rules", "rules.json")
PSS_HEAD = "queryid,calls,total_exec_time,mean_exec_time,rows,query\n"
_SCRIPTS = {}


def load_script(name):
    """A script in this directory (yb-lint.py, extract-version-data.py) as a module."""
    if name not in _SCRIPTS:
        spec = importlib.util.spec_from_file_location(name[:-3].replace("-", "_"),
                                                      os.path.join(HERE, name))
        _SCRIPTS[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_SCRIPTS[name])
    return _SCRIPTS[name]


def cli(*args):
    return subprocess.run([sys.executable, os.path.join(HERE, "yb-model.py")] + list(args),
                          capture_output=True, text=True)


def annotated(sch, rule, ddl, obj, **kw):
    """A finding whose engine option is written by hand, after safety.annotate: (finding,
    what checking its modelled option found)."""
    col = analyze.Collector()
    f = analyze.Finding(rule, "medium", "probable", obj, "fact", [], fix="Fix.",
                        ddl=safety.changes_of(ddl), **kw)
    col.add(f)
    safety.annotate(sch, [], col, analyze, {"cbo": False}, inputs.Bundle("x"),
                    analyze.load_rules())
    opt = next(o for o in f.to_dict(analyze.load_rules())["options"] if o["sketched"])
    return f, opt


def stats_csv(rows):
    """pg_stats CSV. A row is the text after schemaname ('t,c,f,0,8,...'), or a tuple
    (table, column, null_frac, n_distinct, histogram, correlation)."""
    buf = io.StringIO()
    wr = csv.writer(buf, lineterminator="\n")
    wr.writerow(["schemaname", "tablename", "attname", "inherited", "null_frac", "avg_width",
                 "n_distinct", "most_common_vals", "most_common_freqs", "histogram_bounds",
                 "correlation"])
    for r in rows:
        if isinstance(r, str):
            buf.write("public,%s\n" % r)
            continue
        t, c, nf, nd, hist, corr = r
        wr.writerow(["public", t, c, "f", nf, 8, nd, "", "",
                     ("{%s}" % ",".join(str(int(h)) for h in hist)) if hist else "",
                     "" if corr is None else corr])
    return buf.getvalue()


def ops_of(sql, sch, table):
    sh = sqlshape.analyze(sql, sch)
    return sh, analyze._pred_index(sh.preds_for(table))


def _findings(bundle):
    res = analyze.run(bundle)
    return [(f["rule"], f["object"], f["severity"]) for f in res["findings"]
            if not f["rule"].startswith("LINT-")]


class Case(unittest.TestCase):
    """Bundles in temporary directories and the reviews of them."""

    def tmpdir(self, files):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        for name, text in files.items():
            os.makedirs(os.path.dirname(os.path.join(d, name)), exist_ok=True)
            with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                fh.write(text)
        return d

    def bundle(self, ddl=None, queries=None, stats=None, rows=None, pss=None, files=None,
               release=None):
        """A loaded bundle: schema DDL, a query list (a missing final ';' is added), pg_stats
        rows (see stats_csv), row counts {relation: rows}, a pg_stat_statements CSV, and any
        other file by name."""
        f = dict(files or {})
        if ddl is not None:
            f["schema.sql"] = ddl
        if queries is not None:
            f["queries.sql"] = queries if queries.rstrip().endswith(";") else queries + ";\n"
        if stats is not None:
            f["ybm_pg_stats.csv"] = stats_csv(stats)
        if rows is not None:
            f["ybm_reltuples.csv"] = "relname,relkind,reltuples\n" + "".join(
                "%s,r,%s\n" % kv for kv in rows.items())
        if pss is not None:
            f["ybm_pss.csv"] = pss
        return inputs.load(self.tmpdir(f), release=release)

    def review(self, plans=None, **kw):
        return analyze.run(self.bundle(**kw), plans=plans)

    def replay(self, ddl, query, root, rows, assumed=None, statistics_missing=(), **match):
        """The review of `query` (P1) when a replay on the customer's release planned it with
        `root` at the top of the plan; `match` overrides fields of the version match."""
        plans = {"version": "9.9.9.9-b1", "mode": "customer", "reltuples": rows,
                 "patterns": [{"id": "P1", "query": query, "plan": [{"Plan": root}]}],
                 "assumed_settings": assumed or {},
                 "statistics_missing": list(statistics_missing),
                 "version_match": dict({"customer": "9.9.9.9", "exact": True,
                                        "drift_known": True, "drift": {"rules": []}}, **match)}
        return self.review(ddl=ddl, queries=query, plans=plans)

    @staticmethod
    def rules(res):
        return {f["rule"] for f in res["findings"]}

    def only(self, res, rule):
        """The one finding of `rule` in the review."""
        got = [f for f in res["findings"] if f["rule"] == rule]
        self.assertEqual(len(got), 1, [f["rule"] for f in res["findings"]])
        return got[0]


class SchemaModes(unittest.TestCase):
    def test_key_modes(self):
        def pk(s):
            return [k.mode for k in s.tables["t"].pk.keys]
        for label, ddl, kw, get, want in (
                ("first key HASH, then ASC",
                 "CREATE TABLE t (a int, b int, PRIMARY KEY (a, b));", {}, pk, ["HASH", "ASC"]),
                ("a colocated database defaults to ASC",
                 "CREATE TABLE t (a int PRIMARY KEY);", {"db_colocated": True}, pk, ["ASC"]),
                ("colocation = false keeps HASH",
                 "CREATE TABLE t (a int PRIMARY KEY) WITH (colocation = false);",
                 {"db_colocated": True}, pk, ["HASH"]),
                ("hash default off", "CREATE INDEX i ON t (a);", {"hash_default": False},
                 lambda s: [k.mode for k in s.indexes["i"].keys], ["ASC"]),
                ("a composite hash group",
                 "CREATE TABLE t (a int, b int, c int, PRIMARY KEY ((a, b) HASH, c DESC));", {},
                 lambda s: s.tables["t"].pk.signature(), "(a, b) HASH, c DESC")):
            with self.subTest(label):
                self.assertEqual(get(schema.parse(ddl, **kw)), want)

    def test_nulls_not_distinct_in_every_form(self):
        for label, ddl, name in (
                ("index", "CREATE TABLE t (id int PRIMARY KEY, s text);\nCREATE UNIQUE INDEX "
                          "NONCONCURRENTLY t_s ON public.t USING lsm (s HASH) NULLS NOT DISTINCT "
                          "SPLIT INTO 4 TABLETS;", "t_s"),
                ("table constraint", "CREATE TABLE t (id int PRIMARY KEY, s text, "
                                     "CONSTRAINT t_s UNIQUE NULLS NOT DISTINCT (s));", "t_s"),
                ("column constraint", "CREATE TABLE t (id int PRIMARY KEY, "
                                      "s text UNIQUE NULLS NOT DISTINCT);", "t_s_key"),
                ("added constraint", "CREATE TABLE t (id int PRIMARY KEY, s text);\n"
                                     "ALTER TABLE ONLY t ADD CONSTRAINT t_s UNIQUE NULLS NOT "
                                     "DISTINCT (s);", "t_s")):
            with self.subTest(label):
                i = schema.parse(ddl).indexes[name]
                self.assertTrue(i.unique and i.nulls_not_distinct)
        self.assertFalse(schema.parse("CREATE UNIQUE INDEX u ON t (s) NULLS DISTINCT;")
                         .indexes["u"].nulls_not_distinct)

    def test_foreign_keys_in_every_form(self):
        ddl = ("CREATE TABLE p (id int PRIMARY KEY, code text, UNIQUE (code));\n"
               "CREATE TABLE a (id int PRIMARY KEY, p_id int REFERENCES p, "
               "code text CONSTRAINT a_code_fk REFERENCES p (code) ON DELETE CASCADE NOT NULL);\n"
               "CREATE TABLE b (id int PRIMARY KEY, code text, "
               "CONSTRAINT b_code_fk FOREIGN KEY (code) REFERENCES public.p(code) DEFERRABLE);\n"
               "CREATE TABLE c (id int PRIMARY KEY, code text);\n"
               "ALTER TABLE ONLY public.c\n    ADD CONSTRAINT c_code_fkey FOREIGN KEY (code) "
               "REFERENCES public.p(code) ON UPDATE SET NULL;\n")
        s = schema.parse(ddl)
        got = sorted((fk.name, fk.table, fk.cols, fk.ref_table, fk.ref_cols, fk.clause)
                     for fk in s.foreign_keys)
        self.assertEqual(got, [
            ("a_code_fk", "a", ["code"], "p", ["code"],
             "FOREIGN KEY (code) REFERENCES p (code) ON DELETE CASCADE"),
            ("a_p_id_fkey", "a", ["p_id"], "p", None, "FOREIGN KEY (p_id) REFERENCES p"),
            ("b_code_fk", "b", ["code"], "p", ["code"],
             "FOREIGN KEY (code) REFERENCES public.p(code) DEFERRABLE"),
            ("c_code_fkey", "c", ["code"], "p", ["code"],
             "FOREIGN KEY (code) REFERENCES public.p(code) ON UPDATE SET NULL")])
        self.assertEqual(sorted(fk.name for fk in s.fks_on_index(s.indexes["p_code_key"])),
                         ["a_code_fk", "b_code_fk", "c_code_fkey"])
        self.assertEqual([fk.name for fk in s.fks_on_index(s.tables["p"].pk)], ["a_p_id_fkey"])

    def test_ysql_dump_index_and_partition_attach(self):
        ddl = ("CREATE TABLE p (id bigint NOT NULL, ts timestamptz NOT NULL) PARTITION BY RANGE (ts)\n"
               "SPLIT INTO 1 TABLETS;\n\\if :use_roles\n    ALTER TABLE p OWNER TO x;\n\\endif\n"
               "CREATE TABLE p1 (id bigint NOT NULL, ts timestamptz NOT NULL, "
               "CONSTRAINT p1_pkey PRIMARY KEY((id) HASH));\n\\if :use_roles\n\\endif\n"
               "ALTER TABLE ONLY public.p ATTACH PARTITION public.p1 FOR VALUES FROM ('a') TO ('b');\n"
               "CREATE INDEX NONCONCURRENTLY pi ON public.p1 USING lsm (ts ASC) INCLUDE (id) "
               "SPLIT INTO 3 TABLETS;")
        s = schema.parse(ddl)
        self.assertEqual(s.tables["p"].partitions, ["p1"])
        self.assertEqual(s.indexes["pi"].include, ["id"])
        self.assertIn("p1_pkey", [i.name for i in s.indexes_on("p")])


class AccessPaths(unittest.TestCase):
    sch = schema.parse("""
        CREATE TABLE t (h1 int, h2 int, r int, v int, n int, PRIMARY KEY ((h1, h2) HASH, r ASC));
        CREATE INDEX t_v ON t (v HASH, r DESC) INCLUDE (n) WHERE v IS NOT NULL;
    """)

    def best(self, sql):
        sh, ops = ops_of(sql, self.sch, "t")
        paths = []
        for idx in self.sch.indexes_on("t"):
            r = analyze.eval_path(idx, ops, sh, "t", cbo=False)
            if r["usable"]:
                r["order_ok"] = analyze.order_ok(idx, ops, sh, "t")
                r["covering"], r["missing"] = analyze.covering(idx, sh, "t", self.sch.tables["t"].cols)
            paths.append(r)
        return analyze.choose(paths), {p["index"]: p for p in paths}

    def test_best_path(self):
        for label, sql, index, flags in (
                ("a partial hash group is unusable", "SELECT * FROM t WHERE h1 = $1", None, {}),
                ("a range on a hash column is unusable",
                 "SELECT * FROM t WHERE h1 = $1 AND h2 > $2", None, {}),
                ("the full hash group orders by the range key",
                 "SELECT * FROM t WHERE h1 = $1 AND h2 = $2 ORDER BY r DESC LIMIT 5", "t_pkey",
                 {"order_ok": True}),
                ("a partial index whose predicate the query implies",
                 "SELECT n FROM t WHERE v = $1 ORDER BY r DESC", "t_v",
                 {"covering": True, "order_ok": True})):
            with self.subTest(label):
                best, _ = self.best(sql)
                self.assertEqual(best and best["index"], index)
                for k, v in flags.items():
                    self.assertEqual(best[k], v, k)
        _, paths = self.best("SELECT * FROM t WHERE h1 = $1")
        self.assertEqual(paths["t_pkey"]["status"], "hash_unbound")

    def test_order_is_broken_by_mixed_directions_and_in_on_hash(self):
        for ddl, sql in (
                ("CREATE TABLE u (a int, b int, c int, PRIMARY KEY (a HASH, b ASC, c ASC));",
                 "SELECT * FROM u WHERE a = $1 ORDER BY b ASC, c DESC"),
                ("CREATE TABLE u (a int, b int, PRIMARY KEY (a HASH, b DESC));",
                 "SELECT * FROM u WHERE a IN ($1, $2) ORDER BY b DESC LIMIT 3")):
            with self.subTest(sql):
                sch = schema.parse(ddl)
                sh, ops = ops_of(sql, sch, "u")
                self.assertFalse(analyze.order_ok(sch.tables["u"].pk, ops, sh, "u"))

    def test_join_binds_inner_key(self):
        sch = schema.parse("CREATE TABLE a (id int PRIMARY KEY); CREATE TABLE b (a_id int, k int, "
                           "PRIMARY KEY (a_id HASH, k ASC));")
        sh = sqlshape.analyze("SELECT * FROM a JOIN b ON b.a_id = a.id WHERE a.id = $1", sch)
        self.assertEqual(sh.joins, [("b", "a_id", "a", "id")])


class CatalogSchema(unittest.TestCase):
    """The schema read from a scratch server's catalog (ybm/catalog.py): the mapping from a
    catalog document to the model, without a container."""

    CAT = {"db_colocated": False,
           "tables": [
               {"name": "Orders", "schema": "public", "kind": "r",
                "props": {"num_tablets": 1, "is_colocated": False, "tablegroup_oid": None},
                "columns": [{"name": "Id", "type": "bigint", "notnull": True, "identity": False,
                             "default": None},
                            {"name": "email", "type": "text", "notnull": False,
                             "identity": False, "default": None}]},
               {"name": "acc", "schema": "public", "kind": "r",
                "props": {"num_tablets": 1, "is_colocated": True, "tablegroup_oid": 16500},
                "columns": [{"name": "id", "type": "bigint", "notnull": True, "identity": False,
                             "default": "nextval('acc_id_seq'::regclass)"}]},
               {"name": "p", "schema": "public", "kind": "p", "partkey": "RANGE (ts)",
                "columns": [{"name": "ts", "type": "date", "notnull": True, "identity": False,
                             "default": None}]},
               {"name": "p1", "schema": "public", "kind": "r", "parent": "p",
                "bound": "FOR VALUES FROM ('2026-01-01') TO ('2026-04-01')",
                "columns": [{"name": "ts", "type": "date", "notnull": True, "identity": False,
                             "default": None}]}],
           "indexes": [
               {"name": "Orders_pkey", "table": "Orders", "primary": True, "unique": True,
                "def": 'CREATE UNIQUE INDEX "Orders_pkey" ON public."Orders" USING lsm ("Id" HASH)'},
               {"name": "o_email", "table": "Orders", "primary": False, "unique": True,
                "constraint": "o_email",
                "def": 'CREATE UNIQUE INDEX o_email ON public."Orders" USING lsm (email HASH) '
                       'NULLS NOT DISTINCT SPLIT INTO 1 TABLETS'},
               {"name": "acc_pkey", "table": "acc", "primary": True, "unique": True,
                "def": "CREATE UNIQUE INDEX acc_pkey ON public.acc USING lsm (id ASC)"}],
           "fks": [{"name": "x_fk", "table": "acc", "ref_table": "Orders", "cols": ["id"],
                    "ref_cols": ["Id"], "def": 'FOREIGN KEY (id) REFERENCES "Orders"("Id")'}]}

    def test_catalog_document_to_model(self):
        sch = catalog.build(json.loads(json.dumps(self.CAT)), declared={"o_email": 8})
        o = sch.tables["Orders"]
        self.assertEqual((o.pk.name, o.pk.signature()), ("Orders_pkey", "(Id) HASH"))
        idx = sch.indexes["o_email"]
        self.assertTrue(idx.constraint and idx.nulls_not_distinct and idx.unique)
        self.assertEqual(idx.split, "INTO 8")  # declared in the DDL, loaded with one tablet
        self.assertTrue(sch.is_colocated("acc"))  # a tablegroup member
        self.assertTrue(sch.tables["acc"].cols["id"]["sequence"])
        self.assertEqual(sch.tables["p"].partition_by, ("RANGE", ["ts"]))
        self.assertEqual((sch.tables["p1"].partition_of, sch.tables["p1"].bound_to),
                         ("p", "2026-04-01"))
        self.assertEqual(sch.tables["p"].partitions, ["p1"])
        self.assertEqual([(f.table, f.ref_table) for f in sch.foreign_keys], [("acc", "Orders")])

    def test_rejected_statements_are_not_in_the_model(self):
        sch = catalog.build({"tables": [], "indexes": [], "fks": []}, failed=[
            ("CREATE TABLE t (id bigint, PRIMARY KEY (id HASH))", 'type "x" does not exist')])
        self.assertNotIn("t", sch.tables)  # the server's model: never filled in from text
        self.assertTrue(any("rejected" in n and "not in the model" in n
                            for n in sch.parse_notes))
        for cat, hd in (({"db_colocated": True}, True), ({}, False)):
            sch = catalog.build(dict(cat, tables=[], indexes=[], fks=[]), hash_default=hd)
            self.assertEqual(sch.hash_default, hd)

    def test_a_partitioned_parent_takes_the_database_colocation(self):
        cat = {"db_colocated": True, "indexes": [], "fks": [], "tables": [
            {"name": "p", "schema": "public", "kind": "p", "partkey": "RANGE (ts)",
             "props": {"num_tablets": None, "is_colocated": None, "tablegroup_oid": None},
             "columns": [{"name": "ts", "type": "date", "notnull": True, "identity": False,
                          "default": None}]}]}
        self.assertTrue(catalog.build(cat).is_colocated("p"))

    def test_one_tablet_load_keeps_the_declared_counts(self):
        ddl, declared = catalog.one_tablet(
            'CREATE TABLE public."Ab" (id int) SPLIT INTO 4 TABLETS;\n'
            "CREATE UNIQUE INDEX NONCONCURRENTLY ix ON public.t USING lsm (a HASH) "
            "SPLIT INTO 36 TABLETS;\n")
        self.assertEqual(declared, {"Ab": 4, "ix": 36})
        self.assertNotIn("36", ddl)

    def test_repairs_supply_what_an_incomplete_dump_left_out(self):
        fixes = catalog._repairs([
            ("CREATE TABLE archive.o (id int)", 'schema "archive" does not exist'),
            ("CREATE TABLE public.l (id bigint DEFAULT nextval('public.l_id_seq'::regclass))",
             'relation "public.l_id_seq" does not exist'),
            ("CREATE INDEX i ON nowhere (a)", 'relation "nowhere" does not exist'),
            ("CREATE TABLE c (id bigint DEFAULT nextval('c_seq'), p int REFERENCES gone(id))",
             'relation "gone" does not exist')], "")  # a missing parent is not a sequence
        self.assertEqual(fixes, ['CREATE SCHEMA IF NOT EXISTS "archive";',
                                 "CREATE SEQUENCE IF NOT EXISTS public.l_id_seq;"])


class UnreadableInputs(Case):
    """A file the engine cannot read stops the review with its name and the likely cause (exit
    2), never an empty review; an Excel byte-order mark is simply read."""

    DDL = "CREATE TABLE t (id bigint NOT NULL, PRIMARY KEY ((id) HASH));\n"

    def write(self, files, binary=()):
        d = self.tmpdir(files)
        for name, data in binary:
            with open(os.path.join(d, name), "wb") as fh:
                fh.write(data)
        return d

    def test_a_byte_order_mark_is_read(self):
        d = self.write({"schema.sql": self.DDL}, [(
            "ybm_settings.csv", "\ufeffname,setting\nyb_enable_cbo,on\n".encode("utf-8"))])
        self.assertEqual(inputs.load(d).settings, {"yb_enable_cbo": "on"})

    def test_unreadable_files_are_named(self):
        for label, files, binary, say in (
                ("utf-16 schema", {}, [("schema.sql", self.DDL.encode("utf-16"))], "UTF-16"),
                ("semicolons", {"schema.sql": self.DDL,
                                "ybm_settings.csv": "name;setting\nwork_mem;4MB\n"}, [],
                 "semicolons"),
                ("no header", {"schema.sql": self.DDL,
                               "ybm_settings.csv": "work_mem,4MB\nyb_enable_cbo,on\n"}, [],
                 "has no name column"),
                # value columns too: a default would make every index look unused, or every
                # statement empty
                ("no idx_scan", {"schema.sql": self.DDL, "ybm_index_usage.csv":
                                 "schemaname,relname,indexrelname\npublic,t,t_pkey\n"}, [],
                 "ybm_index_usage.csv has no idx_scan column"),
                ("pss without a header", {"schema.sql": self.DDL, "ybm_pss.csv":
                                          "1,10,5.0,0.5,10,SELECT * FROM t\n"
                                          "2,20,6.0,0.3,20,SELECT 1\n"}, [],
                 "ybm_pss.csv has no query column"),
                ("pss without times", {"schema.sql": self.DDL, "ybm_pss.csv":
                                       "query,calls\nSELECT 1,3\n"}, [],
                 "has no total_exec_time column (or total_time, mean_exec_time, mean_time)")):
            with self.subTest(label):
                with self.assertRaises(inputs.InputError) as e:
                    inputs.load(self.write(files, binary))
                self.assertIn(say, str(e.exception))

    def test_an_empty_capture_file_is_a_missing_input(self):
        # psql's \\copy creates the file before its query runs, so a failed query (no
        # pg_stat_statements) leaves it empty: missing, not unreadable.
        d = self.write({"schema.sql": self.DDL, "ybm_pss.csv": "", "ybm_index_usage.csv": "\n"})
        os.makedirs(os.path.join(d, "node1"))
        with open(os.path.join(d, "node1", "ybm_pss.csv"), "w") as fh:
            fh.write("")
        b = inputs.load(d)
        self.assertEqual((b.pss, b.index_usage), ([], {}))
        self.assertEqual(sorted(b.empty_files),
                         ["node1/ybm_pss.csv", "ybm_index_usage.csv", "ybm_pss.csv"])
        self.assertNotIn("ybm_pss.csv", b.present)
        self.assertIn("workload", [m["id"] for m in preflight.assess(b)["missing"]])
        self.assertTrue(any(o.startswith("ybm_pss.csv is empty") for o in
                            analyze.run(b)["open_items"]))

    def test_meta_without_a_header_stops(self):
        meta = "version,PostgreSQL 15.2-YB-2025.2.3.0-b1\nnodes,3\n"
        with self.assertRaises(inputs.InputError) as e:
            inputs.load(self.write({"schema.sql": self.DDL, "ybm_meta.csv": meta}))
        self.assertIn("ybm_meta.csv has no key column", str(e.exception))
        wide = "version,nodes\nPostgreSQL 15.2-YB-2025.2.3.0-b1,3\n"  # one row per capture
        b = inputs.load(self.write({"schema.sql": self.DDL, "ybm_meta.csv": wide}))
        self.assertEqual((b.version, b.cluster_nodes), ("2025.2.3.0", 3))

    def test_the_cli_stops_with_the_cause(self):
        d = self.write({}, [("schema.sql", self.DDL.encode("utf-16"))])
        r = cli("analyze", d)
        self.assertEqual(r.returncode, 2)
        self.assertIn("schema.sql is UTF-16", r.stderr)
        r = cli("fixpoint", d)
        self.assertEqual((r.returncode, "schema.sql is UTF-16" in r.stderr), (2, True))
        r = cli("analyze", self.write({"schema.sql": "-- nothing here\nSELECT 1;\n"}))
        self.assertEqual(r.returncode, 2)
        self.assertIn("no table could be read from schema.sql", r.stderr)


PRESCRIPTION = re.compile(r"(?:^|[.;:]\s+|\bthen\s+)(?:drop|replace|rebuild|re-key|rekey|remove)\b"
                          r"|\b(?:CREATE|DROP|ALTER)\s+(?:UNIQUE\s+)?(?:INDEX|TABLE)\b", re.I)
# A change told as an instruction: a sentence or clause that starts with one of these verbs (an
# upper-case SQL word such as INCLUDE is not an instruction). SCHEMA verbs are never right in
# finding text; the others only where the finding offers options (what could be done lives in
# them) or comes from the linter.
def _verbs(words):
    return "|".join("[%s%s]%s" % (w[0].upper(), w[0].lower(), w[1:]) for w in words)


_SCHEMA = ("Add", "Index", "Bucket", "Hash", "Create", "Lead", "Partition", "Declare", "Split",
           "Pre-split", "Merge", "Cover", "Include", "Replace", "Drop", "Rebuild", "Re-key",
           "Rekey", "Remove", "Annotate", "Make", "Restructure")
_OTHER = ("Keep", "Use", "Name", "Repeat", "Reduce", "Check", "Consider", "Bind", "Target",
          "Handle", "Set")
_START = r"(?:^|[.;!?]\s+|[,;]\s+(?:or|then)\s+|\botherwise\s+)"
# A schema verb in a clause joined by "and" ("re-capture ... and annotate every key") is an
# instruction too; the lower-case form only, so a subject such as "Index and ..." is not.
SCHEMA_INSTRUCTION = re.compile(r"%s(?:%s)\b|\band\s+(?:%s)\b" % (
    _START, _verbs(_SCHEMA), "|".join(w.lower() for w in _SCHEMA)))
INSTRUCTION = re.compile(r"%s(?:%s)\b" % (_START, _verbs(_SCHEMA + _OTHER)))


def prescriptions(res):
    """Finding texts that instruct a change or carry a statement, instead of giving reasons:
    any destructive instruction or statement anywhere, and any instruction at all from a
    finding that offers options (what could be done lives in them) or comes from the linter."""
    out = []
    for f in res["findings"]:
        strict = bool(f.get("options")) or f["rule"].startswith("LINT-")
        for t in (f.get("fix") or "", f.get("fact") or ""):
            if PRESCRIPTION.search(t) or SCHEMA_INSTRUCTION.search(t) or (
                    strict and INSTRUCTION.search(t)):
                out.append("%s %s: %s" % (f["rule"], f["object"], t))
    return out


class OptionsNotPrescriptions(Case):
    """A finding states why, and the options its rule offers; the report groups findings by
    area into suggestions (additive, checked clean) and decisions, with what must stay true and
    where they conflict; it prints no DDL, which `yb-model.py sketch` gives for a chosen option."""

    DDL = ("CREATE TABLE o (id bigint NOT NULL, c bigint, PRIMARY KEY ((id) HASH));\n"
           "CREATE INDEX o_c ON o (c HASH);\n"
           "CREATE TABLE u (a int NOT NULL, b int NOT NULL, v int, PRIMARY KEY ((a) HASH));\n")
    Q = ("SELECT * FROM o WHERE c = $1;\n"
         "INSERT INTO u (a, b, v) VALUES ($1, $2, $3) ON CONFLICT (b) DO UPDATE SET v = 1")

    def run_review(self):
        d = self.tmpdir({"schema.sql": self.DDL, "queries.sql": self.Q + ";\n",
                         "ybm_pg_stats.csv": stats_csv(["o,c,f,0.8,8,1000,,,,0"]),
                         "ybm_reltuples.csv": "relname,relkind,reltuples\no,r,1e7\nu,r,1e6\n"})
        r = cli("review", d, "--no-replay", "--accept-missing")
        self.assertIn(r.returncode, (0, 1), r.stderr)
        with open(os.path.join(d, "review.json")) as fh, open(os.path.join(d, "review.md")) as m, \
                open(os.path.join(d, "chat.md")) as c:
            return d, json.load(fh), m.read(), c.read()

    @unittest.skipUnless(os.path.isdir(FIXTURE), "fixture bundle not present")
    def test_findings_give_reasons_not_instructions(self):
        # Every finding text explains; what could be done lives in the options, and no text
        # carries a statement to run.
        branches = self.review(
            ddl="CREATE TABLE h (id bigint NOT NULL, a int, b int, c text, x int, y int, "
                "created_at timestamptz, PRIMARY KEY ((id) HASH));\n"
                "CREATE INDEX h_a ON h (a HASH);\nCREATE INDEX h_created ON h (created_at ASC);\n"
                "CREATE INDEX h_xy ON h ((x, y) HASH);\n"
                "CREATE TABLE p (id bigint NOT NULL, ts date NOT NULL, PRIMARY KEY (id ASC)) "
                "PARTITION BY RANGE (ts);\n",
            queries="SELECT a, b FROM h WHERE a = $1;\nSELECT * FROM h WHERE lower(c) = $1;\n"
                    "SELECT id FROM h WHERE x = $1;\nSELECT id FROM h WHERE b = $1 ORDER BY "
                    "created_at LIMIT 10", rows={"h": 1e7})
        self.assertLessEqual({"CAP020", "CAP003", "CAP001", "STA004"}, self.rules(branches))
        # A range on a hash column, an unindexed join column, a low-cardinality unique index.
        more = self.review(
            ddl="CREATE TABLE h (id bigint NOT NULL, a int, b int, x int, y int, "
                "PRIMARY KEY ((id) HASH));\nCREATE INDEX h_xy ON h ((x, y) HASH);\n"
                "CREATE UNIQUE INDEX h_b ON h (b HASH);\n"
                "CREATE TABLE j (id bigint NOT NULL, hid bigint, PRIMARY KEY ((id) HASH));\n",
            queries="SELECT id FROM h WHERE x = $1 AND y > $2;\n"
                    "SELECT j.id FROM h JOIN j ON j.hid = h.id WHERE h.a = $1;\n",
            rows={"h": 1e7, "j": 1e7}, stats=[("h", "b", 0, 300, [], 0.0)])
        self.assertLessEqual({"CAP001", "CAP031", "STA002"}, self.rules(more))
        self.assertIn("yb_hash_code", self.only(more, "CAP001")["fix"])
        self.assertIn("enforces uniqueness", self.only(more, "STA002")["fix"])
        reviews = [analyze.run(inputs.load(FIXTURE)), self.run_review()[1], branches, more,
                   self.review(ddl=PartitionIndexCopies.DUMP, pss=PSS_HEAD +
                               "1,10,5,0.5,10,SELECT id FROM events WHERE created_at = $1\n",
                               files=PartitionIndexCopies().usage(0, 0))]
        for res in reviews:
            self.assertEqual(prescriptions(res), [])

    def test_every_rule_fallback_gives_reasons(self):
        """A rule's fallback text becomes a finding's text whenever the engine passes none, so
        each must hold to the same rule, reachable today or not."""
        with open(RULES) as fh:
            rules = json.load(fh)["rules"]
        bad = []
        for rid, r in sorted(rules.items()):
            for fld in ("fix", "fix_if_unavailable"):
                t = r.get(fld) or ""
                if PRESCRIPTION.search(t) or SCHEMA_INSTRUCTION.search(t) or (
                        r.get("options") and INSTRUCTION.search(t)):
                    bad.append("%s %s: %s" % (rid, fld, t))
        self.assertEqual(bad, [])

    def test_a_destructive_option_is_always_a_decision(self):
        # An unused index on a two-node cluster, captured on both: its drop checks clean, and
        # it is still the user's call.
        b = NodeCoverage().nodes((0, 10), (0, 20), nodes=2)
        res = analyze.run(b)
        wrk = self.only(res, "WRK003")
        drop = next(o for o in wrk["options"] if o["id"] == "drop")
        self.assertTrue(drop["sketched"] and drop["checked"])
        self.assertEqual(drop["costs"], [])
        area = next(d for d in res["dossiers"] if "t" in d["area"])
        self.assertEqual(area["suggestions"], [])
        self.assertEqual([dc["object"] for dc in area["decisions"]], ["t_a"])

    def test_suggestions_decisions_and_no_ddl(self):
        d, res, md, chat = self.run_review()
        self.assertNotRegex(md, r"(?m)^\s*(CREATE|DROP|ALTER) (UNIQUE )?(INDEX|TABLE)\b")
        sta1 = self.only(res, "STA001")
        opts = {o["id"]: o for o in sta1["options"]}
        self.assertTrue(opts["partial"]["sketched"] and opts["partial"]["checked"])
        self.assertEqual(opts["partial"]["costs"], [])
        self.assertIn("P1 (UNRANKED) on o loses its access path (o_c) and falls back to a scan",
                      opts["drop"]["costs"])
        self.assertFalse(opts["lead_with_other"]["checked"])
        # o_c: two findings name it, so it is a decision; u: one finding whose option checks
        # clean, so it is a suggestion with its reasons.
        area = md[md.index("## By area"):md.index("## Findings")]
        self.assertIn("- **Decision** on o_c:", area)
        self.assertIn("Remove the index (costs: P1 (UNRANKED) on o loses its access path", area)
        self.assertRegex(area, r"- \*\*Suggestion\*\* for u \(F\d+ CAP060, .*\): Add a unique "
                               r"index on exactly the conflict columns\. Why: P\d+: INSERT")
        legend = md[md.index("## Options by rule"):md.index("## Disputed by replay")]
        self.assertIn("**STA001, Nullable leading hash column holds many NULLs:**", legend)
        self.assertIn("## Decisions to make", md)
        self.assertRegex(chat, r"CAP060\*\* \(high\): .* suggested: Add a unique index")
        self.assertRegex(chat, r"STA001\*\* \(high\): .* decide between Index only the rows")
        # The DDL of a chosen option, with what checking it found.
        r = cli("sketch", d, sta1["id"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("DROP INDEX o_c;", r.stdout)
        self.assertIn("never on the customer's cluster", r.stdout)
        self.assertEqual(cli("sketch", d, "F999").returncode, 2)


class ReviewerPlan(Case):
    """The skill's plan (one direction per area, chosen from the options) is checked by the
    engine as one combined change, never amended: conflicts go back as decisions."""

    DDL = OptionsNotPrescriptions.DDL
    Q = OptionsNotPrescriptions.Q

    def setUp(self):
        self.d = self.tmpdir({"schema.sql": self.DDL, "queries.sql": self.Q + ";\n",
                              "ybm_pg_stats.csv": stats_csv(["o,c,f,0.8,8,3,,,,0"]),
                              "ybm_reltuples.csv": "relname,relkind,reltuples\no,r,1e7\n"
                                                   "u,r,1e6\n"})
        r = cli("review", self.d, "--no-replay", "--accept-missing")
        self.assertIn(r.returncode, (0, 1), r.stderr)
        with open(os.path.join(self.d, "review.json")) as fh:
            self.res = json.load(fh)
        self.ids = {f["rule"]: f["id"] for f in self.res["findings"]}

    def plan(self, areas):
        p = os.path.join(self.d, "plan.json")
        with open(p, "w") as fh:
            json.dump({"areas": areas}, fh)
        r = cli("plan", self.d, p)
        with open(os.path.join(self.d, "plan-check.json")) as fh, \
                open(os.path.join(self.d, "plan.md")) as m:
            return r, json.load(fh), m.read()

    def test_a_coherent_plan_passes_with_the_areas_suggestions(self):
        # o_c: one change (the partial index) settles both findings; u's suggestion (the
        # unique index CAP060 needs) is taken without being named.
        r, chk, md = self.plan([
            {"area": "o", "diagnosis": "o_c holds mostly NULLs and is read only by value.",
             "direction": "Leave the NULLs out of o_c.",
             "choices": {self.ids["STA001"]: "partial", self.ids["STA002"]: "keep"},
             "questions": ["Is c ever looked up without a value?"]}])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((chk["errors"], chk["conflicts"], chk["unchecked"]), ([], [], []))
        self.assertIn("**Diagnosis:** o_c holds mostly NULLs", md)
        self.assertIn("the engine's suggestion", md)  # CAP060, not named in the plan
        self.assertNotRegex(md, r"(?m)^\s*(CREATE|DROP|ALTER) (UNIQUE )?(INDEX|TABLE)\b")
        self.assertIn("sketch <bundle> <finding> --option <id>", md)
        self.assertIn("Is c ever looked up without a value?", md)
        with open(os.path.join(self.d, "chat.md")) as fh:
            self.assertIn("(engine check: no conflict)", fh.read())
        # An option the engine cannot model is an open question, never a pass.
        r, chk, md = self.plan([{"area": "o", "direction": "x", "choices": {
            self.ids["STA001"]: "keep", self.ids["STA002"]: "composite"}}])
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(chk["unchecked"]), 1)
        self.assertIn("put them to the user as open questions", md)
        with open(os.path.join(self.d, "chat.md")) as fh:
            self.assertIn("1 choice(s) not checked for the user to settle", fh.read())

    def test_conflicting_choices_are_reported_not_amended(self):
        r, chk, md = self.plan([{"area": "o", "direction": "x", "choices": {
            self.ids["STA001"]: "partial", self.ids["STA002"]: "drop"}}])
        self.assertEqual(r.returncode, 1)
        self.assertIn("%s and %s each change o_c: choose one change for it" % tuple(
            sorted([self.ids["STA001"], self.ids["STA002"]])), chk["conflicts"])
        # Dropping o_c alone takes P1's access path away.
        r, chk, md = self.plan([{"area": "o", "direction": "x", "choices": {
            self.ids["STA002"]: "drop"}}])
        self.assertTrue(any(c.startswith("P1 (UNRANKED) on o loses its access path")
                            for c in chk["conflicts"]), chk["conflicts"])

    def test_malformed_plans_and_paths_are_errors_not_tracebacks(self):
        r, chk, md = self.plan([{"choices": {}}, {"area": 7}])
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        p = os.path.join(self.d, "plan.json")
        r = cli("plan", os.path.join(self.d, "schema.sql"), p, "--out",
                os.path.join(self.d, "new", "dir"))
        self.assertIn(r.returncode, (0, 1, 2), r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.d, "new", "dir", "plan.md")))

    def test_disputed_findings_and_bad_shapes_are_refused(self):
        f = dict(self.res["findings"][0], section="disputed")
        o = f["options"][0]
        self.assertEqual(plan.sketch_for(f, o, None)[0], None)
        r, chk, md = self.plan([{"area": "o", "choices": ["F1"]}, "o"])
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertTrue(any("must map finding ids" in e for e in chk["errors"]), chk["errors"])
        self.assertTrue(any("must be an object" in e for e in chk["errors"]), chk["errors"])
        r, chk, md = self.plan([{"area": "o", "questions": 3}])
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertTrue(any('"questions" must be a list' in e for e in chk["errors"]))
        r, chk, md = self.plan([{"area": "o", "questions": "is o read by id?"}])
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(chk["areas"][0]["questions"], ["is o read by id?"])
        r, chk, md = self.plan([{"area": "o", "questions": [{"q": "x"}, "ok?"],
                                 "diagnosis": ["a", "b"]}])
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(chk["areas"][0]["questions"], ["ok?"])
        self.assertTrue(any('"diagnosis" must be a string' in e for e in chk["errors"]))
        self.assertNotIn("{'q'", md)

    def test_a_scratch_server_that_fails_is_an_error_not_a_traceback(self):
        with open(os.path.join(self.d, "plan.json"), "w") as fh:
            json.dump({"areas": []}, fh)
        ybm_cli = load_script("yb-model.py")
        errors = (RuntimeError("the scratch server did not start"),
                  subprocess.TimeoutExpired("docker", 600),  # a slow load
                  json.JSONDecodeError("the scratch server did not start", "", 0))  # garbled

        def fail(*a, **k):
            raise err_now
        for err_now in errors:
            for argv in (["plan", self.d, os.path.join(self.d, "plan.json"), "--catalog"],
                         ["sketch", self.d, self.ids["STA002"], "--option", "drop",
                          "--catalog"]):
                with self.subTest(argv[0], error=type(err_now).__name__), \
                        mock.patch.object(catalog, "attach", fail):
                    err = io.StringIO()
                    with mock.patch.object(sys, "argv", ["yb-model.py"] + argv), \
                            mock.patch.object(sys, "stderr", err), \
                            mock.patch.object(sys, "stdout", io.StringIO()):
                        self.assertEqual(ybm_cli.main(), 2)
                    self.assertIn("yb-model: cannot", err.getvalue())

    def test_sketch_finds_the_bundle_of_a_review_written_elsewhere(self):
        out = os.path.join(self.d, "elsewhere")
        r = cli("review", self.d, "--no-replay", "--accept-missing", "--out", out)
        self.assertIn(r.returncode, (0, 1), r.stderr)
        sid = self.ids["STA002"]
        r = cli("sketch", out, sid, "--option", "drop")
        self.assertEqual(r.returncode, 2)
        self.assertIn("pass --bundle", r.stderr)
        r = cli("sketch", out, sid, "--option", "drop", "--bundle", self.d)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("DROP INDEX o_c;", r.stdout)

    def test_sketch_prints_the_chosen_option_with_its_check(self):
        sid = self.ids["STA002"]
        r = cli("sketch", self.d, sid, "--option", "drop")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("-- Checked alone: P1 (UNRANKED) on o loses its access path", r.stdout)
        self.assertIn("DROP INDEX o_c;", r.stdout)
        r = cli("sketch", self.d, sid, "--option", "composite")
        self.assertEqual(r.returncode, 2)
        self.assertIn("no engine sketch for this option", r.stderr)

    def test_a_plan_with_unknown_choices_is_an_error(self):
        r, chk, md = self.plan([{"area": "nowhere", "choices": {}},
                                {"area": "o", "choices": {"F99": "partial",
                                                          self.ids["STA001"]: "rebuild_it"}}])
        self.assertEqual(r.returncode, 2)
        self.assertEqual(len(chk["errors"]), 3, chk["errors"])


class SketchCorrectness(Case):
    """Modelled options must be valid YSQL and keep what they claim to keep."""

    PART = ("CREATE TABLE ev (id bigint NOT NULL, k bigint, ts date NOT NULL, v int, "
            "PRIMARY KEY ((id) HASH, ts ASC)) PARTITION BY RANGE (ts);\n"
            "CREATE TABLE ev_q1 PARTITION OF ev FOR VALUES FROM ('2026-01-01') TO "
            "('2026-04-01');\n")

    def test_new_indexes_on_a_partitioned_table(self):
        # No CONCURRENTLY on a partitioned parent, and no unique index without its key.
        res = self.review(ddl=self.PART, queries=(
            "INSERT INTO ev (id, k, ts, v) VALUES ($1, $2, $3, $4) ON CONFLICT (k) DO NOTHING;\n"
            "SELECT * FROM ev WHERE lower(v::text) = $1"), rows={"ev": 1e6})
        c60 = self.only(res, "CAP060")
        self.assertIsNone(c60["ddl"])
        self.assertIn("must contain its partition key (ts)", c60["fix"])
        for f in res["findings"]:
            self.assertNotIn("CONCURRENTLY", f.get("ddl") or "", f["rule"])
        self.assertIsNone(schema.new_index_sql(schema.parse(self.PART), "u", "ev", "(k) HASH",
                                               unique=True))
        self.assertEqual(schema.new_index_sql(schema.parse(self.PART), "u", "ev",
                                              "(k) HASH, ts ASC", unique=True),
                         "CREATE UNIQUE INDEX u ON ev ((k) HASH, ts ASC);")

    def test_a_rekey_of_an_insert_ordered_key_keeps_every_column(self):
        res = self.review(ddl="CREATE TABLE l (created_at timestamptz NOT NULL, id bigint NOT "
                              "NULL, PRIMARY KEY (created_at ASC, id ASC));\n",
                          rows={"l": 1e7}, stats=[("l", "created_at", 0, -1, [], 0.99)],
                          files={"ybm_table_usage.csv":
                                                  "relname,n_tup_ins,n_tup_upd\nl,100000,0\n"})
        f = self.only(res, "STA004")
        self.assertIn("PRIMARY KEY ((created_at) HASH, id ASC)", f["ddl"])

    def test_an_upsert_into_a_partition_finds_its_parents_unique_index(self):
        ddl = self.PART + ("CREATE UNIQUE INDEX ev_k ON ONLY ev (k HASH, ts ASC);\n"
                           "CREATE UNIQUE INDEX ev_q1_k ON ev_q1 (k HASH, ts ASC);\n"
                           "ALTER INDEX ev_k ATTACH PARTITION ev_q1_k;\n")
        res = self.review(ddl=ddl, queries="INSERT INTO ev_q1 (id, k, ts) VALUES ($1, $2, $3) "
                                           "ON CONFLICT (k, ts) DO NOTHING")
        self.assertNotIn("CAP060", self.rules(res))

    def test_an_upsert_into_a_partition_sees_its_parents_partial_index(self):
        ddl = self.PART + ("CREATE UNIQUE INDEX ev_k ON ONLY ev (k HASH, ts ASC) WHERE v > 0;\n"
                           "CREATE UNIQUE INDEX ev_q1_k ON ev_q1 (k HASH, ts ASC) WHERE v > 0;\n"
                           "ALTER INDEX ev_k ATTACH PARTITION ev_q1_k;\n")
        res = self.review(ddl=ddl, queries="INSERT INTO ev_q1 (id, k, ts, v) VALUES ($1, $2, $3, "
                                           "$4) ON CONFLICT (k, ts) DO NOTHING")
        f = self.only(res, "CAP060")
        self.assertIn("is partial (WHERE", f["fact"])
        self.assertIsNone(f["ddl"])

    def test_a_literal_with_two_dashes_is_kept_in_the_model(self):
        c = changes.create("CREATE INDEX i ON t (a) WHERE note <> '--';  -- trailing note")
        idx = safety._index_of(schema.parse("CREATE TABLE t (a int PRIMARY KEY, note text);"),
                               c.stmt)
        self.assertIn("'--'", idx.where)


class SelfCheck(unittest.TestCase):
    def test_a_regression_only_the_combination_brings_is_reported(self):
        fs = [{"rule": "CAP020", "object": "i%d on t" % n, "table": "t", "ddl": "x",
               "changes": [], "severity": "medium", "section": "schema"} for n in range(12)]
        new = {"rule": "STA001", "object": "j(c)", "table": "u", "severity": "high",
               "section": "schema"}

        def run(bundle, schema_override=None):
            return {"findings": [new] if schema_override == "batch" else []}
        with mock.patch.object(fixpoint.analyze, "build_schema", lambda b: None), \
                mock.patch.object(fixpoint, "_apply",
                                  lambda sch, f: "batch" if len(f) > 1 else "one"), \
                mock.patch.object(fixpoint.analyze, "run", run):
            out = fixpoint.check(None, final={"findings": fs})
        self.assertEqual(out["runs"], 13)  # the batch, then all twelve alone
        self.assertEqual(len(out["problems"]), 1)
        self.assertTrue(out["problems"][0].startswith("introduced when the sketches for"))


class SelfCheckByObject(unittest.TestCase):
    def test_the_same_rule_on_a_new_index_counts_and_a_moved_finding_does_not(self):
        sketch = {"rule": "CAP020", "object": "j on u", "table": "u", "ddl": "x",
                  "severity": "medium", "section": "schema", "options": [], "changes": [
                      {"kind": "create", "sql": "CREATE INDEX CONCURRENTLY j_cov ON u (c);",
                       "name": "j_cov"},
                      {"kind": "drop", "sql": "DROP INDEX j;", "name": "j"}]}
        old = {"rule": "STA001", "object": "j(c)", "table": "u", "severity": "high",
               "section": "schema"}
        for after, problems in (
                ([{"rule": "STA001", "object": "j_cov(c)", "table": "u", "severity": "high",
                   "section": "schema"}], 0),  # moved to the replacement: the same finding
                ([{"rule": "STA001", "object": "k(c)", "table": "u", "severity": "high",
                   "section": "schema"}], 1)):  # a new index elsewhere: reported
            with mock.patch.object(fixpoint.analyze, "build_schema", lambda b: None), \
                    mock.patch.object(fixpoint, "_apply", lambda sch, f: None), \
                    mock.patch.object(fixpoint.analyze, "run",
                                      lambda b, schema_override=None, a=after: {"findings": a}):
                out = fixpoint.check(None, final={"findings": [sketch, old]})
            self.assertEqual(len(out["problems"]), problems, out["problems"])

    def run_check(self, final, after):
        with mock.patch.object(fixpoint.analyze, "build_schema", lambda b: None), \
                mock.patch.object(fixpoint, "_apply", lambda sch, f: None), \
                mock.patch.object(fixpoint.analyze, "run",
                                  lambda b, schema_override=None: {"findings": after}):
            return fixpoint.check(None, final=final)

    def test_another_finding_of_the_rule_on_the_table_is_not_this_one(self):
        """Resolution is judged by object: a second CAP020 on the table that stays (no
        sketch, or another batch) does not make the fixed one unresolved."""
        fixed = {"rule": "CAP020", "object": "a on t", "table": "t", "ddl": "x",
                 "severity": "medium", "section": "schema", "options": [], "changes": [
                     {"kind": "create", "sql": "CREATE INDEX a_cov ON t (c);", "name": "a_cov"},
                     {"kind": "drop", "sql": "DROP INDEX a;", "name": "a"}]}
        other = {"rule": "CAP020", "object": "b on t", "table": "t", "severity": "medium",
                 "section": "schema"}
        self.assertEqual(self.run_check({"findings": [fixed, other]}, [other])["problems"], [])
        moved = dict(other, object="a_cov on t")  # still there, on its replacement
        out = self.run_check({"findings": [fixed, other]}, [other, moved])
        self.assertEqual(out["problems"], ["not resolved by its own sketch: CAP020 a on t"])

    def test_findings_replay_removed_or_disputed_are_not_new(self):
        """The re-runs are static, so a finding replay refuted (reconciliation) or disputed
        comes back in them; it is not something the sketch introduced."""
        sketch = {"rule": "CAP020", "object": "j on u", "table": "u", "ddl": "x",
                  "severity": "medium", "section": "schema", "options": [], "changes": [
                      {"kind": "create", "sql": "CREATE INDEX x ON u (c);", "name": "x"}]}
        disputed = {"rule": "CAP002", "object": "P2 on v", "table": "v", "severity": "high",
                    "section": "disputed"}
        back = [{"rule": "CAP001", "object": "P1 on w", "table": "w", "severity": "high",
                 "section": "schema"}, dict(disputed, section="schema")]
        out = self.run_check({"findings": [sketch, disputed], "reconciliation": [
            {"finding": "CAP001", "object": "P1 on w", "outcome": "refuted by replay"}]}, back)
        self.assertEqual(out["problems"], [])

    def test_a_default_with_a_known_cost_is_checked_and_its_cost_named(self):
        sketch = {"rule": "WRK003", "object": "j", "table": "u", "ddl": "x", "severity": "medium",
                  "section": "schema", "changes": [{"kind": "drop", "sql": "DROP INDEX j;",
                                                    "name": "j"}],
                  "options": [{"id": "drop", "sketched": True,
                               "costs": ["foreign key f on u loses its index"]}]}
        out = self.run_check({"findings": [sketch]}, [
            {"rule": "CAP032", "object": "f on u", "table": "u", "severity": "high",
             "section": "schema"}])
        self.assertEqual(out["fixed_with_ddl"], 1)
        self.assertEqual(len(out["problems"]), 1, out["problems"])
        self.assertIn("a known cost of that option: foreign key f", out["problems"][0])

    def test_a_replacement_is_found_by_its_name_not_its_sql(self):
        # A name the SQL pattern could not read (quoted, with a space), from the constructors.
        names = fixpoint._replacements([{"changes": [
            changes.create('CREATE INDEX "odd name v2" ON s.u (c);', "odd name v2").to_dict(),
            changes.drop("s.odd.name").to_dict()]}])  # schema s, index "odd.name"
        self.assertEqual(names, {"odd name v2": "odd.name"})


class ReportText(unittest.TestCase):
    def test_a_lint_finding_starts_with_its_reason_not_its_rule_id(self):
        f = {"id": "F1", "rule": "LINT-YB020", "title": "LINT-YB020", "object": "line 3",
             "fact": "t: table has no primary key.", "fix": None, "options": []}
        self.assertEqual(report._choices(f), "t: table has no primary key.")

    def test_the_self_check_line_claims_only_the_default_options(self):
        res = analyze.run(inputs.load(FIXTURE))
        for fp, says in (({"problems": ["introduced by the sketch for X"]}, "PROBLEMS"),
                         ({"problems": [], "fixed_with_ddl": 3, "batches": 1}, "for each of 3")):
            md = report.render(dict(res, fixpoint=fp))
            line = next(x for x in md.splitlines() if x.startswith("Self-check"))
            self.assertIn("default option", line)
            self.assertIn(says, line)
            self.assertNotIn("modelled option", line)

    def test_the_access_table_shows_each_binding_map_with_its_legend(self):
        md = report.render(analyze.run(inputs.load(FIXTURE)))
        rows = [x for x in md.splitlines() if x.startswith("| P")]
        self.assertTrue(any(re.search(r"\[(= |in |<> |or= |f |- )*(=|in|<>|or=|f|-)\]", x)
                            for x in rows), rows[:3])
        self.assertIn("= equality, in IN list, <> range, or= equality in every OR arm, "
                      "op another operator the index serves (as GIN does), f filter only, "
                      "- unbound", md)

    def test_a_review_without_a_server_says_it_is_static_only(self):
        res = analyze.run(inputs.load(FIXTURE))
        chat, md = report.render_chat(res), report.render(res)
        self.assertEqual(chat.splitlines()[2], report.static_warning(res))  # after the headline
        self.assertIn("> **Static-only review: no scratch YugabyteDB was run.**", md)
        self.assertIn("significantly less accurate", chat)
        self.assertIn("static only (nothing proven on a server)", report.review_level(res))
        res["replay"] = {"version": "9.9.9.9-b1"}
        self.assertIsNone(report.static_warning(res))
        self.assertNotIn("Static-only", report.render_chat(res))

    def test_options_by_rule_lists_every_option_of_the_rule(self):
        opt = lambda i: {"id": i, "label": i, "when": "w", "tradeoff": "t",
                         "check_first": "c", "change": "none"}
        guarded = {"id": "F1", "rule": "STA001", "title": "x", "options": [opt("partial")]}
        plain = dict(guarded, id="F2", options=[opt("partial"), opt("drop")])
        self.assertEqual([o["id"] for o in report._rule_options([guarded, plain], "STA001")],
                         ["partial", "drop"])


class KeyGap(Case):
    """CAP014: a key column bound after an unbound one is reached by a seek per distinct value
    of the gap; the rule reads the binding map, offers a re-key (primary key) or a rebuild
    (index), an index that leads with the bound columns, or keep."""
    DDL = ("CREATE TABLE es (c text NOT NULL, e text NOT NULL, v text NOT NULL, s text NOT NULL, "
           "d text NOT NULL, f text NOT NULL, x float, PRIMARY KEY ((c, e) HASH, v, s, d, f));\n")
    BOX = "SELECT x FROM es WHERE c = $1 AND e = $2 AND v = 'a' AND f = 'w1';\n"

    def test_a_gap_is_a_finding_and_its_sketch_moves_the_bound_column_forward(self):
        res = self.review(ddl=self.DDL, queries=self.BOX)
        f = self.only(res, "CAP014")
        self.assertEqual((f["severity"], f["object"]), ("medium", "es_pkey on es"))
        self.assertIn("pattern unranked", f["caveats"])  # weakened, not muted, without pss
        self.assertIn("not s, d between them", f["fact"])
        self.assertIn("unknown without pg_stats", f["fact"])
        self.assertIn("PRIMARY KEY ((c, e) HASH, v ASC, f ASC, s ASC, d ASC)", f["ddl"])
        self.assertEqual([o["id"] for o in f["options"]], ["rekey", "index_bound_first", "keep"])
        self.assertTrue(next(o for o in f["options"] if o["id"] == "rekey")["sketched"])
        self.assertEqual(prescriptions(res), [])

    def test_no_gap_no_finding(self):
        for where in ("v = 'a' AND s = 'x' AND d = '' AND f = ''",
                      "v = 'a' AND f = '' AND (s = 'x' AND d = '' OR s = 'y' AND d = '')",
                      "v = 'a'"):
            with self.subTest(where):
                res = self.review(ddl=self.DDL, queries="SELECT x FROM es WHERE c = $1 AND "
                                                        "e = $2 AND %s;\n" % where)
                self.assertNotIn("CAP014", self.rules(res))

    def test_few_gap_values_make_it_low(self):
        f = self.only(self.review(ddl=self.DDL, queries=self.BOX, rows={"es": 1e6}, stats=[
            ("es", "s", 0, 3, [], 0.0), ("es", "d", 0, 2, [], 0.0)]), "CAP014")
        self.assertEqual(f["severity"], "low")
        self.assertIn("up to ~6 distinct values", f["fact"])

    def test_an_index_is_rebuilt_not_rekeyed(self):
        ddl = ("CREATE TABLE t (id bigint NOT NULL, a int, b int, c int, PRIMARY KEY ((id) HASH));"
               "\nCREATE INDEX t_abc ON t ((a) HASH, b ASC, c ASC);\n")
        f = self.only(self.review(ddl=ddl, queries="SELECT id FROM t WHERE a = $1 AND c = $2;\n"),
                      "CAP014")
        self.assertEqual([o["id"] for o in f["options"]],
                         ["reorder", "index_bound_first", "keep"])
        self.assertIn("ON t ((a) HASH, c ASC, b ASC)", f["ddl"])

    def test_a_reorder_that_opens_a_gap_for_another_read_names_it_as_a_cost(self):
        # The box read wants f before s; a read of one stat over every period wants s before f.
        res = self.review(ddl=self.DDL, queries=self.BOX + "SELECT x FROM es WHERE c = $1 AND "
                          "e = $2 AND v = 'a' AND s = 'x' AND d = '';\n")
        f = self.only(res, "CAP014")
        rekey = next(o for o in f["options"] if o["id"] == "rekey")
        self.assertTrue(any("P2" in c and "only after an unbound" in c for c in rekey["costs"]),
                        rekey["costs"])


class ProbeSeeks(unittest.TestCase):
    PLAN = [{"Plan": {"Node Type": "Index Scan", "Index Name": "i", "Actual Rows": 10,
                      "Storage Table Rows Scanned": 10,
                      "Read Metrics": {"Metric rocksdb_number_db_seek": 801.0}},
             "Read Metrics": {"Metric rocksdb_number_db_seek": 801}}]

    def test_seeks_come_from_the_debug_read_metrics(self):
        self.assertEqual(probes.metrics(self.PLAN)["seeks"], 801)
        plain = [{"Plan": {"Node Type": "Index Scan", "Actual Rows": 1}}]
        self.assertIsNone(probes.metrics(plain)["seeks"])
        self.assertIn("801 seeks", probes.describe("CAP014", {"cases": {
            "claim": probes.metrics(self.PLAN)}}))

    def test_a_release_without_seek_counters_is_inconclusive(self):
        class C:
            def __init__(self, plan):
                self.plan, self.sent = plan, []

            def sql(self, text, db="ybm", flags=()):
                self.sent.append(text)
                name = "ybm_p14_gap_pkey" if "ybm_p14_gap " in text else "ybm_p14_ord_pkey"
                return subprocess.CompletedProcess([], 0, json.dumps(self.plan(name)), "")

        def plan(seeks):
            def make(name):
                node = {"Node Type": "Index Scan", "Index Name": name, "Actual Rows": 400,
                        "Storage Table Rows Scanned": 400}
                if seeks is not None:
                    node["Read Metrics"] = {"Metric rocksdb_number_db_seek":
                                            seeks if "gap" in name else 2}
                return [{"Plan": node}]
            return make
        for seeks, verdict in ((None, "inconclusive"), (801, "holds"), (20, "refuted")):
            with self.subTest(seeks=seeks):
                c = C(plan(seeks))
                out = probes.run(c, ["CAP014"], "")
                self.assertEqual(out["CAP014"]["verdict"], verdict, out)
                self.assertTrue(any("DEBUG" in t for t in c.sent))


class SmallFixes(Case):
    """Edges the hosted review found: the headline's share, a container that never became
    ready, --replay failing loudly, and lint advice on expression keys."""

    def test_the_headline_share_covers_the_patterns_it_names(self):
        f = {"id": "F1", "severity": "high", "confidence": "confirmed", "title": "T",
             "object": "o", "fix": "Do it.", "caveats": [],
             "patterns": ["P1", "P2", "P3", "P4", "P5"]}
        res = {"findings": [f], "patterns": [{"id": "P%d" % i, "time_share": 0.1}
                                             for i in range(1, 6)]}
        self.assertIn("on P1, P2, P3 and 2 more (50.0% of total statement time)",
                      report.headline(res))
        f["patterns"] = ["P1", "P2"]
        self.assertIn("on P1, P2 (20.0% of total statement time)", report.headline(res))

    def test_a_container_that_never_becomes_ready_is_removed(self):
        calls = []

        def sh(cmd, inp=None, timeout=600):
            calls.append(cmd[:2])
            return SimpleNamespace(returncode=0 if cmd[1] != "exec" else 1, stdout="",
                                   stderr="")
        with mock.patch.object(replay, "_sh", sh), mock.patch.object(replay.time, "sleep"):
            c = replay.Container("img:1", "ybm-test")
            c.sql = lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="")
            with self.assertRaises(RuntimeError):
                c.__enter__()
        self.assertEqual(calls[-1], ["docker", "rm"])

    def test_replay_on_request_fails_loudly(self):
        d = self.tmpdir({"schema.sql": "CREATE TABLE t (id bigint PRIMARY KEY);\n",
                         "queries.sql": "SELECT * FROM t WHERE id = 1;\n",
                         "ybm_meta.csv": "key,value\nversion,PostgreSQL 15.2-YB-2025.2.3.0-b1\n"})
        env = dict(os.environ, YBM_CONTAINER_CLI=shutil.which("false") or "/usr/bin/false")
        r = subprocess.run([sys.executable, os.path.join(HERE, "yb-model.py"), "review", d,
                            "--accept-missing", "--replay", "--image", "none:0"],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("replay failed", r.stderr)

    def test_lint_asks_for_not_null_on_the_columns_of_an_expression(self):
        lint = load_script("yb-lint.py")
        out = lint.lint("CREATE TABLE u (id bigint PRIMARY KEY, email text);\n"
                        "CREATE UNIQUE INDEX u_email ON u (lower(email));\n")
        f = [x for x in out if x.rule == "YB027"][0]
        self.assertTrue(f.fix.startswith("Declare email NOT NULL if they are never NULL (the input "
                                         "column(s) of lower(email)), or make the index "
                                         "partial: WHERE lower(email) IS NOT NULL."), f.fix)


class Volume(Case):
    def test_repeated_informational_lint_is_one_row_and_one_line(self):
        ddl = "".join("CREATE TABLE t%d (id bigint, ts timestamp, PRIMARY KEY ((id) HASH));\n"
                      % i for i in range(5))
        res = self.review(ddl=ddl)
        lint = [f for f in res["findings"] if f["rule"] == "LINT-YB031"]
        self.assertGreater(len(lint), 2)  # review.json keeps every finding
        md = report.render(res)
        self.assertEqual(len([l for l in md.splitlines() if "| LINT-YB031 |" in l]), 1)
        self.assertIn("| LINT-YB031 | %d objects" % len(lint), md)
        self.assertIn("LINT-YB031 on %d objects" % len(lint), md)


class AnnotationAsWritten(unittest.TestCase):
    def test_each_key_s_annotation_comes_from_the_ddl_as_written(self):
        ddl = ("CREATE TABLE a (id bigint, PRIMARY KEY ((id) HASH));\n"
               "CREATE TABLE b (id bigint PRIMARY KEY);\n"
               "CREATE INDEX b_x ON b (id ASC);\n")
        sch = schema.parse(ddl)
        for k in [k for t in sch.tables.values() for k in t.pk.keys] + \
                [k for i in sch.indexes.values() for k in i.keys]:
            k.explicit = False  # the catalog's dump-wide flag for a plain dump
        catalog.annotated_as_written(sch, ddl)
        self.assertEqual([k.explicit for k in sch.tables["a"].pk.keys], [True])
        self.assertEqual([k.explicit for k in sch.tables["b"].pk.keys], [False])
        self.assertEqual([k.explicit for k in sch.indexes["b_x"].keys], [True])


class ImageChoice(unittest.TestCase):
    def tags(self, *tags):
        out = "\n".join("docker.io/yugabytedb/yugabyte:%s" % t for t in tags)
        return mock.patch.object(replay, "_sh", return_value=mock.Mock(stdout=out))

    def test_exact_then_earlier_then_newer_of_the_line_never_across_lines(self):
        with self.tags("2025.2.6.0-b111", "2026.1.1.2-b9", "2025.2.4.0-b1"):
            self.assertEqual(replay.find_image("2025.2.6.0"),
                             ("yugabytedb/yugabyte:2025.2.6.0-b111", True))
            self.assertEqual(replay.find_image("2025.2.5.2"),
                             ("yugabytedb/yugabyte:2025.2.4.0-b1", False))
        with self.tags("2025.2.6.0-b111", "2026.1.1.2-b9"):
            self.assertEqual(replay.find_image("2025.2.5.2"),
                             ("yugabytedb/yugabyte:2025.2.6.0-b111", False))
            self.assertEqual(replay.find_image("2024.2.1.0"), (None, False))
        named = replay.find_image("2025.2.5.2", "yugabytedb/yugabyte:2026.1.1.2-b9")
        self.assertEqual(named, ("yugabytedb/yugabyte:2026.1.1.2-b9", False))

    def test_labels_say_whether_the_image_is_earlier_or_newer(self):
        vm = {"customer": "2025.2.5.2"}
        self.assertEqual(plans.image_relation(vm, "2025.2.6.0"),
                         "a newer release than 2025.2.5.2")
        self.assertEqual(plans.image_relation(vm, "2025.2.4.0"),
                         "nearest earlier release to 2025.2.5.2")


class NotLoaded(unittest.TestCase):
    def test_a_table_the_statement_names_but_the_schema_lacks_is_not_loaded(self):
        sch = schema.parse("CREATE TABLE t (id int PRIMARY KEY, x int);")
        q = "SELECT * FROM t JOIN logs l ON l.id = t.id JOIN arch.o o ON o.id = t.id"
        pat = {"shape": sqlshape.analyze(q, sch), "tables": ["t"]}
        self.assertEqual(replay.not_loaded(pat, {"public.t"}), ["arch.o", "logs"])
        self.assertEqual(replay.not_loaded(pat, None), [])


class Pitfalls(Case):
    """references/pitfalls.md claims only what a test pins; a reviewer's own entries never
    carry a name from the bundle they came from."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        patcher = mock.patch.dict(os.environ, {"YBM_HOME": self.home})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_every_handled_or_flagged_entry_names_a_test_that_exists(self):
        es = pitfalls.shipped()
        self.assertGreater(len(es), 10)
        self.assertEqual(len({e["id"] for e in es}), len(es))
        mod = sys.modules[__name__]
        for e in es:
            with self.subTest(e["id"]):
                self.assertIn(e["status"], pitfalls.STATUSES)
                self.assertTrue(e["body"])
                m = pitfalls.TEST.search(e["note"])
                if e["status"] == "not detected":
                    continue
                self.assertIsNotNone(m, "a %s entry names its test" % e["status"])
                obj = mod
                for part in m.group(1).split("."):
                    obj = getattr(obj, part, None)
                self.assertIsNotNone(obj, m.group(1))

    def test_an_entry_never_names_the_bundle(self):
        d = self.tmpdir({"schema.sql": "CREATE SCHEMA billing;\nCREATE TABLE billing.ledger_rows "
                                       "(id bigint PRIMARY KEY);\nCREATE INDEX ledger_by_day ON "
                                       "billing.ledger_rows (id);\n",
                         "ybm_meta.csv": "key,value\nversion,PostgreSQL 15.2-YB-2.20.1.3-b1\n"
                                         "node,10.1.2.3:5433\n",
                         "node1/ybm_meta.csv": "key,value\nnode,fd00::12:5433\n"})
        terms = pitfalls.private_terms(inputs.load(d)) | pitfalls.raw_terms(d)
        self.assertLessEqual({"billing", "ledger_rows", "ledger_by_day", "10.1.2.3"}, terms)
        self.assertNotIn("2.20.1.3", terms)  # a release is not an address
        self.assertIn("fd00::12", terms)  # nor is an IPv6 node address left out
        with self.assertRaises(pitfalls.Refused):
            pitfalls.add("v6-node", "A node lagged", "not detected", "fd00::12 lagged.", terms)
        # a release, or an address that is not the bundle's, is generic
        self.assertEqual(pitfalls.leaks("On 2.20.1.3 and v2.20.1.3-b1 the planner differs; "
                                        "seen behind 192.0.2.7 too.", terms), [])
        for body in ("The Ledger_Rows table was cut.", "Seen on 10.1.2.3 only.",
                     "schema billing had no CREATE.", "billing.ledger_rows was cut.",
                     "public.ledger_rows was cut.", "ledger_rows.id was null.",
                     "node 10.1.2.3:5433 lagged.", "index billing.ledger_by_day unused."):
            with self.subTest(body), self.assertRaises(pitfalls.Refused):
                pitfalls.add("cut-table", "A table was cut", "not detected", body, terms)
        with self.assertRaises(pitfalls.Refused):  # a shipped id is a hit, not a new entry
            pitfalls.add("stats-reset", "x", "not detected", "y", terms)
        self.assertEqual(pitfalls.hits()["stats-reset"]["count"], 1)
        pitfalls.add("pasted-grid", "Query grid pasted with borders", "not detected",
                     "Strip the box characters from a copy first.", terms)
        with self.assertRaises(pitfalls.Refused):
            pitfalls.add("pasted-grid", "again", "not detected", "again", terms)
        self.assertEqual([e["id"] for e in pitfalls.local()], ["pasted-grid"])
        self.assertIn("pasted-grid", pitfalls.listing())
        self.assertIn("### pasted-grid: Query grid pasted with borders", pitfalls.export())

    def test_an_unreadable_bundle_still_keeps_its_names_out(self):
        d = self.tmpdir({})
        with open(os.path.join(d, "schema.sql"), "wb") as fh:
            fh.write("CREATE TABLE shipment_legs (id bigint PRIMARY KEY);\n".encode("utf-16"))
        env = dict(os.environ, YBM_HOME=self.home)
        r = subprocess.run([sys.executable, os.path.join(HERE, "yb-model.py"), "pitfall", "add",
                            d, "--id", "utf16-dump", "--title", "UTF-16 dump", "--body",
                            "shipment_legs came out empty"], capture_output=True, text=True,
                           env=env)
        self.assertEqual(r.returncode, 2)
        self.assertIn("shipment_legs", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.home, "pitfalls.md")))

    def test_a_plain_dump_is_flagged_and_counted(self):
        d = self.tmpdir({"schema.sql": "CREATE TABLE t (id bigint PRIMARY KEY, v int);\n",
                         "queries.sql": "SELECT v FROM t WHERE id = 1;\n"})
        r = subprocess.run([sys.executable, os.path.join(HERE, "yb-model.py"), "review", d,
                            "--accept-missing", "--no-replay"], capture_output=True, text=True,
                           env=dict(os.environ, YBM_HOME=self.home))
        self.assertIn(r.returncode, (0, 1), r.stderr)
        with open(os.path.join(d, "review.json")) as fh:
            res = json.load(fh)
        self.assertIn("CFG004", self.rules(res))
        self.assertEqual(prescriptions(res), [])
        self.assertIn("plain-dump", res["pitfalls"])
        with open(os.path.join(d, "review.md")) as fh:
            self.assertIn("Known pitfalls that apply (references/pitfalls.md): ", fh.read())
        self.assertEqual(pitfalls.hits()["plain-dump"]["count"], 1)


class NodeCoverage(Case):
    """pg_stat_statements and pg_stat_user_indexes count only what ran through the node they
    are read on. A capture per node is added up; until every node is in, an index with no
    scans is not proven unused and its drop starts with a check on every node."""

    DDL = ("CREATE TABLE t (id bigint NOT NULL, a int, PRIMARY KEY ((id) HASH));\n"
           "CREATE INDEX t_a ON t (a HASH);\n")
    USAGE = "schemaname,relname,indexrelname,idx_scan\npublic,t,t_a,%d\n"
    PSS = PSS_HEAD + "1,%d,%d,1,%d,SELECT * FROM t WHERE id = $1\n"

    def nodes(self, *captures, nodes=None, label="10.0.0.%d:5433"):
        """A bundle with one folder per node: captures are (idx_scan, calls); label is the
        node address collect.sql recorded (%d: the node's number)."""
        d = self.tmpdir({"schema.sql": self.DDL})
        for n, (scans, calls) in enumerate(captures, 1):
            sub = os.path.join(d, "node%d" % n)
            os.makedirs(sub)
            for name, text in (("ybm_index_usage.csv", self.USAGE % scans),
                               ("ybm_pss.csv", self.PSS % (calls, calls, calls)),
                               ("ybm_meta.csv", "key,value\nnode,%s\n%s" % (
                                   label.replace("%d", str(n)),
                                   "nodes,%d\n" % nodes if nodes else ""))):
                with open(os.path.join(sub, name), "w") as fh:
                    fh.write(text)
        return inputs.load(d)

    def test_one_node_of_unknown_many_is_not_proof(self):
        res = self.review(ddl=self.DDL, files={"ybm_index_usage.csv": self.USAGE % 0})
        f = self.only(res, "WRK003")
        self.assertEqual(f["confidence"].split()[0].rstrip(";,"), "probable")
        self.assertIn("on every node", f["ddl"])
        self.assertIn("cluster of unknown size", f["fact"])
        self.assertTrue(any(o.startswith("Workload and usage counters cover") for o in
                            res["open_items"]))

    def test_every_node_captured_confirms(self):
        b = self.nodes((0, 10), (0, 20), nodes=2)
        self.assertTrue(b.usage_complete)
        self.assertEqual((b.index_usage["t_a"]["idx_scan"], b.pss[0]["calls"]), (0, 30))
        res = analyze.run(b)
        f = self.only(res, "WRK003")
        self.assertTrue(f["confidence"].startswith("confirmed"))
        # One finding carries the drop (others on t_a defer to it); no check is needed.
        self.assertEqual([x["ddl"] for x in res["findings"] if x.get("ddl")], ["DROP INDEX t_a;"])
        self.assertFalse(any(o.startswith("Workload and usage counters cover") for o in
                             res["open_items"]))

    def test_scans_on_another_node_keep_the_index(self):
        b = self.nodes((0, 10), (7, 20), nodes=2)
        self.assertEqual(b.index_usage["t_a"]["idx_scan"], 7)
        self.assertNotIn("WRK003", self.rules(analyze.run(b)))

    def test_a_node_captured_twice_counts_once(self):
        b = self.nodes((0, 10), (0, 20), nodes=2)
        d = os.path.dirname(b.ddl_files[0])
        shutil.copytree(os.path.join(d, "node1"), os.path.join(d, "node1-copy"))
        b = inputs.load(d)
        self.assertEqual(b.pss[0]["calls"], 30)
        self.assertEqual(b.duplicate_captures, [("10.0.0.1:5433", "node1-copy")])
        self.assertTrue(any("node1-copy" in o for o in analyze.run(b)["open_items"]))

    def test_loopback_labels_fall_back_to_the_folder(self):
        # collect.sql run on each host over localhost (or a socket) labels every node alike.
        for label in ("127.0.0.1:5433", "local:", "::1:5433"):
            with self.subTest(label):
                b = self.nodes((0, 10), (0, 20), (0, 30), nodes=3, label=label)
                self.assertEqual(b.pss[0]["calls"], 60)
                self.assertEqual(sorted(b.captures), ["node1", "node2", "node3"])
                self.assertEqual(b.duplicate_captures, [])
                # Folder names do not show the folders are different nodes: one may repeat
                # another's node, so a drop still starts with the every-node check.
                self.assertFalse(b.usage_complete)
                res = analyze.run(b)
                self.assertIn("node-label-loopback", res["pitfalls"])
                self.assertTrue(any("loopback address or a Unix socket" in o
                                    for o in res["open_items"]))
                self.assertTrue(self.only(res, "WRK003")["confidence"].startswith("probable"))
                drops = [x["ddl"] for x in res["findings"] if x.get("ddl")]
                self.assertTrue(drops and all("first, on every node" in x for x in drops), drops)
        # One node, one capture over localhost: nothing to tell apart.
        b = self.nodes((0, 10), nodes=1, label="127.0.0.1:5433")
        self.assertTrue(b.usage_complete)

    def test_row_estimates_are_not_added_up(self):
        b = self.nodes((0, 10), (0, 20), nodes=2)
        d = os.path.dirname(b.ddl_files[0])
        for n, (ins, live) in ((1, (5, 1000)), (2, (7, 40))):
            with open(os.path.join(d, "node%d" % n, "ybm_table_usage.csv"), "w") as fh:
                fh.write("schemaname,relname,n_tup_ins,n_live_tup\npublic,t,%d,%d\n" % (ins, live))
        u = inputs.load(d).table_usage["t"]
        self.assertEqual((u["n_tup_ins"], u["n_live_tup"]), (12, 1000))

    def test_fewer_captures_than_nodes(self):
        b = self.nodes((0, 10), nodes=3)
        self.assertFalse(b.usage_complete)
        f = self.only(analyze.run(b), "WRK003")
        self.assertIn("1 of 3 nodes", f["fact"])


class Schemas(Case):
    """Tables outside schema public are known as schema.name, so same-named tables in two
    schemas stay apart, DDL names the schema, and an unqualified name in a query resolves
    through search_path as the server resolves it."""

    DDL = ("CREATE TABLE public.orders (id bigint NOT NULL, customer_id bigint, "
           "PRIMARY KEY ((id) HASH));\n"
           "CREATE INDEX orders_customer ON public.orders USING lsm (customer_id HASH);\n"
           "CREATE TABLE archive.orders (id bigint NOT NULL, legacy_ref text, "
           "CONSTRAINT orders_pkey PRIMARY KEY ((id) HASH));\n"
           "CREATE INDEX orders_customer ON archive.orders USING lsm (legacy_ref HASH);\n"
           "ALTER TABLE ONLY archive.orders ADD CONSTRAINT orders_ref UNIQUE (legacy_ref);\n")

    def test_same_names_in_two_schemas_stay_apart(self):
        sch = schema.parse(self.DDL)
        self.assertEqual(sorted(sch.tables), ["archive.orders", "orders"])
        self.assertEqual(sorted(sch.indexes),
                         ["archive.orders_customer", "archive.orders_ref", "orders_customer"])
        self.assertEqual(sch.tables["archive.orders"].col_order, ["id", "legacy_ref"])
        self.assertEqual(sch.tables["archive.orders"].pk.name, "archive.orders_pkey")
        self.assertEqual(sch.tables["orders"].pk.name, "orders_pkey")

    def test_ddl_names_the_schema_and_reads_back(self):
        sch = schema.parse(self.DDL)
        idx = sch.indexes["archive.orders_customer"]
        self.assertEqual(schema.drop_sql(idx), "DROP INDEX archive.orders_customer;")
        self.assertTrue(schema.index_sql(idx, idx.name + "_v2").startswith(
            "CREATE INDEX CONCURRENTLY orders_customer_v2 ON archive.orders "))
        self.assertTrue(schema.drop_sql(sch.indexes["archive.orders_ref"]).startswith(
            "ALTER TABLE archive.orders DROP CONSTRAINT orders_ref;"))
        self.assertEqual([c.as_tuple() for c in safety.changes_of(
            "DROP INDEX archive.orders_customer;\n"
            "ALTER TABLE archive.orders DROP CONSTRAINT orders_ref;")],
            [("drop", "archive.orders_customer"), ("drop", "archive.orders_ref")])
        self.assertEqual([c.as_tuple() for c in safety.changes_of(
            "-- CREATE TABLE archive.orders_new (... PRIMARY KEY ((legacy_ref) HASH));")],
            [("rekey", "archive.orders", "(legacy_ref) HASH")])

    def test_queries_resolve_through_the_search_path(self):
        q = ("SELECT * FROM orders WHERE customer_id = $1;\n"
             "SELECT * FROM archive.orders WHERE legacy_ref = $1;\n")
        res = self.review(ddl=self.DDL, queries=q)
        self.assertEqual([p["tables"] for p in res["patterns"]], [["orders"], ["archive.orders"]])
        item = [o for o in res["open_items"] if o.startswith("Queries name orders")]
        self.assertTrue(item and "archive.orders" in item[0], res["open_items"])
        res = self.review(ddl=self.DDL, queries=q, files={
            "ybm_settings.csv": "name,setting\nsearch_path,\"archive, public\"\n"})
        self.assertEqual(res["patterns"][0]["tables"], ["archive.orders"])

    def test_a_name_only_schemas_off_the_search_path_define_is_reported(self):
        ddl = "".join("CREATE TABLE %s.orders (id bigint NOT NULL, PRIMARY KEY ((id) HASH));\n"
                      % t for t in ("tenant_a", "tenant_b"))
        res = self.review(ddl=ddl, queries="SELECT * FROM orders WHERE id = $1")
        item = [o for o in res["open_items"] if o.startswith("Queries name orders")]
        self.assertTrue(item, res["open_items"])
        self.assertIn("tenant_a.orders and tenant_b.orders define it, but none of them is on "
                      "the search path (public)", item[0])
        self.assertIn("several-schemas", res["pitfalls"])

    def test_a_name_off_the_search_path_is_never_read_as_public(self):
        # public is not on the search path: its orders must not stand in for the name.
        ddl = ("CREATE TABLE public.orders (id bigint NOT NULL, PRIMARY KEY ((id) HASH));\n"
               "CREATE TABLE tenant_b.orders (id bigint NOT NULL, PRIMARY KEY ((id) HASH));\n")
        res = self.review(ddl=ddl, queries="SELECT * FROM orders WHERE id = $1", files={
            "ybm_settings.csv": "name,setting\nsearch_path,app\n"})
        self.assertEqual([p["tables"] for p in res["patterns"]], [[]])  # not public.orders
        self.assertTrue(any("none of them is on the search path (app)" in o
                            for o in res["open_items"]), res["open_items"])

    def test_a_name_only_one_schema_off_the_path_defines_is_read_there_and_said(self):
        ddl = "".join("CREATE TABLE app.%s (id bigint NOT NULL, PRIMARY KEY ((id) HASH));\n" % t
                      for t in ("orders", "items"))
        res = self.review(ddl=ddl, queries="SELECT * FROM orders WHERE id = $1;\n"
                                           "SELECT * FROM items WHERE id = $1")
        self.assertEqual(sorted(p["tables"][0] for p in res["patterns"]),
                         ["app.items", "app.orders"])
        item = [o for o in res["open_items"] if o.startswith("Queries name items, orders")]
        self.assertTrue(item and "only schema app defines them" in item[0], res["open_items"])
        self.assertIn("several-schemas", res["pitfalls"])

    def test_replayed_plans_name_relations_as_the_model_does(self):
        sch = schema.parse(self.DDL)
        scan = {"Node Type": "Index Scan", "Relation Name": "orders",
                "Index Name": "orders_customer"}
        for extra, table in (({"Schema": "archive"}, "archive.orders"), ({}, "orders")):
            with self.subTest(table):
                facts = plans.summarize([{"Plan": dict(scan, **extra)}], sch)
                self.assertEqual((facts["scans"][0]["table"], facts["scans"][0]["index"]),
                                 (table, schema.in_schema_of(table, "orders_customer")))

    def test_replay_resolves_names_on_the_same_search_path(self):
        b = self.bundle(ddl=self.DDL, files={
            "ybm_settings.csv": "name,setting\nsearch_path,\"archive, public\"\n"})
        sql, _ = replay.settings_sql(b, "customer", None, {}, {})
        self.assertIn("SET search_path = archive, public;", sql)

    def test_catalog_keys_partition_parents_and_splits_by_schema(self):
        ddl, declared = catalog.one_tablet(
            "CREATE INDEX NONCONCURRENTLY orders_customer ON archive.orders USING lsm "
            "(legacy_ref HASH) SPLIT INTO 6 TABLETS;\n"
            "CREATE INDEX NONCONCURRENTLY orders_customer ON public.orders USING lsm "
            "(customer_id HASH) SPLIT INTO 2 TABLETS;\n")
        self.assertEqual(declared, {"archive.orders_customer": 6, "orders_customer": 2})
        cols = [{"name": "ts", "type": "date", "notnull": True, "identity": False,
                 "default": None}]
        sch = catalog.build({"fks": [], "tables": [
            {"name": "ev", "schema": "public", "kind": "p", "partkey": "RANGE (ts)",
             "columns": cols, "props": {}},
            {"name": "ev_q1", "schema": "parts", "kind": "r", "parent": "ev",
             "parent_schema": "public", "columns": cols, "props": {"is_colocated": False}}],
            "indexes": [
                {"name": "ev_ts", "schema": "public", "table": "ev", "primary": False,
                 "unique": False, "def": "CREATE INDEX ev_ts ON ONLY public.ev USING lsm (ts ASC)"},
                {"name": "ev_q1_ts_idx", "schema": "parts", "table": "ev_q1", "primary": False,
                 "unique": False, "parent": "ev_ts", "parent_schema": "public",
                 "def": "CREATE INDEX ev_q1_ts_idx ON parts.ev_q1 USING lsm (ts ASC)"}]})
        self.assertEqual(sch.tables["parts.ev_q1"].partition_of, "ev")
        self.assertEqual(sch.indexes["parts.ev_q1_ts_idx"].parent, "ev_ts")
        self.assertEqual(analyze.partition_copies(sch, "ev_ts"), ["parts.ev_q1_ts_idx"])

    def test_statistics_follow_their_schema(self):
        b = self.bundle(ddl=self.DDL, files={"ybm_pg_stats.csv": stats_csv([]) +
                                             "archive,orders,legacy_ref,f,0.9,8,10,,,,0\n"
                                             "public,orders,customer_id,f,0.1,8,10,,,,0\n",
                                             "ybm_reltuples.csv": "schemaname,relname,relkind,"
                                             "reltuples\narchive,orders,r,1000\npublic,orders,r,9\n"})
        sch = analyze.build_schema(b)
        self.assertEqual(sorted(b.stats), [("archive.orders", "legacy_ref"),
                                           ("orders", "customer_id")])
        self.assertEqual(b.reltuples, {"archive.orders": 1000.0, "orders": 9.0})
        self.assertIn("archive.orders", sch.tables)


class PartitionIndexCopies(Case):
    """An index on a partition that the server made as a copy of a partitioned index cannot
    be dropped on its own (YSQL: "cannot drop index ... because index ... requires it"): a
    finding on it folds into the finding on the parent index, which rebuilds every copy."""

    def test_copies_fold_into_the_parent_finding(self):
        cols = [{"name": "id", "type": "bigint", "notnull": True, "identity": False,
                 "default": None},
                {"name": "tenant_id", "type": "bigint", "notnull": False, "identity": False,
                 "default": None},
                {"name": "created_at", "type": "date", "notnull": True, "identity": False,
                 "default": None}]
        tables = [{"name": "events", "schema": "public", "kind": "p",
                   "partkey": "RANGE (created_at)", "columns": cols, "props": {}}]
        indexes = [{"name": "events_tenant", "table": "events", "primary": False,
                    "unique": False,
                    "def": "CREATE INDEX events_tenant ON ONLY public.events USING lsm "
                           "(tenant_id HASH)"}]
        for q in ("q1", "q2"):
            tables.append({"name": "events_" + q, "schema": "public", "kind": "r",
                           "parent": "events", "props": {"is_colocated": False},
                           "bound": "FOR VALUES FROM ('2026-01-01') TO ('2026-04-01')",
                           "columns": cols})
            indexes.append({"name": "events_%s_tenant_id_idx" % q, "table": "events_" + q,
                            "primary": False, "unique": False, "parent": "events_tenant",
                            "def": "CREATE INDEX events_%s_tenant_id_idx ON public.events_%s "
                                   "USING lsm (tenant_id HASH)" % (q, q)})
        b = self.bundle(ddl="-- loaded on a scratch server\n", rows={
            "events": 2e7, "events_q1": 1e7, "events_q2": 1e7}, stats=[
            ("events_q1", "tenant_id", 0.7, 100, [], None),
            ("events_q2", "tenant_id", 0.7, 100, [], None),
            ("events", "tenant_id", 0.7, 100, [], None)])
        self.cat = {"tables": tables, "indexes": indexes, "fks": []}
        self.rows = {"events": 2e7, "events_q1": 1e7, "events_q2": 1e7}
        self.stats = [("events_q1", "tenant_id", 0.7, 100, [], None),
                      ("events_q2", "tenant_id", 0.7, 100, [], None),
                      ("events", "tenant_id", 0.7, 100, [], None)]
        b.catalog_schema = catalog.build(json.loads(json.dumps(self.cat)))
        res = analyze.run(b)
        on = {f["index"]: f for f in res["findings"] if f["rule"] == "STA001"}
        self.assertEqual(sorted(on), ["events_tenant"])  # one finding, on the parent
        self.assertIn("events_q1_tenant_id_idx", on["events_tenant"]["fact"])
        for f in res["findings"]:
            self.assertNotRegex(f.get("ddl") or "", r"DROP INDEX events_q\d_tenant_id_idx")

    def test_a_used_copy_keeps_the_parent_rebuilt_not_dropped(self):
        self.test_copies_fold_into_the_parent_finding()
        b = self.bundle(ddl="-- loaded on a scratch server\n", rows=self.rows, stats=self.stats,
                        pss=PSS_HEAD + "1,1000000,5000,0.005,1000000,"
                                       "SELECT * FROM events_q1 WHERE tenant_id = $1\n")
        b.catalog_schema = catalog.build(json.loads(json.dumps(self.cat)))
        f = self.only(analyze.run(b), "STA001")
        self.assertNotIn("DROP INDEX events_tenant;\n", (f["ddl"] or "") + "\n")
        self.assertIn("CREATE INDEX", f["ddl"] or "")

    DUMP = ("CREATE TABLE public.events (id bigint NOT NULL, tenant_id bigint, created_at date "
            "NOT NULL) PARTITION BY RANGE (created_at);\n"
            "CREATE TABLE public.events_q1 PARTITION OF public.events FOR VALUES FROM "
            "('2026-01-01') TO ('2026-04-01');\n"
            "CREATE TABLE public.events_q2 PARTITION OF public.events FOR VALUES FROM "
            "('2026-04-01') TO ('2026-07-01');\n"
            "CREATE INDEX events_tenant ON ONLY public.events USING lsm (tenant_id HASH);\n"
            "CREATE INDEX events_q1_tenant_id_idx ON public.events_q1 USING lsm (tenant_id HASH);\n"
            "CREATE INDEX events_q2_tenant_id_idx ON public.events_q2 USING lsm (tenant_id HASH);\n"
            "ALTER INDEX public.events_tenant ATTACH PARTITION public.events_q1_tenant_id_idx;\n"
            "ALTER INDEX public.events_tenant ATTACH PARTITION public.events_q2_tenant_id_idx;\n")

    def usage(self, q1, q2):
        return {"ybm_index_usage.csv": "schemaname,relname,indexrelname,idx_scan\n"
                "public,events_q1,events_q1_tenant_id_idx,%d\n"
                "public,events_q2,events_q2_tenant_id_idx,%d\n" % (q1, q2)}

    def test_a_dump_names_the_copies(self):
        sch = schema.parse(self.DUMP)
        self.assertEqual(analyze.partition_copies(sch, "events_tenant"),
                         ["events_q1_tenant_id_idx", "events_q2_tenant_id_idx"])
        self.assertEqual(analyze.usage_names(sch, "events_tenant"),
                         ["events_q1_tenant_id_idx", "events_q2_tenant_id_idx"])

    def test_an_unused_partitioned_index_is_judged_and_dropped_through_its_parent(self):
        q = "SELECT id FROM events WHERE created_at = $1"
        res = self.review(ddl=self.DUMP, pss=PSS_HEAD + "1,10,5,0.5,10,%s\n" % q,
                          files=self.usage(0, 0))
        wrk = [f for f in res["findings"] if f["rule"] == "WRK003"]
        self.assertEqual([f["index"] for f in wrk], ["events_tenant"])  # never a copy
        ddl = "\n".join(f.get("ddl") or "" for f in res["findings"])
        self.assertIn("indexrelname IN ('events_q1_tenant_id_idx', 'events_q2_tenant_id_idx')",
                      ddl)
        self.assertIn("DROP INDEX events_tenant;", ddl)
        self.assertNotRegex(ddl, r"DROP INDEX events_q\d_tenant_id_idx")

    def test_scans_on_a_copy_keep_the_partitioned_index(self):
        # A boolean partitioned index gets a pure DROP (STA008); its copies were scanned.
        ddl = self.DUMP.replace("tenant_id bigint", "tenant_id bigint, archived boolean") + (
            "CREATE INDEX events_arch ON ONLY public.events USING lsm (archived HASH);\n"
            "CREATE INDEX events_q1_archived_idx ON public.events_q1 USING lsm (archived HASH);\n"
            "ALTER INDEX public.events_arch ATTACH PARTITION public.events_q1_archived_idx;\n")
        usage = self.usage(0, 7)  # one copy of events_tenant unscanned, the other scanned
        usage["ybm_index_usage.csv"] += "public,events_q1,events_q1_archived_idx,7\n"
        res = self.review(ddl=ddl, pss=PSS_HEAD + "1,10,5,0.5,10,SELECT 1\n", files=usage)
        f = self.only(res, "STA008")
        self.assertEqual(f["index"], "events_arch")
        self.assertIn("indexrelname IN ('events_q1_archived_idx')", f["ddl"])
        drop = next(o for o in f["options"] if o["id"] == "drop")
        self.assertIn("drops events_arch, which was scanned 7 times", drop["costs"])
        self.assertNotIn("WRK003", self.rules(res))  # no copy is "unused" on its own

    def test_a_copy_is_never_dropped_on_its_own(self):
        # One partition skewed, the other not: the parent (which borrows the first partition's
        # statistics) has no finding, so the copy's finding has no parent finding to fold into,
        # and a pattern on the parent table uses the parent index, not the copy.
        st = [("events_q1", "tenant_id", 0.0, -1, [], None),
              ("events_q2", "tenant_id", 0.7, 100, [], None)]
        pss = PSS_HEAD + "1,1000,500,0.5,1000,SELECT id FROM events WHERE tenant_id = $1\n"
        rows = {"events": 2e7, "events_q1": 1e7, "events_q2": 1e7}
        for mode in ("text", "catalog"):
            with self.subTest(mode):
                if mode == "text":
                    res = self.review(ddl=self.DUMP, stats=st, rows=rows, pss=pss)
                else:
                    self.test_copies_fold_into_the_parent_finding()
                    b = self.bundle(ddl="-- loaded on a scratch server\n", stats=st, rows=rows,
                                    pss=pss)
                    b.catalog_schema = catalog.build(json.loads(json.dumps(self.cat)))
                    res = analyze.run(b)
                f = [f for f in res["findings"] if f["rule"] == "STA001"]
                self.assertEqual([x["index"] for x in f], ["events_q2_tenant_id_idx"])
                self.assertIsNone(f[0]["ddl"])
                self.assertIn("only through events_tenant", f[0]["fix"])
                for f in res["findings"]:
                    self.assertNotRegex(f.get("ddl") or "", r"DROP INDEX events_q\d_tenant_id_idx")

    def test_rebuilding_a_unique_partitioned_index_keeps_its_guarantee(self):
        ddl = self.DUMP + (
            "CREATE UNIQUE INDEX events_u ON ONLY public.events USING lsm (tenant_id HASH, "
            "created_at ASC);\n" + "".join(
                "CREATE UNIQUE INDEX events_%s_u ON public.events_%s USING lsm (tenant_id HASH, "
                "created_at ASC);\nALTER INDEX public.events_u ATTACH PARTITION "
                "public.events_%s_u;\n" % (q, q, q) for q in ("q1", "q2")))
        sch = schema.parse(ddl)
        f, opt = annotated(sch, "CAP020", "CREATE UNIQUE INDEX events_u_cov ON events "
                                          "((tenant_id) HASH, created_at ASC) INCLUDE (id);\n"
                                          "DROP INDEX events_u;  -- after events_u_cov is valid",
                           "events_u", table="events", index="events_u")
        self.assertEqual(opt["costs"], [])
        after = safety.apply(sch, f.changes)
        self.assertEqual(sorted(analyze.partition_copies(after, "events_u_cov")),
                         ["events_q1_events_u_cov", "events_q2_events_u_cov"])

    def test_lint_never_advises_dropping_a_partition_copy(self):
        ddl = self.DUMP + (
            "CREATE INDEX events_tenant_ts ON ONLY public.events USING lsm (tenant_id HASH, "
            "created_at ASC);\n" + "".join(
                "CREATE INDEX events_%s_ts ON public.events_%s USING lsm (tenant_id HASH, "
                "created_at ASC);\nALTER INDEX public.events_tenant_ts ATTACH PARTITION "
                "public.events_%s_ts;\n" % (q, q, q) for q in ("q1", "q2")))
        res = self.review(ddl=ddl)
        # The pairs of copies are superseded in the lint rows, and no finding is made of them.
        rows = [r for r in res["lint"] if r["rule"] in ("YB051", "YB052")
                and "events_q" in r["message"]]
        self.assertTrue(rows)
        self.assertTrue(all(r["superseded"] for r in rows), rows)
        self.assertFalse([f for f in res["findings"] if f["rule"].startswith("LINT-YB05")
                          and "events_q" in f["fact"]])
        self.assertFalse(any(o.startswith("yb-lint.py failed") for o in res["open_items"]))

    def test_dropping_a_partitioned_index_drops_its_copies(self):
        self.test_copies_fold_into_the_parent_finding()
        sch = catalog.build(json.loads(json.dumps(self.cat)))
        after = safety.apply(sch, safety.changes_of("DROP INDEX events_tenant;"))
        self.assertEqual([n for n in after.indexes if "tenant" in n], [])


@unittest.skipUnless(os.environ.get("YBM_LIVE") and os.path.isdir(FIXTURE),
                     "set YBM_LIVE=1 to run against a local container")
class CatalogLive(unittest.TestCase):
    """End to end on a scratch container: the catalog review of the fixture equals the text
    review except for SPLIT clauses the fixture never declared beyond one tablet."""

    def test_fixture_reviews_alike(self):
        text = analyze.run(inputs.load(FIXTURE))
        b = inputs.load(FIXTURE)
        catalog.attach(b)
        cat = analyze.run(b)
        key = lambda f: (f["rule"], f["object"], f["severity"])  # noqa: E731
        self.assertEqual(sorted(map(key, text["findings"])), sorted(map(key, cat["findings"])))
        self.assertEqual(b.catalog_info["rejected"], [])


class QueryShapes(unittest.TestCase):
    def test_or_binds_looser_than_and(self):
        """An OR of conjunctions written without parentheses round each arm is a disjunction:
        no column of it is bound by equality, and a column every arm binds is recorded as
        or_eq."""
        for where in ("s = 'x' AND d = '' OR s = 'y' AND d = ''",
                      "(s = 'x' AND d = '') OR (s = 'y' AND d = '')"):
            with self.subTest(where):
                ops = sorted({(p.col, p.op) for p in sqlshape.analyze(
                    "SELECT a FROM t WHERE k = $1 AND (%s)" % where).preds})
                self.assertEqual(ops, [("d", "or"), ("d", "or_eq"), ("k", "eq"), ("s", "or"),
                                       ("s", "or_eq")])
        ops = {(p.col, p.op) for p in sqlshape.analyze(
            "SELECT a FROM t WHERE s = 'x' AND d = '' OR s = 'y' AND d != ''").preds}
        self.assertIn(("s", "or_eq"), ops)
        self.assertNotIn(("d", "or_eq"), ops)  # one arm does not bind it

    def test_row_in_binds_like_an_in_list(self):
        sh = sqlshape.analyze("SELECT a FROM t WHERE (s, d) IN (('x', ''), ('y', ''))")
        self.assertEqual(sorted((p.col, p.op) for p in sh.preds), [("d", "in"), ("s", "in")])
        sh = sqlshape.analyze("SELECT a FROM t WHERE (s, d) < ($1, $2)")
        self.assertEqual(sorted((p.col, p.op) for p in sh.preds),
                         [("d", "rowcmp"), ("s", "rowcmp")])  # a comparison still binds nothing

    def test_an_or_of_stats_is_not_a_point_lookup(self):
        ddl = ("CREATE TABLE es (c text NOT NULL, e text NOT NULL, v text NOT NULL, "
               "s text NOT NULL, d text NOT NULL, f text NOT NULL, x float, "
               "PRIMARY KEY ((c, e) HASH, v, s, d, f));\n")
        sch = schema.parse(ddl)
        sh = sqlshape.analyze("SELECT x FROM es WHERE c = $1 AND e = $2 AND (v = 'a' AND "
                              "f = '' AND (s = 'x' AND d = '' OR s = 'y' AND d = ''))", sch)
        ops = analyze._pred_index(sh.preds_for("es"))
        r = analyze.eval_path(sch.tables["es"].pk, ops, sh, "es", cbo=False)
        self.assertFalse(r["full_unique"], r)
        self.assertEqual(r["bound"], 3, r)

    def test_the_binding_map_names_the_gap_and_what_follows_it(self):
        sch = schema.parse("CREATE TABLE es (c text NOT NULL, e text NOT NULL, v text NOT NULL, "
                           "s text NOT NULL, d text NOT NULL, f text NOT NULL, x float, "
                           "PRIMARY KEY ((c, e) HASH, v, s, d, f));\n")
        pk = sch.tables["es"].pk
        for where, prefix, gap, after, text in (
                ("v = 'a' AND f = 'w1'", 3, ["s", "d"], ["f"], "= = = - - ="),
                ("v = 'a' AND f = '' AND (s = 'x' AND d = '' OR s = 'y' AND d = '')", 3, [], [],
                 "= = = or= or= ="),
                ("v = 'a' AND f = '' AND (s = 'x' AND d != '' OR s = 'y' AND d != '')", 3,
                 ["d"], ["f"], "= = = or= f ="),
                ("v = 'a' AND s = 'x' AND d = '' AND f = ''", 6, [], [], "= = = = = ="),
                ("v = 'a' AND s > 'x' AND f = ''", 4, ["d"], ["f"], "= = = <> - ="),
                ("v = 'a'", 3, [], [], "= = = - - -")):
            with self.subTest(where):
                sh = sqlshape.analyze("SELECT x FROM es WHERE c = $1 AND e = $2 AND " + where, sch)
                r = analyze.eval_path(pk, analyze._pred_index(sh.preds_for("es")), sh, "es",
                                      cbo=False)
                m = r["binding"]
                self.assertEqual((m["prefix"], m["gap"], m["after_gap"]), (prefix, gap, after))
                self.assertEqual(report._binding_text(m), text)
        sh = sqlshape.analyze("SELECT x FROM es WHERE c = $1 AND f = ''", sch)  # hash unbound
        r = analyze.eval_path(pk, analyze._pred_index(sh.preds_for("es")), sh, "es", cbo=False)
        self.assertNotIn("binding", r)  # not usable: no map

    def test_expression_predicate(self):
        sh = sqlshape.analyze("SELECT id FROM c WHERE lower(email) = $1")
        self.assertEqual([(p.expr, p.op) for p in sh.preds], [("lower(email)", "eq")])

    def test_between_and_like_prefix(self):
        sh = sqlshape.analyze("SELECT 1 FROM t WHERE a BETWEEN $1 AND $2 AND b LIKE 'x%' AND c LIKE '%x'")
        self.assertEqual(sorted((p.col, p.op) for p in sh.preds),
                         [("a", "range"), ("b", "prefix"), ("c", "like")])

    def test_cte_is_not_a_table(self):
        sh = sqlshape.analyze("WITH x AS (SELECT id FROM o WHERE c = $1) SELECT * FROM x")
        self.assertEqual(sh.tables, [])
        self.assertEqual(sh.subshapes[0].tables, ["o"])

    def test_aggregate_flag(self):
        self.assertTrue(sqlshape.analyze("SELECT count(*) FROM t WHERE a = 1").aggregate)

    def test_on_conflict_forms(self):
        sh = sqlshape.analyze("INSERT INTO t (a, b) VALUES ($1, $2) "
                              "ON CONFLICT ON CONSTRAINT t_a_key DO NOTHING")
        self.assertEqual((sh.conflict_cols, sh.conflict_constraint), ([], "t_a_key"))
        sh = sqlshape.analyze("INSERT INTO t (a, b) VALUES ($1, $2) ON CONFLICT (a) "
                              "WHERE a IS NOT NULL DO UPDATE SET b = excluded.b")
        self.assertEqual((sh.conflict_cols, sh.conflict_where), (["a"], "a is not null"))


class Safety(unittest.TestCase):
    def test_rekey_to_superset_loses_uniqueness(self):
        s = schema.parse("CREATE TABLE t (id int, p int, o int, PRIMARY KEY (id HASH));")
        after = safety.apply(s, safety.changes_of(
            "-- CREATE TABLE t_new (... PRIMARY KEY ((p) HASH, o ASC, id ASC)) SPLIT INTO 3 TABLETS;"))
        self.assertEqual([n for *_, n in safety.lost_uniqueness(s, after)], ["t_pkey"])
        after2 = safety.apply(s, safety.changes_of(
            "-- CREATE TABLE t_new (... PRIMARY KEY ((p) HASH, o ASC, id ASC));\n"
            "CREATE UNIQUE INDEX CONCURRENTLY t_id ON t ((id) HASH);"))
        self.assertEqual(safety.lost_uniqueness(s, after2), [])

    def test_non_unique_replacement_of_unique_index(self):
        s = schema.parse("CREATE TABLE t (a int, b int, PRIMARY KEY (a HASH));"
                         "CREATE UNIQUE INDEX u ON t (b HASH) WHERE b IS NOT NULL;")
        after = safety.apply(s, safety.changes_of(
            "CREATE INDEX CONCURRENTLY u_nn ON t ((b) HASH) WHERE b IS NOT NULL;\n"
            "DROP INDEX CONCURRENTLY u;"))
        self.assertEqual([n for *_, n in safety.lost_uniqueness(s, after)], ["u"])

    def test_on_conflict_target(self):
        s = schema.parse("CREATE TABLE t (a int, b int, c int, PRIMARY KEY (a HASH));"
                         "CREATE UNIQUE INDEX u ON t (b, c);")
        pats = [{"id": "P1", "shape": sqlshape.analyze(
            "INSERT INTO t (a, b, c) VALUES ($1, $2, $3) ON CONFLICT (b, c) DO NOTHING", s)}]
        after = safety.apply(s, safety.changes_of("DROP INDEX CONCURRENTLY u;"))
        self.assertEqual(safety.lost_conflict_targets(s, after, pats), [("P1", "t", ["b", "c"])])

    def test_a_not_null_guard_on_a_unique_key_keeps_uniqueness(self):
        s = schema.parse("CREATE TABLE t (a int, b int, c int, st text, PRIMARY KEY (a HASH));"
                         "CREATE UNIQUE INDEX u ON t (b HASH) WHERE st = 'a';")
        new = "CREATE UNIQUE INDEX CONCURRENTLY u_v2 ON t ((b) HASH)%s;\nDROP INDEX u;"
        for label, where, lost in (
                ("a NULL key never conflicts", " WHERE (st = 'a') AND (b IS NOT NULL)", []),
                ("no predicate covers more rows", "", []),
                ("a guard on a column outside the key", " WHERE (st = 'a') AND (c IS NOT NULL)",
                 ["u"]),
                ("another predicate", " WHERE (st = 'b')", ["u"]),
                ("an OR is compared as written", " WHERE (st = 'a' OR b IS NOT NULL)", ["u"])):
            with self.subTest(label):
                after = safety.apply(s, safety.changes_of(new % where))
                self.assertEqual([n for *_, n in safety.lost_uniqueness(s, after)], lost)

    def test_nulls_not_distinct_is_part_of_the_guarantee(self):
        s = schema.parse("CREATE TABLE t (a int, b int, PRIMARY KEY (a HASH));"
                         "CREATE UNIQUE INDEX u ON t (b HASH) NULLS NOT DISTINCT;")
        for label, new, lost in (
                ("kept", "CREATE UNIQUE INDEX CONCURRENTLY u2 ON t ((b) HASH) NULLS NOT DISTINCT;", []),
                ("NULLs become distinct", "CREATE UNIQUE INDEX CONCURRENTLY u2 ON t ((b) HASH);",
                 ["u"]),
                # under NULLS NOT DISTINCT a NULL key does conflict, so excluding it is weaker
                ("a NOT NULL guard", "CREATE UNIQUE INDEX CONCURRENTLY u2 ON t ((b) HASH) NULLS "
                                     "NOT DISTINCT WHERE b IS NOT NULL;", ["u"])):
            with self.subTest(label):
                after = safety.apply(s, safety.changes_of(new + "\nDROP INDEX u;"))
                self.assertEqual([n for *_, n in safety.lost_uniqueness(s, after)], lost)

    def test_on_conflict_on_constraint_target(self):
        # The replacement keeps the columns unique, but not as the constraint the statement
        # names: the INSERT would fail.
        s = schema.parse("CREATE TABLE t (a int, b int, PRIMARY KEY (a HASH), "
                         "CONSTRAINT t_b_key UNIQUE (b));")
        pats = [{"id": "P1", "shape": sqlshape.analyze(
            "INSERT INTO t (a, b) VALUES ($1, $2) ON CONFLICT ON CONSTRAINT t_b_key DO NOTHING",
            s)}]
        after = safety.apply(s, safety.changes_of(
            "CREATE UNIQUE INDEX CONCURRENTLY t_b_v2 ON t ((b) HASH);\n"
            "ALTER TABLE t DROP CONSTRAINT t_b_key;"))
        self.assertEqual(safety.lost_conflict_targets(s, after, pats),
                         [("P1", "t", ["ON CONSTRAINT t_b_key"])])

    def test_bucketing_loses_order(self):
        s = schema.parse("CREATE TABLE t (id int, ts timestamptz, PRIMARY KEY (id HASH));"
                         "CREATE INDEX t_ts ON t (ts DESC);")
        pats = [{"id": "P1", "weight": "HOT", "shape": sqlshape.analyze(
            "SELECT id FROM t WHERE ts > $1 ORDER BY ts DESC LIMIT 10", s)}]
        before = safety.access_map(s, pats, analyze, False)
        after = safety.apply(s, safety.changes_of(
            "CREATE INDEX CONCURRENTLY t_ts_bkt ON t ((yb_hash_code(ts) % 16) ASC, ts DESC);\n"
            "DROP INDEX CONCURRENTLY t_ts;"))
        regs = safety.regressions(before, safety.access_map(after, pats, analyze, False),
                                  {"P1": "HOT"})
        # The bucketed index is still reachable by skip scan over its buckets, but rows no
        # longer come back in ts order.
        self.assertTrue(regs and "index order" in regs[0]["why"], regs)

    def test_two_rebuilds_of_one_index_stay_alternatives(self):
        # A NULL guard and a covering INCLUDE both rebuild one index: each keeps its own
        # sketch (alternatives), the area names the tension, and a plan that chooses both is
        # a conflict.
        s = schema.parse("CREATE TABLE o (id int, c int, d int, t int, PRIMARY KEY (id HASH));"
                         "CREATE INDEX oc ON o (c HASH, d ASC);")
        fs = [{"id": "F1", "rule": "STA001", "index": "oc", "changes": [
                  c.to_dict() for c in safety.changes_of(
                      "CREATE INDEX CONCURRENTLY oc_nn ON o ((c) HASH, d ASC) WHERE c IS NOT "
                      "NULL;\nDROP INDEX oc;")],
               "options": [{"id": "partial", "change": "rebuild", "sketched": True}]},
              {"id": "F2", "rule": "CAP020", "index": "oc", "changes": [
                  c.to_dict() for c in safety.changes_of(
                      "CREATE INDEX CONCURRENTLY oc_cov ON o ((c) HASH, d ASC) INCLUDE (t);\n"
                      "DROP INDEX oc;")],
               "options": [{"id": "include", "change": "rebuild", "sketched": True}]}]
        from ybm import dossier
        t = dossier.tensions(fs, [])
        self.assertTrue(any(x.startswith("F1 STA001, F2 CAP020 all concern oc") for x in t), t)
        self.assertIn("F1 and F2 each change oc: choose one change for it",
                      plan.conflicts(s, [], {"cbo": False}, inputs.Bundle("x"),
                                     [c for f in fs for c in map(
                                         changes.Change.from_dict, f["changes"])],
                                     {"oc": {"F1", "F2"}}))

@unittest.skipUnless(os.path.isdir(FIXTURE), "fixture bundle not present")
class Metamorphic(Case):
    """Changes to the fixture that must not change the review, or may change only how the
    recommended DDL is written. Every review of the fixture must also recommend DDL that
    YSQL can run."""

    def variant(self, ddl=lambda text: text, row=lambda name, r: None):
        """The review of the fixture with its schema passed through ddl(text) and every CSV
        row through row(file name, row)."""
        files = {}
        for name in sorted(os.listdir(FIXTURE)):
            with open(os.path.join(FIXTURE, name), newline="", encoding="utf-8") as fh:
                text = fh.read()
            if name == "schema.sql":
                text = ddl(text)
            elif name.endswith(".csv"):
                rows = list(csv.DictReader(io.StringIO(text)))
                for r in rows:
                    row(name, r)
                buf = io.StringIO()
                wr = csv.DictWriter(buf, fieldnames=list(rows[0]) if rows else [],
                                    lineterminator="\n")
                wr.writeheader()
                wr.writerows(rows)
                text = buf.getvalue()
            files[name] = text
        return analyze.run(inputs.load(self.tmpdir(files)))

    def assert_runnable(self, res, colocated=False, quoted=()):
        """No DROP INDEX CONCURRENTLY (YSQL rejects it); on a colocated database no HASH key
        and no SPLIT clause, comments included; every name in `quoted` written quoted."""
        for f in res["findings"]:
            ddl = f.get("ddl") or ""
            with self.subTest(f["id"]):
                self.assertNotIn("DROP INDEX CONCURRENTLY", ddl + (f.get("fix") or ""))
                if colocated:
                    self.assertEqual(re.findall(r"\bHASH\b|\bSPLIT\b", ddl), [], ddl)
                self.assertEqual([t.text for t in tokenize(ddl) if t.kind == "word" and
                                  t.text.lower() in quoted], [], ddl)

    def test_statement_order_does_not_matter(self):
        b = inputs.load(FIXTURE)
        base = _findings(b)
        stmts = []
        for st in split_statements(tokenize(b.ddl)):
            stmts.append(b.ddl[st[0].pos:st[-1].pos + len(st[-1].text)])
        # Tables before indexes still, but each group reversed.
        tables = [x for x in stmts if re.match(r"(?is)\s*CREATE\s+(TABLE|SEQUENCE)", x)]
        rest = [x for x in stmts if x not in tables]
        b.ddl = ";\n".join(list(reversed(tables)) + list(reversed(rest))) + ";\n"
        self.assertEqual(sorted(base), sorted(_findings(b)))

    def test_unrelated_table_does_not_change_findings(self):
        b = inputs.load(FIXTURE)
        base = _findings(b)
        b.ddl += "\nCREATE TABLE zz_unrelated (k bigint PRIMARY KEY, v text);\n" \
                 "CREATE INDEX zz_v ON zz_unrelated (v);\n"
        res = analyze.run(b)
        got = [(f["rule"], f["object"], f["severity"]) for f in res["findings"]
               if not f["rule"].startswith("LINT-") and "zz_" not in f["object"] and
               "zz_" not in f["fact"]]
        self.assertEqual(sorted(base), sorted(got))

    def test_scaling_rows_keeps_capability_findings(self):
        b = inputs.load(FIXTURE)
        base = {(r, o) for r, o, _ in _findings(b) if r.startswith("CAP")}
        b.reltuples = {k: v * 10 for k, v in b.reltuples.items()}
        got = {(r, o) for r, o, _ in _findings(b) if r.startswith("CAP")}
        self.assertEqual(base, got)

    def test_fixpoint(self):
        out = fixpoint.check(inputs.load(FIXTURE))
        self.assertEqual(out["problems"], [])
        self.assertGreater(out["fixed_with_ddl"], 0)

    def test_quoted_mixed_case_names_change_only_the_quoting(self):
        # Every table, index and column renamed to a quoted mixed-case name (orders ->
        # "Orders"), in the schema, the statistics, the usage files and the statements.
        sch = schema.parse(inputs.load(FIXTURE).ddl)
        names = (set(sch.tables) | set(sch.indexes) |
                 {t.pk.name for t in sch.tables.values() if t.pk} |
                 {c for t in sch.tables.values() for c in t.cols})
        mixed = {n: "_".join(p.capitalize() for p in n.split("_")) for n in names}

        def quote(text):
            out, last = [], 0
            for t in tokenize(text):
                if t.kind == "word" and t.text.lower() in mixed:
                    out += [text[last:t.pos], '"%s"' % mixed[t.text.lower()]]
                    last = t.pos + len(t.text)
            return "".join(out) + text[last:]

        def row(name, r):
            for k in ("tablename", "attname", "relname", "indexrelname", "table_name"):
                if r.get(k) in mixed:
                    r[k] = mixed[r[k]]
            if "query" in r:
                r["query"] = quote(r["query"])

        res = self.variant(quote, row)

        def key(f):
            return (f["rule"], f["object"].replace('"', "").lower(), f["severity"],
                    (f.get("ddl") or "").replace('"', "").lower())
        base = analyze.run(inputs.load(FIXTURE))
        self.assertEqual(sorted(map(key, base["findings"])), sorted(map(key, res["findings"])))
        self.assert_runnable(base)
        self.assert_runnable(res, quoted=set(mixed))

    def test_colocated_database_gets_range_keys_and_no_split(self):
        def colocated_dump(text):  # what a colocated database dumps: range keys, no SPLIT
            text = re.sub(r"\(([^()]+)\)\s+HASH\b", lambda m: ", ".join(
                c.strip() + " ASC" for c in m.group(1).split(",")), text)
            return re.sub(r"\s+SPLIT INTO \d+ TABLETS", "", re.sub(r"\bHASH\b", "ASC", text))

        def row(name, r):
            if r.get("key") == "colocated":
                r["value"] = "true"

        res = self.variant(colocated_dump, row)
        # The expression-index template (CAP003) is among those exercised.
        self.assertIn("CAP003", {f["rule"] for f in res["findings"] if f.get("ddl")})
        self.assert_runnable(res, colocated=True)


class Versions(Case):
    """Release handling against a synthetic table: the real one is a local cache, never
    committed, so tests must not depend on it."""

    G = {"yb_enable_cbo": "legacy_mode", "yb_max_merge_scan_streams": None,
         "yb_use_hash_splitting_by_default": "on"}
    TABLE = {"tags": {
        "9.1.0.0": {"gucs": dict(G), "anchors": {"CAP001": False}, "profiles": {}},
        "9.1.2.0": {"gucs": dict(G, yb_max_merge_scan_streams="64"),
                    "anchors": {"CAP001": True},
                    "profiles": {"yugabyted": {"yb_enable_cbo": "on"}}},
        "9.2.0.0": {"gucs": dict(G, yb_max_merge_scan_streams="64"),
                    "anchors": {"CAP001": True},
                    "profiles": {"yugabyted --enhance_pg_compatibility":
                                 {"yb_use_hash_splitting_by_default": "off"}}}}}

    def setUp(self):
        self.saved = versions._DATA
        versions._DATA = json.loads(json.dumps(self.TABLE))

    def tearDown(self):
        versions._DATA = self.saved

    def test_release_resolution_and_drift(self):
        v = versions
        self.assertEqual(v.resolve("9.1.2.0"), ("9.1.2.0", None))
        tag, note = v.resolve("9.1.3.1")
        self.assertEqual(tag, "9.1.2.0")
        self.assertIn("nearest earlier release 9.1.2.0", note)
        # Another line's earlier release stands in; a newer release never does.
        self.assertEqual(v.resolve("9.3.0.0")[0], "9.2.0.0")
        self.assertEqual(v.resolve("9.1.1.5")[0], "9.1.0.0")
        tag, note = v.resolve("9.0.5.0")
        self.assertIsNone(tag)
        self.assertIn("older than every release", note)
        self.assertFalse(v.available("9.1.0.0", "yb_max_merge_scan_streams"))
        self.assertTrue(v.available("9.1.2.0", "yb_max_merge_scan_streams"))
        self.assertIs(v.pinned("9.1.0.0", "CAP001"), False)
        d = v.drift("9.1.0.0", "9.1.2.0", rules=["CAP001"])
        self.assertIn("yb_max_merge_scan_streams", d["settings"])
        self.assertEqual(d["rules"], ["CAP001"])

    def test_settings_conditional_when_deployment_tools_disagree(self):
        for tag, name, values in (("9.1.2.0", "yb_enable_cbo", ["legacy_mode", "on"]),
                                  ("9.2.0.0", "yb_use_hash_splitting_by_default", ["off", "on"])):
            with self.subTest(name):
                val, _, per = versions.setting(tag, name)
                self.assertIsNone(val)
                self.assertEqual(sorted(set(per.values())), values)

    def test_unverified_tool_profile_makes_a_setting_conditional(self):
        versions._DATA["tags"]["9.1.2.0"]["profiles"]["YBA new universe"] = versions.UNVERIFIED
        val, src, per = versions.setting("9.1.2.0", "yb_use_hash_splitting_by_default")
        self.assertIsNone(val)
        self.assertIn("not verified", src)

    def test_extractor_merges_source_paths_and_finds_profiles(self):
        evd = load_script("extract-version-data.py")
        self.assertEqual(evd.merge_paths({"a": ["x"], "b": ["y"]}, {"a": {"z"}}),
                         {"a": ["x", "z"], "b": ["y"]})
        src = SimpleNamespace(complete=False, show=lambda tag, path: "",
                              grep=lambda tag, regex, pathspec, context=0: [])
        self.assertEqual(evd.profiles(src, "v9.1.0.0", {}), {"YBA new universe": "unverified"})
        src.complete = True  # a git run searches everything: no hit means no override
        self.assertEqual(evd.profiles(src, "v9.1.0.0", {}), {})
        default, opt_in = evd._yugabyted_profiles(
            "conf = ['yb_enable_cbo=on']\n"
            "# Enhanced PG compatibility (enhance_pg_compatibility)\n"
            "PG_PARITY_FLAGS = 'yb_use_hash_splitting_by_default=false'\n")
        self.assertEqual(default, {"yb_enable_cbo": "on"})
        self.assertEqual(opt_in, {"yb_use_hash_splitting_by_default": "off"})
        # A flag whose macro type carries digits (int32, int64) is found.
        hit = [("src/yb/x.cc", "DEFINE_RUNTIME_int32(ysql_sequence_cache_minval, 100,\n"
                                "    \"Minimum cache size\");")]
        src = SimpleNamespace(grep=lambda tag, regex, pathspec, context=0:
                              hit if re.search(regex, hit[0][1]) else [])
        self.assertEqual(evd.gflag_defaults(src, "v9", {})["ysql_sequence_cache_minval"], "100")

    def test_review_without_a_release_cache_says_how_to_build_it(self):
        versions._DATA = {"tags": {}}
        items = self.review(ddl="CREATE TABLE t (id int PRIMARY KEY);\n", files={
            "ybm_meta.csv": "key,value\nversion,PostgreSQL 15-YB-9.3.0.0-b1\n"})["open_items"]
        miss = [o for o in items if o.startswith("RELEASE-DATA-MISSING")]
        self.assertEqual(len(miss), 1)
        self.assertIn("update-versions --repo", miss[0])
        self.assertIn("ask the user first", miss[0])


class RuleText(unittest.TestCase):
    def test_rules_name_no_release_and_no_rejected_ddl(self):
        with open(RULES, encoding="utf-8") as fh:
            txt = fh.read()
        self.assertNotRegex(txt, r"\b20\d\d\.\d+\.\d+")  # release facts live in the cache
        self.assertNotIn('"since"', txt)
        self.assertNotIn("DROP INDEX CONCURRENTLY", txt)  # YSQL rejects it


@unittest.skipUnless(os.path.isdir(FIXTURE), "fixture bundle not present")
class Fixture(unittest.TestCase):
    def run_cli(self):
        r = cli("analyze", FIXTURE)
        self.assertIn(r.returncode, (0, 1), r.stderr)  # 1 = high findings, not a failure
        return r.stdout

    def test_deterministic(self):
        a, b = self.run_cli(), self.run_cli()
        self.assertEqual(hashlib.sha256(a.encode()).hexdigest(),
                         hashlib.sha256(b.encode()).hexdigest())


class ZeroRowLookups(Case):
    def test_a_lookup_that_never_finds_anything_belongs_to_its_table(self):
        ddl = ("CREATE TABLE a (id bigint NOT NULL, b_id bigint, PRIMARY KEY ((id) HASH));\n"
               "CREATE TABLE b (id bigint NOT NULL, PRIMARY KEY ((id) HASH));\n")
        pss = PSS_HEAD + ("1,500000,900,0.0018,0,SELECT id FROM a WHERE id = $1\n"
                          "2,500000,900,0.0018,0,SELECT a.id FROM a JOIN b ON b.id = a.b_id "
                          "WHERE a.id = $1\n")
        res = self.review(ddl=ddl, pss=pss)
        got = {f["patterns"][0]: f["table"] for f in res["findings"] if f["rule"] == "WRK005"}
        self.assertEqual(got, {"P1": "a", "P2": None}, got)


def facts_of(sch, table, typoids=None):
    """A facts entry (measure.FACTS) for a parsed table, as the scratch server would give it."""
    t = sch.tables[table]
    return {"schema": "public", "name": table, "columns": [
        {"name": c, "typoid": (typoids or {}).get(c, 25), "typmod": -1,
         "type": t.cols[c]["type"], "notnull": t.cols[c].get("notnull", False),
         "identity": False, "generated": False} for c in t.col_order]}


class FakeServer:
    """A container whose value constructor is played by a function: comp -> values."""

    def __init__(self, make):
        self.make, self.sent = make, []

    def sql(self, text, db="ybm", flags=()):
        self.sent.append(text)
        if "json_object_agg(comp, json_build_array(vals, reps))" in text:
            comps = {}
            for line in self.sent[-2].splitlines():
                m = re.match(r"SELECT ybm_domain\((\d+), ARRAY\[(\d+)[^]]*\]::oid\[\], "
                             r"ARRAY\[[^]]*\]::int\[\], (\d+), (.*)\);", line)
                if m:
                    lits = [x.replace("''", "'") for x in
                            re.findall(r"'((?:[^']|'')*)'", m.group(4)) if x != "{}"]
                    vals = self.make(int(m.group(2)), int(m.group(3)), lits)
                    comps[m.group(1)] = [vals, [v if v in vals else None for v in lits]]
            return subprocess.CompletedProcess([], 0, json.dumps(comps), "")
        return subprocess.CompletedProcess([], 0, "", "")


def text_values(typoid, size, lits):
    """The value constructor for the tests: literals first, then v0, v1, ...; a boolean (oid 16)
    has two values."""
    if typoid == 16:
        return ["true", "false"][:size]
    out = list(lits)
    i = 0
    while len(out) < size:
        if "v%d" % i not in out:
            out.append("v%d" % i)
        i += 1
    return out[:size]


def sample(test, ddl, table, literals=None, budget=4000, typoids=None):
    b = test.bundle(ddl=ddl)
    sch = analyze.build_schema(b)
    plan, why = generate.plan_table(sch, b, table, facts_of(sch, table, typoids), literals or {},
                                    budget)
    if plan is None:
        return None, why
    failed = generate.materialize(FakeServer(text_values), "db", {table: plan})
    if failed:
        return None, failed[table]
    return generate.finalize(plan), None


class MeasuredPass(Case):
    """The measured pass: samples shaped by the statistics, parameters bound to sample values,
    anomalies judged against what a read asks for, and MEAS001 only when no finding explains
    one (the container part is validated live; see evals/yb-model/README.md)."""
    DDL = ("CREATE TABLE c (t text NOT NULL, d text NOT NULL, g text NOT NULL, m text NOT NULL, "
           "l text NOT NULL, b text NOT NULL, v float, PRIMARY KEY ((t, d) HASH, g, m, l, b));\n")

    def test_literals_and_parameters_are_tied_to_their_columns(self):
        q = ("SELECT v FROM c WHERE t = $1::text AND d = $2 AND g = 'g1' AND b IN ('x', $3) "
             "AND $4 < m AND l = ANY($5) LIMIT $6")
        self.assertEqual(measure.literals(q), {"g": ["'g1'"], "b": ["'x'"]})
        self.assertEqual(measure.params(q), {1: ("t", "="), 2: ("d", "="), 3: ("b", "in:1"),
                                             4: ("m", ">"), 5: ("l", "any"),
                                             6: ("limit", None)})

    def test_the_sample_fits_the_budget_and_holds_every_bound_literal(self):
        plan, why = sample(self, self.DDL, "c", {"g": ["g1"], "b": ["day:3"]}, budget=500)
        self.assertIsNone(why)
        self.assertEqual(plan["range_cols"], ["g", "m", "l", "b"])
        prod = 1
        for x in plan["range_sizes"]:
            prod *= x
        self.assertLessEqual(prod, 500)
        self.assertEqual((plan["per_prefix"], plan["rows"]), (prod, 2 * prod))
        self.assertEqual(plan["shape"]["g"]["domain"][0], "g1")
        self.assertEqual(plan["shape"]["b"]["domain"][0], "day:3")
        sql = "\n".join(generate.load_sql(plan, []))
        self.assertIn("OVERRIDING SYSTEM VALUE", sql)
        self.assertIn("(SELECT vals FROM ybm_dom WHERE comp = 0) d0", sql)  # server values
        self.assertEqual(measure.bind("SELECT v FROM c WHERE m = ANY($1)", {"c": plan}, ["c"]),
                         {1: "'{\"%s\"}'" % plan["shape"]["m"]["domain"][0]})
        vals = measure.bind("SELECT v FROM c WHERE t = $1 AND d = $2 AND b = $3",
                            {"c": plan}, ["c"])
        self.assertEqual(vals, {1: "'%s'" % plan["shape"]["t"]["domain"][0],
                                2: "'%s'" % plan["shape"]["d"]["domain"][0], 3: "'day:3'"})

    def test_a_type_s_own_value_count_sizes_the_sample(self):
        plan, why = sample(self, "CREATE TABLE f (id int NOT NULL, on_ boolean NOT NULL, flag "
                                 "boolean, PRIMARY KEY ((id) HASH, on_ ASC));\n", "f",
                           typoids={"on_": 16, "flag": 16})
        self.assertIsNone(why)
        self.assertEqual(plan["shape"]["flag"]["domain"], ["true", "false"])
        self.assertEqual((plan["range_sizes"], plan["per_prefix"]), ([2], 2))  # planned 8

    def test_tables_the_generator_cannot_fill_are_named_not_guessed(self):
        ddl = ("CREATE TABLE p (id int NOT NULL, ts date NOT NULL, PRIMARY KEY (id, ts)) "
               "PARTITION BY RANGE (ts);\n"
               "CREATE TABLE f (a int, b text);\n"
               "CREATE TABLE w (id int NOT NULL, x text NOT NULL, PRIMARY KEY ((id) HASH));\n")
        self.assertIsNone(sample(self, ddl, "p")[1])  # its partitions' bounds filter the load
        plan, why = sample(self, ddl, "f", budget=300)  # no key: one flat run of the budget
        self.assertEqual((why, plan["rows"], plan["per_prefix"]), (None, 300, 300))
        b = self.bundle(ddl=ddl)
        sch = analyze.build_schema(b)
        plan, _ = generate.plan_table(sch, b, "w", facts_of(sch, "w"), {})
        failed = generate.materialize(FakeServer(lambda t, n, l: []), "db", {"w": plan})
        self.assertIn("could not make a value of type", failed["w"])

    def test_seeks_are_judged_against_what_the_read_asks_for(self):
        self.assertEqual(measure.asked("SELECT 1 FROM c WHERE m IN ($1, $2, $3)"), 4)
        self.assertEqual(measure.asked("SELECT 1 FROM c WHERE m = 'a' OR m = 'b'"), 2)
        gap = {"returned": 240, "seeks": 241, "scanned": 240, "asked": 1}
        inlist = {"returned": 100, "seeks": 101, "scanned": 100, "asked": 101}
        wide = {"returned": 2, "seeks": 2, "scanned": 4000, "asked": 1}
        self.assertEqual([measure.anomalies(x) for x in (gap, inlist, wide)],
                         [["seeks"], [], ["scanned"]])

    def test_an_anomaly_no_finding_explains_is_meas001(self):
        q = "SELECT v FROM c WHERE t = $1 AND d = $2 AND g = $3 AND b = $4;\n"
        b = self.bundle(ddl=self.DDL, queries=q)
        m = {"id": "P1", "table": "c", "weight": "UNRANKED", "returned": 240, "scanned": 240,
             "seeks": 241, "asked": 1, "index": "c_pkey", "anomalies": ["seeks"],
             "scanned_per_row": 1.0, "seeks_per_row": 1.0}
        plans = {"measured": {"budget": 4000, "patterns": [dict(m)], "tables": {}}}
        res = analyze.run(b, plans=plans)
        self.assertEqual(res["measured"]["patterns"][0]["explained_by"], ["CAP014"])
        self.assertNotIn("MEAS001", self.rules(res))
        with mock.patch.object(analyze, "_key_gap", lambda *a, **k: None):
            res = analyze.run(self.bundle(ddl=self.DDL, queries=q),
                              plans={"measured": {"budget": 4000, "patterns": [dict(m)],
                                                  "tables": {}}})
        f = self.only(res, "MEAS001")
        self.assertEqual((f["severity"], f["object"]), ("medium", "P1 on c"))
        res["measured"]["tables"] = {"c": {"measured": True, "per_prefix": 5632}}
        line = next(x for x in report.render(res).splitlines()
                    if x.startswith("Measured pass"))
        self.assertIn("(5632 rows per hash prefix;", line)
        self.assertIn("1 unexplained (MEAS001: P1)", line)
        self.assertIn("241 seeks where the read asks for about 1", f["fact"])
        self.assertEqual(prescriptions(res), [])


class ServerOracle(Case):
    """The replayed plan as oracle: its deparsed conditions are read with server.py's own
    reader, its binding of key columns is compared with the model's, it settles CAP014, and the
    measured pass binds parameters by it. Plans are recorded JSON (no container)."""
    DDL = ("CREATE TABLE c (t text NOT NULL, d text NOT NULL, g text NOT NULL, m text NOT NULL, "
           "l text NOT NULL, b text NOT NULL, v float, PRIMARY KEY ((t, d) HASH, g, m, l, b));\n")

    def test_the_reader_reads_the_server_s_conditions(self):
        a = [x for x, _ in server.atoms(server.read(
            "((c.t = $1) AND ((c.d)::text = $2) AND (c.m = ANY (ARRAY[$3, $4])) AND "
            "($5 < c.b) AND (c.l IS NULL) AND (\"Odd Col\" = 'it''s'::text))"))]
        self.assertEqual([(x["col"], x["op"]) for x in a],
                         [("t", "eq"), ("d", "eq"), ("m", "in"), ("b", "range"),
                          ("l", "isnull"), ("Odd Col", "eq")])
        self.assertEqual(a[3]["sym"], ">")  # $5 < b reads as b > $5
        self.assertEqual(a[2]["value"], [3, 4])
        t = server.read("((c.m = 'x'::text) AND (c.l = ''::text)) OR ((c.m = 'y'::text) AND "
                        "(c.l <> ''::text))")
        self.assertEqual(t[0], "or")
        self.assertEqual(server.read("((e.value ->> 'op'::text) = $2)"),
                         {"residue": "(e.value ->> 'op'::text) = $2"})  # kept, never guessed

    def plan(self, index_cond, storage_filter=None, bitmap_arms=None):
        if bitmap_arms:
            node = {"Node Type": "YB Bitmap Table Scan", "Relation Name": "c", "Alias": "c",
                    "Plans": [{"Node Type": "BitmapOr", "Plans": [
                        {"Node Type": "Bitmap Index Scan", "Index Name": "c_pkey",
                         "Index Cond": a} for a in bitmap_arms]}]}
        else:
            node = {"Node Type": "Index Scan", "Relation Name": "c", "Alias": "c",
                    "Index Name": "c_pkey", "Index Cond": index_cond}
            if storage_filter:
                node["Storage Filter"] = storage_filter
        return [{"Plan": node}]

    def test_the_plan_s_binding_map(self):
        sch = schema.parse(self.DDL)
        pk = sch.tables["c"].pk
        HB = "(c.t = $1) AND (c.d = $2) AND (c.g = $3)"
        def m(plan):
            return " ".join(x["code"] for x in server.server_map(server.scans(plan)[0], pk))
        self.assertEqual(m(self.plan("(%s AND (c.b = $4))" % HB)),
                         "eq eq eq none none eq")
        self.assertEqual(m(self.plan("(%s AND (c.l = ''::text) AND (c.b = $4))" % HB,
                                     "((c.m = 'x'::text) OR (c.m = 'y'::text))")),
                         "eq eq eq filter eq eq")
        arms = ["(%s AND (c.m = '%s'::text) AND (c.l = ''::text) AND (c.b = $4))" % (HB, v)
                for v in ("x", "y")]
        self.assertEqual(m(self.plan(None, bitmap_arms=arms)), "eq eq eq or_eq eq eq")
        model = [{"col": "m", "code": "eq"}, {"col": "l", "code": "or_eq"}]
        plan = [{"col": "m", "code": "filter"}, {"col": "l", "code": "filter"}]
        self.assertEqual(server.compare_maps(model, plan),
                         [{"col": "m", "model": "eq", "server": "filter"}])
        self.assertEqual(server.compare_maps([{"col": "l", "code": "or_eq"}],
                                             [{"col": "l", "code": "or_eq"}]), [])
        t = next(iter(schema.parse('CREATE TABLE "T" ("createdAt" int NOT NULL, id int NOT '
                                   'NULL, PRIMARY KEY ((id) HASH, "createdAt" ASC));')
                      .tables.values()))
        def scan(cond):
            return server.scans([{"Plan": {"Node Type": "Index Scan", "Relation Name": "T",
                                           "Alias": "t", "Index Name": "T_pkey",
                                           "Index Cond": cond}}])[0]
        self.assertEqual([x["code"] for x in server.server_map(
            scan('((t.id = $1) AND (t."createdAt" = $2))'), t.pk)], ["eq", "eq"])  # case kept
        self.assertEqual([x["code"] for x in server.server_map(
            scan('((t.id = o.id) AND (t."createdAt" = $2))'), t.pk)], ["eq", "eq"])  # a join

    def replayed(self, plan, assumed=False):
        return {"version": "9.9.9.9", "mode": "customer",
                "assumed_settings": {"yb_enable_cbo": "x"} if assumed else {},
                "version_match": {"customer": "9.9.9.9", "exact": True, "drift_known": True,
                                  "drift": {"rules": []}},
                "patterns": [{"id": "P1", "query": "", "plan": plan}]}

    def test_the_plan_settles_cap014(self):
        HB = "(c.t = $1) AND (c.d = $2) AND (c.g = $3)"
        or_q = ("SELECT v FROM c WHERE t = $1 AND d = $2 AND g = $3 AND b = $4 AND "
                "(m = 'x' AND l = '' OR m = 'y' AND l = '');\n")
        filtered = self.plan("(%s AND (c.b = $4))" % HB,
                             "(((c.m = 'x'::text) AND (c.l = ''::text)) OR ((c.m = 'y'::text) "
                             "AND (c.l = ''::text)))")
        res = self.review(ddl=self.DDL, queries=or_q)
        self.assertNotIn("CAP014", self.rules(res))  # the model: bound arm by arm
        res = self.review(ddl=self.DDL, queries=or_q, plans=self.replayed(filtered))
        f = self.only(res, "CAP014")  # the plan: the OR is a filter, so b follows a gap
        self.assertEqual(f["replay"], "P1: Index Scan using c_pkey (binds b after an unbound "
                                      "m, l)")
        self.assertEqual(f["confidence"], "confirmed (replay)")
        self.assertEqual(res["gaps"], [])  # or= was the model's open question, not an error
        box = "SELECT v FROM c WHERE t = $1 AND d = $2 AND g = $3 AND b = $4;\n"
        seek = self.plan("(%s AND (c.m = $5) AND (c.l = $6) AND (c.b = $4))" % HB)
        res = self.review(ddl=self.DDL, queries=box, plans=self.replayed(seek))
        self.assertNotIn("CAP014", self.rules(res))  # the plan binds every column: no gap
        self.assertEqual(res["gaps"][0]["layer"], "bind.pred")  # the model read it otherwise
        res = self.review(ddl=self.DDL, queries=box, plans=self.replayed(seek, assumed=True))
        self.assertNotIn("CAP014", self.rules(res))  # the plan stands, settings assumed

    def test_a_plan_from_another_release_neither_adds_nor_withdraws(self):
        HB = "(c.t = $1) AND (c.d = $2) AND (c.g = $3)"
        box = "SELECT v FROM c WHERE t = $1 AND d = $2 AND g = $3 AND b = $4;\n"
        seek = self.plan("(%s AND (c.m = $5) AND (c.l = $6) AND (c.b = $4))" % HB)
        other = self.replayed(seek)
        other["version_match"] = {"customer": "9.9.9.9", "exact": False, "drift_known": False,
                                  "image_release": "9.9.9.1"}
        res = self.review(ddl=self.DDL, queries=box, plans=other)
        f = self.only(res, "CAP014")  # kept: another release's plan settles nothing
        self.assertIn("differences between the two releases are unknown", f["replay"])
        self.assertNotIn("refuted by replay", [r["outcome"] for r in res["reconciliation"]])
        other["version_match"] = {"customer": "9.9.9.9", "exact": False, "drift_known": True,
                                  "image_release": "9.9.9.1", "drift": {"rules": []}}
        gap = self.plan("(%s AND (c.b = $4))" % HB)
        res = self.review(ddl=self.DDL, queries=box, plans=dict(other, patterns=[
            {"id": "P1", "query": "", "plan": gap}]))
        self.assertIn("nearest earlier release", self.only(res, "CAP014")["confidence"])

    def test_a_probe_s_verdict_covers_what_the_plan_adds(self):
        HB = "(c.t = $1) AND (c.d = $2) AND (c.g = $3)"
        or_q = ("SELECT v FROM c WHERE t = $1 AND d = $2 AND g = $3 AND b = $4 AND "
                "(m = 'x' AND l = '' OR m = 'y' AND l = '');\n")
        filtered = self.plan("(%s AND (c.b = $4))" % HB,
                             "(((c.m = 'x'::text) AND (c.l = ''::text)) OR ((c.m = 'y'::text) "
                             "AND (c.l = ''::text)))")
        plans = self.replayed(filtered)
        plans["probes"] = {"CAP014": {"verdict": "refuted", "detail": "claim.seeks >= 50 * "
                                      "control.seeks", "claim": {}, "control": {}}}
        res = self.review(ddl=self.DDL, queries=or_q, plans=plans)
        self.assertNotIn("CAP014", self.rules(res))  # the plan's gap, refuted by the probe
        self.assertTrue(any("probe" in r["outcome"] for r in res["reconciliation"]))

    def test_the_finding_follows_the_index_the_plan_used(self):
        ddl = self.DDL + "CREATE INDEX c_alt ON c ((t, d) HASH, l, b);\n"
        box = "SELECT v FROM c WHERE t = $1 AND d = $2 AND g = $3 AND b = $4;\n"
        res = self.review(ddl=ddl, queries=box)
        static = [f["object"] for f in res["findings"] if f["rule"] == "CAP014"]
        self.assertIn("c_pkey on c", static)
        alt = [{"Plan": {"Node Type": "Index Scan", "Relation Name": "c", "Alias": "c",
                         "Index Name": "c_alt", "Index Cond": "((c.t = $1) AND (c.d = $2) AND "
                                                              "(c.b = $4))",
                         "Filter": "(c.g = $3)"}}]
        res = self.review(ddl=ddl, queries=box, plans=self.replayed(alt))
        by = {f["object"]: f for f in res["findings"] if f["rule"] == "CAP014"}
        self.assertNotIn("c_pkey on c", by)  # one access: the plan's, on c_alt
        self.assertIn("confirmed", by["c_alt on c"]["confidence"])
        self.assertIn({"layer": "path", "pattern": "P1", "table": "c", "model": "c_pkey",
                       "server": "c_alt", "taken": "plan"}, res["gaps"])

    def test_a_withdrawn_pattern_leaves_the_finding_s_text(self):
        HB = "(c.t = $1) AND (c.d = $2) AND (c.g = $3)"
        q = ("SELECT v FROM c WHERE t = $1 AND d = $2 AND g = $3 AND b = $4;\n"
             "SELECT v FROM c WHERE t = $1 AND d = $2 AND g = $3 AND l = $4;\n")
        res = self.review(ddl=self.DDL, queries=q)
        f = self.only(res, "CAP014")
        self.assertEqual(sorted(f["patterns"]), ["P1", "P2"])
        self.assertTrue(f["fact"].startswith("P1 "))
        seek = self.plan("(%s AND (c.m = $5) AND (c.l = $6) AND (c.b = $4))" % HB)
        res = self.review(ddl=self.DDL, queries=q, plans=self.replayed(seek))
        f = self.only(res, "CAP014")
        self.assertEqual(f["patterns"], ["P2"])
        self.assertTrue(f["fact"].startswith("P2 "), f["fact"])
        self.assertNotIn("P1", (f.get("replay") or "") + f["fix"])  # rebuilt, not patched

    def test_a_plan_shown_gap_joins_the_model_s_finding_as_confirmed(self):
        HB = "(c.t = $1) AND (c.d = $2) AND (c.g = $3)"
        q = ("SELECT v FROM c WHERE t = $1 AND d = $2 AND g = $3 AND b = $4 AND "
             "(m = 'x' AND l = '' OR m = 'y' AND l = '');\n"
             "SELECT v FROM c WHERE t = $1 AND d = $2 AND g = $3 AND b = $4;\n")
        filtered = self.plan("(%s AND (c.b = $4))" % HB,
                             "(((c.m = 'x'::text) AND (c.l = ''::text)) OR ((c.m = 'y'::text) "
                             "AND (c.l = ''::text)))")
        res = self.review(ddl=self.DDL, queries=q)
        self.assertEqual(self.only(res, "CAP014")["patterns"], ["P2"])  # the model: P2 only
        res = self.review(ddl=self.DDL, queries=q, plans=self.replayed(filtered))
        f = self.only(res, "CAP014")
        self.assertEqual(f["patterns"], ["P1", "P2"])
        self.assertEqual(f["confidence"], "probable")  # P2 did not plan: not all replayed
        self.assertEqual(f["replay"], "P1: Index Scan using c_pkey (binds b after an unbound "
                                      "m, l)")  # what the plan that ran showed

    def test_the_measured_pass_binds_by_the_plan(self):
        q = "SELECT v FROM c WHERE (t, d) = ($1, $2) AND m = ANY($3)"
        plan, _ = sample(self, self.DDL, "c", {"g": ["g1"]})
        self.assertEqual(plan["shape"]["g"]["domain"][0], "g1")
        srv = {1: ("t", "="), 2: ("d", "="), 3: ("m", "in:0")}
        vals = measure.bind(q, {"c": plan}, ["c"], srv, ["text", "text", "text[]"])
        self.assertEqual(vals[1], "'%s'" % plan["shape"]["t"]["domain"][0])
        self.assertEqual(vals[3], "'{\"%s\"}'" % plan["shape"]["m"]["domain"][0])
        self.assertEqual(measure.compare_params(q, srv)[0]["param"], 1)  # the text missed it
        plan["shape"]["m"].update(type="text[]", domain=['{"v0"}'])  # an array column itself
        vals = measure.bind("SELECT v FROM c WHERE m = $1", {"c": plan}, ["c"],
                            {1: ("m", "=")}, ["text[]"])
        self.assertEqual(vals[1], "'{\"v0\"}'")  # its own value, not an array of it

    def test_statements_the_server_cannot_prepare_as_written(self):
        q, notes = server.statement('COPY (SELECT a FROM shop.public.t WHERE b = $1) TO STDOUT')
        self.assertEqual(q, "SELECT a FROM public.t WHERE b = $1")
        self.assertEqual(notes, ["COPY (query) TO: the query",
                                 "database.schema.table: schema.table"])
        q, notes = server.statement("SELECT 'x.y.z' FROM s.t WHERE a.b = 1")
        self.assertEqual((q, notes), ("SELECT 'x.y.z' FROM s.t WHERE a.b = 1", []))
        sql = replay.patterns_sql([{"id": "P1", "query": "COPY (SELECT 1 FROM t) TO STDOUT"}],
                                  "/tmp/x")
        self.assertIn("EXPLAIN (VERBOSE, FORMAT JSON) SELECT 1 FROM t;", sql)

    def test_parameter_tests_and_row_comparisons_bind_their_parameters(self):
        self.assertEqual(server.read("($2 IS NULL)"), {"param": 2, "op": "isnull"})
        plan = [{"Plan": {"Node Type": "Index Scan", "Relation Name": "c", "Alias": "c",
                          "Index Name": "c_pkey", "Index Cond": "(c.t = $1)",
                          "Storage Filter": "((ROW(c.m, c.l) < ROW($3, $4)) AND (($2 IS NULL) "
                                            "OR (c.b = $2)))"}}]
        self.assertEqual(server.params(plan)[0], {1: ("t", "="), 3: ("m", "<"),
                                                  4: ("l", "<"), 2: ("b", "=")})
        self.assertEqual(server.residue(plan), [])

    def test_parameters_no_condition_places_are_bound_by_their_type(self):
        self.assertEqual(server.read("(yb_hash_code(q.id) >= $8)"),
                         {"expr": "yb_hash_code(id)", "op": "range", "sym": ">=",
                          "kind": "param", "value": 8})
        plan, _ = sample(self, self.DDL, "c")
        q = ("SELECT CASE WHEN length(m) > $1 THEN $2 END FROM c WHERE t = $3 AND "
             "yb_hash_code(t) >= $4 AND yb_hash_code(t) < $5")
        srv = {3: ("t", "="), 4: ("expr:yb_hash_code(t)", ">="),
               5: ("expr:yb_hash_code(t)", "<")}
        vals = measure.bind(q, {"c": plan}, ["c"], srv,
                            ["integer", "text", "text", "integer", "integer"], {"text": "v0"})
        self.assertEqual((vals[1], vals[2], vals[4], vals[5]),
                         ("'0'", "'v0'", "'-2147483648'", "'2147483647'"))
        self.assertEqual(vals[3], "'%s'" % plan["shape"]["t"]["domain"][0])

    def test_replay_asks_the_server_for_parameter_types(self):
        sql = replay.patterns_sql([{"id": "P1", "query": "SELECT 1 FROM c WHERE t = $1"}],
                                  "/tmp/x")
        self.assertIn("SELECT parameter_types FROM pg_prepared_statements WHERE name = "
                      "'ybm_p1'", sql)


class GeneratorFacts(unittest.TestCase):
    """Step 1 of the generator: the scratch server's own constraint expressions give each
    column the constants it is compared with (recorded server answers, no container)."""
    FACTS = {"tables": [
        {"schema": "public", "name": "child", "parent": None, "partition_constraint": None,
         "checks": ["(status = ANY (ARRAY['active'::text, 'done'::text]))"]},
        {"schema": "public", "name": "ev", "parent": None, "partition_constraint": None,
         "checks": None},
        {"schema": "public", "name": "ev_q1", "parent": "ev", "checks": None,
         "partition_constraint": "((ts IS NOT NULL) AND (ts >= '2026-01-01'::date) AND "
                                 "(ts < '2026-04-01'::date))"},
        {"schema": "public", "name": "ev_def", "parent": "ev", "checks": None,
         "partition_constraint": "(NOT ((ts IS NOT NULL) AND (ts >= '2026-01-01'::date)))"}]}

    def test_constants_from_checks_and_partition_bounds(self):
        self.assertEqual(measure.schema_constants(self.FACTS), {
            ("public", "child"): {"status": ["'active'", "'done'"]},
            ("public", "ev"): {"ts": ["'2026-01-01'"]}})  # credited to the root; upper bounds
        self.assertEqual(server.constants("(qty <= 10)"), {})  # an upper bound may be excluded


class EqualityGraph(Case):
    """B: columns that must share values are one component; a foreign key's child takes no more
    values than its parent; the measured tables bring their referenced tables, parents first."""
    FACTS = {"tables": [{"schema": "public", "name": n, "columns": [
        {"name": c} for c in cs]} for n, cs in (("a", ["id", "b_id"]), ("b", ["id", "c_id"]),
                                                 ("c", ["id"]), ("x", ["id", "a_id"]))],
             "fks": [{"schema": "public", "table": "a", "cols": ["b_id"], "ref_schema": "public",
                      "ref_table": "b", "ref_cols": ["id"]},
                     {"schema": "public", "table": "b", "cols": ["c_id"], "ref_schema": "public",
                      "ref_table": "c", "ref_cols": ["id"]}]}

    def test_referenced_tables_load_first(self):
        order, cyclic = generate.generation_set(self.FACTS, {("public", "a")})
        self.assertEqual(order, [("public", "c"), ("public", "b"), ("public", "a")])
        self.assertEqual(cyclic, [])
        facts = dict(self.FACTS, fks=self.FACTS["fks"] + [
            {"schema": "public", "table": "c", "cols": ["id"], "ref_schema": "public",
             "ref_table": "a", "ref_cols": ["id"]}])
        order, cyclic = generate.generation_set(facts, {("public", "a")})
        self.assertEqual(len(order), 3)
        self.assertEqual(len(cyclic), 1)  # one key dropped to break the cycle, recorded

    def test_foreign_keys_and_joins_share_a_component(self):
        tables = {("public", n) for n in ("a", "b", "c", "x")}
        comp, edges = generate.components(self.FACTS, tables, [(("a", "id"), ("x", "a_id"))])
        self.assertEqual(comp[(("public", "a"), "b_id")], comp[(("public", "b"), "id")])
        self.assertEqual(comp[(("public", "b"), "c_id")], comp[(("public", "c"), "id")])
        self.assertEqual(comp[(("public", "a"), "id")], comp[(("public", "x"), "a_id")])
        self.assertNotEqual(comp[(("public", "a"), "id")], comp[(("public", "b"), "id")])
        self.assertEqual(len(edges), 2)

    def test_constants_spread_through_a_component_and_a_child_stays_within_its_parent(self):
        def slot(comp, size, lits):
            return {"comp": comp, "size": size, "literals": lits}
        plans = {"a": {"shape": {"b_id": slot(1, 50, ["7"])}},
                 "b": {"shape": {"id": slot(1, 8, [])}}}
        keys = {"a": ("public", "a"), "b": ("public", "b")}
        generate.settle(plans, [((("public", "a"), "b_id"), (("public", "b"), "id"))], keys)
        self.assertEqual(plans["b"]["shape"]["id"]["literals"], ["7"])  # the parent has it too
        self.assertEqual(plans["a"]["shape"]["b_id"]["size"], 8)  # no more than the parent

    def test_every_unique_set_gets_an_injective_column_where_one_is_free(self):
        ddl = ("CREATE TABLE u (h int NOT NULL, r int NOT NULL, x text, y text, "
               "PRIMARY KEY ((h) HASH, r));\n")
        b = self.bundle(ddl=ddl)
        sch = analyze.build_schema(b)
        plan, _ = generate.plan_table(sch, b, "u", facts_of(sch, "u"), {},
                                      uniques=[["x", "y"], ["r"]])
        self.assertEqual(plan["shape"]["x"]["role"], "n")
        self.assertEqual(plan["shape"]["y"]["role"], "m")
        self.assertEqual(plan["unique_unmet"], [["r"]])  # a key column alone: recorded


class ScriptedServer:
    """A container answering by substring: [(needle, stdout, stderr)], first match wins."""

    def __init__(self, answers):
        self.answers, self.sent = answers, []

    def sql(self, text, db="ybm", flags=()):
        self.sent.append(text)
        for needle, out, err in self.answers:
            if needle in text:
                return subprocess.CompletedProcess([], 0, out, err)
        return subprocess.CompletedProcess([], 0, "", "")


class FilteredLoad(Case):
    """C: the table keeps its constraints and its own predicates decide what loads: one-column
    predicates narrow a column's values, the rest filter candidate rows; every loss and every
    constraint the load gives up is recorded."""
    P = ("public", "ev")
    FACTS = {"tables": [
        {"schema": "public", "name": "ev", "parent": None, "partkey": ["ts"],
         "checks": [{"name": "ev_v", "expr": "(v > 0)", "cols": ["v"]},
                    {"name": "ev_lohi", "expr": "(lo < hi)", "cols": ["lo", "hi"]}]},
        {"schema": "public", "name": "ev_q1", "parent": "ev", "is_default": False,
         "partition_constraint": "((ts >= '2026-01-01'::date) AND (ts < '2026-04-01'::date))"},
        {"schema": "public", "name": "ev_q2", "parent": "ev", "is_default": False,
         "partition_constraint": "((ts >= '2026-04-01'::date) AND (ts < '2026-07-01'::date))"},
        {"schema": "public", "name": "p", "parent": None}],
        "fks": [{"name": "ev_p", "schema": "public", "table": "ev", "cols": ["pid"],
                 "ref_schema": "public", "ref_table": "p", "ref_cols": ["id"]},
                {"name": "ev_self", "schema": "public", "table": "ev", "cols": ["up"],
                 "ref_schema": "public", "ref_table": "ev", "ref_cols": ["id"]}]}

    def test_the_table_s_own_predicates(self):
        preds = generate.predicates(self.FACTS, self.P, {("public", "p")})
        labels = [p[0] for p in preds]
        self.assertEqual(labels, ["check ev_v", "check ev_lohi", "partitions of ev",
                                  "foreign key ev_p"])  # a key to itself: checked by the server
        self.assertIn("2026-04-01", preds[2][2])
        self.assertIn(" OR ", preds[2][2])  # any partition's bound
        self.assertEqual(preds[3][2], "(pid IS NULL OR (pid) IN (SELECT id FROM p))")
        self.assertEqual([p[0] for p in generate.predicates(self.FACTS, self.P, set())][-1],
                         "partitions of ev")  # a parent that did not load: the server decides
        facts = json.loads(json.dumps(self.FACTS))
        facts["tables"][2]["is_default"] = True
        self.assertNotIn("partitions of ev",
                         [p[0] for p in generate.predicates(facts, self.P, set())])
        self.assertEqual(generate.predicates(self.FACTS, self.P, {("public", "p")},
                                             [self.FACTS["fks"][0]])[-1][0], "partitions of ev")

    def test_one_column_predicates_narrow_values(self):
        unary = generate.unary_predicates(self.FACTS, self.P)
        self.assertEqual(sorted(unary), ["ts", "v"])  # lo < hi reads two columns: the filter's
        self.assertEqual(unary["v"], [("ev_v", "(v > 0)")])
        self.assertIsNone(unary["ts"][0][0])  # a bound, never dropped
        shape = {c: {"type": t, "pool": pool, "size": 2, "comp": i, "notnull": True}
                 for i, (c, t, pool) in enumerate((("v", "int", ["0", "1", "2"]),
                                                    ("ts", "date", ["2027-01-01"])))}
        plans = {"ev": {"shape": shape}}
        srv = ScriptedServer([("v::int", '["1", "2"]', ""), ("v::date", "", "")])
        failed, gaps, drops = generate.narrow(srv, "db", plans, self.FACTS, {"ev": self.P})
        self.assertEqual(shape["v"]["domain"], ["1", "2"])
        self.assertEqual(shape["v"]["comp"], 2)  # its own list, written to the server
        self.assertTrue(any("INSERT INTO ybm_dom VALUES (2, ARRAY['1', '2']" in t
                            for t in srv.sent))
        self.assertIn("partition bounds", failed["ev"])  # no date in any partition
        shape["v"]["pool"], shape["ts"]["pool"] = ["0"], ["2026-01-02"]
        srv = ScriptedServer([("v::int", "", ""), ("v::date", '["2026-01-02"]', "")])
        failed, gaps, drops = generate.narrow(srv, "db", plans, self.FACTS, {"ev": self.P})
        self.assertEqual((failed, drops), ({}, [(self.P, "ev_v")]))  # unsatisfiable: dropped
        self.assertEqual(gaps[0]["layer"], "gen.check_dropped")
        self.assertEqual(shape["v"]["domain"], ["0"])

    def test_candidates_keep_keys_and_pair_other_columns(self):
        plan, _ = sample(self, "CREATE TABLE t (id int NOT NULL, a int NOT NULL, b int NOT NULL, "
                               "PRIMARY KEY ((id) HASH));\n", "t")
        drop, create, insert = generate.load_sql(plan, [("check c", "c", "(a < b)")])
        self.assertIn("generate_series(0, 63)", create)  # 2 rows: 64 candidates at least
        self.assertIn("CASE WHEN (g.n / 2) = 0 THEN 0 ELSE hashtext(", create)  # copy 0: row
        self.assertIn("':1')", create)
        self.assertIn("':2')", create)  # a and b shift by different offsets
        self.assertIn("WHERE ((a < b)) IS NOT FALSE", insert)  # NULL passes, as in a CHECK
        self.assertIn("DISTINCT ON (ybm_n % 2)", insert)
        self.assertIn("ON CONFLICT DO NOTHING", insert)
        self.assertIn("(WHERE ((a < b)) IS FALSE)", generate.rejects_sql(
            [("check c", "c", "(a < b)")]))

    def test_nulls_are_an_exact_fraction_on_different_rows_per_column(self):
        plan, _ = sample(self, "CREATE TABLE t (id int NOT NULL, a int, b int, "
                               "PRIMARY KEY ((id) HASH));\n", "t")
        for c in ("a", "b"):
            plan["shape"][c]["null_frac"] = 0.5
        create = generate.load_sql(plan, [])[1]
        n_rows = plan["rows"]
        nulls = {}
        rules = re.findall(r"\* 7919 \+ (\d+)\) % (\d+)\)", create)[::2]  # each twice
        self.assertEqual(len(rules), 2)  # a, b: (offset, rows)
        for c, (off, rows) in zip(("a", "b"), rules):  # each column's NULL rule, every row
            off, rows = int(off), int(rows)
            self.assertEqual(rows, n_rows)
            nulls[c] = {n for n in range(n_rows)
                        if ((n * 7919 + off) % n_rows + 1) // 2 > ((n * 7919 + off) % n_rows) // 2}
            self.assertEqual(len(nulls[c]), n_rows // 2)  # exactly the fraction
        self.assertNotEqual(nulls["a"], nulls["b"])

    def test_the_load_records_what_it_gives_up(self):
        plan, _ = sample(self, "CREATE TABLE ev (id int NOT NULL, pid int, v int, "
                               "PRIMARY KEY ((id) HASH));\n", "ev")
        facts = {"tables": [{"schema": "public", "name": "ev", "checks": [
            {"name": "ev_v", "expr": "(f(v) > 0)", "cols": ["v"]}]}], "fks": [
            {"name": "ev_p", "schema": "public", "table": "ev", "cols": ["pid"],
             "ref_schema": "public", "ref_table": "p", "ref_cols": ["id"]}]}
        srv = ScriptedServer([
            ("WHERE ((f(v) > 0)) IS FALSE", "", "ERROR:  function f(integer) does not exist"),
            ("INSERT INTO ev", "", 'ERROR:  insert or update on table "ev" violates foreign '
                                   'key constraint "ev_p"'),
            ("SELECT count(*) FROM ev", "1\n", ""),
            ("json_build_object", '{"foreign key ev_p": 3}', "")])
        gaps = []
        loaded, why = measure._load(srv, "db", facts, self.P, plan, {("public", "p")}, [], gaps)
        self.assertEqual((loaded, why), (0, "load failed: ERROR:  insert or update on table "
                                            '"ev" violates foreign key constraint "ev_p"'))
        self.assertEqual([g["layer"] for g in gaps][:2], ["gen.predicate",
                                                          "gen.constraint_dropped"])
        drops = [t for t in srv.sent if "DROP CONSTRAINT" in t]
        self.assertIn("CONSTRAINT IF EXISTS ev_v;", drops[0])  # not evaluable: dropped
        self.assertIn("CONSTRAINT IF EXISTS ev_p;", drops[1])  # named by the server: dropped
        inserts = [t for t in srv.sent if t.startswith("INSERT INTO ev")]
        self.assertIn("SELECT id FROM p", inserts[0])
        self.assertNotIn("SELECT id FROM p", inserts[1])  # and no longer filters the retry
        self.assertNotIn("TRIGGER ALL", "".join(srv.sent))  # system triggers (keys) stay on
        srv.answers[1:2] = []
        gaps = []
        loaded, why = measure._load(srv, "db", facts, self.P, plan, {("public", "p")}, [], gaps)
        self.assertEqual((loaded, why), (1, None))
        self.assertEqual(gaps[-1]["layer"], "gen.filtered")
        self.assertEqual(gaps[-1]["rejects"], {"foreign key ev_p": 3})


class RoundTrip(Case):
    """D: the server checks the loaded sample against the plan it came from, and the measured
    plan against the replayed one."""
    DDL = ("CREATE TABLE c (h text NOT NULL, r text NOT NULL, x text, "
           "PRIMARY KEY ((h) HASH, r ASC));\n")

    def test_the_sample_s_shape_is_counted_on_the_server(self):
        plan, _ = sample(self, self.DDL, "c", budget=50)
        exp = generate.expected_shape(plan)
        self.assertEqual(exp["h"][0], 2)
        self.assertEqual(exp["r"][0], plan["range_sizes"][0])
        plan["shape"]["x"]["null_frac"] = 1.0
        self.assertEqual(generate.expected_shape(plan)["x"][0], 0)  # all NULL: no value
        plan["shape"]["x"]["null_frac"] = 0.0
        rows = plan["rows"]
        counts = json.dumps({"rows": rows, "h": [2, 0], "r": [1, 0],
                             "x": [exp["x"][0], rows // 2]})
        srv = ScriptedServer([("count(DISTINCT", counts, "")])
        gaps = generate.verify_shape(srv, "db", plan, ("public", "c"))
        self.assertNotIn("ANALYZE", "".join(srv.sent))  # statistics would change the plans
        self.assertIn('count(DISTINCT x::text)', srv.sent[0])
        self.assertEqual([(g["model"], g.get("distinct"), g.get("null_frac")) for g in gaps],
                         [("r", 1, None), ("x", None, 0.5)])  # counts kept as numbers
        gaps = generate.verify_shape(ScriptedServer([]), "db", plan, ("public", "c"))
        self.assertEqual(gaps[0]["model"], "column counts")  # never silently fine

    def test_a_range_bounded_on_both_sides_finds_rows(self):
        plan, _ = sample(self, self.DDL, "c", budget=50)
        srv = ScriptedServer([("ORDER BY v::text", json.dumps({"r": sorted(
            plan["shape"]["r"]["domain"])}), "")])
        generate.sort_domains(srv, "db", {"c": plan}, {("c", "r"), ("c", "nope")})
        srt = plan["shape"]["r"]["sorted"]
        vals = measure.bind("SELECT x FROM c WHERE r >= $1 AND r < $2", {"c": plan}, ["c"])
        self.assertLess(srt.index(vals[1][1:-1]), srt.index(vals[2][1:-1]))

    def test_a_scan_the_replay_never_chose_is_a_flip(self):
        def plan(*scans):
            return [{"Plan": {"Node Type": "Append", "Plans": [
                {"Node Type": n, "Relation Name": "t", "Index Name": i} for n, i in scans]}}]
        replayed = plan(("Index Scan", "t_a_pkey"), ("Index Scan", "t_b_pkey"))
        self.assertEqual(generate.plan_flip(replayed, plan(("Index Scan", "t_a_pkey"))), [])
        self.assertEqual(generate.plan_flip(replayed, plan(("Seq Scan", None))),
                         [("Seq Scan", None)])

    def test_a_flipped_plan_raises_nothing(self):
        ddl = "CREATE TABLE t (id int NOT NULL, x int, PRIMARY KEY ((id) HASH));\n"
        m = {"id": "P1", "table": "t", "anomalies": ["scanned"], "returned": 1, "scanned": 900,
             "scanned_per_row": 900, "asked": 1, "seeks": 1, "scan": "Seq Scan"}
        plans = {"measured": {"patterns": [dict(m)], "tables": {}, "gaps": []}}
        res = self.review(ddl=ddl, queries="SELECT x FROM t WHERE id = 1", plans=plans)
        self.assertIn("MEAS001", self.rules(res))
        self.assertIn("no pg_stats", self.only(res, "MEAS001")["caveats"])  # a default shape
        other = dict(plans, version="9.9.9.1", version_match={
            "customer": "9.9.9.9", "exact": False, "drift_known": False,
            "image_release": "9.9.9.1"})
        res = self.review(ddl=ddl, queries="SELECT x FROM t WHERE id = 1", plans=other)
        self.assertNotIn("MEAS001", self.rules(res))  # another release: withheld, and said
        self.assertIn("not raised as MEAS001 (P1)", report.render(res))
        other["version_match"] = {"customer": "9.9.9.9", "exact": False, "drift_known": True,
                                  "image_release": "9.9.9.1", "drift": {"rules": []}}
        res = self.review(ddl=ddl, queries="SELECT x FROM t WHERE id = 1", plans=other)
        self.assertIn("nearest earlier release", self.only(res, "MEAS001")["confidence"])
        plans["measured"]["patterns"][0]["plan_flip"] = ["Seq Scan "]
        res = self.review(ddl=ddl, queries="SELECT x FROM t WHERE id = 1", plans=plans)
        self.assertNotIn("MEAS001", self.rules(res))


class RoundEightFixes(Case):
    """Fixes from review round 8, one behaviour each."""

    def test_not_equal_reads_alike_from_text_and_plan(self):
        self.assertEqual(measure.compare_params("SELECT 1 FROM t WHERE a <> $1",
                                                {1: ("a", "ne")}), [])
        self.assertEqual(measure.compare_params("SELECT 1 FROM t WHERE a != $1",
                                                {1: ("a", "ne")}), [])

    def test_a_keyless_table_s_unique_set_gets_an_injective_column(self):
        ddl = "CREATE TABLE f (a int, x text, y text);\n"
        b = self.bundle(ddl=ddl)
        sch = analyze.build_schema(b)
        plan, _ = generate.plan_table(sch, b, "f", facts_of(sch, "f"), {}, uniques=[["x"]])
        self.assertEqual(plan["shape"]["x"]["role"], "n")

    def test_narrowing_a_column_named_like_its_own_aliases(self):
        shape = {"v": {"type": "int", "pool": ["0", "1"], "size": 2, "comp": 0,
                       "notnull": True}}
        facts = {"tables": [{"schema": "public", "name": "t", "checks": [
            {"name": "t_v", "expr": "(v > 0)", "cols": ["v"]}]}]}
        srv = ScriptedServer([("ybm_v::int AS v", '["1"]', "")])
        generate.narrow(srv, "db", {"t": {"shape": shape}}, facts, {"t": ("public", "t")})
        self.assertEqual(shape["v"]["domain"], ["1"])
        self.assertNotIn("SELECT v,", srv.sent[0])

    def test_a_flipped_plan_is_not_reported_as_unexplained(self):
        ddl = "CREATE TABLE t (id int NOT NULL, x int, PRIMARY KEY ((id) HASH));\n"
        m = {"id": "P1", "table": "t", "anomalies": ["scanned"], "returned": 1, "scanned": 900,
             "scanned_per_row": 900, "asked": 1, "seeks": 1, "scan": "Seq Scan",
             "plan_flip": ["Seq Scan "]}
        plans = {"measured": {"patterns": [m], "tables": {}, "gaps": []}, "patterns": [],
                 "version": "9.9.9.9", "mode": "customer"}
        res = self.review(ddl=ddl, queries="SELECT x FROM t WHERE id = 1", plans=plans)
        self.assertNotIn("unexplained (MEAS001", report.render(res))
        plans["measured"]["joins"] = ["P7"]
        res = self.review(ddl=ddl, queries="SELECT x FROM t WHERE id = 1", plans=plans)
        self.assertIn("joins (the pass runs single-table reads): P7", report.render(res))
        plans["measured"] = {"patterns": [], "tables": {}, "gaps": [], "joins": ["P7"]}
        res = self.review(ddl=ddl, queries="SELECT x FROM t WHERE id = 1", plans=plans)
        self.assertIn("joins (the pass runs single-table reads): P7", report.render(res))

    def test_a_failed_recording_still_removes_the_container(self):
        closed = []

        class Inner:
            name = "x"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                closed.append(True)
                return False
        rec = replay.Recorder(Inner(), "/nonexistent-dir/recorded.json")
        with self.assertRaises(OSError):
            with rec:
                pass
        self.assertEqual(closed, [True])


class BoundValues(Case):
    """Every value the workload binds is in the sample, or the review says which are not."""

    def test_hash_key_values_get_their_prefixes(self):
        ddl = "CREATE TABLE h (id int NOT NULL, x text, PRIMARY KEY ((id) HASH));\n"
        plan, _ = sample(self, ddl, "h", {"id": [str(i) for i in range(5)]})
        self.assertEqual(plan["prefixes"], 5)
        self.assertEqual(generate.unloaded_literals(plan), {})
        plan, _ = sample(self, ddl, "h", {"id": [str(i) for i in range(12)]})
        self.assertEqual(plan["prefixes"], generate.MAX_PREFIXES)
        self.assertEqual(generate.unloaded_literals(plan), {"id": (8, 12)})


class Resettle(unittest.TestCase):
    """B after C: narrowing a parent's values narrows its foreign key children too."""

    def test_a_child_keeps_only_values_its_parent_holds(self):
        def slot(comp, pool, size, role):
            return {"comp": comp, "pool": pool, "domain": pool[:size], "size": size,
                    "role": role}
        keys = {"p": ("public", "p"), "c": ("public", "c")}
        edge = [((("public", "c"), "pid"), (("public", "p"), "id"))]
        def run(parent):
            plans = {"p": {"shape": {"id": parent}, "prefixes": 2, "range_cols": []},
                     "c": {"shape": {"pid": slot(1, ["0", "1", "2", "3"], 3, "m")},
                           "prefixes": 1, "range_cols": [], "flat": 8}}
            srv = ScriptedServer([])
            generate.resettle(srv, "db", plans, edge, keys)
            return plans["c"]["shape"]["pid"], srv
        child, srv = run(slot(5, ["1", "2"], 2, "n"))  # narrowed: 0 is gone
        self.assertEqual(child["domain"], ["1", "2"])
        self.assertEqual(child["comp"], 6)  # its own list, on the server
        self.assertIn("INSERT INTO ybm_dom VALUES (6, ARRAY['1', '2']", srv.sent[0])
        child, _ = run(slot(5, ["1", "2", "3"], 3, "p"))  # a hash key: two prefixes hold 1, 2
        self.assertEqual(child["domain"], ["1", "2"])


class Scrub(Case):
    """Gap records leave the engine as shapes: constants and row values are replaced."""

    def test_constants_and_row_values_are_replaced(self):
        self.assertEqual(server.scrub("(e.value ->> 'op'::text) = $2 AND x > 10.5 AND t1.c = 3"),
                         "(e.value ->> '?'::text) = $2 AND x > N AND t1.c = N")
        self.assertEqual(server.scrub('ERROR:  new row violates check constraint "c" '
                                      'Failing row contains (1, abc, 2026-01-01).'),
                         'ERROR:  new row violates check constraint "c" '
                         'Failing row contains (...)')
        self.assertEqual(server.scrub("DETAIL:  Key (a, b)=(x, 7) already exists."),
                         "DETAIL:  Key (a, b)= (...)")

    def test_every_gap_record_is_scrubbed(self):
        ddl = "CREATE TABLE c (t text NOT NULL, v int, PRIMARY KEY ((t) HASH));\n"
        plans = {"version": "9.9.9.9", "mode": "customer", "assumed_settings": {},
                 "version_match": {"exact": True},
                 "patterns": [{"id": "P1", "query": "", "plan": [{"Plan": {
                     "Node Type": "Index Scan", "Relation Name": "c", "Alias": "c",
                     "Index Name": "c_pkey", "Index Cond": "(c.t = 'secret'::text)",
                     "Filter": "((c.v ->> 'k'::text) = 'secret'::text)"}}]}]}
        plans["measured"] = {"tables": {"c": {"measured": False, "reason":
                                              "load failed: Failing row contains (secret)"}},
                             "patterns": [{"id": "P1", "table": "c", "error":
                                           "ERROR:  bad value 'secret' LINE 1: ... 'secret'"}],
                             "gaps": [{"layer": "gen.ddl", "errors": 2,
                                       "server": "ERROR:  'secret'"}]}
        res = self.review(ddl=ddl, queries="SELECT v FROM c WHERE t = 'secret'", plans=plans)
        self.assertTrue(res["gaps"])
        self.assertNotIn("secret", json.dumps(res["gaps"]))
        self.assertNotIn("secret", json.dumps(res["measured"]))
        md = report.render(res)  # the review quotes the user's own queries; its record of
        self.assertNotIn("secret", md[md.index("## Validation"):])  # the run does not
        self.assertEqual(res["measured"]["gaps"][0]["errors"], 2)  # counts stay


class ScratchPreparation(Case):
    """One preparation for every scratch database: the catalog's repairs and colocation retry
    apply to replay and the measured pass too; a pattern whose tables did not load is the
    input's failure, not the statement's."""

    def test_prepare_completes_an_incomplete_dump(self):
        class Box:
            def __init__(self):
                self.sent, self.files, self.coloc = [], [], False

            def sql(self, text, db="ybm", flags=()):
                self.sent.append(text)
                if "colocation = true" in text:
                    self.coloc = True
                return subprocess.CompletedProcess([], 0, "", "")

            def file_sql(self, path, db="ybm"):
                with open(path) as fh:
                    text = fh.read()
                self.files.append(text)
                errs = []
                if not self.coloc and "colocation_id" in text:
                    errs.append("ysqlsh:%s:2: ERROR:  cannot set colocation_id for "
                                "non-colocated table" % path)
                elif "CREATE SCHEMA" not in text and "archive.t" in text:
                    errs.append('ysqlsh:%s:3: ERROR:  schema "archive" does not exist' % path)
                return subprocess.CompletedProcess([], 0, "\n".join(errs), "")
        ddl = ("CREATE TABLE a (id int PRIMARY KEY) WITH (colocation_id='2');\n"
               "CREATE TABLE archive.t (id int PRIMARY KEY);\n")
        box = Box()
        failed, info = catalog.prepare(box, "ybm", ddl, colocated=False)
        self.assertEqual(failed, [])
        self.assertTrue(info["colocated"])
        self.assertIn('CREATE SCHEMA IF NOT EXISTS "archive";', info["repairs"])
        self.assertTrue(box.sent[0].startswith("DROP DATABASE IF EXISTS ybm"))

    def test_a_pattern_whose_tables_did_not_load_is_not_blamed(self):
        ddl = "CREATE TABLE t (id int PRIMARY KEY);\n"
        plans = {"version": "9.9.9.9", "mode": "customer", "assumed_settings": {},
                 "version_match": {"exact": True},
                 "ddl_errors": {"count": 1, "first": ['schema "archive" does not exist']},
                 "preparation": {"repairs": [], "colocated": False},
                 "patterns": [{"id": "P1", "query": "", "plan": None,
                               "error": 'ERROR:  relation "archive.t" does not exist',
                               "missing_tables": ["archive.t"]}]}
        res = self.review(ddl=ddl, queries="SELECT id FROM archive.t WHERE id = 1",
                          plans=plans)
        self.assertNotIn("PLN005", self.rules(res))
        md = report.render(res)
        self.assertIn("DDL the scratch server rejects", md)
        self.assertIn("did not load: P1 (archive.t)", md)


class OtherOperators(unittest.TestCase):
    """An operator the binding codes do not know is one atom with its symbol; whether an index
    serves it is what the server says (Index Cond: op, sought; a filter: never sought)."""

    def test_unknown_operators_are_read_not_left_over(self):
        self.assertEqual(server.atom("docs.data @> $1"),
                         {"col": "data", "op": "other", "sym": "@>", "kind": "param",
                          "value": 1})
        self.assertEqual(server.atom("$1 <@ docs.data")["col"], "data")
        self.assertIn("residue", server.atom("f(x) @> g(y)"))
        sch = schema.parse("CREATE TABLE docs (id int PRIMARY KEY, data jsonb);\n"
                           "CREATE INDEX docs_gin ON docs USING gin (data);\n")
        idx = sch.indexes["docs_gin"]
        def scan(**cond):
            return server.scans([{"Plan": dict({"Node Type": "Index Scan", "Relation Name": "docs",
                                                "Alias": "docs", "Index Name": "docs_gin"},
                                               **cond)}])[0]
        self.assertEqual(server.server_map(scan(**{"Index Cond": "(docs.data @> $1)"}), idx),
                         [{"col": "data", "code": "op"}])
        self.assertEqual(server.server_map(scan(**{"Filter": "(docs.data @> $1)"}), idx),
                         [{"col": "data", "code": "filter"}])  # in a filter: never sought
        self.assertEqual(server.residue([{"Plan": {"Node Type": "Index Scan",
                                                   "Relation Name": "docs",
                                                   "Index Cond": "(docs.data @> $1)"}}]), [])
        self.assertEqual(analyze.derive_binding([{"col": "data", "code": "op"}], 0)["prefix"], 1)


class PlanAccess(Case):
    """The replayed plan's access per (pattern, table): partitions fold to their table, their
    index copies to the parent's index or, with none, to the partitions' own keys."""

    def run_plan(self, ddl, q, scans):
        b = self.bundle(ddl=ddl, queries=q)
        sch = analyze.build_schema(b)
        pats = analyze.build_patterns(b, sch)
        plans = {"patterns": [{"id": "P1", "query": q, "plan": [{"Plan": {
            "Node Type": "Append", "Plans": [dict({"Relation Name": r, "Alias": r},
                                                  **{"Node Type": n, "Index Name": i})
                                             for r, n, i in scans]}}]}]}
        return analyze.plan_access(plans, pats, sch)

    def test_partition_scans_fold_to_the_parent(self):
        ddl = ("CREATE TABLE ev (id bigint NOT NULL, ts date NOT NULL, PRIMARY KEY (id, ts)) "
               "PARTITION BY RANGE (ts);\n"
               "CREATE TABLE ev_a PARTITION OF ev FOR VALUES FROM ('2026-01-01') TO "
               "('2026-02-01');\n"
               "CREATE TABLE ev_b PARTITION OF ev FOR VALUES FROM ('2026-02-01') TO "
               "('2026-03-01');\n")
        pa = self.run_plan(ddl, "SELECT ts FROM ev WHERE id = $1",
                           [("ev_a", "Index Scan", "ev_a_pkey"),
                            ("ev_b", "Index Scan", "ev_b_pkey")])
        self.assertIsNotNone(pa[("P1", "ev")])  # one access, not a conflict
        self.assertEqual(pa[("P1", "ev")]["node"], "Index Scan")
        pa = self.run_plan(ddl, "SELECT ts FROM ev WHERE id = $1",
                           [("ev_a", "Index Scan", "ev_a_pkey"), ("ev_b", "Seq Scan", None)])
        self.assertIsNone(pa[("P1", "ev")])  # partitions read two ways: the model decides


class Recorded(unittest.TestCase):
    """The whole replay and measured pass, offline: a live run's SQL calls and answers on the
    synthetic generator fixture (evals/yb-model/fixtures/gen) are served back, and the result
    is held to properties every sample must have. A changed SQL call fails here: record again
    (see the fixture's README)."""
    FIX = os.path.join(HERE, "..", "..", "..", "evals", "yb-model", "fixtures", "gen")

    @classmethod
    def setUpClass(cls):
        rec = os.path.join(cls.FIX, "recorded.json")
        if not os.path.isfile(rec):
            raise unittest.SkipTest("no recording")
        cls.runs = []
        saved = replay.Container
        replay.Container = lambda *a, **k: replay.Recorded(rec)
        try:
            for _ in range(2):
                b = inputs.load(os.path.join(cls.FIX, "bundle"), release="2025.2.6.0")
                cls.runs.append(replay.run(b, image="yugabytedb/yugabyte:2025.2.6.0-b111",
                                           measure=True, catalog=True))
                cls.catalog_info = b.catalog_info
        finally:
            replay.Container = saved
        cls.m = cls.runs[0]["measured"]

    def test_the_schema_is_the_replay_database_s_catalog(self):
        self.assertEqual(self.catalog_info["load_seconds"], 0.0)  # read, not loaded again
        self.assertEqual(self.catalog_info["rejected"], [])

    def test_every_table_loads_with_its_constraints(self):
        self.assertEqual(sorted(t for t, v in self.m["tables"].items() if not v["measured"]),
                         [])
        dropped = [g for g in self.m["gaps"] if g["layer"] in (
            "gen.check_dropped", "gen.constraint_dropped", "gen.predicate", "gen.fk_cycle")]
        self.assertEqual([(g["layer"], g["table"]) for g in dropped],
                         [("gen.fk_cycle", "cb")])  # only what a cycle forces

    def test_the_sample_is_as_planned_and_every_read_finds_rows(self):
        layers = {g["layer"] for g in self.m["gaps"]}
        self.assertFalse(layers & {"gen.shape", "gen.plan_flip", "gen.empty", "gen.facts",
                                   "gen.narrow"}, self.m["gaps"])
        done = [p for p in self.m["patterns"] if "returned" in p]
        self.assertEqual(len(done), len(self.m["patterns"]))
        self.assertTrue(all(p["returned"] >= 1 for p in done))

    def test_rows_a_predicate_rejects_are_counted(self):
        lost = [g for g in self.m["gaps"] if g["layer"] == "gen.filtered"]
        for g in lost:
            self.assertLess(g["server"], g["model"])
            self.assertTrue(g["rejects"])  # which predicate, how many

    def test_the_run_is_deterministic(self):
        self.assertEqual(json.dumps(self.runs[0], sort_keys=True, default=str),
                         json.dumps(self.runs[1], sort_keys=True, default=str))


class Recall(unittest.TestCase):
    """Every public fixture's answer key: each defect with an engine_match is found, and no
    trap with an engine_absent is fallen into (evals/yb-model/recall.py)."""

    def test_public_fixtures_meet_their_answer_keys(self):
        script = os.path.join(HERE, "..", "..", "..", "evals", "yb-model", "recall.py")
        if not os.path.isfile(script):
            self.skipTest("evals not present")
        r = subprocess.run([sys.executable, script, "ecommerce", "keygap"], capture_output=True,
                           text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"keygap\s+G1\s+defect\s+CAP014\s+hit")
        self.assertRegex(r.stdout, r"keygap\s+T2\s+trap\s+-\s+ok")


class Preflight(Case):
    """Missing inputs are named, gate the review, and mute the rules that would guess."""

    DDL = ("CREATE TABLE items (id bigint NOT NULL, group_id bigint NOT NULL, name text, "
           "PRIMARY KEY ((id) HASH));\n"
           "CREATE INDEX items_group ON items ((group_id) HASH);\n")
    Q = "SELECT * FROM items WHERE group_id = $1"

    def test_definitions_are_consistent(self):
        rules = analyze.load_rules()
        for d in preflight.defs():
            self.assertIn(d["id"], preflight.DETECT, d["id"])
            for r in d.get("mutes", []) + (d.get("weakens") or {}).get("rules", []):
                self.assertIn(r, rules, "%s names unknown rule %s" % (d["id"], r))
        self.assertEqual(set(preflight.DETECT), {d["id"] for d in preflight.defs()})

    def test_unranked_list_withholds_rekey(self):
        res = self.review(ddl=self.DDL, queries=self.Q)
        self.assertNotIn("CAP050", self.rules(res))
        self.assertEqual(res["preflight"]["muted"]["CAP050"]["withheld"], ["items"])
        self.assertTrue(all("pattern unranked" in f["caveats"] for f in res["findings"]
                            if f["rule"] == "CAP020"))
        pss = PSS_HEAD + "1,1000000,5000,0.005,1000000,\"%s\"\n" % self.Q
        self.assertIn("CAP050", self.rules(self.review(ddl=self.DDL, pss=pss)))

    def test_review_is_gated_and_writes_the_chat_message(self):
        d = self.tmpdir({"schema.sql": self.DDL, "queries.sql": self.Q + ";\n"})
        r = cli("preflight", d)
        self.assertEqual(r.returncode, 3)
        self.assertIn("Proceed with the review anyway? (yes / no)", r.stdout)
        r = cli("review", d, "--no-replay")
        self.assertEqual(r.returncode, 3)
        self.assertFalse(os.path.exists(os.path.join(d, "review.md")))
        r = cli("review", d, "--no-replay", "--accept-missing")
        self.assertIn(r.returncode, (0, 1), r.stderr)
        with open(os.path.join(d, "review.md")) as fh:
            md = fh.read()
        self.assertIn("### Muted checks (inputs missing)", md)
        self.assertIn("INCOMPLETE", md)
        # The review prints no DDL at all: a statement exists only for a chosen option.
        self.assertNotRegex(md, r"(?m)^\s*(CREATE|DROP|ALTER) (UNIQUE )?(INDEX|TABLE)\b")
        self.assertNotIn("## DDL sketches", md)
        # The chat message is engine-written: rendered from review.json, so the same review
        # always gives the same message.
        with open(os.path.join(d, "chat.md")) as fh:
            chat = fh.read()
        with open(os.path.join(d, "review.json")) as fh:
            res = json.load(fh)
        self.assertEqual(chat, report.render_chat(res, os.path.join(d, "review.md")))
        self.assertTrue(chat.startswith(report.headline(res)))
        for part in ("Top findings:", "INCOMPLETE", "Missing inputs that change the findings:",
                     "Withheld candidates"):
            self.assertIn(part, chat)

    @unittest.skipUnless(os.path.isdir(FIXTURE), "fixture bundle not present")
    def test_a_complete_bundle_is_not_told_minor_inputs_are_missing(self):
        txt = preflight.render_prompt({"missing": []}, replay_note="will not run: no image")
        self.assertTrue(txt.startswith("yb-model preflight: all inputs present."), txt)
        self.assertNotIn("minor ones are missing", txt)

    def test_complete_bundle_needs_no_confirmation(self):
        pf = preflight.assess(inputs.load(FIXTURE))
        self.assertEqual(pf["missing"], [])
        self.assertFalse(pf["needs_confirmation"])


class KeysetAndConflicts(Case):
    DDL = ("CREATE TABLE ev (c uuid NOT NULL, id uuid NOT NULL, ts timestamptz NOT NULL, "
           "src text, ext bigint NOT NULL, v text, PRIMARY KEY ((c) HASH, id ASC));\n"
           "CREATE INDEX ev_tl ON ev ((c) HASH, ts DESC, id DESC);\n"
           "CREATE UNIQUE INDEX ev_ext ON ev ((ext) HASH);\n"
           "CREATE INDEX ev_src ON ev ((c) HASH, src ASC);\n")
    PAGE = ("SELECT * FROM ev WHERE c = $1 AND (ts, id) < ($2, $3) "
            "ORDER BY ts DESC, id DESC LIMIT 50")

    def test_keyset_page_uses_ordered_index_and_flags_row_cursor(self):
        res = self.review(ddl=self.DDL, queries=self.PAGE)
        self.assertEqual(res["patterns"][0]["access"][0]["path"], "ev_tl")
        self.assertNotIn("CAP010", self.rules(res))
        self.assertIn("CAP013", self.rules(res))
        self.assertEqual(prescriptions(res), [])
        fixed = self.PAGE.replace("AND (ts, id)", "AND ts <= $2 AND (ts, id)")
        self.assertNotIn("CAP013", self.rules(self.review(ddl=self.DDL, queries=fixed)))

    def test_declared_complete_list_reports_unused_index(self):
        self.assertNotIn("IDX001", self.rules(self.review(ddl=self.DDL, queries=self.PAGE)))
        idx = self.only(self.review(ddl=self.DDL,
                                    queries="-- ybm: workload-complete\n" + self.PAGE), "IDX001")
        self.assertIn("ev_src", idx["fact"])
        self.assertIn("workload declared complete", idx["caveats"])

    def test_on_conflict_names_a_constraint_or_a_partial_arbiter(self):
        ddl = ("CREATE TABLE t (id bigint NOT NULL, a text, b text, PRIMARY KEY ((id) HASH), "
               "CONSTRAINT t_a_key UNIQUE (a));\n"
               "CREATE UNIQUE INDEX t_b_idx ON t (b HASH);\n"
               "CREATE UNIQUE INDEX t_ab_live ON t (a HASH, b ASC) WHERE b IS NOT NULL;\n")
        ins = "INSERT INTO t (id, a, b) VALUES ($1, $2, $3) ON CONFLICT %s DO NOTHING"
        for target, fires in (("ON CONSTRAINT t_a_key", False), ("ON CONSTRAINT t_pkey", False),
                              ("ON CONSTRAINT t_b_idx", True),  # an index, not a constraint
                              ("ON CONSTRAINT t_nope", True),
                              ("(a, b) WHERE b IS NOT NULL", False),  # the partial index
                              ("(a, b)", True)):
            with self.subTest(target):
                res = self.review(ddl=ddl, queries=ins % target)
                self.assertEqual("CAP060" in self.rules(res), fires)
                self.assertEqual(prescriptions(res), [])  # reasons, on every branch
        f = self.only(self.review(ddl=ddl, queries=ins % "(a, b)"), "CAP060")
        self.assertIn("ON CONFLICT (a, b) WHERE b IS NOT NULL (the change_target option)",
                      f["fix"])
        self.assertIsNone(f["ddl"])

    def test_on_conflict_fix_uses_an_existing_unique_subset_or_a_safe_new_index(self):
        # (ext) has its unique index; (src, ext) has none, but ev_ext already makes it unique.
        res = self.review(ddl=self.DDL, queries=(
            "INSERT INTO ev (c, id, ts, src, ext) VALUES ($1,$2,$3,$4,$5) "
            "ON CONFLICT (src, ext) DO NOTHING;\n"
            "INSERT INTO ev (c, id, ts, src, ext) VALUES ($1,$2,$3,$4,$5) "
            "ON CONFLICT (ext) DO NOTHING;\n"))
        f = self.only(res, "CAP060")
        self.assertIn("ev_ext", f["fact"])
        self.assertIn("ON CONFLICT (ext)", f["fix"])
        self.assertIsNone(f["ddl"])
        # No unique subset: a new unique index, hashed on the NOT NULL column.
        ddl = ("CREATE TABLE x (k bigint NOT NULL, src varchar, v int NOT NULL, "
               "PRIMARY KEY ((k) HASH));\n")
        q = "INSERT INTO x (k, src, v) VALUES ($1, $2, $3) ON CONFLICT (src, v) DO NOTHING"
        f = self.only(self.review(ddl=ddl, queries=q), "CAP060")
        self.assertIn("((v) HASH, src ASC)", f["ddl"])
        self.assertIn("src may be NULL", f["fix"])
        # A colocated table cannot take a hash key: the same index with range keys.
        f = self.only(self.review(ddl=ddl.replace(");\n", ") WITH (colocation = true);\n"),
                                  queries=q), "CAP060")
        self.assertIn("(v ASC, src ASC)", f["ddl"])
        self.assertNotIn("HASH", f["ddl"])


class Probes(unittest.TestCase):
    """Probe definitions are valid, and verdicts fold into findings as specified."""

    def test_definitions_parse(self):
        rules = analyze.load_rules()
        self.assertTrue(probes.rules_with_probes())
        for rid, p in probes.rules_with_probes().items():
            self.assertIn(rid, rules)
            self.assertTrue(p["setup"] and p["cases"] and p["holds"], rid)
            for expr in p.get("applies", []) + p["holds"]:
                probes.parse_check(expr, list(p["cases"]))
            for q in p["cases"].values():
                self.assertNotRegex(q, r"\$\d", "probe queries use literals: " + rid)

    def test_checks_reject_code(self):
        for bad in ("__import__('os')", "claim.__class__", "open('x')", "claim.nope > 1"):
            with self.assertRaises((ValueError, SyntaxError)):
                probes.parse_check(bad, ["claim"])

    def test_verdicts(self):
        p = {"cases": {"claim": "", "control": ""}, "applies": ["claim.index == 'i'"],
             "holds": ["claim.scanned >= 10 * claim.returned"]}

        def m(**k):
            return dict(dict.fromkeys(probes.METRICS, 0), **k)
        for claim, verdict in ((m(index="i", scanned=500, returned=50), "holds"),
                               (m(index="i", scanned=50, returned=50), "refuted"),
                               (m(index="j", scanned=500, returned=50), "inconclusive")):
            self.assertEqual(probes.judge(p, {"claim": claim, "control": m()})[0], verdict)

    def test_refuted_probe_withdraws_finding(self):
        col = analyze.Collector()
        col.add(analyze.Finding("CAP013", "medium", "probable", "ix on t", "fact", ["P1"]))
        recon = []
        probes.apply(col, {"version_match": {"image_release": "9.9.9.9", "exact": True},
                           "probes": {"CAP013": {"verdict": "refuted",
                                                 "detail": "claim.scanned >= 1", "cases": {}}}},
                     recon)
        self.assertEqual(col.items, {})
        self.assertIn("9.9.9.9", recon[0]["outcome"])

    def test_a_refutation_on_another_release_stands_only_when_the_drift_is_known(self):
        other = {"image_release": "9.9.9.9", "customer_release": "9.9.8.0", "exact": False}
        for label, vm, removed in (
                ("drift unknown", dict(other, drift_known=False), False),
                ("this rule drifted", dict(other, drift_known=True,
                                           drift={"rules": ["CAP013"]}), False),
                ("drift known, other rules", dict(other, drift_known=True,
                                                  drift={"rules": ["CAP001"]}), True)):
            with self.subTest(label):
                col, recon = analyze.Collector(), []
                col.add(analyze.Finding("CAP013", "medium", "probable", "ix on t", "fact",
                                        ["P1"]))
                probes.apply(col, {"version_match": vm, "probes": {"CAP013": {
                    "verdict": "refuted", "detail": "claim.scanned >= 1", "cases": {}}}}, recon)
                self.assertEqual(not col.items, removed)
                if not removed:
                    f = list(col.items.values())[0]
                    self.assertIn("neither confirms nor refutes", f.verified)
                    self.assertEqual(recon, [])
                # A probe that holds there does not verify the finding either.
                col = analyze.Collector()
                col.add(analyze.Finding("CAP013", "medium", "probable", "ix on t", "fact",
                                        ["P1"]))
                probes.apply(col, {"version_match": vm, "probes": {"CAP013": {
                    "verdict": "holds", "detail": "", "cases": {}}}}, [])
                v = list(col.items.values())[0].verified
                self.assertEqual(v.startswith("holds on"), removed, v)


class IndexRuleEdges(Case):
    """Partial queue indexes, two-valued columns, and drops that settle other findings."""

    def test_partial_queue_index_on_a_flag_is_not_flagged(self):
        res = self.review(ddl="CREATE TABLE q (id bigint NOT NULL, done boolean NOT NULL, "
                              "PRIMARY KEY ((id) HASH));\n"
                              "CREATE INDEX q_todo ON q (done ASC) WHERE (done = false);\n",
                          stats=["q,done,f,0,1,1,{f},{1},,1"], rows={"q": 1000000})
        got = {(f["rule"], f["index"]) for f in res["findings"]}
        self.assertNotIn(("STA008", "q_todo"), got)
        self.assertNotIn(("STA004", "q_todo"), got)

    def test_absolute_skew_severity_scales_with_rows_on_one_hash_code(self):
        ddl = ("CREATE TABLE s (id bigint NOT NULL, g bigint NOT NULL, "
               "PRIMARY KEY ((id) HASH));\nCREATE INDEX s_g ON s ((g) HASH);\n")
        for rows, sev in {1e7: "low", 5e8: "medium", 5e9: "high"}.items():  # top value: 2%
            res = self.review(ddl=ddl, stats=["s,g,f,0,8,5000,{7},{0.02},,0"],
                              rows={"s": int(rows)})
            self.assertEqual([f["severity"] for f in res["findings"] if f["rule"] == "STA003"],
                             [sev], rows)

    def test_a_unique_index_is_never_offered_a_drop(self):
        # The singleton-row idiom: a UNIQUE index on one boolean column.
        res = self.review(ddl="CREATE TABLE s (id bigint NOT NULL, one boolean, PRIMARY KEY "
                              "((id) HASH));\nCREATE UNIQUE INDEX s_one ON s (one ASC);\n",
                          rows={"s": 1})
        f = self.only(res, "STA008")
        self.assertIsNone(f["ddl"])
        self.assertEqual([o["id"] for o in f["options"]], ["partial", "keep"])
        for g in res["findings"]:
            if g.get("index") == "s_one":
                self.assertFalse(any(o["change"] == "drop" for o in g["options"]), g["rule"])

    def test_a_drop_never_settles_the_other_findings_on_the_index(self):
        # STA008 sketches a drop; the other findings on the index keep their own sketches and
        # options, and the area says they pull different ways.
        res = self.review(ddl="CREATE TABLE f (id bigint NOT NULL, flag boolean, "
                              "PRIMARY KEY ((id) HASH));\n"
                              "CREATE INDEX f_flag ON f ((flag) HASH);\n",
                          stats=["f,flag,f,0.6,1,2,{t},{0.3},,0"], rows={"f": 10000000})
        on = [f for f in res["findings"] if f["index"] == "f_flag"]
        drop = [f for f in on if f["rule"] == "STA008"]
        self.assertEqual(len(drop), 1)
        self.assertIn("idx_scan", drop[0]["ddl"])
        others = [f for f in on if f["rule"] != "STA008" and f.get("ddl")]
        self.assertTrue(others)
        self.assertFalse(any("resolves this too" in (f["fix"] or "") for f in on))
        area = next(d for d in res["dossiers"] if d["area"] == ["f"])
        self.assertTrue(any("all concern f_flag" in t for t in area["tensions"]),
                        area["tensions"])
        self.assertEqual([d["object"] for d in area["decisions"]], ["f_flag"])

class ReplayAuthority(Case):
    """What a replayed plan may and may not do to a static finding."""

    DDL = ("CREATE TABLE r (id bigint NOT NULL, k bigint, v text, PRIMARY KEY ((id) HASH));\n"
           "CREATE INDEX r_v ON r ((v) HASH);\n")
    Q = "SELECT id FROM r WHERE k = $1"   # nothing leads with k: CAP002, predicts a scan
    INDEX_SCAN = {"Node Type": "Index Scan", "Relation Name": "r", "Index Name": "r_v",
                  "Alias": "r"}

    def test_the_plan_s_access_replaces_the_model_s(self):
        # The model predicts a scan (CAP002); the replayed plan reads r_v. The plan's access is
        # the access: no CAP002, and the disagreement is a path gap, not a refuted finding.
        res = self.replay(self.DDL, self.Q, self.INDEX_SCAN, {"r": 1e7})
        self.assertNotIn("CAP002", self.rules(res))
        self.assertNotIn("refuted by replay", [r["outcome"] for r in res["reconciliation"]])
        self.assertIn({"layer": "path", "pattern": "P1", "table": "r", "model": "no index",
                       "server": "r_v", "taken": "plan"}, res["gaps"])
        self.assertEqual(res["patterns"][0]["access"][0]["source"], "plan")
        self.assertIn("P1 on r: plan r_v, model no index", report.render(res))

    def test_a_release_difference_in_an_access_rule_keeps_the_model_s_access(self):
        res = self.replay(self.DDL, self.Q, self.INDEX_SCAN, {"r": 1e7}, exact=False,
                          drift={"rules": ["CAP014"]}, customer_release="9.9.9.9",
                          image_release="9.9.9.8")
        cap = self.only(res, "CAP002")  # the model's access: no index leads with k
        self.assertEqual(res["patterns"][0]["access"][0]["source"], "model")
        self.assertIn("CAP014 differs", cap["replay"])
        self.assertNotIn("confirmed", cap["confidence"])
        self.assertIn("the engine's model chose every access", report.render(res))

    def test_the_plan_s_node_says_whether_the_index_covers(self):
        q = "SELECT id, k FROM r WHERE v = $1"  # the model: r_v lacks k (CAP020)
        res = self.review(ddl=self.DDL, queries=q)
        self.assertIn("CAP020", self.rules(res))
        only = dict(self.INDEX_SCAN, **{"Node Type": "Index Only Scan"})
        res = self.replay(self.DDL, q, only, {"r": 1e7})
        self.assertNotIn("CAP020", self.rules(res))  # the plan fetched no row: covering
        res = self.replay(self.DDL, q, self.INDEX_SCAN, {"r": 1e7})
        self.assertIn("confirmed", self.only(res, "CAP020")["confidence"])

    def test_under_assumed_settings_the_plan_stands_labelled(self):
        res = self.replay(self.DDL, self.Q, self.INDEX_SCAN, {"r": 1e7},
                          assumed={"yb_enable_cbo": "legacy_mode (compiled default)"})
        self.assertNotIn("CAP002", self.rules(res))  # the plan stands; no "disputed" state
        self.assertIn("path", [g["layer"] for g in res["gaps"]])
        seq = dict(self.INDEX_SCAN, **{"Node Type": "Seq Scan", "Index Name": None})
        res = self.replay(self.DDL, self.Q, seq, {"r": 1e7},
                          assumed={"yb_enable_cbo": "legacy_mode (compiled default)"})
        cap = self.only(res, "CAP002")  # the plan's scan, confirmed and labelled
        self.assertIn("planner settings assumed", cap["confidence"])
        self.assertIn("P1: Seq Scan", cap["replay"])

    def test_without_statistics_the_plan_corroborates_and_the_model_decides(self):
        # The plan reads r_v, chosen on defaults: the model's access (no index) stands, the
        # finding stays probable, and the plan's other choice is listed for the reader.
        res = self.replay(self.DDL, self.Q, self.INDEX_SCAN, {"r": 1e7},
                          statistics_missing=["column statistics"])
        cap = self.only(res, "CAP002")
        self.assertEqual(cap["confidence"], "probable")
        self.assertIn("chosen without column statistics", cap["replay"])
        self.assertEqual(res["patterns"][0]["access"][0]["source"], "model")
        self.assertIn({"layer": "path", "pattern": "P1", "table": "r", "model": "no index",
                       "server": "r_v", "taken": "model"}, res["gaps"])
        self.assertIn("P1 on r: model no index, plan r_v", report.render(res))
        self.assertIn("corroborate or dispute findings but confirm, remove or raise none",
                      report.render(res))
        # A weak plan raises no plan-only finding: a scan where the model reads r_v is a gap.
        seq = dict(self.INDEX_SCAN, **{"Node Type": "Seq Scan", "Index Name": None})
        q = "SELECT id FROM r WHERE v = $1"
        self.assertIn("PLN001", self.rules(self.replay(self.DDL, q, seq, {"r": 1e7})))
        res = self.replay(self.DDL, q, seq, {"r": 1e7}, statistics_missing=["table sizes"])
        self.assertNotIn("PLN001", self.rules(res))
        self.assertIn("P1 on r: model r_v, plan sequential scan", report.render(res))
        # Under assumed settings alone the plan still decides (labelled).
        res = self.replay(self.DDL, self.Q, self.INDEX_SCAN, {"r": 1e7},
                          assumed={"yb_enable_cbo": "legacy_mode (compiled default)"})
        self.assertEqual(res["patterns"][0]["access"][0]["source"], "plan")

    def test_unknown_drift_on_another_image_confirms_and_refutes_nothing(self):
        res = self.replay(self.DDL, self.Q, self.INDEX_SCAN, {"r": 1e7},
                          exact=False, drift_known=False, image_release=None)
        cap = self.only(res, "CAP002")
        self.assertIn("differences between the two releases are unknown", cap["replay"])
        self.assertNotEqual(cap["section"], "disputed")


class Workload(Case):
    """Statement time shares, and a query list merged with pg_stat_statements: statements it
    has are ranked from it, the rest are added unranked."""

    def test_shares_are_of_all_statement_time_and_dropped_rows_are_listed(self):
        res = self.review(ddl="CREATE TABLE w (id bigint NOT NULL, PRIMARY KEY ((id) HASH));\n",
                          pss=PSS_HEAD + "1,100,600,6,100,SELECT * FROM w WHERE id = $1\n"
                              "2,100,300,3,0,COMMIT\n"
                              "3,10,100,10,10,SELECT * FROM other_schema_table WHERE x = $1\n")
        self.assertEqual(res["patterns"][0]["time_share"], 0.6)
        item = [o for o in res["open_items"] if "not analysed as access patterns" in o]
        self.assertEqual(len(item), 1)
        self.assertIn("2 of 3", item[0])

    def test_fingerprint_matches_constants_casts_and_lists(self):
        fp = fingerprint
        self.assertEqual(fp("SELECT * FROM t WHERE a = 5 AND b IN (1, 2, 3) LIMIT 10"),
                         fp("select *\n  from T where a = $1 and b in ($2, $3) limit $4"))
        self.assertEqual(fp("SELECT 1 FROM t WHERE f = $2::boolean AND x = -1"),
                         fp("SELECT $1 FROM t WHERE f = $2 AND x = $3"))
        self.assertEqual(fp("INSERT INTO t (a, b) VALUES (1, 'x'), (2, 'y')"),
                         fp("INSERT INTO t (a, b) VALUES ($1, $2)"))
        self.assertNotEqual(fp("SELECT * FROM t WHERE a = 1"), fp("SELECT * FROM t WHERE b = 1"))
        self.assertEqual(fp("SELECT a - 1 FROM t"), "select a - ? from t")

    def test_listed_statements_missing_from_pss_are_added_unranked(self):
        res = self.review(ddl=Preflight.DDL,
                          pss=PSS_HEAD + "1,1000,500,0.5,1000,SELECT * FROM items WHERE id = $1\n",
                          queries="SELECT * FROM items WHERE id = 42;\n"
                                  "SELECT * FROM items WHERE name = 'x';\n")
        self.assertEqual([(p["id"], p["source"], p["weight"]) for p in res["patterns"]],
                         [("P1", "pg_stat_statements", "HOT"), ("P2", "queries.sql", "UNRANKED")])
        item = [o for o in res["open_items"] if o.startswith("queries.sql lists")]
        self.assertIn("2 statement(s): 1 found in pg_stat_statements", item[0])
        self.assertIn("(P2)", item[0])
        on_p2 = [f for f in res["findings"] if f["patterns"] == ["P2"]]
        self.assertTrue(on_p2)
        self.assertTrue(all(analyze.LISTED_UNRANKED in f["caveats"] for f in on_p2))


class Lint(unittest.TestCase):
    """yb-lint: redundant indexes (YB051, YB052) and unique indexes that admit many NULL rows
    (YB027, which fires only when a key column may be NULL)."""

    # id is only in the primary key, a and b are declared NOT NULL, c and d are nullable.
    T = ("CREATE TABLE t (id bigint, a int NOT NULL, b int NOT NULL, c int, d int, "
         "PRIMARY KEY ((id) HASH));\n")

    @staticmethod
    def lint(ddl, *rules):
        return [f for f in load_script("yb-lint.py").lint(ddl) if not rules or f.rule in rules]

    def test_real_prefix_and_duplicate_are_named_without_ddl(self):
        found = self.lint(self.T + "CREATE INDEX i1 ON t (a HASH);\n"
                                   "CREATE INDEX i2 ON t (a HASH, b ASC);\n"
                                   "CREATE INDEX i3 ON t (b ASC, c ASC);\n"
                                   "CREATE INDEX i4 ON t (b ASC, c ASC);\n")
        redundant = [f for f in found if f.rule in ("YB051", "YB052")]
        self.assertIn(("YB051", "i1"), {(f.rule, f.message.split("'")[1]) for f in redundant})
        self.assertEqual(len([f for f in redundant if f.rule == "YB052"]), 1)
        for f in found:  # YSQL rejects DROP INDEX CONCURRENTLY
            self.assertNotIn("DROP INDEX CONCURRENTLY", f.message + f.fix)
        for f in redundant:  # a reasoned suggestion, never a statement to run
            self.assertRegex(f.fix, r"^Probably redundant: .* removing \w+ saves its write cost\.$")
            self.assertNotRegex(f.fix, r"\b(DROP|CREATE|ALTER) (INDEX|TABLE)\b")

    def test_layout_uniqueness_and_include_are_respected(self):
        self.assertEqual(self.lint(self.T + "CREATE INDEX h ON t (a HASH);\n"
                                            "CREATE INDEX r ON t (a ASC, b ASC);\n"
                                            "CREATE UNIQUE INDEX u ON t (b HASH);\n"
                                            "CREATE INDEX ub ON t (b HASH, c ASC);\n"
                                            "CREATE INDEX ci ON t (c ASC) INCLUDE (a);\n"
                                            "CREATE INDEX cb ON t (c ASC, b ASC);\n"
                                            "CREATE INDEX g ON t ((a, b) HASH);\n",
                                   "YB051", "YB052"), [])

    def test_unique_index_on_columns_that_cannot_be_null_is_not_flagged(self):
        for label, ddl in (
                ("NOT NULL columns", self.T + "CREATE UNIQUE INDEX u ON t (a ASC, b ASC);\n"),
                # id has no NOT NULL in its declaration: the primary key makes it NOT NULL.
                ("table-level PK", self.T + "CREATE UNIQUE INDEX u ON t (id ASC);\n"),
                ("column-level PK", "CREATE TABLE t (id bigint PRIMARY KEY, a int);\n"
                                    "CREATE UNIQUE INDEX u ON t (id ASC);\n"),
                ("index before its table", "CREATE UNIQUE INDEX u ON t (id ASC);\n" + self.T),
                ("partial index",
                 self.T + "CREATE UNIQUE INDEX u ON t (c ASC) WHERE c IS NOT NULL;\n"),
                ("NULLS NOT DISTINCT",
                 self.T + "CREATE UNIQUE INDEX u ON t (c ASC) NULLS NOT DISTINCT;\n")):
            with self.subTest(label):
                self.assertEqual(self.lint(ddl, "YB027"), [])

    def test_every_nullable_key_column_is_named(self):
        for label, ddl, cols in (
                ("one of two", self.T + "CREATE UNIQUE INDEX u ON t (a ASC, c ASC);\n", "c"),
                ("both", self.T + "CREATE UNIQUE INDEX u ON t (c ASC, d ASC);\n", "c, d"),
                ("an expression", self.T + "CREATE UNIQUE INDEX ue ON t (lower(a) ASC);\n",
                 "lower(a)"),
                ("an undeclared table", "CREATE UNIQUE INDEX uz ON elsewhere (x ASC);\n", "x")):
            with self.subTest(label):
                got = self.lint(ddl, "YB027")
                self.assertEqual(len(got), 1)
                self.assertIn("nullable column(s) %s." % cols, got[0].message)
        f, = self.lint(self.T + "CREATE UNIQUE INDEX u ON t (a ASC, c ASC);\n", "YB027")
        self.assertEqual(f.message,
                         "u: unique index on nullable column(s) c. NULL is distinct from NULL, "
                         "so many NULL rows are permitted and Voyager live migration can raise "
                         "false conflicts on it.")
        self.assertEqual(f.fix,
                         "Declare c NOT NULL if they are never NULL, or make the index partial: "
                         "WHERE c IS NOT NULL.")
        f, = self.lint(self.T + "CREATE UNIQUE INDEX u ON t (c ASC, d ASC);\n", "YB027")
        self.assertIn("Declare c, d NOT NULL", f.fix)
        self.assertIn("WHERE c IS NOT NULL AND d IS NOT NULL.", f.fix)


class TabletCounts(Case):
    def test_cluster_wide_counts_confirm_and_one_node_listings_stay_probable(self):
        ddl = ("CREATE TABLE big (id bigint NOT NULL, PRIMARY KEY ((id) HASH)) "
               "SPLIT INTO 1 TABLETS;\n")
        for tablets, confidence, fact in (
                ("schemaname,relname,num_tablets\npublic,big,1\n", "confirmed", ""),
                ("table_name,tablets\nbig,1\n", "probable", "one node")):
            with self.subTest(confidence):
                f = self.only(self.review(ddl=ddl, rows={"big": 5e7},
                                          files={"ybm_tablets.csv": tablets}), "SPL001")
                self.assertTrue(f["confidence"].startswith(confidence), f["confidence"])
                self.assertIn(fact, f["fact"])


class ReplaySettings(Case):
    def test_settings_without_release_facts_use_image_defaults_and_keep_cbo(self):
        b = self.bundle(ddl="CREATE TABLE t (id int PRIMARY KEY);\n",
                        files={"ybm_settings.csv": "name,setting\nyb_enable_cbo,on\n"})
        assumed = {}
        boot = {"yb_enable_cbo": "legacy_mode", "yb_enable_bitmapscan": "off",
                "yb_enable_base_scans_cost_model": "off",
                "yb_enable_optimizer_statistics": "off"}
        _, gucs = replay.settings_sql(b, "customer", None, assumed, boot)
        self.assertEqual(gucs["yb_enable_cbo"], "on")          # the bundle wins
        self.assertNotIn("yb_enable_optimizer_statistics", gucs)  # derived from cbo
        self.assertEqual(gucs["yb_enable_bitmapscan"], "off")
        self.assertEqual(sorted(assumed), ["yb_enable_bitmapscan"])
        self.assertIn("compiled default of the replay image", assumed["yb_enable_bitmapscan"])

    def test_replay_runs_end_to_end_against_a_stub_container(self):
        # No container runtime in tests: every docker call is stubbed, so this catches
        # errors in the replay code path itself.
        sent = []
        done = SimpleNamespace(stdout="", stderr="", returncode=0)

        class Stub:
            name = "stub"

            def __init__(self, image, name, keep=False, **flags):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def sql(self, text, db="ybm", flags=()):
                sent.append(text)
                return done

            def file_sql(self, path, db="ybm"):
                return done

            def mkdir(self, remote):
                pass

            def copy_out(self, remote, local):
                pass

        b = self.bundle(ddl="CREATE TABLE t (id bigint NOT NULL, PRIMARY KEY (id ASC));\n",
                        queries="SELECT * FROM t WHERE id = $1",
                        files={"ybm_meta.csv": "key,value\ncolocated,true\n"})
        with mock.patch.object(replay, "Container", Stub), \
                mock.patch.object(replay, "_sh", lambda *a, **k: done), \
                mock.patch.object(replay, "find_image",
                                  lambda *a: ("yugabytedb/yugabyte:9.9.9.9-b1", True)):
            res = replay.run(b)
        self.assertIn("CREATE DATABASE ybm WITH colocation = true;", sent)
        self.assertEqual([p["id"] for p in res["patterns"]], ["P1"])
        # A replay database that cannot be created stops the replay with the cause.
        failed = SimpleNamespace(stdout="", stderr="ERROR:  permission denied", returncode=1)
        Stub.sql = lambda self, text, db="ybm", flags=(): failed if "CREATE DATABASE" in text \
            else done
        with mock.patch.object(replay, "Container", Stub), \
                mock.patch.object(replay, "_sh", lambda *a, **k: done), \
                mock.patch.object(replay, "find_image",
                                  lambda *a: ("yugabytedb/yugabyte:9.9.9.9-b1", True)), \
                self.assertRaises(RuntimeError) as e:
            replay.run(b)
        self.assertIn("could not create the replay database: ERROR:  permission denied",
                      str(e.exception))

    def test_injected_statistics_name_quoted_tables_exactly(self):
        b = self.bundle(ddl='CREATE TABLE "Ev" ("Grp" bigint NOT NULL, PRIMARY KEY (("Grp") HASH));\n',
                        stats=["Ev,Grp,f,0,8,10,,,,0"], rows={"Ev": 100})
        sql, rel = replay.build_inject_sql(b, analyze.build_schema(b))
        self.assertIn("to_regclass('public.\"Ev\"'), 'Grp',", sql)
        self.assertIn("WHERE relname = 'Ev' AND", sql)
        self.assertEqual(rel["Ev"], 100.0)


class ReleaseSources(Case):
    """The release comes from wherever the bundle states it, in a fixed order of trust."""

    HEADER = ("--\n-- PostgreSQL database dump\n--\n\n"
              "-- Dumped from database version 15.12-YB-2025.2.3.0-b0\n"
              "-- Dumped by pg_dump version 15.12-YB-2026.1.1.0-b0\n\n")
    DDL = "CREATE TABLE t (id bigint NOT NULL, PRIMARY KEY ((id) HASH));\n"

    def test_dump_header_names_the_server_release(self):
        b = self.bundle(ddl=self.HEADER + self.DDL)
        self.assertEqual((b.version, b.version_source), ("2025.2.3.0", "the ysql_dump header"))

    def test_order_of_trust_and_disagreement(self):
        meta = "key,value\nversion,PostgreSQL 15.12-YB-2025.2.4.0-b0 on x86_64-pc-linux-gnu\n"
        d = self.tmpdir({"schema.sql": self.HEADER + self.DDL, "ybm_meta.csv": meta})
        res = analyze.run(inputs.load(d))
        self.assertEqual(res["version"], "2025.2.4.0")
        self.assertTrue(any(o.startswith("Release sources disagree") for o in res["open_items"]))
        b = inputs.load(d, release="2026.1.2.0-b5")
        self.assertEqual((b.version, b.version_source), ("2026.1.2.0", "the user (--release)"))

    def test_postgres_dump_names_no_release(self):
        res = self.review(ddl="-- Dumped from database version 15.4\n" + self.DDL)
        self.assertIsNone(res["version"])
        self.assertTrue(any("names no YugabyteDB release" in o for o in res["open_items"]))

    def test_cli_rejects_a_release_that_is_not_one(self):
        r = cli("preflight", self.tmpdir({"schema.sql": self.DDL}), "--release", "2026.1")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--release needs", r.stderr)


class QuotedNames(Case):
    """A quoted mixed-case name is not its lower-case spelling. Names in the bundle files are
    matched exactly, so such a name keeps its statistics (a hand-made file that differs from
    the schema only in case still matches), and a query that names it is understood."""

    def test_hash_bounds_and_conflict_targets_on_quoted_columns(self):
        res = self.review(
            ddl='CREATE TABLE "T" ("Id" bigint NOT NULL, "Em" text NOT NULL, v int, '
                'PRIMARY KEY (("Id") HASH));\nCREATE UNIQUE INDEX t_em ON "T" (("Em") HASH);\n',
            queries='SELECT v FROM "T" WHERE yb_hash_code("Id") >= 100 AND '
                    'yb_hash_code("Id") < 200;\n'
                    'INSERT INTO "T" ("Id", "Em", v) VALUES ($1, $2, $3) '
                    'ON CONFLICT ("Em") DO NOTHING;\n')
        self.assertEqual(res["patterns"][0]["access"][0]["path"], "T_pkey")
        self.assertFalse({"CAP003", "CAP060"} & self.rules(res), self.rules(res))

    def test_a_plan_binding_a_quoted_hash_column_is_not_a_partial_hash_scan(self):
        sch = schema.parse('CREATE TABLE u ("UserId" bigint NOT NULL, "TenantId" bigint NOT NULL, '
                           'PRIMARY KEY (("TenantId", "UserId") HASH));')
        scan = {"node": "Index Scan", "index": "u_pkey",
                "index_cond": '(("TenantId" = 7) AND ("UserId" = 42))'}
        self.assertFalse(plans.partial_hash_scan(scan, sch))
        self.assertTrue(plans.partial_hash_scan(dict(scan, index_cond='("UserId" = 42)'), sch))

    def test_quoted_mixed_case_names_keep_their_statistics(self):
        for label, table, col in (("plain", "s", "g"), ("quoted column", "s", '"G"'),
                                  ("quoted table", '"S"', "g")):
            with self.subTest(label):
                res = self.review(
                    ddl="CREATE TABLE %s (id bigint NOT NULL, %s bigint NOT NULL, "
                        "PRIMARY KEY ((id) HASH));\nCREATE INDEX s_g ON %s ((%s) HASH);\n"
                        % (table, col, table, col),
                    stats=["%s,%s,f,0,8,5000,{7},{0.02},,0" % (table.strip('"'), col.strip('"'))],
                    rows={table.strip('"'): 5e9})
                self.assertEqual(self.only(res, "STA003")["severity"], "high")

    def test_names_that_differ_only_in_case_match_the_schema(self):
        b = self.bundle(ddl='CREATE TABLE orders (id bigint NOT NULL, "Ref" text, '
                            'PRIMARY KEY ((id) HASH));\n',
                        stats=["ORDERS,ID,f,0,8,-1,,,,0", "orders,ref,f,0.5,8,10,,,,0"],
                        rows={"Orders": 10})
        sch = analyze.build_schema(b)
        self.assertEqual(sorted(b.stats), [("orders", "Ref"), ("orders", "id")])
        self.assertEqual(b.reltuples, {"orders": 10.0})
        b.align_names(sch)  # aligning twice changes nothing
        self.assertEqual(sorted(b.stats), [("orders", "Ref"), ("orders", "id")])

    def test_distinct_quoted_names_are_not_merged(self):
        b = self.bundle(ddl='CREATE TABLE t (id bigint NOT NULL, v int, "V" int, '
                            'PRIMARY KEY ((id) HASH));\n',
                        stats=["t,v,f,0.1,8,10,,,,0", "t,V,f,0.9,8,10,,,,0"])
        analyze.build_schema(b)
        self.assertEqual((b.stats[("t", "v")]["null_frac"], b.stats[("t", "V")]["null_frac"]),
                         (0.1, 0.9))
        self.assertEqual(b.stats_duplicates, [])


class DuplicateStats(Case):
    """A column that ybm_pg_stats.csv lists twice (nodes that disagree) is recorded in
    stats_duplicates, and one row is kept: the one with the most nodes_reporting when the file
    has that column (a tie keeps the first), else the first."""

    DDL = "CREATE TABLE t (id bigint NOT NULL, v text, PRIMARY KEY ((id) HASH));\n"
    HEAD = "schemaname,tablename,attname,inherited,null_frac"

    def load(self, rows, nodes=False):
        """Rows are 'table,column,inherited,null_frac' plus ',nodes_reporting' when nodes."""
        return self.bundle(ddl=self.DDL, files={"ybm_pg_stats.csv": (
            self.HEAD + (",nodes_reporting" if nodes else "") + "\n" +
            "".join("public,%s\n" % r for r in rows))})

    def test_the_row_kept(self):
        two = [("t", "v", 2)]
        for label, rows, nodes, kept, dups in (
                ("without nodes_reporting, the first", ["t,v,f,0.1", "t,v,f,0.2"], False, 0.1, two),
                ("the later row has more nodes", ["t,v,f,0.1,1", "t,v,f,0.2,2"], True, 0.2, two),
                ("the earlier row has more nodes", ["t,v,f,0.1,3", "t,v,f,0.2,1"], True, 0.1, two),
                ("counts compare as numbers", ["t,v,f,0.1,9", "t,v,f,0.2,10"], True, 0.2, two),
                ("a tie keeps the first of the tied", ["t,v,f,0.1,1", "t,v,f,0.2,3", "t,v,f,0.3,3"],
                 True, 0.2, [("t", "v", 3)]),
                ("an empty count loses", ["t,v,f,0.1,", "t,v,f,0.2,2"], True, 0.2, two),
                ("nan loses", ["t,v,f,0.1,nan", "t,v,f,0.2,1"], True, 0.2, two),
                ("n/a loses", ["t,v,f,0.1,2", "t,v,f,0.2,n/a"], True, 0.1, two),
                ("none usable: the first", ["t,v,f,0.1,", "t,v,f,0.2,n/a"], True, 0.1, two),
                ("inherited rows are not counted", ["t,v,t,0.9", "t,v,f,0.1"], False, 0.1, [])):
            with self.subTest(label):
                b = self.load(rows, nodes)
                self.assertEqual(b.stats[("t", "v")]["null_frac"], kept)
                self.assertEqual(b.stats_duplicates, dups)

    def test_duplicates_are_sorted_and_count_every_row(self):
        b = self.load(["t,v,f,0.1", "t,id,f,0"])
        self.assertEqual(b.stats_duplicates, [])
        self.assertEqual(sorted(b.stats), [("t", "id"), ("t", "v")])
        self.assertEqual(self.bundle(ddl=self.DDL).stats_duplicates, [])
        # T.V is another column (a quoted name), not a second row for t.v.
        b = self.load(["t,v,f,0.1", "a,z,f,0.1", "T,V,f,0.2", "t,v,f,0.3", "a,z,f,0.2",
                       "t,c,f,0", "t,c,f,0", "b,x,f,0"])
        self.assertEqual(b.stats_duplicates, [("a", "z", 2), ("t", "c", 2), ("t", "v", 2)])


class SafeRecommendations(Case):
    """Recommendations YSQL can run, that keep what the original guaranteed."""

    SOFT_DELETE = ("CREATE TABLE r (id bigint NOT NULL, content_hash text, publisher_id bigint, "
                   "deleted_at timestamp, PRIMARY KEY ((id) HASH));\n"
                   "CREATE INDEX r_pub ON r (publisher_id HASH);\n"
                   "CREATE UNIQUE INDEX r_ch ON r (content_hash HASH, publisher_id ASC) WHERE "
                   "((content_hash IS NOT NULL) AND (deleted_at IS NULL));\n")
    COLOCATED = {"ybm_meta.csv": "key,value\ncolocated,true\n"}

    def test_replacement_keeps_unique_include_predicate_and_split(self):
        s = schema.parse('CREATE TABLE t (a int, b int, "C" int, PRIMARY KEY ((a) HASH));\n'
                         'CREATE UNIQUE INDEX i ON t (b HASH) INCLUDE ("C") SPLIT INTO 8 TABLETS '
                         'WHERE ("C" IS NOT NULL);')
        i = s.indexes["i"]
        ddl = schema.index_sql(i, "i_cov", add_include=["a"])
        self.assertTrue(ddl.startswith("CREATE UNIQUE INDEX CONCURRENTLY i_cov ON t"))
        self.assertIn('INCLUDE ("C", a) SPLIT INTO 8 TABLETS WHERE ("C" IS NOT NULL)', ddl)
        self.assertEqual(schema.qi("order"), '"order"')   # reserved words stay quoted
        self.assertEqual(schema.qi("created_at"), "created_at")
        bkt = schema.index_sql(i, "i_bkt", keys="(yb_hash_code(b) % 16) ASC, b ASC",
                               split="SPLIT AT VALUES ((1))", partitioned=True)
        self.assertTrue(bkt.startswith("CREATE UNIQUE INDEX i_bkt"))  # no CONCURRENTLY
        self.assertIn("SPLIT AT VALUES ((1)) WHERE", bkt)
        self.assertEqual(schema.drop_index_sql("i", "i_cov"), "DROP INDEX i;  -- after i_cov is valid")
        # INCLUDE, then NULLS NOT DISTINCT, then SPLIT and WHERE (gram.y IndexStmt)
        n = schema.parse("CREATE TABLE t (a int, b int, c int, PRIMARY KEY ((a) HASH));\n"
                         "CREATE UNIQUE INDEX n ON t (b HASH) INCLUDE (c) NULLS NOT DISTINCT "
                         "SPLIT INTO 4 TABLETS;").indexes["n"]
        self.assertIn("INCLUDE (c, a) NULLS NOT DISTINCT SPLIT INTO 4 TABLETS;",
                      schema.index_sql(n, "n_cov", add_include=["a"]))

    def test_a_rebuilt_expression_keeps_its_string_literals(self):
        s = schema.parse("CREATE TABLE t (a text, b text, PRIMARY KEY ((a) HASH));\n"
                         "CREATE UNIQUE INDEX u ON t ((lower(a || ', ' || b)) HASH) WHERE "
                         "(b <> ' (x)');")
        ddl = schema.index_sql(s.indexes["u"], "u_cov", add_include=["a"])
        self.assertIn("lower(a || ', ' || b)", ddl)
        self.assertIn("' (x)'", ddl)
        self.assertEqual(expr_of(tokenize('lower("First, Name")')), 'lower("First, Name")')

    def test_prose_never_offers_a_unique_index_for_removal(self):
        # STA004 on a unique timestamp-led index: bucket it, but no "drop it" clause.
        ddl = ("CREATE TABLE ev (id bigint NOT NULL, created_at timestamptz NOT NULL, k int, "
               "PRIMARY KEY ((id) HASH));\n")
        for unique, says in (("", True), ("UNIQUE ", False)):
            with self.subTest(unique=bool(unique)):
                f = self.only(self.review(ddl=ddl + "CREATE %sINDEX ev_created ON ev "
                                              "(created_at ASC, k ASC);\n" % unique,
                                          rows={"ev": 1000000}), "STA004")
                self.assertNotIn("drop it", f["fix"])  # a reason; the drop is an option
                self.assertEqual(any(o["id"] == "drop" for o in f["options"]), says)
        # WRK004 on a write-hot table: the unique index is kept by name, never in the list of
        # indexes the fix offers to drop.
        ddl = ("CREATE TABLE w (id bigint NOT NULL, a int, b int, c int, d int, "
               "PRIMARY KEY ((id) HASH));\nCREATE UNIQUE INDEX w_a ON w (a HASH);\n"
               "CREATE INDEX w_b ON w (b HASH);\nCREATE INDEX w_c ON w (c HASH);\n"
               "CREATE INDEX w_d ON w (d HASH);\n")
        f = self.only(self.review(ddl=ddl, rows={"w": 1e6}, pss=PSS_HEAD +
                                  "1,900000,9000,0.01,900000,INSERT INTO w VALUES ($1,$2,$3,$4,$5)"
                                  "\n"), "WRK004")
        self.assertTrue(f["fix"].startswith("w_a is unique and enforces a constraint, so no "
                                            "option removes it. Of w_b, w_c, w_d, the index "
                                            "findings"), f["fix"])
        self.assertNotIn("checked DDL", f["fix"])  # IDX001 carries none
        for uniq in ("UNIQUE ", ""):  # every index unique, or none: both texts are reasons
            res = self.review(ddl=ddl.replace("CREATE INDEX", "CREATE %sINDEX" % uniq),
                              rows={"w": 1e6}, pss=PSS_HEAD + "1,900000,9000,0.01,900000,"
                              "INSERT INTO w VALUES ($1,$2,$3,$4,$5)\n")
            self.assertIn("WRK004", self.rules(res))
            self.assertEqual(prescriptions(res), [])

    def test_bucketed_index_keeps_every_key_and_include(self):
        f = self.only(self.review(
            ddl="CREATE TABLE ev (id bigint NOT NULL, created_at timestamptz NOT NULL, k int, "
                "PRIMARY KEY ((id) HASH));\n"
                "CREATE INDEX ev_created ON ev (created_at ASC, k ASC) INCLUDE (id);\n",
            rows={"ev": 1000000}), "STA004")
        self.assertIn("(yb_hash_code(created_at) % 16) ASC, created_at ASC, k ASC", f["ddl"])
        self.assertIn("INCLUDE (id)", f["ddl"])
        self.assertIn("DROP INDEX ev_created;", f["ddl"])

    def test_unique_constraint_is_dropped_with_alter_table(self):
        for label, ddl in (
                ("inline", "CREATE TABLE u (id int, e text, PRIMARY KEY ((id) HASH), "
                           "CONSTRAINT u_email UNIQUE (e));\n"),
                ("added with columns", "CREATE TABLE u (id int, e text, PRIMARY KEY ((id) HASH));\n"
                                       "ALTER TABLE ONLY u ADD CONSTRAINT u_email UNIQUE (e);\n"),
                # ysql_dump writes a unique constraint as its index plus USING INDEX
                # (pg_dump.c), which also renames the index to the constraint's name.
                ("ysql_dump: USING INDEX", "CREATE TABLE u (id int, e text, PRIMARY KEY ((id) HASH));\n"
                 "CREATE UNIQUE INDEX NONCONCURRENTLY u_e_idx ON public.u USING lsm (e HASH);\n"
                 "ALTER TABLE ONLY public.u\n    ADD CONSTRAINT u_email UNIQUE USING INDEX u_e_idx;\n")):
            with self.subTest(label):
                s = schema.parse(ddl)
                self.assertEqual(sorted(i.name for i in s.indexes_on("u")), ["u_email", "u_pkey"])
                idx = s.indexes["u_email"]
                self.assertTrue(idx.constraint)
                sql = schema.drop_sql(idx, "u_email_v2")
                self.assertTrue(sql.startswith("ALTER TABLE u DROP CONSTRAINT u_email;"))
                self.assertEqual([c.as_tuple() for c in safety.changes_of(sql)],
                                 [("drop", "u_email")])

    def test_a_dumped_unique_constraint_is_never_dropped_as_an_index(self):
        res = self.review(
            ddl="CREATE TABLE public.customers (id bigint NOT NULL, email text, "
                "CONSTRAINT customers_pkey PRIMARY KEY ((id) HASH));\n"
                "CREATE UNIQUE INDEX NONCONCURRENTLY customers_email_key ON public.customers "
                "USING lsm (email HASH);\nALTER TABLE ONLY public.customers\n"
                "    ADD CONSTRAINT customers_email_key UNIQUE USING INDEX customers_email_key;\n",
            rows={"customers": 1000000}, stats=[("customers", "email", 0.5, 1000, [], None)])
        f = self.only(res, "STA001")
        self.assertIn("ALTER TABLE customers DROP CONSTRAINT customers_email_key;", f["ddl"])
        self.assertNotIn("DROP INDEX customers_email_key", f["ddl"])

    def test_a_unique_hash_key_says_what_each_option_does_to_its_guarantee(self):
        ddl = ("CREATE TABLE s (id bigint NOT NULL, t int, e text, PRIMARY KEY ((id) HASH));\n"
               "CREATE UNIQUE INDEX s_te ON s (t HASH, e ASC);\n")
        skewed = self.only(self.review(ddl=ddl, rows={"s": 1e7}, stats=[
            "s,t,f,0,4,50,{7},{0.4},,0"]), "STA003")
        self.assertIn("only the wider key unique", skewed["fix"])
        self.assertIn("separate_hot_value", skewed["fix"])
        self.assertFalse(any(o["id"] == "drop" for o in skewed["options"]))
        few = self.only(self.review(ddl=ddl, rows={"s": 1e7},
                                    stats=[("s", "t", 0, 3, [], 0.0)]), "STA002")
        self.assertIn("only the wider key unique", few["fix"])
        self.assertNotIn("separate_hot_value", few["fix"])  # STA002 does not offer it

    def test_skewed_primary_key_is_never_dropped(self):
        f = self.only(self.review(
            ddl="CREATE TABLE od (offer_id bigint NOT NULL, dept bigint NOT NULL, "
                "PRIMARY KEY ((offer_id) HASH, dept ASC));\n",
            rows={"od": 1000000}, stats=[("od", "offer_id", 0, 300, [], 0.0)]), "STA002")
        self.assertIn("cannot be dropped", f["fix"])
        self.assertIn("only the wider key would be", f["fix"])  # what composite does to it
        self.assertNotIn("Drop the index", f["fix"])
        self.assertEqual(f["basis_inputs"], ["the schema DDL", "pg_stats", "row counts"])

    def test_never_set_columns_leave_out_used_rare_and_event_columns(self):
        f = self.only(self.review(
            ddl="CREATE TABLE c (id bigint NOT NULL, a text, b text, ended_at timestamp, "
                "flag text, PRIMARY KEY ((id) HASH));\n"
                "CREATE INDEX c_active ON c (id ASC) WHERE (ended_at IS NULL);\n"
                "CREATE INDEX c_flag ON c (flag HASH);\n",
            rows={"c": 100000000},
            stats=[("c", "a", 1.0, 0, [], None), ("c", "b", 0.9996, 10, [], None),
                   ("c", "ended_at", 1.0, 0, [], None), ("c", "flag", 1.0, 0, [], None),
                   ("c", "id", 0, -1, [], 0.0)]), "STA006")
        listed = f["fact"].split("excluded): ", 1)[1].split(". Not counted", 1)[0]
        self.assertIn("c.a", listed)
        for col in ("c.b", "c.flag", "c.ended_at"):
            self.assertNotIn(col, listed)
        self.assertIn("c.b (~40k non-NULL rows)", f["fact"])
        self.assertIn("referenced by an index", f["fact"])
        self.assertIn("drops every index", f["fix"])

    def test_correlation_alone_never_makes_a_range_key_monotonic(self):
        spread = [10 ** 15 + i * (9.2 * 10 ** 18 - 10 ** 15) / 100 for i in range(101)]
        res = self.review(
            ddl="CREATE TABLE w (facet_id bigint NOT NULL, x int, PRIMARY KEY (facet_id ASC)) "
                "SPLIT AT VALUES ((1000), (2000));\n"
                "CREATE TABLE s (id bigint DEFAULT nextval('s_id_seq'::regclass) NOT NULL, "
                "v int, PRIMARY KEY (id ASC));\n"
                "CREATE TABLE h (id bigint NOT NULL, code int, PRIMARY KEY ((id) HASH));\n"
                "CREATE INDEX h_code ON h (code ASC);\n",
            rows={"w": 300000000, "s": 1000000, "h": 1000000},
            stats=[("w", "facet_id", 0, -1, spread, 1.0),
                   ("s", "id", 0, -1, list(range(1, 1000001, 10000)), 1.0),
                   ("h", "code", 0, 5000, [], 0.95)])
        sta4 = {f["object"]: f for f in res["findings"] if f["rule"] == "STA004"}
        self.assertNotIn("w_pkey(facet_id)", sta4)   # random ids on a pre-split range key
        self.assertNotIn("h_code(code)", sta4)       # correlation is noise on a hash table
        self.assertIn("sequence", sta4["s_pkey(id)"]["fact"])

    def test_soft_delete_predicate_is_implied_and_a_better_partial_index_is_named(self):
        q1 = ("SELECT * FROM r WHERE content_hash = $1 AND publisher_id = $2 AND deleted_at IS "
              "NULL ORDER BY id LIMIT 10")
        res = self.review(ddl=self.SOFT_DELETE, queries=q1)
        acc = res["patterns"][0]["access"][0]
        self.assertEqual((acc["path"], acc["point"]), ("r_ch", True))
        self.assertNotIn("CAP010", self.rules(res))
        cap21 = self.only(self.review(ddl=self.SOFT_DELETE,
                                      queries=q1.replace(" AND deleted_at IS NULL", "")), "CAP021")
        self.assertIn("r_ch", cap21["fact"])

    def test_primary_key_swap_on_a_colocated_table_uses_range_keys(self):
        pss = PSS_HEAD + '1,1000000,5000,0.005,1000000,"SELECT * FROM acct WHERE ext = $1"\n'
        f = self.only(self.review(
            ddl="CREATE TABLE acct (id bigint NOT NULL, ext text NOT NULL, v text, "
                "PRIMARY KEY (id ASC));\nCREATE UNIQUE INDEX acct_ext ON acct (ext ASC);\n",
            pss=pss, files=self.COLOCATED), "CAP051")
        self.assertIn("ON acct (id ASC);", f["ddl"])
        self.assertEqual(re.findall(r"\bHASH\b|\bSPLIT\b", f["ddl"]), [], f["ddl"])

    def test_a_key_swap_never_drops_the_old_index_as_ddl(self):
        # After a table swap the old index belongs to the retired table; before it, dropping it
        # removes the access path and fails while a foreign key is built on it.
        ddl = ("CREATE TABLE p (id bigint NOT NULL, code text NOT NULL, name text, "
               "PRIMARY KEY ((id) HASH));\nCREATE UNIQUE INDEX p_code ON p (code HASH);\n"
               "CREATE TABLE c (id bigint NOT NULL, code text, PRIMARY KEY ((id) HASH), "
               "CONSTRAINT c_code_fkey FOREIGN KEY (code) REFERENCES p(code));\n"
               "CREATE TABLE q (id bigint NOT NULL, g bigint NOT NULL, PRIMARY KEY ((id) HASH));\n"
               "CREATE INDEX q_g ON q (g HASH);\n")
        pss = (PSS_HEAD + '1,1000000,5000,0.005,1000000,"SELECT * FROM p WHERE code = $1"\n'
               '2,1000000,5000,0.005,1000000,"SELECT * FROM q WHERE g = $1"\n')
        res = self.review(ddl=ddl, pss=pss)
        swaps = [f for f in res["findings"] if f["rule"] in ("CAP050", "CAP051")]
        self.assertEqual(sorted(f["rule"] for f in swaps), ["CAP050", "CAP051"])
        for f in swaps:
            runnable = "\n".join(l for l in f["ddl"].split("\n") if not l.startswith("--"))
            self.assertNotIn("DROP INDEX", runnable, f["ddl"])
            self.assertIn("goes when the old table is dropped", f["ddl"])

    def test_a_key_swap_still_retires_a_constraint_a_statement_names(self):
        res = self.review(
            ddl="CREATE TABLE p (id bigint NOT NULL, code text NOT NULL, v int, "
                "PRIMARY KEY ((id) HASH), CONSTRAINT p_code UNIQUE (code));\n",
            pss=PSS_HEAD + '1,1000000,5000,0.005,1000000,"SELECT * FROM p WHERE code = $1"\n'
                '2,100000,500,0.005,100000,"INSERT INTO p (id, code, v) VALUES ($1, $2, $3) '
                'ON CONFLICT ON CONSTRAINT p_code DO UPDATE SET v = excluded.v"\n')
        swap = next(o for o in self.only(res, "CAP051")["options"] if o["id"] == "swap_keys")
        self.assertTrue(swap["checked"])
        self.assertTrue(any("loses its ON CONFLICT target" in c for c in swap["costs"]),
                        swap["costs"])

    def test_an_option_that_loses_uniqueness_carries_the_cost_not_an_amendment(self):
        sch = schema.parse("CREATE TABLE t (a int, b int, PRIMARY KEY (a ASC));\n"
                           "CREATE UNIQUE INDEX u ON t (b ASC);\n")
        f, opt = annotated(sch, "WRK003", "DROP INDEX u;", "u", index="u")
        self.assertEqual(f.ddl, "DROP INDEX u;")  # not amended
        self.assertEqual(opt["costs"], ["loses the unique guarantee of u (b)"])

    def test_a_null_guard_on_a_unique_index_needs_no_amendment(self):
        res = self.review(ddl="CREATE TABLE t (id bigint NOT NULL, em text, st text, "
                              "PRIMARY KEY ((id) HASH));\n"
                              "CREATE UNIQUE INDEX t_em ON t ((em) HASH) WHERE (st = 'a');\n",
                          rows={"t": 1000000}, stats=[("t", "em", 0.5, 1000, [], None)])
        f = self.only(res, "STA001")
        self.assertIn("WHERE ((st = 'a')) AND (em IS NOT NULL)", f["ddl"])
        self.assertNotIn("added by the safety check", f["ddl"])
        self.assertNotIn("SAF001", self.rules(res))

    def test_nulls_not_distinct_unique_keys(self):
        res = self.review(
            ddl="CREATE TABLE devices (id bigint NOT NULL, serial text, PRIMARY KEY ((id) HASH));\n"
                "ALTER TABLE ONLY devices ADD CONSTRAINT devices_serial_key UNIQUE NULLS NOT "
                "DISTINCT (serial);\n"
                "CREATE TABLE users (id bigint NOT NULL, ext_ref text, PRIMARY KEY ((id) HASH));\n"
                "CREATE UNIQUE INDEX users_ext_ref ON users (ext_ref HASH) NULLS NOT DISTINCT;\n",
            queries="INSERT INTO devices (id, serial) VALUES ($1, $2) "
                    "ON CONFLICT (serial) DO NOTHING",
            rows={"users": 1000000}, stats=[("users", "ext_ref", 0.5, 1000, [], None)])
        # The constraint is the arbiter; a NULL key conflicts, so neither the NULL-hotspot
        # rebuild nor the "NULL is distinct" notes apply.
        self.assertFalse({"CAP060", "STA001", "STA005", "LINT-YB027"} & self.rules(res),
                         self.rules(res))
        # nor does the safety pass call a rebuild of that index a NULL hotspot brought back
        sch = schema.parse("CREATE TABLE users (id bigint NOT NULL, ext_ref text, "
                           "PRIMARY KEY ((id) HASH));\nCREATE UNIQUE INDEX u2 ON users "
                           "(ext_ref HASH) NULLS NOT DISTINCT;\n")
        b = self.bundle(ddl="CREATE TABLE users (id bigint);\n",
                        stats=[("users", "ext_ref", 0.5, 1000, [], None)])
        self.assertEqual(safety.reintroduced(sch, ["u2"], b, analyze), [])

    def test_an_index_a_foreign_key_depends_on(self):
        # A foreign key depends on the unique index it was created against, so that index can
        # only be dropped after the key. A replacement that can back the key (unique, not
        # partial, on the same columns) re-points it; otherwise the DDL is withheld.
        ddl = ("CREATE TABLE p (id int, code text, PRIMARY KEY (id HASH));\n"
               "CREATE UNIQUE INDEX p_code ON p (code HASH);\n"
               "CREATE TABLE c (id int, code text, PRIMARY KEY (id HASH));\n"
               "ALTER TABLE ONLY c ADD CONSTRAINT c_code_fkey FOREIGN KEY (code) "
               "REFERENCES p(code) ON DELETE CASCADE;\n")
        cov = ("CREATE UNIQUE INDEX CONCURRENTLY p_code_cov ON p ((code) HASH) INCLUDE (id);\n"
               "DROP INDEX p_code;  -- after p_code_cov is valid")
        for label, rule, change, kept in (
                ("covering rebuild", "CAP020", cov, True),
                ("partial rebuild", "STA001", cov.replace("INCLUDE (id)", "WHERE code IS NOT NULL"),
                 False),
                ("plain drop", "WRK003", "DROP INDEX p_code;", False)):
            with self.subTest(label):
                sch = schema.parse(ddl)
                f, opt = annotated(sch, rule, change, "p_code", table="p", index="p_code")
                if kept:
                    self.assertEqual(f.ddl.split("\n"), [
                        "CREATE UNIQUE INDEX CONCURRENTLY p_code_cov ON p ((code) HASH) INCLUDE (id);",
                        "ALTER TABLE c DROP CONSTRAINT c_code_fkey;",
                        "DROP INDEX p_code;  -- after p_code_cov is valid",
                        "ALTER TABLE c ADD CONSTRAINT c_code_fkey FOREIGN KEY (code) REFERENCES "
                        "p(code) ON DELETE CASCADE NOT VALID;",
                        "ALTER TABLE c VALIDATE CONSTRAINT c_code_fkey;"])
                else:
                    self.assertIsNone(f.ddl)
                    self.assertIn("foreign key c_code_fkey on c depends on p_code, and nothing "
                                  "in this option can back it", opt["costs"])

    def test_rebuilds_of_an_index_a_foreign_key_depends_on_are_not_merged_into_a_partial_one(self):
        res = self.review(
            ddl="CREATE TABLE customers (id bigint NOT NULL, email text, name text, "
                "PRIMARY KEY ((id) HASH));\n"
                "CREATE UNIQUE INDEX customers_email ON customers (email HASH);\n"
                "CREATE TABLE orders (id bigint NOT NULL, customer_email text NOT NULL, "
                "PRIMARY KEY ((id) HASH));\nALTER TABLE ONLY orders ADD CONSTRAINT "
                "orders_customer_email_fkey FOREIGN KEY (customer_email) "
                "REFERENCES customers(email);\n",
            queries="SELECT name FROM customers WHERE email = $1",
            rows={"customers": 1000000}, stats=[("customers", "email", 0.5, 1000, [], None)])
        self.assertIsNone(self.only(res, "STA001")["ddl"])  # partial: cannot back the key
        cov = self.only(res, "CAP020")["ddl"]
        self.assertIn("ALTER TABLE orders DROP CONSTRAINT orders_customer_email_fkey;\n"
                      "DROP INDEX customers_email;", cov)
        self.assertIn("ALTER TABLE orders VALIDATE CONSTRAINT orders_customer_email_fkey;", cov)

    def test_a_table_swap_names_the_foreign_keys_to_move(self):
        res = self.review(
            ddl="CREATE TABLE ev (id bigint DEFAULT nextval('ev_id_seq'::regclass) NOT NULL, "
                "v int, PRIMARY KEY (id ASC));\n"
                "CREATE TABLE note (id bigint NOT NULL, ev_id bigint, PRIMARY KEY ((id) HASH));\n"
                "ALTER TABLE ONLY note ADD CONSTRAINT note_ev_fkey FOREIGN KEY (ev_id) "
                "REFERENCES ev(id);\n", rows={"ev": 1000000},
            stats=[("ev", "id", 0, -1, list(range(1, 1000001, 10000)), 1.0)])
        f = self.only(res, "STA004")
        self.assertIn("-- foreign keys that reference ev (note_ev_fkey on note) must be dropped "
                      "before the swap and added back after it", f["ddl"])

    def test_duplicated_stats_rows_are_an_open_item(self):
        res = self.review(ddl="CREATE TABLE d (id bigint NOT NULL, a int, PRIMARY KEY ((id) HASH));\n",
                          stats=[("d", "a", 0.1, 10, [], None), ("d", "a", 0.2, 20, [], None)])
        self.assertTrue(any("more than once" in o and "d.a (2 rows)" in o
                            for o in res["open_items"]))


class PlanFindingFixes(Case):
    """A plan-only finding (PLN001-003) fires because no static finding predicted the plan, so
    its fix says what the planner chose. It must not point at a finding that is absent."""

    EV = ("CREATE TABLE ev (id bigint NOT NULL, grp bigint NOT NULL, ts timestamptz NOT NULL, "
          "PRIMARY KEY ((id) HASH));\n"
          "CREATE INDEX ev_grp_ts ON ev (grp HASH, ts DESC);\n")
    SEQ_R = {"Node Type": "Seq Scan", "Relation Name": "r"}

    @staticmethod
    def sort_over(scan):
        return {"Node Type": "Limit", "Plans": [{"Node Type": "Sort", "Plans": [scan]}]}

    def test_sort_finding_names_the_scan_the_planner_used(self):
        # The index leads with grp and ends in ts DESC, so the ORDER BY is served on paper and
        # no static rule predicts a Sort. The replayed plan sorts anyway.
        scan = {"Node Type": "Index Scan", "Relation Name": "ev", "Index Name": "ev_grp_ts"}
        res = self.replay(self.EV, "SELECT ts FROM ev WHERE grp = $1 ORDER BY ts DESC LIMIT 20",
                          self.sort_over(scan), {"ev": 5000})
        f = self.only(res, "PLN002")
        self.assertEqual((f["object"], f["fact"]),
                         ("P1 sort", "P1: replayed plan sorts before LIMIT."))
        self.assertNotIn("See CAP010", f["fix"])
        self.assertIn("the replayed plan for P1 uses Index Scan using ev_grp_ts.", f["fix"])
        self.assertNotIn("Other findings", f["fix"])   # nothing else is on P1
        self.assertTrue(f["fix"].startswith(           # the headline quotes the first sentence
            "An index led by the equality columns, then the ORDER BY columns in the requested "
            "direction, would return the rows in order without the sort."))
        self.assertEqual(prescriptions(res), [])
        self.assertNotIn("See CAP010", report.render(res))

    def test_fix_names_the_other_findings_on_the_pattern(self):
        # No index leads with k, so CAP002 predicts a scan. Nothing predicts the Sort.
        res = self.replay(ReplayAuthority.DDL, "SELECT id FROM r WHERE k = $1 ORDER BY id LIMIT 5",
                          self.sort_over(self.SEQ_R), {"r": 1e7})
        static = [f for f in res["findings"]
                  if "P1" in f["patterns"] and not f["rule"].startswith("PLN")]
        self.assertIn("CAP002", {f["rule"] for f in static})
        fix = self.only(res, "PLN002")["fix"]
        for f in static:
            self.assertIn("%s (%s)" % (f["rule"], f["object"]), fix)
        self.assertIn("the replayed plan for P1 uses Seq Scan. Other findings on P1: "
                      "CAP002 (r(k)).", fix)
        self.assertTrue(fix.startswith("An index led by"))
        self.assertEqual(prescriptions(res), [])

    def test_other_findings_are_sorted_capped_and_leave_out_what_the_report_drops(self):
        col = analyze.Collector()
        for rule, obj, pats in (("CAP020", "a on t", ["P1"]), ("CAP013", "b on t", ["P1"]),
                                ("CAP012", "c on t", ["P1", "P2"]), ("CAP002", "t(d)", ["P1"]),
                                ("CAP031", "t(e)", ["P1"]),
                                ("CAP010", "f on t", ["P2"]),        # another pattern
                                ("WRK001", "P1", ["P1"]),            # folded into Measured
                                ("PLN001", "P1 scan of t", ["P1"])):  # plan-only
            col.add(analyze.Finding(rule, "medium", "probable", obj, "fact", pats))
        fix = plans._plan_fix("PLN002", "P1", [], col)
        self.assertIn("the replayed plan for P1 has no scan of a named table.", fix)
        self.assertIn("Other findings on P1: CAP002 (t(d)), CAP012 (c on t), CAP013 (b on t), "
                      "CAP020 (a on t), and 1 more.", fix)
        for left_out in ("CAP010", "WRK001", "PLN001"):
            self.assertNotIn(left_out, fix)

    def test_scan_finding_fix(self):
        res = self.replay(ReplayAuthority.DDL, "SELECT id FROM r WHERE id = $1", self.SEQ_R,
                          {"r": 1e7})
        self.assertEqual(self.only(res, "PLN001")["fix"],
                         "An index that serves this pattern's predicates would avoid this scan. "
                         "No static finding predicted this: the replayed plan for P1 uses Seq "
                         "Scan.")

    def test_append_finding_fix_counts_the_repeated_scan(self):
        parts = "".join("CREATE TABLE ev_%d PARTITION OF ev FOR VALUES FROM (%d) TO (%d);\n" %
                        (i, i, i + 1) for i in (1, 2, 3))
        ddl = ("CREATE TABLE ev (id bigint NOT NULL, ts bigint NOT NULL, v text, "
               "PRIMARY KEY ((id) HASH, ts ASC)) PARTITION BY RANGE (ts);\n" + parts)
        root = {"Node Type": "Append",
                "Plans": [{"Node Type": "Seq Scan", "Relation Name": "ev_%d" % i}
                          for i in (1, 2, 3)]}
        # A pattern that binds the partition key reads what its own range spans, and one that
        # filters nothing reads every partition by design: no PLN003 for either.
        for q in ("SELECT v FROM ev WHERE id = $1 AND ts > $2", "SELECT v FROM ev"):
            self.assertNotIn("PLN003", self.rules(self.replay(ddl, q, root, {"ev": 1000})))
        facts = {"scans": [{"node": "Seq Scan", "relation": "ev_%d" % i, "table": "ev",
                            "index": None} for i in (1, 2, 3)]}
        fix = plans._plan_fix("PLN003", "P1", facts["scans"], analyze.Collector())
        self.assertNotIn("See CAP030", fix)
        self.assertIn("the replayed plan for P1 uses Seq Scan x3.", fix)
        self.assertTrue(fix.startswith(
            "A predicate on the partition key in the query would let the planner prune."))

    def test_rejected_partial_index_survives_a_replay_on_the_weaker_index(self):
        # CAP021 names a partial index the query cannot prove; replay confirms it while the
        # planner uses another index, and would refute it only by using that partial index.
        q = "SELECT id FROM r WHERE content_hash = $1 AND publisher_id = $2"
        weaker = {"Node Type": "Index Scan", "Relation Name": "r", "Index Name": "r_pub"}
        f = self.only(self.replay(SafeRecommendations.SOFT_DELETE, q, weaker, {"r": 1e7}),
                      "CAP021")
        self.assertTrue(f["confidence"].startswith("confirmed"), f["confidence"])
        used = {"Node Type": "Index Scan", "Relation Name": "r", "Index Name": "r_ch"}
        res = self.replay(SafeRecommendations.SOFT_DELETE, q, used, {"r": 1e7})
        self.assertNotIn("CAP021", self.rules(res))


if __name__ == "__main__":
    unittest.main()
