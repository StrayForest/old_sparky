# OldSparky Agent Guide

## Priority

- This repository is site-only. Default scope is `platform/`.
- Read `platform/docs/CURRENT.md` first for current state and priority.
- Use `platform/docs/README.md` as the task router; open only the relevant owner documents.
- Optimize Codex limits: targeted reads, bounded output, quiet checks, no unnecessary subagents.

## Boundaries

- Active application: `platform/apps`, `platform/python_packages`, `platform/alembic`, `platform/deploy`, `platform/tools`, `platform/tests`.
- Platform data belongs in `platformdb`, schema `platform`.
- Never commit secrets, `.env*`, tokens, passwords, cookies, private reports or live operator credentials.
- Preserve unrelated dirty files; do not revert user changes.

## Required Skills

- `$openai-knowledge`: OpenAI/Codex/API/model/prompt docs.
- `$platform-implementation-strategy`: platform API/schema/runtime/permission/workflow changes.
- `$deadlock-workflow-guardrails`: Deadlock ready/captain/dream-slot/assignment/roster/bracket changes.
- `$frontend-ux-polish`: meaningful `platform/apps/platform_web` UI work.
- `$platform-performance-monitoring`: performance monitoring, load-test instrumentation and bottleneck analysis.
- `$platform-code-change-verification`: after platform code/tests/migrations/build/release changes.
- `$code-cleanup-after-changes`: after code, UI, API, test, docs, skill or tooling changes.
- `$platform-launch-qa`: production QA or blocker triage.
- `$release-handoff-summary`: final handoff for substantive work.

## Context Rules

- Start with `rg` or focused file reads; avoid full-repo scans unless required.
- For large docs/logs/reports, read indexes or bounded ranges first and summarize retained evidence instead of dumping raw files.
- Never recursively search session history, dependency trees, build output or unbounded generated directories.
- Run tests/builds/lint/release checks through `platform/tools/platform_run_quiet.sh` when live output is unnecessary.
- Keep quality gates intact while saving context: focused checks first, broader checks only when risk/release scope requires them.

## Architecture Rules

- Keep routes/handlers thin.
- Keep domain/workflow rules in domain/services.
- Keep persistence in models/migrations/repositories.
- Use `apply_patch` for manual edits when working locally.

## Completion and GitHub Publication

For completed substantive work:

1. run the applicable verification;
2. remove stale/replaced artifacts;
3. commit the coherent verified package;
4. `git fetch origin`;
5. push the current branch to the matching GitHub branch;
6. fetch again and verify local `HEAD` equals `origin/<branch>`.

A local-only commit is not a completed handoff. Never automatically use `--force` or `--force-with-lease`; reconcile any divergence first.

## GitHub CI/CD is the release authority

- Do not run platform tests, builds or migrations manually from the Codex shell
  for normal work. These checks are owned by GitHub Actions; a local result is
  neither required nor sufficient for release authorization.
- If a GitHub job fails, use its GitHub Actions logs as the first diagnostic
  source. Run a local reproduction only when explicitly requested or when it
  is necessary to isolate the CI failure, and never treat that reproduction as
  the release gate.
- After pushing to `dev`, wait for the GitHub Actions
  `Platform security and build` workflow for the exact target SHA to finish.
  Inspect the backend job and every required job; the aggregate
  `platform-security-build` commit status must be `success`.
- Never dispatch `Platform production deploy` while the security/build workflow
  is pending, failing or still running. Do not run deployment in parallel with
  CI, even when local verification is green.
- If local and GitHub results differ, treat the GitHub result as authoritative
  for release gating, investigate the environment difference, fix it, push the
  fix and repeat the complete CI gate.

## Production deployment

- Normal production releases must be started through the GitHub Actions
  `Platform production deploy` workflow from the reviewed `dev` branch:
  `gh workflow run platform-production-deploy.yml --repo StrayForest/old_sparky --ref dev --field mode=deploy`.
- The deploy workflow must fail closed unless the exact target SHA has a green
  `platform-security-build` status. This gate is checked by GitHub Actions
  before packaging, SSH or any production release side effect.
- Wait for and report the GitHub Actions run and its live deployment result;
  a successful branch push is not a production deployment.
- Do not invoke `platform_build_release.sh` or `platform_release_deploy.sh`
  directly from the Codex shell or production host for a normal release.
  Direct host commands are reserved for an explicitly authorized recovery or
  rollback procedure.

## Verification

- Push the reviewed commit to `dev` and wait for the GitHub Actions
  `Platform security and build` workflow. It owns backend tests, migration
  scenarios, web build/hermetic checks, smoke checks and the aggregate
  `platform-security-build` status.
- Use `gh run watch <run-id> --repo StrayForest/old_sparky --exit-status` and
  inspect the backend job plus every required job before deployment.
- Release verification and live smoke are performed by the GitHub Actions
  `Platform production deploy` workflow described in
  `platform/docs/deployment-runbook.md`.

Done means verified and published to GitHub, or any skipped verification/publication is explicitly named with the reason.
