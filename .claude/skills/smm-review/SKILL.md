---
name: smm-review
description: Perform an independent, evidence-based review of a diff or implementation. Invoke when a completed change needs review before acceptance.
---

# SMM Review

Perform an independent, evidence-based code review. Stay read-only unless explicitly authorized otherwise. Consult `docs/DEVELOPMENT_POLICY.md` for policy rules and security boundaries.

## Steps

1. **Read the diff or specified files.** Use `git diff` or the named files. Do not expand scope to unrelated code.
2. **Check acceptance criteria and security boundaries.** Read the task description, relevant ADR, and security gates that apply.
3. **Find actual defects.** Focus on correctness bugs, regressions, and meaningful risks. Skip speculative findings and unrelated refactoring suggestions.
4. **Classify each finding:**
   - **P0** — data loss, security breach, production outage, or bypass of approval gates.
   - **P1** — incorrect behavior that breaks a stated acceptance criterion.
   - **P2** — likely edge-case failure or degraded behavior under realistic conditions.
   - **P3** — minor quality issue with low impact.
5. **For each finding provide:**
   - File and line reference (exact when available).
   - One-sentence description of the defect.
   - Impact: what breaks or is at risk.
   - Smallest reasonable correction.
   - Confidence: VERIFIED (reproducible from code) or HYPOTHESIS (inferred).
6. **Report** in a compact table or list ordered P0→P3. State the total count per priority. If no findings, say so explicitly.

## Boundaries

- Read-only. Do not edit files, commit, or trigger any action unless the task explicitly authorizes it.
- Do not perform a broad repository audit beyond the specified diff or files.
- Do not report findings outside the agreed scope as blockers.
