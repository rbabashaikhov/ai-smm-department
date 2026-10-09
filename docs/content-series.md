# Content series

Audience: whoever plans content and operates the queue.

A series is a set of publications meant to be read as one narrative rather
than as independent posts. Membership is entirely optional: a publication
with no series behaves exactly as it did before series existed, which is
what keeps the publications already in production working untouched.

---

## 1. Two strategies

| | `standalone_series` | `reply_thread` |
|---|---|---|
| Each part is | its own top-level post | the first is a post, the rest are replies |
| A reader arriving at part 3 | gets a complete thought | sees the thread above it |
| Part numbering in the text | **required** — nothing else tells the reader where they are | **forbidden** — the thread order is already visible |
| Media | any part may carry images | opening post only; Threads replies are text |
| Order | enforced by default, can be waived | always enforced — a reply needs its parent's real id |

The choice is the Strategist's, and it is recorded on the series.

---

## 2. Data model

```
content_series
  id, project_id, title, description, narrative_goal, target_audience,
  publishing_strategy, status, enforce_order, planned_total,
  created_at, updated_at

publications  (all five added columns are nullable)
  series_id                 -> content_series.id
  series_position           1-based position within the series
  series_total              how many parts, denormalised for "2/3"
  parent_publication_id     reply_thread: the post this one answers
  previous_publication_id   the part before this one, in either strategy
```

Constraints that carry weight:

* `uq_publications_series_position` — unique `(series_id, series_position)`,
  **partial** on `series_id IS NOT NULL`, so the rows outside any series do
  not compete for positions;
* `ck_publications_series_membership` — a position without a series, or a
  series without a position, is rejected: ordering would be undecidable;
* `ck_publications_parent_not_self` / `..._previous_not_self` — no
  one-element cycles;
* the foreign keys use `ON DELETE SET NULL`, so removing a series leaves
  its publications intact rather than deleting published work.

`parent_publication_id` and `previous_publication_id` are separate on
purpose. They normally point at the same row, but reply structure and
narrative order need not coincide — a thread that branches would have them
differ, and collapsing them now would make that impossible later.

---

## 3. When a part may be published

`check_series_readiness` decides, and it returns rather than raises, so a
worker can put a record back instead of burning a retry on something that
is simply not its turn:

1. No series → ready. Nothing else applies.
2. Series cancelled → not ready.
3. `enforce_order` and an earlier part is not `published` → not ready, with
   the blocking parts named.
4. `standalone_series` → ready.
5. `reply_thread`, position 1 → ready; it is an ordinary post.
6. `reply_thread`, later position → ready **only** when the parent is
   `published` *and* carries a `threads_post_id`, which is then handed to
   the API as `reply_to_id`.

A part that is not ready is released back to `scheduled` with
`next_attempt_at` ten minutes out. `attempt_count` is untouched: waiting is
not a failed attempt, and counting it would eventually exhaust the retry
budget of a part that never did anything wrong.

---

## 4. The agents

**Strategist** plans the series as a whole before planning any part:
`series_title`, `target_audience`, `narrative_goal`, `planned_total` and a
`publishing_strategy` with a stated reason. Each part then gets a
`position`, a `main_thesis`, a `standalone_value`, and the transitions to
and from its neighbours. Positions are renumbered in code afterwards rather
than trusted to the model, because a gap or a duplicate would reach the
queue as a broken series.

**Copywriter** receives strategy-specific rules. For `standalone_series`
every part carries a title, the series label with its number, a one-line
link back when it is not the first, one thesis, a close, and an announcement
of the next part when one exists. For `reply_thread` the first part sets the
subject and each reply continues it — no repeated introductions, no manual
numbering.

**Editor** reviews each post *and* the series as a whole. Deterministic
continuity checks run before the model sees the draft and cannot be
overridden by it: numbering, duplicate or uninformative titles, identical
text, repeated openings, two parts arguing the same thesis, a part sharing
no subject matter with the one before it, replies that number themselves or
restate the opening, media in a reply, and the length limit. The model adds
a `narrative_score` and its own observations. Any deterministic continuity
problem blocks approval, exactly as a factual error does.

The subject-matter comparison ignores the series scaffolding — the words
"часть", "серия" and the series title itself — because those appear in every
part by design and would otherwise make any two parts look related.

**Publisher** enforces order and resolves the reply target. A reply carrying
media is refused: Threads replies are text only, and sending one anyway
would fail in a way that leaves the outcome unclear.

---

## 5. Operating a series

```bash
cd /root/ai-smm

docker compose --profile cli run --rm -T cli series < /dev/null
docker compose --profile cli run --rm -T cli series-show 1 < /dev/null

docker compose --profile cli run --rm -T cli series-create \
  --project ai-catalog-consultant \
  --title "AI Catalog Consultant" \
  --goal "почему детерминированный выбор победил vector search" \
  --audience "инженеры, строящие LLM-продукты над каталогом" \
  --strategy standalone_series --total 3 < /dev/null

# Place an existing publication at a position. Never touches its text,
# its status or its Threads post id.
docker compose --profile cli run --rm -T cli series-attach 1 2 \
  --position 2 --total 3 < /dev/null
```

`series-show` prints each part with its readiness, so "why has part 3 not
gone out" is answerable in one command.

---

## 6. Registering the existing AI Catalog Consultant series

Publications 1 and 2 are live; 3 is not. The goal is to record the series
that already exists, without touching a single published byte.

**What this must not do:** change the text, images or `threads_post_id` of
anything published; publish part 3; create a duplicate row.

`series-attach` writes only `series_id`, `series_position`, `series_total`
and the neighbour links, and the CLI re-reads status, `threads_post_id` and
`published_at` afterwards and rolls back if any of them moved.

```bash
cd /root/ai-smm

# 1. Snapshot first. This is a write to production data.
/root/ai-smm/backup.sh

# 2. Create the series. No publication is touched yet.
docker compose --profile cli run --rm -T cli series-create \
  --project ai-catalog-consultant \
  --title "AI Catalog Consultant" \
  --description "Как строился AI-консультант по каталогу телевизоров" \
  --goal "почему выбор товаров стал детерминированным, а vector search — подтверждающим" \
  --audience "инженеры, строящие LLM-продукты поверх каталога" \
  --strategy standalone_series --total 3 < /dev/null

# 3. Attach the three publications, in order.
for pair in "1 1" "2 2" "3 3"; do
  set -- $pair
  docker compose --profile cli run --rm -T cli series-attach <series_id> "$1" \
    --position "$2" --total 3 < /dev/null
done

# 4. Verify: parts 1 and 2 still published with their original ids.
docker compose --profile cli run --rm -T cli series-show <series_id> < /dev/null
docker compose --profile cli run --rm -T cli queue < /dev/null
```

Expected afterwards: the series reads `active` (2 of 3 published), parts 1
and 2 keep `threads_post_id` `17956888254278916` and `18075070049556193`,
part 3 stays `approved`, `sched=none`, `reviewed=no` — unschedulable until
it is approved and scheduled deliberately.

**Accepted deviation.** Parts 1 and 2 do not carry the "часть N из 3" label,
because they were written and published before the series existed and will
not be edited. The draft-time continuity check would flag that, which is
correct for new content; it does not apply here, and
`validate_series_structure` — the check that runs against the database —
does not look at labels. Part 3 carries its label, so a reader arriving at
it knows there are two more.

### Proposed text for publication 3

**Not written to the queue.** This is a proposal; the record still holds the
original text until it is approved.

```
Запрос «подешевле» сломал красивую архитектуру
AI Catalog Consultant — часть 3 из 3

В прошлой части выбор товаров держался на SQL и детерминированном
ранжировании. Это работает, пока ограничение можно посчитать.
«Подешевле» — нельзя: модель придумывала значения, которых нет в каталоге.

Семантический защитник блокировал их до вызова инструментов, но ответ мог
повторить. Reject-and-retry не сработал. Ограничение остаётся открытым.
```

434 characters, within the 450 limit for a post carrying media. Against the
current 407-character text it adds a title, the series label, and an opening
line that links back to publication 2 — the three things the current text
lacks as the closing part of a series. The facts are unchanged: the
semantic guard, the failed reject-and-retry, and the limitation still being
open. No brand name and no store link in the caption, as before.

The image stays `telegram-04-tradeoff.jpg`, unchanged.

---

## 7. Risks and limitations

| | Risk | Why it is bounded |
|---|---|---|
| R1 | A part is stuck in `needs_review`, blocking the rest of the series | By design: stepping over it would publish part 3 before part 2. `series-show` names the blocker |
| R2 | A reply is attempted against a parent whose publish was ambiguous | The parent stays `needs_review` and never reaches `published`, so the reply is never released |
| R3 | A long series ties up the queue — one part per poll, held back by order | `enforce_order` can be waived for `standalone_series`; never for a thread |
| R4 | The continuity checks are heuristics: word overlap and phrase matching | They block approval, so a false positive costs a revision, not a bad post. They are deliberately deterministic so the model cannot wave a real problem through |
| R5 | A reply carrying media fails after the point of no return | Caught by the Editor before publishing; at publish time it becomes `needs_review` rather than a silent failure |
| R6 | Threads could change how replies are addressed | `reply_to_id` comes from a stored `threads_post_id`; nothing is derived or guessed |
| R7 | The migration adds columns to a table holding live rows | Additive only: no existing row is read or written. Verified against a seeded pre-series row, including a downgrade and re-upgrade |
| R8 | `series_total` is denormalised and can disagree with reality | `validate_series_structure` reports the disagreement; `series-show` prints it |

---

## 8. Deployment — done 2026-10-09

Production runs `ai-smm:706b383` at schema `79731eadacd7`. The series is
registered as id 1; publication 3 is attached but unscheduled and
unreviewed, so nothing can publish it.

The plan below is kept as the record of what was done and as the
procedure for the next schema change.

| Phase | Action | Rollback | Status |
|---|---|---|---|
| 1 | Rehearse the migration against a restored copy of the production dump | — | done |
| 2 | Build and transfer the image; keep `be3bc75` on the host | delete the new image | done |
| 3 | `backup.sh`, and restore it into a throwaway database to prove it works | — | done |
| 4 | `compose run --rm migrate upgrade head` → `79731eadacd7` | `downgrade a3fbcef157c3`; the columns are additive, so nothing is lost | done |
| 5 | Recreate the worker on the new image, still `AI_SMM_DRY_RUN=true` | `AI_SMM_VERSION=be3bc75` | done |
| 6 | Verify: queue unchanged, parts 1–2 still published, worker healthy | as above | done |
| 7 | Register the existing series (section 6) | `UPDATE publications SET series_id=NULL, series_position=NULL, series_total=NULL, previous_publication_id=NULL WHERE series_id=1; DELETE FROM content_series WHERE id=1;` | done |
| 8 | Leave part 3 unscheduled until its text is approved | — | done |

**The version pin is the thing to get right.** `AI_SMM_VERSION` in
`/root/ai-smm/.env` selects the image for *every* service in the project,
including the one-shot `migrate` container. Running `migrate upgrade head`
while the pin still named the old image was a no-op that reported success:
`head` meant the old image's head, which was already applied. Update the
pin before running the migration, and check the revision afterwards rather
than trusting the exit code.

The migration can be applied before the new image runs: the previous code
ignores columns it does not know about. Running the migration and the
deployment as separate steps means either can be reverted alone.

Acceptance after phase 6, before anything else:

```bash
docker compose --profile cli run --rm -T cli queue < /dev/null
docker exec <postgres-container> psql -U <user> -d ai_smm -c "
  select id, status, threads_post_id, series_id from publications order by id;"
```

Parts 1 and 2 must still be `published` with their original ids, and all
three must still show `series_id` NULL until phase 7 runs.

### What was verified on the real data

Before touching production, the dump was restored locally and the
migration *and* the series registration were replayed against it. Body and
image hashes were compared against a pristine restore of the same dump and
matched exactly. The same comparison was then run on production either
side of the registration, and came out identical: no text, image, status,
Threads post id, publication date or approval flag moved.

Rollback was rehearsed from the migrated state *with the series already
registered*: the downgrade drops the series and its links and leaves all
three publications byte-identical to the pre-migration dump.
