# API and worker service recovery

- Status: Exceptional operator procedure
- Owner: Production operator
- Last reviewed: 2026-10-19

Use this procedure only to restore the API and worker services on the exact
currently active immutable release. It does not install or select a release,
change configuration or schema, or replace the normal release workflow.

## Preconditions and behavior

The operator-only `platform-production-service-recovery.yml` workflow offers
two API/worker modes, each requiring its exact confirmation phrase, active
source SHA, and release slug. Use `api-worker` only when Nginx is already
healthy and active. Use `api-worker-nginx` for the observed API/worker/Nginx
dead-success prestate. The reviewed controller runs from the workflow's
trusted no-checkout secret job; it does not fetch code or copy an unverified
helper to the host.

Before starting either service, the controller holds the canonical platform
release lock and checks the active release SHA and slug, stable current/active
pointers, release configuration and installed service-unit identities, no
pending release transaction, and the normal production preflight. PostgreSQL,
Redis, and web must be active. Available disk must satisfy the normal
minimum-free-space and maximum-usage thresholds. Both API and worker must be
exactly inactive/dead with `Result=success` and no running main process. Any
different, unknown, or failed API/worker prestate is refused.

In `api-worker` mode, Nginx must already be active and healthy. In
`api-worker-nginx` mode, Nginx may be either active/running/success with a
valid PID or inactive/dead/success with no PID; it must be enabled, use the
expected root-owned system unit without drop-ins, and have an unchanged
configuration identity. The controller runs `nginx -t`, validates the
configured certificate hostname and remaining validity, and binds the
certificate and private-key file identity before any service start. If Nginx
was inactive, it starts only after API and worker readiness succeed. The unit,
configuration, certificate, and key identities must remain stable throughout
recovery. This mode also requires a loopback TLS request to
`/api/v1/health/ready` to succeed with the configured certificate.

The controller starts `deadlock-api` first and requires its existing database
and Redis health check to pass. It then starts `deadlock-worker` and requires a
bounded Celery control ping from the local `celery@hostname` node. In
`api-worker-nginx` mode, Nginx starts after those checks only if it was
initially inactive. If a start or readiness check fails, it stops every
attempted service in reverse order and verifies the original API/worker/Nginx
prestate. The bounded
`PRODUCTION_API_WORKER_RECOVERY` receipt reports fixed status and failure
classification without environment values or raw command output.

The controller does not change release pointers, application configuration,
schema, timers, backup state, or application data directly; it verifies that
the captured timer state is preserved. Starting the services resumes normal
request and task processing, which may write application data. Recovery is
not complete until the receipt succeeds and the operator completes the normal
external post-release smoke/QA against the public application. Do not use this
procedure for an unknown or failed API/worker prestate, a different active
release, or a release installation or rollback.

## Dispatch

Verify the current source SHA and release slug from trusted production
evidence. Confirm there is no pending transaction and that all preconditions
above hold before dispatching:

```bash
gh workflow run platform-production-service-recovery.yml \
  --repo StrayForest/old_sparky \
  --ref dev \
  -f mode=api-worker-nginx \
  -f confirmation=RECOVER-DEADLOCK-API-WORKER-NGINX \
  -f expected_source_sha=<ACTIVE_SOURCE_SHA> \
  -f expected_release_slug=<ACTIVE_RELEASE_SLUG>
```

For `api-worker`, use `-f mode=api-worker` with
`-f confirmation=RECOVER-DEADLOCK-API-WORKER` only when Nginx is already
active. For the stopped-Nginx case, use the exact `api-worker-nginx` example
above. Preserve the receipt, confirm the requested services remain healthy
and timer states match the captured state, then perform the external
post-release smoke/QA. A refusal or failed
rollback receipt is a stop condition; do not retry with changed preconditions
or use a direct host restart as a substitute.
