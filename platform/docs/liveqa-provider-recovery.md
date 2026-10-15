# LiveQA provider recovery

- Status: Exceptional operator procedure
- Owner: Production operator
- Last reviewed: 2026-10-15

Use this procedure only when protected LiveQA consumers reference an exact
immutable engine provider whose payload root is missing. It does not authorize
normal release installation, application deployment or pointer changes.

## Source and archive validation

Bind the exact artifact run and attempt, source commit, outer artifact digest,
inner release-tar digest and cryptographic attestation. Validate the complete
tar using the canonical release validator from its exact source commit, or a
reviewed validator proven compatible with that artifact contract. Extract the
full archive into a new root-only private directory; do not hand-extract files
or use a candidate checkout. A later validator can require files absent from
an older successful artifact. Preserve that refusal and use the source-matched
validator without weakening its checks. Retain the archive and private
extraction through final LiveQA completion.

## Provider-only publication

Run only the reviewed one-time provider tool under the canonical platform
release lock. It must bind the source and runtime manifest to the verified
artifact, validate the complete v1 source tree and engine map, and compare the
protected current/previous release pointers and all provider consumers before,
immediately before and after publication. The only allowed publication is the
missing immutable provider directory. Do not run the generation installer or
reconciler, switch active pointers, alter users or secrets, replace an existing
provider, or remove other artifacts. On any mismatch, preserve protected state
and stop. Provider recovery does not replace the required verified backup,
browser QA or normal GitHub Actions release gates.

For the verified 2026-10-15 recovery, bind the invocation to app directory
`/opt/oldsparky/platform`, current active source
`9ceaf09ab7ba4bf5c5e636865106910eaa034c8d`, and the extracted runtime at
`/root/old_sparky/.git/codex-performance-20261012/artifact-37614074702-attempt1-quarantine/private-extract-11479063406-19603-source19603-validator/gha-37614074702-1-19603dfff122/liveqa-runtime`.
The active 9ce tool does not contain this new recovery command. After the
reviewed feature blob is published and its exact-SHA checks pass, place one
O_EXCL root-owned, mode-0500 copy in a dedicated root-only directory outside
both the LiveQA trust root and active release, named by the tool SHA-256; do
not modify the active release or LiveQA tree. For the reviewed source blob,
the 106,351-byte sidecar SHA-256 is
`e2f3412de794b70c1fe5868b58f50cf1398fbbe2a3e9de1714f33f83af1f54a9`, and the path is
`/root/.oldsparky/provider-recovery-tools/platform_live_qa_runtime_install-e2f3412de794b70c1fe5868b58f50cf1398fbbe2a3e9de1714f33f83af1f54a9.py`.
Before execution, verify that sidecar's owner, mode, link count, size and
SHA-256 against the published blob, and verify the sidecar parent directory's
stable device/inode/owner/mode identity. Confirm that the extracted source and
its manifest still match the attested run-37614074702/attempt-1 artifact, and
take a fresh protected reference snapshot. The exact locked command is:

```sh
/opt/oldsparky/platform/current/tools/platform_release_lock_exec.sh \
  --app-dir /opt/oldsparky/platform \
  --expected-sha 9ceaf09ab7ba4bf5c5e636865106910eaa034c8d \
  -- /usr/bin/python3.12 -I -B \
  /root/.oldsparky/provider-recovery-tools/platform_live_qa_runtime_install-e2f3412de794b70c1fe5868b58f50cf1398fbbe2a3e9de1714f33f83af1f54a9.py \
  provider-recover \
  --runtime-source /root/old_sparky/.git/codex-performance-20261012/artifact-37614074702-attempt1-quarantine/private-extract-11479063406-19603-source19603-validator/gha-37614074702-1-19603dfff122/liveqa-runtime
```

The command is eligible only if the source tree and all three consumer
references still pass their fixed checks while the canonical lock is held. An
already-valid target is reported as `already-valid`; an invalid existing
target is preserved and causes refusal. If a new target is published but a
post-publication check fails, retain it for inspection and stop. After a
successful result, independently verify the immutable provider tree and all
three consumer references, plus unchanged current/previous/active pointers,
before considering any later QA action. Do not treat this provider-only
operation as a backup, deployment, restore or QA pass.
