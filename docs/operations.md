# AI SMM Department — Operations Runbook

Audience: whoever operates the deployed worker. Assumes SSH access to the VPS
as root and no other privileges.

> Names in angle brackets — `<postgres-container>`, `<shared-network>`,
> `<another-service>` — stand for resources belonging to other projects on
> the same host. They are deliberately not named in a public repository.
> Substitute the real values from `docker ps` and `/root/ai-smm/.env`
> before running any command below.

Deployed state as of 2026-10-09: worker running in **dry run**, queue parked,
no Threads credentials on the server. See [production-deployment-plan.md](production-deployment-plan.md)
for the architecture and the reasoning behind it.

---

## 1. Layout on the VPS

| Path | Contents | Mode |
|---|---|---|
| `/root/ai-smm/compose.yaml` | compose project `ai-smm` | 644 |
| `/root/ai-smm/ai-smm.env` | runtime secrets (DB password, API tokens) | **600** |
| `/root/ai-smm/.env` | deploy variables: image tag, network, mount paths | 600 |
| `/root/ai-smm/backup.sh` | nightly dump of the `ai_smm` database | 700 |
| `/srv/ai-smm/knowledge/` | knowledge base and media assets, mounted **read-only** | 755 |
| `/srv/ai-smm/data/` | JSON queues staged for import | 755 |
| `/srv/ai-smm/ssh/id_storage` | SFTP key for the media storage | 600 |
| `/root/backups/ai-smm/` | database dumps and the pre-deployment role snapshot | 700 |

The database lives in the existing `<postgres-container>` container, in its
own database `ai_smm` owned by the non-superuser role `ai_smm`.

---

## 2. Everyday commands

All commands run from `/root/ai-smm`. Append `< /dev/null` when scripting them
over SSH: `docker compose run` attaches stdin and will otherwise swallow the
rest of a piped script.

```bash
cd /root/ai-smm

# What is in the queue (times shown in Europe/Moscow, stored in UTC)
docker compose --profile cli run --rm -T cli queue

# One record with its full attempt history
docker compose --profile cli run --rm -T cli show 2

# Anything that needs a human
docker compose --profile cli run --rm -T cli errors

# Counters and the current mode
docker compose --profile cli run --rm -T cli stats

# Who did what
docker compose --profile cli run --rm -T cli audit --limit 30

# Worker logs (structured JSON, secrets redacted)
docker logs ai-smm-worker --tail 50
docker logs ai-smm-worker -f | grep -v apscheduler
```

A convenience alias is worth adding to `~/.bashrc`:

```bash
alias aismm='cd /root/ai-smm && docker compose --profile cli run --rm -T cli'
```

---

## 3. Importing content

The importer only reads the JSON; it never modifies it, and rerunning it
creates nothing twice. The file must be readable by uid 10001, so a queue
produced by `publish_saved.py` (mode 600) needs a readable copy.

```bash
install -m 644 /path/to/queue.json /srv/ai-smm/data/queue.json

docker compose --profile cli run --rm -T \
  -v /srv/ai-smm/data:/srv/ai-smm/data:ro \
  cli import-json /srv/ai-smm/data/queue.json --display-name "Project name"
```

A record that already carries a `threads_post_id` is imported as `published`,
which puts it permanently out of the worker's reach.

---

## 4. Scheduling

Two independent gates stand between a draft and a live post:

1. `human_reviewed` — someone read this exact text and these exact images.
2. `AI_SMM_DRY_RUN=false` — the deployment is allowed to publish at all.

```bash
# Gate 1: record the review (do this only after reading the text)
docker compose --profile cli run --rm -T cli approve 2 --note "checked text and image"

# Put it on the schedule. A naive time is read in Europe/Moscow, never UTC.
docker compose --profile cli run --rm -T cli schedule 2 --at "2026-10-15T10:00"
docker compose --profile cli run --rm -T cli schedule 2 --at "+30m"
```

While `AI_SMM_DRY_RUN=true` a due record is validated, recorded as a `dry_run`
attempt and pushed one hour forward; nothing is sent.

---

## 5. The first real publish

Do not do this until the specific post has been approved by the owner.

```bash
cd /root/ai-smm

# 1. Add the credentials that are deliberately absent today.
#    THREADS_ACCESS_TOKEN, STORAGE_SSH_HOST, STORAGE_SSH_USER
nano ai-smm.env            # keep mode 600

# 2. Install a real SFTP key for the media storage (see section 9).
#    /srv/ai-smm/ssh/id_storage is an empty placeholder right now.

# 3. Preview the exact content one more time.
docker compose --profile cli run --rm -T cli publish-now 2

# 4. Turn publishing on and restart the worker.
sed -i 's/^AI_SMM_DRY_RUN=true/AI_SMM_DRY_RUN=false/' ai-smm.env
docker compose up -d --force-recreate worker

# 5. Publish exactly one post, by hand, watching it.
docker compose --profile cli run --rm -T cli publish-now 2 --live --confirm-reviewed

# 6. Turn it back off until the next approved post.
sed -i 's/^AI_SMM_DRY_RUN=false/AI_SMM_DRY_RUN=true/' ai-smm.env
docker compose up -d --force-recreate worker
```

A real publish is **not reversible from here**: deleting the post is a manual
action in the Threads app.

---

## 6. When a record lands in `needs_review`

This status means the outcome of a call to Threads could not be determined.
The worker will never touch such a record again. **Check the account before
doing anything else.**

```bash
docker compose --profile cli run --rm -T cli errors
docker compose --profile cli run --rm -T cli show 2
```

`show` prints each attempt with its `phase` and `creation_id`. An attempt that
reached `phase=publish` with `outcome=timeout` or `unknown` is the ambiguous
case: Threads may hold the post.

Open the Threads account and look for the post.

```bash
# The post exists -> close the record with the real id. No retry.
docker compose --profile cli run --rm -T cli reconcile 2 \
  --published 17900000000000123 --note "found in the account, verified by hand"

# The post does not exist -> and only then, retry.
docker compose --profile cli run --rm -T cli reconcile 2 \
  --retry --confirm-not-published --at "+10m" --note "account checked, nothing was created"

# Abandon it.
docker compose --profile cli run --rm -T cli reconcile 2 --cancel --note "superseded"
```

`--retry` without `--confirm-not-published` is refused. That refusal is the
whole point: a blind retry is how an account ends up with the same post twice.

---

## 7. Restart, recovery, rollback

```bash
# Restart (state lives in PostgreSQL; nothing is lost)
docker compose restart worker

# Stop only this project. Nothing else on the host is affected.
docker compose stop worker
docker compose down                 # the external network is not removed

# Roll back to a previous image
sed -i 's/^AI_SMM_VERSION=.*/AI_SMM_VERSION=<previous-sha>/' .env
docker compose up -d worker

# Emergency stop of all publishing, without stopping the service
sed -i 's/^AI_SMM_DRY_RUN=false/AI_SMM_DRY_RUN=true/' ai-smm.env
docker compose up -d --force-recreate worker
```

On start the worker reconciles whatever the previous process left:

* a row in `claimed` with an expired lease goes back to `scheduled` — nothing
  external had happened yet;
* a row in `publishing` goes to `needs_review` — an external call was in
  flight and its result is unknown.

`recover` runs the same pass by hand:

```bash
docker compose --profile cli run --rm -T cli recover
```

---

## 8. Backup and restore

`backup.sh` runs nightly at 03:30 server time (Europe/Vilnius) and keeps 14
days. It dumps only `ai_smm` and takes no exclusive locks, so the other
projects on the shared instance are unaffected.

```bash
/root/ai-smm/backup.sh                      # run now
ls -l /root/backups/ai-smm/
tail /root/backups/ai-smm/backup.log
```

Restore into a throwaway database first — never straight over the live one:

```bash
PGU=$(docker inspect <postgres-container> \
  --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | grep '^POSTGRES_USER=' | cut -d= -f2)

docker exec <postgres-container> psql -U "$PGU" -d postgres \
  -c "CREATE DATABASE ai_smm_restore_test OWNER ai_smm;"

cat /root/backups/ai-smm/ai_smm-<stamp>.dump \
  | docker exec -i <postgres-container> \
      pg_restore -U "$PGU" -d ai_smm_restore_test --no-owner

docker exec <postgres-container> psql -U "$PGU" -d ai_smm_restore_test \
  -c "select status, count(*) from publications group by status;"

docker exec <postgres-container> psql -U "$PGU" -d postgres \
  -c "DROP DATABASE ai_smm_restore_test WITH (FORCE);"
```

`/root/backups/ai-smm/roles-before-ai-smm-*.sql` is the snapshot of the
instance's roles taken before `ai_smm` was created. Keep it: it is the
reference for what the shared instance looked like beforehand.

---

## 9. Media upload — not configured yet

Image and carousel posts need the file reachable over public HTTPS, which the
existing `file-storage` container serves from `/srv/file-storage` on this same
host. The worker uploads over SFTP.

Today `/srv/ai-smm/ssh/id_storage` is an **empty placeholder** and
`STORAGE_SSH_HOST` / `STORAGE_SSH_USER` are empty, so an image publish would
fail in preflight — safely, before anything reaches Threads. Text posts are
unaffected.

Before the first image publish, pick one:

* **A dedicated SSH key.** Generate a keypair, authorise the public half for a
  user that may write only to `/srv/file-storage`, and install the private half
  at `/srv/ai-smm/ssh/id_storage` (mode 600, root-owned). Preferable: the
  current `.env` uses `root` over SSH, which the worker does not need.
* **A direct mount.** Since the storage is on this host, bind-mount
  `/srv/file-storage/ai-smm` into the container read-write and skip SFTP
  entirely. Cheaper and removes the key, but needs a small change to
  `StorageUploader`.

Note also that `paramiko` is configured with `AutoAddPolicy`, which accepts any
host key. Pin the known host key before using SFTP across an untrusted path.

---

## 10. Health and monitoring

The container has no port. `HEALTHCHECK` runs `ai-smm-healthcheck`, which
passes when the database answers and the last completed cycle is newer than
`AI_SMM_POLL_INTERVAL_SECONDS * AI_SMM_HEALTH_STALE_FACTOR` (180s by default).
A worker that is running but wedged therefore reports unhealthy.

```bash
docker inspect ai-smm-worker --format '{{.State.Health.Status}}'
docker inspect ai-smm-worker --format '{{json .State.Health.Log}}' | tail -c 600
```

Worth watching:

* `cli errors` returning anything — a record needs a human.
* `"Database unreachable"` in the logs — the shared PostgreSQL is down.
  Publishing stalls; the worker stays up and resumes on its own.
* `"requires manual review"` at ERROR level — an ambiguous publish happened.

Logs rotate at 10 MB × 5 files per service. No global
`/etc/docker/daemon.json` was created, so other containers keep their own
behaviour.

---

## 11. What must never be done on this host

The `ai-smm` project is a separate compose project attached to
`<shared-network>` as an **external** network, so its `up`/`down` cannot
recreate or remove the shared services. Keep it that way:

* do not `docker compose down` any other project;
* do not run `docker system prune` (it would delete the other projects'
  reclaimable images);
* do not remove Docker volumes or networks;
* do not modify Traefik's global configuration, the firewall or DNS;
* do not upgrade or restart the shared PostgreSQL, Redis, n8n or Traefik;
* do not publish a port from this project: it is reachable
  inside the Docker network and must stay that way.
