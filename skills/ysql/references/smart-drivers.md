# Smart Driver Connection Examples

## Python — `pip install yugabytedb-psycopg2`
```python
conn = psycopg2.connect(
    host="yb-tserver-0,yb-tserver-1,yb-tserver-2", port="5433",
    dbname="yugabyte", user="yugabyte", password="yugabyte",
    load_balance="true",
    topology_keys="aws.us-east.us-east-1a:1,aws.us-east.us-east-1b:2"
)
```

## Java — `jdbc:yugabytedb://`
```java
String url = "jdbc:yugabytedb://host1:5433,host2:5433,host3:5433/yugabyte"
    + "?load-balance=true&topology-keys=aws.us-east.us-east-1a:1"
    + "&yb-servers-refresh-interval=300&failed-host-reconnect-delay-secs=5";
```

## Go — `github.com/yugabyte/pgx/v5/pgxpool`
```go
connStr := "postgres://yugabyte:yugabyte@host1:5433,host2:5433/yugabyte" +
    "?load_balance=true&topology_keys=aws.us-east.us-east-1a:1"
```

## Node.js — `@yugabytedb/pg`
```javascript
const client = new Client({
    host: 'yb-tserver-0', port: 5433,
    loadBalance: true,               // MUST be true for topologyKeys to work
    topologyKeys: 'aws.us-east.*:1',
});
```
**Node.js trap:** `topologyKeys` is silently ignored if `loadBalance` is not `true`.
