# SMM-025A — Multi-Project Control Center: Product & UI Design

| | |
|---|---|
| Project | PRJ-013 AI SMM Department 2.0 |
| Task | SMM-025A — the first technical stage of SMM-025 (Multi-project Control Center) |
| Document status | **Revision 3 — aligned with the updated PRJ-013 roadmap, 2026-10-13.** §2 lists the decisions in force and the questions deferred to later roadmap items |
| Baseline | `main` at `8f1689edff32b42d5050d6009cabd429f3ab84bd` |
| Builds on | The SMM-025A technical audit (accepted): defects, F1–F8, J1–J9, AC-A1…AC-A10 — kept unchanged |
| Supersedes | Revisions 1 and 2 of this file (never committed). Their workspace, brand, author-catalogue, channel and connector designs, the future-capabilities backlog and the integration-principles list were removed as contradicting the updated concept or premature |
| Implementation | **Not started.** F1–F8 are implemented only after a separate permission from the owner |
| Mode | Documentation only. No application code, schema, worker, deployment or production change |

---

## 1. Executive Summary

**What the product is.** AI SMM Department 2.0 is an **internal platform for one owner** who runs several lines of activity, blogs and experiments.
- **Project** is the main business entity.
- Each project has its own topic, description, style, knowledge base, content and publishing settings.
- There is no Workspace and no Brand Profile entity.
- Authors and authorial style are project settings for now.

**What SMM-025A is.** The first technical stage of SMM-025: the frontend improvements F1–F8 from the accepted audit.
- Safe project scoping and switching.
- Correct navigation.
- A Projects Overview.
- Distinct loading, empty and error states.
- Inactive-project indication.
- A deployment-wide worker-mode banner.
- Breadcrumbs.
- Automated tests for J1–J9.

There are no new business mutations and no backend change. ProjectMembership, RBAC and server-side data isolation stay as they are.

**What comes later** follows the PRJ-013 roadmap, SMM-025…SMM-032 (§9). The future modes — Manual, AI-Assisted and Autopilot — share one Content Lifecycle. In all of them:
- human approval is mandatory;
- publishing is done only by the existing worker through the PostgreSQL queue.

**Verdict:** F1–F8 are ready for implementation and wait for the owner's permission. Nothing blocks them.

---

## 2. Decisions and Deferred Questions

### 2.1 Decisions in force

| # | Decision | Source |
|---|---|---|
| D-1 | AI SMM Department 2.0 is an internal platform for one owner with several lines of activity, blogs and experiments. No SaaS, no client organisations | Owner, 2026-10-13 |
| D-2 | **Project** is the main business entity. Each project has its own topic, description, style, knowledge base, content and publishing settings. No separate Workspace or Brand Profile | Owner, 2026-10-13 |
| D-3 | Authors and authorial style are **project settings**. A separate author model is introduced only when there is a practical need | Owner, 2026-10-13 |
| D-4 | ProjectMembership, RBAC and server-side data isolation are kept as they are | Owner, 2026-10-13 |
| D-5 | SMM-025A is the first technical stage of SMM-025. Its scope is F1–F8 / J1–J9 / AC-A1…AC-A10, without extension. Implementation only after a separate permission | Owner, 2026-10-13 |
| D-6 | No new business mutations in SMM-025A | Owner, 2026-10-13 |
| D-7 | The roadmap is SMM-025…SMM-032 as listed in §9 | Owner's task manager, as relayed on 2026-10-13 |
| D-8 | Manual, AI-Assisted and Autopilot are modes on one common Content Lifecycle (§4.3) | Owner, 2026-10-13 |
| D-9 | No publication without explicit human approval. `full_auto` (in the sense of bypassing approval) stays reserved | Owner, 2026-10-13; [ADR automation-and-publication-safety](../adr/automation-and-publication-safety.md) |
| D-10 | Publications are sent only by the existing worker through the PostgreSQL queue. No n8n | Owner, 2026-10-13; existing architecture |
| D-11 | External tools may be connected through **separate adapters** when a concrete task appears. Nothing is designed for them in advance | Owner, 2026-10-13 |
| D-12 | Production business mutations stay off until FU-024C1-5 (with FU-024C2-1/2/3) is closed and the owner approves the switch separately | [follow-ups.md](../follow-ups.md) |
| D-13 | Existing URLs are kept. Detail pages stay flat (`/content/:id`, `/publications/:id`) and take the project from the row | Part of F1–F8 |

### 2.2 Not designed now (explicitly out of scope)

- SaaS, client organisations, Workspace / Brand hierarchy.
- Global author catalogues.
- Tool Registry, Plugin SDK, a universal connector set.
- Integrations with Trendsee, LiveDune or other third-party platforms.
- New tables or APIs for hypothetical capabilities.

### 2.3 Deferred questions (tied to roadmap items; none blocks SMM-025A)

| # | Question | Belongs to |
|---|---|---|
| Q-1 | What Autopilot automates within D-9. For example: generation, submission for review, choosing a time for an already approved item. Also how the names Manual / AI-Assisted / Autopilot map to the ADR's `manual` / `semi_auto` / reserved `full_auto` | SMM-032 |
| Q-2 | How a second project is created and how the existing owner gets a membership in it. Today `create_owner` creates a new user and fails on an existing email, and projects are created only by `ai-smm import` (S8) | SMM-025 (after 025A) |
| Q-3 | Where project style and authors are stored: the existing `project_settings` JSON sections, or something else. How generation reads them (today nothing reads them, S6) | SMM-025 (after 025A) |
| Q-4 | What `is_active = false` means. Today it is display-only and an inactive project still publishes (S9) | SMM-025 / SMM-029 |
| Q-5 | Credentials and rate limits for more than one account or platform. Today: one `THREADS_ACCESS_TOKEN` and one deployment-wide cap (S1, S2) | SMM-028 |
| Q-6 | The future of the legacy CLI `ai-smm approve`, which sets `human_reviewed` outside the editorial layer (S10) | SMM-029 or before enabling approvals in production |
| Q-7 | What SMM-025 contains after SMM-025A | Owner |

---

## 3. Current State

Checked in the repository at the baseline. Production was not accessed.

### 3.1 What is already multi-project

- **Data.**
  - `projects` and `project_memberships` (unique per `(project_id, user_id)`; roles viewer < editor < admin < owner).
  - `project_settings` (three versioned JSONB sections).
  - `project_id` on `publications`, `content_series` and `content_items`.
  - Same-project checks on editorial-to-delivery links.
  - [models.py](../../src/ai_smm/db/models.py)
- **API.**
  - Every project route uses `require_project_role`. Row-addressed routes load the row, then authorize against the row's project.
  - A non-member gets 404, the same as a missing id; an insufficient role gets 403.
  - [dependencies.py](../../src/ai_smm/api/dependencies.py), [test_api_projects_rbac.py](../../tests/test_api_projects_rbac.py)
- **UI.**
  - Routes `/projects/:projectId/{,content,content/new,publications,attention,audit}`.
  - A switcher in the top bar.
  - Query keys grouped per project.
  - Role-aware controls that are not rendered at all on a read-only deployment.
  - [AppRoutes.tsx](../../web/src/routes/AppRoutes.tsx), [AppLayout.tsx](../../web/src/layouts/AppLayout.tsx), [queryKeys.ts](../../web/src/api/queryKeys.ts), [rbac.ts](../../web/src/features/auth/rbac.ts)

### 3.2 Findings from the audit (addressed by F1–F8)

1. **Confirmed defect: state and data carry over between projects.**
   - Project routes are siblings, so `/projects/A/content` → `/projects/B/content` reuses the same component instance.
   - With `placeholderData: keepPreviousData`, A's rows can render under B's URL while B is loading.
   - Local state (pagination, filters, drafts) carries over as well.
   - Display only; the backend is not affected.
2. **No access state for an unknown or forbidden project.**
   - The Dashboard shows two "Не найдено" banners.
   - The sidebar still shows that project's nav.
   - The switcher's value matches no option.
3. Switching always lands on the Dashboard, so the current section is lost.
4. A single-project user still has to pick the project.
5. No cross-project overview.
6. The worker-mode banner looks per-project but is deployment-wide.
7. Inactive projects are not marked in the switcher (`/auth/me` has no `is_active`; `GET /projects` has).
8. "No results for this filter" and "project is empty" look the same.

### 3.3 Runtime facts that SMM-025A does not change

These are recorded so the UI does not hide them and later roadmap items start from facts. They are not requirements.

| # | Fact | Evidence |
|---|---|---|
| S1 | One Threads account per deployment | `os.getenv("THREADS_ACCESS_TOKEN")` — [publishing/threads.py:62](../../src/ai_smm/publishing/threads.py#L62) |
| S2 | The worker publishes `platform="threads"` only; its daily cap and spacing are global across projects | [worker.py:265](../../src/ai_smm/worker.py#L265) |
| S3 | One media upload target (`STORAGE_*`) | [storage.py](../../src/ai_smm/storage.py) |
| S4 | Knowledge is files in `knowledge/projects/<id>/` plus one global `editorial_policy.md`. The repository has one project, `ai-catalog-consultant` | [knowledge/loader.py:41](../../src/ai_smm/knowledge/loader.py#L41) |
| S5 | The AI pipeline runs from the CLI only and reaches PostgreSQL through `ai-smm import` as publications, bypassing content items | [importer.py:139](../../src/ai_smm/importer.py#L139), [graph.py](../../src/ai_smm/graph.py) |
| S6 | `project_settings` sections (`content_config`, `publishing_config`, `brand_config`) are stored but read only by the settings API | [application/projects.py](../../src/ai_smm/application/projects.py) |
| S7 | A project is created only by `ensure_project` during `ai-smm import` | [queue.py:65](../../src/ai_smm/queue.py#L65) |
| S8 | A membership can only be created together with a new owner user | [users.py:65](../../src/ai_smm/application/users.py#L65) |
| S9 | `projects.is_active` is display-only | Not read by the worker, queue or services |
| S10 | Two approval paths: editorial (`content_approvals`, human `actor_user_id NOT NULL`) and the legacy CLI `approve` on publications | [cli.py:375](../../src/ai_smm/cli.py#L375) |
| S11 | No engagement analytics; only queue counters | `/operations/summary`, CLI `stats` |
| S12 | Agents create `ChatOpenAI` when their module is imported | [agents/copywriter.py:48](../../src/ai_smm/agents/copywriter.py#L48), editor, strategist |

### 3.4 Constraints in force

- Production is read-only for business operations (`AI_SMM_API_MUTATIONS_ENABLED=false`, least-privilege role `ai_smm_cc`).
- Any further production migration while the queue can be written is gated by FU-024B4A-1.
- The worker, the scheduler and the queue semantics are not changed in SMM-025A.

---

## 4. Product Model

### 4.1 Project-centric model (existing storage, no new tables)

```
owner (user) ──< project_memberships (role) >── PROJECT
                                                  ├── identity: display_name, description, timezone, language
                                                  ├── project_settings  (content / publishing / brand JSON, versioned)
                                                  ├── knowledge         (files: knowledge/projects/<id>/)
                                                  ├── content_items ──< content_revisions ──< content_approvals (human)
                                                  │         └──< content_publication_links
                                                  ├── content_series
                                                  ├── publications ──< publication_attempts   (queue: worker only)
                                                  └── audit view (audit_log, filtered by subject)
deployment-wide: worker mode, mutation gate, Threads account (S1), rate limits (S2)
```

### 4.2 What a project consists of, and where it lives today

| Project aspect | Stored in today | Status |
|---|---|---|
| Topic, description | `projects.display_name`, `projects.description` | EXISTING |
| Timezone, language, default platform | `projects.default_timezone`, `default_language`, `default_platform` | EXISTING |
| Style, tone, authors and authorial style | `project_settings.brand_config` / `content_config` (free JSON, versioned) | PARTIAL: stored, not read by generation (S6). Storage choice is Q-3 |
| Knowledge base | `knowledge/projects/<id>/` (`projects.knowledge_path`) | EXISTING as files; no UI (S4) |
| Content | `content_items`, `content_revisions`, `content_approvals`, `content_series` | EXISTING |
| Publishing settings | `projects.default_platform`, `project_settings.publishing_config`; deployment-wide account and limits | PARTIAL (S1, S2, S6) |
| Access | `project_memberships` + RBAC | EXISTING (D-4) |
| Active / inactive | `projects.is_active` | Display-only (S9, Q-4) |

- Deployment-wide facts (worker mode, the single Threads account, rate limits) are shown as such, never as project properties (F6).
- With one owner, the owner is the member of every project. Other roles exist and are kept (D-4), but no extra role model is designed.

### 4.3 Common Content Lifecycle (existing; all modes use it)

```
draft revision (immutable; source: human | strategist | copywriter | editor | import | future agents)
   → submit → in_review
   → HUMAN approval of that exact revision (content_approvals)            ← mandatory in every mode
   → materialize: immutable Publication snapshot (approved)
   → schedule → worker claims (SKIP LOCKED, lease) → attempt → published | failed | needs_review
```

| Mode (future) | Who produces drafts | Who approves | Who publishes |
|---|---|---|---|
| Manual | A person | A person | The worker, at the scheduled time |
| AI-Assisted | Agents on request; a person edits | A person | The worker, at the scheduled time |
| Autopilot (SMM-032) | Agents, possibly on a schedule | **A person** | The worker, at the scheduled time |

Exactly what Autopilot automates beyond drafting is Q-1. It never includes approval, and it never publishes outside the worker and the queue.

---

## 5. Information Architecture (SMM-025A)

### 5.1 Routes

Existing, kept. F1 only regroups the project routes under one layout route; no URL changes.

```
/login
/                                   → F2: one project → /projects/:id, otherwise → /projects
/projects                           Projects Overview (F4)
/projects/:projectId                ┐
/projects/:projectId/content        │  wrapped by ProjectScope (F1):
/projects/:projectId/content/new    │  access check, identity strip,
/projects/:projectId/publications   │  <Outlet key={projectId}/>
/projects/:projectId/attention      │
/projects/:projectId/audit          ┘  (admin+)
/content/:contentItemId             row-addressed; breadcrumb from row (F7)
/publications/:publicationId        row-addressed; breadcrumb from row (F7)
```

No new sections are added in SMM-025A. Project sections added by later stages, such as project settings or knowledge, belong under `/projects/:projectId/…` inside `ProjectScope`.

### 5.2 Project switching (F3)

| From | To after choosing project B |
|---|---|
| A's Dashboard, Content, Publications, Attention | Same section in B, query filters dropped |
| A's Audit | B's Audit if the role in B is admin+, otherwise B's Dashboard |
| `/content/new`, `/content/:id`, `/publications/:id` | B's Dashboard |
| `/projects` | B's Dashboard |

- The switcher offers only projects from `GET /projects` (falling back to `/auth/me` while that loads), with an "(неактивен)" label for `is_active = false`.
- It is disabled when the user has no projects.
- Switching remounts the project subtree, so no A data or state renders under B.

### 5.3 Breadcrumbs (F7)

```
Projects › Demo Project › Content › <title>
Projects › Demo Project › Publications › #128
```

On a detail page the project segment comes from the loaded row, and is shown only after the row loads. A row that fails to load shows no project segment.

### 5.4 No access and not found

| Situation | Server | UI |
|---|---|---|
| `/projects/X`: X unknown or no membership | 404 | One panel "Проект не найден или у вас нет доступа" and a link to Projects. No sub-resource request. The sidebar and switcher do not adopt X |
| Row in another project | 404 | Existing "Не найдено". The active project is unchanged |
| Role too low (Audit) | 403 | Hidden from nav (existing). A direct URL shows the existing role message |
| Session expired | 401 | Existing: cache cleared, redirect to login |
| Read-only deployment | 403 `MUTATIONS_DISABLED` | Existing: no write controls, read-only banner |

---

## 6. User Journeys (SMM-025A: J1–J9)

| # | Journey | Expected result |
|---|---|---|
| J1 | User with 0 projects logs in | Empty state with a hint to contact the owner. Switcher disabled. No project API calls |
| J2 | User with 1 project logs in | Lands on that project's Dashboard |
| J3 | User with several projects logs in | Projects Overview with per-project counters |
| J4 | Switch from A's Publications (filtered) to B | Lands on B's Publications, filters dropped. A's rows are never shown |
| J5 | Switch from a detail page | Lands on B's Dashboard |
| J6 | Switch from A's Audit (admin) to B (viewer) | Lands on B's Dashboard. Audit hidden |
| J7 | Open an unknown or forbidden project URL | One no-access panel. No sub-requests. Active project unchanged |
| J8 | Open an empty project, read-only deployment | "Project is empty" state. No create action |
| J9 | Back/forward between two projects' same section | Each shows only its own data. Local state resets |

Journeys for later roadmap items are written when those items are specified.

---

## 7. SMM-025A Technical Plan — F1–F8 (unchanged)

### 7.1 Changes

| # | Change |
|---|---|
| F1 | `ProjectScope` layout route: access check through `GET /projects/{id}`, project identity strip, `<Outlet key={projectId}/>` |
| F2 | Index redirect: exactly one project goes to its Dashboard, otherwise to `/projects` |
| F3 | Switcher fed by `GET /projects`: inactive marker, keeps the current section, drops filters, disabled when the user has no projects |
| F4 | Projects page becomes an overview with per-project `/operations/summary` counters; one failing row does not break the page |
| F5 | Separate empty states for "no results for this filter" and "project is empty" |
| F6 | Worker-mode banner says it is deployment-wide |
| F7 | Project breadcrumb on detail pages |
| F8 | Two-project fixtures and tests for J1–J9 |

### 7.2 Files

| # | Files |
|---|---|
| F1 | `web/src/routes/AppRoutes.tsx`, new `web/src/features/projects/ProjectScope.tsx` |
| F2 | `web/src/routes/AppRoutes.tsx` |
| F3 | `web/src/layouts/AppLayout.tsx` |
| F4 | `web/src/pages/ProjectsPage.tsx` |
| F5 | `web/src/pages/ContentListPage.tsx`, `web/src/pages/PublicationsPage.tsx`, `web/src/pages/DashboardPage.tsx` |
| F6 | `web/src/pages/DashboardPage.tsx` |
| F7 | `web/src/pages/ContentDetailPage.tsx`, `web/src/pages/PublicationDetailPage.tsx` |
| F8 | `web/src/test/fixtures.ts`, new `web/src/tests/multiProject.test.tsx` |

Not touched:
- `AuthProvider`, `api/client.ts`, CSRF handling, `rbac.ts`, `endpoints.ts` (every endpoint needed already exists);
- every mutation path;
- the whole backend.

### 7.3 Wireframes

**Shell with project scope (F1, F3, F6)**

```
┌──────────────────────┬───────────────────────────────────────────────────────────┐
│ SMM Control Center   │ Проект: [ Demo Project ▾ ] (owner)        user  [Выйти]   │
│                      ├───────────────────────────────────────────────────────────┤
│ Проекты              │ [Только чтение …]                       (existing banner)  │
│ ── Demo Project ──   │ ┌ Demo Project · demo · threads · Europe/Moscow · owner ┐ │
│  Dashboard           │ │ [неактивен]  (only when is_active = false)             │ │
│  Content             │ └────────────────────────────────────────────────────────┘ │
│  Publications        │  <page, remounted when the project changes>               │
│  Attention           │                                                           │
│  Audit*              │                                                           │
└──────────────────────┴───────────────────────────────────────────────────────────┘
 * admin and owner only (unchanged)
```

**Projects Overview (F4)**

```
┌ Проекты — проекты, в которых у вас есть роль ──────────────────────────────────┐
│ Проект            Роль    Attention  Due now  Next scheduled   Published 24h      │
│ Demo Project      owner   ⚠ 2        1        14 окт 09:00     3        [Open →]  │
│   demo · threads · Europe/Moscow                                                  │
│ Client X          editor  0          0        —                0        [Open →]  │
│ Old Campaign [неактивен]  viewer  —  (counters unavailable)             [Open →]  │
│ ▸ Settings (collapsed, as today)                                                  │
└───────────────────────────────────────────────────────────────────────────────────┘
```

- Each row loads its summary independently. A failure shows "—" on that row only.
- 0 projects: the J1 empty state.

**No access (F1)**

```
┌──────────────────────────────────────────────────────┐
│ Проект не найден или у вас нет доступа.              │
│ [← К списку проектов]                                │
└──────────────────────────────────────────────────────┘
```

**Worker-mode banner (F6), on the Dashboard**

```
Режим worker: UNKNOWN (общий для всего деплоя, не для проекта). …existing warning…
```

**Empty states (F5)**

```
No results for this filter:  "Ничего не найдено по выбранным фильтрам."  [Сбросить фильтры]
Project is empty:            "В проекте пока нет материалов."  (+ Create only if allowed and not read-only)
```

### 7.4 State rules

| State | Rule |
|---|---|
| Loading | Skeleton or the existing loader. Never data from another project; placeholder data only within the same project |
| Empty | The two distinct messages above |
| Error | Existing `ErrorBanner` with retry. On the overview, one row's error is shown in that row |
| Read-only | Write controls not rendered (existing). Read-only banner (existing) |
| No access | One panel (F1), no sub-requests |

### 7.5 Acceptance criteria (unchanged from the audit)

| # | Criterion |
|---|---|
| AC-A1 | **No cross-project display.** After navigating from project A to B on the same section (switcher, back/forward, URL), no A row is ever rendered under B. Checked with a delayed B response. Pagination and filter state resets |
| AC-A2 | **The switcher keeps the section** for dashboard, content, publications and attention (and audit when the role allows). It drops filters. It goes to the Dashboard from detail pages and `content/new`, and from Audit when the role in B is below admin |
| AC-A3 | **Unknown or forbidden `/projects/X`:** exactly one no-access message and a link to Projects. Zero requests to `/projects/X/*` sub-resources. The sidebar does not adopt X |
| AC-A4 | **Login landing:** 0 projects → empty state, switcher disabled. 1 → its Dashboard. ≥2 → the overview with per-project counters |
| AC-A5 | **The overview tolerates partial failure:** a failing summary shows "—" on that row only |
| AC-A6 | **Inactive projects** are labelled in the switcher, the overview and the identity strip |
| AC-A7 | **Empty states:** "no results for this filter" and "project is empty" are distinct. No create action on a read-only deployment |
| AC-A8 | **No new write paths:** no new POST or PATCH. `readOnly.test.tsx` and `safety.test.tsx` pass unchanged. Read-only banner and server gate unchanged |
| AC-A9 | **Authorization unchanged:** no backend diff. `/auth/me`, CSRF and RBAC unchanged. No project id in any request body |
| AC-A10 | **Quality gates:** `npm run lint`, `tsc` and `vitest run` pass. New tests wait with `waitFor`/`findBy` (FU-024B2-1) |

### 7.6 Estimate and risks

- **Size:** S–M, about 1–1.5 days, frontend only.
- **Risk — route regrouping (F1):** may break existing route tests. Mitigation: URLs stay identical.
- **Risk — flaky Vitest (FU-024B2-1):** new tests must not add to it.
- **Risk — overview cost (F4):** one summary request per project. Fine for the expected handful of projects.
- **Production limitation:** a second project and the owner's membership in it cannot be created with existing tools (Q-2). Until then production shows the single-project path (J2). J3–J6 and J9 are proven by tests.

### 7.7 Implementation defaults

These apply unless the owner says otherwise.

| Detail | Default |
|---|---|
| Where the access check takes the project from | `GET /projects/{id}`, not `/auth/me` (fresher; one extra round trip) |
| Switcher source | `GET /projects`, falling back to `me.projects` while loading |
| Inactive projects in the switcher | Listed, labelled "(неактивен)", not hidden |
| Overview columns | Role, Attention, Due now, Next scheduled, Published 24h; settings collapsed |

---

## 8. Security Model

### 8.1 Principles kept

| Principle | Enforced by |
|---|---|
| Every operation has a verifiable project scope | `require_project_role` and the row-first dependencies (row → project → membership → role). The request body never names a project |
| The backend is the source of truth for authorization | Per-request RBAC. The UI's `permissions()` is presentation only |
| No disclosure of another project's data on switching, not even briefly | Server: 404 for non-members. UI after F1: remount per project, per-project query keys, no placeholder data across projects |
| The UI does not replace server-side RBAC | Hidden controls are backed by server 403 / `MUTATIONS_DISABLED` |
| No publication without explicit human approval (D-9) | `content_approvals` (human actor, append-only), approval only in effect on the current revision, materialize from the approved revision only |
| Only the worker publishes, through the PostgreSQL queue (D-10) | Unique `idempotency_key`, `FOR UPDATE SKIP LOCKED`, lease, attempts, `needs_review` for unknown outcomes. No publish endpoint in the API |
| Worker and scheduler unchanged; mode changes and production mutations need separate owner approval | D-6, D-12 |

### 8.2 What SMM-025A changes in security terms

Nothing on the server. SMM-025A removes the transient cross-project display (F1) and adds a no-access state that sends no sub-requests (F1). Authentication, CSRF, RBAC, the mutation gate, the worker and the queue are untouched.

---

## 9. Roadmap Context (PRJ-013)

Source: the owner's task manager, as relayed on 2026-10-13. This document specifies only SMM-025A. The other items are listed for orientation; none is designed here.

| Item | Title | Relation to this document |
|---|---|---|
| SMM-025 | Multi-project Control Center | SMM-025A (F1–F8) is its first technical stage. Later contents: Q-7 (likely candidates are Q-2 and Q-3) |
| SMM-026 | Image Agent | A future content source. Its output enters the common lifecycle as draft revisions (§4.3) |
| SMM-027 | Video Agent | Same rule as SMM-026. Video is not among the current publication formats (`text`, `image`, `carousel`, `thread`) |
| SMM-028 | Multi-channel publishing and Feedback Loop | Starts from S1, S2 and S11 and from Q-5. Publishing stays with the worker through the PostgreSQL queue |
| SMM-029 | Production reliability | Related open items are in [follow-ups.md](../follow-ups.md). Also candidate for Q-4 and Q-6 |
| SMM-030 | Research into reusing existing work | — |
| SMM-031 | Social Insights API Research | Research only. Feeds SMM-028's feedback loop |
| SMM-032 | Autopilot MVP | Manual / AI-Assisted / Autopilot on the common lifecycle (§4.3), within D-9 and D-10. Q-1 |

**Rules that hold for every item above:**
1. Every new content source writes **draft** revisions and goes through human approval.
2. Only the worker publishes, through the PostgreSQL queue.
3. No n8n and no new orchestration engine.
4. Everything stays project-scoped and authorized on the server.
5. **External tools** are connected through **separate adapters** when a concrete task appears (D-11). No registry, SDK, universal connectors or speculative tables and APIs are created ahead of that task.

---

## 10. Gap Analysis (SMM-025A)

| Component | Class | Evidence | Covered by |
|---|---|---|---|
| Project scoping across pages | EXISTING (with defect) | [AppRoutes.tsx](../../web/src/routes/AppRoutes.tsx), per-page `useParams` + `keepPreviousData` | F1 |
| Access check and no-access state | MISSING (UI); server EXISTING | `require_project_role` → 404 | F1 |
| Single-project landing | MISSING | Index → `/projects` | F2 |
| Switcher: section, inactive, disabled | PARTIAL | [AppLayout.tsx:65-83](../../web/src/layouts/AppLayout.tsx#L65-L83) | F3 |
| Projects Overview with counters | PARTIAL | [ProjectsPage.tsx](../../web/src/pages/ProjectsPage.tsx) | F4 |
| Distinct empty states | PARTIAL | "Ничего не найдено." for both | F5 |
| Deployment-wide worker mode | PARTIAL | [DashboardPage.tsx:29-65](../../web/src/pages/DashboardPage.tsx#L29-L65) | F6 |
| Breadcrumbs on detail pages | PARTIAL | Project id link on Content Detail only | F7 |
| Multi-project tests | MISSING | Fixtures have one project ([fixtures.ts](../../web/src/test/fixtures.ts)) | F8 |
| Creating a second project and membership | MISSING (S7, S8) | `ensure_project`, `create_owner` | Not in SMM-025A (Q-2) |
| Project style and authors used by generation | PARTIAL (S6) | `project_settings` stored, unread | Not in SMM-025A (Q-3) |

---

## 11. Open Questions

None blocks SMM-025A. The deferred questions Q-1…Q-7 (§2.3) belong to later roadmap items. Implementation defaults are in §7.7.

---

## 12. Acceptance Criteria

### 12.1 This document

1. F1–F8, J1–J9 and AC-A1…AC-A10 are unchanged from the audit.
2. The product model is project-centric. There are no Workspace or Brand Profile entities, and authors and style are project settings.
3. The roadmap SMM-025…SMM-032 is recorded; only SMM-025A is specified.
4. Manual, AI-Assisted and Autopilot are placed on the common Content Lifecycle, with mandatory human approval and worker-only publishing.
5. No SaaS, workspace or brand hierarchy, author catalogue, registry or SDK, connector set, third-party integration, or speculative tables and APIs are designed.
6. No application code, schema, worker or deployment change was made to produce it.

### 12.2 SMM-025A implementation

AC-A1…AC-A10 in §7.5, after the owner's separate permission to start.

---

## Appendix — Verification notes

**Checked in the repository at the baseline (read-only):**
- `web/src` routes, layout, pages, hooks, endpoints and fixtures.
- The API routers, dependencies and schemas.
- `db/models.py`.
- `application/{projects,users,content}.py`.
- `worker.py`, `queue.py`, `publishing/threads.py`, `storage.py`, `knowledge/loader.py`, `importer.py`.
- Agents.
- `cli.py`, `config.py`.
- `docs/api.md`, `docs/follow-ups.md`, the automation ADR.

**Not verified:**
- Anything in production: number of projects, users and memberships; live configuration.
- The roadmap list in §9: taken from the owner's message; the task manager itself was not accessed.

No code was executed and no tests were run for this document.
