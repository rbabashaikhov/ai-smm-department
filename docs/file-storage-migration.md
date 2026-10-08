# Public file storage migration

Moves the public file storage off the original storage VPS, whose network
Meta's media crawlers cannot reach, onto the VPS that already runs the n8n
stack.

Only the storage moved. The AI SMM Department application, LangGraph,
Langfuse, Threads publishing logic and the n8n workflows are unchanged.

## Why

Threads `media_type=IMAGE` container creation failed with
`error_subcode 2207052` for every URL served from the old host. See
[threads-media-hosting.md](threads-media-hosting.md) for the original
diagnosis.

The migration was validated by creating one container per host from the
same JPEG bytes and the same token:

| host | network | container creation |
| --- | --- | --- |
| `gstatic.com` (control) | — | OK |
| old storage, `files.apps.leadmeter.ru` | RU hosting provider | FAILED, 2207052 |
| new storage, `media.arcade-lab.info` | LT hosting provider | OK |

The control proves the token and the image are fine, so the only variable
left is the host Meta has to download from.

### A note on diagnosing this

Probing Meta's crawler prefixes with an **outbound** TCP connect is not a
usable test. Both VPSes time out against `173.252.64.0/18` and
`69.63.176.0/20`, yet only the old one actually blocks the crawler. Those
prefixes simply do not accept connections on :443. Only real container
creation distinguishes a reachable host from an unreachable one.

## Locations

| | old | new |
| --- | --- | --- |
| public host | `files.apps.leadmeter.ru` | `media.arcade-lab.info` |
| host | `<old-storage-host>` (RU) | `<n8n-vps-host>` (LT) |
| SSH user | `<old-ssh-user>` | `root` |
| storage root | `/srv/miniapps/file-storage` | `/srv/file-storage` |
| web server | Caddy, shared edge for many sites | Caddy, dedicated to storage |
| TLS / routing | Caddy, own ACME | Traefik, ACME TLS-ALPN resolver |
| DNS | domain registrar's nameservers | Cloudflare, DNS-only record |

Paths below the storage root are identical on both hosts, so any path that
worked on the old host resolves to the same file on the new one.

`files.apps.leadmeter.ru` was deliberately **left pointing at the old VPS**.
The new storage got a new hostname instead, which means:

- no DNS change was needed in the `leadmeter.ru` zone, and the
  `*.apps.leadmeter.ru` wildcard is untouched
- URLs already handed out under `files.apps.leadmeter.ru` keep serving from
  the old host, so nothing that references them breaks
- cutover is a `.env` change, not a DNS change, and is therefore instant
  and trivially reversible

**The old storage is still intact and still serving.** Nothing was deleted.

## New VPS layout

```
/srv/file-storage/            755 root:root   <- STORAGE_REMOTE_ROOT
├── ai-smm/                   755 root:root
├── ai-food-coach/
└── other-projects/
```

Files are `644 root:root`. Uploads arrive over SFTP as `root`; the web
container mounts the tree **read-only**, so the serving path cannot write.

Configuration on the VPS, both new files:

- `/root/file-storage/compose.yml` — Caddy container, Traefik labels,
  joined to the external `n8n-compose_default` network.
- `/root/file-storage/Caddyfile` — serving rules, copied from the old
  host's `files.apps.leadmeter.ru` block so the response contract matches.

Nothing in `/root/n8n-compose/` was edited and no existing container was
restarted.

## Serving contract

TLS terminates at Traefik; Caddy speaks plain HTTP on `:80` inside the
Docker network with `auto_https off`.

- static files only, `file_server` without `browse` — no directory listing
- `GET` / `HEAD` / `OPTIONS` only, everything else `405`
- dotfiles `404`
- active content (`.html`, `.svg`, `.js`, `.xml`, …) `403`, so an uploaded
  file can never execute in the storage origin
- `X-Content-Type-Options: nosniff`, `Content-Security-Policy:
  default-src 'none'; sandbox`, `X-Robots-Tag: noindex, nofollow, noarchive`
- `Cache-Control: public, max-age=86400`, permissive CORS for `GET`/`HEAD`

## Validation performed

Against `https://media.arcade-lab.info`, after the cutover:

- HTTP 200, 0 redirects, valid Let's Encrypt certificate
  (`CN=media.arcade-lab.info`), `content-type: image/jpeg`,
  `content-length: 185508`, downloaded bytes SHA-256-identical to source
- 200 for both a `facebookexternalhit` and a browser User-Agent
- directory listing `404`, dotfile `404`, `PUT` `405`, traversal `404`,
  plain HTTP `301` to HTTPS
- SFTP upload through the unmodified `StorageUploader`
  (`scripts/test_storage_upload.py`) succeeded, including its own
  `verify_url` round trip
- `scripts/diagnose_threads_media.py` reported **both** the external
  control container and the storage container created: "image publishing
  works"

`arcade-lab.info` was used as the validation host before the
`media.arcade-lab.info` record existed, because it was the only name
already pointing at this VPS. Its router was removed afterwards; the apex
now returns `404` and serves no files.

## Configuration

Only `.env` changes; `StorageUploader` is unmodified.

```
STORAGE_SSH_HOST=<n8n-vps-host>
STORAGE_SSH_PORT=22
STORAGE_SSH_USER=root
STORAGE_SSH_KEY_PATH=<path-to-n8n-vps-ssh-key>
STORAGE_REMOTE_ROOT=/srv/file-storage
STORAGE_PUBLIC_BASE_URL=https://media.arcade-lab.info
```

`THREADS_STORAGE_PREFIX` is unchanged.

## Rollback

Because the old host kept its hostname and its files, rollback is a `.env`
revert with no DNS involved:

```
STORAGE_SSH_HOST=<old-storage-host>
STORAGE_SSH_USER=<old-ssh-user>
STORAGE_SSH_KEY_PATH=<path-to-old-storage-ssh-key>
STORAGE_REMOTE_ROOT=/srv/miniapps/file-storage
STORAGE_PUBLIC_BASE_URL=https://files.apps.leadmeter.ru
```

Optionally stop the new server afterwards:
`cd /root/file-storage && docker compose down`. That removes only the
storage container; the n8n stack is a separate compose project and is
unaffected.

Image publishing returns to failing with 2207052 after a rollback, because
that is the old host's original condition.

Retire the old copy only once the new storage has been stable for a while.

## Known limitations

- `media.arcade-lab.info` serves the whole tree, `ai-food-coach/` and
  `other-projects/` included, exactly as the old host did. Per-project
  hostnames would need separate routers.
- Caddy's `file_server` follows symlinks. The copy was made with
  `rsync --no-links` and the tree is root-owned with root-only writes, but
  a symlink created as root could still point outside the storage root.
- The two copies **diverge from the cutover onwards**: new uploads land only
  on the new host, while `files.apps.leadmeter.ru` keeps serving the old
  frozen copy. Worth setting a retirement date rather than keeping both
  indefinitely.
- Uploads run as `root`. A less privileged owner with group write would be
  tighter, at the cost of an extra account to manage.
