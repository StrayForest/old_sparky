# Stale LiveQA bundle recovery

- Status: Exceptional operator procedure
- Owner: Production operator
- Last reviewed: 2026-10-20

Use this procedure only when the fixed CSP LiveQA bundle is older than its
normal four-hour QA validity window and its exact 13-account roster remains.
It performs exact cleanup through the reviewed active immutable LiveQA
payload. It does not launch a browser, provision a bundle, change an application
release or bypass the usual source/runtime checks.

## Preconditions

First confirm the active application release SHA `P`, the active LiveQA payload
SHA `P`, the fixed bundle `/root/.oldsparky/liveqa/csp-live-qa.json`, and no
retained automated/manual recovery state or `/run` LiveQA gate. The helper
rechecks these identities under lock. The bundle timestamp must use the normal
UTC format and be more than four hours old; timestamps more than 60 seconds in
the future or within the normal validity window are refused. The bundle's
installed mailbox helper must still match the active payload.

Run as root, substituting the exact lowercase 40-character active app source
SHA for `P`:

```bash
P=<exact-active-application-source-sha>
PLATFORM_APP_DIR=/opt/oldsparky/platform \
PLATFORM_LIVE_CSP_QA_BUNDLE=/root/.oldsparky/liveqa/csp-live-qa.json \
PLATFORM_LIVE_QA_TARGET_SHA="$P" \
PLATFORM_LIVE_QA_INSTALL_ROOT="/root/.oldsparky/liveqa/releases/$P" \
/opt/oldsparky/platform/current/tools/platform_release_lock_exec.sh \
  --expected-sha "$P" -- \
  "/root/.oldsparky/liveqa/releases/$P/platform/tools/platform_live_user_qa.sh" \
  recover-bundle
```

The release-lock wrapper verifies the active application SHA and refuses a
pending release transaction. The immutable payload then verifies its own
manifest/runtime and fixed production origin before entering the recovery
executor. That executor confirms the live release-lock supervisor, acquires the
machine-wide LiveQA lock, and only then performs stale-runner recovery. The
trusted `platform_live_user_qa_trusted.sh` dispatcher intentionally does not
forward `recover-bundle`; do not invoke the payload wrapper without the
canonical release-lock command shown above.

## Cleanup and refusal behavior

The mode derives its root-only cleanup inventory from the bundle's exact 13
roster IDs. It refuses any existing recovery/setup state, runtime gate, active
QA operation, malformed bundle/roster, mismatched active payload/helper, or
remaining data outside the existing cleanup contract. It creates no browser
session and does not create a tournament inventory. Existing exact cleanup
validates data relationships and proves absence before removing its inventory.

On any cleanup refusal or failure, keep the generated exact state path printed
by the guard. Continue only with the ordinary `platform_live_user_qa.sh
recover <exact-state-path>` procedure in the [CSP/live QA runbook](csp-live-qa-runbook.md).
Do not edit the inventory or delete roster rows by marker.
