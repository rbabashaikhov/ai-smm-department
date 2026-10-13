# SMM-025B — Sprint Reports

Multi-project & multi-account platform: projects created and configured from the Control Center, several Threads accounts per project with a write-only credentials manager, and account-aware publishing through the existing PostgreSQL queue and worker.

| Report | Content |
|---|---|
| Architecture | [docs/architecture/smm-025b-project-management.md](../../architecture/smm-025b-project-management.md) (rev. 2) — audit, model, contracts, routing, transition, security model, decisions |
| [Implementation](implementation.md) | features, files, autonomous decisions, defects found and fixed |
| [Security](security.md) | threat model, controls with the tests that prove them, cryptography, key management |
| [Migration & rollback](migration-rollback.md) | migration contents, transition hazard, ordered future rollout, rollback matrix |
| [Tests](tests.md) | commands, environment, counts, results, limitations |
| [Open issues](open-issues.md) | what needs the owner's permission, remaining follow-ups |

**State:** branch `feat/smm-025b-multi-project-accounts`, uncommitted. Production, the VPS, the live worker and real credentials were not touched; no publication was made.
