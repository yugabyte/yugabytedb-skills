#!/usr/bin/env python3
"""Print this fixture's workload: the statements build.sh replays so pg_stat_statements holds a
realistic mix. Deterministic (fixed seed). Each line: count, then a template whose {c} is a
customer id, {o} an order id, {s} a shipped order id and {a} an audit_log id."""

import random

MIX = [
    (1500, "SELECT * FROM orders WHERE customer_id = {c} ORDER BY created_at DESC LIMIT 20;"),
    (1200, "SELECT sku, qty, price FROM order_items WHERE order_id = {o};"),
    (900, "SELECT id, email FROM customers WHERE id = {c};"),
    (800, "INSERT INTO events (customer_id, kind, payload) VALUES ({c}, 'view', '{{}}');"),
    (600, "SELECT id FROM customers WHERE lower(email) = 'user{c}@example.com';"),
    (400, "UPDATE orders SET status = 'shipped' WHERE id = {o};"),
    (300, "INSERT INTO orders (customer_id, status, total) VALUES ({c}, 'pending', 10.00);"),
    (300, "SELECT carrier, tracking FROM shipments WHERE order_id IN ({s}, {s2}, {s3}) "
          "ORDER BY shipped_at DESC LIMIT 10;"),
    (200, "SELECT entity, detail FROM audit_log WHERE id = {a};"),
    (150, "SELECT count(*) FROM orders WHERE status = 'pending';"),
    (100, "SELECT id, total FROM orders WHERE customer_id = {c} AND created_at > '2026-01-01';"),
]


def main():
    rng = random.Random(7)
    lines = []
    for n, tpl in MIX:
        for _ in range(n):
            lines.append(tpl.format(c=rng.randint(1, 20000), o=rng.randint(1, 100000),
                                    s=rng.randint(1, 50000), s2=rng.randint(1, 50000),
                                    s3=rng.randint(1, 50000), a=rng.randint(1, 100000)))
    rng.shuffle(lines)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
