# Transaction Retry Patterns

## Python
```python
import psycopg2, time, random

TRANSIENT = {'40001', '40P01'}

def with_retry(conn, operation, max_retries=5):
    for attempt in range(max_retries):
        try:
            result = operation(conn)
            conn.commit()
            return result
        except psycopg2.Error as e:
            conn.rollback()              # MUST rollback before retry
            if e.pgcode not in TRANSIENT:
                raise                    # permanent error — fail fast
            delay = min(0.025 * (2 ** attempt), 2.0) + random.uniform(0, 0.01)
            time.sleep(delay)
    raise Exception(f"Transaction failed after {max_retries} retries")
```

## Java
```java
catch (SQLException e) {
    if ("40001".equals(e.getSQLState()) || "40P01".equals(e.getSQLState())) {
        Thread.sleep(Math.min(25 * (1L << attempt), 2000) + ThreadLocalRandom.current().nextLong(10));
        continue;
    }
    throw e;
}
```

## Rules
- Always ROLLBACK before retry
- Exponential backoff + jitter
- Bounded retries (3–10)
- Treat 40001/40P01 identically
- Design for idempotency
- Log retry count + SQLSTATE
