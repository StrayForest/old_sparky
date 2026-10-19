# API and worker service recovery

- Status: Exceptional operator procedure
- Owner: Production operator
- Last reviewed: 2026-10-19

Use this procedure only to restore the API and worker services on the exact
currently active immutable release. It does not install or select a release,
change configuration or schema, or replace the normal release workflow.

## Preconditions and behavior

The operator-only `platform-production-service-recovery.yml` workflow accepts
`api-worker` mode only with the exact confirmation phrase, active source SHA,
and release slug. The reviewed controller runs from the workflow's trusted
no-checkout secret job; it does not fetch code or copy an unverified helper to
the host.

Before starting either service, the controller holds the canonical platform
release lock and checks the active release SHA and slug, stable current/active
pointers, release configuration and installed service-unit identities, no
pending release transaction, and the normal production preflight. PostgreSQL,
Redis, web, and Nginx must be active. Available disk must satisfy the normal
minimum-free-space and maximum-usage thresholds. Both API and worker must be
exactly inactive/dead with `Result=success` and no running main process. Any
different, unknown, or failed prestate is refused.

The controller starts `deadlock-api` first and requires its existing database
and Redis health check to pass. It then starts `deadlock-worker` and requires a
bounded Celery control ping from the local `celery@hostname` node. If a start
or readiness check fails, it stops attempted services in reverse order and
verifies the original stopped prestate. The bounded
`PRODUCTION_API_WORKER_RECOVERY` receipt reports fixed status and failure
classification without environment values or raw command output.

The controller does not change release pointers, application configuration,
schema, timers, backup state, or application data directly. Starting the
services resumes normal request and task processing, which may write
application data. Require the normal post-release QA before declaring recovery
complete. Do not use this procedure for a failed/unknown service state, a
different active release, or a release installation or rollback.

## Dispatch

Verify the current source SHA and release slug from trusted production
evidence. Confirm there is no pending transaction and that all preconditions
above hold before dispatching:

```bash
gh workflow run platform-production-service-recovery.yml \
  --repo StrayForest/old_sparky \
  --ref dev \
  -f mode=api-worker \
  -f confirmation=RECOVER-DEADLOCK-API-WORKER \
  -f expected_source_sha=<ACTIVE_SOURCE_SHA> \
  -f expected_release_slug=<ACTIVE_RELEASE_SLUG>
```

Preserve the workflow receipt and verify the API and worker remain healthy
after the run. A refusal is a stop condition; do not retry with changed
preconditions or use a direct host restart as a substitute.
