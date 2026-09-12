# Smart Driver Connection Examples

YugabyteDB publishes 9 smart drivers for YSQL. Each extends the upstream PostgreSQL driver with cluster-aware load balancing (distribute connections across all nodes) and topology-aware load balancing. Most drivers accept `cloud.region.zone[:priority]`; psycopg3 accepts `cloud.region.zone` or `cloud.region.*`, without priorities, with equal preference across matching placements. **The parameter names differ per driver — use the exact spelling shown in each section.**

| Language | Driver | Package | Enable load balancing |
| --- | --- | --- | --- |
| Python | [psycopg3](https://docs.yugabyte.com/stable/develop/drivers-orms/python/yugabyte-psycopg3/) | `psycopg-yugabytedb` | `load_balance_hosts=true` |
| Python | [psycopg2](https://docs.yugabyte.com/stable/develop/drivers-orms/python/yugabyte-psycopg2/) | `psycopg2-yugabytedb` | `load_balance=true` |
| Java | [JDBC](https://docs.yugabyte.com/stable/develop/drivers-orms/java/yugabyte-jdbc/) | `com.yugabyte:jdbc-yugabytedb` (Maven; URL scheme `jdbc:yugabytedb://`) | `load-balance=true` |
| Java | [R2DBC](https://docs.yugabyte.com/stable/develop/drivers-orms/java/yb-r2dbc/) | `com.yugabyte:r2dbc-postgresql` | `loadBalanceHosts=true` |
| Go | [pgx](https://docs.yugabyte.com/stable/develop/drivers-orms/go/yb-pgx/) | `github.com/yugabyte/pgx/v5` | `load_balance=true` |
| Node.js | [node-postgres](https://docs.yugabyte.com/stable/develop/drivers-orms/nodejs/yugabyte-node-driver/) | `@yugabytedb/pg` | `loadBalance: true` |
| C# | [Npgsql](https://docs.yugabyte.com/stable/develop/drivers-orms/csharp/ysql/) | `NpgsqlYugabyteDB` (NuGet) | `Load Balance Hosts=true` |
| Rust | [rust-postgres](https://docs.yugabyte.com/stable/develop/drivers-orms/rust/yb-rust-postgres/) | `yb-postgres` (crate) | `load_balance=true` |
| Ruby | [ruby-pg](https://docs.yugabyte.com/stable/develop/drivers-orms/ruby/yb-ruby-pg/) | `yugabytedb-ysql` (gem) | `load_balance=true` |

**Version selection must respect the application.** These examples omit version numbers; they do not request an upgrade. Inspect the project’s runtime, dependency declarations, lockfile, and dependency-management/BOM constraints first. Retain compatible existing versions unless changing them is part of the task. For a new dependency or requested upgrade, resolve the latest compatible published release of the named YugabyteDB package from its registry (PyPI, Maven Central, the Go module proxy, npm, NuGet, crates.io, RubyGems). Check the selected artifact’s runtime/target-framework requirements and transitive dependencies; newer driver majors can drop older runtimes. Do not filter on a YugabyteDB version suffix or silently change the application’s runtime/framework to install the newest driver. If only prerelease versions are available, identify that status explicitly. Fetch documentation or source matching the selected release to confirm supported options.

The table links to each driver’s documentation; the sections below follow the same order. Replace example hosts, credentials, and placement labels with the cluster’s values and configure TLS for the deployment.

**Python: match the driver the codebase already uses, subject to the coexistence rules below.** If the project imports `psycopg` (psycopg3), use `psycopg-yugabytedb`. If it imports `psycopg2`, use `psycopg2-yugabytedb`. Do not default to psycopg2 for new code — psycopg3 is the current driver.

## Python (psycopg3) — `pip install psycopg-yugabytedb`
The import name stays `psycopg`. Install system `libpq` and use Python 3.10 or later; follow the fork’s documented system-libpq installation path. Do not add upstream `psycopg-binary` or `psycopg-c`: the fork’s coexistence restriction excludes them, even though the upstream binary package bundles libpq. Opt in with `load_balance_hosts` — note it is `_hosts`, unlike psycopg2's `load_balance`. (The driver accepts the dashed spelling `load-balance-hosts` as well; this file uses the underscore form throughout.)
```python
import psycopg

conn = psycopg.connect(
    "host=yb-tserver-0,yb-tserver-1,yb-tserver-2 port=5433 "
    "dbname=yugabyte user=yugabyte password=yugabyte "
    "load_balance_hosts=true "
    "topology_keys=aws.us-east.us-east-1a,aws.us-east.us-east-1b"
)
```
Pooling: `pip install "psycopg-yugabytedb[pool]"`, then use `psycopg_pool.ConnectionPool(<same conninfo>, min_size=4, max_size=20)` as usual. The pool opens each connection through `psycopg.connect()`, which this package provides, so pooled connections are load-balanced like direct ones.

### psycopg3 traps

- Do not install `psycopg-yugabytedb` alongside upstream `psycopg`, `psycopg-binary`, or `psycopg-c`. Check installed packages and dependency declarations first. If a dependency requires upstream psycopg3 (for example `langchain_postgres`), surface the conflict before replacing packages: isolate the smart-driver workload in a separate environment/process, or retain upstream psycopg with `load_balance_hosts=random` and multiple explicit hosts with libpq >= 16 (required for random host selection), or use infrastructure-level load balancing. When migrating, remove `topology_keys`, `yb_servers_refresh_interval`, and `failed_host_reconnect_delay_secs` (including dashed aliases) from connection strings and keyword arguments; upstream libpq rejects these fork-only options. Replace the fork’s `load_balance_hosts=true` (or `load-balance-hosts=true`) with the upstream underscore spelling `load_balance_hosts=random`. Upstream random selection requires a maintained host list; it neither discovers tservers nor enforces topology keys. Fetch the [libpq connection documentation](https://www.postgresql.org/docs/current/libpq-connect.html#LIBPQ-CONNECT-LOAD-BALANCE-HOSTS) before migrating. The `[pool]` extra above is supported; it is not an upstream binary/C extra.
- Use topology entries without priority suffixes: `:1` and `:2` become part of the zone name, not preference levels. The zone may be `*`: `aws.us-east.*` matches every zone in that cloud/region, including newly discovered zones. Cloud and region wildcards are rejected. Multiple entries form an allowlist with equal preference for application connections. Bootstrap and discovery control connections can contact other cluster nodes; topology keys do not isolate all driver traffic to the allowed placements. List multiple allowed zones when availability across zones is required; if none matches a live tserver, `connect()` raises `OperationalError` without cluster-wide fallback. Omit `topology_keys` for cluster-wide balancing. Fetch the [psycopg3 reference](https://docs.yugabyte.com/stable/develop/drivers-orms/python/yugabyte-psycopg3-reference/) for current restrictions.
- Verify the fork is loaded with `python -c "from psycopg.yb.registry import ClusterRegistry; print('smart driver ok')"`, then open connections and check `SELECT inet_server_addr(), inet_server_port()` against the permitted placements. For topology restrictions, test loss of an allowed zone in a test cluster.

## Python (psycopg2) — `pip install psycopg2-yugabytedb`
Use `psycopg2-yugabytedb-binary` instead for the prebuilt wheel. Install only one variant, without upstream `psycopg2` or `psycopg2-binary` in that environment.
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
The driver registers the `jdbc:yugabytedb://` URL scheme; parameters are hyphenated. For region-wide routing, use `topology-keys=aws.us-east.*:1`, as documented in the [JDBC reference](https://docs.yugabyte.com/stable/develop/drivers-orms/java/yugabyte-jdbc-reference/).
```java
String url = "jdbc:yugabytedb://host1:5433,host2:5433,host3:5433/yugabyte"
    + "?load-balance=true&topology-keys=aws.us-east.us-east-1a:1"
    + "&yb-servers-refresh-interval=300&failed-host-reconnect-delay-secs=5";
```

## Java (R2DBC) — Maven `com.yugabyte:r2dbc-postgresql`
The [R2DBC main-branch implementation](https://github.com/yugabyte/r2dbc-postgresql/blob/main/src/main/java/io/r2dbc/postgresql/TopologyAwareLoadBalancerConnectionStrategy.java) matches `*` in the zone segment. Before using `.topologyKeys("aws.us-east.*:1")`, verify this behavior in `TopologyAwareLoadBalancerConnectionStrategy.java` at the tag or commit matching the selected package release; the example below uses explicit zones.
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

## Go — module `github.com/yugabyte/pgx/v5` (pooling via its `pgxpool` package)
For region-wide routing, use `topology_keys=aws.us-east.*:1`, as documented in the [pgx reference](https://docs.yugabyte.com/stable/develop/drivers-orms/go/yb-pgx-reference/).
```go
connStr := "postgres://yugabyte:yugabyte@host1:5433,host2:5433/yugabyte" +
    "?load_balance=true&topology_keys=aws.us-east.us-east-1a:1"
```

## Node.js — `@yugabytedb/pg`
```javascript
const { Client } = require('@yugabytedb/pg');

const client = new Client({
    host: 'yb-tserver-0', port: 5433,
    database: 'yugabyte', user: 'yugabyte', password: 'yugabyte',
    loadBalance: true,               // topologyKeys requires loadBalance
    topologyKeys: 'aws.us-east.*:1',   // every zone in this cloud/region
});
```
For region-wide routing, `topologyKeys: 'aws.us-east.*:1'` includes newly discovered zones in that region. Use explicit zones with priorities when routing should prefer particular zones instead. Default cluster-wide fallback can use other regions when no preferred tserver is available.

Use one reachable bootstrap hostname in `host`, not a comma-separated string. After connecting, the driver discovers tservers through `yb_servers()` and balances subsequent connections; the initial bootstrap address must be reachable.

**Node.js trap:** set `loadBalance: true` when supplying `topologyKeys`. Although the linked documentation says topology keys are ignored when load balancing is disabled, the published driver rejects this configuration during `Client` construction, including when `loadBalance` is omitted. Check the selected package’s behavior rather than relying on silent fallback.

## C# (Npgsql) — `dotnet add package NpgsqlYugabyteDB`
The example below is verified with package release 9.0.2.2. Its `YBNpgsql` namespace also exists in 8.0.3.2, but older releases such as 4.0.10 and 6.0.10-yb-1-beta export `Npgsql.NpgsqlConnection` instead. Changing the import alone is insufficient: 6.0.10-yb-1-beta rejects the shown `Topology Keys` connection-string setting. Check both the namespace and required routing capabilities in the selected compatible release; do not silently remove topology constraints or upgrade the application’s runtime to make this snippet work.

For region-wide routing, `Topology Keys=aws.us-east.*:1` is supported by the [9.0.2.x driver source](https://github.com/yugabyte/npgsql/blob/9.0.2.x/src/Npgsql/TopologyAwareDataSource.cs). For another package version, inspect `src/Npgsql/TopologyAwareDataSource.cs` at the tag or commit corresponding to that release before using a wildcard; the example below uses explicit zones.
```csharp
using YBNpgsql;

var connString = "Host=yb-tserver-0,yb-tserver-1,yb-tserver-2;Port=5433;Database=yugabyte;"
               + "Username=yugabyte;Password=yugabyte;"
               + "Load Balance Hosts=true;Topology Keys=aws.us-east.us-east-1a:1,aws.us-east.us-east-1b:2";
var conn = new NpgsqlConnection(connString);
```

## Rust — crate `yb-postgres` in `Cargo.toml`
```rust
use yb_postgres::{Client, Error, NoTls};

fn connect() -> Result<Client, Error> {
    Client::connect(
        "postgresql://yugabyte:yugabyte@host1:5433,host2:5433/yugabyte\
         ?load_balance=true&topology_keys=aws.us-east.us-east-1a:1,aws.us-east.us-east-1b:2",
        NoTls,
    )
}
```
Leave cluster-wide fallback enabled by default. Add `&fallback_to_topology_keys_only=true` only when placement restrictions require it. In strict mode, keep multiple allowed zones as above if availability across zones is required; connections fail when all listed placements are unavailable, even if other tservers are healthy.

## Ruby — gem `yugabytedb-ysql`
Release 0.7 loads `concurrent` but does not declare the providing gem, `concurrent-ruby`, as a runtime dependency. Install it explicitly with `gem install concurrent-ruby`. For Bundler-managed applications, declare `gem "concurrent-ruby", require: "concurrent"` in the Gemfile alongside `yugabytedb-ysql`; a globally installed gem alone is not sufficient under `bundle exec`.
Also declare `gem "logger"`: the driver requires it, and [Ruby 4.0 moved it from a default gem to a bundled gem](https://www.ruby-lang.org/en/news/2025/12/25/ruby-4-0-0-released/), which must be included in the bundle.

Build against a YugabyteDB `pg_config`: `gem install yugabytedb-ysql -- --with-pg-config=<yugabyte-install-dir>/postgres/bin/pg_config`.
```ruby
require 'ysql'

conn = YSQL.connect(
  "postgresql://yugabyte:yugabyte@yb-tserver-0:5433,yb-tserver-1:5433/yugabyte" \
  "?load_balance=true&topology_keys=aws.us-east.us-east-1a:1,aws.us-east.us-east-1b:2"
)
```
