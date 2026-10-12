"""Release-aware facts from rules/versions.json, a local cache that scripts/extract-version-data.py
builds from the yugabyte-db source (`yb-model.py update-versions`). The cache is never committed:
without it every answer below is "unknown" and the review asks for the release to be built.

For the customer's release the engine asks three questions:

1. What are the planner settings when the bundle does not include pg_settings? The compiled
   default, unless a deployment tool overrides it (yugabyted, or YBA for new universes). When
   the tools disagree, or a tool's overrides could not be verified, the setting is
   *conditional* and findings that depend on it say so.
2. Does a feature a fix relies on exist on this release (for example merge scan streams)?
3. Is a rule's behaviour pinned by a regress test on this release?

and, for replay on a different image, which of those answers differ between the two releases.

When the cache has no entry for the customer's release, the answers come from the nearest
*earlier* release in the cache, never a newer one: a newer release may already fix what the
customer's release still does. The open items say which release stood in. A release older than
everything in the cache has no stand-in, and its facts are unknown.
"""

import json
import os
import re

_DATA = None


def _load():
    global _DATA
    if _DATA is None:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(here, "..", "rules", "versions.json")
        try:
            with open(path, encoding="utf-8") as fh:
                _DATA = json.load(fh)
        except (OSError, ValueError):
            _DATA = {"tags": {}, "deployment_profiles": []}
    return _DATA


def has_table():
    return bool(_load().get("tags"))


def vt(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:4])


def line(v):
    """Release line, e.g. 2025.2 or 2.20."""
    t = vt(v)
    return t[:2]


def resolve(version):
    """(tag, note): the customer's release, or the nearest earlier release in the cache, with a
    note when it is a stand-in. Never a newer release; None when nothing earlier is cached."""
    if not version:
        return None, "release unknown"
    tags = sorted(_load()["tags"], key=vt)
    if not tags:
        return None, "no release facts cached"
    want = vt(version)
    below = [t for t in tags if vt(t) <= want]
    if not below:
        return None, ("release %s is older than every release in the cache (oldest %s); its "
                      "release facts are unknown" % (version, tags[0]))
    t = below[-1]
    if vt(t) == want[:len(vt(t))]:
        return t, None
    where = "same release line" if line(t) == line(version) else "earlier release line"
    return t, ("release %s is not in the cache; using the nearest earlier release %s (%s)" %
               (version, t, where))


def gucs(tag):
    return dict(_load()["tags"].get(tag, {}).get("gucs", {}))


# A deployment tool whose overrides the extractor could not check on this release (GitHub runs
# search a fixed list of files). Every setting is then unknown for that tool.
UNVERIFIED = "unverified"


def profiles(tag):
    """Effective GUC values per deployment tool on this release: the compiled default, and
    each tool profile extract-version-data.py found in that release's source."""
    base = gucs(tag)
    out = {"compiled default (manual install, upgraded YBA universe)": dict(base)}
    for tool, sets in sorted((_load()["tags"].get(tag, {}).get("profiles") or {}).items()):
        if sets == UNVERIFIED:
            out[tool] = {k: UNVERIFIED for k in base}
            continue
        cur = dict(base)
        for k, v in sets.items():
            if isinstance(v, list):
                v = "/".join(v)  # the tool sets different values on different paths
            cur[k] = v
        out[tool] = cur
    return out


def setting(tag, name):
    """(value, source, per_profile): value is None when profiles disagree or it is absent."""
    if tag is None:
        return None, "release unknown", {}
    per = {k: v.get(name) for k, v in profiles(tag).items()}
    vals = set(per.values())
    if UNVERIFIED in vals:
        return None, "unknown on %s: %s not verified" % (
            tag, ", ".join(sorted(k for k, v in per.items() if v == UNVERIFIED))), per
    if vals == {None}:
        return None, "absent on %s" % tag, per
    if len(vals) == 1:
        return vals.pop(), "default on %s for every deployment tool" % tag, per
    return None, "differs by deployment tool on %s" % tag, per


def available(tag, name):
    if tag is None:
        return None
    return gucs(tag).get(name) is not None


def pinned(tag, rule):
    """True / False if the version table knows the rule on this release, None otherwise."""
    if tag is None:
        return None
    a = _load()["tags"].get(tag, {}).get("anchors", {})
    return a.get(rule)


def drift(tag_a, tag_b, names=None, rules=None):
    """Differences between two releases: settings and rule pinning."""
    out = {"settings": {}, "rules": [], "known": bool(tag_a and tag_b)}
    if not tag_a or not tag_b:
        return out
    pa, pb = profiles(tag_a), profiles(tag_b)
    keys = names or sorted(set(gucs(tag_a)) | set(gucs(tag_b)))
    for k in keys:
        va = {p: v.get(k) for p, v in pa.items()}
        vb = {p: v.get(k) for p, v in pb.items()}
        if va != vb:
            out["settings"][k] = {"customer": va, "replay": vb}
    for r in sorted(rules or []):
        if pinned(tag_a, r) != pinned(tag_b, r):
            out["rules"].append(r)
    return out


_OBS = None


def _obs():
    global _OBS
    if _OBS is None:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        try:
            with open(os.path.join(here, "..", "rules", "observations.json"),
                      encoding="utf-8") as fh:
                _OBS = json.load(fh)
        except (OSError, ValueError):
            _OBS = {"runs": [], "rules": {}}
    return _OBS


def observations(rule):
    """Oracle observations recorded for a rule: [{release, mode, observation}]."""
    return list(_obs().get("rules", {}).get(rule, []))


def has_observations():
    """True when a local oracle cache exists (evals/yb-model/oracle.py writes it)."""
    return bool(_obs().get("runs"))


def oracle_coverage(release):
    """The oracle runs recorded for this release, or None."""
    runs = [r for r in _obs().get("runs", []) if r.get("release") == release]
    return runs or None


def oracle_nearest(release):
    rels = sorted({r["release"] for r in _obs().get("runs", [])}, key=vt)
    if not rels or not release:
        return None
    return min(rels, key=lambda r: (abs(vt(r)[0] - vt(release)[0]) * 1000 +
                                    abs(vt(r)[1] - vt(release)[1]) * 100 +
                                    abs(vt(r)[2] - vt(release)[2])))
