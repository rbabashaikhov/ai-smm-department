---
name: smm-acceptance
description: Evaluate a completed development task against agreed acceptance criteria. Invoke after implementation and review are done, before owner sign-off.
---

# SMM Acceptance

Evaluate a completed task against its acceptance criteria. Consult `docs/DEVELOPMENT_POLICY.md` for policy gates that must be satisfied. This skill produces a recommendation only — it is not authorization to commit, merge, or deploy.

## Steps

1. **Read the original task.** Identify every stated acceptance criterion and the agreed scope.
2. **Collect evidence.** Read the relevant diff, test results, and any demonstration outputs provided. Do not assume evidence that was not provided.
3. **Check Development Policy gates** applicable to the risk level (§3 and §4):
   - LIGHT: static checks passed, no runtime behavior changed unintentionally.
   - STANDARD: targeted tests pass, linters and type checks clean.
   - CRITICAL: security-gates satisfied, rehearsal passed, human approval obtained for every production action; full regression complete before the critical release (not required for local implementation readiness alone — see `docs/DEVELOPMENT_POLICY.md` §3).
4. **Assess each criterion:**

   | Criterion | Status | Evidence | Gap / Risk |
   |-----------|--------|----------|------------|
   | \<criterion> | PASS / FAIL / BLOCKED | \<what was checked> | \<missing or residual> |

5. **Distinguish behavior categories:**
   - **Implemented** — code change is present and complete.
   - **Tested** — automated or manual test confirms the behavior.
   - **Production-verified** — behavior confirmed on the live system.
6. **Identify missing evidence** and residual risks explicitly.
7. **Final recommendation:**
   - **ACCEPT** — all criteria PASS, no P0/P1 gaps, policy gates satisfied.
   - **REWORK** — one or more criteria FAIL or P0/P1 finding present.
   - **BLOCKED** — missing owner decision, security blocker, or required evidence unavailable.

## Boundaries

- Read-only and advisory only.
- Acceptance evaluation is not authorization for commit, merge, deploy, or production action.
- Do not mark a criterion PASS based on incomplete or assumed evidence.
