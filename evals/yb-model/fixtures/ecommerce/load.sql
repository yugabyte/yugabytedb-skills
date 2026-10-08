INSERT INTO customers SELECT g, 'user' || g || '@example.com', (ARRAY['us','eu','apac'])[1 + g % 3], '2025-01-01'::timestamptz + (g || ' minutes')::interval FROM generate_series(1, 20000) g;
-- 30% guest orders with NULL customer; status heavily skewed; created_at follows id.
INSERT INTO orders (customer_id, status, total, created_at)
SELECT CASE WHEN g % 10 < 3 THEN NULL ELSE 1 + (g * 7919) % 20000 END,
       CASE WHEN g % 100 < 85 THEN 'delivered' WHEN g % 100 < 93 THEN 'shipped' WHEN g % 100 < 98 THEN 'pending' ELSE 'cancelled' END,
       (g % 50000) / 100.0, '2026-01-01'::timestamptz + (g || ' seconds')::interval
FROM generate_series(1, 100000) g;
INSERT INTO order_items SELECT o, l, 'sku-' || ((o * l) % 5000), 1 + l % 3, 9.99 FROM generate_series(1, 100000) o, generate_series(1, 3) l;
INSERT INTO events (customer_id, kind, payload, created_at) SELECT 1 + g % 20000, (ARRAY['view','click','cart'])[1 + g % 3], '{}'::jsonb, '2026-01-01'::timestamptz + (g || ' seconds')::interval FROM generate_series(1, 100000) g;
INSERT INTO shipments SELECT o, '2026-02-01'::timestamptz + (o || ' seconds')::interval, (ARRAY['ups','fedex','dhl'])[1 + o % 3], 'T' || o FROM generate_series(1, 50000) o;
INSERT INTO audit_log SELECT g, 'order', '2026-01-01'::timestamptz + ((g * 300) || ' seconds')::interval, 'x' FROM generate_series(1, 100000) g;
ANALYZE;
