# Durable first-install systemd activation

- Status: Accepted
- Date: 2026-09-29
- Owners: Platform maintainers
- Supersedes: [Recovery bootstrap retained abort](recovery-bootstrap-retained-abort.md) for operation-ID first-install systemd behavior

## Context

A clean first install has no previous release whose service state can be
restored. The old design either skipped systemd entirely or treated a mutable
systemd receipt as optional. That left two unsafe outcomes: a successful
installer could leave application units disabled after reboot, and a failure
after enablement could remove the candidate while units still pointed at it.
The operation-less pre-promotion compatibility receipt remains a separate
systemd-free contract and must not be conflated with a staged install.

## Decision

The staged `.release-operation.json` is the single durable authority for an
operation-ID first install. Before any first-install systemd mutation,
`capture-initial-systemd` records `systemd_state_before` for exactly these
seven units:

- `deadlock-api.service`
- `deadlock-worker.service`
- `deadlock-web.service`
- `deadlock-maintenance.timer`
- `deadlock-logrotate.timer`
- `deadlock-cloudflare-ips.timer`
- `deadlock-health-monitor.timer`

Every initial baseline must be `inactive` and `disabled`; `null` is the only
canonical value for `current_before` and `previous_before`. Empty strings,
partial snapshots, a stale `.release-systemd-state.json`, or a mismatched
receipt pair fail closed before cleanup or a systemd mutation. Existing v2
receipts without the new field remain readable only through their documented
legacy recovery boundary; they cannot authorize initial activation.

The deploy state machine has two initial-install activation phases:

1. `activation-pending` installs unit files with
   `PLATFORM_ENABLE_SYSTEMD_UNITS=0`, restarts the three application services,
   and runs readiness/smoke checks.
2. `systemd-activation-pending` restores the recorded baseline, runs the
   installer with `PLATFORM_ENABLE_SYSTEMD_UNITS=1`, verifies all seven units
   are enabled and the services/timers are active, and reruns readiness before
   writing `systemd-activated`.

The restore, installer, live-QA reconcile, enable/restart, readiness and
verification sequence has one aggregate 120-second shell deadline. Each
systemctl, installer or reconcile call is independently capped at 30 seconds
and receives only the remaining
deadline. A failure retains the candidate and receipt. The exit handler
restores and verifies the baseline before returning and rewinds
`systemd-activated` or `activation-committed` to
`systemd-activation-pending`, so a retry never requires units that recovery
already stopped. `staged` and `recovery-restored` aborts verify the exact
baseline before transaction cleanup. Recovery bootstrap and pending recovery
use the same transaction validator and restore-before-cleanup ordering.

## Consequences

- Successful clean installs persist the intended enablement and active state,
  including maintenance, logrotate, Cloudflare and health-monitor timers.
- A crash, timeout, partial restore or SIGKILL leaves a retryable candidate and
  receipt; retries are idempotent and never infer systemd state from the host.
- The immutable recovery closure imports the canonical unit list through its
  trusted sibling path. Any closure change requires regeneration of the
  functional recovery commit and host-tools pin before publication.
- The operation-less v1/v2 pre-promotion bridge remains systemd-free and is
  governed by the retained-abort ADR's compatibility rules.

## Verification obligations

The release boundary suite covers baseline capture, empty-string rejection,
stale-pair retention and retry, failure after activation markers, partial
restore retry, mutable `staged`/`recovery-restored` aborts, aggregate timeout
with a hanging installer, and v1/v2 compatibility. No production recovery or
deployment is used as a test.
