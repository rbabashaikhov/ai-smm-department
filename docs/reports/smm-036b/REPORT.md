# SMM-036B — Browser Acceptance, 2026-10-23

## Result

**PASS for the local browser flow:** login → Generate Topics → queued →
fake ideation succeeded → three ideas → reject with confirmation → select
an idea → fake generation succeeded → open its draft.

The reported disappearing button **did not reproduce** in the supplied
working tree. No permission bypass or speculative React fix was made.
Historical disappearance cannot be attributed to a specific cause from
this run. The initial working tree already contained the Generate Topics
form as uncommitted SMM-036B work; HEAD alone lacks that form.

## Evidence and diagnosis

- Chrome, `http://127.0.0.1:5173/projects/browser-e2e/ideas`.
  Both the first login and a final logout → protected URL → login returned
  to Ideas with an enabled **Generate Topics** button.
- `useProjectPermissions` takes the loaded project role, falling back to
  `/auth/me`; `canEditContent` requires owner and explicit mutation enablement.
  Actual `/auth/me` and project detail responses: HTTP 200, role `owner`,
  `control_plane.mutations_enabled: true`. The React conditions hold.
- Console inspection found Chrome extension connection errors only
  (`chrome-extension://…`); no application React error was observed.
- A separate, reproducible acceptance blocker was found: the API process
  ran from this worktree but its editable Python installation resolved
  `ai_smm` from
  `/home/ruslan/Documents/OTUS-LLM-Engeneering/13/ai-smm-department/src`.
  The first browser-created ideation job had no `content_profile` and the
  current fake worker rejected it. This is **not evidence that the import
  mismatch hid the button**.
- The local launchers now prepend this worktree's `src` and repository root.
  After restarting only the local browser API, a fresh browser-created job
  had `content_profile` and completed successfully without an LLM.
- The existing fake generation helper also had two proven incompatibilities:
  its knowledge loader rejected `allow_missing=True`, then its two-item fake
  result violated the selected idea's `series_size=1`. Both are corrected.

## Browser and database acceptance

Disposable target confirmed before writes:
`127.0.0.1:55432/smm_e2e_owner_test`, existing temporary PostgreSQL 13 cluster.
No migration was run. A separate temporary account,
`browser-036b@local.test`, was seeded in that database; existing users were
not edited. This checks browser behavior, not production PostgreSQL version
or worker-role deployment readiness.

| Check | Observed result |
| --- | --- |
| Owner login / protected Ideas URL | Generate Topics enabled |
| Queue ideation from UI | queued; no automatic duplicate POST observed |
| Fake ideation | succeeded, three proposed ideas appeared through polling |
| Reject third idea | confirmation dialog, then rejected |
| Select second idea | used; navigated to generation detail |
| Fake generation | succeeded, one draft, zero model tokens/cost |
| Open generated draft | body visible; draft, no effective approval |
| Database safety check | draft approved_revision_id NULL, 0 approvals for draft, 0 project publications |
| Final logout/login | button still present and enabled |

Successful ideation: `cb6deef0-a0dc-4f98-9142-3a2ff3b9682f`.
Successful generation: `820f9589-3610-4bf4-b00d-85de4ed87cc5`.
Draft: `357a4134-2df2-4f2f-b153-e76127b46253`.

Earlier failed test jobs are retained as diagnostic evidence: ideation
`56cd5780-a8cf-4cd8-8946-1dd832a9f88f` (wrong checkout, missing snapshot),
generation `2fa129cc-673f-49c8-a361-654812dfb400` (helper incompatibilities).
No job state was manually rewritten to turn a failure into a success.

Artifacts: [API and DB evidence](api-evidence.json),
[console](console.json), [Ideas DOM](ideas-after-login-dom.txt),
[draft DOM](draft-dom.txt), [Ideas screenshot](ideas-after-login.png),
[draft screenshot](draft.png). HTTP evidence excludes cookies, CSRF and
passwords.

## Changes and checks

Preserved and exercised the seven existing modified frontend files.
Added a post-login Ideas regression and explicit Generate Topics assertion
for read-only owners in `web/src/tests/contentIdeas.test.tsx`.
Updated local-only `scripts/local_ideation_api.py` and
`scripts/local_fake_workers.py`; added `scripts/local_browser_safety.py`
and `tests/test_local_browser_helpers.py`.

STANDARD verification; no production/authentication implementation changed:

- Content Ideas UI: **18 passed**, including owner login return URL,
  viewer/editor/admin denial and read-only owner denial.
- Auth, read-only and generation UI suites: **52 passed**.
- Helper guard/import tests: **4 passed**, including execution from outside
  the checkout and rejection of remote/non-test targets or missing opt-in.
- TypeScript, ESLint, Vite production build, Python Ruff and diff check pass.

The fake workers were invoked once per attempt with a minimal environment:
only the disposable database URL, local opt-in, dotenv disabled, and basic
process variables. No external API keys were provided. Python socket
connections are limited to that PostgreSQL port. The workers inject FakeLLM
and FakeGraph and do not start a Publisher.

To repeat against a freshly seeded disposable database, use this worktree's
Python dependencies, set `PYTHON_DOTENV_DISABLED=1`,
`AI_SMM_LOCAL_IDEATION=1` and a loopback test/e2e `AI_SMM_DATABASE_URL`.
Run `python scripts/local_ideation_api.py`, the local Vite server, and from
this repository `python scripts/local_fake_workers.py ideation` after
queuing **3 ideas**, then `python scripts/local_fake_workers.py generation`
after selecting an idea (**series_size=1**). The helper is scoped to that
acceptance fixture. Existing identical fake idea titles can be deduplicated,
so use a fresh test project/database for a full repeat.

## Boundaries and remaining uncertainty

No real LLM, Publisher, production access, commit, push, merge or deployment.
The local API was restarted; the existing UI server and disposable database
were reused and left available. The original disappearance remains
unverified; the reproducible acceptance blockers above are fixed. No release
or production readiness is claimed.
