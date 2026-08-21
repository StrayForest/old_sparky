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

## Verification

- Platform tests: from `platform/`, `tools/platform_run_quiet.sh "platform tests" -- tools/platform_run_tests.sh discover -s tests`.
- Migration: `cd platform && tools/platform_run_alembic.sh upgrade head`.
- Web build: from `platform/`, `tools/platform_run_quiet.sh "web build" -- tools/platform_web_npm.sh --prefix apps/platform_web run build`.
- Docs: `cd platform && .venv_platform/bin/python tools/platform_docs_check.py`.
- Release: use the current commands in `platform/docs/deployment-runbook.md`.

Done means verified and published to GitHub, or any skipped verification/publication is explicitly named with the reason.
