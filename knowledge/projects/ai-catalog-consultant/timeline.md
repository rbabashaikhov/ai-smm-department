# Timeline

## Legacy
Ранний прототип: Telegram + n8n AI Agent + RAG + SQL + Postgres Chat Memory.

## 3C
75 documents, 514 chunks; catalog-wide incremental embeddings; batches of 50; embedding-aware sync.

## 3D
21 retrieval cases. Pure vector similarity полезна как evidence retrieval, но недостаточна как sole product selector. Whole-catalog context тоже дал transcription/selection errors.

## 4B
Structured core. Исторические метрики: former vector-only Hit@5 1/6 → 4/6; MRR 0.458; hybrid MRR 0.519 → 0.778. 413 unit + 59 DB tests.

## 4C
Evidence/semantic layer. Semantic retrieval находил evidence, но не улучшил product choice устойчиво; tie-break ухудшил метрики. 431 unit + 86 DB tests.

## 4D
MCP boundary, 5 typed tools, live agent runtime, closed schemas, tool-call cap.

## 4E
Deterministic query semantics/semantic guard. Bake-off: gpt-4.1-mini, gpt-4o-mini, gpt-4.1; 246 scored turns. Решение: KEEP gpt-4.1-mini.

## 4F
Strict acceptance: HOLD, 4/15. После targeted fixes demo: 7/8 PASS. Reject-and-retry hotfix для invented relative number failed and was reverted.

## Phase 5A
Thin Telegram transport, private chats, session `tg:<chat id>`, 45 transport tests, live accepted.

## 4G
Neutral release как AI Catalog Consultant; production не менялся. Final checks: 938 passed / 142 skipped, 148 DB, n8n-tool 120.
