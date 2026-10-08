"""Render analyze output as the review document. Deterministic: same JSON, same Markdown.

The skill's narrative goes in the one marked slot (<!-- SUMMARY -->); everything else comes
from the engine so that every model produces the same findings, ranking and DDL.
"""


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


def review_level(res):
    """One line: what kind of review this is, and whether inputs that change it are missing."""
    level = "structural review (DDL only)"
    if res["patterns"] and any(p["source"] == "pg_stat_statements" for p in res["patterns"]):
        level = "workload-weighted review"
    elif res["patterns"]:
        level = "query-list review (patterns unranked)"
    if res.get("replay"):
        level += " with plan replay on %s" % res["replay"].get("version")
    major = [m for m in (res.get("preflight") or {}).get("missing", []) if m["tier"] == "major"]
    if major:
        level += ", INCOMPLETE: %d major input(s) missing, run with --accept-missing" % len(major)
    return level


def render_chat(res, review_path="review.md"):
    """The chat message, written by the engine like the rest of the review: the headline, the
    top three findings with their fixes, the review level and, for an incomplete review, what
    is missing and which candidates were withheld. The skill pastes it unchanged."""
    L = [headline(res), ""]
    top = [f for f in res["findings"] if f.get("section") not in ("hygiene", "disputed")][:3]
    if top:
        L.append("Top findings:")
        for n, f in enumerate(top, 1):
            L.append("%d. **%s %s** (%s): %s, %s. Fix: %s" % (
                n, f["id"], f["rule"], f["severity"], f["title"], f["object"],
                " ".join((f.get("fix") or "see the report").split())))
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
        where = " on %s" % ", ".join(top["patterns"][:3])
        if sh:
            where += " (%.1f%% of total statement time)" % (100 * sum(sh))
    first = (top.get("fix") or top["title"]).split(". ")[0].rstrip(".")
    pf = res.get("preflight") or {}
    major = [m["label"].split(":")[0].split(" (")[0] for m in pf.get("missing", [])
             if m["tier"] == "major"]
    gap = (" Inputs missing, so this review is incomplete: %s (see Muted checks)." %
           ", ".join(major)) if major else ""
    return ("**Worst finding: %s** (%s, %s): %s, %s%s. First action: %s. %d findings in total "
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
    w("- Linter: %s" % ("ran (yb-lint.py)" if res["lint_ran"] else "did not run"))
    w("")

    w("## Assumptions and unknowns")
    w("")
    if res["open_items"]:
        for o in res["open_items"]:
            w("- %s" % o)
    else:
        w("- None recorded.")
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
            acc = "; ".join("%s: %s%s%s" % (
                a["table"], a["path"] or "scan",
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
        for f in fs:
            w("| %s | %s | %s | %s | %s | %s |" % (
                f["id"], f["severity"], _conf(f), f["rule"], _cell(f["object"], 60),
                ", ".join(f["patterns"][:8]) + (" ..." if len(f["patterns"]) > 8 else "")))
        w("")
        for f in fs:
            if f["severity"] == "info" and f["rule"].startswith("LINT-"):
                continue
            w("### %s %s: %s" % (f["id"], f["rule"], f["title"]))
            w("")
            w("- **Fact:** %s" % f["fact"])
            if f.get("mechanism"):
                w("- **Mechanism:** %s" % f["mechanism"])
            if f.get("fix"):
                w("- **Fix:** %s" % f["fix"])
            if f.get("measured"):
                w("- **Measured:** %s" % f["measured"])
            if f.get("replay"):
                w("- **Replay:** %s" % f["replay"])
            if f.get("evidence_needed"):
                w("- **Evidence needed:** %s" % f["evidence_needed"])
            elif f["confidence"] == "probable" and f["rule"].startswith("CAP"):
                w("- **Evidence needed:** EXPLAIN (ANALYZE, DIST) of %s on the target, or a "
                  "replay with statistics." % (", ".join(f["patterns"]) or "the pattern"))
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
            w("Informational lint (%d): %s" % (len(infos), "; ".join(
                "%s %s" % (f["id"], _cell(f["fact"], 90)) for f in infos)))
            w("")

    w("## Disputed by replay (planner settings assumed)")
    w("")
    if not disp:
        w("None.")
    else:
        w("The bundle had no pg_settings, so replay ran under assumed planner settings, and "
          "under them the planner disagreed with these findings. They are kept, because the "
          "customer's real settings may differ, but they are not part of the recommended DDL "
          "or action items. Collect `ybm_settings.csv` (collect.sql) and review again to settle "
          "them.")
        w("")
        w("| ID | Severity | Rule | Object | Finding | Why disputed | Fix if it holds |")
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

    ddl = [f for f in fs if f.get("ddl") and f["severity"] in ("critical", "high", "medium")]
    w("## Recommended DDL")
    w("")
    if ddl:
        w("Templates from the findings; fill `<n>` from expected mature size and parse-check "
          "on the target before use.")
        w("")
        w("```sql")
        for f in ddl:
            w("-- %s %s" % (f["id"], f["rule"]))
            w(f["ddl"])
        w("```")
    else:
        w("None generated.")
    w("")

    w("## Safety check of the recommendations")
    w("")
    saf = res.get("safety") or []
    if not saf:
        w("No recommendation carries DDL, so there was nothing to check.")
    else:
        w("Each recommendation's DDL was applied to a copy of the schema, alone and then all "
          "together, and checked for lost uniqueness, lost ON CONFLICT targets, and plan "
          "regressions on every ranked pattern.")
        w("")
        w("| Recommendation | Result | Detail |")
        w("|---|---|---|")
        for r in saf:
            w("| %s %s | %s | %s |" % (r["finding_rule"], _cell(r["object"], 60), r["status"],
                                       _cell("; ".join(r["checks"]), 300)))
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
    if rp:
        w("Replay on `%s` (%s): %d pattern(s) planned, %d failed. DDL errors on replay: %s. "
          "Statistics were injected, not measured on the target; plans are estimates." % (
              rp.get("version"), rp.get("mode"), len(rp.get("planned", [])),
              len(rp.get("failed", [])), rp.get("ddl_errors", "n/a")))
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
        pr = rp.get("probes") or {}
        if pr:
            w("")
            w("Rule probes on `%s` (generated rows, EXPLAIN (ANALYZE, DIST)): %s." % (
                rp.get("version"), "; ".join("%s %s" % (k, v["verdict"])
                                             for k, v in sorted(pr.items()))))
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
            w("Self-check (apply all recommended DDL, review again): DID NOT CONVERGE. %s" %
              "; ".join(fp["problems"]))
        else:
            w("Self-check: applying every recommended DDL to a copy of the schema and reviewing "
              "it again statically resolves the %d schema findings that carry DDL and adds no "
              "new schema finding at medium or above; a second round adds none either. "
              "Workload, plan and safety findings describe the workload as measured and are not "
              "re-checked." % fp.get("fixed_with_ddl", 0))
    w("")
    w("Not verified by this review: actual latencies and RPC counts on the live cluster "
      "(`EXPLAIN (ANALYZE, DIST)`), tablet counts if `ybm_tablets.csv` was not supplied, "
      "client retry behaviour, CDC and retention.")
    w("")

    w("## Action items")
    w("")
    n = 0
    for f in fs:
        if f["severity"] in ("critical", "high", "medium"):
            n += 1
            w("%d. %s (%s): %s" % (n, f["id"], f["severity"], _cell(f["fix"] or f["title"], 300)))
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
