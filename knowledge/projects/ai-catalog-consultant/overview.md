# Overview

## Задача
Построить AI-консультанта по реальному товарному каталогу, который отвечает на естественные запросы, но не использует LLM как источник цены, наличия, модели или характеристик.

## Ключевой принцип
**LLM не является источником каталожной истины.**

- structured facts → PostgreSQL;
- product selection → deterministic Python;
- semantic passages → chunks + pgvector;
- request understanding and wording → LLM.

## Reference dataset
75 товаров, 66 доступных, 4 151 строка характеристик, 75 документов, 514 чанков, все 514 embedded.

## Финальный статус
Backend закрыт 2026-10-02. Telegram transport принят 2026-10-03. Neutral release как AI Catalog Consultant — 2026-10-03.

Рабочий MVP через Telegram принят после live-тестов. Strict acceptance Phase 4F.2: **HOLD, 4/15**. После targeted fixes demo suite: **7/8 PASS**. Это portfolio MVP/reference implementation, а не заявка на безошибочную retail-production систему.
