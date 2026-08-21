# Platform development guide

- Status: Active how-to
- Owner: Platform maintainers
- Last reviewed: 2026-08-16

## Scope boundary

All application work belongs under `platform/`. The platform uses `platformdb`, schema `platform`. Preserve unrelated dirty files and never commit `.env*`, credentials, cookies or private reports.

Primary owners:

- `apps/platform_web`: Next.js App Router UI;
- `apps/platform_api`: FastAPI HTTP contracts and adapters;
- `python_packages/platform_domain`: workflow/domain rules;
- `python_packages/platform_infra`: persistence, security and infrastructure;
- `alembic`: expand/contract migrations;
- `tools` and `deploy`: release and runtime operations.

## Read only what the task needs

1. Read [`CURRENT.md`](CURRENT.md).
2. Use [`README.md`](README.md) to choose the one or two owner documents relevant to the change.
3. Search headings or read bounded ranges in long audits/runbooks; do not load the entire documentation tree by default.

## Bootstrap and local run

```bash
cd /root/old_sparky
platform/tools/platform_bootstrap.sh
cd platform
tools/platform_run_alembic.sh upgrade head
```

Review the ignored `platform/.env.platform` before starting anything. Local tests must use environment `test`, database `platformdb_test`, Redis DB 15 and local object storage. The guarded runner rejects production resources.

Start services in separate terminals:

```bash
cd /root/old_sparky/platform
./tools/platform_run_api.sh
./tools/platform_run_worker.sh
./tools/platform_run_web.sh
```

All Node commands go through `tools/platform_node.sh` or `tools/platform_web_npm.sh`; the supported runtime is Node 26.

## Change workflow

1. Read `CURRENT.md` and identify the owner layer/document.
2. Define behavior, permissions, data impact, rollback and focused tests.
3. Keep routes thin; place workflow rules in domain/services and persistence in models/repositories.
4. For schema work, use an expand migration compatible with the previous release. Never edit an applied migration.
5. Add regression coverage before broad gates.
6. Remove replaced imports, CSS, mocks, routes and stale documentation.
7. Run focused checks, then the relevant full gates.
8. For production-bound work, follow the deployment runbook through live validation.
9. Commit each coherent verified change/package and push it to the matching GitHub branch before handoff unless explicitly requested otherwise.

## Commit and push safety

From the repository root:

```bash
git status --short
git diff --check
git fetch origin
git branch -vv
```

Stage only intended files, commit with a scoped message, then push without rewriting history:

```bash
git push origin HEAD:$(git branch --show-current)
```

After push:

```bash
git fetch origin
git rev-parse HEAD
git rev-parse origin/$(git branch --show-current)
git status --short
```

The two revisions must match. Never automatically use `--force` or `--force-with-lease`. If push is rejected as non-fast-forward, inspect and reconcile both histories before publishing.

A local-only commit is not a completed GitHub handoff.

## Verification commands

Use the quiet wrapper so successful checks emit one line:

```bash
cd /root/old_sparky/platform
tools/platform_run_quiet.sh "platform tests" -- tools/platform_run_tests.sh discover -s tests
tools/platform_run_quiet.sh "web typecheck" -- tools/platform_web_npm.sh --prefix apps/platform_web run typecheck
tools/platform_run_quiet.sh "web lint" -- tools/platform_web_npm.sh --prefix apps/platform_web run lint
tools/platform_run_quiet.sh "web build" -- tools/platform_web_npm.sh --prefix apps/platform_web run build
.venv_platform/bin/python tools/platform_docs_check.py
```

Run Ruff, Bandit, pip-audit, npm audit and `tools/platform_secret_scan.py` for a security or release package. Use Playwright at affected desktop/tablet/mobile viewports for meaningful UI changes.

## Data and test safety

- Never point local tests at `platformdb`, Redis DB 0 or production object storage.
- Do not create production fixtures without an explicit owner and cleanup path.
- Do not run destructive cleanup, load tests or a production restore without explicit approval and a fresh verified backup.
- Rollback switches code and does not automatically downgrade Alembic.
