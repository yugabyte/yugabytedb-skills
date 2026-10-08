#!/usr/bin/env python3
"""
oracle: check the engine's static planner model against the real planner, TAQO-style.

    python3 evals/yb-model/oracle.py [--cases 200] [--seed 1] [--image yugabytedb/yugabyte:TAG]

Generates seeded random cases (key layout x secondary indexes x query shape), creates them
on a scratch YugabyteDB with injected statistics (1M rows, selective columns), EXPLAINs each
query and compares with what ybm predicts:

  capability errors (engine bugs or version differences):
    - engine: no usable index      planner: index scan on that table
    - engine: index order suffices planner: Sort
    - engine: needs a Sort         planner: no Sort, rows still ordered
    - engine: not covering         planner: Index Only Scan
  cost choices (counted, not errors): planner prefers a scan or another index the engine
  also considered usable.

Writes oracle-report.json next to this file and prints a summary with counterexamples.
"""

import argparse
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.join(HERE, "..", "..", "skills", "yb-model", "scripts")
sys.path.insert(0, SKILL)

from ybm import analyze, replay, safety, schema, sqlshape  # noqa: E402

PKS = ["PRIMARY KEY (id HASH)",
       "PRIMARY KEY ((a, b) HASH, id ASC)",
       "PRIMARY KEY (id ASC)",
       "PRIMARY KEY (a HASH, d DESC, id ASC)"]
INDEXES = ["(a HASH)", "(b HASH, d DESC)", "(d ASC)", "(a HASH) INCLUDE (c)",
           "(b HASH) WHERE b IS NOT NULL", "((lower(c)) HASH)", "(a HASH, b ASC)",
           "(e ASC, a ASC)", "((a, e) HASH)"]
WHERES = ["a = $1", "a = $1 AND b = $2", "a IN ($1, $2, $3)", "b > $1", "d > $1",
          "lower(c) = $1", "c = $1", "a = $1 OR b = $2", "id = $1", "e = $1",
          "a = $1 AND e = $2", "b = $1", "a = $1 AND d > $2", "e > $1 AND a = $2"]
ORDERS = ["", "", " ORDER BY d DESC LIMIT 10", " ORDER BY id LIMIT 10", " ORDER BY b LIMIT 10"]
COLS = ["*", "id", "a, c", "a, b"]
BATCH = 120
CBO = True
STATS = {"id": (0.0, -1.0), "a": (0.0, 100000.0), "b": (0.0, 50000.0), "c": (0.0, -1.0),
         "d": (0.0, -1.0), "e": (0.0, 20000.0)}


def gen(n, seed):
    rnd = random.Random(seed)
    cases = []
    for i in range(n):
        t = "t%04d" % i
        pk = rnd.choice(PKS)
        idx = rnd.sample(INDEXES, rnd.randint(0, 2))
        q = "SELECT %s FROM %s WHERE %s%s" % (rnd.choice(COLS), t, rnd.choice(WHERES),
                                             rnd.choice(ORDERS))
        ddl = ["CREATE TABLE %s (id bigint NOT NULL, a int NOT NULL, b int, c text, "
               "d timestamptz NOT NULL, e int NOT NULL, %s)" % (t, pk)]
        for j, ix in enumerate(idx):
            ddl.append("CREATE INDEX %s_i%d ON %s %s" % (t, j, t, ix))
        cases.append({"id": t, "ddl": ddl, "query": q})
    return cases


def predict(case):
    sch = schema.parse(";\n".join(case["ddl"]) + ";")
    shape = sqlshape.analyze(case["query"], sch)
    pat = {"id": "P1", "shape": shape, "weight": "HOT"}
    m = safety.access_map(sch, [pat], analyze, CBO).get("P1", {}).get(case["id"])
    return m, shape


def sql_for(cases):
    out = ["SET yb_non_ddl_txn_for_sys_tables_allowed = ON;", replay.INJECT_FN]
    for c in cases:
        out.extend(x + ";" for x in c["ddl"])
    for c in cases:
        out.append("UPDATE pg_class SET reltuples = 1000000, relpages = 0 WHERE relname LIKE "
                   "'%s%%';" % c["id"])
        for col, (nf, nd) in STATS.items():
            out.append("SELECT ybm_inject('%s', '%s', %s, 8, %s, NULL, NULL, NULL, NULL);"
                       % (c["id"], col, nf, nd))
    return "\n".join(out) + "\n"


def explain_sql(cases, outdir, cbo="on"):
    lines = ["SET yb_enable_cbo = %s;" % cbo, "SET plan_cache_mode = force_generic_plan;",
             "\\pset format unaligned", "\\pset tuples_only on"]
    for c in cases:
        n = replay.nparams(c["query"])
        lines.append("\\o %s/%s.json" % (outdir, c["id"]))
        if n:
            lines.append("PREPARE q_%s AS %s;" % (c["id"], c["query"]))
            lines.append("EXPLAIN (FORMAT JSON) EXECUTE q_%s(%s);" % (c["id"],
                                                                     ", ".join(["NULL"] * n)))
            lines.append("DEALLOCATE q_%s;" % c["id"])
        else:
            lines.append("EXPLAIN (FORMAT JSON) %s;" % c["query"])
        lines.append("\\o")
    return "\n".join(lines) + "\n"


def walk(node, parent=None, acc=None):
    acc = [] if acc is None else acc
    acc.append((node, parent))
    for ch in node.get("Plans", []) or []:
        walk(ch, node, acc)
    return acc


def scan_info(plan, table):
    nodes = walk(plan[0]["Plan"])
    scans = [n for n, _ in nodes if n.get("Relation Name") == table]
    s = scans[0] if scans else {}
    return {"node": s.get("Node Type"), "index": s.get("Index Name"),
            "seek": bool(s.get("Index Cond")), "index_cond": s.get("Index Cond"),
            "filters": {k: s[k] for k in ("Storage Index Filter", "Storage Filter", "Filter")
                        if k in s},
            "sort": any(n.get("Node Type") in ("Sort", "Incremental Sort") for n, _ in nodes)}


def compare(case, pred, shape, info):
    out = []
    if pred is None:
        return [("no-prediction", "engine made no prediction for this table")]
    idx_scan = info["node"] in ("Index Scan", "Index Only Scan")
    if idx_scan and info["seek"]:
        # An Index Cond that binds only part of a hash group is not a seek: DocDB reads every
        # row and rechecks (see ybm.plans.partial_hash_scan).
        from ybm import plans as _plans
        sch = schema.parse(";\n".join(case["ddl"]) + ";")
        if _plans.partial_hash_scan({"index": info["index"], "index_cond": info["index_cond"]},
                                    sch):
            info["seek"] = False
            info["partial_hash"] = True
    if not pred["path"] and idx_scan and info["seek"]:
        out.append(("capability", "engine: no usable index; planner seeks %s on %s" % (
            info["index"], info["index_cond"])))
    if not pred["path"] and idx_scan and not info["seek"]:
        if shape.order and shape.limit and not info["sort"]:
            out.append(("ordered-walk", "planner walks %s in ORDER BY order and stops at LIMIT"
                         % info["index"]))
        else:
            out.append(("full-index-walk", "planner reads all of %s with %s" % (
                info["index"], "a partial hash-key condition (full scan)"
                if info.get("partial_hash") else "a filter")))
    if pred["path"] and idx_scan and info["index"] == pred["path"] and not pred["point"]:
        if pred["order_ok"] is True and info["sort"]:
            out.append(("capability", "engine: %s gives the order; planner sorts" % pred["path"]))
        if pred["order_ok"] is False and not info["sort"] and shape.order:
            out.append(("capability", "engine: sort needed; planner returns order from %s"
                        % pred["path"]))
        if not pred["covering"] and info["node"] == "Index Only Scan":
            out.append(("capability", "engine: %s not covering; planner Index Only Scan"
                        % pred["path"]))
    if pred["path"] and not idx_scan:
        out.append(("cost", "engine: %s usable; planner chose %s" % (pred["path"], info["node"])))
    elif pred["path"] and info["index"] and info["index"] != pred["path"]:
        out.append(("cost", "engine: %s; planner: %s" % (pred["path"], info["index"])))
    return out


def partial_hash_probe(c, cbo):
    """Measure, on real rows, what an Index Scan with a partial hash-key Index Cond reads."""
    sql = ("DROP DATABASE IF EXISTS ybm_probe; CREATE DATABASE ybm_probe WITH colocation = false;")
    c.sql(sql, db="yugabyte")
    body = """
CREATE TABLE p (id bigint, a int, b int, c text, PRIMARY KEY ((a, b) HASH, id ASC)) SPLIT INTO 3 TABLETS;
INSERT INTO p SELECT g, g %% 1000, g %% 37, 'x' FROM generate_series(1, 20000) g;
ANALYZE p;
SET yb_enable_cbo = %s;
/*+ IndexScan(p p_pkey) */ EXPLAIN (ANALYZE, DIST, FORMAT JSON) SELECT c FROM p WHERE a = 5;
""" % cbo
    out = c.sql(body, db="ybm_probe", flags=("-tA",)).stdout
    try:
        plan = json.loads(out[out.index("["):])[0]["Plan"]
    except (ValueError, KeyError, IndexError):
        return None
    return {"node": plan.get("Node Type"), "index_cond": plan.get("Index Cond"),
            "rows_returned": plan.get("Actual Rows"),
            "rows_scanned": plan.get("Storage Table Rows Scanned") or
            plan.get("Storage Index Rows Scanned"),
            "read_requests": plan.get("Storage Table Read Requests"), "table_rows": 20000}


def record(img, cbo, seed, cases, results, cap, kinds, probe):
    """Merge this run's observations into skills/yb-model/rules/observations.json."""
    release = img.split(":")[-1].split("-")[0]
    path = os.path.join(SKILL, "..", "rules", "observations.json")
    try:
        obs = json.load(open(path))
    except (OSError, ValueError):
        obs = {"about": "Written by evals/yb-model/oracle.py. Do not edit by hand.",
               "runs": [], "rules": {}}
    key = (release, cbo, seed)
    obs["runs"] = [r for r in obs["runs"] if (r["release"], r["mode"], r["seed"]) != key]
    obs["runs"].append({"release": release, "mode": cbo, "seed": seed, "cases": len(cases),
                        "capability_disagreements": cap, "by_kind": kinds})
    rules = obs.setdefault("rules", {})
    for rid in list(rules):
        rules[rid] = [o for o in rules[rid] if (o["release"], o["mode"]) != (release, cbo)]
    walks = sum(1 for r in results if (r.get("prediction") or {}).get("path") and
                r.get("planner", {}).get("node") in ("Index Scan", "Index Only Scan") and
                not r["planner"].get("seek") and not r["planner"].get("sort") and
                r["planner"].get("index") == r["prediction"]["path"])
    partial = sum(1 for r in results if r.get("planner", {}).get("partial_hash"))
    if partial:
        rules.setdefault("CAP001", []).append({
            "release": release, "mode": cbo,
            "observation": "planner chose an Index Scan with a partial hash-key Index Cond in "
                           "%d of %d generated cases" % (partial, len(cases))})
    if probe and probe.get("rows_scanned"):
        rules.setdefault("CAP001", []).append({
            "release": release, "mode": cbo,
            "observation": "forced partial hash-key Index Scan read %s of %s rows to return %s "
                           "(%s read requests)" % (probe["rows_scanned"], probe["table_rows"],
                                                   probe["rows_returned"],
                                                   probe["read_requests"])})
    if walks:
        rules.setdefault("CAP012", []).append({
            "release": release, "mode": cbo,
            "observation": "planner walked the predicted index in ORDER BY order without a "
                           "seek in %d of %d generated cases" % (walks, len(cases))})
    for rid in [k for k, v in rules.items() if not v]:
        del rules[rid]
    obs["runs"].sort(key=lambda r: (r["release"], r["mode"], r["seed"]))
    with open(path, "w") as fh:
        json.dump(obs, fh, indent=1, sort_keys=True)
        fh.write("\n")
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=int, default=200)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--image")
    ap.add_argument("--cbo", default="on", choices=["on", "legacy_mode"])
    args = ap.parse_args()
    global CBO
    CBO = args.cbo == "on"
    cases = gen(args.cases, args.seed)
    img, _ = replay.find_image(None, args.image)
    if not img:
        print("no local yugabytedb/yugabyte image", file=sys.stderr)
        return 2
    import tempfile
    work = tempfile.mkdtemp(prefix="ybm-oracle-")
    setup_out = ""
    os.makedirs(os.path.join(work, "plans"), exist_ok=True)
    # One database per batch: a single-node cluster caps tablet replicas (~544), so the
    # batch is created, explained and dropped before the next.
    with replay.Container(img, "ybm-oracle-%d" % os.getpid()) as c:
        for b in range(0, len(cases), BATCH):
            chunk = cases[b:b + BATCH]
            c.sql("DROP DATABASE IF EXISTS ybm; CREATE DATABASE ybm WITH colocation = false;",
                  db="yugabyte")
            p1 = os.path.join(work, "setup.sql")
            open(p1, "w").write(sql_for(chunk))
            setup_out += c.file_sql(p1).stdout
            p2 = os.path.join(work, "explain.sql")
            open(p2, "w").write(explain_sql(chunk, "/tmp/ybm_oracle", args.cbo))
            replay._sh(["docker", "exec", c.name, "mkdir", "-p", "/tmp/ybm_oracle"])
            c.file_sql(p2)
        replay._sh(["docker", "cp", "%s:/tmp/ybm_oracle/." % c.name,
                    os.path.join(work, "plans")])
        probe = partial_hash_probe(c, args.cbo)
    results, cap, cost, err, kinds = [], 0, 0, 0, {}
    for case in cases:
        pred, shape = predict(case)
        fp = os.path.join(work, "plans", case["id"] + ".json")
        txt = open(fp).read().strip() if os.path.isfile(fp) else ""
        if not txt:
            err += 1
            results.append({**case, "error": "no plan"})
            continue
        info = scan_info(json.loads(txt), case["id"])
        diffs = compare(case, pred, shape, info)
        cap += sum(1 for k, _ in diffs if k == "capability")
        cost += sum(1 for k, _ in diffs if k == "cost")
        for k, _ in diffs:
            kinds[k] = kinds.get(k, 0) + 1
        results.append({**case, "prediction": pred, "planner": info, "diffs": diffs})
    rep = {"image": img, "cbo": args.cbo, "cases": len(cases), "seed": args.seed, "no_plan": err,
           "capability_disagreements": cap, "cost_choices": cost, "by_kind": kinds,
           "setup_errors": [l for l in setup_out.splitlines() if "ERROR" in l][:20],
           "results": results}
    out = os.path.join(HERE, "oracle-report-%s-%s.json" % (img.split(":")[-1], args.cbo))
    json.dump(rep, open(out, "w"), indent=1)
    print("image %s: %d cases, %d without plan, %d capability disagreements, %d cost choices; "
          "by kind %s" % (img, len(cases), err, cap, cost, json.dumps(kinds, sort_keys=True)))
    for r in results:
        for k, d in r.get("diffs", []):
            if k == "capability":
                print("  %s | %s | %s | %s" % (r["id"], " ; ".join(r["ddl"][1:]) or "-",
                                               r["query"], d))
    print("report: %s" % out)
    print("observations: %s" % record(img, args.cbo, args.seed, cases, results, cap, kinds,
                                      probe))
    return 0


if __name__ == "__main__":
    sys.exit(main())
