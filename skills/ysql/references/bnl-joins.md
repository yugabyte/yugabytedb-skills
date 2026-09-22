# Join Strategies: Nested Loop vs Batched Nested Loop (BNL)

YugabyteDB supports the standard PostgreSQL join methods (Nested Loop, Hash Join, Merge Join) plus a YB-specific one: **Batched Nested Loop Join (BNL)**.

A plain `Nested Loop` re-queries the inner table once per outer row, and each of those is a separate network RPC — so it degrades fast as the outer side grows. BNL batches up to `yb_bnl_batch_size` outer keys into one `= ANY(ARRAY[...])` lookup instead.

Design implications for schema and query authors:
- **Index the inner join column.** BNL needs an index to batch lookups against; without one the join falls back to a scan per outer row. This is the same requirement as the foreign-key indexing rule above.
- **Keep join conditions batchable** — a plain equality on the raw inner column. Wrapping the *inner* column in a function/expression, or joining on a non-equality operator, prevents batching (the same pushdown-defeating pattern that breaks index usage). Put any computed expression on the outer side, or persist and index the computed value.
- **Keep statistics current.** If the planner estimates ~1 outer row it sees no reason to batch. `ANALYZE` after bulk loads and schema changes.
- **Check `yb_bnl_batch_size` on older clusters** — it defaults to `1` (BNL **off**) on 2.20 and earlier, and `1024` from 2.21 / 2024.2 onward.

Note that `SET enable_nestloop = off` does **not** disable BNL on 2.21+ — BNL is a separate join strategy there, and the `pg_hint_plan` hints differ (`NestLoop(...)` = unbatched, `YBBatchedNL(...)` = batched).

> **Diagnosing a plan that should batch but doesn't:** use `yb-query-analysis` — it owns performance investigation, and [query-tuning.md](../../yb-query-analysis/references/query-tuning.md) has the GUCs and remediation. It draws on `explain-plan-analyzer` (Stage 3b) for reading the join nodes.
