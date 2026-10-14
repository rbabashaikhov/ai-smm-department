# SMM-025D — Rollout and rollback plan

**Nothing here has been executed.** Every group below needs the owner's
separate approval, as in SMM-025C; between groups the system is in a
consistent, supported state. Production as of 2026-10-14 (read-only
inventory, SMM-025C Group F):

| Component | Now | After this plan (release N = the merge commit of SMM-025D) |
|---|---|---|
| Schema | `a7c2e5f19b40` | `c4d81e2a9f63` (guards, `mutation_contexts`, `approval_grants`, `audit_log.written_by`) |
| Worker | `ai-smm:5f32b50`, owner role, LIVE, legacy allow-list `ai-catalog-consultant` | `ai-smm:N`, same role and allow-list, credential key directory mounted read-only |
| smm-api | `5f32b50`, `ai_smm_cc` (SMM-025B grants), serves login | `N`, `ai_smm_cc` with grants **G2**, no login route, public key + allow-list |
| smm-web | `5f32b50` | `N` (nginx routes sign-in and step-up to smm-auth; step-up dialog) |
| smm-auth | — | `N` (same API image), `ai_smm_auth`, `cc-auth` network, bound to `10.231.18.2` |
| Mutations | off; the release refuses `mutations enable` | **still off**; R7 is a separate, later decision (§4) |

Conventions as in SMM-025C: `ssh n8n-vps 'bash -s' < script`, read-only
psql (`PGOPTIONS='-c default_transaction_read_only=on'`) for every check,
secrets as key names only, no helper files left on the VPS, `</dev/null`
on `docker exec` / `compose run`. `$CC=/root/ai-smm-control-center`,
`$W=/root/ai-smm`.

## 1. Order and why

```
A  read-only facts + stage release N (nothing running changes)
K  credential key pair (outside the window)
B  window, dump, restore check, off-host copy          ─┐
C  migrate a7c2e5f19b40 -> c4d81e2a9f63                 │ one window
F  worker 5f32b50 -> N                                  ─┘
D  smm-auth in the database (ADR-024C-3A P3) + G2 for ai_smm_cc
E  Control Center 5f32b50 -> N (smm-api, smm-web, smm-auth), T-N6, smoke
O  observation (P6)
R7 business mutations: separate release flag, separate approval (§4)
```

- **C and F are one coordinated step, in one window — mandatory.** On
  schema `c4d81e2a9f63` only worker **N** may be started. **No older worker
  — `5f32b50`, `706b383` or any other — may be started, restarted or
  recreated on the new schema, at any time, including as a rollback**
  (FU-025D-5). `5f32b50` would refuse to start (exit 78, publishes and
  fails nothing); older images have no such check and are not supported
  on this schema.
- Between C and F the worker `5f32b50` that is **already running** stays
  as it is (the identity gate of C requires a running worker); it is not
  restarted. F follows C immediately. If C fails, the schema is unchanged
  and F is not started; if F fails, the worker is not started again until
  worker N passes its preflight, or the schema is downgraded first (§3).
- **K before F**: worker N's `compose.yaml` requires the key directory.
- **C before D**: `smm_auth.functions.sql` N checks that the new tables
  exist (`approval_grants`).
- **D before E**: the wrapper N runs the auth role gate and the G2 API
  role gate before `up`; both fail closed until D is done.
- **G2 (in D) only right before E**: G2 takes session creation from
  `ai_smm_cc`, so the running API `5f32b50` can no longer sign anyone in
  or out; existing sessions keep working (reads, `last_seen_at`). The gap
  between D2 and E is minutes, read-only.

## 2. Groups

### A — facts and staging (read-only on the host)

1. Inventory as in SMM-025C Group 0: containers, images, revisions,
   `alembic_version` = `a7c2e5f19b40`, worker env keys (names only),
   `ai_smm_cc` verify (5f32b50 file) 0 FAIL.
2. **T-N5 facts**: `ip -4 route`, `docker network inspect` of every
   network — `10.231.18.0/29` must overlap none (if it does, pick another
   /29, change compose + packaging test constant, new release).
3. Build on the laptop from the merge commit N:
   `docker build --target api --label org.opencontainers.image.revision=N -t ai-smm-api:N .`,
   `docker build --label … -t ai-smm-web:N web`, worker `ai-smm:N`
   (`--target runtime`). Save, sha256, transfer, `docker load`.
4. Stage `$CC-staging/N/`: `compose.control-center.yml`, `cc-*.sh`,
   `db/*.sql`, examples; check the four pins of `cc-gate.sh` against the
   staged files (`sha256sum`).

**Checkpoint A**: images N loaded with the label `N`; staged files equal
Git at N; no overlap; nothing running changed.

### K — credential keys (docs/credential-keys.md §3–4)

1. `install -d -o 10001 -g 10001 -m 700 /srv/ai-smm/credential-keys`.
2. `keygen` with image N (one-off container, the directory writable for
   that run only); record **key id** and date in the deployment record;
   the public key goes into `control-center.compose.env` in E.
3. Off-host encrypted copy, separate from database dumps; test-decrypt.

**Checkpoint K**: file mode 600, owner 10001; key id recorded; off-host
copy decrypted once. Nothing running changed.

### B — backup (window)

As SMM-025C Group B: `control-center.window`, `cc-db-backup.sh dump`,
`verify`, encrypted off-host copy, `control-center.offhost-ack`.

**Checkpoint B**: `cc-compose.sh preflight` PASS (with the **current**
scripts); evidence and ack present.

### C — migration

`cc-compose.sh migrate upgrade c4d81e2a9f63` with the **N scripts** in
`$CC` (identity, queue and backup-evidence gates). Then read-only:
`alembic_version` = `c4d81e2a9f63`; triggers present
(`SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'trg\_%\_guard\_%' AND NOT tgisinternal`: 18);
published rows unchanged; `audit_log.written_by` NULL on every old row.

**Checkpoint C** — if anything fails, the migration transaction rolled
back (one transaction, `lock_timeout`); stop.

### F — worker switch (same window)

1. `$W/compose.yaml` N; `ai-smm.env` gains
   `AI_SMM_CREDENTIALS_PRIVATE_KEY_FILES=/run/ai-smm/credential-keys/current.pem`;
   `AI_SMM_CREDENTIAL_KEYS_HOST_PATH=/srv/ai-smm/credential-keys` where
   compose interpolates it. Backup of both files first.
2. `docker compose -p ai-smm run --rm cli preflight worker` with image N:
   every check PASS (`schema.revision` c4d81e2a9f63, allow-list known,
   `accounts.credential_keys` "no stored credentials").
3. `AI_SMM_VERSION=N docker compose -p ai-smm up -d worker`; healthy;
   logs show preflight PASS; queue Q unchanged.

**Checkpoint F**: worker N healthy, LIVE as before, allow-list unchanged;
no attempt written in the window.

### D — smm-auth in the database, then G2

1. As the instance superuser: `smm_auth.functions.sql`, then
   `ai_smm_auth.grants.sql`; `\password ai_smm_auth` interactively (never
   on a command line); `ai_smm_auth.verify.sql` → 0 FAIL, ≥ 400 checks.
2. `ai_smm_cc.grants.sql` N (G2); `ai_smm_cc.verify.sql` N → 0 FAIL.
   From here the running API `5f32b50` cannot sign in; go to E directly.

**Checkpoint D**: both verify files 0 FAIL; `session-anomalies.sql` = 0.

### E — Control Center N

1. Back up `$CC` files (`rollback-5f32b50/`).
2. Install the N scripts, verify files, compose file; write
   `control-center.auth.env` (600, `ai_smm_auth` URL; URL never printed);
   `control-center.compose.env` with the 4 keys (public key from K,
   allow-list `ai-catalog-consultant` = the worker's).
3. `cc-compose.sh preflight` → identity (allow-list equal to the
   worker's, key id printed = K's), api-role, **auth-role**, queue PASS.
4. `cc-compose.sh up` → smm-api, smm-auth, smm-web healthy.
5. **T-N6** (existing containers only): from smm-api, TCP to
   `smm-auth:8000` refused; from smm-web, `smm-auth` resolves into
   `10.231.18.0/29`; smm-auth listens only on `10.231.18.2:8000`
   (`/proc/net/tcp`). Any failure → rollback E at once.
6. Smoke through the tunnel (owner password file in `/dev/shm`, shredded
   after): login 200 (served by smm-auth), `/auth/me` 200, logout 204,
   `/auth/me` 401; `smm-api` own `/api/v1/auth/login` 404;
   `mutations status` → read-only; audit rows of the smoke carry
   `written_by = ai_smm_auth`.

**Checkpoint E**: the above, `session-anomalies.sql` = 0, worker untouched.

### O — observation

24 h: logs of smm-auth (no 5xx), sign-in works, the worker publishes the
allow-listed legacy project as before (or nothing is due), no
`ApprovalLineage` failure on legacy rows.

## 3. Rollback

**Rule: roll back applications, keep the schema.** The migration only adds
(two tables, one nullable column, functions, triggers) and the owner role
— worker, migrate, CLI — is never guarded.

| From | To | Procedure | Loses |
|---|---|---|---|
| CC N | CC `5f32b50` | `cc-compose.sh down`; restore `rollback-5f32b50/` (compose, scripts, env files); re-apply `ai_smm_cc.grants.sql` **of 5f32b50** (gives back session creation) and check with its verify file; `up` | nothing (sessions opened by smm-auth stay valid) |
| smm-auth objects | none | `smm_auth.drop.sql`, `ai_smm_auth.revoke.sql` (the P3 rollback, `tests/test_db_auth_contracts.py`) — only after CC rollback | nothing |
| Worker N | an older worker (`5f32b50`, `706b383`, …) | **forbidden on schema `c4d81e2a9f63`.** Only after the schema has been downgraded to `a7c2e5f19b40` (row below); until then a failing worker N stays stopped and the queue waits | — |
| Schema `c4d81e2a9f63` | `a7c2e5f19b40` | **not through the wrapper**: the migration refuses without `AI_SMM_DOWNGRADE_REMOVES_MUTATION_GUARDS=yes`, which the wrapper never passes. A separately approved manual one-off with the owner URL, mutations off, backup first | `audit_log.written_by` (provenance of rows since C), `mutation_contexts`, `approval_grants`; no business row |
| Keys | — | docs/credential-keys.md §5–7 | — |

Old API `5f32b50` on schema `c4d81e2a9f63`, before G2: compatible by
design — it reads, and its only writes (session, `last_seen_at`, `auth.*`
audit) are allowed to a role without business rights; the guards' audit
rule is tested in `tests/test_db_guards.py`. Its own test suite passes on the new
schema (as the owner role; tests.md §4). With G2 applied it cannot sign in
— hence the grants step of the CC rollback: `5f32b50`'s grants file over
G2 on the new schema gives back session INSERT, `revoked_at`, audit INSERT
and `password_hash` read, and `5f32b50`'s verify file then passes (913
checks, 0 FAIL). That rollback reopens FU-024C2-1 for as long as it lasts
(accepted for the read-only mode, as before).

## 4. R7 — business mutations (later, separate approval)

Release N still ships `CC_RELEASE_PERMITS_MUTATIONS=no`. After the owner
accepts SMM-025D, a release N′ = N + that one reviewed line (no schema,
no image change other than the scripts) lifts it. Then, each approved:

1. **Mutation role**: `ai_smm_cc_rw.grants.sql` N (requires the SMM-025D
   schema; EXECUTE on the six guard functions); `\password ai_smm_cc_rw`;
   `control-center.api-rw.env` (600); `ai_smm_cc_rw.verify.sql` 0 FAIL.
2. **FU-024C1-5 checklist** (§5) on N′.
3. `cc-compose.sh mutations enable --approved-by <owner>` inside a window,
   queue Q before/after.
4. **Smoke without a publication**, in a test project: account; a
   credential with `verify` against the real platform is a real call —
   skip it, or use `verify: false` and revoke right after; draft →
   revision → submit → reject; an approval attempt **without** step-up →
   `403 APPROVAL_CONFIRMATION_REQUIRED`; schedule nothing.
5. Any doubt: `cc-compose.sh mutations disable` (always allowed, one
   recreate).

## 5. FU-024C1-5 checklist on the exact release

Run on the host, read-only except where stated, before `mutations
enable`; every line is a release gate.

| # | Check | Expected |
|---|---|---|
| 1 | `cc-compose.sh preflight` | identity, api-role, auth-role, queue PASS |
| 2 | `ai_smm_cc_rw.verify.sql`, `ai_smm_cc.verify.sql`, `ai_smm_auth.verify.sql` | 0 FAIL each |
| 3 | guard triggers / functions | 18 `trg_*_guard_*` triggers; PUBLIC has no EXECUTE on any `guard_*` function |
| 4 | direct SQL as `ai_smm_cc_rw` without a bound session (psql, a throwaway transaction, `ROLLBACK`) | `INSERT INTO publications …` refused `guard: …` |
| 5 | worker preflight | PASS; `accounts.credential_keys` lists the key id of K |
| 6 | key id printed by the wrapper | = deployment record (K) |
| 7 | T-N6 | as in E.5 |
| 8 | `session-anomalies.sql` | 0 |
| 9 | backup | a verified dump of `c4d81e2a9f63` with off-host copy, < 24 h |
| 10 | rollback rehearsed | `mutations disable` returns to read-only (`mutations status`) |
