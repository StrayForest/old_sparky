# Encrypted maintenance failure evidence transport

- Status: Accepted
- Date: 2026-10-16
- Owners: Platform release maintainers
- Supersedes: [private-repository transport in the original capture/reset ADR](maintenance-failure-capture-and-reset.md)

## Context

The repository is public, so GitHub Actions artifacts cannot contain raw host
evidence. Cause review still needs the bounded unit properties, transaction
receipts, queued jobs, cgroup membership and selected service journal that the
read-only capture helper collects. Reset must consume the same durable host
evidence without placing a decryption key or plaintext evidence in Actions.

## Decision

The capture action accepts one base64 PEM X.509 recipient certificate. The
workflow requires its SHA-256 fingerprint over canonical DER to match the
source-pinned recipient certificate, accepts exactly one certificate with an
RSA-3072 public key, and checks that the runner can use the selected CMS
algorithms before opening production SSH. The profile is OpenSSL CMS
EnvelopedData with AES-256-GCM content encryption and RSA-OAEP/SHA-256 key
transport. The private key remains in the operator's protected local `.git`
evidence directory with mode `0600`; it is never stored in Actions, repository
secrets, chat or a commit.

The host writes the exact bounded raw JSON once, with O_EXCL, under the
root-owned mode-`0700`
`shared/.release-recovery/maintenance-failure-evidence/` directory. Its
filename binds the source SHA, capture workflow run ID and attempt. The
existing evidence schema also binds the signed security/producer/publisher
tuple, bundle digest, transaction receipts and host identity. This host copy
is retained for reset and cause review; staging cleanup removes only the
temporary transfer directory.

The operation receipt is validated by the canonical release-transaction
validator. In `recovery-restored`, its `systemd_state_before: null` permits the
systemd companion receipt to be absent; evidence records that absence with a
null companion digest and reset rechecks the same receipt presence and hash.
When present, the companion is validated by its canonical reader and bound by
operation ID, application path, pointers and identities. Its `operation` field
is not compared to the operation receipt because the canonical writer uses
`rollback` for the systemd-state receipt.

The runner holds plaintext only in a private temporary file long enough to
encrypt it. It uploads exactly two members: DER CMS ciphertext and a closed
`encrypted_maintenance_failure_report` manifest with the source/run provenance,
recipient certificate fingerprint, ciphertext SHA-256 digest and size, and
algorithm profile. The encrypted plaintext is either the full
`maintenance_failure_evidence` document or a closed
`maintenance_failure_diagnostic` containing source/run bindings, a fixed stage
and error code, and—only for a systemd property failure—a bounded private cause
record. That record identifies the fixed service or timer query, its closed
failure class and return code, missing or extra fixed properties, and bounded
base64 stdout/stderr. It remains inside CMS ciphertext; it is never written to
the public manifest, Actions output or logs. The service query uses the fixed
Service/Unit property set; the timer query uses only its Timer/Unit properties.
When a guard fails after the bound host output path is established, the
workflow encrypts and uploads that diagnostic, then still finishes with a
failed Action conclusion. The public manifest, Actions output and logs contain
no plaintext digest or size. No raw journal or plaintext JSON is printed or
uploaded. The public artifact expires after one day. CMS
encryption does not authenticate the sender; trust comes from the
exact-source Actions run, the previously verified signed recovery
producer/publisher chain, and the independently verified GitHub artifact
metadata.

For a later reset, the workflow validates the exact completed capture run and
artifact API ID/digest, the two-member archive, closed manifest, source/run
tuple, recipient fingerprint and caller-supplied locally reviewed plaintext
digest before SSH. It transfers no evidence file to the host. The reset helper
reads the corresponding durable source/run-named host file under the
canonical release lock, compares its plaintext digest and full recorded
transaction/unit tuple, and rechecks all existing timer, job, process and
cgroup guards before the sole `systemctl reset-failed` call. Evidence is not
removed by reset; retention or disposal is a separate reviewed operation.

After download, the operator verifies the exact artifact ID, name, run and
attempt, source SHA and API `sha256:` archive digest. The operator records
those values and the exact security, producer, publisher and bundle bindings
in a closed JSON file created under the protected `.git` evidence directory
with mode `0600`. The local decrypt tool accepts the original archive and this
independent binding file; it verifies the archive digest, exact two-member ZIP,
closed manifest and every manifest field against the binding before CMS
decryption. It then checks the closed plaintext schema and source/run binding,
writes a new mode-`0600` no-replace file under `.git`, and reports the
plaintext digest and size locally. A diagnostic supports cause review only:
the reset validator accepts only `maintenance_failure_evidence` with its full
unit and transaction tuple. The plaintext digest is therefore available for
the later guarded reset input without publishing it in the artifact or Actions
logs. The local plaintext is the durable cause-review copy. Runner
temporary plaintext, certificate, SSH material and staging files are removed
on every exit path.

The binding schema is closed: `schema`, `kind`, `repository`, `workflow_path`,
`artifact_id`, `artifact_name`, `artifact_digest`, `capture_run_id`,
`capture_run_attempt`, `source_sha`, `bundle_sha256`, `security_run_id`,
`security_run_attempt`, `recovery_run_id`, `recovery_run_attempt`,
`publisher_run_id`, and `publisher_run_attempt`. `artifact_digest` preserves the
GitHub API value in `sha256:<hex>` form. The operator creates this binding only
after checking the exact completed run and artifact metadata for the linked
security, recovery producer and publisher runs; the local tool then proves the
downloaded bytes and encrypted manifest match that binding.

The local command is
`python3 platform/tools/platform_decrypt_maintenance_evidence.py --artifact-archive <archive.zip> --binding-json <protected-binding.json> --private-key <0600-private-key.pem> --certificate <matching-certificate.pem> --destination <new-git-private-evidence.json> --git-dir <repository/.git>`.

## Verification ownership

The existing `test_platform_recovery_bootstrap` privileged owner exercises the
extracted fake-SSH capture shell through encrypted staging, a real OpenSSL CMS
round trip, recipient fingerprint rejection, wrong-key and tampered-tag
failures, and verifies that the artifact contains no plaintext. Existing
helper tests cover the durable root-only no-overwrite path and reset's fresh
state checks. Production reset remains unauthorized until the decrypted cause
evidence has been reviewed and a separate operation is approved.
