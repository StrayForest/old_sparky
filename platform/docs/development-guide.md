# Platform development guide

- Status: Active how-to
- Owner: Platform maintainers
- Last reviewed: 2026-08-23

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
5. Add regression coverage before broad gates. For async UI mutations, cover duplicate-submit, stale-response and editable-draft races when applicable; permission-sensitive UI must consume backend capabilities rather than infer access from visibility or presentation state.
6. Remove replaced imports, CSS, mocks, routes and stale documentation.
7. Push the coherent reviewed package and use GitHub Actions as the release verification authority. Local checks may help diagnose a failure but are not the normal production gate.
8. For production-bound work merged or pushed to `dev`, wait for the exact-SHA `Platform security and build` push run. When it succeeds for the current `dev` HEAD, `Platform production auto-deploy` is expected to validate that SHA and dispatch the immutable `Platform production deploy` workflow automatically.
9. Follow the deployment runbook and the [release state machine](release-state-machine.md) through live validation. Do not manually dispatch production for the normal `dev` path and do not call the low-level release installer directly.
10. Commit each coherent verified change/package and push it to the matching GitHub branch before handoff unless explicitly requested otherwise.

The test-group ownership and runner contract is maintained in
[`test-suite-governance.md`](test-suite-governance.md). Local and CI
verification use the stable gate IDs exposed by
`tools/platform_verify.py`; specialized runners are implementation details.
Do not use grep exclusions to define test ownership.

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

GitHub Actions owns the release gate, while canonical local gates are
recommended for fast feedback. Keep them isolated from production resources.
The standard local commands are:

```bash
cd /root/old_sparky/platform
.venv_platform/bin/python tools/platform_verify.py backend
.venv_platform/bin/python tools/platform_verify.py python-quality
.venv_platform/bin/python tools/platform_verify.py security
.venv_platform/bin/python tools/platform_verify.py migration
.venv_platform/bin/python tools/platform_verify.py docs
.venv_platform/bin/python tools/platform_verify.py web-quality
.venv_platform/bin/python tools/platform_verify.py web-hermetic
.venv_platform/bin/python tools/platform_verify.py verification-contract
```

Use `tools/platform_verify.py backend --focused <unittest-selector>` for
focused feedback. Raw underlying commands are reserved for debugging the
canonical runner itself or isolating a CI failure. The complete local
deterministic aggregate is available as `tools/platform_verify.py ci`; it does
not run production smoke, live QA or load testing.

For a production-bound `dev` change, final verification is the GitHub chain for the same current-head SHA:

1. `Platform security and build` succeeds and publishes `platform-security-build=success`.
2. `Platform production auto-deploy` accepts that SHA rather than skipping it as stale/already deployed.
3. `Platform production deploy` succeeds, including immutable-artifact validation and live smoke.

Manual `Platform production deploy` remains an operator fallback/read-only preflight tool, not the routine continuation of a successful `dev` push.

## Data and test safety

- Never point local tests at `platformdb`, Redis DB 0 or production object storage.
- Do not create production fixtures without an explicit owner and cleanup path.
- Do not run destructive cleanup, load tests or a production restore without explicit approval and a fresh verified backup.
- Rollback switches code and does not automatically downgrade Alembic.
