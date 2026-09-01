# Platform Agent Guide

## Scope

- This directory is the production web platform and the only application scope in this branch.
- MVP/production priority: live HTTPS safety, security hardening and organizer/player/admin flows.

## Layers

- API routes: `apps/platform_api/app/api/routes/`; keep them thin.
- API contracts: `apps/platform_api/app/api/schemas.py`.
- Domain rules: `python_packages/platform_domain/`.
- Infra, config, SQLAlchemy models, security, audit: `python_packages/platform_infra/`.
- Web UI/API client/types/i18n: `apps/platform_web/`.
- Migrations: `alembic/versions/`, targeting `platformdb.platform`.

## Invariants

- Profile-level Deadlock dream slots are the source of truth.
- Invite-only reads stay scoped to organizer, participants and admins.
- Terminal tournament states freeze organizer match administration.
- Preserve the active `https://old-sparky.com` domain, secure-cookie and Cloudflare-origin contour unless a reviewed release explicitly changes it.
- Use release scripts under `tools/`; rollback does not reverse DB migrations automatically.

## Context and completion

- Read `docs/CURRENT.md`, then use `docs/README.md` to select only task-relevant documents.
- For docs, AGENTS or skills, also read `docs/documentation-governance.md` and
  use `$platform-documentation-maintenance`.
- Prefer focused reads and quiet verification output.
- Completed substantive work must be committed and pushed to the matching GitHub branch; verify local `HEAD` equals `origin/<branch>` after push.
- Never force-push automatically.

## Production deployment

- Normal production starts with a reviewed push to `dev`. The exact-SHA
  security/build result feeds `Platform production auto-deploy`, which dispatches
  `Platform production deploy` after its current-head checks.
- Wait for the Actions run and include its run ID/URL and live result in the
  handoff. A push to `dev` alone is not a deployment.
- Do not manually dispatch production for the normal `dev` path. Do not run
  `tools/platform_build_release.sh` or
  `tools/platform_release_deploy.sh` directly from an agent shell for a normal
  release; direct server execution is recovery/rollback-only and requires
  explicit operator authorization.

## Verification

- Use `.venv_platform/bin/python tools/platform_verify.py <gate>` from this
  directory; test placement and gate ownership live in `docs/test-suite-governance.md`.
- API/domain: `backend`; migration: `migration`; docs/skills: `docs`;
  web: `web-quality` and, when applicable, `web-hermetic`.
- Release: follow `docs/deployment-runbook.md` and the current CSP mode documented in `docs/CURRENT.md`.
