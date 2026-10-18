# FU-024C1-5 checklist for R7 — status on 2026-10-15

Two lists: the release-gate table of
[rollout-rollback.md §5](../rollout-rollback.md#5-fu-024c1-5-checklist-on-the-exact-release)
(10 items, on the exact release), and the requirements of FU-024C1-5
itself ([follow-ups.md](../../../follow-ups.md#fu-024c1-5--verify-the-safety-gates-before-publication-mutations-are-switched-on)).
**RO** = checked on the pre-SMM-026A production release, read-only (2026-10-15 ~07:50Z,
`~/.local/share/ai-smm-deploy/audit-SMM-025D-R7-20261015/r7-ro.raw`, and
Group E / Group O); **LOCAL** = proved on release N′ locally; **PROD** =
needs a production change, therefore an approval, and is a step of
[runbook.md](runbook.md).

## 1. The release-gate table (§5)

| # | Check | Expected | Now | Where it is completed |
|---|---|---|---|---|
| 1 | `cc-compose.sh preflight` | identity, api-role, auth-role, queue PASS | **RO, on N:** PASS 2026-10-15 05:52Z (Group E); pre-integration N′ rehearsal passed | R7.2 after migration to `f7a91c2d5e04` |
| 2 | `ai_smm_cc_rw.verify.sql`, `ai_smm_cc.verify.sql`, `ai_smm_auth.verify.sql` | 0 FAIL each | **RO:** cc 932 checks 0 FAIL, auth 962 checks 0 FAIL; rw: role does not exist yet. Pre-integration local values are historical only: SMM-026A changes the cc/rw verification contracts | **PROD** R7.2/R7.4; revalidate integrated N′ locally |
| 3 | guard triggers / functions | 18 `trg_*_guard_*`; PUBLIC no EXECUTE on any `guard_*` | **RO: done** — 18 triggers on 11 tables; 25 `guard_*` functions, PUBLIC EXECUTE 0 | — |
| 4 | direct SQL as `ai_smm_cc_rw` without a bound session | `INSERT INTO publications …` refused `guard: …` | **LOCAL:** refused for a reviewed+scheduled publication, a business audit row and a project (rehearsal R7-2, the exact production procedure: `SET LOCAL ROLE` in a rolled-back transaction) | **PROD** R7.5, after R7.4 |
| 5 | worker preflight | PASS; the start log names the key id of K | **RO: done** — preflight 6/6 PASS (Group O baseline); `Worker starting` lists `credential_keys: ['bd53581cbed23d6d']`. (The table said "preflight lists the key id": it does so only once a credential is stored — wording corrected in §5) | — |
| 6 | key id printed by the wrapper | = deployment record (K) | **RO: done** — `bd53581cbed23d6d` (Group E preflight; recomputed from `control-center.compose.env` 07:49Z) | re-read at R7.2 / R7.6 |
| 7 | T-N6 | as in E.5 | **RO: done** on N (Group E, all PASS) | repeated at R7.2 (images change) |
| 8 | `session-anomalies.sql` | 0 | **RO: done** — 0 | repeated after R7.7 |
| 9 | backup | a verified pre-migration dump of `c4d81e2a9f63` < 24 h, off-host | **open** — the latest verified dump is `a7c2e5f19b40` (Group B, pre-migration); the nightly dump has no checksum/off-host copy | **PROD** R7.3, before upgrade to `f7a91c2d5e04` |
| 10 | rollback rehearsed | `mutations disable` → read-only | **LOCAL: done** — rehearsal R7-6 (+ role revoke R7-8) | **PROD** R7.8 |

## 2. FU-024C1-5's own requirements

| Requirement | Evidence | Status |
|---|---|---|
| Human approval: no route, agent or job can approve or set `human_reviewed`; only an approved, materialised snapshot can be scheduled | DB guards (ADR-025D §4.2–4.4); `tests/test_db_guards.py`; `tests/mutation_role/test_mr_guard_refusals.py`; rehearsal R7-2 (forged reviewed+scheduled row refused), R7-4 (approval without step-up 403, nothing written), R7-5 (a used grant does not approve twice) | **LOCAL done** |
| … and the worker publishes nothing else | `guard_publication_problem` before any external call (ADR-025D §4.5); `tests/test_publish_routing.py`; rehearsal R7-7: a row changed after approval → `failed` (ApprovalLineage), an unreviewed row → `blocked`, never published | **LOCAL done** |
| Central mutation gate and service-boundary guard refuse with the flag off; the flag is a literal in `compose.control-center.yml` | `tests/test_api_mutation_gate.py`; `tests/test_control_center_mutations.py::test_the_compose_literal_still_says_false`; rehearsal R7-0 / R7-6 (403 `MUTATIONS_DISABLED`); production Group E smoke (18 refusals) | **LOCAL + RO done** |
| RBAC, CSRF, project scoping | owner-only business rights (SMM-025D); `tests/mutation_role/test_mr_api_projects_rbac.py`, `tests/test_api_projects_rbac.py`, `tests/test_api_csrf.py`; rehearsal R7-4 (CSRF refusal); production Group E smoke (CSRF) | **LOCAL + RO done** |
| Command ↔ worker concurrency (row lock, `SKIP LOCKED`, lease) and duplicate-publication barriers | `tests/test_api_command_concurrency.py`, `tests/mutation_role/test_mr_api_command_concurrency.py`, `tests/test_queue_claiming.py`, `tests/test_content_concurrency.py` (full suite on N′) | **LOCAL done** |
| The DB role of FU-024B3-1 in place, grants checked | `ai_smm_cc` (G2) and `ai_smm_auth` in production, 0 FAIL (RO) | **RO done**; `ai_smm_cc_rw` → **PROD** R7.4 |
| Generation isolation | `ai_smm_generation` has only three SECURITY DEFINER contracts and no publishing/approval/credential access | SMM-026A DB-role tests; no generation worker is part of R7 | **not activated**; a later service/LLM-budget decision is required |
| FU-024C2-1, FU-024C2-2, FU-024C2-3 closed | SMM-025D: smm-auth + G2 (sessions), `written_by` (audit provenance), `ai_smm_cc_rw` + guards (privilege model) — deployed and smoke-tested read-only (Groups C–E) | **code + deployment done**; formal closure = owner's acceptance of ADR-025D (R7.0) |
| Rehearsal of the enable / disable switch on a production-like copy, including the rollback and what the worker does with anything scheduled meanwhile | `scripts/r7_mutation_rehearsal.sh` on N′ images: the production schema (migrated by the image), the production roles from the production SQL files, the wrapper of N′ with every gate; [evidence/r7-mutation-rehearsal.log](evidence/r7-mutation-rehearsal.log). **Finding:** `disable` does not cancel scheduled rows; the worker publishes approved ones (runbook R7.R1) | **LOCAL done**; the data is synthetic, not a production copy (decision D3) |
| The checklist executed and recorded for that release, reviewed; the owner approves the compose change separately | this file + [readiness-report.md](readiness-report.md) | **for review** |
