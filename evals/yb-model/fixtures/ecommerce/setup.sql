-- Synthetic e-commerce schema with planted defects. See answer-key.json.
CREATE SEQUENCE order_seq;
CREATE TABLE customers (
    id          bigint NOT NULL,
    email       text NOT NULL,
    region      text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id HASH)
);
CREATE UNIQUE INDEX customers_email ON customers (email HASH);

CREATE TABLE orders (
    id           bigint NOT NULL DEFAULT nextval('order_seq'),
    customer_id  bigint,
    status       text NOT NULL,
    total        numeric(12,2),
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id HASH)
);
CREATE INDEX orders_customer ON orders (customer_id HASH);
CREATE INDEX orders_customer_created ON orders (customer_id HASH, created_at ASC);
CREATE INDEX orders_status ON orders (status HASH);
CREATE INDEX orders_total ON orders (total ASC);

CREATE TABLE order_items (
    order_id  bigint NOT NULL,
    line_no   int NOT NULL,
    sku       text NOT NULL,
    qty       int NOT NULL,
    price     numeric(12,2) NOT NULL,
    PRIMARY KEY ((order_id, line_no) HASH)
);

CREATE TABLE events (
    id          bigint GENERATED ALWAYS AS IDENTITY,
    customer_id bigint NOT NULL,
    kind        text NOT NULL,
    payload     jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id ASC)
);

CREATE TABLE shipments (
    order_id    bigint NOT NULL,
    shipped_at  timestamptz NOT NULL,
    carrier     text NOT NULL,
    tracking    text,
    PRIMARY KEY ((order_id) HASH, shipped_at DESC)
);

CREATE TABLE audit_log (
    id          bigint NOT NULL,
    entity      text NOT NULL,
    created_at  timestamptz NOT NULL,
    detail      text
) PARTITION BY RANGE (created_at);
CREATE TABLE audit_log_2026q1 PARTITION OF audit_log (PRIMARY KEY (id HASH)) FOR VALUES FROM ('2026-01-01') TO ('2026-04-01');
CREATE TABLE audit_log_2026q2 PARTITION OF audit_log (PRIMARY KEY (id HASH)) FOR VALUES FROM ('2026-04-01') TO ('2026-07-01');
CREATE TABLE audit_log_2026q3 PARTITION OF audit_log (PRIMARY KEY (id HASH)) FOR VALUES FROM ('2026-07-01') TO ('2026-10-01');
CREATE TABLE audit_log_2026q4 PARTITION OF audit_log (PRIMARY KEY (id HASH)) FOR VALUES FROM ('2026-10-01') TO ('2027-01-01');
