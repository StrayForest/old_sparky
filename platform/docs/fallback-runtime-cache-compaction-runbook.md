# Fallback runtime cache compaction

- Status: Active operational procedure
- Owner: Platform release maintainers

This exceptional operation reclaims the unused full-Chromium subtree from the
fixed retained live-QA fallback cache while preserving its pinned sandbox
helper, headless shell, WebKit, ffmpeg and provenance contract. It is a
verify-existing backup operation, not scheduled maintenance or a general cache
prune.

## Admission

Use only the reviewed production backup workflow after its exact current-dev
security run and helper attestation succeed. Select `verify-existing` and the
explicit fallback-cache compaction option. Do not combine it with build-Node
cache eviction or create a new dump. The operation verifies the newest backup;
it never falls back to an older pair, rotates an archive or changes the restore
disk floor.

Before mutation, the workflow holds the canonical release, retained-load,
source/build and live-QA locks. The guard validates the fixed cache against
source `4a04b2dffaf0d02c2d3910e7ba28dca9b89de209`, its immutable manifest and
the closed 4a provenance tuple: the pinned Node archive, package-lock SHA256
`bbfe1a66cc39665cffac0b59672877716f53785b92dbb09ff921a2627300f92f`, browser
manifest SHA256 `ee39bc924bc3d1bd895626c2910f1292d109bbfeeb5abd113acb45e1951cc942`,
and exact sandbox-helper identity. Ordinary runtime-cache preparation remains
bound to the current trusted checkout's package lock. The compaction path uses
the fixed 4a tuple only for this fixed legacy cache; it does not accept caller-
selected commits or hashes. It records a root-private, source/run/bundle-bound
intent containing a bounded inventory of the original Chromium subtree. The
current validator preserves the 4a cache's content, permission, and provenance
predicates; this operation does not stage or execute a second legacy helper.

## Transaction and recovery

The transaction renames the original subtree to a fixed sibling, installs only
the validated sandbox helper, updates only the tree digest in the manifest,
then checks the existing cache contract and runs isolated sandboxed Chromium
headless-shell and WebKit probes. Both systemd scopes must be collected and the
live-QA service must be idle before a durable validated receipt is written.
Only then may it remove the inventory-bound original subtree and write the
completion receipt.

Resume is explicit and binds the prior run, attempt, exact source SHA and
attested bundle SHA. Before the validated phase, recovery restores the original
subtree and exact manifest bytes. After validation, it verifies the compacted
cache and resumes deletion only for entries in the recorded inventory; unknown
or changed entries stop recovery and preserve remaining bytes. Receipts are
durable and phase-specific. Any incomplete or ambiguous state blocks QA and
backup verification until reviewed recovery succeeds.

The operation changes no backup bytes or completion time. After compaction,
run the ordinary guarded newest-backup verification and use its sampled disk
floor result; the size of the current database or dump is not a restore peak
bound. A disk-floor failure remains a failure and does not authorize a retry
without new measured headroom.
