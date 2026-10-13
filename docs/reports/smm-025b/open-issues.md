# SMM-025B — Open Issues

No architectural blocker was found: B1 and B2 are implemented and locally verified. What remains needs either an owner decision, a production step, or is deliberately out of this sprint's scope.

## Needs the owner's permission (production boundary)

| # | Item | Notes |
|---|---|---|
| P-1 | Code review, then commit / push / merge of the branch | nothing is committed |
| P-2 | Production rollout R0–R6 | [migration-rollback.md §3](migration-rollback.md#3-future-rollout-sequence-each-step-separately-approved); each step separately |
| P-3 | Generating the production key pair and its off-host backup | security §4 |
| P-4 | Business mutations in production | unchanged precondition: FU-024C1-5 with FU-024C2-1/2/3 — and OI-1 below |

## Open issues

Recorded in the project register as **FU-025B-1…9** ([docs/follow-ups.md](../../follow-ups.md#from-the-smm-025b-review-2026-10-13)): OI-1→1, OI-2→2, OI-3→3, OI-4→4, OI-5→5, OI-7→6, OI-8 (remainder)→7, OI-12→8, OI-11→9. OI-9 stays 025A Q-4, OI-10 stays FU-024B2-1.

| # | Issue | Priority | Blocks |
|---|---|---|---|
| OI-1 | **Mutation privilege model (FU-024C2-3) must cover the new tables.** An editable API needs: `INSERT` on `projects`, `project_memberships`, `project_settings`; `UPDATE (display_name, description, default_timezone, default_language, version, updated_at)` on `projects`; `INSERT`/`UPDATE` on `social_accounts` (no `DELETE`); `INSERT` and `UPDATE (status, ciphertext, nonce, ephemeral_public_key, superseded_at, revoked_at, verification_status, verified_at)` on `social_account_credentials` — **write** of key-material columns without **read** (PostgreSQL allows column `UPDATE` without `SELECT` only when the `WHERE` clause does not read them; `_retire_active` filters on `social_account_id`/`status`, which are readable). Today `ai_smm_cc` has none of these, by design | P1 | production mutations |
| OI-2 | **Gated wrapper verb** `cc-compose.sh create-project` (identity, queue-window, backup gates, like `create-owner`) so a second project can be created in production before mutations | P2 | R6 |
| OI-3 | Agents (strategist/copywriter) do not yet read `topics`/`tone_of_voice`; the UI says so | P2 | AI-assisted stage |
| OI-4 | `ai-smm credentials rewrap` for key rotation without re-entering tokens | P3 | — |
| OI-5 | Egress policy for the API container: it now makes one read-only call to the Threads Graph API (`GET /me`) when an owner saves a token | P3 | R5 |
| OI-6 | ~~Import-time `load_dotenv()` in `publishing/threads.py` and `storage.py`~~ — **resolved** (implementation D10): only explicit entry points load a local `.env` | — | — |
| OI-7 | Per-account rate limits: the daily cap and spacing are still per platform across all accounts (conservative: never exceeds the old limits) | P3 | SMM-028 |
| OI-8 | ~~Configuration errors at rollout turn scheduled rows into `failed`~~ — **resolved** (D11): the worker preflight refuses to start on a configuration that does not fit the queue. Remaining by design: a configuration change *while a correctly started worker runs* (e.g. the key file removed under it) is still refused row by row | P3 | — |
| OI-12 | The worker container's restart policy turns a failed preflight into a restart loop; harmless (nothing is claimed) but noisy. Optional: alert on exit code 78 in monitoring | P3 | — |
| OI-9 | `is_active` of a project is still display-only (025A Q-4) | P3 | — |
| OI-10 | The web test suite runs 139 tests; one pre-existing ordering race was fixed (D7). FU-024B2-1 (known vitest instability) remains open for anything not yet observed | P3 | — |
| OI-11 | Legacy rows created by `import-json`/CLI are not project-scoped for image paths (only the editorial layer validates) | P3 | — |

## Not done on purpose

- No universal connector framework, no OAuth flow, no owner-level account pool, no Workspace/Brand entities.
- No change to the publication lifecycle, idempotency, leases or retries.
- No production access of any kind; no real publication; no real credential.
