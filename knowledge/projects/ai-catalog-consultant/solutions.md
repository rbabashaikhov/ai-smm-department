# Key engineering solutions

1. **Structured truth instead of LLM facts** — prices, availability, counts and model attributes come from typed tools backed by SQL.
2. **Semantic evidence, not semantic product selection** — pgvector supplies supporting passages.
3. **Five closed tools** — no arbitrary SQL or filter expression exposed to the model.
4. **Three-state feature evidence** — `yes / no / not_listed`.
5. **Semantic guard** — unsupported hard constraints invented by the model are removed before the tool call.
6. **Embedding-aware incremental indexing** — unchanged `content_hash` preserves embeddings; only changed/new chunks are re-embedded.
7. **Thin Telegram transport** — channel adapter only; no business/RAG logic there.
