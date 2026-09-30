#!/usr/bin/env python3
"""Static systemd unit/timer contract for the platform host.

The production host intentionally does not run ``systemd-analyze`` during a
release.  This small parser keeps the unit graph and schedule policy in the
repository-owned deterministic test contour instead.  It reads only the
tracked unit files and never talks to a systemd manager.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
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


def parse_unit(path: Path) -> UnitFile:
    """Parse the simple key/value subset used by the tracked unit files."""

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

    paths = sorted(
        path
        for path in systemd_root.iterdir()
        if path.is_file() and path.suffix in {".service", ".timer"}
    )
    names = tuple(path.name for path in paths)
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
    units = {path.name: parse_unit(path) for path in paths}
    for unit in units.values():
        if _FORBIDDEN_MARKERS.search(unit.text):
            raise SystemdContractError(f"{unit.name}: legacy/sparkydb marker found")
    return units


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
        if service.values("Service", "Restart"):
            raise SystemdContractError(
                f"{service_name}: oneshot service must not auto-restart"
            )
        exec_values = service.values("Service", "ExecStart")
        if not exec_values or any(value.startswith("-") for value in exec_values):
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
