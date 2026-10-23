# SMM-036B — final diff review

2026-10-23, branch `feat/smm-036b-browser-acceptance`, base `2abf57b`.
Scope: review all tracked and untracked SMM-036B work, fix demonstrated
issues, and prepare the file set. No commit/staging/push/merge/deploy.

## Finding fixed

**P2 — uncertain create outcome was unlocked after a failed queue read.**
After a topic-create POST loses its response, the page requires an explicit
queue check before another POST. TanStack Query `refetch()` resolves even
when the read fails. The previous code unconditionally removed the block,
so pressing "Проверить задания" during an outage re-enabled Generate Topics
without establishing the server state and could cause duplicate jobs.

`ContentIdeasPage.tsx` now checks `result.isSuccess` before removing the
block and resetting the error. The regression test failed before the fix;
it now verifies both that a failed read keeps the block and that a successful
read restores the action without automatically sending another POST.

The initial suspicion about state surviving a project switch was ruled out:
`ProjectScope` already keys its outlet by project ID. A test with owners of
both projects passes without any additional component wrapper or state-reset
implementation. That regression is retained.

## Review coverage and final validation

- Compared topic create/list endpoint wrappers and TypeScript response/request
  types against the actual FastAPI routes and schemas; project-scoped keys,
  owner/read-only presentation gates and existing server authorization agree.
- Reviewed create deduplication, disabled pending action, no automatic POST
  retry, unknown-outcome recovery, active-job polling and idea refresh.
- Reviewed local helpers for worktree import selection, explicit test opt-in,
  loopback test/e2e DB restriction, injected fakes, one-job operation, no
  Publisher entry point, and no automatic migration or dotenv loading.
- Inspected report artifacts for credentials; no password, API key, cookie or
  CSRF token is included. User/project/content data is local test-fixture data.

Final checks after the functional fix:

| Check | Result |
| --- | --- |
| Content Ideas, auth, read-only, generation UI | **73 passed** (21 Ideas + 52 related) |
| Helper import/target guards | **4 passed** |
| TypeScript / ESLint / Vite build | pass |
| Python Ruff / diff whitespace | pass |

The prior Browser Acceptance evidence in REPORT.md remains the record of
the actual happy-path browser run. This review's recovery fix was validated
with an integration test simulating a lost POST and failed/successful reads;
no second live database acceptance run or real model call was needed.

## Exact proposed file set

Application and UI tests (7):

- `web/src/api/endpoints.ts`
- `web/src/api/queryKeys.ts`
- `web/src/features/ideas/hooks.ts`
- `web/src/pages/ContentIdeasPage.tsx`
- `web/src/test/fixtures.ts`
- `web/src/tests/contentIdeas.test.tsx`
- `web/src/types/api.ts`

Disposable local helpers and their tests (4):

- `scripts/local_browser_safety.py`
- `scripts/local_fake_workers.py`
- `scripts/local_ideation_api.py`
- `tests/test_local_browser_helpers.py`

Acceptance and review evidence (8):

- `docs/reports/smm-036b/REPORT.md`
- `docs/reports/smm-036b/REVIEW.md`
- `docs/reports/smm-036b/api-evidence.json`
- `docs/reports/smm-036b/console.json`
- `docs/reports/smm-036b/draft-dom.txt`
- `docs/reports/smm-036b/draft.png`
- `docs/reports/smm-036b/ideas-after-login-dom.txt`
- `docs/reports/smm-036b/ideas-after-login.png`

Suggested commit title:
`feat(web): add topic ideation UI and disposable browser acceptance`

## Limits

Ready for a local commit review; not a production activation/release gate.
The historical missing-button cause is still unverified. The UI displays and
polls the latest five ideation jobs; this slice does not add a full job
history or track active jobs outside that page. The local fake fixtures
expect three requested ideas and one generated draft. PostgreSQL browser
acceptance used the existing disposable version 13 cluster, not a production
role/version rehearsal. These limits and the retained diagnostic failed
jobs are documented in REPORT.md.
