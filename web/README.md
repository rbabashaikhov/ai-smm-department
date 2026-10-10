# SMM Control Center (web)

Audience: whoever runs the operator UI locally or changes it.

Status: SMM-023, private-first MVP. Runs locally against the FastAPI
control plane. **Not deployed**, no public domain.

The UI is a client of the existing `/api/v1` and nothing else. It cannot
publish: there is no publish, publish-now or retry-publish control or
call anywhere, and a test scans both the rendered routes and the source
tree for one. Scheduling only moves a row inside the queue; the worker is
still the only process that sends anything to Threads.

## Stack

| | |
|---|---|
| Runtime | Node 24 (see `.nvmrc`; Vite 8 / Vitest 5 need ≥ 22.12) |
| UI | React 19, React Router 7 (declarative mode), plain CSS |
| Server state | TanStack Query 5 |
| Forms / UI state | React local state |
| Build | Vite 8, TypeScript 6.0 |
| Tests | Vitest 5, React Testing Library 16, jsdom |
| Lint | ESLint 10 + typescript-eslint + react-hooks |

No Redux, no SSR, no UI framework.

## Running it

```bash
# 1. The API, against a local database (never production):
export AI_SMM_DATABASE_URL=postgresql+psycopg://...@127.0.0.1:5432/ai_smm_dev
export AI_SMM_API_COOKIE_SECURE=false      # plain http only
export AI_SMM_API_MUTATIONS_ENABLED=true   # local DB only; default is read-only
uvicorn ai_smm.api.app:create_app --factory --host 127.0.0.1 --port 8000

# 2. The UI:
cd web
npm ci
npm run dev                                # http://127.0.0.1:5173
```

Vite proxies `/api` and `/health` to `AI_SMM_API_PROXY_TARGET`
(default `http://127.0.0.1:8000`), so the browser sees one origin, the
session cookie is first-party and there is no CORS configuration
anywhere. `VITE_API_BASE_URL` prefixes API calls; leave it empty for
same-origin, which is also the intended deployment shape (one reverse
proxy in front of both).

```bash
npm test            # vitest run
npm run typecheck   # tsc --noEmit
npm run lint        # eslint .
npm run build       # typecheck + production bundle in dist/
```

## Layout

```
src/
  api/          client.ts (the only fetch), errors.ts, endpoints.ts, queryKeys.ts, context.tsx
  features/     auth/ (bootstrap, login, logout, RBAC), content/, publications/, projects/
  components/   ConfirmDialog, ErrorBanner, SnapshotView, small UI pieces
  layouts/      AppLayout: sidebar, header, project switcher
  pages/        one component per route
  routes/       AppRoutes and the auth guard
  lib/          time (timezone-explicit scheduling), redact
  types/        mirrors of the API schemas
  test/         fake API server, fixtures, render helper
  tests/        integration tests per area
```

## Routes

| Route | Page |
|---|---|
| `/login` | email + password |
| `/projects` | projects with role, timezone, settings |
| `/projects/:projectId` | dashboard from `/operations/summary` + content totals |
| `/projects/:projectId/content` | content list, filters, pagination |
| `/projects/:projectId/content/new` | create item + first revision |
| `/projects/:projectId/publications` | publication list, filters, commands |
| `/projects/:projectId/attention` | `failed` / `needs_review` |
| `/projects/:projectId/audit` | audit trail (admin+) |
| `/content/:contentItemId` | revision, history, approval, materialize |
| `/publications/:publicationId` | immutable snapshot, preflight, attempts |

## Session and CSRF

- The session is the `smm_session` HttpOnly cookie. Page code never sees
  it; every request goes out with `credentials: "include"`.
- Bootstrap is `GET /auth/me` → `GET /auth/csrf` → protected routes. A
  401 means "not signed in" and leads to `/login`.
- Login is `POST /auth/login` → `GET /auth/me` → `GET /auth/csrf` →
  prefetch projects → redirect.
- The CSRF token lives in a private field of the `ApiClient` instance:
  in memory only, never in web storage, never in React state. It is sent
  as `X-CSRF-Token` on POST/PUT/PATCH/DELETE only, and not on login.
- Any later 401 clears the token and the query cache and returns to
  `/login`. After an explicit logout the next login starts at
  `/projects` rather than at the previous session's page.

## Errors

Every failure is an `ApiError` parsed from the API envelope, with
`status`, `code`, `details`, `requestId` and `retryAfter`. Typed guards
(`isVersionConflict`, `isStaleRevision`, `isPublicationNotEditable`,
`isInvalidStateTransition`, `isUnavailable`, …) narrow `details` to the
shape each code carries.

Reads retry a 503 (after `Retry-After`) or a network failure up to twice,
and nothing else. Commands are never retried automatically: after a 503
the confirm button stays disabled until `Retry-After` has passed and the
person presses it again.

## Rules the UI keeps

- **Commands are never optimistic.** The screen shows what the server
  answered, after it answered, and each success invalidates only the
  related queries.
- **Every consequential command is confirmed**: approve, reject,
  materialize, schedule, reschedule, cancel.
- **A revision is never edited.** "New Revision" POSTs a new revision
  with the `expected_item_version` read when the editor opened.
  `VERSION_CONFLICT` saves nothing and asks for a reload, with the unsaved
  text kept visible to copy.
- **Decisions name the revision on screen.** submit / approve / reject
  send the `revision_id` (and item version) frozen when the dialog
  opened. `STALE_REVISION` reloads the current revision.
- **Materialize is "Подготовить публикацию / Create delivery snapshot"**,
  never "Publish". `PUBLICATION_NOT_EDITABLE` shows the blocking
  publication, its status and the previous revision, with a link to it.
  Nothing cancels it automatically.
- **Scheduling shows the exact snapshot** (re-read when the dialog
  opens) and takes the time in the project's IANA timezone. It is sent
  with that zone's offset at that instant (`2026-10-12T09:00:00+03:00`).
  A wall time that does not exist in the zone (a DST gap) is refused
  rather than guessed.
- **RBAC is presentation only.** viewer: read-only. editor: content
  commands, no queue commands. admin/owner: everything, including
  schedule / reschedule / cancel and audit. The backend checks every
  request regardless.
- **A read-only deployment offers no write control at all.** When
  `GET /auth/me` reports `control_plane.mutations_enabled: false` — or
  does not report it — every role, owner included, sees only reads and a
  "Только чтение" notice. A `MUTATIONS_DISABLED` refusal is shown as
  read-only, not as a role problem.
- **The worker's mode is never guessed.** The dashboard shows what
  `/operations/summary` reports as `worker_mode`: `LIVE` or `DRY RUN` only
  with a source, otherwise `UNKNOWN` with a warning that a scheduled post
  may go out. The API's own `api_dry_run` is shown labelled as the API's.

## Known limits

- The API has no read endpoint for a content item's delivery snapshots
  (`content_publication_links`). The content page links to a snapshot
  after materializing or from a `PUBLICATION_NOT_EDITABLE` answer; a
  publication links back through its `source_ref` trace.
- A publication list row carries no `last_error`; the table reads the
  detail for rows that have had attempts (bounded by the page size).
- Approvals and revisions name users by id; there is no user directory
  endpoint, so the UI shows "you" or a short id.
