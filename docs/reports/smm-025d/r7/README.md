# R7 — production mutations activation readiness

Preparation of R7 — switching the Control Center to business-mutation
mode in production — on top of SMM-025D (release `dce93e5`, in production
read-only since 2026-10-15). **Local and read-only work only:** branch
`feat/smm-025d-r7-readiness`, uncommitted; nothing committed, pushed,
merged or deployed; no production role, file or container changed; no
publication.

| Document | Content |
|---|---|
| [readiness-report.md](readiness-report.md) | the result: what is ready, what was found and fixed, the remaining blockers, the owner's decisions |
| [checklist.md](checklist.md) | FU-024C1-5: the 10 release-gate items and the follow-up's own requirements — read-only now / local / needs a production change |
| [runbook.md](runbook.md) | R7.0–R7.9 in production, the smoke without a publication, the rollback |
| [CHECKPOINT.md](CHECKPOINT.md) | working log |
| [evidence/r7-mutation-rehearsal.log](evidence/r7-mutation-rehearsal.log) | the local rehearsal of enable → smoke → approve/schedule → disable → worker → revoke |

The integrated release **N′** is based on main `fbf1b1a`: it preserves the
SMM-026A migration chain and generation role contracts, while adding the
one R7 release permit: `readonly CC_RELEASE_PERMITS_MUTATIONS=yes`. It does
not deploy a generation worker or enable mutations by default.
in `deploy/control-center/cc-gate.sh`. It permits the mode; the Control
Center still starts read-only until `mutations enable`. Tooling added with
it: `scripts/r7_mutation_rehearsal.sh`, `scripts/r7_rehearsal_client.py`,
`scripts/r7_production_smoke.sh`.
