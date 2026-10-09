# First live publish — exact procedure

Audience: whoever runs the first real publication, start to finish, in one
sitting. Every step is a command to run and a result to check before moving
on. Nothing here is automatic.

**Target:** publication 2 of `ai-catalog-consultant`, format `image`.
**Not in scope:** publication 3, and the scheduler. Both stay parked.

**Publication 2 was published on 2026-10-09**, following this procedure:
[post DeRdCDFACXw](https://www.threads.com/@ruslan.babashaikhov/post/DeRdCDFACXw),
Threads id `18075070049556193`. What follows is kept as the procedure for
the next one, with the corrections the first run surfaced.

A real publish is irreversible from the command line. Deleting the post
afterwards is a manual action in the Threads app.

All commands run from `/root/ai-smm` on the VPS. `< /dev/null` is required:
`docker compose run` attaches stdin and would otherwise swallow the rest of
a pasted block.

> Names in angle brackets — `<postgres-container>`, `<shared-network>`,
> `<another-service>` — stand for resources belonging to other projects on
> the same host. They are deliberately not named in a public repository.
> Substitute the real values from `docker ps` and `/root/ai-smm/.env`
> before running any command below.

---

## Step 0 — state before you start

```bash
cd /root/ai-smm
docker compose --profile cli run --rm -T cli queue < /dev/null
docker compose --profile cli run --rm -T cli stats < /dev/null
```

Expected, and worth actually reading rather than skimming:

| | |
|---|---|
| publication 1 | `published`, carries `threads_post_id` — out of reach |
| publication 2 | `approved`, `sched=none`, `reviewed=**yes**` |
| publication 3 | `approved`, `sched=none`, `reviewed=no` |
| `dry_run mode` | `True` |

Publication 2 is already reviewed, so the only gate left is
`AI_SMM_DRY_RUN`, plus the fact that it is not scheduled. Both have to be
opened deliberately, in steps 4 and 5.

If anything is already `scheduled`, stop and find out why before going on.

---

## Step 1 — approve the text and the image — DONE 2026-10-09

Already recorded: the owner confirmed the original caption and
`telegram-03-comparison.jpg`, including the brand names and store links
visible in the screenshot, which are intentional and are consistent with
publication 1. The audit log holds the approval.

To re-read what was approved:

```bash
docker compose --profile cli run --rm -T cli show 2 < /dev/null
docker compose --profile cli run --rm -T cli publish-now 2 < /dev/null
```

Read the caption in full. Open the image:
`knowledge/projects/ai-catalog-consultant/assets/telegram-03-comparison.jpg`.

Confirm, explicitly:

- [ ] the caption says what you want said, and 433 characters is the whole of it;
- [ ] the screenshot is the one you want public, including the brand names
      and the store links visible inside it;
- [ ] `format` is `image` and exactly one image is attached;
- [ ] `threads_post_id` is empty.

Only then record the review. This is the first of the two gates and it is
the one a human owns:

```bash
docker compose --profile cli run --rm -T cli approve 2 \
  --note "reviewed text and image before the first live publish" < /dev/null
```

---

## Step 2 — credentials — DONE 2026-10-09

`THREADS_ACCESS_TOKEN` is installed in `/root/ai-smm/ai-smm.env`, mode 600,
root-owned. It was piped over the SSH channel on stdin, so it never reached
a command line, a shell history or a log; it is in no other file on the
host.

To replace or rotate it later, edit the file in an editor — not with
`echo`, which lands in `~/.bash_history` — and recreate the container:

```bash
nano /root/ai-smm/ai-smm.env     # set THREADS_ACCESS_TOKEN=...
chmod 600 /root/ai-smm/ai-smm.env
docker compose up -d --force-recreate worker
```

Verify it read-only, without publishing anything:

```bash
docker compose --profile cli run --rm -T cli token-check --project < /dev/null
```

Expected:

- `account: @ruslan.babashaikhov (id 28375548782128062)` — the right account;
- `status : token is accepted for reading`;
- the listed posts include `17956888254278916` (publication 1) and **no**
  post carrying publication 2's text.

`threads_content_publish` cannot be verified without publishing. If the
token lacks it, step 5 fails with an HTTP 4xx and the record goes to
`failed` — no post is created, and that outcome is safe.

---

## Step 3 — media — DONE 2026-10-09

Verified end to end: `ThreadsPublisher.upload_image` wrote the approved
original into `/srv/file-storage/ai-smm/threads`, and the result was fetched
from outside the VPS as `HTTP 200`, `image/jpeg`, 159 554 bytes,
byte-identical to the source (sha256 `4dd28b93…`).

The artifact of that check is still published. The live publish uploads
under its own unique filename, so the artifact can be deleted at any time:

```bash
rm /srv/file-storage/ai-smm/threads/20261009T114652Z-46f9a834-telegram-03-comparison.jpg
```

Re-confirm the mount is live before publishing:

```bash
docker compose --profile cli run --rm -T --entrypoint sh cli -c '
  touch /srv/ai-smm/media/.probe && echo "media writable" && rm /srv/ai-smm/media/.probe
' < /dev/null
```

The publish path uploads and verifies the image over public HTTPS *before*
it creates the post, so a storage problem stops the run with nothing sent.

---

## Step 4 — guarantee a single executor

**Do not change `AI_SMM_DRY_RUN`.** `publish-now --live` sets `dry_run=False`
for its own run only, so it publishes while the background worker stays in
dry run and cannot publish anything at all. Flipping the environment
variable would remove that guarantee for no benefit.

What does need handling is which process does the work. Publishing by hand
requires the record to be `scheduled`, and a scheduled record is exactly
what the background worker polls for. Both go through the same atomic
claim, so `FOR UPDATE SKIP LOCKED` already makes a double publish
impossible — but the worker could claim it first and record a dry run,
which would leave the manual command with nothing to claim.

Remove the ambiguity by stopping the worker for the duration. Only this
project's container is touched:

```bash
docker compose stop worker
docker inspect ai-smm-worker --format 'running={{.State.Running}} exit={{.State.ExitCode}}'
docker ps --filter name=ai-smm --format '{{.Names}}'   # must print nothing
```

`exit=0` means it finished its cycle and shut down cleanly rather than
being killed mid-flight.

Publication 3 stays safe throughout: it is `approved` with `sched=none` and
`reviewed=no`, and the worker only ever claims a `scheduled` record.

---

## Step 5 — publish exactly one post

Preview once more. This contacts nothing:

```bash
docker compose --profile cli run --rm -T cli publish-now 2 < /dev/null
```

Then schedule it for now and publish it. `publish-now --live` takes the same
claim path the worker would, so the row is reserved exactly as usual:

```bash
docker compose --profile cli run --rm -T cli schedule 2 --at now \
  --note "first live publish" < /dev/null

docker compose --profile cli run --rm -T cli publish-now 2 \
  --live --confirm-reviewed < /dev/null
```

Expected output:

```
result : published
post_id: <numeric id>
```

**If you see `result : needs_review`, stop.** The outcome of the call could
not be determined. Do not run the command again — go to "If it goes wrong"
below.

---

## Step 6 — the post id

The id printed above is also in the database and in the audit log:

```bash
docker compose --profile cli run --rm -T cli show 2 < /dev/null
```

---

## Step 7 — check what was published

```bash
docker compose --profile cli run --rm -T cli token-check --project < /dev/null
```

The newest entry should be the post you just created, with `media_type`
`IMAGE` and a permalink. Open the permalink and confirm:

- [ ] the caption is the 433 characters you approved, not truncated;
- [ ] the image renders, and is the comparison screenshot;
- [ ] the alt text is present.

---

## Step 8 — check the database

```bash
PGU=$(docker inspect <postgres-container> \
  --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | grep '^POSTGRES_USER=' | cut -d= -f2)

docker exec <postgres-container> psql -U "$PGU" -d ai_smm -c "
  select id, status, threads_post_id, published_at, attempt_count
    from publications order by id;"

docker exec <postgres-container> psql -U "$PGU" -d ai_smm -c "
  select publication_id, attempt_number, phase, outcome, threads_post_id
    from publication_attempts order by id;"
```

Expected: publication 2 is `published` with the id from step 6 and a
`published_at`; its last attempt is `phase=publish`, `outcome=success`;
publication 3 is untouched.

Confirm it cannot happen twice:

```bash
docker compose --profile cli run --rm -T cli publish-now 2 --live --confirm-reviewed < /dev/null
```

This must refuse — the record already carries publish metadata.

---

## Step 9 — restart the worker

Do this straight away, before anything else. `AI_SMM_DRY_RUN` was never
changed, so there is nothing to put back:

```bash
docker compose up -d worker
sleep 20

docker inspect ai-smm-worker --format 'health={{.State.Health.Status}}'
docker exec ai-smm-worker printenv AI_SMM_DRY_RUN   # must print: true
docker compose --profile cli run --rm -T cli stats < /dev/null
docker compose --profile cli run --rm -T cli queue < /dev/null
```

The published record cannot be claimed again — it carries a
`threads_post_id` — so the restart is safe even with the worker polling.

Decide separately whether the token stays on the server. Leaving it means
the only thing between the queue and a live post is `AI_SMM_DRY_RUN`;
removing it means publishing is impossible until it is put back.

Publication 3 must still read `approved`, `sched=none`, `reviewed=no`.

Turning the scheduler loose — that is, scheduling records and leaving
`AI_SMM_DRY_RUN=false` — is a separate decision, to be taken after this
post has been live for a while.

Take a backup of the new state:

```bash
/root/ai-smm/backup.sh
```

---

## If it goes wrong

| What you see | What it means | What to do |
|---|---|---|
| `result : failed` | Threads rejected the request; **no post was created** | read `cli show 2`, fix the cause, reschedule |
| `result : retry_scheduled` | the media upload failed; nothing reached Threads | safe; it will retry, or fix the storage first |
| `result : needs_review` | **the outcome is unknown** — the post may exist | see below |
| `result : blocked` | a gate refused it; nothing was attempted | `cli show 2` prints the reason |

### needs_review

Threads may or may not hold the post. **Do not retry.**

```bash
docker compose --profile cli run --rm -T cli errors < /dev/null
docker compose --profile cli run --rm -T cli token-check --project < /dev/null
```

Open the account and look for the post.

```bash
# It exists -> close the record with the real id. No second post is created.
docker compose --profile cli run --rm -T cli reconcile 2 \
  --published <id> --note "verified by hand in the account" < /dev/null

# It does not exist -> and only then, retry.
docker compose --profile cli run --rm -T cli reconcile 2 \
  --retry --confirm-not-published --note "account checked, nothing created" < /dev/null
```

Then return to the safe state (step 9) regardless of which branch you took.

### Emergency stop

```bash
docker compose -p ai-smm stop worker
```

Nothing else on the host is affected. `AI_SMM_DRY_RUN=true` already means
the worker cannot publish; stopping it also prevents a dry-run cycle from
claiming a record you are about to handle by hand.
