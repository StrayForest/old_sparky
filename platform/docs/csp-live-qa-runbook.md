# Platform CSP and live QA runbook

- Status: Active how-to
- Owner: Production operator
- Last reviewed: 2026-09-01

This is the special CSP/browser/live-user QA path. Normal production release,
rollback and release-transaction recovery are owned by
[`deployment-runbook.md`](deployment-runbook.md).

## Preconditions

1. Commit the reviewed release scope and preserve unrelated work. Release
   metadata records `HEAD`; never build an artifact from dirty release sources.
2. Run focused checks and all gates required by the changed owners.
3. Confirm migration expand/rollback compatibility.
4. Confirm services healthy, disk at least 5 GiB free/below 85%, and protected
   `current`/`previous` releases.
5. Create a fresh restore-verified backup.

## Build

```bash
cd /root/old_sparky
platform/tools/platform_build_release.sh <release-slug>
```

The Report-Only candidate omits a dependency baseline. The separate
enforcement build must name the accepted candidate release directory by its
absolute path:

```bash
cd /root/old_sparky
platform/tools/platform_build_release.sh \
  --dependency-baseline \
  /root/old_sparky/platform/dist/releases/<candidate-release-slug> \
  <enforcement-release-ref>
```

That gate requires byte-identical direct requirements, the tracked hash-locked
Python lock, generated freeze, wheelhouse manifest and web package lock.
Omission of the flag or any drift is a failed enforcement build, because the
only accepted candidate-to-final runtime delta is the reviewed CSP header-mode
line. Every build installs the hash-locked `requirements-platform.lock.txt`,
regenerates the sorted freeze and refuses any package-pin difference before
packaging. For this CSP candidate the tracked lock's package pins must also
equal the accepted active production freeze; dependency updates require their
own reviewed release. Release builds require the exact
reviewed Node `26.3.1` and npm `11.16.0` toolchain recorded in `RELEASE.json`.

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
  --expected-csp-mode report-only
/opt/oldsparky/platform/shared/venv/bin/python \
  tools/platform_deploy_smoke.py \
  --edge-origin https://old-sparky.com \
  --expected-csp-mode report-only
```

Do not print the shared environment. Use `--skip-python-deps` only after
dependency compatibility has been verified against the existing shared venv.
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
Pass `--expected-csp-mode enforce` for an enforcement release. For the initial
CSP ownership transfer, restart the nonce-capable web process and immediately
apply/reload the validated CSP-free Nginx pair; do not run smoke in the brief
intermediate state where both layers still emit Report-Only CSP.

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
  --expected-csp-mode report-only
```

`--edge-insecure-loopback` is forbidden for non-loopback origins. Public smoke
keeps normal certificate verification. The CSP mode is mandatory and must
match the candidate (`report-only`) or final release (`enforce`).
The loopback/SNI smoke requires ready health `200`; the public smoke requires
the intentionally private health endpoint to return `403` with the shared
non-document security headers.

## CSP two-release maintenance window

The standard sequence below remains the required default for future CSP mode
changes. The 2026-08-13 activation was an explicit owner exception: enforcement
was activated after the owner waived manual auth/Turnstile, repeated
enforcement browser and 30-minute/24-hour observation evidence. Do not
reuse that exception implicitly or record the waived gates as passed.

1. Build and activate an immutable fail-closed candidate using the normal
   release flow above and `--expected-csp-mode report-only` for both origin/SNI
   and public smoke.
2. Run all release/security gates, both live-user contours below and the
   complete applicable QA/performance contour. A passing deploy smoke or only
   one live-user contour is not the candidate gate.
3. After synthetic probes finish, observe for at least 30 clean minutes. There
   must be no unexplained first-party CSP report, missing/duplicate/wrong-mode
   header, nonce/cache failure, new 5xx/service warning, queue/DB pressure or
   accepted-performance regression. Classify browser-extension noise without
   weakening the allowlist.
4. Create a separate immutable release whose reviewed source delta is the
   one-line response-header selection from Report-Only to enforcement. Build
   it with `--dependency-baseline` pointing at the accepted candidate release
   directory as shown above; dependency drift or an omitted baseline blocks
   progression. Do not edit the candidate artifact in place. Activate it and
   repeat origin/SNI and public smoke with `--expected-csp-mode enforce`, then
   repeat both live-user contours with a fresh marker.
5. Keep the Report-Only candidate as `previous` and observe enforcement for 24
   hours. Follow CSP telemetry, critical browser journeys, health/journals,
   queue/DB/system pressure and the accepted performance contour. Record
   rollout evidence in private reports and update current state only after it
   exists.

Any wrong CSP mode, unexpected directive/source, reused or short nonce,
nonce-bearing cached HTML, live-user cleanup failure or unexplained first-party
violation stops progression. Never add `unsafe-inline`, `unsafe-eval` or an
origin just to make a report disappear. The exact policy, ownership and report
limiter are in the [security runbook](security-runbook.md).

## CSP live-user gates

Candidate and enforcement each require two independent contours, in this
order:

1. an automated tournament/roster/bracket journey using short-lived QA
   sessions and no authentication route or Turnstile;
2. after its exact-ID cleanup succeeds, a human auth lifecycle and production
   Turnstile check in ordinary Chrome.

Neither contour substitutes for the other. Use a fresh `liveqa-*` marker for
each release. Do not run the contours concurrently or start the manual contour
until the automated inventory, users, sessions, tournament and media have all
been confirmed absent.

The automated journey runs from the source checkout because release artifacts
intentionally exclude tests and `node_modules`. The checkout must be the
reviewed source for the release being exercised; the supervisor installs exact
dependencies from the tracked lockfile into its immutable cache. Run as root
and first create the root-only bundle through the public shell wrapper:

```bash
cd /root/old_sparky
install -d -o root -g root -m 0700 /root/.oldsparky/liveqa
platform/tools/platform_install_live_qa_user.sh --apply
install -o root -g root -m 0500 \
  platform/tools/platform_live_qa_mailbox_helper.py \
  /root/.oldsparky/liveqa/platform_live_qa_mailbox_helper.py
PLATFORM_APP_DIR=/opt/oldsparky/platform \
platform/tools/platform_provision_live_csp_qa.sh \
  --marker liveqa-csp-candidate-<unique> \
  --bundle-path /root/.oldsparky/liveqa/csp-live-qa.json \
  --primary-email 'REPLACE_WITH_PRIMARY_LIVE_QA_EMAIL' \
  --mailbox-helper /root/.oldsparky/liveqa/platform_live_qa_mailbox_helper.py
```

For an existing bundle, both automated live workflows refresh the installed
mailbox helper from the exact-SHA trusted checkout while holding the machine
release lock, before starting the browser supervisor. The provenance guard then
checks the helper digest and `0500` root-only mode. This repairs a stale helper
without replacing the bundle or credentials; never bypass the guard or copy a
helper from an unrelated checkout.

The canonical automated journey dispatch is:

```bash
gh workflow run platform-live-user-qa.yml \
  --ref dev \
  -f confirmation=RUN-LIVE-USER-QA
```

Dispatch only after the exact deployed `dev` SHA is confirmed. The workflow
creates no fixture until the release lock, checkout, helper, bundle and browser
preflight checks pass.

The dedicated `oldsparky-liveqa` system account has `/nonexistent` as its home,
`/usr/sbin/nologin` as its shell, its same-named non-root primary group and no
supplementary groups. Never substitute `oldsparky` or `oldsparky-platform`:
those identities own deployment or production-runtime paths. The wrappers
also require a clean `platform/` checkout whose `HEAD` exactly matches the
active `current/RELEASE.json` source commit, and require the installed helper's
SHA-256 to match the reviewed helper in that checkout.

The same installer loads two named AppArmor profiles granting `userns` only to
the checksum-pinned Chromium and headless-shell revision paths below the
root-owned immutable runtime cache. It validates the profile before loading it
and requires the installed root-owned `0644` copy to match the reviewed source.
Keep `kernel.apparmor_restrict_unprivileged_userns=1` and
`kernel.unprivileged_userns_clone=1`; automated preflight refuses a disabled
global restriction or either missing named profile. Never replace this narrow
profile with a global sysctl change.

`platform_provision_live_csp_qa.py` is the wrapper's internal implementation;
do not invoke it directly or print/load the production environment in the
interactive shell. The provisioner writes only to `platformdb`, schema
`platform`, and success output contains only the marker, bundle path and account
count. It never prints generated credentials.

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

On the first run for a reviewed commit, the supervisor builds a root-owned,
read-only cache under `/var/lib/oldsparky-liveqa/runtime-<source-commit>`. It
uses the pinned official Node 26 archive checksum, `npm ci` from the tracked
package lock, the exact Playwright browser revision, and a content manifest;
drift is refused. Chromium runs as `oldsparky-liveqa` with its matching
root-owned mode `4755` SUID sandbox helper. Do not disable the sandbox, change
the global AppArmor/sysctl contour, or add
`--no-sandbox`/`--disable-setuid-sandbox`. The only AppArmor exception is the
reviewed profile installed above for the two pinned executable paths.

Daily platform maintenance prunes only strict
`/var/lib/oldsparky-liveqa/runtime-<40 lowercase hex>` cache directories,
protecting the live `current` and `previous` source commits and keeping exactly
the newest additional fallback. Storage maintenance holds the release and
source-build locks before taking the same machine-wide lock as live QA. The
guard refuses a non-idle QA identity/cgroup and validates root ownership plus
the read-only cache/manifest/type/device/symlink contract. It intentionally
does not recompute the multi-gigabyte content digest during retention; the
runtime reuse path still verifies that digest before execution. Deletion uses
a validated hidden tombstone so the next maintenance run can reclaim an
interrupted deletion. The standalone command also takes the release lock and is
dry-run unless `--apply` is supplied. Preview and apply commands are owned by the
[operations runbook](operations-runbook.md#retention-and-disk-safety).

Bind the source checkout to the production app directory and run the journey
on the server:

```bash
cd /root/old_sparky
PLATFORM_APP_DIR=/opt/oldsparky/platform \
PLATFORM_LIVE_CSP_QA_BUNDLE=/root/.oldsparky/liveqa/csp-live-qa.json \
PLAYWRIGHT_LIVE_BASE_URL=https://old-sparky.com \
platform/tools/platform_live_user_qa.sh
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

Recovery takes the same per-bundle lock, validates the root-owned `0700`
directory and `0600` inventory/session files, and creates no replacement
state. If the browser committed its one marker-description tournament before
recording the ID, recovery resolves at most that one exact row, validates its
owner and complete graph, atomically appends the exact ID, then invokes normal
exact cleanup. It unlinks retained files only after the cleanup absence proof.

All provision, public-browser, automated, manual and recovery commands share
one machine-wide nonblocking lock. If SIGKILL interrupts the atomic state setup
before publication, remove only the retained path after validation:

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

Recovery is idempotent and completes any recorded restart-pending phase. After
it succeeds, rerun preflight and smoke for the restored CSP mode before deciding
whether to retry installation or perform a normal rollback.

Preview, then apply the symlink rollback:

```bash
cd /opt/oldsparky/platform/current
tools/platform_release_rollback.sh --dry-run
tools/platform_release_rollback.sh
```

The rollback script applies by default; it has no `--apply` option and must
never be invoked with one. A final CSP release rolls back to the Report-Only
candidate by symlink/service rollback only because both releases use the same
CSP-free Nginx pair. Validate that rollback with the candidate tool and
`--expected-csp-mode report-only`.

When rolling the initial CSP candidate back to the pre-change release, restore
the previous release's Nginx vhost/snippet pair first, then switch the app:

```bash
cd /opt/oldsparky/platform/current
tools/platform_release_rollback.sh --dry-run
/opt/oldsparky/platform/shared/venv/bin/python \
  /opt/oldsparky/platform/previous/tools/platform_install_nginx.py \
  --apply --reload --json
tools/platform_release_rollback.sh
```

Here `--apply` belongs only to the previous release's Nginx installer; the
release rollback command has no such option. Installing the previous Nginx
pair before switching the application deliberately prefers a brief duplicate
Report-Only policy over a gap with no CSP owner.

After a candidate-to-pre-change rollback, rerun preflight and invoke that old
release's deploy-smoke tool without `--expected-csp-mode`; that option did not
exist before nonce CSP. Do not downgrade DB migrations automatically; use a
compatible release or a reviewed forward fix.
