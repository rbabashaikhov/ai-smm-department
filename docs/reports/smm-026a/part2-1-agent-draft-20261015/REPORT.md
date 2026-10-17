# SMM-026A/2.1 — Agent → Draft: Backend Foundation (2026-10-15)

Статус: локальная реализация, **не закоммичено** (0 коммитов поверх `cc2c4d2`
на ветке `feat/smm-026a-agent-stabilization`, worktree
`ai-smm-department-smm026a`). Production, Group O, R7 не затронуты. Реальных
LLM-вызовов не выполнялось — вся работа на fake-graph тестах.

## Результат прогона тестов (важно: полный прогон не завершён)

Контекст почти исчерпан (99% недельного лимита) посреди повторного полного
прогона `pytest tests/` после серии фиксов. Дождались уже запущенного
прогона (4-й за сессию), дальше не продолжаем.

Последний завершённый полный прогон (`tests/`, тестовый Postgres 16 в Docker,
роль `ai_smm` — владелец БД, не суперюзер, + `postgres` как admin-подключение,
ровно по рецепту `docs/api.md §10`):

**2848 passed, 49 failed, 1 skipped** (1111s, ~18.5 мин).

Новый код (SMM-026A/2.1) сам по себе полностью зелёный:
- `tests/test_generation_queue.py` — 18/18
- `tests/test_generation_worker.py` — 10/10
- `tests/test_db_guards_generation.py` — 11/11 (включая security-инварианты:
  без bound-сессии отказ, только owner, requester=bound user, worker не
  guarded)
- `tests/test_api_generation.py` — 18/18 (RBAC, project isolation, 404/403/409)
- `tests/test_api_mutation_gate.py` — новые 2 роута (`create_generation_job`,
  `cancel_generation_job`) добавлены в `GATED`, весь файл зелёный (115+ тестов)
- Миграция `e04cb3a748f5`: upgrade/downgrade round-trip подтверждён вручную
  (upgrade to head → downgrade to c4d81e2a9f63 → upgrade to head), с реальными
  данными в таблице между шагами — ничего не потеряно, аудит-гард
  (`guard_audit_log_insert`) корректно откатывается к версии c4d81e2a9f63.

### Оставшиеся 49 failed — все до начала SMM-026A/2.1 не встречались

1. **`tests/test_db_guards.py` (18), `tests/test_e2e_owner_workflow.py` (2),
   `tests/mutation_role/test_mr_*.py` (24)** — одна и та же причина:
   `psycopg.errors.InsufficientPrivilege: permission denied for table
   content_items` при INSERT через роль `ai_smm_cc_rw`. Это **голый отказ
   PostgreSQL на уровне привилегий**, не триггер (сравните с моими новыми
   guard-тестами, где ожидается текст "reserved to its owner" и т.п. — здесь
   этого текста нет вообще). Похоже на то, что живая роль `ai_smm_cc_rw`
   в рамках ОДНОЙ pytest-сессии (роли session-scoped) теряет часть грантов
   на `content_items` — вероятный источник: тесты
   `tests/test_control_center_mutations.py` (drift-тесты, которые явно делают
   `GRANT ...`/`REVOKE ...` на живой роли для проверки detection) либо
   что-то в порядке выполнения файлов внутри одной сессии. **Не воспроизведено
   точечно** (SMM-026A/2.1 файлы сами по себе проходят чисто в изоляции) —
   требует отдельного прогона с `-p no:randomly` / `--lf` и, возможно,
   bisect по файлам, чтобы подтвердить, что это не регрессия именно от моих
   правок в `guards.py`/`models.py`. Не похоже на регрессию (мои изменения в
   SQL-грантах затрагивают только `content_generation_jobs`, не
   `content_items`), но не проверено до конца.

2. **`tests/test_api_contract.py::test_the_api_exposes_exactly_the_planned_routes`**
   — простой и ожидаемый: список "ровно эти роуты" не включает 3 новых
   generation-job роута. Тривиальный фикс (добавить 3 пути в список), не
   применён из-за остановки по лимиту.

3. **`tests/test_migrations.py::test_a_lock_timeout_rolls_back_every_pending_revision`**
   — не исследовано, похоже на таймингово-чувствительный тест (lock timeout).

### Уже исправлено в рамках этой сессии (были найдены и закрыты)

- `ck_generation_jobs_claim_shape` constraint был двунаправленным
  (`status='claimed' ⟺ есть claim-поля`) — ломал легитимный cancel
  claimed-джобы через Control Center (guard запрещает трогать claim-поля при
  cancel, constraint требовал их обнулить). Сделан однонаправленным
  (`status='claimed' ⟹ есть поля`). Исправлено и в модели, и в неприменённой
  миграции.
- Гранты INSERT/UPDATE для `ai_smm_cc_rw` на `content_generation_jobs` были
  слишком узкими — SQLAlchemy ORM шлёт все колонки явно (включая NULL для
  worker-полей), привилегия должна это покрывать (как уже сделано для
  `publications`). Расширены, зеркально `publications`.
- `guard_audit_log_insert` (из `c4d81e2a9f63`) не принимал
  `generation.queued`/`generation.cancelled` в белый список business-audit
  действий. Не стал трогать уже выпущенную миграцию `c4d81e2a9f63` —
  сделал версию v2 (`CREATE OR REPLACE` той же функции) внутри новой
  миграции `e04cb3a748f5`, с откатом в `downgrade()` к точной версии v1.
  `models.py` для `create_all` (тесты) собирает ту же v2.
  Пришлось исправить тест с фиксированным числом триггеров (18→20) и
  две "head"-константы (`test_migration_guards.py`), заменил на
  предполагаемый HEAD вместо устаревшего GUARDS.
- `cc-gate.sh`: пины sha256 для `ai_smm_cc.verify.sql` и
  `ai_smm_cc_rw.verify.sql` устарели после моих правок грантов —
  обновлены.
- `src/ai_smm/preflight.py::EXPECTED_SCHEMA_REVISION` обновлён на
  `e04cb3a748f5` (стандартная практика при любой новой миграции, не триггер
  деплоя).
- Тестовые баги (не в прод-коде): ложные срабатывания строкового поиска по
  docstring'ам (`approve_revision` упоминался в прозе, не в импорте) в
  `test_generation_worker.py` и `test_api_mutation_gate.py` (cli.py
  docstring) — заменены на AST-проверку импортов / перефразированы.

## Реализовано (архитектура, без изменений с последнего отчёта)

- **DB**: аддитивная Alembic-миграция `e04cb3a748f5` (`down_revision =
  c4d81e2a9f63`) — таблица `content_generation_jobs`, nullable
  `content_items.generation_job_id`, индексы, guard-триггеры
  insert/update, v2-патч `guard_audit_log_insert`.
- **Security design (ключевой вопрос был решён ДО написания кода записи)**:
  generation worker пишет как table owner (та же роль, что publish worker,
  CLI, importer) — это явно доверенная роль по дизайну `guards.py`
  (`_GUARDED` пропускает owner-роль). Не использован суперюзер, не
  отключались триггеры, не имитировалась human-сессия. Путь через
  Control Center (`ai_smm_cc_rw`) для create/cancel — под полным guard-
  контролем (session-bound, owner-only, запрет менять worker-поля).
- **Generation worker** (`src/ai_smm/generation_worker.py`): атомарный
  claim (`FOR UPDATE SKIP LOCKED`), lease/recovery, bounded retries,
  весь LLM-вызов до единственной финальной транзакции (drafts + статус job
  одним коммитом) — крах до коммита не оставляет дублей. Никогда не
  импортирует `approve_revision`/`materialize_approved_revision`/
  `ai_smm.publishing` (проверено тестом).
- **API/CLI**: 4 роута (`list`, `get`, `create`, `cancel`) с
  RBAC=OWNER на мутациях, VIEWER+ на чтении; под тем же mutation gate,
  что и остальные business-команды. CLI: `generation enqueue/status/
  list/cancel/run-once`.
- **Editor metadata**: версионированный контракт в `revision.meta`
  (`editor_verdict.approved` отдельно от `human_approved`/
  `ContentApproval` — последняя таблица остаётся пустой независимо от
  вердикта Editor).

## Список изменённых/новых файлов

Изменено:
```
deploy/control-center/cc-gate.sh
deploy/control-center/db/ai_smm_cc.grants.sql
deploy/control-center/db/ai_smm_cc.verify.sql
deploy/control-center/db/ai_smm_cc_rw.grants.sql
deploy/control-center/db/ai_smm_cc_rw.verify.sql
src/ai_smm/api/app.py
src/ai_smm/api/dependencies.py
src/ai_smm/api/schemas.py
src/ai_smm/cli.py
src/ai_smm/config.py
src/ai_smm/db/guards.py
src/ai_smm/db/models.py
src/ai_smm/preflight.py
tests/test_api_mutation_gate.py
tests/test_db_least_privilege.py
tests/test_db_mutation_role.py
tests/test_migration_guards.py
```

Новые:
```
alembic/versions/e04cb3a748f5_content_generation_jobs.py
src/ai_smm/api/routers/generation.py
src/ai_smm/application/generation.py
src/ai_smm/generation_queue.py
src/ai_smm/generation_worker.py
tests/test_api_generation.py
tests/test_db_guards_generation.py
tests/test_generation_queue.py
tests/test_generation_worker.py
```

(Плюс несвязанные untracked evidence-файлы от более ранних этапов
SMM-026A/1 и 1b — JSON/log, не трогались в этой сессии.)

## Git status

Ветка: `feat/smm-026a-agent-stabilization`. Последний коммит: `cc2c4d2`
("feat(agents): stabilize series pipeline (SMM-026A/1-1c)"). **0 новых
коммитов** — ничего не закоммичено в этой сессии, push/merge не
выполнялись. Production, Group O, R7 не затронуты.

## Оставшиеся блокеры перед commit/приёмкой

1. Диагностировать и закрыть 18+24+2=44 "permission denied for
   table content_items" отказов (§1 выше) — не похоже на регрессию,
   но не доказано.
2. Обновить `tests/test_api_contract.py` на 3 новых роута.
3. Разобраться с `test_migrations.py::test_a_lock_timeout_rolls_back_every_pending_revision`.
4. Не выполнен прогон `tests/test_api_generation.py` + полного набора
   ПОСЛЕ последнего фикса (`cc-gate.sh`/`preflight.py`/`test_migration_guards.py`)
   одним финальным чистым прогоном без параллельных изменений — текущие
   2848/49/1 числа уже после большинства фиксов, но без финальной
   верификации, что 49→meньше.
5. Commit не выполнялся (не разрешено в этом сообщении).

Остановлено по явному указанию пользователя (лимит 99%): дальнейшие
прогоны/фиксы не предпринимались после получения этого отчёта.
