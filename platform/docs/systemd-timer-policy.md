# Platform systemd timer and service policy

- Status: Active reference
- Owner: Production operator
- Last reviewed: 2026-10-01

The unit files under `platform/deploy/systemd/` are a closed 13-unit host
contract. The deterministic parser and fake-installer tests in
`tests/test_platform_systemd_timer_contract.py` validate this policy without
contacting a host systemd manager.

## Closed inventory

The eight services are explicitly split into three long-running services and
five fail-closed oneshots. The five timers are listed separately so the
release-state inventory and both installers cannot silently acquire a second
unit set.

| Unit | Shape | User/group | Restart | Target/schedule |
| --- | --- | --- | --- | --- |
| `deadlock-api.service` | long-running `simple` | `oldsparky-api/oldsparky-api` + `oldsparky-media` | `on-failure`, 5s | — |
| `deadlock-worker.service` | long-running `simple` | `oldsparky-worker/oldsparky-worker` + `oldsparky-media` | `on-failure`, 5s | — |
| `deadlock-web.service` | long-running `simple` | `oldsparky-web/oldsparky-web` | `always`, 5s | — |
| `deadlock-maintenance.service` | `oneshot` | `root/root` | none | daily 04:15 |
| `deadlock-logrotate.service` | `oneshot` | `root/root` | none | boot + every 15m |
| `deadlock-offsite-backup.service` | `oneshot` | `root/root` | none | daily 05:15 |
| `deadlock-cloudflare-ips.service` | `oneshot` | `root/root` | none | daily 02:35 |
| `deadlock-health-monitor.service` | `oneshot` | `root/root` | none | boot + every 5m |
| `deadlock-maintenance.timer` | timer | — | — | maintenance service |
| `deadlock-logrotate.timer` | timer | — | — | logrotate service |
| `deadlock-offsite-backup.timer` | timer | — | — | off-site backup service |
| `deadlock-cloudflare-ips.timer` | timer | — | — | Cloudflare service |
| `deadlock-health-monitor.timer` | timer | — | — | health service |

Long-running services have exact `ExecStart` paths, environment paths and
cgroup ceilings (`TasksMax=128`, `MemoryMax=1G`). Their service environments
are limited to the reviewed runtime/shared/env-file paths; API and worker may
write only their dedicated staging/state paths, and web may write only the
standalone Next cache. All three use the reviewed `NoNewPrivileges`, private
devices/tmp, strict system protection, invisible process view, restricted
address families/namespaces/realtime/SUID-SGID, empty capability bounding and
ambient sets, and locked personality. API additionally owns its runtime
directory and `LimitNOFILE=65535`; web retains `SuccessExitStatus=143` and its
five-start/300-second start limit.

Oneshots have exact `User`, `Group`, `Type`, `ExecStart`, environment and
resource/sandbox values. Maintenance, off-site backup and health require the
current-release symlink condition; off-site backup also requires the reviewed
backup env file. Logrotate requires the `/var/log` mount. Their failure is
visible: no restart directives, success-status overrides or ignored command
prefixes are accepted.

## Dependencies and schedules

The parser checks the complete `After`, `Wants`, `Requires`/mount and
condition sets, not merely timer `Unit=` targets: API and worker wait for
network, Redis and PostgreSQL ordering while wanting network and Redis; web
waits for network and API ordering while wanting network; maintenance wants
PostgreSQL; off-site backup waits for network and maintenance and wants
network; Cloudflare refresh waits for network and Nginx and wants network; and
health waits for network, API, worker, web and Nginx and wants network. Missing
or extra dependency/condition directives fail the exact contract.

Every timer has `Persistent=true` so a missed run is recovered, and a bounded
`RandomizedDelaySec` so independent hosts do not converge on one exact second.
The schedules are:

| Timer | Schedule | Jitter | Accuracy | Target |
| --- | --- | --- | --- | --- |
| `deadlock-cloudflare-ips.timer` | daily at 02:35 host time | 30m | 5m | `deadlock-cloudflare-ips.service` |
| `deadlock-maintenance.timer` | daily at 04:15 host time | 30m | 1m | `deadlock-maintenance.service` |
| `deadlock-offsite-backup.timer` | daily at 05:15 host time | 30m | 1m | `deadlock-offsite-backup.service` |
| `deadlock-health-monitor.timer` | boot + every 5m | 30s | 15s | `deadlock-health-monitor.service` |
| `deadlock-logrotate.timer` | boot + every 15m | 5m | 1m | `deadlock-logrotate.service` |

The parser scans every effective `ExecStart`, `ExecStartPre`, `ExecStartPost`,
`ExecStartReload`, `ExecStop`, `ExecStopPost` and `ExecCondition`, plus every
condition/assertion entry, for all systemd command-prefix combinations. An
ignored (`-`) failure is rejected unless a future change adds an exact
`(unit, directive, command)` entry to the named rationale map with a reviewed
owner; the current map is empty. Quotes, C escapes, control characters,
ambiguous whitespace and command prefixes are rejected rather than partially
decoded. The loader also holds the unit root by an `O_NOFOLLOW` directory
descriptor, checks stable pre/post identity and enumeration, and confirms a
same-fd digest. Symlinks, hard links and replacement/in-place mutation races
fail before parsing.

The worker no longer runs `platform_refresh_home_content.sh` during startup.
That hook was an ignored `ExecStartPost` failure boundary. Freshness is owned
by the worker's `home-content-refresh` beat task (bounded 30-minute cadence and
expiry), the API home cache-miss path, and the bounded patch-detail miss
refresh path. Existing worker, home-content and patch-miss tests cover those
owners; the systemd contract asserts that no startup hook remains. This keeps
worker startup independent of optional content-source availability without
making a failed refresh invisible.

## Installer and release consistency

`platform_release_systemd_state.py` owns the same exact 13-unit release-state
inventory. The normal unit installer installs all 13 units, enables only API,
worker, web and the four normal timers, and never enables off-site backup. The
maintenance installer installs its six maintenance/logrotate/off-site files
but enables only maintenance and logrotate timers. Every `systemctl` and
`journalctl` call in the maintenance installer goes through the existing
30-second bounded helper (`TERM`, five-second kill-after), so manager or
journal stalls fail visibly before later operations. Neither installer runs
real host commands in the fake/temp-file contract tests.

Off-site backup remains installed but disabled until the manual encrypted
backup recovery drill in the [backup runbook](backup-restore-runbook.md).
Watchdogs, `Type=notify` and runtime probes are intentionally outside this
Phase A contract.
