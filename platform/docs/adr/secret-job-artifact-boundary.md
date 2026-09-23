# Secret-job artifact boundary

- Status: Superseded by [Digest-bound trusted live-QA runtime generation](trusted-live-qa-runtime-generation.md)
- Date: 2026-09-19
- Owners: Platform maintainers

## Context

Production and Cloudflare credentials must not share a runner checkout with
candidate source, candidate helpers, or an unverified archive. A workflow
artifact is an untrusted data handoff even when its producer is a successful
candidate build. Live-user QA is the special case: its browser test must use a
production-only credential contour without allowing a candidate mailbox
helper or candidate QA supervisor to run on the credential-bearing path.

## Decision

Candidate validation and builds run on secret-free jobs. A credential-bearing
consumer starts without checkout and accepts only a closed, least-permission
handoff. Cloudflare Draft publication receives a digest-bound bundle and runs
only the pinned Wrangler client; the read-only Cloudflare audit uses a fixed
inline projection. Production recovery, retention, storage and release
actions use the installed dispatcher/helpers under `/opt/oldsparky/platform`
and never source a workflow checkout.

The live-user QA consumer accepts only the fixed origin and target SHA, then
calls the installed remote dispatcher. The dispatcher verifies that the
active release metadata matches the SHA, that the root-owned bundle is mode
`0600`, and that the root-owned mailbox helper is the fixed
`/root/.oldsparky/liveqa/platform_live_qa_mailbox_helper.py`. The browser
supervisor itself must be the separately provisioned, root-owned
`/root/.oldsparky/liveqa/platform_live_user_qa_trusted.sh` (mode `0755`); it
is never copied from the candidate checkout or the workflow artifact. The
dispatcher fails closed if that host installation is absent, replaced, or
points at another mailbox helper.

Classifier reasons and all values written to `GITHUB_OUTPUT` are bounded
single-line printable values. Autodeploy extracts only a bounded regular
manifest member from the classifier ZIP. Production artifact directories are
created by a root-owned helper using directory-relative `O_NOFOLLOW` opens,
atomic `mkdirat`-style creation, and post-create inode checks.

## Consequences

- A live-user QA run is unavailable until the host image provisions the
  trusted supervisor and its browser/test runtime outside the candidate
  release. This is intentional: silently falling back to
  `current/tools/platform_live_user_qa.sh` would reintroduce candidate code
  into the secret boundary.
- Candidate builds remain testable and deployable through data-only, digest-
  checked handoffs, while secret jobs have no source checkout to execute.
- ZIP and output protocol violations fail closed and leave no trusted
  artifact or output record to consume.
