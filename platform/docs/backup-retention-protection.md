# Named backup-pair retention

- Status: Active operator reference
- Owner: Production operator
- Last reviewed: 2026-10-16

This optional manifest lets a reviewed rotating backup operation preserve
specific recovery pairs while retaining recent verified backups. The unit in
this source tree opts into the fixed private manifest and a keep value of 15;
production remains on its current unit until the reviewed release chain
installs this source. Other no-manifest callers and preserve-existing mode keep
their existing behavior.

## Manifest and validation

Pass `--protected-backup-manifest PATH` only to a supported backup-creation
mode. The closed schema is version 1 with `schema_version: 1` and a
`protected_dump_files` array of canonical `.dump` basenames; the `.json` sidecar
name is derived. The manifest must be root-owned, regular, mode `0600`,
single-link, opened with `O_NOFOLLOW`, at most 64 KiB, and stable during each
file-descriptor read. The source snapshots it before work and rechecks it
around pruning.

Every listed dump and sidecar must exist and pass stable path/descriptor
identity checks before database configuration or DB work. The bounded sidecar
reader validates its bytes and identity; sidecar fields must bind the
canonical dump name, size, and SHA-256 format. The dump payload is not rehashed,
so the manifest preserves selected names and identities but does not certify
dump contents or restore status. The flag is rejected for skip, verify-only,
probe, purge, and low-level verify/check modes.

## Rotation behavior

Named pairs bypass both unverified-pair pruning and age/count rotation,
whatever their current verification status. An `UNKNOWN` or unverified pair
stays unverified and does not count as a verified backup. Configured rotation
retains the union of named pairs and the newest `max(keep, 15)` pairs whose
sidecars explicitly say `restore_verified: true`.

Missing, malformed, unsafe, or initially incomplete manifest input fails
closed before DB work and never falls back to ordinary rotation. Pair
identities and sidecar claims are checked after restore and again before
unlink, then checked after retention. Detected drift fails closed, though
retention may have begun after an earlier check; the whole operation is not
transactional. The private proof records full pre-delete pair identities and
sidecar claims for every removed pair, its retention reason, and the exact
falsey restore-status class where relevant. It does not hash dump payloads.
Unverified pruning does not use the sidecar's `dump_file` as an unlink path:
absolute, traversal, or mismatched sibling metadata is rejected before unlink,
keeping deletion within the configured backup directory.

## Rollback

If rollback restores a helper that does not recognize the flag, do not omit
the manifest and run rotating full maintenance. Use the supported lock-aware
backup-only `--backup-only --apply` mode until the manifest-aware helper and a
valid private manifest are restored. Backup-only preserves every complete
prior pair without a separate `--preserve-existing-backups` flag. The manifest
and the removal proof are private; neither is copied into public summaries,
sidecars, or workflow artifacts.
