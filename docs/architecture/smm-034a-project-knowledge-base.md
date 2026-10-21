# SMM-034A — Project Knowledge Base in the Control Center

| | |
|---|---|
| Project | PRJ-013 AI SMM Department 2.0 |
| Task | SMM-034A — first working Knowledge Base scenario in the Control Center |
| Baseline | `main` at `9ff2260cfdeb26f8bc956f124f31877921b6fd3f` |
| Status | Read-only slice implemented locally, with its production packaging (read-only mount on `smm-api`, §2.1); not deployed. Write path **not** implemented (storage blocker, §3) |
| Production | Not touched. One read-only inspection of the VPS (§1) |

## 1. How the knowledge base is stored in production

Read-only inspection on 2026-10-21 (`docker inspect`, `ls`, `find`; no
writes):

| Fact | Value |
|---|---|
| Host directory | `/srv/ai-smm/knowledge` (`AI_SMM_KNOWLEDGE_HOST_PATH`), plain files, not a git checkout |
| Owner / mode | uid:gid `1000:1000`; directories `775`, documents `644` |
| Projects present | `ai-catalog-consultant` only |
| Worker (`ai-smm-worker`) | bind mount `/srv/ai-smm/knowledge -> /srv/ai-smm/knowledge`, **`rw=false`** |
| Control Center (`smm-api`, `smm-auth`, `smm-web`) | **no mounts at all** (release 9ff2260); `read_only` root filesystem |
| Control Center mode | business mutations disabled (`AI_SMM_API_MUTATIONS_ENABLED=false`) |

Consequences:

* The Control Center cannot write the knowledge base anywhere: its image
  is read-only, it has no volume, and the worker's mount is read-only.
  Even a read-write bind mount of the existing directory would fail —
  the containers run as uid `10001`, the files belong to `1000` with mode
  `644`.
* The running production Control Center (9ff2260) cannot even **read**
  the knowledge base: `smm-api` has no knowledge mount. The API reports
  this as `available: false` and the page says so; it does not fail.
  §2.1 adds the read-only mount for the next release.
* Reading needs no host change: uid `10001` reads the tree through the
  *other* bits (`775` directories, `644` documents), as the worker does.

## 2. What SMM-034A implements (read-only)

* `ai_smm.knowledge.documents` — lists and reads the documents the
  agents' loader (`ai_smm.knowledge.loader.load_project_knowledge`)
  reads: top-level `.md`/`.txt`/`.yaml`/`.yml` of the project directory.
  The loader is unchanged; a test asserts both see the same files.
* Isolation and traversal: the project id must be one valid segment and
  resolve directly under `projects/`, with no symlinked project
  directory; a document name is one segment matching
  `[A-Za-z0-9][A-Za-z0-9._-]{0,127}` with an allowed suffix; symlinked
  documents are neither listed nor read; non-UTF-8 and >256 KiB files are
  not served. Membership is checked first (`ViewerProject`), so another
  project's knowledge is `404`.
* API: `GET /api/v1/projects/{id}/knowledge`,
  `GET /api/v1/projects/{id}/knowledge/{name}` (viewer+). No write route.
* UI: *Knowledge Base* in the project sidebar — document list, document
  text shown as plain text (never rendered as HTML), `?doc=` deep links,
  a read-only notice, and an "unavailable on this deployment" state.

### 2.1 Production packaging (read-only)

`compose.control-center.yml`: `smm-api` only gets a literal bind mount
`/srv/ai-smm/knowledge -> /srv/ai-smm/knowledge`, `read_only: true`,
`create_host_path: false`, and `AI_SMM_KNOWLEDGE_ROOT` as a literal —
the worker's host directory and container path, so the API lists the
same files the agents' loader reads. No new interpolation variable, so
no production env file and no wrapper allow-list changes. The mutation
override does not touch volumes. The compose pin in `cc-gate.sh` is
updated. The local runtime (`compose.runtime.yml`) mounts `./knowledge`
the same way. Operator checks before and after `up`:
[control-center-deployment.md §2.3](../control-center-deployment.md#23-knowledge-base-mount-smm-034a).

## 3. Minimal safe way to add editing (proposal, needs owner decision)

Writing must not go into the image, must not widen the worker's mount,
and must not give the Control Center write access to anything else. The
smallest path that satisfies that:

1. **Storage (ops, production change).** Make the host directory
   writable by the API's uid only, e.g. `chown -R 10001:10001
   /srv/ai-smm/knowledge/projects` and `chmod 755/644` — after a backup.
   The worker keeps its `:ro` mount, so agents still cannot write.
2. **Mounts (compose change under review).** The read-only mount of
   §2.1 stays. Only `compose.control-center.mutations.yml` (mutation
   mode, approved per release) would add a writable mount of
   `projects/` alone; `editorial_policy.md` is never mounted writable.
3. **API.** `PUT /api/v1/projects/{id}/knowledge/{name}` — `OwnerProject`,
   CSRF, the central mutation gate, existing `.md` files only (no create,
   rename or delete), body `{content, expected_sha256}`; refuse on a
   SHA mismatch (409, the optimistic lock this slice already exposes),
   size limit, UTF-8. Write atomically: temp file in the same directory,
   `fsync`, `os.replace`; refuse symlinks (`O_NOFOLLOW`).
4. **Audit.** One `audit_log` row per save (actor, project, document,
   old/new SHA-256). Today `ai_smm_cc_rw` writes audit only inside its
   guarded functions, so this needs a grant or a small `SECURITY DEFINER`
   function — a database-role change, CRITICAL tier.
5. **Agents.** No loader change: the worker reads files on each run, so a
   saved document is used by the next generation job.

Rejected: writing to the image or `/tmp`; a Docker volume the worker
cannot see; storing documents in PostgreSQL (a second source of truth
for the loader); giving the worker a writable mount.

## 4. Known limitations of the slice

* Production shows "unavailable" until a release with §2.1 is approved
  and deployed.
* Only top-level documents are shown, as the loader reads them;
  `assets/README.md`, `snippets/README.md` and `editorial_policy.md` are
  not. A file the loader would read but whose name falls outside the
  allowed pattern (e.g. a leading dot or a space) is not listed.
* Markdown is shown as source text, not rendered.
