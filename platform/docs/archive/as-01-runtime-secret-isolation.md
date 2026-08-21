# AS-01 runtime identity and secret isolation — closed

- Status: Archived / resolved
- Closed: 2026-08-21
- Production verification: successful
- Implementation commit: `a87483f1ac07ee44852df7f59be73b6b1bd71705`
- Production deployment run: `32451045973`

## Original finding

Next.js, FastAPI and Celery used the same `oldsparky-platform` Unix identity and sourced the same full `.env.platform`. A compromise or environment disclosure in the public web process could therefore expose backend database, session, R2, mail, Turnstile and other service credentials and could cross same-UID process/staging boundaries.

## Remediation delivered

- Created separate locked runtime identities: `oldsparky-web`, `oldsparky-api`, `oldsparky-worker`.
- Kept `/opt/oldsparky/platform/shared/.env.platform` as the canonical operator source but changed it to `root:root` mode `0600`; runtime identities do not read it directly.
- Added deterministic per-service runtime env rendering under `/opt/oldsparky/platform/shared/env/`.
- Web receives only its required runtime/public configuration and no database, session-signing, R2 secret, mail-delivery, Turnstile secret or OpenAI credential.
- Worker receives its required DB/Redis/Celery/R2/OpenAI inputs but not session, Turnstile, Resend/SMTP or unrelated auth credentials.
- API receives the server-side configuration required for HTTP/auth/business behavior.
- Added the `oldsparky-media` supplementary group only to API and worker for shared media staging.
- Worker state and web cache remain service-owned.
- Added `ProtectProc=invisible` to all three systemd units.
- Added fail-closed runtime identity/environment checks and regression tests for forbidden service variables and systemd ownership.
- Updated release/install/runtime preparation so the isolation boundary is applied during normal immutable deployment and remains compatible with restart/rollback.

## Verification

The implementation passed:

- backend unit/integration suite;
- security/static gates including pip-audit, Ruff, Bandit and repository secret scan;
- frontend typecheck, lint and production build;
- Playwright smoke suite;
- production preflight and immutable release installation;
- systemd service restart checks;
- origin/public deploy smoke checks.

GitHub Actions production deployment run `32451045973` completed successfully after applying the new identities, env permissions and runtime directories.

A stale broad Playwright setup discovered during the release gate was corrected separately in commit `dc216c3edfe22c1b4bb9faaa506e601a91b0d49b`; no production security control was weakened to make the test pass.

## Remaining hygiene

Rotation of previously shared backend credentials is recommended when convenient, but it is treated as operator credential hygiene rather than an open AS-01 implementation blocker because the runtime exposure boundary itself is now enforced and verified in production.
