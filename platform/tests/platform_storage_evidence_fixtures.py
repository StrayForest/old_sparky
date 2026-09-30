"""Private fixed report fixtures shared by storage evidence contract tests."""

from __future__ import annotations

import json


def valid_storage_report() -> bytes:
    """Return a complete framed report without live filesystem ownership."""

    lines = [
        '{"schema":1,"kind":"platform_storage_diagnostics",'
        '"active_release_id":"release-1",'
        '"active_source_sha":"' + "a" * 40 + '"}',
        "=== retained_load_lock ===",
        "state=unlocked",
        "=== filesystem_usage ===",
    ]
    for category in ("root", "tmp", "var_tmp", "platform_runtime", "logs"):
        lines.extend(
            (
                f"--- df {category}",
                "Filesystem 1024-blocks Used Available Capacity Mounted on",
                "1000 400 600 40% /private/path",
                f"--- inode {category}",
                "Inodes IUsed IFree IUse% Mounted on",
                "1000 400 40%",
            )
        )
    lines.extend(
        (
            "=== journal_usage ===",
            "Archived and active journals take up 32.0M in the file system.",
        )
    )
    lines.append("=== service_sandbox ===")
    for service in ("deadlock-api", "deadlock-worker", "deadlock-web"):
        lines.extend(
            (
                f"--- service {service}",
                "ActiveState=active",
                "SubState=running",
                "Result=success",
                "ExecMainCode=exited",
                "ExecMainStatus=0",
                "NRestarts=0",
                "MemoryCurrent=1",
                "MemoryPeak=1",
                "MemoryMax=infinity",
                "TasksCurrent=1",
                "TasksMax=infinity",
                "CPUUsageNSec=1",
            )
        )
    lines.append("=== known_category_usage ===")
    for category in (
        "source_release_artifacts",
        "browser_test_artifacts",
        "preprod_screenshots",
        "live_qa_runtime",
        "backups",
    ):
        lines.extend((f"--- category {category}", "42 /private/path"))
    lines.extend(
        (
            "=== storage_retention_dry_run ===",
            json.dumps(
                {
                    "ok": True,
                    "mode": "dry-run",
                    "production_releases": {
                        "protected": [],
                        "retained": [],
                        "deleted": [],
                        "protected_count": 0,
                        "retained_count": 0,
                        "deleted_count": 0,
                        "reclaimable_bytes": 0,
                    },
                    "source_release_artifacts": {
                        "protected": [],
                        "retained": [],
                        "deleted": [],
                        "protected_count": 0,
                        "retained_count": 0,
                        "deleted_count": 0,
                        "reclaimable_bytes": 0,
                    },
                    "live_qa_runtime_caches": {
                        "protected": [],
                        "retained": [],
                        "deleted": [],
                        "reclaimed_tombstones": [],
                        "protected_count": 0,
                        "retained_count": 0,
                        "deleted_count": 0,
                        "reclaimed_tombstone_count": 0,
                    },
                    "transient": {
                        "failed_builds": {"count": 0, "reclaimable_bytes": 0},
                        "browser_test_artifacts": {"count": 0, "reclaimable_bytes": 0},
                        "preprod_screenshots": {"count": 0, "reclaimable_bytes": 0},
                        "reclaimable_bytes": {
                            "failed_builds": 0,
                            "browser_test_artifacts": 0,
                            "preprod_screenshots": 0,
                        },
                    },
                    "duration_seconds": 0.1,
                    "limits": {
                        "minimum_free_bytes": 1,
                        "maximum_used_percent": 90,
                    },
                    "disk_before": {"free_bytes": 2, "used_percent": 10},
                    "disk_after": {"free_bytes": 2, "used_percent": 10},
                    "backup": {"status": "skipped"},
                },
                separators=(",", ":"),
            ),
        )
    )
    return ("\n".join(lines) + "\n").encode()


# The maintenance helper at both deployed source SHAs (0700b740 and the
# deployment-record merge 87547df2) has the same pre-count JSON contract.  The
# bytes below are a captured ``--json --skip-backup`` result with deterministic
# clock/disk fixtures; keep the pretty-printed form because the remote command
# writes it verbatim into the diagnostics frame.
LEGACY_RETENTION_0700_OUTPUT = b'''{
  "ok": true,
  "mode": "dry-run",
  "started_at_utc": "2026-09-30T12:34:56.123000Z",
  "completed_at_utc": "2026-09-30T12:34:56.123000Z",
  "duration_seconds": 0.0,
  "backup": {
    "status": "skipped"
  },
  "production_releases": {
    "protected": [
      "release-current",
      "release-previous"
    ],
    "retained": [],
    "deleted": [],
    "reclaimable_bytes": 0
  },
  "source_release_artifacts": {
    "protected": [],
    "retained": [],
    "deleted": [],
    "reclaimable_bytes": 0
  },
  "live_qa_runtime_caches": {
    "protected": [],
    "retained": [],
    "deleted": [],
    "reclaimed_tombstones": [
      "runtime-old"
    ]
  },
  "transient": {
    "failed_builds": [
      ".build-old"
    ],
    "browser_test_artifacts": [
      "test-results-old"
    ],
    "preprod_screenshots": [
      "shot-old.png"
    ],
    "reclaimable_bytes": {
      "failed_builds": 13,
      "browser_test_artifacts": 17,
      "preprod_screenshots": 19
    }
  },
  "disk_before": {
    "total_bytes": 1000000,
    "used_bytes": 400000,
    "free_bytes": 600000,
    "used_percent": 40.0
  },
  "disk_after": {
    "total_bytes": 1000000,
    "used_bytes": 400000,
    "free_bytes": 600000,
    "used_percent": 40.0
  },
  "limits": {
    "minimum_free_bytes": 0,
    "maximum_used_percent": 100.0,
    "live_qa_runtime_keep": 1
  }
}
'''

# The helper source at 87547df2 is byte-for-byte identical to the recorded
# 0700b740 helper; retain a separately named fixture so both provenance checks
# remain explicit in the regression tests.
LEGACY_RETENTION_87547_OUTPUT = LEGACY_RETENTION_0700_OUTPUT


def legacy_storage_report(retention_output: bytes) -> bytes:
    """Frame one captured retention result as a complete diagnostics report."""

    prefix, separator, suffix = valid_storage_report().partition(
        b"=== storage_retention_dry_run ===\n"
    )
    if not separator:
        raise AssertionError("storage fixture is missing the retention frame")
    return prefix + separator + retention_output
