# SMM-025D R7 readiness — working log

Autonomous preparation of R7 (production mutations activation readiness),
started 2026-10-15 while Group O observes release `dce93e5` in production.
Branch `feat/smm-025d-r7-readiness` from `main` `dce93e5`; **nothing
committed, nothing deployed, production read-only**.

## Scope (owner's request)

1. FU-024C1-5 checklist (rollout-rollback.md §5), 10 items.
2. Split: read-only now vs. needs a production change.
3. Readiness of `ai_smm_cc_rw`, grants, session binding, approval grants, guards, smm-auth.
4. Release N′: mutations permitted, off by default.
5. Local tests of N′, incl. "no publication without human approval".
6. R7 runbook, rollback, smoke without publications.
7. Fix local issues, re-test.
8. Readiness report with remaining blockers.

## Log

- 2026-10-15 ~06:10Z: branch created; docs read (mutation-role.md, ADR-025D,
  rollout-rollback §4–5, follow-ups FU-024C1-5 / FU-024B4A-1 / FU-025D-*).
- ~07:50Z: production read-only facts (`audit-SMM-025D-R7-20261015/r7-ro.raw`):
  18 guard triggers / 11 tables, 25 guard functions, PUBLIC EXECUTE 0; cc
  verify 932/0, auth 962/0; anomalies 0; key id wrapper = worker start log =
  `bd53581cbed23d6d`; `ai_smm_cc_rw` absent; latest verified dump is
  pre-migration (`a7c2e5f19b40`); worker LIVE with the deployment token for
  the allow-listed `ai-catalog-consultant`.
- N′: `CC_RELEASE_PERMITS_MUTATIONS=yes` (+ comment). Tests: mechanism on
  the shipped scripts, the lock on a `no` copy, N′ starts read-only, the
  compose literal stays "false"; documented `mutations enable` now runs;
  the R7 runbook joins the documented-invocation test. CC suites 928 passed.
- `scripts/r7_mutation_rehearsal.sh` + `r7_rehearsal_client.py`: PASSED
  R7-0..R7-8 on N′ images (synthetic version …025d0e07, content = N).
  Fixed on the way (script bugs): SIGPIPE from `| head -1` under pipefail;
  audit check counted the setup row.
- Findings: `mutations disable` does not cancel scheduled rows and the
  worker publishes approved ones → runbook R7.R1; no operator `cancel`
  (FU-025D-6); a blocked row is re-claimed BATCH_SIZE times per cycle with
  an audit row each (FU-025D-7, pre-existing); §5 item 5 wording (preflight
  lists key ids only once a credential exists) corrected; rollout §4 said
  "no image change" — the CC images must be rebuilt for the label.
- Smoke client made production-safe: own project `r7-smoke` created by the
  owner via the API, refuses a legacy-publishing project, leaves items
  rejected and the account disabled; `scripts/r7_production_smoke.sh`.
- Docs: r7/{README,checklist,runbook}.md; mutation-role §8,
  control-center-deployment, rollout §4/§5, smm-025d README, follow-ups.
- Final: the smoke moved to its own project `r7-smoke` → a non-allow-listed
  project needs a destination to submit → the smoke addresses its own
  unverified test account. Rehearsal PASSED R7-0..R7-8 (smoke 22/0,
  approve-path 20/0); full suite 2756 passed; CC suites 960 passed; ruff,
  whitespace, links clean; no Docker leftovers. readiness-report.md written.
- **Integration update (2026-10-18):** the original work was based on
  `dce93e5`. SMM-026A main is `fbf1b1a`: its migrations advance
  `c4d81e2a9f63 → e04cb3a748f5 → f7a91c2d5e04` and its cc/rw verify files
  have new pins. The integration keeps those pins, preserves default
  read-only mode and does not add a generation-worker service, role
  password or LLM calls. The historical local results above must be
  supplemented by targeted revalidation on the integrated commit.
- **Next (owner):** review; decisions D1–D5; after Group O and targeted
  integration validation: commit/merge N′, then R7.1… each separately
  approved.
