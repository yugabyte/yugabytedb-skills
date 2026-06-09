---
name: yb-rag-langchain
description: Use this skill when building RAG, semantic search, or hybrid (vector + full-text) search on YugabyteDB with LangChain — including PGVectorStore setup, vector/GIN index creation, hybrid search configuration, metadata filtering, and relational + vector queries. Triggers on mentions of YugabyteDB + LangChain, PGVectorStore on YugabyteDB, pgvector + YSQL, or RAG pipelines using `langchain_postgres` against a YugabyteDB cluster.
metadata:
  tags: yugabytedb, langchain, pgvector, rag, semantic-search, hybrid-search, ybhnsw
---

# YugabyteDB + LangChain — Agent Guide

Concise rules for building RAG and semantic search on YugabyteDB with LangChain. Follow these to avoid silent issues specific to this stack.

## TL;DR — the six things that can bite you

1. Use `PGVectorStore` (v2), **not** `PGVector` (v1). The v1 class lacks hybrid search and explicit metadata columns.
2. YugabyteDB's ANN index is **`ybhnsw`**, but YSQL transparently translates `hnsw` → `ybhnsw`, so LangChain's `HNSWIndex` works unchanged. IVFFlat is still unsupported.
3. PGVectorStore does **not** create vector or GIN indexes for you — call `apply_vector_index()` and (for hybrid search) `apply_hybrid_search_index()` after the table exists. No raw SQL needed for these.
4. `HybridSearchConfig.primary_top_k` / `secondary_top_k` default to **4** and **silently override** the `k=` argument at search time. Set them explicitly.
5. Metadata filtering only works on **explicit columns** (`metadata_columns=[...]`). Filters against the JSONB blob column are not pushed down.
6. LangChain has **no native JOIN** between vector search and relational predicates. Use a `VIEW` or raw SQL for queries like "transactions > £100 AND notes match 'fraud'".

## Connection & dependencies

- **Recommend YugabyteDB 2025.2 or later** for every new setup. Earlier releases have less complete pgvector support; do not help users build on older versions without first advising an upgrade.
- YSQL listens on **port 5433** (not 5432).
- `langchain_postgres` requires **psycopg3** (`psycopg[binary]`), not psycopg2.
- `PGVectorStore` v2 requires a `PGEngine`, not a raw connection string.

```python
from langchain_postgres import PGEngine, PGVectorStore
from langchain_postgres.v2.indexes import HNSWIndex
from langchain_postgres.v2.hybrid_search_config import HybridSearchConfig

engine = PGEngine.from_connection_string(
    "postgresql+psycopg://yugabyte:yugabyte@localhost:5433/yugabyte"
)
```

Enable the extension once per database:
```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

## Creating the table (declare metadata columns up front)

`init_vectorstore_table` creates the table. Declare any metadata fields you need to filter on as **explicit columns** — JSONB-only metadata is not filterable.

```python
from langchain_postgres.v2.engine import Column

engine.init_vectorstore_table(
    table_name="embed1",
    vector_size=4096,
    metadata_columns=[
        Column("username", "TEXT", nullable=True),
    ],
    metadata_json_column="langchain_metadata",  # JSONB catch-all
    id_column="langchain_id",
    content_column="content",
    embedding_column="embedding",
)
```

## Index creation (mandatory — PGVectorStore won't do it for you)

Use the `PGVectorStore` helper methods, not raw SQL. YugabyteDB transparently maps the standard `hnsw` access method to `ybhnsw`, so the LangChain `HNSWIndex` class works as-is.

```python
from langchain_postgres.v2.indexes import HNSWIndex

# ANN vector index — YSQL silently translates hnsw → ybhnsw
store.apply_vector_index(
    HNSWIndex(name="embed1_hnsw_idx", m=16, ef_construction=200)
)
# async equivalent:
# await store.aapply_vector_index(HNSWIndex(...))

# GIN index on the tsvector column for hybrid full-text search
# (only call this if PGVectorStore was created with hybrid_search_config)
store.apply_hybrid_search_index()
# async equivalent:
# await store.aapply_hybrid_search_index()
```

For explicit **metadata columns** there's no PGVectorStore helper — create a B-tree index via raw SQL:

```sql
CREATE INDEX IF NOT EXISTS embed1_username_idx ON embed1 (username);
```

After bulk loads, run `ANALYZE` so the planner picks up the new indexes:
```sql
ANALYZE embed1;
```

Query-time HNSW tuning:
```sql
SET hnsw.ef_search = 100;  -- higher = better recall, slower
```

## Instantiating the store with hybrid search

**Critical**: set `primary_top_k` and `secondary_top_k` explicitly — otherwise you get 4 results regardless of `k=`.

```python
hybrid = HybridSearchConfig(
    primary_top_k=10,    # vector-side candidates
    secondary_top_k=10,  # full-text-side candidates
    fusion_function_parameters={"k": 60},  # RRF default
)

store = PGVectorStore.create_sync(
    engine=engine,
    embedding_service=embedding_model,
    table_name="embed1",
    metadata_columns=["username"],
    hybrid_search_config=hybrid,
)
```

## Adding documents — use deterministic IDs

Random UUIDs cause duplicates on re-ingest. Hash the content + canonical metadata.

```python
import json, uuid
from langchain_core.documents import Document

def doc_id(content: str, metadata: dict) -> str:
    payload = content + json.dumps(metadata, sort_keys=True)
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, payload))

docs = [Document(page_content=c, metadata=m) for c, m in pairs]
ids = [doc_id(d.page_content, d.metadata) for d in docs]
store.add_documents(docs, ids=ids)
```

## Searching — filter on explicit columns only

```python
# Works: username is an explicit column
results = store.similarity_search_with_score(
    "fraud investigation notes",
    k=10,
    filter={"username": "alice"},
)

# Does NOT filter: 'department' lives only in the JSONB blob
# results = store.similarity_search(..., filter={"department": "risk"})  # ignored
```

If you need to filter on a JSONB-only field, either re-create the table with that field as an explicit column, or drop down to raw SQL.

## Relational + vector queries (JOIN scenario)

LangChain cannot join across tables. For *"transactions > £100 where the depositor's notes mention fraud"*, use either:

### Option A — VIEW + PGVectorStore

```sql
CREATE VIEW high_value_fraud_chunks AS
SELECT c.langchain_id, c.content, c.embedding, c.username, c.langchain_metadata
FROM embed1 c
JOIN transactions t ON t.customer = c.username
WHERE t.amount > 100
  AND c.content ILIKE '%fraud%';
```

Then point a second `PGVectorStore` at the view (read-only).

### Option B — raw SQL (recommended for one-off queries)

```python
import psycopg

query_vec = embedding_model.embed_query("fraud investigation")

with psycopg.connect(conn_str) as conn, conn.cursor() as cur:
    cur.execute("""
        SELECT c.content, c.username, t.amount,
               1 - (c.embedding <=> %s::vector) AS score
        FROM embed1 c
        JOIN transactions t ON t.customer = c.username
        WHERE t.amount > 100
        ORDER BY c.embedding <=> %s::vector
        LIMIT 10
    """, (query_vec, query_vec))
    rows = cur.fetchall()
```

The `ybhnsw` index is still used by the planner here when the table is large enough — verify with `EXPLAIN`.

## Cheat sheet for common mistakes

| Symptom | Cause | Fix |
|---|---|---|
| `index method "ivfflat" does not exist` | Used `ivfflat` | Use `HNSWIndex` via `apply_vector_index()` — YSQL maps `hnsw` → `ybhnsw` automatically |
| Hybrid search returns no full-text matches | Forgot the tsv GIN index | Call `store.apply_hybrid_search_index()` (or `aapply_hybrid_search_index`) after table creation |
| Search returns 4 results when `k=10` | Hybrid config defaulted | Set `primary_top_k=10, secondary_top_k=10` |
| Filter returns unfiltered results | Filtered on JSONB-only field | Declare field as explicit `metadata_columns` |
| Re-ingest creates duplicates | Random UUIDs | Pass deterministic `ids=` |
| `connection refused on :5432` | Used PostgreSQL port | YSQL is on 5433 |
| `ModuleNotFoundError: psycopg2` | Used psycopg2 with langchain_postgres | Install `psycopg[binary]` |
| Slow vector search | Missing or unbuilt HNSW index | Create `ybhnsw`, then `ANALYZE` |
| `dimensions=3072` mismatch on text-embedding-3-large | Default OpenAI output | Pass `OpenAIEmbeddings(model="text-embedding-3-large", dimensions=4096)` |
| `invalid input syntax for type uuid` on `add_documents` | Passed a raw hex string as ID | Use `uuid.uuid5(uuid.NAMESPACE_DNS, payload)` — produces a deterministic UUID |

## Version notes

- **Always recommend YugabyteDB 2025.2 or later.** Earlier releases either lack `ybhnsw` entirely or ship with known pgvector limitations that have since been fixed. If a user reports being on an older version (2.20, 2.23, 2024.x), advise upgrading before debugging vector-index behaviour.
- `langchain_postgres` **≥ 0.0.14** for `PGVectorStore` v2; verify `from langchain_postgres import PGVectorStore` resolves.
- `PGEngine.from_connection_string()` accepts a SQLAlchemy URL — use the `postgresql+psycopg://` driver prefix.
