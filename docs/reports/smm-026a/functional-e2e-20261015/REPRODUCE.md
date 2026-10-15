# Воспроизведение Functional E2E (2026-10-15)

Всё локально. Production, VPS, Threads и Storage не нужны и не должны быть доступны.

## Параметры прогона

| Параметр | Значение |
|---|---|
| Код | `main` = `dce93e5`, отдельный detached worktree |
| Модель | `gpt-4.1-mini` во всех трёх агентах (Strategist t=0.4, Copywriter t=0.5, Editor t=0), `timeout=30`, `max_retries=1` |
| Цена для расчёта | $0.40 input, $0.10 cached input, $1.60 output за 1M токенов |
| Проект / KB | `ai-catalog-consultant`, `knowledge/` из того же коммита |
| `max_revisions` | 3 (`E2E_MAX_REVISIONS` для диагностики: 6) |
| Стратегия | агент выбирает сам (без 3-го аргумента) или `standalone_series` |
| `publish_live` | `False` всегда (Publisher — dry run) |
| Langfuse | выключен: `LANGFUSE_TRACING_ENABLED=false`, ключей нет |
| Учёт токенов | `langchain_core.callbacks.get_usage_metadata_callback` (все вызовы процесса) |
| Диагностика `E2E_DIAG_P1=1` | в процессе раннера подавляет ложное «same opening» (D2); файлы репозитория не меняет |

## Окружение: только ключ OpenAI

```bash
K=$(grep '^OPENAI_API_KEY=' <repo>/.env | cut -d= -f2- | tr -d '"'"'")
run(){ env -i HOME=$HOME PATH=$PATH OPENAI_API_KEY="$K" LANGFUSE_TRACING_ENABLED=false \
  timeout 900 uv run python evidence/e2e_agents.py "$@"; }
```

`env -i` гарантирует отсутствие `THREADS_*`, `STORAGE_*`, `*DATABASE_URL`; раннер
дополнительно падает, если они заданы.

## Фаза A — агенты (реальные LLM)

Запуск из корня worktree (нужен `uv sync --frozen`):

```bash
run normal   out/run-normal.json                       # стратегия на выбор агента
run normal   out/run-standalone.json standalone_series
run revision out/run-revision.json  standalone_series  # force_bad_draft=True: внедрённый рекламный черновик
E2E_DIAG_P1=1 run normal out/diag.json standalone_series
E2E_DIAG_P1=1 E2E_MAX_REVISIONS=6 run normal out/diag6.json standalone_series
```

(`E2E_DIAG_P1`/`E2E_MAX_REVISIONS` передавать внутрь `env -i`.) Выход: JSON с
каждым шагом графа (вывод узла, секунды, токены, стоимость), итог и `crash`, если граф упал.

## Фаза B — редакционный слой, approval, worker (fake transport)

```bash
docker --context default run -d --name ai-smm-e2e-pg -e POSTGRES_PASSWORD=<admin> \
  -e POSTGRES_DB=ai_smm_e2e_test -p 127.0.0.1:55440:5432 postgres:16.15-bookworm
docker --context default exec ai-smm-e2e-pg psql -U postgres -d ai_smm_e2e_test \
  -c "CREATE ROLE ai_smm LOGIN PASSWORD '<owner>' CREATEROLE;" \
  -c "ALTER DATABASE ai_smm_e2e_test OWNER TO ai_smm;" -c "ALTER SCHEMA public OWNER TO ai_smm;"
export AI_SMM_TEST_DATABASE_URL=postgresql+psycopg://ai_smm:<owner>@127.0.0.1:55440/ai_smm_e2e_test
export AI_SMM_TEST_ADMIN_DATABASE_URL=postgresql+psycopg://postgres:<admin>@127.0.0.1:55440/ai_smm_e2e_test
uv run pytest -q tests/test_e2e_owner_workflow.py           # санити: стек поднимается (2 passed)
cp <report>/evidence/test_zz_functional_e2e_agents.py tests/  # только во временном worktree
env -u THREADS_ACCESS_TOKEN -u OPENAI_API_KEY \
  E2E_AGENT_RESULT=out/run-normal.json E2E_EVIDENCE=out/phaseB-evidence.json \
  uv run pytest -q tests/test_zz_functional_e2e_agents.py   # 30 проверок
docker --context default rm -f -v ai-smm-e2e-pg             # после прогона
```

Тест берёт последнюю версию Copywriter и последний вердикт Editor из JSON фазы A,
загружает их через `POST /content-items` (`source=copywriter`), проходит
submit → step-up → approve → materialize → schedule/reschedule/cancel и запускает
настоящие `claim_due_publication` + `publish_claimed_publication` с `FakePublisher`.
Любой `connect()` не на loopback внутри теста — `AssertionError`.
