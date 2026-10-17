# Signed pruning of a consumed quarantine candidate

- Status: Accepted
- Date: 2026-10-17
- Owners: Platform maintainers

This decision supersedes the later-removal paragraph in the
[recovery-bootstrap ADR](recovery-bootstrap-retained-abort.md#superseding-decision-populated-v2-quiesce-candidates).
The original quarantine and recovery contracts remain unchanged.

## Context

`quarantine_populated_quiesce` preserves a populated failed candidate as
untrusted evidence and consumes its recovery receipt only after the recorded
runtime and service snapshot has been restored. Keeping that tree indefinitely
can retain substantial storage, but ordinary release retention does not have
authority to remove it: the quarantine receipt belongs to the original failed
deployment, while a later cleanup has its own signed workflow identity.

## Decision

The immutable recovery generation exposes a separate
`prune_quarantined_candidate` capability. The recovery workflow validates the
exact failed-deployment run, attempt, workflow, ref, source and failed job, and
derives the sole candidate slug from that F tuple. The current cleanup
workflow's own source SHA, run/attempt and signed bundle SHA form a distinct U
binding; U never replaces or edits the original receipt's creator identity.

Pruning requires the original root-owned quarantine receipt to be in
`receipt_consumed` phase. The action reads and hashes its exact bytes, then
binds that digest, the F tuple, the original recovery creator tuple, candidate
tree digest/counts/bytes and inode identities, quarantine-root identity,
current/previous release identities, and U tuple in a private durable intent.
It does not change the original receipt.

Under the canonical release and retained-load locks, the signed wrapper
requires no pending release sidecars or systemd jobs, unchanged current and
previous pointers, an unchanged protected LiveQA provider-source closure,
stable active service invocation IDs, valid Nginx configuration and passing
API/web readiness. The transaction validates a bounded root-owned tree of
regular files and directories, rejects symlinks, special files, hard links,
cross-device descendants and mountpoints, and confirms the same snapshot after
a no-replace rename into a deterministic private staging path. It deletes only
that staged inode using no-follow directory descriptors and rechecks the
original receipt and protected pointers before recording completion. Backups,
host-tools generations, providers, current and previous releases and every
other quarantine entry remain outside the action's deletion scope.

The intent advances through `planned`, `deleting` and `deleted`. A retry in
`planned` requires the exact original tree; a retry in `deleting` may continue
only the same staged root inode after validating every remaining entry. If
deletion completed before the final intent write, the exact U-bound retry may
finalize only when both candidate and stage paths are absent and the original
receipt and pointer bindings still match. A `deleted` replay succeeds only
when those paths remain absent. A changed or ambiguous target fails closed.

## Consequences

The action reclaims one exact quarantined candidate while retaining the
original receipt and cleanup tombstone as audit evidence. A failure can leave
a partial private stage, but only the same U-bound intent may resume it. There
is no broad retention mode or direct helper/`rm` path. A future cleanup with a
different workflow run is not implicitly authorized to adopt an existing
intent; it needs a separately reviewed recovery path.

## Implementation references

The fd-relative no-follow walker follows Python's [`os.fwalk` and `dir_fd`
contracts](https://docs.python.org/3/library/os.html#os.fwalk). Same-filesystem
no-replace staging uses Linux [`renameat2(RENAME_NOREPLACE)`](https://man7.org/linux/man-pages/man2/renameat2.2.html),
and mountpoint detection uses the documented [`/proc/<pid>/mountinfo`
format](https://www.man7.org/linux/man-pages/man5/proc_pid_mountinfo.5.html).
Directory synchronization uses [`fsync(2)`](https://www.man7.org/linux/man-pages/man2/fsync.2.html)
after durable intent, rename and deletion boundaries.
