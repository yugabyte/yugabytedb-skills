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
    line (major.minor) that is older than the customer's release, whose differences
    reconcile() then accounts for. Never a newer image (a newer planner may already fix what
    the customer's release still does) and never across lines unless the caller names the
    image."""
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
        # The newest image at or below the customer's release (closest code), else the oldest
        # above it.
        want = versions.vt(version)
        below = [t for t in same if versions.vt(t.split(":")[1]) <= want]
        if below:
            return below[-1], False
        return None, False
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
    for p in patterns:
        q = p["query"].strip().rstrip(";")
        n = nparams(q)
        name = "ybm_%s" % p["id"].lower()
        lines.append("\\echo ===YBM %s" % p["id"])
        lines.append("\\o %s/%s.json" % (outdir, p["id"]))
        if n:
            lines.append("PREPARE %s AS %s;" % (name, q))
            lines.append("EXPLAIN (VERBOSE, FORMAT JSON) EXECUTE %s(%s);" % (name, ", ".join(["NULL"] * n)))
            lines.append("DEALLOCATE %s;" % name)
        else:
            lines.append("EXPLAIN (VERBOSE, FORMAT JSON) %s;" % q)
        lines.append("\\o")
    return "\n".join(lines) + "\n"


class Container:
    def __init__(self, image, name, keep=False, master_flags=None, tserver_flags=None):
        self.image, self.name, self.keep = image, name, keep
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
                return self
            time.sleep(3)
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


def _errors(text, limit=50):
    errs = [l.strip() for l in text.splitlines() if "ERROR:" in l]
    return errs[:limit], len(errs)


def run(bundle, image=None, mode="customer", keep=False, max_patterns=200, probe_rules=()):
    ps = an.planner_settings(bundle)
    sch = an.build_schema(bundle)
    pats = an.build_patterns(bundle, sch)
    # The top ranked patterns, and every listed one (unranked, so possibly hot).
    patterns = pats[:max_patterns] + [p for p in pats[max_patterns:]
                                      if p["source"] == "queries.sql"]
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
        with Container(img, "ybm-replay-%d" % os.getpid(), keep=keep) as c:
            coloc = "true" if ps["colocated"] else "false"
            r = c.sql("CREATE DATABASE ybm WITH colocation = %s;" % coloc, db="yugabyte")
            result["server_version"] = c.sql("SELECT version();", flags=("-tA",)).stdout.strip()
            ddl_path = os.path.join(work, "ddl.sql")
            with open(ddl_path, "w") as fh:
                fh.write(strip_dump(bundle.ddl))
            out = c.file_sql(ddl_path).stdout
            errs, n = _errors(out)
            result["ddl_errors"] = {"count": n, "first": errs}
            inj, rel = build_inject_sql(bundle, sch)
            result["reltuples"] = rel
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
            _sh(["docker", "exec", c.name, "mkdir", "-p", remote_out])
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
            _sh(["docker", "cp", "%s:%s" % (c.name, remote_out), local_plans])
            for p in patterns:
                entry = {"id": p["id"], "query": p["query"], "plan": None,
                         "error": perr.get(p["id"])}
                fp = os.path.join(local_plans, "%s.json" % p["id"])
                if os.path.isfile(fp):
                    with open(fp, encoding="utf-8") as fh:
                        txt = fh.read().strip()
                    if txt:
                        try:
                            entry["plan"] = json.loads(txt)
                        except ValueError:
                            entry["error"] = entry["error"] or "unparseable EXPLAIN output"
                result["patterns"].append(entry)
            result["version"] = img.split(":")[-1]
            if probe_rules:
                from . import probes
                result["probes"] = probes.run(c, probe_rules, set_sql)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return result
