#!/usr/bin/env python3
"""
yb-lint — static checks for YugabyteDB YSQL DDL.

Catches the defects that are mechanically detectable from DDL text alone: clause-ordering
errors that fail at parse time, missing split clauses, hash keys on nullable columns,
monotonic leading range keys, partition keys missing from primary keys, and redundant
indexes.

It cannot check anything that needs data (null fractions, cardinality, value skew) or a
running cluster. Those stay open items — see references/validation.md.

Usage:
    python3 yb-lint.py schema.sql [more.sql ...]
    python3 yb-lint.py --format json schema.sql
    python3 yb-lint.py --min-severity error schema.sql

Exit codes: 0 clean or warnings only, 1 at least one error, 2 bad invocation.
Python 3.8+, standard library only.
"""

import argparse
import json
import os
import re
import sys

SEVERITIES = {"error": 3, "warn": 2, "info": 1}

# Columns whose names imply a monotonically increasing value.
MONOTONIC_HINT = re.compile(
    r"^(.*_)?(id|seq|sequence|created_at|updated_at|inserted_at|modified_at|"
    r"timestamp|ts|event_time|occurred_at|logged_at|version|offset|lsn)$",
    re.I,
)

FLOAT_TYPES = re.compile(r"\b(real|float4|float8|double\s+precision|float)\b", re.I)


class Finding:
    def __init__(self, rule, severity, line, message, fix=""):
        self.rule = rule
        self.severity = severity
        self.line = line
        self.message = message
        self.fix = fix

    def as_dict(self, path):
        return {
            "file": path,
            "line": self.line,
            "rule": self.rule,
            "severity": self.severity,
            "message": self.message,
            "fix": self.fix,
        }


def strip_comments(sql):
    """Blank out comments while preserving line numbers and offsets."""
    out = list(sql)
    i, n = 0, len(sql)
    while i < n:
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j == -1 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            j = n if j == -1 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif sql[i] == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            i = min(j + 1, n)
        else:
            i += 1
    return "".join(out)


def split_statements(sql):
    """Yield (statement_text, start_offset). Naive but adequate for DDL files."""
    depth, start = 0, 0
    for i, ch in enumerate(sql):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == ";" and depth == 0:
            chunk = sql[start:i]
            if chunk.strip():
                yield chunk, start
            start = i + 1
    tail = sql[start:]
    if tail.strip():
        yield tail, start


def line_of(sql, offset):
    return sql.count("\n", 0, offset) + 1


def balanced_slice(text, open_idx):
    """Return the substring inside the parentheses starting at open_idx."""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1 : i], i
    return text[open_idx + 1 :], len(text)


def top_level_split(text, sep=","):
    parts, depth, cur = [], 0, []
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def key_columns(keydef):
    """Parse a PRIMARY KEY / index column list into [(name, modifier), ...]."""
    cols = []
    for part in top_level_split(keydef):
        p = part.strip()
        if p.startswith("("):
            inner, end = balanced_slice(p, 0)
            trailing = p[end + 1 :].strip().upper()
            mod = "HASH" if "HASH" in trailing else (trailing.split()[0] if trailing else "")
            for c in top_level_split(inner):
                cols.append((c.strip().strip('"').lower(), mod or "HASH"))
        else:
            toks = p.replace('"', "").split()
            if not toks:
                continue
            name = toks[0].lower()
            mod = ""
            for t in toks[1:]:
                if t.upper() in ("HASH", "ASC", "DESC"):
                    mod = t.upper()
                    break
            cols.append((name, mod))
    return cols


class Statement:
    def __init__(self, text, offset, sql):
        self.text = text
        self.offset = offset
        self.line = line_of(sql, offset)
        self.upper = text.upper()


def parse_columns(body):
    """Map column name -> declaration text, from a CREATE TABLE body."""
    cols = {}
    for part in top_level_split(body):
        p = part.strip()
        if re.match(r"^(PRIMARY\s+KEY|CONSTRAINT|UNIQUE|CHECK|FOREIGN\s+KEY|EXCLUDE)\b", p, re.I):
            continue
        toks = p.replace('"', "").split()
        if toks:
            cols[toks[0].lower()] = p
    return cols


def check_table(st, findings):
    t = st.text
    m = re.search(
        r"CREATE\s+(?:UNLOGGED\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.\"]+)", t, re.I
    )
    if not m:
        return
    name = m.group(1).replace('"', "")
    short = name.split(".")[-1]

    is_partition_of = re.search(r"\bPARTITION\s+OF\b", t, re.I)
    partition_by = re.search(r"\bPARTITION\s+BY\s+(RANGE|LIST|HASH)\s*\(", t, re.I)

    body, cols = "", {}
    open_paren = t.find("(", m.end())
    if open_paren != -1 and not is_partition_of:
        body, _ = balanced_slice(t, open_paren)
        cols = parse_columns(body)

    has_split = re.search(r"\bSPLIT\s+(INTO|AT\s+VALUES)\b", t, re.I)

    # --- split clauses -------------------------------------------------------
    if re.search(r"\bSPLIT\s+INTO\s+\d+\s*(?!TABLETS)(?:;|\)|$|\s+WHERE)", t, re.I) or (
        re.search(r"\bSPLIT\s+INTO\s+\d+\b", t, re.I)
        and not re.search(r"\bSPLIT\s+INTO\s+\d+\s+TABLETS\b", t, re.I)
    ):
        findings.append(
            Finding(
                "YB001",
                "error",
                st.line,
                f"{short}: SPLIT INTO is missing the TABLETS keyword.",
                "Write SPLIT INTO <n> TABLETS.",
            )
        )

    if is_partition_of and not has_split:
        findings.append(
            Finding(
                "YB010",
                "info",
                st.line,
                f"{short}: partition has no SPLIT INTO, so it starts at one tablet and "
                "auto-splits as it grows.",
                "Fine for average workloads. Pre-split only for high-ingest partitions, "
                "where the auto-split ramp is itself a write hotspot.",
            )
        )

    # ysql_dump writes SPLIT INTO 1 TABLETS on every parent; only a real split is a mistake.
    if partition_by and has_split and not re.search(r"\bSPLIT\s+INTO\s+1\s+TABLETS\b", t, re.I):
        findings.append(
            Finding(
                "YB011",
                "warn",
                st.line,
                f"{short}: SPLIT clause on a partitioned parent. The parent holds no rows; "
                "splits do not inherit to partitions.",
                "Put SPLIT INTO on each partition and on each per-partition index instead.",
            )
        )

    # --- primary key ---------------------------------------------------------
    pk_cols = []
    pk = re.search(r"\bPRIMARY\s+KEY\s*\(", t, re.I)
    if pk:
        keydef, _ = balanced_slice(t, t.index("(", pk.end() - 1))
        pk_cols = key_columns(keydef)
    else:
        for cname, decl in cols.items():
            if re.search(r"\bPRIMARY\s+KEY\b", decl, re.I):
                pk_cols = [(cname, "")]
                break

    # A partitioned parent without a PK is the documented shape when uniqueness is per child.
    if not pk_cols and not is_partition_of and body and not partition_by:
        findings.append(
            Finding(
                "YB020",
                "warn",
                st.line,
                f"{short}: no primary key. YugabyteDB will shard on the internal ybrowid, "
                "so you get no control over distribution or ordering.",
                "Declare a primary key chosen from the dominant access pattern.",
            )
        )

    if pk_cols:
        lead, lead_mod = pk_cols[0]
        decl = cols.get(lead, "")
        effective = lead_mod or "HASH"  # first PK column defaults to HASH

        if effective in ("ASC", "DESC") and MONOTONIC_HINT.match(lead):
            findings.append(
                Finding(
                    "YB022",
                    "warn",
                    st.line,
                    f"{short}: range key leads with '{lead}', which looks monotonic. Every "
                    "insert goes to the tail tablet.",
                    "Use HASH if the access is equality, or a bucket-based key. Note ASC also "
                    "sorts NULLs last, so NULLs pile into the tail too.",
                )
            )

        if partition_by:
            pcols_raw, _ = balanced_slice(t, t.index("(", partition_by.end() - 1))
            pcols = {c.strip().strip('"').lower() for c in top_level_split(pcols_raw)}
            pk_names = {c for c, _ in pk_cols}
            missing = pcols - pk_names
            if missing:
                findings.append(
                    Finding(
                        "YB023",
                        "error",
                        st.line,
                        f"{short}: partition key {sorted(missing)} is not in the primary key. "
                        "PostgreSQL rejects this at the parent.",
                        "Add the partition key to the PK, or declare the PK per child via "
                        "CREATE TABLE + ATTACH PARTITION and accept the trade-offs.",
                    )
                )
            for pc in pcols:
                if FLOAT_TYPES.search(cols.get(pc, "")):
                    findings.append(
                        Finding(
                            "YB024",
                            "error",
                            st.line,
                            f"{short}: partition key '{pc}' is a floating-point type. Binary "
                            "float cannot represent decimals exactly, so bound comparisons "
                            "can misroute rows.",
                            "Use numeric, integer, or a timestamp type.",
                        )
                    )

    # --- ambiguous composite hash -------------------------------------------
    if pk:
        keydef, _ = balanced_slice(t, t.index("(", pk.end() - 1))
        parts = top_level_split(keydef)
        if len(parts) > 1 and not parts[0].strip().startswith("("):
            if any(re.search(r"\bHASH\b", p, re.I) for p in parts):
                findings.append(
                    Finding(
                        "YB025",
                        "error",
                        st.line,
                        f"{short}: HASH appears in a multi-column PK without a parenthesised "
                        "hash group. PRIMARY KEY (a, b, c HASH) is not "
                        "PRIMARY KEY ((a, b, c) HASH).",
                        "Parenthesise the hash group explicitly.",
                    )
                )

    # --- data types ----------------------------------------------------------
    for cname, decl in cols.items():
        if re.search(r"\bjson\b(?!b)", decl, re.I):
            findings.append(
                Finding(
                    "YB030",
                    "warn",
                    st.line,
                    f"{short}.{cname}: type json re-parses on every extraction.",
                    "Use jsonb.",
                )
            )
        if re.search(r"\btimestamp\b(?!\s*\()", decl, re.I) and not re.search(
            r"\bWITH\s+TIME\s+ZONE\b|\btimestamptz\b", decl, re.I
        ):
            findings.append(
                Finding(
                    "YB031",
                    "info",
                    st.line,
                    f"{short}.{cname}: timestamp without time zone.",
                    "Prefer timestamptz stored in UTC.",
                )
            )

    return {"name": name, "short": short, "pk": pk_cols, "cols": cols}


def check_index(st, findings, index_registry, table_cols=None, table_pk=None):
    t = st.text
    m = re.search(
        r"CREATE\s+(UNIQUE\s+)?INDEX\s+((?:NON)?CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?"
        r"([\w.\"]+)\s+ON\s+(?:ONLY\s+)?([\w.\"]+)",
        t,
        re.I,
    )
    if not m:
        return
    is_unique = bool(m.group(1))
    iname = m.group(3).replace('"', "").split(".")[-1]
    tname = m.group(4).replace('"', "").split(".")[-1]

    open_paren = t.find("(", m.end())
    if open_paren == -1:
        return
    keydef, close = balanced_slice(t, open_paren)
    cols = key_columns(keydef)

    tail = t[close + 1 :]
    tail_upper = tail.upper()

    # --- clause ordering -----------------------------------------------------
    w = tail_upper.find("WHERE")
    s = max(tail_upper.find("SPLIT INTO"), tail_upper.find("SPLIT AT"))
    if w != -1 and s != -1 and s > w:
        findings.append(
            Finding(
                "YB002",
                "error",
                st.line,
                f"{iname}: SPLIT clause appears after WHERE. This is a syntax error.",
                "Order is: column list -> INCLUDE -> SPLIT INTO/SPLIT AT VALUES -> WHERE.",
            )
        )

    if re.search(r"\bSPLIT\s+INTO\s+\d+\b", tail, re.I) and not re.search(
        r"\bSPLIT\s+INTO\s+\d+\s+TABLETS\b", tail, re.I
    ):
        findings.append(
            Finding(
                "YB001",
                "error",
                st.line,
                f"{iname}: SPLIT INTO is missing the TABLETS keyword.",
                "Write SPLIT INTO <n> TABLETS.",
            )
        )

    has_split = bool(re.search(r"\bSPLIT\s+(INTO|AT\s+VALUES)\b", tail, re.I))
    if not has_split and not re.search(r"\bON\s+ONLY\b", t, re.I):
        findings.append(
            Finding(
                "YB012",
                "info",
                st.line,
                f"{iname}: no SPLIT clause. An index is its own relation and never inherits "
                "the base table's split.",
                "Fine if the base table isn't pre-split either. If it is, decide this "
                "index's count deliberately rather than by omission.",
            )
        )

    # --- bucketed index without pinned buckets ------------------------------
    if re.search(r"yb_hash_code\s*\(", keydef, re.I):
        if not re.search(r"\(\s*yb_hash_code\s*\([^)]*\)\s*%\s*\d+\s*\)", keydef, re.I):
            findings.append(
                Finding(
                    "YB003",
                    "error",
                    st.line,
                    f"{iname}: the yb_hash_code modulo expression is not parenthesised.",
                    "Write (yb_hash_code(col) % n) ASC.",
                )
            )
        if not re.search(r"\bSPLIT\s+AT\s+VALUES\b", tail, re.I):
            findings.append(
                Finding(
                    "YB013",
                    "warn",
                    st.line,
                    f"{iname}: bucketed index without SPLIT AT VALUES. N buckets are not "
                    "guaranteed to land on N tablets, so this is half a fix.",
                    "Add SPLIT AT VALUES ((1), (2), ... (n-1)).",
                )
            )

    # --- key quality ---------------------------------------------------------
    if cols:
        lead, lead_mod = cols[0]
        effective = lead_mod or "HASH"
        if effective in ("ASC", "DESC") and MONOTONIC_HINT.match(lead):
            if not re.search(r"yb_hash_code", keydef, re.I):
                findings.append(
                    Finding(
                        "YB022",
                        "warn",
                        st.line,
                        f"{iname}: range index leads with '{lead}', which looks monotonic. "
                        "All inserts land on the tail tablet.",
                        "Use a bucket-based index, or HASH if the access is equality.",
                    )
                )
        guarded = re.search(r"\bWHERE\b.*IS\s+NOT\s+NULL", tail, re.I)
        if effective == "HASH" and not guarded:
            decl = (table_cols or {}).get(tname, {}).get(lead)
            if decl is not None and not re.search(r"\bNOT\s+NULL\b", decl, re.I):
                findings.append(
                    Finding(
                        "YB021",
                        "warn",
                        st.line,
                        f"{iname}: hashes '{lead}', which {tname} declares nullable. All NULLs "
                        "share one hash code, so they land on one tablet that DocDB cannot "
                        "split (the split key reduces to the hash code).",
                        "Measure null_frac. If a meaningful fraction is NULL, add "
                        f"WHERE {lead} IS NOT NULL and size the split for the rest.",
                    )
                )
            else:
                findings.append(
                    Finding(
                        "YB026",
                        "info",
                        st.line,
                        f"{iname}: hashes '{lead}'. Confirm it is high-cardinality and not "
                        "value-skewed — neither is checkable from DDL.",
                        "Measure n_distinct and the top value's share (references/validation.md).",
                    )
                )

    if is_unique and re.search(r"\bWHERE\b", tail, re.I) is None and \
            re.search(r"\bNULLS\s+NOT\s+DISTINCT\b", tail, re.I) is None:
        # A key column is NOT NULL when its declaration says so or it is in the primary key.
        # An expression, or a column this DDL never declares, may be NULL.
        decls = (table_cols or {}).get(tname, {})
        pk_names = (table_pk or {}).get(tname, set())
        nullable = [
            c
            for c, _ in cols
            if c not in pk_names and not re.search(r"\bNOT\s+NULL\b", decls.get(c, ""), re.I)
        ]
        if nullable:
            names = ", ".join(nullable)
            guard = " AND ".join(f"{c} IS NOT NULL" for c in nullable)
            findings.append(
                Finding(
                    "YB027",
                    "info",
                    st.line,
                    f"{iname}: unique index on nullable column(s) {names}. NULL is distinct "
                    "from NULL, so many NULL rows are permitted and Voyager live migration "
                    "can raise false conflicts on it.",
                    f"Declare {names} NOT NULL if they are never NULL, or make the index "
                    f"partial: WHERE {guard}.",
                )
            )

    inc = re.search(r"\bINCLUDE\s*\(([^)]*)\)", tail, re.I)
    include = {c.strip().strip('"').lower() for c in inc.group(1).split(",")} if inc else set()
    # Key layout as YugabyteDB resolves it: an unannotated first column is HASH (the default
    # outside colocation), later unannotated columns ASC.
    layout = tuple((c, m or ("HASH" if i == 0 else "ASC")) for i, (c, m) in enumerate(cols))
    index_registry.setdefault(tname, []).append(
        (iname, layout, st.line, bool(re.search(r"\bWHERE\b", tail, re.I)), is_unique, include)
    )


def check_sequence(st, findings):
    m = re.search(r"CREATE\s+SEQUENCE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.\"]+)", st.text, re.I)
    if not m:
        return
    name = m.group(1).replace('"', "").split(".")[-1]
    cache = re.search(r"\bCACHE\s+(\d+)", st.text, re.I)
    if not cache or int(cache.group(1)) < 100:
        findings.append(
            Finding(
                "YB040",
                "warn",
                st.line,
                f"{name}: sequence cache is {cache.group(1) if cache else 'unset (default 1)'}. "
                "Every allocation becomes a distributed round trip.",
                "CACHE 10000, or set ysql_sequence_cache_method=server. Expect large ID gaps; "
                "that is correct behaviour.",
            )
        )


def cross_checks(index_registry, findings, max_indexes):
    for table, idxs in index_registry.items():
        non_partial = [i for i in idxs if not i[3]]
        if len(idxs) > max_indexes:
            findings.append(
                Finding(
                    "YB050",
                    "warn",
                    idxs[0][2],
                    f"{table}: {len(idxs)} indexes. Each one is a Raft participant on every "
                    "write, and the statement commits when its slowest participant acks.",
                    f"Justify each index against a named access pattern; target <= {max_indexes}.",
                )
            )
        for a in non_partial:
            for b in non_partial:
                if a[0] == b[0] or not covers(b, a):
                    continue
                a_name, a_key, a_line = a[0], a[1], a[2]
                cols = ", ".join("%s %s" % kv for kv in a_key)
                if len(a_key) == len(b[1]):
                    if b[0] < a[0] and covers(a, b):
                        continue  # identical pair: report it once
                    findings.append(
                        Finding(
                            "YB052",
                            "warn",
                            a_line,
                            f"{table}: index '{a_name}' ({cols}) duplicates '{b[0]}': same key "
                            "columns, layout and sort order, not unique, and its INCLUDE "
                            "columns are covered.",
                            "Confirm idx_scan = 0 and that no hint or constraint names it, "
                            f"then DROP INDEX {a_name}.",
                        )
                    )
                else:
                    findings.append(
                        Finding(
                            "YB051",
                            "warn",
                            a_line,
                            f"{table}: index '{a_name}' ({cols}) is a prefix of '{b[0]}' with "
                            "the same hash group and sort order, is not unique, and its "
                            "INCLUDE columns are covered, so it is probably redundant.",
                            "Confirm idx_scan = 0 and that no hint or constraint names it, "
                            f"then DROP INDEX {a_name}.",
                        )
                    )


def covers(b, a):
    """True when index b can serve everything index a serves, so a is redundant: neither is
    partial (the caller filters), a is not unique (it enforces a constraint b does not, unless
    both are unique on the same key), a's key with its HASH / ASC / DESC layout is a prefix of
    b's, both have the same hash group, and a's INCLUDE columns are in b's key or INCLUDE."""
    a_key, b_key = a[1], b[1]
    if len(a_key) > len(b_key) or b_key[: len(a_key)] != a_key:
        return False
    if a[4] and not (b[4] and len(a_key) == len(b_key)):
        return False
    hash_a = [c for c, m in a_key if m == "HASH"]
    hash_b = [c for c, m in b_key if m == "HASH"]
    if hash_a != hash_b:
        return False
    return a[5] <= ({c for c, _ in b_key} | b[5])


def lint(sql, max_indexes=6):
    findings = []
    clean = strip_comments(sql)
    index_registry = {}
    stmts = [Statement(t, o, clean) for t, o in split_statements(clean)]

    # Pass 1: tables. Indexes may appear before the table they sit on, so column
    # nullability has to be collected before any index is judged.
    table_cols, table_pk = {}, {}
    for st in stmts:
        if re.search(r"\bCREATE\s+(UNLOGGED\s+)?TABLE\b", st.upper):
            info = check_table(st, findings)
            if info:
                table_cols[info["short"]] = info["cols"]
                table_pk[info["short"]] = {c for c, _ in info["pk"]}

    # Pass 2: everything else.
    for st in stmts:
        if re.search(r"\bCREATE\s+(UNIQUE\s+)?INDEX\b", st.upper):
            check_index(st, findings, index_registry, table_cols, table_pk)
        elif re.search(r"\bCREATE\s+SEQUENCE\b", st.upper):
            check_sequence(st, findings)
    cross_checks(index_registry, findings, max_indexes)
    findings.sort(key=lambda f: (f.line, f.rule))
    return findings


def main():
    ap = argparse.ArgumentParser(description="Static checks for YugabyteDB YSQL DDL.")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    ap.add_argument("--min-severity", choices=["info", "warn", "error"], default="info")
    ap.add_argument("--max-indexes", type=int, default=6)
    args = ap.parse_args()

    floor = SEVERITIES[args.min_severity]
    all_rows, worst = [], 0

    for path in args.files:
        if not os.path.isfile(path):
            print(f"yb-lint: no such file: {path}", file=sys.stderr)
            return 2
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            sql = fh.read()
        for f in lint(sql, args.max_indexes):
            if SEVERITIES[f.severity] < floor:
                continue
            worst = max(worst, SEVERITIES[f.severity])
            all_rows.append(f.as_dict(path))

    if args.format == "json":
        print(json.dumps({"findings": all_rows}, indent=2))
    else:
        if not all_rows:
            print("yb-lint: no findings.")
        else:
            current = None
            for r in all_rows:
                if r["file"] != current:
                    current = r["file"]
                    print(f"\n{current}")
                    print("-" * len(current))
                print(f"  {r['severity'].upper():5s} {r['rule']}  line {r['line']}")
                print(f"        {r['message']}")
                if r["fix"]:
                    print(f"        fix: {r['fix']}")
            errors = sum(1 for r in all_rows if r["severity"] == "error")
            warns = sum(1 for r in all_rows if r["severity"] == "warn")
            infos = sum(1 for r in all_rows if r["severity"] == "info")
            print(f"\n{errors} error(s), {warns} warning(s), {infos} info.")
            print("Statistics-dependent checks (null_frac, cardinality, skew) cannot be done "
                  "from DDL. See references/validation.md.")

    return 1 if worst == SEVERITIES["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
