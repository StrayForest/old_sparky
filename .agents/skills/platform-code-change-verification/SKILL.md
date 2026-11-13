---
name: platform-code-change-verification
description: Use after platform code, migration, frontend, build, test, or release-tooling changes.
---

# Platform Code Change Verification

Select the smallest relevant verification set.

## Checks

- From `platform/`, use `tools/platform_verify.py impact --changed-path <repo-relative-path>`
  to identify complete owner gates for each changed path. The impact plan is a
  selector, not a replacement for those gates or exact-SHA CI. Unknown,
  sensitive or mixed changes require the full route.
- Run the canonical owner selectors from the plan: `backend` for API/domain
  behavior, `migration` for schema/data changes, `web-quality` and
  `web-hermetic` for web behavior, and `python-quality`, `security`, `docs`
  and `verification-contract` when their owners are affected. Use focused
  selectors only for local diagnosis; do not use raw test discovery or direct
  Alembic commands in place of owner gates. Gate prerequisites and contour
  ownership are defined in `platform/docs/test-suite-governance.md`.
- Preserve behavior-specific coverage for workflow races, final persisted
  constraints, populated migration upgrades/retries and release-state-machine
  recovery. The test-suite governance document assigns these scenarios to
  their canonical contours.
- For production release and rollback, follow
  `platform/docs/deployment-runbook.md`. Require successful exact-SHA Security
  and the automatic AUTO/production-child chain. A normal app release also
  requires the source-bound restore-verified backup. Host-tools provisioning
  follows its owner runbook when the pin changes. Do not invoke
  `platform_release_deploy.sh` directly or manually dispatch the normal
  production deployment. A retained migration/post-restart state is a
  recovery outcome, not a passing release.
- Docs/skills-only changes use `docs`; instruction or workflow-policy changes
  also use `verification-contract`. Do not run application gates unless
  behavior changed.

Successful checks should emit only their final status line. Read the retained
failure log only after a non-zero exit; do not stream normal build/test output
into the agent context.

## Output

Report commands run, pass/fail/skipped status, key failure lines, and remaining risk.
