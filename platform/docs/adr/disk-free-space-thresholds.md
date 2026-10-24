# Disk free-space thresholds

- Status: Accepted
- Date: 2026-10-24
- Owners: Platform release and operations maintainers

## Context

The shared disk-health policy used a 5 GiB minimum-free threshold and an 85%
maximum-used threshold. Backup, restore and release-admission tools also
implemented a 15% capacity floor, duplicating the same percentage rule across
several paths. The absolute 5 GiB floor serves a separate minimum-capacity
purpose and remains necessary for small filesystems.

## Decision

Require at least `max(5 GiB, ceil(10% of filesystem capacity))` available for
general storage health and maintenance. This is equivalent to the independent
5 GiB minimum plus a maximum-used threshold of 90%. Exact boundaries pass.
Operator overrides may strengthen the policy by raising minimum free space or
lowering maximum used space; they may not set a minimum below 5 GiB or maximum
usage above 90%.

Admission guards preserve their additive capacity reservations. Release and
Live-QA runtime installation retain the 128 MiB lead margin. Backup/restore
retains the 256 MiB lead margin; disk-image admission also reserves the full
576 MiB image. These reserves are added to the selected free-space floor and
are not part of the percentage calculation. Backup identity, artifact
verification, locks, cleanup and retention rules do not change.

CPU thresholds and other percentages remain independent and are not changed by
this disk policy.

## Deployment and consequences

The change ships through the normal exact-SHA Security and automatic
production-deployment chain. The immutable candidate contains the reviewed
tools and systemd unit templates; the guarded release transaction installs
those templates and verifies systemd activation. Do not update units through
one-off flags or a direct installer invocation. Until that source-bound
release completes, the installed production policy remains the previous one.
The release-independent host-tools bundle has no storage-policy or service
unit members, so its pinned generation remains unchanged.

The percentage guard allows operation with 10–15% free capacity on filesystems
where the 5 GiB floor does not dominate. Additive operation reserves remain
unchanged, so a particular backup, runtime install or release can still refuse
when its own operation reserve is unavailable. Rollback is a normal reviewed
release of the prior source; this policy writes no application data and
requires no schema migration.

The canonical helper is
[`platform_disk_policy.py`](../../tools/platform_disk_policy.py). Its
percentage calculation is also exercised by release-install, backup/restore,
maintenance and health-monitor boundary tests.
