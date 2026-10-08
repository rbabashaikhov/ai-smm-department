# Content angles

## Monday — project overview
**AI-консультант по каталогу: почему обычного чат-бота недостаточно**

Story: natural-language request → stale LLM knowledge problem → separate truth/retrieval/ranking/conversation → Telegram demo → honest status.

## Wednesday — technical solution
**Почему vector search не должен выбирать товары в каталоге**

Story: initial RAG assumption → Phase 3D → exact filters vs semantic similarity → structured SQL + ranking → vector evidence.

## Friday — failure / lesson
**Запрос «подешевле» сломал красивую архитектуру**

Story: vague phrase → invented number → semantic guard removes it from tool call → wording can still repeat it → reject-and-retry failed → limitation frozen/documented.

## Extra
- Почему 75 товаров превратились в 514 chunks
- Как не пересчитывать embeddings на каждом rebuild
- 5 closed tools вместо arbitrary SQL
- Почему `not_listed` ≠ `no`
- Bake-off 4.1-mini vs 4o-mini vs 4.1
- 7/8 demo PASS и 4/15 strict acceptance
- Telegram как thin adapter
- n8n zero-item bug
- Почему “больше контекста” ухудшило ответы
