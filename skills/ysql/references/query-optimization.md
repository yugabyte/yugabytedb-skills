# Query optimization — `timestamp` vs `now()`

`now()` returns a `timestamptz`. When that value is compared against a `timestamp without time zone` column, the data type mismatch forces an implicit conversion that prevents the index condition from being pushed down to DocDB. The mismatch is especially costly when the predicate is combined with `ORDER BY`, `LIMIT`, or interval arithmetic such as `now() - interval`. Columns typed as `timestamptz` are not affected.

**Symptom — index condition not pushed down:**

```
EXPLAIN (ANALYZE, DIST, DEBUG)
SELECT ses.stream_id, ses.product_id
FROM active_session AS ses
WHERE ses.expiration_time > now() - make_interval(days => 10);
-- Index Only Scan using active_session_user_exp_stream_idx on active_session ses
-- Index Cond: (expiration_time > (now() - '10 days'::interval))
-- Rows Removed by Index Recheck: 4991
-- Storage Index Rows Scanned: 5000
```

Only 9 rows were needed, but 5000 index rows were scanned and 4991 were discarded by `Rows Removed by Index Recheck`. The `now() - interval` predicate is not pushed into DocDB, so the scan reads far more rows than the query returns.

**Fix — cast `now()` to the column's type so the predicate is pushed down:**

```
EXPLAIN (ANALYZE, DIST)
SELECT ses.stream_id, ses.product_id
FROM active_session AS ses
WHERE ses.expiration_time > now()::timestamp - make_interval(days => 10);
-- Index Only Scan using active_session_user_exp_stream_idx on active_session ses
-- Index Cond: (expiration_time > ((now())::timestamp without time zone - '10 days'::interval))
-- Heap Fetches: 0
-- Storage Index Read Requests: 1
-- Storage Index Rows Scanned: 9
```

With `now()::timestamp`, the condition is pushed down: only 9 index rows are scanned to return 9 rows, in a single read request.

**Two ways to fix:**
- **Schema change (preferred):** define the column as `timestamptz` so it matches `now()` directly.
- **No schema change:** cast the constant to the column's type in the predicate — `now()::timestamp`.
