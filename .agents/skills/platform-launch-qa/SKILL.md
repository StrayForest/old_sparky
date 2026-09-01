---
name: platform-launch-qa
description: Use for platform launch QA, production smoke tests, launch blockers, or end-to-end platform validation.
---

# Platform Launch QA

Use for MVP launch QA planning, execution, or blocker triage.

## Workflow

- Treat `https://old-sparky.com` as the active production origin. Include
  domain, HTTPS, secure-cookie and Cloudflare Access checks in live QA; use the
  current CSP mode from `platform/docs/CURRENT.md`.
- Use `references/checklist.md` only when detailed scenario coverage is needed.
- Record pass/fail/blocked/not-run for covered scenarios.
- Keep legacy bot and `sparkydb` isolation explicit.

## Output

Return scenario status, blockers, regression risks, checks used, and go-live recommendation.
