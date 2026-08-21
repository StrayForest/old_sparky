# AS-11 — Public worker-error sanitization

- Status: Resolved
- Closed: 2026-08-22
- Owner: Platform maintainers

## Finding

`Tournament.automation_last_error` is part of the public tournament response contract, while Deadlock automation previously persisted `str(exception)` into that field. A worker/domain exception could therefore expose internal error text, identifiers or other unintended details through a public tournament response.

## Remediation

The persistence boundary now treats public automation error text as an explicit allowlist:

- any non-null arbitrary value assigned to `Tournament.automation_last_error` is replaced before ORM flush with the stable public message `Tournament automation failed. A retry is scheduled.`;
- restricted application/worker logs retain only tournament ID, failure count and a one-way 16-hex error fingerprint; the original exception text is not logged by the sanitizer;
- migration `20260821_0039` irreversibly rewrites every historical non-null `platform.tournaments.automation_last_error` value to the same stable message;
- the existing retry counter/backoff behavior and the public API field shape are preserved.

The persistence guard is intentionally below individual worker catch sites so a future automation path cannot accidentally reintroduce raw exception persistence by assigning a new exception string to the public field.

## Regression evidence

`tests/test_as11_worker_error_sanitization.py` creates a public tournament, injects a marker-bearing `RuntimeError` through the real Deadlock automation failure recorder, commits it through the production ORM session boundary and verifies:

- the stored value is the stable generic message;
- the public `GET /api/v1/tournaments/{slug}` response contains the generic message and not the marker;
- the restricted sanitizer log contains a fingerprint but not the marker/raw exception text.

The normal backend CI also applies Alembic through head, so migration ordering and execution are exercised against an isolated PostgreSQL database.

## Deployment gate

Production deployment is owned by the repository's automatic `platform-production-deploy` workflow after the merged `dev` commit passes `Platform security and build`. This finding is considered production-closed only while the corresponding merged commit has a green production-deploy/live-smoke status; a failed deployment reopens the operational closure state.

## Retained invariant

Any field reachable from an anonymous/public API contract must contain an explicit public value, never arbitrary exception text. Detailed diagnostics stay in restricted telemetry and must remain data-minimized.
