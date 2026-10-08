# Real failures and lessons

## Vector search was not enough
Exact constraints and semantic similarity are different problems. Final design: structured selection + semantic evidence.

## Whole catalog context was not the answer
More context introduced transcription errors, including a non-existent model code, wrong price copied from a neighbouring row and wrong spec copied from another product.

## n8n zero-item trap
A query returning zero rows meant IF never ran. Fix: `SELECT count(*)::int` so a row always exists before branching.

## Re-indexing initially destroyed embeddings
Delete/reinsert cleared embeddings unnecessarily. Fixed with section + `content_hash` sync.

## Invented hard constraints
Observed patterns included PS5 → invented HDMI 2.1 hard requirement and vague «подешевле» → invented `max_price`. Semantic guard blocks these from the catalog query.

## Known remaining limitation
The guard can remove the invented number from the tool call, but final wording may still repeat it. In Phase 4F.3 the unsupported number was removed in 32/32 sessions, yet appeared in wording in most sessions. Reject-and-retry was tested and reverted.
