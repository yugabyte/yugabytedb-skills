# Transaction Retry Patterns

Contents: [psycopg3](#python-psycopg3--psycopg-yugabytedb), [psycopg2](#python-psycopg2--psycopg2-yugabytedb), [Java](#java), [rules](#rules).

For the Python helpers, pass an idle connection with `autocommit=False`. The helper owns the transaction: `operation(conn)` must not commit, roll back, or perform non-idempotent external side effects. `max_attempts` includes the initial attempt. If rollback fails, stop retrying and discard the connection. If rollback raises an `Exception`, the helper raises the original error with the rollback error as its explicit cause. Interruptions such as `KeyboardInterrupt` or `SystemExit` during rollback propagate immediately, with the original error retained as exception context.

## Python (psycopg3 — `psycopg-yugabytedb`)
```python
import logging, psycopg, time, random
from psycopg.errors import SerializationFailure, DeadlockDetected  # 40001, 40P01

def with_retry(conn, operation, max_attempts=5):
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    for attempt in range(max_attempts):
        try:
            result = operation(conn)
            conn.commit()
            return result
        except BaseException as e:       # clean up on interruption too
            try:
                conn.rollback()          # rollback must succeed before retry
            except Exception as rollback_error:
                raise e from rollback_error  # stop; preserve original as primary
            if not isinstance(e, (SerializationFailure, DeadlockDetected)) or attempt + 1 == max_attempts:
                raise                    # permanent error or exhausted attempts
            logging.warning("Retry %d after SQLSTATE %s", attempt + 1, e.sqlstate)
            delay = min(0.025 * (2 ** attempt), 2.0) + random.uniform(0, 0.01)
            time.sleep(delay)
```
psycopg3 exposes one exception class per SQLSTATE; `e.sqlstate` still holds the raw code if you need to log it.

## Python (psycopg2 — `psycopg2-yugabytedb`)
```python
import logging, psycopg2, time, random

TRANSIENT = {'40001', '40P01'}

def with_retry(conn, operation, max_attempts=5):
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    for attempt in range(max_attempts):
        try:
            result = operation(conn)
            conn.commit()
            return result
        except BaseException as e:       # clean up on interruption too
            try:
                conn.rollback()          # rollback must succeed before retry
            except Exception as rollback_error:
                raise e from rollback_error  # stop; preserve original as primary
            if not isinstance(e, psycopg2.Error) or e.pgcode not in TRANSIENT or attempt + 1 == max_attempts:
                raise                    # permanent error or exhausted attempts
            logging.warning("Retry %d after SQLSTATE %s", attempt + 1, e.pgcode)
            delay = min(0.025 * (2 ** attempt), 2.0) + random.uniform(0, 0.01)
            time.sleep(delay)
```

## Java
Pass an idle JDBC connection with auto-commit disabled. The helper owns the transaction: the operation must not commit, roll back, or perform non-idempotent external side effects. `maxAttempts` is positive and includes the initial attempt. The callback may throw checked exceptions; the caller handles or propagates them. Any operation/commit exception or error triggers a rollback attempt. Only SQLSTATE `40001` and `40P01` are retried after successful rollback; application errors and interruptions propagate immediately. If rollback fails, discard the connection; its failure is suppressed on the original exception/error.

```java
import java.sql.Connection;
import java.sql.SQLException;
import java.util.concurrent.ThreadLocalRandom;

public final class TransactionRetry {
    @FunctionalInterface
    public interface Operation<T> {
        T run(Connection conn) throws Exception;
    }

    public static <T> T withRetry(Connection conn, Operation<T> operation,
                                  int maxAttempts) throws Exception {
        if (maxAttempts < 1) {
            throw new IllegalArgumentException("maxAttempts must be at least 1");
        }
        for (int attempt = 0; attempt < maxAttempts; attempt++) {
            try {
                T result = operation.run(conn);
                conn.commit();
                return result;
            } catch (Exception | Error original) {
                try {
                    conn.rollback();
                } catch (Exception | Error cleanup) {
                    if (cleanup != original) {
                        original.addSuppressed(cleanup);
                    }
                    throw original;       // failed cleanup: never retry
                }
                String state = original instanceof SQLException
                    ? ((SQLException) original).getSQLState() : null;
                if (!("40001".equals(state) || "40P01".equals(state))
                        || attempt + 1 == maxAttempts) {
                    throw original;
                }
                System.err.printf("Retry %d after SQLSTATE %s%n", attempt + 1, state);
                long delay = Math.min(25L * (1L << Math.min(attempt, 7)), 2000L);
                Thread.sleep(delay + ThreadLocalRandom.current().nextLong(10));
            }
        }
        throw new AssertionError("unreachable");
    }
}
```

## Rules
- Always ROLLBACK before retry
- Exponential backoff + jitter
- Bounded retries (3–10)
- Treat 40001/40P01 identically
- Design for idempotency
- Log retry count + SQLSTATE
