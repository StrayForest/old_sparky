# External fixture credential transport

## Confidentiality boundary

The external-load input contains `control_email`; the fixture manifest contains
session and CSRF tokens, user IDs, fixture slugs and marker data. Treat both as
credentials. Actions artifacts are readable to repository readers, so their
only permitted payloads are age ciphertext and the closed public descriptor.
Never upload or log plaintext input/manifest, their credentials, or a digest of
the low-entropy control email. The descriptor contains exact run/attempt and
runner/app binding, nonsecret dimensions and the random-token manifest digest;
it contains no email, token, user ID, slug or marker.

The fixture-setup workflow writes this descriptor through the shared
`platform_fixture_credential_transport.py descriptor` command. Its public
projection is a closed allowlist. The scheduled read-mix profile may expose
only `fixture_schedule=read-mix-concurrency-ramp-v1`; the builder copies that
value into the manifest summary, and the validator rejects any mismatch there.
The load consumer also compares the summary with the decrypted manifest. Other
fixture kinds reject the schedule field; ordinary descriptors omit it.

The workflow pins age v1.3.2 and the release archive SHA-256 in
`tools/platform_fixture_credential_transport.py`. The recipient is public and
checked in. A private identity is a fixture-only key, never an SSH or
Cloudflare key. Candidate code receives the validated manifest and descriptor
in private runner files, but not `control_email`; the load-client has no
production SSH secret. The source/profile resolver runs on a separate runner after masking and
scrubbing the event file; it never receives the email. A later serializer job
has no checkout and uses fixed inline code to serialize and age-encrypt the
private input before publishing ciphertext. Setup, load-client and diagnostic
jobs stage the fixture identity with fixed inline code, decrypt with the pinned
age binary, and remove the identity before any checkout-controlled code runs.
The load-client and diagnostic parent may read the validated fixture manifest
from a mode-`0600` runner file, but neither receives `control_email` or the
production SSH key. Setup/finalization consume the encrypted input only in
trusted workflow steps. The manual retained-load cleanup route also masks and
scrubs the event before checkout or processing; it receives the operator email
only in its trusted validation step. If ciphertext decryption fails, the job
fails closed and the canonical manual cleanup route remains available.

The Actions workflow verifies the pinned release archive digest, then extracts
only its `age/age` member under `$RUNNER_TEMP/fixture-age-tools`. The executable
is therefore `$RUNNER_TEMP/fixture-age-tools/age/age`; each trusted encrypt or
decrypt step checks that exact path before using it. Do not flatten the member
or invoke the parent `age` directory. To verify input encryption, fixture setup,
manifest encryption, trusted load-client decryption, descriptor validation and
staged cleanup together with the real pinned archive, run from `platform/`:

```bash
/usr/bin/python3.12 -m unittest \
  tests.test_platform_secret_artifact_boundary.FixtureCredentialWorkflowBoundaryTests.test_workflow_age_install_encrypt_consumer_decrypt_uses_real_archive_layout
```

The test uses a generated synthetic identity and payload. It does not read the
configured fixture identity or any production input.

Event scrub steps reparse rewritten JSON with duplicate-key rejection, require
`inputs.control_email` to be absent, and verify that the original and canonical
email values are absent from every parsed string key and value. Unrelated event
metadata may contain the text `control_email`; a byte search for that field
name is not a valid scrub check. Before parsing, each step opens the runner event
file without following symlinks and checks that it is a regular, runner-owned,
single-link file no larger than 64 KiB, with no special permission bits or
group/other write permission. Runner-owned `0600` and `0644` inputs are accepted;
the step seals the already-open file descriptor to `0600` and verifies that mode
before reading its contents.

The manual retained-cleanup workflow writes its scrubbed event to the private
`$RUNNER_TEMP/retained-cleanup-sanitized-event.json` handoff. Its later
validation step reads that bounded, owner-only file rather than assuming a
subsequent action sees the same contents at `GITHUB_EVENT_PATH`. This keeps the
masked cleanup input stable across action steps while leaving GitHub's event
file under runner ownership.

To run the event-mode, credential-scrub, cleanup handoff and finalizer-route
regressions from `platform/`:

```bash
/usr/bin/python3.12 -m unittest \
  tests.test_platform_external_load_workflow_contract.ExternalLoadWorkflowContractTests.test_event_scrub_steps_seal_owner_readable_event_files_and_reject_unsafe_metadata \
  tests.test_platform_external_load_workflow_contract.ExternalLoadWorkflowContractTests.test_retained_cleanup_identity_uses_event_file_and_private_stdin_handoff \
  tests.test_platform_external_load_workflow_contract.ExternalLoadWorkflowContractTests.test_fixture_commit_without_any_candidate_job_allows_exact_finalizer_cleanup
```

The real pinned-age integration regression also executes the derive-source-
binding scrub and descriptor validator after the synthetic manifest is
encrypted. It uses only synthetic values and checks that the derived workflow
outputs contain no private input.

## Provision the fixture-only identity

For the current recipient, provision the already-prepared matching identity; do
not generate a new random key and compare it with the fixed recipient. The
private identity file must remain outside the repository. The command below
accepts its path interactively, verifies its owner/mode/link count and derived
public recipient, and sends only the private key line to GitHub CLI over stdin.
It never prints the key or copies it to another file.

```bash
set -euo pipefail
umask 077
read -r -p "Path to the existing private age identity file: " identity_file
[[ "$identity_file" = /* ]]
test ! -L "$identity_file"
repo_root="$(realpath -e -- "$(git rev-parse --show-toplevel)")"
identity_real="$(realpath -e -- "$identity_file")"
case "$identity_real" in "$repo_root"|"$repo_root"/*) exit 1 ;; esac
test "$(stat -c '%F:%h:%a:%u' -- "$identity_real")" = "regular file:1:600:$(id -u)"
test "$(stat -c '%F:%a:%u' -- "$(dirname -- "$identity_real")")" = "directory:700:$(id -u)"
test "$(stat -c '%s' -- "$identity_real")" -le 4096
identity_dir="$(mktemp -d)"
chmod 700 "$identity_dir"
trap 'rm -rf -- "$identity_dir"' EXIT
python3 platform/tools/platform_fixture_credential_transport.py install-age --directory "$identity_dir" >/dev/null
recipient="$("$identity_dir/age-keygen" -y "$identity_real")"
test "$recipient" = "age1egptr8zhhvfe0ggmyu4cr9azhfsjpvvn3p5eyppznnzx8c5yedjsyeaasa"
key_lines="$(awk '/^AGE-SECRET-KEY-1/ && length($0)==74 { n++ } END { print n+0 }' "$identity_real")"
test "$key_lines" = 1
gh secret set FIXTURE_MANIFEST_AGE_IDENTITY --env fixture-manifest < <(awk '/^AGE-SECRET-KEY-1/ && length($0)==74 { printf "%s", $0 }' "$identity_real")
gh secret set FIXTURE_MANIFEST_AGE_IDENTITY --env production < <(awk '/^AGE-SECRET-KEY-1/ && length($0)==74 { printf "%s", $0 }' "$identity_real")
```

Restrict the `fixture-manifest` environment to the `dev` branch. Keep
`FIXTURE_MANIFEST_AGE_IDENTITY` only in that environment and the existing
`production` environment, never as a repository secret. The same fixture-only
identity is needed by production fixture setup, the CPU diagnostic and trusted
finalization; it is separate from SSH and Cloudflare credentials. Do not change
existing production environment protections as part of this setup.

For first-time bootstrap or rotation, first stop new diagnostic dispatches
and let every run encrypted to the current recipient finish its canonical
cleanup. Do not replace either environment key while an active run may need to
decrypt its input or manifest. Generate a new identity in a private
mode-`0700` directory and derive its public recipient with the pinned
`age-keygen -y`. Review and update the checked-in recipient and its tests in a
source change, pass CI, and deploy that source before provisioning the matching
private key with the command above. With runs quiesced, update both environment
secrets to the matching new identity before resuming dispatches. Never compare
a newly generated random recipient against the current fixed recipient or
provision a key that the active source cannot decrypt.

Do not create or change environment secrets until the source change is
reviewed.

## Failure handling and evidence

If fixture setup has begun and decryption fails, the workflow remains failed.
Use the existing exact-run manual cleanup procedure with the operator-supplied
control identity; do not fabricate a manifest or bypass source-binding checks.
A failed decrypt is not proof that cleanup ran.

For a terminal run that fails before fixture preparation, verify the exact
attempt's job and step statuses. Treat it as a prefixture no-op only when
`Validate external-load dispatch` failed before setup and `Prepare external
fixture with ephemeral SSH`, `Run checked-out external HTTP load client`, and
`Run isolated zero-credit authenticated SSR CPU pair` are all skipped, with no
fixture-preparation step started. Missing reports or a default open-namespace
field alone do not show that a fixture or load existed. If fixture preparation
started, use the exact-run cleanup procedure even when later evidence is
missing.

Do not manually download or decrypt the private-input or encrypted-manifest
artifacts. Use the authenticated artifact inspection procedure only for the
closed descriptor and separately sanitized report, fingerprint and cleanup
evidence. A short retention period does not replace encryption or justify
publishing plaintext.
