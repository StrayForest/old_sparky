---
name: platform-launch-qa
description: Use for MVP no-domain launch QA, smoke tests, launch blockers, or end-to-end platform validation.
---

# Platform Launch QA

Use for MVP launch QA planning, execution, or blocker triage.

## Workflow

- Keep no-domain production boundary in mind; domain/HTTPS checks are deferred.
- Use `references/checklist.md` only when detailed scenario coverage is needed.
- Record pass/fail/blocked/not-run for covered scenarios.
- Keep legacy bot and `sparkydb` isolation explicit.

## Output

Return scenario status, blockers, regression risks, checks used, and go-live recommendation.
