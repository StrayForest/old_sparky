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
_FAIL_HIDING_EXEC_DIRECTIVES = frozenset(
    {"ExecStart", "ExecStartPre", "ExecStartPost", "ExecCondition"}
)


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

# A oneshot is an operational boundary: adding a new directive can change
# whether a failed maintenance/backup run is visible or silently retried.  An
# exact allow-list keeps that policy reviewable and makes accidental unit-file
# drift fail in the repository contract test.
_EXPECTED_ONESHOT_DIRECTIVES: Mapping[str, frozenset[str]] = {
    "deadlock-maintenance.service": frozenset(
        {
            "Type",
            "User",
            "Group",
            "WorkingDirectory",
            "Environment",
            "ExecStart",
            "Nice",
            "IOSchedulingClass",
            "CPUQuota",
            "MemoryMax",
            "TimeoutStartSec",
            "UMask",
            "PrivateTmp",
            "ProtectSystem",
            "LockPersonality",
        }
    ),
    "deadlock-logrotate.service": frozenset(
        {
            "Type",
            "User",
            "Group",
            "ExecStart",
            "Nice",
            "IOSchedulingClass",
            "IOSchedulingPriority",
            "PrivateTmp",
            "ProtectSystem",
            "ProtectHome",
            "LockPersonality",
            "MemoryDenyWriteExecute",
            "TimeoutStartSec",
        }
    ),
    "deadlock-offsite-backup.service": frozenset(
        {
            "Type",
            "User",
            "Group",
            "WorkingDirectory",
            "Environment",
            "ExecStart",
            "UMask",
            "Nice",
            "IOSchedulingClass",
            "CPUQuota",
            "MemoryMax",
            "TimeoutStartSec",
            "NoNewPrivileges",
            "PrivateTmp",
            "PrivateDevices",
            "ProtectSystem",
            "ProtectHome",
            "ProtectKernelTunables",
            "ProtectKernelModules",
            "ProtectKernelLogs",
            "ProtectControlGroups",
            "ProtectClock",
            "ProtectHostname",
            "LockPersonality",
            "RestrictSUIDSGID",
            "RestrictRealtime",
            "RestrictNamespaces",
            "CapabilityBoundingSet",
            "RestrictAddressFamilies",
        }
    ),
    "deadlock-cloudflare-ips.service": frozenset(
        {
            "Type",
            "TimeoutStartSec",
            "User",
            "Group",
            "WorkingDirectory",
            "Environment",
            "ExecStart",
            "UMask",
            "NoNewPrivileges",
            "PrivateTmp",
            "ProtectSystem",
            "ProtectHome",
            "ReadWritePaths",
            "LockPersonality",
        }
    ),
    "deadlock-health-monitor.service": frozenset(
        {
            "Type",
            "User",
            "Group",
            "WorkingDirectory",
            "ExecStart",
            "Nice",
            "CPUQuota",
            "MemoryMax",
            "TimeoutStartSec",
            "UMask",
            "PrivateTmp",
            "ProtectSystem",
            "ProtectHome",
            "NoNewPrivileges",
            "LockPersonality",
        }
    ),
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

    oneshot_services = tuple(policy.unit for policy in EXPECTED_TIMERS.values())
    for service_name in oneshot_services:
        service = units[service_name]
        _expect_values(service, "Service", "Type", ("oneshot",))
        service_entries = service.sections.get("Service", {})
        restart_directives = sorted(_RESTART_DIRECTIVES.intersection(service_entries))
        if restart_directives:
            raise SystemdContractError(
                f"{service_name}: oneshot service has unexpected restart directives "
                f"{restart_directives!r}"
            )
        if service.values("Service", "SuccessExitStatus"):
            raise SystemdContractError(
                f"{service_name}: oneshot service must not override SuccessExitStatus"
            )
        for key in _FAIL_HIDING_EXEC_DIRECTIVES:
            if key == "ExecStart":
                values = service.effective_resettable_values("Service", key)
            else:
                values = service.values("Service", key)
            if any(_has_ignored_exec_prefix(value) for value in values):
                raise SystemdContractError(
                    f"{service_name}: {key} must fail closed (ignored '-' prefix forbidden)"
                )
        for section_name, entries in service.sections.items():
            for key, values in entries.items():
                if (key.startswith("Condition") or key.startswith("Assert")) and any(
                    value.startswith("-") for value in values
                ):
                    raise SystemdContractError(
                        f"{service_name}: {key} must not hide a failure"
                    )
        unexpected = set(service_entries) - _EXPECTED_ONESHOT_DIRECTIVES[service_name]
        if unexpected:
            raise SystemdContractError(
                f"{service_name}: unexpected oneshot Service directives "
                f"{sorted(unexpected)!r}"
            )
        exec_values = service.effective_resettable_values("Service", "ExecStart")
        if not exec_values or any(_has_ignored_exec_prefix(value) for value in exec_values):
            raise SystemdContractError(
                f"{service_name}: oneshot ExecStart must fail closed"
            )

    for service_name, restart in {
        "deadlock-api.service": "on-failure",
        "deadlock-worker.service": "on-failure",
        "deadlock-web.service": "always",
    }.items():
        service = units[service_name]
        _expect_values(service, "Service", "Type", ("simple",))
        _expect_values(service, "Service", "Restart", (restart,))
        _expect_values(service, "Service", "RestartSec", ("5",))

    web = units["deadlock-web.service"]
    _expect_values(web, "Service", "SuccessExitStatus", ("143",))
    _expect_values(web, "Unit", "StartLimitIntervalSec", ("300s",))
    _expect_values(web, "Unit", "StartLimitBurst", ("5",))


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
