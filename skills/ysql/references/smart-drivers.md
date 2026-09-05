# Smart Driver Connection Examples

YugabyteDB publishes 9 smart drivers for YSQL. Each extends the upstream PostgreSQL driver with cluster-aware load balancing (distribute connections across all nodes) and topology-aware load balancing (`cloud.region.zone[:priority]`). **The parameter names differ per driver — use the exact spelling shown in each section.**

| Language | Driver | Package | Enable load balancing |
| --- | --- | --- | --- |
| Python | psycopg3 | `psycopg-yugabytedb` | `load_balance_hosts=true` |
| Python | psycopg2 | `psycopg2-yugabytedb` | `load_balance=true` |
| Java | JDBC | `jdbc:yugabytedb://` | `load-balance=true` |
| Java | R2DBC | `com.yugabyte:r2dbc-postgresql` | `loadBalanceHosts=true` |
| Go | pgx | `github.com/yugabyte/pgx/v5` | `load_balance=true` |
| Node.js | node-postgres | `@yugabytedb/pg` | `loadBalance: true` |
| C# | Npgsql | `NpgsqlYugabyteDB` (NuGet) | `Load Balance Hosts=true` |
| Rust | rust-postgres | `yb-postgres` (crate) | `load_balance=true` |
| Ruby | ruby-pg | `yugabytedb-ysql` (gem) | `load_balance=true` |

**Python: match the driver the codebase already uses.** If the project imports `psycopg` (psycopg3), use `psycopg-yugabytedb`. If it imports `psycopg2`, use `psycopg2-yugabytedb`. Do not default to psycopg2 for new code — psycopg3 is the current driver.

## Python (psycopg3) — `pip install psycopg-yugabytedb`
The import name stays `psycopg`; existing psycopg3 code needs no changes. Opt in with `load_balance_hosts` (not `load_balance`).
```python
import psycopg

conn = psycopg.connect(
    "host=yb-tserver-0,yb-tserver-1,yb-tserver-2 port=5433 "
    "dbname=yugabyte user=yugabyte password=yugabyte "
    "load_balance_hosts=true "
    "topology_keys=aws.us-east.us-east-1a"
)
```
Pooling: `pip install "psycopg-yugabytedb[pool]"`, then `psycopg_pool.ConnectionPool(<same conninfo>, min_size=4, max_size=20)` — pooled connections still go through the smart-driver dispatcher.

**psycopg3 traps:**
- `psycopg-yugabytedb` and upstream `psycopg` / `psycopg-binary` / `psycopg-c` all install into `site-packages/psycopg/` and cannot coexist. Uninstall the upstream package first, or use a dedicated virtualenv.
- If `topology_keys` matches no live node, `connect()` raises `OperationalError` — there is no cluster-wide fallback in the current release.

## Python (psycopg2) — `pip install psycopg2-yugabytedb`
(`psycopg2-yugabytedb-binary` for the prebuilt wheel.)
```python
import psycopg2

conn = psycopg2.connect(
    host="yb-tserver-0,yb-tserver-1,yb-tserver-2", port="5433",
    dbname="yugabyte", user="yugabyte", password="yugabyte",
    load_balance="true",
    topology_keys="aws.us-east.us-east-1a:1,aws.us-east.us-east-1b:2"
)
```

## Java (JDBC) — `jdbc:yugabytedb://`
```java
String url = "jdbc:yugabytedb://host1:5433,host2:5433,host3:5433/yugabyte"
    + "?load-balance=true&topology-keys=aws.us-east.us-east-1a:1"
    + "&yb-servers-refresh-interval=300&failed-host-reconnect-delay-secs=5";
```

## Java (R2DBC) — `com.yugabyte:r2dbc-postgresql:1.1.0-yb-2`
```java
PostgresqlConnectionFactory connectionFactory = new PostgresqlConnectionFactory(
    PostgresqlConnectionConfiguration.builder()
        .addHost("host1", 5433)
        .addHost("host2", 5433)
        .username("yugabyte").password("yugabyte").database("yugabyte")
        .loadBalanceHosts(true)
        .ybServersRefreshInterval(10)
        .build());
```
URL form: `r2dbc:postgresql://user:password@host:5433/yugabyte?loadBalanceHosts=true`. Topology: `topologyKeys` (`cloud.region.zone:priority`, comma-separated) — ignored unless `loadBalanceHosts` is true.

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

## C# (Npgsql) — `dotnet add package NpgsqlYugabyteDB`
```csharp
using YBNpgsql;

var connString = "Host=yb-tserver-0,yb-tserver-1,yb-tserver-2;Port=5433;Database=yugabyte;"
               + "Username=yugabyte;Password=yugabyte;"
               + "Load Balance Hosts=true;Topology Keys=aws.us-east.us-east-1a:1,aws.us-east.us-east-1b:2";
var conn = new NpgsqlConnection(connString);
```

## Rust — `yb-postgres = "0.19.7-yb-1-beta.3"` in `Cargo.toml`
```rust
use yb_postgres::{Client, NoTls};

let mut client = Client::connect(
    "postgresql://yugabyte:yugabyte@host1:5433,host2:5433/yugabyte\
     ?load_balance=true&topology_keys=aws.us-east.us-east-1a&yb_servers_refresh_interval=0",
    NoTls,
)?;
```
Add `&fallback_to_topology_keys_only=true` to restrict fallback to the listed keys only.

## Ruby — `gem install yugabytedb-ysql`
Build against a YugabyteDB `pg_config`: `gem install yugabytedb-ysql -- --with-pg-config=<yugabyte-install-dir>/postgres/bin/pg_config`.
```ruby
require 'ysql'

conn = YSQL.connect(
  "postgresql://yugabyte:yugabyte@yb-tserver-0:5433,yb-tserver-1:5433/yugabyte" \
  "?load_balance=true&topology_keys=aws.us-east.us-east-1a:1,aws.us-east.us-east-1b:2"
)
```
