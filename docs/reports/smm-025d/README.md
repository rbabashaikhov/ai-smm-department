# SMM-025D — Production Mutations & Security Hardening

Makes business mutations safe to switch on against the LIVE worker: the
database itself refuses any change that is not the project owner's, any
approval the owner did not confirm with a password through the isolated
sign-in service, and any publication that is not exactly an approved
revision for its approved destination. Packages smm-auth and the
credential keys. Baseline `main` `5f32b50`.

| Report | Content |
|---|---|
| [ADR-025D](../../adr/ADR-025D-database-enforced-mutation-safety.md) | decision, mechanisms, residuals, configuration, network |
| [Implementation](implementation.md) | by phase, autonomous decisions, defects |
| [Security review](security-review.md) | attackers, every bypass with its test, authorization, credentials, findings fixed |
| [Rollout & rollback](rollout-rollback.md) | groups A–O with checkpoints, rollback matrix, R7, FU-024C1-5 checklist |
| [Tests](tests.md) | commands, results, what each suite proves, limitations |
| [Residual risks](residual-risks.md) | what remains, ranked |
| [Credential keys](../../credential-keys.md) | packaging, backup, restore, rotation, revocation |
| [Checkpoint](CHECKPOINT.md) | the working log of the sprint |
| [evidence/](evidence/) | network rehearsal log (passwords redacted) |
| [r7/](r7/README.md) | R7 readiness (release N′, FU-024C1-5 checklist, runbook, mutation rehearsal) |

**State:** branch `feat/smm-025d-mutations-hardening`, **uncommitted**.
Nothing committed, pushed, merged or deployed; production, the VPS, its
containers, roles and secrets untouched; no publication made. The release
keeps `CC_RELEASE_PERMITS_MUTATIONS=no`.
