# Platform CSP and live QA runbook

- Status: Active how-to
- Owner: Production operator
- Last reviewed: 2026-10-20

This is the special CSP/browser/live-user QA path. Normal production release,
rollback and release-transaction recovery are owned by
[`deployment-runbook.md`](deployment-runbook.md).

## Preconditions

1. Commit the reviewed release scope and preserve unrelated work. Release
   metadata records `HEAD`; never build an artifact from dirty release sources.
2. Run focused checks and all gates required by the changed owners.
3. Confirm migration expand/rollback compatibility.
4. Confirm services healthy, disk has at least 5 GiB available and at most 85%
   conservative use, and protected `current`/`previous` releases.
5. Create a fresh restore-verified backup.

## Build

```bash
cd /root/old_sparky
platform/tools/platform_build_release.sh <release-slug>
```

Every CSP release is built once as an immutable enforced release. There is no
production Report-Only candidate or temporary mode switch. The build gate
requires byte-identical direct requirements, the tracked hash-locked Python
lock, generated freeze, wheelhouse manifest and web package lock. Every build
installs the hash-locked `requirements-platform.lock.txt`, regenerates the
sorted freeze and refuses any package-pin difference before packaging.
Dependency updates require their own reviewed release. Release builds require
the exact reviewed Node `26.3.1` and npm `11.16.0` toolchain recorded in
`RELEASE.json`.

Record the tarball path, SHA-256 and source commit from `RELEASE.json`.
Verify the adjacent immutable checksum before installation; the installer
repeats this check and refuses malformed archives before extraction:

```bash
(cd platform/dist/releases && \
  sha256sum -c <release-slug>.tar.gz.sha256)
```

## Stage and activate

```bash
cd /opt/oldsparky/platform/current
tools/platform_release_preflight.sh \
  --require-previous --require-verified-backup --require-edge-parity \
  --backup-max-age-hours 24

cd /root/old_sparky
platform/tools/platform_release_deploy.sh \
  --artifact platform/dist/releases/<release-slug>.tar.gz \
  --app-dir /opt/oldsparky/platform \
  --expected-csp-mode enforce
tools/platform_release_preflight.sh \
  --require-previous --require-verified-backup --backup-max-age-hours 24
/opt/oldsparky/platform/shared/venv/bin/python \
  tools/platform_deploy_smoke.py \
  --edge-origin https://127.0.0.1 \
  --edge-host old-sparky.com \
  --edge-insecure-loopback \
  --expected-csp-mode enforce
/opt/oldsparky/platform/shared/venv/bin/python \
  tools/platform_deploy_smoke.py \
  --edge-origin https://old-sparky.com \
  --expected-csp-mode enforce
```

Do not print the shared environment. Use `--skip-python-deps` only after
dependency compatibility has been verified against the existing shared venv.
Normal installs reuse it only after the root verifier proves release and
transaction identities, dependency and wheelhouse bytes, interpreter ABI,
installed records/scripts/bytecode, `pip check` and exact freeze; any mismatch
uses the offline rebuild and full rollback snapshot.
That mode now publishes a root-only rollback receipt containing the exact
pre-install release target, an `unchanged` transition marker and the accepted
artifact-freeze digest. A normal rollback then proves the shared venv still
matches that freeze and switches only the release pointers; missing, modified
or mismatched receipt data fails closed. Do not use `--skip-venv-restore` to
bypass a receipt failure.
`platform_install_systemd_units.sh` also runs the reviewed service-user
preparer for the newly current release before restart; this creates and owns
the intentionally unbundled `.next/cache` mount target plus the existing
shared worker/media runtime directories. Never restart `deadlock-web` against
a new release before that preparation succeeds.
Pass `--expected-csp-mode enforce` for the release. The nonce-capable web
process owns the document policy and Nginx remains CSP-free; do not run smoke
until the new web process is ready and the validated Nginx pair is active.

## Nginx changes

The installer validates and atomically installs the vhost plus shared security
snippet. It rolls both back on failure.

```bash
cd /opt/oldsparky/platform/current
/opt/oldsparky/platform/shared/venv/bin/python \
  tools/platform_install_nginx.py --apply --reload --json
```

Dry-run is the default. The candidate must pass its policy validator and
`nginx -t`. For local HTTPS/SNI smoke:

```bash
/opt/oldsparky/platform/shared/venv/bin/python \
  tools/platform_deploy_smoke.py \
  --edge-origin https://127.0.0.1 \
  --edge-host old-sparky.com \
  --edge-insecure-loopback \
  --expected-csp-mode enforce
```

`--edge-insecure-loopback` is forbidden for non-loopback origins. Public smoke
keeps normal certificate verification. The CSP mode is mandatory and must be
`enforce` for every production release.
The loopback/SNI smoke requires ready health `200`; the public smoke requires
the intentionally private health endpoint to return `403` with the shared
non-document security headers.

## CSP production change

1. Build and activate one immutable release with the enforced policy described
   in the [security runbook](security-runbook.md). Do not deploy a temporary
   Report-Only policy.
2. Run all release/security gates, both live-user contours below and the
   complete applicable QA/performance contour. A passing deploy smoke or only
   one live-user contour is not the release gate.
3. Observe the enforced release for at least 24 hours. There must be no
   unexplained first-party CSP report, missing/duplicate/wrong-mode header,
   nonce/cache failure, new 5xx/service warning, queue/DB pressure or accepted
   performance regression. Classify browser-extension noise without weakening
   the policy.

Any wrong CSP mode, unexpected directive/source, reused or short nonce,
nonce-bearing cached HTML, live-user cleanup failure or unexplained first-party
violation stops progression. The `script-src` compatibility tokens and the
AdSense style-attribute exception are intentional parts of the documented
policy; do not add unrelated sources to silence a report. The exact policy,
ownership and report limiter are in the [security runbook](security-runbook.md).

## CSP live-user gates

Each production release requires two independent contours, in this order:

1. an automated tournament/roster/bracket journey using short-lived QA
   sessions and no authentication route or Turnstile;
2. after its exact-ID cleanup succeeds, a human auth lifecycle and production
   Turnstile check in ordinary Chrome.

Neither contour substitutes for the other. Use a fresh `liveqa-*` marker for
each release. Do not run the contours concurrently or start the manual contour
until the automated inventory, users, sessions, tournament and media have all
been confirmed absent.

The credential-bearing journey never runs a candidate checkout or unverified mailbox helper. The source-only `liveqa-runtime` manifest v3 adds confined declared `engine_links` to the v2 keys (no `source_sha`); its engine-tree digest uses the typed, domain-separated `oldsparky-liveqa-engine-v2` encoding for regular-file digests and exact symlink targets. The provider manifest v2 binds `source_sha`, carries the same link map and installs candidate-owned bytes under `provider-runtime`; prior complete v1 generations remain immutable at `runtime` and may serve only when their exact engine map and package-lock digest match. Activation checks Node 26.3.1, package lock, required Playwright packages, browser roots, Chromium sandbox and the exact file/link maps under the release lock, then records the provider generation in the digest-bound payload before switching active. Missing, stale or incompatible providers fail closed; no environment or candidate-checkout path is trusted. Provider hashing stays bounded by signed member limits; release bootstrap validates the full archive before omitting only the wheelhouse and standalone web bundle from its temporary control tree. For missing-provider recovery, see the [provider recovery runbook](liveqa-provider-recovery.md).

The runtime contract treats one installer-produced payload as one identity: standalone dispatcher `verify`, safe-env `validate-runtime` and root guard payload/runtime-root resolution must agree before activation. Only source-owned typed `engine_links` materialize in the payload; an external provider's validated link map remains metadata and creates no local `provider-runtime` links. A mismatch fails closed.

The fixed root-owned supervisor at
`/root/.oldsparky/liveqa/platform_live_user_qa_trusted.sh` (mode `0755`) then
dispatches only that active generation; a missing, stale, symlinked or
interrupted installation fails closed. The host still needs the dedicated
account/AppArmor profile. Run as root and first create the root-only bundle
through the public shell wrapper:

The root-only generation remains under `/root`, which the dedicated browser
identity cannot traverse. The trusted runner therefore binds the validated
suite and engine directories read-only into unique
`/var/lib/oldsparky-liveqa/runtime-suite-<source-sha>-<gate-nonce>` and
`runtime-engine-<provider-sha>-<gate-nonce>` paths in the transient systemd
unit's private mount namespace. The aliases exist only for that collected
unit and are removed after its processes have exited; the host mount table and
generation ownership do not change. The browser's preseeded input, home,
temporary files and test results remain in its dedicated writable
`/run/oldsparky-liveqa` gate.

The generation manifest includes the launch supervisor's direct script and data dependencies, including the provisioning shell wrapper, its Python implementation and reviewed AppArmor profile. Reconciliation rejects an incomplete required-member set. Before any account installation or provisioning, the supervisor runs the fixed `web/tests/support/live-engine-preflight.cjs` through the existing `engine-preflight` wrapper mode after identity checks. It opens and closes an `about:blank` page in Chromium and WebKit under the normal QA UID, locks and read-only mounts; it navigates no app route and needs no account or database. Failure is an early `engine_preflight` stage/check, not a Live QA pass.

```bash
cd /root/old_sparky
install -d -o root -g root -m 0700 /root/.oldsparky/liveqa
platform/tools/platform_install_live_qa_user.sh --apply
PLATFORM_APP_DIR=/opt/oldsparky/platform \
platform/tools/platform_provision_live_csp_qa.sh \
  --marker liveqa-csp-candidate-<unique> \
  --bundle-path /root/.oldsparky/liveqa/csp-live-qa.json \
  --primary-email 'REPLACE_WITH_PRIMARY_LIVE_QA_EMAIL' \
  --mailbox-helper /root/.oldsparky/liveqa/platform_live_qa_mailbox_helper.py
```

For an existing bundle, release reconciliation owns the fixed mailbox helper
alongside the supervisor and dispatcher. Automated secret-bearing workflows
never refresh it from a checkout. The dispatcher checks the fixed
`/root/.oldsparky/liveqa/platform_live_qa_mailbox_helper.py` path, root-only
`0500` mode, its active-manifest digest and the bundle's exact helper binding
before invoking the trusted supervisor. Never bypass that check or copy a
helper from an unrelated checkout.

The canonical automated journey dispatch is:

```bash
gh workflow run platform-live-user-qa.yml \
  --ref dev \
  -f confirmation=RUN-LIVE-USER-QA
```

Dispatch only after confirming the exact deployed `dev` SHA; no fixture is created until release lock, active SHA, host supervisor/helper, bundle and browser preflight checks pass.
The fixed desktop journey uses a 1,500-second child cap for its 600-second Playwright test plus separate 600-second teardown (300 seconds planning reserve); systemd stays capped at 30 minutes.
The remote operation cap is 2,400 seconds (900 seconds planning reserve) inside the 45-minute job (final 300 seconds shared with bounded handoff/report steps; pre-helper checks consume part); external setup/network time is not guaranteed, other action limits are unchanged, and timeout/cleanup uncertainty fails closed.
The workflow accepts a bounded dispatcher diagnostic only when its fixed schema and child exit code agree. Early-failure `stage` and `check` values are closed enums; the sanitizer publishes no exception text, paths, environment values or stderr. Once the trusted dispatcher starts, fixed markers identify validated pre-child, helper-trust, spawn, and known live-user wrapper boundaries; a started child’s unmarked nonzero exit remains stage-free `unclassified`, timeout remains `child_timeout`, and a zero-exit child missing valid counts or its success diagnostic becomes `protocol_failure` and cannot pass. Failures before the dispatcher starts—including SSH setup—and SSH exit 255 remain unclassified. Markers preserve the original failure outcome and ordinary fixture cleanup. The direct-exec UserQA entrypoint uses `/usr/bin/env -S /usr/bin/python3.12 -I -B` so the fixed sudo command keeps isolated imports and disables bytecode writes. These diagnostics cannot establish the cause of historical runs.

A failed canonical browser run writes closed reporter data beside counts. Under the QA lock, the guard validates ID, project, status, attempts and safe source locations against the immutable payload before atomically publishing `public-live-qa.<gate-id>.failure-v2.json` under `/root/.oldsparky/liveqa/diagnostics/`. Failed attempts include a closed page-error class and route; a source asset basename and bounded line/column are retained only when a stack frame matches a successfully loaded same-origin Next chunk. The afterEach hook attaches an explicit empty-or-captured result; observed empty is absent, while missing or malformed evidence is unavailable. When the 16 KiB budget drops event detail, test identities, statuses and source locations remain, and removed event data is marked unavailable/truncated. Raw error messages, stacks, full URLs or queries, DOM, attachment bytes, credentials and personal data are never persisted. The root-only directory and regular files use `0700`/`0600`; reports are capped at 16 KiB, retained seven days and at most ten records. They are private operator records, excluded from GitHub artifacts and public counts. Gate/source/app/manifest/tree/marker digests and timestamp support workflow/ledger correlation but do not cryptographically bind a GitHub run/attempt. Existing v1 records remain readable by retention. Store preparation is best effort and cannot skip the browser suite or change a passing result; when failed counts cannot be privately published, the fixed `failure_report_unavailable` marker accompanies the failed QA result and ordinary gate cleanup still runs.

LiveUserQA failures use the same private store through `live-user-failures-v3.json` and are published before gate cleanup as `live-user-qa.<gate-id>.failure-v3.json`. The guard binds the record to the gate, source/app SHAs and marker, then validates it against the active immutable payload; this does not cryptographically bind a GitHub run or attempt. A record retains only the fixed journey ID/project, failure `status` (`failed`, `timedOut` or `interrupted`), matching `run_status` (`failed`, `timedout` or `interrupted`) and attempt, an allowlisted repository-relative `TestResult.errors[].location`, and a closed `apiJson` operation/method/status/error-kind tuple only when its candidate frame exactly matches the terminal error location. Missing, malformed or unmatched fields are `unavailable`; raw messages, stacks, URLs, IDs, request/response bodies and credentials are never persisted. Public counts and failure status remain unchanged; the private report is excluded from public artifacts and uses the same size and retention limits above.

An empty sanitized workflow artifact does not show that the private record is absent. An incident read is root-local, bounded and validated by the active immutable guard: open the diagnostics directory with a no-follow directory descriptor; examine only the v3 filename namespace within the ten-record store bound; pass each candidate through `_read_failure_store_file` and `_validate_retained_failure_report` before projecting closed fields. Require exactly one record matching the gate/source/app/marker tuple and active-payload manifest/tree identity, corroborated by the independent workflow handoff or ledger. These digests support correlation but do not cryptographically bind a GitHub run or attempt. This is not a supported reader CLI or an SSH/raw-report export path; never print raw JSON.

The normal wrapper removes root state only after exact-inventory cleanup succeeds. Cleanup commits the deletion, then opens a fresh database session to verify zero recorded user, tournament, media, session and audit rows, plus no marker-owned users or tournaments; state removal also requires the exact browser gate to be absent. No database mutation occurs between that verification and state removal. This control flow supports the cleanup result but does not persist a separate SQL receipt.
The launch workflow validates `base_url`, `provision` and the optional marker with the canonical bounded-ASCII workflow-input parser before it creates any SSH key file or performs keyscan. It hands the accepted values to the remote
host as private mode-0600 JSON on stdin; the SSH command contains only the
literal `live-launch` dispatcher mode. The remote dispatcher and
the trusted launch wrapper pass the values as fixed argv in this order: base
URL, provision flag, marker, and target SHA. The wrapper requires exactly four
arguments, validates them, verifies the target, and invokes the release-locked
dispatcher; the managed `platform_live_launch_supervisor.sh` validates them
again. The wrapper does not depend on custom environment variables surviving
`sudo`. Shell punctuation, quotes, newlines,
command substitutions, option-like prefixes, Unicode/control/NUL-equivalent
and overlong markers are rejected without reaching SSH, and rejected values
are never printed in the browser report.

To rotate a stale existing bundle, dispatch the same launch workflow with
`provision=true` and a fresh marker. The provisioner does not unlink the old
bundle: under the release lock it checks the prior exact roster IDs, marker
tournaments and recovery state, then atomically publishes a new bundle and
roster. Any remaining old scope fails closed and must be recovered before a
retry.

```bash
gh workflow run platform-live-launch.yml \
  --ref dev \
  -f base_url=https://old-sparky.com \
  -f provision=true \
  -f marker=liveqa-csp-rotate-<unique>
```

The dedicated `oldsparky-liveqa` system account has `/nonexistent` as its home,
`/usr/sbin/nologin` as its shell, its same-named non-root primary group and no
supplementary groups. `oldsparky-platform` must exist because it owns platform
runtime paths. The legacy `oldsparky` identity may be absent; when present, it
remains reserved and must not collide with the dedicated LiveQA UID or GID.
The trusted supervisor consumes only the active release-bound generation; it
checks the active `current/RELEASE.json` source commit before starting and
refuses any candidate-provided path that is not digest-bound by the installed
manifest.

The supervisor suppresses ordinary installer, identity and browser output.
The trusted dispatcher also emits the same fixed `LIVE_LAUNCH_STATUS` line
when a `run-launch` check fails before the supervisor starts. Its `stage` is
one of the existing closed values (`validation`, `identity`,
`trusted_generation` or `dispatch`); it carries only the target source SHA and
numeric exit status. The dispatcher accepts success only for a complete
browser run with exit status zero; an absent, duplicate, malformed or
mismatched line fails closed. The workflow publishes only the closed
stage/status projection and never persists SSH stderr or raw child output as
the browser report.

The same installer loads two named AppArmor profiles granting `userns` only to
the checksum-pinned Chromium and headless-shell revision paths below the
root-owned immutable installed runtime. It validates the profile before loading it
and requires the installed root-owned `0644` copy to match the reviewed source.
Keep `kernel.apparmor_restrict_unprivileged_userns=1` and
`kernel.unprivileged_userns_clone=1`; automated preflight refuses a disabled
global restriction or either missing named profile. Never replace this narrow
profile with a global sysctl change.

`platform_provision_live_csp_qa.py` is the wrapper's internal implementation;
do not invoke it directly or print/load the production environment in the
interactive shell. The provisioner writes only to `platformdb`, schema
`platform`, and success output contains only the marker, bundle path and account
count. It never prints generated credentials. In trusted runs, safe-env binds
the interpreter's Python path to the complete immutable QA payload root; each
database helper derives its nested `platform` package directory from its own
installed file path before importing platform packages.

The bundle is an exact v1 object:

```json
{
  "version": 1,
  "marker": "liveqa-csp-candidate-<unique>",
  "created_at": "<recent-UTC-timestamp>",
  "email": "REPLACE_WITH_PRIMARY_LIVE_QA_EMAIL",
  "password": "<generated-primary-password>",
  "mailbox_helper": "/root/.oldsparky/liveqa/platform_live_qa_mailbox_helper.py",
  "roster_accounts": [
    {"id": "<exact-user-id>", "email": "<marked-email>", "password": "<generated-password>"}
  ]
}
```

`roster_accounts` must contain exactly 13 unique preverified entries of the
shown shape. The top-level `email` and `password` are reserved for the later
manual auth contour; provisioning does not pre-create that account. The bundle
directory, bundle and helper must be root-owned regular non-symlink paths with
modes `0700`, `0600` and `0500` respectively; their ancestors must remain
root-controlled and not group/world writable. The mailbox boundary rejects a
bundle older than four hours or more than 60 seconds in the future. Provision
shortly before the run.

### Automated tournament/bracket contour

First run the complete public production-browser suite through the dedicated
non-root supervisor. Direct production `npm run test:live`, root Playwright,
and root Chromium are forbidden:

```bash
cd /root/old_sparky
PLATFORM_APP_DIR=/opt/oldsparky/platform \
PLATFORM_LIVE_CSP_QA_BUNDLE=/root/.oldsparky/liveqa/csp-live-qa.json \
PLAYWRIGHT_LIVE_BASE_URL=https://old-sparky.com \
platform/tools/platform_live_browser_qa.sh public
```

The supervisor consumes the already-installed, root-owned immutable runtime
under `/root/.oldsparky/liveqa/releases/<source-commit>/runtime`. The release
builder obtains the pinned official Node 26 executable, lock-installed
Playwright packages, exact browser revisions and checksum-pinned browser
archives; the runtime and payload manifests are verified again on the host.
Chromium runs as `oldsparky-liveqa` with its matching root-owned mode `4755`
SUID sandbox helper at the exact reviewed path and digest. Do not disable the
sandbox, change the global AppArmor/sysctl contour, or add
`--no-sandbox`/`--disable-setuid-sandbox`. The only AppArmor exception is the
reviewed profile installed above for the two pinned executable paths.

Release reconciliation retains only inactive generation directories after
exact active/current/previous SHA identity checks; it never removes the active
generation or a secret bundle. The guard and dispatcher recompute payload/file
digests before execution, reject symlinks and every special bit except the
exact checksum-pinned sandbox, and fail closed on an interrupted
pointer/manifest switch. Storage maintenance still owns the legacy
`/var/lib/oldsparky-liveqa/runtime-<40 lowercase hex>` fallback caches used by
the non-trusted browser contour. Preview and apply commands are owned by the
[operations runbook](operations-runbook.md#retention-and-disk-safety).

The normal workflow dispatch runs the host-installed trusted supervisor on the
server. For a manually controlled host run, invoke that same fixed path:

```bash
PLATFORM_APP_DIR=/opt/oldsparky/platform \
PLATFORM_LIVE_CSP_QA_BUNDLE=/root/.oldsparky/liveqa/csp-live-qa.json \
PLAYWRIGHT_LIVE_BASE_URL=https://old-sparky.com \
/root/.oldsparky/liveqa/platform_live_user_qa_trusted.sh
```

Before Playwright starts, the wrapper derives the marker from the bundle and
creates a root-owned `0700` temporary state directory. It seeds a `0600`
inventory with the 13 roster IDs, creates one additional disposable workflow
player, and issues exactly 14 browser sessions with a maximum one-hour TTL.
Only session-token digests enter `platformdb`; plaintext tokens exist only in a
root-owned `0600` ephemeral session file. Playwright injects the production
host-only cookie into isolated contexts and confirms each exact user identity
through the normal session endpoint before using it.

This contour covers tournament creation and joining, ready/assignment flow,
bracket progression and the associated CSP/browser gate. Ready Check uses its
server-known local timer, while passive bracket changes appear after a manual
reload. It does not exercise registration, verification, login, password reset/change or
Turnstile and therefore is not evidence for those controls. Its only auth
mutation is bounded teardown logout for the short-lived fixture sessions.

The EXIT trap deletes only the exact 14 user/session IDs and exact
tournament/media IDs recorded through atomic replacement, then proves that no
marked row remains. Marker-only bulk deletion is forbidden. On any cleanup
failure the root-only inventory and session file are retained for explicit
operator recovery, and rollout progression is blocked.

Normal runs refuse any retained `live-user-qa.*` state. Recover only the exact
path printed by the failed supervisor; do not create or edit an inventory:

```bash
PLATFORM_APP_DIR=/opt/oldsparky/platform \
PLATFORM_LIVE_CSP_QA_BUNDLE=/root/.oldsparky/liveqa/csp-live-qa.json \
platform/tools/platform_live_user_qa.sh recover \
  /root/.oldsparky/liveqa/live-user-qa.<exact-suffix>
```

Recovery uses the same per-bundle lock and validates its exact root-owned state.
If the browser committed one marker tournament without recording its ID,
recovery resolves only that row after owner/graph validation; exact cleanup must
prove absence before retained state is removed. For a stale bundle with its
13-account roster still present, use the separate [stale-bundle recovery
procedure](liveqa-stale-bundle-recovery.md); it runs only from the active
immutable QA payload under both canonical locks.

All QA commands share one machine-wide nonblocking lock. If SIGKILL interrupts
setup, remove only the retained path after validation:

```bash
platform/tools/platform_live_user_qa.sh recover-setup \
  /root/.oldsparky/liveqa/.live-user-qa.setup-<exact-id>
```

If SIGKILL interrupts the public suite, recover its non-secret `/run` gate:

```bash
platform/tools/platform_live_browser_qa.sh recover \
  /run/oldsparky-liveqa/public-live-qa.<exact-suffix>
```

### Manual auth and production Turnstile contour

Run this only after the automated cleanup has succeeded. Use an ordinary,
visible, non-WebDriver Chrome profile on a normal end-user network; do not use
CDP/remote debugging, Playwright/Selenium, Xvfb or a personal browser profile.
Create a fresh disposable unsynchronized profile, turn password saving and
autofill off, and do not open DevTools, export network data, or copy credentials
through the clipboard.
Cloudflare documents that Selenium/Playwright are detected as automation and
that production challenge outcomes are unpredictable; its dummy sitekeys are
only for testing environments and cannot constitute production evidence. See
[Turnstile testing](https://developers.cloudflare.com/turnstile/troubleshooting/testing/)
and [client-side error codes](https://developers.cloudflare.com/turnstile/troubleshooting/client-side-errors/error-codes/).

Export only these non-secret paths and prepare the manual state. Preparation
fails unless the automated fixture scope is already absent:

```bash
cd /root/old_sparky
export PLATFORM_APP_DIR=/opt/oldsparky/platform
export PLATFORM_LIVE_CSP_QA_BUNDLE=/root/.oldsparky/liveqa/csp-live-qa.json
platform/tools/platform_manual_live_auth_qa.sh prepare
platform/tools/platform_manual_live_auth_qa.sh show-email
platform/tools/platform_manual_live_auth_qa.sh show-display-name
platform/tools/platform_manual_live_auth_qa.sh show-password registration
```

The `show-*` commands reveal a value only on `/dev/tty` and refuse a non-root or
non-interactive caller. Never use command substitution or redirection and never
paste those values into a command, log, report or chat. Type them directly into
Chrome, including the exact displayed `liveqa-*` display name, complete
registration with the real production Turnstile, then obtain
and enter the verification code:

```bash
platform/tools/platform_manual_live_auth_qa.sh code email-verification
```

In Chrome, finish verification, logout, login with the registration password,
logout again and submit a password-reset request. Then obtain the reset code
and the independently generated reset password:

```bash
platform/tools/platform_manual_live_auth_qa.sh code password-reset
platform/tools/platform_manual_live_auth_qa.sh show-password reset
```

Confirm the password reset with that reset password. In the authenticated
account UI, change the password back from the reset password to the original
registration password, using `show-password reset` and `show-password
registration` again if needed, then perform the final logout. Do not copy a
Turnstile token or attempt to reuse it.

The Resend sent-copy helper uses only the fixed sent-email list/retrieve API and
its key from the root-owned production environment file, never the database,
OTP rows or application logs. It accepts only a marker-derived recipient and
exactly one message at or after the bundle timestamp. Dummy keys, a protection
bypass or retrieving OTP from the database/logs are forbidden. After final QA,
rotate the application to a `sending_access` key; do not leave a read-capable
key in the application environment for future convenience.

After the human flow, the operator tool must attest the exact audit sequence,
resolve the one manual account to its exact ID, prove the 13 automated roster
IDs remain absent, perform exact-ID session/user cleanup and prove no marker
residue remains:

```bash
platform/tools/platform_manual_live_auth_qa.sh attest-and-cleanup
unset PLATFORM_LIVE_CSP_QA_BUNDLE PLATFORM_APP_DIR
```

After the final logout and successful attested cleanup, close Chrome and delete
only that exact disposable profile directory.

If the human flow is interrupted, a Turnstile/auth step fails, or attestation
refuses the sequence, do not construct an inventory by hand. Run the bounded
recovery command, which never records a passing gate:

```bash
platform/tools/platform_manual_live_auth_qa.sh abort-and-cleanup
```

It accepts an expired but otherwise unchanged root-only state/bundle solely to
resolve zero users or the one exact derived-email user, refuses any ambiguous
tournament/participant/media scope, and confirms absence before removing the
recovery files. After an abort, provision a fresh marker before retrying.

`prepare` and `attest-and-cleanup` are the only commands that load the fixed
production environment/database contour during a passing run;
`abort-and-cleanup` loads the same contour only for recovery. The state is a
root-owned `0600` file beside the bundle and expires after two hours. For the
standard bundle its path
is `/root/.oldsparky/liveqa/csp-live-qa.manual-auth-state.json`; the recovery
inventory is
`/root/.oldsparky/liveqa/csp-live-qa.manual-auth-inventory.json`. Cleanup
publishes that exact one-user `0600` inventory only after attestation; on any
ambiguity it retains the state/inventory for recovery instead of performing
marker-wide deletion. A missing step, unexpected account/tournament/media,
failed Turnstile, audit mismatch or cleanup failure blocks progression. Use
the reviewed shell interface rather than invoking its internal Python module.

For enforcement, provision a different unique `liveqa-*` marker and repeat the
automated contour, its verified cleanup, and the manual contour. Bundle
replacement fails closed unless the previous marker and all previous exact IDs
are absent. Never reuse a stale bundle or marker. This root-run QA is not a
filesystem sandbox: use only the reviewed source commit against the exact
configured production origin. See the
[security runbook](security-runbook.md#live-csp-qa-and-mailbox-boundary) for
the full boundary.

After the enforcement live QA and its verified exact cleanup have completed,
retire the obsolete root-only bundle, manual state/inventory files and installed
mailbox helper. Remove only those exact paths after proving no disposable
user/tournament/media/session residue remains; never leave retired credentials
on disk and never commit or log their contents.

## Post-deploy gate

- Alembic current equals head;
- API, worker, web and Nginx active with no new warning-or-higher journal;
- home, auth, API, Next static, local assets and 404 security policy pass;
- automated tournament/bracket and exact cleanup pass;
- manual production Turnstile/auth lifecycle and its exact cleanup pass;
- support and changed product paths pass live checks;
- CDN/R2 assets and cache behavior remain valid;
- disk, memory, queue, DB pool and backup freshness remain in budget;
- `current` resolves to the new release and `previous` to the rollback release.

Update only the current state in the roadmap. Store detailed output in private
reports rather than documentation.

## Rollback

Install and rollback share a durable transaction record. If either command
reports a pending operation, do not retry, edit symlinks or remove the record.
Recover the inode-checked operation first from the stable shared bundle. If the
rollback pointer has already switched, `current/tools/platform_release_rollback.sh`
is a compatibility shim to the same bundle:

```bash
/opt/oldsparky/platform/shared/.release-recovery/platform_release_rollback.sh \
  --recover-pending \
  --app-dir /opt/oldsparky/platform
```

Recovery resumes only `rollback-cache-purged`; legacy `restart-pending` receipts
fail closed without purge proof. After it succeeds, rerun preflight and smoke
for the restored CSP mode before deciding
whether to retry installation or perform a normal rollback.

Preview, then apply the symlink rollback:

```bash
cd /opt/oldsparky/platform/current
tools/platform_release_rollback.sh --dry-run
tools/platform_release_rollback.sh
```

The rollback script applies by default; it has no `--apply` option and must
never be invoked with one. A CSP release rolls back to the previous enforced
application release by the normal pointer/service rollback. Both releases
keep the document policy in the Next.js proxy; rollback does not enable a
diagnostic Report-Only policy.

Validate the restored release with the rollback tool and
`--expected-csp-mode enforce`.

If a rollback ever targets a release predating nonce CSP, rerun preflight and
invoke that old release's deploy-smoke tool without `--expected-csp-mode`; that
option did not exist in those releases. Do not downgrade DB migrations
automatically; use a compatible release or a reviewed forward fix.
