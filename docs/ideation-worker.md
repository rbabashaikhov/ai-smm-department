# Topic ideation worker (SMM-035B3B1)

Status: **implemented and tested locally with a fake model. Not deployed,
not started, no real model call made.** The API gate
(`Settings.topic_ideation_jobs_enabled`) stays off, so nothing can queue a
job.

## What it does

`ai-smm-ideation-worker` (`src/ai_smm/ideation_worker.py`) polls
`topic_ideation_jobs`, claims one due job, makes **one** structured-output
call (`gpt-4.1-mini`, `src/ai_smm/agents/ideation.py`) and saves the
answer as `content_ideas` rows of that job's project. Then it stops for
that job. It writes no content item, approval or publication, and it is a
separate process from the publish worker and the generation worker.

- **Inputs.** Only what the API copied into `params` when the job was
  queued: the project's content profile snapshot and up to 20 recent idea
  titles, plus the owner's `topic_hint`. The worker reads no table. A job
  queued before this stage (`params = {}`) fails at once, without a model
  call. All three inputs are treated as data, not instructions: each is
  JSON with `<`/`>` escaped inside its own tag, under `UNTRUSTED_DATA_RULE`.
- **Validation.** The answer must contain 1..`idea_count` ideas with
  distinct titles. Bounds: title ≤ 200, description ≤ 2000, angle ≤ 1000
  characters. A title the project already had is dropped. Nothing is
  truncated: the answer is used as it is or not at all. A bad answer fails
  the job without a retry. `topic_ideation_complete` checks the same
  bounds again and saves all of the ideas or none.
- **Retries and cost.** A runtime error (timeout, API error) is retried
  with backoff (30 s base, 600 s cap) up to the job's `max_attempts`
  (default 2). Each claim uses up one attempt, so one job makes at most
  `max_attempts` model calls, even after a crash. `result` records tokens
  and an indicative `cost_usd`.
- **Lost claim.** A result for a claim the worker no longer holds (lease
  expired, job cancelled) is discarded by the contract.

## Database

| Object | Change |
|---|---|
| `topic_ideation_jobs.params` | migration `1c9e4b7d2a05`: `jsonb NOT NULL DEFAULT '{}'`, an object ≤ 64 KiB |
| `guard_topic_ideation_jobs_insert` | additionally checks the shape of `params`: exactly `content_profile` (object) and `recent_idea_titles` (≤ 20 strings of 1..200 chars) |
| `topic_ideation_claim` | also returns `params`; dropped and recreated (changed `RETURNS TABLE`) |
| `ai_smm_cc_rw` | `INSERT (params)` on `topic_ideation_jobs`; verify file and its pin in `cc-gate.sh` updated |
| `ai_smm_ideation` | new role (`deploy/control-center/db/ai_smm_ideation.{grants,verify,revoke}.sql`): `LOGIN NOINHERIT`, limit 5, EXECUTE on the three `topic_ideation_*` contracts only, no table, column, sequence or other callable function |

The worker checks the same rules itself (`--check`, and at start, exit 2).
It refuses superuser access, any table or column access, any other
callable function in `public` (generation and publication contracts
included), a missing contract, or an empty `OPENAI_API_KEY`.

## Order of a future rollout (each step separately approved)

1. Back up the database, then apply migration `b3a7d2e91c4f` (if it is not
   applied yet) and then `1c9e4b7d2a05`. The release image's preflight
   expects `1c9e4b7d2a05`.
2. Re-apply `ai_smm_cc_rw.grants.sql`, then run its verify. The pin in
   `cc-gate.sh` matches the new verify file.
3. Apply `ai_smm_ideation.grants.sql` **after** the migration. Re-apply
   it after any migration that recreates a `topic_ideation_*` contract,
   because dropping a function drops its grants. Then run
   `ai_smm_ideation.verify.sql`: no `FAIL` line.
4. Add a validating wrapper for `compose.ideation.yml`, like
   `gen-compose.sh` (not part of this stage), then run `config` and `up`
   with a dedicated OpenAI key.
5. Enable `topic_ideation_jobs_enabled`, which is still not read from the
   environment.

Rollback: `docker compose -p ai-smm-ideation down`, then
`ai_smm_ideation.revoke.sql`. Downgrading `1c9e4b7d2a05` refuses while
any job carries `params`.
