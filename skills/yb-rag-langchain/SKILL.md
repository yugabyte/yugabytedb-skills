---
name: yb-rag-langchain
description: Use this skill when building RAG, semantic search, or hybrid (vector + full-text) search on YugabyteDB with LangChain — including PGVectorStore setup, vector/GIN index creation, hybrid search configuration, metadata filtering, and relational + vector queries. Triggers on mentions of YugabyteDB + LangChain, PGVectorStore on YugabyteDB, pgvector + YSQL, or RAG pipelines using `langchain_postgres` against a YugabyteDB cluster.
metadata:
  tags: yugabytedb, langchain, pgvector, rag, semantic-search, hybrid-search, ybhnsw
---

# YugabyteDB + LangChain — Agent Guide

Use `PGVectorStore` (the v2 integration) for new RAG and hybrid-search applications. `PGVector` is the older integration. The examples below target `langchain-postgres` 0.0.17; check the installed release before relying on filtering or hybrid-search behavior.

## Connection & dependencies

- For new deployments, use a supported YugabyteDB release with the required pgvector features; 2025.2 or later is the baseline for these examples. Check the [pgvector support and limitations](https://docs.yugabyte.com/stable/additional-features/pg-extensions/extension-pgvector/) for the deployed version before upgrading or troubleshooting an existing cluster.
- YSQL normally listens on **5433**. Use the actual configured endpoint.
- `langchain-postgres` declares upstream **`psycopg[binary]`** as a dependency; use `postgresql+psycopg://` in SQLAlchemy URLs.
- **Check for the smart driver before installing RAG dependencies.** Keep `psycopg-yugabytedb` separate from upstream `psycopg`, `psycopg-binary`, and `psycopg-c`. If present, preserve the working environment and surface the choice: isolate RAG in another environment/process, or explicitly migrate the shared workload to upstream psycopg. Before migrating, fetch the [fork’s options and restrictions](https://docs.yugabyte.com/stable/develop/drivers-orms/python/yugabyte-psycopg3-reference/) and [upstream libpq options](https://www.postgresql.org/docs/current/libpq-connect.html): remove or translate every fork-only setting and alias in connection strings and keyword arguments. Package replacement alone is not a migration.
- `PGVectorStore` requires a `PGEngine`, not a raw connection string. The following is a local-development example; configure credentials and TLS for deployment.

```python
from langchain_postgres import PGEngine, PGVectorStore

engine = PGEngine.from_connection_string(
    "postgresql+psycopg://yugabyte:yugabyte@localhost:5433/yugabyte"
)
```

For upstream random host selection, first check the loaded libpq with `psycopg.pq.version() >= 160000`. It returns a packed integer: libpq 16.2 is `160002`. Maintain an explicit permitted-host list: upstream does not discover tservers or enforce topology keys. This is an alternative connection URL; substitute real hosts and configure TLS before using it:

```python
import psycopg

if psycopg.pq.version() < 160000:
    raise RuntimeError("Random host selection requires libpq 16 or later")
cluster_url = (
    "postgresql+psycopg://yugabyte:yugabyte@/yugabyte"
    "?host=yb-tserver-0,yb-tserver-1&port=5433,5433"
    "&load_balance_hosts=random"
)
# For a cluster deployment, replace the localhost engine creation above with:
# engine = PGEngine.from_connection_string(cluster_url)
```

SQLAlchemy also accepts repeated `host=hostname:port` query parameters and converts them to libpq host/port lists; see [multiple-host URL formats](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#multiple-fallback-hosts). These SQLAlchemy URL forms differ from raw libpq keyword arguments.

Verify new connections with `SELECT inet_server_addr(), inet_server_port()` and test host loss in a test cluster. All listed hosts being unavailable causes connection failure.

## Create the table and store

Choose the embedding model and dimensions together. Every stored vector and query vector must use the same model and dimension. For example, `text-embedding-3-large` produces 3072 dimensions by default; its `dimensions` option can shorten the output, not expand it to 4096. See the [embedding guide](https://developers.openai.com/api/docs/guides/embeddings). This example also requires `langchain-openai` and `OPENAI_API_KEY`.

Explicit metadata columns are useful for typed predicates and B-tree indexes. The helper's catch-all metadata column is **JSON**, not JSONB. In 0.0.17, dictionary filters can address both explicit columns and keys inside that JSON column.

```python
from langchain_openai import OpenAIEmbeddings
from langchain_postgres.v2.engine import Column
from langchain_postgres.v2.hybrid_search_config import (
    HybridSearchConfig,
    reciprocal_rank_fusion,
)
from langchain_postgres.v2.indexes import HNSWQueryOptions

embedding_model = OpenAIEmbeddings(
    model="text-embedding-3-large", dimensions=3072
)
hybrid = HybridSearchConfig(
    tsv_column="content_tsv",
    primary_top_k=20,       # vector candidates before fusion
    secondary_top_k=20,     # full-text candidates before fusion
    fusion_function=reciprocal_rank_fusion,
    fusion_function_parameters={"rrf_k": 60},
)

engine.init_vectorstore_table(
    table_name="embed1",
    vector_size=3072,
    metadata_columns=[Column("username", "TEXT", nullable=True)],
    metadata_json_column="langchain_metadata",
    id_column="langchain_id",
    content_column="content",
    embedding_column="embedding",
    hybrid_search_config=hybrid,
)

store = PGVectorStore.create_sync(
    engine=engine,
    embedding_service=embedding_model,
    table_name="embed1",
    metadata_columns=["username"],
    hybrid_search_config=hybrid,
    index_query_options=HNSWQueryOptions(ef_search=100),
)
```

Run table initialization once for a new table. It attempts `CREATE EXTENSION IF NOT EXISTS vector` and then creates the table; it is not an existing-table migration. Have an administrator enable the extension if needed. For an existing table, validate its columns and dimensions, then create the store without calling initialization. Do not set `overwrite_existing=True` to repair a mismatch: it drops the table.

Pass the same hybrid configuration to table initialization and store creation so the `content_tsv` column exists and is populated during ingestion. Direct SQL writes must maintain that column too. For a preexisting table without it, the library can compute text vectors on demand and build an expression GIN index instead.

## Create indexes after creating the store

The store does not automatically create ANN or full-text indexes. Exact vector search works without an ANN index; add indexes for the query workload. YugabyteDB maps `hnsw` to `ybhnsw`, so the LangChain index helper can be used:

```python
from langchain_postgres.v2.indexes import HNSWIndex

store.apply_vector_index(
    HNSWIndex(name="embed1_hnsw_idx", m=16, ef_construction=200)
)
store.apply_hybrid_search_index(concurrently=True)
```

These are one-time index creation calls; inspect existing indexes before rerunning. Async equivalents are `aapply_vector_index()` and `aapply_hybrid_search_index()`. The GIN call explicitly requests concurrent creation, which makes the library use AUTOCOMMIT; its default transaction would make YugabyteDB downgrade the build to nonconcurrent. This matters when adding the index to an existing table receiving writes. See [index creation modes](https://docs.yugabyte.com/stable/api/ysql/the-sql-language/statements/ddl_create_index/). Vector index builds can block writes; `concurrently=True` does not make those online in YugabyteDB. IVFFlat is unsupported. Verify the target release's limitations before scheduling index creation.

For frequently filtered explicit metadata, add an appropriate SQL index:

```sql
CREATE INDEX IF NOT EXISTS embed1_username_idx ON embed1 (username);
ANALYZE embed1;
```

Run `ANALYZE` after bulk ingestion too. `HNSWQueryOptions` above applies `hnsw.ef_search` to searches on their own pooled connections; a `SET` issued on a different session does not configure the whole pool. ANN filtering can return fewer matches than requested; measure recall and inspect the actual plan.

## Add documents with stable IDs

In 0.0.17, ingestion upserts by ID. Use a stable source/chunk ID when edits should replace an existing document. A content-derived ID deduplicates identical content and metadata, but changed content produces a new ID and requires explicit cleanup of the old document.

```python
import json
import uuid
from langchain_core.documents import Document

def doc_id(content: str, metadata: dict) -> str:
    payload = json.dumps(
        [content, metadata], sort_keys=True, separators=(",", ":"),
        ensure_ascii=False,
    )
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, payload))

pairs = [("Investigate suspected fraud", {"username": "alice", "department": "risk"})]
docs = [Document(page_content=c, metadata=m) for c, m in pairs]
ids = [doc_id(d.page_content, d.metadata) for d in docs]
store.add_documents(docs, ids=ids)
```

The ID column defaults to UUID. Hex-only UUID strings are valid too; arbitrary non-UUID identifiers need a deliberately chosen text ID column. Ingestion commits individual rows, so a failed batch can be partially stored; stable IDs make retrying the batch safe against duplicates.

## Search and filter

Hybrid candidate counts and final result count are different controls: `primary_top_k` and `secondary_top_k` cap candidates, while search-time `k` caps the fused output. Both candidate limits default to 4, so increasing `k` alone may leave too few candidates. RRF is selected explicitly above; the default fusion function in 0.0.17 is weighted-sum ranking.

In 0.0.17, text search mutates the configuration's `fts_query`, and fusion mutates its parameters. Use a fresh configuration and parameter dictionary for each query so later or concurrent searches do not reuse the first query's text:

```python
from dataclasses import replace

query = "fraud investigation notes"
query_hybrid = replace(
    hybrid,
    fts_query=query,
    fusion_function_parameters=dict(hybrid.fusion_function_parameters),
)
results = store.similarity_search_with_score(
    query,
    k=10,
    filter={"username": "alice", "department": "risk"},
    hybrid_search_config=query_hybrid,
)
```

`username` addresses an explicit column; `department` addresses the catch-all JSON field in 0.0.17. Nested JSON keys use dotted paths. Confirm operator support in the installed release; never substitute raw user input into SQL. For vector-only search, pass `hybrid_search_config=None`. Hybrid fusion scores and vector distances have different meanings; do not apply one threshold to both.

## Relational predicates and vector search

`PGVectorStore` does not expose a JOIN builder. Use a read-only view or parameterized SQL for relational predicates. Assume an existing `transactions(customer, amount)` table whose `customer` matches `embed1.username`.

A view can restrict eligible documents without duplicating them when a customer has multiple qualifying transactions:

```sql
CREATE VIEW high_value_chunks AS
SELECT c.langchain_id, c.content, c.embedding, c.username, c.langchain_metadata
FROM embed1 c
WHERE EXISTS (
    SELECT 1 FROM transactions t
    WHERE t.customer = c.username AND t.amount > 100
);
```

Create a separate `PGVectorStore` pointing at `high_value_chunks`, with the same embedding model and `metadata_columns=["username"]`. Use it only for reads and create indexes on the underlying table. Semantic matching comes from the query embedding; an `ILIKE '%fraud%'` condition would add a literal substring restriction, not semantic search.

For a one-off query, use psycopg with **libpq conninfo**, not a SQLAlchemy URL. Set `YSQL_CONNINFO` to the same database, with deployment credentials and TLS:

```python
import os
import psycopg

query_vec = embedding_model.embed_query("fraud investigation")
vector_literal = json.dumps(query_vec)
with psycopg.connect(os.environ["YSQL_CONNINFO"]) as conn, conn.cursor() as cur:
    cur.execute("""
        SELECT c.content, c.username,
               1 - (c.embedding <=> %s::vector) AS cosine_similarity
        FROM embed1 c
        WHERE EXISTS (
            SELECT 1 FROM transactions t
            WHERE t.customer = c.username AND t.amount > %s
        )
        ORDER BY c.embedding <=> %s::vector
        LIMIT %s
    """, (vector_literal, 100, vector_literal, 10))
    rows = cur.fetchall()
```

Index use depends on the plan, selectivity, and release. Verify with `EXPLAIN (ANALYZE, DIST)`; table size alone does not guarantee an ANN index scan for a relationally filtered query.

## Troubleshooting and version checks

| Symptom | Check | Action |
| --- | --- | --- |
| Connection refused | Host, configured port, TLS, reachability | YSQL commonly uses 5433; verify the actual endpoint |
| `ModuleNotFoundError: psycopg2` | SQLAlchemy URL selected psycopg2 | Use `postgresql+psycopg://`; check smart-driver coexistence before installing upstream packages |
| Too few hybrid results | Candidate limits, filters, available matches | Set both candidate limits and final `k`; inspect both searches |
| Metadata filter fails | Installed library version, field type and path | Verify supported dictionary filters; use parameterized SQL for unsupported predicates |
| Re-ingest duplicates | IDs change between runs | Use stable source/chunk IDs or content IDs with cleanup for edits |
| Slow vector search | Missing index or unsuitable plan | Inspect `ybhnsw`, statistics, filters, and `EXPLAIN` |
| Vector dimension mismatch | Embedding output versus column dimension | Align the model and schema; re-embed when changing models or dimensions |
| Invalid UUID input | Identifier format versus ID column type | Use a valid UUID string or explicitly create a text ID column |

`PGVectorStore` is exported by `langchain-postgres` 0.0.14, but this guide's filtering and hybrid behavior was checked against 0.0.17. Resolve a compatible release and verify its APIs before generating code. Implementation references: [vector store](https://github.com/langchain-ai/langchain-postgres/blob/main/langchain_postgres/v2/async_vectorstore.py) and [hybrid configuration](https://github.com/langchain-ai/langchain-postgres/blob/main/langchain_postgres/v2/hybrid_search_config.py); these links track development, so use the installed package source when behavior differs.
