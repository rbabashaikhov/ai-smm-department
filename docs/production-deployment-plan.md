# Production Deployment Plan — AI SMM Department

Статус документа: **фазы 1–8 выполнены, развёрнуто в dry-run.**
Реальная публикация в Threads не производилась.

| | |
|---|---|
| Дата аудита | 2026-10-09 |
| Дата развёртывания | 2026-10-09 |
| Repo HEAD на момент аудита | `e2c9419` |
| Развёрнутая версия | `ai-smm:73460cd` (ветка `feat/production-deployment`) |
| Статус worker на VPS | `healthy`, `AI_SMM_DRY_RUN=true`, портов нет |
| Секреты | в документ не выносятся; хосты/пользователи — ссылкой на `.env` |

**Операционные процедуры:** [operations.md](operations.md).

Разделы 1–13 ниже — исходный аудит и план, сохранены как есть для истории
решений. Раздел 15 описывает, что фактически развёрнуто и чем это
отличается от плана.

Обозначения: **UNKNOWN** — данные не получены, предположение не подставлено.

---

## 1. Current state

### 1.1 Что фактически в репозитории

```
src/ai_smm/
  graph.py                 LangGraph: strategist → copywriter → editor →(revise|publish)→ publisher
  state.py                 SMMState (TypedDict, total=False)
  models.py                PublicationPlan / PublicationItem (pydantic)
  storage.py               StorageUploader: SFTP upload + публичная HTTPS-верификация
  agents/{strategist,copywriter,editor,publisher}.py
  knowledge/loader.py      загрузка knowledge/projects/<id> + editorial_policy.md
  publishing/threads.py    ThreadsPublisher: text / thread / image / carousel
scripts/                   18 CLI-скриптов: run_demo, publish_live, publish_saved, test_*
knowledge/                 база знаний, пилот ai-catalog-consultant + assets/*.jpg
data/publications/*.json   локальная очередь публикаций (gitignored)
docs/                      file-storage-migration.md, threads-media-hosting.md, screenshots/
```

### 1.2 Entry points

| Скрипт | Назначение | Live-публикация |
|---|---|---|
| `scripts/run_demo.py` | полный pipeline, `publish_live=False` | нет (dry run) |
| `scripts/publish_live.py` | полный pipeline, `publish_live=True` | **да**, без подтверждения |
| `scripts/publish_saved.py` | публикация из сохранённой JSON-очереди | да, только с `--live --confirm-reviewed` |
| `scripts/test_*.py`, `diagnose_threads_media.py` | ручные диагностические прогоны | частично да |

Запуск: `uv run python scripts/<name>.py`. Python 3.12, `uv_build`, src-layout.

### 1.3 Архитектура LangGraph

`StateGraph(SMMState)`, compile без checkpointer → **состояние графа не персистентно**.
Revision loop: `editor → increment_revision → copywriter`, ограничение `max_revisions=3`;
при исчерпании лимита управление всё равно уходит в `publisher`, но тот блокирует
публикацию, если `editor_approved` ложно.

LLM создаётся **на уровне модуля**: `llm = ChatOpenAI(model="gpt-4.1-mini", temperature=…,
timeout=30, max_retries=1)` в каждом из трёх агентов, `CallbackHandler()` — тоже на уровне
модуля. Следствие: переменные окружения обязаны быть установлены **до** импорта
`ai_smm.agents.*`, иначе импорт падает. Для контейнера это приемлемо (env приходит из
environment), но исключает позднюю конфигурацию и усложняет тесты.

### 1.4 Publisher и Threads API

`publisher_node` → `prepare_publications()` → при `publish_live=True`:
1. **media preflight** — все изображения загружаются в storage и верифицируются до того,
   как создан хотя бы один пост. При сбое storage — `status=blocked`, в Threads не ушло ничего.
2. последовательная публикация; при первой ошибке — `break`, итог `status=partial`.

`ThreadsPublisher` (`src/ai_smm/publishing/threads.py`, 691 строка):
* `publish_text` — один вызов с `auto_publish_text=true` (атомарно).
* `create_image_post` / `publish_carousel` — **двухфазно**: `POST /me/threads` (container)
  затем `POST /me/threads_publish` (creation_id). Таймаут `httpx` = 30 с, retry нет.
* Валидация изображений против документированных лимитов Threads (8 МБ, 320–1440 px,
  соотношение ≤10:1, JPEG/PNG).
* `ThreadsMediaFetchError` — специальная обработка `error_subcode 2207052`
  (`is_transient=false`): осознанный fail-fast без retry, с диагностикой URL.
* Жёстко зашитый `PILOT_MEDIA` и `PILOT_PROJECT_ID = "ai-catalog-consultant"` в publisher.

### 1.5 Хранение черновиков и результатов

Единственное persistent-хранилище — файл `data/publications/<project_id>.json`
(`schema_version: 1`), плюс `*.json.lock` для `fcntl`-блокировки.
Фактическое состояние пилотной очереди: пост 1 — `published` (`threads_post_id`,
`published_at`), посты 2–3 — `approved`, ждут публикации.

`scripts/publish_saved.py` уже реализует почти все нужные production-свойства:
* `fcntl.flock(LOCK_EX | LOCK_NB)` — защита от параллельных процессов;
* атомарная запись через `mkstemp` + `fsync` + `os.replace` + `fsync` каталога;
* маркер `status=publishing` + `attempt_started_at` записывается **до** обращения к Threads;
* любое исключение → `status=needs_review` + `last_error`, процесс останавливается;
* отказ публиковать, если уже есть `threads_post_id` или `published_at`;
* `--due` отбирает только `approved` с наступившим `scheduled_at` (tz-aware, приводится к UTC);
* `--live` требует явного `--confirm-reviewed`.

**Это готовая модель для таблицы в PostgreSQL** — её логику нужно перенести, а не переизобретать.

### 1.6 StorageUploader

SFTP через `paramiko` → `STORAGE_REMOTE_ROOT`, `mkdir -p`, `chmod 0644`, затем
HTTPS-верификация (код ответа, непустое тело, согласованность `Content-Type`).
Имя файла уникально: `<timestamp>-<uuid8>-<stem><suffix>` — исключает попадание в кэш
Meta/CDN старой версии. Защита от traversal (`..`) присутствует.

Проблема: в коде зашиты `DEFAULT_PUBLIC_BASE_URL = "https://files.apps.leadmeter.ru"`
и `DEFAULT_REMOTE_ROOT = "/srv/miniapps/file-storage"` — устаревшие инфраструктурные
значения, не соответствующие текущему хранилищу (`media.arcade-lab.info`,
`/srv/file-storage`). Если env-переменная не задана, код молча уйдёт на неправильный хост.

### 1.7 Langfuse

`@observe(...)` на узлах графа, `CallbackHandler()` в `config={"callbacks": [...]}` при
вызовах LLM, `propagate_attributes(...)` в `run_demo.py`, явный `get_client().flush()`
в конце CLI. Проект использует Langfuse Cloud (`cloud.langfuse.com`).

### 1.8 Тесты

**Автоматических тестов нет.** `pytest` и `pytest-asyncio` объявлены в dev-группе,
но каталога `tests/` не существует, `testpaths` не настроен, ни один файл в `scripts/`
не содержит `test_`-функций или `import pytest` — это ручные runner-скрипты.
Для CI-гейта перед деплоем тестовую базу придётся создавать с нуля.

### 1.9 Переменные окружения

Из `.env.example` / `.env` (13 ключей, значения не выводятся):

`OPENAI_API_KEY`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL`,
`THREADS_ACCESS_TOKEN`, `THREADS_API_BASE_URL`, `STORAGE_SSH_HOST`, `STORAGE_SSH_PORT`,
`STORAGE_SSH_USER`, `STORAGE_SSH_KEY_PATH`, `STORAGE_REMOTE_ROOT`,
`STORAGE_PUBLIC_BASE_URL`, `THREADS_STORAGE_PREFIX`.

`storage.py` дополнительно читает недокументированный `STORAGE_SSH_PASSWORD`
(в `.env.example` отсутствует).

### 1.10 Deployment-артефакты

`Dockerfile`, `compose.yaml`, `.dockerignore`, CI-конфигурация, systemd-юниты,
Makefile — **отсутствуют полностью**.

### 1.11 Что мешает unattended execution

| № | Блокер | Последствие |
|---|---|---|
| 1 | Нет планировщика и демона — только разовый CLI | публикация невозможна без человека |
| 2 | Очередь — файл на диске разработчика | нет общего состояния для worker, нет истории |
| 3 | Нет checkpointer в LangGraph | падение в середине графа = потеря работы и повторные траты на LLM |
| 4 | Логирование через `print()` (включая `print(image_info)`, тела ошибок Threads) | нет уровней, нет структуры, риск попадания чувствительных URL/ответов в stdout |
| 5 | `knowledge/loader.py`: `PROJECT_ROOT = Path(__file__).resolve().parents[3]` | в контейнере с установленным пакетом путь укажет в site-packages → `knowledge/` не найдётся |
| 6 | `publish_saved.py`: `ROOT = parents[1]`, пути изображений обязаны быть относительными корню репо | требует наличия всего дерева репозитория внутри контейнера |
| 7 | `SSH_KEY_PATH` — путь к файлу на машине разработчика | нужен безопасный монтаж ключа в контейнер |
| 8 | Таймаут на `threads_publish` → неизвестный результат, автоматизированной сверки нет | риск двойной публикации при наивном retry |
| 9 | Жёсткие константы `PILOT_PROJECT_ID` / `PILOT_MEDIA` в publisher | мультипроектность невозможна без правки кода |
| 10 | Нет graceful shutdown (SIGTERM) | `docker stop` может прервать публикацию в худший момент |

---

## 2. Infrastructure inventory (VPS, read-only audit)

Проведён read-only аудит целевого VPS. Детали чужих сервисов, топология
общей инфраструктуры и состояние защиты хоста намеренно не приводятся в
публичном репозитории. Ниже — только то, на что опираются решения
остальной части документа.

### 2.1 Ресурсы

Ubuntu 24.04 LTS, 2 vCPU, 3.8 ГиБ RAM (доступно ≈ 2.5 ГиБ), 25 ГБ диска
(занято около половины), swap 512 МБ. Часовой пояс хоста отличается от
UTC — существенно для планировщика.

### 2.2 Docker

Docker Engine 29.x, Compose v5.x, overlayfs, cgroup v2, драйвер логов
`json-file`. Глобального `/etc/docker/daemon.json` нет: ограничения
размера логов не заданы, и менять это глобально нельзя — поведение
изменилось бы для всех контейнеров на хосте.

### 2.3 Что уже работает на хосте

Хост не выделен под этот проект. На нём работают сервисы других проектов,
включая общий экземпляр PostgreSQL 16, reverse proxy с автоматическим TLS
и статическое файловое хранилище, обслуживающее `media.arcade-lab.info`.
Все они подключены к одной внешней Docker-сети.

Три следствия для нас:

1. Общий PostgreSQL можно переиспользовать, выделив собственную БД и
   непривилегированную роль, — на хосте уже применён именно такой паттерн
   изоляции для другого проекта.
2. Reverse proxy настроен с `exposedByDefault=false`: маршрутизируется
   только то, что явно помечено лейблами.
3. На хосте уже работает hardened Python-сервис другого проекта, чью
   конфигурацию следует воспроизвести, а не изобретать заново:
   отдельный compose-проект, подключённый к существующей сети как
   `external`; без публикации портов; `read_only` корневая ФС; `cap_drop:
   ALL`; `no-new-privileges`; лимиты памяти и PIDs; запуск под
   непривилегированным uid; образ запинен; секреты в env-файле с правами
   600 вне Git.

### 2.4 PostgreSQL

PostgreSQL 16, доступен только внутри хоста и общей Docker-сети, наружу
не публикуется. Инстанс общий с другими проектами; занято менее десятой
части лимита соединений. Сервер работает в UTC.

### 2.5 Безопасность

Детали защиты хоста в публичном репозитории не приводятся. Значимое для
архитектуры: этот проект **не публикует портов** и остаётся доступен
только внутри Docker-сети; непривилегированных пользователей на хосте
нет, весь доступ — под root.

### 2.6 Backup, cron, логи

До начала работ автоматических бэкапов БД, пользовательских cron-задач и
ротации логов контейнеров на хосте не было. Всё перечисленное добавлено
этим проектом только для себя.

### 2.7 Доступность внешних API

С VPS доступны OpenAI API, Threads Graph API и Langfuse Cloud; ограничений
на исходящий трафик не обнаружено.

### 2.8 UNKNOWN

* Параметры резервного копирования на уровне провайдера VPS.
* Политика ротации `THREADS_ACCESS_TOKEN`.
* Остаток дисковой квоты у провайдера при росте медиа-хранилища.

## 3. Target architecture

```
                         Internet
                            │
                    80/443  │  (существующий Traefik, не меняем)
                            ▼
              ┌──────────── <shared-network> (external bridge) ────────────┐
              │                                                               │
   ┌──────────┴─────────┐   ┌──────────────────┐   ┌─────────────────────┐    │
   │ traefik  (чужой)   │   │ file-storage     │   │ postgres 16.15      │    │
   │                    │   │ caddy, /srv RO   │   │ 127.0.0.1:5432      │    │
   └────────────────────┘   └──────────────────┘   └──────────┬──────────┘    │
                                     ▲                        │ db: ai_smm    │
                                     │ SFTP (out-of-band)     │ role: ai_smm  │
   ┌─────────────────────────────────┼────────────────────────┴──────────┐    │
   │  compose project: ai-smm   (НОВОЕ, портов не публикует)             │    │
   │                                                                     │    │
   │   ┌───────────────────────────┐      ┌───────────────────────────┐   │    │
   │   │ ai-smm-worker             │      │ ai-smm-scheduler          │   │    │
   │   │ забирает due-публикации   │◄────►│ APScheduler + PG jobstore  │   │    │
   │   │ FOR UPDATE SKIP LOCKED    │      │ tz-aware, misfire_grace    │   │    │
   │   │ → ThreadsPublisher        │      │ (в релизе 1 — один процесс)│   │    │
   │   └─────────────┬─────────────┘      └───────────────────────────┘   │    │
   │                 │                                                     │    │
   │   ┌─────────────┴─────────────┐                                       │    │
   │   │ ai-smm-cli (profile)      │  LangGraph pipeline по требованию      │    │
   │   │ docker compose run --rm   │  (strategist→copywriter→editor→pub)    │    │
   │   └───────────────────────────┘                                       │    │
   └─────────────────────────────────────────────────────────────────────────┘  │
              │                                                               │
              └───────────────────────────────────────────────────────────────┘
                            │                │                │
                            ▼                ▼                ▼
                     api.openai.com   graph.threads.net   cloud.langfuse.com
```

Будущий слой (не в релизе 1): `ai-smm-api` (FastAPI) + `ai-smm-ui` (React/Vite),
публикуемые через существующий Traefik по отдельному hostname с auth.

### Минимальный production scope релиза 1

**Входит:**
1. Dockerfile + compose-проект `ai-smm`, по образцу `<another-service>`.
2. Схема PostgreSQL: проекты, публикации, попытки публикации, журнал аудита.
3. Миграция текущего `data/publications/*.json` в БД (посты 2–3 в статусе `approved`,
   пост 1 — как `published` с существующим `threads_post_id`).
4. Scheduler + worker: автоматическая публикация due-постов с идемпотентностью.
5. Структурное логирование с маскированием секретов, graceful shutdown.
6. Backup/restore для БД `ai_smm`, ротация логов контейнеров.
7. Сохранение работоспособности существующих CLI-сценариев.

**Не входит:** FastAPI, React, новые агенты, мультиканальность, автоматический
retry неоднозначных публикаций, публичный HTTP endpoint.

---

## 4. Deployment topology

| Компонент | Размещение | Порты | Лимиты |
|---|---|---|---|
| `ai-smm-scheduler` | контейнер, сеть `<shared-network>` | нет | 384 МБ / 128 pids |
| `ai-smm-worker` | контейнер, та же сеть (релиз 1: совмещён со scheduler) | нет | см. выше |
| `ai-smm-cli` | `docker compose run --rm`, compose profile `cli` | нет | 512 МБ |
| БД `ai_smm` | существующий `<postgres-container>` | уже на `127.0.0.1:5432` | — |
| Код/compose | `/root/ai-smm/` (mode 700), env-файл mode 600 | — | — |
| Knowledge + assets | `/srv/ai-smm/knowledge` → монтаж **read-only** | — | — |
| Бэкапы | `/root/backups/ai-smm/` | — | — |

Бюджет памяти: доступно 2.5 ГиБ, запрашиваем ≤ 512 МБ → запас сохраняется.
Публичных портов не добавляется; поверхность атаки хоста не меняется.

---

## 5. Database decision

### Варианты

| Критерий | A. Существующий Postgres, своя БД + роль | B. Отдельный контейнер Postgres |
|---|---|---|
| Изоляция данных | БД + непривилегированная роль + `REVOKE CONNECT` для прочих — достаточно | полная |
| Blast radius | общий инстанс: сбой/OOM/рестарт затрагивает n8n и samsung | независим |
| RAM | **0 дополнительно** | +150–250 МБ из 2.5 ГиБ доступных |
| Disk | +десятки МБ | +образ и volume |
| Backup | нужен отдельный `pg_dump` для `ai_smm` (всё равно нужен) | свой цикл бэкапа |
| Версия/апгрейды | привязка к 16.15, апгрейд решает владелец n8n-стека | независимый контроль |
| Сопровождение | минимум нового | ещё один сервис, ещё один пароль, ещё один бэкап |
| Прецедент на хосте | **есть** (`<other-db-1>` + роли `<other-roles>`) | нет |

### Решение: **вариант A** — существующий PostgreSQL 16.15, отдельная БД и роль

Обоснование: объём данных ничтожен (очередь публикаций — десятки строк в год),
занято лишь 9 из 100 соединений, а на хосте уже отработан именно этот паттерн
изоляции. Второй инстанс Postgres забрал бы 6–10% доступной RAM и удвоил
операционную нагрузку без выигрыша, релевантного этой рабочей нагрузке.

Главный минус — общий blast radius: рестарт или OOM инстанса останавливает и наш
worker. Компенсируется в приложении: пул с `pool_pre_ping`, повторное подключение
с backoff, и обязательное требование — **worker должен корректно переживать
временную недоступность БД** (см. тест T9). При этом правило остаётся:
инстанс Postgres мы не обновляем, не перезапускаем и не переконфигурируем.

Условие пересмотра: появление в проекте тяжёлых нагрузок (pgvector-поиск по
большому корпусу, аналитика) либо требование независимого окна обслуживания.

### Подготовка БД (единственная запись в чужой инстанс — требует согласования)

```sql
CREATE ROLE ai_smm LOGIN PASSWORD '<сгенерированный>';   -- NOSUPERUSER, NOCREATEDB
CREATE DATABASE ai_smm OWNER ai_smm ENCODING 'UTF8';
REVOKE ALL ON DATABASE ai_smm FROM PUBLIC;
\c ai_smm
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT ALL ON SCHEMA public TO ai_smm;
```

Эти команды **создают** новые объекты и не изменяют существующие базы, роли и
настройки сервера. Выполняются только после явного подтверждения пользователя.

### Схема (релиз 1)

```sql
-- время хранится исключительно в timestamptz; сервер в UTC
CREATE TABLE projects (
    id              text PRIMARY KEY,              -- 'ai-catalog-consultant'
    display_name    text NOT NULL,
    knowledge_path  text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TYPE publication_status AS ENUM (
    'draft', 'approved', 'scheduled',
    'claimed', 'publishing', 'published',
    'failed', 'needs_review', 'cancelled'
);

CREATE TABLE publications (
    id                  bigserial PRIMARY KEY,
    project_id          text NOT NULL REFERENCES projects(id),
    platform            text NOT NULL DEFAULT 'threads',
    ordinal             integer NOT NULL,
    title               text NOT NULL,
    format              text NOT NULL,             -- text | image | carousel | thread
    body                text NOT NULL,
    images              jsonb NOT NULL DEFAULT '[]',
    status              publication_status NOT NULL DEFAULT 'draft',
    scheduled_at        timestamptz,
    -- идемпотентность: один и тот же логический пост нельзя создать дважды
    idempotency_key     text NOT NULL UNIQUE,
    threads_post_id     text UNIQUE,               -- NULL до публикации
    published_at        timestamptz,
    claimed_by          text,                      -- идентификатор worker
    claimed_at          timestamptz,
    lease_expires_at    timestamptz,
    attempt_count       integer NOT NULL DEFAULT 0,
    next_attempt_at     timestamptz,
    last_error          text,
    editor_score        numeric(3,1),
    human_reviewed      boolean NOT NULL DEFAULT false,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, platform, ordinal)
);

CREATE INDEX publications_due_idx
    ON publications (scheduled_at)
    WHERE status = 'scheduled';

-- журнал каждой попытки: источник истины при разборе неоднозначных исходов
CREATE TABLE publication_attempts (
    id                bigserial PRIMARY KEY,
    publication_id    bigint NOT NULL REFERENCES publications(id),
    attempt_number    integer NOT NULL,
    worker_id         text NOT NULL,
    phase             text NOT NULL,       -- preflight|container|publish|verify
    started_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz,
    outcome           text,                -- success|error|timeout|unknown
    threads_creation_id text,              -- известен до publish → ключ для сверки
    threads_post_id   text,
    http_status       integer,
    error_type        text,
    error_message     text,
    UNIQUE (publication_id, attempt_number)
);

CREATE TABLE audit_log (
    id          bigserial PRIMARY KEY,
    at          timestamptz NOT NULL DEFAULT now(),
    actor       text NOT NULL,             -- worker:<id> | cli:<user> | scheduler
    action      text NOT NULL,
    subject     text,
    details     jsonb NOT NULL DEFAULT '{}'
);
```

Миграции — Alembic (`alembic/versions/`), запуск отдельной командой
`docker compose run --rm ai-smm-cli alembic upgrade head`; **не** автоматически
при старте контейнера, чтобы два экземпляра не мигрировали одновременно.

---

## 6. Scheduler design

### Выбор

**APScheduler 3.x** с `SQLAlchemyJobStore` в БД `ai_smm` — для расписания запусков
(«в 10:00 проверить очередь»). Сама очередь публикаций живёт в таблице
`publications`, а не в jobstore. Это принципиально: jobstore APScheduler не даёт
ни истории попыток, ни атомарного резервирования, ни аудита. APScheduler отвечает
только за «когда проснуться», решение «что публиковать» принимает SQL-запрос.

Альтернативы отклонены: Celery/RQ — требуют брокера и несоразмерны одной задаче
в сутки; системный cron — не даёт state и затрудняет наблюдаемость внутри контейнера.

### Время и timezone

* Сервер Postgres в **UTC**, хост в **Europe/Vilnius (+03:00)** — расхождение есть,
  поэтому все столбцы — `timestamptz`, а контейнеры запускаются с `TZ=UTC`.
* Планирование в пользовательской зоне: `scheduled_at` хранится как `timestamptz`,
  вводится с явным offset (как уже требует `publish_saved.py`:
  «`scheduled_at must contain a timezone offset`»).
* `APP_DISPLAY_TZ` (например `Europe/Vilnius`) используется только для отображения.
* APScheduler инициализируется с `timezone=utc`.
* Конкретная зона публикаций для расписания — **UNKNOWN**, см. открытый вопрос Q3.

### Перезапуск приложения и пропущенные задания

* `SQLAlchemyJobStore` → расписание переживает рестарт.
* `misfire_grace_time=3600`, `coalesce=True`: после простоя одно наверстывающее
  срабатывание вместо лавины.
* Независимо от APScheduler, при каждом старте worker выполняет **reconcile-проход**:
  `scheduled`-посты с истёкшим `scheduled_at` публикуются с опозданием;
  посты, застрявшие в `claimed` с просроченным `lease_expires_at`, освобождаются;
  посты в `publishing` **не** освобождаются автоматически — переводятся в
  `needs_review` (см. ниже).

### Атомарное резервирование

```sql
WITH due AS (
    SELECT id FROM publications
     WHERE status = 'scheduled'
       AND scheduled_at <= now()
       AND (next_attempt_at IS NULL OR next_attempt_at <= now())
     ORDER BY scheduled_at
     FOR UPDATE SKIP LOCKED
     LIMIT 1
)
UPDATE publications p
   SET status = 'claimed',
       claimed_by = :worker_id,
       claimed_at = now(),
       lease_expires_at = now() + interval '15 minutes',
       updated_at = now()
  FROM due
 WHERE p.id = due.id
RETURNING p.*;
```

`FOR UPDATE SKIP LOCKED` + аренда (lease) дают корректность при любом числе
worker-процессов. Дополнительно, пока worker один, `pg_try_advisory_lock`
на уровне приложения гарантирует единственность активного publisher-цикла.
Транзакция резервирования фиксируется **до** первого сетевого вызова.

### Идемпотентность

Три независимых барьера:
1. `idempotency_key UNIQUE` — один логический пост нельзя завести дважды.
2. `threads_post_id UNIQUE` + отказ публиковать запись, у которой он уже заполнен
   (перенос существующей проверки из `publish_saved.py`).
3. Запись `publication_attempts` с `phase` и `threads_creation_id` **до** вызова
   `/me/threads_publish` — повторный запуск видит незавершённую попытку и
   останавливается вместо повторной отправки.

### Retry и backoff

| Класс ошибки | Поведение |
|---|---|
| Сбой media preflight (storage/SFTP/верификация URL) | безопасно, в Threads ничего не ушло → retry с backoff `2^n` мин, cap 60 мин, до 5 попыток |
| HTTP 5xx / сетевой сбой **до** получения ответа на container-фазу | retry с backoff (пост не создан) |
| HTTP 4xx (валидация, невалидный токен) | без retry → `failed`, требует вмешательства |
| `ThreadsMediaFetchError` (subcode 2207052, `is_transient=false`) | **без retry** → `needs_review`; поведение уже реализовано в коде |
| Rate limit (429 / Meta-код лимита) | retry после `Retry-After`, иначе backoff ≥ 15 мин |
| **Таймаут или обрыв на фазе `threads_publish`** | **retry запрещён** → `needs_review` |

### Rate limits Threads API

Публикации — не более 1 поста за цикл worker, минимальный интервал между
публикациями одного аккаунта `MIN_PUBLISH_INTERVAL` (по умолчанию 5 минут),
реализуется как проверка `max(published_at)` перед резервированием.
Документированная квота Threads — 250 публикаций на пользователя за 24 часа;
при планируемом объёме (единицы постов в сутки) это не ограничение, но счётчик
за последние 24 часа ведётся из таблицы `publications` и блокирует цикл при
приближении к квоте. Точные лимиты для карусельных контейнеров — **UNKNOWN**,
проверить перед увеличением объёма.

### Неопределённый результат: Threads принял, клиент получил таймаут

Это главный сценарий риска. Протокол:

1. До `POST /me/threads_publish` в `publication_attempts` фиксируется строка
   `phase='publish'`, `outcome=NULL`, с известным `threads_creation_id`.
2. Таймаут/обрыв → `outcome='unknown'`, публикация переходит в
   `status='needs_review'`, пишется `audit_log`. **Никакого автоматического повтора.**
3. Worker выполняет **одну** read-only попытку сверки: `GET /me/threads` со
   `since`-фильтром и сопоставление по `creation_id`/тексту. Результат только
   записывается в `publication_attempts`, автоматического решения не принимает.
4. Если сверка однозначно нашла пост — оператор командой
   `ai-smm reconcile --publication <id> --threads-post-id <id>` закрывает запись
   как `published`. Если однозначно не нашла — `ai-smm reconcile --publication <id>
   --retry` возвращает её в `scheduled`.
5. Пост в статусе `needs_review` **никогда** не берётся планировщиком.
   Единственный выход — ручное решение.

То же правило применяется к записям, найденным в состоянии `publishing` после
аварийного завершения worker: состояние внешней системы неизвестно → `needs_review`.

---

## 7. Docker design

### Dockerfile (план)

Multi-stage, `python:3.12-slim-bookworm` **с пином по digest** (не `latest`),
`uv` для установки зависимостей из `uv.lock` (`--frozen`, `--no-dev`).

```
Stage builder : uv sync --frozen --no-dev → /app/.venv
Stage runtime : копируется .venv + src + alembic
                ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=UTC
                ENV AI_SMM_KNOWLEDGE_ROOT=/srv/ai-smm/knowledge   # решает блокер №5
                USER 10001:10001
                ENTRYPOINT ["/app/.venv/bin/python","-m","ai_smm.worker"]
```

Требуемая правка кода: `knowledge/loader.py` должен читать корень из
`AI_SMM_KNOWLEDGE_ROOT` с текущим `parents[3]` как fallback — иначе путь внутри
контейнера указывает в site-packages.

`.dockerignore`: `.env*`, `.git`, `data/`, `backups/`, `__pycache__/`, `.venv`,
`.ruff_cache`, `docs/screenshots/`. Секреты в образ не попадают ни на одном слое.

### compose.yaml (план)

```yaml
name: ai-smm                      # отдельный проект: не трогает <shared-stack>

services:
  worker:
    image: ai-smm:${AI_SMM_VERSION}      # пин по git sha, никогда latest
    container_name: ai-smm-worker
    restart: unless-stopped
    env_file: [ai-smm.env]               # mode 600 на VPS, вне git
    environment:
      TZ: UTC
    networks: [n8n]
    volumes:
      - /srv/ai-smm/knowledge:/srv/ai-smm/knowledge:ro
      - /srv/ai-smm/ssh/id_storage:/run/secrets/storage_key:ro   # mode 600, root-owned
    read_only: true
    tmpfs: [/tmp]
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    mem_limit: 384m
    pids_limit: 128
    stop_grace_period: 90s               # publishing-цикл успевает закрыться
    healthcheck:
      test: ["CMD", "/app/.venv/bin/python", "-m", "ai_smm.healthcheck"]
      interval: 60s
      timeout: 10s
      retries: 3
      start_period: 30s
    logging:
      driver: json-file
      options: { max-size: "10m", max-file: "5" }
    labels: { traefik.enable: "false" }

  cli:
    image: ai-smm:${AI_SMM_VERSION}
    profiles: [cli]                      # запускается только через `compose run`
    env_file: [ai-smm.env]
    networks: [n8n]
    volumes:
      - /srv/ai-smm/knowledge:/srv/ai-smm/knowledge:ro
      - /srv/ai-smm/ssh/id_storage:/run/secrets/storage_key:ro
    entrypoint: ["/app/.venv/bin/python"]
    mem_limit: 512m

networks:
  n8n:
    name: <shared-network>
    external: true                       # подключаемся, не создаём
```

Ключевые свойства: **`ports:` отсутствует** — наружу ничего не открывается,
к Postgres обращаемся по DNS-имени `<postgres-container>:5432` внутри сети.
`external: true` гарантирует, что `docker compose up/down` нашего проекта не
затронет чужие сервисы и сеть.

`healthcheck` проверяет: процесс жив, соединение с БД отвечает, последний цикл
планировщика завершился не позже `2 × интервала`. Публичный HTTP endpoint для
этого не нужен.

### `.env.example` (дополнить)

К существующим 13 ключам добавить:

```
# Database (существующий инстанс Postgres, доступ только внутри docker-сети)
AI_SMM_DATABASE_URL=postgresql+psycopg://ai_smm:<password>@<postgres-container>:5432/ai_smm

# Runtime
AI_SMM_KNOWLEDGE_ROOT=/srv/ai-smm/knowledge
AI_SMM_WORKER_ID=worker-1
AI_SMM_SCHEDULE_CRON=*/5 * * * *
AI_SMM_DISPLAY_TZ=Europe/Vilnius
AI_SMM_MIN_PUBLISH_INTERVAL_SECONDS=300
AI_SMM_MAX_ATTEMPTS=5
AI_SMM_LEASE_SECONDS=900
AI_SMM_DRY_RUN=true            # релиз 1 стартует в dry-run
LOG_LEVEL=INFO
LOG_FORMAT=json

# Также задокументировать уже используемый, но отсутствующий в примере ключ:
STORAGE_SSH_PASSWORD=
```

Плюс исправить `storage.py`: убрать устаревшие константы
`files.apps.leadmeter.ru` / `/srv/miniapps/file-storage` — обязательные параметры
должны требоваться явно, а не молча подставляться.

### Logging и ротация

* Приложение: structured JSON в stdout, уровни, **обязательный фильтр маскирования**
  для `OPENAI_API_KEY`, `THREADS_ACCESS_TOKEN`, `LANGFUSE_SECRET_KEY`, пароля БД
  и заголовка `Authorization`. Все `print()` в `threads.py`, `storage.py`,
  `publisher.py` заменяются на logger (сейчас печатаются тела ответов Threads).
* Контейнер: `max-size: 10m`, `max-file: 5` — ограничение на уровне нашего сервиса.
  Глобальный `/etc/docker/daemon.json` **не создаём**: это изменило бы поведение
  чужих контейнеров и потребовало бы перезапуска демона.

### Backup / restore

```bash
# backup (cron 03:30 EEST, пишет в /root/backups/ai-smm/)
docker exec <postgres-container> pg_dump -U ai_smm -d ai_smm -Fc \
  > /root/backups/ai-smm/ai_smm-$(date -u +%Y%m%dT%H%M%SZ).dump
# retention 14 дней; knowledge/ и compose — отдельным tar

# restore в проверочную БД, никогда поверх рабочей без явного решения
docker exec -i <postgres-container> pg_restore -U ai_smm -d ai_smm_restore_test < <dump>
```

`pg_dump` только читает и не блокирует запись; на чужие базы не влияет.

### Rollback

Образы версионируются по git sha, предыдущий тег сохраняется на хосте.
Откат: `AI_SMM_VERSION=<prev>` в `.env` → `docker compose up -d worker`.
Миграции пишутся с рабочим `downgrade()`; при несовместимости схемы откат
выполняется как `downgrade` + смена образа. Полный аварийный останов —
`docker compose -p ai-smm stop worker` (затрагивает только наш проект) и возврат
к ручному `scripts/publish_saved.py`.

---

## 8. Security considerations

| Требование | Реализация |
|---|---|
| Секреты вне Git | `.gitignore` уже покрывает `.env` и `.env.*` с исключением `.env.example` — проверено |
| Секреты вне образа | `.dockerignore` исключает `.env*`; передача только через `env_file` в runtime |
| Секреты вне логов | обязательный redaction-фильтр; удаление `print()` тел ответов API |
| SSH-ключ storage | монтируется read-only из `/srv/ai-smm/ssh/` (root-owned, 600), в образ не копируется |
| Публичные порты | **не добавляются**; публичный HTTP endpoint в релизе 1 **не нужен** |
| Сеть | только исходящие вызовы к OpenAI / Threads / Langfuse + Postgres внутри docker-сети |
| Привилегии | `USER 10001`, `read_only: true`, `cap_drop: ALL`, `no-new-privileges` |
| Docker socket | не монтируется |
| Права в БД | непривилегированная роль `ai_smm`, только своя база, `REVOKE ALL ... FROM PUBLIC` |
| Traefik | `traefik.enable=false` как защита в глубину |
| Защита от случайной публикации | `AI_SMM_DRY_RUN=true` по умолчанию; live требует явного переключения |
| `paramiko.AutoAddPolicy` | **уязвимость**: принимает любой host key. Заменить на проверку известного ключа после первичного pinning |
| Ротация `THREADS_ACCESS_TOKEN` | процедура не определена → открытый вопрос Q5 |

Фактическая проверка на утечки: в текущем репозитории `.env` не отслеживается
Git (покрыт `.gitignore`), в коде найдены только инфраструктурные хосты
(`files.apps.leadmeter.ru`), не секреты.

---

## 9. Deployment phases

Этапы независимы и упорядочены по возрастанию риска. Фазы 1–2 не касаются VPS.

### Фаза 1 — Production packaging (локально)

* **Компоненты:** `Dockerfile`, `.dockerignore`, `compose.yaml`, `.env.example`,
  `src/ai_smm/config.py` (единая загрузка настроек), `logging_setup.py` с
  redaction, правка `knowledge/loader.py` (`AI_SMM_KNOWLEDGE_ROOT`),
  очистка констант в `storage.py`, замена `print()` на logger.
* **Риски:** правка `loader.py` или `storage.py` ломает существующие CLI-сценарии.
* **Зависимости:** нет.
* **Acceptance:** `scripts/run_demo.py` и `scripts/publish_saved.py --post 2`
  (dry run) работают как до изменений; `ruff check` чисто; секретов в выводе нет.
* **Rollback:** `git revert`; VPS не затронут.

### Фаза 2 — Docker build и локальные тесты

* **Компоненты:** сборка образа, `tests/` (первые автотесты), запуск на локальном
  Postgres в Docker.
* **Риски:** расхождение локального и серверного Postgres (16.15) — фиксируем
  тот же minor в локальной проверке.
* **Зависимости:** фаза 1.
* **Acceptance:** тесты T1, T2, T3, T6, T10, T11 (раздел 10) проходят локально.
* **Rollback:** удалить локальный образ.

### Фаза 3 — Подготовка VPS (первая запись на сервер)

* **Компоненты:** создание `/root/ai-smm/`, `/srv/ai-smm/{knowledge,ssh}`,
  `/root/backups/ai-smm/`; перенос образа (`docker save | ssh | docker load`);
  размещение `ai-smm.env` (mode 600).
* **Риски:** низкие — только новые каталоги и файлы. Образ добавит ~300–400 МБ
  к 11 ГБ занятого диска из 25 ГБ.
* **Зависимости:** фаза 2; **подтверждение пользователя (Q1, Q2)**.
* **Acceptance:** `docker image ls` содержит `ai-smm:<sha>`;
  `docker ps` по-прежнему показывает те же 6 чужих контейнеров в том же состоянии;
  `free -h` и `df -h` без существенных изменений.
* **Rollback:** `docker image rm ai-smm:<sha>`, удаление созданных каталогов.
  Чужие объекты не затрагиваются.

### Фаза 4 — PostgreSQL и миграции

* **Компоненты:** `CREATE ROLE ai_smm` + `CREATE DATABASE ai_smm`, `alembic upgrade head`,
  импорт `data/publications/ai-catalog-consultant.json` (пост 1 → `published`
  с существующим `threads_post_id`, посты 2–3 → `approved`).
* **Риски:** **запись в общий инстанс Postgres.** Только `CREATE`, никакого
  `ALTER`/`DROP` существующих объектов. Перед выполнением — снимок
  `pg_dumpall --roles-only` в `/root/backups/ai-smm/` для фиксации исходного состояния.
* **Зависимости:** фаза 3; **явное подтверждение пользователя (Q2)**.
* **Acceptance:** `\l` содержит `ai_smm`; существующие базы
  (`<other-db-2>`, `<other-db-1>`) и роли не изменены; `<another-service>`
  остаётся `healthy`; импортированный пост 1 имеет `status=published` и
  **не** может быть выбран планировщиком.
* **Rollback:** `DROP DATABASE ai_smm; DROP ROLE ai_smm;` — чужих объектов не касается.

### Фаза 5 — Scheduler и очередь, запуск в dry-run

* **Компоненты:** `docker compose -p ai-smm up -d worker` с `AI_SMM_DRY_RUN=true`.
* **Риски:** непредвиденное обращение к Threads; снимается тем, что dry-run
  физически не вызывает publish-методы (проверяется тестом T6).
* **Зависимости:** фаза 4.
* **Acceptance:** контейнер `healthy`; в логах видны циклы планировщика;
  `ss -tlnp` на хосте показывает тот же набор портов, что при аудите;
  потребление памяти worker < 384 МБ.
* **Rollback:** `docker compose -p ai-smm down` (только наш проект).

### Фаза 6 — Dry-run запланированной публикации

* **Компоненты:** тестовая запись с `scheduled_at = now() + 5 минут`.
* **Риски:** ошибка в логике due-отбора.
* **Зависимости:** фаза 5.
* **Acceptance:** запись зарезервирована ровно один раз, в
  `publication_attempts` одна строка с `phase='preflight'`, `outcome='success'`,
  в Threads **ничего не отправлено** (проверяется по аккаунту), статус возвращён
  в `scheduled` или помечен как dry-run-исполненный.
* **Rollback:** удалить тестовую запись.

### Фаза 7 — Controlled real publish

* **Компоненты:** `AI_SMM_DRY_RUN=false`, публикация **одного** реального поста
  (кандидат — пост 2 пилотной очереди, формат `image`, уже одобрен).
* **Риски:** **необратимо** — публичная публикация. Неверный текст/изображение
  уходит в публичный аккаунт.
* **Зависимости:** фазы 5–6; **подтверждение пользователя по конкретному тексту
  и изображению (Q4)**.
* **Acceptance:** в Threads ровно один новый пост; в БД `status=published`,
  заполнен `threads_post_id`, `attempt_count=1`; повторный цикл планировщика
  не создаёт второй пост (T6).
* **Rollback:** автоматического нет — пост удаляется вручную в Threads;
  `AI_SMM_DRY_RUN=true` немедленно останавливает дальнейшие публикации.

### Фаза 8 — Monitoring, recovery, backup

* **Компоненты:** cron-бэкап `pg_dump` для `ai_smm` + retention, проверка
  восстановления в отдельную БД, алерт на записи в `needs_review`,
  `ai-smm reconcile` CLI.
* **Риски:** cron на хосте, где пользовательского crontab ещё не было —
  добавляем только свою запись.
* **Зависимости:** фаза 7.
* **Acceptance:** дамп создаётся и восстанавливается в `ai_smm_restore_test`;
  тесты T4, T7, T8, T9 проходят на сервере; логи контейнера ротируются по 10 МБ.
* **Rollback:** удалить cron-запись.

### Фаза 9 — Подготовка к FastAPI/React Control Center

* **Компоненты:** только подготовительные — слой репозиториев/сервисов, чтобы
  будущий API переиспользовал ту же логику очереди; описание схемы и контракта.
  Код API и UI **не пишется**.
* **Риски:** преждевременное усложнение.
* **Зависимости:** фаза 8 стабильно работает ≥ 1 недели.
* **Acceptance:** документированный контракт; worker продолжает работать
  без изменений в поведении.
* **Rollback:** изменения чисто внутренние, `git revert`.

### Совместимость

`scripts/publish_saved.py` сохраняется как рабочий аварийный путь публикации
(с JSON-очередью) на протяжении всех фаз. Предложение: после фазы 4 он читает
БД, но прежний JSON-режим остаётся за флагом `--queue`. `ThreadsPublisher`
и `StorageUploader` не меняют публичный интерфейс; меняется только логирование
и устранение устаревших дефолтов.

---

## 10. Acceptance tests

| № | Проверка | Метод | Критерий |
|---|---|---|---|
| T1 | Docker build успешен | `docker build` на чистом клоне | образ собирается, в слоях нет `.env` (`docker history`) |
| T2 | Запуск без публичных портов | `docker inspect` + `ss -tlnp` на хосте | у наших контейнеров нет `PortBindings`; набор портов хоста идентичен аудиту |
| T3 | Сохранность данных после restart | создать запись → `compose restart worker` | запись и расписание APScheduler на месте, статусы не изменились |
| T4 | Корректность миграций | `alembic upgrade head` на пустой БД, затем `downgrade base` | обе операции без ошибок; повторный `upgrade` идемпотентен |
| T5 | Выполнение одной scheduled task | запись с `scheduled_at = now()+2min`, dry-run | ровно один `claim`, ровно одна строка в `publication_attempts` |
| T6 | Отсутствие двойной публикации | два worker-процесса одновременно на одной due-записи | один публикует, второй получает 0 строк (`SKIP LOCKED`); `threads_post_id` один |
| T7 | Аварийное завершение worker | `docker kill -s SIGKILL` в фазе `publishing` | после старта запись в `needs_review`, автоповтора нет, в `audit_log` событие |
| T8 | Таймаут Threads API | подмена базового URL на эндпоинт, зависающий дольше таймаута | `outcome='unknown'`, статус `needs_review`, **ни одной** повторной отправки |
| T10 | Безопасное завершение | `docker stop` во время публикации | SIGTERM обработан, текущая попытка доведена до записи результата или помечена, выход в пределах `stop_grace_period` |
| T11 | Отсутствие секретов в логах | `docker logs` + прогон CLI, `grep` по известным префиксам токенов | ни одного совпадения; значения замаскированы |
| T12 | Отсутствие влияния на другие контейнеры | снимок `docker ps`, `docker inspect` (Id, StartedAt), `free -h`, `df -h` до и после каждой фазы | Id и StartedAt чужих контейнеров неизменны; `<another-service>` `healthy`; БД `<other-db-2>`/`<other-db-1>` не изменены |

T12 выполняется как контрольная проверка **после каждой** фазы, затрагивающей VPS.

---

## 11. Rollback plan

| Уровень | Действие | Влияние на чужие сервисы |
|---|---|---|
| Приложение | `AI_SMM_DRY_RUN=true` + `compose up -d worker` | нет |
| Версия образа | `AI_SMM_VERSION=<prev>` + `compose up -d worker` | нет |
| Останов сервиса | `docker compose -p ai-smm stop worker` | нет (только наш проект) |
| Полное удаление сервиса | `docker compose -p ai-smm down` (сеть `external` не удаляется) | нет |
| Схема БД | `alembic downgrade <rev>` | нет |
| Удаление БД | `DROP DATABASE ai_smm; DROP ROLE ai_smm;` | нет |
| Восстановление данных | `pg_restore` из `/root/backups/ai-smm/` | нет |
| Аварийный режим работы | вернуться к `scripts/publish_saved.py` с JSON-очередью | нет |
| Публикация в Threads | **необратимо автоматически** — удаление только вручную | — |

Запрещено на всех уровнях откатa: `docker compose down` для чужих проектов,
`docker system prune`, удаление чужих volume/network, изменение конфигурации
Traefik, firewall, DNS, обновление Postgres.

---

## 12. Risk register

| № | Риск | Серьёзность | Митигация |
|---|---|---|---|
| R1 | Двойная публикация при неоднозначном ответе Threads | **высокая** | запрет авто-retry на фазе `publish`, `needs_review`, `threads_post_id UNIQUE`, ручная сверка |
| R2 | Публикация не того текста/изображения в публичный аккаунт | **высокая** | `AI_SMM_DRY_RUN=true` по умолчанию, `human_reviewed`, контролируемая фаза 7 с подтверждением |
| R3 | Общий Postgres: OOM/рестарт инстанса останавливает worker | средняя | `pool_pre_ping`, reconnect с backoff, T9; worker не держит долгих транзакций |
| R4 | Запись в чужой инстанс Postgres на фазе 4 | средняя | только `CREATE`, снимок `--roles-only` до изменений, T12, подтверждение Q2 |
| R5 | Ограниченная RAM (2.5 ГиБ available, n8n уже занимает 1 ГиБ) | средняя | `mem_limit: 384m`, `pids_limit`, мониторинг `free -h` после каждой фазы |
| R6 | Утечка секретов через `print()` тел ответов Threads | средняя | замена на logger с redaction, T11 |
| R7 | `paramiko.AutoAddPolicy` принимает любой host key (MITM на SFTP) | средняя | pinning известного host key, отказ от `AutoAddPolicy` |
| R8 | Неограниченный рост json-логов (нет `daemon.json`, нет logrotate) | средняя | `max-size`/`max-file` в нашем compose; глобальную конфигурацию не меняем |
| R9 | Отсутствие автоматических бэкапов на хосте | средняя | собственный cron `pg_dump` + проверка восстановления (фаза 8) |
| R10 | Устаревшие константы в `storage.py` указывают на несуществующее хранилище | средняя | убрать дефолты, требовать явные значения; иначе молчаливая загрузка «в никуда» |
| R11 | `knowledge/loader.py` и `publish_saved.py` завязаны на layout репозитория | средняя | `AI_SMM_KNOWLEDGE_ROOT`, монтаж knowledge read-only, проверка на старте |
| R12 | Нулевое покрытие автотестами перед первым production-деплоем | средняя | фаза 2 создаёт тесты T1–T11 как гейт |
| R14 | Rate limit / истечение `THREADS_ACCESS_TOKEN` | низкая | счётчик за 24 ч, `MIN_PUBLISH_INTERVAL`, backoff; ротация — Q5 |
| R15 | Нет checkpointer в LangGraph: падение = потеря работы и повторные траты | низкая | CLI-генерация идемпотентна по запуску; checkpointer в Postgres — после релиза 1 |
| R16 | Модульная инициализация `ChatOpenAI`/`CallbackHandler` требует env до импорта | низкая | фабрики вместо модульных синглтонов в фазе 1 |
| R17 | Расхождение timezone хоста (+03:00) и Postgres (UTC) | низкая | `timestamptz` везде, `TZ=UTC` в контейнерах, отдельный `DISPLAY_TZ` |

---

## 13. Open questions (блокируют deployment)

| № | Вопрос | Блокирует | Почему нельзя решить без пользователя |
|---|---|---|---|
| **Q1** | Подтверждается ли этот VPS (хост из `STORAGE_SSH_HOST`, обслуживающий `media.arcade-lab.info`) как целевой для развёртывания? В `~/.ssh/config` есть и другие серверы. | фаза 3 | выбор целевого хоста — решение владельца инфраструктуры |
| **Q2** | Согласовано ли создание роли `ai_smm` и базы `ai_smm` в существующем инстансе `<postgres-container>`? Это единственная запись в общий сервис. | фаза 4 | затрагивает инструмент, от которого зависят чужие проекты |
| **Q3** | В какой timezone планируются публикации и каковы допустимые окна/частота? Хост в Europe/Vilnius, Postgres в UTC. | фазы 5–7 | бизнес-требование, из кода не выводится |
| **Q4** | Какой конкретный пост публикуется в контролируемом live-тесте (фаза 7)? Кандидат — пост 2 пилотной очереди (формат `image`, `approved`). Текст и изображение подтверждаются до отправки. | фаза 7 | необратимая публичная публикация |
| **Q5** | Каков срок жизни `THREADS_ACCESS_TOKEN` и процедура его ротации? | фаза 8 | без этого сервис молча перестанет публиковать при истечении токена |
| **Q6** | Допустимо ли, чтобы при недоступности общего Postgres публикации задерживались (вместо отдельного инстанса БД)? | решение раздела 5 | компромисс изоляция/ресурсы — решение владельца |
| **Q7** | Нужна ли уже сейчас мультипроектность, или релиз 1 закрепляет пилот `ai-catalog-consultant`? От ответа зависит, убирать ли `PILOT_MEDIA`/`PILOT_PROJECT_ID` в фазе 1. | фаза 1 | влияет на объём рефакторинга |

---

## 14. Статус открытых вопросов

| № | Вопрос | Статус |
|---|---|---|
| Q1 | Целевой VPS | **подтверждён** |
| Q2 | Создание роли и базы `ai_smm` | **разрешено и выполнено** |
| Q3 | Timezone планирования | **решено:** `Europe/Moscow` для ввода и отображения, UTC в хранении |
| Q4 | Пост для первой реальной публикации | **открыт** — блокирует фазу 7 |
| Q5 | Срок жизни и ротация `THREADS_ACCESS_TOKEN` | **открыт** |
| Q6 | Допустимость задержки при недоступности общего Postgres | **принято:** worker переживает простой, публикации задерживаются |
| Q7 | Мультипроектность | **решено:** предусмотрена в схеме, UI не реализуется |

---

## 15. Фактически развёрнутое состояние (2026-10-09)

### 15.1 Что развёрнуто

| Компонент | Факт |
|---|---|
| Образ | `ai-smm:73460cd`, 336 МБ локально, базовые образы по digest |
| Контейнер | `ai-smm-worker`, compose project `ai-smm`, `restart=unless-stopped` |
| Сеть | `<shared-network>` как `external` — чужие сервисы не пересоздаются |
| Порты | **нет** (`PortBindings=map[]`) |
| Привилегии | `user=10001:10001`, `ReadonlyRootfs=true`, `CapDrop=[ALL]`, `no-new-privileges` |
| Лимиты | 384 МБ RAM, 0.75 CPU, 128 PIDs; фактическое потребление 56 МиБ |
| Логи | `json-file`, `max-size=10m`, `max-file=5`; глобальный `daemon.json` не создавался |
| БД | `ai_smm` в существующем `<postgres-container>`, роль `ai_smm` (NOSUPERUSER, NOCREATEDB) |
| Миграция | `a3fbcef157c3`, применена разово через `compose run migrate` |
| Backup | `/root/ai-smm/backup.sh`, cron 03:30, retention 14 дней, restore проверен |
| Режим | `AI_SMM_DRY_RUN=true`; `THREADS_ACCESS_TOKEN` и `OPENAI_API_KEY` на сервере **пустые** |

### 15.2 Отличия от плана

| Планировалось | Фактически | Причина |
|---|---|---|
| Отдельные контейнеры scheduler и worker | один контейнер `ai-smm-worker` | APScheduler — только периодический триггер, расписание в таблице; второй процесс не нужен и экономит RAM |
| APScheduler с persistent jobstore | jobstore не используется | решение 8: расписание целиком в `publications.scheduled_at`, рестарт ничего не теряет |
| `PROJECT_ROOT` для путей изображений | отдельный `AI_SMM_MEDIA_ROOT` | в контейнере пути `knowledge/...` не разрешались относительно `/app` |
| `deploy.resources.limits` | `cpus` / `mem_limit` / `pids_limit` | Compose v5 запрещает одновременное использование |

### 15.3 Результаты приёмки

| № | Проверка | Результат |
|---|---|---|
| T1 | Docker build, отсутствие `.env` в слоях | пройдено |
| T2 | Запуск без публичных портов | пройдено, набор портов хоста не изменился |
| T3 | Сохранность данных после restart | пройдено, состояние побитово идентично |
| T4 | Миграции `upgrade` / `downgrade` / повторный `upgrade` | пройдено, ENUM-типы удаляются корректно |
| T5 | Одна scheduled task | пройдено, dry-run в запланированную секунду |
| T6 | Отсутствие двойной публикации | пройдено, 8 конкурентных worker на 6 записей |
| T7 | Аварийное завершение worker (SIGKILL) | пройдено, запись → `needs_review`, автоповтора нет |
| T8 | Timeout Threads API | пройдено, `outcome=timeout`, автоповтора нет |
| T9 | Недоступность PostgreSQL | пройдено, worker выжил, восстановился сам, `restarts=0` |
| T10 | Безопасное завершение | пройдено, SIGTERM, `clean=true`, exit 0, 409 мс |
| T11 | Отсутствие секретов в логах | пройдено, проверено в контейнере |
| T12 | Отсутствие влияния на соседние сервисы | пройдено, Id и StartedAt всех 6 контейнеров неизменны |

Автотесты: **128 passed**, внешние API замоканы, `ruff check` чист.

### 15.4 Что осталось до первой реальной публикации

1. **Q4** — подтверждение конкретного поста (кандидат: пост 2, формат `image`).
2. **Credentials на сервере** — `THREADS_ACCESS_TOKEN`, `STORAGE_SSH_HOST`,
   `STORAGE_SSH_USER` сейчас пустые.
3. **SFTP-ключ для медиа** — `/srv/ai-smm/ssh/id_storage` пустой placeholder;
   см. [operations.md §9](operations.md).
4. **`AI_SMM_DRY_RUN=false`** — переключается только под наблюдением.

### 15.5 Известные ограничения

| № | Ограничение | Влияние | Исправление |
|---|---|---|---|
| L1 | Роль `ai_smm` может `CONNECT` к `<other-db-1>` и `<other-db-2>` через `PUBLIC` | низкое: читаемых таблиц **0**; то же верно для существующих ролей (`<other-role-1>` → `<other-db-2>`) | `REVOKE CONNECT ON DATABASE <other-db-1>, <other-db-2> FROM PUBLIC` — изменяет чужие базы, требует согласования |
| L2 | `paramiko.AutoAddPolicy` принимает любой host key | MITM на SFTP | pinning host key до первой реальной загрузки медиа |
| L3 | Медиа-загрузка не настроена | image/carousel публикации упадут в preflight (безопасно, до Threads) | см. [operations.md §9](operations.md) |
| L4 | `publish_saved.py` создаёт JSON с правами 600 | uid 10001 не прочитает файл при импорте | `install -m 644` читаемой копии |
| L5 | `ChatOpenAI` и `CallbackHandler` создаются на уровне модуля | env нужен до импорта `ai_smm.agents.*` | не влияет на worker (агенты не импортируются); фабрики вместо синглтонов — отдельная задача |
| L6 | Один worker, advisory-lock не задействован | при добавлении второго worker корректность обеспечивает `SKIP LOCKED` + lease | проверено тестом на 8 конкурентных worker |
| L7 | Нет checkpointer в LangGraph | падение в середине графа = повторные траты на LLM | вне scope релиза 1 |
| L8 | Образ переносится через `docker save`/`load` | нет registry, откат требует наличия предыдущего образа на хосте | сохранять предыдущий тег до подтверждения новой версии |

---

## 16. STOP POINT

Развёртывание в dry-run завершено. Реальная публикация в Threads
**не производилась** и невозможна в текущей конфигурации: `AI_SMM_DRY_RUN=true`,
токен Threads на сервере отсутствует, очередь припаркована
(`scheduled_at=NULL`, `human_reviewed=false` у всех записей).

Ожидается подтверждение конкретного поста для первой реальной публикации (Q4).
