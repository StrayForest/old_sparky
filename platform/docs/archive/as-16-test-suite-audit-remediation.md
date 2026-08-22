# AS-16 — test-suite audit remediation

- Status: resolved and live-validated on `dev` at `04d691b95b6ba9fde5982aed658523fe2e896407`
- Scope: `platform/tests`, deterministic frontend tests and the production code needed to verify their behavior
- Audit rule: findings were evaluated as regression risk and executable behavior, not as coverage percentages

## Findings closed

| Audit finding | Regression protection added |
| --- | --- |
| Production browser QA ran on the GitHub runner with the wrong privilege/sandbox contour | `platform-live-launch.yml` now connects over SSH and invokes the server-owned root supervisor with the dedicated QA UID; it requires a signed success marker. |
| No deterministic CI proof for run-async → Celery → persisted assignment | Worker task contract, task failure/release behavior and queue route tests cover the handoff and persisted result boundary. |
| Migration 0040 had only a source-level check | `platform_migration_scenario.py` seeds populated 0039 data, verifies fail-fast repair guidance, repairs the duplicate state, retries 0040 and checks the final state. |
| Dream-slot replace-all had no real concurrent payloads | The profile workspace test submits two concurrent replace-all payloads and asserts exactly one complete payload remains. |
| Admin user deletion lacked permission/UI regression coverage | API tests cover regular-admin, self, superadmin-target, confirmation, owner/media blockers, session invalidation and audit persistence; the web test covers the disabled-to-enabled confirmation flow and DELETE payload. |
| Steam unlink and audit-me paths were mocked happy paths | Integration tests exercise password-backed and Steam-only unlink recovery plus anonymous/own-only/limit ordering for `/api/v1/audit/me`. |
| Frontend CI hid deterministic tests and fixtures drifted from production | `test:hermetic` runs the ordinary suite and participant contour without name-based exclusions; fixtures use `captain_selection_starts_at` and current account/captain endpoints. |

## Execution contract

The active procedure is [`../test-suite-governance.md`](../test-suite-governance.md), with the machine-readable group manifest at `platform/tests/test-suite-manifest.json`. It defines one owner and runner for backend, migration, web-hermetic, server-smoke, live-public and destructive live-user contours.

The production browser workflow deliberately does not install Playwright or run a credential-bearing browser on the runner. It passes only the canonical origin to the production SSH supervisor, which owns the lock, Chromium sandbox, runtime cache and dedicated QA identity.

## Verification evidence

- Full backend discovery: passed.
- Populated migration scenario: passed.
- Full `npm run test:hermetic` across desktop, wide, tablet and mobile: passed.
- Web typecheck, lint and production build: passed.
- Ruff, Python compile, docs checker, YAML parsing and secret scan: passed.
- Security/build workflow `32589822458`: passed.
- Production deployment workflow `32590065060`: passed.
- Production browser gate workflow `32590276914`: passed, including the
  server-owned supervisor, Access-protected admin boundary and the live public
  browser suite.

The release was merged into `dev` and published after the live gate passed;
the final repository state and documentation publication are verified by the
release handoff.
