--
-- YSQL database dump
--

-- Dumped from database version 15.12-YB-2026.1.1.2-b0
-- Dumped by ysql_dump version 15.12-YB-2026.1.1.2-b0

SET yb_binary_restore = true;
SET yb_ignore_pg_class_oids = false;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_settings WHERE name = 'yb_ignore_relfilenode_ids') THEN
    EXECUTE 'SET yb_ignore_relfilenode_ids TO false';
  END IF;
END $$;
SET yb_non_ddl_txn_for_sys_tables_allowed = true;
SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

-- Set variable use_tablespaces (if not already set)
\if :{?use_tablespaces}
\else
\set use_tablespaces true
\endif

-- Set variable use_roles (if not already set)
\if :{?use_roles}
\else
\set use_roles true
\endif

-- YB: disable auto analyze to avoid conflicts with catalog changes
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_settings WHERE name = 'yb_disable_auto_analyze') THEN
    EXECUTE format('ALTER DATABASE %I SET yb_disable_auto_analyze TO on', current_database());
  END IF;
END $$;

\if :use_tablespaces
    SET default_tablespace = '';
\endif

--
-- Name: audit_log; Type: TABLE; Schema: public; Owner: yugabyte
--


-- For binary upgrade, must preserve pg_type oid
SELECT pg_catalog.binary_upgrade_set_next_pg_type_oid('16422'::pg_catalog.oid);


-- For binary upgrade, must preserve pg_type array oid
SELECT pg_catalog.binary_upgrade_set_next_array_pg_type_oid('16421'::pg_catalog.oid);


-- For binary upgrade, must preserve pg_class oids and relfilenodes
SELECT pg_catalog.binary_upgrade_set_next_heap_pg_class_oid('16420'::pg_catalog.oid);
SELECT pg_catalog.binary_upgrade_set_next_heap_relfilenode('16420'::pg_catalog.oid);

CREATE TABLE public.audit_log (
    id bigint NOT NULL,
    entity text NOT NULL,
    created_at timestamp with time zone NOT NULL,
    detail text
)
PARTITION BY RANGE (created_at)
SPLIT INTO 1 TABLETS;


\if :use_roles
    ALTER TABLE public.audit_log OWNER TO yugabyte;
\endif

SET default_table_access_method = heap;

--
-- Name: audit_log_2026q1; Type: TABLE; Schema: public; Owner: yugabyte
--


-- For binary upgrade, must preserve pg_type oid
SELECT pg_catalog.binary_upgrade_set_next_pg_type_oid('16425'::pg_catalog.oid);


-- For binary upgrade, must preserve pg_type array oid
SELECT pg_catalog.binary_upgrade_set_next_array_pg_type_oid('16424'::pg_catalog.oid);


-- For binary upgrade, must preserve pg_class oids and relfilenodes
SELECT pg_catalog.binary_upgrade_set_next_heap_pg_class_oid('16423'::pg_catalog.oid);
SELECT pg_catalog.binary_upgrade_set_next_heap_relfilenode('16423'::pg_catalog.oid);


-- For binary upgrade, must preserve pg_class oids and relfilenodes
SELECT pg_catalog.binary_upgrade_set_next_index_pg_class_oid('16426'::pg_catalog.oid);
SELECT pg_catalog.binary_upgrade_set_next_index_relfilenode('16426'::pg_catalog.oid);

CREATE TABLE public.audit_log_2026q1 (
    id bigint NOT NULL,
    entity text NOT NULL,
    created_at timestamp with time zone NOT NULL,
    detail text,
    CONSTRAINT audit_log_2026q1_pkey PRIMARY KEY((id) HASH)
)
SPLIT INTO 1 TABLETS;


\if :use_roles
    ALTER TABLE public.audit_log_2026q1 OWNER TO yugabyte;
\endif

--
-- Name: audit_log_2026q2; Type: TABLE; Schema: public; Owner: yugabyte
--


-- For binary upgrade, must preserve pg_type oid
SELECT pg_catalog.binary_upgrade_set_next_pg_type_oid('16430'::pg_catalog.oid);


-- For binary upgrade, must preserve pg_type array oid
SELECT pg_catalog.binary_upgrade_set_next_array_pg_type_oid('16429'::pg_catalog.oid);


-- For binary upgrade, must preserve pg_class oids and relfilenodes
SELECT pg_catalog.binary_upgrade_set_next_heap_pg_class_oid('16428'::pg_catalog.oid);
SELECT pg_catalog.binary_upgrade_set_next_heap_relfilenode('16428'::pg_catalog.oid);


-- For binary upgrade, must preserve pg_class oids and relfilenodes
SELECT pg_catalog.binary_upgrade_set_next_index_pg_class_oid('16431'::pg_catalog.oid);
SELECT pg_catalog.binary_upgrade_set_next_index_relfilenode('16431'::pg_catalog.oid);

CREATE TABLE public.audit_log_2026q2 (
    id bigint NOT NULL,
    entity text NOT NULL,
    created_at timestamp with time zone NOT NULL,
    detail text,
    CONSTRAINT audit_log_2026q2_pkey PRIMARY KEY((id) HASH)
)
SPLIT INTO 1 TABLETS;


\if :use_roles
    ALTER TABLE public.audit_log_2026q2 OWNER TO yugabyte;
\endif

--
-- Name: audit_log_2026q3; Type: TABLE; Schema: public; Owner: yugabyte
--


-- For binary upgrade, must preserve pg_type oid
SELECT pg_catalog.binary_upgrade_set_next_pg_type_oid('16435'::pg_catalog.oid);


-- For binary upgrade, must preserve pg_type array oid
SELECT pg_catalog.binary_upgrade_set_next_array_pg_type_oid('16434'::pg_catalog.oid);


-- For binary upgrade, must preserve pg_class oids and relfilenodes
SELECT pg_catalog.binary_upgrade_set_next_heap_pg_class_oid('16433'::pg_catalog.oid);
SELECT pg_catalog.binary_upgrade_set_next_heap_relfilenode('16433'::pg_catalog.oid);


-- For binary upgrade, must preserve pg_class oids and relfilenodes
SELECT pg_catalog.binary_upgrade_set_next_index_pg_class_oid('16436'::pg_catalog.oid);
SELECT pg_catalog.binary_upgrade_set_next_index_relfilenode('16436'::pg_catalog.oid);

CREATE TABLE public.audit_log_2026q3 (
    id bigint NOT NULL,
    entity text NOT NULL,
    created_at timestamp with time zone NOT NULL,
    detail text,
    CONSTRAINT audit_log_2026q3_pkey PRIMARY KEY((id) HASH)
)
SPLIT INTO 1 TABLETS;


\if :use_roles
    ALTER TABLE public.audit_log_2026q3 OWNER TO yugabyte;
\endif

--
-- Name: audit_log_2026q4; Type: TABLE; Schema: public; Owner: yugabyte
--


-- For binary upgrade, must preserve pg_type oid
SELECT pg_catalog.binary_upgrade_set_next_pg_type_oid('16440'::pg_catalog.oid);


-- For binary upgrade, must preserve pg_type array oid
SELECT pg_catalog.binary_upgrade_set_next_array_pg_type_oid('16439'::pg_catalog.oid);


-- For binary upgrade, must preserve pg_class oids and relfilenodes
SELECT pg_catalog.binary_upgrade_set_next_heap_pg_class_oid('16438'::pg_catalog.oid);
SELECT pg_catalog.binary_upgrade_set_next_heap_relfilenode('16438'::pg_catalog.oid);


-- For binary upgrade, must preserve pg_class oids and relfilenodes
SELECT pg_catalog.binary_upgrade_set_next_index_pg_class_oid('16441'::pg_catalog.oid);
SELECT pg_catalog.binary_upgrade_set_next_index_relfilenode('16441'::pg_catalog.oid);

CREATE TABLE public.audit_log_2026q4 (
    id bigint NOT NULL,
    entity text NOT NULL,
    created_at timestamp with time zone NOT NULL,
    detail text,
    CONSTRAINT audit_log_2026q4_pkey PRIMARY KEY((id) HASH)
)
SPLIT INTO 1 TABLETS;


\if :use_roles
    ALTER TABLE public.audit_log_2026q4 OWNER TO yugabyte;
\endif

--
-- Name: customers; Type: TABLE; Schema: public; Owner: yugabyte
--


-- For binary upgrade, must preserve pg_type oid
SELECT pg_catalog.binary_upgrade_set_next_pg_type_oid('16387'::pg_catalog.oid);


-- For binary upgrade, must preserve pg_type array oid
SELECT pg_catalog.binary_upgrade_set_next_array_pg_type_oid('16386'::pg_catalog.oid);


-- For binary upgrade, must preserve pg_class oids and relfilenodes
SELECT pg_catalog.binary_upgrade_set_next_heap_pg_class_oid('16385'::pg_catalog.oid);
SELECT pg_catalog.binary_upgrade_set_next_heap_relfilenode('16385'::pg_catalog.oid);


-- For binary upgrade, must preserve pg_class oids and relfilenodes
SELECT pg_catalog.binary_upgrade_set_next_index_pg_class_oid('16389'::pg_catalog.oid);
SELECT pg_catalog.binary_upgrade_set_next_index_relfilenode('16389'::pg_catalog.oid);

CREATE TABLE public.customers (
    id bigint NOT NULL,
    email text NOT NULL,
    region text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT customers_pkey PRIMARY KEY((id) HASH)
)
SPLIT INTO 1 TABLETS;


\if :use_roles
    ALTER TABLE public.customers OWNER TO yugabyte;
\endif

--
-- Name: events; Type: TABLE; Schema: public; Owner: yugabyte
--


-- For binary upgrade, must preserve pg_type oid
SELECT pg_catalog.binary_upgrade_set_next_pg_type_oid('16411'::pg_catalog.oid);


-- For binary upgrade, must preserve pg_type array oid
SELECT pg_catalog.binary_upgrade_set_next_array_pg_type_oid('16410'::pg_catalog.oid);


-- For binary upgrade, must preserve pg_class oids and relfilenodes
SELECT pg_catalog.binary_upgrade_set_next_heap_pg_class_oid('16409'::pg_catalog.oid);
SELECT pg_catalog.binary_upgrade_set_next_heap_relfilenode('16409'::pg_catalog.oid);


-- For binary upgrade, must preserve pg_class oids and relfilenodes
SELECT pg_catalog.binary_upgrade_set_next_index_pg_class_oid('16413'::pg_catalog.oid);
SELECT pg_catalog.binary_upgrade_set_next_index_relfilenode('16413'::pg_catalog.oid);

CREATE TABLE public.events (
    id bigint NOT NULL,
    customer_id bigint NOT NULL,
    kind text NOT NULL,
    payload jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT events_pkey PRIMARY KEY(id ASC)
);


\if :use_roles
    ALTER TABLE public.events OWNER TO yugabyte;
\endif

--
-- Name: events_id_seq; Type: SEQUENCE; Schema: public; Owner: yugabyte
--


-- For binary upgrade, must preserve pg_class oids and relfilenodes
SELECT pg_catalog.binary_upgrade_set_next_heap_pg_class_oid('16408'::pg_catalog.oid);
SELECT pg_catalog.binary_upgrade_set_next_heap_relfilenode('16408'::pg_catalog.oid);

ALTER TABLE public.events ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 100
);


--
-- Name: order_items; Type: TABLE; Schema: public; Owner: yugabyte
--


-- For binary upgrade, must preserve pg_type oid
SELECT pg_catalog.binary_upgrade_set_next_pg_type_oid('16405'::pg_catalog.oid);


-- For binary upgrade, must preserve pg_type array oid
SELECT pg_catalog.binary_upgrade_set_next_array_pg_type_oid('16404'::pg_catalog.oid);


-- For binary upgrade, must preserve pg_class oids and relfilenodes
SELECT pg_catalog.binary_upgrade_set_next_heap_pg_class_oid('16403'::pg_catalog.oid);
SELECT pg_catalog.binary_upgrade_set_next_heap_relfilenode('16403'::pg_catalog.oid);


-- For binary upgrade, must preserve pg_class oids and relfilenodes
SELECT pg_catalog.binary_upgrade_set_next_index_pg_class_oid('16406'::pg_catalog.oid);
SELECT pg_catalog.binary_upgrade_set_next_index_relfilenode('16406'::pg_catalog.oid);

CREATE TABLE public.order_items (
    order_id bigint NOT NULL,
    line_no integer NOT NULL,
    sku text NOT NULL,
    qty integer NOT NULL,
    price numeric(12,2) NOT NULL,
    CONSTRAINT order_items_pkey PRIMARY KEY((order_id, line_no) HASH)
)
SPLIT INTO 1 TABLETS;


\if :use_roles
    ALTER TABLE public.order_items OWNER TO yugabyte;
\endif

--
-- Name: order_seq; Type: SEQUENCE; Schema: public; Owner: yugabyte
--


-- For binary upgrade, must preserve pg_class oids and relfilenodes
SELECT pg_catalog.binary_upgrade_set_next_heap_pg_class_oid('16384'::pg_catalog.oid);
SELECT pg_catalog.binary_upgrade_set_next_heap_relfilenode('16384'::pg_catalog.oid);

CREATE SEQUENCE public.order_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 100;


\if :use_roles
    ALTER TABLE public.order_seq OWNER TO yugabyte;
\endif

--
-- Name: orders; Type: TABLE; Schema: public; Owner: yugabyte
--


-- For binary upgrade, must preserve pg_type oid
SELECT pg_catalog.binary_upgrade_set_next_pg_type_oid('16394'::pg_catalog.oid);


-- For binary upgrade, must preserve pg_type array oid
SELECT pg_catalog.binary_upgrade_set_next_array_pg_type_oid('16393'::pg_catalog.oid);


-- For binary upgrade, must preserve pg_class oids and relfilenodes
SELECT pg_catalog.binary_upgrade_set_next_heap_pg_class_oid('16392'::pg_catalog.oid);
SELECT pg_catalog.binary_upgrade_set_next_heap_relfilenode('16392'::pg_catalog.oid);


-- For binary upgrade, must preserve pg_class oids and relfilenodes
SELECT pg_catalog.binary_upgrade_set_next_index_pg_class_oid('16397'::pg_catalog.oid);
SELECT pg_catalog.binary_upgrade_set_next_index_relfilenode('16397'::pg_catalog.oid);

CREATE TABLE public.orders (
    id bigint DEFAULT nextval('public.order_seq'::regclass) NOT NULL,
    customer_id bigint,
    status text NOT NULL,
    total numeric(12,2),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT orders_pkey PRIMARY KEY((id) HASH)
)
SPLIT INTO 1 TABLETS;


\if :use_roles
    ALTER TABLE public.orders OWNER TO yugabyte;
\endif

--
-- Name: shipments; Type: TABLE; Schema: public; Owner: yugabyte
--


-- For binary upgrade, must preserve pg_type oid
SELECT pg_catalog.binary_upgrade_set_next_pg_type_oid('16417'::pg_catalog.oid);


-- For binary upgrade, must preserve pg_type array oid
SELECT pg_catalog.binary_upgrade_set_next_array_pg_type_oid('16416'::pg_catalog.oid);


-- For binary upgrade, must preserve pg_class oids and relfilenodes
SELECT pg_catalog.binary_upgrade_set_next_heap_pg_class_oid('16415'::pg_catalog.oid);
SELECT pg_catalog.binary_upgrade_set_next_heap_relfilenode('16415'::pg_catalog.oid);


-- For binary upgrade, must preserve pg_class oids and relfilenodes
SELECT pg_catalog.binary_upgrade_set_next_index_pg_class_oid('16418'::pg_catalog.oid);
SELECT pg_catalog.binary_upgrade_set_next_index_relfilenode('16418'::pg_catalog.oid);

CREATE TABLE public.shipments (
    order_id bigint NOT NULL,
    shipped_at timestamp with time zone NOT NULL,
    carrier text NOT NULL,
    tracking text,
    CONSTRAINT shipments_pkey PRIMARY KEY((order_id) HASH, shipped_at DESC)
)
SPLIT INTO 1 TABLETS;


\if :use_roles
    ALTER TABLE public.shipments OWNER TO yugabyte;
\endif

--
-- Name: audit_log_2026q1; Type: TABLE ATTACH; Schema: public; Owner: yugabyte
--

ALTER TABLE ONLY public.audit_log ATTACH PARTITION public.audit_log_2026q1 FOR VALUES FROM ('2026-01-01 00:00:00+00') TO ('2026-04-01 00:00:00+00');


--
-- Name: audit_log_2026q2; Type: TABLE ATTACH; Schema: public; Owner: yugabyte
--

ALTER TABLE ONLY public.audit_log ATTACH PARTITION public.audit_log_2026q2 FOR VALUES FROM ('2026-04-01 00:00:00+00') TO ('2026-07-01 00:00:00+00');


--
-- Name: audit_log_2026q3; Type: TABLE ATTACH; Schema: public; Owner: yugabyte
--

ALTER TABLE ONLY public.audit_log ATTACH PARTITION public.audit_log_2026q3 FOR VALUES FROM ('2026-07-01 00:00:00+00') TO ('2026-10-01 00:00:00+00');


--
-- Name: audit_log_2026q4; Type: TABLE ATTACH; Schema: public; Owner: yugabyte
--

ALTER TABLE ONLY public.audit_log ATTACH PARTITION public.audit_log_2026q4 FOR VALUES FROM ('2026-10-01 00:00:00+00') TO ('2027-01-01 00:00:00+00');


--
-- Name: events_id_seq; Type: SEQUENCE SET; Schema: public; Owner: yugabyte
--

SELECT pg_catalog.setval('public.events_id_seq', 100800, true);


--
-- Name: order_seq; Type: SEQUENCE SET; Schema: public; Owner: yugabyte
--

SELECT pg_catalog.setval('public.order_seq', 100300, true);


--
-- Name: customers_email; Type: INDEX; Schema: public; Owner: yugabyte
--


-- For binary upgrade, must preserve pg_class oids and relfilenodes
SELECT pg_catalog.binary_upgrade_set_next_index_pg_class_oid('16391'::pg_catalog.oid);
SELECT pg_catalog.binary_upgrade_set_next_index_relfilenode('16391'::pg_catalog.oid);

CREATE UNIQUE INDEX NONCONCURRENTLY customers_email ON public.customers USING lsm (email HASH) SPLIT INTO 1 TABLETS;


--
-- Name: orders_customer; Type: INDEX; Schema: public; Owner: yugabyte
--


-- For binary upgrade, must preserve pg_class oids and relfilenodes
SELECT pg_catalog.binary_upgrade_set_next_index_pg_class_oid('16399'::pg_catalog.oid);
SELECT pg_catalog.binary_upgrade_set_next_index_relfilenode('16399'::pg_catalog.oid);

CREATE INDEX NONCONCURRENTLY orders_customer ON public.orders USING lsm (customer_id HASH) SPLIT INTO 1 TABLETS;


--
-- Name: orders_customer_created; Type: INDEX; Schema: public; Owner: yugabyte
--


-- For binary upgrade, must preserve pg_class oids and relfilenodes
SELECT pg_catalog.binary_upgrade_set_next_index_pg_class_oid('16400'::pg_catalog.oid);
SELECT pg_catalog.binary_upgrade_set_next_index_relfilenode('16400'::pg_catalog.oid);

CREATE INDEX NONCONCURRENTLY orders_customer_created ON public.orders USING lsm (customer_id HASH, created_at ASC) SPLIT INTO 1 TABLETS;


--
-- Name: orders_status; Type: INDEX; Schema: public; Owner: yugabyte
--


-- For binary upgrade, must preserve pg_class oids and relfilenodes
SELECT pg_catalog.binary_upgrade_set_next_index_pg_class_oid('16401'::pg_catalog.oid);
SELECT pg_catalog.binary_upgrade_set_next_index_relfilenode('16401'::pg_catalog.oid);

CREATE INDEX NONCONCURRENTLY orders_status ON public.orders USING lsm (status HASH) SPLIT INTO 1 TABLETS;


--
-- Name: orders_total; Type: INDEX; Schema: public; Owner: yugabyte
--


-- For binary upgrade, must preserve pg_class oids and relfilenodes
SELECT pg_catalog.binary_upgrade_set_next_index_pg_class_oid('16402'::pg_catalog.oid);
SELECT pg_catalog.binary_upgrade_set_next_index_relfilenode('16402'::pg_catalog.oid);

CREATE INDEX NONCONCURRENTLY orders_total ON public.orders USING lsm (total ASC);


--
-- Name: FUNCTION pg_stat_statements_reset(userid oid, dbid oid, queryid bigint); Type: ACL; Schema: pg_catalog; Owner: postgres
--

\if :use_roles
SELECT pg_catalog.binary_upgrade_set_record_init_privs(true);
REVOKE ALL ON FUNCTION pg_catalog.pg_stat_statements_reset(userid oid, dbid oid, queryid bigint) FROM PUBLIC;
SELECT pg_catalog.binary_upgrade_set_record_init_privs(false);
\endif


--
-- Name: TABLE pg_stat_statements; Type: ACL; Schema: pg_catalog; Owner: postgres
--

\if :use_roles
SELECT pg_catalog.binary_upgrade_set_record_init_privs(true);
GRANT SELECT ON TABLE pg_catalog.pg_stat_statements TO PUBLIC;
SELECT pg_catalog.binary_upgrade_set_record_init_privs(false);
\endif


--
-- Name: TABLE pg_stat_statements_info; Type: ACL; Schema: pg_catalog; Owner: postgres
--

\if :use_roles
SELECT pg_catalog.binary_upgrade_set_record_init_privs(true);
GRANT SELECT ON TABLE pg_catalog.pg_stat_statements_info TO PUBLIC;
SELECT pg_catalog.binary_upgrade_set_record_init_privs(false);
\endif


-- YB: re-enable auto analyze after all catalog changes
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_settings WHERE name = 'yb_disable_auto_analyze') THEN
    EXECUTE format('ALTER DATABASE %I SET yb_disable_auto_analyze TO off', current_database());
  END IF;
END $$;

--
-- YSQL database dump complete
--

