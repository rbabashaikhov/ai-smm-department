# Architecture

```text
Catalog source
  ↓
Python ingestion
  ↓
PostgreSQL 16: products + product_specs
  ↓
Python indexing: documents + chunks
  ↓
n8n RAG Indexing → text-embedding-3-small → pgvector

Telegram
  ↓
n8n Telegram transport
  ↓
n8n AI Consultant (gpt-4.1-mini, window memory)
  ↓
MCP
  ↓
Python Consultant Core
  ↓
SQL + Feature Registry + deterministic ranking
  ↓
structured evidence + catalog passages
  ↓
grounded answer
```

## Границы
- Python: domain/core logic.
- PostgreSQL: catalog truth.
- MCP: controlled tool boundary.
- n8n: orchestration/integration.
- LLM: understanding/tool choice/wording.

В live runtime vector search не выбирает товары: выбор structured, chunks используются как evidence.

Closed tools: `search_tvs`, `get_tv`, `compare_tvs`, `recommend_tvs`, `get_catalog_stats`.
