---
name: explain-plan-analyzer
description: Analyzes PostgreSQL (and YugabyteDB) EXPLAIN / EXPLAIN ANALYZE query plans to identify performance issues. Use this skill whenever a user pastes an explain plan, asks to analyze a query plan, mentions seq scans or full table scans, wants index recommendations, or asks why a query is slow. Trigger even if the user just says "look at this plan", "what's wrong with this query", or pastes raw EXPLAIN output.
---

# Explain Plan Analyzer

When a user provides a PostgreSQL or YugabyteDB EXPLAIN or EXPLAIN ANALYZE plan, work through these stages in order: parse → identify issues → recommend fixes → present clearly.

---

## Stage 1: Understand the Plan Type

First determine what you're working with:
- **EXPLAIN only** — has cost estimates but no actual timing or row counts
- **EXPLAIN ANALYZE** — has both estimates *and* actuals (actual time=, actual rows=, loops=)
- **EXPLAIN (ANALYZE, BUFFERS)** — also includes I/O hit/miss data

Note this at the start of your response, as it affects how confident you can be about findings.

---

## Stage 2: Parse Key Node Attributes

For each node in the plan tree, extract where present:
- **Node type**: Seq Scan, Index Scan, Index Only Scan, Bitmap Heap Scan, Hash Join, Nested Loop, Merge Join, Sort, Hash, Aggregate, etc.
- **Relation name**: the table or index being accessed
- **rows**: planner's estimated row count
- **actual rows**: real rows returned (ANALYZE only)
- **cost**: startup..total cost
- **actual time**: startup..total ms per loop (ANALYZE only)
- **loops**: how many times this node executed (ANALYZE only)
- **filter / index cond / recheck cond**: the predicate applied
- **rows removed by filter**: how many rows were scanned but discarded
- **Buffers hit/read**: I/O activity (BUFFERS only)

---

## Stage 3: Seq Scan Analysis (Highest Priority)

Seq Scans are the first and most critical thing to evaluate.

### Significance Thresholds

Flag a Seq Scan as **significant (🔴 Critical)** if ANY of these apply:
| Condition | Reason |
|---|---|
| Estimated rows > 10,000 | Large table being fully scanned |
| Actual rows > 10,000 (if ANALYZE) | Confirmed large scan |
| Node is inside a Nested Loop with loops > 10 | Cost multiplies: even 500-row scans become expensive |
| "Rows removed by filter" > 50,000 | Massive discard ratio — filter could become an index |
| Seq Scan cost > 30% of total plan cost | Dominates the query |

Flag a Seq Scan as **minor (🟡 Low priority)** if:
- Estimated rows < 1,000 AND not inside a loop
- The table is clearly a small config/lookup table
- Query total cost is already very low (< 100)

**For YugabyteDB**: Lower the threshold to 1,000 rows for 🔴 Critical. LSM-tree storage makes sequential scans significantly more expensive than in standard Postgres due to SST file structure. Mention this explicitly.

### What to Report for Each Significant Seq Scan

1. **Table name** and estimated / actual row count
2. **Filter condition** — this is your index candidate
3. **Rows removed by filter** if present — high discard = strong index opportunity
4. **Loop count** if inside a Nested Loop
5. **Specific index recommendation** based on the filter

**Index recommendation format:**
```sql
-- If filter is: (status = 'pending')
CREATE INDEX ON orders (status);

-- If filter is: (user_id = $1 AND created_at > $2)
CREATE INDEX ON orders (user_id, created_at);

-- If most rows have status='completed' and you only query 'pending':
CREATE INDEX ON orders (status) WHERE status != 'completed';
```

---

## Stage 4: Secondary Issue Checks

After seq scans, check for these in order of typical impact:

### 4a. Row Estimate Mismatch (ANALYZE only)
- If estimated rows vs actual rows differ by **> 10×**, flag stale or bad statistics
- Common cause: recent bulk loads, heavy deletes, or correlated columns
- Recommendation: `ANALYZE table_name;` or `CREATE STATISTICS` for correlated columns
- Very large mismatches (> 100×) can cause the planner to choose completely wrong join strategies

### 4b. Nested Loop with Large Row Counts
- A Nested Loop is efficient for small outer sets (< ~1,000 rows)
- If outer side has many rows AND inner side does a Seq Scan or expensive lookup: flag it
- The actual cost is: outer_rows × inner_cost_per_loop
- Recommendation: consider forcing a Hash Join via `SET enable_nestloop = off` temporarily to test, or add an index on the join column

### 4c. Hash Join Spilling to Disk
- Visible as: `Batches: N` where N > 1 in a Hash node
- Means the hash table didn't fit in `work_mem`
- Recommendation: `SET work_mem = '64MB';` (or higher, test in session first)

### 4d. Sort Spilling to Disk
- Visible as: `Sort Method: external merge` or `external sort`
- Recommendation: increase `work_mem`

### 4e. High "Rows Removed by Filter" on an Index Scan
- Means the index is finding rows but many are discarded by a post-scan filter
- The index may not be selective enough, or a composite index with the filter column would help

### 4f. Bitmap Heap Scan with High Recheck
- `Rows Removed by Index Recheck: N` being high means lossy bitmap pages
- Recommendation: increase `work_mem` so the bitmap stays exact

### 4g. Very High Total Cost / Slow Execution
- If EXPLAIN ANALYZE shows total actual time > 1,000ms for what should be a simple query, flag it even if no single obvious issue dominates

---

## Stage 5: Output Format

Structure your response exactly as follows:

### Plan Type
State whether this is EXPLAIN, EXPLAIN ANALYZE, or EXPLAIN (ANALYZE, BUFFERS), and note any limitations this places on the analysis.

### Plan Overview
1–2 sentences: what the query does, overall shape (joins, aggregations, etc.), and total cost or execution time if available.

### 🔴 Critical Issues
One section per issue. For each:
- **Issue title** (e.g., "Seq Scan on `orders` — 450,000 rows")
- What it is and why it matters
- Specific fix with SQL if applicable

If no critical issues: say "No critical issues found."

### 🟡 Secondary Issues
Brief bullets for lower-priority findings (row estimate mismatches, memory spills, etc.)

If none: omit this section.

### ✅ Recommended Actions
Numbered list, ordered by expected impact:
1. Most impactful fix (usually the biggest seq scan index)
2. Second fix
3. ...

Keep this list actionable — real SQL commands where possible.

---

## Guidelines

- **Always explain why** something is a problem, not just that it is
- **Be specific**: name the table, show the filter, write the actual CREATE INDEX statement
- If the plan is truncated, say so and analyze what's available
- Do not recommend indexes that are already being used (already showing Index Scan in the plan)
- If the plan looks healthy, say so clearly — a good plan is worth confirming
- For EXPLAIN-only (no ANALYZE): caveat your findings with "based on planner estimates"
- If the user mentions this is YugabyteDB, apply the lower thresholds and add YB-specific context
