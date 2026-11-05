# Field notes: what real migrations ran into

Read this during planning (phase 3), before starting a live data phase, and whenever a run is slow, stuck or failing in a way its output does not explain.

These notes come from support and engineering experience with past migrations, collected up to November 2026. They are observations, not product limits or guarantees:

- Present them to the user as field experience ("in past migrations…"), never as a documented limit.
- Numbers depend on cluster shape, schema and release. Use them to decide what to check and when to suggest a test run, not to promise a rate.
- A newer release may have fixed an issue. When one applies, check the release notes and the docs for the user's version before acting on it.
- The CLI's own output, `--help` and the docs still win over these notes.

## 1. Plan-time checks

Raise each item that applies to the user's setup when presenting the flow options. Check what you can with read-only queries.

### Live migration throughput

| Observation | What to do with it |
|---|---|
| Throughput limits of live migration have not been formally benchmarked. Around 2,000 change events per second on a single replication slot is what has worked well in completed migrations. | Get the source's peak write rate (the assessment's per-table write rates, or the user's monitoring). If peaks are well above about 2,000 events/s, tell the user this is beyond what has been commonly run and recommend a production-like dry run without cutover before committing. |
| Change-event import throughput scales with import parallelism. One test on a small 3-node multi-region cluster went from about 40 events/s with the conservative default to about 900 at 200 parallel connections, about 2,250 at 400 and about 3,500 at 800 (80% CPU). | If the importer lags with low target CPU, look at the parallelism and adaptive-parallelism settings in `--help`, and keep total connections below the target's connection limit. Raising parallelism a lot can need internal settings changed together. Changing an undocumented internal setting on a running migration has corrupted its resume state, and some can only be reset by starting over. Never set an undocumented environment variable or internal setting yourself; that is for Yugabyte support to advise. |
| Adaptive parallelism is conservative: it backs off at a CPU threshold and many runs sit at 1 or 2 connections. It averages CPU only over healthy nodes, so it can keep adding load to a cluster that is losing nodes. | Watch target CPU and node health yourself during import, not only Voyager's rate. If nodes go down, slow the import rather than letting it retry harder. |
| Multi-column or partial unique indexes, and unique indexes on nullable columns, can make the importer treat unrelated events as conflicting and serialise them. Import then drops to tens of events per second. | List unique indexes on the migrated tables before the data phase. If the import rate collapses on such tables, check the docs for the CDC partitioning options of `import data` (`--help`) for the installed version. |
| Change events are applied in parallel without the source's transaction boundaries. A primary-key update arrives as a delete plus an insert. | Tell the user the target is not read-consistent while streaming; validate only after cutover. |

### Slots, WAL and disk

| Observation | What to do with it |
|---|---|
| On a PostgreSQL source, the replication slot keeps WAL from the start of the snapshot. If `max_slot_wal_keep_size` is lower than the WAL written during the whole snapshot, the slot is invalidated and live export must start over. Export cannot resume partway through the snapshot phase. | Before starting live export, estimate WAL rate × expected snapshot duration and compare with `max_slot_wal_keep_size` and free disk on the source. The user changes the setting. |
| A replication slot nobody consumes keeps WAL forever. On YugabyteDB, a slot left unconsumed for days built up enough WAL files to hit the open-files limit and take nodes down. | Whenever a Voyager process will stay stopped, or the migration is abandoned, tell the user to drop the slot it leaves (on the source, or on YugabyteDB after a fall-back cutover). |
| YugabyteDB allows a small, fixed number of replication slots per cluster (10 by default). Other CDC pipelines on the same cluster use them too. | For fall-back, check the existing slots on the target (`pg_replication_slots`) before cutover, so the reverse stream can create one. |
| Change files accumulate on the Voyager host for as long as the migration runs. | For migrations expected to stream for weeks, plan `archive changes` from the start and check disk at every poll (live reference §6). |

### Schema traits that hurt live migration

Check these with read-only queries during planning, alongside the flow's documented limitations.

| Trait | What happened |
|---|---|
| DDL on the source while streaming | Not supported. Export or import breaks, or the target silently diverges. Agree a schema freeze with the user for the whole live phase. Additive DDL on the target followed by an import restart has been made to work, but only with care. |
| An index created on the target during streaming | The importer can hit repeated serialization (40001, schema version mismatch) errors and retries; a restart of the importer clears it. Create indexes before cutover only with that in mind. |
| Tables without a primary key | Not supported for live migration. |
| Quoted or mixed-case schema names | Failed in export on older releases; renaming on the source was the workaround. Check the release notes for the installed version. |
| YugabyteDB-specific DDL in the source schema files (`SPLIT INTO`, tablespaces, `HASH`/`ASC` key options), for example a schema prepared by hand for YugabyteDB | `import schema` parses PostgreSQL syntax and failed on these. Create those tables on the target by hand (user approval) and import the rest. |
| Partitioned tables | The partition set is fixed: new partitions cannot be added during the migration. Have the user pre-create the partitions needed for the expected duration plus a margin. Older releases also required the primary key on the parent. |
| hstore columns | A NULL value inside an hstore has stopped CDC permanently on older releases, and hstore values have arrived as NULL in the reverse direction. Check for hstore columns and test them. |
| Domains over enums and other unusual types | Can stop the CDC connector at startup. Check each column type against the connector limitations on the flow page. |
| Sequences | Cutover restores sequences on the target. It has failed when a sequence existed on one side only (a sequence created on the target after export started, or a table subset leaving sequences behind). Compare sequence lists before cutover. |
| Savepoint rollbacks in the application | The YugabyteDB change stream has emitted rows that were rolled back to a savepoint (`ROLLBACK TO SAVEPOINT`, then `COMMIT`); a full `ROLLBACK` is not affected. In fall-back this showed up as duplicate-key errors on the source, and it can silently diverge tables without a unique constraint. Before recommending fall-back, ask whether the application uses savepoints (many ORMs do for nested transactions). |
| Source DDL during fall-back | A column added on the target while streaming back killed both reverse processes. They stayed down for days, close to the end of the change-retention window, and whether rows were missed afterwards could not be proven. The schema freeze covers the fall-back window too. |

### Snapshot (initial data) import

| Observation | What to do with it |
|---|---|
| The target cluster, not the Voyager host, is almost always the bottleneck. A 16 vCPU / 32 GB Voyager host has been enough in tests. Teams that needed a large snapshot done fast added 2–3 times the target resources for the import window. | Compare the assessment's import-time estimate with the user's window. If it does not fit, suggest temporarily scaling the target out (more nodes scaled close to linearly in tests). In one large migration, going from 3 nodes of 8 vCPU to 6 nodes of 16 vCPU and raising the adaptive-parallelism maximum cut the estimate from about two weeks to under three days. |
| Too many concurrent load connections on a few large nodes ran a node out of open files. A wider cluster of smaller nodes with fewer concurrent loads finished. | If import fails with connection-refused or open-files errors, reduce parallelism before retrying. |
| A truncated or newly created table starts with few tablets, so loading it concentrates on one or two tablets until automatic splitting catches up. | For very large tables, discuss pre-splitting on the target before import (YugabyteDB docs; Voyager does not emit split DDL). |
| Rows close to or above 128 MB are effectively unsupported by YugabyteDB, and columns above a few MB are discouraged. | Check the largest row and column sizes on the source during planning for tables with large text, bytea or JSON. |
| Batch import resumes from the last committed batch. With several very large tables, a resume can proceed more slowly than the first run. | Expect resume to take time. Do not restart a slow resume. |

### Fall-back (reverse direction) specifics

| Observation | What to do with it |
|---|---|
| The YugabyteDB change stream reads tablets one after another, so the reverse export rate falls as the number of tablets in the migrated tables grows. In one test, about 2,300 table tablets gave only about 2,200 events/s. The rate was healthy at about 1,500 tablets. | If fall-back matters and the application's write rate after cutover is high, keep the tablet count of the migrated tables modest (fewer, larger tablets) and test the reverse stream at peak load before cutover. |
| An automatic tablet split on a streamed table has killed the reverse exporter. | Check the docs for the user's version on tablet splitting during CDC. Pre-splitting the hottest tables before cutover avoids splits during streaming. If the exporter dies after a split, restart it with the same command. |
| Creating the reverse replication slot at cutover took minutes on clusters with many tablets. There is no way back until it exists. | Tell the user the fall-back path is not available until the reverse stream reports it is running. |
| The reverse exporter can go silent: rate drops to zero with a live PID and no error, usually after a network interruption. | Use the stuck rule: compare the slot's lag on YugabyteDB with the report. If it is not advancing, restart the exporter. |
| Applying changes back to a managed source slowed over time as the source outgrew its cache and hit its IOPS cap. Reverse apply ran well below forward speed. | Size the source for the reverse write rate, not its old read load, if a long fall-back period is planned. |
| The reverse stream's changes are retained on YugabyteDB for a limited time (hours by default). A reverse stream stopped for longer than that cannot resume. | Keep the reverse processes running for the whole fall-back window. If one is down longer than the retention, tell the user fall-back is no longer possible without starting over. |
| Dropping foreign keys and unique constraints for the reverse apply needs superuser, or a specific grant on `session_replication_role` that only newer YugabyteDB and PostgreSQL versions offer. Turning off Voyager's guardrail checks to get past this, without that access, left foreign keys active and the reverse apply hit foreign-key violations. | Check this during planning with the fall-back page's version-specific steps. Do not suggest disabling guardrail checks as a workaround. |
| The reverse stream captures changes only from the moment its slot exists, which is during cutover. | Writes to YugabyteDB must wait until `cutover status` shows the cutover completed (live reference §7). |
| Upgrading YugabyteDB during a fall-back migration broke the reverse stream for an older Voyager release. | Treat a target upgrade mid-migration like a Voyager upgrade: check both release notes for compatibility first, and get the user's approval. |

### Environment

| Observation | What to do with it |
|---|---|
| The CDC connector needs Java 17 or later, and package installs have not always installed it. With an older Java, live export may start but fail only when the first change arrives. | Check `java -version` on the Voyager host at setup. |
| `pg_dump` older than the source's major version fails export. | Compare `pg_dump --version` with the source version at setup. |
| The import user on the target needed real superuser for some steps; YugabyteDB's `yb_superuser` role was not equivalent, and the narrower grant on `session_replication_role` exists only on newer YugabyteDB releases. | Check the import-data page's target-user requirements against the user's YugabyteDB version before the data phase. |
| PostgreSQL-compatible forks (for example EDB) are not supported sources: data moved, but schema translation and assessment did not work reliably. | Treat a fork as out of scope per SKILL.md, and tell the user. |
| Air-gapped and restricted hosts repeatedly hit package-repository, GPG-key and dependency problems. | Use the install page's air-gapped instructions from the start. Install checks for other source types can be irrelevant for PostgreSQL; the install page says how to proceed. |
| YugabyteDB Aeon targets expose only some connectivity by default. The connector used for fall-back may need ports opened by Yugabyte. | For fall-back or fall-forward on Aeon, check the connector requirements on the cutover page early and ask the user to arrange access. |
| A managed source may need its replication settings applied and the instance restarted before a replication connection works. | If live export cannot open a replication connection, check the managed service's replication parameters and whether a restart is pending. |
| Live migration takes table ownership through a shared role; the grant script sets this up. | Explain the ownership options from the grant-script page; the user picks the least invasive. |
| YugabyteDB Connection Manager (the built-in pooler) caused "prepared statement does not exist" errors when cutover restored sequences. | If the target uses Connection Manager and a cutover step fails with prepared-statement errors, check the docs for connecting Voyager directly, bypassing the pooler. |
| Options that truncate target tables on restart fail while any replication slot or CDC stream exists on the target database, even on unrelated tables. | Check for slots on the target before planning a restart that truncates. |

## 2. Symptom → likely cause

Check the likely causes in order, with evidence, before changing anything.

| Phase | Symptom | Likely causes to check |
|---|---|---|
| Export schema | `pg_dump` version or snapshot errors | Client and server major versions differ (the install page names the supported client); a pooler or replica as the source. |
| Export schema | Hangs with no progress after the assessment step | A known hang in the performance-recommendation step on some releases; check the release notes and `--help` for options that skip it, with the user's agreement. |
| Import schema | Fails on statements the analyze report did not flag | A newer `pg_dump` emitting syntax the target does not accept (the release notes may list a fix); partitioned-table primary keys on older targets. |
| Import data (snapshot) | Very slow with low target CPU | Adaptive parallelism capped low; one tablet absorbing a large table (see §1, snapshot). |
| Import data (snapshot) | Connection refused, open-files or RPC errors | Too much concurrency for the node count; rows above the RPC size limit. |
| Import data (snapshot) | Constraint violation on data the source accepts | `NOT VALID` constraints on the source; a newer release may fix it. |
| Live import | Rate of tens of events per second, low target CPU | Unique-index conflict detection serialising events; parallelism too low. |
| Live import | Repeated 40001 / schema version mismatch | DDL or index creation on the target during streaming. |
| Live import | Rate fell to zero, PID alive | Read the log tail. A restart with the same command has cleared stalls in the field, but only after the log is read and quoted to the user. |
| Live export | Slot invalidated or "requested WAL segment has already been removed" | `max_slot_wal_keep_size` too small for the snapshot duration; export restart needs a fresh start (user approval). |
| Live export | Connector dies at startup or on first change | Java version; an unsupported column type (domain over enum, hstore NULLs on older releases). |
| Cutover | Stays initiated | A streaming process is dead (live reference §7). With hundreds of tables, cutover's own checks have taken around 20 minutes; check liveness and the log before calling it stuck. |
| Cutover | Fails restoring sequences | A sequence present on only one side; prepared-statement errors through Connection Manager. |
| Fall-back | Reverse export stops after a tablet split | Auto-split on a streamed table; restart the exporter. |
| Fall-back | Reverse export slow and behind | Tablet count of the migrated tables; reverse export rate falls as it grows. |
| Fall-back | Duplicate key on the source | Savepoint rollbacks in the application emitted as changes. |
| Fall-back | Reverse apply stuck on a lock | A session on the source idle in a transaction; ask the user to find and end it. |
| Any long run | Disk filling on the Voyager host or source | Change files not archived; a slot retaining WAL while a process is down. |
