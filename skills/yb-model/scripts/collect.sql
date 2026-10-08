-- yb-model evidence collector. Read-only: SELECTs against catalog and statistics views.
--
-- Run from an empty directory, connected to the application database:
--   ysqlsh -h <host> -U <user> -d <db> -f collect.sql
--   ysql_dump -h <host> -U <user> -d <db> --schema-only --include-yb-metadata > schema.sql
-- Then pass the directory to: python3 scripts/yb-model.py analyze <dir>
--
-- Each \copy writes one CSV in the current directory. A query that fails on an older release
-- (for example a missing column) only skips that file. pg_stat_statements must be installed in
-- this database for ybm_pss.csv. All non-system schemas are collected.

\set ON_ERROR_STOP 0

\copy (SELECT 'version' AS key, version() AS value UNION ALL SELECT 'database', current_database() UNION ALL SELECT 'colocated', yb_is_database_colocated()::text UNION ALL SELECT 'node', coalesce(host(inet_server_addr()), 'local') || ':' || coalesce(inet_server_port()::text, '') UNION ALL SELECT 'nodes', (SELECT count(*) FROM yb_servers())::text UNION ALL SELECT 'postmaster_start', pg_postmaster_start_time()::text UNION ALL SELECT 'captured_at', now()::text) TO 'ybm_meta.csv' CSV HEADER

\copy (SELECT name, setting FROM pg_settings WHERE name LIKE 'yb\_%' OR name IN ('work_mem', 'enable_bitmapscan', 'enable_seqscan', 'enable_indexscan', 'plan_cache_mode', 'random_page_cost', 'default_statistics_target', 'search_path') ORDER BY name) TO 'ybm_settings.csv' CSV HEADER

\copy (SELECT schemaname, tablename, attname, inherited, null_frac, avg_width, n_distinct, most_common_vals, most_common_freqs, histogram_bounds, correlation FROM pg_stats WHERE schemaname NOT IN ('pg_catalog', 'information_schema') AND schemaname NOT LIKE 'pg\_%' ORDER BY tablename, attname) TO 'ybm_pg_stats.csv' CSV HEADER

\copy (SELECT n.nspname AS schemaname, c.relname, c.relkind, c.reltuples FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg\_%' AND c.relkind IN ('r', 'p', 'i', 'I') ORDER BY c.relname) TO 'ybm_reltuples.csv' CSV HEADER

\copy (SELECT schemaname, relname, indexrelname, idx_scan, idx_tup_read, idx_tup_fetch FROM pg_stat_user_indexes WHERE schemaname NOT IN ('pg_catalog', 'information_schema') AND schemaname NOT LIKE 'pg\_%' ORDER BY relname, indexrelname) TO 'ybm_index_usage.csv' CSV HEADER

\copy (SELECT schemaname, relname, seq_scan, seq_tup_read, idx_scan, n_tup_ins, n_tup_upd, n_tup_del, n_live_tup FROM pg_stat_user_tables WHERE schemaname NOT IN ('pg_catalog', 'information_schema') AND schemaname NOT LIKE 'pg\_%' ORDER BY relname) TO 'ybm_table_usage.csv' CSV HEADER

-- Tablet counts from the master catalog (cluster-wide). yb_local_tablets would list only the
-- tablets with a peer on the node this session is connected to.
\copy (SELECT n.nspname AS schemaname, c.relname, (yb_table_properties(c.oid)).num_tablets AS num_tablets FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg\_%' AND c.relkind IN ('r', 'i', 'm') ORDER BY n.nspname, c.relname) TO 'ybm_tablets.csv' CSV HEADER

-- Full statement text matters: a truncated WHERE clause cannot be mapped to an index.
\copy (SELECT * FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())) TO 'ybm_pss.csv' CSV HEADER
