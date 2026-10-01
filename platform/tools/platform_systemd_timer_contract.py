#!/usr/bin/env python3
"""Static systemd unit/timer contract for the platform host.

The production host intentionally does not run ``systemd-analyze`` during a
release.  This small parser keeps the unit graph and schedule policy in the
repository-owned deterministic test contour instead.  It reads only the
tracked unit files and never talks to a systemd manager.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Iterable, Mapping


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_ROOT = PLATFORM_ROOT / "deploy" / "systemd"


class SystemdContractError(ValueError):
    """Raised when a unit file cannot satisfy the repository contract."""


@dataclass(frozen=True)
class UnitFile:
    """Small lossless-enough representation of systemd key/value entries."""

    name: str
    sections: Mapping[str, Mapping[str, tuple[str, ...]]]
    text: str

    def values(self, section: str, key: str) -> tuple[str, ...]:
        return self.sections.get(section, {}).get(key, ())

    def effective_resettable_values(self, section: str, key: str) -> tuple[str, ...]:
        """Apply systemd's empty-assignment reset semantics to a list.

        An empty assignment clears all earlier assignments for directives such
        as ``ExecStart``.  Keeping the raw values lossless while exposing the
        effective ordered list lets the contract model the manager's behavior
        instead of treating ``ExecStart=`` as a literal empty command.
        """

        effective: list[str] = []
        for value in self.values(section, key):
            if value == "":
                effective.clear()
            else:
                effective.append(value)
        return tuple(effective)

    def one(self, section: str, key: str) -> str:
        values = self.values(section, key)
        if len(values) != 1:
            raise SystemdContractError(
                f"{self.name}: expected one {section}.{key}, got {len(values)}"
            )
        return values[0]


@dataclass(frozen=True)
class TimerPolicy:
    """Expected schedule and trigger semantics for one timer."""

    unit: str
    calendar: str | None = None
    boot: str | None = None
    active: str | None = None
    randomized: str = ""
    accuracy: str = ""
    persistent: str = "true"


EXPECTED_UNITS: tuple[str, ...] = (
    "deadlock-api.service",
    "deadlock-worker.service",
    "deadlock-web.service",
    "deadlock-maintenance.service",
    "deadlock-maintenance.timer",
    "deadlock-logrotate.service",
    "deadlock-logrotate.timer",
    "deadlock-offsite-backup.service",
    "deadlock-offsite-backup.timer",
    "deadlock-cloudflare-ips.service",
    "deadlock-cloudflare-ips.timer",
    "deadlock-health-monitor.service",
    "deadlock-health-monitor.timer",
)

EXPECTED_TIMERS: Mapping[str, TimerPolicy] = {
    "deadlock-maintenance.timer": TimerPolicy(
        unit="deadlock-maintenance.service",
        calendar="*-*-* 04:15:00",
        randomized="30m",
        accuracy="1m",
    ),
    "deadlock-logrotate.timer": TimerPolicy(
        unit="deadlock-logrotate.service",
        boot="15m",
        active="15m",
        randomized="5m",
        accuracy="1m",
    ),
    "deadlock-offsite-backup.timer": TimerPolicy(
        unit="deadlock-offsite-backup.service",
        calendar="*-*-* 05:15:00",
        randomized="30m",
        accuracy="1m",
    ),
    "deadlock-cloudflare-ips.timer": TimerPolicy(
        unit="deadlock-cloudflare-ips.service",
        calendar="*-*-* 02:35:00",
        randomized="30m",
        accuracy="5m",
    ),
    "deadlock-health-monitor.timer": TimerPolicy(
        unit="deadlock-health-monitor.service",
        boot="5m",
        active="5m",
        randomized="30s",
        accuracy="15s",
    ),
}

# These are the only units the normal installer may enable.  Off-site backup
# is installed for a later, operator-controlled recovery gate but is never
# enabled by either normal installer.
EXPECTED_SYSTEMD_INSTALL_ENABLE: tuple[tuple[str, ...], ...] = (
    ("deadlock-api.service", "deadlock-worker.service", "deadlock-web.service"),
    (
        "deadlock-maintenance.timer",
        "deadlock-logrotate.timer",
        "deadlock-cloudflare-ips.timer",
        "deadlock-health-monitor.timer",
    ),
)
EXPECTED_MAINTENANCE_INSTALL_ENABLE: tuple[tuple[str, ...], ...] = (
    ("deadlock-maintenance.timer", "deadlock-logrotate.timer"),
)
OFFSITE_TIMER = "deadlock-offsite-backup.timer"

_UNIT_NAME = re.compile(r"^deadlock-[A-Za-z0-9_.-]+\.(?:service|timer)$")
_FORBIDDEN_MARKERS = re.compile(r"(?:sparkydb|legacy|telegram)", re.IGNORECASE)
_TRUSTED_UNIT_MODE = 0o644
_RESTART_DIRECTIVES = frozenset(
    {
        "Restart",
        "RestartSec",
        "RestartPreventExitStatus",
        "RestartForceExitStatus",
        "RestartSteps",
        "RestartMaxDelaySec",
        "RestartMode",
    }
)
_CANONICAL_EXECUTABLE = re.compile(r"^/[A-Za-z0-9_./-]+$")
_CANONICAL_EXEC_ARGUMENT = re.compile(r"^[A-Za-z0-9_./=-]+$")


def _has_ignored_exec_prefix(value: str) -> bool:
    """Return whether a command line uses systemd's failure-ignoring prefix.

    systemd accepts ``@``, ``:``, ``-`` and one of ``+``, ``!`` or ``!!`` in
    any order before the executable.  Checking only ``value.startswith('-')``
    therefore misses valid combinations such as ``+-/bin/true`` and
    ``@-/bin/true``.  Stop at the first non-prefix character so a hyphen in an
    executable name or argument is not treated as an ignored-status marker.
    """

    prefix_end = 0
    while prefix_end < len(value):
        if value.startswith("!!", prefix_end):
            prefix_end += 2
            continue
        if value[prefix_end] in "@-:!+":
            prefix_end += 1
            continue
        break
    return "-" in value[:prefix_end]


def _is_canonical_exec_value(value: str) -> bool:
    """Accept only the closed command syntax used by tracked oneshot units.

    systemd decodes quotes, C escapes and command prefixes before it decides
    whether the executable ignores a failure.  Reimplementing that grammar
    incompletely would leave another bypass, so this contract accepts only the
    deliberately smaller syntax used by the reviewed units: an absolute
    executable, optional single-space-separated arguments, and a restricted
    argument alphabet.  Any quoting, escaping, control character, prefix or
    ambiguous whitespace is rejected instead of normalized.
    """

    if not value or any(character in value for character in "\t\r\n\v\f"):
        return False
    tokens = value.split(" ")
    if any(not token for token in tokens):
        return False
    if not _CANONICAL_EXECUTABLE.fullmatch(tokens[0]):
        return False
    return all(_CANONICAL_EXEC_ARGUMENT.fullmatch(token) for token in tokens[1:])

# The tracked services are a closed host boundary.  Keep the inventory split
# by runtime shape so a new long-running service cannot quietly inherit the
# oneshot failure policy (or vice versa).  The section maps below are exact:
# values, repeated Environment entries, dependency edges, conditions, cgroup
# limits and sandbox directives are all part of the reviewed contract.
LONG_RUNNING_SERVICES: tuple[str, ...] = (
    "deadlock-api.service",
    "deadlock-worker.service",
    "deadlock-web.service",
)
ONESHOT_SERVICES: tuple[str, ...] = (
    "deadlock-maintenance.service",
    "deadlock-logrotate.service",
    "deadlock-offsite-backup.service",
    "deadlock-cloudflare-ips.service",
    "deadlock-health-monitor.service",
)
EXPECTED_SERVICES: tuple[str, ...] = LONG_RUNNING_SERVICES + ONESHOT_SERVICES

# An ignored command prefix is never accepted in the tracked inventory.  The
# mapping is deliberately available as a named owner/rationale escape hatch,
# but must remain empty until a narrowly scoped operational exception is
# reviewed.  A bare boolean or a unit-wide exception would make a future
# failure-hiding change invisible to the contract.
IGNORED_EXEC_PREFIX_RATIONALES: Mapping[tuple[str, str, str], str] = {}

_EXEC_DIRECTIVES = frozenset(
    {
        "ExecStart",
        "ExecStartPre",
        "ExecStartPost",
        "ExecStartReload",
        "ExecStop",
        "ExecStopPost",
        "ExecCondition",
    }
)


def _service_sections(
    unit: Mapping[str, tuple[str, ...]],
    service: Mapping[str, tuple[str, ...]],
    install: Mapping[str, tuple[str, ...]] | None = None,
) -> Mapping[str, Mapping[str, tuple[str, ...]]]:
    sections: dict[str, Mapping[str, tuple[str, ...]]] = {
        "Unit": unit,
        "Service": service,
    }
    if install is not None:
        sections["Install"] = install
    return sections


_EXPECTED_SERVICE_SECTIONS: Mapping[str, Mapping[str, Mapping[str, tuple[str, ...]]]] = {
    "deadlock-api.service": _service_sections(
        {
            "Description": ("Old Sparky Arena API",),
            "After": ("network-online.target redis-server.service postgresql.service",),
            "Wants": ("network-online.target redis-server.service",),
        },
        {
            "Type": ("simple",),
            "User": ("oldsparky-api",),
            "Group": ("oldsparky-api",),
            "SupplementaryGroups": ("oldsparky-media",),
            "WorkingDirectory": ("/opt/oldsparky/platform/current",),
            "Environment": (
                "PLATFORM_RUNTIME_SERVICE=api",
                "PLATFORM_SHARED_DIR=/opt/oldsparky/platform/shared",
                "PLATFORM_ENV_FILE=/opt/oldsparky/platform/shared/env/api.env",
                "PLATFORM_PYTHON_BIN=/opt/oldsparky/platform/shared/venv/bin/python",
                "PLATFORM_API_WORKERS=2",
            ),
            "RuntimeDirectory": ("oldsparky-ready-vote-cprofile",),
            "RuntimeDirectoryMode": ("0700",),
            "LogRateLimitIntervalSec": ("30s",),
            "LogRateLimitBurst": ("5000",),
            "ExecStart": ("/opt/oldsparky/platform/current/tools/platform_run_api.sh",),
            "Restart": ("on-failure",),
            "RestartSec": ("5",),
            "UMask": ("0007",),
            "LimitNOFILE": ("65535",),
            "NoNewPrivileges": ("true",),
            "PrivateTmp": ("true",),
            "PrivateDevices": ("true",),
            "ProtectSystem": ("strict",),
            "ProtectHome": ("true",),
            "ProtectProc": ("invisible",),
            "ProtectKernelTunables": ("true",),
            "ProtectKernelModules": ("true",),
            "ProtectControlGroups": ("true",),
            "ProtectClock": ("true",),
            "ProtectHostname": ("true",),
            "RestrictAddressFamilies": ("AF_UNIX AF_INET AF_INET6",),
            "RestrictNamespaces": ("true",),
            "RestrictRealtime": ("true",),
            "RestrictSUIDSGID": ("true",),
            "LockPersonality": ("true",),
            "CapabilityBoundingSet": ("",),
            "AmbientCapabilities": ("",),
            "ReadWritePaths": ("/opt/oldsparky/platform/shared/media-staging",),
            "TasksMax": ("128",),
            "MemoryMax": ("1G",),
        },
        {"WantedBy": ("multi-user.target",)},
    ),
    "deadlock-worker.service": _service_sections(
        {
            "Description": ("Old Sparky Arena Worker",),
            "After": ("network-online.target redis-server.service postgresql.service",),
            "Wants": ("network-online.target redis-server.service",),
        },
        {
            "Type": ("simple",),
            "User": ("oldsparky-worker",),
            "Group": ("oldsparky-worker",),
            "SupplementaryGroups": ("oldsparky-media",),
            "WorkingDirectory": ("/opt/oldsparky/platform/current",),
            "Environment": (
                "PLATFORM_RUNTIME_SERVICE=worker",
                "PLATFORM_SHARED_DIR=/opt/oldsparky/platform/shared",
                "PLATFORM_ENV_FILE=/opt/oldsparky/platform/shared/env/worker.env",
                "PLATFORM_PYTHON_BIN=/opt/oldsparky/platform/shared/venv/bin/python",
                "PLATFORM_WORKER_CONCURRENCY=2",
            ),
            "LogRateLimitIntervalSec": ("30s",),
            "LogRateLimitBurst": ("2000",),
            "ExecStart": ("/opt/oldsparky/platform/current/tools/platform_run_worker.sh",),
            "Restart": ("on-failure",),
            "RestartSec": ("5",),
            "UMask": ("0007",),
            "NoNewPrivileges": ("true",),
            "PrivateTmp": ("true",),
            "PrivateDevices": ("true",),
            "ProtectSystem": ("strict",),
            "ProtectHome": ("true",),
            "ProtectProc": ("invisible",),
            "ProtectKernelTunables": ("true",),
            "ProtectKernelModules": ("true",),
            "ProtectControlGroups": ("true",),
            "ProtectClock": ("true",),
            "ProtectHostname": ("true",),
            "RestrictAddressFamilies": ("AF_UNIX AF_INET AF_INET6",),
            "RestrictNamespaces": ("true",),
            "RestrictRealtime": ("true",),
            "RestrictSUIDSGID": ("true",),
            "LockPersonality": ("true",),
            "CapabilityBoundingSet": ("",),
            "AmbientCapabilities": ("",),
            "ReadWritePaths": (
                "/opt/oldsparky/platform/shared/media-staging /opt/oldsparky/platform/shared/worker-state",
            ),
            "TasksMax": ("128",),
            "MemoryMax": ("1G",),
        },
        {"WantedBy": ("multi-user.target",)},
    ),
    "deadlock-web.service": _service_sections(
        {
            "Description": ("Old Sparky Arena Web",),
            "After": ("network-online.target deadlock-api.service",),
            "Wants": ("network-online.target",),
            "StartLimitIntervalSec": ("300s",),
            "StartLimitBurst": ("5",),
        },
        {
            "Type": ("simple",),
            "User": ("oldsparky-web",),
            "Group": ("oldsparky-web",),
            "WorkingDirectory": ("/opt/oldsparky/platform/current",),
            "Environment": (
                "NODE_ENV=production",
                "PLATFORM_RUNTIME_SERVICE=web",
                "PLATFORM_SHARED_DIR=/opt/oldsparky/platform/shared",
                "PLATFORM_ENV_FILE=/opt/oldsparky/platform/shared/env/web.env",
                "PLATFORM_NODE_BIN=/opt/oldsparky/platform/shared/node-v26.3.1/bin/node",
            ),
            "LogRateLimitIntervalSec": ("30s",),
            "LogRateLimitBurst": ("2000",),
            "ExecStart": ("/opt/oldsparky/platform/current/tools/platform_run_web.sh",),
            "SuccessExitStatus": ("143",),
            "Restart": ("always",),
            "RestartSec": ("5",),
            "UMask": ("0077",),
            "NoNewPrivileges": ("true",),
            "PrivateTmp": ("true",),
            "PrivateDevices": ("true",),
            "ProtectSystem": ("strict",),
            "ProtectHome": ("true",),
            "ProtectProc": ("invisible",),
            "ProtectKernelTunables": ("true",),
            "ProtectKernelModules": ("true",),
            "ProtectControlGroups": ("true",),
            "ProtectClock": ("true",),
            "ProtectHostname": ("true",),
            "RestrictAddressFamilies": ("AF_UNIX AF_INET AF_INET6",),
            "RestrictNamespaces": ("true",),
            "RestrictRealtime": ("true",),
            "RestrictSUIDSGID": ("true",),
            "LockPersonality": ("true",),
            "CapabilityBoundingSet": ("",),
            "AmbientCapabilities": ("",),
            "ReadWritePaths": (
                "/opt/oldsparky/platform/current/apps/platform_web/.next/standalone/.next/cache",
            ),
            "TasksMax": ("128",),
            "MemoryMax": ("1G",),
        },
        {"WantedBy": ("multi-user.target",)},
    ),
    "deadlock-maintenance.service": _service_sections(
        {
            "Description": ("Old Sparky Arena backup and storage maintenance",),
            "After": ("postgresql.service",),
            "Wants": ("postgresql.service",),
            "ConditionPathIsSymbolicLink": ("/opt/oldsparky/platform/current",),
        },
        {
            "Type": ("oneshot",),
            "User": ("root",),
            "Group": ("root",),
            "WorkingDirectory": ("/opt/oldsparky/platform/current",),
            "Environment": ("PLATFORM_PYTHON_BIN=/opt/oldsparky/platform/shared/venv/bin/python",),
            "ExecStart": (
                "/opt/oldsparky/platform/shared/venv/bin/python /opt/oldsparky/platform/current/tools/platform_storage_maintenance.py --apply --backup-keep 14 --release-keep 5 --test-artifact-max-age-days 7 --screenshot-max-age-days 30 --failed-build-max-age-days 1 --minimum-free-gib 5 --maximum-used-percent 85",
            ),
            "Nice": ("10",),
            "IOSchedulingClass": ("idle",),
            "CPUQuota": ("50%",),
            "MemoryMax": ("512M",),
            "TimeoutStartSec": ("30min",),
            "UMask": ("0077",),
            "PrivateTmp": ("true",),
            "ProtectSystem": ("full",),
            "LockPersonality": ("true",),
        },
    ),
    "deadlock-logrotate.service": _service_sections(
        {
            "Description": ("Rotate Old Sparky platform logs by bounded size",),
            "Documentation": ("man:logrotate(8)",),
            "RequiresMountsFor": ("/var/log",),
        },
        {
            "Type": ("oneshot",),
            "User": ("root",),
            "Group": ("root",),
            "ExecStart": ("/usr/sbin/logrotate /etc/logrotate.conf",),
            "Nice": ("19",),
            "IOSchedulingClass": ("idle",),
            "IOSchedulingPriority": ("7",),
            "PrivateTmp": ("true",),
            "ProtectSystem": ("full",),
            "ProtectHome": ("true",),
            "LockPersonality": ("true",),
            "MemoryDenyWriteExecute": ("true",),
            "TimeoutStartSec": ("5min",),
        },
    ),
    "deadlock-offsite-backup.service": _service_sections(
        {
            "Description": ("Old Sparky encrypted off-site database backup",),
            "Documentation": ("file:/opt/oldsparky/platform/current/docs/backup-restore-runbook.md",),
            "After": ("network-online.target deadlock-maintenance.service",),
            "Wants": ("network-online.target",),
            "ConditionPathIsSymbolicLink": ("/opt/oldsparky/platform/current",),
            "ConditionPathExists": ("/opt/oldsparky/platform/shared/.env.backup",),
        },
        {
            "Type": ("oneshot",),
            "User": ("root",),
            "Group": ("root",),
            "WorkingDirectory": ("/opt/oldsparky/platform/current",),
            "Environment": ("PYTHONDONTWRITEBYTECODE=1",),
            "ExecStart": (
                "/opt/oldsparky/platform/shared/venv/bin/python /opt/oldsparky/platform/current/tools/platform_backup_offsite.py --apply --env-file /opt/oldsparky/platform/shared/.env.backup --platform-env-file /opt/oldsparky/platform/shared/.env.platform --backup-dir /opt/oldsparky/platform/shared/backups --max-age-hours 30 --json",
            ),
            "UMask": ("0077",),
            "Nice": ("10",),
            "IOSchedulingClass": ("idle",),
            "CPUQuota": ("50%",),
            "MemoryMax": ("256M",),
            "TimeoutStartSec": ("30min",),
            "NoNewPrivileges": ("true",),
            "PrivateTmp": ("true",),
            "PrivateDevices": ("true",),
            "ProtectSystem": ("strict",),
            "ProtectHome": ("true",),
            "ProtectKernelTunables": ("true",),
            "ProtectKernelModules": ("true",),
            "ProtectKernelLogs": ("true",),
            "ProtectControlGroups": ("true",),
            "ProtectClock": ("true",),
            "ProtectHostname": ("true",),
            "LockPersonality": ("true",),
            "RestrictSUIDSGID": ("true",),
            "RestrictRealtime": ("true",),
            "RestrictNamespaces": ("true",),
            "CapabilityBoundingSet": ("",),
            "RestrictAddressFamilies": ("AF_UNIX AF_INET AF_INET6",),
        },
    ),
    "deadlock-cloudflare-ips.service": _service_sections(
        {
            "Description": ("Validate and refresh Cloudflare origin IP ranges for Nginx",),
            "After": ("network-online.target nginx.service",),
            "Wants": ("network-online.target",),
        },
        {
            "Type": ("oneshot",),
            "TimeoutStartSec": ("210s",),
            "User": ("root",),
            "Group": ("root",),
            "WorkingDirectory": ("/opt/oldsparky/platform/current",),
            "Environment": ("PLATFORM_PYTHON_BIN=/opt/oldsparky/platform/shared/venv/bin/python",),
            "ExecStart": (
                "/bin/bash /opt/oldsparky/platform/current/tools/platform_release_lock_exec.sh --app-dir /opt/oldsparky/platform -- /opt/oldsparky/platform/shared/venv/bin/python /opt/oldsparky/platform/current/tools/platform_update_cloudflare_ips.py --apply --reload",
            ),
            "UMask": ("0022",),
            "NoNewPrivileges": ("true",),
            "PrivateTmp": ("true",),
            "ProtectSystem": ("strict",),
            "ProtectHome": ("true",),
            "ReadWritePaths": ("/etc/nginx /run /var/log/nginx",),
            "LockPersonality": ("true",),
        },
    ),
    "deadlock-health-monitor.service": _service_sections(
        {
            "Description": ("Old Sparky Arena lightweight production health gate",),
            "After": ("network-online.target deadlock-api.service deadlock-worker.service deadlock-web.service nginx.service",),
            "Wants": ("network-online.target",),
            "ConditionPathIsSymbolicLink": ("/opt/oldsparky/platform/current",),
        },
        {
            "Type": ("oneshot",),
            "User": ("root",),
            "Group": ("root",),
            "WorkingDirectory": ("/opt/oldsparky/platform/current",),
            "ExecStart": (
                "/opt/oldsparky/platform/shared/venv/bin/python /opt/oldsparky/platform/current/tools/platform_health_monitor.py --disk-min-free-gib 5 --disk-max-used-percent 85",
            ),
            "Nice": ("10",),
            "CPUQuota": ("20%",),
            "MemoryMax": ("128M",),
            "TimeoutStartSec": ("90s",),
            "UMask": ("0077",),
            "PrivateTmp": ("true",),
            "ProtectSystem": ("strict",),
            "ProtectHome": ("true",),
            "NoNewPrivileges": ("true",),
            "LockPersonality": ("true",),
        },
    ),
}

_EXPECTED_TIMER_SECTIONS: Mapping[str, Mapping[str, Mapping[str, tuple[str, ...]]]] = {
    "deadlock-maintenance.timer": {
        "Unit": {"Description": ("Run Old Sparky Arena maintenance daily",)},
        "Timer": {
            "OnCalendar": ("*-*-* 04:15:00",),
            "RandomizedDelaySec": ("30m",),
            "Persistent": ("true",),
            "AccuracySec": ("1m",),
            "Unit": ("deadlock-maintenance.service",),
        },
        "Install": {"WantedBy": ("timers.target",)},
    },
    "deadlock-logrotate.timer": {
        "Unit": {"Description": ("Check Old Sparky platform log sizes every 15 minutes",)},
        "Timer": {
            "OnBootSec": ("15m",),
            "OnUnitActiveSec": ("15m",),
            "RandomizedDelaySec": ("5m",),
            "Persistent": ("true",),
            "AccuracySec": ("1m",),
            "Unit": ("deadlock-logrotate.service",),
        },
        "Install": {"WantedBy": ("timers.target",)},
    },
    "deadlock-offsite-backup.timer": {
        "Unit": {"Description": ("Run encrypted Old Sparky off-site backup daily",)},
        "Timer": {
            "OnCalendar": ("*-*-* 05:15:00",),
            "RandomizedDelaySec": ("30m",),
            "Persistent": ("true",),
            "AccuracySec": ("1m",),
            "Unit": ("deadlock-offsite-backup.service",),
        },
        "Install": {"WantedBy": ("timers.target",)},
    },
    "deadlock-cloudflare-ips.timer": {
        "Unit": {"Description": ("Refresh Cloudflare origin IP ranges daily",)},
        "Timer": {
            "OnCalendar": ("*-*-* 02:35:00",),
            "RandomizedDelaySec": ("30m",),
            "Persistent": ("true",),
            "AccuracySec": ("5m",),
            "Unit": ("deadlock-cloudflare-ips.service",),
        },
        "Install": {"WantedBy": ("timers.target",)},
    },
    "deadlock-health-monitor.timer": {
        "Unit": {"Description": ("Run Old Sparky Arena health gate every five minutes",)},
        "Timer": {
            "OnBootSec": ("5m",),
            "OnUnitActiveSec": ("5m",),
            "RandomizedDelaySec": ("30s",),
            "Persistent": ("true",),
            "AccuracySec": ("15s",),
            "Unit": ("deadlock-health-monitor.service",),
        },
        "Install": {"WantedBy": ("timers.target",)},
    },
}

_EXPECTED_UNIT_SECTIONS: Mapping[str, Mapping[str, Mapping[str, tuple[str, ...]]]] = {
    **_EXPECTED_SERVICE_SECTIONS,
    **_EXPECTED_TIMER_SECTIONS,
}


def _metadata(path: Path) -> os.stat_result:
    """Read path metadata without following a replacement symlink."""

    try:
        return os.lstat(path)
    except OSError as exc:
        raise SystemdContractError(f"{path.name}: cannot lstat unit file: {exc}") from exc


def _metadata_at(directory_fd: int, path: Path) -> os.stat_result:
    """Read one directory entry without following a symlink."""

    try:
        return os.lstat(path.name, dir_fd=directory_fd)
    except OSError as exc:
        raise SystemdContractError(f"{path.name}: cannot lstat unit file: {exc}") from exc


def _validate_unit_metadata(
    path: Path,
    metadata: os.stat_result,
    root_metadata: os.stat_result,
    *,
    expected_identity: tuple[int, ...] | None = None,
) -> None:
    """Apply the repository trust boundary to a tracked unit file."""

    if stat.S_ISLNK(metadata.st_mode):
        raise SystemdContractError(f"{path.name}: symlink unit files are forbidden")
    if not stat.S_ISREG(metadata.st_mode):
        raise SystemdContractError(f"{path.name}: unit file must be a regular file")
    if metadata.st_dev != root_metadata.st_dev:
        raise SystemdContractError(f"{path.name}: unit file is outside the trusted filesystem")
    if metadata.st_uid != root_metadata.st_uid or metadata.st_gid != root_metadata.st_gid:
        raise SystemdContractError(f"{path.name}: unexpected unit file ownership")
    if stat.S_IMODE(metadata.st_mode) != _TRUSTED_UNIT_MODE:
        raise SystemdContractError(
            f"{path.name}: unit file mode must be {_TRUSTED_UNIT_MODE:o}"
        )
    if metadata.st_nlink != 1:
        raise SystemdContractError(f"{path.name}: hard-linked unit files are forbidden")
    if expected_identity is not None and _identity(metadata) != expected_identity:
        raise SystemdContractError(f"{path.name}: unit file changed while being read")


def _identity(metadata: os.stat_result) -> tuple[int, ...]:
    """Return fields needed to detect replacement or in-place mutation."""

    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_gid,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_trusted_unit(
    path: Path,
    root_metadata: os.stat_result,
    directory_fd: int,
) -> str:
    """Read one unit through a stable descriptor inside the trust boundary."""

    before = _metadata_at(directory_fd, path)
    _validate_unit_metadata(path, before, root_metadata)
    expected_identity = _identity(before)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path.name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise SystemdContractError(f"{path.name}: cannot open trusted unit file: {exc}") from exc
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            opened = os.fstat(stream.fileno())
            _validate_unit_metadata(
                path,
                opened,
                root_metadata,
                expected_identity=expected_identity,
            )
            payload = stream.read()
            payload_digest = hashlib.sha256(payload).digest()
            after_read = os.fstat(stream.fileno())
            _validate_unit_metadata(
                path,
                after_read,
                root_metadata,
                expected_identity=expected_identity,
            )
            stream.seek(0)
            confirmation = stream.read()
            if hashlib.sha256(confirmation).digest() != payload_digest:
                raise SystemdContractError(
                    f"{path.name}: unit content changed while being read"
                )
            after_confirmation = os.fstat(stream.fileno())
            _validate_unit_metadata(
                path,
                after_confirmation,
                root_metadata,
                expected_identity=expected_identity,
            )
    except SystemdContractError:
        raise
    except OSError as exc:
        raise SystemdContractError(f"{path.name}: cannot read trusted unit file: {exc}") from exc

    after_close = _metadata_at(directory_fd, path)
    _validate_unit_metadata(
        path,
        after_close,
        root_metadata,
        expected_identity=expected_identity,
    )
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SystemdContractError(f"{path.name}: unit file is not valid UTF-8") from exc


def parse_unit(path: Path, *, text: str | None = None) -> UnitFile:
    """Parse the simple key/value subset used by the tracked unit files."""

    if text is None:
        text = path.read_text(encoding="utf-8")
    sections: dict[str, dict[str, list[str]]] = {}
    section: str | None = None
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith(('#', ';')):
            continue
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1].strip()
            if not section:
                raise SystemdContractError(f"{path.name}:{line_number}: empty section")
            sections.setdefault(section, {})
            continue
        if section is None or '=' not in line:
            raise SystemdContractError(f"{path.name}:{line_number}: malformed entry")
        key, value = (part.strip() for part in line.split('=', 1))
        if not key:
            raise SystemdContractError(f"{path.name}:{line_number}: empty key")
        sections.setdefault(section, {}).setdefault(key, []).append(value)
    return UnitFile(
        name=path.name,
        sections={
            section_name: {
                key: tuple(values) for key, values in entries.items()
            }
            for section_name, entries in sections.items()
        },
        text=text,
    )


def load_units(systemd_root: Path = SYSTEMD_ROOT) -> dict[str, UnitFile]:
    """Load and validate the closed platform unit file set."""

    root_path_before = _metadata(systemd_root)
    if not stat.S_ISDIR(root_path_before.st_mode):
        raise SystemdContractError("systemd unit root must be a directory")
    root_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        root_fd = os.open(systemd_root, root_flags)
    except OSError as exc:
        raise SystemdContractError(
            f"{systemd_root}: cannot open trusted unit root: {exc}"
        ) from exc

    try:
        root_metadata = os.fstat(root_fd)
        if not stat.S_ISDIR(root_metadata.st_mode):
            raise SystemdContractError("systemd unit root must be a directory")
        if _identity(root_metadata) != _identity(root_path_before):
            raise SystemdContractError("systemd unit root changed while being opened")

        entries_before = tuple(sorted(os.listdir(root_fd)))
        names = tuple(
            name
            for name in entries_before
            if Path(name).suffix in {".service", ".timer"}
        )
        expected = set(EXPECTED_UNITS)
        actual = set(names)
        if actual != expected:
            missing = sorted(expected - actual)
            unexpected = sorted(actual - expected)
            raise SystemdContractError(
                f"unit inventory mismatch: missing={missing!r} unexpected={unexpected!r}"
            )
        if any(not _UNIT_NAME.fullmatch(name) for name in names):
            raise SystemdContractError("unit inventory contains a non-platform name")
        paths = tuple(systemd_root / name for name in names)
        units = {
            path.name: parse_unit(
                path,
                text=_read_trusted_unit(path, root_metadata, root_fd),
            )
            for path in paths
        }
        for unit in units.values():
            if _FORBIDDEN_MARKERS.search(unit.text):
                raise SystemdContractError(f"{unit.name}: legacy/sparkydb marker found")

        root_metadata_after = os.fstat(root_fd)
        if _identity(root_metadata_after) != _identity(root_metadata):
            raise SystemdContractError("systemd unit root changed while being read")
        entries_after = tuple(sorted(os.listdir(root_fd)))
        if entries_after != entries_before:
            raise SystemdContractError("systemd unit root enumeration changed while being read")
        root_path_after = _metadata(systemd_root)
        if _identity(root_path_after) != _identity(root_metadata):
            raise SystemdContractError("systemd unit root was replaced while being read")
        return units
    finally:
        os.close(root_fd)


def _expect_values(unit: UnitFile, section: str, key: str, expected: Iterable[str]) -> None:
    actual = unit.values(section, key)
    expected_tuple = tuple(expected)
    if actual != expected_tuple:
        raise SystemdContractError(
            f"{unit.name}: {section}.{key}={actual!r}, expected {expected_tuple!r}"
        )


def _validate_exact_unit_directives(units: Mapping[str, UnitFile]) -> None:
    """Reject unit, service and timer directive drift at the source boundary."""

    for unit_name, expected_sections in _EXPECTED_UNIT_SECTIONS.items():
        unit = units[unit_name]
        expected_section_names = set(expected_sections)
        actual_section_names = set(unit.sections)
        if actual_section_names != expected_section_names:
            unexpected = sorted(actual_section_names - expected_section_names)
            missing = sorted(expected_section_names - actual_section_names)
            raise SystemdContractError(
                f"{unit_name}: section inventory mismatch: "
                f"missing={missing!r} unexpected={unexpected!r}"
            )
        for section_name, expected_entries in expected_sections.items():
            actual_entries = unit.sections[section_name]
            expected_keys = set(expected_entries)
            actual_keys = set(actual_entries)
            if actual_keys != expected_keys:
                unexpected = sorted(actual_keys - expected_keys)
                missing = sorted(expected_keys - actual_keys)
                restart_drift = sorted(
                    _RESTART_DIRECTIVES.intersection(actual_keys - expected_keys)
                )
                if restart_drift and unit_name in ONESHOT_SERVICES:
                    raise SystemdContractError(
                        f"{unit_name}: oneshot service has unexpected restart "
                        f"directives {restart_drift!r}"
                    )
                raise SystemdContractError(
                    f"{unit_name}: {section_name} directive inventory mismatch: "
                    f"missing={missing!r} unexpected={unexpected!r}"
                )
            for key, expected in expected_entries.items():
                try:
                    _expect_values(unit, section_name, key, expected)
                except SystemdContractError as exc:
                    if key in _EXEC_DIRECTIVES:
                        raise SystemdContractError(
                            f"{unit_name}: {section_name}.{key} must fail closed; {exc}"
                        ) from exc
                    raise


def _validate_exec_failure_visibility(units: Mapping[str, UnitFile]) -> None:
    """Reject failure-ignoring command/condition prefixes in every service."""

    for service_name in EXPECTED_SERVICES:
        service = units[service_name]
        for section_name, entries in service.sections.items():
            for key, values in entries.items():
                if key in _EXEC_DIRECTIVES:
                    # Empty assignments reset the effective systemd command
                    # list.  Check only commands that the manager will run;
                    # an ignored command hidden behind a reset is inert.
                    effective_values = service.effective_resettable_values(
                        section_name, key
                    )
                    for value in effective_values:
                        rationale = IGNORED_EXEC_PREFIX_RATIONALES.get(
                            (service_name, key, value)
                        )
                        if _has_ignored_exec_prefix(value) and not rationale:
                            raise SystemdContractError(
                                f"{service_name}: {section_name}.{key} must fail "
                                "closed (ignored '-' prefix forbidden)"
                            )
                    continue
                if key.startswith(("Condition", "Assert")):
                    for value in values:
                        # Conditions do not use the full command-prefix
                        # grammar, but a leading '-' (or a combined prefix
                        # that contains it) is still a failure-hiding input.
                        if _has_ignored_exec_prefix(value):
                            raise SystemdContractError(
                                f"{service_name}: {key} must not hide a failure"
                            )


def validate_unit_graph(units: Mapping[str, UnitFile]) -> None:
    """Ensure every explicit Unit= reference resolves to a tracked unit."""

    for unit in units.values():
        for section_entries in unit.sections.values():
            for key, values in section_entries.items():
                if key != "Unit":
                    continue
                for referenced in values:
                    if referenced not in units:
                        raise SystemdContractError(
                            f"{unit.name}: Unit={referenced!r} does not exist"
                        )
    for timer_name, policy in EXPECTED_TIMERS.items():
        timer = units[timer_name]
        if timer.one("Timer", "Unit") != policy.unit:
            raise SystemdContractError(
                f"{timer_name}: timer target does not match policy"
            )


def validate_timer_policy(units: Mapping[str, UnitFile]) -> None:
    """Validate deterministic schedules, catch-up and jitter semantics."""

    for timer_name, policy in EXPECTED_TIMERS.items():
        timer = units[timer_name]
        _expect_values(timer, "Timer", "Unit", (policy.unit,))
        _expect_values(
            timer,
            "Timer",
            "OnCalendar",
            () if policy.calendar is None else (policy.calendar,),
        )
        _expect_values(
            timer,
            "Timer",
            "OnBootSec",
            () if policy.boot is None else (policy.boot,),
        )
        _expect_values(
            timer,
            "Timer",
            "OnUnitActiveSec",
            () if policy.active is None else (policy.active,),
        )
        _expect_values(timer, "Timer", "RandomizedDelaySec", (policy.randomized,))
        _expect_values(timer, "Timer", "Persistent", (policy.persistent,))
        _expect_values(timer, "Timer", "AccuracySec", (policy.accuracy,))


def validate_failure_policy(units: Mapping[str, UnitFile]) -> None:
    """Keep oneshot failures visible and continuous services restartable."""

    # Scan before the exact-value check so an unsafe prefix receives the
    # failure-visibility diagnostic even when it also changes the reviewed
    # command value.  The exact map then closes every other drift boundary.
    _validate_exec_failure_visibility(units)
    _validate_exact_unit_directives(units)

    for service_name in ONESHOT_SERVICES:
        service = units[service_name]
        exec_values = service.effective_resettable_values("Service", "ExecStart")
        if not exec_values or any(
            _has_ignored_exec_prefix(value) or not _is_canonical_exec_value(value)
            for value in exec_values
        ):
            raise SystemdContractError(
                f"{service_name}: oneshot ExecStart must fail closed "
                "(canonical absolute command syntax required)"
            )


def validate_all(systemd_root: Path = SYSTEMD_ROOT) -> dict[str, UnitFile]:
    """Run all static checks and return the parsed units for callers/tests."""

    units = load_units(systemd_root)
    validate_unit_graph(units)
    validate_timer_policy(units)
    validate_failure_policy(units)
    return units


def main() -> int:
    validate_all()
    print(f"systemd timer contract: PASS ({len(EXPECTED_UNITS)} units)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
