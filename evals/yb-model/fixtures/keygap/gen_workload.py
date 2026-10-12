#!/usr/bin/env python3
"""Print this fixture's workload (deterministic): reads of one time bucket through each key
order, a read of named metrics written as an OR of conjunctions without parentheses round each
arm, and the upsert that writes the counters."""

import random

MIX = [
    # one bucket, every metric: binds bucket after the unbound metric and label (CAP014)
    (1200, "SELECT metric, label, value FROM counters WHERE tenant_id = 't1' AND device_id = "
           "'d{d}' AND grp = 'g1' AND bucket = 'day:{b}';"),
    # the same read through the bucket-first key: no gap
    (1200, "SELECT metric, label, value FROM counters_by_bucket WHERE tenant_id = 't1' AND "
           "device_id = 'd{d}' AND grp = 'g1' AND bucket = 'day:{b}';"),
    # named metrics: an OR of conjunctions, each arm binding metric and label
    (800, "SELECT metric, value FROM counters WHERE tenant_id = 't1' AND device_id = 'd{d}' "
          "AND grp = 'g1' AND bucket = '' AND (metric = 'm{m}' AND label = '' OR "
          "metric = 'm{m2}' AND label = '');"),
    (600, "INSERT INTO counters (tenant_id, device_id, grp, metric, label, bucket, value) "
          "VALUES ('t1', 'd{d}', 'g1', 'm{m}', '', '', 1) ON CONFLICT (tenant_id, device_id, "
          "grp, metric, label, bucket) DO UPDATE SET value = counters.value + EXCLUDED.value, "
          "updated_at = now();"),
]


def main():
    rng = random.Random(11)
    lines = []
    for n, tpl in MIX:
        for _ in range(n):
            lines.append(tpl.format(d=rng.randint(1, 40), b=rng.randint(1, 10),
                                    m=rng.randint(1, 60), m2=rng.randint(1, 60)))
    rng.shuffle(lines)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
