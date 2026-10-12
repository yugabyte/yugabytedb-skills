"""Render analyze output as the review document. Deterministic: same JSON, same Markdown.

The skill's narrative goes in the one marked slot (<!-- SUMMARY -->); everything else comes
from the engine so that every model produces the same findings, ranking and options.

A finding states why something is a problem and the options its rule offers, each with what
checking it alone found; it does not prescribe a change, and the review prints no DDL. Where
one finding on an object has an option that checks clean, it is offered as a suggestion with
its reasons; everything else is a decision for the reviewer and the user. `yb-model.py
sketch` prints the DDL of an option once it is chosen.
"""


def _opt(o):
    """One option with what checking it found."""
    if not o.get("checked"):
        tag = "not checked" if o["change"] != "none" else "no schema change"
    else:
        tag = ("costs: " + "; ".join(o["costs"])) if o["costs"] else (
            "checked: no cost found" if o["change"] != "none" else "no schema change")
    return "%s%s (%s)" % ("*" if o.get("sketched") else "", o["label"], tag)


def _place(res, fid):
    """('suggestion', entry) or ('decision', entry) for a finding, from the areas."""
    for d in res.get("dossiers") or []:
        for sg in d.get("suggestions") or []:
            if sg["finding"] == fid:
                return "suggestion", sg
        for dc in d.get("decisions") or []:
            if any(x["finding"] == fid for x in dc["findings"]):
                return "decision", dc
    return None, None


def _choices(f, res=None):
    """What to do about a finding, in one line: its suggestion with the reason, the options to
    decide between, or its suggestion when the rule offers no options."""
    kind, e = _place(res or {}, f["id"])
    if kind == "suggestion":
        return "suggested: %s, because %s" % (e["label"], " ".join(f["fact"].split()))
    opts = f.get("options") or []
    if opts:
        return "decide between " + " / ".join(o["label"] for o in opts)
    return " ".join((f.get("fix") or f.get("fact") or f["title"]).split())  # lint: its reason


BIND_SYMBOL = {"eq": "=", "in": "in", "range": "<>", "or_eq": "or=", "op": "op", "filter": "f",
               "none": "-"}


def _binding_text(m):
    """A key binding map in key order: = equality, in IN list, <> range, or= equality in every
    OR arm, f filter only, - unbound."""
    return " ".join(BIND_SYMBOL[c["code"]] for c in m["cols"])


def _plan_binding_text(a):
    """The replayed plan's binding, when it differs from the model's (another index, or
    another binding of the same one)."""
    pb = a.get("plan_binding")
    if not pb:
        return ""
    text = " ".join(BIND_SYMBOL[c["code"]] for c in pb["cols"])
    same = pb["index"] == a.get("path") and a.get("binding") and \
        text == _binding_text(a["binding"])
    return "" if same else " ; plan: %s [%s]" % (pb["index"], text)


def _rule_options(fs, rule):
    """Every option a rule offers across its findings: one finding may lack the drop (a unique
    index is never offered one)."""
    opts = {}
    for f in fs:
        if f["rule"] == rule:
            for o in f.get("options") or []:
                opts.setdefault(o["id"], o)
    return list(opts.values())


def _cell(s, n=None):
    s = (s or "").replace("|", "\\|").replace("\n", " ")
    s = " ".join(s.split())
    if n and len(s) > n:
        s = s[:n - 3] + "..."
    return s


def _fmt(n):
    if n is None:
        return "?"
    n = float(n)
    for unit, div in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(n) >= div:
            return ("%.1f%s" % (n / div, unit)).replace(".0" + unit, unit)
    return "%d" % n if n == int(n) else "%.1f" % n


def _conf(f):
    return "; ".join([f["confidence"]] + list(f.get("caveats") or []))


def static_warning(res):
    """The warning a review without a scratch server carries at its top, or None."""
    if res.get("replay"):
        return None
    return ("**Static-only review: no scratch YugabyteDB was run.** Nothing here was proven on a "
            "server: access paths, key bindings and costs are the engine's predictions, so this "
            "review is fuzzier and significantly less accurate than one checked on a cluster. "
            "For a proven review, run it again with a local yugabytedb/yugabyte image of the "
            "release (`review --replay --measure`; pulling the image needs the user's OK).")


def review_level(res):
    """One line: what kind of review this is, and whether inputs that change it are missing."""
    level = "structural review (DDL only)"
    if res["patterns"] and any(p["source"] == "pg_stat_statements" for p in res["patterns"]):
        level = "workload-weighted review"
    elif res["patterns"]:
        level = "query-list review (patterns unranked)"
    if res.get("replay"):
        level += " with plan replay on %s" % res["replay"].get("version")
    else:
        level += ", static only (nothing proven on a server)"
    major = [m for m in (res.get("preflight") or {}).get("missing", []) if m["tier"] == "major"]
    if major:
        level += ", INCOMPLETE: %d major input(s) missing, run with --accept-missing" % len(major)
    return level


def render_chat(res, review_path="review.md"):
    """The chat message, written by the engine like the rest of the review: the headline, the
    top three findings with their options, the review level and, for an incomplete review, what
    is missing and which candidates were withheld. The skill pastes it unchanged."""
    L = [headline(res), ""]
    if static_warning(res):
        L += [static_warning(res), ""]
    top = [f for f in res["findings"] if f.get("section") not in ("hygiene", "disputed")][:3]
    if top:
        L.append("Top findings:")
        for n, f in enumerate(top, 1):
            L.append("%d. **%s %s** (%s): %s, %s. %s" % (
                n, f["id"], f["rule"], f["severity"], f["title"], f["object"],
                _choices(f, res)))
        L.append("")
    L.append("Review level: %s." % review_level(res))
    pf = res.get("preflight") or {}
    major = [m for m in pf.get("missing", []) if m["tier"] == "major"]
    if major:
        L.append("Missing inputs that change the findings: %s." % "; ".join(
            m["label"].split(";")[0] for m in major))
        held = sorted((r, v["withheld"]) for r, v in (pf.get("muted") or {}).items()
                      if v.get("withheld"))
        if held:
            L.append("Withheld candidates (collect the inputs to evaluate them): %s." % "; ".join(
                "%s: %s" % (r, ", ".join(w[:6]) + (" ..." if len(w) > 6 else ""))
                for r, w in held))
    disputed = [f["id"] for f in res["findings"] if f.get("section") == "disputed"]
    if disputed:
        L.append("Disputed by replay under assumed planner settings, kept but not recommended: "
                 "%s." % ", ".join(disputed))
    L += ["", "Full review: %s" % review_path]
    return "\n".join(L) + "\n"


def headline(res):
    """The review's opening sentences, written by the engine so every model says the same."""
    fs = [f for f in res["findings"] if f.get("section") not in ("hygiene", "disputed")] or \
        [f for f in res["findings"] if f.get("section") != "disputed"]
    if not fs:
        return "**No findings.** See open items for what could not be checked."
    share = {p["id"]: p.get("time_share") for p in res["patterns"]}
    counts = {}
    for f in fs:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    order = ["critical", "high", "medium", "low", "info"]
    tally = ", ".join("%d %s" % (counts[k], k) for k in order if counts.get(k))
    top = fs[0]
    sh = [share.get(p) for p in top["patterns"] if share.get(p) is not None]
    where = ""
    if top["patterns"]:
        more = len(top["patterns"]) - 3
        where = " on %s%s" % (", ".join(top["patterns"][:3]),
                              " and %d more" % more if more > 0 else "")
        if sh:  # the share of every pattern the sentence covers, the "more" included
            where += " (%.1f%% of total statement time)" % (100 * sum(sh))
    first = _choices(top, res).split(". ")[0].rstrip(".")
    pf = res.get("preflight") or {}
    major = [m["label"].split(":")[0].split(" (")[0] for m in pf.get("missing", [])
             if m["tier"] == "major"]
    gap = (" Inputs missing, so this review is incomplete: %s (see Muted checks)." %
           ", ".join(major)) if major else ""
    return ("**Worst finding: %s** (%s, %s): %s, %s%s. Start here: %s. %d findings in total "
            "(%s).%s" % (top["id"], top["severity"], _conf(top), top["title"], top["object"],
                         where, first, len(fs), tally, gap))


def render(res):
    L = []
    w = L.append
    ps = res["settings"]
    w("# YugabyteDB schema review")
    w("")
    w(headline(res))
    w("")
    if static_warning(res):
        w("> " + static_warning(res))
        w("")
    w("<!-- NOTES: optionally replace this line with at most two sentences of context the engine "
      "could not know (from the user). Do not restate or change findings or numbers. -->")
    w("")
    w("## What this review is based on")
    w("")
    w("- Inputs: %s" % (", ".join(res["inputs"]) or "none"))
    about = (["from " + res["version_source"]] if res.get("version_source") else []) + (
        ["rules use release data for %s" % ps["release"]] if ps.get("release") else [])
    w("- Version: %s%s" % (res["version"] or "unknown",
                           (" (%s)" % "; ".join(about)) if about else ""))
    srcs = ps.get("sources") or {}
    if srcs:
        w("- Settings source: %s" % "; ".join("%s from %s" % (k, v)
                                              for k, v in sorted(srcs.items())))
    w("- Planner: %s; bitmap scans %s; yb_max_merge_scan_streams %s; database colocated %s" % (
        ps.get("cbo_setting") or "cost model unknown",
        {True: "on", False: "off", None: "unknown"}[ps.get("bitmap")],
        ps.get("merge_streams") if ps.get("merge_streams") is not None else "unknown",
        {True: "yes", False: "no", None: "unknown"}[ps.get("colocated")]))
    rp = res.get("replay")
    pf = res.get("preflight") or {}
    w("- Review level: %s" % review_level(res))
    w("- Schema model: %s" % (
        "read from a scratch server's catalog" if res.get("schema_model") == "catalog" else
        "read from the DDL text (the scratch server's catalog could not be read: %s)" %
        res["catalog_error"] if res.get("catalog_error") else
        "read from the DDL text (no scratch server ran; see pitfalls static-only)"))
    w("- Linter: %s" % ("ran (yb-lint.py)" if res["lint_ran"] else "did not run"))
    w("")

    w("## Assumptions and unknowns")
    w("")
    if res["open_items"]:
        for o in res["open_items"]:
            w("- %s" % o)
    else:
        w("- None recorded.")
    if res.get("pitfalls"):
        w("")
        w("Known pitfalls that apply (references/pitfalls.md): %s." % ", ".join(res["pitfalls"]))
    w("")

    if pf.get("missing"):
        w("### Muted checks (inputs missing)")
        w("")
        w("A muted rule was not evaluated, because its result would rest on a guess. A weakened "
          "rule still ran, and each of its findings carries the caveat in its confidence.")
        w("")
        w("| Missing input | Tier | Muted rules | Weakened rules (caveat) | Skipped or reduced |")
        w("|---|---|---|---|---|")
        muted = pf.get("muted") or {}
        for m in pf["missing"]:
            mr = []
            for r in m["mutes"]:
                n = len((muted.get(r) or {}).get("withheld") or [])
                mr.append("%s (%d withheld)" % (r, n) if n else r)
            w("| %s | %s | %s | %s | %s |" % (
                _cell(m["label"], 80), m["tier"], ", ".join(mr) or "none",
                ("%s (%s)" % (", ".join(m["weakens"]), m["caveat"])) if m["weakens"] else "none",
                _cell("; ".join(m["stages"] + (
                    ["query list declared complete, so %s still runs (marked 'workload declared "
                     "complete')" % ", ".join(m["declared_complete_runs"])]
                    if m.get("declared_complete_runs") else [])), 300) or "none"))
        w("")
        held = sorted(r for r, v in (pf.get("muted") or {}).items() if v.get("withheld"))
        if held:
            w("Withheld candidates (the rule matched, but on inputs too thin to report): %s. "
              "Collect the missing inputs and review again to evaluate them." % "; ".join(
                  "%s: %s" % (r, ", ".join(pf["muted"][r]["withheld"][:6]) +
                              (" ..." if len(pf["muted"][r]["withheld"]) > 6 else ""))
                  for r in held))
            w("")
    w("## Access patterns")
    w("")
    if not res["patterns"]:
        w("No access patterns supplied.")
    else:
        w("| ID | Weight | Calls | Time share | Kind | Access path | Query |")
        w("|---|---|---|---|---|---|---|")
        shown = res["patterns"][:40] + [p for p in res["patterns"][40:]
                                        if p["weight"] == "UNRANKED"]
        for p in shown:
            acc = "; ".join("%s: %s%s%s%s%s" % (
                a["table"], a["path"] or "scan",
                (" [%s]" % _binding_text(a["binding"])) if a.get("binding") else "",
                _plan_binding_text(a),
                " (point)" if a["point"] else "",
                " (skip scan)" if a["skip_scan"] else "") for a in p.get("access", []))
            if p.get("plan_summary"):
                acc += " ; replay: " + p["plan_summary"]
            w("| %s | %s | %s | %s | %s | %s | `%s` |" % (
                p["id"], p["weight"], _fmt(p["calls"]) if p["calls"] is not None else "",
                ("%.1f%%" % (100 * p["time_share"])) if p["time_share"] is not None else "",
                p["kind"], _cell(acc, 160), _cell(p["query"], 140)))
        if len(res["patterns"]) > len(shown):
            w("")
            w("%d further patterns are in the JSON output." % (len(res["patterns"]) - len(shown)))
        if any(a.get("binding") for p in shown for a in p.get("access", [])):
            w("")
            w("[ ] is how the pattern binds each column of that key, in key order: = equality, "
              "in IN list, <> range, or= equality in every OR arm, op another operator the "
              "index serves (as GIN does), f filter only, - unbound. "
              "A column bound after a - or f is reached by seeking under every value of the "
              "columns before it (review.json: access.binding).")
    w("")

    w("## By area")
    w("")
    dos = res.get("dossiers") or []
    if not dos:
        w("No findings.")
    else:
        w("The findings grouped by the tables a change can reach (partitions with their parent, "
          "tables joined by a foreign key). A **suggestion** is the only finding on its object, "
          "whose option only adds something (a new index) and checked alone with no cost found; "
          "it comes with its reasons, for the user to confirm. Everything else is a "
          "**decision**: an object several findings name, or an option that drops, rebuilds or "
          "re-keys something, has a cost or could not be checked. Weigh its options with the "
          "user, who knows how the application will grow. No option is applied, and the review "
          "prints no DDL.")
        w("")
        for d in dos:
            w("### %s (worst: %s)" % (", ".join(d["area"][:6]) + (
                " and %d more" % (len(d["area"]) - 6) if len(d["area"]) > 6 else ""),
                d["worst"]))
            w("")
            for sg in d.get("suggestions") or []:
                w("- **Suggestion** for %s (%s %s, %s): %s. Why: %s Check first: %s.%s" % (
                    _cell(sg["object"], 60), sg["finding"], sg["rule"], sg["about"],
                    sg["label"], _cell(sg["why"], 400), sg["check_first"],
                    (" Other options: %s." % " / ".join(sg["others"])) if sg["others"] else ""))
            for dc in d.get("decisions") or []:
                w("- **Decision** on %s:" % _cell(dc["object"], 60))
                for x in dc["findings"]:
                    w("  - %s %s, %s: %s" % (x["finding"], x["rule"], x["about"], " / ".join(
                        _opt(o) for o in x["options"])))
            notes = d.get("notes") or []
            by_rule = {}
            for nt in notes:
                by_rule.setdefault(nt["rule"], []).append(nt)
            for rule, group in by_rule.items():
                if len(group) > 3:  # one line per rule: the objects, not a line each
                    w("- %s on %d objects (%s): %s" % (rule, len(group), _cell(", ".join(
                        nt["object"] for nt in group[:8]) + (" ..." if len(group) > 8 else ""),
                        120), _cell(group[0]["about"], 90)))
                    continue
                for nt in group:
                    w("- %s %s on %s: %s" % (nt["finding"], nt["rule"], _cell(nt["object"], 60),
                                              _cell(nt["about"], 90)))
            if d["must_keep"]:
                w("")
                w("Must stay true:")
                for e in d["must_keep"][:15]:
                    w("- %s" % e)
                if len(d["must_keep"]) > 15:
                    w("- ... %d more in review.json" % (len(d["must_keep"]) - 15))
            if d["tensions"]:
                w("")
                w("Tensions:")
                for t in d["tensions"]:
                    w("- %s" % t)
            w("")

    w("## Findings")
    w("")
    allf = res["findings"]
    fs = [f for f in allf if f.get("section") not in ("hygiene", "disputed")]
    hyg = [f for f in allf if f.get("section") == "hygiene"]
    disp = [f for f in allf if f.get("section") == "disputed"]
    if not fs:
        w("No findings.")
    else:
        w("| ID | Severity | Confidence | Rule | Object | Patterns |")
        w("|---|---|---|---|---|---|")
        lint_rows = {}
        for f in fs:
            if f["severity"] == "info" and f["rule"].startswith("LINT-"):
                lint_rows.setdefault(f["rule"], []).append(f)
                if len(lint_rows[f["rule"]]) > 1:
                    continue  # one row per rule: the count stands for the rest
        for f in fs:
            group = lint_rows.get(f["rule"]) if f["severity"] == "info" else None
            if group and len(group) > 1:
                if f is group[0]:
                    w("| %s | info | %s | %s | %d objects (%s) | |" % (
                        "%s..%s" % (group[0]["id"], group[-1]["id"]), _conf(f), f["rule"],
                        len(group), _cell(", ".join(g["object"] for g in group[:6]) +
                                          (" ..." if len(group) > 6 else ""), 60)))
                continue
            w("| %s | %s | %s | %s | %s | %s |" % (
                f["id"], f["severity"], _conf(f), f["rule"], _cell(f["object"], 60),
                ", ".join(f["patterns"][:8]) + (" ..." if len(f["patterns"]) > 8 else "")))
        w("")
        first = {}
        for f in fs:
            if f["severity"] == "info" and f["rule"].startswith("LINT-"):
                continue
            same = (f["rule"], f.get("table"), f["severity"],
                    tuple(o.get("label") for o in f.get("options") or []))
            if f.get("table") and f.get("options") and same in first:
                # Same rule, table, severity and options as an earlier finding: its own fact only.
                w("- **%s** (%s, as %s): %s" % (f["id"], _cell(f["object"], 60), first[same],
                                               _cell(f["fact"], 300)))
                w("")
                continue
            first.setdefault(same, f["id"])
            w("### %s %s: %s" % (f["id"], f["rule"], f["title"]))
            w("")
            w("- **Fact:** %s" % f["fact"])
            if f.get("mechanism"):
                w("- **Mechanism:** %s" % f["mechanism"])
            if f.get("fix"):
                w("- **Why it matters here:** %s" % f["fix"])
            if f.get("options"):
                w("- **Options** (see Options by rule; * is the one the engine models by "
                  "default): %s" % " / ".join(_opt(o) for o in f["options"]))
            if f.get("measured"):
                w("- **Measured:** %s" % f["measured"])
            if f.get("replay"):
                w("- **Replay:** %s" % f["replay"])
            if f.get("evidence_needed"):
                w("- **Evidence needed:** %s" % f["evidence_needed"])
            elif f["confidence"] == "probable" and f["rule"].startswith("CAP"):
                w("- **Evidence needed:** a replay with statistics, or EXPLAIN (ANALYZE, DIST) "
                  "of %s on the target if it is a SELECT (ANALYZE executes the statement; plan "
                  "a write with plain EXPLAIN)." % (", ".join(f["patterns"]) or "the pattern"))
            if f.get("verified"):
                w("- **Probe on the release:** %s" % f["verified"])
            elif f.get("rule") in (res.get("probe_rules") or []):
                w("- **Probe on the release:** not run (no replay); the mechanism is unverified "
                  "on this release.")
            if f.get("observed"):
                w("- **Observed by the planner oracle:** %s" % "; ".join(
                    "%s (%s): %s" % (o["release"], o["mode"], o["observation"])
                    for o in f["observed"][:4]))
            if f.get("test_refs"):
                w("- **Pinned by:** %s" % "; ".join(
                    "`%s` (\"%s\")" % (t["file"], t["anchor"]) for t in f["test_refs"]))
            elif f.get("basis") == "computed from the bundle":
                ins = f.get("basis_inputs") or ["the bundle"]
                w("- **Basis:** computed from %s; no planner behaviour is involved, so no "
                  "regress test applies." % (", ".join(ins[:-1]) + " and " + ins[-1]
                                             if len(ins) > 1 else ins[0]))
            w("")
        infos = [f for f in fs if f["severity"] == "info" and f["rule"].startswith("LINT-")]
        if infos:
            by_rule = {}
            for f in infos:
                by_rule.setdefault(f["rule"], []).append(f)
            w("Informational lint (%d): %s" % (len(infos), "; ".join(
                ("%s %s" % (g[0]["id"], _cell(g[0]["fact"], 90))) if len(g) == 1 else
                "%s on %d objects, e.g. %s" % (r, len(g), _cell(g[0]["fact"], 90))
                for r, g in by_rule.items())))
            w("")

    w("## Options by rule")
    w("")
    seen = []
    for f in fs:
        if f.get("options") and f["rule"] not in seen:
            seen.append(f["rule"])
    if not seen:
        w("No finding offers options.")
    for rule in seen:
        f = next(x for x in fs if x["rule"] == rule)
        w("**%s, %s:**" % (rule, f["title"]))
        w("")
        for o in _rule_options(fs, rule):
            w("- %s: when %s. Trade-off: %s. Check first: %s." % (
                o["label"], o["when"], o["tradeoff"], o["check_first"]))
        w("")

    w("## Disputed by replay (planner settings assumed)")
    w("")
    if not disp:
        w("None.")
    else:
        w("The bundle had no pg_settings, so replay ran under assumed planner settings, and "
          "under them the planner disagreed with these findings. They are kept, because the "
          "customer's real settings may differ, but they are not part of the suggestions "
          "or decisions. Collect `ybm_settings.csv` (collect.sql) and review again to settle "
          "them.")
        w("")
        w("| ID | Severity | Rule | Object | Finding | Why disputed | Why it would matter |")
        w("|---|---|---|---|---|---|---|")
        for f in disp:
            w("| %s | %s | %s | %s | %s | %s | %s |" % (
                f["id"], f["severity"], f["rule"], _cell(f["object"], 60),
                _cell(f["fact"], 200), _cell(f["disputed"], 220), _cell(f["fix"], 160)))
    w("")
    w("## Workload hygiene (confirm with the application team)")
    w("")
    if not hyg:
        w("Nothing to raise." if any(p.get("source") == "pg_stat_statements"
                                     for p in res["patterns"])
          else "Not assessed: needs pg_stat_statements.")
    else:
        w("How the application uses the database, measured from pg_stat_statements. These are "
          "not schema defects; the fix is usually in application code, the ORM or the driver.")
        w("")
        w("| ID | Severity | Rule | What was measured | Suggested action |")
        w("|---|---|---|---|---|")
        for f in hyg:
            w("| %s | %s | %s %s | %s | %s |" % (f["id"], f["severity"], f["rule"],
                                                 _cell(f["title"], 60), _cell(f["fact"], 220),
                                                 _cell(f["fix"], 200)))
    w("")
    w("## What is already sound")
    w("")
    if res["sound"]:
        for s in res["sound"]:
            w("- %s" % s)
    else:
        w("- Nothing could be confirmed as sound from the inputs given.")
    w("")

    w("## Validation and limitations")
    w("")
    lint = res["lint"]
    if res["lint_ran"]:
        if lint:
            w("yb-lint.py: %d finding(s) (%d superseded by statistics-based rules)." % (
                len(lint), sum(1 for l in lint if l["superseded"])))
        else:
            w("yb-lint.py: no findings.")
    else:
        w("yb-lint.py did not run; DDL is unlinted.")
    w("")
    w("Options: each one the engine can model (its own sketch, a drop of a non-unique index, "
      "or no schema change) was applied alone to a model of the schema and checked for lost "
      "uniqueness, lost ON CONFLICT targets, foreign keys left without an index, indexes in "
      "use dropped, defects brought back, and plan regressions on every ranked pattern; what "
      "it found is shown with the option. Options marked not checked were not modelled. The "
      "combination chosen for a plan is checked by `yb-model.py plan`; the DDL of a chosen "
      "option is printed by `yb-model.py sketch`, never in this review.")
    w("")
    if rp:
        w("Replay on `%s` (%s): %d pattern(s) planned, %d failed. DDL errors on replay: %s. "
          "Statistics were injected, not measured on the target; plans are estimates." % (
              rp.get("version"), rp.get("mode"), len(rp.get("planned", [])),
              len(rp.get("failed", [])), rp.get("ddl_errors", "n/a")))
        if rp.get("repairs"):
            w("")
            w("The schema dump is incomplete; the scratch database was completed with: %s." %
              "; ".join(rp["repairs"]))
        if rp.get("ddl_rejected"):
            from .server import scrub
            w("")
            w("DDL the scratch server rejects (the input, as the server reads it): %s.%s" % (
                "; ".join(scrub(e) for e in rp["ddl_rejected"]),
                (" Not planned because the tables they read did not load: %s." %
                 "; ".join(rp["blocked"])) if rp.get("blocked") else ""))
        if rp.get("statistics_missing"):
            w("")
            w("The replayed plans were chosen without %s, on the planner's defaults: they "
              "corroborate or dispute findings but confirm, remove or raise none, and the engine's "
              "model chose each pattern's access (where the plan differs is listed below)." %
              " or ".join(rp["statistics_missing"]))
        vm = rp.get("version_match") or {}
        if vm and not vm.get("exact", True) and rp.get("drift_unknown"):
            w("")
            w("Replay ran on %s, not the customer's %s, and without release facts for both "
              "the differences between the two releases could not be computed. Replay "
              "therefore confirmed and refuted nothing; access-path findings stay as the "
              "static review left them." % (rp.get("version"), vm.get("customer")))
        elif vm and not vm.get("exact", True):
            d = vm.get("drift") or {}
            w("")
            w("Replay ran on %s, the nearest image to the customer's %s. Differences between "
              "the two releases: settings %s; rules whose tested behaviour differs: %s. Those "
              "rules were not confirmed by this replay." % (
                  vm.get("image_release"), vm.get("customer"),
                  ", ".join(sorted(d.get("settings", {}))) or "none",
                  ", ".join(d.get("rules", [])) or "none"))
            if rp.get("access_from_plans") is False:
                w("One of those rules decides access paths, so the replayed plans' access was "
                  "used for no pattern: the engine's model chose every access, and no access-path "
                  "finding is confirmed by this replay.")
        pr = rp.get("probes") or {}
        if pr:
            w("")
            w("Rule probes on `%s` (generated rows, EXPLAIN (ANALYZE, DIST), with DEBUG where "
              "a probe counts seeks): %s." % (
                rp.get("version"), "; ".join("%s %s" % (k, v["verdict"])
                                             for k, v in sorted(pr.items()))))
        gp = [g for g in res.get("gaps") or [] if not g.get("layer", "").startswith("gen.")]
        compared = sum(1 for p in res.get("patterns", []) for a in p.get("access", [])
                       if a.get("plan_binding"))
        if compared or gp:
            by = {}
            for g in gp:
                by[g["layer"]] = by.get(g["layer"], 0) + 1
            w("")
            w("Server checks on the replayed plans: %d key binding(s) compared with the engine's "
              "model; disagreements recorded as gaps (review.json gaps): %s." % (
                  compared, ", ".join("%s %d" % kv for kv in sorted(by.items())) or "none"))
            paths = [g for g in gp if g["layer"] == "path"]
            weak = [g for g in paths if g.get("taken") == "model"]
            paths = [g for g in paths if g.get("taken") != "model"]
            if weak:  # plans chosen without statistics: the model's access stood
                w("")
                w("Where the replayed plan, chosen without the customer's statistics, took another "
                  "access than the engine's model (the review follows the model; check these on "
                  "the cluster): %s.%s" % ("; ".join(
                      "%s on %s: model %s, plan %s" % (
                          g["pattern"], g["table"], g["model"], g["server"])
                      for g in weak[:20]), " (%d more in review.json)" % (len(weak) - 20)
                      if len(weak) > 20 else ""))
            if paths:  # the plan's access stood; what the engine's model would have read instead
                w("")
                w("Where the replayed plan took another access than the engine's model (the "
                  "review follows the plan; findings the model's access would have made are not "
                  "raised): %s.%s" % ("; ".join(
                      "%s on %s: plan %s, model %s" % (
                          g["pattern"], g["table"], g["server"], g["model"])
                      for g in paths[:20]), " (%d more in review.json)" % (len(paths) - 20)
                      if len(paths) > 20 else ""))
        ms = res.get("measured") or {}
        if ms.get("patterns") or ms.get("tables") or ms.get("joins"):
            done = [m for m in ms.get("patterns", []) if "returned" in m]
            flagged = [m for m in done if m.get("anomalies") and not m.get("plan_flip")]
            unexplained = [m["id"] for m in flagged if not m.get("explained_by") and
                           not m.get("withheld")]
            withheld = [m for m in flagged if m.get("withheld")]
            skipped = ["%s (%s)" % (t, v["reason"]) for t, v in sorted(ms.get("tables",
                                                                               {}).items())
                       if not v.get("measured")]
            w("")
            sizes = sorted({v["per_prefix"] for v in ms.get("tables", {}).values()
                            if v.get("measured") and v.get("per_prefix")})
            have = set((res.get("preflight") or {}).get("present") or [])
            stats = [x for x in ("pg_stats", "reltuples") if x not in have]
            w("Measured pass on generated samples (%s rows per hash prefix; %s): %d read(s) "
              "measured, %d over a threshold, %s.%s%s" % (
                  ("%d" % sizes[0]) if len(sizes) == 1 else
                  ("%d to %d" % (sizes[0], sizes[-1])) if sizes else "no",
                  "shaped by the statistics, plans chosen under the full-scale statistics"
                  if not stats else "without %s, so the sample's shape is a default and the "
                  "plans are chosen on the sample itself" % " or ".join(
                      dict((("pg_stats", "column statistics"),
                            ("reltuples", "table sizes")))[x] for x in stats),
                  len(done), len(flagged),
                  ("%d unexplained (MEAS001: %s)" % (len(unexplained), ", ".join(unexplained)))
                  if unexplained else "none unexplained" if withheld else
                  "every one explained by a finding",
                  (" Not measured: %s." % "; ".join(skipped + (
                      ["joins (the pass runs single-table reads): %s" % ", ".join(
                          ms["joins"])] if ms.get("joins") else []))) if skipped or
                  ms.get("joins") else "",
                  (" Could not run: %s." % "; ".join("%s (%s)" % (m["id"], m["error"])
                                                      for m in ms.get("patterns", [])
                                                      if m.get("error"))) if any(
                      m.get("error") for m in ms.get("patterns", [])) else ""))
            if withheld:
                w("")
                w("Measured anomalies not raised as MEAS001 (%s): the %s." % (
                    ", ".join(m["id"] for m in withheld),
                    withheld[0]["withheld"].replace("not used: ", "", 1)))
            gen = [g for g in res.get("gaps") or [] if g.get("layer", "").startswith("gen.")]
            lost = ["%s (%s of %s rows loaded)" % (g["table"], g["server"], g["model"])
                    for g in gen if g["layer"] == "gen.filtered"]
            gone = ["%s.%s" % (g["table"], g.get("constraint") or g["model"]) for g in gen
                    if g["layer"] in ("gen.check_dropped", "gen.constraint_dropped",
                                      "gen.fk_cycle") or g.get("constraint")]
            shape = ["%s.%s (%s)" % (g["table"], g["model"], "; ".join(
                ([("%s distinct where the plan made %s" % (g["distinct"],
                                                         g["planned_distinct"]))]
                 if "distinct" in g else []) +
                ([("null fraction %.2f where the plan made %.2f" % (g["null_frac"],
                                                                   g["planned_null_frac"]))]
                 if "null_frac" in g else []) or [g.get("server") or "not counted"]))
                for g in gen if g["layer"] == "gen.shape"]
            short = ["%s.%s (%d of %d)" % (g["table"], g["model"], g["held"], g["bound"])
                     for g in gen if g["layer"] == "gen.literals"]
            shown = ("gen.filtered", "gen.check_dropped", "gen.constraint_dropped",
                     "gen.fk_cycle", "gen.shape", "gen.plan_flip", "gen.empty", "gen.literals")
            other = {}
            for g in gen:
                if g["layer"] not in shown and not g.get("constraint"):
                    other[g["layer"]] = other.get(g["layer"], 0) + 1
            flips = ["%s (%s)" % (g["pattern"], ", ".join(g["server"])) for g in gen
                     if g["layer"] == "gen.plan_flip"]
            empty = [g["pattern"] for g in gen if g["layer"] == "gen.empty"]
            if lost or gone:
                w("")
                w("%s%s%s" % (
                    "The samples obey the schema's own constraints." if not gone else
                    "The samples obey the schema's constraints except those listed here.",
                    (" Rows the constraints rejected: %s." % "; ".join(lost)) if lost else "",
                    (" Constraints the generator could not satisfy, dropped from the scratch "
                     "copy: %s." % "; ".join(gone)) if gone else ""))
            if short or other:
                w("")
                w("Sample limits:%s%s" % (
                    (" values the workload binds that the sample could not hold (held of "
                     "bound): %s." % "; ".join(short)) if short else "",
                    (" other generator records (review.json gaps): %s." % ", ".join(
                        "%s %d" % kv for kv in sorted(other.items()))) if other else ""))
            if shape or flips or empty:
                w("")
                w("Round trip:%s%s%s" % (
                    (" sample columns not as planned: %s." % "; ".join(shape))
                    if shape else "",
                    (" plans that changed on the sample, so their counters are not used: %s."
                     % "; ".join(flips)) if flips else "",
                    (" reads that returned no row on the sample: %s." % ", ".join(empty))
                    if empty else ""))
        if rp.get("unplannable"):
            w("")
            w("Not planned on replay (a replay limitation, not a defect; add casts such as "
              "`$2::boolean` to plan them): %s." % "; ".join(rp["unplannable"]))
        if rp.get("assumed_settings"):
            w("")
            w("Replay assumed: %s." % "; ".join("%s=%s" % kv for kv in
                                                 sorted(rp["assumed_settings"].items())))
    else:
        w("No plan replay was run: access-path findings are static predictions (probable).")
    if res["reconciliation"]:
        w("")
        w("Findings removed because the replayed plan contradicted them:")
        w("")
        for r in res["reconciliation"]:
            w("- %s %s (%s): %s" % (r["finding"], r["object"], ", ".join(r["patterns"]),
                                     r["plan"]))
    w("")
    fp = res.get("fixpoint")
    if fp is not None:
        w("")
        if fp.get("problems"):
            w("Self-check (apply each finding's default option, review again): PROBLEMS. %s" %
              "; ".join(fp["problems"]))
        else:
            w("Self-check: the engine's default option for each of %d findings, applied to a "
              "model of the schema (options that touch different objects together, in %d "
              "batch(es)), resolves its own finding and adds no new schema finding at medium or "
              "above. Workload and plan findings describe the workload as measured and are not "
              "re-checked." % (fp.get("fixed_with_ddl", 0), fp.get("batches", 1)))
    w("")
    w("Not verified by this review: actual latencies and RPC counts on the live cluster "
      "(`EXPLAIN (ANALYZE, DIST)`), tablet counts if `ybm_tablets.csv` was not supplied, "
      "client retry behaviour, CDC and retention.")
    w("")

    w("## Decisions to make")
    w("")
    n = 0
    for f in fs:
        if f["severity"] in ("critical", "high", "medium"):
            n += 1
            w("%d. %s (%s) %s, %s: %s" % (n, f["id"], f["severity"], f["title"],
                                         _cell(f["object"], 60), _cell(_choices(f, res), 300)))
    if n == 0:
        w("None at medium severity or above.")
    if disp:
        w("")
        w("Before acting on the %d disputed finding(s), collect pg_settings and review again "
          "(see Disputed by replay)." % len(disp))
    hy = [f for f in hyg if f["severity"] in ("critical", "high", "medium")]
    if hy:
        w("")
        w("For the application team: %s." % "; ".join(
            "%s %s" % (f["id"], _cell(f["title"], 70)) for f in hy))
    w("")
    return "\n".join(L)
