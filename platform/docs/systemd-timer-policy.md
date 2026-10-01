# Platform systemd timer policy

- Status: Active reference
- Owner: Production operator
- Last reviewed: 2026-10-01

The unit files under `platform/deploy/systemd/` are a closed host contract.
The deterministic parser and fake-installer tests in
`tests/test_platform_systemd_timer_contract.py` validate this policy without
contacting a host systemd manager.

| Timer | Schedule | Jitter | Accuracy | Catch-up | Target |
| --- | --- | --- | --- | --- | --- |
| `deadlock-cloudflare-ips.timer` | daily at 02:35 host time | 30 minutes | 5 minutes | persistent | `deadlock-cloudflare-ips.service` |
| `deadlock-maintenance.timer` | daily at 04:15 host time | 30 minutes | 1 minute | persistent | `deadlock-maintenance.service` |
| `deadlock-offsite-backup.timer` | daily at 05:15 host time | 30 minutes | 1 minute | persistent | `deadlock-offsite-backup.service` |
| `deadlock-health-monitor.timer` | boot + every 5 minutes | 30 seconds | 15 seconds | persistent | `deadlock-health-monitor.service` |
| `deadlock-logrotate.timer` | boot + every 15 minutes | 5 minutes | 1 minute | persistent | `deadlock-logrotate.service` |

Every timer has `Persistent=true` so a missed run is recovered, and a bounded
`RandomizedDelaySec` so independent hosts do not converge on one exact second.
Each timer target is a fail-closed `Type=oneshot` service: backup,
maintenance, logrotate, Cloudflare refresh and health failures remain visible
and are not hidden by an automatic service restart. The API and worker use
`Restart=on-failure`; the web service uses its explicit bounded
`Restart=always` policy and graceful-stop status.

The oneshot `Service` sections use an exact reviewed directive allow-list. They
cannot add `SuccessExitStatus`, restart modifiers, or ignored (`-`) prefixes on
the effective `ExecStart` list, `ExecStartPre`, `ExecStartPost` or
`ExecCondition`; an empty `ExecStart=` resets earlier entries, so the final
effective list must still be non-empty and fail closed. Each effective oneshot
command uses the closed canonical form: an absolute executable, single-space
tokenization, and a restricted argument alphabet. Quotes, backslashes/C
escapes, control characters, ambiguous whitespace and command prefixes are
rejected rather than partially decoded. Condition and assertion entries also
cannot hide a failure. The contract loader holds the unit root by an
`O_NOFOLLOW` directory descriptor, requires stable pre/post identity and
enumeration, and opens each unit relative to that descriptor. Each unit must be
a regular, owner/group-matched `0644` file with one hard link; descriptor
`fstat` identity includes `mtime_ns` and `ctime_ns`, plus a same-fd digest
confirmation. Symlinks, hard links, and replacement or in-place mutation races
are rejected before parsing.

The normal unit installer enables the API, worker and web services plus the
Cloudflare, health, maintenance and logrotate timers. The maintenance
installer enables only the maintenance and logrotate timers. Both installers
install the off-site unit files but leave `deadlock-offsite-backup.timer`
disabled. An operator may enable it only after the manual encrypted-backup
recovery drill in the [backup runbook](backup-restore-runbook.md).
