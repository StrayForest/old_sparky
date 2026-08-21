---
name: platform-code-change-verification
description: Use after platform code, migration, frontend, build, test, or release-tooling changes.
---

# Platform Code Change Verification

Select the smallest relevant verification set.

## Checks

- API/domain: run from `platform/` as
  `tools/platform_run_quiet.sh "platform tests" -- .venv_platform/bin/python -m unittest discover -s tests`
  (from repo root:
  `cd platform && .venv_platform/bin/python -m unittest discover -s tests`).
  Do not run `platform/.venv_platform/bin/python -m unittest discover -s platform/tests`
  from repo root: it can collide with Python's stdlib `platform` module and
  leave `apps.*` imports unresolved.
- Migration: `cd platform && tools/platform_run_alembic.sh upgrade head`
- Web: run from `platform/` as
  `tools/platform_run_quiet.sh "web build" -- tools/platform_web_npm.sh --prefix apps/platform_web run build`.
- Release from the repository root:
  `platform/tools/platform_run_quiet.sh "release preflight" -- platform/tools/platform_release_preflight.sh --require-previous`;
  `platform/tools/platform_run_quiet.sh "deploy smoke" -- platform/.venv_platform/bin/python platform/tools/platform_deploy_smoke.py`.
- Codex/docs-only: syntax checks only; do not run app tests unless behavior changed.

Successful checks should emit only their final status line. Read the retained
failure log only after a non-zero exit; do not stream normal build/test output
into the agent context.

## Output

Report commands run, pass/fail/skipped status, key failure lines, and remaining risk.
