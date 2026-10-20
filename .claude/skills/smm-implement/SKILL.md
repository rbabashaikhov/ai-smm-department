---
name: smm-implement
description: Implement an approved, minimal vertical slice for AI SMM Department. Invoke when a task has been approved and is ready for implementation.
---

# SMM Implement

Implement an approved, minimal vertical slice. Follow `docs/DEVELOPMENT_POLICY.md` for all rules; this skill references only the delta.

## Steps

1. **Understand scope.** Read the task description and acceptance criteria. Clarify any ambiguity before touching code.
2. **Read relevant files only.** Read the source files, ADR, and runbook that apply to this slice. Do not read unrelated files.
3. **Identify risk level.** Classify as LIGHT / STANDARD / CRITICAL per §3 of the Development Policy. State the classification and reasoning in one sentence.
4. **Implement within scope.** Reuse existing architecture and components. Do not refactor, rename, or clean up adjacent code. Do not add features beyond the agreed criteria.
5. **Run targeted checks** appropriate to the risk level (§3):
   - LIGHT: verify affected files and links; static format/diff checks.
   - STANDARD: targeted unit/integration tests for the changed slice; linters and type checks.
   - CRITICAL: targeted tests + mandatory security-gates and rehearsal checks during development; full regression is required before a critical release, not after every local edit — see `docs/DEVELOPMENT_POLICY.md` §3.
6. **Report** in a short block:
   - Files changed and why.
   - Checks run and their results.
   - Known limitations and deviations from scope.
   - Any blocker requiring owner decision.

## Boundaries

- Stop after local implementation.
- Do not commit, push, merge, deploy, run migrations, or modify production without explicit owner approval.
- If an architectural or security blocker is found, stop the dangerous part and report it immediately.
