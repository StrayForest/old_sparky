# OldSparky Agent Guide

## Priority

- The root agent is orchestration-only: delegate all substantive work—including research, project reads, edits, checks, Git/CI and publication—to subagents; then synthesize their results, coordinate dependencies, and report progress and the final outcome. Every subagent, including nested subagents, must run exclusively as `gpt-5.6-luna` with `reasoning_effort=max`; when explicitly overriding the model, set `fork_turns` to `none` or a positive number (never a full-history fork).
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
- A successful security/build run caused by a push to the current `dev` HEAD
  automatically feeds `Platform production auto-deploy`. That workflow must
  re-check that the tested SHA is still the current `dev` HEAD, require the
  exact-SHA security status and skip a SHA already marked successfully deployed.
- Do not manually dispatch production while the security/build workflow is
  pending, failing or still running. Manual `Platform production deploy` is an
  operator fallback, not the normal release path.
- If local and GitHub results differ, treat the GitHub result as authoritative
  for release gating, investigate the environment difference, fix it, push the
  fix and repeat the complete CI gate.

## Production deployment

- Normal production releases start automatically after a reviewed commit is
  pushed to `dev` and the exact-SHA `Platform security and build` push run
  succeeds. `Platform production auto-deploy` validates that successful run is
  for the current `dev` HEAD and dispatches `Platform production deploy` with
  `mode=deploy`.
- The deploy workflow repeats the fail-closed exact-SHA
  `platform-security-build=success` check before packaging, SSH or any
  production release side effect.
- Watch and report all three GitHub Actions stages for the same target SHA:
  security/build, auto-deploy and production deploy/live smoke. A successful
  branch push alone is not a production deployment; a successful current-head
  security/build run is expected to continue into the automatic deploy chain.
- Use manual `Platform production deploy` only as an explicitly justified
  operator fallback or read-only preflight path. Never use it to bypass a
  pending, failed, missing or stale security/build result.
- Do not invoke `platform_build_release.sh` or `platform_release_deploy.sh`
  directly from the Codex shell or production host for a normal release.
  Direct host commands are reserved for an explicitly authorized recovery or
  rollback procedure.

## Verification

- Push the reviewed commit to `dev` and wait for the GitHub Actions
  `Platform security and build` workflow. It owns backend tests, migration
  scenarios, web build/hermetic checks, smoke checks and the aggregate
  `platform-security-build` status.
- After that exact-SHA push run succeeds, verify that
  `Platform production auto-deploy` accepts the same current `dev` HEAD and
  dispatches the production workflow. Then wait for the production deployment
  and live smoke to finish.
- Use `gh run watch <run-id> --repo StrayForest/old_sparky --exit-status` and
  inspect failed GitHub Actions jobs before attempting any local reproduction.
- Release verification and live smoke are performed by the GitHub Actions
  `Platform production deploy` workflow described in
  `platform/docs/deployment-runbook.md`.

Done means verified and published to GitHub, or any skipped verification/publication is explicitly named with the reason.
