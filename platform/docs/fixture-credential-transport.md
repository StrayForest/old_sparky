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
test "$(stat -c '%F:%h:%a:%u' -- "$identity_file")" = "regular file:1:600:$(id -u)"
test "$(stat -c '%F:%a:%u' -- "$(dirname -- "$identity_file")")" = "directory:700:$(id -u)"
test "$(stat -c '%s' -- "$identity_file")" -le 4096
identity_dir="$(mktemp -d)"
chmod 700 "$identity_dir"
trap 'rm -rf -- "$identity_dir"' EXIT
python3 platform/tools/platform_fixture_credential_transport.py install-age --directory "$identity_dir" >/dev/null
recipient="$("$identity_dir/age-keygen" -y "$identity_file")"
test "$recipient" = "age1egptr8zhhvfe0ggmyu4cr9azhfsjpvvn3p5eyppznnzx8c5yedjsyeaasa"
key_lines="$(awk '/^AGE-SECRET-KEY-1/ && length($0)==74 { n++ } END { print n+0 }' "$identity_file")"
test "$key_lines" = 1
gh secret set FIXTURE_MANIFEST_AGE_IDENTITY --env fixture-manifest < <(awk '/^AGE-SECRET-KEY-1/ && length($0)==74 { printf "%s", $0 }' "$identity_file")
gh secret set FIXTURE_MANIFEST_AGE_IDENTITY --env production < <(awk '/^AGE-SECRET-KEY-1/ && length($0)==74 { printf "%s", $0 }' "$identity_file")
```

Restrict the `fixture-manifest` environment to the `dev` branch. Keep
`FIXTURE_MANIFEST_AGE_IDENTITY` only in that environment and the existing
`production` environment, never as a repository secret. The same fixture-only
identity is needed by production fixture setup, the CPU diagnostic and trusted
finalization; it is separate from SSH and Cloudflare credentials. Do not change
existing production environment protections as part of this setup.

For first-time bootstrap or rotation, generate a new identity in a private
mode-`0700` directory and derive its public recipient with the pinned
`age-keygen -y`. Review and update the checked-in recipient and its tests in a
source change, pass CI, and deploy that source before provisioning the matching
private key with the command above. Never compare a newly generated random
recipient against the current fixed recipient or provision a key that the
active source cannot decrypt.

Do not create or change environment secrets until the source change is
reviewed.

## Failure handling and evidence

If fixture setup has begun and decryption fails, the workflow remains failed.
Use the existing exact-run manual cleanup procedure with the operator-supplied
control identity; do not fabricate a manifest or bypass source-binding checks.
A failed decrypt is not proof that cleanup ran.

Do not manually download or decrypt the private-input or encrypted-manifest
artifacts. Use the authenticated artifact inspection procedure only for the
closed descriptor and separately sanitized report, fingerprint and cleanup
evidence. A short retention period does not replace encryption or justify
publishing plaintext.
