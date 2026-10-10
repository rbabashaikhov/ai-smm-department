# Follow-ups

Open items accepted for later work. Each one names where it came from,
what is wrong or missing, and what "done" means. None of them blocks the
release it came from.

---

## From the SMM-024B.2 code review

The architectural review of SMM-024B.2 (read-only control plane) ended in
**ACCEPT WITH FOLLOW-UPS** on 2026-10-11. The three follow-ups below were
recorded at that point.

### FU-024B2-1 — Flaky Vitest tests

**Status:** open. **Pre-existing:** yes. The tests are already flaky on
`main` at `46dbe85`, before SMM-024B.2, so the flakiness was not
introduced by it.

Evidence (20 consecutive `vitest run` on Node 24):

| Test | `main` | SMM-024B.2 | Failure |
|---|---|---|---|
| `src/tests/auth.test.tsx` › loads me, then the CSRF token, then the protected page | 5/20 | 4/20 | `expected -1 to be greater than 1`: the call order is asserted as soon as the "Проекты" heading appears, before `GET /api/v1/projects` has been recorded |
| `src/tests/content.test.tsx` › content list › shows the columns and passes filters and pagination to the API | 1/20 | 1/20 | `findByRole` gives up after the default 1000 ms under load |
| `src/tests/publications.test.tsx` › publication list › shows the delivery columns and passes filters to the API | 0/20 | 1/20 | same cause as above |

Proposed fix:

- assert the request order inside `waitFor`, once `GET /api/v1/projects`
  has been recorded, instead of right after the heading renders;
- raise the Testing Library async timeout centrally
  (`configure({ asyncUtilTimeout })` in `src/test/setup.ts`) rather than
  per test.

Done when: 50 consecutive `npx vitest run` pass, with no test weakened
(no removed assertions, no `retry`).

### FU-024B2-2 — Validate `worker_mode_source` when a worker heartbeat is introduced

**Status:** open. **Depends on:** a worker change, which the ADR
postpones and which needs separate approval.

Today `observed_worker_mode()` always returns `UNKNOWN` with no source,
and `worker_mode_source` in `/operations/summary` is an unconstrained
`str | None`. That is safe only because nothing ever sets it. When the
worker starts reporting its mode, the API must not let that field become
a weaker kind of truth than the rule it replaces.

Requirements for that change:

- **Invariant:** `worker_mode` is `live` or `dry_run` **only** with a
  non-null `worker_mode_source`; `unknown` **only** with `null`.
  Enforce it in the schema, not only by convention.
- **Constrained source:** `worker_mode_source` is a closed set of
  identifiers (an enum such as `worker_heartbeat`), not free text. It never
  carries a hostname, a container id, a path, a DSN or any other topology
  or secret.
- **Freshness:** a report older than an explicit threshold (for example,
  derived from the poll interval and `AI_SMM_HEALTH_STALE_FACTOR`) is
  `unknown`, not the last mode seen. So is a report without a timestamp.
- **Attribution:** the report names the worker (`worker_id`). With
  several workers, or several reporting different modes, the answer is
  `unknown` unless the rule for combining them is decided and tested.
- **No inference:** still never derived from the API's `AI_SMM_DRY_RUN`,
  nor from past attempt outcomes.
- **Tests:** fresh / stale / missing / conflicting reports, the invariant
  above, and that the UI shows `LIVE` / `DRY RUN` with the source and
  `UNKNOWN` otherwise.

### FU-024B2-3 — Documentation: read-only means business operations; authentication still writes session state

**Status:** open.

Several places say the read-only Control Center "only reads" or "writes
nothing". That is true of **business state** — content, approvals,
project settings, the publication queue — and not of authentication.
With `AI_SMM_API_MUTATIONS_ENABLED=false`, the API still writes:

| Trigger | Writes |
|---|---|
| `POST /api/v1/auth/login` (success) | a new `user_sessions` row; `users.last_login_at` and `updated_at`; an `auth.login` audit row |
| `POST /api/v1/auth/login` (failure) | an `auth.login_failed` audit row |
| `POST /api/v1/auth/logout` | `user_sessions.revoked_at`; an `auth.logout` audit row |
| any authenticated request, `GET` included | `user_sessions.last_seen_at` (the idle deadline) |

`docs/api.md` (Read-only mode) already lists these. The wording elsewhere
should use the same precise terms: "read-only for business operations;
authentication and session maintenance still write session and audit
rows". Places to align:

- `README.md` (HTTP API — control plane: "По умолчанию API только
  читает");
- `docs/runtime-packaging.md` §7;
- `docs/adr/automation-and-publication-safety.md` (Control plane safety);
- `docs/operations.md` (the Control Center note);
- `docs/production-deployment-plan.md` §17.3;
- the UI notice in `web/src/layouts/AppLayout.tsx`, if its wording is
  changed too.

Done when: no document or UI text claims that the read-only Control Center
writes nothing, and each of them names the session and audit writes, or
links to the table in `docs/api.md`.
