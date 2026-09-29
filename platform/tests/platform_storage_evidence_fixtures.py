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
