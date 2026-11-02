# Backup retention

- Status: Active operator reference
- Owner: Production operator
- Last reviewed: 2026-11-02

The production policy retains exactly one complete backup pair: the newest
pair whose dump and sidecar pass the isolated restore, checksum, freshness and
Alembic revision checks. The fixed manifest remains a root-owned audit input
for the source-bound maintenance report; its names no longer exempt pairs from
rotation. After a newly created pair passes verification, all other complete
backup pairs are eligible for removal, including pairs listed by the old
manifest and pairs without a successful restore marker.

## Writer behavior

Every backup-creating path uses the same rotation: full maintenance and
`--backup-only --apply` both create and verify first, then retain exactly the
new verified pair. Backup-only skips release, build, browser, screenshot and
live-QA retention; it does not skip backup rotation. The
`--verify-existing-backup-only` path restores and validates the newest existing
pair without creating or pruning anything. There is no preserve-existing
writer mode.

The pair inventory is captured before database work and rechecked before
publication. Rotation checks each dump and sidecar as a complete regular-file
pair, binds sidecar size and checksum claims, and rechecks inode/stat identity
and sidecar digest immediately before unlink. Unverified pairs must carry an
explicitly supported falsey restore status; malformed or ambiguous metadata
fails closed. The postcondition requires exactly one verified pair, matching
the new archive identity and checksum. The private proof binds the selected
pair, removed pair identities, manifest snapshot and removal set; it does not
read or hash dump payloads during pruning.

No pruning begins before isolated restore and Alembic verification succeeds.
If pruning fails partway through, the new verified pair remains and the
operation reports failure; removed historical pairs are not transactionally
restored. A failed create or restore leaves the existing archive set intact.
The maintenance service and source-bound backup workflow hold the canonical
operation locks and validate the exact source/Security binding before this
writer runs.

## Operator procedure

Use the reviewed GitHub Actions production workflow and the exact currently
deployed source binding. Do not run the low-level backup helper directly on
the production host. Before a planned operation, verify the installed unit
uses `--backup-keep 1` and the source-bound workflow still requires its exact
Security run, attempt and source SHA. Review the private report for
`rotation_mode=rotate-existing`, a passing `latest-restore-verified-only-v1`
proof, one selected pair matching the new archive, and a completed removal
count. Verify-existing reports must show `rotation_mode=verify-only` and zero
removals.

The backup manifest and full removal proof are private. Public workflow
artifacts and summaries may expose only the bounded success fields needed to
bind the new archive to its Security and workflow runs.
