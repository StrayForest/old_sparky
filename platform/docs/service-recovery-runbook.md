# Production service recovery

- Status: Exceptional operator procedure
- Owner: Production operator
- Last reviewed: 2026-10-25

Use this procedure only to restore web, API or worker services on the exact
currently active immutable release. It does not install or select a release,
change configuration or schema, or replace the normal release workflow.

## Preconditions and behavior

The operator-only `platform-production-service-recovery.yml` workflow offers
`web`, `api-worker`, and `api-worker-nginx` modes. API/worker modes require
their exact confirmation phrase, active source SHA, and release slug. Use
`api-worker` only when Nginx is already healthy and active; use
`api-worker-nginx` for the observed API/worker/Nginx dead-success prestate.
The reviewed controller runs from the workflow's trusted no-checkout secret
job; it does not fetch code or copy an unverified helper to the host.

### Web-only recovery

The existing `web` mode uses the exact confirmation `RECOVER-DEADLOCK-WEB` and
restarts only `deadlock-web`, even when systemd reports it active. It requires
the current release and canonical release lock to validate, no pending release
transaction, and API, worker, and Nginx active. It polls the web origin for
HTTP 200 readiness; a successful marker proves web readiness, not memory
headroom. The restart can briefly interrupt visitor requests.

A measured post-QA memory-reserve failure is also a valid reason to use this
existing mode when the proactive backup guard's closed `parent_active_poll`
sample is below the existing 1 GiB `MemAvailable` floor and separate
post-load evidence shows web-process memory pressure. This is a diagnostic
trigger, not an OOM finding or attribution to a particular service. Preserve
the 1 GiB guard and all additive backup reserves; an observed recovery margin
does not create a new policy threshold.

Before dispatch, close the exact QA/load namespace and verify there is no
active load/client, backup, deployment, or retained-load lock. Record the
exact active source SHA, release slug, current/previous targets, closed
pre-recovery memory sample, and API/web/Nginx readiness. Web mode has no
expected-SHA or release-slug inputs: these identity checks are operator-run
before and after recovery, not workflow-enforced binding. The workflow holds
the canonical release lock but does not acquire a retained-load lock; the
operator idle-window check is not mutual exclusion or race protection. Coordinate
so no external load starts until recovery and post-checks finish.

After recovery, confirm the same source SHA, release slug, current/previous
targets, and release artifact identity; verify API readiness and web HTTP 200,
then capture closed memory evidence again. A recovery pass is not a backup
admission pass. Require the existing memory floor and operation reserves before
one backup producer, and stop if memory remains below threshold or any activity
or identity changes. Do not infer that the restart caused the memory change.

Before starting either service, the controller holds the canonical platform
release lock and checks the active release SHA and slug, stable current/active
pointers, release configuration and installed service-unit identities, no
pending release transaction, and the normal production preflight. PostgreSQL,
Redis, and web must be active. Available disk must satisfy the normal
minimum-free-space and maximum-usage thresholds. Both API and worker must be
exactly inactive/dead with `Result=success` and no running main process. Any
different, unknown, or failed API/worker prestate is refused.

The storage check requires at least `max(5 GiB, ceil(10% of filesystem
capacity))` available and no more than 90% used. The 5 GiB floor is independent
of the percentage rule.

The PostgreSQL unit check is not a database-health proof: the aggregate
`postgresql.service` may be active while its cluster is unavailable. The
normal release preflight also opens the configured database and executes a
read-only `SELECT 1`; recovery refuses before starting application services if
that check fails.

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
