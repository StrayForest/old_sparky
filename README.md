> [!IMPORTANT]
> **Source-available for review only.** No permission is granted to use, copy, modify, distribute, deploy, host, or create derivative works from this code without prior written permission. See [LICENSE](LICENSE).

# OldSparky

OldSparky is the production web platform for Deadlock tournament operations.

## Active product

All active application code lives under [`platform/`](platform/):

- `platform/apps/platform_web` — Next.js web UI;
- `platform/apps/platform_api` — FastAPI HTTP/API layer;
- `platform/apps/platform_worker` — Celery/background work;
- `platform/python_packages` — domain and infrastructure packages;
- `platform/alembic` — database migrations for `platformdb.platform`;
- `platform/deploy` and `platform/tools` — release, runtime, QA and operations;
- `platform/tests` — backend/integration regression coverage;
- `platform/docs` — current architecture, product, security and operational documentation.

## Production contour

The active production stack uses Next.js, FastAPI/Gunicorn, Celery, PostgreSQL, Redis, Nginx, Cloudflare and R2/CDN. It includes Steam OpenID authentication, tournament lifecycle management, deterministic Deadlock team assignment, locked rosters, bracket progression, security controls, immutable releases and rollback tooling.

## Start here

For engineering work:

1. read [`AGENTS.md`](AGENTS.md);
2. read [`platform/AGENTS.md`](platform/AGENTS.md);
3. read [`platform/docs/CURRENT.md`](platform/docs/CURRENT.md);
4. use [`platform/docs/README.md`](platform/docs/README.md) to open only the task-specific owner documents.

Do not scan the whole repository or documentation tree by default.

## Git workflow

Verified substantive work is not complete until it is committed and pushed to the matching GitHub branch. A local-only server commit is not a completed handoff. Follow [`platform/docs/development-guide.md`](platform/docs/development-guide.md) for the safe commit/push procedure.
