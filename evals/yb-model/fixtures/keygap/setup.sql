-- keygap fixture: counters per device, read by time bucket. Synthetic.
-- counters: the bucket sits after metric and label in the key, so a read of one bucket binds
-- a key column after two unbound ones (planted: CAP014). counters_by_bucket: the same data with
-- the bucket first (the control: no gap).
CREATE TABLE counters (
    tenant_id   text NOT NULL,
    device_id   text NOT NULL,
    grp         text NOT NULL,
    metric      text NOT NULL,
    label       text NOT NULL,
    bucket      text NOT NULL,
    value       double precision,
    updated_at  timestamptz DEFAULT now(),
    PRIMARY KEY ((tenant_id, device_id) HASH, grp ASC, metric ASC, label ASC, bucket ASC)
);
CREATE TABLE counters_by_bucket (
    tenant_id   text NOT NULL,
    device_id   text NOT NULL,
    grp         text NOT NULL,
    metric      text NOT NULL,
    label       text NOT NULL,
    bucket      text NOT NULL,
    value       double precision,
    updated_at  timestamptz DEFAULT now(),
    PRIMARY KEY ((tenant_id, device_id) HASH, grp ASC, bucket ASC, metric ASC, label ASC)
);
