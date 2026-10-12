"""Pitfalls: known gaps and input traps, in two files of one format.

- references/pitfalls.md ships with the skill: curated, generic, one engine status per entry.
- Each reviewer's own cache, $YBM_HOME/pitfalls.md (default ~/.yb-model/pitfalls.md), holds
  what that reviewer met that the shipped file does not cover yet. It lives outside the
  repository and the installed skill, so updates do not overwrite it and git does not pick it
  up. `yb-model.py pitfall export` prints it for a pull request.

An entry:

    ### <id>: <title>
    Engine: handled | flagged | not detected[ (<note>)]

    <what you see, why it happens, what to do>

handled: the engine deals with it. flagged: the engine detects it and adds an open item.
not detected: a known gap the reviewer has to watch for. In the shipped file a handled or
flagged entry names the test that pins it, as "(test: Class)" or "(test: Class.method)".

Each review records which shipped entries applied (review.json "pitfalls") and counts them in
$YBM_HOME/pitfall-hits.json, so the entries reviewers meet most stand out.
"""

import datetime
import json
import os
import re

STATUSES = ("handled", "flagged", "not detected")
HEAD = re.compile(r"^### ([a-z0-9][a-z0-9-]*): (.+?)\s*$")
ENGINE = re.compile(r"^Engine: (handled|flagged|not detected)\b\s*(.*?)\s*$")
TEST = re.compile(r"\(test: ([A-Za-z_][\w.]*)\)")
SHIPPED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "references",
                       "pitfalls.md")
LOCAL_HEADER = ("# My pitfalls\n\nWritten by `yb-model.py pitfall add`; same format as the "
                "skill's references/pitfalls.md. Generic only: no customer, table or value.\n")


def home():
    return os.environ.get("YBM_HOME") or os.path.join(os.path.expanduser("~"), ".yb-model")


def local_path():
    return os.path.join(home(), "pitfalls.md")


def parse(text):
    """Entries of a pitfalls file, in order: {id, title, status, note, body, section}."""
    out, cur, section = [], None, ""
    for line in text.splitlines():
        m = HEAD.match(line)
        if line.startswith("## "):
            section, cur = line[3:].strip(), None
            continue
        if m:
            cur = {"id": m.group(1), "title": m.group(2), "status": None, "note": "",
                   "body": [], "section": section}
            out.append(cur)
            continue
        if cur is None:
            continue
        e = ENGINE.match(line)
        if e and cur["status"] is None:
            cur["status"], cur["note"] = e.group(1), e.group(2)
        else:
            cur["body"].append(line)
    for c in out:
        c["body"] = "\n".join(c["body"]).strip()
    return out


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def shipped():
    return parse(_read(SHIPPED))


def local():
    return parse(_read(local_path()))


def render(e):
    return "### %s: %s\nEngine: %s%s\n\n%s\n" % (
        e["id"], e["title"], e["status"], (" " + e["note"]) if e.get("note") else "", e["body"])


# --- privacy -------------------------------------------------------------------------------
# An IPv4 address; not a release such as 2.20.1.3 (after "YB-" or "v", or before "-b").
_IP = re.compile(r"(?<![\w.-])(?<!YB-)(?<!v)\d{1,3}(?:\.\d{1,3}){3}(?!\w|\.\d|-b)")


def private_terms(bundle):
    """Names from the bundle that must never reach a pitfall: tables, indexes, constraints,
    schemas other than public, and the database."""
    from . import analyze
    sch = analyze.build_schema(bundle)
    terms = set()
    for key in list(sch.tables) + list(sch.indexes):
        terms.add(key)  # schema.name as the review writes it, and each part on its own
        for part in key.split("."):
            terms.add(part)
    for t in sch.tables.values():
        if t.pk is not None and t.pk.name:
            terms.add(t.pk.name)
    db = (bundle.meta or {}).get("database")
    if db:
        terms.add(db)
    host = _host((bundle.meta or {}).get("node"))
    if host:
        terms.add(host)
    return {t for t in terms
            if t and len(t) >= 3 and t.lower() != "public" and not _LOCAL.match(t)}


_LOCAL = re.compile(r"^(?:127\.\d+\.\d+\.\d+|::1?|0\.0\.0\.0)(?::\d+)?$")  # loopback


def _host(node):
    """The address in a collect.sql node label (host:port, an IPv6 host included)."""
    node = (node or "").strip()
    host = node.rsplit(":", 1)[0] if node.count(":") >= 1 else node
    return host if host and host.lower() not in ("local", "localhost") else None


_CREATE = re.compile(r'\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:UNIQUE\s+)?(?:MATERIALIZED\s+)?'
                     r'(?:TABLE|INDEX|SCHEMA|VIEW|SEQUENCE|DATABASE|TABLEGROUP)\s+'
                     r'(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?([\w."$]+)', re.I)


# Colon-separated hex groups; an IPv6 address when it has "::" or eight groups (not a time
# such as 12:34:56). collect.sql writes host:port, so the address without its last group is
# kept too.
_IP6 = re.compile(r"(?<![\w:])[0-9a-f]{0,4}(?::[0-9a-f]{0,4}){2,8}(?![\w:])", re.I)


def _ip6(text):
    out = set()
    for m in _IP6.finditer(text):
        a = m.group(0)
        if "::" in a or a.count(":") == 7:
            out.add(a)
            if "::" in a.rsplit(":", 1)[0]:
                out.add(a.rsplit(":", 1)[0])
    return out


def raw_terms(path):
    """Names in the CREATE statements of every .sql file under path, and the IP addresses in
    every file (node addresses in ybm_meta.csv, hosts in a dump header), in any encoding: also
    the fallback when the bundle itself cannot be read, which is when pitfalls get written."""
    files = [path] if os.path.isfile(path) else [
        os.path.join(d, f) for d, _, fs in os.walk(path) for f in fs]
    terms = set()
    for fp in files:
        try:
            with open(fp, "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        text = raw.decode("utf-16", "ignore") if b"\x00" in raw[:4096] else \
            raw.decode("utf-8", "ignore")
        terms.update(_IP.findall(text))
        terms.update(_ip6(text))
        if not fp.endswith(".sql"):
            continue
        for m in _CREATE.finditer(text):
            terms.update(p.strip('"') for p in m.group(1).split("."))
    return {t for t in terms
            if len(t) >= 3 and t.lower() != "public" and not _LOCAL.match(t)}


def leaks(text, terms):
    """Bundle names and addresses found in text (whole words, any case)."""
    low = text.lower()
    return sorted(t for t in terms
                  if re.search(r"(?<![\w$])%s(?![\w$])" % re.escape(t.lower()), low))


# --- the reviewer's cache ------------------------------------------------------------------
class Refused(Exception):
    pass


def add(pid, title, status, body, terms):
    """Append an entry to the reviewer's cache. Refuses a malformed entry, one that names
    anything from the bundle, and an id the shipped file already has (that is a hit)."""
    if not re.match(r"^[a-z0-9][a-z0-9-]*$", pid or ""):
        raise Refused("the id must be lower-case words joined by '-', e.g. pasted-query-table")
    if status not in STATUSES:
        raise Refused("the status must be one of: %s" % ", ".join(STATUSES))
    if not (title or "").strip() or not (body or "").strip():
        raise Refused("an entry needs a title and a body (what you see, why, what to do)")
    if "\n### " in "\n" + body or "\n## " in "\n" + body:
        raise Refused("the body cannot contain headings")
    found = leaks(" ".join([pid, title, body]), terms)
    if found:
        raise Refused("the entry names something from the bundle: %s. Describe it generically "
                      "(a table, an index, a node) and try again" % ", ".join(found))
    if any(e["id"] == pid for e in shipped()):
        record_hits([pid])
        raise Refused("%s is already in references/pitfalls.md; counted as a hit instead" % pid)
    if any(e["id"] == pid for e in local()):
        raise Refused("%s is already in %s" % (pid, local_path()))
    os.makedirs(home(), exist_ok=True)
    path = local_path()
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as fh:
        if new:
            fh.write(LOCAL_HEADER)
        fh.write("\n" + render({"id": pid, "title": title.strip(), "status": status,
                                "note": "", "body": body.strip()}))
    return path


def hits_path():
    return os.path.join(home(), "pitfall-hits.json")


def hits():
    try:
        with open(hits_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def record_hits(ids):
    """Count each id once more. Best effort: a read-only home only loses the count."""
    if not ids:
        return
    h = hits()
    today = datetime.date.today().isoformat()
    for i in ids:
        e = h.setdefault(i, {"count": 0})
        e["count"] += 1
        e["last"] = today
    try:
        os.makedirs(home(), exist_ok=True)
        tmp = hits_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(h, fh, indent=1, sort_keys=True)
        os.replace(tmp, hits_path())
    except OSError:
        pass


def listing():
    """Every entry the reviewer should know, shipped then local, with hit counts."""
    h = hits()
    rows = []
    for src, es in (("shipped", shipped()), ("mine", local())):
        for e in es:
            rows.append("%-7s %-30s %-13s %4d  %s" % (
                src, e["id"], e["status"], h.get(e["id"], {}).get("count", 0), e["title"]))
    head = "%-7s %-30s %-13s %4s  %s" % ("source", "id", "engine", "hits", "title")
    return "\n".join([head] + rows + ["", "Shipped: %s" % os.path.normpath(SHIPPED),
                                      "Mine:    %s" % local_path()])


def export():
    """The reviewer's entries that the shipped file does not have, ready for a pull request."""
    known = {e["id"] for e in shipped()}
    return "\n".join(render(e) for e in local() if e["id"] not in known)


# --- which shipped entries applied to a review ---------------------------------------------
def detect(bundle, sch, out):
    """Ids of shipped entries this review met, from what the engine saw."""
    ids = set()
    if (bundle.index_usage or bundle.pss) and not getattr(bundle, "usage_complete", False):
        ids.add("usage-one-node")
    if getattr(bundle, "capture_label_fallbacks", None) and len(bundle.captures) > 1:
        ids.add("node-label-loopback")
    if getattr(sch, "ambiguous", None) or getattr(sch, "unresolved", None) or \
            getattr(sch, "off_path", None):
        ids.add("several-schemas")
    if any(f["rule"] == "CFG004" for f in out.get("findings", [])):
        ids.add("plain-dump")
    if (getattr(bundle, "catalog_info", None) or {}).get("repairs"):
        ids.add("incomplete-dump")
    if bundle.stats_duplicates:
        ids.add("stats-duplicates")
    if any(i.method not in ("lsm", "hash", None) for i in sch.indexes.values()):
        ids.add("non-lsm-indexes")
    if any(getattr(i, "parent", None) for i in sch.indexes.values()):
        ids.add("partitioned-index-usage")
    if not bundle.tablets_cluster_wide and bundle.tablets:
        ids.add("tablet-counts-one-node")
    vm = ((out.get("replay") or {}).get("version_match")) or {}
    if vm and not vm.get("exact"):
        ids.add("replay-other-release")
    if len(sch.tables) > 300:
        ids.add("large-schemas")
    known = {e["id"] for e in shipped()}
    return sorted(ids & known)
