# Platform operations runbook

- Status: Active how-to and reference
- Owner: Production operator
- Last reviewed: 2026-08-23

## Runtime checks

```bash
systemctl is-active deadlock-api deadlock-worker deadlock-web nginx
systemctl --failed
journalctl -u deadlock-api -u deadlock-worker -u deadlock-web \
  --since -1h -p warning --no-pager
df -h /
```

API, web, PostgreSQL and Redis bind loopback; Nginx is the only origin listener.
The platform connects directly to PostgreSQL. The retired legacy Telegram bot,
its `sparkydb` database and host PgBouncer are absent and must not become
platform dependencies.

`deadlock-health-monitor.timer` performs the lightweight five-minute readiness,
service, disk, memory, backup-age and certificate-expiry gate.
`deadlock-maintenance.timer` runs the daily restore-verified backup and bounded
retention workflow.

The read-only edge proof is:

```bash
cd /opt/oldsparky/platform/current
/opt/oldsparky/platform/shared/venv/bin/python \
  tools/platform_validate_edge_policy.py --json
```

It compares the current Cloudflare ranges with both the Nginx real-IP include
and managed UFW rules. It does not prove that a direct-origin request is
blocked; perform that negative test separately from an approved external
network. A release preflight with `--require-edge-parity` fails closed when the
range proof is unavailable or mismatched.

## Retention and disk safety

Maintenance keeps:

| Data | Policy |
| --- | --- |
| production releases | newest 5; always protect `current` and `previous` |
| local release artifacts | newest 5 matching release groups |
| verified DB backups | newest 14 |
| Playwright results | 7 days |
| live-QA runtime caches | protect current/previous source commits; keep exactly the newest additional fallback |
| maintenance reports | newest 30 |
| systemd journal | 30 days, 512 MiB, preserve 5 GiB free |

It never deletes shared env/runtimes, upload staging, current/previous releases,
live-QA caches for the current/previous source commits, business rows or
canonical retained reports. Backup failure stops pruning. Live-QA cache pruning
also takes the machine-wide live-QA lock, requires the dedicated browser cgroup
and user identity to be idle, accepts only root-owned non-symlink
`runtime-<40 lowercase hex>` trees with the published read-only manifest
contract, and always retains at least the newest valid unprotected fallback.
The keep count is a hard cap for unprotected caches, not an age window.
Maintenance fails below 5 GiB free or above 85% disk use.

Apply mode holds locks in the fixed order: platform release operation, source
build output, then live-QA machine lock. This keeps release pointers stable
through deletion and avoids deadlocks with install, rollback, build and browser
workflows. The systemd unit retains its single storage-maintenance command, so
rolling `current` back never invokes a guard subcommand missing from that
release.

Retention intentionally does not recompute each multi-gigabyte tree's content
digest. Destructive eligibility instead requires the exact 40-hex name,
matching immutable manifest identity, root ownership, expected read-only modes,
same-device regular file/directory types and non-escaping symlinks. Runtime
reuse still recomputes and compares the full tree digest before execution.
Deletion first renames a revalidated cache to a strict hidden tombstone; the
next maintenance run validates and reclaims a tombstone left by interruption.

```bash
cd /opt/oldsparky/platform/current
tools/platform_install_maintenance.sh
systemctl start deadlock-maintenance.service
systemctl status deadlock-maintenance.timer --no-pager
journalctl -u deadlock-maintenance.service --since -2days --no-pager
```

Preview storage cleanup without backup or deletion:

```bash
/opt/oldsparky/platform/shared/venv/bin/python \
  /opt/oldsparky/platform/current/tools/platform_storage_maintenance.py
```

Preview live-QA runtime cache retention (also machine-locked and idle-gated):

```bash
/usr/bin/python3 -I \
  /opt/oldsparky/platform/current/tools/platform_live_qa_guard.py \
  prune-runtime-cache --keep 1
```

The subcommand is dry-run unless `--apply` is explicit and takes the release
lock before the live-QA lock. Daily storage maintenance runs the nested apply
form only after its restore-verified backup step succeeds.

## Prepared media and R2

- Public bucket `oldsparky` contains decoded/re-encoded immutable variants only.
- PostgreSQL owns keys, dimensions, MIME type, size and SHA-256.
- Browser reads use `https://cdn.old-sparky.com`; page/API serialization makes
  no S3 reads.
- Source staging is private, bounded and removed after processing.
- Failed replacement removes only new partial keys and retains the old asset.
- Reconciliation uses bounded DB rows; never begin with a full bucket scan.

Read-only connectivity check:

```bash
cd /opt/oldsparky/platform/current
tools/platform_run_quiet.sh "R2 connectivity" -- \
  /opt/oldsparky/platform/shared/venv/bin/python tools/platform_r2_smoke.py \
  --env-file /opt/oldsparky/platform/shared/.env.platform
```

Use `--apply --json` only in a planned mutation window. CDN diagnosis uses
`tools/platform_check_cdn.py <prepared-url> --json`. Legacy-media migration is
dry-run by default; apply bounded batches only after a fresh verified backup,
then verify by DB row and targeted HeadObject/CDN requests. Deletion requires a
grace period and explicit approval.

## SSE connection pressure

Public bracket SSE has layered application and Nginx admission limits; the stable capacity values belong in [`production-architecture.md`](production-architecture.md). Application rejections are emitted as HTTP 429 with a bounded retry hint, limiter-backend failure is fail-closed as HTTP 503, and Nginx connection-limit rejection is also HTTP 429.

Use the application journal and structured Nginx access log to distinguish ordinary reconnects from sustained pressure:

```bash
journalctl -u deadlock-api --since -1h --no-pager \
  | grep -E 'Rejected SSE connection|Failed to release SSE connection lease|limiter backend is unavailable'

grep '/bracket/events' /var/log/nginx/platform-access.log \
  | grep '"status":429' \
  | tail -n 50
```

The application logs a privacy-preserving source fingerprint rather than the raw source address for admission rejections. A failed immediate lease release is not by itself a leak: bounded expiry reclaims the slot, but repeated release failures indicate Redis/runtime trouble and require investigation. Do not raise SSE ceilings to suppress 429s without correlating them with legitimate concurrency and VPS/API/Redis resource evidence.

## Performance

Targets under normal non-saturated load:

- ordinary reads/small mutations: p95 below 500 ms;
- retained workflow load: p95 below 1000 ms;
- browser INP at or below 200 ms;
- public/detail LCP at or below 2.5 s.

Measure one user step before tuning. Capture response bytes, p50/p95/p99,
SQL/request, DB and compute time, CPU/RAM/load, connections, locks, cache state,
queue wait and worker lifecycle. Use
`tools/platform_production_qa.py --collect-performance` and
`tools/platform_performance_audit.py`; detailed JSON stays under shared storage.

Do not increase Gunicorn/Celery workers, add PgBouncer, install exporters or
introduce a cache/schema rewrite without retained evidence. If both VPS cores
remain saturated after unnecessary work is removed, classify a capacity limit
instead of hiding it with more local workers.

## Alert thresholds

- failed/restarted service;
- disk below 5 GiB free or above 85%;
- newest backup older than 24 hours or not restore-verified;
- sustained Celery queue growth or retry exhaustion;
- repeated 5xx/security delivery errors;
- sustained SSE 429/503 outside expected abusive traffic, especially with API/Redis resource pressure;
- p95 breach correlated with CPU, DB wait or lock evidence;
- Origin CA expiry inside the monitor threshold.
