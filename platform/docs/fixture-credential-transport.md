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

After reviewing the recipient change, run this from the repository root. It
creates an ephemeral identity, verifies the public recipient and streams the
private key directly to the authenticated GitHub CLI. Do not display or copy
the identity into a command argument, shell history, log or file outside this
private directory.

```bash
set -euo pipefail
umask 077
identity_dir="$(mktemp -d)"
chmod 700 "$identity_dir"
trap 'rm -rf -- "$identity_dir"' EXIT
python3 platform/tools/platform_fixture_credential_transport.py install-age --directory "$identity_dir" >/dev/null
"$identity_dir/age-keygen" -o "$identity_dir/identity.txt" 2>/dev/null
chmod 600 "$identity_dir/identity.txt"
test "$("$identity_dir/age-keygen" -y "$identity_dir/identity.txt")" = \
  "age1egptr8zhhvfe0ggmyu4cr9azhfsjpvvn3p5eyppznnzx8c5yedjsyeaasa"
gh secret set FIXTURE_MANIFEST_AGE_IDENTITY --env fixture-manifest < "$identity_dir/identity.txt"
gh secret set FIXTURE_MANIFEST_AGE_IDENTITY --env production < "$identity_dir/identity.txt"
```

Configure `fixture-manifest` to allow only the `dev` branch. Keep the
`FIXTURE_MANIFEST_AGE_IDENTITY` secret value in both named environments and
never add it to repository secrets. Rotation requires a reviewed recipient
change and updating both environment secrets together. Do not create or change
environment secrets until the source change is reviewed.

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
