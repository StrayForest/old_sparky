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
- Prefer focused reads and quiet verification output.
- Completed substantive work must be committed and pushed to the matching GitHub branch; verify local `HEAD` equals `origin/<branch>` after push.
- Never force-push automatically.

## Production deployment

- Start normal production releases only through the GitHub Actions `Platform
  production deploy` workflow from the reviewed `dev` branch:
  `gh workflow run platform-production-deploy.yml --repo StrayForest/old_sparky --ref dev --field mode=deploy`.
- Wait for the Actions run and include its run ID/URL and live result in the
  handoff. A push to `dev` alone is not a deployment.
- Do not run `tools/platform_build_release.sh` or
  `tools/platform_release_deploy.sh` directly from an agent shell for a normal
  release; direct server execution is recovery/rollback-only and requires
  explicit operator authorization.

## Verification

- API/domain: `.venv_platform/bin/python -m unittest discover -s tests` from this directory.
- Migration: `tools/platform_run_alembic.sh upgrade head`.
- Web: `cd apps/platform_web && npm run build`.
- Release: follow `docs/deployment-runbook.md` and the current CSP mode documented in `docs/CURRENT.md`.
