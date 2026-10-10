# HTTP API (control plane)

Audience: whoever runs the API locally or reviews how it is wired.

Status: SMM-022A. The API exists, is covered by tests and is **not
deployed**. There is no compose service for it yet and no migration has
been applied to the production database.

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
  `ai_smm.worker`, and a test asserts that on the import graph.

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

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `AI_SMM_DATABASE_URL` | — | required; the same database the worker uses |
| `AI_SMM_API_SESSION_LIFETIME_SECONDS` | `604800` (7 days) | absolute session lifetime, fixed at login |
| `AI_SMM_API_SESSION_IDLE_SECONDS` | `86400` (24 hours) | idle timeout, measured from the last request |
| `AI_SMM_API_COOKIE_SECURE` | `true` | `Secure` on the session cookie; set `false` only for local http |
| `LOG_LEVEL`, `LOG_FORMAT` | `INFO`, `json` | as for the worker |

No new secret is introduced: there is no signing key to manage, because
sessions are server-side and the CSRF token is stored as a digest
alongside the session.

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

The flow is fixed: **project from the database → membership → role check
→ action**. A project id in a request body is never consulted; the id in
the URL is loaded from the database first. `editor` has no endpoint of its
own yet — it exists for the content endpoints of a later stage.

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

## 6. Errors and request ids

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

### Login failures

An unknown address, a wrong password, a malformed address and a
deactivated account all answer with exactly the same 401
`INVALID_CREDENTIALS` body, and a missing account still pays for one hash
verification, so the API cannot be used to find out which addresses
exist.

---

## 7. Audit

Written to the existing `audit_log` table, actor `user:<uuid>` or
`system:bootstrap`:

| Action | When |
|---|---|
| `user.create_owner` | console bootstrap (`system:bootstrap`) |
| `auth.login` | successful login |
| `auth.login_failed` | failed login, with the normalised address only |
| `auth.logout` | session revoked |
| `project.settings.update` | settings written, with the new version and the sections touched |

No password, no raw session token and no CSRF token is ever written to an
audit entry.

---

## 8. Tests

```bash
AI_SMM_TEST_DATABASE_URL=postgresql+psycopg://...@127.0.0.1:5432/ai_smm_test \
  pytest -q
```

The API suites are `tests/test_api_auth.py`, `test_api_csrf.py`,
`test_api_projects_rbac.py`, `test_api_health.py`,
`test_api_contract.py`, `test_security_passwords.py` and
`tests/test_cli_user.py`. They drive the real application through its
factory against a disposable database; nothing is mocked except, where a
test needs an expired session, the stored timestamps.

`tests/test_migrations.py` additionally asserts that the control-plane
migration is additive: that `publications` is byte-for-byte identical
before and after it, that the four new `projects` columns all have
defaults, and that a project and a publication written before the upgrade
survive it unchanged.
