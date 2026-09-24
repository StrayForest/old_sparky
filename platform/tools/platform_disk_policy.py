#!/usr/bin/env python3
"""Shared conservative disk-health policy for platform maintenance tools."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import shutil
from typing import Any


BYTES_PER_GIB = 1024**3
DEFAULT_MIN_FREE_GIB = 5.0
DEFAULT_MAX_USED_PERCENT = 85.0


@dataclass(frozen=True, slots=True)
class DiskSnapshot:
    """A bounded disk snapshot plus validity for fail-closed decisions."""

    total_bytes: int
    free_bytes: int
    used_percent: float
    valid: bool

    @property
    def used_bytes(self) -> int:
        return max(0, self.total_bytes - self.free_bytes)

    def as_dict(self) -> dict[str, int | float]:
        """Return the stable JSON shape used by maintenance evidence."""

        return {
            "total_bytes": self.total_bytes,
            "used_bytes": self.used_bytes,
            "free_bytes": self.free_bytes,
            "used_percent": round(self.used_percent, 2),
        }


def minimum_free_bytes(min_free_gib: float) -> int:
    """Convert a finite non-negative GiB threshold to bytes."""

    if not math.isfinite(min_free_gib) or min_free_gib < 0:
        raise ValueError("minimum free space must be finite and non-negative")
    return int(min_free_gib * BYTES_PER_GIB)


def snapshot_from_usage(usage: Any) -> DiskSnapshot:
    """Normalize ``shutil.disk_usage`` and fail closed on impossible values."""

    try:
        total_bytes = int(usage.total)
        free_bytes = int(usage.free)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return DiskSnapshot(total_bytes=0, free_bytes=0, used_percent=100.0, valid=False)

    if total_bytes <= 0 or free_bytes < 0 or free_bytes > total_bytes:
        return DiskSnapshot(
            total_bytes=total_bytes,
            free_bytes=max(0, free_bytes),
            used_percent=100.0,
            valid=False,
        )

    used_percent = (total_bytes - free_bytes) * 100 / total_bytes
    return DiskSnapshot(
        total_bytes=total_bytes,
        free_bytes=free_bytes,
        used_percent=used_percent,
        valid=True,
    )


def snapshot_for_path(path: Path) -> DiskSnapshot:
    """Read one filesystem snapshot using the same semantics everywhere."""

    return snapshot_from_usage(shutil.disk_usage(path))


def is_healthy(
    snapshot: DiskSnapshot,
    *,
    min_free_bytes: int,
    max_used_percent: float,
) -> bool:
    """Apply the hard gate; exact threshold boundaries are healthy."""

    if (
        not snapshot.valid
        or min_free_bytes < 0
        or not math.isfinite(max_used_percent)
        or max_used_percent <= 0
        or max_used_percent > 100
    ):
        return False
    return (
        snapshot.free_bytes >= min_free_bytes
        and snapshot.used_percent <= max_used_percent
    )
