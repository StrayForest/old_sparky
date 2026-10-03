# Immutable recovery bootstrap for retained release abort

- Status: Accepted (compatibility and operation-less recovery scope)
- Date: 2026-09-27
- Owners: Platform maintainers

The operation-ID clean first-install systemd contract in this ADR is
superseded by [Durable first-install systemd activation](first-install-systemd-activation.md).
This ADR remains authoritative for the operation-less v1/v2 compatibility
bridges and retained-release recovery provenance.

## Context

A retained transaction can leave `current` pointing at an older or partially
compatible release. Selecting recovery code from that release or checking
out source on the production host is not a dependable recovery authority.
Migration uncertainty remains irreversible and must stay fail-closed.

## Decision

The trusted default-branch security run produces a deterministic,
non-deployable bundle containing only the closed recovery-helper closure. Its
manifest binds source SHA **A**, exact security run/attempt, artifact digest,
modes and member digests. The producer recovery workflow has its own reviewed
workflow SHA **B**; a separate completed-run publisher has workflow SHA **C**.
The producer only builds, attests and uploads the bundle. The publisher is
triggered by that completed producer run, re-reads the exact producer
run/attempt/job and security run/attempt from the API, and uploads schema-3
evidence containing both producer and publisher identities plus the
publisher-owned outer artifact name, positive artifact ID and API digest.
The outer artifact name includes publisher run/attempt **C**, while the
inner producer bundle name and digest remain bound to producer **B**. Operators pass all
three exact run/attempt pairs to recovery or abort; no workflow performs a
latest-by-SHA or same-run self-publication lookup. Consumers verify the bundle
attestation against **B**, independently bind bundle source **A**, and
cross-check publisher **C**, the producer artifact API digest, and job evidence.
The publisher verifies the downloaded producer artifact ZIP bytes against that
API digest before extracting its one bundle member. Because the GitHub certificate's
`runInvocationURI` identifies a run/attempt rather than a job, the exact job ID
is selected from that attempt's jobs API, emitted by the closed evidence, and
cross-checked as an evidence/API pair; it is never inferred from or fabricated
into the certificate URI.

The host installs one root-owned, immutable, content-addressed generation
under `shared/.release-recovery/generations/<bundle-sha256>`. Its manifest
advertises the two closed capabilities `abort_retained_only` and
`recover_pending`; the fixed abort entrypoint executes only
`abort_retained_only`, accepts a v2
`recovery-restored` install receipt, restores the recorded runtime, verifies
the retained systemd receipt, and performs two-phase completion: it removes
the candidate/venv cleanup artifacts with `complete-recovery
--retain-receipt`, clears the systemd receipt, and only then removes the
operation receipt with a final `complete-recovery`. A retry after either
durable cleanup side effect is idempotent and fails closed if the receipt pair
or cleanup identities are inconsistent. It never checks out source, runs
Alembic, downgrades a migration or selects a normal deploy entrypoint.

The normal release-recover entrypoint has an explicit topology split but one
immutable control plane. It transfers or reuses the exact attested bundle
whose SHA is bound to the security-run/recovery-run evidence, installs it
under that content address, validates its closed manifest and requested
`recover_pending` capability, and invokes only
`platform_recover_pending.sh`. A staged operation-ID first-install receipt with
no prior current is governed by the durable `systemd_state_before` snapshot and
the `systemd-activation-pending`/`systemd-activated` phases in the superseding
ADR; the generation restores and verifies that baseline before transaction
cleanup. An operation-less pre-promotion receipt is the separate systemd-free
compatibility path described below. A current-only receipt must carry a complete
API/worker/web/timer snapshot; it has no first-install systemd receipt, but the
generation restores that exact snapshot before transaction cleanup and records
a durable `filesystem-restored-services-pending` phase for retry. Neither topology
sources `current/tools`. Upgrade and rollback use that same generation wrapper
for the two-pointer systemd contract, while release-specific data-plane
helpers are accepted only through the systemd manifest bound to the recorded
release.

For an operation-ID rollback, `platform_recover_pending.sh` owns the complete
phase matrix inside that generation: pre-runtime phases retain the filesystem
transaction, runtime-pending phases first record
`filesystem-restored-runtime-pending` after restoring pointers/venv and then
restore the original runtime and bound systemd receipt, `restart-pending`
resumes the swapped target, and later phases only verify and complete the
two-phase cleanup. The explicit filesystem marker makes a kill between the
filesystem and runtime steps replay the runtime step instead of incorrectly
considering the rollback complete. A retry after the systemd receipt has been
cleared consumes the already-proven transaction phase without querying
systemd. No rollback recovery path executes a helper from
`current/tools` or invokes the release's rollback shim; release-specific
runtime files are data-plane inputs whose immutable receipt manifest is
revalidated before use.

The same wrapper has a narrowly scoped pre-promotion branch for an exact
operation-less version-1 `install` receipt in `quiesce-pending`. It requires
complete active/inactive service/timer state, unchanged current/previous identities, an absent
candidate path or empty safe candidate directory (the legacy receipt has no
candidate inode binding), and no systemd receipt. New version-2 pre-quiesce
receipts additionally carry and restore the exact enabled/disabled state; a
version-1 receipt has no enabled fields and never infers them. It restores the recorded
snapshot through immutable helper
code and then calls `abort-quiesce`. Unknown keys, other operation-less phases,
partial state or an occupied candidate fail before any systemd call. This is
separate from the operation-less `recovery-restored` receipt-owned cleanup
bridge.

When both pointers were absent before that receipt, the only accepted
compatibility snapshot is fully inactive and disabled (version 2) or fully
inactive with no enablement fields (version 1). That proof is a no-op: the
immutable wrapper consumes the receipt without invoking `systemctl`.

New transaction receipts carry one immutable 32-hex `operation_id`; the
systemd receipt must carry the same ID and the canonical current/previous
release paths and inode identities before any runtime helper, `systemctl`, or
receipt unlink. Its helper manifest covers both rollback targets, including
the live-QA runtime installer, and is checked against the release actually
passed to runtime restoration. The only compatibility bridge is the exact
legacy v2 `install`/`recovery-restored` receipt with no systemd receipt: it
performs receipt-owned candidate cleanup only, never synthesizes an ID or
executes a retained release helper. Missing, mismatched, or present systemd
state fails closed.

The mutable systemd installer follows the same durable-boundary rule for
retired unit files. Before its first stop/disable it records exact source and
backup identities, digests, and active/enabled states in an fsynced,
root-owned schema-2 record. It writes `phase=cleanup-pending` before deleting
any backup after a verified install/reload. Retries in that phase validate and
remove only identity-bound remnants, then clear the record last; a kill at any
unlink, directory removal, or record-clear boundary therefore converges
without adopting an orphan.

The trusted workflow also rejects duplicate or unsafe outer/inner ZIP members,
bounded-size/compression violations, symlink/special/non-regular entries, and
unsafe staging directories. Recovery staging cleanup reports both the primary
operation and cleanup result; a cleanup failure cannot turn a failed recovery
into success. Recursive cleanup of a receipt-bound release tree first validates
the complete root-owned, single-link, non-writable regular-file/directory tree
on one device. The operation-less pre-quiesce bridge has no inode binding for
a temporary `.venv-install-*` or `.freeze-check-*` directory, so it retains
any such directory (and its receipt) instead of recursively deleting it;
unexpected symlinks, hardlinks, special files or occupied content are never
cleanup authority.

## Consequences

- The event-range classifier artifact for a recovery-bootstrap-only change
  remains `deployable=false`. Baseline reconciliation can authorize only a
  separately authenticated baseline-to-target range that includes application
  or runtime changes and passes the ordinary full deploy gates; a
  bootstrap-only range remains a verified no-op.
- Missing, uncertain, tampered or migration-uncertain receipts remain
  retained. Any helper, restore, verification or completion failure leaves
  the receipt in place.
- Host recovery remains available even when the active release cannot safely
  interpret the newer transaction contract.

## Authenticated baseline reconciliation for bootstrap-only source ranges

Status: proposed Phase B capability; not implemented or available for use.
This section specifies a future, separately reviewed authorization path. Until
its workflow and pinned host implementation are merged, fully gated, and
provisioned, a bootstrap-only range remains non-deployable and must not be
activated by manual workflow dispatch.

A successful recovery-bootstrap-only classifier artifact is not deployment
authority. If Phase B is implemented, its automatic chain may enter only the
`baseline-reconcile` mode of the production deploy workflow, and only after the
exact target SHA has passed the full `Platform security and build` gates. That
mode must not accept operator-supplied source identities, use target-release
tools to inspect the active release, or publish a successful deployment
marker by itself.

The workflow obtains the active release source SHA and transaction state from a
new, read-only command in the pinned immutable host-tools generation. That
command validates the root-owned `current` release pointer and its release
receipt using fixed host-side code, and reports only a bounded baseline tuple
(source SHA, release slug, digest of the exact `RELEASE.json` bytes, and stable
current-link/release-directory device and inode identities) and whether a
release transaction is pending. It does not execute `current/tools`
or any candidate-release code. Before the normal build or any production write,
the workflow requires a clean transaction state and verifies the reported
source SHA against an exact successful production deployment proof for that
same SHA: the canonical production-deploy workflow and run attempt, its one
completed successful deployment job, and the bot-authored success status
pointing to that exact attempt. The source must also be present on the target's
first-parent history. The proof lookup is bounded and consumes complete API
responses; missing, stale, malformed, ambiguous, truncated or non-ancestor
evidence fails closed.

The success proof is not rejected solely because its run is old while the
immutable host receipt still identifies that exact active release. Freshness
comes from the live, lock-protected host identity and the latest complete
status row for the production-deploy context: it must still be the exact
successful attempt, with no tied, future, pending or failure replacement. A
host source mismatch, retained transaction, changed status, missing run record
or incomplete API response invalidates the baseline.

The trusted classifier then computes the complete first-parent path set from
that authenticated source through the exact target SHA. If the set contains
only recovery-bootstrap files and documentation, the workflow completes as a
verified no-op: it does not build or transfer an application release, write a
production pending status, or emit a successful deployment marker. If the set
contains application or runtime changes, the same exact target must still
classify as an ordinary deployable full route with all canonical gates passed.
The bootstrap-only artifact never supplies this authority.

The immutable host query is repeated inside the pinned production deployment
supervisor after it acquires the canonical release and retained-load locks and
immediately before it launches the candidate release helper. The source SHA,
release slug, receipt digest and clean transaction state must still match the
baseline used for classification, and the exact target must still be the
reviewed `dev` head. A
changed source, pending transaction, failed read or ambiguous proof stops the
operation before writes. The supervisor retains both locks across this check
and candidate activation, so another release or recovery cannot change the
baseline between authorization and mutation. Reconciliation never chooses a
different historical marker to paper over a host mismatch. After a documented
rollback or recovery, only the SHA reported by the active host is considered,
and it still needs its own exact successful deployment proof and clean
transaction state.

Adding this capability follows the host-tools two-commit process. Commit A
adds the fixed read-only helper, its closed dispatcher command, and contract
tests. After A passes exact full CI, operators provision and verify its new
content-addressed generation through the approved root-console procedure
while retaining the prior generation. Commit B changes the reviewed pin and
records A's exact closure baseline; it merges only after the generation is
installed and the host capability contract passes. The reconciliation route
must not dispatch against a pin that has not completed this sequence.
