# Credential keys: packaging, backup, restore, rotation, revocation

SMM-025D, FU-025C-2. How the key pair that seals social-account tokens
(`ai_smm.security.credentials`) is deployed and looked after. No key, token
or password appears in this document, in Git, or in any report; commands
print at most a **key id** (the first 16 hex characters of the SHA-256 of
the public key, not secret).

## 1. Who holds what

| Process | Public key | Private key | Why |
|---|---|---|---|
| smm-api (Control Center) | **yes**, `AI_SMM_CREDENTIALS_PUBLIC_KEY` | no | seals a token an owner pastes in; can open nothing |
| smm-auth, smm-web, CC `migrate`, CC `cli` | no | no | never handle tokens |
| worker (`compose.yaml`, service `worker`) | no | **yes**, read-only mount | the only process that decrypts, right before publishing |
| worker-side `cli` (`ai-smm accounts verify`, preflight) | no | **yes**, same mount | operator checks on the worker host |
| worker-side `migrate` | no | no | schema only |
| PostgreSQL, its dumps | ciphertext only | **never** | a dump or a leaked API role yields nothing to decrypt |

The two halves never meet in one container. Token ↔ account binding: a
ciphertext's associated data names its account and credential row, so a
ciphertext moved to another row fails to decrypt; the worker additionally
asks the platform who the token belongs to (GET /me) and compares it with
the account's bound `external_account_id` before every publish, and never
falls back to the deployment token for an account-bound publication.

## 2. Packaging

### Control Center (`compose.control-center.yml`, `cc-compose.sh`)

`control-center.compose.env` (not secret, mode 644 is fine) holds, besides
`SMM_CC_VERSION` and `SMM_CC_SHARED_NETWORK`:

```
SMM_CC_CREDENTIALS_PUBLIC_KEY=<44-character base64 of the 32-byte X25519 public key>
SMM_CC_LEGACY_THREADS_PROJECTS=<comma-separated project ids, or empty>
```

Both keys are required (the wrapper refuses a file without them, and
Compose refuses to render without them). The wrapper validates their shape
(base64 of exactly 32 bytes; lowercase project ids) and, before `up`,
`restart` and `preflight`, that the allow-list equals the running worker's
`AI_SMM_LEGACY_THREADS_PROJECTS` as a set — the Settings page then shows
legacy publishing exactly as the worker applies it. They are interpolated
into **smm-api only**. The wrapper prints the public key's id at `up`,
`restart` and `preflight`; the operator compares it with the id recorded
when the pair was generated (§3).

### Worker (`compose.yaml`)

`AI_SMM_CREDENTIAL_KEYS_HOST_PATH` (required by the compose file) names a
host directory, owner `10001:10001`, mode `700`, holding the private key
file(s), owner `10001`, mode `600` (the worker refuses a group- or
world-readable key). It is bind-mounted **read-only** at
`/run/ai-smm/credential-keys` into `worker` and `cli`, never into
`migrate`. `ai-smm.env` lists the file(s) the worker loads:

```
AI_SMM_CREDENTIALS_PRIVATE_KEY_FILES=/run/ai-smm/credential-keys/current.pem
```

The directory is **not** under the knowledge, media or backup paths, and
is excluded from any host backup that also holds database dumps.

The worker preflight (`ai-smm preflight worker`, run on every start)
fails — the worker does not start — when an active credential was sealed
with a key id for which no private key is loaded.

## 3. Generating the pair (once per key)

On the worker host, in a one-off container with the key directory
writable for this run only:

```bash
sudo install -d -o 10001 -g 10001 -m 700 /srv/ai-smm/credential-keys
docker compose -p ai-smm run --rm --no-deps \
  -v /srv/ai-smm/credential-keys:/run/ai-smm/credential-keys:rw \
  cli credentials keygen --private-key-file /run/ai-smm/credential-keys/current.pem
```

`keygen` creates the file with mode 600 and `O_EXCL` (it never overwrites)
and prints the key id and the `AI_SMM_CREDENTIALS_PUBLIC_KEY=` line. Record
the **key id** and the date in the deployment record; put the public key
into `control-center.compose.env` as `SMM_CC_CREDENTIALS_PUBLIC_KEY`. Do not
paste the private key anywhere; it is never displayed.

## 4. Backup

The private key is deliberately not in any database dump. Losing it is
recoverable (every token is re-entered, §7) but should not happen:

1. Right after generation, make one encrypted off-host copy, exactly as for
   the database dump of SMM-025C Group B (`gpg --symmetric` with a
   passphrase the owner keeps outside the host, or the owner's public
   key), and test the decryption once on the owner's machine.
2. Keep that copy **apart from database dumps**: a dump plus the key yields
   every token. Different storage, different passphrase.
3. Record in the deployment record: key id, date, where the copy is,
   the date the decryption was tested. Never the passphrase.

## 5. Restore (host rebuilt, file lost)

1. Decrypt the off-host copy on the owner's machine; copy it to the host
   over SSH into the key directory as `current.pem`.
2. `chown 10001:10001` and `chmod 600` the file; `chmod 700` the directory.
3. `docker compose -p ai-smm run --rm cli preflight worker` must report
   `accounts.credential_keys` PASS ("keys held for all N key id(s)").
4. Only then start the worker. Shred the decrypted copy on the owner's
   machine.

## 6. Rotation (planned)

Re-sealing without re-entering tokens is FU-025B-4 (open). Until then:

1. Generate the new pair as `next.pem` (§3); record its key id.
2. List both files in `AI_SMM_CREDENTIALS_PRIVATE_KEY_FILES`
   (`current.pem,next.pem`) and recreate the worker; preflight PASS.
3. Put the new public key into `control-center.compose.env`; `cc-compose.sh
   restart smm-api` (the printed key id must be the new one).
4. Re-enter each account's token in the Control Center (Settings → account
   → credential): the new credential supersedes the old one.
5. When no active credential carries the old key id (preflight lists the
   ids it holds; `SELECT key_id, count(*) FROM social_account_credentials
   WHERE status = 'active' GROUP BY 1`, read-only), rename `next.pem` to
   `current.pem`, list only it, recreate the worker, preflight PASS.
6. Shred the old file; delete its off-host copy after the retention the
   owner sets for database dumps that still hold ciphertext sealed with it.

## 7. Revocation (a key or a token may have leaked)

The ciphertext alone is useless; a leaked **private key together with a
dump or database access** exposes every token sealed with it. Treat any
suspected leak of the private key as a leak of all those tokens:

1. `cc-compose.sh mutations disable` (always allowed), so nothing new is
   approved or scheduled meanwhile; cancel scheduled publications of the
   affected accounts if the worker must keep running for others.
2. **Revoke the tokens at the platform** (Meta app settings / the
   account's security page). This is the only revocation that stops an
   attacker who holds a plaintext token.
3. In the Control Center, revoke each affected credential (Settings →
   account → Revoke): the worker then refuses the account
   (`CREDENTIAL_REVOKED`) instead of trying a dead token.
4. Generate a new pair (§3), install it as the only key, recreate the
   worker, replace the public key in `control-center.compose.env`, restart
   smm-api.
5. Enter fresh tokens; each is verified against the account's identity
   before it is stored as `verified`.
6. Shred the old key file and its off-host copy.

A leaked **public key** needs nothing: it is not secret. A **substituted**
public key (someone edits `control-center.compose.env`) makes new
credentials undecryptable for the worker: the worker preflight fails and
the account is refused, closed; the key id printed by the wrapper differs
from the recorded one.

## 8. What is checked automatically

| Check | Where |
|---|---|
| public key shape, allow-list shape, both keys present | `cc-compose.sh` (`cc_load_compose_env`), `tests/test_control_center_wrapper.py` |
| CC allow-list = worker allow-list | `cc_gate_identity`, `tests/test_control_center_gates.py` |
| only smm-api gets the public key; no CC service gets a private key | `tests/test_control_center_packaging.py` |
| worker: read-only key mount, never in `migrate` | `tests/test_control_center_packaging.py` (worker compose) |
| key file mode 600 enforced on load | `ai_smm.security.credentials.load_private_key_file`, `tests/test_credentials*.py` |
| an active credential without its private key stops the worker | `ai_smm.preflight`, `tests/test_preflight*.py` |
| no fallback to the deployment token; identity re-check before publish | `ai_smm.publishing.routing`, `tests/test_publish_routing.py` |
