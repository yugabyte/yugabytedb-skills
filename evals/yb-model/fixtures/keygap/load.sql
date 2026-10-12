-- 40 devices x 60 metrics x 4 labels x 11 buckets (all-time '' + 10 daily) per table.
INSERT INTO counters (tenant_id, device_id, grp, metric, label, bucket, value)
SELECT 't1', 'd' || d, 'g1', 'm' || m, CASE WHEN l = 0 THEN '' ELSE 'l' || l END,
       CASE WHEN b = 0 THEN '' ELSE 'day:' || b END, random()
FROM generate_series(1, 40) d, generate_series(1, 60) m, generate_series(0, 3) l,
     generate_series(0, 10) b;
INSERT INTO counters_by_bucket SELECT * FROM counters;
ANALYZE counters;
ANALYZE counters_by_bucket;
