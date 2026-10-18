# SMM-026A/2.1-FU — Final Security & Reliability Fixes

Дата проверки: 2026-10-18
Ветка: `feat/smm-026a-agent-stabilization`
Базовый Git SHA: `b6bf5091d4627482ba43c5870d95c03fe0726657`
SHA-256 проверенного незакоммиченного code diff вместе с новой миграцией:
`70a6ef613340248be776f24af13f06ce8692f267dd9eb002a4b69c9c3638d79f`

## Итог

Исправлены оба P1 и оба P2 независимого review. Реализация остаётся
незакоммиченной. Production, R7, Group O, publisher worker, реальные LLM-вызовы
и публикации не затрагивались.

## Исправления

### P1: DB-enforced retry limit

- В `content_generation_jobs` добавлено доверенное свойство `max_attempts`:
  `NOT NULL`, default `2`, DB constraint `1..10`.
- Авторизованный backend задаёт лимит при создании job. Guard проверяет диапазон
  и запрещает последующее изменение.
- `generation_claim(text, integer)` и `generation_fail(...)` больше не принимают
  лимит от `ai_smm_generation`; обе функции используют значение строки job.
- Claim атомарно увеличивает `attempt_count`. Последний просроченный claim
  переводится в terminal `failed`; исчерпанная queued job повторно не claim-ится.
- Старые worker-controlled сигнатуры удаляются при upgrade и явно проверяются
  как отсутствующие. Direct-role тест подтверждает отказ вызова claim с
  произвольным третьим аргументом и невозможность reclaim после лимита.
- Существующие job получают исторический default `2`; downgrade восстанавливает
  V1-контракты и удаляет колонку/constraint.

### P1: agent output и terminal validation

- `single_post` нормализуется в persisted `text` до вычисления `content_hash` и
  передачи пакета в PostgreSQL.
- До `generation_complete` валидируется весь пакет: число публикаций, структура
  items, непустой текст, формат, title и конечность editor score.
- Неподдерживаемые форматы и malformed output являются terminal errors, не
  запускают второй LLM-вызов и не создают частичных draft.
- После non-transient ошибки SQL-контракта выполняется rollback aborted
  transaction и terminal `generation_fail` в новой транзакции. Lease ownership
  и idempotency остаются DB-enforced.
- DB-side content hash, атомарное сохранение drafts/job completion и защита от
  повторного создания draft не изменены и покрыты тестами.

### P2: topic_hint

- `topic_hint` передаётся в LangGraph state и включается в задание Strategist.
- Ограничение длины `500` проверяется на backend, worker и Strategist boundary.
- Hint явно помечен как выбор угла, а не источник фактов; knowledge загружается
  только по `job.project_id`. Тест подтверждает фактическое присутствие hint в
  prompt и неизменность project-scoped knowledge source.

### P2: release gate

- После изменения независимой матрицы `ai_smm_cc_rw.verify.sql` обновлён её
  fail-closed SHA-256 pin в `deploy/control-center/cc-gate.sh`.

### FU2: output terminality и DB timing bounds

- Подготовка и JSON-сериализация всего agent output выполняются до
  `generation_complete`; malformed/non-serializable output завершает job
  терминально и не допускает второго graph/LLM-вызова.
- Generation SQL contracts ограничивают передаваемые worker значения lease и
  backoff диапазоном `1..3600` секунд. Oversized значения отклоняются напрямую
  от имени `ai_smm_generation`.

## Security invariants

- `ai_smm_generation`: `LOGIN NOINHERIT`, без superuser/CREATEDB/CREATEROLE/
  replication/BYPASSRLS, без memberships и без прав на tables/views/sequences.
- Роль имеет EXECUTE только на три актуальных контракта: claim, complete, fail.
- Все три функции — `SECURITY DEFINER`, с `search_path = pg_catalog, pg_temp`,
  без dynamic SQL; их независимый owner не superuser и не generation role.
- Claim/fail используют job-owned retry ceiling; complete/fail проверяют точный
  `job_id`, `claimed_by` и непросроченный lease.
- Human approval, publications, credentials и sessions роли недоступны.
- Provenance выводится из claimed job и project; API role не может подменить
  generation provenance. Content hash проверяется в PostgreSQL над тем же
  canonical body, что использует ApprovalLineage.
- API RBAC, mutation gate, CSRF и project ownership не ослаблялись.
- LIVE publisher logic не изменён.

## Тестирование

Изолированная среда: PostgreSQL 16.15, `127.0.0.1:55437`, отдельные локальные
test roles/database с `CREATEDB` только для fixture role.

Targeted generation/security/migration/API/agent suite:

- `356 passed in 153.40s`, exit code `0`.

Targeted после локализации release-pin regression:

- `tests/test_control_center_mutations.py` и
  `tests/test_control_center_packaging.py`;
- `155 passed in 20.02s`, exit code `0`.

Targeted FU2 (worker output, direct DB role, queue, migrations, guards):

- `83 passed in 38.55s`, exit code `0`.

Первый полный suite:

- `2912 passed, 1 skipped, 20 failed in 1177.12s`, exit code `1`;
- все 20 FAIL имели одну причину: изменённый verify-файл не совпадал со старым
  fail-closed pin, поэтому Control Center проверки останавливались до своих
  предметных assertions;
- лог: `pytest-full-pg16.log`, статус: `pytest-full-pg16.status`.

После локализации, обновления pin и targeted PASS был необходим один повторный
полный прогон, поскольку первый не исполнил 19 предметных Control Center
сценариев.

Полный suite до FU2:

- **2932 passed, 1 skipped in 1149.96s (0:19:09)**;
- exit code `0`, wall time `1152.66s`;
- лог: `pytest-full-final-pg16.log`;
- статус: `pytest-full-final-pg16.status`.

Финальный полный FU2 suite `pytest -q tests/`:

- **2935 passed, 1 skipped in 1168.78s (0:19:28)**;
- exit code `0`, wall time `1171.58s`;
- лог: `pytest-full-fu2-pg16.log`;
- статус: `pytest-full-fu2-pg16.status`.

Дополнительно: `ruff check src tests alembic` PASS, `git diff --check` PASS,
Python AST parse PASS. Поиск секретов в code diff не обнаружил credentials,
ключей или token values.

## Миграции

- Новый HEAD: `f7a91c2d5e04` (`e04cb3a748f5 -> f7a91c2d5e04`).
- `e04cb3a748f5` теперь устанавливает замороженные V1 contracts/guards, что
  сохраняет корректный исторический upgrade/downgrade path.
- Новый revision добавляет `max_attempts`, ставит V2 guards/contracts и удаляет
  небезопасные старые сигнатуры.
- Targeted tests подтвердили upgrade, downgrade к `e04cb3a748f5`, повторный
  upgrade, наличие/отсутствие колонок, constraints и точных signatures.

## Затронутые файлы

27 tracked modified + 1 новая миграция:

- `alembic/versions/e04cb3a748f5_content_generation_jobs.py`
- `alembic/versions/f7a91c2d5e04_generation_retry_policy.py`
- `deploy/control-center/cc-gate.sh`
- `deploy/control-center/db/ai_smm_cc_rw.grants.sql`
- `deploy/control-center/db/ai_smm_cc_rw.verify.sql`
- `deploy/control-center/db/ai_smm_generation.grants.sql`
- `deploy/control-center/db/ai_smm_generation.verify.sql`
- `src/ai_smm/agents/strategist.py`
- `src/ai_smm/api/routers/generation.py`
- `src/ai_smm/api/schemas.py`
- `src/ai_smm/application/generation.py`
- `src/ai_smm/cli.py`
- `src/ai_smm/db/generation_contracts.py`
- `src/ai_smm/db/guards.py`
- `src/ai_smm/db/models.py`
- `src/ai_smm/generation_queue.py`
- `src/ai_smm/generation_worker.py`
- `src/ai_smm/preflight.py`
- `src/ai_smm/state.py`
- `tests/test_agent_pipeline.py`
- `tests/test_api_generation.py`
- `tests/test_db_generation_role.py`
- `tests/test_db_guards_generation.py`
- `tests/test_db_mutation_role.py`
- `tests/test_generation_queue.py`
- `tests/test_generation_worker.py`
- `tests/test_migration_guards.py`
- `tests/test_migrations.py`

Новые evidence/report artifacts в этом каталоге: настоящий отчёт и шесть
full-suite log/status файла. Ранее существовавшие unrelated untracked evidence
файлы не изменялись.

## Остаточные риски и готовность

- При transient DB outage claim может оставаться `claimed` до lease expiry;
  это ожидаемый crash-recovery путь. DB ceiling гарантирует конечное число
  платных попыток.
- `topic_hint` остаётся пользовательской инструкцией, но ограничен по длине,
  не меняет источник knowledge, не обходит output/DB validation и human review.
- Остаточных P0/P1/P2 из поставленного review не выявлено.

Состояние готово к отдельному security review и локальному commit после явного
разрешения. Commit, push и merge не выполнялись.
