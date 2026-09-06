# Smart Driver Connection Examples

YugabyteDB publishes 9 smart drivers for YSQL. Each extends the upstream PostgreSQL driver with cluster-aware load balancing (distribute connections across all nodes) and topology-aware load balancing (`cloud.region.zone[:priority]`). **The parameter names differ per driver — use the exact spelling shown in each section.**

| Language | Driver | Package | Enable load balancing |
| --- | --- | --- | --- |
| Python | psycopg3 | `psycopg-yugabytedb` | `load_balance_hosts=true` |
| Python | psycopg2 | `psycopg2-yugabytedb` | `load_balance=true` |
| Java | JDBC | `com.yugabyte:jdbc-yugabytedb` (Maven; URL scheme `jdbc:yugabytedb://`) | `load-balance=true` |
| Java | R2DBC | `com.yugabyte:r2dbc-postgresql` | `loadBalanceHosts=true` |
| Go | pgx | `github.com/yugabyte/pgx/v5` | `load_balance=true` |
| Node.js | node-postgres | `@yugabytedb/pg` | `loadBalance: true` |
| C# | Npgsql | `NpgsqlYugabyteDB` (NuGet) | `Load Balance Hosts=true` |
| Rust | rust-postgres | `yb-postgres` (crate) | `load_balance=true` |
| Ruby | ruby-pg | `yugabytedb-ysql` (gem) | `load_balance=true` |

**Versions are deliberately not pinned here.** Resolve the latest release of the named package from its registry at generation time (PyPI, Maven Central, the Go module proxy, npm, NuGet, crates.io, RubyGems) — never write a version from memory. The YugabyteDB drivers are separate packages, so do not filter by version format: PyPI, NuGet and RubyGems publish plain version numbers, while Maven, npm, Go and crates.io use the upstream version plus a `-yb-N` qualifier.

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
- `psycopg-yugabytedb` and upstream `psycopg` / `psycopg-binary` / `psycopg-c` all install into `site-packages/psycopg/` and cannot coexist. Uninstall the upstream package first, or use a dedicated virtualenv. If another dependency requires upstream `psycopg` (for example `langchain_postgres`, see the `yb-rag-langchain` skill), the two cannot share one environment: tell the user and let them choose between a dedicated virtualenv for the smart driver and upstream `psycopg` with infrastructure-level load balancing — do not pick silently.
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

## Java (JDBC) — Maven `com.yugabyte:jdbc-yugabytedb`
The driver registers the `jdbc:yugabytedb://` URL scheme; parameters are hyphenated.
```java
String url = "jdbc:yugabytedb://host1:5433,host2:5433,host3:5433/yugabyte"
    + "?load-balance=true&topology-keys=aws.us-east.us-east-1a:1"
    + "&yb-servers-refresh-interval=300&failed-host-reconnect-delay-secs=5";
```

## Java (R2DBC) — Maven `com.yugabyte:r2dbc-postgresql`
```java
PostgresqlConnectionFactory connectionFactory = new PostgresqlConnectionFactory(
    PostgresqlConnectionConfiguration.builder()
        .addHost("host1", 5433)
        .addHost("host2", 5433)
        .username("yugabyte").password("yugabyte").database("yugabyte")
        .loadBalanceHosts(true)
        .topologyKeys("aws.us-east.us-east-1a:1,aws.us-east.us-east-1b:2")
        .ybServersRefreshInterval(10)
        .build());
```
URL form: `r2dbc:postgresql://user:password@host:5433/yugabyte?loadBalanceHosts=true&topologyKeys=aws.us-east.us-east-1a:1`. `topologyKeys` takes `cloud.region.zone:priority`, comma-separated, and is ignored unless `loadBalanceHosts` is true.

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

## Rust — crate `yb-postgres` in `Cargo.toml`
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
