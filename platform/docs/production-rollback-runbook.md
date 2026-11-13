# Platform production rollback runbook

- Status: Active how-to
- Owner: Production operator
- Last reviewed: 2026-11-13

Use this procedure for an explicitly authorized production application rollback.
It switches the application release pointers to the exact verified previous
release; it does **not** automatically downgrade Alembic. Review database
compatibility first. Never bypass a missing or mismatched rollback receipt,
dependency-freeze proof, lock or target identity.

## Host identification and private evidence

When the shell may already be on the production host, identify it before
looking for SSH credentials: a missing SSH agent does not mean the host is
remote or inaccessible. Start one Bash shell and create a private durable
report directory for preflight, rollback and post-rollback smoke evidence:

```bash
umask 077
install -d -m 0700 /root/.oldsparky/reports
REPORT_DIR="$(mktemp -d -p /root/.oldsparky/reports rollback.XXXXXXXXXX)"
chmod 0700 "$REPORT_DIR"
run_private() {
  label=$1
  shift
  if "$@" >"$REPORT_DIR/$label.log" 2>&1; then
    command_rc=0
  else
    command_rc=$?
  fi
  printf '%s\n' "$command_rc" >"$REPORT_DIR/$label.exit"
  cat "$REPORT_DIR/$label.log"
  printf 'exit=%s\n' "$command_rc"
  return "$command_rc"
}
```

Keep all command blocks in this same Bash shell so `REPORT_DIR` and
`run_private()` remain defined. If starting in a new shell or directly at a
later section, rerun this complete setup block first. It records stdout,
stderr and each command's actual exit status in mode-`0600` files under a
mode-`0700` directory. Never store these logs in the repository or include
credentials, cookies, private reports or live user data.

Run these read-only identity checks:

```bash
id -u
hostname -f
stat -c '%U %G %a %n' /opt/oldsparky/platform
readlink -e /opt/oldsparky/platform/current
readlink -e /opt/oldsparky/platform/previous
systemctl is-active deadlock-api.service deadlock-worker.service deadlock-web.service
```

Proceed locally only when the checks identify the root shell on the production
host, the root-owned application directory and the expected release pointers.
If the shell is not on the production host, do not infer an SSH host, alias or
identity from the public domain or a missing local SSH agent. Stop direct host
rollback until an established production access path is available. If the
goal is only to keep a feature out of a future release, use the reviewed
source-control revert described below; it does not switch the current
production runtime. `old-sparky.com` is a Cloudflare public edge, not an SSH
target.

The rollback helper's dry run is the authoritative, lock-protected check of
current/previous release identity, transaction state and rollback eligibility;
a separate lock observation is not a substitute for it.

## Preflight and smoke

Run the strict preflight before rollback. A failed preflight is not a pass.
Inspect its bounded structured result and private log; stop on any missing
backup, edge parity, release identity, lock, service, or other failed
predicate. The rollback helper dry run also validates its own locked
transaction and exact release identities.

The active Alembic head check may be deferred only when the restored release's
head is older than the database solely because a later migration is additive
and compatible with that exact release. Review the exact migration and release
compatibility first: it must add only new data structures/data and leave every
existing structure used by the restored release intact. The defer option
skips only the active `current == heads` assertion; it does not bypass backup
freshness, edge parity, pointer, lock, health or smoke checks. If any other
predicate fails, or the schema change is destructive, incompatible or unclear,
stop and do not defer. Never downgrade Alembic or drop data during rollback.

For that narrowly reviewed additive-compatible case only, run:

```bash
cd /opt/oldsparky/platform/current
run_private preflight-reviewed-additive tools/platform_release_preflight.sh \
  --require-previous --require-verified-backup --require-edge-parity \
  --backup-max-age-hours 24 --defer-active-alembic-revision-check || exit 1
```

For all other cases, omit the defer option:

```bash
cd /opt/oldsparky/platform/current
run_private preflight tools/platform_release_preflight.sh \
  --require-previous --require-verified-backup --require-edge-parity \
  --backup-max-age-hours 24 || exit 1
```

After either successful preflight, run both origin/SNI and public-edge smoke
checks. The expected CSP mode must match the active release.

```bash
cd /opt/oldsparky/platform/current
run_private origin-smoke /opt/oldsparky/platform/shared/venv/bin/python -B \
  tools/platform_deploy_smoke.py \
  --edge-origin https://127.0.0.1 \
  --edge-host old-sparky.com \
  --edge-insecure-loopback \
  --expected-csp-mode enforce || exit 1

run_private public-smoke /opt/oldsparky/platform/shared/venv/bin/python -B \
  tools/platform_deploy_smoke.py \
  --edge-origin https://old-sparky.com \
  --expected-csp-mode enforce || exit 1
```

`--edge-insecure-loopback` is allowed only for loopback. Public smoke keeps
normal certificate verification. Treat the loopback and public-edge results
as independent; loopback health does not establish public-edge success.

## Rollback

Confirm the exact `current` and `previous` release identities against the
failed release and intended target. The helper path below resolves through
`current` before the pointer changes; do not select a rollback helper through
the newly restored `current` afterward.

Preview first. The dry run must exit successfully and report
`RELEASE_ROLLBACK status=review class=rollback` for the intended exact previous
release. Review its identities and lock/transaction checks. Any mismatch,
refusal, unexpected class, pending operation or nonzero exit aborts; retain
receipts and logs and use the documented recovery workflow for an interrupted
transaction.

```bash
cd /opt/oldsparky/platform/current
run_private dry-run tools/platform_release_rollback.sh --dry-run || exit 1
```

After reviewing the dry run, database compatibility and preflight, apply by
invoking the same guarded helper with no arguments. Apply is the default; there
is no `--apply` option. Require exit status 0, `RELEASE_RUNTIME
status=restored` with the expected target identity, and `RELEASE_ROLLBACK
status=passed`. Otherwise stop and retain receipts and private logs; do not
retry blindly, edit pointers, clear receipts, invoke an installer or run
systemd commands by hand.

```bash
cd /opt/oldsparky/platform/current
run_private apply tools/platform_release_rollback.sh || exit 1
```

The rollback tool prepares a root-owned shared recovery bundle and a
compatibility handoff in the previous release before switching `current`, so
recovery remains available if the process dies after the pointer switch.

## Interrupted rollback

If an operation-ID rollback is interrupted, use the exact content-addressed
generation selected and verified by the release-recover workflow. Invoke its
immutable wrapper directly; do not use `current/tools` or a release rollback
shim as recovery control code:

```bash
/opt/oldsparky/platform/shared/.release-recovery/generations/<bundle-sha256>/platform_recover_pending.sh \
  --app-dir /opt/oldsparky/platform
```

The wrapper resumes the recorded rollback phase, validates the operation-ID
and systemd receipt pair before systemd or runtime work, and completes receipt
cleanup in two phases. A crash after receipt clear is finished from the
transaction phase without querying systemd. The legacy shim remains only for
normal rollback handoff compatibility and is not an operation-ID recovery
authority. Recovery resumes only `rollback-cache-purged`; legacy
`restart-pending` receipts fail closed without purge proof.

## After rollback

Repeat the preflight and both smoke checks above, using `run_private` and
distinct labels such as `post-rollback-preflight`,
`post-rollback-origin-smoke` and `post-rollback-public-smoke`. Require loopback
API readiness and web readiness, then configured-origin/SNI and public-edge
smoke success for the restored release's CSP mode and expected site, API,
static and security behavior.

Runtime rollback does not revert the source branch. Remove the rolled-back
feature from the release source separately with a reviewed source-control
revert (or a corrective commit if the feature is mixed with unrelated work),
then push that reviewed change to `dev`. Require the exact-SHA Security gate
and automatic production chain for that source change. Do not make a direct
production install to keep the feature out of the next release.
