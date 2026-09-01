# Platform incident response

- Status: Active how-to
- Owner: Incident commander
- Last reviewed: 2026-09-01

## Severity

- SEV-1: credential/private-data exposure, destructive corruption, widespread
  auth bypass or complete production outage;
- SEV-2: degraded core workflow, repeated 5xx, media/worker backlog or backup
  failure without confirmed loss;
- SEV-3: isolated defect, false-positive control or non-critical monitoring gap.

## First 15 minutes

1. Name an incident commander; record UTC start, release, symptom and scope.
2. Preserve bounded evidence: request IDs, CF-Ray, route/status counts,
   relevant journals and `current`/`previous` targets. Never dump env/secrets.
3. Stop propagation with the smallest reversible control. Do not delete data as
   containment.
4. Protect a fresh DB backup and current/previous releases when safe.
5. Communicate verified facts, user impact and next decision time.

## Playbooks

### Auth, role or data exposure

Pause the affected public action if necessary, revoke affected sessions,
remove unauthorized roles through audited paths and rotate only proven-exposed
credentials. Verify normal-user admin denial and inspect logs for the confirmed
interval. Public support-address scraping requires removing every public
artifact/cache copy, not JavaScript obfuscation.

### Media or R2

Disable new upload intake while retaining ready CDN reads. Inspect DB state,
queue and bounded logs; rerun idempotent jobs. Delete only new partial keys.
Never begin with broad bucket deletion/listing.

### Database or migration

Stop writes. Prefer a compatible previous app release or reviewed forward fix.
A production restore follows the backup runbook and explicit operator gate;
never auto-downgrade migrations.

### Cloudflare or origin

Check public edge, origin SNI, Nginx, firewall ranges and certificate expiry as
separate layers. Do not pause proxying while browsers depend on an Origin CA.
Rollback Nginx vhost/snippet and firewall changes as reviewed units.

## Recovery and review

Before resolution, run readiness/smoke, affected role journey, queue/DB/disk,
backup freshness and live edge checks. Within 72 hours record a redacted
timeline, root cause, detection gap, corrective owner/deadline and necessary
test/runbook changes. Store incident evidence outside public Git.
