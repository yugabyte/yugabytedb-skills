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

from ybm import (analyze, catalog, fixpoint, inputs, plans, preflight, probes,  # noqa: E402
                 replay, report, safety, schema, sqlshape, versions)
from ybm.sqltok import fingerprint, split_statements, tokenize  # noqa: E402

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

    def replay(self, ddl, query, root, rows, assumed=None, **match):
        """The review of `query` (P1) when a replay on the customer's release planned it with
        `root` at the top of the plan; `match` overrides fields of the version match."""
        plans = {"version": "9.9.9.9-b1", "mode": "customer", "reltuples": rows,
                 "patterns": [{"id": "P1", "query": query, "plan": [{"Plan": root}]}],
                 "assumed_settings": assumed or {},
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

    def test_rejected_statements_fall_back_to_the_ddl_text(self):
        sch = catalog.build({"tables": [], "indexes": [], "fks": []}, failed=[
            ("CREATE TABLE t (id bigint, PRIMARY KEY (id HASH))", 'type "x" does not exist')])
        self.assertIn("t", sch.tables)
        self.assertTrue(any("rejected" in n for n in sch.parse_notes))
        # The fallback follows the bundle's sharding default and the database's colocation.
        for cat, hd in (({"db_colocated": True}, True), ({}, False)):
            sch = catalog.build(dict(cat, tables=[], indexes=[], fks=[]), hash_default=hd,
                                failed=[("CREATE TABLE u (id bigint PRIMARY KEY)", "x")])
            self.assertEqual(sch.tables["u"].pk.keys[0].mode, "ASC")
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
            ("CREATE INDEX i ON nowhere (a)", 'relation "nowhere" does not exist')], "")
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
                 "has no name column")):
            with self.subTest(label):
                with self.assertRaises(inputs.InputError) as e:
                    inputs.load(self.write(files, binary))
                self.assertIn(say, str(e.exception))

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


class NodeCoverage(Case):
    """pg_stat_statements and pg_stat_user_indexes count only what ran through the node they
    are read on. A capture per node is added up; until every node is in, an index with no
    scans is not proven unused and its drop starts with a check on every node."""

    DDL = ("CREATE TABLE t (id bigint NOT NULL, a int, PRIMARY KEY ((id) HASH));\n"
           "CREATE INDEX t_a ON t (a HASH);\n")
    USAGE = "schemaname,relname,indexrelname,idx_scan\npublic,t,t_a,%d\n"
    PSS = PSS_HEAD + "1,%d,%d,1,%d,SELECT * FROM t WHERE id = $1\n"

    def nodes(self, *captures, nodes=None):
        """A bundle with one folder per node: captures are (idx_scan, calls)."""
        d = self.tmpdir({"schema.sql": self.DDL})
        for n, (scans, calls) in enumerate(captures, 1):
            sub = os.path.join(d, "node%d" % n)
            os.makedirs(sub)
            for name, text in (("ybm_index_usage.csv", self.USAGE % scans),
                               ("ybm_pss.csv", self.PSS % (calls, calls, calls)),
                               ("ybm_meta.csv", "key,value\nnode,10.0.0.%d:5433\n%s" % (
                                   n, "nodes,%d\n" % nodes if nodes else ""))):
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
        self.assertEqual(safety.changes_of("DROP INDEX archive.orders_customer;\n"
                                           "ALTER TABLE archive.orders DROP CONSTRAINT orders_ref;"),
                         [("drop", "archive.orders_customer"), ("drop", "archive.orders_ref")])
        self.assertEqual(safety.changes_of(
            "-- CREATE TABLE archive.orders_new (... PRIMARY KEY ((legacy_ref) HASH));"),
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

    def test_a_merged_rebuild_keeps_nulls_not_distinct(self):
        s = schema.parse("CREATE TABLE o (id int, c int, t int, PRIMARY KEY (id HASH));"
                         "CREATE UNIQUE INDEX oc ON o (c HASH) NULLS NOT DISTINCT;")
        a = SimpleNamespace(rule="CAP020", severity="medium", fix="", obj="a", ddl=(
            "CREATE UNIQUE INDEX CONCURRENTLY oc_cov ON o ((c) HASH) INCLUDE (t) NULLS NOT "
            "DISTINCT;\nDROP INDEX oc;"))
        b = SimpleNamespace(rule="CAP020", severity="low", fix="", obj="b", ddl=(
            "CREATE UNIQUE INDEX CONCURRENTLY oc_cov2 ON o ((c) HASH) INCLUDE (id) NULLS NOT "
            "DISTINCT;\nDROP INDEX oc;"))
        safety.merge_replacements(s, SimpleNamespace(items={("x", "a"): a, ("x", "b"): b}))
        self.assertIn("INCLUDE (id, t) NULLS NOT DISTINCT;", a.ddl)

    def test_two_rebuilds_of_one_index_merge(self):
        s = schema.parse("CREATE TABLE o (id int, c int, d int, t int, PRIMARY KEY (id HASH));"
                         "CREATE INDEX oc ON o (c HASH, d ASC);")
        a = SimpleNamespace(rule="STA001", severity="high", fix="", obj="STA001", ddl=(
            "CREATE INDEX CONCURRENTLY oc_nn ON o ((c) HASH, d ASC) WHERE c IS NOT NULL;\n"
            "DROP INDEX oc;"))
        b = SimpleNamespace(rule="CAP020", severity="medium", fix="", obj="CAP020", ddl=(
            "CREATE INDEX CONCURRENTLY oc_cov ON o ((c) HASH, d ASC) INCLUDE (t);\nDROP INDEX oc;"))
        rows = safety.merge_replacements(s, SimpleNamespace(items={("STA001", "x"): a,
                                                                   ("CAP020", "y"): b}))
        self.assertEqual(rows[0]["status"], "merged")
        self.assertIsNone(b.ddl)
        self.assertIn("INCLUDE (t)", a.ddl)
        self.assertIn("WHERE (c IS NOT NULL)", a.ddl)  # the predicate as written
        self.assertEqual(a.ddl.count("DROP INDEX"), 1)
        self.assertNotIn("CONCURRENTLY oc;", a.ddl)


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

    def test_answer_key_recall(self):
        res = json.loads(self.run_cli())
        with open(os.path.join(FIXTURE, "..", "answer-key.json"), encoding="utf-8") as fh:
            key = json.load(fh)
        got = {(f["rule"], f["object"]) for f in res["findings"]}
        missing = []
        for item in key["defects"]:
            if not any(r == m["rule"] and m.get("object_contains", "") in o
                       for m in item["engine_match"] for r, o in got):
                missing.append(item["id"])
        self.assertEqual(missing, [])


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

    def test_row_comparison_is_not_a_seek_bound(self):
        ops = {(p.col, p.op) for p in sqlshape.analyze(self.PAGE).preds}
        self.assertIn(("ts", "rowcmp"), ops)
        self.assertNotIn(("ts", "range"), ops)

    def test_keyset_page_uses_ordered_index_and_flags_row_cursor(self):
        res = self.review(ddl=self.DDL, queries=self.PAGE)
        self.assertEqual(res["patterns"][0]["access"][0]["path"], "ev_tl")
        self.assertNotIn("CAP010", self.rules(res))
        self.assertIn("CAP013", self.rules(res))
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
                self.assertEqual("CAP060" in self.rules(self.review(ddl=ddl, queries=ins % target)),
                                 fires)
        f = self.only(self.review(ddl=ddl, queries=ins % "(a, b)"), "CAP060")
        self.assertIn("ON CONFLICT (a, b) WHERE b IS NOT NULL.", f["fix"])
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

    def test_drop_settles_other_findings_on_the_index(self):
        res = self.review(ddl="CREATE TABLE f (id bigint NOT NULL, flag boolean, "
                              "PRIMARY KEY ((id) HASH));\n"
                              "CREATE INDEX f_flag ON f ((flag) HASH);\n",
                          stats=["f,flag,f,0.6,1,2,{t},{0.3},,0"], rows={"f": 10000000})
        on = [f for f in res["findings"] if f["index"] == "f_flag"]
        drop = [f for f in on if f["rule"] == "STA008"]
        self.assertEqual(len(drop), 1)
        self.assertIn("idx_scan", drop[0]["ddl"])
        others = [f for f in on if f["rule"] != "STA008"]
        self.assertTrue(others)
        self.assertTrue(all(f["ddl"] is None for f in others))
        self.assertNotIn("SAF002", self.rules(res))


class ReplayAuthority(Case):
    """What a replayed plan may and may not do to a static finding."""

    DDL = ("CREATE TABLE r (id bigint NOT NULL, k bigint, v text, PRIMARY KEY ((id) HASH));\n"
           "CREATE INDEX r_v ON r ((v) HASH);\n")
    Q = "SELECT id FROM r WHERE k = $1"   # nothing leads with k: CAP002, predicts a scan
    INDEX_SCAN = {"Node Type": "Index Scan", "Relation Name": "r", "Index Name": "r_v",
                  "Alias": "r"}

    def test_refutation_removes_when_settings_are_known(self):
        res = self.replay(self.DDL, self.Q, self.INDEX_SCAN, {"r": 1e7})
        self.assertNotIn("CAP002", self.rules(res))
        self.assertEqual(res["reconciliation"][0]["finding"], "CAP002")

    def test_refutation_under_assumed_settings_disputes(self):
        res = self.replay(self.DDL, self.Q, self.INDEX_SCAN, {"r": 1e7},
                          assumed={"yb_enable_cbo": "legacy_mode (compiled default)"})
        cap = self.only(res, "CAP002")
        self.assertEqual(cap["section"], "disputed")
        self.assertIn("yb_enable_cbo", cap["disputed"])
        md = report.render(res)
        self.assertIn("## Disputed by replay (planner settings assumed)", md)
        self.assertNotIn("CAP002", md.split("## Findings")[1].split("## Disputed")[0])

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

    def test_real_prefix_and_duplicate_say_plain_drop_index(self):
        found = self.lint(self.T + "CREATE INDEX i1 ON t (a HASH);\n"
                                   "CREATE INDEX i2 ON t (a HASH, b ASC);\n"
                                   "CREATE INDEX i3 ON t (b ASC, c ASC);\n"
                                   "CREATE INDEX i4 ON t (b ASC, c ASC);\n")
        redundant = [f for f in found if f.rule in ("YB051", "YB052")]
        self.assertIn(("YB051", "i1"), {(f.rule, f.message.split("'")[1]) for f in redundant})
        self.assertEqual(len([f for f in redundant if f.rule == "YB052"]), 1)
        for f in found:  # YSQL rejects DROP INDEX CONCURRENTLY
            self.assertNotIn("DROP INDEX CONCURRENTLY", f.message + f.fix)
        for f in redundant:
            self.assertRegex(f.fix, r"then DROP INDEX \w+\.$")

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

            def __init__(self, image, name, keep=False):
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
                self.assertEqual(safety.changes_of(sql), [("drop", "u_email")])

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

    def test_skewed_primary_key_is_never_dropped(self):
        f = self.only(self.review(
            ddl="CREATE TABLE od (offer_id bigint NOT NULL, dept bigint NOT NULL, "
                "PRIMARY KEY ((offer_id) HASH, dept ASC));\n",
            rows={"od": 1000000}, stats=[("od", "offer_id", 0, 300, [], 0.0)]), "STA002")
        self.assertIn("cannot be dropped", f["fix"])
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
        self.assertIn("CAP051", self.rules(res))
        self.assertTrue(any("ON CONFLICT target lost" in c for r in res["safety"]
                            for c in r["checks"]), res["safety"])

    def test_safety_amendment_keeps_the_uniqueness_as_ysql_accepts_it(self):
        for label, ddl, colocated, added in (
                ("hash keys", "CREATE TABLE t (a int, b int, PRIMARY KEY (a ASC));\n"
                              "CREATE UNIQUE INDEX u ON t (b ASC);\n", False,
                 "t_b_uniq ON t ((b) HASH);"),
                ("range keys on a colocated table",
                 "CREATE TABLE t (a int, b int, PRIMARY KEY (a ASC));\n"
                 "CREATE UNIQUE INDEX u ON t (b ASC);\n", True, "t_b_uniq ON t (b ASC);"),
                ("NULLS NOT DISTINCT",
                 "CREATE TABLE t (a int, b int, PRIMARY KEY (a ASC));\n"
                 "CREATE UNIQUE INDEX u ON t (b ASC) NULLS NOT DISTINCT;\n", False,
                 "t_b_uniq ON t ((b) HASH) NULLS NOT DISTINCT;"),
                ("the predicate as written",
                 'CREATE TABLE "T" ("A" int, "B" int, "St" text, PRIMARY KEY ("A" ASC));\n'
                 'CREATE UNIQUE INDEX u ON "T" ("B" ASC) WHERE ("St" = \'a\');\n', False,
                 '"T_B_uniq" ON "T" (("B") HASH) WHERE ("St" = \'a\');')):
            with self.subTest(label):
                sch = schema.parse(ddl, db_colocated=colocated)
                col = analyze.Collector()
                col.add(analyze.Finding("WRK003", "low", "confirmed", "u", "fact", [],
                                        index="u", ddl="DROP INDEX u;"))
                safety.check(sch, [], col, analyze, {"cbo": False}, inputs.Bundle("x"),
                             analyze.Finding)
                self.assertIn("CREATE UNIQUE INDEX CONCURRENTLY " + added,
                              next(iter(col.items.values())).ddl)

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
                col = analyze.Collector()
                col.add(analyze.Finding(rule, "medium", "probable", "p_code", "fact", [],
                                        table="p", index="p_code", fix="Fix.", ddl=change))
                rows = safety.check(sch, [], col, analyze, {"cbo": False}, inputs.Bundle("x"),
                                    analyze.Finding)
                f = col.items[(rule, "p_code")]
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
                    self.assertIn("foreign key c_code_fkey on c depends on p_code", f.fix)
                    self.assertEqual(rows[0]["status"], "withheld")

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
            "Lead an index with the equality columns and follow with the ORDER BY columns in "
            "the requested direction."))
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
        self.assertTrue(fix.startswith("Lead an index"))

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
                         "Check whether an index can serve this pattern's predicates. No static "
                         "finding predicted this: the replayed plan for P1 uses Seq Scan.")

    def test_append_finding_fix_counts_the_repeated_scan(self):
        parts = "".join("CREATE TABLE ev_%d PARTITION OF ev FOR VALUES FROM (%d) TO (%d);\n" %
                        (i, i, i + 1) for i in (1, 2, 3))
        ddl = ("CREATE TABLE ev (id bigint NOT NULL, ts bigint NOT NULL, v text, "
               "PRIMARY KEY ((id) HASH, ts ASC)) PARTITION BY RANGE (ts);\n" + parts)
        root = {"Node Type": "Append",
                "Plans": [{"Node Type": "Seq Scan", "Relation Name": "ev_%d" % i}
                          for i in (1, 2, 3)]}
        fix = self.only(self.replay(ddl, "SELECT v FROM ev WHERE id = $1 AND ts > $2", root,
                                    {"ev": 1000}), "PLN003")["fix"]
        self.assertNotIn("See CAP030", fix)
        self.assertIn("the replayed plan for P1 uses Seq Scan x3.", fix)
        self.assertTrue(fix.startswith(
            "Add a predicate on the partition key so the planner can prune."))

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

    def test_scan_phrase_counts_repeats_and_cuts_long_lists(self):
        seq = {"node": "Seq Scan", "index": None}
        idx = [{"node": "Index Scan", "index": "p%d_idx" % n} for n in range(6)]
        self.assertEqual(plans._scan_phrase([seq] * 12), "Seq Scan x12")
        self.assertEqual(plans._scan_phrase([seq, idx[1], seq]),
                         "Seq Scan x2, Index Scan using p1_idx")
        self.assertEqual(plans._scan_phrase(idx),
                         "Index Scan using p0_idx, Index Scan using p1_idx, Index Scan using "
                         "p2_idx, Index Scan using p3_idx and 2 more")


if __name__ == "__main__":
    unittest.main()
