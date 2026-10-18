# R7 — readiness report (2026-10-15; integrated with SMM-026A)

**Verdict.** Release **N′** — current main `fbf1b1a` plus
`CC_RELEASE_PERMITS_MUTATIONS=yes`
— is prepared, tested and rehearsed locally and is **ready for review**.
Switching mutations on in production is **not ready to execute yet**: it
needs the owner's acceptance of SMM-025D, five decisions (§4) and the
production steps of the [runbook](runbook.md), several of which change
production and need their own approval (§5). Nothing was committed,
pushed, merged or deployed; production was only read.

## 1. Release N′

| | |
|---|---|
| Change | `deploy/control-center/cc-gate.sh`: `readonly CC_RELEASE_PERMITS_MUTATIONS=yes` (+ its comment), integrated with SMM-026A's generation API, migrations and role files |
| Default | **off.** No mode file → `up`, `restart`, `preflight` start smm-api read-only as `ai_smm_cc`; `compose.control-center.yml` keeps the literal `AI_SMM_API_MUTATIONS_ENABLED: "false"`; only `mutations enable --approved-by NAME` (role gate on `ai_smm_cc_rw`) writes the mode file for the running release |
| Images | CC images are rebuilt from exact N′. They intentionally include SMM-026A API/web/Alembic changes; the LIVE publisher image is not rebuilt or restarted. No generation-worker service is introduced |
| Schema, roles | production starts at `c4d81e2a9f63`; rollout needs an approved linear upgrade through `e04cb3a748f5` to `f7a91c2d5e04`, refreshed `ai_smm_cc` grants and then the existing R7 `ai_smm_cc_rw` role. `ai_smm_generation` is reserved for a later worker activation |
| Kept | the lock mechanism (`no`) stays in the code and is tested on a `no` copy, for any later release that needs it |

## 2. Component readiness

| Component | Production now (read-only check) | Local evidence on N′ | Ready? |
|---|---|---|---|
| Guards (triggers, functions) | 18 triggers on 11 tables; 25 `guard_*`; PUBLIC EXECUTE 0 | `test_db_guards.py`, rehearsal R7-2 (forged reviewed+scheduled publication, business audit row, project — all `guard: …`) | yes |
| Session binding | — (unused until mutations on) | every business write in the rehearsal went through `guard_bind_session`; unbound SQL refused (R7-2) | yes |
| Approval grants / step-up | `approval_grants` empty | R7-4 approval without step-up → 403, nothing written; R7-5 wrong password 401, grant used once, second approval refused | yes |
| smm-auth | in production since Group E; auth verify 962/0; T-N6 PASS | sign-in, sign-out, step-up preview and grant through nginx (R7-4/R7-5) | yes |
| `ai_smm_cc` (G2) | verify 932/0, anomalies 0 | unchanged by the rw role (R7-1, R7-8) | yes |
| `ai_smm_cc_rw` | **does not exist** (by design) | grants + verify 936/0 (R7-1); revoke after disable (R7-8) | ready to create (R7.4) |
| Wrapper `mutations enable/disable` | N refuses enable | R7-3 / R7-6 through the N′ wrapper with every gate; 960 wrapper/packaging/gate tests | yes |
| Worker | `dce93e5`, LIVE, key `bd53581cbed23d6d` loaded | R7-7: approved row taken; tampered → `failed` (ApprovalLineage); unreviewed → `blocked`, never published | yes |
| Credential keys | key id wrapper = worker start log = record | `verify:false` credential sealed and revoked; no key material in any response | yes |
| Generation worker | no production service or role credential | SMM-026A role is EXECUTE-only and worker has no publishing/approval/credential access | **not activated by R7** |
| Backup for R7 | latest verified dump is **pre-migration** (`a7c2e5f19b40`) | — | **no → R7.3** |

## 3. Checklist FU-024C1-5

Item by item in [checklist.md](checklist.md). Of the 10 release-gate
items: 3, 5, 6, 7, 8 are **done** read-only on production; 1 is done on
N (Group E) and locally on N′; 2 is done for `ai_smm_cc`/`ai_smm_auth`
and locally for `ai_smm_cc_rw`; 4 and 10 are done **locally** (rehearsal)
and repeat in production after R7.4 / R7.6; **9 is open** (R7.3). Of the
follow-up's own requirements, all are evidenced locally; the formal
closure of FU-024C2-1/2/3 is the owner's acceptance of ADR-025D.

## 4. Decisions for the owner

| # | Decision | Recommendation |
|---|---|---|
| D1 | Where the production smoke runs | its own project `r7-smoke`, created by the owner through the API in the smoke (not allow-listed, no verified account: nothing can be routed). It stays in the database: project, membership, settings, a disabled account, a revoked credential without material, two rejected items, audit rows — no API role can delete |
| D2 | After the smoke and the disable test (R7.8): stay enabled or stay read-only | enable again only when the first real content is planned; the mode is per release and survives no upgrade anyway |
| D3 | "Production-like copy" for FU-024C1-5's rehearsal | the local rehearsal (production schema from the image, production SQL files, production wrapper, synthetic data) plus the production smoke is sufficient; restoring the production dump elsewhere would copy password hashes and sessions for no additional proof |
| D4 | The smoke stores a credential with `verify:false` (a dummy string, sealed, then revoked and wiped) | accept: it exercises the public key and revocation without a platform call |
| D5 | Accept the residual risks R-1…R-13 of SMM-025D and the two new follow-ups FU-025D-6 (no operator cancel) and FU-025D-7 (blocked row re-claimed) for R7 | accept, with the runbook's R7.R1 procedure; FU-025D-6 before the first real schedule is preferable |

## 5. Remaining blockers, in order

| # | Blocker | Kind | Runbook |
|---|---|---|---|
| B1 | Group O of SMM-025D finished and accepted (end slice ≥ 2026-10-16T06:00Z) | observation | R7.0 |
| B2 | Owner's acceptance of SMM-025D (ADR-025D is "Proposed") and of D1–D5 | decision | R7.0 |
| B3 | Review, commit, merge, push of N′ | repository | R7.0 |
| B4 | Integrate/retest N′ on `fbf1b1a`; build, stage, migrate `c4d81e2a9f63 → f7a91c2d5e04`, refresh grants and switch the CC N → N′ read-only | **production change** | R7.1, R7.2 |
| B5 | Verified pre-migration dump of `c4d81e2a9f63`, off-host copy, owner's decrypt test, ack | **production change** | R7.3 |
| B6 | `ai_smm_cc_rw` created; its password set by the owner (`\password`); `control-center.api-rw.env` written by the owner; three role checks 0 FAIL | **production DB change** | R7.4 |
| B7 | Checklist items 1, 2, 4 on N′ in production | read-only / rolled-back txn | R7.5 |
| B8 | `mutations enable` + smoke + `disable` test, inside a window | **production change** | R7.6–R7.8 |

Not blockers, but conditions while mutations are on: **FU-024B4A-1**
(queue gate → DDL gap) must be closed before any further production
migration while the queue can be written; Group-O-style observation after
enable (R7.9).

## 6. Found and fixed during this work

| Finding | Fix |
|---|---|
| `mutations disable` leaves approved, scheduled rows scheduled; the LIVE worker publishes them | runbook R7.R1 (cancel in the UI while the mode is on; emergency brake: stop the worker); FU-025D-6 for an operator `cancel` |
| No operator CLI command cancels a publication | FU-025D-6 (P2) |
| A blocked row is re-claimed `BATCH_SIZE` times per cycle, an audit row each (pre-existing since 2026-10-09; unreachable for the API roles because of the guards) | FU-025D-7 (P3) |
| rollout §4 said "no image change other than the scripts" — the CC images must be rebuilt for their label | corrected |
| rollout §5 item 5 expected the worker preflight to list the key id — it does so only once a credential exists | corrected: the `Worker starting` log line names the loaded key ids |
| The smoke must not run where a mistaken schedule could publish | the client refuses a project with legacy publishing; it runs in `r7-smoke`, addresses only an unverified account, leaves items rejected and the account disabled |
| In a non-allow-listed project a revision cannot be submitted without a destination (found when the smoke moved to `r7-smoke`) | the smoke names its own test account; documented |
| Tests asserted "this release refuses mutations" | the lock is tested on a `no` copy; new tests: N′ ships `yes`, the mechanism copy is byte-identical to what ships, the compose literal still says "false", N′ reports read-only without a mode file and ignores another release's mode file; the documented `mutations enable` runs; the R7 runbook's wrapper calls are checked like the other runbooks' |

## 7. Tests (local, 2026-10-15)

| Suite | Result |
|---|---|
| Full Python suite on the pre-integration N′ | **2756 passed, 0 failed** (10 min 37 s; 2752 on N); targeted revalidation on the integrated commit is required |
| CC wrapper, mutations, packaging, gates, role gates, runtime packaging (final files) | **960 passed** |
| `scripts/r7_mutation_rehearsal.sh` on N′ images | **PASSED R7-0…R7-8** (67 PASS lines; [evidence](evidence/r7-mutation-rehearsal.log)) |
| ruff (`src tests alembic`, the new client) | clean |
| whitespace (`git diff --check`) | clean |
| Secret scan of the evidence log | 0 findings |

"No publication without human approval", proved at every layer: the
API refuses an approval without step-up (403, nothing written); the
database refuses a forged reviewed+scheduled row from the API role, a
second use of a grant, and any business write outside a bound
transaction; the worker refuses a row whose content differs from the
approved revision (`failed`, no external call) and never publishes an
unreviewed row (`blocked`).

## 8. Changes on the branch (uncommitted)

`deploy/control-center/cc-gate.sh`; `tests/test_control_center_mutations.py`,
`tests/test_control_center_wrapper.py`; new `scripts/r7_mutation_rehearsal.sh`,
`scripts/r7_rehearsal_client.py`, `scripts/r7_production_smoke.sh`; docs:
`docs/reports/smm-025d/r7/` (this report, checklist, runbook, checkpoint,
evidence), `docs/mutation-role.md` §8, `docs/control-center-deployment.md`,
`docs/reports/smm-025d/rollout-rollback.md` §4–5,
`docs/reports/smm-025d/README.md`, `docs/follow-ups.md` (FU-025D-6, FU-025D-7).

## 9. SMM-026A integration note

The integration preserves SMM-026A's generation-aware verify pins and the
linear migration chain. R7 still starts read-only; enabling mutations does
not start a generation worker. A future worker service, its
`ai_smm_generation` password, monitoring and LLM-spend limits require a
separate review and approval.
