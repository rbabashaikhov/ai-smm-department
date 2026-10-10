# HTTP API (control plane)

Audience: whoever runs the API locally or reviews how it is wired.

Status: SMM-022A (identity, membership, project settings), SMM-022B
(reading the existing publishing core and three safe queue commands) and
SMM-022C (content items, immutable revisions, human approval, and the
bridge that materialises an approved revision into a Publication).
The API exists, is covered by tests and is **not deployed**. There is no
compose service for it yet and no migration has been applied to the
production database. SMM-022B needed no migration at all: it reads and
moves rows in the schema the worker already had.

---

## 1. The rule that shapes everything else

**The API is the control plane. The worker is the execution plane.**

The API reads and edits the metadata around the publication queue:
who may sign in, which projects they may touch, and the editorial
settings of a project. It does not publish, and it is not allowed to:

- there is no `POST /publish`, no `POST /threads/publish` and no
  `publish-now` endpoint, and a test asserts that no route path contains
  the word;
- no module under `src/ai_smm/api/` imports `ai_smm.publishing` or
  `ai_smm.worker`, and a test asserts that on the import graph;
- the same test walks `src/ai_smm/application/` too, where the one
  permitted edge into the publishing layer is the pure content validator
  the preview reuses (`validate_ready_to_publish` and the exception it
  raises). Everything that can create a post — `ThreadsPublisher`,
  `publish_claimed_publication`, `claim_due_publication` — is banned from
  both packages, by import and by name.

The three commands the API does have — schedule, reschedule, cancel —
move a row *inside* the queue. They set `scheduled_at`, or take the row
out; the worker still decides when anything is sent, and it still has to
claim the row first.

A publication reaches Threads exactly one way: the worker claims a due
row with `FOR UPDATE SKIP LOCKED`, writes an attempt row, and calls the
Threads API. Keeping that path single is what makes the duplicate
barriers (idempotency key, unique `threads_post_id`, refusal to re-publish
a row that already has one) meaningful. A second process that could
publish would make all three advisory.

---

## 2. Running it locally

```bash
# A database the API may talk to. The schema comes from Alembic.
export AI_SMM_DATABASE_URL=postgresql+psycopg://ai_smm:...@127.0.0.1:5432/ai_smm
alembic upgrade head

# A browser will not return a Secure cookie over plain http.
export AI_SMM_API_COOKIE_SECURE=false

uvicorn ai_smm.api.app:create_app --factory --host 127.0.0.1 --port 8000
```

`create_app()` is a factory on purpose: importing the module opens no
connection and makes no outbound call, so `--reload`, a test and a
migration shell can all import it safely.

Interactive schema: `http://127.0.0.1:8000/docs`.

For the packaged runtime (API image, web reverse proxy, single origin)
see [runtime-packaging.md](runtime-packaging.md).

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `AI_SMM_DATABASE_URL` | — | required; the same database the worker uses |
| `AI_SMM_API_SESSION_LIFETIME_SECONDS` | `604800` (7 days) | absolute session lifetime, fixed at login |
| `AI_SMM_API_SESSION_IDLE_SECONDS` | `86400` (24 hours) | idle timeout, measured from the last request |
| `AI_SMM_API_COOKIE_SECURE` | `true` | `Secure` on the session cookie; set `false` only for local http |
| `AI_SMM_API_DOCS_ENABLED` | `true` | serve `/docs`, `/redoc`, `/openapi.json`; the packaged runtime sets `false` (they answer 404 JSON) |
| `LOG_LEVEL`, `LOG_FORMAT` | `INFO`, `json` | as for the worker |

No new secret is introduced, and there is no signing key or CSRF secret
to manage, because nothing is signed and nothing extra is stored:

- the raw session token exists only in the `smm_session` HttpOnly cookie;
- `user_sessions` stores only its SHA-256 digest, so the table cannot be
  replayed as a login;
- the CSRF token is not stored in the database at all. It is derived
  deterministically from the raw session token as
  `HMAC-SHA256(session_token, "ai-smm-csrf-v1")` — the domain separator
  is the message — and recomputed from the cookie on every request.

---

## 3. Creating the first user

There is **no public registration**. A user is created at the console,
against an existing project:

```bash
ai-smm user create-owner \
  --email operator@example.com \
  --display-name "Operator" \
  --project my-project
```

The command asks for the password twice with `getpass`, so it never
appears in an argument, in the shell history or in a process listing.
There is no default password. It then, in one transaction:

- normalises the address (trim, lowercase) and refuses a duplicate;
- hashes the password with Argon2id;
- creates the user and an `owner` membership of that project;
- writes an audit entry as actor `system:bootstrap`.

It prints the new user id and nothing secret. A user without a membership
would be able to sign in and see nothing, so neither half is allowed to
land alone.

---

## 4. Login flow

```bash
BASE=http://127.0.0.1:8000

# 1. Log in. The response sets the smm_session cookie and hands back the
#    CSRF token for this session.
curl -s -c jar.txt -X POST $BASE/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"operator@example.com","password":"..."}'

# 2. Safe requests need the cookie only.
curl -s -b jar.txt $BASE/api/v1/auth/me

# 3. The CSRF token again, when a tab does not have it. Always the same value.
curl -s -b jar.txt $BASE/api/v1/auth/csrf

# 4. Unsafe requests need the cookie and the header.
curl -s -b jar.txt -X PATCH $BASE/api/v1/projects/my-project/settings \
  -H 'Content-Type: application/json' -H "X-CSRF-Token: $TOKEN" \
  -d '{"expected_version":0,"content_config":{"tone":"dry"}}'

curl -s -b jar.txt -X POST $BASE/api/v1/auth/logout -H "X-CSRF-Token: $TOKEN"
```

### Sessions

| | |
|---|---|
| Cookie | `smm_session`; HttpOnly, SameSite=Lax, Path=/, Secure in production |
| Token | 32 random bytes, URL-safe. The client cookie is the only copy |
| Stored | SHA-256 digest only, so a dump of `user_sessions` cannot be replayed as a login |
| Absolute lifetime | 7 days, fixed at login (`expires_at`) |
| Idle timeout | 24 hours, derived from `last_seen_at` |
| Revocation | logout sets `revoked_at`; the session token and its CSRF token stop working immediately |
| Deactivation | a session of a deactivated user stops working on the next request |

JWT is deliberately not used: a stateless token cannot be revoked, and
revoking a session the moment an operator leaves is the property that
matters here.

### CSRF

`GET`, `HEAD`, `OPTIONS` and `TRACE` need no token. `POST`, `PUT`,
`PATCH` and `DELETE` must carry `X-CSRF-Token`.

The token is **derived from the session token, not stored**: it is
`HMAC-SHA256(session_token, "ai-smm-csrf-v1")`, recomputed from the
cookie on every request. That gives three properties:

- **stable**, so every browser tab of one session holds the same working
  token and `GET /api/v1/auth/csrf` never invalidates a tab that already
  has one. It is also returned by `login`, so the first unsafe request
  needs no extra round trip;
- **one-way**, so handing the token to page scripts or putting it in a
  header does not expose the session cookie it came from. It is not
  accepted as a session cookie either;
- **bound to the session**, so revoking, expiring or idling out the
  session invalidates the CSRF token with it — validation starts from
  the cookie, and a token minted for an earlier session of the same user
  is refused.

No CSRF secret is stored in the database and no signing key has to be
configured.

The check is a dependency of the whole `/api/v1` router rather than of
each route, so a route added later is protected by default. The single
exemption is `POST /api/v1/auth/login`, which has no session yet and so
no token to present.

SameSite=Lax alone is not treated as sufficient: it says nothing about an
older client, a proxy that rewrites the attribute, or a same-site
subdomain.

---

## 5. Endpoints

| Method | Path | Access |
|---|---|---|
| `GET` | `/health/live` | public; process only, no database |
| `GET` | `/health/ready` | public; `SELECT 1` only |
| `POST` | `/api/v1/auth/login` | public (CSRF-exempt) |
| `POST` | `/api/v1/auth/logout` | session + CSRF |
| `GET` | `/api/v1/auth/me` | session |
| `GET` | `/api/v1/auth/csrf` | session |
| `GET` | `/api/v1/projects` | session; only projects with a membership |
| `GET` | `/api/v1/projects/{id}` | viewer+ |
| `GET` | `/api/v1/projects/{id}/settings` | viewer+ |
| `PATCH` | `/api/v1/projects/{id}/settings` | admin+, CSRF, `expected_version` |
| `GET` | `/api/v1/projects/{id}/publications` | viewer+, filtered, paginated |
| `GET` | `/api/v1/projects/{id}/series` | viewer+, paginated |
| `GET` | `/api/v1/projects/{id}/operations/summary` | viewer+ |
| `GET` | `/api/v1/projects/{id}/operations/attention` | viewer+, paginated |
| `GET` | `/api/v1/projects/{id}/audit` | **admin+**, paginated |
| `GET` | `/api/v1/publications/{id}` | viewer+ |
| `GET` | `/api/v1/publications/{id}/preview` | viewer+ |
| `GET` | `/api/v1/publications/{id}/attempts` | viewer+, paginated |
| `POST` | `/api/v1/publications/{id}/schedule` | admin+, CSRF |
| `POST` | `/api/v1/publications/{id}/reschedule` | admin+, CSRF |
| `POST` | `/api/v1/publications/{id}/cancel` | admin+, CSRF |
| `GET` | `/api/v1/series/{id}` | viewer+ |
| `GET` | `/api/v1/projects/{id}/content-items` | viewer+, paginated |
| `POST` | `/api/v1/projects/{id}/content-items` | editor+, CSRF |
| `GET` | `/api/v1/content-items/{id}` | viewer+ |
| `GET` | `/api/v1/content-items/{id}/revisions` | viewer+, paginated |
| `POST` | `/api/v1/content-items/{id}/revisions` | editor+, CSRF, `expected_item_version` |
| `GET` | `/api/v1/content-items/{id}/approvals` | viewer+, paginated |
| `POST` | `/api/v1/content-items/{id}/submit-review` | editor+, CSRF |
| `POST` | `/api/v1/content-items/{id}/approve` | editor+, CSRF |
| `POST` | `/api/v1/content-items/{id}/reject` | editor+, CSRF |
| `POST` | `/api/v1/content-items/{id}/materialize` | editor+, CSRF |

### Health

`/health/live` answers from the process alone. It must stay 200 while the
database is down, or an orchestrator would restart a container that is
merely waiting.

`/health/ready` checks exactly one dependency with `SELECT 1` and answers
503 `DEPENDENCY_UNAVAILABLE` when it fails. It does not call OpenAI or
Threads: a probe that spends an external quota, or that fails because a
third party is slow, takes the service out of rotation for no reason.

### RBAC

`viewer < editor < admin < owner`. Every role includes the rights of the
ones below it.

| Action | viewer | editor | admin | owner |
|---|---|---|---|---|
| see the project in `GET /projects` | ✅ | ✅ | ✅ | ✅ |
| `GET /projects/{id}` | ✅ | ✅ | ✅ | ✅ |
| `GET /projects/{id}/settings` | ✅ | ✅ | ✅ | ✅ |
| `PATCH /projects/{id}/settings` | ❌ 403 | ❌ 403 | ✅ | ✅ |
| read publications, preview, attempts, series, operations | ✅ | ✅ | ✅ | ✅ |
| `GET /projects/{id}/audit` | ❌ 403 | ❌ 403 | ✅ | ✅ |
| schedule / reschedule / cancel | ❌ 403 | ❌ 403 | ✅ | ✅ |
| read content items, revisions, approvals | ✅ | ✅ | ✅ | ✅ |
| create item / revision, submit, approve, reject, materialize | ❌ 403 | ✅ | ✅ | ✅ |

Approval is editor+ and scheduling is admin+, on purpose: deciding that a
text is right and deciding when it goes out are different
responsibilities. `materialize` is editor+ because it only produces a
Publication in `approved` — it neither schedules nor delivers.

The flow is fixed: **project from the database → membership → role check
→ action**. A project id in a request body is never consulted; the id in
the URL is loaded from the database first. `editor` has no endpoint of its
own yet — it exists for the content endpoints of a later stage.

For a resource addressed by its own id — `/publications/{id}`,
`/series/{id}` — the same rule starts one step earlier: **row from the
database → its `project_id` → membership → role → action**. The id in the
URL grants nothing by itself, and a row belonging to another project
answers 404 with the same body as a row that does not exist.

A project the caller has no membership on answers **404, not 403**, with
the same body as a project id that does not exist, so the API cannot be
used to discover which projects exist. An existing membership that is too
low answers 403: at that point the caller already knows the project is
there.

### Project settings and optimistic locking

Settings are three JSONB sections — `content_config`, `publishing_config`,
`brand_config` — plus a `version`.

The row is optional. A project that predates the table has none, and
`GET` answers with empty sections and `version: 0`. The first `PATCH`
must state `expected_version: 0` and creates the row at version 1.

A `PATCH` states the version it read. The write is a conditional `UPDATE`
on that version, so two editors who loaded the same settings cannot
silently overwrite each other; the second gets 409:

```json
{"error": {"code": "VERSION_CONFLICT",
           "message": "These settings were modified by someone else. ...",
           "details": {"expected_version": 1, "current_version": 2},
           "request_id": "..."}}
```

A section that is not supplied is left untouched. A section that **is**
supplied is replaced whole: merging two JSON documents whose shape the
backend does not yet own would make it impossible to delete a key.

---

## 6. Reading and steering the queue

### Pagination

Every list endpoint takes `limit` (1–200, default 50) and `offset`
(default 0), and answers with the same envelope:

```json
{"items": [...], "total": 128, "limit": 50, "offset": 0}
```

`total` counts what the filter matches, not what the page holds, so a
client can show "50 of 128" without a second request. An offset past the
end is an empty page, not an error; a `limit` outside the range is a 422.

### Publication filters

`GET /api/v1/projects/{id}/publications` takes `status` (repeat it to
narrow to a set, as `ai-smm queue --status` does), `format`, `series_id`
and `human_reviewed`. Rows come back ordered by `ordinal`, the same order
the CLI shows.

A list row carries no body text — it is a list — but it does carry
`body_chars` and `image_count`. The detail endpoint has the text, the
images and the operational fields (`last_error`, `next_attempt_at`, the
lease, the idempotency key).

### Preview

`GET /api/v1/publications/{id}/preview` runs the publishing layer's
existing preflight with the claim and review gates relaxed, exactly as
the CLI preview relaxes them: an operator inspecting a record holds no
claim, and an unreviewed record is the normal thing to preview — that is
how they decide whether to approve it.

It reports `content_valid` / `content_error` (the preflight), whether the
row is `human_reviewed`, whether its turn has come in its series
(`series_ready`, `blocking_parts`), and `publishable_now` for all three
together. **It sends nothing.** It is a pure function of the row, and a
test asserts that touching Threads from it raises.

### Operations

`/operations/summary` gives per-status counts (all nine, zero-filled),
`due_now`, `needs_attention`, `published_last_24h`, `last_published_at`,
`next_scheduled_at`, series counts, and the deployment's `dry_run` flag
so a panel can explain why nothing is going out. Every aggregate is
filtered by project: the platform-wide helpers in `ai_smm.queue` are
deliberately unused here, because over HTTP they would leak one project's
activity into another's summary.

`/operations/attention` is the HTTP counterpart of `ai-smm errors`:
`failed` and `needs_review` rows, newest change first. **Listing one does
not retry it.**

### The three commands

| Command | Allowed from | Effect |
|---|---|---|
| `schedule` | `approved`, `failed` | sets `scheduled_at`, clears the last error and the lease |
| `reschedule` | `scheduled`, `failed` | moves `scheduled_at` |
| `cancel` | `draft`, `approved`, `scheduled`, `failed` | status `cancelled`, terminal |

Everything else is **409 `INVALID_STATE_TRANSITION`**, with the current
status and the allowed set in `details`:

- `claimed` — a worker holds a lease on it right now; it returns to
  `scheduled` by itself when the lease expires;
- `publishing`, `needs_review` — the outcome is unknown. Only a human who
  has checked the real Threads account can say what happened, with
  `ai-smm reconcile`. **Nothing here ever retries one of these**, which
  is the whole reason the queue has the state;
- `published`, `cancelled` — terminal;
- `draft` for the scheduling commands — approve the exact text first
  (`ai-smm approve`), which also moves it to `approved`.

A row that carries publish metadata (`threads_post_id` or
`published_at`) is refused whatever its status says: something reached
the platform, so rescheduling it would duplicate a live post. That
barrier is `ai_smm.queue.reschedule`'s own, and it is surfaced as the
same 409.

The allowed-from table lives in one place,
`ai_smm.application.publications.COMMAND_POLICY`, derived from the
transition table documented on `PublicationStatus`. The router does not
compare statuses; it calls the application function and translates its
exceptions. The mutation and its audit entry are
`ai_smm.queue.reschedule` / `ai_smm.queue.cancel` — the same functions
the CLI and the worker call, unchanged.

#### Concurrency with the worker

Authorisation loads the publication, and a worker can claim the row
between that read and the write — the queue is built so that this happens
without anyone coordinating. Acting on the snapshot would set the status
back to `scheduled` and clear `claimed_by`, `claimed_at` and
`lease_expires_at` while the worker was already publishing it, which is
how one post goes out twice.

So a command does this, in this order:

1. authorisation has already decided which project the caller may act on;
2. the row is re-selected `FOR UPDATE`, taking the row lock;
3. `populate_existing=True` overwrites the stale instance in the
   session's identity map, so the ORM cannot hand back the snapshot and
   flush its fields over what the worker committed;
4. the project on the freshly read row is compared with the authorised
   one again;
5. the transition is checked against the **status read under the lock**;
6. `queue.reschedule` / `queue.cancel` performs the mutation;
7. the request transaction commits, releasing the lock.

Both directions are covered:

- **the command gets there first.** The worker's candidate query is
  `FOR UPDATE SKIP LOCKED`, so it does not block on the locked row and
  does not claim it; it moves on to the next due publication.
- **the worker gets there first.** The command reads `claimed` under the
  lock and answers 409 `INVALID_STATE_TRANSITION`. The lease is left
  exactly as the worker set it, and no audit entry is written.

The wait for the lock is bounded by a transaction-local `lock_timeout`
of 3 seconds, so an HTTP request cannot hang on a row that something
else is holding unexpectedly. When it fires, nothing has been read and
nothing changed, so the answer is 503 `DEPENDENCY_UNAVAILABLE` with
`details.reason = "publication_locked"` and `Retry-After: 2` — "come
back", not a verdict about a state the request could not see. In normal
operation this never fires: the worker commits its claim before it makes
any network call, so the lock it holds lives for one short transaction.

`scheduled_at` must carry a timezone offset
(`2026-10-12T09:00:00+03:00`). The CLI reads a naive time in the operator
timezone as a convenience; an API that guessed one could move a publish
by hours, so a naive value is a 422. A time in the past means "due
immediately", as `ai-smm schedule --at now` does.

### Series

`GET /api/v1/projects/{id}/series` lists series with `parts_total` and
`parts_published`. `GET /api/v1/series/{id}` adds the parts in position
order and `issues` — the structural report from
`ai_smm.series.validate_series_structure`, so the API and the CLI cannot
disagree about whether a series is well formed. Nothing here creates or
changes a series; that stays in the CLI.

### Audit

`GET /api/v1/projects/{id}/audit` is **admin and above**: an audit trail
names who did what, and a viewer has no business reading it. It takes an
optional `action` filter.

`audit_log` has no `project_id` column, so "the entries of this project"
is derived: the subjects belonging to the project are selected in SQL
from `publications` and `content_series`, and an entry matches when its
subject is one of them or `project:{id}` itself. An entry about a user (a
login, the owner bootstrap) belongs to no project and is therefore not
listed — a user is not owned by one.

---

## 7. Content, revisions and human approval

The editorial layer sits in front of the delivery queue:

```
ContentItem -> ContentRevision -> human approval of that exact revision
            -> materialize (editor+) -> Publication in `approved`
            -> schedule (admin+) -> worker -> Threads
```

`Publication` stays the delivery entity, exactly as before; legacy rows
are untouched and keep flowing. The worker reads none of the new tables.

### Revisions are immutable

A content item holds no text. Every version is a row in
`content_revisions` — body, format, images, items, metadata, provenance
(`source`, `source_ref`), the editor's score and notes, and
`content_hash`, the SHA-256 of the deliverable snapshot (title, body,
format, images, items). There is no endpoint that changes a revision,
and a database trigger rejects `UPDATE` and `DELETE` on the table, so the
guarantee holds for every client, not only this API. Changing content
means `POST /revisions`, which needs the item version the author read
(`expected_item_version`, 409 `VERSION_CONFLICT` if stale).

### States

| From | Action | To |
|---|---|---|
| — | create (with revision 1) | `draft` |
| `draft` | submit-review | `in_review` |
| `in_review` | approve | `approved` |
| `in_review` | reject | `rejected` |
| any but `archived` | new revision | `draft` |

A rejected or approved revision is not resubmitted: the next step is a
new revision. `archived` exists in the schema but has no transition in
this stage; archived items are read-only.

### Approval belongs to one exact revision

submit-review, approve and reject all name the `revision_id` the person
looked at. If it is not the item's current revision the request is 409
`STALE_REVISION` — the reviewer read old text. A revision of another item
is 404.

Creating a revision returns the item to `draft` and clears
`approved_revision_id`, so an approval of revision N never covers
revision N+1. The database enforces this independently of the code:

- `(status = 'approved') = (approved_revision_id IS NOT NULL)`;
- `approved_revision_id IS NULL OR approved_revision_id = current_revision_id`;
- `current_revision_id` and `approved_revision_id` are composite foreign
  keys onto `content_revisions(content_item_id, id)`, so either can only
  name a revision of the same item. The current-revision key is
  deferred to commit, because an item and its first revision are
  written together and refer to each other.

Decisions are appended to `content_approvals` and never changed: a
trigger rejects `UPDATE` and `DELETE`, and the composite key ties each
decision to a revision of the same item.

### Only a human approves

`content_approvals.actor_user_id` is a NOT NULL reference to `users`, and
the application functions for approve and reject take a `User`. An agent
can create a draft revision — `source` = `copywriter`, `strategist` and so
on, `created_by_user_id` NULL, audited as `agent:<source>` — but cannot
store an approval, cannot schedule and cannot publish. `source` is
provenance only; it grants nothing.

### Concurrency

Every command locks the item row (`SELECT ... FOR UPDATE` with
`populate_existing`, the same pattern as the queue commands) before it
reads anything: revision numbering cannot collide, a stale version is
caught, and an approval that queued behind a new revision wakes up to
find it stale instead of approving text nobody read. The wait is bounded
(3 s `lock_timeout`, then 503 with `Retry-After`).

### Materialisation: the bridge into delivery

`POST /content-items/{id}/materialize` turns the **approved current
revision** into a Publication and stops there:

- the item must be `approved`, and the latest recorded human decision
  for that exact revision must be `approved`;
- the snapshot (title — the revision's, else the item's — body, format,
  images, items, editor score) is checked by the delivery preflight
  first; content the worker would refuse is a 422 and nothing is
  written;
- the Publication is left in `approved`, `human_reviewed = true`, no
  `scheduled_at`: a safe pre-delivery state. **It is not scheduled and
  not published.** An admin schedules it with the existing command.

**A Publication is an immutable delivery snapshot of one revision.**
Materialisation only ever creates one; it never rewrites an existing
Publication with a newer revision. Approval and scheduling are separate
gates held by different people, and an admin who has opened a
Publication must schedule exactly what they read. A row lock protects the
row's state but not that intent: if the content could change underneath,
the schedule command would still see `approved` and queue text the admin
never saw.

#### Lineage: `content_publication_links`

The link table is the authoritative editorial → delivery mapping. One
row per Publication, saying which revision it is the snapshot of. The
row is written once, together with the Publication, and never repointed:

```
content_publication_links
  content_item_id  uuid      -> content_items.id
  revision_id      uuid      -- (content_item_id, revision_id)
                             --   -> content_revisions(content_item_id, id)
  platform         varchar(40)
  publication_id   bigint    -> publications.id   UNIQUE
  created_at       timestamptz
  PRIMARY KEY (content_item_id, revision_id, platform)
```

| Guarantee | Enforced by |
|---|---|
| a revision is delivered at most once per platform | primary key |
| a Publication is never attributed to two revisions | `UNIQUE(publication_id)` |
| the revision belongs to the item on the same row | composite foreign key |
| the Publication is in the item's project and on the row's platform | `BEFORE INSERT OR UPDATE` trigger — a foreign key cannot express it without a composite key on `publications`, which is not altered |
| a linked Publication cannot be deleted | `ON DELETE RESTRICT` |

`publications` gains no column. Its `source_ref` (`content_item:{id}`)
and deterministic `idempotency_key` (`content:{item}:{revision}:{platform}`,
`UNIQUE`) stay as a human-readable trace and a second, independent
barrier against duplicates. Nothing reads them to decide anything; a
Publication whose `source_ref` names an item but has no link is not that
item's, and a Publication carrying a revision's key without a link is
refused as tampered lineage (409).

**Idempotent** per (item, revision, platform): a repeat finds the link
and answers 200 `"result": "unchanged"`, writing nothing, whatever state
delivery has since reached.

**A newer revision needs the earlier snapshot cancelled first.** For a
newer approved revision, the item's existing Publications on the platform
(found through the links) decide:

| Earlier linked Publication | Result |
|---|---|
| `draft`, `approved`, `scheduled`, `claimed`, `publishing`, `needs_review`, `published`, `failed` | **409 `PUBLICATION_NOT_EDITABLE`** — "Previous delivery snapshot must be cancelled before a newer revision can be materialized." Nothing is changed, linked or created |
| `cancelled` (or none) | a new Publication and a new link — `"created"`; the cancelled Publication keeps its link as the historical lineage of the revision it carried |

`draft` and `approved` block too, on purpose: that is exactly the window
in which an admin may have opened the Publication and be about to
schedule it. To deliver the newer revision, the earlier snapshot is
cancelled explicitly (`POST /publications/{id}/cancel`, admin+), then the
item is materialised again. The 409's `details` name the blocking
`publication_id`, its status and the `previous_revision_id`.

The earlier Publication is decided on an unlocked read and never locked —
nothing here writes it, and a lock would make the worker skip it. A
cancel or a schedule of it that is still in flight neither blocks
materialisation nor is acted on before it commits. Two materialisations
of one item are serialised by the item lock, so no second live
Publication can appear between the check and the insert. A cancelled
revision is never materialised a second time: its link answers the
repeat.

The platform is the project's `default_platform`. The ordinal continues
the project's sequence; concurrent materialisations in one project are
serialised on a transaction-scoped advisory lock so they cannot take the
same one.

---

## 8. Errors and request ids

Every failure has the same shape:

```json
{"error": {"code": "...", "message": "...", "details": {}, "request_id": "..."}}
```

| Code | Status |
|---|---|
| `AUTH_REQUIRED` | 401 |
| `INVALID_CREDENTIALS` | 401 |
| `FORBIDDEN` | 403 |
| `CSRF_REQUIRED`, `CSRF_INVALID` | 403 |
| `NOT_FOUND` | 404 |
| `VALIDATION_ERROR` | 422 |
| `VERSION_CONFLICT` | 409 |
| `INVALID_STATE_TRANSITION` | 409 |
| `STALE_REVISION` | 409 |
| `PUBLICATION_NOT_EDITABLE` | 409 |
| `DEPENDENCY_UNAVAILABLE` | 503 |
| `INTERNAL_ERROR` | 500 |

No SQL, no stack frame, no connection string and no secret reaches a
client. A validation error reports the field and the rule that failed,
never the value that was submitted — otherwise a malformed login would
echo the password back into every proxy log.

Every request gets a `request_id`, returned in `X-Request-ID`. An
inbound `X-Request-ID` is reused when it matches `[A-Za-z0-9._-]{1,64}`
and replaced otherwise, so a caller cannot inject anything into the logs.
The access log line is method, path, status, duration and the id:
cookies, headers and bodies are never logged, and the existing redacting
filter covers everything else.

### A 2xx means committed

The request session is a dependency with yield, declared with
`scope="function"`: it commits when the endpoint returns and **before**
the response is sent. FastAPI's default scope for such a dependency
runs its exit code after the response, which let a client read a 2xx
before the write was durable — a browser that logged in and immediately
asked `GET /auth/me` was refused with 401 — and would have reported a
failed commit (for instance the deferred revision key) as a success.
`tests/test_api_transaction_boundary.py` looks at the database from a
second connection at the moment the response is handed to the server.

### Login failures

An unknown address, a wrong password, a malformed address and a
deactivated account all answer with exactly the same 401
`INVALID_CREDENTIALS` body, and a missing account still pays for one hash
verification, so the API cannot be used to find out which addresses
exist.

---

## 9. Audit

Written to the existing `audit_log` table, actor `user:<uuid>` or
`system:bootstrap`:

| Action | When |
|---|---|
| `user.create_owner` | console bootstrap (`system:bootstrap`) |
| `auth.login` | successful login |
| `auth.login_failed` | failed login, with the normalised address only |
| `auth.logout` | session revoked |
| `project.settings.update` | settings written, with the new version and the sections touched |
| `rescheduled` | schedule or reschedule command (the existing action name, written by `ai_smm.queue`) |
| `cancelled` | cancel command (likewise) |
| `content.created` | item created with its first revision |
| `content.revision_created` | any new revision, with its number, source and hash |
| `content.submitted_for_review` | draft -> in_review |
| `content.approved` / `content.rejected` | the human decision, with the revision and its hash |
| `content.materialized` | publication created: `result`, `publication_id`, `revision_id`, `revision_number`, `content_hash`, `platform`. A replay or a refusal writes nothing |

Content entries use the subject `content_item:<uuid>` and appear in the
project audit view.

The two command actions are the queue's own vocabulary, not new ones: an
entry written by the API is told apart from the CLI's and the worker's by
its actor, which is `user:<uuid>` rather than `cli:<name>` or
`worker:<id>`.

No password, no raw session token and no CSRF token is ever written to an
audit entry.

---

## 10. Tests

```bash
AI_SMM_TEST_DATABASE_URL=postgresql+psycopg://...@127.0.0.1:5432/ai_smm_test \
  pytest -q
```

The API and security suites are `tests/test_api_auth.py`,
`test_api_csrf.py`, `test_api_projects_rbac.py`,
`test_api_publications_read.py`, `test_api_publication_commands.py`,
`test_api_series_operations.py`, `test_api_command_concurrency.py`,
`test_api_content.py`, `test_content_materialize.py`,
`test_content_concurrency.py`, `test_content_links.py`,
`test_api_health.py`, `test_api_transaction_boundary.py`,
`test_api_contract.py`, `test_security_passwords.py`,
`test_security_tokens.py` and `tests/test_cli_user.py`. They drive the
real application through its factory against a disposable database;
nothing is mocked except, where a test needs an expired session, the
stored timestamps.

`tests/test_api_command_concurrency.py` runs real contention against
PostgreSQL — the actual `queue.claim_due_publication` with its
`FOR UPDATE SKIP LOCKED`, every participant on its own connection, and
one test where the command genuinely blocks on a lock the worker holds
until the worker commits. Nothing about the locking is mocked. Removing
either half of the fix (the lock, or `populate_existing`) makes three of
those tests fail.

`tests/test_migrations.py` additionally asserts that the control-plane
migration is additive: that `publications` is byte-for-byte identical
before and after it, that the four new `projects` columns all have
defaults, and that a project and a publication written before the upgrade
survive it unchanged.
