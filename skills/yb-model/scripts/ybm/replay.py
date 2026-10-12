"""Replay access patterns on a scratch YugabyteDB of the customer's version, with the
customer's statistics injected the way the in-repo TAQO tests do it (pg_class.reltuples and
pg_statistic written directly). No customer rows are needed: the planner costs an empty table
as if it held the measured data.

Requires Docker and a local yugabytedb/yugabyte image. It never pulls an image; if the
version is missing it says which `docker pull` to run.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time

from . import analyze as an
from . import sqlshape
from .schema import qi, qn, split_key

INJECT_FN = r"""
CREATE OR REPLACE FUNCTION ybm_inject(rel regclass, col name, p_null real, p_width int,
    p_nd real, p_mcv text, p_mcf real[], p_hist text, p_corr real) RETURNS void AS $f$
DECLARE att int; typ oid; typmod int; coll oid; eqop oid; ltop oid; k int := 0;
  kinds int[] := '{0,0,0,0,0}'; ops oid[] := '{0,0,0,0,0}'; colls oid[] := '{0,0,0,0,0}';
  nums text[] := '{NULL,NULL,NULL,NULL,NULL}'; vals text[] := '{NULL,NULL,NULL,NULL,NULL}';
  q text;
BEGIN
  SELECT a.attnum, a.atttypid, a.atttypmod, a.attcollation INTO att, typ, typmod, coll
    FROM pg_attribute a WHERE a.attrelid = rel AND a.attname = col AND NOT a.attisdropped;
  IF att IS NULL THEN RAISE NOTICE 'ybm: no column %.%', rel, col; RETURN; END IF;
  SELECT o.oid INTO eqop FROM pg_operator o WHERE o.oprname = '=' AND o.oprleft = typ
    AND o.oprright = typ ORDER BY o.oid LIMIT 1;
  SELECT o.oid INTO ltop FROM pg_operator o WHERE o.oprname = '<' AND o.oprleft = typ
    AND o.oprright = typ ORDER BY o.oid LIMIT 1;
  IF p_mcv IS NOT NULL AND p_mcf IS NOT NULL AND eqop IS NOT NULL THEN
    k := k + 1; kinds[k] := 1; ops[k] := eqop; colls[k] := coll;
    nums[k] := quote_literal(p_mcf::text) || '::real[]';
    vals[k] := format('array_in(%L::cstring, %s, %s)', p_mcv, typ, typmod);
  END IF;
  IF p_hist IS NOT NULL AND ltop IS NOT NULL THEN
    k := k + 1; kinds[k] := 2; ops[k] := ltop; colls[k] := coll;
    vals[k] := format('array_in(%L::cstring, %s, %s)', p_hist, typ, typmod);
  END IF;
  IF p_corr IS NOT NULL AND ltop IS NOT NULL THEN
    k := k + 1; kinds[k] := 3; ops[k] := ltop; colls[k] := coll;
    nums[k] := format('ARRAY[%s]::real[]', p_corr);
  END IF;
  DELETE FROM pg_statistic WHERE starelid = rel AND staattnum = att AND NOT stainherit;
  q := format('INSERT INTO pg_statistic VALUES (%s, %s, false, %s, %s, %s, '
    '%s,%s,%s,%s,%s, %s,%s,%s,%s,%s, %s,%s,%s,%s,%s, %s,%s,%s,%s,%s, %s,%s,%s,%s,%s)',
    rel::oid, att, p_null, p_width, p_nd,
    kinds[1], kinds[2], kinds[3], kinds[4], kinds[5], ops[1], ops[2], ops[3], ops[4], ops[5],
    colls[1], colls[2], colls[3], colls[4], colls[5],
    coalesce(nums[1], 'NULL::real[]'), coalesce(nums[2], 'NULL::real[]'),
    coalesce(nums[3], 'NULL::real[]'), coalesce(nums[4], 'NULL::real[]'),
    coalesce(nums[5], 'NULL::real[]'),
    coalesce(vals[1], 'NULL'), coalesce(vals[2], 'NULL'), coalesce(vals[3], 'NULL'),
    coalesce(vals[4], 'NULL'), coalesce(vals[5], 'NULL'));
  EXECUTE q;
EXCEPTION WHEN others THEN
  RAISE NOTICE 'ybm: could not inject %.%: %', rel, col, SQLERRM;
END $f$ LANGUAGE plpgsql;
"""

# Planner settings copied from the customer, when the target release knows them.
PLANNER_GUCS = ("yb_enable_cbo", "yb_enable_base_scans_cost_model",
                "yb_enable_optimizer_statistics", "yb_enable_bitmapscan", "enable_bitmapscan",
                "yb_max_merge_scan_streams", "yb_bnl_batch_size", "yb_enable_batchednl",
                "yb_prefer_bnl", "work_mem", "random_page_cost", "enable_seqscan",
                "enable_indexscan", "yb_enable_derived_saops", "yb_enable_saop_pushdown",
                "yb_enable_expression_pushdown", "yb_fetch_row_limit", "yb_fetch_size_limit",
                "yb_enable_distinct_pushdown", "yb_enable_index_aggregate_pushdown",
                "yb_use_hash_splitting_by_default", "yb_enable_parallel_append")


def _lit(s):
    return "'" + str(s).replace("'", "''") + "'"


def container_cli():
    """The container CLI: $YBM_CONTAINER_CLI, else docker, else podman (Podman Desktop installs
    it under /opt/podman/bin, often only aliased as docker in an interactive shell)."""
    for c in (os.environ.get("YBM_CONTAINER_CLI"), shutil.which("docker"), shutil.which("podman"),
              "/opt/podman/bin/podman"):
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return "docker"


def _sh(cmd, inp=None, timeout=600):
    if cmd and cmd[0] == "docker":
        cmd = [container_cli()] + list(cmd[1:])
    return subprocess.run(cmd, input=inp, capture_output=True, text=True, timeout=timeout)


def find_image(version, image=None):
    """(image, exact). Exact release first; otherwise the newest image of the same release
    line (major.minor) at or below the customer's release (closest code), else the oldest newer
    one of that line. A non-exact image's differences from the customer's release are applied
    through plans.standing (unknown differences confirm nothing) and the labels say whether it
    is earlier or newer. Never across lines unless the caller names the image."""
    from . import versions
    if image:
        tag = image.split(":")[-1].split("-")[0]
        return image, bool(version) and versions.vt(tag)[:4] == versions.vt(version)[:4]
    r = _sh(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}", "yugabytedb/yugabyte"])
    # Podman names local images docker.io/yugabytedb/yugabyte:<tag>.
    tags = sorted((re.sub(r"^docker\.io/", "", t) for t in r.stdout.split()
                   if t and not t.endswith(":<none>")),
                  key=lambda t: versions.vt(t.split(":")[1]))
    if not version:
        return (tags[-1], False) if tags else (None, False)
    exact = [t for t in tags if t.split(":")[1].startswith(version)]
    if exact:
        return exact[-1], True
    same = [t for t in tags if versions.line(t.split(":")[1]) == versions.line(version)]
    if same:
        # The newest image at or below the customer's release (closest code). An image above
        # it is not used: it may have planner behaviour the customer's release lacks.
        want = versions.vt(version)
        below = [t for t in same if versions.vt(t.split(":")[1]) <= want]
        if below:
            return below[-1], False
        # Otherwise the oldest newer image of the line: labelled as a newer release, and its
        # differences from the customer's release (or that they are unknown) are reported.
        return same[0], False
    return None, False


def strip_dump(ddl):
    """Drop psql meta-commands and statements that would move or break the scratch database."""
    out = []
    for line in ddl.splitlines():
        s = line.strip()
        if s.startswith("\\"):
            continue
        if re.match(r"(?i)^(CREATE|DROP|ALTER)\s+DATABASE\b", s):
            continue
        if re.match(r"(?i)^ALTER\s+.*\bOWNER\s+TO\b", s):
            continue
        if re.match(r"(?i)^(GRANT|REVOKE)\b", s):
            continue
        out.append(line)
    return "\n".join(out)


def not_loaded(pattern, have):
    """The tables `pattern`'s statement names (as the SQL reader resolves them, before they are
    matched to the schema, which holds only what loaded) that are not among the loaded
    relations `have` (schema-qualified, lower case); [] when `have` is unknown."""
    if have is None:
        return []
    named = sorted({t for s in sqlshape.flatten(pattern["shape"]) for t in s.tables}) \
        if pattern.get("shape") is not None else pattern.get("tables") or []
    return [t for t in named if ("." in t and t.lower() or "public." + t.lower()) not in have]


def build_inject_sql(bundle, sch):
    lines = ["SET yb_non_ddl_txn_for_sys_tables_allowed = ON;", INJECT_FN]
    rel = {}
    for name, n in sorted(bundle.reltuples.items()):
        if n is not None and n >= 0:
            rel[name] = n
    for name, u in sorted(bundle.table_usage.items()):
        if name not in rel and u.get("n_live_tup"):
            rel[name] = u["n_live_tup"]
    # Indexes inherit their table's row count when the dump has none for them.
    for iname, idx in sorted(sch.indexes.items()):
        if iname not in rel and idx.table in rel:
            rel[iname] = rel[idx.table]
    for tname, t in sorted(sch.tables.items()):
        if t.pk and t.pk.name not in rel and tname in rel:
            rel[t.pk.name] = rel[tname]
    for name, n in sorted(rel.items()):
        schema, rel_name = split_key(name)
        lines.append("UPDATE pg_class SET reltuples = %s, relpages = 0 WHERE relname = %s "
                     "AND relnamespace = %s::regnamespace;" % (
                         float(n), _lit(rel_name), _lit(qi(schema or "public"))))
    for (t, c), st in sorted(bundle.stats.items()):
        mcf = "ARRAY[%s]::real[]" % ",".join(repr(x) for x in st["mcf"]) if st["mcf"] else "NULL"
        # to_regclass folds unquoted names to lower case; a key outside public is qualified
        regclass = _lit(qn(t) if split_key(t)[0] else "public." + qi(t))
        lines.append(
            "SELECT ybm_inject(to_regclass(%s), %s, %s, %d, %s, %s, %s, %s, %s) "
            "WHERE to_regclass(%s) IS NOT NULL;" % (
                regclass, _lit(c), st["null_frac"] or 0.0, st["avg_width"] or 0,
                st["n_distinct"] or 0.0, _lit(st["mcv"]) if st["mcv"] else "NULL", mcf,
                _lit(st["hist"]) if st["hist"] else "NULL",
                st["correlation"] if st["correlation"] is not None else "NULL", regclass))
    return "\n".join(lines) + "\n", rel


def settings_sql(bundle, mode, tag=None, assumed=None, boot=None):
    """Session settings for replay: the customer's pg_settings; for planner settings the
    bundle lacks, the customer's release default (the compiled default when deployment tools
    disagree, recorded in `assumed`). Without release facts for the customer's release, the
    replay image's own compiled defaults (`boot`, from pg_settings.boot_val) are set, also
    recorded in `assumed`. The container launcher's defaults never apply."""
    from . import versions
    out = []
    gucs = dict((k, v) for k, v in bundle.settings.items() if k in PLANNER_GUCS)
    for name in ("yb_enable_cbo", "yb_enable_base_scans_cost_model",
                 "yb_enable_optimizer_statistics", "yb_enable_bitmapscan",
                 "yb_max_merge_scan_streams", "yb_use_hash_splitting_by_default"):
        if name in gucs:
            continue
        if tag is None:
            v = (boot or {}).get(name)
            if v is not None:
                gucs[name] = v
                if assumed is not None:
                    assumed[name] = "%s (compiled default of the replay image; no release " \
                                    "facts for %s)" % (v, bundle.version or "the release")
            continue
        v, src, per = versions.setting(tag, name)
        if v is None and per:
            v = next((val for k, val in per.items() if k.startswith("compiled")), None)
            if v is not None and assumed is not None:
                assumed[name] = "%s (compiled default; deployment tools differ: %s)" % (
                    v, "; ".join("%s=%s" % (k, x) for k, x in sorted(per.items())))
        if v is not None:
            gucs[name] = v
    # yb_enable_cbo sets the two older cost-model flags itself. When it is known, a default
    # for either flag (applied in name order, after yb_enable_cbo for one of them) would undo
    # part of it, so they are only set when the bundle itself gives them.
    if "yb_enable_cbo" in gucs:
        for legacy in ("yb_enable_base_scans_cost_model", "yb_enable_optimizer_statistics"):
            if legacy not in bundle.settings:
                gucs.pop(legacy, None)
                if assumed is not None:
                    assumed.pop(legacy, None)
    if mode == "on":
        gucs.update({"yb_enable_cbo": "on", "yb_enable_base_scans_cost_model": "on",
                     "yb_enable_optimizer_statistics": "on"})
    elif mode == "off":
        gucs.update({"yb_enable_cbo": "legacy_mode", "yb_enable_base_scans_cost_model": "off",
                     "yb_enable_optimizer_statistics": "off"})
    for k in sorted(gucs):
        out.append("DO $g$ BEGIN PERFORM set_config(%s, %s, false); EXCEPTION WHEN others THEN "
                   "RAISE NOTICE 'ybm: setting %s not applied: %%', SQLERRM; END $g$;"
                   % (_lit(k), _lit(gucs[k]), re.sub(r"[^a-z_]", "", k)))
    out.append("SET plan_cache_mode = force_generic_plan;")
    # Unqualified names resolve as the review resolved them (analyze.search_path).
    out.append("SET search_path = %s;" % ", ".join(qi(s) for s in an.search_path(bundle)))
    return "\n".join(out) + "\n", gucs


def nparams(q):
    ns = [int(x) for x in re.findall(r"\$(\d+)", re.sub(r"'(?:[^']|'')*'", "", q))]
    return max(ns) if ns else 0


def patterns_sql(patterns, outdir):
    lines = ["\\pset format unaligned", "\\pset tuples_only on"]
    from . import server
    for p in patterns:
        q, _ = server.statement(p["query"])  # what the server can prepare
        n = nparams(q)
        name = "ybm_%s" % p["id"].lower()
        lines.append("\\echo ===YBM %s" % p["id"])
        lines.append("\\o %s/%s.json" % (outdir, p["id"]))
        if n:
            lines.append("PREPARE %s AS %s;" % (name, q))
            lines.append("EXPLAIN (VERBOSE, FORMAT JSON) EXECUTE %s(%s);" % (name, ", ".join(["NULL"] * n)))
            # The server's type for each $n (server.py reads which column each one binds).
            lines.append("\\o %s/%s.types" % (outdir, p["id"]))
            lines.append("SELECT parameter_types FROM pg_prepared_statements WHERE name = '%s';"
                         % name)
            lines.append("\\o")
            lines.append("DEALLOCATE %s;" % name)
        else:
            lines.append("EXPLAIN (VERBOSE, FORMAT JSON) %s;" % q)
        lines.append("\\o")
    return "\n".join(lines) + "\n"


class Container:
    def __init__(self, image, name, keep=False, master_flags=None, tserver_flags=None):
        self.image, self.name, self.keep = image, name, keep
        self.master_flags = master_flags
        self.start = ["bin/yugabyted", "start", "--background=false", "--ui=false"]
        if master_flags:
            self.start.append("--master_flags=" + master_flags)
        if tserver_flags:
            self.start.append("--tserver_flags=" + tserver_flags)

    def __enter__(self):
        _sh(["docker", "rm", "-f", self.name])
        # --pull=never: an image is only ever downloaded after the user agreed to it.
        r = _sh(["docker", "run", "-d", "--pull=never", "--name", self.name, self.image] +
                self.start)
        if r.returncode != 0:
            raise RuntimeError("docker run failed: " + r.stderr.strip())
        for _ in range(100):
            r = self.sql("SELECT 1", db="yugabyte")
            if r.returncode == 0:
                # yugabyted does not pass every --master_flags entry to the master (the
                # tablet-replica limit stays on): set them on the running master too.
                for kv in (self.master_flags or "").split(","):
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        _sh(["docker", "exec", self.name, "bash", "-c",
                             'bin/yb-ts-cli --server_address="$(hostname):7100" set_flag '
                             '--force %s %s' % (k.strip(), v.strip())])
                return self
            time.sleep(3)
        # __exit__ never runs when __enter__ raises: remove the container here.
        if not self.keep:
            _sh(["docker", "rm", "-f", self.name])
        raise RuntimeError("YugabyteDB did not become ready in the container")

    def __exit__(self, *a):
        if not self.keep:
            _sh(["docker", "rm", "-f", self.name])

    def sql(self, text, db="ybm", flags=()):
        return _sh(["docker", "exec", "-i", self.name, "bash", "-c",
                    'bin/ysqlsh -h "$(hostname)" -X -q -v ON_ERROR_STOP=0 %s -d %s'
                    % (" ".join(flags), db)], inp=text)

    def file_sql(self, local_path, db="ybm"):
        remote = "/tmp/" + os.path.basename(local_path)
        _sh(["docker", "cp", local_path, "%s:%s" % (self.name, remote)])
        return _sh(["docker", "exec", self.name, "bash", "-c",
                    'bin/ysqlsh -h "$(hostname)" -X -q -v ON_ERROR_STOP=0 -d %s -f %s 2>&1'
                    % (db, remote)], timeout=1800)

    def mkdir(self, remote):
        _sh(["docker", "exec", self.name, "mkdir", "-p", remote])

    def copy_out(self, remote, local):
        _sh(["docker", "cp", "%s:%s" % (self.name, remote), local])


class Recorder:
    """A container that writes every call and its answer to `path` (JSON) on exit: the answers
    a Recorded container serves offline (YBM_RECORD=path)."""

    def __init__(self, inner, path):
        self.inner, self.path, self.calls = inner, path, []
        self.name = inner.name

    def __enter__(self):
        self.inner.__enter__()
        return self

    def __exit__(self, *a):
        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump({"calls": self.calls}, fh, indent=0, sort_keys=True)
        finally:  # the container goes whatever happens to the recording
            ret = self.inner.__exit__(*a)
        return ret

    def _log(self, key, r):
        self.calls.append(dict(key, stdout=r.stdout, stderr=r.stderr, rc=r.returncode))
        return r

    def sql(self, text, db="ybm", flags=()):
        return self._log({"call": "sql", "db": db, "flags": list(flags), "text": text},
                         self.inner.sql(text, db=db, flags=flags))

    def file_sql(self, local_path, db="ybm"):
        with open(local_path, encoding="utf-8") as fh:
            text = fh.read()
        return self._log({"call": "file_sql", "db": db, "flags": [], "text": text},
                         self.inner.file_sql(local_path, db=db))

    def mkdir(self, remote):
        self.inner.mkdir(remote)

    def copy_out(self, remote, local):
        self.inner.copy_out(remote, local)
        files = {}
        for root, _, names in os.walk(local):
            for n in sorted(names):
                with open(os.path.join(root, n), encoding="utf-8") as fh:
                    files[os.path.relpath(os.path.join(root, n), local)] = fh.read()
        self.calls.append({"call": "copy_out", "db": "", "flags": [], "text": remote,
                           "files": files})


class Recorded:
    """Serves a Recorder's answers in place of a container: each call gets the next answer
    recorded for the same call. A call that was never recorded raises: the SQL the engine
    sends has changed, and the recording must be made again on a live server."""

    def __init__(self, path):
        with open(path, encoding="utf-8") as fh:
            calls = json.load(fh)["calls"]
        self.answers, self.name = {}, "recorded"
        for c in calls:
            self.answers.setdefault((c["call"], c["db"], tuple(c["flags"]), c["text"]),
                                    []).append(c)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def _next(self, key):
        q = self.answers.get(key)
        if not q:
            raise KeyError("call not in the recording (record it again): %s %s %.200s" % (
                key[0], key[1], key[3]))
        return q.pop(0) if len(q) > 1 else q[0]

    def sql(self, text, db="ybm", flags=()):
        c = self._next(("sql", db, tuple(flags), text))
        return subprocess.CompletedProcess([], c["rc"], c["stdout"], c["stderr"])

    def file_sql(self, local_path, db="ybm"):
        with open(local_path, encoding="utf-8") as fh:
            c = self._next(("file_sql", db, (), fh.read()))
        return subprocess.CompletedProcess([], c["rc"], c["stdout"], c["stderr"])

    def mkdir(self, remote):
        pass

    def copy_out(self, remote, local):
        c = self._next(("copy_out", "", (), remote))
        for rel, text in c["files"].items():
            os.makedirs(os.path.dirname(os.path.join(local, rel)), exist_ok=True)
            with open(os.path.join(local, rel), "w", encoding="utf-8") as fh:
                fh.write(text)


def _errors(text, limit=50):
    errs = [l.strip() for l in text.splitlines() if "ERROR:" in l]
    return errs[:limit], len(errs)


def _patterns(bundle, max_patterns):
    sch = an.build_schema(bundle)
    pats = an.build_patterns(bundle, sch)
    # The top ranked patterns, and every listed one (unranked, so possibly hot).
    return sch, pats[:max_patterns] + [p for p in pats[max_patterns:]
                                       if p["source"] == "queries.sql"]


def run(bundle, image=None, mode="customer", keep=False, max_patterns=200, probe_rules=(),
        measure=False, catalog=False):
    """Replay the bundle's patterns on a scratch server. catalog: read the schema model from
    the replay's own database (bundle.catalog_schema), before the patterns are built.
    probe_rules: rule ids, or a function of the bundle (called once the schema is read)."""
    ps = an.planner_settings(bundle)
    # With the catalog, the schema and patterns are built once it is read (in the container).
    sch, patterns = (None, None) if catalog else _patterns(bundle, max_patterns)
    from . import versions
    img, exact = find_image(bundle.version, image)
    if not img:
        raise RuntimeError(
            "No local yugabytedb/yugabyte image for release %s or its release line. Ask the "
            "user before downloading: docker pull yugabytedb/yugabyte:<%s-bNN tag from Docker "
            "Hub>" % (bundle.version, bundle.version))
    cust_tag, _ = versions.resolve(bundle.version)
    img_tag, _ = versions.resolve(img.split(":")[-1].split("-")[0])
    work = tempfile.mkdtemp(prefix="ybm-replay-")
    import json as _json
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "..", "rules", "rules.json"), encoding="utf-8") as fh:
        rule_ids = list(_json.load(fh)["rules"])
    result = {"image": img, "mode": mode, "patterns": [], "errors": [],
              "version_match": {"customer": bundle.version, "customer_release": cust_tag,
                                "image_release": img_tag, "exact": exact,
                                "drift_known": bool(cust_tag and img_tag),
                                "drift": versions.drift(cust_tag, img_tag, rules=rule_ids)},
              "assumed_settings": {}}
    try:
        # The tablet-replica limit of a one-node scratch cluster is the environment, not the
        # customer's DDL: lifted, as for the catalog, so SPLIT INTO clauses load as written.
        box = Container(img, "ybm-replay-%d" % os.getpid(), keep=keep,
                        master_flags="enforce_tablet_replica_limits=false")
        if os.environ.get("YBM_RECORD"):
            box = Recorder(box, os.environ["YBM_RECORD"])
        with box as c:
            from . import catalog as catalog_mod
            ddl_sql = strip_dump(bundle.ddl)
            # The customer's sharding default for keys the DDL does not annotate.
            head = "" if ps["hash_default"] else "SET yb_use_hash_splitting_by_default = off;\n"
            try:  # the same preparation as the catalog's (and the measured pass's)
                failed, prep = catalog_mod.prepare(c, "ybm", ddl_sql, ps["colocated"], head)
            except RuntimeError as e:
                # Every later step runs in this database; without it each would fail with a
                # FATAL that names no cause.
                raise RuntimeError(str(e).replace("the scratch database ybm",
                                                  "the replay database"))
            result["server_version"] = c.sql("SELECT version();", flags=("-tA",)).stdout.strip()
            result["preparation"] = prep
            result["ddl_errors"] = {"count": len(failed),
                                    "first": [e for _, e in failed][:50]}
            if catalog and getattr(bundle, "catalog_schema", None) is None:
                # One load: the catalog is read from this database, then the patterns are
                # built on the server's schema.
                try:
                    catalog_mod.attach(bundle, container=c, db="ybm", loaded=(failed, prep))
                    bundle.catalog_info["image"] = img
                except Exception as e:
                    result["catalog_error"] = str(e)[:300]
            if sch is None:
                sch, patterns = _patterns(bundle, max_patterns)
            if callable(probe_rules):
                probe_rules = probe_rules(bundle)
            # The tables the scratch database has: a pattern that reads one it lacks fails
            # because its DDL did not load, not because of the statement.
            q = c.sql("SELECT json_agg(json_build_array(lower(n.nspname || '.' || c.relname), "
                      "c.relkind)) FROM pg_class c "
                      "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relkind IN "
                      "('r', 'p', 'v', 'm', 'f') AND n.nspname NOT IN ('pg_catalog', "
                      "'information_schema') AND n.nspname NOT LIKE 'pg\\_%';", flags=("-tA",))
            try:
                rels = json.loads((q.stdout or "").strip() or "[]") or []
                have = {n for n, _ in rels}
            except ValueError:
                rels, have = [], None
            # Tables, as the catalog counts them (views are readable, not tables).
            result["tables_loaded"] = sum(1 for _, k in rels if k in ("r", "p")) \
                if have is not None else None
            q = c.sql("SELECT count(*) FROM pg_index i JOIN pg_class t ON t.oid = i.indrelid "
                      "JOIN pg_namespace n ON n.oid = t.relnamespace WHERE NOT i.indisprimary "
                      "AND n.nspname NOT IN ('pg_catalog', 'information_schema') AND "
                      "n.nspname NOT LIKE 'pg\\_%';", flags=("-tA",))
            try:
                result["indexes_loaded"] = int((q.stdout or "").strip().splitlines()[-1])
            except (ValueError, IndexError):
                result["indexes_loaded"] = None
            inj, rel = build_inject_sql(bundle, sch)
            result["reltuples"] = rel
            # Plans chosen without these are a weaker witness; the label on every replay
            # confirmation says so (plans.standing).
            result["statistics_missing"] = [x for x, have in (
                ("column statistics", bundle.stats), ("table sizes", rel)) if not have]
            inj_path = os.path.join(work, "inject.sql")
            with open(inj_path, "w") as fh:
                fh.write(inj)
            out = c.file_sql(inj_path).stdout
            errs, n = _errors(out)
            result["inject_errors"] = {"count": n, "first": errs,
                                       "notices": [l.strip() for l in out.splitlines()
                                                   if "ybm:" in l][:50]}
            boot = {}
            if cust_tag is None:
                q = c.sql("SELECT name || '=' || boot_val FROM pg_settings WHERE name IN (%s);"
                          % ", ".join(_lit(n) for n in PLANNER_GUCS), flags=("-tA",))
                for line in q.stdout.splitlines():
                    if "=" in line:
                        k, v = line.split("=", 1)
                        boot[k.strip()] = v.strip()
            set_sql, gucs = settings_sql(bundle, mode, cust_tag, result["assumed_settings"],
                                         boot)
            result["settings"] = gucs
            remote_out = "/tmp/ybm_plans"
            pq = os.path.join(work, "patterns.sql")
            with open(pq, "w") as fh:
                fh.write(set_sql + patterns_sql(patterns, remote_out))
            c.mkdir(remote_out)
            out = c.file_sql(pq).stdout
            result["settings_notices"] = [l.strip() for l in out.splitlines()
                                          if "not applied" in l]
            perr = {}
            cur = None
            for line in out.splitlines():
                if line.startswith("===YBM "):
                    cur = line.split()[1]
                elif "ERROR:" in line and cur:
                    perr.setdefault(cur, line.strip())
            local_plans = os.path.join(work, "plans")
            c.copy_out(remote_out, local_plans)
            for p in patterns:
                entry = {"id": p["id"], "query": p["query"], "plan": None,
                         "error": perr.get(p["id"])}
                missing = not_loaded(p, have)
                if entry["error"] and missing:
                    entry["missing_tables"] = missing  # its DDL did not load
                fp = os.path.join(local_plans, "%s.json" % p["id"])
                if os.path.isfile(fp):
                    with open(fp, encoding="utf-8") as fh:
                        txt = fh.read().strip()
                    if txt:
                        try:
                            entry["plan"] = json.loads(txt)
                        except ValueError:
                            entry["error"] = entry["error"] or "unparseable EXPLAIN output"
                tp = os.path.join(local_plans, "%s.types" % p["id"])
                if os.path.isfile(tp):
                    with open(tp, encoding="utf-8") as fh:
                        raw = fh.read().strip().strip("{}")
                    entry["param_types"] = [x.strip().strip('"') for x in
                                            re.findall(r'"[^"]*"|[^,]+', raw)] if raw else []
                result["patterns"].append(entry)
            result["version"] = img.split(":")[-1]
            if probe_rules:
                from . import probes
                result["probes"] = probes.run(c, probe_rules, set_sql)
            if measure:
                from . import measure as measure_mod
                result["measured"] = measure_mod.run(c, bundle, sch, patterns, set_sql, inj,
                                                     ddl_sql, replayed=result["patterns"],
                                                     colocated=prep["colocated"], head=head)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return result
