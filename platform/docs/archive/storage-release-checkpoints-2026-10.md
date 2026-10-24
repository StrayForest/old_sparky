# Historical release and storage checkpoints — October 2026

- Status: Historical evidence
- Owner: Platform maintainers
- Recorded: 2026-10-25

This compact record preserves decisions and failure boundaries that are not
part of the live state. It does not establish current production status;
[`CURRENT.md`](../CURRENT.md) owns that. Detailed historical load evidence is
in the [performance reference](../../performance/README.md), and procedures
remain in the [operations runbook](../operations-runbook.md), [backup and
restore runbook](../backup-restore-runbook.md), and [host-tools provisioning
ADR](../adr/production-host-tools-provisioning.md).

## 2026-10-20: low-space maintenance and protected-release recovery

Source `6aa4b0c7e00b687a12d83af611df8c9f711615b3` was released after its
exact-source security check, restore-verified backup, automatic deployment,
and child release succeeded. The first maintenance attempt
([39478657395](https://github.com/StrayForest/old_sparky/actions/runs/39478657395))
failed at backup admission. Same-filesystem accounting showed that the fresh
dump would leave less than the required disk-image admission space. Two
reviewed old release candidates were then removed, after which maintenance
([39481347298](https://github.com/StrayForest/old_sparky/actions/runs/39481347298))
created and restore-verified a fresh backup and removed four eligible releases.
Its `preexisting_archives_preserved=false` result did not prove that older
archive members survived; it was not treated as an archive-preservation pass.

Three protected release payloads were subsequently restored and validated:
`gha-38182702358-1-f1154c7dba4f`, `gha-38000614899-1-7e5769ba3f85`, and
`gha-37760797674-1-80e457e57081`. Their original `.rollback` metadata could
not be recovered, so they remained `rollback_ready=false`; only root directory
mtimes were normalized from authenticated release metadata. The current and
previous pointers, shared runtime, and services were unchanged. Root-private
recovery receipts remain outside the repository.

Storage diagnostics
([39481745604](https://github.com/StrayForest/old_sparky/actions/runs/39481745604))
overlapped timer-started maintenance and observed its active lock, so its
retention summary was incomplete. The non-overlapping follow-up
([39482071066](https://github.com/StrayForest/old_sparky/actions/runs/39482071066))
was the valid post-maintenance snapshot: it reported matching enabled timer
unit hashes, healthy services, zero locks, protected release pins present,
provider verification success, and the expected transport archives and
sidecars. The overlap result must not be presented as a clean inventory.

## 2026-10-25: restore-verified maintenance after disk-floor refusal

On the live `12b6a818268da426eaa3b737566c5f05d2b52ac4` release, maintenance
([40139765922](https://github.com/StrayForest/old_sparky/actions/runs/40139765922))
initially stopped at the unchanged disk-image scratch admission floor; scratch
cleanup was confirmed and pruning did not run. Under the documented locked
release-only retention path, six verified unpinned release candidates were
removed while current, previous, recursive and protected pins were preserved.
The next canonical maintenance run
([40140934751](https://github.com/StrayForest/old_sparky/actions/runs/40140934751))
created a `127,972,590`-byte checksum- and restore-verified backup, passed
Alembic and protected-retention checks, and rotated 20 canonical retention
entries. It ended with `8,653,447,168` bytes free (`21.65%`), unchanged live
release pointers, healthy services and endpoints, and no jobs, locks or
transaction. The enabled timer was active and waiting. This is one successful
pass; it is not a cadence or capacity guarantee.

## 2026-10-17: source-bound backup blocked automatic deployment

For source `a9e6cb107847cb6717e9d661a8fd5c2e71e47838`, Security and recovery
bootstrap artifacts succeeded, but the automatic deployment parent was
cancelled because no qualifying backup was bound to that exact source. An
available restore-verified backup belonged to a different source and could not
substitute. The recovery attempt then failed during quiesce with an unavailable
SSH outcome; the cause was unknown. This checkpoint reinforces that backup
provenance is exact-source authority, and a failed recovery probe is not a
deployment.

The October 10 transport and October 15 backup/restore checkpoints remain
linked from the [performance transport runbook](../performance-transport-runbook.md)
and [performance reference](../../performance/README.md); neither supplies a
current release or admission result.
