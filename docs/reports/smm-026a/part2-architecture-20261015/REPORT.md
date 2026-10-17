# SMM-026A/2 — Agent → Draft → Control Center: архитектурная проработка

**Только анализ и проектирование. Код не менялся, миграции не применялись,
реальных LLM-вызовов не было.** Ветка `feat/smm-026a-agent-stabilization`
(рабочая копия на коммите `cc2c4d2`), worktree не изменён:
`git status --short` пуст, ничего не стейджено.

Изучено: `db/models.py`, `queue.py`, `worker.py`, `application/content.py`,
`application/control_plane.py`, `api/routers/content.py`,
`api/mutation_gate.py`, `security/rbac.py`, `api/dependencies.py`, `series.py`,
`cli.py`, `config.py`, `web/src/pages/CreateContentPage.tsx`,
`web/src/features/projects/hooks.tsx`, `web/src/api/endpoints.ts`, `graph.py`,
`state.py`, и отчёты SMM-026A/1–1c.

## 1. Текущий разрыв

Агентный pipeline (`ai_smm.graph.build_graph()`) и редакционный слой БД никак
не связаны:

- Strategist/Copywriter/Editor работают **только в памяти процесса**, их
  запускают вручную скриптами (`scripts/run_demo.py`,
  `scripts/test_strategist.py`) или через pytest. Результат — `dict`,
  возвращаемый `graph.invoke(...)`, который никто не сохраняет.
- `content_items`/`content_revisions` поддерживают провенанс агента
  (`RevisionSource.STRATEGIST/COPYWRITER/EDITOR`, `editor_score`,
  `editor_notes` — уже есть в схеме, см. `db/models.py:162-174, 1284-1287`),
  но **ничего их туда не кладёт**. Мост `create_content_item()` есть и
  работает (проверено вручную в functional-E2E, фаза B), просто его никто не
  вызывает после `graph.invoke()`.
- Группировка «3 поста — одна серия» существует только на уровне
  **публикаций** (`ContentSeries` ссылается на `Publication`, не на
  `ContentItem` — `db/models.py:530-539`). На уровне черновиков такой связи
  нет вообще: план Strategist (`series_title`, `publishing_strategy`,
  `planned_total`) после генерации текстов никуда не сохраняется, кроме как в
  свободном JSONB `content_revisions.metadata`, если его туда явно положить.
- Вердикт Editor (`editor_approved`, `editor_narrative_score`,
  `editor_issues`) — вообще не персистентен: `ContentRevision` хранит только
  `editor_score`/`editor_notes` (число и текст), а дихотомию «approved/not»
  и список issues класть некуда без изменения схемы или использования
  `metadata`.
- UI: `CreateContentPage.tsx` — это ручная форма (`RevisionForm` на пустом
  драфте). Кнопки «сгенерировать через агентов» нет нигде.

Итог: техническая возможность (бридж `create_content_item`, статусная
машина, approval, очередь, worker) полностью готова и захеджирована
тестами; не хватает ровно одного звена — **кто вызывает граф и кладёт его
результат в БД**, и минимального UI поверх этого.

## 2. Пользовательский сценарий (предлагается)

1. Owner на странице проекта нажимает **«Generate content plan»** (новая
   кнопка рядом с «Create Content», видна только при
   `can.canEditContent` — то же правило, что уже ограничивает ручное
   создание до owner+mutations-on).
2. Короткая форма: `series_size` (по умолчанию 3), необязательная
   `publishing_strategy` (авто/standalone/reply_thread), необязательная
   тематическая подсказка. Submit → `POST
   /projects/{id}/generation-jobs` → `202 Accepted`, job в статусе
   `queued`.
3. Страница job'а (или баннер на Content list) опрашивает статус
   (react-query `refetchInterval`, как уже делает `useMutationsEnabled`
   и другие polling-хуки). `queued → claimed → succeeded` обычно за
   20–90 c (по данным реальных E2E part1/1b).
4. По готовности — редирект/ссылка на **N новых content items**
   (`status=draft`, `source=copywriter`), сгруппированных по
   `generation_job_id`, с видимым **вердиктом Editor** на каждом (сейчас
   отсутствует в UI — закрывает находку D7 из functional-E2E отчёта).
5. Дальше — **без единой новой строчки бизнес-логики**: owner открывает
   существующий `ContentDetailPage`, правит при необходимости (новая
   ревизия), `submit-review → step-up password → approve → materialize →
   schedule`. Весь этот путь уже реализован, протестирован (97 офлайн- и
   десятки DB-тестов) и не меняется.
6. Если Editor не одобрил — это видно сразу на карточке черновика
   (`editor_approved=false`, `editor_issues`); owner либо правит текст сам
   (новая ревизия, `source=human`), либо (этап 3, не в этом MVP) жмёт
   «Перегенерировать».

Явно **не** в этом сценарии: автоматическое одобрение, автопубликация,
повторные попытки без участия человека сверх ретраев самого job'а.

## 3. Минимальные изменения

### 3.1 DB schema — одна новая таблица, одна новая колонка

```sql
CREATE TYPE generation_job_status AS ENUM
    ('queued', 'claimed', 'succeeded', 'failed', 'cancelled');

CREATE TABLE content_generation_jobs (
    id                  uuid PRIMARY KEY,
    project_id          varchar(120) NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    requested_by_user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    status              generation_job_status NOT NULL DEFAULT 'queued',
    params              jsonb NOT NULL DEFAULT '{}',   -- series_size, publishing_strategy, topic_hint
    result              jsonb NOT NULL DEFAULT '{}',   -- content_item_ids, tokens, cost_usd, seconds
    last_error          text,
    attempt_count       integer NOT NULL DEFAULT 0,
    next_attempt_at     timestamptz,
    claimed_by          varchar(120),
    claimed_at          timestamptz,
    lease_expires_at    timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    finished_at         timestamptz
);
CREATE INDEX ix_generation_jobs_due
    ON content_generation_jobs (next_attempt_at)
    WHERE status IN ('queued', 'claimed');
CREATE INDEX ix_generation_jobs_project ON content_generation_jobs (project_id);

ALTER TABLE content_items
    ADD COLUMN generation_job_id uuid
        REFERENCES content_generation_jobs(id) ON DELETE SET NULL;
CREATE INDEX ix_content_items_generation_job ON content_items (generation_job_id);
```

Намеренно НЕ меняется: `content_revisions`, `content_approvals`,
`publications`, `content_series`, ни один guard/триггер из `db/guards.py`.
Вердикт Editor (`editor_approved`, `editor_narrative_score`,
`editor_issues`, `series_title`/`position`/`series_total`/
`publishing_strategy`) кладётся в уже существующий свободный
`content_revisions.metadata` (JSONB) — ровно так, как это уже делал вручную
phase-B функционального E2E теста. Числовой `editor_score` и текстовый
`editor_notes` — в уже существующие колонки без изменений.

Миграция — **чисто аддитивная** (новый тип, новая таблица, одна nullable
колонка с индексом): совместима со старым workerом «как есть» (см. §7) и
проходит тот же тест на аддитивность, что уже требует
`tests/test_migrations.py` для прежних миграций control-plane.

### 3.2 API — 3 новых роута, по образцу `routers/content.py`

```
POST /api/v1/projects/{project_id}/generation-jobs   201  owner+, mutations-gate
     {series_size?, publishing_strategy?, topic_hint?}
     -> GenerationJobOut {id, status: "queued", ...}

GET  /api/v1/generation-jobs/{job_id}                 200  viewer+
     -> GenerationJobOut {id, status, result, error, content_item_ids}

GET  /api/v1/projects/{project_id}/generation-jobs     200  viewer+
     -> paginated list (как list_items)

POST /api/v1/generation-jobs/{job_id}/cancel          200  owner+, mutations-gate
     -> отменяет только queued/claimed с истёкшим lease; running — нет
        (не убивает воркер, просто просит не ретраить).
```

Всё проходит через **существующий** `mutation_gate` (он уже навешан на весь
`/api/v1` роутер — ничего добавлять не нужно) + `require_mutations_enabled`
внутри команды (как в `materialize_approved_revision`). Чтение — существующие
`ViewerProject`/новый `ViewerGenerationJob`-зависимость по аналогии с
`ViewerContentItem` (копия паттерна `dependencies.py:400-476`, тот же
подход: загрузить по id → авторизовать по `project_id` строки).

### 3.3 Worker — отдельный лёгкий процесс, не трогает LIVE worker

**Рекомендация: отдельный процесс** `ai_smm.generation_worker` (новый модуль,
аналог `ai_smm/worker.py` по структуре, в разы короче), а не второй job в
существующем `Worker`. Причины:

- generation-worker-у не нужен **ни один** из security-критичных ресурсов
  LIVE worker'а: ни приватный ключ credential'ов, ни `social_accounts`, ни
  `publications`, ни `ThreadsPublisher`. Отдельный процесс = отдельная,
  более узкая DB-роль (`ai_smm_generation`: `INSERT/UPDATE` на
  `content_generation_jobs`, `INSERT` на `content_items`/
  `content_revisions`, `SELECT` на `projects`, `INSERT` на `audit_log` —
  и ничего больше), в духе уже принятого в проекте принципа
  least-privilege (`docs/least-privilege-role.md`, роли `ai_smm_auth`,
  `ai_smm_cc`, `ai_smm_cc_rw`).
- Падение/зависание нового, менее обкатанного кода (реальные LLM-вызовы,
  внешняя сеть к OpenAI) **не может** уронить или заблокировать
  security-hardened LIVE worker — они физически разные процессы/контейнеры.
- Деплой/откат независимы: новую фичу можно выкатывать и откатывать, не
  трогая worker, который уже прошёл R7-готовность и ждёт отдельного окна.

Внутреннее устройство — **дословно тот же паттерн**, что уже есть в
`worker.py` и `queue.py`:

```python
# ai_smm/generation_queue.py — аналог queue.claim_due_publication
def claim_due_generation_job(session, *, worker_id, lease_seconds) -> GenerationJob | None:
    candidate = (
        select(ContentGenerationJob.id)
        .where(status == QUEUED, next_attempt_at <= now)
        .order_by(created_at).limit(1)
        .with_for_update(skip_locked=True)          # тот же FOR UPDATE SKIP LOCKED
    )
    ...  # UPDATE ... WHERE status = 'queued' RETURNING id — та же гонка-free схема

# ai_smm/generation_worker.py — аналог Worker.run_cycle/process_one
class GenerationWorker:
    def run(self):
        setup_logging(...); self.install_signal_handlers()
        scheduler = BackgroundScheduler(timezone="UTC")
        scheduler.add_job(self.run_cycle_safely, trigger="interval",
                           seconds=settings.generation_poll_interval_seconds, ...)
        ...  # идентично Worker.run(), другой settings-namespace

    def process_one(self):
        with session_scope(settings) as session:
            job = claim_due_generation_job(session, worker_id=..., lease_seconds=...)
            if job is None: return None
            session.commit()                         # claim виден всем сразу
            self._run_job(session, job)               # см. §4

def main() -> int:
    load_local_dotenv(); settings = get_settings()
    GenerationWorker(settings=settings).run()
```

Используются **без изменений**: `ai_smm.db.session.session_scope` /
`with_db_retry`, `ai_smm.queue.record_audit`, `ai_smm.knowledge.loader`,
`ai_smm.graph.build_graph`, `ai_smm.application.content.create_content_item`.
Новый код — по объёму сравним с `queue.py`-частью про claim/lease (≈150–200
строк) плюс обвязка воркера (≈150 строк) — то есть то же, что уже есть для
публикаций, просто продублированное по образцу для другой таблицы.

**Альтернатива подешевле** (если в ревью решат не заводить второй
контейнер): второй `scheduler.add_job(...)` прямо в существующем
`Worker.run()`. APScheduler `BackgroundScheduler` по умолчанию поднимает пул
потоков (`ThreadPoolExecutor`, `max_workers` по умолчанию 10 — в коде это
никак не переопределено), так что второй периодический job технически не
блокирует первый. Плата — общая судьба процессов и общая, более широкая
DB-роль. Решение — на усмотрение ревью; таблица/контракты ниже одинаковы в
обоих случаях.

### 3.4 Frontend — одна страница/кнопка + переиспользование существующего

- `CreateContentPage.tsx`: кнопка-ссылка «Generate with agents» рядом с
  существующей формой, видимая при том же `can.canEditContent`.
- Новая `GenerateContentPage.tsx`: короткая форма (3 поля) +
  `useMutation` на `api.generation.create(...)`, затем `useQuery` с
  `refetchInterval` на статус job'а (тот же паттерн, что уже используют
  существующие polling-хуки в `features/projects/hooks.tsx`).
- `web/src/api/endpoints.ts`: блок `generation: { create, get, list,
  cancel }` — по образцу уже существующего `content: {...}` блока.
- **Не трогается**: `ContentDetailPage.tsx`, `RevisionForm.tsx`,
  `PublicationActions.tsx` — разве что в `ContentDetailPage` добавить
  вывод `editor_approved`/`editor_issues` из `revision.metadata`, если они
  там есть (мелкая, некритичная правка, отдельным PR).

## 4. Контракты

```python
# Job (новая таблица)
GenerationJobParams = TypedDict(
    "GenerationJobParams",
    {"series_size": int, "publishing_strategy": str | None, "topic_hint": str | None},
)

GenerationJobResult = TypedDict(
    "GenerationJobResult",
    {
        "content_item_ids": list[str],       # в порядке позиции в серии
        "series_title": str,
        "revision_count": int,               # сколько доработок понадобилось
        "tokens": dict[str, int],            # {"input": .., "output": .., "cached": ..}
        "cost_usd": float,
        "seconds": float,
    },
)

# Editor verdict (в content_revisions.metadata, не новая колонка)
RevisionGenerationMetadata = TypedDict(
    "RevisionGenerationMetadata",
    {
        "generation_job_id": str,
        "series_title": str,
        "position": int,
        "series_total": int,
        "publishing_strategy": str,
        "editor_approved": bool,
        "editor_narrative_score": float,
        "editor_issues": list[str],
    },
)
```

Граф вызывается **без изменений**: `build_graph().invoke(state)`, где
`state` — ровно `SMMState` (`ai_smm/state.py`), `publish_live` всегда
`False` (генерация никогда не публикует — `publisher_node` в этом потоке
просто игнорируется: читаем только `content_plan`/`draft`/`editor_*`).
Один вызов `create_content_item(..., source=RevisionSource.COPYWRITER,
editor_score=.., editor_notes=.., metadata={...RevisionGenerationMetadata})`
на каждую публикацию из `draft["publications"]` — функция уже существует и
не меняется.

## 5. Статусы, retries, идемпотентность, ошибки

| | Publication (существует) | GenerationJob (предлагается) |
|---|---|---|
| Статусы | 9, с `needs_review` на неоднозначный внешний исход | 5, **без** `needs_review` |
| Почему проще | Threads POST может «и создать пост, и не ответить» | вызов LLM не создаёт внешнего состояния; процесс может упасть где угодно — ничего вовне не произошло |
| Claim | `FOR UPDATE SKIP LOCKED` + lease | то же самое, тот же код-паттерн |
| Retry | `mark_retryable_failure`/`mark_permanent_failure`, `AttemptOutcome` (success/error/timeout/unknown/dry_run) | проще: сетевой/таймаут-эксепшен → retry (backoff, `max_attempts`, по умолчанию 2); ValueError/неизвестный project → `failed` сразу, без retry |
| Идемпотентность исхода | `publication_attempts` пишется **до** внешнего вызова — на случай обрыва | **одна транзакция**: все вызовы LLM — до открытия транзакции записи; `create_content_item × N` + `job.status = succeeded` коммитятся одним махом. Упал где угодно раньше — в БД не осталось ничего, lease истёк, job снова `queued`, следующий воркер просто переделает LLM-вызовы заново. **Дублирующихся content_items в принципе не может возникнуть** — не нужен аналог `needs_review`/ручной реконсиляции |
| Ошибка показывается | `last_error`, `/errors` CLI/UI | то же: `last_error`, `GET .../generation-jobs/{id}` |

Это заметно **проще** модели паблишинга именно потому, что генерация не
имеет необратимого внешнего эффекта — на этом стоит явно настоять в ревью,
чтобы не переносить сюда лишний аппарат `publication_attempts`/`AttemptOutcome`
по инерции.

## 6. Права доступа и human approval

- Создание/отмена job'а — **owner+**, тот же уровень, что уже требуют ВСЕ
  мутации content-слоя (`OwnerProject`/`OwnerContentItem` в
  `dependencies.py` — несмотря на то, что докстринги в `routers/content.py`
  местами говорят «(editor+)», фактическая проверка — `MembershipRole.OWNER`
  **уже сейчас**, и UI уже явно говорит «Создавать контент может только
  владелец проекта» — это сознательное усиление SMM-025D, не моя правка;
  новая фича просто следует уже принятому правилу, не ослабляя и не
  переопределяя его).
- Чтение статуса/результата — viewer+, как везде.
- **Human approval не меняется ни на миллиметр**: генератор вызывает
  **только** `create_content_item` (всегда `status=draft`,
  `approved_revision_id=NULL`). Ни `approve_revision`, ни
  `materialize_approved_revision` воркер генерации никогда не вызывает —
  физически не импортирует эти функции. Approve по-прежнему требует
  аутентифицированного `User` (`_require_human`) и step-up пароль через
  `smm-auth` (`ApprovalGrant`) — вся эта машинерия SMM-025D не затрагивается.
  Агент может *написать* ревизию (как и сейчас — `created_by_user_id=NULL`
  для агентского источника, см. докстринг `RevisionInput` в
  `application/content.py:179-183`), но не может её одобрить — это уже
  гарантировано на уровне БД (`content_approvals.actor_user_id NOT NULL`),
  а не только кодом.
- `AI_SMM_API_MUTATIONS_ENABLED=false` (текущее состояние production)
  автоматически блокирует и создание generation job через API — т.е. до
  отдельного решения о включении mutations в проде эта фича физически
  недоступна через Control Center, только через CLI/офлайн — ровно так же,
  как сейчас недоступны ручное создание контента и approve.

## 7. Миграции, rollback, совместимость с LIVE worker

- Одна Alembic-миграция, `down_revision = "c4d81e2a9f63"` (текущий head),
  **только** `CREATE TYPE`, `CREATE TABLE`, `ALTER TABLE ... ADD COLUMN
  ... NULL`, индексы. Rollback — `DROP COLUMN`/`DROP TABLE`/`DROP TYPE` в
  обратном порядке; безопасен, т.к. колонка nullable и по умолчанию пуста
  для всех существующих строк.
- **LIVE worker (`ai_smm/worker.py`) не читает и не пишет** ни
  `content_generation_jobs`, ни `content_items`, ни `content_revisions` —
  он работает только с `publications`. Новая таблица и колонка для него
  невидимы и до, и после миграции: старый образ воркера продолжает
  работать как прежде и во время раскатки этой фичи, и после — его вообще
  не нужно пересобирать/перезапускать ради этой миграции (в отличие от
  SMM-025D, где миграция требовала одновременной замены образа воркера).
  Это даёт заметно более простое и низкорисковое окно внедрения, чем
  любая из прошлых миграций control-plane.
- Новая DB-роль `ai_smm_generation` (если выбран отдельный процесс) —
  отдельный grants-файл по образцу `deploy/control-center/db/ai_smm_cc.grants.sql`,
  отдельная миграция/скрипт, не блокирует основную схемную миграцию.

## 8. Критерии приёмки и этапы

**Этап 1 — backend, без UI.** Миграция применена на тестовой БД;
`content_generation_jobs` + `generation_job_id` на месте; `ai_smm.generation_worker`
(отдельный процесс) + `claim_due_generation_job` с тем же FOR UPDATE SKIP
LOCKED; API-роуты создания/чтения/отмены job'а за mutation-gate; CLI
`ai-smm generation enqueue --project <id>` для ручной проверки без UI.
*Приёмка:* job, поставленный через CLI/API, забирается воркером и создаёт
ровно N (`series_size`) `content_items` со статусом `draft`, корректным
provenance (`source=copywriter`, `editor_score`, `metadata` с вердиктом),
без единой публикации; принудительный kill процесса в середине обработки
→ после рестарта job переделан с нуля, **дублей content_items нет**; полный
текущий набор тестов (1298 офлайн + 2822 DB) по-прежнему зелёный без
единого изменения в `worker.py`/`publishing/`.

**Этап 2 — UI.** Кнопка «Generate», страница статуса job'а, вердикт Editor
виден на карточке черновика. *Приёмка:* полный путь владельца
план→3 текста→просмотр вердикта→правка при необходимости→approve со
step-up→materialize→schedule — целиком через UI, без CLI, на
mutations-enabled стенде, publish — только dry-run/fake.

**Этап 3 — эксплуатация.** Отдельный контейнер на VPS (не трогая LIVE
worker), ограничения (лимит job'ов/день/проект, грубая оценка стоимости
перед запуском), Langfuse-тег `generation_job_id` в трассах, токены/
стоимость job'а видны в UI.

## Проверка отдельного вопроса: можно ли обойтись текущей очередью без новой инфраструктуры

**Да.** Новый класс сущности («фоновая задача, которую нужно разобрать один
раз, без повторяющегося расписания») укладывается в уже существующий
паттерн «PostgreSQL как очередь» один в один: та же таблица-как-очередь,
тот же `FOR UPDATE SKIP LOCKED`, та же лизинговая модель claim/lease, тот
же `APScheduler.BackgroundScheduler` как периодический триггер, тот же
`session_scope`/`with_db_retry`. Новых классов инфраструктуры (брокер
сообщений, Redis, Celery, отдельная БД) не требуется — только одна новая
таблица в уже работающем PostgreSQL и один новый лёгкий процесс,
использующий уже существующий Python-пакет и уже существующие библиотеки
(`apscheduler`, `sqlalchemy`, `langgraph`, `langchain-openai`) без единой
новой зависимости в `pyproject.toml`.

## Рекомендация

Архитектура минимальна и совместима: схема аддитивна, LIVE worker не
затрагивается физически, human approval не ослаблен ни в одной точке,
идемпотентность проще, чем у publishing-очереди (нет внешнего побочного
эффекта до финального коммита). Рекомендую согласовать **этап 1** как
первый PR SMM-026A/2 (миграция + генерационный воркер + CLI + API, без
UI), с отдельным ревью на DB-роль `ai_smm_generation` и её grants перед
тем, как это попадёт на VPS. UI (этап 2) — отдельный, меньший PR поверх
готового backend'а.
