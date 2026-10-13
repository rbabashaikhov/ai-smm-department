# SMM-025B — Multi-Project & Multi-Account Platform: Architecture

| | |
|---|---|
| Project | PRJ-013 AI SMM Department 2.0 |
| Task | SMM-025B — projects created and configured from the Control Center (B1), several social accounts per platform with an encrypted credentials manager (B1), and account-aware publishing through the existing queue and worker (B2) |
| Document status | **Revision 2, 2026-10-13 — final architecture of the SMM-025B sprint.** Revision 1 (project creation only, "no migration") is superseded: multi-account publishing needs two tables and two columns |
| Baseline | `main` at `6fc6d22fa64d7c061ca3ee742a19f72d5757ebd9` |
| Branch | `feat/smm-025b-multi-project-accounts` (local, not committed) |
| Builds on | [SMM-025A design](smm-025a-product-ui-design.md) rev. 3 — answers its deferred **Q-2** (second project), **Q-3** (style storage) and **Q-5** (credentials for more than one account) |
| Reports | [docs/reports/smm-025b/](../reports/smm-025b/) — implementation, security, migration & rollback, tests, open issues |
| Production | **Not touched.** No deployment, no VPS access, no production database, no real publication. Everything below is local |

---

## 0. Summary

**Goal.** One owner runs several projects. Each project has its own settings, knowledge and content, and publishes through **its own** Threads accounts — possibly several per project — whose tokens are entered in a separate Credentials page, stored encrypted, and never readable back. A human approves the exact content **and** the account it goes to; the existing PostgreSQL queue and worker deliver it through that account and no other.

**What the code did before this sprint** (details §1):

1. No supported way to create a project with a membership; the existing owner could not get a second project.
2. One Threads token per deployment (`THREADS_ACCESS_TOKEN`); the worker claims due rows of **every** project and publishes them all with it.
3. No edit path for the project record; settings were raw JSON.
4. Image paths and knowledge ids were not confined to their project.
5. `import-json` created owner-less projects and overwrote names.

**The model** (§2) — two new tables, two new nullable columns, one guard trigger:

```
Project ─< ProjectMembership (owner)          [existing]
   │  └── ProjectSettings (topics, tone)       [existing JSONB, typed keys]
   ├─< SocialAccount (platform, label, external id, active/disabled)        [new]
   │      └─< Credential (sealed ciphertext, fingerprint, status)           [new]
   ├─< ContentItem ─< ContentRevision ── social_account_id (the destination) [new column]
   │                       └─< ContentApproval (human, this exact revision)
   └─< Publication ── social_account_id (immutable delivery snapshot)        [new column]
                         NULL = legacy row: deployment token, allow-listed projects only
```

**The guarantees** (§6):

- **Approval covers content and destination.** The destination account is part of the immutable revision and of its content hash. Choosing another account creates a new revision, which clears the approval.
- **No implicit fallback.** An account-bound row is published only with that account's own decrypted token, after the token's identity (`GET /me`) is verified to be that account. Any failure stops before the point of no return; the deployment token is never tried. Legacy (account-less) rows use the deployment token only for projects explicitly allow-listed by the operator; a new project is never on that list.
- **Old worker cannot misroute.** A trigger refuses to claim an account-bound row unless the claiming transaction declares account routing, which only the new worker does. An old worker on the new schema skips nothing silently — it fails loudly and publishes nothing.
- **Secrets.** The API holds only a public key and can only encrypt; the worker holds the private key in a file outside the database. Ciphertext is bound to its account and credential row. Replacing or revoking a credential destroys its ciphertext. No endpoint returns a secret; logs, audit, errors and test output carry only a fingerprint.

---

## 1. Current State Audit

Checked in the repository at the baseline. Every statement carries a file reference. §1.1–§1.8 are from revision 1 of this document (project scope); §1.9 adds the publishing, credential and media audit of the sprint.


Checked in the repository at the baseline. Every statement carries a file reference.

### 1.1 Data model

| Table | Relevant columns and constraints | Reference |
|---|---|---|
| `projects` | `id String(120)` **primary key, client-chosen, no format check**; `display_name`, `description` (default `""`), `knowledge_path` (NOT NULL), `default_platform` (default `threads`), `is_active`, `default_timezone` (default `Europe/Moscow`), `default_language` (default `ru`), `version` (default 1, **unused**), `created_at`, `updated_at`. No uniqueness on `display_name` | [models.py:251-298](../../src/ai_smm/db/models.py#L251-L298) |
| `project_memberships` | `(project_id, user_id)` unique; role enum viewer < editor < admin < owner; `ON DELETE CASCADE` from both sides | [models.py:744-785](../../src/ai_smm/db/models.py#L744-L785) |
| `project_settings` | PK = `project_id`; three JSONB sections `content_config`, `publishing_config`, `brand_config`; `version` (optimistic lock). **Optional row**: absent means version 0 and empty sections | [models.py:788-820](../../src/ai_smm/db/models.py#L788-L820) |
| `users` | normalised unique e-mail; no global/system role — every right is per project | [models.py:652-694](../../src/ai_smm/db/models.py#L652-L694) |
| `publications`, `content_series`, `content_items` | `project_id` FK `ON DELETE RESTRICT`; `publications` unique on `(project_id, platform, ordinal)` and on `idempotency_key` | [models.py:371-449](../../src/ai_smm/db/models.py#L371-L449), [models.py:895-906](../../src/ai_smm/db/models.py#L895-L906) |
| `content_publication_links` | trigger: the publication must be in the content item's project | [models.py:1229-1279](../../src/ai_smm/db/models.py#L1229-L1279) |

**Nothing in the schema forces a project to have a member.** A membership references a project; the reverse cannot be a foreign key. The guarantee has to come from the code path that creates projects (§4.1, §4.6).

**Capability check — second project for the same owner:** the schema allows it (`(project_id, user_id)` is unique per pair, not per user). The code does not (§1.2, §1.6).

### 1.2 Application services

| Service | Behaviour | Reference |
|---|---|---|
| `ensure_project` | Creates a `Project` if the id is unknown — **without a membership, without an audit entry**. If it exists, **overwrites `display_name` and `knowledge_path`** | [queue.py:65-91](../../src/ai_smm/queue.py#L65-L91) |
| `import_json_queue` | Calls `ensure_project` with `display_name = --display-name or project_id` and `knowledge_path = knowledge/projects/<id>`. Never creates `scheduled` rows | [importer.py:165-171](../../src/ai_smm/importer.py#L165-L171), [importer.py:111-136](../../src/ai_smm/importer.py#L111-L136) |
| `create_owner` | Creates a **new** user plus an owner membership in one transaction. **Fails with `DuplicateUserError` if the e-mail exists**, so it cannot attach an existing owner to another project | [users.py:65-133](../../src/ai_smm/application/users.py#L65-L133), [users.py:97-100](../../src/ai_smm/application/users.py#L97-L100) |
| `list_user_projects` | Only projects with a membership, ordered by id | [application/projects.py:72-85](../../src/ai_smm/application/projects.py#L72-L85) |
| `get_settings_view` / `update_settings` | Version-checked, **whole-section replace**; first PATCH creates the row with `expected_version = 0`; audit `project.settings.update`. **No size limit and no schema** for the sections | [application/projects.py:99-221](../../src/ai_smm/application/projects.py#L99-L221) |
| `require_mutations_enabled` | Service-boundary repeat of the mutation gate | [control_plane.py:43-49](../../src/ai_smm/application/control_plane.py#L43-L49) |
| `schedule_publication` | The only API path to `scheduled` (`reschedule_publication` delegates to it). Checks gate, row lock, transition. **No project-level publishing check** | [publications.py:413-471](../../src/ai_smm/application/publications.py#L413-L471), [publications.py:474-497](../../src/ai_smm/application/publications.py#L474-L497) |
| `_build_publication` (materialize) | Creates the delivery row as `approved`, `human_reviewed=True`, `scheduled_at=None` — never `scheduled` | [content.py:783-820](../../src/ai_smm/application/content.py#L783-L820) |
| `_validate_input` (revisions) | Checks format and non-empty body only. **`images` are free-form dicts**; no project scoping of paths | [content.py:226-233](../../src/ai_smm/application/content.py#L226-L233), [schemas.py:354](../../src/ai_smm/api/schemas.py#L354) |

### 1.3 HTTP API and RBAC

| Route | Access | Reference |
|---|---|---|
| `GET /api/v1/projects` | session; member projects only | [routers/projects.py:36-45](../../src/ai_smm/api/routers/projects.py#L36-L45) |
| `GET /api/v1/projects/{id}` | viewer+ | [routers/projects.py:48-54](../../src/ai_smm/api/routers/projects.py#L48-L54) |
| `GET /api/v1/projects/{id}/settings` | viewer+ | [routers/projects.py:57-67](../../src/ai_smm/api/routers/projects.py#L57-L67) |
| `PATCH /api/v1/projects/{id}/settings` | admin+, CSRF, `expected_version` | [routers/projects.py:70-102](../../src/ai_smm/api/routers/projects.py#L70-L102) |
| `GET /api/v1/auth/me` | session; `projects[]` = id, name, role — a **snapshot** used by the UI at sign-in | [routers/auth.py:136-160](../../src/ai_smm/api/routers/auth.py#L136-L160) |
| create / update project record | **none** | — |

- Authorisation always runs *resource from DB → membership → role*. A non-member gets 404, a too-low role 403 ([dependencies.py:143-189](../../src/ai_smm/api/dependencies.py#L143-L189)). Role ordering lives only in [security/rbac.py](../../src/ai_smm/security/rbac.py).
- `require_project_role` needs a project id in the URL. **Creating a project has no project to authorise against**, and there is no global role, so a creation rule must be defined (§4.5, decision OD-1 in §8).
- The mutation gate is a dependency of the whole `/api/v1` router and refuses every unsafe method except login/logout ([mutation_gate.py:42-78](../../src/ai_smm/api/mutation_gate.py#L42-L78), [app.py:90-93](../../src/ai_smm/api/app.py#L90-L93)). A test fails when an unsafe route is added without being listed in `GATED` ([test_api_mutation_gate.py:178](../../tests/test_api_mutation_gate.py#L178), [test_api_mutation_gate.py:383-396](../../tests/test_api_mutation_gate.py#L383-L396)); the same map drives the least-privilege proof that `ai_smm_cc` cannot perform any business write ([test_db_least_privilege.py:792-811](../../tests/test_db_least_privilege.py#L792-L811)).
- Production: `AI_SMM_API_MUTATIONS_ENABLED` is the literal `"false"` ([compose.control-center.yml:62](../../compose.control-center.yml#L62)); `ai_smm_cc` has only `SELECT` on `projects`, `project_memberships`, `project_settings` ([ai_smm_cc.grants.sql:138-139](../../deploy/control-center/db/ai_smm_cc.grants.sql#L138-L139)). Enabling writes is FU-024C2-3 / FU-024C1-5 (P1, open).

### 1.4 Settings and "style"

- Sections are free JSON. Nothing outside the settings API reads them — not the agents, not the worker (025A S6).
- No key convention exists for topic or tone of voice.
- `description`, `default_timezone`, `default_language` are columns on `projects`, deliberately **not** duplicated into settings ([application/projects.py:99-106](../../src/ai_smm/application/projects.py#L99-L106)).

### 1.5 Knowledge base

- Files under `knowledge/projects/<project_id>/` (`*.md`, `*.txt`, `*.yaml`) plus `assets/` images, and one **global** `knowledge/editorial_policy.md` ([loader.py:41-124](../../src/ai_smm/knowledge/loader.py#L41-L124)).
- The loader builds the directory from **`project_id`**, not from `projects.knowledge_path` ([loader.py:61](../../src/ai_smm/knowledge/loader.py#L61)). The two agree only by the importer's convention. The id is not validated, so an id like `../x` would leave the root — harmless today (ids come from operators), not harmless once ids come from HTTP.
- In production the knowledge tree is a read-only mount for the runtime; the Control Center containers do not mount it ([production-deployment-plan.md:316](../production-deployment-plan.md), [compose.control-center.yml](../../compose.control-center.yml)). **The API cannot see or change knowledge, and should not.**
- The repository contains one project, `ai-catalog-consultant`. Agents hard-code it as `PILOT_PROJECT_ID` for pilot-only rules ([copywriter.py:17](../../src/ai_smm/agents/copywriter.py#L17), [editor.py:19](../../src/ai_smm/agents/editor.py#L19), [publisher.py:13](../../src/ai_smm/agents/publisher.py#L13)); any other id runs in the generic mode.
- Publication images are resolved against one shared `media_root` and checked only to stay inside it ([service.py:97-130](../../src/ai_smm/publishing/service.py#L97-L130)) — **not** inside the publication's own project.

### 1.6 CLI

| Command | Relevance | Reference |
|---|---|---|
| `ai-smm user create-owner --email --display-name --project` | New user + owner membership on an **existing** project; fails on an existing e-mail. In production wrapped as `cc-compose.sh create-owner` behind the identity, queue-window and backup gates | [cli.py:940-1020](../../src/ai_smm/cli.py#L940-L1020), [control-center-deployment.md:189](../control-center-deployment.md) |
| `ai-smm import-json PATH [--display-name]` | The only creator of projects; no membership; overwrites name on re-run | [cli.py:878-904](../../src/ai_smm/cli.py#L878-L904), [operations.md:132](../operations.md) |
| `ai-smm schedule` | Moves a row to `scheduled` outside the API; no project-level publishing check | [cli.py:331](../../src/ai_smm/cli.py#L331) |
| `ai-smm publish-now --live --confirm-reviewed` | Publishes one row immediately with the deployment token | [cli.py:437](../../src/ai_smm/cli.py#L437) |
| `ai-smm approve` | Legacy: sets `human_reviewed` outside the editorial layer (025A Q-6) | [cli.py:375-387](../../src/ai_smm/cli.py#L375-L387) |

### 1.7 Worker, queue and publishing

- `claim_due_publication` selects the earliest due `scheduled` row with `platform = 'threads'` — **across all projects** ([queue.py:275-356](../../src/ai_smm/queue.py#L275-L356)). Called without any project filter ([worker.py:238-263](../../src/ai_smm/worker.py#L238-L263)).
- One account: `THREADS_ACCESS_TOKEN` from the environment ([threads.py:62-76](../../src/ai_smm/publishing/threads.py#L62-L76)).
- Daily cap and spacing are counted per platform, i.e. shared by all projects ([worker.py:265-313](../../src/ai_smm/worker.py#L265-L313), [queue.py:121-146](../../src/ai_smm/queue.py#L121-L146)).
- Hard gate before any network call: `human_reviewed` must be true, no prior post id ([service.py:143-187](../../src/ai_smm/publishing/service.py#L143-L187)).
- `projects.is_active` is read by nothing in the backend; an inactive project still publishes (025A S9).
- The worker reads none of `projects`, `project_memberships`, `project_settings`. **Creating or editing a project cannot, by itself, cause a publication.**

### 1.8 React Control Center

| Element | State | Reference |
|---|---|---|
| Routes | `/projects`, `/projects/:projectId/{,content,content/new,publications,attention,audit}`. **No create page, no settings page** | [AppRoutes.tsx:70-79](../../web/src/routes/AppRoutes.tsx#L70-L79) |
| Projects Overview | Read-only table: name, id, role, queue counters, platform, timezone, language; settings shown as a raw JSON `<details>` | [ProjectsPage.tsx:13-39](../../web/src/pages/ProjectsPage.tsx#L13-L39), [ProjectsPage.tsx:85-151](../../web/src/pages/ProjectsPage.tsx#L85-L151) |
| Empty state | "У вас нет доступа ни к одному проекту. Обратитесь к владельцу проекта." — no create action | [ProjectsPage.tsx:95-97](../../web/src/pages/ProjectsPage.tsx#L95-L97) |
| API client | `projects.list/get/settings` only; no write calls for projects | [endpoints.ts:52-56](../../web/src/api/endpoints.ts#L52-L56) |
| `/auth/me` | Held in `AuthProvider` state, loaded once at bootstrap/login; **no refresh function** (only `retryBootstrap`, which shows a loading screen). The sidebar role and `/` redirect read it (FU-025A-4) | [AuthProvider.tsx:42-131](../../web/src/features/auth/AuthProvider.tsx#L42-L131), [AppLayout.tsx:47](../../web/src/layouts/AppLayout.tsx#L47), [AppRoutes.tsx:49-57](../../web/src/routes/AppRoutes.tsx#L49-L57) |
| Permissions | `canEditContent` (editor+), `canCommandPublications` (admin+), all false on a read-only deployment. No permission for settings or project creation | [rbac.ts:17-25](../../web/src/features/auth/rbac.ts#L17-L25) |

### 1.9 Publishing, credentials, media and secrets (sprint audit)

| Area | Fact | Reference |
|---|---|---|
| Publisher factory | The worker calls a zero-argument `publisher_factory()`; the default builds `ThreadsPublisher()`, which reads `THREADS_ACCESS_TOKEN` from the environment in its constructor | [worker.py:44-48](../../src/ai_smm/worker.py#L44-L48), [threads.py:61-80](../../src/ai_smm/publishing/threads.py#L61-L80) |
| Account identity | Every Threads call goes to `/me/...`: **the token alone decides the account**. A wrong token publishes to the wrong account without any other signal | [threads.py:162](../../src/ai_smm/publishing/threads.py#L162), [threads.py:384](../../src/ai_smm/publishing/threads.py#L384) |
| Point of no return | `mark_publishing` + commit before the publish call; everything before it (preflight, media upload) is safely retryable | [service.py:437-447](../../src/ai_smm/publishing/service.py#L437-L447) |
| Factory call site | `publisher_factory()` is called after the dry-run branch and before media upload — the natural place to resolve an account | [service.py:357](../../src/ai_smm/publishing/service.py#L357) |
| Series replies | A reply's `reply_to_id` is the parent's `threads_post_id`; it is only valid **in the same account** as the parent. Nothing checks that today (one account) | [series.py:266-366](../../src/ai_smm/series.py#L266-L366) |
| Rate limits | Daily cap and spacing count per platform across projects | [worker.py:265-313](../../src/ai_smm/worker.py#L265-L313) |
| Media | Images are uploaded to one storage target under `THREADS_STORAGE_PREFIX`; the uploader confines writes to its prefix. Local image paths are checked only against the shared media root | [threads.py:272-319](../../src/ai_smm/publishing/threads.py#L272-L319), [service.py:97-130](../../src/ai_smm/publishing/service.py#L97-L130) |
| Log redaction | A filter masks configured secret values and token shapes (`THQ…`, `Bearer …`, `token=…`) on every record, including exceptions and the `context` dict | [logging_setup.py:25-149](../../src/ai_smm/logging_setup.py#L25-L149) |
| API validation errors | Report field and rule only, never the submitted value | [errors.py:230-252](../../src/ai_smm/api/errors.py#L230-L252) |
| Langfuse | Used only by the agents (`@observe`, LangChain callback); never on publishing or API paths | [copywriter.py:7-60](../../src/ai_smm/agents/copywriter.py#L7-L60) |
| `.env` loading | `threads.py` and `storage.py` call `load_dotenv()` **at import**. A test that deletes a variable and then imports either module silently loads the developer's real `.env` (observed during this sprint: `test_config.py::test_storage_requires_a_public_base_url` fails or passes depending on that file). Test runs in this sprint use `PYTHON_DOTENV_DISABLED=1` | [threads.py:16](../../src/ai_smm/publishing/threads.py#L16), [storage.py:31](../../src/ai_smm/storage.py#L31) |
| CLI publishing | `publish-now --live` publishes one row immediately with the deployment factory | [cli.py:437-566](../../src/ai_smm/cli.py#L437-L566) |

### 1.10 Findings

| # | Finding | Severity | Addressed in |
|---|---|---|---|
| A-1 | No way to create a project with a membership; existing owner cannot get a second project | Blocks goal | §4.1, B1 |
| A-2 | Every project's scheduled rows publish with the one deployment token | **Critical** for multi-project | §5, §6 |
| A-3 | `import-json` creates owner-less projects and overwrites `display_name` | High | §4.6 |
| A-4 | No edit path for the project record | Blocks goal | §4.1 |
| A-5 | Settings JSON without schema or size limit | Medium | §4.2 |
| A-6 | Revision image paths not confined to the project | Medium | §4.7 |
| A-7 | Project id unvalidated, used as a path segment by the knowledge loader | Medium | §4.7 |
| A-8 | UI has no create/settings pages; `/auth/me` cannot be refreshed | Blocks goal | §3 |
| A-9 | The token decides the account; nothing verifies which account a token belongs to | **Critical** once there are several tokens | §5.3 |
| A-10 | Series replies assume one account | High once there are several accounts | §5.5 |
| A-11 | Import-time `load_dotenv()` can pull real secrets into test processes | Medium (test hygiene, secret exposure) | Open issue OI-6 |
| A-12 | `is_active` display-only (025A Q-4) | Out of scope | — |

---

## 2. Target Model

### 2.1 Entities

| Entity | Table | Status | Purpose |
|---|---|---|---|
| Project | `projects` | existing, reused | identity, description, timezone, language |
| ProjectMembership | `project_memberships` | existing | the owner's role; all authorisation |
| ProjectSettings | `project_settings` | existing | `content_config.topics`, `brand_config.tone_of_voice` (typed keys) |
| **SocialAccount** | `social_accounts` | **new** | one account on one platform, belonging to one project |
| **Credential** | `social_account_credentials` | **new** | sealed token of an account; at most one active |
| Revision → account | `content_revisions.social_account_id` | **new nullable column** | the destination a human approves together with the text |
| Publication → account | `publications.social_account_id` | **new nullable column** | the destination the worker must use |

Deliberately not introduced: a connector/provider registry, an owner-level account pool, OAuth flows, per-account rate limits, a key-management service.

### 2.2 `social_accounts`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `project_id` | varchar(120) FK `projects` RESTRICT | an account belongs to exactly one project — RBAC stays project-scoped |
| `platform` | varchar(40), CHECK `in ('threads')` | the only platform the worker can deliver to |
| `display_name` | varchar(120) | owner's label ("Личный блог") |
| `status` | varchar(16) CHECK `active`/`disabled`; `disabled_at` required when disabled | |
| `external_account_id`, `external_username` | varchar(120), nullable | set by the first successful connection test; immutable afterwards. **Publishing requires it** |
| `version` | int | optimistic concurrency |
| `created_by_user_id`, `created_at`, `updated_at`, `disabled_at` | | |

Constraints: `UNIQUE (project_id, id)` (target of the publications composite FK); partial `UNIQUE (project_id, platform, external_account_id) WHERE external_account_id IS NOT NULL` — the same Threads account cannot be connected twice in one project. The same Threads account *may* be connected in two projects (two rows, two credentials): an explicit owner choice, kept simple.

### 2.3 `social_account_credentials`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | part of the AEAD associated data |
| `social_account_id` | uuid FK RESTRICT | |
| `status` | `active` / `superseded` / `revoked` | partial unique index: **one active per account** |
| `scheme`, `key_id` | varchar | construction version; which key pair sealed it |
| `ephemeral_public_key`, `nonce`, `ciphertext` | bytea, **NULL unless active** | CHECK `(status = 'active') = (ciphertext IS NOT NULL …)`: replacing or revoking a credential destroys its ciphertext in the database |
| `fingerprint` | char(12) | first 12 hex of SHA-256 of the token — the only "view" of a token anywhere |
| `verification_status`, `verified_at` | `unverified` / `verified` | result of the connection test at entry |
| `expires_at` | timestamptz, nullable | owner-entered reminder; informational |
| `created_by_user_id`, `created_at`, `superseded_at`, `revoked_at` | | |

### 2.4 Destination columns

- `content_revisions.social_account_id` — nullable FK. Part of the immutable revision and, when set, of `content_hash`. A NULL keeps the hash of every existing revision unchanged.
- `publications.social_account_id` — nullable; **composite FK `(project_id, social_account_id)` → `social_accounts (project_id, id)`**, so the database refuses a publication routed to another project's account. Copied from the approved revision at materialisation; never changed afterwards (no endpoint writes it).
- NULL on a publication = **legacy row**: everything that existed before this migration, and rows of allow-listed legacy projects. It keeps its old meaning — deployment token — under §5.2.

### 2.5 Field ownership (project)

| Concept | Stored in | Editable by |
|---|---|---|
| Identifier (slug) | `projects.id` | set once; immutable (URL, knowledge dir, keys, audit subjects) |
| Name, description, timezone, language | `projects.*` | admin+ (`PATCH /projects/{id}`, version-checked) |
| Topics, tone of voice | `project_settings` typed keys | admin+ (`PATCH /projects/{id}/settings`, version-checked) |
| Knowledge path | `projects.knowledge_path` = `knowledge/projects/<id>` | never |
| Accounts | `social_accounts` | **owner** |
| Credentials | `social_account_credentials` | **owner**, write-only |
| Legacy routing | `AI_SMM_LEGACY_THREADS_PROJECTS` (operator configuration) | operator, by release |

---

## 3. User Journeys and UI

All write controls appear only when `mutations_enabled` and the role allows (as in SMM-025A). Read-only deployments show everything read-only.

| # | Journey | Flow |
|---|---|---|
| J-1 | Create a project | Projects → "+ Новый проект" → `/projects/new` (name, id suggested from name, description, topics, tone, timezone, language) → `201` → `/auth/me` refreshed → lands in the new project, role owner |
| J-2 | Edit project | `/projects/:id/settings`: card "Основное" (record) and card "Редакционный профиль" (topics, tone), each with its own version and Save; `409` shows a re-read panel |
| J-3 | Connect accounts | `/projects/:id/accounts` (owner): "+ Аккаунт" (platform Threads, label). Several Threads accounts per project. Each row: status, @username once verified, credential state, publishing readiness and why not |
| J-4 | Enter / replace / revoke a token | **Separate page** `/projects/:id/credentials` (owner): per account a password field (`autocomplete=off`, never echoed), optional expiry, "Сохранить и проверить". Server tests the token (`GET /me`), binds the account to that Threads identity on first success, refuses a token of a different identity. Shows only fingerprint, status, verified time, expiry. "Отозвать" destroys the stored secret |
| J-5 | Disable / enable account | Accounts page; disabling warns how many scheduled rows will then fail instead of publishing |
| J-6 | Choose the destination | Content create / new revision form: "Аккаунт публикации" select (active accounts of this project; "legacy" option only for an allow-listed project). The content detail shows the account of the current revision; changing it = a new revision |
| J-7 | Approve | Approval panel shows text **and** destination account; approving approves that revision, i.e. both |
| J-8 | Materialise, schedule | As before. Publication pages show the account. Scheduling an unready route is refused with the reason (`PUBLISHING_ROUTE_NOT_READY`) |
| J-9 | Several projects | Overview and switcher list all; settings, accounts, credentials, content and audit are per project; a non-member gets 404 |

Wireframe of the credentials manager (the other pages follow the SMM-025A style and revision 1 wireframes):

```
┌ Учётные данные · Путевые заметки ─────────────────────────────────────────┐
│ Секреты хранятся зашифрованно и не могут быть прочитаны обратно.          │
│ ┌ Личный блог · threads · @travel_ru · ● готов к публикации ─────────────┐│
│ │ Активный токен: отпечаток 3f9a1c0b77d2 · проверен 13.10 18:02 · до 12.12││
│ │ Новый токен  [••••••••••••••••••••••••]  Срок до [ 2026-12-12 ]        ││
│ │                           [Заменить и проверить]   [Отозвать токен]     ││
│ └────────────────────────────────────────────────────────────────────────┘│
│ ┌ Эксперименты · threads · не подключён · ○ нет токена ──────────────────┐│
│ │ Токен [                        ]           [Сохранить и проверить]     ││
│ └────────────────────────────────────────────────────────────────────────┘│
└────────────────────────────────────────────────────────────────────────────┘
```

---

## 4. API and Data Contracts

All routes are under `/api/v1`, behind CSRF and the mutation gate (deny by default; listed in the gate's route inventory test).

### 4.1 Projects

| Method & path | Role | Body → response |
|---|---|---|
| `POST /projects` | caller must be **owner of at least one project** | `{id, display_name, description?, default_timezone?, default_language?, topics?, tone_of_voice?}` (`extra=forbid`) → `201 ProjectOut` (role owner); replay by the owner with the same name → `200`; `409 PROJECT_ALREADY_EXISTS` / `PROJECT_NAME_TAKEN` / `PROJECT_LIMIT_REACHED` |
| `PATCH /projects/{id}` | admin+ | `{expected_version, display_name?, description?, default_timezone?, default_language?}` → `200 ProjectOut`; `409 VERSION_CONFLICT` |
| `PATCH /projects/{id}/settings` | admin+ | unchanged contract; typed validation of `topics` (≤10 × ≤80 chars, unique) and `tone_of_voice` (≤4000); 16 KiB per section; unknown keys preserved |

Creation writes `projects` + owner `project_memberships` + `project_settings` v1 + `project.create` audit in one transaction (a failure injected between any two writes leaves nothing). A concurrent create of the same id: the loser's insert fails inside a savepoint and is answered by the replay rule. `ProjectOut` gains `legacy_publishing: bool`.

Validation: id `^[a-z0-9](?:[a-z0-9-]{1,62}[a-z0-9])$`, no `--`, reserved `new`, `settings`, `api`, `admin`, `projects`; name 1–120; description ≤2000; IANA timezone (loads with `zoneinfo`); language `^[a-z]{2}(-[A-Z]{2})?$`.

### 4.2 Social accounts

| Method & path | Role | Notes |
|---|---|---|
| `GET /projects/{id}/social-accounts` | viewer+ | list with `credential` (metadata of the active one) and `publishing_ready` + `not_ready_reason` |
| `POST /projects/{id}/social-accounts` | owner | `{platform: "threads", display_name}` → `201` |
| `GET /social-accounts/{account_id}` | viewer+ (of its project) | account + credential history metadata |
| `PATCH /social-accounts/{account_id}` | owner | `{expected_version, display_name}` |
| `POST /social-accounts/{account_id}/disable` · `/enable` | owner | `{expected_version}`; disable answers `scheduled_publications` |

### 4.3 Credentials (write-only)

| Method & path | Role | Notes |
|---|---|---|
| `POST /social-accounts/{account_id}/credentials` | owner | `{access_token, expires_at?, verify: true}` → `201 CredentialOut` (metadata only). Supersedes the active one in the same transaction |
| `POST /social-account-credentials/{credential_id}/revoke` | owner | → `200 CredentialOut` |

There is **no** route that returns `access_token`, ciphertext, nonce or key material, and `CredentialOut` has no such field (a contract test walks the OpenAPI schema for it).

Connection test (when `verify` is true): the API calls Threads `GET /me?fields=id,username` with the submitted token **before** sealing it.

| Outcome | Response | Stored |
|---|---|---|
| identity == account's bound identity, or account not bound yet | `201`, `verification_status = verified`; first success binds `external_account_id`/`external_username` | yes |
| identity ≠ bound identity | `409 CREDENTIAL_ACCOUNT_MISMATCH` | nothing |
| identity already bound to another account of this project | `409 ACCOUNT_ALREADY_CONNECTED` | nothing |
| Threads rejects the token (4xx) | `422 CREDENTIAL_REJECTED` | nothing |
| Threads unreachable / 5xx | `502 DEPENDENCY_UNAVAILABLE` | nothing |
| `verify: false` | `201`, `unverified` | yes — but an account without a bound identity is **not publishable** |
| no public key configured | `503 CREDENTIALS_NOT_CONFIGURED` | nothing |

The API cannot re-test a stored token (it has no private key). Re-testing is "replace" with the same token, or the operator command `ai-smm accounts verify <id>` next to the worker. The worker re-verifies identity before every live publication anyway (§5.3).

### 4.4 Content and publications

- Revision input (`POST /projects/{id}/content-items`, `POST /content-items/{id}/revisions`) accepts `social_account_id` (nullable). Rules: the account must be in the item's project and active; NULL is accepted only for an allow-listed legacy project; otherwise `422`.
- Revision, publication, preview and list outputs carry `social_account_id` and `social_account` (`{id, display_name, platform, external_username, status}`).
- `POST /publications/{id}/schedule` and `/reschedule` additionally refuse `409 PUBLISHING_ROUTE_NOT_READY` (§5.4).

### 4.5 Authorisation summary

| Action | Minimum | Why |
|---|---|---|
| read accounts, credential metadata | viewer | editors must see destinations to choose one |
| choose destination on a revision | editor (existing revision right) | the choice is reviewed by the approver |
| approve | existing approve right (editor+), human only | unchanged |
| schedule | admin | unchanged |
| create/rename/disable accounts, set/revoke credentials | **owner** | controls which external identity the project speaks as |
| create project | owner of ≥1 project | no global role exists |

Missing project or non-membership → 404; too-low role → 403. An account or credential addressed by its own id is loaded first and authorised against **its** project (the existing row-first pattern).

### 4.6 Importer and CLI

- `import-json` no longer creates projects: an unknown `project_id` is refused (pointing at `ai-smm project create`), and an existing project's name and knowledge path are no longer rewritten (`--display-name` accepted and ignored with a warning). Imported rows are legacy rows (`social_account_id` NULL).
- New: `ai-smm project create`, `ai-smm credentials keygen`, `ai-smm accounts verify`, `ai-smm preflight worker`; `ai-smm user create-owner --create-project NAME` bootstraps an empty installation (user, project and owner membership in one transaction).
- No module loads a local `.env` at import; only the `ai-smm` and `ai-smm-worker` entry points do (`ai_smm.local_env`).
- `ai-smm schedule` and `ai-smm publish-now --live` apply the same route check as the API; `publish-now` publishes through the row's own route.

### 4.7 Knowledge and media isolation

- Knowledge stays files under `knowledge/projects/<id>/`, edited in git, read-only to the runtime and invisible to the API. `load_project_knowledge` validates the id and confines the path to `knowledge/projects/`.
- Revision image paths must be relative, without `..`, and under `knowledge/projects/<project_id>/assets/` of the item's project.
- Topics and tone of voice live in settings (DB); facts live in files. Agents do not read topics/tone yet (later stage).

---

## 5. Account-Aware Publishing (B2)

### 5.1 Route resolution

A **route** is what the worker needs to publish one row: which publisher, with which token, expecting which identity. The policy is `ai_smm.account_routing` (pure: rows and configuration, no decryption; the control plane may import it). The worker side, `ai_smm.publishing.routing.resolve_route`, adds the one step the control plane must never be able to take — opening the sealed token — and is unreachable from `ai_smm.api`/`ai_smm.application` (import-graph tests). The identity check lives in `ai_smm.platform_identity` (read-only `GET /me`):

| Publication | Route | Refused (permanent failure, no fallback) when |
|---|---|---|
| `social_account_id` set | **account route**: that account's active credential, decrypted with the worker's private key, AAD = (account id, credential id) | account missing, disabled, other project, other platform, no bound identity, no active credential, decryption/authentication fails, key not configured |
| `social_account_id` NULL | **legacy route**: deployment `THREADS_ACCESS_TOKEN` via the old factory | project not in `AI_SMM_LEGACY_THREADS_PROJECTS` (unset = none), platform ≠ threads |

The two routes use **two different factories**: `legacy_publisher_factory()` (zero-argument, the old one) and `account_publisher_factory(token)`. The account path has no reference to the legacy factory, so a fallback is not merely avoided but impossible to write without changing the function's signature. `ThreadsPublisher(access_token=…)` uses exactly the given token and never reads the environment when one is passed.

### 5.2 Legacy rows

Every publication existing before the migration has `social_account_id` NULL and keeps its meaning: the deployment token, **provided its project is allow-listed**. The release that deploys the new worker sets `AI_SMM_LEGACY_THREADS_PROJECTS=ai-catalog-consultant`. A project created through SMM-025B is never added implicitly. Forgetting the variable is caught before anything happens: the worker's **mandatory preflight** (`ai_smm.preflight`, run by `Worker.run()` before recovery, the scheduler and any claim; also `ai-smm preflight worker` for the operator) refuses to start with exit 78 when a pending legacy row belongs to a project outside the allow-list, an allow-listed project does not exist, the schema is not at `a7c2e5f19b40`, the deployment token is missing for live legacy rows, or a stored credential's key is not held. The existing queue is left exactly as it was ([migration-rollback.md §3a](../reports/smm-025b/migration-rollback.md#3a-worker-preflight-mandatory-fail-closed)).

### 5.3 Identity verification before the point of no return

For an account route in live mode, before any media upload or publish call: `GET /me?fields=id` with the decrypted token; the id must equal `social_accounts.external_account_id`.

| Result | Outcome |
|---|---|
| matches | continue |
| differs | **permanent failure** "token belongs to another Threads account"; nothing is sent |
| 401/403/400 | permanent failure "credential rejected"; owner replaces the token, admin reschedules |
| network error / 5xx | retryable failure (nothing has been sent) |

Dry-run mode resolves and decrypts the route (proving configuration) but makes no network call.

### 5.4 Scheduling check

`check_route_ready(publication)` — the same rules as §5.1 minus decryption and network — runs in `schedule_publication` (API schedule/reschedule) and in the CLI `schedule` / `publish-now`. It gives the person an early, precise refusal. The worker re-checks everything at publish time, because state can change in between (account disabled, credential revoked).

### 5.5 Series

- `attach_publication` refuses to put publications of different accounts into one series.
- `check_series_readiness` refuses a reply whose parent has a different `social_account_id` (a reply id is only meaningful inside its account).

### 5.6 Transition between the old and the new worker

| Situation | What happens | Why it is safe |
|---|---|---|
| Migration applied, **old** worker still running, only legacy rows | old worker publishes legacy rows exactly as before | new columns are nullable; old ORM never selects them |
| Old worker meets an **account-bound** scheduled row | its claim `UPDATE` is refused by trigger `trg_publications_account_claim_guard`; the cycle logs an error; nothing is published | the trigger requires `set_config('ai_smm.account_routing','on', true)` in the claiming transaction, which only the new `claim_due_publication` sets |
| New worker **started** with a legacy row of a non-allow-listed project pending | worker refuses to start (preflight, exit 78); nothing claimed, nothing failed | §5.2 |
| Legacy row of a non-allow-listed project scheduled **while** the new worker runs | refused by the scheduling check (API/CLI); if it got there anyway, permanent failure at route resolution, nothing sent | §5.1, §5.4 |
| New worker, account row, credential revoked while scheduled | preflight permanent failure | §5.1 |
| Downgrade attempted while an account-bound row is not terminal | migration downgrade **refuses** | after a downgrade those rows would become legacy rows and publish with the deployment token |
| Rollback of the image only (schema stays) | old worker + guard trigger as above | |

In production, account-bound rows cannot exist before business mutations are enabled (the API is read-only and `ai_smm_cc` cannot write), so the guard is defence in depth for the rollout window.

### 5.7 Queue, idempotency, retries, leases

Unchanged mechanisms: `FOR UPDATE SKIP LOCKED` claim, leases, `needs_review` for ambiguous outcomes, idempotency key and unique `threads_post_id`, retry back-off. The only queue change is the `set_config` line in the claim transaction. Retries of an account row resolve the route again each time — they can never switch to another account, because the account is a column of the row.

---

## 6. Security Model

### 6.1 Threats and controls

| # | Threat | Control | Test |
|---|---|---|---|
| T-1 | Post goes to the wrong account | destination in the approved revision; composite FK; route resolution per row; identity check; no fallback; old-worker guard | routing, identity mismatch, guard trigger, downgrade refusal |
| T-2 | Human approval bypassed | unchanged editorial invariants; account change = new revision; schedule needs an approved, materialised snapshot | approval invalidation, existing approval suites |
| T-3 | Token read back through the API | no route or schema field returns it; ciphertext NULL after replace/revoke | OpenAPI walk, response scans |
| T-4 | Token leaked in logs, audit, errors, Langfuse, test output | `SecretToken` masks repr/str; token never in audit details (fingerprint only); validation errors never echo values; redaction filter extended with stored-token values in the worker; Langfuse not on these paths | log capture + audit scan + error body scan for a sentinel token |
| T-5 | Compromised API / leaked API DB role decrypts tokens | API holds the public key only; private key file in worker; `ai_smm_cc` has no access to the new tables at all | grants unchanged (no grant on new tables) |
| T-6 | Ciphertext moved between rows in the DB | AEAD associated data = (account id, credential id) | swap test |
| T-7 | Cross-project access | row-first authorisation for accounts/credentials; composite FK; revision account must be in the item's project | isolation suites |
| T-8 | Token of another identity pasted by mistake | verification at entry binds/compares identity; worker re-checks | mismatch tests |
| T-9 | Private key lost | tokens are re-obtainable from Meta: re-enter them; backups of the key file are kept off-host separately from DB dumps (§7.3) | — |
| T-10 | Private key stolen together with a DB dump | full compromise of stored tokens: revoke tokens at Meta, rotate key, re-enter | runbook |

### 6.2 Key management

- `ai-smm credentials keygen --private-key-file PATH` writes a PKCS#8 X25519 key with mode 0600 (created with `O_EXCL`) and prints the public key and its key id.
- Worker/CLI: `AI_SMM_CREDENTIALS_PRIVATE_KEY_FILES` (comma-separated, for rotation). A file readable by group/others is refused.
- API: `AI_SMM_CREDENTIALS_PUBLIC_KEY` (base64, not secret).
- Rotation: generate a new pair; give the worker both private keys; switch the API to the new public key; replace each credential (re-enter tokens, or a future `rewrap` command); remove the old private key. `key_id` on every row shows what is still sealed with the old key.
- Recovery: back up the private key file off-host (encrypted, like the DB backups) **separately** from database dumps; a DB dump alone is useless to an attacker and a key alone has nothing to open.

---

## 7. Migration, Compatibility and Rollback

### 7.1 Migration `a7c2e5f19b40_social_accounts_credentials`

Additive: two tables, two nullable columns with foreign keys and indexes, one unique constraint target, one trigger function and trigger. No existing row is rewritten. Downgrade drops exactly these, after **refusing** if any account-bound publication is not `published`/`cancelled` (no override), and — because it would destroy accounts, credentials and every destination/attribution — refuses over existing account data unless `AI_SMM_DOWNGRADE_DESTROYS_ACCOUNT_DATA=yes`. With real account data, roll back the application and keep the schema.

### 7.2 Compatibility matrix

| Component | Old code on new schema | New code on old schema |
|---|---|---|
| Worker | legacy rows: unchanged; account rows: claim refused by the guard | not supported (new ORM selects new columns) — deploy migration first |
| Control Center API (`0cbd641`) | unchanged reads; account tables invisible to it | not supported |
| `ai_smm_cc` grants | this release grants `SELECT` on `social_accounts` and column `SELECT` on the **metadata** columns of `social_account_credentials` only (never `ciphertext`, `nonce`, `ephemeral_public_key`); the API code reads credentials with `credential_metadata_only()`, so it works under that grant. Verify file and its `cc-gate.sh` pin updated | the previous release's verify file fails on the new `SELECT`: re-apply its grants file before rolling the CC back |

### 7.3 Rollout and rollback

The full, ordered, reversible rollout is in [docs/reports/smm-025b/migration-rollback.md](../reports/smm-025b/migration-rollback.md). It is a future plan; nothing of it is executed in this sprint.

---

## 8. Decisions Taken (previous open decisions)

| # | Decision | Taken |
|---|---|---|
| OD-1 | Who creates projects | owner of ≥1 project |
| OD-2 | How a new project is kept off the deployment account | superseded: new projects publish only through their own accounts; the deployment token serves only allow-listed legacy projects |
| OD-3 | Production path before mutations | `ai-smm project create` CLI (a gated wrapper verb is a rollout item, not added in this sprint) |
| OD-4 | Importer | refuses unknown projects, stops overwriting metadata |
| D-5 | Account ownership | per project; same external account in two projects = two rows |
| D-6 | Destination binding | in the immutable revision and its hash |
| D-7 | Encryption | sealed box (X25519 + HKDF + AES-GCM), public key in the API, private key file in the worker |
| D-8 | Identity check | at credential entry and before each live publish |

Remaining open items are in [open-issues.md](../reports/smm-025b/open-issues.md).
