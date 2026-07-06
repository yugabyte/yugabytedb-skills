---
name: yb-technical-advisories
description: Use when checking whether a YugabyteDB or YugabyteDB Anywhere (YBA) deployment is affected by any currently published Technical Advisory (TA), before or after an upgrade, or during routine health/version checks. Triggers on "technical advisory", "TA-#####", "is my version affected", "known issues for this version", "TA-CL-#####"/"TA-REOL-#####", or any request to check a YugabyteDB/YBA version against published advisories. Always fetches the live advisory list from docs.yugabyte.com rather than relying on a memorized list, because new advisories are published continuously.
---

# YugabyteDB Technical Advisories Checker

Technical Advisories (TAs) are official Yugabyte publications that describe issues which "may impact the stability or security of production deployments," together with affected versions, mitigations, and fix versions. This skill cross-references the customer's actual YugabyteDB / YugabyteDB Anywhere (YBA) versions against the **live** list of advisories and produces a clear, professional report of what applies to them.

**Source of truth (always fetch live — do not rely on memorized advisory data, it goes stale within days):**
- List of all advisories: <https://docs.yugabyte.com/stable/releases/techadvisories/>
- RSS feed (same data, machine-readable): <https://docs.yugabyte.com/stable/releases/techadvisories/index.xml>
- Individual advisory detail page: `https://docs.yugabyte.com/stable/releases/techadvisories/<id-lowercase>/` (for example `.../ta-30772/`) — contains full **Description**, **Mitigation**, and **Details** sections not shown in the summary table.

If you cannot reach the internet in the current environment, say so explicitly and ask the user to run the check themselves or paste the advisories page content — never guess or fabricate advisory contents or version ranges.

## Step 1: Establish which versions are in scope

Ask for, or determine, every relevant version — a customer's stack usually has more than one:

| Component | How to determine it |
|---|---|
| **YugabyteDB (YSQL/YCQL/DocDB)** | `SELECT version();` over YSQL, or `yb-admin --master_addresses <hosts> get_universe_config` / `yb-ctl status`. In YBA-managed universes, every node in a universe should be on the same version. |
| **YugabyteDB Anywhere (YBA)** | Bottom of the YBA UI, or `GET /api/v1/app_version` (unauthenticated, returns `{"version":"2024.2.3.0-b..."}`), or `GET /api/v2/customers/{cUUID}/universes/{uniUUID}` (authenticated, has a per-universe `softwareVersion` — see the `yba-api` skill for request details). |
| **YugabyteDB Voyager** | `yb-voyager version` |
| **CDC connector (Debezium)** | Connector JAR version, formatted like `dz.1.9.5.yb.grpc.2024.1` — distinct from the DB version. |

Note that YBA and the YugabyteDB it manages are released in lockstep by series (for example YBA 2024.2.x manages YugabyteDB 2024.2.x) but can be on different patch/build numbers, so check **both** independently — some advisories affect only one of the two products even within the same release series.

If the user has not given a version, ask for it rather than assuming "latest." Do not proceed with a check against an unconfirmed version — the whole value of this skill is precision.

## Step 2: Fetch the current advisory list

Fetch <https://docs.yugabyte.com/stable/releases/techadvisories/> and read the **List of advisories** table. Each row has: `Advisory` (ID), `Synopsis`, `Product`, `Affected Versions`, `Date`.

Do this fresh for every check — do not reuse advisory data from earlier in the conversation if more than a few minutes have passed, and never reuse it across separate chat sessions.

## Step 3: Filter by product

Match the `Product` column against the components identified in Step 1:

| Product value seen in the table | Applies to |
|---|---|
| `YugabyteDB` | Core DB — always check |
| `YSQL` | Only relevant if the customer uses the PostgreSQL-compatible API |
| `YCQL` | Only relevant if the customer uses the Cassandra-compatible API |
| `CDC` | Only relevant if Change Data Capture / logical replication is in use |
| `YugabyteDB Anywhere` | Only relevant if self-managed YBA is in use (not applicable to YugabyteDB Aeon) |
| `YugabyteDB Voyager` | Only relevant if Voyager was used for migration |
| `YugabyteDB gRPC (Debezium) Connector` | Only relevant if that specific CDC connector is deployed |

A row can list multiple products comma-separated (for example `YugabyteDB, YugabyteDB Anywhere`) — it applies if **any** listed product is in use.

Do not silently skip a product the customer didn't explicitly mention if it's a default/always-on component (YSQL and YugabyteDB core apply to virtually every deployment).

## Step 4: Match the version against the "Affected Versions" expression

This is the step most prone to error — work through it carefully and show your reasoning. The column uses several patterns:

| Pattern | Meaning | Example |
|---|---|---|
| `All` | Every version of that product is affected | `All` |
| `vX.Y` or `vX.Y.x` | The entire release series, any patch/build | `v2.20`, `v2.20.x` |
| `vX.Y.Z` (exact, no range) | That one specific release only | `v2025.2.0` |
| `vX.Y.Z to vX.Y.Z2` | Inclusive range, compared numerically component-by-component | `v2024.2.0.0 to v2024.2.9.0` |
| `vX.Y.Z+` | That version and every later release (fix not yet available, or advisory describes an ongoing risk) | `v2025.1.1.0+` |
| Comma-separated list | Any one of the listed patterns matching is a match (logical OR) | `v2.20.x, v2024.1, v2024.2` |

Comparison rules:
- Compare version numbers component-by-component as integers (`2025.2.3.0` > `2025.2.2.9` > `2025.2.2.1`), not as strings.
- When the table entry has fewer components than the customer's version (a whole series, e.g. `v2024.2`), truncate the customer's version to the same number of components before comparing.
- Treat YugabyteDB's calendar-versioned releases (`v2024.1.x`, `v2024.2.x`, `v2025.1.x`, `v2025.2.x`, ...) and legacy semantic releases (`v2.14`, `v2.16`, `v2.18`, `v2.20`) as the same kind of ordered version — do not assume calendar-style is always "newer" without comparing the actual release chronology if series are mixed.
- If a match is ambiguous or the pattern doesn't cleanly fit the rules above, do not silently exclude it — flag it as **"needs manual review"** in the report and quote the raw table text so a human can confirm.

## Step 5: Pull mitigation detail for every match

For every advisory that matches in Steps 3 and 4, fetch its individual detail page (`.../ta-<id>/`, lowercase, e.g. `ta-30772`) and extract:
- A one- to two-sentence plain-language summary of the **Description**
- The **Mitigation** section verbatim (or condensed if long) — this typically includes a workaround and/or the fix version
- Any diagnostic query or command provided (many TAs include a YSQL query or flag to check whether the issue has already been triggered) — surface this as an actionable step, not just background

## Step 6: Produce the report

Present results as a table, most severe/urgent first (data-loss or data-inconsistency risks before performance-only issues), followed by a short narrative summary. Use this structure:

```markdown
## Technical Advisory Check — <product> <version>

Checked against the live advisory list at docs.yugabyte.com on <date>.

| Advisory | Synopsis | Status | Recommended Action |
|---|---|---|---|
| [TA-30772](https://docs.yugabyte.com/stable/releases/techadvisories/ta-30772/) | Potential database inconsistency with very large transactions | ⚠️ Affected (v2025.1.0.0–v2025.1.3.x) | Run the provided diagnostic query; upgrade to v2025.1.4.0+ when feasible |
| [TA-26440](https://docs.yugabyte.com/stable/releases/techadvisories/ta-26440/) | Transparent Huge Pages causing memory issues | ⚠️ Affected (All versions) | Disable THP per the advisory; no fix version, this is a host-level setting |

### Summary
<Plain-language summary: how many advisories are relevant, whether any are data-loss/data-correctness risks vs. cosmetic, and the single highest-priority next step.>

### Not currently affected
<Optionally list advisories that were checked and ruled out, so the customer can see the check was thorough — especially useful for a compliance/audit trail.>
```

Status values to use consistently: `⚠️ Affected`, `✅ Not affected`, `❓ Needs manual review` (from Step 4's ambiguous case), and `🛑 Action already overdue` (for advisories whose affected range has no `+`, meaning a fix already exists and the customer is simply behind on upgrading).

## Anti-patterns

| Anti-pattern | Consequence | Do instead |
|---|---|---|
| Answering from memory instead of fetching the live list | Advisories are added/updated continuously (dates in the table run to the present); a memorized answer can miss a brand-new TA or one that's since been retired | Always fetch the live advisories page (and detail pages) in the current session before reporting |
| Treating "Affected Versions: All" as "every product is affected" | The row still only applies to the `Product` column's stated component | Filter by product first, then by version |
| Comparing versions as strings | `v2024.2.10.0` sorts before `v2024.2.9.0` as a string, giving a wrong verdict | Compare component-by-component as integers |
| Ignoring the `+` suffix | Reports a version as "not affected" once it passes the last explicitly-tested build, when the advisory means "still affected, no fix yet" | Treat `vX.Y.Z+` as open-ended: still affected at and above that version unless the detail page states otherwise |
| Reporting only the advisory ID and synopsis | Customer has no idea what to actually do | Always fetch the detail page and include the Mitigation section for every match |
| Skipping YBA because the user only mentioned "YugabyteDB" | Some TAs (e.g. ulimit/snapshot data loss, THP) specifically list `YugabyteDB Anywhere` as an affected product and are easy to miss | Always ask about / check both the database version and the YBA version managing it |
| Presenting a "not affected" verdict for a version pattern you weren't fully sure how to parse | False sense of security on a potential data-loss issue | Mark unclear matches as "needs manual review" and show the raw table text |
| Suggesting the customer stop here | TAs are advisory, not exhaustive; some issues surface first through Yugabyte Support | Recommend confirming with Yugabyte Support/Customer Success before taking irreversible action (e.g. an emergency upgrade) based solely on this check |
| One-off checks with no follow-up | New advisories are published regularly (the list above added 5 new entries in the ~10 weeks before this skill was written) | Suggest subscribing to the RSS feed (<https://docs.yugabyte.com/stable/releases/techadvisories/index.xml>) for ongoing monitoring, or re-running this check before each upgrade planning cycle |

## Special advisory ID formats

A few advisories don't follow the plain `TA-#####` numeric pattern — handle them the same way (fetch, filter, report), just recognize what they mean:
- `TA-CL-#####` — advisories specific to upgrade/compatibility issues between release series (the `CL` denotes a cross-release concern, e.g. `TA-CL-23623` for a v2.20 → v2024.1 upgrade failure).
- `TA-REOL-##` — "Replicated End of Life" notices, relevant only to customers who deployed YBA via Replicated rather than YBA Installer/Kubernetes.

These still go through the same product/version filtering — an `REOL` or `CL` advisory that doesn't match the customer's deployment method or upgrade path should be reported as not applicable, not omitted from the check.
