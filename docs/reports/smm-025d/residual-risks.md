# SMM-025D — Residual risks

What remains after SMM-025D, ranked. None of these allows publishing text
that no human approved for that destination; each is either inherent,
accepted, or recorded as a follow-up.

| # | Risk | Severity | Why it remains | Mitigation / follow-up |
|---|---|---|---|---|
| R-1 | **S5 acts as the owner** for every non-approval command (edit, submit, schedule an *already approved* snapshot, cancel, accounts, credentials) while it can see the owner's cookie | medium | inherent while the API serves the owner's browser session | approval needs a fresh password via smm-auth; the worker publishes only approved lineage; cancel/disable are reversible; audit names the role (`written_by`) |
| R-2 | **S5a** (smm-auth compromised) can open sessions and issue approval grants for users it verifies | medium | whoever checks the password decides the identity | smm-auth is small, has no business-table rights, no listener on the shared network (rehearsed), argon2 semaphore, rate limit; its role is verified function-body by function-body before every `up` |
| R-3 | the first identity binding and `verification_status = verified` are the API's conclusion from GET /me | low | the database cannot call the platform | the worker re-checks identity before every publish; a bound identity never changes; grants pin it |
| R-4 | reads are not project-scoped in SQL; API roles can read session hashes and keep visible sessions alive (`last_seen_at`) | low (confidentiality) | session resolution for reads stays in Python (FU-025D-1) | raw tokens never stored; writes are bound in SQL |
| R-5 | smm-auth can reach smm-api over the shared network (T-N4 reverse) | low | smm-api listens on `0.0.0.0` (FU-024B3-2, FU-025D-2) | session + CSRF still required by smm-api |
| R-6 | old API `5f32b50` **as `ai_smm_cc`** on schema `c4d81e2a9f63` was not run as an image | low | its own suite passes on the new schema as the owner role (1914 passed; role suites skip without an admin URL), and its grants + verify files pass on the new schema (913 checks, 0 FAIL, sign-in rights back) | CC rollback restores the old grants file; observation step O |
| R-6a | a CC rollback to `5f32b50` gives `ai_smm_cc` its session-creating rights back, i.e. reopens FU-024C2-1 | low while read-only | the old API signs in itself | accepted for the read-only mode, as before SMM-025D |
| R-7 | a wrong public key in `control-center.compose.env` is detected late (worker preflight, when an active credential cannot be opened) | low | the CC cannot see the private key | the wrapper prints the key id for comparison with the deployment record (FU-025D-4); fails closed |
| R-8 | editor and admin cannot do anything business-related | — (product) | owner-only by decision | FU-025D-3 |
| R-9 | rate limiting is per e-mail (10/15 min) plus a global nginx cap; no per-client limit | low | all traffic arrives through one SSH tunnel address | as designed in ADR-024C-3A |
| R-10 | **G2 gap** during rollout: between applying G2 and switching the CC to N, the running API cannot sign anyone in or out | low (minutes, read-only) | G2 must precede the N gate | rollout D→E back to back |
| R-11 | downgrade of `c4d81e2a9f63` drops provenance of rows written since the upgrade | low | additive migration; downgrade is manual and separately approved | application rollback keeps the schema |
| R-12 | FU-025A-1: Content Detail keeps local state when the item changes | low | UI | the approve dialog confirms against smm-auth's own reading (content hash, current, under review) and refuses on mismatch |
| R-13 | FU-024B4A-1 (queue gate → DDL gap) for migration C | low while mutations are off | only the worker writes the queue during the window | window + queue gate; mutations stay off until R7 |
