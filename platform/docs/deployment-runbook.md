# Platform deployment runbook

- Status: Active how-to
- Owner: Production operator
- Last reviewed: 2026-08-16

Use this document for the normal immutable release path. CSP mode changes and production browser/live-user evidence are intentionally isolated in [`csp-live-qa-runbook.md`](csp-live-qa-runbook.md); do not load that document for routine releases.

## Preconditions

1. Work from a clean, reviewed commit; release metadata records `HEAD`.
2. Run the focused verification required by the changed owners.
3. Confirm migration expand/rollback compatibility.
4. Confirm services are healthy, disk has at least 5 GiB free and is below 85%, and `current`/`previous` releases are protected.
5. Create a fresh restore-verified backup.

## Build

```bash
cd /root/old_sparky
platform/tools/platform_build_release.sh <release-slug>
(cd platform/dist/releases && sha256sum -c <release-slug>.tar.gz.sha256)
```

Record the artifact path, SHA-256 and source commit from `RELEASE.json`. Release builds use the pinned Python/Node dependency inputs and must fail closed on lock/freeze drift.

## Preflight, install and activate

```bash
cd /opt/oldsparky/platform/current
tools/platform_release_preflight.sh \
  --require-previous --require-verified-backup --backup-max-age-hours 24

cd /root/old_sparky
platform/tools/platform_release_install.sh \
  platform/dist/releases/<release-slug>.tar.gz \
  /opt/oldsparky/platform

cd /opt/oldsparky/platform/current
tools/platform_run_alembic.sh upgrade head
tools/platform_install_systemd_units.sh
/opt/oldsparky/platform/shared/venv/bin/python \
  tools/platform_install_nginx.py --json
systemctl restart deadlock-api deadlock-worker deadlock-web
/opt/oldsparky/platform/shared/venv/bin/python \
  tools/platform_install_nginx.py --apply --reload --json
```

The systemd installer prepares service-owned runtime paths before restart. Never restart `deadlock-web` against a new release before that preparation succeeds. Do not print service environments or secrets.

## Smoke

```bash
cd /opt/oldsparky/platform/current

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

`--edge-insecure-loopback` is allowed only for loopback. Public smoke keeps normal certificate verification. The expected CSP mode must match the active release.

## Nginx-only changes

```bash
cd /opt/oldsparky/platform/current
/opt/oldsparky/platform/shared/venv/bin/python \
  tools/platform_install_nginx.py --json
/opt/oldsparky/platform/shared/venv/bin/python \
  tools/platform_install_nginx.py --apply --reload --json
```

Dry-run is the default. Apply only after policy validation and `nginx -t` succeed.

## Rollback

Use the repository rollback tooling and the exact previous immutable release. Rollback switches application release pointers; it does **not** automatically downgrade Alembic. Do not bypass a missing or mismatched rollback receipt or dependency-freeze proof.

After rollback, repeat preflight plus origin/SNI and public smoke against the restored release.

## Special release contours

Open only when applicable:

- CSP candidate/enforcement changes, live browser QA, Turnstile/auth contour, AppArmor Chromium sandbox and CSP observation gates: [`csp-live-qa-runbook.md`](csp-live-qa-runbook.md).
- The live public browser workflow is owned by [`test-suite-governance.md`](test-suite-governance.md) and must invoke the dedicated server wrapper over SSH. Do not run production Playwright on the GitHub runner.
- Backup/restore: [`backup-restore-runbook.md`](backup-restore-runbook.md).
- Security policy and CSP ownership: [`security-runbook.md`](security-runbook.md).
- Incident response: [`incident-response.md`](incident-response.md).
