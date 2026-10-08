#!/usr/bin/env python3
"""
extract-version-data: build rules/versions.json from the yugabyte-db source, per release tag.
Nothing version-specific is written by hand; every value below is read from the source of the
release it describes. rules/versions.json is a local cache: build the releases you review, and
never commit it.

    # every release since 2.20 from a local checkout (no checkout of tags; uses git show)
    python3 extract-version-data.py --repo ~/yugabyte-db

    # add or refresh one release from GitHub (network: fetches ~25 files from
    # raw.githubusercontent.com/yugabyte/yugabyte-db/<tag>/...)
    python3 extract-version-data.py --github --release 2026.1.2.0

For every release it records:
  gucs      default of each planner setting the rules depend on: the compiled default from
            guc.c / guc_tables.c, overridden by the tserver's PG-flag default (pg_wrapper.cc);
            null when the setting does not exist in that release
  gflags    default of each server flag the rules depend on (AUTO flags: the value a new
            install gets)
  anchors   whether each rule's regress-test citation exists in that release
  profiles  planner settings that deployment tools inject into ysql_pg_conf_csv on that
            release, discovered by scanning bin/yugabyted and the YBA (managed/) universe
            creation code for '<setting>=<value>'
A git run also records `source_paths`: the files where each fact was found, so a GitHub
refresh fetches exactly those files.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

GUCS = ["yb_enable_cbo", "yb_enable_base_scans_cost_model", "yb_enable_optimizer_statistics",
        "yb_enable_bitmapscan", "yb_max_merge_scan_streams", "yb_use_hash_splitting_by_default",
        "yb_enable_batchednl", "yb_bnl_batch_size", "yb_enable_hash_batch_in",
        "yb_enable_saop_pushdown", "yb_enable_distinct_pushdown",
        "yb_enable_expression_pushdown", "yb_read_from_followers",
        "yb_enable_pg_stat_statements_rpc_stats"]
GFLAGS = ["ysql_enable_auto_analyze_service", "ysql_enable_auto_analyze_infra",
          "ysql_enable_auto_analyze", "enable_automatic_tablet_splitting",
          "ysql_sequence_cache_minval"]
REGRESS = "src/postgres/src/test/regress/expected/"
GUC_FILES = ["src/postgres/src/backend/utils/misc/guc.c",
             "src/postgres/src/backend/utils/misc/guc_tables.c"]
PG_WRAPPER = "src/yb/yql/pgwrapper/pg_wrapper.cc"
YUGABYTED = "bin/yugabyted"
YBA_ROOT = "managed/src/main/java"
GITHUB_RAW = "https://raw.githubusercontent.com/yugabyte/yugabyte-db/%s/%s"
# Files a GitHub run searches, because raw GitHub has no grep. A git run records the files where
# it actually found each fact (source_paths in the cache), and later GitHub runs use those.
DEFAULT_SOURCE_PATHS = {
    "src/yb": ["src/yb/common/common_flags.cc", "src/yb/master/catalog_manager_bg_tasks.cc",
               "src/yb/server/server_common_flags.cc"],
    YBA_ROOT: [YBA_ROOT + "/com/yugabyte/yw/controllers/handlers/UniverseCRUDHandler.java"],
}


def vtuple(tag):
    return tuple(int(x) for x in re.findall(r"\d+", tag)[:4])


def onoff(v):
    v = v.strip().strip('"')
    return {"true": "on", "false": "off"}.get(v.lower(), v)


class GitSource:
    complete = True  # git grep searches the whole tree: "not found" means absent

    def __init__(self, repo):
        self.repo = repo

    def _git(self, *args):
        r = subprocess.run(["git", "-C", self.repo] + list(args), capture_output=True,
                           text=True, errors="replace")
        return r.stdout if r.returncode == 0 else None

    def tags(self, since):
        tags = [t for t in (self._git("tag", "-l", "v2*") or "").split()
                if re.match(r"^v\d+\.\d+\.\d+\.\d+$", t)]
        return sorted((t for t in tags if vtuple(t) >= vtuple(since)), key=vtuple)

    def show(self, tag, path):
        return self._git("show", "%s:%s" % (tag, path))

    def grep(self, tag, regex, pathspec, context=0):
        """[(path, text)] of matching lines (with `context` lines after)."""
        args = ["grep", "-n", "-E"] + (["-A%d" % context] if context else []) + \
            [regex, tag, "--", pathspec]
        out = []
        for line in (self._git(*args) or "").splitlines():
            m = re.match(r"^[^:]+:([^:]+)[:-]\d+[:-](.*)$", line)
            if m:
                out.append((m.group(1), m.group(2)))
        return out


class GitHubSource:
    """Reads single files of one tag from GitHub. Raw GitHub has no search, so grep() only
    searches a fixed list of files: DEFAULT_SOURCE_PATHS merged with the files earlier git
    extractions found facts in. A fact outside those files is missed, so callers must treat
    "not found" as unverified, not as absent."""

    complete = False

    def __init__(self, paths):
        self.paths = paths
        self.cache = {}

    def tags(self, since):
        raise RuntimeError("GitHub mode needs an explicit --release")

    def show(self, tag, path):
        key = (tag, path)
        if key not in self.cache:
            try:
                with urllib.request.urlopen(GITHUB_RAW % (tag, path), timeout=30) as r:
                    self.cache[key] = r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
                self.cache[key] = None
        return self.cache[key]

    def grep(self, tag, regex, pathspec, context=0):
        out = []
        for path in self.paths.get(pathspec, []):
            txt = self.show(tag, path) or ""
            lines = txt.splitlines()
            for i, line in enumerate(lines):
                if re.search(regex, line):
                    for j in range(i, min(len(lines), i + context + 1)):
                        out.append((path, lines[j]))
        return out


def enum_default(src, symbol):
    """Map an enum constant like YB_COST_MODEL_LEGACY to its GUC string value."""
    m = re.search(r'\{\s*"([\w-]+)"\s*,\s*%s\s*,' % re.escape(symbol), src)
    return m.group(1) if m else symbol


def guc_defaults(src, tag):
    text = "".join(src.show(tag, f) or "" for f in GUC_FILES)
    wrapper = src.show(tag, PG_WRAPPER) or ""
    over = {}
    for m in re.finditer(r"DEFINE_(?:RUNTIME_|NON_RUNTIME_)?PG_FLAG\(\s*\w+,\s*(\w+),\s*([^,]+),",
                         wrapper):
        over[m.group(1)] = onoff(m.group(2))
    out = {}
    for g in GUCS:
        m = re.search(r'\{\s*"%s"[\s\S]{0,1500}?&(\w+),\s*([^,\n]+),' % re.escape(g), text)
        if not m:
            out[g] = None
            continue
        v = m.group(2).strip()
        if re.match(r"^[A-Z_][A-Z0-9_]+$", v):
            v = enum_default(text, v)
        out[g] = over.get(g, onoff(v))
    return out


def gflag_defaults(src, tag, paths):
    out = {}
    for g in GFLAGS:
        hits = src.grep(tag, r"DEFINE_[A-Za-z_]+\(%s," % g, "src/yb", context=3)
        if not hits:
            out[g] = None
            continue
        paths.setdefault("src/yb", set()).add(hits[0][0])
        text = "\n".join(t for _, t in hits)
        m = re.search(r"DEFINE_([A-Za-z_]+)\(\s*%s\s*,([^;]*?)\)\s*;" % g, text, re.S)
        if not m:
            out[g] = None
            continue
        args = [a.strip() for a in m.group(2).split(",")]
        out[g] = onoff(args[2]) if "AUTO" in m.group(1) and len(args) >= 3 else \
            (onoff(args[0]) if args else None)
    return out


def anchors(src, tag, rules, cache):
    out = {}
    for rid, r in sorted(rules.items()):
        for ref in r.get("test", []):
            key = (tag, ref["file"])
            if key not in cache:
                txt = src.show(tag, REGRESS + ref["file"])
                if txt is None:
                    old = ref["file"].replace("yb.orig.", "yb_").replace("yb.port.", "yb_pg_")
                    txt = src.show(tag, REGRESS + old)
                cache[key] = txt or ""
            out.setdefault(rid, False)
            if ref["anchor"] in cache[key]:
                out[rid] = True
    return out


ASSIGN = re.compile(r"\b(%s)\s*=\s*([A-Za-z0-9_]+)" % "|".join(GUCS))


def _assignments(lines):
    found = {}
    for line in lines:
        s = line.strip()
        if s.startswith("#") or s.startswith("//") or s.startswith("*"):
            continue
        for m in ASSIGN.finditer(s):
            found.setdefault(m.group(1), set()).add(onoff(m.group(2)))
    return {k: (sorted(v)[0] if len(v) == 1 else sorted(v)) for k, v in sorted(found.items())}


OPT_IN = re.compile(r"PARITY|enhance_pg_compat|pg_compat|EPCM", re.I)


def _yugabyted_profiles(text):
    """Split yugabyted's injected settings into the default path and the opt-in Enhanced PG
    Compatibility path (constants or blocks whose surrounding code names PG parity /
    enhance_pg_compatibility)."""
    default, opt_in, window = [], [], []
    for line in text.splitlines():
        if ASSIGN.search(line):
            (opt_in if any(OPT_IN.search(w) for w in window) else default).append(line)
        if line.strip():
            window = (window + [line])[-12:]
    return _assignments(default), _assignments(opt_in)


def profiles(src, tag, paths):
    """Planner settings each deployment tool injects on this release, read from its code."""
    out = {}
    yd = src.show(tag, YUGABYTED) or ""
    default, opt_in = _yugabyted_profiles(yd)
    if default:
        out["yugabyted"] = default
    if opt_in:
        out["yugabyted --enhance_pg_compatibility"] = opt_in
    hits = src.grep(tag, r"ysql_pg_conf_csv.*(%s)\s*=" % "|".join(GUCS), YBA_ROOT)
    hits = [(p, t) for p, t in hits if "/test/" not in p]
    for p, _ in hits:
        paths.setdefault(YBA_ROOT, set()).add(p)
    got = _assignments(t for _, t in hits)
    if got:
        out["YBA new universe"] = got
    elif not src.complete:
        # GitHub runs search a fixed file list; YBA may set these elsewhere on this release.
        out["YBA new universe"] = "unverified"
    return out


def merge_paths(*maps):
    """Union of {pathspec: [files]} maps, per key, so no run loses another run's files."""
    out = {}
    for m in maps:
        for k, v in m.items():
            out.setdefault(k, set()).update(v)
    return {k: sorted(v) for k, v in sorted(out.items())}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", help="local yugabyte-db checkout")
    ap.add_argument("--github", action="store_true", help="fetch files from GitHub instead")
    ap.add_argument("--release", action="append", help="release(s) to (re)extract, e.g. "
                    "2026.1.2.0; default with --repo: every release since --tags-from")
    ap.add_argument("--tags-from", default="v2.20.0.0")
    ap.add_argument("--out", help="versions.json to update (default: ../rules/versions.json)")
    args = ap.parse_args()
    here = os.path.dirname(os.path.abspath(__file__))
    out = args.out or os.path.join(here, "..", "rules", "versions.json")
    rules = json.load(open(os.path.join(here, "..", "rules", "rules.json")))["rules"]
    try:
        data = json.load(open(out))
    except (OSError, ValueError):
        data = {}
    data.setdefault("tags", {})
    if args.github:
        if not args.release:
            ap.error("--github needs --release")
        sp = merge_paths(DEFAULT_SOURCE_PATHS, data.get("source_paths") or {})
        src = GitHubSource(sp)
        tags = ["v" + r.lstrip("v") for r in args.release]
    elif args.repo:
        src = GitSource(args.repo)
        tags = ["v" + r.lstrip("v") for r in args.release] if args.release else \
            src.tags(args.tags_from)
    else:
        ap.error("give --repo or --github")
    cache, paths = {}, {}
    for i, tag in enumerate(tags, 1):
        print("[%d/%d] %s" % (i, len(tags), tag), file=sys.stderr)
        if src.show(tag, GUC_FILES[0]) is None:
            print("  %s: not found at this source; skipped" % tag, file=sys.stderr)
            continue
        data["tags"][tag[1:]] = {"gucs": guc_defaults(src, tag),
                                 "gflags": gflag_defaults(src, tag, paths),
                                 "anchors": anchors(src, tag, rules, cache),
                                 "profiles": profiles(src, tag, paths),
                                 "source": "github" if args.github else "git"}
    if not args.github:
        data["source_paths"] = merge_paths(data.get("source_paths") or {}, paths)
    data.pop("deployment_profiles", None)
    data["source"] = "yugabyte-db release tags; regenerate with extract-version-data.py"
    with open(out, "w") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
        fh.write("\n")
    print("wrote %s (%d releases in table)" % (out, len(data["tags"])), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
