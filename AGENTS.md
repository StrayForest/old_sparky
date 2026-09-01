# OldSparky Agent Guide

## Priority

- This is a site-only repository; application work belongs under `platform/`.
- Read `platform/docs/CURRENT.md`, then use `platform/docs/README.md` to route
  the task to its owner document. Keep routine context focused.
- Preserve unrelated dirty files and never commit secrets, `.env*` files,
  tokens, passwords, cookies, private reports or live personal data.

## Scope and architecture

- Active application areas are `platform/apps`, `platform/python_packages`,
  `platform/alembic`, `platform/deploy`, `platform/tools` and `platform/tests`.
- Platform data uses `platformdb`, schema `platform`; never touch the retired
  legacy bot or `sparkydb` from platform work.
- Keep routes/handlers thin, domain rules in services, persistence in models/
  migrations/repositories, and release behavior in `platform/tools`.
- Use `apply_patch` for manual edits. Prefer `rg` and focused reads; do not
  scan dependency trees, generated output or session history without a reason.

## Instruction and skill hierarchy

- Root rules apply globally. More specific `platform/AGENTS.md` and
  `platform/apps/platform_web/AGENTS.md` add path-specific rules.
- For docs, AGENTS or skills, use `$platform-documentation-maintenance` and
  read `platform/docs/documentation-governance.md`.
- Use `$openai-knowledge` for OpenAI/Codex/API/model/prompt documentation;
  `$platform-implementation-strategy` before platform API, schema, permission,
  workflow, runtime, release or cross-module changes;
  `$deadlock-workflow-guardrails` before Deadlock workflow changes;
  `$frontend-ux-polish` for meaningful web UI changes;
  `$platform-performance-monitoring` for performance/observability work;
  `$platform-launch-qa` for launch or live QA; and `$release-handoff-summary`
  at the end of substantive work.
- After any code, docs, skill, test or tooling package, use
  `$code-cleanup-after-changes` and `$platform-code-change-verification`.

## Change and verification rules

- Define behavior, owner layer, permissions, data impact, rollback risk and
  focused tests before changing behavior. Put test placement and gate ownership
  in `platform/docs/test-suite-governance.md`.
- Use the canonical registry `platform/tools/platform_verify.py`; run it from
  `platform/` through the quiet wrapper when normal output is unnecessary.
- A missing safe dependency is `LOCAL GATE BLOCKED`, not a reduced pass. Do not
  hide failures with exclusions or silent retries.

## Production and publication

- GitHub Actions is the release authority. A reviewed push to `dev` must pass
  exact-SHA `Platform security and build`, then the automatic production
  chain; do not manually dispatch normal production deployment or call low-
  level release installers directly. Follow `deployment-runbook.md` for the
  exceptional recovery/fallback paths.
- Substantive work is complete only after applicable checks, stale-artifact
  cleanup, a coherent commit, push to the matching branch, and a post-push
  fetch proving local `HEAD == origin/<branch>`. Never force-push automatically.
- If publication or a required external gate is unavailable, name it clearly in
  the handoff instead of claiming completion.
