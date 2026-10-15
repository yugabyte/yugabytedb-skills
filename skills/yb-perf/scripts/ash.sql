-- ash.sql -- read-only Active Session History (ASH) summary for ONE node: what sessions were
-- doing or waiting on. ASH is a per-node in-memory buffer: run on every tserver (all-nodes.sh)
-- before calling anything cluster-wide.
-- Run: ysqlsh -h <host> -p 5433 -U <user> -d <db> -f ash.sql            (last 10 minutes)
--      ysqlsh ... -v window='30 minutes' -f ash.sql                       (other window)
-- First section errors with 'relation "yb_active_session_history" does not exist' => ASH is off
-- (TServer GFlag ysql_yb_enable_ash, restart; Aeon: support ticket). permission denied => needs pg_monitor.
\pset pager off
SET statement_timeout = '30s';
\if :{?window}
\else
\set window '10 minutes'
\endif

\echo '== ASH buffer on this node: samples and time span (span = how far back history reaches) =='
SELECT count(*) AS samples, min(sample_time) AS oldest, max(sample_time) AS newest,
       count(*) FILTER (WHERE sample_time > now() - interval :'window') AS samples_in_window
FROM yb_active_session_history;

\echo '== Wait breakdown in the window. RPCWait/TableRead can dominate under ordinary load; compare with a healthy baseline =='
SELECT wait_event_type, wait_event, wait_event_component,
       count(*) AS samples, round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct
FROM yb_active_session_history
WHERE sample_time > now() - interval :'window'
GROUP BY wait_event_type, wait_event, wait_event_component
ORDER BY samples DESC LIMIT 20;

\echo '== Top query_id by samples, with the dominant wait (query text from this node''s pg_stat_statements) =='
WITH q AS (
  SELECT query_id, count(*) AS samples,
         mode() WITHIN GROUP (ORDER BY wait_event_type || ':' || wait_event) AS top_wait
  FROM yb_active_session_history
  WHERE sample_time > now() - interval :'window'
  GROUP BY query_id
)
SELECT q.query_id, q.samples, round(100.0 * q.samples / sum(q.samples) OVER (), 1) AS pct, q.top_wait,
       (SELECT left(s.query, 70) FROM pg_stat_statements s WHERE s.queryid = q.query_id LIMIT 1) AS query
FROM q ORDER BY q.samples DESC LIMIT 15;

\echo '== Hot tablets (TServer samples). wait_event_aux holds a tablet id PREFIX; the table name resolves only for tablets hosted on this node =='
SELECT a.wait_event_aux AS tablet_id_prefix, count(*) AS samples,
       min(concat_ws('.', nullif(t.ysql_schema_name, ''), t.table_name)) AS table_on_this_node
FROM yb_active_session_history a
LEFT JOIN yb_local_tablets t ON t.tablet_id LIKE a.wait_event_aux || '%'
WHERE a.wait_event_component = 'TServer' AND a.wait_event_aux IS NOT NULL
  AND a.sample_time > now() - interval :'window'
GROUP BY a.wait_event_aux
ORDER BY samples DESC LIMIT 10;

\echo '== Samples by cloud / region / zone of the node that ran the top-level request =='
SELECT s.cloud, s.region, s.zone, count(*) AS samples,
       round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct
FROM yb_active_session_history a
JOIN yb_servers() s ON a.top_level_node_id = s.uuid::uuid
WHERE a.sample_time > now() - interval :'window'
GROUP BY s.cloud, s.region, s.zone
ORDER BY samples DESC;
