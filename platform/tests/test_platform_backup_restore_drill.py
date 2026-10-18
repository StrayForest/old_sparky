from __future__ import annotations

import argparse
import ast
import contextlib
import datetime as dt
import fcntl
import hashlib
import importlib.util
import io
import inspect
import json
import os
import pathlib
import re
import selectors
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import uuid
from typing import Any
from unittest import mock


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "platform" / "tools" / "platform_backup_restore_drill.py"
SPEC = importlib.util.spec_from_file_location("platform_backup_restore_drill", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
backup_drill = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = backup_drill
SPEC.loader.exec_module(backup_drill)


_SYNTHETIC_RESTORE_CI_ARTIFACT_NAMES = frozenset(
    {
        "synthetic-restore-diagnostic.json",
        "synthetic-restore-diagnostic-failure.json",
    }
)
_SYNTHETIC_RESTORE_TEST_ID = (
    "tests.test_platform_backup_restore_drill."
    "PlatformBackupRestoreDrillTests.test_synthetic_pg16_restore_diagnostic_under_root"
)


def _write_synthetic_restore_ci_artifact(
    name: str, artifact: dict[str, Any]
) -> pathlib.Path:
    if name not in _SYNTHETIC_RESTORE_CI_ARTIFACT_NAMES:
        raise RuntimeError("Synthetic restore CI artifact name failed validation.")
    artifact_root = REPO_ROOT / "platform" / "ci-backend"
    artifact_directory = artifact_root / "backend-privileged"
    for directory in (artifact_root, artifact_directory):
        created = False
        try:
            directory.mkdir(mode=0o755)
        except FileExistsError:
            pass
        else:
            created = True
        if created:
            directory.chmod(0o755)
        info = directory.lstat()
        if (
            not directory.is_dir()
            or directory.is_symlink()
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o755
        ):
            raise RuntimeError("Synthetic restore CI artifact directory failed validation.")
    artifact_path = artifact_directory / name
    temporary_path = artifact_directory / f".{name}.tmp"
    descriptor = os.open(
        temporary_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        0o644,
    )
    temporary_created = True
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
        ):
            raise RuntimeError("Synthetic restore CI artifact file failed validation.")
        os.fchmod(descriptor, 0o644)
        payload = (
            json.dumps(artifact, separators=(",", ":"), sort_keys=True).encode("ascii")
            + b"\n"
        )
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # Publish only a complete report without replacing an earlier artifact.
        os.link(temporary_path, artifact_path, follow_symlinks=False)
        temporary_path.unlink()
        temporary_created = False
        published = artifact_path.lstat()
        if (
            not stat.S_ISREG(published.st_mode)
            or published.st_uid != os.geteuid()
            or published.st_nlink != 1
            or stat.S_IMODE(published.st_mode) != 0o644
        ):
            raise RuntimeError("Synthetic restore artifact publication failed validation.")
        directory_fd = os.open(
            artifact_directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_created:
            temporary_path.unlink(missing_ok=True)
    return artifact_path


def _write_synthetic_restore_diagnostic_artifact(
    report: dict[str, Any], *, source_sha: str
) -> pathlib.Path:
    if (
        not backup_drill.valid_scratch_synthetic_restore_diagnostic(report)
        or re.fullmatch(r"[0-9a-f]{40}", source_sha) is None
    ):
        raise RuntimeError("Synthetic restore CI artifact context failed validation.")
    scratch_backend = report.get("scratch_backend")
    if type(scratch_backend) is not str or scratch_backend not in backup_drill.SCRATCH_BACKENDS:
        raise RuntimeError("Synthetic restore CI artifact backend failed validation.")
    artifact = {
        "kind": "synthetic_restore_diagnostic_artifact",
        "schema": 1,
        "source_sha": source_sha,
        "scratch_backend": scratch_backend,
        "diagnostic": report,
    }
    return _write_synthetic_restore_ci_artifact(
        "synthetic-restore-diagnostic.json", artifact
    )


_SYNTHETIC_RESTORE_STDERR_REASONS = frozenset(
    {
        "missing_module",
        "missing_executable",
        "missing_shared_library",
        "permission_denied",
        "namespace_setup_failure",
        "invalid_command_argument",
        "unknown_unit_property",
        "systemd_bus_unavailable",
        "synthetic_backend_exit_observer_failed",
        "synthetic_invalid_index_plan",
        "synthetic_unknown_restore_phase",
    }
)


def _synthetic_restore_stderr_reason(line: str) -> str | None:
    if line == "[FAIL] Synthetic restore reported an unknown phase.":
        return "synthetic_unknown_restore_phase"
    if line == "[FAIL] Synthetic restore returned an invalid index plan.":
        return "synthetic_invalid_index_plan"
    if line == "[FAIL] Synthetic restore backend-exit observer failed.":
        return "synthetic_backend_exit_observer_failed"
    if re.fullmatch(
        r"ModuleNotFoundError: No module named ['\"]"
        r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*['\"]",
        line,
    ):
        return "missing_module"
    if re.fullmatch(
        r"PermissionError: \[Errno [0-9]{1,3}\] Permission denied"
        r"(?:: (?:'[^'\r\n]*'|\"[^\"\r\n]*\"))?",
        line,
    ) or re.fullmatch(
        r"Failed at step EXEC spawning [^:\r\n]{1,512}: Permission denied",
        line,
    ):
        return "permission_denied"
    if re.fullmatch(
        r"FileNotFoundError: \[Errno [0-9]{1,3}\] No such file or directory"
        r"(?:: (?:'[^'\r\n]*'|\"[^\"\r\n]*\"))?",
        line,
    ) or re.fullmatch(
        r"Failed at step EXEC spawning [^:\r\n]{1,512}: No such file or directory",
        line,
    ):
        return "missing_executable"
    if re.fullmatch(
        r"[^:\r\n]{1,256}: error while loading shared libraries: "
        r"[A-Za-z0-9_.+-]+\.so(?:\.[0-9]+)*: cannot open shared object file: "
        r"No such file or directory",
        line,
    ) or re.fullmatch(
        r"ImportError: [A-Za-z0-9_.+-]+\.so(?:\.[0-9]+)*: cannot open shared object file: "
        r"No such file or directory",
        line,
    ):
        return "missing_shared_library"
    if re.fullmatch(
        r"(?:systemd-run: )?Failed to set up (?:mount|user|network|IPC|UTS|cgroup|time) "
        r"namespacing: (?:Permission denied|Operation not permitted|Invalid argument)",
        line,
    ) or re.fullmatch(
        r"Failed at step NAMESPACE spawning [^:\r\n]{1,512}: "
        r"(?:Permission denied|Operation not permitted|Invalid argument)",
        line,
    ):
        return "namespace_setup_failure"
    if re.fullmatch(
        r"(?:systemd-run|postgres|initdb|pg_ctl|pg_restore|psql): "
        r"(?:error: )?(?:unrecognized|unknown|invalid) (?:option|argument)(?: .*)?",
        line,
    ) or re.fullmatch(
        r"[^:\r\n]{1,256}: error: unrecognized arguments: .+",
        line,
    ):
        return "invalid_command_argument"
    if re.fullmatch(
        r"(?:systemd-run: )?Unknown assignment: [A-Za-z_][A-Za-z0-9_.]*=.*",
        line,
    ):
        return "unknown_unit_property"
    if re.fullmatch(
        r"(?:systemd-run: )?Failed to connect to bus: .+", line
    ) or re.fullmatch(
        r"System has not been booted with systemd as init system .*Can't operate\.",
        line,
    ):
        return "systemd_bus_unavailable"
    return None


def _classify_synthetic_restore_stderr(
    stderr_suffix: bytes | None, *, complete: bool | None
) -> dict[str, Any]:
    if stderr_suffix is None:
        if complete is not None:
            raise RuntimeError("Synthetic restore stderr suffix metadata is inconsistent.")
        return {
            "schema": 1,
            "classification": "unavailable",
            "reasons": [],
            "unknown_lines": 0,
            "suffix_bytes": None,
            "suffix_sha256": None,
            "suffix_complete": None,
        }
    if type(stderr_suffix) is not bytes or len(stderr_suffix) > backup_drill.SCRATCH_SYNTHETIC_STDERR_TAIL_BYTES:
        raise RuntimeError("Synthetic restore stderr suffix exceeded its fixed evidence bound.")
    if type(complete) is not bool:
        raise RuntimeError("Synthetic restore stderr suffix completeness failed validation.")

    # Classify the final bounded suffix only. Text is never included in the report.
    reasons: list[str] = []
    unknown_lines = 0 if complete else 1
    for line in stderr_suffix.decode("utf-8", errors="replace").splitlines():
        if not line:
            continue
        if len(line) > 1024 or any(ord(char) < 32 or ord(char) > 126 for char in line):
            unknown_lines += 1
            continue
        reason = _synthetic_restore_stderr_reason(line)
        if reason is None:
            unknown_lines += 1
        elif reason not in reasons:
            reasons.append(reason)
    if reasons and unknown_lines:
        classification = "mixed"
    elif reasons:
        classification = "known"
    else:
        classification = "unknown"
    if not set(reasons) <= _SYNTHETIC_RESTORE_STDERR_REASONS:
        raise RuntimeError("Synthetic restore stderr reason failed closed validation.")
    return {
        "schema": 1,
        "classification": classification,
        "reasons": reasons,
        "unknown_lines": unknown_lines,
        "suffix_bytes": len(stderr_suffix),
        "suffix_sha256": hashlib.sha256(stderr_suffix).hexdigest(),
        "suffix_complete": complete,
    }


def _write_synthetic_restore_failure_artifact(
    failure: Exception,
    *,
    source_sha: str,
    scratch_backend: str = backup_drill.SCRATCH_BACKEND_TMPFS_V1,
) -> pathlib.Path:
    if (
        re.fullmatch(r"[0-9a-f]{40}", source_sha) is None
        or type(scratch_backend) is not str
        or scratch_backend not in backup_drill.SCRATCH_BACKENDS
    ):
        raise RuntimeError("Synthetic restore failure artifact source failed validation.")
    unit_diagnostic = getattr(failure, "scratch_unit_diagnostic", None)
    if unit_diagnostic is not None and not backup_drill.valid_scratch_unit_diagnostic(
        unit_diagnostic
    ):
        raise RuntimeError("Synthetic restore failure unit summary failed validation.")
    journal_diagnostic = getattr(
        failure,
        "scratch_unit_journal_diagnostic",
        backup_drill._scratch_empty_journal_diagnostic("unavailable"),
    )
    if not backup_drill.valid_scratch_unit_journal_diagnostic(journal_diagnostic):
        raise RuntimeError("Synthetic restore failure journal summary failed validation.")
    first_fault = getattr(failure, "scratch_first_fault", None)
    if first_fault is not None and not backup_drill.valid_scratch_first_fault(first_fault):
        raise RuntimeError("Synthetic restore failure site failed validation.")
    image_identity_diagnostic = getattr(
        failure, "scratch_image_identity_diagnostic", None
    )
    if image_identity_diagnostic is not None and (
        scratch_backend != backup_drill.SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1
        or not backup_drill.valid_scratch_image_identity_diagnostic(
            image_identity_diagnostic
        )
        or first_fault is None
        or first_fault.get("origin") != "controller"
        or first_fault.get("site") != "controller_identity"
        or first_fault.get("verification_site") != "controller_image_mount_identity"
        or unit_diagnostic is None
        or unit_diagnostic.get("verification_site") != "controller_image_mount_identity"
    ):
        raise RuntimeError("Synthetic restore image identity projection failed validation.")
    cleanup_status = getattr(failure, "scratch_cleanup_status", "unknown")
    if not backup_drill.valid_scratch_cleanup_status(cleanup_status):
        raise RuntimeError("Synthetic restore cleanup status failed validation.")
    cleanup_diagnostic = getattr(failure, "scratch_cleanup_diagnostic", None)
    if cleanup_diagnostic is None:
        cleanup_diagnostic = {
            "schema": 1,
            "service_stopped": None,
            "mount_absent": None,
            "mountpoint_removed": None,
            "loop_released": None,
            "image_created_by_invocation": None,
            "image_unlinked": None,
            "retention_receipt_written": None,
            "protected_paths_cleaned": None,
            "cleanup_status": cleanup_status,
        }
    if (
        not backup_drill.valid_scratch_cleanup_diagnostic(cleanup_diagnostic)
        or cleanup_diagnostic["cleanup_status"] != cleanup_status
        or (
            scratch_backend == backup_drill.SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1
            and cleanup_status == "confirmed"
            and (
                cleanup_diagnostic["image_created_by_invocation"] is None
                or (
                    cleanup_diagnostic["image_created_by_invocation"] is True
                    and (
                        cleanup_diagnostic["loop_released"] is not True
                        or cleanup_diagnostic["image_unlinked"] is not True
                    )
                )
            )
        )
    ):
        raise RuntimeError("Synthetic restore cleanup projection failed validation.")

    private_output = getattr(failure, "scratch_unit_private_output", None)
    if private_output is None:
        if unit_diagnostic is not None:
            raise RuntimeError("Synthetic restore output was missing its closed unit summary.")
        stderr_suffix = None
        stderr_suffix_complete = None
    elif (
        not isinstance(private_output, tuple)
        or len(private_output) != 2
        or any(type(part) is not bytes for part in private_output)
        or len(private_output[0]) > backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES
        or len(private_output[1]) > backup_drill.SCRATCH_PRIVATE_STDERR_BYTES
    ):
        raise RuntimeError("Synthetic restore failure output exceeded its closed bounds.")
    else:
        # Stdout and stderr text are never exported; only fixed reason enums and
        # the bounded stderr byte count/hash are retained.
        if unit_diagnostic is not None and (
            len(private_output[0]) != unit_diagnostic["stdout_bytes"]
            or len(private_output[1]) != unit_diagnostic["stderr_bytes"]
            or hashlib.sha256(private_output[0]).hexdigest()
            != unit_diagnostic["stdout_sha256"]
            or hashlib.sha256(private_output[1]).hexdigest()
            != unit_diagnostic["stderr_sha256"]
        ):
            raise RuntimeError("Synthetic restore failure output did not match its closed summary.")

    if private_output is not None:
        stderr_suffix = getattr(failure, "scratch_synthetic_stderr_tail", None)
        stderr_suffix_complete = getattr(
            failure, "scratch_synthetic_stderr_tail_complete", None
        )
        if stderr_suffix is not None and (
            type(stderr_suffix) is not bytes
            or len(stderr_suffix) > backup_drill.SCRATCH_SYNTHETIC_STDERR_TAIL_BYTES
            or type(stderr_suffix_complete) is not bool
        ):
            raise RuntimeError("Synthetic restore stderr suffix failed validation.")
        if (
            stderr_suffix is not None
            and unit_diagnostic is not None
            and not unit_diagnostic["stderr_truncated"]
            and stderr_suffix != private_output[1][
                -backup_drill.SCRATCH_SYNTHETIC_STDERR_TAIL_BYTES :
            ]
        ):
            raise RuntimeError("Synthetic restore stderr suffix did not match its closed summary.")
    else:
        stderr_suffix = None
        stderr_suffix_complete = None

    artifact = {
        "kind": "synthetic_restore_diagnostic_failure_artifact",
        "schema": 1,
        "source_sha": source_sha,
        "scratch_backend": scratch_backend,
        "test_id": _SYNTHETIC_RESTORE_TEST_ID,
        "failure_stage": "run_synthetic_restore_diagnostic",
        "scratch_first_fault": first_fault,
        "scratch_unit_diagnostic": unit_diagnostic,
        "scratch_unit_journal_diagnostic": journal_diagnostic,
        "scratch_cleanup_status": cleanup_status,
        "scratch_cleanup_diagnostic": cleanup_diagnostic,
        **(
            {"scratch_image_identity_diagnostic": image_identity_diagnostic}
            if image_identity_diagnostic is not None
            else {}
        ),
        "stderr_diagnostic": _classify_synthetic_restore_stderr(
            stderr_suffix, complete=stderr_suffix_complete
        ),
    }
    return _write_synthetic_restore_ci_artifact(
        "synthetic-restore-diagnostic-failure.json", artifact
    )


def _run_synthetic_restore_diagnostic_for_ci(
    *, source_sha: str, scratch_backend: str | None = None
) -> dict[str, Any]:
    backend = (
        os.environ.get("PLATFORM_SCRATCH_BACKEND", backup_drill.SCRATCH_BACKEND_TMPFS_V1)
        if scratch_backend is None
        else scratch_backend
    )
    if type(backend) is not str or backend not in backup_drill.SCRATCH_BACKENDS:
        raise RuntimeError("Synthetic restore backend selection failed closed.")
    try:
        return backup_drill.run_synthetic_restore_diagnostic(scratch_backend=backend)
    except Exception as failure:
        try:
            _write_synthetic_restore_failure_artifact(
                failure, source_sha=source_sha, scratch_backend=backend
            )
        except Exception:
            # The original diagnostic exception remains the test failure if
            # secondary evidence cannot be retained; never serialize raw details.
            pass
        raise


class StagedWorkerInput:
    """Expose START first and DONE only after the worker emits its candidate."""

    def __init__(self) -> None:
        self._reads = [backup_drill.SCRATCH_START_TOKEN, backup_drill.SCRATCH_DONE_TOKEN]

    def read(self, size: int = -1) -> bytes:
        if not self._reads:
            return b""
        value = self._reads.pop(0)
        if size >= 0 and len(value) > size:
            self._reads.insert(0, value[size:])
            return value[:size]
        return value


class ScratchAllocationTreeTests(unittest.TestCase):
    def test_metadata_walk_observes_created_and_deleted_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scratch-allocation-tree-") as temporary_dir:
            root = pathlib.Path(temporary_dir)
            data = root / "data"
            base = data / "base"
            nested = base / "nested"
            wal = data / "pg_wal"
            nested.mkdir(parents=True)
            wal.mkdir()
            (data / "PG_VERSION").write_bytes(b"16")
            (base / "rel").write_bytes(b"abc")
            (nested / "piece").write_bytes(b"12345")
            (wal / "segment").write_bytes(b"1234567")
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            self.addCleanup(os.close, root_fd)

            def sample() -> dict[str, list[int]]:
                return backup_drill._scratch_allocation_tree(
                    root_fd,
                    os.fstat(root_fd).st_dev,
                    time.monotonic() + 1.0,
                )

            before = sample()
            self.assertEqual(before["data"][0], 17)
            self.assertEqual(before["data"][2:], [4, 4])
            self.assertEqual(before["base"][0], 8)
            self.assertEqual(before["base"][2:], [2, 2])
            self.assertEqual(before["pg_wal"][0], 7)
            self.assertEqual(before["pg_wal"][2:], [1, 1])

            added = base / "new"
            added.write_bytes(b"later")
            after_create = sample()
            self.assertEqual(after_create["data"][0], before["data"][0] + 5)
            self.assertEqual(after_create["data"][2], before["data"][2] + 1)
            added.unlink()
            after_delete = sample()
            self.assertEqual(after_delete["data"][0], before["data"][0])
            self.assertEqual(after_delete["base"][2], before["base"][2])

    def test_symlink_is_rejected_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scratch-allocation-link-") as temporary_dir:
            root = pathlib.Path(temporary_dir)
            data = root / "data"
            (data / "base").mkdir(parents=True)
            (data / "pg_wal").mkdir()
            outside = root / "outside"
            outside.write_text("private fixture", encoding="ascii")
            (data / "linked").symlink_to(outside)
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            self.addCleanup(os.close, root_fd)
            with self.assertRaises(backup_drill._ScratchAllocationReadError) as caught:
                backup_drill._scratch_allocation_tree(
                    root_fd,
                    os.fstat(root_fd).st_dev,
                    time.monotonic() + 1.0,
                )
            self.assertEqual(caught.exception.reason, "unsupported_entry")

    def test_node_cap_counts_root_once_and_does_not_list_all_names(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scratch-allocation-cap-") as temporary_dir:
            root = pathlib.Path(temporary_dir)
            data = root / "data"
            (data / "base").mkdir(parents=True)
            (data / "pg_wal").mkdir()
            (data / "extra").write_bytes(b"x")
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            self.addCleanup(os.close, root_fd)
            with (
                mock.patch.object(backup_drill, "SCRATCH_ALLOCATION_ENTRY_MAX", 3),
                mock.patch.object(backup_drill.os, "listdir", side_effect=AssertionError("unbounded listing")),
            ):
                with self.assertRaises(backup_drill._ScratchAllocationReadError) as caught:
                    backup_drill._scratch_allocation_tree(
                        root_fd,
                        os.fstat(root_fd).st_dev,
                        time.monotonic() + 1.0,
                    )
            self.assertEqual(caught.exception.reason, "entry_cap")


class ScratchAllocationAttributionTests(unittest.TestCase):
    @staticmethod
    def _sample(phase: str, *, status: str = "complete", reason: str | None = None) -> dict[str, Any]:
        missing = status == "unknown"
        return {
            "schema": 1,
            "phase": phase,
            "status": status,
            "unknown_reason": reason,
            "sample_unix_ns": 1_800_000_000_000_000_000,
            "sample_offset_ms": 25,
            "guard_available_bytes": 1_066_323_968 if phase == "parent_last_guard" else None,
            "guard_sample_lag_ms": 4 if phase == "parent_last_guard" else None,
            "mount": {
                "dev": 2049 if not missing else None,
                "ino": 15 if not missing else None,
                "mount_id": 55 if not missing else None,
                "total_bytes": 512 * 1024**2 if not missing else None,
                "used_bytes": 128 * 1024**2 if not missing else None,
                "free_bytes": 384 * 1024**2 if not missing else None,
            },
            "host": {
                "mem_available_bytes": 1_066_323_968 if not missing else None,
                "rss_bytes": 90 * 1024**2 if not missing else None,
                "memory_current_bytes": 300 * 1024**2 if not missing else None,
                "anon_bytes": 20 * 1024**2 if not missing else None,
                "file_bytes": 280 * 1024**2 if not missing else None,
                "shmem_bytes": 200 * 1024**2 if not missing else None,
            },
            "tree": {
                name: None if missing else [10, 16, 1, 1]
                for name in ("data", "base", "pg_wal")
            },
        }

    def test_four_lifecycle_boundaries_parse_and_match_private_failure_frame(self) -> None:
        phases = (
            "initdb_end",
            "postgres_start_end",
            "restore_platform_start",
            "restore_platform_end",
        )
        raw = b"".join(
            backup_drill.SCRATCH_ALLOCATION_FRAME_MARKER
            + json.dumps(self._sample(phase), separators=(",", ":"), sort_keys=True).encode("ascii")
            + b"\n"
            for phase in phases
        )
        attribution = backup_drill._scratch_parse_allocation_attribution(
            raw,
            truncated=False,
            archive_size_bytes=23,
            archive_sha256="a" * 64,
        )
        self.assertTrue(backup_drill.valid_scratch_allocation_attribution(attribution))
        self.assertEqual(attribution["status"], "complete")
        self.assertEqual([row["phase"] for row in attribution["samples"]], list(phases))
        error = RuntimeError("primary restore failure")
        error.scratch_allocation_attribution = attribution
        report = backup_drill.failure_report_for_exception(
            error, private_scratch_diagnostics=True
        )
        frame = backup_drill._scratch_allocation_attribution_frame(attribution)
        self.assertEqual(report["scratch_allocation_attribution"], attribution)
        self.assertEqual(
            frame,
            backup_drill.SCRATCH_ALLOCATION_ATTRIBUTION_MARKER
            + json.dumps(attribution, separators=(",", ":"), sort_keys=True).encode("ascii")
            + b"\n",
        )
        self.assertNotIn(
            "scratch_allocation_attribution",
            backup_drill.failure_report_for_exception(
                error, private_scratch_diagnostics=False
            ),
        )

    def test_worker_failure_and_parent_samples_remain_partial(self) -> None:
        phases = (
            "initdb_end",
            "postgres_start_end",
            "restore_platform_start",
            "worker_failure",
        )
        raw = b"".join(
            backup_drill.SCRATCH_ALLOCATION_FRAME_MARKER
            + json.dumps(self._sample(phase), separators=(",", ":"), sort_keys=True).encode("ascii")
            + b"\n"
            for phase in phases
        )
        attribution = backup_drill._scratch_parse_allocation_attribution(
            raw,
            truncated=False,
            archive_size_bytes=23,
            archive_sha256="e" * 64,
            parent_sample=self._sample("parent_last_guard", status="unknown", reason="deadline"),
        )
        self.assertTrue(backup_drill.valid_scratch_allocation_attribution(attribution))
        self.assertEqual(attribution["status"], "partial")
        self.assertEqual(attribution["unknown_reason"], "deadline")
        self.assertEqual(
            [row["phase"] for row in attribution["samples"]],
            [*phases, "parent_last_guard"],
        )

    def test_worker_sample_rechecks_mount_row_identity_after_walk(self) -> None:
        host = {
            "mem_available_bytes": 2**30,
            "rss_bytes": 90 * 1024**2,
            "memory_current_bytes": 300 * 1024**2,
            "anon_bytes": 20 * 1024**2,
            "file_bytes": 280 * 1024**2,
            "shmem_bytes": 200 * 1024**2,
        }
        control_group = "/system.slice/oldsparky-backup-restore-" + "c" * 24 + ".service"
        args = argparse.Namespace(
            private_scratch_diagnostics=True,
            _scratch_authorization_probe=False,
            _scratch_phase_origin_monotonic_ns=time.monotonic_ns(),
        )
        with tempfile.TemporaryDirectory(prefix="scratch-worker-sample-") as temporary_dir:
            root = pathlib.Path(temporary_dir)
            (root / "data" / "base").mkdir(parents=True)
            (root / "data" / "pg_wal").mkdir()
            (root / "data" / "base" / "rel").write_bytes(b"relation")
            (root / "data" / "pg_wal" / "segment").write_bytes(b"wal")
            root_info = root.stat()
            device = f"{os.major(root_info.st_dev)}:{os.minor(root_info.st_dev)}"

            def mountinfo(mount_id: int) -> bytes:
                return (
                    f"{mount_id} 1 {device} / {root} rw - tmpfs tmpfs rw,size=536870912\n"
                ).encode("ascii")

            for rows, expected_status, expected_reason in (
                ([mountinfo(55), mountinfo(55)], "complete", None),
                ([mountinfo(55), mountinfo(56)], "unknown", "mount_identity"),
            ):
                with self.subTest(mount_id=expected_status):
                    with tempfile.TemporaryDirectory(prefix="scratch-worker-mountinfo-") as mountinfo_dir:
                        mountinfo_paths = []
                        for index, payload in enumerate(rows):
                            path = pathlib.Path(mountinfo_dir) / f"mountinfo-{index}"
                            path.write_bytes(payload)
                            mountinfo_paths.append(path)
                        pending = list(mountinfo_paths)
                        real_open = os.open

                        def fake_open(path, flags, *open_args, **kwargs):
                            if os.fspath(path) == "/proc/self/mountinfo":
                                return real_open(pending.pop(0), flags, *open_args, **kwargs)
                            return real_open(path, flags, *open_args, **kwargs)

                        with (
                            mock.patch.object(backup_drill.os, "open", side_effect=fake_open),
                            mock.patch.object(
                                backup_drill,
                                "_scratch_allocation_host_values",
                                return_value=(host, True),
                            ),
                            mock.patch.object(
                                backup_drill,
                                "_scratch_private_proc_file",
                                return_value=f"0::{control_group}\n".encode("ascii"),
                            ),
                        ):
                            sample = backup_drill._scratch_allocation_worker_sample(
                                args, root, "restore_platform_start"
                            )
                    self.assertEqual(pending, [])
                    self.assertTrue(backup_drill.valid_scratch_allocation_sample(sample))
                    self.assertEqual(sample["status"], expected_status)
                    self.assertEqual(sample["unknown_reason"], expected_reason)
                    self.assertEqual(sample["mount"]["mount_id"], 55)

    def test_phase_parser_ignores_interleaved_allocation_frames(self) -> None:
        phase_rows = [
            (phase, event)
            for phase in backup_drill.SCRATCH_PHASES
            for event in ("start", "end")
        ]
        lines: list[bytes] = []
        allocation_line = (
            backup_drill.SCRATCH_ALLOCATION_FRAME_MARKER
            + json.dumps(
                self._sample("initdb_end"), separators=(",", ":"), sort_keys=True
            ).encode("ascii")
            + b"\n"
        )
        for index, (phase, event) in enumerate(phase_rows):
            if index == 5:
                lines.append(allocation_line)
            frame = {
                "ordinal": index + 1,
                "phase": phase,
                "event": event,
                "worker_offset_ms": index,
            }
            lines.append(
                backup_drill.SCRATCH_PHASE_FRAME_MARKER
                + json.dumps(frame, separators=(",", ":"), sort_keys=True).encode("ascii")
                + b"\n"
            )

        phase = backup_drill._scratch_parse_phase_attribution(
            b"".join(lines),
            truncated=False,
            complete=True,
            launch_monotonic_ns=1,
        )
        self.assertTrue(backup_drill.valid_scratch_phase_attribution(phase))
        self.assertEqual(phase["status"], "complete")
        self.assertEqual(len(phase["frames"]), backup_drill.SCRATCH_PHASE_FRAME_MAX_COUNT)

    def test_capture_prefix_and_malformed_frames_remain_partial_unknowns(self) -> None:
        first = (
            backup_drill.SCRATCH_ALLOCATION_FRAME_MARKER
            + json.dumps(self._sample("initdb_end"), separators=(",", ":"), sort_keys=True).encode("ascii")
            + b"\n"
        )
        prefix = backup_drill._scratch_parse_allocation_attribution(
            first,
            truncated=True,
            archive_size_bytes=23,
            archive_sha256="b" * 64,
        )
        self.assertTrue(backup_drill.valid_scratch_allocation_attribution(prefix))
        self.assertEqual((prefix["status"], prefix["unknown_reason"]), ("partial", "capture_prefix"))
        malformed = backup_drill._scratch_parse_allocation_attribution(
            first + backup_drill.SCRATCH_ALLOCATION_FRAME_MARKER + b"{bad}\n",
            truncated=False,
            archive_size_bytes=23,
            archive_sha256="b" * 64,
        )
        self.assertTrue(backup_drill.valid_scratch_allocation_attribution(malformed))
        self.assertEqual((malformed["status"], malformed["unknown_reason"]), ("partial", "malformed"))
        empty = backup_drill._scratch_parse_allocation_attribution(
            b"", truncated=False, archive_size_bytes=23, archive_sha256="b" * 64
        )
        self.assertTrue(backup_drill.valid_scratch_allocation_attribution(empty))
        self.assertEqual((empty["status"], empty["unknown_reason"], empty["samples"]), (
            "unknown", "capture_unavailable", []
        ))

    def test_parent_sample_is_appended_without_replacing_first_fault(self) -> None:
        worker = (
            backup_drill.SCRATCH_ALLOCATION_FRAME_MARKER
            + json.dumps(self._sample("initdb_end"), separators=(",", ":"), sort_keys=True).encode("ascii")
            + b"\n"
        )
        parent = self._sample("parent_last_guard", status="unknown", reason="deadline")
        attribution = backup_drill._scratch_parse_allocation_attribution(
            worker,
            truncated=False,
            archive_size_bytes=23,
            archive_sha256="c" * 64,
            parent_sample=parent,
        )
        self.assertEqual(attribution["status"], "partial")
        self.assertEqual(attribution["unknown_reason"], "deadline")
        self.assertEqual(attribution["samples"][-1]["phase"], "parent_last_guard")
        error = PermissionError(13, "primary reserve failure")
        backup_drill._retain_first_scratch_fault(
            error, origin="controller", site="controller_active_poll"
        )
        first_fault = dict(error.scratch_first_fault)
        error.scratch_allocation_attribution = attribution
        backup_drill._retain_first_scratch_fault(
            error, origin="worker", site="worker_restore"
        )
        report = backup_drill.failure_report_for_exception(
            error, private_scratch_diagnostics=True
        )
        self.assertEqual(error.scratch_first_fault, first_fault)
        self.assertEqual(report["scratch_first_fault"], first_fault)
        self.assertEqual(report["scratch_allocation_attribution"], attribution)
        self.assertIn(
            backup_drill.SCRATCH_ALLOCATION_ATTRIBUTION_MARKER,
            backup_drill.private_failure_frame_for_exception(error, enabled=True).encode("ascii"),
        )

    def test_schema_rejects_boolean_counters_and_partial_values_as_complete(self) -> None:
        sample = self._sample("initdb_end")
        self.assertTrue(backup_drill.valid_scratch_allocation_sample(sample))
        malformed = {
            **sample,
            "tree": {
                **sample["tree"],
                "data": [True, 16, 1, 1],
            },
        }
        self.assertFalse(backup_drill.valid_scratch_allocation_sample(malformed))
        unknown = self._sample("initdb_end", status="unknown", reason="deadline")
        partial = {
            "schema": 1,
            "status": "partial",
            "unknown_reason": "deadline",
            "archive_size_bytes": 23,
            "archive_sha256": "d" * 64,
            "samples": [unknown],
        }
        self.assertTrue(backup_drill.valid_scratch_allocation_attribution(partial))
        self.assertFalse(
            backup_drill.valid_scratch_allocation_attribution(
                {**partial, "status": "complete", "unknown_reason": None}
            )
        )

    def test_parent_last_guard_uses_pinned_proc_root_fd_and_local_mount_row(self) -> None:
        pid_text = "4321"
        invocation = "b" * 32
        expected_start = "123456"
        control_group = "/system.slice/oldsparky-backup-restore-" + "c" * 24 + ".service"
        relative_mount = "run/oldsparky-backup-restore-" + "a" * 24
        mountpoint = pathlib.Path("/") / relative_mount
        with tempfile.TemporaryDirectory(prefix="scratch-parent-last-guard-") as temporary_dir:
            root = pathlib.Path(temporary_dir)
            scratch = root / relative_mount
            (scratch / "data" / "base").mkdir(parents=True)
            (scratch / "data" / "pg_wal").mkdir()
            (scratch / "data" / "base" / "rel").write_bytes(b"rel")
            (scratch / "data" / "pg_wal" / "segment").write_bytes(b"wal")
            real_open = os.open
            real_stat = os.stat
            temp_root_fd = real_open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            self.addCleanup(os.close, temp_root_fd)
            root_info = os.fstat(temp_root_fd)
            mountinfo = (
                f"22 1 {os.major(root_info.st_dev)}:{os.minor(root_info.st_dev)} "
                f"/ /{relative_mount} rw - tmpfs tmpfs rw,size=536870912\n"
            ).encode("ascii")
            mountinfo_reads = [mountinfo, mountinfo]

            def read_mountinfo(_pid_text: str, _name: str, _cap: int) -> bytes:
                return mountinfo_reads.pop(0)

            def fake_open(path, flags, *args, **kwargs):
                if os.fspath(path).endswith("/exe"):
                    raise AssertionError("parent telemetry must not read proc exe")
                if os.fspath(path) == f"/proc/{pid_text}/root":
                    return real_open(root, flags)
                return real_open(path, flags, *args, **kwargs)

            def fake_stat(path, *args, **kwargs):
                if os.fspath(path) == f"/proc/{pid_text}/root":
                    return real_stat(root)
                if os.fspath(path) == f"/proc/{pid_text}/ns/mnt":
                    return types.SimpleNamespace(st_dev=7, st_ino=8)
                return real_stat(path, *args, **kwargs)

            resource = {
                "schema": 1,
                "status": "complete",
                "sample_offset_ms": 20,
                "unknown_reason": None,
                "controller": {"identity_bound": True, "rss_bytes": 1_000_000},
                "worker": {"identity_bound": True, "rss_bytes": 2_000_000},
                "cgroup": {
                    "identity_bound": True,
                    "memory_current_bytes": 300_000_000,
                    "anon_bytes": 20_000_000,
                    "file_bytes": 280_000_000,
                    "shmem_bytes": 200_000_000,
                    "events": {"high": 0, "max": 0, "oom": 0, "oom_kill": 0},
                },
            }
            guard = {
                "schema": 2,
                "guard_site": "parent_active_poll",
                "guard_reason": "memory_reserve",
                "available_bytes": backup_drill.SCRATCH_MEMORY_ADMISSION_BYTES - 1,
                "required_bytes": backup_drill.SCRATCH_MEMORY_ADMISSION_BYTES,
            }
            with (
                mock.patch.object(backup_drill.os, "open", side_effect=fake_open),
                mock.patch.object(backup_drill.os, "stat", side_effect=fake_stat),
                mock.patch.object(
                    backup_drill,
                    "_scratch_process_start_time",
                    return_value=expected_start,
                ),
                mock.patch.object(
                    backup_drill,
                    "_scratch_proc_status_observation",
                    return_value=(int(pid_text), 1, 2_000_000),
                ),
                mock.patch.object(
                    backup_drill,
                    "_scratch_private_proc_file",
                    return_value=f"0::{control_group}\n".encode("ascii"),
                ),
                mock.patch.object(
                    backup_drill,
                    "_scratch_allocation_proc_bytes",
                    side_effect=read_mountinfo,
                ),
            ):
                sample_started = time.monotonic()
                sample = backup_drill._scratch_allocation_parent_last_guard(
                    mountpoint=mountpoint,
                    worker_identity=(pid_text, invocation, expected_start),
                    control_group=control_group,
                    launch_monotonic_ns=time.monotonic_ns(),
                    guard_diagnostic=guard,
                    resource_attribution=resource,
                    operation_deadline=time.monotonic() + 1.0,
                    guard_monotonic_ns=time.monotonic_ns(),
                )
                sample_elapsed = time.monotonic() - sample_started
                self.assertEqual(mountinfo_reads, [])
            self.assertTrue(backup_drill.valid_scratch_allocation_sample(sample))
            self.assertEqual(sample["phase"], "parent_last_guard")
            self.assertEqual(sample["status"], "complete")
            self.assertEqual(sample["mount"]["mount_id"], 22)
            self.assertEqual(sample["tree"]["base"][0], 3)
            self.assertEqual(sample["tree"]["pg_wal"][0], 3)
            # This disposable fixture is expected to finish quickly. The
            # assertion is a test observation, not a hard syscall deadline.
            self.assertLess(sample_elapsed, 1.0)

            mountinfo_reads.extend(
                [mountinfo, mountinfo.replace(b"22 1 ", b"23 1 ", 1)]
            )
            with (
                mock.patch.object(backup_drill.os, "open", side_effect=fake_open),
                mock.patch.object(backup_drill.os, "stat", side_effect=fake_stat),
                mock.patch.object(
                    backup_drill,
                    "_scratch_process_start_time",
                    return_value=expected_start,
                ),
                mock.patch.object(
                    backup_drill,
                    "_scratch_proc_status_observation",
                    return_value=(int(pid_text), 1, 2_000_000),
                ),
                mock.patch.object(
                    backup_drill,
                    "_scratch_private_proc_file",
                    return_value=f"0::{control_group}\n".encode("ascii"),
                ),
                mock.patch.object(
                    backup_drill,
                    "_scratch_allocation_proc_bytes",
                    side_effect=read_mountinfo,
                ),
            ):
                changed = backup_drill._scratch_allocation_parent_last_guard(
                    mountpoint=mountpoint,
                    worker_identity=(pid_text, invocation, expected_start),
                    control_group=control_group,
                    launch_monotonic_ns=time.monotonic_ns(),
                    guard_diagnostic=guard,
                    resource_attribution=resource,
                    operation_deadline=time.monotonic() + 1.0,
                    guard_monotonic_ns=time.monotonic_ns(),
                )
            self.assertTrue(backup_drill.valid_scratch_allocation_sample(changed))
            self.assertEqual(changed["status"], "unknown")
            self.assertEqual(changed["unknown_reason"], "mount_identity")
            self.assertEqual(changed["mount"]["mount_id"], 22)


class PlatformBackupRestoreDrillTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_proc_start_time = backup_drill._scratch_process_start_time
        self._proc_start_time_patch = mock.patch.object(
            backup_drill, "_scratch_process_start_time", return_value="123456"
        )
        self._proc_start_time_patch.start()
        self.addCleanup(self._proc_start_time_patch.stop)

    def test_scratch_disk_image_backend_command_and_mount_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            scratch_root = pathlib.Path(temporary_dir) / "scratch"
            scratch_root.mkdir(mode=0o700)
            archive_path = pathlib.Path(temporary_dir) / "platformdb.dump"
            archive_path.write_bytes(b"PGDMPsynthetic archive")
            image_path = backup_drill.SCRATCH_DISK_IMAGE_ROOT / "restore-0123456789abcdef01234567.ext4"
            common = {
                "mountpoint": scratch_root,
                "archive_path": archive_path,
                "archive_stat": archive_path.stat(),
                "archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
                "timestamp_slug": "20261012T000000Z",
                "unit_name": "oldsparky-backup-restore-0123456789abcdef01234567.service",
            }
            default_command = backup_drill._scratch_unit_command(**common)
            self.assertTrue(any(
                item.startswith("--property=TemporaryFileSystem=") for item in default_command
            ))
            self.assertFalse(any(item.startswith("--property=MountImages=") for item in default_command))

            disk_command = backup_drill._scratch_unit_command(
                **common,
                scratch_backend=backup_drill.SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1,
                scratch_image_path=image_path,
            )
            self.assertIn(
                f"--property=MountImages={image_path}:{scratch_root}:rw,nosuid,nodev,noexec",
                disk_command,
            )
            self.assertFalse(any(
                item.startswith("--property=TemporaryFileSystem=") for item in disk_command
            ))
            for invariant in (
                "--property=MemoryMax=671088640",
                "--property=MemorySwapMax=0",
                "--property=PrivateNetwork=yes",
                "--property=PrivateDevices=yes",
                "--property=NoNewPrivileges=yes",
            ):
                self.assertIn(invariant, disk_command)
            probe_command = backup_drill._scratch_unit_command(
                mountpoint=scratch_root,
                archive_path=None,
                archive_stat=None,
                archive_sha256=None,
                timestamp_slug="20261012T000000Z",
                unit_name="oldsparky-backup-restore-0123456789abcdef01234567.service",
                authorization_probe=True,
                scratch_backend=backup_drill.SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1,
                scratch_image_path=image_path,
            )
            self.assertIn("--_scratch-authorization-probe", probe_command)
            self.assertFalse(any(
                item.startswith("--property=BindReadOnlyPaths=") and "archive.dump" in item
                for item in probe_command
            ))
            self.assertFalse(any("--_scratch-archive" in item for item in probe_command))
            synthetic_disk_command = backup_drill._scratch_unit_command(
                mountpoint=scratch_root,
                archive_path=None,
                archive_stat=None,
                archive_sha256=None,
                timestamp_slug="20261012T000000Z",
                unit_name="oldsparky-backup-restore-0123456789abcdef01234567.service",
                synthetic_restore_diagnostic=True,
                scratch_backend=backup_drill.SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1,
                scratch_image_path=image_path,
            )
            self.assertIn(
                f"--property=MountImages={image_path}:{scratch_root}:rw,nosuid,nodev,noexec",
                synthetic_disk_command,
            )
            self.assertIn(backup_drill.SCRATCH_SYNTHETIC_RESTORE_DIAGNOSTIC_MODE, synthetic_disk_command)
            self.assertIn("--property=MemorySwapMax=0", synthetic_disk_command)
            self.assertIn("--property=PrivateDevices=yes", synthetic_disk_command)
            self.assertFalse(any(
                item.startswith("--property=BindReadOnlyPaths=") and "archive.dump" in item
                for item in synthetic_disk_command
            ))
            self.assertFalse(any("--_scratch-archive" in item for item in synthetic_disk_command))
            with self.assertRaisesRegex(ValueError, "cannot bind a disk image"):
                backup_drill._scratch_unit_command(**common, scratch_image_path=image_path)

    def test_synthetic_restore_diagnostic_cli_is_closed_and_backend_typed(self) -> None:
        output = io.StringIO()
        argv = [
            str(backup_drill.__file__),
            backup_drill.SCRATCH_SYNTHETIC_RESTORE_DIAGNOSTIC_MODE,
            "--_scratch-backend",
            backup_drill.SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1,
            "--json",
        ]
        result = {"kind": "synthetic_restore_diagnostic", "schema": 3}
        with (
            mock.patch.object(backup_drill.sys, "argv", argv),
            mock.patch.object(
                backup_drill, "run_synthetic_restore_diagnostic", return_value=result
            ) as run_synthetic,
            mock.patch.object(backup_drill, "create_backup") as create_backup,
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(backup_drill.main(), 0)
        run_synthetic.assert_called_once_with(
            scratch_backend=backup_drill.SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1
        )
        create_backup.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()), result)

        invalid = [*argv, "--verify-dump", "/root/protected.dump"]
        with (
            mock.patch.object(backup_drill.sys, "argv", invalid),
            mock.patch.object(backup_drill, "run_synthetic_restore_diagnostic") as forbidden_run,
            mock.patch.object(backup_drill, "create_backup") as forbidden_backup,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(backup_drill.main(), 1)
        forbidden_run.assert_not_called()
        forbidden_backup.assert_not_called()

    def test_disk_image_authorization_probe_cli_is_typed_and_narrow(self) -> None:
        argv = [
            str(backup_drill.__file__),
            "--_scratch-authorization-probe",
            "--_scratch-backend",
            backup_drill.SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1,
            "--json",
            "--private-scratch-diagnostics",
        ]
        output = io.StringIO()
        with (
            mock.patch.object(backup_drill.sys, "argv", argv),
            mock.patch.object(
                backup_drill,
                "run_scratch_authorization_probe",
                return_value={"schema": 1, "status": "completed"},
            ) as probe,
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(backup_drill.main(), 0)
        probe.assert_called_once_with(
            scratch_backend=backup_drill.SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1
        )
        self.assertEqual(json.loads(output.getvalue()), {"schema": 1, "status": "completed"})

        invalid_argv = [
            str(backup_drill.__file__),
            "--_scratch-backend",
            backup_drill.SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1,
        ]
        with (
            mock.patch.object(backup_drill.sys, "argv", invalid_argv),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            backup_drill.parse_args()

    def test_scratch_disk_mount_snapshot_requires_exact_ext4_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            mountpoint = pathlib.Path(temporary_dir) / "scratch"
            mountpoint.mkdir(mode=0o700)
            mountpoint.chmod(0o700)
            expected = (
                str(mountpoint),
                "7:42",
                "ext4",
                "/dev/disk/by-id/unit-private-alias",
                "/",
                "rw,nosuid,nodev,noexec,relatime",
            )
            statvfs_result = types.SimpleNamespace(
                f_blocks=140_000,
                f_frsize=4096,
                f_bavail=20_000,
            )
            root_mount_stat = types.SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o700,
                st_uid=0,
                st_gid=0,
                st_dev=mountpoint.stat().st_dev,
                st_ino=mountpoint.stat().st_ino,
            )
            real_lstat = pathlib.Path.lstat

            def root_owned_mount_lstat(path: pathlib.Path) -> os.stat_result:
                if path == mountpoint:
                    return root_mount_stat  # type: ignore[return-value]
                return real_lstat(path)

            with (
                mock.patch.object(backup_drill, "_scratch_mountinfo_for", return_value=expected),
                mock.patch.object(
                    backup_drill,
                    "_scratch_sysfs_loop_device_predicates",
                    return_value=(None, {
                        "device_major_minor_valid": True,
                        "sysfs_mount_verified": True,
                        "sysfs_device_resolved": True,
                        "sysfs_loop_device": True,
                        "sysfs_device_number_matches": True,
                        "sysfs_backing_file_present": True,
                    }),
                ),
                mock.patch.object(backup_drill.os, "statvfs", return_value=statvfs_result),
                mock.patch.object(pathlib.Path, "lstat", root_owned_mount_lstat),
            ):
                snapshot = backup_drill._scratch_disk_mount_snapshot(mountpoint)
            self.assertEqual(snapshot["filesystem"], "ext4")
            self.assertEqual(snapshot["device"], "7:42")
            self.assertEqual(snapshot["total_bytes"], 140_000 * 4096)
            self.assertEqual(snapshot["free_bytes"], 20_000 * 4096)

            invalid_mounts = (
                ((*expected[:2], "tmpfs", *expected[3:]), "filesystem_not_ext4", {"fs_ext4"}),
                ((*expected[:4], "/nested", expected[5]), "mount_root_not_global", {"root_expected"}),
                ((*expected[:5], "nosuid,nodev,noexec,relatime"), "mount_options_missing", {"option_rw"}),
                ((*expected[:5], "rw,nodev,noexec,relatime"), "mount_options_missing", {"option_nosuid"}),
                ((*expected[:5], "rw,nosuid,noexec,relatime"), "mount_options_missing", {"option_nodev"}),
                ((*expected[:5], "rw,nosuid,nodev,relatime"), "mount_options_missing", {"option_noexec"}),
                ((*expected[:3], " /dev/disk/by-id/unit-private-alias ", *expected[4:]), "source_noncanonical", {"source_canonical"}),
                (("/other/mount", *expected[1:]), "mountpoint_mismatch", {"target_expected"}),
            )
            for invalid, expected_reason, failed_predicates in invalid_mounts:
                with (
                    self.subTest(invalid=invalid),
                    mock.patch.object(backup_drill, "_scratch_mountinfo_for", return_value=invalid),
                    mock.patch.object(
                        backup_drill,
                        "_scratch_sysfs_loop_device_predicates",
                        return_value=(None, {
                            "device_major_minor_valid": True,
                            "sysfs_mount_verified": True,
                            "sysfs_device_resolved": True,
                            "sysfs_loop_device": True,
                            "sysfs_device_number_matches": True,
                            "sysfs_backing_file_present": True,
                        }),
                    ),
                    mock.patch.object(backup_drill.os, "statvfs", return_value=statvfs_result),
                    mock.patch.object(pathlib.Path, "lstat", root_owned_mount_lstat),
                ):
                    with self.assertRaisesRegex(RuntimeError, "pinned private ext4 image mount") as failure:
                        backup_drill._scratch_disk_mount_snapshot(mountpoint)
                    self.assertEqual(
                        failure.exception.scratch_disk_mount_failure_reason,
                        expected_reason,
                    )
                    predicates = failure.exception.scratch_disk_mount_predicates
                    self.assertEqual(set(predicates), {
                        "fs_ext4", "source_canonical", "device_major_minor_valid",
                        "sysfs_mount_verified", "sysfs_device_resolved", "sysfs_loop_device",
                        "sysfs_device_number_matches", "sysfs_backing_file_present", "root_expected",
                        "target_expected", "option_rw", "option_nosuid", "option_nodev",
                        "option_noexec",
                    })
                    self.assertEqual({key for key, value in predicates.items() if not value}, failed_predicates)
                    first_fault = backup_drill._scratch_first_fault(
                        failure.exception, origin="worker", site="worker_mount_snapshot"
                    )
                    self.assertTrue(backup_drill.valid_scratch_first_fault(first_fault))
                    self.assertEqual(first_fault["disk_mount_failure_reason"], expected_reason)
                    self.assertEqual(first_fault["disk_mount_predicates"], predicates)

            for bad_stats, reason, expected_predicates in (
                (
                    types.SimpleNamespace(f_blocks=100_000, f_frsize=4096, f_bavail=20_000),
                    "total_capacity_out_of_bounds",
                    {"total_capacity_bounded": False, "free_capacity_above_stop": True},
                ),
                (
                    types.SimpleNamespace(f_blocks=140_000, f_frsize=4096, f_bavail=1),
                    "free_below_stop",
                    {"total_capacity_bounded": True, "free_capacity_above_stop": False},
                ),
            ):
                with (
                    self.subTest(bad_stats=bad_stats),
                    mock.patch.object(backup_drill, "_scratch_mountinfo_for", return_value=expected),
                    mock.patch.object(
                        backup_drill,
                        "_scratch_sysfs_loop_device_predicates",
                        return_value=(None, {
                            "device_major_minor_valid": True,
                            "sysfs_mount_verified": True,
                            "sysfs_device_resolved": True,
                            "sysfs_loop_device": True,
                            "sysfs_device_number_matches": True,
                            "sysfs_backing_file_present": True,
                        }),
                    ),
                    mock.patch.object(backup_drill.os, "statvfs", return_value=bad_stats),
                    mock.patch.object(pathlib.Path, "lstat", root_owned_mount_lstat),
                ):
                    with self.assertRaisesRegex(RuntimeError, "below its bounded usable capacity") as failure:
                        backup_drill._scratch_disk_mount_snapshot(mountpoint)
                    self.assertEqual(failure.exception.scratch_disk_mount_failure_reason, reason)
                    self.assertEqual(failure.exception.scratch_disk_mount_predicates, expected_predicates)

            with (
                mock.patch.object(backup_drill, "_scratch_mountinfo_for", side_effect=OSError(2, "missing")),
                mock.patch.object(pathlib.Path, "lstat", root_owned_mount_lstat),
                self.assertRaises(RuntimeError) as failure,
            ):
                backup_drill._scratch_disk_mount_snapshot(mountpoint)
            self.assertEqual(failure.exception.scratch_disk_mount_failure_reason, "mount_metadata")

    def test_scratch_disk_mount_snapshot_projects_closed_failure_predicates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            mountpoint = pathlib.Path(temporary_dir) / "scratch"
            mountpoint.mkdir(mode=0o700)
            mountpoint.chmod(0o700)
            expected = (
                str(mountpoint), "7:42", "ext4", "/dev/loop42", "/",
                "rw,nosuid,nodev,noexec,relatime",
            )
            root_stat = types.SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o700,
                st_uid=0,
                st_gid=0,
                st_dev=mountpoint.stat().st_dev,
                st_ino=mountpoint.stat().st_ino,
            )
            real_lstat = pathlib.Path.lstat

            def lstat(path: pathlib.Path) -> os.stat_result:
                if path == mountpoint:
                    return root_stat  # type: ignore[return-value]
                return real_lstat(path)

            bad_root = types.SimpleNamespace(**vars(root_stat))
            bad_root.st_gid = 1
            with (
                mock.patch.object(backup_drill, "_scratch_mountinfo_for", return_value=expected),
                mock.patch.object(
                    backup_drill,
                    "_scratch_sysfs_loop_device_predicates",
                    return_value=(None, {
                        "device_major_minor_valid": True,
                        "sysfs_mount_verified": True,
                        "sysfs_device_resolved": True,
                        "sysfs_loop_device": True,
                        "sysfs_device_number_matches": True,
                        "sysfs_backing_file_present": True,
                    }),
                ),
                mock.patch.object(pathlib.Path, "lstat", lstat),
                mock.patch.object(
                    backup_drill.os,
                    "statvfs",
                    return_value=types.SimpleNamespace(
                        f_blocks=100_000, f_frsize=4096, f_bavail=20_000
                    ),
                ),
            ):
                with self.assertRaises(RuntimeError) as failure:
                    backup_drill._scratch_disk_mount_snapshot(mountpoint)
                self.assertEqual(
                    failure.exception.scratch_disk_mount_failure_reason,
                    "total_capacity_out_of_bounds",
                )

            with (
                mock.patch.object(backup_drill, "_scratch_mountinfo_for", return_value=expected),
                mock.patch.object(
                    backup_drill,
                    "_scratch_sysfs_loop_device_predicates",
                    return_value=(None, {
                        "device_major_minor_valid": True,
                        "sysfs_mount_verified": True,
                        "sysfs_device_resolved": True,
                        "sysfs_loop_device": True,
                        "sysfs_device_number_matches": True,
                        "sysfs_backing_file_present": True,
                    }),
                ),
                mock.patch.object(pathlib.Path, "lstat", lambda path: bad_root if path == mountpoint else real_lstat(path)),
            ):
                with self.assertRaises(RuntimeError) as failure:
                    backup_drill._scratch_disk_mount_snapshot(mountpoint)
                self.assertEqual(
                    failure.exception.scratch_disk_mount_failure_reason,
                    "root_gid_mismatch",
                )
                self.assertEqual(failure.exception.scratch_disk_mount_predicates, {
                    "root_uid": True, "root_gid": False, "root_mode_0700": True,
                })

            with (
                mock.patch.object(backup_drill, "_scratch_mountinfo_for", return_value=expected),
                mock.patch.object(
                    backup_drill,
                    "_scratch_sysfs_loop_device_predicates",
                    return_value=(None, {
                        "device_major_minor_valid": True,
                        "sysfs_mount_verified": True,
                        "sysfs_device_resolved": True,
                        "sysfs_loop_device": True,
                        "sysfs_device_number_matches": True,
                        "sysfs_backing_file_present": True,
                    }),
                ),
                mock.patch.object(pathlib.Path, "lstat", lstat),
                mock.patch.object(backup_drill.os, "statvfs", side_effect=OSError(5, "private")),
            ):
                with self.assertRaises(RuntimeError) as failure:
                    backup_drill._scratch_disk_mount_snapshot(mountpoint)
                self.assertEqual(
                    failure.exception.scratch_disk_mount_failure_reason,
                    "statvfs_unavailable",
                )
                self.assertEqual(backup_drill._scratch_exception_projection(failure.exception), ("os_error", 5))

    def test_sysfs_mount_device_resolution_is_major_minor_bound_and_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            sys_root = pathlib.Path(temporary_dir) / "sys"
            dev_block = sys_root / "dev" / "block"
            devices = sys_root / "devices"
            loop = devices / "virtual" / "block" / "loop42"
            loop_loop = loop / "loop"
            loop_loop.mkdir(parents=True)
            dev_block.mkdir(parents=True)
            (loop / "dev").write_text("7:42\n", encoding="ascii")
            (loop_loop / "backing_file").write_text("/run/private/image.ext4\n", encoding="ascii")
            (dev_block / "7:42").symlink_to(loop)

            with (
                mock.patch.object(backup_drill, "SCRATCH_SYS_DEV_BLOCK_ROOT", dev_block),
                mock.patch.object(backup_drill, "SCRATCH_SYS_DEVICES_ROOT", devices),
                mock.patch.object(
                    backup_drill,
                    "_scratch_mountinfo_for",
                    return_value=("/sys", "0:1", "sysfs", "sysfs", "/", "ro,nosuid,nodev"),
                ),
            ):
                reason, predicates = backup_drill._scratch_sysfs_loop_device_predicates("7:42")
                self.assertIsNone(reason)
                self.assertEqual(predicates, {
                    "device_major_minor_valid": True,
                    "sysfs_mount_verified": True,
                    "sysfs_device_resolved": True,
                    "sysfs_loop_device": True,
                    "sysfs_device_number_matches": True,
                    "sysfs_backing_file_present": True,
                })
                for malformed in ("7:042", "7/42", "7:42 "):
                    with self.subTest(device=malformed):
                        malformed_reason, malformed_predicates = (
                            backup_drill._scratch_sysfs_loop_device_predicates(malformed)
                        )
                        self.assertEqual(malformed_reason, "device_major_minor_invalid")
                        self.assertFalse(malformed_predicates["device_major_minor_valid"])

                (dev_block / "7:43").symlink_to(loop)
                mismatch_reason, mismatch = backup_drill._scratch_sysfs_loop_device_predicates("7:43")
                self.assertEqual(mismatch_reason, "sysfs_device_number_mismatch")
                self.assertTrue(mismatch["sysfs_loop_device"])
                self.assertFalse(mismatch["sysfs_device_number_matches"])

                not_loop = devices / "virtual" / "block" / "sda"
                not_loop.mkdir(parents=True)
                (not_loop / "dev").write_text("8:0\n", encoding="ascii")
                (dev_block / "8:0").symlink_to(not_loop)
                nonloop_reason, nonloop = backup_drill._scratch_sysfs_loop_device_predicates("8:0")
                self.assertEqual(nonloop_reason, "sysfs_not_loop_device")
                self.assertTrue(nonloop["sysfs_device_resolved"])
                self.assertFalse(nonloop["sysfs_loop_device"])

                (dev_block / "9:0").symlink_to(devices / "missing" / "loop9")
                missing_reason, missing = backup_drill._scratch_sysfs_loop_device_predicates("9:0")
                self.assertEqual(missing_reason, "sysfs_device_unavailable")
                self.assertFalse(missing["sysfs_device_resolved"])

                (loop_loop / "backing_file").unlink()
                no_backing_reason, no_backing = backup_drill._scratch_sysfs_loop_device_predicates("7:42")
                self.assertEqual(no_backing_reason, "sysfs_not_loop_device")
                self.assertTrue(no_backing["sysfs_device_number_matches"])
                self.assertFalse(no_backing["sysfs_backing_file_present"])

            with (
                mock.patch.object(backup_drill, "SCRATCH_SYS_DEV_BLOCK_ROOT", dev_block),
                mock.patch.object(backup_drill, "SCRATCH_SYS_DEVICES_ROOT", devices),
                mock.patch.object(
                    backup_drill,
                    "_scratch_mountinfo_for",
                    return_value=("/sys", "0:1", "tmpfs", "tmpfs", "/", "ro"),
                ),
            ):
                reason, predicates = backup_drill._scratch_sysfs_loop_device_predicates("7:42")
                self.assertEqual(reason, "sysfs_mount_unverified")
                self.assertFalse(predicates["sysfs_mount_verified"])

    def test_process_image_mount_accepts_source_alias_only_with_exact_loop_join(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            mountpoint = pathlib.Path(temporary_dir) / "scratch"
            mountpoint.mkdir(mode=0o700)
            image_path = pathlib.Path(temporary_dir) / "image.ext4"
            image_path.write_bytes(b"synthetic fixed image")
            info = image_path.stat()
            identity = (info.st_dev, info.st_ino, info.st_size, info.st_uid, info.st_gid, 0o600, 1)
            loop_matches = [("loop42", "7:42")]
            mountinfo = (
                "1 0 7:42 / "
                + str(mountpoint)
                + " rw,nosuid,nodev,noexec,relatime - ext4 /dev/disk/by-id/private-alias rw\n"
            )
            with (
                mock.patch.object(backup_drill, "_scratch_image_path_identity", return_value=identity),
                mock.patch.object(backup_drill, "_scratch_image_loop_matches", return_value=loop_matches),
                mock.patch.object(pathlib.Path, "read_text", return_value=mountinfo),
                mock.patch.object(backup_drill, "_scratch_mountpoint_is_mounted", return_value=False),
            ):
                self.assertEqual(
                    backup_drill._scratch_verify_process_image_mount(
                        pid="123", mountpoint=mountpoint, image_path=image_path, image_identity=identity
                    ),
                    "7:42",
                )
                with mock.patch.object(
                    backup_drill, "_scratch_image_loop_matches", return_value=[("loop43", "7:43")]
                ):
                    with self.assertRaisesRegex(RuntimeError, "exact loop-backed"):
                        backup_drill._scratch_verify_process_image_mount(
                            pid="123", mountpoint=mountpoint, image_path=image_path, image_identity=identity
                        )
                with mock.patch.object(backup_drill, "_scratch_image_loop_matches", return_value=[]):
                    with self.assertRaisesRegex(RuntimeError, "association is not unique"):
                        backup_drill._scratch_verify_process_image_mount(
                            pid="123", mountpoint=mountpoint, image_path=image_path, image_identity=identity
                        )

    def test_scratch_mountinfo_matches_root_and_projects_missing_row(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            target = pathlib.Path(temporary_dir) / "scratch"
            target.mkdir()
            original_read_text = pathlib.Path.read_text
            root_row = "1 0 0:1 / / rw - ext4 /dev/root rw\n"

            def read_root(path: pathlib.Path, *args, **kwargs) -> str:
                if str(path) == "/proc/self/mountinfo":
                    return root_row
                return original_read_text(path, *args, **kwargs)

            with mock.patch.object(pathlib.Path, "read_text", read_root):
                self.assertEqual(
                    backup_drill._scratch_mountinfo_for(target),
                    ("/", "0:1", "ext4", "/dev/root", "/", "rw"),
                )

            nonmatching_row = "1 0 0:1 / /elsewhere rw - ext4 /dev/other rw\n"

            def read_nonmatching(path: pathlib.Path, *args, **kwargs) -> str:
                if str(path) == "/proc/self/mountinfo":
                    return nonmatching_row
                return original_read_text(path, *args, **kwargs)

            with mock.patch.object(pathlib.Path, "read_text", read_nonmatching):
                with self.assertRaises(RuntimeError) as failure:
                    backup_drill._scratch_mountinfo_for(target)
            self.assertEqual(
                failure.exception.scratch_disk_mount_failure_reason,
                "mount_row_missing",
            )

    def test_scratch_interpreter_identity_failure_names_exact_stat_operation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = pathlib.Path(temporary_dir)
            executable = root / "python"
            executable.write_bytes(b"python")
            mountpoint = root / "mount"
            image = root / "image.ext4"
            image.write_bytes(b"image")
            unit_name = "oldsparky-backup-restore-" + ("a" * 24) + ".service"
            control_group = f"/system.slice/{unit_name}"
            properties = {
                "MemoryMax": str(backup_drill.SCRATCH_MEMORY_MAX_BYTES),
                "MemorySwapMax": "0",
                "OOMPolicy": "kill",
                "PrivateNetwork": "yes",
                "RestrictAddressFamilies": "AF_UNIX",
                "ProtectSystem": "strict",
                "ProtectHome": "tmpfs",
                "NoNewPrivileges": "yes",
                "ReadWritePaths": "",
                "LoadState": "loaded",
                "ActiveState": "active",
                "ControlGroup": control_group,
                "ExecMainPID": "4242",
                "MountImages": f"{image}:{mountpoint}:",
                "TemporaryFileSystem": "",
                "InaccessiblePaths": " ".join(backup_drill._SCRATCH_INACCESSIBLE_PATHS),
                "BindReadOnlyPaths": " ".join(
                    (
                        str(mountpoint / "platform_backup_restore_drill.py"),
                        str(mountpoint / "platform_disk_policy.py"),
                    )
                ),
            }
            original_read_text = pathlib.Path.read_text
            original_stat = pathlib.Path.stat

            def read_text(path: pathlib.Path, *args, **kwargs) -> str:
                if str(path) == "/proc/4242/cgroup":
                    return f"0::{control_group}\n"
                return original_read_text(path, *args, **kwargs)

            def run_case(failing_path: pathlib.Path) -> str:
                def stat(path: pathlib.Path, *args, **kwargs) -> os.stat_result:
                    if path == failing_path:
                        raise FileNotFoundError(2, "private", str(path))
                    if str(path) == "/proc/4242/exe":
                        return original_stat(executable, *args, **kwargs)
                    return original_stat(path, *args, **kwargs)

                with (
                    mock.patch.object(pathlib.Path, "read_text", read_text),
                    mock.patch.object(pathlib.Path, "stat", stat),
                ):
                    with self.assertRaises(RuntimeError) as failure:
                        backup_drill._scratch_expected_properties(
                            {"properties": properties},
                            unit_name=unit_name,
                            mountpoint=mountpoint,
                            python_binary=executable,
                            authorization_probe=True,
                            scratch_backend=backup_drill.SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1,
                            scratch_image_path=image,
                        )
                site = backup_drill._scratch_verification_site(failure.exception)
                self.assertNotIn(str(failing_path), str(failure.exception))
                return site

            self.assertEqual(run_case(executable), "expected_executable_stat")
            self.assertEqual(run_case(pathlib.Path("/proc/4242/exe")), "proc_exe_stat")

    def test_unexpected_identity_read_failure_survives_private_artifact_projection(self) -> None:
        mountpoint = pathlib.Path("/run/oldsparky-backup-restore-" + ("d" * 24))
        unit_name = mountpoint.name + ".service"
        control_group = f"/system.slice/{unit_name}"
        executable = pathlib.Path(sys.executable).resolve(strict=True)
        properties = {
            "MemoryMax": str(backup_drill.SCRATCH_MEMORY_MAX_BYTES),
            "MemorySwapMax": "0",
            "OOMPolicy": "kill",
            "PrivateNetwork": "yes",
            "RestrictAddressFamilies": "AF_UNIX",
            "ProtectSystem": "strict",
            "ProtectHome": "tmpfs",
            "NoNewPrivileges": "yes",
            "ReadWritePaths": "",
            "LoadState": "loaded",
            "ActiveState": "active",
            "ControlGroup": control_group,
            "ExecMainPID": "4242",
            "TemporaryFileSystem": f"{mountpoint}:rw,nosuid,nodev,noexec,size=512M",
            "MountImages": "",
            "InaccessiblePaths": " ".join(backup_drill._SCRATCH_INACCESSIBLE_PATHS),
            "BindReadOnlyPaths": " ".join(
                (
                    str(mountpoint / "platform_backup_restore_drill.py"),
                    str(mountpoint / "platform_disk_policy.py"),
                )
            ),
        }
        original_read_text = pathlib.Path.read_text

        def unexpected_proc_parse(path: pathlib.Path, *args: Any, **kwargs: Any) -> str:
            if str(path) == "/proc/4242/cgroup":
                raise AttributeError("private proc detail must not enter the diagnostic frame")
            return original_read_text(path, *args, **kwargs)

        with mock.patch.object(pathlib.Path, "read_text", unexpected_proc_parse):
            with self.assertRaises(AttributeError) as failure:
                backup_drill._scratch_expected_properties(
                    {"properties": properties},
                    unit_name=unit_name,
                    mountpoint=mountpoint,
                    python_binary=executable,
                    authorization_probe=True,
                )

        error = failure.exception
        self.assertEqual(
            backup_drill._scratch_verification_site(error),
            "process_cgroup_read",
        )
        self.assertEqual(backup_drill._scratch_verification_error_details(error), ("other", None))
        error.scratch_authorization_probe = True
        first_fault = backup_drill._scratch_first_fault(
            error,
            origin="controller",
            site="controller_identity",
        )
        self.assertEqual(
            first_fault,
            {
                "schema": 1,
                "origin": "controller",
                "site": "controller_identity",
                "exception_kind": "attribute",
                "errno": None,
                "verification_site": "process_cgroup_read",
            },
        )
        error.scratch_first_fault = first_fault
        error.scratch_unit_diagnostic = backup_drill._scratch_unit_diagnostic(
            {"properties": {"ActiveState": "active", "SubState": "running", "Result": "success"}},
            launcher_exit_status=1,
            stdout_prefix=b"",
            stdout_observed=0,
            stdout_complete=True,
            stderr_prefix=b"",
            stderr_observed=0,
            stderr_complete=True,
            phase="confinement_verification_failed",
            verification_error="other",
            verification_site="process_cgroup_read",
        )
        error.scratch_cleanup_status = "confirmed"
        report = backup_drill.failure_report_for_exception(
            error,
            private_scratch_diagnostics=True,
        )
        self.assertEqual(report["scratch_first_fault"], first_fault)
        self.assertEqual(
            report["scratch_unit_diagnostic"]["verification_site"],
            first_fault["verification_site"],
        )
        frame = backup_drill.private_failure_frame_for_exception(error, enabled=True)
        self.assertIsNotNone(frame)
        assert frame is not None
        self.assertNotIn("private proc detail", frame)
        self.assertNotIn("/proc/4242", frame)
        parsed = backup_drill._parse_bound_scratch_first_fault(
            (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            frame.encode("ascii"),
            stdout_truncated=False,
            stderr_truncated=False,
        )
        self.assertEqual(parsed, first_fault)

        poll_error = OSError(5, "synthetic active poll read failure")
        poll_error.scratch_verification_site = "active_poll_stream_read"
        poll_fault = backup_drill._scratch_first_fault(
            poll_error,
            origin="controller",
            site="controller_active_poll",
        )
        self.assertEqual(
            poll_fault,
            {
                "schema": 1,
                "origin": "controller",
                "site": "controller_active_poll",
                "exception_kind": "os_error",
                "errno": 5,
                "verification_site": "active_poll_stream_read",
            },
        )
        assert poll_fault is not None
        poll_error.scratch_authorization_probe = True
        poll_error.scratch_first_fault = poll_fault
        poll_error.scratch_unit_diagnostic = backup_drill._scratch_unit_diagnostic(
            {"properties": {"ActiveState": "active", "SubState": "running", "Result": "success"}},
            launcher_exit_status=1,
            stdout_prefix=b"",
            stdout_observed=0,
            stdout_complete=True,
            stderr_prefix=b"",
            stderr_observed=0,
            stderr_complete=True,
            phase="authorization_failed",
            verification_error="os_error",
            verification_errno=5,
            verification_site="active_poll_stream_read",
            worker_frame_status="missing",
            terminal_snapshot={
                "load_state": "loaded",
                "active_state": "inactive",
                "sub_state": "dead",
                "result": "exit-code",
                "exec_main_status": 1,
                "exec_main_code": "exited",
                "start_token_sent": True,
                "completion_token_sent": False,
            },
        )
        self.assertTrue(
            backup_drill.valid_scratch_unit_diagnostic(poll_error.scratch_unit_diagnostic)
        )
        poll_error.scratch_cleanup_status = "confirmed"
        poll_report = backup_drill.failure_report_for_exception(
            poll_error,
            private_scratch_diagnostics=True,
        )
        self.assertEqual(poll_report["scratch_first_fault"], poll_fault)
        self.assertEqual(
            poll_report["scratch_unit_diagnostic"]["worker_frame_status"], "missing"
        )
        poll_frame = backup_drill.private_failure_frame_for_exception(
            poll_error,
            enabled=True,
        )
        self.assertIsNotNone(poll_frame)
        assert poll_frame is not None
        poll_parsed = backup_drill._parse_bound_scratch_first_fault(
            (json.dumps(poll_report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            poll_frame.encode("ascii"),
            stdout_truncated=False,
            stderr_truncated=False,
        )
        self.assertEqual(poll_parsed, poll_fault)
        self.assertIsNone(
            backup_drill._scratch_first_fault(
                type("Err", (RuntimeError,), {"scratch_verification_site": "active_poll_stream_read"})(),
                origin="controller",
                site="controller_identity",
            )
        )
        malformed_poll_unit = dict(poll_error.scratch_unit_diagnostic)
        malformed_poll_unit["worker_frame_status"] = "not_started"
        self.assertFalse(backup_drill.valid_scratch_unit_diagnostic(malformed_poll_unit))
        prestart_output = backup_drill._scratch_unit_diagnostic(
            {
                "properties": {
                    "ActiveState": "failed",
                    "SubState": "failed",
                    "Result": "exit-code",
                    "ExecMainStatus": "203",
                }
            },
            launcher_exit_status=1,
            stdout_prefix=b"pre-start launcher output",
            stdout_observed=len(b"pre-start launcher output"),
            stdout_complete=True,
            stderr_prefix=b"",
            stderr_observed=0,
            stderr_complete=True,
            phase="unit_exited_before_confinement",
            worker_frame_status="not_started",
            terminal_snapshot={
                "load_state": "loaded",
                "active_state": "failed",
                "sub_state": "failed",
                "result": "exit-code",
                "exec_main_status": 203,
                "exec_main_code": "exited",
                "start_token_sent": False,
                "completion_token_sent": False,
            },
        )
        self.assertIsNotNone(prestart_output)
        self.assertTrue(backup_drill.valid_scratch_unit_diagnostic(prestart_output))
        assert prestart_output is not None
        self.assertEqual(prestart_output["stdout_bytes"], len(b"pre-start launcher output"))
        started_without_frame = dict(prestart_output)
        started_without_frame["terminal_snapshot"] = {
            **prestart_output["terminal_snapshot"],
            "start_token_sent": True,
        }
        self.assertFalse(backup_drill.valid_scratch_unit_diagnostic(started_without_frame))

    def test_scratch_disk_image_admission_reserves_floor_lead_and_full_image(self) -> None:
        total_bytes = 100 * 1024**3
        required = backup_drill._scratch_disk_image_required_free_bytes(total_bytes)
        self.assertEqual(
            required,
            backup_drill._required_disk_floor(total_bytes)
            + backup_drill.RESTORE_DISK_LEAD_BYTES
            + backup_drill.SCRATCH_DISK_IMAGE_BYTES,
        )
        self.assertEqual(
            required - backup_drill._required_disk_floor(total_bytes)
            - backup_drill.RESTORE_DISK_LEAD_BYTES,
            576 * 1024**2,
        )
        with self.assertRaises(backup_drill.RestoreGuardStop):
            backup_drill._scratch_disk_image_required_free_bytes(0)

    def test_scratch_disk_image_builder_preallocates_before_fixed_mkfs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            temporary_root = pathlib.Path(temporary_dir)
            image_root = temporary_root / "images"
            parent_identity = temporary_root.stat()
            total_bytes = 100 * 1024**3
            required = (
                backup_drill._required_disk_floor(total_bytes)
                + backup_drill.RESTORE_DISK_LEAD_BYTES
                + 8192
            )
            snapshot = types.SimpleNamespace(
                valid=True,
                total_bytes=total_bytes,
                free_bytes=required + 1024**3,
            )
            real_fstat = os.fstat
            real_lstat = pathlib.Path.lstat
            real_posix_fallocate = os.posix_fallocate
            allocation_calls: list[tuple[int, int, int, int]] = []
            formatter_events: list[str] = []

            def root_owned_lstat(path: pathlib.Path) -> os.stat_result:
                if path in {temporary_root, image_root}:
                    return types.SimpleNamespace(
                        st_mode=stat.S_IFDIR | 0o700,
                        st_uid=0,
                        st_gid=0,
                        st_dev=parent_identity.st_dev,
                        st_ino=parent_identity.st_ino if path == temporary_root else 99,
                    )  # type: ignore[return-value]
                actual = real_lstat(path)
                if path == image_root / "restore-0123456789abcdef01234567.ext4":
                    return types.SimpleNamespace(
                        st_mode=actual.st_mode,
                        st_uid=0,
                        st_gid=0,
                        st_dev=actual.st_dev,
                        st_ino=actual.st_ino,
                        st_size=actual.st_size,
                        st_nlink=1,
                    )  # type: ignore[return-value]
                return actual

            def root_owned_fstat(descriptor: int) -> types.SimpleNamespace:
                actual = real_fstat(descriptor)
                return types.SimpleNamespace(
                    st_mode=actual.st_mode,
                    st_uid=0,
                    st_gid=0,
                    st_dev=actual.st_dev,
                    st_ino=actual.st_ino,
                    st_size=actual.st_size,
                    st_nlink=1,
                    st_blocks=actual.st_blocks,
                )

            def simulated_lazy_inode_zeroout(
                argv: list[str], **_kwargs: object
            ) -> subprocess.CompletedProcess[bytes]:
                # Model a formatter zeroout path that punches the second
                # filesystem block while preserving an already written block.
                formatter_events.append("mkfs")
                with pathlib.Path(argv[-1]).open("r+b") as image_file:
                    image_file.truncate(0)
                    image_file.truncate(8192)
                    image_file.write(b"mkfs-sentinel")
                return subprocess.CompletedProcess(argv, 0)

            def record_exact_inode_allocation(
                descriptor: int, offset: int, length: int
            ) -> None:
                info = os.fstat(descriptor)
                allocation_calls.append((info.st_dev, info.st_ino, offset, length))
                formatter_events.append("fallocate")
                real_posix_fallocate(descriptor, offset, length)

            with (
                mock.patch.object(backup_drill, "SCRATCH_DISK_IMAGE_ROOT", image_root),
                mock.patch.object(backup_drill, "SCRATCH_DISK_IMAGE_BYTES", 8192),
                mock.patch.object(backup_drill.os, "geteuid", return_value=0),
                mock.patch.object(
                    backup_drill,
                    "_scratch_mountinfo_for",
                    return_value=("/", "8:1", "ext4", "/dev/sda1", "/", "rw,relatime"),
                ),
                mock.patch.object(backup_drill, "snapshot_for_path", return_value=snapshot),
                mock.patch.object(
                    backup_drill.subprocess,
                    "run",
                    side_effect=simulated_lazy_inode_zeroout,
                ) as mkfs,
                mock.patch.object(
                    backup_drill.os,
                    "posix_fallocate",
                    side_effect=record_exact_inode_allocation,
                ),
                mock.patch.object(pathlib.Path, "lstat", root_owned_lstat),
                mock.patch.object(backup_drill.os, "fstat", side_effect=root_owned_fstat),
            ):
                image_path, identity = backup_drill._scratch_prepare_disk_image(
                    "0123456789abcdef01234567"
                )
            self.assertEqual(image_path.parent, image_root)
            self.assertEqual(identity[2], 8192)
            self.assertEqual(image_path.stat().st_blocks * 512, 8192)
            self.assertEqual(image_path.read_bytes()[:13], b"mkfs-sentinel")
            self.assertEqual(
                allocation_calls,
                [
                    (image_path.stat().st_dev, image_path.stat().st_ino, 0, 8192),
                    (image_path.stat().st_dev, image_path.stat().st_ino, 0, 8192),
                ],
            )
            self.assertEqual(formatter_events, ["fallocate", "mkfs", "fallocate"])
            self.assertEqual(
                mkfs.call_args.args[0],
                [
                    "/usr/sbin/mkfs.ext4", "-F", "-q", "-m", "0",
                    "-E", "nodiscard,lazy_itable_init=0", str(image_path),
                ],
            )
            self.assertTrue(mkfs.call_args.kwargs["check"])
            self.assertEqual(mkfs.call_args.kwargs["timeout"], 30)

            image_path.unlink()

            with (
                mock.patch.object(backup_drill, "SCRATCH_DISK_IMAGE_ROOT", image_root),
                mock.patch.object(backup_drill, "SCRATCH_DISK_IMAGE_BYTES", 8192),
                mock.patch.object(backup_drill.os, "geteuid", return_value=0),
                mock.patch.object(
                    backup_drill,
                    "_scratch_mountinfo_for",
                    return_value=("/", "8:1", "ext4", "/dev/sda1", "/", "rw,relatime"),
                ),
                mock.patch.object(backup_drill, "snapshot_for_path", return_value=snapshot),
                mock.patch.object(
                    backup_drill.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0),
                ),
                mock.patch.object(
                    backup_drill.os,
                    "posix_fallocate",
                    side_effect=OSError(28, "synthetic allocation failure"),
                ),
                mock.patch.object(pathlib.Path, "lstat", root_owned_lstat),
                mock.patch.object(backup_drill.os, "fstat", side_effect=root_owned_fstat),
                self.assertRaises(OSError),
            ):
                backup_drill._scratch_prepare_disk_image("0123456789abcdef01234567")
            self.assertFalse(image_path.exists())
            self.assertFalse(image_path.exists())

    def test_disk_image_setup_collision_is_reported_as_not_created(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            temporary_root = pathlib.Path(temporary_dir)
            (temporary_root / "platform").mkdir()
            image_root = temporary_root / "images"
            token = "0123456789abcdef01234567"
            image_path = image_root / f"restore-{token}.ext4"
            image_root.mkdir(mode=0o700)
            image_path.write_bytes(b"preexisting protected inode")
            image_path.chmod(0o600)
            original_bytes = image_path.read_bytes()
            original_identity = image_path.stat().st_dev, image_path.stat().st_ino
            total_bytes = 100 * 1024**3
            snapshot = types.SimpleNamespace(
                valid=True,
                total_bytes=total_bytes,
                free_bytes=(
                    backup_drill._required_disk_floor(total_bytes)
                    + backup_drill.RESTORE_DISK_LEAD_BYTES
                    + backup_drill.SCRATCH_DISK_IMAGE_BYTES
                    + 1024**3
                ),
            )
            first_fault = {
                "schema": 1,
                "origin": "controller",
                "site": "controller_unit_setup",
                "exception_kind": "runtime",
                "errno": None,
            }
            real_lstat = pathlib.Path.lstat
            root_lstat_paths: list[pathlib.Path] = []

            def root_owned_parent_lstat(path: pathlib.Path) -> os.stat_result:
                actual = real_lstat(path)
                if path in {temporary_root, image_root}:
                    root_lstat_paths.append(path)
                    return types.SimpleNamespace(
                        st_mode=stat.S_IFDIR | 0o700,
                        st_uid=0,
                        st_gid=0,
                        st_dev=actual.st_dev,
                        st_ino=actual.st_ino,
                    )  # type: ignore[return-value]
                return actual

            def unsafe_parent_lstat(path: pathlib.Path) -> os.stat_result:
                actual = real_lstat(path)
                if path == temporary_root:
                    return types.SimpleNamespace(
                        st_mode=stat.S_IFDIR | 0o700,
                        st_uid=1234,
                        st_gid=1234,
                        st_dev=actual.st_dev,
                        st_ino=actual.st_ino,
                    )  # type: ignore[return-value]
                return actual

            with (
                mock.patch.object(backup_drill, "SCRATCH_DISK_IMAGE_ROOT", image_root),
                mock.patch.object(backup_drill.os, "geteuid", return_value=0),
                mock.patch.object(
                    backup_drill,
                    "_scratch_mountinfo_for",
                    return_value=("/", "8:1", "ext4", "/dev/sda1", "/", "rw,relatime"),
                ),
                mock.patch.object(backup_drill, "snapshot_for_path", return_value=snapshot),
                mock.patch.object(pathlib.Path, "lstat", root_owned_parent_lstat),
                mock.patch.object(backup_drill.os, "open", wraps=os.open) as open_spy,
            ):
                with self.assertRaises(FileExistsError) as raised:
                    backup_drill._scratch_prepare_disk_image(token)
            open_spy.assert_called_once()
            self.assertEqual(open_spy.call_args.args[0], image_path)
            self.assertTrue(open_spy.call_args.args[1] & os.O_EXCL)
            self.assertEqual(set(root_lstat_paths), {temporary_root, image_root})
            failure = raised.exception
            self.assertFalse(failure.scratch_image_created_by_invocation)
            self.assertIsNone(failure.scratch_image_unlinked)
            self.assertIsNone(failure.scratch_loop_released)
            self.assertIsNone(failure.scratch_retention_receipt_written)
            self.assertEqual(image_path.read_bytes(), original_bytes)
            self.assertEqual((image_path.stat().st_dev, image_path.stat().st_ino), original_identity)

            with (
                mock.patch.object(backup_drill, "SCRATCH_DISK_IMAGE_ROOT", image_root),
                mock.patch.object(backup_drill.os, "geteuid", return_value=0),
                mock.patch.object(
                    backup_drill,
                    "_scratch_mountinfo_for",
                    return_value=("/", "8:1", "ext4", "/dev/sda1", "/", "rw,relatime"),
                ),
                mock.patch.object(backup_drill, "snapshot_for_path", return_value=snapshot),
                mock.patch.object(pathlib.Path, "lstat", unsafe_parent_lstat),
                mock.patch.object(backup_drill.os, "open", wraps=os.open) as unsafe_open_spy,
            ):
                with self.assertRaisesRegex(RuntimeError, "parent directory identity is unsafe"):
                    backup_drill._scratch_prepare_disk_image(token)
            unsafe_open_spy.assert_not_called()
            self.assertEqual(image_path.read_bytes(), original_bytes)
            self.assertEqual((image_path.stat().st_dev, image_path.stat().st_ino), original_identity)

            failure.scratch_first_fault = first_fault
            backup_drill._scratch_cleanup_failure(
                failure,
                [],
                authorization_probe=False,
                service_stopped=True,
                mount_absent=True,
                mountpoint_removed=True,
                image_created_by_invocation=failure.scratch_image_created_by_invocation,
                image_removed=failure.scratch_image_unlinked,
                loop_released=failure.scratch_loop_released,
                retention_receipt_written=failure.scratch_retention_receipt_written,
                protected_paths_cleaned=True,
            )
            self.assertEqual(failure.scratch_cleanup_status, "confirmed")
            self.assertEqual(
                failure.scratch_cleanup_diagnostic,
                {
                    "schema": 1,
                    "service_stopped": True,
                    "mount_absent": True,
                    "mountpoint_removed": True,
                    "loop_released": None,
                    "image_created_by_invocation": False,
                    "image_unlinked": None,
                    "retention_receipt_written": None,
                    "protected_paths_cleaned": True,
                    "cleanup_status": "confirmed",
                },
            )
            self.assertTrue(
                backup_drill.valid_scratch_cleanup_diagnostic(
                    failure.scratch_cleanup_diagnostic
                )
            )
            with mock.patch.object(sys.modules[__name__], "REPO_ROOT", temporary_root):
                artifact_path = _write_synthetic_restore_failure_artifact(
                    failure,
                    source_sha="f" * 40,
                    scratch_backend=backup_drill.SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1,
                )
            artifact = json.loads(artifact_path.read_text(encoding="ascii"))
            self.assertEqual(artifact["scratch_cleanup_status"], "confirmed")
            self.assertEqual(
                artifact["scratch_cleanup_diagnostic"]["image_created_by_invocation"],
                False,
            )
            self.assertEqual(image_path.read_bytes(), original_bytes)
            image_path.unlink()
            artifact_path.unlink()

            real_lstat = pathlib.Path.lstat

            def root_owned_lstat(path: pathlib.Path) -> os.stat_result:
                actual = real_lstat(path)
                if path in {temporary_root, image_root} or path == image_path:
                    return types.SimpleNamespace(
                        st_mode=(stat.S_IFREG if path == image_path else stat.S_IFDIR)
                        | (0o600 if path == image_path else 0o700),
                        st_uid=0,
                        st_gid=0,
                        st_dev=actual.st_dev,
                        st_ino=actual.st_ino,
                        st_size=actual.st_size,
                        st_nlink=1,
                    )  # type: ignore[return-value]
                return actual

            receipt_writer = mock.patch.object(
                backup_drill, "_scratch_write_image_cleanup_receipt"
            )
            with (
                mock.patch.object(backup_drill, "SCRATCH_DISK_IMAGE_ROOT", image_root),
                mock.patch.object(backup_drill, "SCRATCH_DISK_IMAGE_BYTES", 4096),
                mock.patch.object(backup_drill.os, "geteuid", return_value=0),
                mock.patch.object(
                    backup_drill,
                    "_scratch_mountinfo_for",
                    return_value=("/", "8:1", "ext4", "/dev/sda1", "/", "rw,relatime"),
                ),
                mock.patch.object(backup_drill, "snapshot_for_path", return_value=snapshot),
                mock.patch.object(pathlib.Path, "lstat", root_owned_lstat),
                mock.patch.object(backup_drill.os, "fstat", side_effect=OSError("private")),
                receipt_writer as write_receipt,
            ):
                with self.assertRaises(OSError) as raised_identity:
                    backup_drill._scratch_prepare_disk_image(token)
            identity_failure = raised_identity.exception
            self.assertTrue(identity_failure.scratch_image_created_by_invocation)
            self.assertIsNone(identity_failure.scratch_image_unlinked)
            self.assertTrue(identity_failure.scratch_loop_released)
            write_receipt.assert_called_once()
            self.assertTrue(identity_failure.scratch_retention_receipt_written)
            self.assertTrue(image_path.is_file())
            self.assertEqual(image_path.stat().st_size, 0)

            identity_failure.scratch_first_fault = first_fault
            backup_drill._scratch_cleanup_failure(
                identity_failure,
                [],
                authorization_probe=False,
                service_stopped=True,
                mount_absent=True,
                mountpoint_removed=True,
                image_created_by_invocation=True,
                image_removed=None,
                loop_released=True,
                retention_receipt_written=True,
                protected_paths_cleaned=True,
            )
            self.assertEqual(identity_failure.scratch_cleanup_status, "unknown")
            self.assertTrue(
                backup_drill.valid_scratch_cleanup_diagnostic(
                    identity_failure.scratch_cleanup_diagnostic
                )
            )
            with mock.patch.object(sys.modules[__name__], "REPO_ROOT", temporary_root):
                identity_artifact_path = _write_synthetic_restore_failure_artifact(
                    identity_failure,
                    source_sha="a" * 40,
                    scratch_backend=backup_drill.SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1,
                )
            identity_artifact = json.loads(
                identity_artifact_path.read_text(encoding="ascii")
            )
            self.assertEqual(identity_artifact["scratch_cleanup_status"], "unknown")
            self.assertTrue(
                identity_artifact["scratch_cleanup_diagnostic"][
                    "image_created_by_invocation"
                ]
            )
            self.assertIsNone(
                identity_artifact["scratch_cleanup_diagnostic"]["image_unlinked"]
            )
            self.assertTrue(image_path.is_file())
            identity_artifact_path.unlink()
            image_path.unlink()

    def test_scratch_disk_image_identity_rejects_sparse_or_replaced_inode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            image_path = pathlib.Path(temporary_dir) / "image.ext4"
            with mock.patch.object(backup_drill, "SCRATCH_DISK_IMAGE_BYTES", 4096):
                image_path.write_bytes(b"x" * 4096)
                image_path.chmod(0o600)
                real_fstat = os.fstat
                real_lstat = pathlib.Path.lstat

                def root_owned_file_stat(descriptor: int) -> types.SimpleNamespace:
                    actual = real_fstat(descriptor)
                    return types.SimpleNamespace(
                        st_mode=actual.st_mode,
                        st_dev=actual.st_dev,
                        st_ino=actual.st_ino,
                        st_size=actual.st_size,
                        st_uid=0,
                        st_gid=0,
                        st_nlink=1,
                        st_blocks=actual.st_blocks,
                    )

                def root_owned_path_lstat(path: pathlib.Path) -> os.stat_result:
                    actual = real_lstat(path)
                    if path == image_path.parent:
                        return types.SimpleNamespace(
                            st_mode=stat.S_IFDIR | 0o700,
                            st_dev=actual.st_dev,
                            st_ino=actual.st_ino,
                            st_uid=0,
                            st_gid=0,
                        )  # type: ignore[return-value]
                    return types.SimpleNamespace(
                        st_mode=actual.st_mode,
                        st_dev=actual.st_dev,
                        st_ino=actual.st_ino,
                        st_size=actual.st_size,
                        st_uid=0,
                        st_gid=0,
                        st_nlink=1,
                    ) if path == image_path else actual  # type: ignore[return-value]

                with (
                    mock.patch.object(backup_drill, "SCRATCH_DISK_IMAGE_ROOT", image_path.parent),
                    mock.patch.object(backup_drill.os, "fstat", side_effect=root_owned_file_stat),
                    mock.patch.object(pathlib.Path, "lstat", root_owned_path_lstat),
                ):
                    identity = backup_drill._scratch_image_path_identity(image_path)
                self.assertEqual(identity[2], 4096)
                image_path.unlink()
                image_path.touch(mode=0o600)
                image_path.chmod(0o600)
                with image_path.open("r+b") as handle:
                    handle.truncate(4096)
                with (
                    mock.patch.object(backup_drill, "SCRATCH_DISK_IMAGE_ROOT", image_path.parent),
                    mock.patch.object(backup_drill.os, "fstat", side_effect=root_owned_file_stat),
                    mock.patch.object(pathlib.Path, "lstat", root_owned_path_lstat),
                    self.assertRaisesRegex(RuntimeError, "fixed allocation contract"),
                ):
                    backup_drill._scratch_image_path_identity(image_path)
                image_path.unlink()
                image_path.write_bytes(b"y" * 4096)
                image_path.chmod(0o644)
                with (
                    mock.patch.object(backup_drill, "SCRATCH_DISK_IMAGE_ROOT", image_path.parent),
                    mock.patch.object(backup_drill.os, "fstat", side_effect=root_owned_file_stat),
                    mock.patch.object(pathlib.Path, "lstat", root_owned_path_lstat),
                    self.assertRaisesRegex(RuntimeError, "fixed allocation contract"),
                ):
                    backup_drill._scratch_image_path_identity(image_path)

    def test_scratch_disk_root_transition_checks_owner_mode_identity_and_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = pathlib.Path(temporary_dir) / "scratch"
            root.mkdir(mode=0o700)
            real_lstat = pathlib.Path.lstat
            before = types.SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o700, st_uid=0, st_gid=0, st_dev=7, st_ino=11
            )
            after = types.SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o700, st_uid=1234, st_gid=5678, st_dev=7, st_ino=11
            )
            lstat_values = [before, after]

            def staged_lstat(path: pathlib.Path) -> os.stat_result:
                if path == root and lstat_values:
                    return lstat_values.pop(0)  # type: ignore[return-value]
                return real_lstat(path)

            with (
                mock.patch.object(pathlib.Path, "lstat", staged_lstat),
                mock.patch.object(backup_drill.os, "chown") as chown,
            ):
                backup_drill._scratch_prepare_disk_root_for_postgres(
                    root, postgres_uid=1234, postgres_gid=5678
                )
            chown.assert_called_once_with(root, 1234, 5678, follow_symlinks=False)

            bad_preconditions = (
                types.SimpleNamespace(
                    st_mode=stat.S_IFDIR | 0o711, st_uid=0, st_gid=0, st_dev=7, st_ino=11
                ),
                types.SimpleNamespace(
                    st_mode=stat.S_IFDIR | 0o700, st_uid=0, st_gid=9, st_dev=7, st_ino=11
                ),
            )
            for bad_before in bad_preconditions:
                with (
                    self.subTest(bad_before=bad_before),
                    mock.patch.object(pathlib.Path, "lstat", return_value=bad_before),
                    mock.patch.object(backup_drill.os, "chown") as rejected_chown,
                    self.assertRaisesRegex(RuntimeError, "root:root mode 0700 pre-PG contract"),
                ):
                    backup_drill._scratch_prepare_disk_root_for_postgres(
                        root, postgres_uid=1234, postgres_gid=5678
                    )
                rejected_chown.assert_not_called()

            with (
                mock.patch.object(pathlib.Path, "lstat", side_effect=[before, after]),
                mock.patch.object(backup_drill.os, "chown", side_effect=PermissionError),
                self.assertRaisesRegex(RuntimeError, "could not be assigned to the exact postgres identity"),
            ):
                backup_drill._scratch_prepare_disk_root_for_postgres(
                    root, postgres_uid=1234, postgres_gid=5678
                )

            wrong_after = types.SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o711, st_uid=1234, st_gid=5678, st_dev=7, st_ino=11
            )
            with (
                mock.patch.object(pathlib.Path, "lstat", side_effect=[before, wrong_after]),
                mock.patch.object(backup_drill.os, "chown"),
                self.assertRaisesRegex(RuntimeError, "identity changed during the postgres ownership transition"),
            ):
                backup_drill._scratch_prepare_disk_root_for_postgres(
                    root, postgres_uid=1234, postgres_gid=5678
                )

            worker_tree = ast.parse(inspect.getsource(backup_drill._scratch_worker_main))
            direct_calls: list[tuple[str, int]] = []
            for node in ast.walk(worker_tree):
                if isinstance(node, ast.Call):
                    function_name = (
                        node.func.id if isinstance(node.func, ast.Name) else None
                    )
                    if function_name in {
                        "_scratch_prepare_disk_root_for_postgres",
                        "_scratch_run_observed_phase",
                        "_scratch_prepare_postgres_directory",
                        "run_command",
                    }:
                        direct_calls.append((function_name, node.lineno))
            transition_line = min(
                line for name, line in direct_calls
                if name == "_scratch_prepare_disk_root_for_postgres"
            )
            archive_verify_line = min(
                line for name, line in direct_calls
                if name == "_scratch_run_observed_phase"
            )
            first_pg_directory_line = min(
                line for name, line in direct_calls
                if name == "_scratch_prepare_postgres_directory"
            )
            first_pg_command_line = min(
                line for name, line in direct_calls if name == "run_command"
            )
            self.assertLess(archive_verify_line, transition_line)
            self.assertLess(transition_line, first_pg_directory_line)
            self.assertLess(transition_line, first_pg_command_line)

    def test_synthetic_disk_worker_transitions_root_before_postgres_discovery(self) -> None:
        events: list[str] = []
        args = argparse.Namespace(
            _scratch_backend=backup_drill.SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1,
            _scratch_archive=None,
            _scratch_archive_dev=None,
            _scratch_archive_ino=None,
            _scratch_archive_size=None,
            _scratch_archive_sha256=None,
        )

        def transition(*_args: Any, **_kwargs: Any) -> None:
            events.append("ownership_transition")

        def require_pg_commands(*_commands: str) -> None:
            events.append("postgres_discovery")
            raise RuntimeError("stop before any PostgreSQL executable")

        with (
            mock.patch.object(
                backup_drill.pwd,
                "getpwnam",
                return_value=types.SimpleNamespace(pw_uid=1234, pw_gid=5678),
            ),
            mock.patch.object(
                backup_drill,
                "_scratch_disk_mount_snapshot",
                side_effect=[
                    {"total_bytes": 560 * 1024**2, "free_bytes": 500 * 1024**2},
                    {"total_bytes": 560 * 1024**2, "free_bytes": 500 * 1024**2},
                ],
            ) as snapshot,
            mock.patch.object(
                backup_drill,
                "_scratch_prepare_disk_root_for_postgres",
                side_effect=transition,
            ),
            mock.patch.object(backup_drill, "require_commands", side_effect=require_pg_commands),
            self.assertRaisesRegex(RuntimeError, "stop before any PostgreSQL executable"),
        ):
            backup_drill._scratch_synthetic_restore_diagnostic_worker(
                args,
                pathlib.Path("/run/oldsparky-backup-restore-0123456789abcdef01234567"),
            )
        self.assertEqual(events, ["ownership_transition", "postgres_discovery"])
        self.assertEqual(snapshot.call_count, 2)

    def test_scratch_disk_filesystem_peak_sampler_is_capacity_bounded_and_stable(self) -> None:
        root = pathlib.Path("/run/oldsparky-backup-restore-0123456789abcdef01234567")
        capacity = 560 * 1024**2
        sampler = backup_drill._ScratchFilesystemPeakSampler(
            root,
            backend=backup_drill.SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1,
            postgres_owner=(1234, 5678),
        )
        with mock.patch.object(
            backup_drill,
            "_scratch_disk_mount_snapshot",
            side_effect=[
                {"total_bytes": capacity, "free_bytes": capacity - 10 * 1024**2},
                {"total_bytes": capacity, "free_bytes": capacity - 20 * 1024**2},
            ],
        ):
            sampler._sample()
            sampler._sample()
        self.assertEqual(sampler.capacity_bytes, capacity)
        self.assertEqual(sampler.peak_bytes, 20 * 1024**2)
        with mock.patch.object(
            backup_drill,
            "_scratch_disk_mount_snapshot",
            return_value={"total_bytes": capacity + 1, "free_bytes": capacity - 20 * 1024**2},
        ):
            with self.assertRaisesRegex(RuntimeError, "capacity changed during sampling"):
                sampler._sample()

    def test_scratch_disk_image_cleanup_requires_loop_release_and_exact_inode(self) -> None:
        identity = (1, 2, 4096, 0, 0, 0o600, 1)
        with tempfile.TemporaryDirectory() as temporary_dir:
            image_path = pathlib.Path(temporary_dir) / "image.ext4"
            image_path.write_bytes(b"x" * 4096)
            with (
                mock.patch.object(backup_drill, "_scratch_image_path_identity", return_value=identity),
                mock.patch.object(backup_drill, "_scratch_image_loop_matches", return_value=[("loop1", "7:1")]),
            ):
                with self.assertRaisesRegex(RuntimeError, "still has a systemd loop association"):
                    backup_drill._scratch_unlink_image_exact(image_path, identity)
            self.assertTrue(image_path.exists())
            with (
                mock.patch.object(backup_drill, "_scratch_image_path_identity", return_value=identity),
                mock.patch.object(backup_drill, "_scratch_image_loop_matches", return_value=[]),
                mock.patch.object(backup_drill, "_scratch_fsync_directory"),
            ):
                backup_drill._scratch_unlink_image_exact(image_path, identity)
            self.assertFalse(image_path.exists())

    def test_scratch_disk_loop_inventory_requires_verified_sysfs_and_exact_backing_inode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = pathlib.Path(temporary_dir) / "sys-block"
            loop = root / "loop42"
            (loop / "loop").mkdir(parents=True)
            image_path = pathlib.Path(temporary_dir) / "image.ext4"
            image_path.write_bytes(b"image")
            backing_file = loop / "loop" / "backing_file"
            backing_file.write_text(str(image_path) + "\n", encoding="utf-8")
            (loop / "dev").write_text("7:42\n", encoding="ascii")
            root_info = types.SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o555, st_uid=0, st_gid=0
            )
            real_lstat = pathlib.Path.lstat

            def sysfs_root_lstat(path: pathlib.Path) -> os.stat_result:
                if path == root:
                    return root_info  # type: ignore[return-value]
                return real_lstat(path)

            identity = (
                image_path.stat().st_dev,
                image_path.stat().st_ino,
                image_path.stat().st_size,
                0,
                0,
                0o600,
                1,
            )
            with (
                mock.patch.object(backup_drill, "SCRATCH_SYS_BLOCK_ROOT", root),
                mock.patch.object(pathlib.Path, "lstat", sysfs_root_lstat),
                mock.patch.object(
                    backup_drill,
                    "_scratch_mountinfo_for",
                    return_value=("/sys", "0:42", "sysfs", "sysfs", "/", "ro,nosuid,nodev"),
                ),
                mock.patch.object(backup_drill, "_scratch_image_path_identity", return_value=identity),
            ):
                self.assertEqual(
                    backup_drill._scratch_image_loop_matches(image_path, identity),
                    [("loop42", "7:42")],
                )
                backing_file.write_text(str(pathlib.Path(temporary_dir) / "other-image.ext4") + "\n", encoding="utf-8")
                self.assertEqual(backup_drill._scratch_image_loop_matches(image_path, identity), [])
                backing_file.write_text(str(image_path) + "\n", encoding="utf-8")
                wrong_identity = (identity[0], identity[1] + 1, *identity[2:])
                with mock.patch.object(
                    backup_drill, "_scratch_image_path_identity", return_value=wrong_identity
                ):
                    with self.assertRaisesRegex(RuntimeError, "backing image identity changed"):
                        backup_drill._scratch_image_loop_matches(image_path, identity)
                with mock.patch.object(
                    backup_drill,
                    "_scratch_mountinfo_for",
                    return_value=("/sys", "0:42", "tmpfs", "tmpfs", "/", "ro"),
                ):
                    with self.assertRaisesRegex(RuntimeError, "verified host sysfs mount"):
                        backup_drill._scratch_image_loop_matches(image_path, identity)

    def test_scratch_disk_image_uncertain_cleanup_writes_closed_private_receipt(self) -> None:
        identity = (1, 2, 4096, 0, 0, 0o600, 1)
        with tempfile.TemporaryDirectory() as temporary_dir:
            receipt_root = pathlib.Path(temporary_dir) / "images"
            receipt_root.mkdir(mode=0o700)
            image_path = receipt_root / "restore-0123456789abcdef01234567.ext4"
            with mock.patch.object(backup_drill, "SCRATCH_DISK_IMAGE_ROOT", receipt_root):
                with mock.patch.object(backup_drill.os, "fchown") as fchown:
                    backup_drill._scratch_write_image_cleanup_receipt(
                        image_path,
                        identity,
                        unit_name="oldsparky-backup-restore-0123456789abcdef01234567.service",
                        reason="loop_association_persists",
                    )
                    fchown.assert_called_once_with(mock.ANY, 0, 0)
                receipt_path = receipt_root / "cleanup-restore-0123456789abcdef01234567.json"
                info = receipt_path.lstat()
                self.assertTrue(stat.S_ISREG(info.st_mode))
                self.assertEqual(
                    (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)),
                    (os.geteuid(), os.getegid(), 0o600),
                )
                receipt = json.loads(receipt_path.read_text(encoding="ascii"))
                self.assertEqual(receipt["cleanup_status"], "retained")
                self.assertEqual(receipt["reason"], "loop_association_persists")
                self.assertEqual(receipt["image_ino"], identity[1])
                self.assertEqual(set(receipt), {
                    "schema", "unit_name", "image_name", "image_dev", "image_ino",
                    "image_size_bytes", "cleanup_status", "reason",
                })
                with self.assertRaisesRegex(ValueError, "inputs are invalid"):
                    backup_drill._scratch_write_image_cleanup_receipt(
                        image_path,
                        identity,
                        unit_name="untrusted.service",
                        reason="raw_exception_text",
                    )

    def test_scratch_postgres_memory_setting_is_paired_and_safety_options_stay_fixed(self) -> None:
        socket_dir = pathlib.Path("/run/oldsparky-backup-restore-deadbeef/socket")
        initdb_command = backup_drill._scratch_initdb_command(
            "/usr/lib/postgresql/16/bin/initdb",
            pathlib.Path("/run/oldsparky-backup-restore-deadbeef/pgdata"),
        )
        initdb_settings = [
            initdb_command[index + 1]
            for index, argument in enumerate(initdb_command[:-1])
            if argument == "--set"
        ]
        start_options = backup_drill._scratch_postgres_start_options(socket_dir)
        start_settings = {
            start_options[index + 1].split("=", 1)[0]: start_options[index + 1].split("=", 1)[1]
            for index, argument in enumerate(start_options[:-1])
            if argument == "-c"
        }

        self.assertEqual(initdb_settings, ["shared_buffers=16MB"])
        self.assertEqual(
            start_settings["shared_buffers"],
            initdb_settings[0].split("=", 1)[1],
        )
        self.assertEqual(
            start_settings,
            {
                "listen_addresses": "",
                "unix_socket_directories": str(socket_dir),
                "port": str(backup_drill.SCRATCH_PG_PORT),
                "fsync": "on",
                "full_page_writes": "on",
                "synchronous_commit": "on",
                "dynamic_shared_memory_type": "mmap",
                "shared_buffers": "16MB",
                "work_mem": "4MB",
                "maintenance_work_mem": "32MB",
                "max_connections": "10",
                "max_wal_size": "64MB",
                "min_wal_size": "32MB",
            },
        )
        self.assertNotIn("huge_pages", start_settings)

    def test_synthetic_restore_diagnostic_arms_are_fixed_and_report_requires_all_three(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(
                backup_drill,
                "run_synthetic_restore_diagnostic",
                return_value={"backend": backup_drill.SCRATCH_BACKEND_TMPFS_V1},
            ) as run_diagnostic:
                self.assertEqual(
                    _run_synthetic_restore_diagnostic_for_ci(source_sha="a" * 40),
                    {"backend": backup_drill.SCRATCH_BACKEND_TMPFS_V1},
                )
            run_diagnostic.assert_called_once_with(
                scratch_backend=backup_drill.SCRATCH_BACKEND_TMPFS_V1
            )

        with mock.patch.dict(
            os.environ,
            {"PLATFORM_SCRATCH_BACKEND": backup_drill.SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1},
            clear=True,
        ):
            with mock.patch.object(
                backup_drill,
                "run_synthetic_restore_diagnostic",
                return_value={"backend": backup_drill.SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1},
            ) as run_diagnostic:
                self.assertEqual(
                    _run_synthetic_restore_diagnostic_for_ci(source_sha="b" * 40),
                    {"backend": backup_drill.SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1},
                )
            run_diagnostic.assert_called_once_with(
                scratch_backend=backup_drill.SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1
            )

        with mock.patch.dict(os.environ, {"PLATFORM_SCRATCH_BACKEND": "unknown"}, clear=True):
            with mock.patch.object(
                backup_drill, "run_synthetic_restore_diagnostic"
            ) as run_diagnostic:
                with self.assertRaisesRegex(RuntimeError, "backend selection failed closed"):
                    _run_synthetic_restore_diagnostic_for_ci(source_sha="c" * 40)
            run_diagnostic.assert_not_called()

        socket_dir = pathlib.Path("/run/oldsparky-backup-restore-deadbeef/socket")
        production_options = backup_drill._scratch_postgres_start_options(socket_dir)
        arms = backup_drill.SCRATCH_SYNTHETIC_RESTORE_ARMS
        self.assertEqual(
            [arm[0] for arm in arms],
            ["baseline", "minimal_no_senders", "minimal_no_senders_single_transaction"],
        )
        self.assertEqual(
            backup_drill._scratch_postgres_start_options_for_diagnostic_arm(socket_dir, arms[0]),
            production_options,
        )
        minimal_options = backup_drill._scratch_postgres_start_options_for_diagnostic_arm(
            socket_dir, arms[1]
        )
        self.assertEqual(minimal_options[-4:], ["-c", "wal_level=minimal", "-c", "max_wal_senders=0"])
        self.assertEqual(
            backup_drill._scratch_postgres_start_options_for_diagnostic_arm(socket_dir, arms[2]),
            minimal_options,
        )
        self.assertEqual(backup_drill.SCRATCH_SYNTHETIC_PAYLOAD_ROWS, 4096)
        self.assertEqual(backup_drill.SCRATCH_SYNTHETIC_PAYLOAD_BYTES_PER_ROW, 8192)
        self.assertEqual(backup_drill.SCRATCH_SYNTHETIC_PAYLOAD_BYTES, 33_554_432)
        self.assertLessEqual(backup_drill.SCRATCH_SYNTHETIC_ARCHIVE_MAX_BYTES, 48 * 1024**2)
        expected_rows = backup_drill._scratch_synthetic_expected_rows()
        self.assertEqual(len(expected_rows), backup_drill.SCRATCH_SYNTHETIC_PAYLOAD_ROWS)
        self.assertEqual(expected_rows[0][0:2], [1, "row-00000001"])
        self.assertEqual(expected_rows[-1][0:2], [4096, "row-00004096"])
        self.assertEqual(
            len({row[2] for row in expected_rows}),
            backup_drill.SCRATCH_SYNTHETIC_PAYLOAD_ROWS,
        )
        snapshot_target = backup_drill.DatabaseTarget(
            host=str(socket_dir),
            port=backup_drill.SCRATCH_PG_PORT,
            username="postgres",
            password=None,
            database="platform_restore_drill_20261018t230000z_arm1_12345",
        )
        logical_snapshot = {
            "tables": ["synthetic_restore_probe"],
            "columns": [
                ["id", "integer", "NO"],
                ["label", "text", "NO"],
                ["search_text", "text", "NO"],
            ],
            "primary_key_columns": ["id"],
            "rows": expected_rows,
            "row_count": backup_drill.SCRATCH_SYNTHETIC_PAYLOAD_ROWS,
            "payload_bytes": backup_drill.SCRATCH_SYNTHETIC_PAYLOAD_BYTES,
            "min_payload_bytes": backup_drill.SCRATCH_SYNTHETIC_PAYLOAD_BYTES_PER_ROW,
            "max_payload_bytes": backup_drill.SCRATCH_SYNTHETIC_PAYLOAD_BYTES_PER_ROW,
            "unique_prefix_count": backup_drill.SCRATCH_SYNTHETIC_PAYLOAD_ROWS,
            "prefixes_match_ids": True,
            "like_match_ids": [1],
            "alembic_versions": [backup_drill.SCRATCH_SYNTHETIC_ALEMBIC_REVISION],
            "pg_trgm_installed": True,
            "pg_trgm_functional": True,
            "gin_trgm_index_valid": True,
        }
        logical_stdout = json.dumps(logical_snapshot)
        gin_plan_stdout = json.dumps(
            [
                {
                    "Plan": {
                        "Node Type": "Bitmap Heap Scan",
                        "Plans": [
                            {
                                "Node Type": "Bitmap Index Scan",
                                "Index Name": backup_drill.SCRATCH_SYNTHETIC_TRGM_INDEX,
                            }
                        ],
                    }
                }
            ]
        )

        def snapshot_with_plan(plan_stdout: str) -> tuple[dict[str, Any], list[Any]]:
            outputs = [
                subprocess.CompletedProcess([], 0, stdout=logical_stdout, stderr=""),
                subprocess.CompletedProcess([], 0, stdout=plan_stdout, stderr=""),
            ]
            with mock.patch.object(
                backup_drill, "run_command", side_effect=outputs
            ) as psql_calls:
                result = backup_drill._scratch_synthetic_restore_snapshot(
                    snapshot_target,
                    expected_rows=expected_rows,
                    psql_binary="/usr/lib/postgresql/16/bin/psql",
                )
            return result, psql_calls.call_args_list

        snapshot_result, snapshot_calls = snapshot_with_plan(gin_plan_stdout)
        self.assertEqual(
            snapshot_result,
            {
                "logical_sha256": hashlib.sha256(
                    json.dumps(
                        logical_snapshot, separators=(",", ":"), sort_keys=True
                    ).encode("ascii")
                ).hexdigest()
            },
        )
        logical_query_args = snapshot_calls[0].args[0]
        plan_query_args = snapshot_calls[1].args[0]
        self.assertNotIn("--quiet", logical_query_args)
        self.assertIn("--quiet", plan_query_args)
        self.assertEqual(
            plan_query_args[-1],
            "SET enable_seqscan = off; EXPLAIN (FORMAT JSON) "
            "SELECT id FROM platform.synthetic_restore_probe "
            "WHERE search_text LIKE '00000001%';",
        )
        with self.assertRaisesRegex(RuntimeError, "invalid index plan"):
            snapshot_with_plan(f"SET\n{gin_plan_stdout}")
        sequential_scan_plan = json.dumps([{"Plan": {"Node Type": "Seq Scan"}}])
        with self.assertRaisesRegex(RuntimeError, "did not use its fixed GIN index"):
            snapshot_with_plan(sequential_scan_plan)

        self.assertEqual(
            backup_drill.SCRATCH_SYNTHETIC_RESTORE_PHASES,
            ("restore_platform", "restore_public"),
        )
        self.assertTrue(
            backup_drill._scratch_synthetic_restore_metric_phase("restore_platform")
        )
        self.assertTrue(
            backup_drill._scratch_synthetic_restore_metric_phase("restore_public")
        )
        self.assertFalse(
            backup_drill._scratch_synthetic_restore_metric_phase("alembic_verify")
        )
        with self.assertRaisesRegex(RuntimeError, "unknown phase"):
            backup_drill._scratch_synthetic_restore_metric_phase("unexpected_phase")
        control_target = backup_drill.DatabaseTarget(
            host="/run/oldsparky-backup-restore-deadbeef/socket",
            port=backup_drill.SCRATCH_PG_PORT,
            username="postgres",
            password=None,
            database="postgres",
        )
        restore_database = "platform_restore_drill_20261018t230000z_arm1_12345"
        completed_counts = [
            subprocess.CompletedProcess([], 0, stdout="1\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="0\n", stderr=""),
        ]
        with (
            mock.patch.object(
                backup_drill.subprocess, "run", side_effect=completed_counts
            ) as backend_query,
            mock.patch.object(backup_drill.time, "sleep") as poll_sleep,
        ):
            backup_drill._scratch_wait_for_restore_database_backends_to_exit(
                control_target,
                restore_database,
                psql_binary="/usr/lib/postgresql/16/bin/psql",
            )
        self.assertEqual(backend_query.call_count, 2)
        query_args = backend_query.call_args_list[0].args[0]
        self.assertEqual(query_args[0], "/usr/lib/postgresql/16/bin/psql")
        self.assertEqual(query_args[query_args.index("--dbname") + 1], "postgres")
        self.assertNotIn(f"--set=restore_database={restore_database}", query_args)
        self.assertEqual(
            query_args[-1],
            f"SELECT count(*) FROM pg_stat_activity WHERE datname = '{restore_database}';",
        )
        self.assertEqual(backend_query.call_args_list[0].kwargs["timeout"], 1.0)
        poll_sleep.assert_called_once_with(backup_drill.SCRATCH_SYNTHETIC_WAL_POLL_SECONDS)

        with mock.patch.object(backup_drill.subprocess, "run") as backend_query:
            for invalid_database in (
                "production_database",
                f"{restore_database}'; SELECT pg_sleep(1); --",
            ):
                with self.subTest(invalid_database=invalid_database):
                    with self.assertRaisesRegex(RuntimeError, "observer target is invalid"):
                        backup_drill._scratch_wait_for_restore_database_backends_to_exit(
                            control_target, invalid_database
                        )
            backend_query.assert_not_called()

        with (
            mock.patch.object(
                backup_drill,
                "SCRATCH_SYNTHETIC_WAL_BACKEND_DRAIN_SECONDS",
                0.01,
            ),
            mock.patch.object(
                backup_drill.time, "monotonic", side_effect=[0.0, 0.0, 0.02, 0.03]
            ),
            mock.patch.object(backup_drill.time, "sleep"),
            mock.patch.object(
                backup_drill.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, stdout="1\n", stderr=""),
            ) as backend_query,
        ):
            with self.assertRaisesRegex(RuntimeError, "did not exit"):
                backup_drill._scratch_wait_for_restore_database_backends_to_exit(
                    control_target, restore_database
                )
        backend_query.assert_called_once()

        with mock.patch.object(
            backup_drill.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, stdout="unknown\n", stderr=""),
        ):
            with self.assertRaisesRegex(RuntimeError, "invalid count"):
                backup_drill._scratch_wait_for_restore_database_backends_to_exit(
                    control_target, restore_database
                )

        noisy_startup = b"x" * 8192 + b"\nModuleNotFoundError: No module named 'asyncpg'\n"
        stderr_tail = bytearray()
        for offset in range(0, len(noisy_startup), 1024):
            backup_drill._scratch_append_bounded_tail(
                stderr_tail,
                noisy_startup[offset : offset + 1024],
                backup_drill.SCRATCH_SYNTHETIC_STDERR_TAIL_BYTES,
            )
        self.assertEqual(
            bytes(stderr_tail),
            noisy_startup[-backup_drill.SCRATCH_SYNTHETIC_STDERR_TAIL_BYTES :],
        )
        noisy_startup_classification = _classify_synthetic_restore_stderr(
            bytes(stderr_tail), complete=True
        )
        self.assertEqual(noisy_startup_classification["classification"], "mixed")
        self.assertIn("missing_module", noisy_startup_classification["reasons"])
        self.assertEqual(
            noisy_startup_classification["suffix_bytes"],
            backup_drill.SCRATCH_SYNTHETIC_STDERR_TAIL_BYTES,
        )
        observer_failure = _classify_synthetic_restore_stderr(
            b"[FAIL] Synthetic restore backend-exit observer failed.\n",
            complete=True,
        )
        self.assertEqual(observer_failure["classification"], "known")
        self.assertEqual(
            observer_failure["reasons"],
            ["synthetic_backend_exit_observer_failed"],
        )
        self.assertEqual(observer_failure["unknown_lines"], 0)
        invalid_plan_failure = _classify_synthetic_restore_stderr(
            b"[FAIL] Synthetic restore returned an invalid index plan.\n",
            complete=True,
        )
        self.assertEqual(invalid_plan_failure["classification"], "known")
        self.assertEqual(
            invalid_plan_failure["reasons"], ["synthetic_invalid_index_plan"]
        )
        unknown_phase_failure = _classify_synthetic_restore_stderr(
            b"[FAIL] Synthetic restore reported an unknown phase.\n",
            complete=True,
        )
        self.assertEqual(unknown_phase_failure["classification"], "known")
        self.assertEqual(
            unknown_phase_failure["reasons"], ["synthetic_unknown_restore_phase"]
        )

        report = {
            "kind": "synthetic_restore_diagnostic",
            "schema": 2,
            "status": "completed",
            "postgres_version": "16.15",
            "source_fixture": backup_drill.SCRATCH_SYNTHETIC_FIXTURE_ID,
            "fixture_row_count": backup_drill.SCRATCH_SYNTHETIC_PAYLOAD_ROWS,
            "fixture_payload_bytes": backup_drill.SCRATCH_SYNTHETIC_PAYLOAD_BYTES,
            "archive_format": "pg_dump_custom",
            "archive_size_bytes": 4096,
            "archive_sha256": "a" * 64,
            "arms": [
                {
                    "name": name,
                    "wal_level": wal_level,
                    "max_wal_senders": max_wal_senders,
                    "single_transaction": single_transaction,
                    "restored_table_count": 1,
                    "fixture_row_count": backup_drill.SCRATCH_SYNTHETIC_PAYLOAD_ROWS,
                    "fixture_payload_bytes": backup_drill.SCRATCH_SYNTHETIC_PAYLOAD_BYTES,
                    "logical_sha256": "b" * 64,
                    "phases": [
                        {
                            "phase": phase,
                            "wal_before_bytes": 1000 + phase_index * 1000,
                            "wal_after_bytes": 2000 + phase_index * 1000,
                            "wal_delta_bytes": 1000,
                            "restore_command_elapsed_us": 25_000,
                            "phase_elapsed_us": 35_000,
                            "memory_start_bytes": 64 * 1024**2,
                            "memory_peak_bytes": 128 * 1024**2,
                            "tmpfs_peak_sampled_bytes": 16 * 1024**2,
                            "tmpfs_sample_period_ms": 10,
                            "memory_scope": backup_drill.SCRATCH_SYNTHETIC_MEMORY_SCOPE,
                            "wal_scope": backup_drill.SCRATCH_SYNTHETIC_WAL_SCOPE,
                            "elapsed_scope": backup_drill.SCRATCH_SYNTHETIC_ELAPSED_SCOPE,
                            "phase_elapsed_scope": backup_drill.SCRATCH_SYNTHETIC_PHASE_ELAPSED_SCOPE,
                            "tmpfs_scope": backup_drill.SCRATCH_SYNTHETIC_TMPFS_SCOPE,
                        }
                        for phase_index, phase in enumerate(
                            backup_drill.SCRATCH_SYNTHETIC_RESTORE_PHASES
                        )
                    ],
                }
                for name, wal_level, max_wal_senders, single_transaction in arms
            ],
        }
        self.assertTrue(backup_drill.valid_scratch_synthetic_restore_diagnostic(report))
        schema3_tmpfs = json.loads(json.dumps(report))
        schema3_tmpfs["schema"] = 3
        schema3_tmpfs["scratch_backend"] = backup_drill.SCRATCH_BACKEND_TMPFS_V1
        schema3_tmpfs["scratch_filesystem_capacity_bytes"] = backup_drill.SCRATCH_TMPFS_BYTES
        self.assertTrue(backup_drill.valid_scratch_synthetic_restore_diagnostic(schema3_tmpfs))

        schema3_disk = json.loads(json.dumps(report))
        schema3_disk["schema"] = 3
        schema3_disk["scratch_backend"] = backup_drill.SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1
        disk_capacity = 560 * 1024**2
        schema3_disk["scratch_filesystem_capacity_bytes"] = disk_capacity
        for arm in schema3_disk["arms"]:
            for phase in arm["phases"]:
                phase.pop("tmpfs_peak_sampled_bytes")
                phase.pop("tmpfs_sample_period_ms")
                phase.pop("tmpfs_scope")
                phase["disk_peak_sampled_bytes"] = 16 * 1024**2
                phase["disk_capacity_bytes"] = disk_capacity
                phase["disk_sample_period_ms"] = 10
                phase["disk_scope"] = backup_drill.SCRATCH_SYNTHETIC_DISK_SCOPE
        self.assertTrue(backup_drill.valid_scratch_synthetic_restore_diagnostic(schema3_disk))
        wrong_backend_metric = json.loads(json.dumps(schema3_disk))
        wrong_backend_metric["arms"][0]["phases"][0]["tmpfs_peak_sampled_bytes"] = 1
        self.assertFalse(
            backup_drill.valid_scratch_synthetic_restore_diagnostic(wrong_backend_metric)
        )
        over_disk_stop = json.loads(json.dumps(schema3_disk))
        over_disk_stop["arms"][0]["phases"][0]["disk_peak_sampled_bytes"] = (
            disk_capacity - backup_drill.SCRATCH_TMPFS_STOP_BYTES + 1
        )
        self.assertFalse(
            backup_drill.valid_scratch_synthetic_restore_diagnostic(over_disk_stop)
        )
        wrong_capacity = json.loads(json.dumps(schema3_disk))
        wrong_capacity["arms"][0]["phases"][0]["disk_capacity_bytes"] -= 1
        self.assertFalse(
            backup_drill.valid_scratch_synthetic_restore_diagnostic(wrong_capacity)
        )
        result_frame = (
            json.dumps(report, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode("ascii")
        self.assertLessEqual(len(result_frame), backup_drill.SCRATCH_SYNTHETIC_RESULT_MAX_BYTES)
        self.assertEqual(backup_drill._scratch_synthetic_worker_candidate(result_frame), report)
        missing_arm = json.loads(json.dumps(report))
        missing_arm["arms"].pop()
        self.assertFalse(backup_drill.valid_scratch_synthetic_restore_diagnostic(missing_arm))
        changed_arm = json.loads(json.dumps(report))
        changed_arm["arms"][2]["single_transaction"] = False
        self.assertFalse(backup_drill.valid_scratch_synthetic_restore_diagnostic(changed_arm))
        changed_phase = json.loads(json.dumps(report))
        changed_phase["arms"][0]["phases"][1]["wal_delta_bytes"] += 1
        self.assertFalse(backup_drill.valid_scratch_synthetic_restore_diagnostic(changed_phase))
        missing_phase = json.loads(json.dumps(report))
        missing_phase["arms"][1]["phases"].pop()
        self.assertFalse(backup_drill.valid_scratch_synthetic_restore_diagnostic(missing_phase))
        swapped_arms = json.loads(json.dumps(report))
        swapped_arms["arms"][0], swapped_arms["arms"][1] = (
            swapped_arms["arms"][1], swapped_arms["arms"][0]
        )
        self.assertFalse(backup_drill.valid_scratch_synthetic_restore_diagnostic(swapped_arms))
        swapped_phases = json.loads(json.dumps(report))
        swapped_phases["arms"][0]["phases"].reverse()
        self.assertFalse(backup_drill.valid_scratch_synthetic_restore_diagnostic(swapped_phases))
        changed_logical_hash = json.loads(json.dumps(report))
        changed_logical_hash["arms"][2]["logical_sha256"] = "c" * 64
        self.assertFalse(
            backup_drill.valid_scratch_synthetic_restore_diagnostic(changed_logical_hash)
        )
        wrong_metric_scope = json.loads(json.dumps(report))
        wrong_metric_scope["arms"][0]["phases"][0]["memory_scope"] = "whole_arm"
        self.assertFalse(
            backup_drill.valid_scratch_synthetic_restore_diagnostic(wrong_metric_scope)
        )
        wrong_fixture_size = json.loads(json.dumps(report))
        wrong_fixture_size["fixture_payload_bytes"] -= 1
        self.assertFalse(backup_drill.valid_scratch_synthetic_restore_diagnostic(wrong_fixture_size))
        bad_version = json.loads(json.dumps(report))
        bad_version["postgres_version"] = "17.1"
        self.assertFalse(backup_drill.valid_scratch_synthetic_restore_diagnostic(bad_version))
        with (
            mock.patch.object(backup_drill.os, "lseek", return_value=0),
            mock.patch.object(backup_drill.os, "write", return_value=5),
            mock.patch.object(backup_drill.os, "read") as read_peak,
        ):
            with self.assertRaisesRegex(RuntimeError, "could not be reset"):
                backup_drill._scratch_read_or_reset_memory_peak(10, reset=True)
        read_peak.assert_not_called()

        with tempfile.TemporaryDirectory() as temporary_dir:
            cluster = pathlib.Path(temporary_dir) / "data"
            cluster.mkdir()
            pid_file = cluster / "postmaster.pid"
            pid_file.write_text("synthetic live-cluster marker\n", encoding="ascii")
            with self.assertRaisesRegex(RuntimeError, "must be reaped"):
                backup_drill._scratch_cleanup_synthetic_cluster(
                    (cluster,), root_device=cluster.stat().st_dev
                )
            self.assertTrue(pid_file.is_file())

        with tempfile.TemporaryDirectory() as temporary_dir:
            temporary_root = pathlib.Path(temporary_dir)
            (temporary_root / "platform").mkdir()
            image_root = temporary_root / "image-root"
            image_root.mkdir(mode=0o700)
            image_path = image_root / "image.ext4"
            with image_path.open("wb") as image_file:
                image_file.truncate(backup_drill.SCRATCH_DISK_IMAGE_BYTES)
            real_fstat = os.fstat
            real_lstat = pathlib.Path.lstat

            def image_identity_fstat(descriptor: int) -> types.SimpleNamespace:
                actual = real_fstat(descriptor)
                return types.SimpleNamespace(
                    st_mode=stat.S_IFREG | 0o600,
                    st_dev=123,
                    st_ino=456,
                    st_size=backup_drill.SCRATCH_DISK_IMAGE_BYTES,
                    st_uid=0,
                    st_gid=0,
                    st_nlink=1,
                    st_blocks=0,
                ) if actual.st_ino == image_path.stat().st_ino else actual

            def image_identity_lstat(path: pathlib.Path) -> os.stat_result:
                if path == image_root:
                    return types.SimpleNamespace(
                        st_mode=stat.S_IFDIR | 0o700,
                        st_dev=123,
                        st_ino=321,
                        st_uid=0,
                        st_gid=0,
                    )  # type: ignore[return-value]
                if path == image_path:
                    return types.SimpleNamespace(
                        st_mode=stat.S_IFREG | 0o600,
                        st_dev=123,
                        st_ino=456,
                        st_size=backup_drill.SCRATCH_DISK_IMAGE_BYTES,
                        st_uid=0,
                        st_gid=0,
                        st_nlink=1,
                    )  # type: ignore[return-value]
                return real_lstat(path)

            with (
                mock.patch.object(backup_drill, "SCRATCH_DISK_IMAGE_ROOT", image_root),
                mock.patch.object(backup_drill.os, "fstat", side_effect=image_identity_fstat),
                mock.patch.object(pathlib.Path, "lstat", image_identity_lstat),
                self.assertRaisesRegex(RuntimeError, "fixed allocation contract") as image_error,
            ):
                backup_drill._scratch_image_path_identity(image_path)
            source_image_diagnostic = image_error.exception.scratch_image_identity_diagnostic
            self.assertTrue(
                backup_drill.valid_scratch_image_identity_diagnostic(source_image_diagnostic)
            )
            self.assertEqual(source_image_diagnostic["failed_predicate"], "fully_allocated")
            self.assertEqual(
                source_image_diagnostic["predicates"],
                {
                    "regular_file": True,
                    "exact_size": True,
                    "owner_mode_nlink": True,
                    "path_inode_match": True,
                    "parent_identity_stable": True,
                    "same_filesystem": True,
                    "fully_allocated": False,
                },
            )
            with mock.patch.object(sys.modules[__name__], "REPO_ROOT", temporary_root):
                artifact_path = _write_synthetic_restore_diagnostic_artifact(
                    schema3_tmpfs, source_sha="c" * 40
                )
            self.assertEqual(
                json.loads(artifact_path.read_text(encoding="ascii")),
                {
                    "kind": "synthetic_restore_diagnostic_artifact",
                    "schema": 1,
                    "source_sha": "c" * 40,
                    "scratch_backend": schema3_tmpfs["scratch_backend"],
                    "diagnostic": schema3_tmpfs,
                },
            )
            self.assertEqual(stat.S_IMODE(artifact_path.stat().st_mode), 0o644)
            self.assertFalse(
                (artifact_path.parent / ".synthetic-restore-diagnostic.json.tmp").exists()
            )

            synthetic_stdout = b"private stdout must not be exported"
            synthetic_stderr = b"".join(
                (
                    b"ModuleNotFoundError: No module named 'asyncpg'\n",
                    b"File '/root/private/secret.cfg', line 7\n",
                    b"PermissionError: [Errno 13] Permission denied: '",
                    b"/run/oldsparky-backup-restore-0123456789abcdef01234567/worker.py'\n",
                    b"FileNotFoundError: [Errno 2] No such file or directory: '",
                    b"/opt/runner/private/python'\n",
                    b"/opt/runner/private/python: error while loading shared libraries: ",
                    b"libpq.so.5: cannot open shared object file: No such file or directory\n",
                    b"pg_ctl: error: invalid option '--private-argument=value'\n",
                    b"systemd-run: Failed to connect to bus: No such file or directory\n",
                    b"Failed at step NAMESPACE spawning /private/path: Operation not permitted\n",
                    b"systemd-run: Unknown assignment: PrivateValue=secret-data\n",
                    b"platform_backup_restore_drill.py: error: unrecognized arguments: private-value\n",
                    b"password=must-not-export\n",
                    b"runner id abcdef0123456789abcdef0123456789\n",
                    b"warning /root/private path-with-spaces\n",
                )
            )
            unit_diagnostic = {
                "schema": 3,
                "phase": "authorization_failed",
                "active_state": "active",
                "sub_state": "running",
                "result": "success",
                "exec_main_status": 127,
                "launcher_exit_status": 1,
                "stdout_bytes": len(synthetic_stdout),
                "stdout_sha256": hashlib.sha256(synthetic_stdout).hexdigest(),
                "stdout_truncated": False,
                "stderr_bytes": len(synthetic_stderr),
                "stderr_sha256": hashlib.sha256(synthetic_stderr).hexdigest(),
                "stderr_truncated": False,
                "verification_error": "policy_error",
                "verification_errno": None,
                "verification_site": "controller_image_mount_identity",
            }
            failure = RuntimeError("private exception text must not be exported")
            failure.scratch_first_fault = {
                "schema": 1,
                "origin": "controller",
                "site": "controller_identity",
                "exception_kind": "runtime",
                "errno": None,
                "verification_site": "controller_image_mount_identity",
            }
            failure.scratch_image_identity_diagnostic = source_image_diagnostic
            failure.scratch_unit_diagnostic = unit_diagnostic
            failure.scratch_unit_journal_diagnostic = {
                "schema": 1,
                "status": "complete",
                "sync_status": "synced",
                "bytes_read": 120,
                "complete": True,
                "records": 1,
                "reason": "namespace_setup",
                "namespace_kind": "mount",
                "errno": "EACCES",
                "target": "scratch_tmpfs",
            }
            failure.scratch_cleanup_status = "confirmed"
            failure.scratch_cleanup_diagnostic = {
                "schema": 1,
                "service_stopped": True,
                "mount_absent": True,
                "mountpoint_removed": True,
                "loop_released": True,
                "image_created_by_invocation": True,
                "image_unlinked": True,
                "retention_receipt_written": None,
                "protected_paths_cleaned": True,
                "cleanup_status": "confirmed",
            }
            failure.scratch_unit_private_output = (synthetic_stdout, synthetic_stderr)
            failure.scratch_synthetic_stderr_tail = synthetic_stderr[
                -backup_drill.SCRATCH_SYNTHETIC_STDERR_TAIL_BYTES :
            ]
            failure.scratch_synthetic_stderr_tail_complete = True
            with mock.patch.object(sys.modules[__name__], "REPO_ROOT", temporary_root):
                _write_synthetic_restore_failure_artifact(
                    failure,
                    source_sha="e" * 40,
                    scratch_backend=backup_drill.SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1,
                )
            with (
                mock.patch.object(sys.modules[__name__], "REPO_ROOT", temporary_root),
                mock.patch.object(
                    backup_drill,
                    "run_synthetic_restore_diagnostic",
                    side_effect=failure,
                ),
            ):
                with self.assertRaises(RuntimeError) as raised:
                    _run_synthetic_restore_diagnostic_for_ci(
                        source_sha="e" * 40,
                        scratch_backend=backup_drill.SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1,
                    )
            self.assertIs(raised.exception, failure)
            failure_artifact_path = (
                temporary_root
                / "platform"
                / "ci-backend"
                / "backend-privileged"
                / "synthetic-restore-diagnostic-failure.json"
            )
            failure_artifact = json.loads(
                failure_artifact_path.read_text(encoding="ascii")
            )
            self.assertEqual(
                set(failure_artifact),
                {
                    "kind",
                    "schema",
                    "source_sha",
                    "scratch_backend",
                    "test_id",
                    "failure_stage",
                    "scratch_first_fault",
                    "scratch_unit_diagnostic",
                    "scratch_unit_journal_diagnostic",
                    "scratch_cleanup_status",
                    "scratch_cleanup_diagnostic",
                    "scratch_image_identity_diagnostic",
                    "stderr_diagnostic",
                },
            )
            self.assertEqual(
                failure_artifact["kind"],
                "synthetic_restore_diagnostic_failure_artifact",
            )
            self.assertEqual(failure_artifact["source_sha"], "e" * 40)
            self.assertEqual(
                failure_artifact["scratch_backend"],
                backup_drill.SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1,
            )
            self.assertEqual(failure_artifact["test_id"], _SYNTHETIC_RESTORE_TEST_ID)
            self.assertEqual(failure_artifact["failure_stage"], "run_synthetic_restore_diagnostic")
            self.assertEqual(
                failure_artifact["scratch_unit_diagnostic"], unit_diagnostic
            )
            self.assertEqual(
                failure_artifact["scratch_unit_journal_diagnostic"],
                failure.scratch_unit_journal_diagnostic,
            )
            self.assertEqual(
                failure_artifact["scratch_first_fault"], failure.scratch_first_fault
            )
            self.assertEqual(failure_artifact["scratch_cleanup_status"], "confirmed")
            self.assertEqual(
                failure_artifact["scratch_cleanup_diagnostic"],
                failure.scratch_cleanup_diagnostic,
            )
            self.assertTrue(
                backup_drill.valid_scratch_cleanup_diagnostic(
                    failure_artifact["scratch_cleanup_diagnostic"]
                )
            )
            unproven_cleanup = json.loads(
                json.dumps(failure_artifact["scratch_cleanup_diagnostic"])
            )
            unproven_cleanup["loop_released"] = False
            self.assertFalse(backup_drill.valid_scratch_cleanup_diagnostic(unproven_cleanup))
            unknown_cleanup = json.loads(
                json.dumps(failure_artifact["scratch_cleanup_diagnostic"])
            )
            unknown_cleanup["mount_absent"] = None
            unknown_cleanup["loop_released"] = None
            unknown_cleanup["image_unlinked"] = None
            unknown_cleanup["retention_receipt_written"] = None
            unknown_cleanup["cleanup_status"] = "failed"
            self.assertTrue(backup_drill.valid_scratch_cleanup_diagnostic(unknown_cleanup))
            malformed_cleanup = json.loads(
                json.dumps(failure_artifact["scratch_cleanup_diagnostic"])
            )
            malformed_cleanup["unit_name"] = "private"
            self.assertFalse(backup_drill.valid_scratch_cleanup_diagnostic(malformed_cleanup))
            self.assertEqual(
                failure_artifact["scratch_image_identity_diagnostic"],
                source_image_diagnostic,
            )
            self.assertTrue(
                backup_drill.valid_scratch_image_identity_diagnostic(
                    failure_artifact["scratch_image_identity_diagnostic"]
                )
            )
            original_image_diagnostic = failure.scratch_image_identity_diagnostic
            invalid_image_diagnostics = []
            all_true = json.loads(json.dumps(original_image_diagnostic))
            all_true["predicates"] = dict.fromkeys(
                backup_drill.SCRATCH_IMAGE_IDENTITY_PREDICATES, True
            )
            invalid_image_diagnostics.append(all_true)
            unknown_key = json.loads(json.dumps(original_image_diagnostic))
            unknown_key["private_path"] = "/not-exportable"
            invalid_image_diagnostics.append(unknown_key)
            missing_key = json.loads(json.dumps(original_image_diagnostic))
            missing_key.pop("observed_size")
            invalid_image_diagnostics.append(missing_key)
            wrong_type = json.loads(json.dumps(original_image_diagnostic))
            wrong_type["observed_size"] = True
            invalid_image_diagnostics.append(wrong_type)
            out_of_range = json.loads(json.dumps(original_image_diagnostic))
            out_of_range["allocated_bytes"] = 2**63
            invalid_image_diagnostics.append(out_of_range)
            wrong_failed_predicate = json.loads(json.dumps(original_image_diagnostic))
            wrong_failed_predicate["predicates"]["exact_size"] = False
            invalid_image_diagnostics.append(wrong_failed_predicate)
            contradictory_size = json.loads(json.dumps(original_image_diagnostic))
            contradictory_size["predicates"]["exact_size"] = False
            contradictory_size["failed_predicate"] = "exact_size"
            invalid_image_diagnostics.append(contradictory_size)
            contradictory_allocation = json.loads(json.dumps(original_image_diagnostic))
            contradictory_allocation["allocated_bytes"] = contradictory_allocation[
                "observed_size"
            ]
            invalid_image_diagnostics.append(contradictory_allocation)
            contradictory_mode = json.loads(json.dumps(original_image_diagnostic))
            contradictory_mode["predicates"]["owner_mode_nlink"] = True
            contradictory_mode["failed_predicate"] = "fully_allocated"
            contradictory_mode["observed_mode"] = 0o640
            invalid_image_diagnostics.append(contradictory_mode)
            for invalid_projection in invalid_image_diagnostics:
                self.assertFalse(
                    backup_drill.valid_scratch_image_identity_diagnostic(invalid_projection)
                )
                failure.scratch_image_identity_diagnostic = invalid_projection
                with self.assertRaisesRegex(RuntimeError, "image identity projection"):
                    _write_synthetic_restore_failure_artifact(
                        failure,
                        source_sha="e" * 40,
                        scratch_backend=backup_drill.SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1,
                    )
            failure.scratch_image_identity_diagnostic = original_image_diagnostic
            with self.assertRaisesRegex(RuntimeError, "image identity projection"):
                _write_synthetic_restore_failure_artifact(
                    failure,
                    source_sha="e" * 40,
                    scratch_backend=backup_drill.SCRATCH_BACKEND_TMPFS_V1,
                )
            wrong_origin = dict(failure.scratch_first_fault)
            wrong_origin["origin"] = "worker"
            failure.scratch_first_fault = wrong_origin
            with self.assertRaisesRegex(RuntimeError, "site failed validation"):
                _write_synthetic_restore_failure_artifact(
                    failure,
                    source_sha="e" * 40,
                    scratch_backend=backup_drill.SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1,
                )
            failure.scratch_first_fault["origin"] = "controller"
            with self.assertRaisesRegex(RuntimeError, "source failed validation"):
                _write_synthetic_restore_failure_artifact(
                    failure,
                    source_sha="not-a-source-sha",
                    scratch_backend=backup_drill.SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1,
                )
            stderr_diagnostic = failure_artifact["stderr_diagnostic"]
            self.assertEqual(
                set(stderr_diagnostic),
                {
                    "schema",
                    "classification",
                    "reasons",
                    "unknown_lines",
                    "suffix_bytes",
                    "suffix_sha256",
                    "suffix_complete",
                },
            )
            self.assertEqual(stderr_diagnostic["schema"], 1)
            self.assertEqual(stderr_diagnostic["classification"], "mixed")
            self.assertEqual(
                stderr_diagnostic["reasons"],
                [
                    "missing_module",
                    "permission_denied",
                    "missing_executable",
                    "missing_shared_library",
                    "invalid_command_argument",
                    "systemd_bus_unavailable",
                    "namespace_setup_failure",
                    "unknown_unit_property",
                ],
            )
            self.assertGreater(stderr_diagnostic["unknown_lines"], 0)
            self.assertEqual(
                stderr_diagnostic["suffix_bytes"],
                len(failure.scratch_synthetic_stderr_tail),
            )
            self.assertEqual(
                stderr_diagnostic["suffix_sha256"],
                hashlib.sha256(failure.scratch_synthetic_stderr_tail).hexdigest(),
            )
            self.assertTrue(stderr_diagnostic["suffix_complete"])
            for private_value in (
                "/root/",
                "secret.cfg",
                "0123456789abcdef",
                "must-not-export",
                "private stdout",
                "private exception text",
                "path-with-spaces",
                "/opt/runner/private/python",
                "libpq.so.5",
                "private-argument",
                "secret-data",
                "private-value",
                "private-user@example.invalid",
            ):
                self.assertNotIn(private_value, json.dumps(failure_artifact))
            self.assertFalse(failure_artifact_path.stat().st_mode & 0o022)
            self.assertEqual(stat.S_IMODE(failure_artifact_path.stat().st_mode), 0o644)
            self.assertFalse(
                (
                    failure_artifact_path.parent
                    / ".synthetic-restore-diagnostic-failure.json.tmp"
                ).exists()
            )

        with self.assertRaisesRegex(RuntimeError, "unit summary"):
            malformed_failure = RuntimeError("private")
            malformed_failure.scratch_unit_diagnostic = {"schema": 1}
            _write_synthetic_restore_failure_artifact(
                malformed_failure,
                source_sha="e" * 40,
                scratch_backend=backup_drill.SCRATCH_BACKEND_TMPFS_V1,
            )
        with self.assertRaisesRegex(RuntimeError, "closed bounds"):
            oversized_failure = RuntimeError("private")
            oversized_failure.scratch_unit_private_output = (
                b"x" * (backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES + 1),
                b"",
            )
            _write_synthetic_restore_failure_artifact(
                oversized_failure,
                source_sha="e" * 40,
                scratch_backend=backup_drill.SCRATCH_BACKEND_TMPFS_V1,
            )
        with self.assertRaisesRegex(RuntimeError, "evidence bound"):
            _classify_synthetic_restore_stderr(
                b"x" * (backup_drill.SCRATCH_SYNTHETIC_STDERR_TAIL_BYTES + 1),
                complete=True,
            )
        ambiguous_stderr = (
            b"path /root/private with ambiguous spaces\n"
            b"password=private\n"
            b"database row contains private-user@example.invalid\n"
        )
        ambiguous_diagnostic = _classify_synthetic_restore_stderr(
            ambiguous_stderr, complete=True
        )
        self.assertEqual(ambiguous_diagnostic["classification"], "unknown")
        self.assertEqual(ambiguous_diagnostic["reasons"], [])
        self.assertEqual(ambiguous_diagnostic["suffix_bytes"], len(ambiguous_stderr))
        incomplete_diagnostic = _classify_synthetic_restore_stderr(
            b"PermissionError: [Errno 13] Permission denied: '/private/path'\n",
            complete=False,
        )
        self.assertEqual(incomplete_diagnostic["classification"], "mixed")
        self.assertEqual(incomplete_diagnostic["unknown_lines"], 1)
        self.assertFalse(incomplete_diagnostic["suffix_complete"])
        unavailable_diagnostic = _classify_synthetic_restore_stderr(
            None, complete=None
        )
        self.assertEqual(unavailable_diagnostic["classification"], "unavailable")
        with self.assertRaisesRegex(RuntimeError, "metadata is inconsistent"):
            _classify_synthetic_restore_stderr(None, complete=True)
        reporter_error = RuntimeError("original synthetic failure")
        with (
            mock.patch.object(
                backup_drill,
                "run_synthetic_restore_diagnostic",
                side_effect=reporter_error,
            ),
            mock.patch.object(
                sys.modules[__name__],
                "_write_synthetic_restore_failure_artifact",
                side_effect=OSError("artifact write failed"),
            ),
        ):
            with self.assertRaises(RuntimeError) as raised:
                _run_synthetic_restore_diagnostic_for_ci(source_sha="e" * 40)
        self.assertIs(raised.exception, reporter_error)

    def test_synthetic_protected_path_fixture_is_created_and_cleaned_by_identity(self) -> None:
        owner_uid = os.geteuid()
        owner_gid = os.getegid()
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = pathlib.Path(temporary_dir)
            existing = root / "existing"
            existing.mkdir()
            sentinel = existing / "preserve.txt"
            sentinel.write_text("pre-existing", encoding="ascii")
            created = backup_drill._scratch_create_missing_protected_path_directories(
                (existing, root / "missing" / "first", root / "missing" / "second"),
                owner_uid=owner_uid,
                owner_gid=owner_gid,
            )
            self.assertEqual(
                {path for path, _, _ in created},
                {
                    str(root / "missing"),
                    str(root / "missing" / "first"),
                    str(root / "missing" / "second"),
                },
            )
            for path, _, _ in created:
                info = pathlib.Path(path).lstat()
                self.assertTrue(stat.S_ISDIR(info.st_mode))
                self.assertEqual((info.st_uid, info.st_gid), (owner_uid, owner_gid))
                self.assertEqual(stat.S_IMODE(info.st_mode), 0o700)
            backup_drill._scratch_remove_created_protected_path_directories(
                created, owner_uid=owner_uid, owner_gid=owner_gid
            )
            self.assertFalse((root / "missing").exists())
            self.assertTrue(sentinel.is_file())
            self.assertEqual(sentinel.read_text(encoding="ascii"), "pre-existing")

        original_opt_mode = 0o1777
        tightened_opt_mode = original_opt_mode & ~0o022
        current_opt_mode = original_opt_mode

        def opt_info(mode: int, *, inode: int = 700) -> types.SimpleNamespace:
            return types.SimpleNamespace(
                st_mode=stat.S_IFDIR | mode,
                st_dev=11,
                st_ino=inode,
                st_uid=0,
                st_gid=0,
            )

        original_lstat = pathlib.Path.lstat

        def opt_lstat(path: pathlib.Path) -> os.stat_result:
            if path == pathlib.Path("/opt"):
                return opt_info(current_opt_mode)  # type: ignore[return-value]
            return original_lstat(path)

        def opt_fstat(descriptor: int) -> os.stat_result:
            self.assertEqual(descriptor, 91)
            return opt_info(current_opt_mode)  # type: ignore[return-value]

        def opt_fchmod(descriptor: int, mode: int) -> None:
            nonlocal current_opt_mode
            self.assertEqual(descriptor, 91)
            current_opt_mode = mode

        with (
            mock.patch.object(backup_drill.os, "open", return_value=91) as open_opt,
            mock.patch.object(backup_drill.os, "set_inheritable") as set_inheritable,
            mock.patch.object(backup_drill.os, "fstat", side_effect=opt_fstat),
            mock.patch.object(backup_drill.os, "fchmod", side_effect=opt_fchmod) as fchmod,
            mock.patch.object(backup_drill.os, "close") as close_opt,
            mock.patch.object(
                pathlib.Path, "lstat", autospec=True, side_effect=opt_lstat
            ),
        ):
            lease = backup_drill._scratch_tighten_opt_parent_for_synthetic_path_fixture()
            self.assertEqual(
                lease,
                (91, 11, 700, 0, 0, original_opt_mode, tightened_opt_mode),
            )
            self.assertEqual(current_opt_mode, tightened_opt_mode)
            open_opt.assert_called_once_with(
                "/opt",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            set_inheritable.assert_called_once_with(91, False)
            self.assertEqual(
                fchmod.call_args_list,
                [
                    mock.call(91, tightened_opt_mode),
                ],
            )
            close_opt.assert_not_called()
            assert lease is not None
            backup_drill._scratch_finish_opt_parent_adjustment(lease, restore=True)
            self.assertEqual(current_opt_mode, original_opt_mode)
            self.assertEqual(
                fchmod.call_args_list,
                [
                    mock.call(91, tightened_opt_mode),
                    mock.call(91, original_opt_mode),
                ],
            )
            close_opt.assert_called_once_with(91)

        current_opt_mode = original_opt_mode

        def fail_fchmod_without_changing_mode(descriptor: int, mode: int) -> None:
            self.assertEqual((descriptor, mode), (91, tightened_opt_mode))
            raise OSError("injected fchmod refusal before mode change")

        with (
            mock.patch.object(backup_drill.os, "open", return_value=91),
            mock.patch.object(backup_drill.os, "set_inheritable"),
            mock.patch.object(backup_drill.os, "fstat", side_effect=opt_fstat),
            mock.patch.object(
                backup_drill.os, "fchmod", side_effect=fail_fchmod_without_changing_mode
            ) as fchmod,
            mock.patch.object(backup_drill.os, "close") as close_opt,
            mock.patch.object(
                pathlib.Path, "lstat", autospec=True, side_effect=opt_lstat
            ),
        ):
            with self.assertRaisesRegex(OSError, "injected fchmod refusal") as raised:
                backup_drill._scratch_tighten_opt_parent_for_synthetic_path_fixture()
            lease = getattr(raised.exception, "scratch_synthetic_opt_parent_adjustment")
            self.assertEqual(current_opt_mode, original_opt_mode)
            backup_drill._scratch_finish_opt_parent_adjustment(lease, restore=True)
            self.assertEqual(current_opt_mode, original_opt_mode)
            self.assertEqual(fchmod.call_count, 1)
            close_opt.assert_called_once_with(91)

        current_opt_mode = tightened_opt_mode
        with (
            mock.patch.object(backup_drill.os, "fstat", side_effect=opt_fstat),
            mock.patch.object(backup_drill.os, "fchmod") as fchmod,
            mock.patch.object(backup_drill.os, "close") as close_opt,
            mock.patch.object(
                pathlib.Path, "lstat", autospec=True, side_effect=opt_lstat
            ),
        ):
            backup_drill._scratch_finish_opt_parent_adjustment(
                (91, 11, 700, 0, 0, original_opt_mode, tightened_opt_mode),
                restore=False,
            )
            self.assertEqual(current_opt_mode, tightened_opt_mode)
            fchmod.assert_not_called()
            close_opt.assert_called_once_with(91)

        replacement_lease = (91, 11, 700, 0, 0, original_opt_mode, tightened_opt_mode)
        changed_parent = opt_info(tightened_opt_mode, inode=701)
        with (
            mock.patch.object(backup_drill.os, "fstat", return_value=opt_info(tightened_opt_mode)),
            mock.patch.object(backup_drill.os, "fchmod") as fchmod,
            mock.patch.object(backup_drill.os, "close") as close_opt,
            mock.patch.object(
                pathlib.Path,
                "lstat",
                autospec=True,
                side_effect=lambda path: (
                    changed_parent
                    if path == pathlib.Path("/opt")
                    else original_lstat(path)
                ),
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "cleanup could not be confirmed"
            ) as cleanup_error:
                backup_drill._scratch_finish_opt_parent_adjustment(
                    replacement_lease, restore=True
                )
            self.assertRegex(str(cleanup_error.exception.__cause__), "identity changed")
            fchmod.assert_not_called()
            close_opt.assert_called_once_with(91)

        isolated_run_source = inspect.getsource(
            backup_drill._run_isolated_restore_drill
        )
        fixture_setup_start = isolated_run_source.index(
            '_scratch_prepare_synthetic_protected_paths(\n                        scratch_backend=scratch_backend'
        )
        image_setup_position = isolated_run_source.index(
            "_scratch_prepare_disk_image(token)", fixture_setup_start
        )
        self.assertLess(fixture_setup_start, image_setup_position)
        self.assertIn(
            '"scratch_synthetic_path_fixture_created"',
            isolated_run_source[fixture_setup_start:image_setup_position],
        )

        with tempfile.TemporaryDirectory() as temporary_dir:
            root = pathlib.Path(temporary_dir)
            unverified = root / "unverified"
            original_lstat = pathlib.Path.lstat
            original_mkdir = os.mkdir
            injected_error = OSError("injected post-mkdir identity read failure")
            mkdir_succeeded = False
            failed_once = False

            def mark_fixture_mkdir(
                path: str | os.PathLike[str], mode: int = 0o777
            ) -> None:
                nonlocal mkdir_succeeded
                original_mkdir(path, mode)
                if pathlib.Path(path) == unverified:
                    mkdir_succeeded = True

            def fail_first_unverified_lstat(path: pathlib.Path) -> os.stat_result:
                nonlocal failed_once
                if path == unverified and mkdir_succeeded and not failed_once:
                    failed_once = True
                    raise injected_error
                return original_lstat(path)

            with (
                mock.patch.object(
                    backup_drill.os, "mkdir", side_effect=mark_fixture_mkdir
                ),
                mock.patch.object(
                    pathlib.Path,
                    "lstat",
                    autospec=True,
                    side_effect=fail_first_unverified_lstat,
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "directory could not be created"
                ) as raised:
                    backup_drill._scratch_create_missing_protected_path_directories(
                        (unverified,), owner_uid=owner_uid, owner_gid=owner_gid
                    )
            self.assertIs(raised.exception.__cause__, injected_error)
            self.assertTrue(failed_once)
            self.assertEqual(
                getattr(raised.exception, "scratch_synthetic_path_fixture_created"),
                ((str(unverified), -1, -1),),
            )
            self.assertFalse(
                getattr(
                    raised.exception,
                    "scratch_synthetic_path_fixture_cleanup_confirmed",
                )
            )
            self.assertTrue(unverified.is_dir())
            unverified.rmdir()

            if os.geteuid() == 0:
                partial_root = root / "partial-fixture"
                partial_target = partial_root / "created-before-lstat-failure"
                partial_root.mkdir(mode=0o700)
                partial_created = False
                partial_lstat_failed = False
                original_mkdir = os.mkdir

                def create_partial_target(
                    path: str | os.PathLike[str], mode: int = 0o777
                ) -> None:
                    nonlocal partial_created
                    original_mkdir(path, mode)
                    if pathlib.Path(path) == partial_target:
                        partial_created = True

                def fail_partial_target_lstat(
                    path: pathlib.Path,
                ) -> os.stat_result:
                    nonlocal partial_lstat_failed
                    if path == partial_target and partial_created and not partial_lstat_failed:
                        partial_lstat_failed = True
                        raise OSError("injected partial fixture lstat failure")
                    return original_lstat(path)

                expected_partial = ((str(partial_target), -1, -1),)
                with (
                    mock.patch.dict(
                        os.environ,
                        {
                            "GITHUB_ACTIONS": "true",
                            "RUNNER_OS": "Linux",
                            "RUNNER_ENVIRONMENT": "github-hosted",
                        },
                    ),
                    mock.patch.object(
                        backup_drill,
                        "_scratch_synthetic_protected_path_targets",
                        return_value=(partial_target,),
                    ),
                    mock.patch.object(
                        backup_drill.os, "mkdir", side_effect=create_partial_target
                    ),
                    mock.patch.object(
                        pathlib.Path,
                        "lstat",
                        autospec=True,
                        side_effect=fail_partial_target_lstat,
                    ),
                    mock.patch.object(
                        backup_drill,
                        "_scratch_remove_created_protected_path_directories",
                        side_effect=RuntimeError("injected initial cleanup failure"),
                    ),
                ):
                    with self.assertRaisesRegex(
                        RuntimeError, "directory could not be created"
                    ) as prepared:
                        backup_drill._scratch_prepare_synthetic_protected_paths(
                            scratch_backend=backup_drill.SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1
                        )
                self.assertTrue(partial_lstat_failed)
                self.assertEqual(
                    getattr(
                        prepared.exception,
                        "scratch_synthetic_path_fixture_created",
                    ),
                    expected_partial,
                )
                self.assertFalse(
                    getattr(
                        prepared.exception,
                        "scratch_synthetic_path_fixture_cleanup_confirmed",
                    )
                )
                self.assertTrue(partial_target.is_dir())
                with self.assertRaisesRegex(
                    RuntimeError, "cleanup could not be confirmed"
                ):
                    backup_drill._scratch_remove_created_protected_path_directories(
                        expected_partial,
                        owner_uid=0,
                        owner_gid=0,
                    )
                self.assertTrue(partial_target.is_dir())
                partial_target.rmdir()

        rejected_parent_cases = (
            (
                "regular_file",
                stat.S_IFREG | 0o600,
                owner_uid,
                owner_gid,
                ["not_directory"],
            ),
            (
                "symlink",
                stat.S_IFLNK | 0o700,
                owner_uid,
                owner_gid,
                ["not_directory", "symlink"],
            ),
            (
                "uid_mismatch",
                stat.S_IFDIR | 0o700,
                owner_uid + 1,
                owner_gid,
                ["uid_mismatch"],
            ),
            (
                "gid_mismatch",
                stat.S_IFDIR | 0o700,
                owner_uid,
                owner_gid + 1,
                ["gid_mismatch"],
            ),
            (
                "group_or_world_writable",
                stat.S_IFDIR | 0o702,
                owner_uid,
                owner_gid,
                ["group_or_world_writable"],
            ),
            (
                "combined",
                stat.S_IFDIR | 0o777,
                owner_uid + 1,
                owner_gid + 1,
                ["uid_mismatch", "gid_mismatch", "group_or_world_writable"],
            ),
        )
        for case_name, mode, parent_uid, parent_gid, predicates in rejected_parent_cases:
            with self.subTest(parent_rejection=case_name):
                with tempfile.TemporaryDirectory() as temporary_dir:
                    root = pathlib.Path(temporary_dir)
                    target = root / "missing"
                    parent_component_index = len(root.parts) - 1
                    parent_info = types.SimpleNamespace(
                        st_mode=mode, st_uid=parent_uid, st_gid=parent_gid
                    )
                    original_lstat = pathlib.Path.lstat
                    parent_lstat_calls = 0

                    def fake_parent_lstat(path: pathlib.Path) -> os.stat_result:
                        nonlocal parent_lstat_calls
                        if path == target:
                            raise FileNotFoundError(2, "expected missing child")
                        if path == root:
                            parent_lstat_calls += 1
                            if parent_lstat_calls > 1:
                                return parent_info  # type: ignore[return-value]
                        return original_lstat(path)

                    with mock.patch.object(
                        pathlib.Path,
                        "lstat",
                        autospec=True,
                        side_effect=fake_parent_lstat,
                    ):
                        with self.assertRaisesRegex(
                            RuntimeError, "parent is not safely owned"
                        ) as raised:
                            backup_drill._scratch_create_missing_protected_path_directories(
                                (target,), owner_uid=owner_uid, owner_gid=owner_gid
                            )
                    self.assertTrue(
                        getattr(
                            raised.exception,
                            "scratch_synthetic_path_fixture_cleanup_confirmed",
                        )
                    )
                    self.assertEqual(
                        getattr(
                            raised.exception,
                            "scratch_synthetic_path_fixture_created",
                        ),
                        (),
                    )
                    backup_drill._retain_first_scratch_fault(
                        raised.exception,
                        origin="controller",
                        site="controller_synthetic_path_fixture",
                    )
                    first_fault = raised.exception.scratch_first_fault
                    expected_rejection = {
                        "schema": 1,
                        "target_index": 0,
                        "parent_component_index": parent_component_index,
                        "predicates": predicates,
                    }
                    self.assertEqual(
                        first_fault,
                        {
                            "schema": 1,
                            "origin": "controller",
                            "site": "controller_synthetic_path_fixture",
                            "exception_kind": "runtime",
                            "errno": None,
                            "synthetic_path_fixture_rejection": expected_rejection,
                        },
                    )
                    self.assertTrue(
                        backup_drill.valid_scratch_first_fault(first_fault)
                    )
                    self.assertTrue(
                        backup_drill.valid_scratch_synthetic_path_fixture_rejection(
                            expected_rejection
                        )
                    )
                    self.assertFalse(target.exists())
                    if case_name == "combined":
                        frame = backup_drill._scratch_first_fault_frame(first_fault)
                        self.assertIsNotNone(frame)
                        assert frame is not None
                        worker_report = json.dumps(
                            {
                                "ok": False,
                                "error": "fixed private failure marker",
                                "scratch_first_fault": first_fault,
                            },
                            ensure_ascii=False,
                            indent=2,
                        ).encode("utf-8") + b"\n"
                        self.assertEqual(
                            backup_drill._parse_bound_scratch_first_fault(
                                worker_report,
                                frame,
                                stdout_truncated=False,
                                stderr_truncated=False,
                            ),
                            first_fault,
                        )

        valid_rejection = {
            "schema": 1,
            "target_index": 0,
            "parent_component_index": 3,
            "predicates": ["uid_mismatch"],
        }
        for invalid_rejection in (
            {**valid_rejection, "target_index": True},
            {**valid_rejection, "target_index": len(backup_drill._SCRATCH_SYNTHETIC_PATH_FIXTURE_PARENT_COUNTS)},
            {**valid_rejection, "parent_component_index": 4},
            {**valid_rejection, "predicates": []},
            {**valid_rejection, "predicates": ["unknown"]},
            {**valid_rejection, "predicates": ["uid_mismatch", "uid_mismatch"]},
            {**valid_rejection, "predicates": ["gid_mismatch", "uid_mismatch"]},
            {**valid_rejection, "path": "/private/path"},
        ):
            self.assertFalse(
                backup_drill.valid_scratch_synthetic_path_fixture_rejection(
                    invalid_rejection
                )
            )

    def test_synthetic_protected_path_fixture_refuses_symlinks_and_identity_drift(self) -> None:
        owner_uid = os.geteuid()
        owner_gid = os.getegid()
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = pathlib.Path(temporary_dir)
            real = root / "real"
            real.mkdir()
            alias = root / "alias"
            alias.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "not a real directory"):
                backup_drill._scratch_create_missing_protected_path_directories(
                    (alias / "child",), owner_uid=owner_uid, owner_gid=owner_gid
                )
            self.assertFalse((real / "child").exists())

            created = backup_drill._scratch_create_missing_protected_path_directories(
                (root / "owned" / "leaf",),
                owner_uid=owner_uid,
                owner_gid=owner_gid,
            )
            leaf = root / "owned" / "leaf"
            leaf.rmdir()
            leaf.mkdir(mode=0o700)
            sentinel = leaf / "preserve.txt"
            sentinel.write_text("replacement", encoding="ascii")
            with self.assertRaisesRegex(RuntimeError, "cleanup could not be confirmed"):
                backup_drill._scratch_remove_created_protected_path_directories(
                    created, owner_uid=owner_uid, owner_gid=owner_gid
                )
            self.assertEqual(sentinel.read_text(encoding="ascii"), "replacement")

    def test_synthetic_protected_path_fixture_requires_hosted_root_context(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "root GitHub-hosted Linux runner"):
                backup_drill._scratch_prepare_synthetic_protected_paths()

    def test_synthetic_protected_path_fixture_matches_complete_unit_policy(self) -> None:
        targets = set(map(str, backup_drill._scratch_synthetic_protected_path_targets()))
        expected = set(backup_drill._SCRATCH_INACCESSIBLE_PATHS)
        if pathlib.Path("/var/run").is_symlink():
            expected.discard("/var/run/postgresql")
            expected.add("/run/postgresql")
        self.assertEqual(targets, expected)
        self.assertEqual(
            tuple(map(str, backup_drill._scratch_synthetic_protected_path_targets())),
            (
                "/opt/oldsparky/platform/shared",
                "/var/lib/postgresql",
                "/run/postgresql",
                "/etc/postgresql",
                "/root",
            ),
        )

    def test_disk_synthetic_fixture_adds_only_the_fixed_image_parent_chain(self) -> None:
        tmpfs_targets = backup_drill._scratch_synthetic_protected_path_targets(
            scratch_backend=backup_drill.SCRATCH_BACKEND_TMPFS_V1
        )
        disk_targets = backup_drill._scratch_synthetic_protected_path_targets(
            scratch_backend=backup_drill.SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1
        )
        self.assertEqual(disk_targets[: len(tmpfs_targets)], tmpfs_targets)
        self.assertEqual(
            disk_targets[len(tmpfs_targets) :],
            (
                backup_drill.SCRATCH_DISK_IMAGE_ROOT.parent,
                backup_drill.SCRATCH_DISK_IMAGE_ROOT,
            ),
        )

    def test_disk_image_fixture_directory_validator_checks_identity_and_mode(self) -> None:
        if os.geteuid() != 0:
            self.skipTest("disk fixture ownership checks require root in the privileged owner contour")
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = pathlib.Path(temporary_dir)
            old_root = backup_drill.SCRATCH_DISK_IMAGE_ROOT
            try:
                backup_drill.SCRATCH_DISK_IMAGE_ROOT = root / "oldsparky" / "backup-restore-images"
                created = backup_drill._scratch_create_missing_protected_path_directories(
                    (
                        backup_drill.SCRATCH_DISK_IMAGE_ROOT.parent,
                        backup_drill.SCRATCH_DISK_IMAGE_ROOT,
                    ),
                    owner_uid=0,
                    owner_gid=0,
                )
                backup_drill._scratch_validate_synthetic_disk_image_fixture_dirs(
                    paths=(root, root / "oldsparky", backup_drill.SCRATCH_DISK_IMAGE_ROOT.parent, backup_drill.SCRATCH_DISK_IMAGE_ROOT)
                )
                self.assertEqual(len(created), 2)
                self.assertEqual(
                    stat.S_IMODE(backup_drill.SCRATCH_DISK_IMAGE_ROOT.parent.stat().st_mode),
                    0o700,
                )
                self.assertEqual(
                    stat.S_IMODE(backup_drill.SCRATCH_DISK_IMAGE_ROOT.stat().st_mode),
                    0o700,
                )
                backup_drill._scratch_remove_created_protected_path_directories(
                    created, owner_uid=0, owner_gid=0
                )
                self.assertFalse((root / "oldsparky").exists())
            finally:
                backup_drill.SCRATCH_DISK_IMAGE_ROOT = old_root

    def test_synthetic_pg16_restore_diagnostic_under_root(self) -> None:
        if (
            os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
        ):
            self.skipTest("real PostgreSQL restore diagnostics run only in the ephemeral GitHub-hosted gate")
        self.assertEqual(os.geteuid(), 0)
        source_sha = os.environ.get("GITHUB_SHA", "")
        self.assertRegex(source_sha, r"^[0-9a-f]{40}$")
        original_signal_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        scratch_backend = os.environ.get(
            "PLATFORM_SCRATCH_BACKEND", backup_drill.SCRATCH_BACKEND_TMPFS_V1
        )
        self.assertIn(scratch_backend, backup_drill.SCRATCH_BACKENDS)
        report = _run_synthetic_restore_diagnostic_for_ci(
            source_sha=source_sha, scratch_backend=scratch_backend
        )
        self.assertEqual(
            signal.pthread_sigmask(signal.SIG_BLOCK, set()),
            original_signal_mask,
            "successful synthetic restore must restore the caller signal mask after cleanup",
        )
        self.assertTrue(backup_drill.valid_scratch_synthetic_restore_diagnostic(report))
        self.assertEqual(
            [arm["name"] for arm in report["arms"]],
            ["baseline", "minimal_no_senders", "minimal_no_senders_single_transaction"],
        )
        self.assertEqual(len({arm["logical_sha256"] for arm in report["arms"]}), 1)
        self.assertEqual(report["fixture_row_count"], backup_drill.SCRATCH_SYNTHETIC_PAYLOAD_ROWS)
        self.assertEqual(report["fixture_payload_bytes"], 33_554_432)
        for arm in report["arms"]:
            with self.subTest(arm=arm["name"]):
                self.assertEqual(
                    [phase["phase"] for phase in arm["phases"]],
                    ["restore_platform", "restore_public"],
                )
                for phase in arm["phases"]:
                    self.assertGreater(phase["memory_peak_bytes"], 0)
                    if scratch_backend == backup_drill.SCRATCH_BACKEND_TMPFS_V1:
                        self.assertGreater(phase["tmpfs_peak_sampled_bytes"], 0)
                    else:
                        self.assertGreater(phase["disk_peak_sampled_bytes"], 0)
                        self.assertGreater(
                            phase["disk_capacity_bytes"], phase["disk_peak_sampled_bytes"]
                        )
                    self.assertGreaterEqual(phase["wal_delta_bytes"], 0)
                    self.assertGreater(phase["restore_command_elapsed_us"], 0)
                    self.assertGreaterEqual(
                        phase["phase_elapsed_us"], phase["restore_command_elapsed_us"]
                    )
        artifact_path = _write_synthetic_restore_diagnostic_artifact(
            report, source_sha=source_sha
        )
        self.assertEqual(
            json.loads(artifact_path.read_text(encoding="ascii")),
            {
                "kind": "synthetic_restore_diagnostic_artifact",
                "schema": 1,
                "source_sha": source_sha,
                "scratch_backend": scratch_backend,
                "diagnostic": report,
            },
        )

    def test_scratch_process_start_time_parses_binary_proc_stat_safely(self) -> None:
        actual = self._original_proc_start_time(str(os.getpid()))
        self.assertRegex(actual, r"^[0-9]+$")

        tail = [b"S", *([b"0"] * 18), b"987654"]
        fixture = b"123 (comm with ) paren) " + b" ".join(tail) + b"\n"
        with mock.patch.object(
            pathlib.Path, "open", return_value=io.BytesIO(fixture)
        ):
            self.assertEqual(self._original_proc_start_time("123"), "987654")

        malformed = (
            b"123 (comm) S 1 2\n",
            b"123 (comm) " + b" ".join([b"S", *([b"0"] * 18), b"12x"]) + b"\n",
            b"123 (comm) " + b" ".join([b"S", *([b"0"] * 18), b"\xff"]) + b"\n",
            b"123 comm S " + b"0 " * 20,
            b"x" * 8193,
        )
        for payload in malformed:
            with self.subTest(size=len(payload)):
                with mock.patch.object(
                    pathlib.Path, "open", return_value=io.BytesIO(payload)
                ):
                    with self.assertRaisesRegex(RuntimeError, "process identity is malformed"):
                        self._original_proc_start_time("123")

    def _assert_scratch_resource_attribution_is_identity_bound_and_private(self) -> None:
        pid = os.getpid()
        worker_pid = 4321
        invocation = "a" * 32
        unit = "oldsparky-backup-restore-" + "b" * 24 + ".service"
        control_group = "/system.slice/" + unit
        snapshot = {
            "properties": {
                "ActiveState": "active",
                "ExecMainPID": str(worker_pid),
                "InvocationID": invocation,
                "ControlGroup": control_group,
            }
        }
        cgroup_payloads = {
            "memory.current": b"331210752\n",
            "memory.stat": b"anon 1200\nfile 900\nshmem 100\n",
            "memory.events": b"high 1\nmax 2\noom 0\noom_kill 0\n",
        }
        identities = {
            10: (1, 2),
            11: (1, 3),
            12: (1, 4),
        }

        def fake_open(path, flags, *args, **kwargs):
            if path == "/sys/fs/cgroup":
                return 10
            if path == "system.slice":
                return 11
            if path == unit:
                return 12
            if path in cgroup_payloads:
                return 100 + tuple(cgroup_payloads).index(path)
            raise AssertionError("unexpected descriptor open in attribution fixture")

        def fake_fstat(descriptor):
            dev, ino = identities[descriptor]
            return types.SimpleNamespace(st_dev=dev, st_ino=ino)

        def fake_stat(path, *, dir_fd=None, follow_symlinks=True):
            self.assertFalse(follow_symlinks)
            self.assertIn(path, {"system.slice", unit})
            dev, ino = (1, 3) if path == "system.slice" else (1, 4)
            return types.SimpleNamespace(st_dev=dev, st_ino=ino)

        def fake_proc_file(observed_pid, name):
            self.assertEqual(name, "cgroup")
            self.assertIn(observed_pid, {str(worker_pid)})
            return f"0::/system.slice/{unit}\n".encode("ascii")

        def fake_cgroup_file(_descriptor, name):
            return cgroup_payloads.get(name)

        with (
            mock.patch.object(backup_drill, "_scratch_process_start_time", return_value="123456"),
            mock.patch.object(
                backup_drill,
                "_scratch_proc_status_sample",
                side_effect=lambda observed_pid: (observed_pid, 1, 40_000_000),
            ),
            mock.patch.object(backup_drill, "_scratch_private_proc_file", side_effect=fake_proc_file),
            mock.patch.object(backup_drill, "_scratch_private_cgroup_file", side_effect=fake_cgroup_file),
            mock.patch.object(backup_drill.os, "open", side_effect=fake_open),
            mock.patch.object(backup_drill.os, "fstat", side_effect=fake_fstat),
            mock.patch.object(backup_drill.os, "stat", side_effect=fake_stat),
            mock.patch.object(backup_drill.os, "close"),
            mock.patch.object(backup_drill.os, "getpid", return_value=pid),
            mock.patch.object(
                backup_drill.time,
                "monotonic_ns",
                side_effect=(20_000_000, 21_000_000, 22_000_000, 23_000_000),
            ),
        ):
            attribution = backup_drill._scratch_private_resource_attribution(
                snapshot=snapshot,
                worker_identity=(str(worker_pid), invocation, "123456"),
                control_group=control_group,
                launch_monotonic_ns=1_000_000,
            )
            with mock.patch.object(backup_drill, "SCRATCH_RESOURCE_ATTRIBUTION_MAX_BYTES", 1):
                over_cap = backup_drill._scratch_private_resource_attribution(
                    snapshot=snapshot,
                    worker_identity=(str(worker_pid), invocation, "123456"),
                    control_group=control_group,
                    launch_monotonic_ns=1_000_000,
                )
        self.assertTrue(backup_drill.valid_scratch_resource_attribution(attribution))
        self.assertEqual(attribution["status"], "complete")
        self.assertEqual(attribution["sample_offset_ms"], 20)
        self.assertEqual(attribution["controller"]["rss_bytes"], 40_000_000)
        self.assertEqual(attribution["worker"]["rss_bytes"], 40_000_000)
        self.assertEqual(attribution["cgroup"]["memory_current_bytes"], 331_210_752)
        self.assertEqual(attribution["cgroup"]["shmem_bytes"], 100)
        self.assertEqual(over_cap["status"], "unknown")
        self.assertEqual(over_cap["unknown_reason"], "cap_exceeded")

        refusal = RuntimeError("unchanged reserve refusal")
        refusal.scratch_host_guard_diagnostic = {
            "schema": 2,
            "guard_site": "parent_active_poll",
            "guard_reason": "memory_reserve",
            "available_bytes": backup_drill.SCRATCH_MEMORY_ADMISSION_BYTES - 1,
            "required_bytes": backup_drill.SCRATCH_MEMORY_ADMISSION_BYTES,
        }
        refusal.scratch_resource_attribution = attribution
        private_report = backup_drill.failure_report_for_exception(
            refusal, private_scratch_diagnostics=True
        )
        public_report = backup_drill.failure_report_for_exception(
            refusal, private_scratch_diagnostics=False
        )
        self.assertEqual(private_report["error"], str(refusal))
        self.assertEqual(private_report["scratch_resource_attribution"], attribution)
        self.assertNotIn("scratch_resource_attribution", public_report)
        self.assertNotIn("scratch_host_guard_diagnostic", public_report)

    def _assert_scratch_resource_attribution_fails_closed_on_identity_and_parser_errors(self) -> None:
        complete = {
            "schema": 1,
            "status": "complete",
            "sample_offset_ms": 1,
            "unknown_reason": None,
            "controller": {"identity_bound": True, "rss_bytes": 1},
            "worker": {"identity_bound": True, "rss_bytes": 2},
            "cgroup": {
                "identity_bound": True,
                "memory_current_bytes": 3,
                "anon_bytes": 1,
                "file_bytes": 2,
                "shmem_bytes": 0,
                "events": {"high": 0, "max": 0, "oom": 0, "oom_kill": 0},
            },
        }
        self.assertTrue(backup_drill.valid_scratch_resource_attribution(complete))
        for malformed in (
            {**complete, "schema": True},
            {**complete, "unknown_reason": "host-memory-override"},
            {**complete, "unexpected": 1},
            {**complete, "worker": {"identity_bound": False, "rss_bytes": 2}},
            {
                **complete,
                "cgroup": {**complete["cgroup"], "memory_current_bytes": True},
            },
        ):
            with self.subTest(malformed=malformed):
                self.assertFalse(backup_drill.valid_scratch_resource_attribution(malformed))

        unknown = backup_drill._scratch_unknown_resource_attribution("identity_drift", sample_offset_ms=4)
        self.assertTrue(backup_drill.valid_scratch_resource_attribution(unknown))
        self.assertEqual(unknown["status"], "unknown")
        self.assertEqual(unknown["unknown_reason"], "identity_drift")
        self.assertIsNone(unknown["cgroup"]["memory_current_bytes"])
        self.assertFalse(backup_drill.valid_scratch_resource_attribution(
            {**unknown, "unknown_reason": "unrecognized"}
        ))
        self.assertEqual(
            backup_drill._scratch_private_resource_attribution(
                snapshot=None,
                worker_identity=None,
                control_group=None,
                launch_monotonic_ns=None,
            )["unknown_reason"],
            "sample_unavailable",
        )
        self.assertEqual(
            backup_drill._scratch_private_resource_attribution(
                snapshot={"properties": {"ActiveState": "inactive"}},
                worker_identity=("4321", "a" * 32, "123456"),
                control_group="/system.slice/oldsparky-backup-restore-" + "b" * 24 + ".service",
                launch_monotonic_ns=time.monotonic_ns() - 1,
            )["unknown_reason"],
            "identity_drift",
        )
        self.assertIsNone(backup_drill._scratch_private_counter(b"not-a-counter\n"))
        self.assertIsNone(
            backup_drill._scratch_private_key_values(
                b"anon 1\nfile 2\nshmem 3\nshmem 4\n", {"anon", "file", "shmem"}
            )
        )

    def _assert_parent_active_poll_sampler_failure_preserves_guard_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="scratch-attribution-guard-") as temporary_dir:
            archive_path = pathlib.Path(temporary_dir) / "platformdb.dump"
            archive_path.write_bytes(b"PGDMPsynthetic archive")
            archive_path.chmod(0o600)
            archive_identity = (archive_path.stat().st_dev, archive_path.stat().st_ino)

            def root_owned_archive_stat(value):
                # The production path requires a root-owned archive.  This
                # contour also runs as an unprivileged CI user, so project
                # only the fixture's ownership metadata instead of weakening
                # the production check or relying on chown privileges.
                return types.SimpleNamespace(
                    st_mode=value.st_mode,
                    st_uid=0,
                    st_gid=value.st_gid,
                    st_nlink=value.st_nlink,
                    st_dev=value.st_dev,
                    st_ino=value.st_ino,
                    st_size=value.st_size,
                    st_mtime_ns=value.st_mtime_ns,
                    st_ctime_ns=value.st_ctime_ns,
                )

            actual_fstat = os.fstat

            def fstat_with_root_owned_archive(fd):
                value = actual_fstat(fd)
                if (value.st_dev, value.st_ino) == archive_identity:
                    return root_owned_archive_stat(value)
                return value

            token = uuid.uuid4().hex[:24]
            mountpoint = pathlib.Path(f"/run/oldsparky-backup-restore-{token}")
            mapped_mountpoint = pathlib.Path(temporary_dir) / f"mountpoint-{token}"
            if mountpoint.exists():
                self.fail("unique scratch fixture mountpoint unexpectedly exists")

            # The production contract uses a root-owned directory directly
            # under /run.  CI runs this regression as an unprivileged user,
            # so route only this exact generated path into the test-owned
            # temporary directory.  Every unrelated filesystem operation is
            # delegated unchanged to the real OS function.
            actual_mkdir = os.mkdir
            actual_rmdir = os.rmdir
            actual_listdir = os.listdir
            actual_scandir = os.scandir
            actual_stat = os.stat
            actual_lstat = os.lstat

            def mapped_path(path):
                if not isinstance(path, (str, bytes, os.PathLike)):
                    return path
                candidate = pathlib.Path(path)
                return mapped_mountpoint if candidate == mountpoint else path

            def mkdir_in_fixture(path, mode=0o777, *, dir_fd=None):
                target = mapped_path(path)
                if dir_fd is None:
                    return actual_mkdir(target, mode)
                return actual_mkdir(target, mode, dir_fd=dir_fd)

            def rmdir_in_fixture(path, *, dir_fd=None):
                target = mapped_path(path)
                if dir_fd is None:
                    return actual_rmdir(target)
                return actual_rmdir(target, dir_fd=dir_fd)

            def scandir_in_fixture(path="."):
                return actual_scandir(mapped_path(path))

            def listdir_in_fixture(path="."):
                return actual_listdir(mapped_path(path))

            def stat_with_fixture_paths(path, *args, **kwargs):
                target = mapped_path(path)
                value = actual_stat(target, *args, **kwargs)
                if pathlib.Path(path) == mountpoint:
                    return types.SimpleNamespace(
                        st_mode=value.st_mode,
                        st_uid=0,
                        st_gid=0,
                        st_nlink=value.st_nlink,
                        st_dev=value.st_dev,
                        st_ino=value.st_ino,
                        st_size=value.st_size,
                        st_mtime_ns=value.st_mtime_ns,
                        st_ctime_ns=value.st_ctime_ns,
                    )
                if pathlib.Path(path) == archive_path and kwargs.get("follow_symlinks") is False:
                    return root_owned_archive_stat(value)
                return value

            def lstat_with_fixture_paths(path, *args, **kwargs):
                value = actual_lstat(mapped_path(path), *args, **kwargs)
                if pathlib.Path(path) == mountpoint:
                    return types.SimpleNamespace(
                        st_mode=value.st_mode,
                        st_uid=0,
                        st_gid=0,
                        st_nlink=value.st_nlink,
                        st_dev=value.st_dev,
                        st_ino=value.st_ino,
                        st_size=value.st_size,
                        st_mtime_ns=value.st_mtime_ns,
                        st_ctime_ns=value.st_ctime_ns,
                    )
                if pathlib.Path(path) == archive_path:
                    return root_owned_archive_stat(value)
                return value

            class Launcher:
                def __init__(self, stdout, stderr) -> None:
                    self.stdin = io.BytesIO()
                    self.stdout = stdout
                    self.stderr = stderr
                    self.returncode = None

                def poll(self):
                    return self.returncode

                def wait(self, *, timeout=None):
                    del timeout
                    events.append("wait")
                    if self.returncode is None:
                        self.returncode = 1
                    return self.returncode

                def kill(self):
                    events.append("kill")
                    self.returncode = -9

            def eof_pipe():
                read_fd, write_fd = os.pipe()
                os.close(write_fd)
                return os.fdopen(read_fd, "rb", buffering=0)

            events: list[str] = []
            launcher = Launcher(eof_pipe(), eof_pipe())
            guard_error = RuntimeError("fixed parent active-poll reserve refusal")
            host_guard = {
                "schema": 2,
                "guard_site": "parent_active_poll",
                "guard_reason": "memory_reserve",
                "available_bytes": backup_drill.SCRATCH_MEMORY_ADMISSION_BYTES - 4096,
                "required_bytes": backup_drill.SCRATCH_MEMORY_ADMISSION_BYTES,
            }
            guard_error.scratch_host_guard_diagnostic = host_guard

            def host_guard_at(site: str) -> None:
                if site == "parent_active_poll":
                    raise guard_error

            def stop_unit(_unit_name: str) -> None:
                events.append("stop")
                launcher.returncode = 0

            def sample_failure(**_kwargs) -> dict[str, object]:
                events.append("sample")
                raise RuntimeError("secondary sampler failure")

            with (
                mock.patch.object(backup_drill.os, "geteuid", return_value=0),
                mock.patch.object(
                    backup_drill.os, "fstat", side_effect=fstat_with_root_owned_archive
                ),
                mock.patch.object(backup_drill.os, "stat", side_effect=stat_with_fixture_paths),
                mock.patch.object(backup_drill.os, "lstat", side_effect=lstat_with_fixture_paths),
                mock.patch.object(backup_drill.os, "mkdir", side_effect=mkdir_in_fixture),
                mock.patch.object(backup_drill.os, "rmdir", side_effect=rmdir_in_fixture),
                mock.patch.object(backup_drill.os, "listdir", side_effect=listdir_in_fixture),
                mock.patch.object(backup_drill.os, "scandir", side_effect=scandir_in_fixture),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "require_restore_headroom"),
                mock.patch.object(backup_drill, "_scratch_host_guard", side_effect=host_guard_at),
                mock.patch.object(backup_drill, "_scratch_unit_command", return_value=["systemd-run"]),
                mock.patch.object(
                    backup_drill,
                    "_scratch_mountpoint_is_mounted",
                    return_value=False,
                ) as mountpoint_check,
                mock.patch.object(
                    backup_drill,
                    "_scratch_service_snapshot",
                    return_value={
                        "properties": {
                            "LoadState": "not-found",
                            "ActiveState": "inactive",
                            "SubState": "dead",
                            "Result": "success",
                            "ExecMainStatus": "0",
                            "ExecMainPID": "0",
                            "ControlGroup": "",
                        }
                    },
                ),
                mock.patch.object(backup_drill, "_scratch_stop_unit", side_effect=stop_unit),
                mock.patch.object(
                    backup_drill,
                    "_scratch_private_resource_attribution",
                    side_effect=sample_failure,
                ) as sampler,
                mock.patch.object(backup_drill.subprocess, "Popen", return_value=launcher),
                mock.patch.object(backup_drill.secrets, "token_hex", return_value=token),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "fixed parent active-poll reserve refusal"
                ) as failure:
                    backup_drill._run_isolated_restore_drill(
                        archive_path,
                        timestamp_slug="20261014T010203Z",
                        private_scratch_diagnostics=True,
                    )

            sampler.assert_called_once()
            self.assertIs(failure.exception, guard_error)
            self.assertEqual(str(failure.exception), "fixed parent active-poll reserve refusal")
            self.assertEqual(failure.exception.scratch_host_guard_diagnostic, host_guard)
            self.assertEqual(failure.exception.scratch_resource_attribution["status"], "unknown")
            self.assertEqual(
                failure.exception.scratch_resource_attribution["unknown_reason"], "internal_error"
            )
            self.assertLess(events.index("sample"), events.index("stop"))
            self.assertLess(events.index("stop"), events.index("wait"))
            self.assertEqual(launcher.returncode, 0)
            mountpoint_check.assert_has_calls([mock.call(mountpoint), mock.call(mountpoint)])
            self.assertFalse(mapped_mountpoint.exists())
            self.assertFalse(mountpoint.exists())

    def test_scratch_postgres_directory_setup_without_cap_fowner(self) -> None:
        self.assertEqual(os.geteuid(), 0)
        setpriv = shutil.which("setpriv")
        self.assertIsNotNone(setpriv)
        postgres = backup_drill.pwd.getpwnam("postgres")
        with tempfile.TemporaryDirectory(prefix="backup-scratch-mode-") as temporary_dir:
            scratch_root = pathlib.Path(temporary_dir) / "scratch"
            scratch_root.mkdir(mode=0o700)
            setup_prefix = "\n".join(
                (
                    "import importlib.util, json, os, pathlib, sys",
                    "source, root, uid, gid = sys.argv[1:]",
                    "uid, gid = int(uid), int(gid)",
                    "status = pathlib.Path('/proc/self/status').read_text()",
                    "effective = int(next(line.split()[1] for line in status.splitlines() if line.startswith('CapEff:')), 16)",
                    "assert effective == 0xC3, 'worker capability set differs from the reviewed four-capability boundary'",
                    "root = pathlib.Path(root)",
                    "os.chown(root, uid, gid)",
                )
            )
            old_order_script = setup_prefix + "\n" + "\n".join(
                (
                    "data = root / 'data'",
                    "data.mkdir(mode=0o700)",
                    "os.chown(data, uid, gid)",
                    "try:",
                    "    os.chmod(data, 0o700)",
                    "except OSError as exc:",
                    "    print(json.dumps({'stage': 'chmod', 'errno': exc.errno}))",
                    "else:",
                    "    raise AssertionError('old chown-before-chmod order unexpectedly passed')",
                )
            )
            old_order = subprocess.run(
                [str(setpriv), "--bounding-set=-all,+chown,+setuid,+setgid,+dac_override", "--", sys.executable,
                 "-c", old_order_script, str(SCRIPT_PATH), str(scratch_root),
                 str(postgres.pw_uid), str(postgres.pw_gid)],
                check=False, capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(old_order.returncode, 0, old_order.stderr)
            self.assertEqual(
                json.loads(old_order.stdout),
                {"stage": "chmod", "errno": 1},
            )

            scratch_root = pathlib.Path(temporary_dir) / "scratch-fixed"
            scratch_root.mkdir(mode=0o700)
            fixed_order_script = setup_prefix + "\n" + "\n".join(
                (
                    "spec = importlib.util.spec_from_file_location('backup_drill', source)",
                    "module = importlib.util.module_from_spec(spec)",
                    "sys.modules[spec.name] = module",
                    "spec.loader.exec_module(module)",
                    "for name in ('data', 'socket'):",
                    "    module._scratch_prepare_postgres_directory(root / name, uid=uid, gid=gid)",
                )
            )
            fixed_order = subprocess.run(
                [str(setpriv), "--bounding-set=-all,+chown,+setuid,+setgid,+dac_override", "--", sys.executable,
                 "-c", fixed_order_script, str(SCRIPT_PATH), str(scratch_root),
                 str(postgres.pw_uid), str(postgres.pw_gid)],
                check=False, capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(fixed_order.returncode, 0, fixed_order.stderr)
            for name in ("data", "socket"):
                target = scratch_root / name
                target_stat = target.stat()
                with self.subTest(path=name):
                    self.assertEqual(target_stat.st_uid, postgres.pw_uid)
                    self.assertEqual(target_stat.st_gid, postgres.pw_gid)
                    self.assertEqual(target_stat.st_mode & 0o777, 0o700)

    @staticmethod
    def _write_backup_pair(
        output_dir: pathlib.Path,
        dump_name: str,
        payload: bytes,
        *,
        restore_verified: bool = True,
    ) -> tuple[pathlib.Path, pathlib.Path]:
        dump_path = output_dir / dump_name
        metadata_path = dump_path.with_suffix(".json")
        dump_path.write_bytes(payload)
        metadata_path.write_text(
            json.dumps(
                {
                    "format_version": 2,
                    "dump_file": dump_path.name,
                    "size_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "restore_verified": restore_verified,
                }
            ),
            encoding="utf-8",
        )
        return dump_path, metadata_path

    @staticmethod
    def _write_protected_backup_manifest(
        path: pathlib.Path, dump_names: list[str]
    ) -> pathlib.Path:
        path.write_text(
            json.dumps(
                {"schema_version": 1, "protected_dump_files": dump_names},
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    def _create_protected_backup(
        self,
        output_dir: pathlib.Path,
        manifest: pathlib.Path | None,
        *,
        keep: int,
        restore_result: int = 12,
        scratch_backend: str = backup_drill.SCRATCH_BACKEND_TMPFS_V1,
    ) -> dict[str, Any]:
        args = argparse.Namespace(
            env_file=output_dir.parent / ".env.platform",
            output_dir=output_dir,
            keep=keep,
            dump_only=False,
            rotate_existing=True,
            protected_backup_manifest=manifest,
            private_scratch_diagnostics=False,
            scratch_backend=scratch_backend,
        )
        isolated_restore = mock.Mock(return_value=restore_result)
        manifest_owner = (
            self._project_manifest_owner(manifest, 0)
            if manifest is not None
            else contextlib.nullcontext()
        )
        with manifest_owner:
            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {"PLATFORM_DATABASE_URL": "postgresql://platform_user@localhost/platformdb"},
                ),
                mock.patch.object(backup_drill, "load_env", return_value={}),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(
                    backup_drill,
                    "run_disk_guarded_command",
                    side_effect=self._fake_guarded_dump,
                ),
                mock.patch.object(backup_drill, "require_restore_headroom"),
                mock.patch.object(
                    backup_drill,
                    "_run_isolated_restore_drill",
                    isolated_restore,
                ),
                mock.patch.object(
                    backup_drill,
                    "utc_now",
                    return_value=dt.datetime(2026, 10, 6, 12, 0, 0, tzinfo=dt.UTC),
                ),
            ):
                result = backup_drill.create_backup(args)
        self._last_isolated_restore_call = isolated_restore.call_args
        return result

    def test_create_backup_forwards_selected_scratch_backend(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported backup scratch backend"):
            backup_drill.create_backup(argparse.Namespace(scratch_backend="unknown"))

        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir) / "backups"
            output_dir.mkdir()

            self._create_protected_backup(
                output_dir,
                None,
                keep=2,
                scratch_backend=backup_drill.SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1,
            )

        self.assertEqual(
            self._last_isolated_restore_call.kwargs["scratch_backend"],
            backup_drill.SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1,
        )

    @contextlib.contextmanager
    def _project_manifest_owner(self, path: pathlib.Path, uid: int):
        """Project only UID for this synthetic manifest; keep real stat identity."""

        original_path_lstat = pathlib.Path.lstat
        original_fstat = os.fstat
        manifest_info = original_path_lstat(path)

        def with_uid(info: os.stat_result) -> types.SimpleNamespace:
            return types.SimpleNamespace(
                **{
                    field: (uid if field == "st_uid" else getattr(info, field))
                    for field in (
                        "st_dev",
                        "st_ino",
                        "st_mode",
                        "st_uid",
                        "st_gid",
                        "st_nlink",
                        "st_size",
                        "st_mtime_ns",
                        "st_ctime_ns",
                    )
                }
            )

        def projected_lstat(candidate: pathlib.Path):
            info = original_path_lstat(candidate)
            if candidate == path:
                return with_uid(info)
            return info

        def projected_fstat(descriptor: int):
            info = original_fstat(descriptor)
            if (info.st_dev, info.st_ino) == (manifest_info.st_dev, manifest_info.st_ino):
                return with_uid(info)
            return info

        with (
            mock.patch.object(pathlib.Path, "lstat", autospec=True, side_effect=projected_lstat),
            mock.patch.object(backup_drill.os, "fstat", side_effect=projected_fstat),
        ):
            yield

    def _assert_latest_verification_preflight_rejects(
        self,
        output_dir: pathlib.Path,
        env_file: pathlib.Path,
        message: str,
    ) -> None:
        with mock.patch.object(backup_drill, "require_commands") as require_commands:
            with self.assertRaisesRegex((RuntimeError, OSError), message):
                backup_drill.verify_latest_existing_backup(
                    output_dir, env_file, max_age_hours=24
                )
        require_commands.assert_not_called()

    def test_parse_database_url_accepts_platformdb_and_decodes_credentials(self) -> None:
        target = backup_drill.parse_database_url(
            "postgresql+asyncpg://platform%5Fuser:p%40ss@127.0.0.1:5433/platformdb"
        )

        self.assertEqual(target.username, "platform_user")
        self.assertEqual(target.password, "p@ss")
        self.assertEqual(target.host, "127.0.0.1")
        self.assertEqual(target.port, 5433)
        self.assertEqual(target.database, "platformdb")

    def test_parse_database_url_refuses_legacy_database(self) -> None:
        with self.assertRaisesRegex(ValueError, "expected the isolated platformdb"):
            backup_drill.parse_database_url(
                "postgresql+asyncpg://platform_user:secret@127.0.0.1:5432/sparkydb"
            )

    def test_scratch_terminal_diagnostic_schema_is_closed_and_keeps_initial_state(self) -> None:
        initial_snapshot = {
            "properties": {
                "ActiveState": "active",
                "SubState": "running",
                "Result": "success",
                "ExecMainStatus": "0",
            }
        }
        terminal_snapshot = {
            "load_state": "not-found",
            "active_state": "inactive",
            "sub_state": "dead",
            "result": "success",
            "exec_main_status": 0,
            "exec_main_code": "exited",
            "start_token_sent": False,
            "completion_token_sent": False,
        }
        diagnostic = backup_drill._scratch_unit_diagnostic(
            initial_snapshot,
            launcher_exit_status=1,
            stdout_prefix=b"",
            stdout_observed=0,
            stdout_complete=True,
            stderr_prefix=b"",
            stderr_observed=0,
            stderr_complete=True,
            phase="confinement_verification_failed",
            verification_error="os_error",
            verification_errno=2,
            terminal_snapshot=terminal_snapshot,
        )
        self.assertTrue(backup_drill.valid_scratch_unit_diagnostic(diagnostic))
        assert diagnostic is not None
        self.assertEqual(diagnostic["schema"], 2)
        self.assertEqual(diagnostic["active_state"], "active")
        self.assertEqual(diagnostic["terminal_snapshot"]["active_state"], "inactive")

        schema1 = dict(diagnostic)
        schema1["schema"] = 1
        del schema1["terminal_snapshot"]
        self.assertTrue(backup_drill.valid_scratch_unit_diagnostic(schema1))
        for malformed_terminal in (
            {**terminal_snapshot, "private_path": "/root/private"},
            {**terminal_snapshot, "start_token_sent": 1},
            {**terminal_snapshot, "active_state": "unknown-secret-state"},
            {**terminal_snapshot, "exec_main_code": "private-status"},
        ):
            malformed = {**diagnostic, "terminal_snapshot": malformed_terminal}
            self.assertFalse(backup_drill.valid_scratch_unit_diagnostic(malformed))

        root = "/run/oldsparky-backup-restore-" + ("c" * 24)
        synthetic_args = argparse.Namespace(
            _scratch_restore_worker=True,
            _scratch_authorization_probe=False,
            _scratch_synthetic_restore_diagnostic=True,
            _scratch_root=root,
            _scratch_timestamp_slug="20261018T010203Z",
            _scratch_archive=None,
            _scratch_archive_dev=None,
            _scratch_archive_ino=None,
            _scratch_archive_size=None,
            _scratch_archive_sha256=None,
            as_json=False,
            private_scratch_diagnostics=False,
        )
        worker_report = {"kind": "synthetic worker reached after authorization"}
        worker_stdout = io.TextIOWrapper(io.BytesIO())
        with (
            mock.patch.object(backup_drill, "parse_args", return_value=synthetic_args),
            mock.patch.object(
                backup_drill, "_scratch_read_worker_token",
                side_effect=[backup_drill.SCRATCH_START_TOKEN, backup_drill.SCRATCH_DONE_TOKEN],
            ) as read_token,
            mock.patch.object(backup_drill, "_scratch_mount_snapshot"),
            mock.patch.object(backup_drill, "_scratch_cgroup_snapshot"),
            mock.patch.object(backup_drill, "_raise_scratch_host_memory_reserve"),
            mock.patch.object(backup_drill, "_scratch_synthetic_restore_diagnostic_worker", return_value=worker_report) as run_synthetic,
            mock.patch.object(backup_drill.pathlib.Path, "is_dir", return_value=True),
            mock.patch.object(backup_drill.os, "geteuid", return_value=0),
            mock.patch.object(backup_drill.sys, "stdin", io.BytesIO()),
            mock.patch.object(backup_drill.sys, "stdout", worker_stdout),
        ):
            self.assertEqual(backup_drill.main(), 0)
        self.assertEqual(
            [call.args[1] for call in read_token.call_args_list],
            [backup_drill.SCRATCH_START_TOKEN, backup_drill.SCRATCH_DONE_TOKEN],
        )
        run_synthetic.assert_called_once()
        worker_stdout.flush()
        self.assertEqual(
            json.loads(worker_stdout.buffer.getvalue().decode("ascii")), worker_report
        )

        forbidden_archive_args = argparse.Namespace(
            **{
                **vars(synthetic_args),
                "_scratch_archive": "/tmp/external.dump",
            }
        )
        forbidden_stderr = io.StringIO()
        forbidden_stdout = io.TextIOWrapper(io.BytesIO())
        with (
            mock.patch.object(backup_drill, "parse_args", return_value=forbidden_archive_args),
            mock.patch.object(
                backup_drill, "_scratch_read_worker_token",
                return_value=backup_drill.SCRATCH_START_TOKEN,
            ),
            mock.patch.object(backup_drill, "_scratch_mount_snapshot"),
            mock.patch.object(backup_drill, "_scratch_cgroup_snapshot"),
            mock.patch.object(backup_drill, "_raise_scratch_host_memory_reserve"),
            mock.patch.object(backup_drill, "_scratch_synthetic_restore_diagnostic_worker") as forbidden_run,
            mock.patch.object(backup_drill.pathlib.Path, "is_dir", return_value=True),
            mock.patch.object(backup_drill.os, "geteuid", return_value=0),
            mock.patch.object(backup_drill.sys, "stdin", io.BytesIO()),
            mock.patch.object(backup_drill.sys, "stdout", forbidden_stdout),
            mock.patch.object(backup_drill.sys, "stderr", forbidden_stderr),
        ):
            self.assertEqual(backup_drill.main(), 1)
        forbidden_run.assert_not_called()
        forbidden_stdout.flush()
        rejected_input_fault = backup_drill._parse_bound_scratch_first_fault(
            forbidden_stdout.buffer.getvalue(),
            forbidden_stderr.getvalue().encode("ascii"),
            stdout_truncated=False,
            stderr_truncated=False,
        )
        self.assertIsNotNone(rejected_input_fault)
        assert rejected_input_fault is not None
        self.assertEqual(rejected_input_fault["origin"], "worker")
        self.assertEqual(rejected_input_fault["site"], "worker_entry")

        normal_worker_args = argparse.Namespace(**{
            **vars(synthetic_args),
            "_scratch_synthetic_restore_diagnostic": False,
        })
        normal_stderr = io.StringIO()
        with (
            mock.patch.object(backup_drill, "parse_args", return_value=normal_worker_args),
            mock.patch.object(backup_drill, "_scratch_worker_main") as normal_worker,
            mock.patch.object(backup_drill.sys, "stderr", normal_stderr),
        ):
            self.assertEqual(backup_drill.main(), 1)
        normal_worker.assert_not_called()
        self.assertIn("worker input is incomplete", normal_stderr.getvalue())

        unit_name = "oldsparky-backup-restore-" + ("a" * 24) + ".service"
        mountpoint = pathlib.Path("/run/oldsparky-backup-restore-" + ("a" * 24))
        journal_record = json.dumps(
            {
                "_SYSTEMD_UNIT": unit_name,
                "MESSAGE": f"Failed to set up mount namespacing: {mountpoint}: Permission denied",
            },
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        journal_diagnostic = backup_drill._scratch_parse_unit_journal(
            journal_record,
            unit_name=unit_name,
            mountpoint=mountpoint,
            complete=True,
            status="complete",
        )
        self.assertTrue(backup_drill.valid_scratch_unit_journal_diagnostic(journal_diagnostic))
        self.assertEqual(journal_diagnostic["reason"], "namespace_setup")
        self.assertEqual(journal_diagnostic["namespace_kind"], "mount")
        self.assertEqual(journal_diagnostic["errno"], "EACCES")
        self.assertEqual(journal_diagnostic["target"], "scratch_tmpfs")

        prefixed_unit_record = json.dumps(
            {
                "_SYSTEMD_UNIT": unit_name,
                "MESSAGE": (
                    f"{unit_name}: Failed at step NAMESPACE spawning "
                    f"{mountpoint / 'platform_backup_restore_drill.py'}: Operation not permitted"
                ),
            },
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        prefixed_unit = backup_drill._scratch_parse_unit_journal(
            prefixed_unit_record,
            unit_name=unit_name,
            mountpoint=mountpoint,
            complete=True,
            status="complete",
        )
        self.assertEqual(prefixed_unit["reason"], "namespace_setup")
        self.assertEqual(prefixed_unit["target"], "source_script")
        self.assertNotIn(str(mountpoint), json.dumps(prefixed_unit))

        other_unit_name = "oldsparky-backup-restore-" + ("b" * 24) + ".service"
        wrong_prefix_record = json.dumps(
            {
                "_SYSTEMD_UNIT": unit_name,
                "MESSAGE": (
                    f"{other_unit_name}: Failed at step NAMESPACE spawning "
                    f"{mountpoint / 'platform_backup_restore_drill.py'}: Operation not permitted"
                ),
            },
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        wrong_prefix = backup_drill._scratch_parse_unit_journal(
            wrong_prefix_record,
            unit_name=unit_name,
            mountpoint=mountpoint,
            complete=True,
            status="complete",
        )
        self.assertEqual(wrong_prefix["reason"], "unknown")

        terminal_namespace_exit = {
            "load_state": "loaded",
            "active_state": "failed",
            "sub_state": "failed",
            "result": "exit-code",
            "exec_main_status": 226,
            "exec_main_code": "exited",
            "start_token_sent": False,
            "completion_token_sent": False,
        }
        exit_summary_record = json.dumps(
            {
                "_SYSTEMD_UNIT": unit_name,
                "MESSAGE": (
                    f"{unit_name}: Main process exited, code=exited, "
                    "status=226/NAMESPACE"
                ),
            },
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        exit_summary = backup_drill._scratch_parse_unit_journal(
            exit_summary_record,
            unit_name=unit_name,
            mountpoint=mountpoint,
            complete=True,
            status="complete",
            terminal_snapshot=terminal_namespace_exit,
        )
        self.assertEqual(exit_summary["reason"], "namespace_setup")
        self.assertEqual(exit_summary["namespace_kind"], "unknown")
        self.assertEqual(exit_summary["errno"], "unknown")
        self.assertEqual(exit_summary["target"], "unknown")
        exit_summary_without_terminal = backup_drill._scratch_parse_unit_journal(
            exit_summary_record,
            unit_name=unit_name,
            mountpoint=mountpoint,
            complete=True,
            status="complete",
        )
        self.assertEqual(exit_summary_without_terminal["reason"], "unknown")
        unsynced = backup_drill._scratch_parse_unit_journal(
            journal_record,
            unit_name=unit_name,
            mountpoint=mountpoint,
            complete=True,
            status="complete",
            sync_status="timeout",
        )
        self.assertEqual(unsynced["status"], "complete")
        self.assertEqual(unsynced["sync_status"], "timeout")
        self.assertEqual(unsynced["reason"], "unknown")

        prefixed_target_record = json.dumps(
            {
                "_SYSTEMD_UNIT": unit_name,
                "MESSAGE": "Failed to set up mount namespacing: /rooted/private: Permission denied",
            },
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        prefixed_target = backup_drill._scratch_parse_unit_journal(
            prefixed_target_record,
            unit_name=unit_name,
            mountpoint=mountpoint,
            complete=True,
            status="complete",
        )
        self.assertEqual(prefixed_target["target"], "unknown")
        prefixed_failure_record = json.dumps(
            {
                "_SYSTEMD_UNIT": unit_name,
                "MESSAGE": (
                    "Wrapper observed Failed to set up mount namespacing: "
                    f"{mountpoint}: Permission denied"
                ),
            },
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        prefixed_failure = backup_drill._scratch_parse_unit_journal(
            prefixed_failure_record,
            unit_name=unit_name,
            mountpoint=mountpoint,
            complete=True,
            status="complete",
        )
        self.assertEqual(prefixed_failure["reason"], "unknown")
        ambiguous_kind_record = json.dumps(
            {
                "_SYSTEMD_UNIT": unit_name,
                "MESSAGE": (
                    "Failed to set up mount namespacing and failed to set up user namespacing: "
                    f"{mountpoint}: Permission denied"
                ),
            },
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        ambiguous_kind = backup_drill._scratch_parse_unit_journal(
            ambiguous_kind_record,
            unit_name=unit_name,
            mountpoint=mountpoint,
            complete=True,
            status="complete",
        )
        self.assertEqual(ambiguous_kind["reason"], "namespace_setup")
        self.assertEqual(ambiguous_kind["namespace_kind"], "unknown")

        private_record = json.dumps(
            {
                "_SYSTEMD_UNIT": unit_name,
                "MESSAGE": (
                    "Failed at step NAMESPACE spawning "
                    f"{mountpoint / 'platform_backup_restore_drill.py'}: Operation not permitted"
                ),
                "_HOSTNAME": "private-runner-name",
            },
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        private_diagnostic = backup_drill._scratch_parse_unit_journal(
            private_record,
            unit_name=unit_name,
            mountpoint=mountpoint,
            complete=True,
            status="complete",
        )
        self.assertEqual(private_diagnostic["reason"], "namespace_setup")
        self.assertEqual(private_diagnostic["namespace_kind"], "unknown")
        self.assertEqual(private_diagnostic["errno"], "EPERM")
        self.assertEqual(private_diagnostic["target"], "source_script")
        self.assertNotIn("private-runner-name", json.dumps(private_diagnostic))
        self.assertNotIn(str(mountpoint), json.dumps(private_diagnostic))

        unrelated_record = json.dumps(
            {"_SYSTEMD_UNIT": "unrelated.service", "MESSAGE": "Failed at step NAMESPACE"}
        ).encode("utf-8") + b"\n"
        unrelated = backup_drill._scratch_parse_unit_journal(
            unrelated_record,
            unit_name=unit_name,
            mountpoint=mountpoint,
            complete=True,
            status="complete",
        )
        self.assertEqual(unrelated["reason"], "unknown")
        ambiguous = backup_drill._scratch_parse_unit_journal(
            journal_record + private_record,
            unit_name=unit_name,
            mountpoint=mountpoint,
            complete=True,
            status="complete",
        )
        self.assertEqual(ambiguous["reason"], "unknown")
        bounded = backup_drill._scratch_parse_unit_journal(
            journal_record,
            unit_name=unit_name,
            mountpoint=mountpoint,
            complete=False,
            status="bounded",
        )
        self.assertEqual(bounded["status"], "bounded")
        self.assertEqual(bounded["reason"], "unknown")
        line = json.dumps(
            {"_SYSTEMD_UNIT": unit_name, "MESSAGE": "informational"},
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        below_line_cap = line * backup_drill.SCRATCH_SYNTHETIC_JOURNAL_MAX_LINES
        at_line_cap = line * backup_drill.SCRATCH_SYNTHETIC_JOURNAL_QUERY_LINES
        self.assertEqual(
            backup_drill._scratch_journal_capture_status(
                below_line_cap, eof=True, returncode=0
            ),
            ("complete", True),
        )
        self.assertEqual(
            backup_drill._scratch_journal_capture_status(
                at_line_cap, eof=True, returncode=0
            ),
            ("bounded", False),
        )
        with mock.patch.object(
            backup_drill.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(["journalctl", "--sync"], 0, b"", b""),
        ) as journal_sync:
            self.assertEqual(backup_drill._scratch_journal_sync_status(), "synced")
        self.assertEqual(journal_sync.call_args.args[0], ["journalctl", "--sync"])
        self.assertEqual(
            journal_sync.call_args.kwargs["timeout"],
            backup_drill.SCRATCH_SYNTHETIC_JOURNAL_TIMEOUT_SECONDS,
        )
        with mock.patch.object(
            backup_drill.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["journalctl", "--sync"], 2),
        ):
            self.assertEqual(backup_drill._scratch_journal_sync_status(), "timeout")
        malformed = backup_drill._scratch_parse_unit_journal(
            b"not-json\n",
            unit_name=unit_name,
            mountpoint=mountpoint,
            complete=True,
            status="complete",
        )
        self.assertEqual(malformed["status"], "malformed")
        self.assertEqual(malformed["reason"], "unknown")

        with mock.patch.object(
            backup_drill.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ) as reset_failed:
            backup_drill._scratch_reset_failed_unit(unit_name)
        self.assertEqual(
            reset_failed.call_args.args[0], ["systemctl", "reset-failed", unit_name]
        )
        with mock.patch.object(
            backup_drill,
            "_scratch_service_snapshot",
            return_value={"properties": {"LoadState": "not-found"}, "load_state": "not-found"},
        ) as snapshot_not_found:
            self.assertEqual(
                backup_drill._scratch_wait_unit_not_found(unit_name)["load_state"],
                "not-found",
            )
        snapshot_not_found.assert_called_once_with(unit_name)
        with (
            mock.patch.object(backup_drill, "_scratch_reset_failed_unit") as reset_missing,
            mock.patch.object(
                backup_drill,
                "_scratch_wait_unit_not_found",
                return_value={"load_state": "not-found", "properties": {"LoadState": "not-found"}},
            ) as wait_missing,
        ):
            unloaded = backup_drill._scratch_unload_synthetic_unit(
                unit_name, {"load_state": "not-found"}
            )
        reset_missing.assert_not_called()
        wait_missing.assert_called_once_with(unit_name)
        self.assertEqual(unloaded["load_state"], "not-found")

        cleanup_order: list[str] = []
        with (
            mock.patch.object(
                backup_drill,
                "_scratch_reset_failed_unit",
                side_effect=lambda _unit: cleanup_order.append("reset"),
            ) as reset_loaded,
            mock.patch.object(
                backup_drill,
                "_scratch_wait_unit_not_found",
                side_effect=lambda _unit: (
                    cleanup_order.append("wait"),
                    {"load_state": "not-found", "properties": {"LoadState": "not-found"}},
                )[1],
            ) as wait_loaded,
        ):
            loaded = backup_drill._scratch_unload_synthetic_unit(
                unit_name, {"load_state": "loaded"}
            )
        reset_loaded.assert_called_once_with(unit_name)
        wait_loaded.assert_called_once_with(unit_name)
        self.assertEqual(cleanup_order, ["reset", "wait"])
        self.assertEqual(loaded["load_state"], "not-found")
        with self.assertRaisesRegex(RuntimeError, "unit identity"):
            backup_drill._scratch_reset_failed_unit("platformdb.service")

        required_bytes = backup_drill.SCRATCH_MEMORY_ADMISSION_BYTES
        guard_sites = (
            "parent_before_authorization_probe",
            "parent_before_synthetic_restore_diagnostic",
            "parent_before_archive_open",
            "parent_before_unit_launch",
            "parent_active_poll",
            "parent_after_worker_cleanup",
            "worker_after_start_before_authorization_probe",
            "worker_after_start_before_synthetic_diagnostic",
            "worker_after_start_before_archive_open",
            "worker_after_initdb_before_postgres_start",
            "worker_after_postgres_start",
            "worker_pg_restore_resource_guard",
            "worker_after_restore_before_postgres_stop",
        )
        for guard_site in guard_sites:
            host_guard = {
                "schema": 2,
                "guard_site": guard_site,
                "guard_reason": "memory_reserve",
                "available_bytes": required_bytes - 1,
                "required_bytes": required_bytes,
            }
            self.assertTrue(backup_drill.valid_scratch_host_guard_diagnostic(host_guard))
            frame = backup_drill._scratch_host_guard_frame(host_guard)
            self.assertIsNotNone(frame)
            assert frame is not None
            self.assertEqual(
                backup_drill.parse_scratch_host_guard_frame(frame, truncated=False),
                host_guard,
            )
            with mock.patch.object(
                backup_drill,
                "_scratch_mem_available_bytes",
                return_value=required_bytes - 1,
            ):
                with self.assertRaises(RuntimeError) as reserve_failure:
                    backup_drill._raise_scratch_host_memory_reserve(
                        guard_site, message="fixed host reserve failure"
                    )
            self.assertEqual(
                reserve_failure.exception.scratch_host_guard_diagnostic,
                host_guard,
            )
        valid_host_guard = {
            "schema": 2,
            "guard_site": "worker_pg_restore_resource_guard",
            "guard_reason": "memory_reserve",
            "available_bytes": required_bytes - 1,
            "required_bytes": required_bytes,
        }
        for malformed in (
            {**valid_host_guard, "guard_site": "/private/host/path"},
            {**valid_host_guard, "guard_reason": "disk_floor"},
            {**valid_host_guard, "available_bytes": required_bytes},
            {**valid_host_guard, "required_bytes": required_bytes - 1},
            {**valid_host_guard, "unexpected": "private"},
            {**valid_host_guard, "schema": True},
        ):
            self.assertFalse(backup_drill.valid_scratch_host_guard_diagnostic(malformed))

        valid_frame = backup_drill._scratch_host_guard_frame(valid_host_guard)
        assert valid_frame is not None
        failure_report = json.dumps(
            {
                "ok": False,
                "error": "fixed failure",
                "scratch_host_guard_diagnostic": valid_host_guard,
                "scratch_cleanup_status": "confirmed",
            },
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8") + b"\n"
        self.assertEqual(
            backup_drill.parse_scratch_host_guard_failure_report(
                failure_report,
                truncated=False,
            ),
            valid_host_guard,
        )
        malformed_cleanup = json.dumps(
            {
                "ok": False,
                "error": "fixed failure",
                "scratch_host_guard_diagnostic": valid_host_guard,
                "scratch_cleanup_status": "partial",
            },
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8") + b"\n"
        self.assertIsNone(
            backup_drill.parse_scratch_host_guard_failure_report(
                malformed_cleanup,
                truncated=False,
            )
        )
        other_site_report = json.dumps(
            {
                "ok": False,
                "error": "fixed failure",
                "scratch_host_guard_diagnostic": {
                    **valid_host_guard,
                    "guard_site": "worker_after_postgres_start",
                },
            },
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8") + b"\n"
        report_diagnostic = backup_drill.parse_scratch_host_guard_failure_report(
            other_site_report, truncated=False
        )
        frame_diagnostic = backup_drill.parse_scratch_host_guard_frame(
            valid_frame, truncated=False
        )
        self.assertIsNotNone(report_diagnostic)
        self.assertIsNotNone(frame_diagnostic)
        self.assertNotEqual(report_diagnostic, frame_diagnostic)
        self.assertEqual(
            backup_drill._parse_bound_scratch_host_guard_diagnostic(
                failure_report,
                valid_frame,
                stdout_truncated=False,
                stderr_truncated=False,
            ),
            valid_host_guard,
        )
        self.assertIsNone(
            backup_drill._parse_bound_scratch_host_guard_diagnostic(
                other_site_report,
                valid_frame,
                stdout_truncated=False,
                stderr_truncated=False,
            )
        )
        self.assertIsNone(
            backup_drill._parse_bound_scratch_host_guard_diagnostic(
                b"",
                valid_frame,
                stdout_truncated=False,
                stderr_truncated=False,
            )
        )
        for forged_or_incomplete_stdout in (
            b"bounded child output\n" + failure_report,
            failure_report + b"untrusted trailing output",
            failure_report[:-1],
            failure_report + failure_report,
            b'{"ok":false,"ok":false,"error":"x"}\n',
            b'{"ok":false,"error":"x","scratch_host_guard_diagnostic":{}}\n',
        ):
            self.assertIsNone(
                backup_drill.parse_scratch_host_guard_failure_report(
                    forged_or_incomplete_stdout,
                    truncated=False,
                )
            )
        self.assertIsNone(
            backup_drill.parse_scratch_host_guard_failure_report(
                failure_report, truncated=True
            )
        )
        report_without_diagnostic = json.dumps(
            {"ok": False, "error": "ordinary worker failure"}
        ).encode("utf-8")
        self.assertIsNone(
            backup_drill.parse_scratch_host_guard_failure_report(
                report_without_diagnostic, truncated=False
            )
        )
        self.assertIsNone(
            backup_drill.parse_scratch_host_guard_frame(
                valid_frame + valid_frame, truncated=False
            )
        )
        self.assertIsNone(
            backup_drill.parse_scratch_host_guard_frame(
                valid_frame[:-1], truncated=False
            )
        )
        self.assertIsNone(
            backup_drill.parse_scratch_host_guard_frame(
                valid_frame, truncated=True
            )
        )
        duplicate_key_frame = (
            backup_drill.SCRATCH_HOST_GUARD_FRAME_MARKER
            + b'{"schema":2,"schema":2}\n'
        )
        self.assertIsNone(
            backup_drill.parse_scratch_host_guard_frame(
                duplicate_key_frame, truncated=False
            )
        )
        self.assertIsNone(
            backup_drill.parse_scratch_host_guard_failure_report(
                b"child-forged frame exists without helper error JSON\n",
                truncated=False,
            )
        )

        worker_private_stderr = io.BytesIO()
        worker_private_stdout = io.BytesIO()
        with mock.patch.object(
            backup_drill.sys,
            "stderr",
            types.SimpleNamespace(buffer=worker_private_stderr),
        ), mock.patch.object(
            backup_drill.sys,
            "stdout",
            types.SimpleNamespace(buffer=worker_private_stdout),
        ):
            backup_drill._write_scratch_worker_private_output(
                b"private child stdout", b"private child stderr"
            )
        self.assertEqual(worker_private_stdout.getvalue(), b"")
        self.assertEqual(
            worker_private_stderr.getvalue(),
            b"private child stdoutprivate child stderr",
        )

        public_error = RuntimeError("fixed failure")
        public_error.scratch_host_guard_diagnostic = valid_host_guard
        self.assertNotIn(
            "scratch_host_guard_diagnostic",
            backup_drill.failure_report_for_exception(
                public_error, private_scratch_diagnostics=False
            ),
        )
        self.assertEqual(
            backup_drill.failure_report_for_exception(
                public_error, private_scratch_diagnostics=True
            )["scratch_host_guard_diagnostic"],
            valid_host_guard,
        )
        self.assertIsNone(
            backup_drill.private_failure_frame_for_exception(
                public_error, enabled=False
            )
        )
        private_frame = backup_drill.private_failure_frame_for_exception(
            public_error, enabled=True
        )
        self.assertIsNotNone(private_frame)
        assert private_frame is not None
        self.assertIn(
            backup_drill.SCRATCH_HOST_GUARD_FRAME_MARKER.decode("ascii"),
            private_frame,
        )
        self.assertEqual(
            backup_drill.parse_scratch_host_guard_frame(
                private_frame.encode("ascii"), truncated=False
            ),
            valid_host_guard,
        )
        self.assertNotIn("/private/host/path", private_frame)

        def call_site_constants(function: Any, function_name: str) -> list[str]:
            tree = ast.parse(inspect.getsource(function))
            result: list[str] = []

            def constant_strings(node: ast.expr) -> list[str]:
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    return [node.value]
                if isinstance(node, ast.IfExp):
                    return constant_strings(node.body) + constant_strings(node.orelse)
                return []

            for node in sorted(
                ast.walk(tree), key=lambda candidate: getattr(candidate, "lineno", 0)
            ):
                if not isinstance(node, ast.Call):
                    continue
                called_name = (
                    node.func.id if isinstance(node.func, ast.Name) else None
                )
                if called_name != function_name:
                    continue
                if node.args:
                    result.extend(constant_strings(node.args[0]))
                result.extend(
                    value
                    for keyword in node.keywords
                    if keyword.arg == "guard_site"
                    for value in constant_strings(keyword.value)
                )
            return result

        self.assertEqual(
            call_site_constants(
                backup_drill._run_isolated_restore_drill, "_scratch_host_guard"
            ),
            [
                "parent_before_authorization_probe",
                "parent_before_synthetic_restore_diagnostic",
                "parent_before_archive_open",
                "parent_before_unit_launch",
                "parent_active_poll",
                # Probe, synthetic diagnostic, and full restore each have a
                # separate success return with the same final parent reserve check.
                "parent_after_worker_cleanup",
                "parent_after_worker_cleanup",
                "parent_after_worker_cleanup",
            ],
        )
        self.assertEqual(
            call_site_constants(
                backup_drill._scratch_worker_main,
                "_raise_scratch_host_memory_reserve",
            ),
            [
                "worker_after_start_before_authorization_probe",
                "worker_after_start_before_synthetic_diagnostic",
                "worker_after_start_before_archive_open",
            ],
        )
        self.assertEqual(
            call_site_constants(backup_drill._scratch_worker_main, "_scratch_resource_guard"),
            [
                "worker_after_initdb_before_postgres_start",
                "worker_after_postgres_start",
                "worker_pg_restore_resource_guard",
                "worker_after_restore_before_postgres_stop",
            ],
        )
        for owner in (
            backup_drill._scratch_worker_main,
            backup_drill._scratch_synthetic_restore_diagnostic_worker,
        ):
            tree = ast.parse(inspect.getsource(owner))
            backend_guard_calls = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_scratch_resource_guard"
            ]
            self.assertTrue(backend_guard_calls)
            self.assertTrue(
                all(
                    any(
                        keyword.arg == "scratch_backend"
                        and isinstance(keyword.value, ast.Name)
                        and keyword.value.id == "scratch_backend"
                        for keyword in node.keywords
                    )
                    for node in backend_guard_calls
                )
            )
            self.assertTrue(
                all(
                    any(keyword.arg == "expected_owner" for keyword in node.keywords)
                    for node in backend_guard_calls
                )
            )
        guard_root = pathlib.Path("/synthetic-guard-root")
        with (
            mock.patch.object(backup_drill, "require_restore_headroom"),
            mock.patch.object(backup_drill, "_raise_scratch_host_memory_reserve"),
            mock.patch.object(backup_drill, "_scratch_cgroup_snapshot"),
            mock.patch.object(backup_drill, "_scratch_mount_snapshot") as mount_snapshot,
            mock.patch.object(
                backup_drill.pwd,
                "getpwnam",
                return_value=types.SimpleNamespace(pw_uid=26, pw_gid=26),
            ) as get_postgres,
        ):
            backup_drill._scratch_resource_guard(guard_root)
            mount_snapshot.assert_called_once_with(
                guard_root, backend=backup_drill.SCRATCH_BACKEND_TMPFS_V1
            )
            get_postgres.assert_not_called()
            mount_snapshot.reset_mock()
            backup_drill._scratch_resource_guard(
                guard_root,
                scratch_backend=backup_drill.SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1,
                expected_owner=(26, 26),
            )
            mount_snapshot.assert_called_once_with(
                guard_root,
                backend=backup_drill.SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1,
                expected_owner=(26, 26),
            )
            get_postgres.assert_not_called()
            mount_snapshot.reset_mock()
            with self.assertRaisesRegex(RuntimeError, "exact postgres owner"):
                backup_drill._scratch_resource_guard(
                    guard_root,
                    scratch_backend=backup_drill.SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1,
                )
            mount_snapshot.assert_not_called()
        self.assertEqual(
            backup_drill._scratch_host_guard_frame({**valid_host_guard, "guard_site": "unknown"}),
            None,
        )

    def test_scratch_first_fault_projection_is_closed_and_preserves_original(self) -> None:
        first = PermissionError(1, "private path must not escape")
        backup_drill._retain_first_scratch_fault(
            first, origin="worker", site="worker_directory_setup"
        )
        expected = {
            "schema": 1,
            "origin": "worker",
            "site": "worker_directory_setup",
            "exception_kind": "permission",
            "errno": 1,
        }
        self.assertEqual(first.scratch_first_fault, expected)

        # A cleanup failure cannot replace the first diagnostic.
        backup_drill._retain_first_scratch_fault(
            first, origin="controller", site="controller_cleanup"
        )
        self.assertEqual(first.scratch_first_fault, expected)
        cleanup = RuntimeError("secondary private cleanup detail")
        self.assertIsNone(
            backup_drill._scratch_cleanup_failure(
                first,
                [cleanup],
                authorization_probe=True,
                service_stopped=True,
                mount_absent=True,
                mountpoint_removed=False,
                loop_released=None,
                image_removed=None,
                retention_receipt_written=None,
                protected_paths_cleaned=False,
            )
        )
        self.assertEqual(first.scratch_first_fault, expected)
        self.assertEqual(first.scratch_cleanup_status, "failed")
        self.assertEqual(
            first.scratch_cleanup_diagnostic,
            {
                "schema": 1,
                "service_stopped": True,
                "mount_absent": True,
                "mountpoint_removed": False,
                "loop_released": None,
                "image_created_by_invocation": None,
                "image_unlinked": None,
                "retention_receipt_written": None,
                "protected_paths_cleaned": False,
                "cleanup_status": "failed",
            },
        )
        self.assertNotIn(
            "secondary private cleanup detail",
            " ".join(getattr(first, "__notes__", [])),
        )
        confirmed = RuntimeError("primary error")
        backup_drill._scratch_cleanup_failure(
            confirmed,
            [],
            authorization_probe=False,
            service_stopped=True,
            mount_absent=True,
            mountpoint_removed=True,
            protected_paths_cleaned=True,
        )
        self.assertEqual(confirmed.scratch_cleanup_status, "confirmed")
        self.assertTrue(
            backup_drill.valid_scratch_cleanup_diagnostic(
                confirmed.scratch_cleanup_diagnostic
            )
        )
        cleanup_report = backup_drill.failure_report_for_exception(
            confirmed, private_scratch_diagnostics=True
        )
        self.assertEqual(cleanup_report["scratch_cleanup_status"], "confirmed")
        self.assertNotIn(
            "scratch_cleanup_status",
            backup_drill.failure_report_for_exception(
                confirmed, private_scratch_diagnostics=False
            ),
        )
        for invalid_status in (None, True, "partial", "Confirmed"):
            self.assertFalse(backup_drill.valid_scratch_cleanup_status(invalid_status))
        self.assertEqual(
            backup_drill._scratch_exception_projection(
                AttributeError("private detail")
            ),
            ("attribute", None),
        )
        self.assertEqual(
            backup_drill._scratch_exception_projection(
                IndexError("private detail")
            ),
            ("index", None),
        )
        self.assertEqual(
            backup_drill._scratch_exception_projection(
                KeyError("private detail")
            ),
            ("key", None),
        )
        for site in (
            "worker_after_initdb_guard",
            "worker_after_postgres_start_guard",
            "worker_role_setup",
            "worker_pg_restore_resource_guard",
            "worker_after_restore_guard",
        ):
            self.assertTrue(
                backup_drill.valid_scratch_first_fault(
                    {**expected, "site": site}
                )
            )
        self.assertFalse(
            backup_drill.valid_scratch_first_fault(
                {**expected, "origin": "controller"}
            )
        )
        controller_fault = {
            **expected,
            "origin": "controller",
            "site": "controller_cleanup",
        }
        self.assertTrue(backup_drill.valid_scratch_first_fault(controller_fault))
        self.assertFalse(
            backup_drill.valid_scratch_first_fault(
                {**controller_fault, "site": "worker_role_setup"}
            )
        )
        disk_mount_failure = {
            **expected,
            "site": "worker_mount_snapshot",
            "exception_kind": "runtime",
            "errno": None,
            "disk_mount_failure_reason": "filesystem_source_root_options",
        }
        self.assertTrue(backup_drill.valid_scratch_first_fault(disk_mount_failure))
        projected_mount_failure = RuntimeError(
            "Isolated ext4 scratch filesystem is below its bounded usable capacity."
        )
        projected_mount_failure.scratch_disk_mount_failure_reason = "statvfs_bound"
        projected_mount_failure.scratch_verification_site = "worker_mount_identity"
        projected_mount_failure.scratch_first_fault = backup_drill._scratch_first_fault(
            projected_mount_failure,
            origin="worker",
            site="worker_mount_snapshot",
        )
        self.assertEqual(
            backup_drill._scratch_first_fault(
                projected_mount_failure,
                origin="worker",
                site="worker_mount_snapshot",
            ),
            {
                "schema": 1,
                "origin": "worker",
                "site": "worker_mount_snapshot",
                "exception_kind": "runtime",
                "errno": None,
                "verification_site": "worker_mount_identity",
                "disk_mount_failure_reason": "statvfs_bound",
            },
        )
        bound_report = backup_drill.failure_report_for_exception(
            projected_mount_failure,
            private_scratch_diagnostics=True,
        )
        stdout = json.dumps(bound_report, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        stderr = backup_drill._scratch_first_fault_frame(
            projected_mount_failure.scratch_first_fault
        )
        self.assertIsNotNone(stderr)
        self.assertEqual(
            backup_drill._parse_bound_scratch_first_fault(
                stdout,
                stderr or b"",
                stdout_truncated=False,
                stderr_truncated=False,
            ),
            bound_report["scratch_first_fault"],
        )
        self.assertNotIn(b"/private/path", stdout + (stderr or b""))
        sysfs_fault = {
            "schema": 1,
            "origin": "worker",
            "site": "worker_mount_snapshot",
            "exception_kind": "runtime",
            "errno": None,
            "verification_site": "worker_mount_identity",
            "disk_mount_failure_reason": "sysfs_not_loop_device",
            "disk_mount_predicates": {
                "fs_ext4": True,
                "source_canonical": True,
                "device_major_minor_valid": True,
                "sysfs_mount_verified": True,
                "sysfs_device_resolved": True,
                "sysfs_loop_device": False,
                "sysfs_device_number_matches": True,
                "sysfs_backing_file_present": False,
                "root_expected": True,
                "target_expected": True,
                "option_rw": True,
                "option_nosuid": True,
                "option_nodev": True,
                "option_noexec": True,
            },
        }
        self.assertTrue(backup_drill.valid_scratch_first_fault(sysfs_fault))
        sysfs_error = RuntimeError("private mount detail")
        sysfs_error.scratch_first_fault = sysfs_fault
        sysfs_report = backup_drill.failure_report_for_exception(
            sysfs_error, private_scratch_diagnostics=True
        )
        sysfs_stdout = json.dumps(sysfs_report, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        sysfs_stderr = backup_drill._scratch_first_fault_frame(sysfs_fault)
        self.assertIsNotNone(sysfs_stderr)
        self.assertGreater(len(sysfs_stderr or b"") - len(backup_drill.SCRATCH_FIRST_FAULT_MARKER) - 1, 512)
        self.assertLessEqual(len(sysfs_stderr or b"") - len(backup_drill.SCRATCH_FIRST_FAULT_MARKER) - 1,
                             backup_drill.SCRATCH_FIRST_FAULT_FRAME_MAX_BYTES)
        self.assertEqual(
            backup_drill._parse_bound_scratch_first_fault(
                sysfs_stdout,
                sysfs_stderr or b"",
                stdout_truncated=False,
                stderr_truncated=False,
            ),
            sysfs_fault,
        )
        # The largest reachable full-mount predicate frame is the last sysfs
        # leaf before option checks. Earlier source/fs/root/target predicates
        # must be true or the producer would report an earlier failure.
        worst_reason = "sysfs_device_number_mismatch"
        worst_case_fault = {
            **sysfs_fault,
            "disk_mount_failure_reason": worst_reason,
            "disk_mount_predicates": {
                **{key: True for key in sysfs_fault["disk_mount_predicates"]},
                "sysfs_device_number_matches": False,
                "sysfs_backing_file_present": False,
            },
        }
        self.assertTrue(backup_drill.valid_scratch_first_fault(worst_case_fault))
        worst_case_frame = backup_drill._scratch_first_fault_frame(worst_case_fault)
        self.assertIsNotNone(worst_case_frame)
        worst_case_payload = (worst_case_frame or b"").removeprefix(
            backup_drill.SCRATCH_FIRST_FAULT_MARKER
        ).rstrip(b"\n")
        self.assertEqual(
            worst_case_payload,
            json.dumps(worst_case_fault, sort_keys=True, separators=(",", ":")).encode("ascii"),
        )
        self.assertGreater(len(worst_case_payload), 512)
        self.assertLessEqual(len(worst_case_payload), backup_drill.SCRATCH_FIRST_FAULT_FRAME_MAX_BYTES)
        too_large_frame = (
            backup_drill.SCRATCH_FIRST_FAULT_MARKER
            + b"{" + b" " * backup_drill.SCRATCH_FIRST_FAULT_FRAME_MAX_BYTES + b"}\n"
        )
        self.assertIsNone(
            backup_drill._parse_bound_scratch_first_fault(
                sysfs_stdout,
                too_large_frame,
                stdout_truncated=False,
                stderr_truncated=False,
            )
        )
        for malformed_mount_failure in (
            {**disk_mount_failure, "disk_mount_failure_reason": "private_path"},
            {**disk_mount_failure, "origin": "controller"},
            {**disk_mount_failure, "exception_kind": "os_error"},
        ):
            self.assertFalse(backup_drill.valid_scratch_first_fault(malformed_mount_failure))

        first_properties = {
            "LoadState": "loaded",
            "ActiveState": "active",
            "ExecMainPID": "1234",
            "InvocationID": "a" * 32,
        }
        single_mismatches = (
            ("load_state", {"LoadState": "not-found"}),
            ("active_state", {"ActiveState": "inactive"}),
            ("exec_main_pid", {"ExecMainPID": "1235"}),
            ("invocation_id", {"InvocationID": "b" * 32}),
        )
        for property_name, update in single_mismatches:
            with self.subTest(identity_mismatch=property_name):
                second_properties = {**first_properties, **update}
                comparison = backup_drill._scratch_identity_snapshot_comparison(
                    first_properties, second_properties
                )
                self.assertTrue(
                    backup_drill.valid_scratch_identity_snapshot_comparison(comparison)
                )
                assert comparison is not None
                self.assertEqual(
                    [name for name, is_equal in comparison["equal"].items() if not is_equal],
                    [property_name],
                )
                self.assertNotIn("1234", json.dumps(comparison))
                self.assertNotIn("a" * 32, json.dumps(comparison))

                production_failure = backup_drill._scratch_second_snapshot_identity_failure(
                    first_properties,
                    second_properties,
                    expected_pid="1234",
                    expected_invocation_id="a" * 32,
                    synthetic_restore_diagnostic=False,
                )
                self.assertIsNotNone(production_failure)
                self.assertFalse(
                    hasattr(
                        production_failure,
                        "scratch_synthetic_identity_snapshot_comparison",
                    )
                )
                synthetic_failure = backup_drill._scratch_second_snapshot_identity_failure(
                    first_properties,
                    second_properties,
                    expected_pid="1234",
                    expected_invocation_id="a" * 32,
                    synthetic_restore_diagnostic=True,
                )
                self.assertIsNotNone(synthetic_failure)
                synthetic_comparison = getattr(
                    synthetic_failure,
                    "scratch_synthetic_identity_snapshot_comparison",
                    None,
                )
                self.assertEqual(synthetic_comparison, comparison)

        self.assertIsNone(
            backup_drill._scratch_second_snapshot_identity_failure(
                first_properties,
                first_properties,
                expected_pid="1234",
                expected_invocation_id="a" * 32,
                synthetic_restore_diagnostic=True,
            )
        )

        combined_second = {
            **first_properties,
            "LoadState": "error",
            "ActiveState": "failed",
            "ExecMainPID": "0",
            "InvocationID": "unknown",
        }
        combined_comparison = backup_drill._scratch_identity_snapshot_comparison(
            first_properties, combined_second
        )
        self.assertTrue(
            backup_drill.valid_scratch_identity_snapshot_comparison(
                combined_comparison
            )
        )
        assert combined_comparison is not None
        self.assertEqual(
            combined_comparison["equal"],
            {
                "load_state": False,
                "active_state": False,
                "exec_main_pid": False,
                "invocation_id": False,
            },
        )
        self.assertFalse(
            backup_drill.valid_scratch_identity_snapshot_comparison(
                {
                    **combined_comparison,
                    "second": {**combined_comparison["second"], "active_state": "unknown-secret"},
                }
            )
        )
        self.assertFalse(
            backup_drill.valid_scratch_identity_snapshot_comparison(
                {
                    **combined_comparison,
                    "equal": {**combined_comparison["equal"], "exec_main_pid": 1},
                }
            )
        )
        identity_failure = RuntimeError("private unit mismatch")
        identity_failure.scratch_synthetic_identity_snapshot_comparison = combined_comparison
        identity_fault = backup_drill._scratch_first_fault(
            identity_failure, origin="controller", site="controller_identity"
        )
        self.assertTrue(backup_drill.valid_scratch_first_fault(identity_fault))
        assert identity_fault is not None
        self.assertEqual(
            identity_fault["synthetic_identity_snapshot_comparison"],
            combined_comparison,
        )
        self.assertFalse(
            backup_drill.valid_scratch_first_fault(
                {
                    **identity_fault,
                    "synthetic_path_fixture_rejection": {
                        "schema": 1,
                        "target_index": 0,
                        "parent_component_index": 1,
                        "predicates": ["group_or_world_writable"],
                    },
                }
            )
        )
        identity_frame = backup_drill._scratch_first_fault_frame(identity_fault)
        self.assertIsNotNone(identity_frame)
        assert identity_frame is not None
        identity_report = {
            "ok": False,
            "error": "fixed private failure marker",
            "scratch_first_fault": identity_fault,
        }
        identity_stdout = (
            json.dumps(identity_report, ensure_ascii=False, indent=2).encode("utf-8")
            + b"\n"
        )
        self.assertEqual(
            backup_drill._parse_bound_scratch_first_fault(
                identity_stdout,
                identity_frame,
                stdout_truncated=False,
                stderr_truncated=False,
            ),
            identity_fault,
        )

        frame = backup_drill._scratch_first_fault_frame(expected)
        self.assertIsNotNone(frame)
        assert frame is not None
        report = {
            "ok": False,
            "error": "fixed private failure marker",
            "scratch_first_fault": expected,
        }
        stdout = json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        self.assertEqual(
            backup_drill._parse_bound_scratch_first_fault(
                stdout,
                frame,
                stdout_truncated=False,
                stderr_truncated=False,
            ),
            expected,
        )

        # Exercise the real child CLI argv and main() error branch. The typed
        # synthetic worker emits only its canonical closed pair; the original
        # exception text and any private output must not cross either channel.
        mountpoint = pathlib.Path(
            "/run/oldsparky-backup-restore-" + "a" * 24
        )
        unit_command = backup_drill._scratch_unit_command(
            mountpoint=mountpoint,
            archive_path=None,
            archive_stat=None,
            archive_sha256=None,
            timestamp_slug="20261018T120000Z",
            unit_name="oldsparky-backup-restore-" + "a" * 24 + ".service",
            synthetic_restore_diagnostic=True,
            scratch_backend=backup_drill.SCRATCH_BACKEND_DISK_EXT4_IMAGE_V1,
            scratch_image_path=(
                backup_drill.SCRATCH_DISK_IMAGE_ROOT / ("a" * 24 + ".ext4")
            ),
        )
        worker_mode_index = unit_command.index(backup_drill.SCRATCH_WORKER_MODE)
        child_argv = unit_command[worker_mode_index:]
        self.assertNotIn("--private-scratch-diagnostics", child_argv)
        self.assertNotIn("--json", child_argv)
        child_failure = RuntimeError("raw synthetic worker error must not escape")

        def fail_synthetic_worker(args: argparse.Namespace) -> int:
            args._scratch_fault_site = "worker_mount_snapshot"
            raise child_failure

        child_stdout = io.StringIO()
        child_stderr = io.StringIO()
        with (
            mock.patch.object(sys, "argv", [str(SCRIPT_PATH), *child_argv]),
            mock.patch.object(backup_drill, "_scratch_worker_main", side_effect=fail_synthetic_worker),
            contextlib.redirect_stdout(child_stdout),
            contextlib.redirect_stderr(child_stderr),
        ):
            self.assertEqual(backup_drill.main(), 1)
        emitted_stdout = child_stdout.getvalue().encode("utf-8")
        emitted_stderr = child_stderr.getvalue().encode("ascii")
        parsed_child_report = json.loads(emitted_stdout.decode("utf-8"))
        self.assertEqual(
            parsed_child_report,
            {
                "ok": False,
                "error": "Synthetic restore worker failed.",
                "scratch_first_fault": child_failure.scratch_first_fault,
            },
        )
        self.assertEqual(
            backup_drill._parse_bound_scratch_first_fault(
                emitted_stdout,
                emitted_stderr,
                stdout_truncated=False,
                stderr_truncated=False,
            ),
            child_failure.scratch_first_fault,
        )
        self.assertNotIn(b"raw synthetic worker error must not escape", emitted_stdout)
        self.assertNotIn(b"raw synthetic worker error must not escape", emitted_stderr)
        self.assertEqual(
            parsed_child_report["scratch_first_fault"]["site"],
            "worker_mount_snapshot",
        )
        mismatch_fault = backup_drill._scratch_first_fault(
            RuntimeError("second worker fault"),
            origin="worker",
            site="worker_cgroup_snapshot",
        )
        assert mismatch_fault is not None
        mismatch_frame = backup_drill._scratch_first_fault_frame(mismatch_fault)
        assert mismatch_frame is not None
        self.assertIsNone(
            backup_drill._parse_bound_scratch_first_fault(
                emitted_stdout,
                mismatch_frame,
                stdout_truncated=False,
                stderr_truncated=False,
            )
        )
        self.assertEqual(
            backup_drill._scratch_synthetic_worker_failure_candidate(
                emitted_stdout, b"", stderr_eof=False
            ),
            ("pending", None),
        )
        self.assertEqual(
            backup_drill._scratch_synthetic_worker_failure_candidate(
                emitted_stdout, emitted_stderr[:12], stderr_eof=False
            ),
            ("pending", None),
        )
        self.assertEqual(
            backup_drill._scratch_synthetic_worker_failure_candidate(
                emitted_stdout, emitted_stderr, stderr_eof=True
            ),
            ("valid", child_failure.scratch_first_fault),
        )
        self.assertEqual(
            backup_drill._scratch_synthetic_worker_failure_candidate(
                emitted_stdout, mismatch_frame, stderr_eof=True
            ),
            ("invalid", None),
        )

        # The same worker flag without the typed synthetic mode keeps the
        # ordinary CLI error behavior and never emits this protocol frame.
        ordinary_args = [
            backup_drill.SCRATCH_WORKER_MODE,
            "--_scratch-root",
            str(mountpoint),
            "--_scratch-timestamp-slug",
            "20261018T120000Z",
            "--_scratch-archive",
            "/run/archive.dump",
            "--_scratch-archive-dev",
            "1",
            "--_scratch-archive-ino",
            "2",
            "--_scratch-archive-size",
            "8",
            "--_scratch-archive-sha256",
            "0" * 64,
        ]
        ordinary_stdout = io.StringIO()
        ordinary_stderr = io.StringIO()
        with (
            mock.patch.object(sys, "argv", [str(SCRIPT_PATH), *ordinary_args]),
            mock.patch.object(
                backup_drill,
                "_scratch_worker_main",
                side_effect=RuntimeError("ordinary worker failure"),
            ),
            contextlib.redirect_stdout(ordinary_stdout),
            contextlib.redirect_stderr(ordinary_stderr),
        ):
            self.assertEqual(backup_drill.main(), 1)
        self.assertEqual(ordinary_stdout.getvalue(), "")
        self.assertIn("[FAIL] ordinary worker failure", ordinary_stderr.getvalue())
        self.assertNotIn("SCRATCH_FIRST_FAULT_V1", ordinary_stderr.getvalue())

        # Invalid attached faults are not serialized, even in the synthetic
        # worker mode; the typed pair is emitted only after closed validation.
        invalid_stdout = io.StringIO()
        invalid_stderr = io.StringIO()

        def fail_with_invalid_fault(args: argparse.Namespace) -> int:
            error = RuntimeError("invalid fault should not be projected")
            error.scratch_first_fault = {"secret": "never serialize"}
            raise error

        with (
            mock.patch.object(sys, "argv", [str(SCRIPT_PATH), *child_argv]),
            mock.patch.object(
                backup_drill,
                "_scratch_worker_main",
                side_effect=fail_with_invalid_fault,
            ),
            contextlib.redirect_stdout(invalid_stdout),
            contextlib.redirect_stderr(invalid_stderr),
        ):
            self.assertEqual(backup_drill.main(), 1)
        self.assertNotIn("SCRATCH_FIRST_FAULT_V1", invalid_stderr.getvalue())
        self.assertNotIn("never serialize", invalid_stdout.getvalue())
        self.assertNotIn("never serialize", invalid_stderr.getvalue())

        probe_failure = RuntimeError("synthetic private failure text")
        probe_failure.scratch_authorization_probe = True
        probe_failure.scratch_unit_private_output = (b"raw stdout", b"raw stderr")
        probe_failure.scratch_first_fault = expected
        probe_frame = backup_drill.private_failure_frame_for_exception(
            probe_failure, enabled=True
        )
        self.assertIsNotNone(probe_frame)
        assert probe_frame is not None
        self.assertIn("SCRATCH_FIRST_FAULT_V1", probe_frame)
        self.assertNotIn("raw stdout", probe_frame)
        self.assertNotIn("raw stderr", probe_frame)
        self.assertNotIn("synthetic private failure text", probe_frame)
        for malformed in (
            {**expected, "private_path": "/not-allowed"},
            {**expected, "errno": True},
            {**expected, "schema": 1.0},
            {**expected, "errno": 13.0},
            {**expected, "exception_kind": "PermissionError"},
            {**expected, "exception_kind": 7},
            {**expected, "site": "private-path"},
        ):
            self.assertFalse(backup_drill.valid_scratch_first_fault(malformed))
        self.assertIsNone(
            backup_drill._parse_bound_scratch_first_fault(
                stdout,
                frame + frame,
                stdout_truncated=False,
                stderr_truncated=False,
            )
        )
        self._assert_scratch_resource_attribution_is_identity_bound_and_private()
        self._assert_scratch_resource_attribution_fails_closed_on_identity_and_parser_errors()

        class PhaseStderr:
            def __init__(self) -> None:
                self.buffer = self
                self._bytes = io.BytesIO()
                self.flush_count = 0

            def write(self, value: bytes) -> int:
                return self._bytes.write(value)

            def flush(self) -> None:
                self.flush_count += 1

            def getvalue(self) -> bytes:
                return self._bytes.getvalue()

        phase_stderr = PhaseStderr()
        phase_origin = 100_000_000
        phase_args = argparse.Namespace(
            private_scratch_diagnostics=True,
            _scratch_authorization_probe=False,
            _scratch_phase_origin_monotonic_ns=phase_origin,
        )
        phase_times = iter(phase_origin + index * 1_000_000 for index in range(12))
        with mock.patch.object(backup_drill.sys, "stderr", phase_stderr), mock.patch.object(
            backup_drill.time, "monotonic_ns", side_effect=lambda: next(phase_times)
        ):
            for phase in backup_drill.SCRATCH_PHASES:
                for event in ("start", "end"):
                    backup_drill._scratch_emit_phase_boundary(phase_args, phase, event)
        phase_bytes = phase_stderr.buffer.getvalue()
        self.assertEqual(phase_stderr.flush_count, 12)
        self.assertFalse(getattr(phase_args, "_scratch_phase_emit_failed", False))
        complete_phase = backup_drill._scratch_parse_phase_attribution(
            phase_bytes,
            truncated=False,
            complete=True,
            launch_monotonic_ns=900_000_000,
        )
        self.assertTrue(backup_drill.valid_scratch_phase_attribution(complete_phase))
        self.assertEqual(complete_phase["status"], "complete")
        self.assertEqual(complete_phase["frames"][0]["worker_offset_ms"], 0)
        self.assertEqual(complete_phase["frames"][-1]["worker_offset_ms"], 11)
        prefix_bytes = b"".join(phase_bytes.splitlines(keepends=True)[:4])
        self.assertTrue(
            backup_drill._scratch_success_stderr_accepted(
                phase_bytes,
                observed_bytes=len(phase_bytes),
                stderr_eof=True,
                private_diagnostics=True,
                authorization_probe=False,
                launch_monotonic_ns=900_000_000,
            )
        )
        self.assertTrue(
            backup_drill._scratch_success_stderr_accepted(
                b"",
                observed_bytes=0,
                stderr_eof=True,
                private_diagnostics=True,
                authorization_probe=False,
                launch_monotonic_ns=900_000_000,
            )
        )
        self.assertTrue(
            backup_drill._scratch_success_stderr_accepted(
                b"",
                observed_bytes=0,
                stderr_eof=True,
                private_diagnostics=True,
                authorization_probe=True,
                launch_monotonic_ns=900_000_000,
            )
        )
        self.assertTrue(
            backup_drill._scratch_success_stderr_accepted(
                b"",
                observed_bytes=0,
                stderr_eof=True,
                private_diagnostics=False,
                authorization_probe=False,
                launch_monotonic_ns=900_000_000,
            )
        )
        for kwargs in (
            {
                "observed_bytes": len(prefix_bytes),
                "stderr_eof": False,
                "private_diagnostics": True,
                "authorization_probe": False,
            },
            {
                "observed_bytes": len(phase_bytes),
                "stderr_eof": True,
                "private_diagnostics": False,
                "authorization_probe": False,
            },
        ):
            self.assertFalse(
                backup_drill._scratch_success_stderr_accepted(
                    prefix_bytes if kwargs["observed_bytes"] == len(prefix_bytes) else phase_bytes,
                    launch_monotonic_ns=900_000_000,
                    **kwargs,
                )
            )
        prefix_phase = backup_drill._scratch_parse_phase_attribution(
            prefix_bytes,
            truncated=False,
            complete=False,
            launch_monotonic_ns=900_000_000,
            controller_guard_monotonic_ns=901_000_000,
            controller_drain_monotonic_ns=902_000_000,
        )
        self.assertEqual(prefix_phase["status"], "unknown")
        self.assertEqual(prefix_phase["unknown_reason"], "capture_prefix")
        self.assertEqual(len(prefix_phase["frames"]), 4)
        fault_frame = backup_drill._scratch_first_fault_frame(expected)
        self.assertIsNotNone(fault_frame)
        mixed = backup_drill._scratch_parse_phase_attribution(
            prefix_bytes + fault_frame,
            truncated=False,
            complete=False,
            launch_monotonic_ns=900_000_000,
        )
        self.assertEqual(mixed["unknown_reason"], "non_phase_bytes")
        self.assertEqual(len(mixed["frames"]), 4)
        duplicate_key_line = (
            backup_drill.SCRATCH_PHASE_FRAME_MARKER
            + b'{"event":"start","event":"end","ordinal":1,"phase":"archive_verify","worker_offset_ms":0}\n'
        )
        self.assertEqual(
            backup_drill._scratch_parse_phase_attribution(
                duplicate_key_line,
                truncated=False,
                complete=False,
                launch_monotonic_ns=900_000_000,
            )["unknown_reason"],
            "malformed",
        )
        self.assertEqual(
            backup_drill._scratch_parse_phase_attribution(
                phase_bytes,
                truncated=True,
                complete=False,
                launch_monotonic_ns=900_000_000,
            )["unknown_reason"],
            "truncated",
        )
        no_phase_args = argparse.Namespace(
            private_scratch_diagnostics=False,
            _scratch_authorization_probe=False,
            _scratch_phase_origin_monotonic_ns=phase_origin,
        )
        with mock.patch.object(backup_drill.sys, "stderr", phase_stderr):
            backup_drill._scratch_emit_phase_boundary(no_phase_args, "archive_verify", "start")
        self.assertEqual(phase_stderr.flush_count, 12)

        read_fd, write_fd = os.pipe()
        try:
            os.set_blocking(read_fd, False)
            os.write(write_fd, prefix_bytes)
            fake_process = types.SimpleNamespace(
                stderr=types.SimpleNamespace(fileno=lambda: read_fd)
            )
            drained_buffer = bytearray()
            observed, reached_eof = backup_drill._scratch_drain_phase_prefix_now(
                fake_process, drained_buffer, 0
            )
            self.assertEqual(bytes(drained_buffer), prefix_bytes)
            self.assertEqual(observed, len(prefix_bytes))
            self.assertFalse(reached_eof)
        finally:
            os.close(read_fd)
            os.close(write_fd)

        primary_guard = RuntimeError("primary active-poll reserve refusal")
        primary_guard.scratch_host_guard_diagnostic = {
            "schema": 2,
            "guard_site": "parent_active_poll",
            "guard_reason": "memory_reserve",
            "available_bytes": backup_drill.SCRATCH_MEMORY_ADMISSION_BYTES - 1,
            "required_bytes": backup_drill.SCRATCH_MEMORY_ADMISSION_BYTES,
        }
        primary_guard.scratch_first_fault = {
            "schema": 1,
            "origin": "controller",
            "site": "controller_active_poll",
            "exception_kind": "runtime",
            "errno": None,
        }
        primary_guard.scratch_phase_attribution = prefix_phase
        projected = backup_drill.failure_report_for_exception(
            primary_guard, private_scratch_diagnostics=True
        )
        self.assertEqual(projected["error"], str(primary_guard))
        self.assertEqual(projected["scratch_host_guard_diagnostic"], primary_guard.scratch_host_guard_diagnostic)
        self.assertEqual(projected["scratch_first_fault"], primary_guard.scratch_first_fault)
        self.assertEqual(projected["scratch_phase_attribution"], prefix_phase)
        self.assertNotIn(
            "scratch_phase_attribution",
            backup_drill.failure_report_for_exception(
                primary_guard, private_scratch_diagnostics=False
            ),
        )
        phase_events: list[tuple[str, str]] = []
        primary_operation_error = RuntimeError("operation remains primary")
        with self.assertRaises(RuntimeError) as observed_error:
            backup_drill._scratch_run_observed_phase(
                lambda phase, event: phase_events.append((phase, event)),
                "restore_platform",
                lambda: (_ for _ in ()).throw(primary_operation_error),
            )
        self.assertIs(observed_error.exception, primary_operation_error)
        self.assertEqual(phase_events, [("restore_platform", "start")])

    def test_authorization_probe_worker_has_no_archive_or_database_effects(self) -> None:
        root = "/run/oldsparky-backup-restore-" + ("a" * 24)
        args = argparse.Namespace(
            _scratch_root=root,
            _scratch_timestamp_slug="20261013T010203Z",
            _scratch_authorization_probe=True,
            _scratch_archive=None,
            _scratch_archive_dev=None,
            _scratch_archive_ino=None,
            _scratch_archive_size=None,
            _scratch_archive_sha256=None,
            private_scratch_diagnostics=True,
        )
        stdout = io.BytesIO()
        stdin = types.SimpleNamespace(buffer=StagedWorkerInput())
        real_is_dir = pathlib.Path.is_dir

        def probe_is_dir(path: pathlib.Path) -> bool:
            if str(path) == root:
                return True
            return real_is_dir(path)

        with (
            mock.patch.object(backup_drill.os, "geteuid", return_value=0),
            mock.patch.object(backup_drill.os, "environ", {}),
            mock.patch.object(pathlib.Path, "is_dir", probe_is_dir),
            mock.patch.object(backup_drill, "_scratch_mount_snapshot") as mount_snapshot,
            mock.patch.object(backup_drill, "_scratch_cgroup_snapshot") as cgroup_snapshot,
            mock.patch.object(
                backup_drill,
                "_raise_scratch_host_memory_reserve",
            ) as memory_guard,
            mock.patch.object(backup_drill, "_open_verified_scratch_archive") as archive_open,
            mock.patch.object(backup_drill, "require_commands") as require_commands,
            mock.patch.object(backup_drill, "run_command") as run_command,
            mock.patch.object(backup_drill, "perform_restore_drill") as restore,
            mock.patch.object(backup_drill, "_scratch_create_application_role") as role,
            mock.patch.object(backup_drill.sys, "stdin", stdin),
            mock.patch.object(backup_drill.sys, "stdout", types.SimpleNamespace(buffer=stdout)),
        ):
            self.assertEqual(backup_drill._scratch_worker_main(args), 0)

        self.assertEqual(
            json.loads(stdout.getvalue().decode("ascii")),
            {"kind": "authorization_probe", "schema": 1, "status": "completed"},
        )
        mount_snapshot.assert_called_once_with(pathlib.Path(root))
        cgroup_snapshot.assert_called_once_with()
        memory_guard.assert_called_once_with(
            "worker_after_start_before_authorization_probe",
            message="Scratch restore admission memory reserve is unavailable.",
        )
        require_commands.assert_not_called()
        archive_open.assert_not_called()
        run_command.assert_not_called()
        restore.assert_not_called()
        role.assert_not_called()

    def test_authorization_probe_command_retains_confinement_without_archive(self) -> None:
        mountpoint = pathlib.Path(
            "/run/oldsparky-backup-restore-" + ("b" * 24)
        )
        command = backup_drill._scratch_unit_command(
            mountpoint=mountpoint,
            archive_path=None,
            archive_stat=None,
            archive_sha256=None,
            timestamp_slug="20261013T010203Z",
            unit_name="oldsparky-backup-restore-" + ("b" * 24) + ".service",
            private_scratch_diagnostics=True,
            authorization_probe=True,
        )
        self.assertIn("--_scratch-authorization-probe", command)
        self.assertNotIn("--_scratch-archive", command)
        self.assertFalse(any("archive.dump" in item for item in command))
        for required in (
            "--property=MemoryMax=" + str(backup_drill.SCRATCH_MEMORY_MAX_BYTES),
            "--property=MemorySwapMax=0",
            "--property=RuntimeMaxSec=900s",
            "--property=TimeoutStopSec=5s",
            "--property=KillMode=control-group",
            "--property=OOMPolicy=kill",
            "--property=PrivateNetwork=yes",
            "--property=ProtectSystem=strict",
            "--property=NoNewPrivileges=yes",
            "--property=CapabilityBoundingSet=CAP_CHOWN CAP_SETUID CAP_SETGID CAP_DAC_OVERRIDE",
        ):
            self.assertIn(required, command)

        synthetic_command = backup_drill._scratch_unit_command(
            mountpoint=mountpoint,
            archive_path=None,
            archive_stat=None,
            archive_sha256=None,
            timestamp_slug="20261013T010203Z",
            unit_name="oldsparky-backup-restore-" + ("c" * 24) + ".service",
            synthetic_restore_diagnostic=True,
        )
        self.assertNotIn("--collect", synthetic_command)
        self.assertIn("--property=CollectMode=inactive", synthetic_command)
        self.assertEqual(
            [
                item.split("=", 2)[2]
                for item in synthetic_command
                if item.startswith("--property=InaccessiblePaths=")
            ],
            list(backup_drill._SCRATCH_INACCESSIBLE_PATHS),
        )
        self.assertIn("--property=MemoryMax=" + str(backup_drill.SCRATCH_MEMORY_MAX_BYTES), synthetic_command)
        self.assertIn("--property=MemorySwapMax=0", synthetic_command)
        self.assertNotIn("--_scratch-archive", synthetic_command)
        self.assertFalse(any("archive.dump" in item for item in synthetic_command))

    def _assert_scratch_unit_diagnostic_caps_and_private_envelope(self) -> None:
        required_memory = backup_drill.SCRATCH_MEMORY_ADMISSION_BYTES
        available_memory = required_memory - 4096
        admission_diagnostic = {
            "schema": 1,
            "phase": "pre_create_admission",
            "guard_reason": "memory_reserve",
            "available_bytes": available_memory,
            "required_bytes": required_memory,
            "helper_rss_bytes": 8 * 1024 * 1024,
            "direct_parent_rss_bytes": 12 * 1024 * 1024,
            "observed_pair_rss_bytes": 20 * 1024 * 1024,
            "observed_process_count": 2,
        }
        self.assertTrue(backup_drill.valid_scratch_admission_diagnostic(admission_diagnostic))
        for key, value in (
            ("schema", True),
            ("phase", "unit_exited_before_confinement"),
            ("guard_reason", "other"),
            ("available_bytes", True),
            ("available_bytes", required_memory),
            ("required_bytes", required_memory - 1),
        ):
            malformed_admission = dict(admission_diagnostic)
            malformed_admission[key] = value
            self.assertFalse(backup_drill.valid_scratch_admission_diagnostic(malformed_admission))
        malformed_admission = {**admission_diagnostic, "unexpected": 1}
        self.assertFalse(backup_drill.valid_scratch_admission_diagnostic(malformed_admission))
        for key, value in (
            ("helper_rss_bytes", True),
            ("direct_parent_rss_bytes", -1),
            ("observed_pair_rss_bytes", 1),
            ("observed_process_count", True),
            ("observed_process_count", 1),
        ):
            malformed_admission = dict(admission_diagnostic)
            malformed_admission[key] = value
            self.assertFalse(backup_drill.valid_scratch_admission_diagnostic(malformed_admission))
        one_sample = {
            **admission_diagnostic,
            "direct_parent_rss_bytes": None,
            "observed_pair_rss_bytes": None,
            "observed_process_count": 1,
        }
        self.assertTrue(backup_drill.valid_scratch_admission_diagnostic(one_sample))
        no_samples = {
            **admission_diagnostic,
            "helper_rss_bytes": None,
            "direct_parent_rss_bytes": None,
            "observed_pair_rss_bytes": None,
            "observed_process_count": 0,
        }
        self.assertTrue(backup_drill.valid_scratch_admission_diagnostic(no_samples))

        with mock.patch.object(
            backup_drill,
            "_scratch_proc_status_sample",
            side_effect=[(10, 20, 1024), (20, 1, 2048)],
        ), mock.patch.object(backup_drill.os, "getpid", return_value=10), mock.patch.object(
            backup_drill.os, "getppid", return_value=20
        ):
            rss_sample = backup_drill._scratch_admission_rss_snapshot()
        self.assertEqual(
            rss_sample,
            {
                "helper_rss_bytes": 1024,
                "direct_parent_rss_bytes": 2048,
                "observed_pair_rss_bytes": 3072,
                "observed_process_count": 2,
            },
        )
        with mock.patch.object(
            backup_drill,
            "_scratch_proc_status_sample",
            side_effect=OSError("best-effort telemetry failure"),
        ), mock.patch.object(backup_drill.os, "getpid", return_value=10), mock.patch.object(
            backup_drill.os, "getppid", return_value=20
        ):
            unavailable_rss = backup_drill._scratch_admission_rss_snapshot()
        self.assertEqual(
            unavailable_rss,
            {
                "helper_rss_bytes": None,
                "direct_parent_rss_bytes": None,
                "observed_pair_rss_bytes": None,
                "observed_process_count": 0,
            },
        )

        with (
            mock.patch.object(backup_drill, "require_restore_headroom") as headroom,
            mock.patch.object(backup_drill, "_scratch_mem_available_bytes", return_value=available_memory),
            mock.patch.object(backup_drill, "_scratch_admission_rss_snapshot", return_value={
                "helper_rss_bytes": 8 * 1024 * 1024,
                "direct_parent_rss_bytes": 12 * 1024 * 1024,
                "observed_pair_rss_bytes": 20 * 1024 * 1024,
                "observed_process_count": 2,
            }),
        ):
            with self.assertRaisesRegex(
                RuntimeError, r"Scratch restore admission memory reserve is unavailable\."
            ) as admission_failure:
                backup_drill._scratch_host_guard()
        headroom.assert_called_once_with(pathlib.Path("/"))
        self.assertEqual(
            admission_failure.exception.scratch_admission_diagnostic,
            admission_diagnostic,
        )
        with (
            mock.patch.object(backup_drill, "require_restore_headroom"),
            mock.patch.object(backup_drill, "_scratch_mem_available_bytes", return_value=available_memory),
            mock.patch.object(
                backup_drill,
                "_scratch_admission_rss_snapshot",
                side_effect=RuntimeError("telemetry must not replace admission"),
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError, r"Scratch restore admission memory reserve is unavailable\."
            ) as telemetry_failure:
                backup_drill._scratch_host_guard()
        self.assertEqual(
            telemetry_failure.exception.scratch_admission_diagnostic,
            {
                "schema": 1,
                "phase": "pre_create_admission",
                "guard_reason": "memory_reserve",
                "available_bytes": available_memory,
                "required_bytes": required_memory,
                "helper_rss_bytes": None,
                "direct_parent_rss_bytes": None,
                "observed_pair_rss_bytes": None,
                "observed_process_count": 0,
            },
        )

        # The guard is unchanged at the exact threshold and rejects before it
        # tries to open or stage the supplied archive.
        with (
            mock.patch.object(backup_drill.os, "geteuid", return_value=0),
            mock.patch.object(backup_drill, "require_commands") as require_commands,
            mock.patch.object(backup_drill, "require_restore_headroom"),
            mock.patch.object(backup_drill, "_scratch_mem_available_bytes", return_value=available_memory),
            mock.patch.object(backup_drill, "_scratch_admission_rss_snapshot", return_value={
                "helper_rss_bytes": None,
                "direct_parent_rss_bytes": None,
                "observed_pair_rss_bytes": None,
                "observed_process_count": 0,
            }),
        ):
            with self.assertRaisesRegex(
                RuntimeError, r"Scratch restore admission memory reserve is unavailable\."
            ):
                backup_drill._run_isolated_restore_drill(
                    pathlib.Path("/missing-archive-must-not-be-opened.dump"),
                    timestamp_slug="20261012T000000Z",
                    private_scratch_diagnostics=True,
                )
        require_commands.assert_called_once_with("systemd-run", "systemctl", "pg_config", "runuser")

        with (
            mock.patch.object(backup_drill, "require_restore_headroom"),
            mock.patch.object(
                backup_drill,
                "_scratch_mem_available_bytes",
                return_value=required_memory,
            ),
        ):
            backup_drill._scratch_host_guard()

        stdout = b"o" * (backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES + 1)
        stderr = b"e" * (backup_drill.SCRATCH_PRIVATE_STDERR_BYTES + 1)
        diagnostic = backup_drill._scratch_unit_diagnostic(
            {
                "properties": {
                    "ActiveState": "failed",
                    "SubState": "failed",
                    "Result": "exit-code",
                    "ExecMainStatus": "203",
                }
            },
            launcher_exit_status=1,
            stdout_prefix=stdout,
            stdout_observed=len(stdout),
            stdout_complete=True,
            stderr_prefix=stderr,
            stderr_observed=len(stderr),
            stderr_complete=True,
        )
        self.assertIsNotNone(diagnostic)
        assert diagnostic is not None
        self.assertTrue(backup_drill.valid_scratch_unit_diagnostic(diagnostic))
        self.assertEqual(diagnostic["stdout_bytes"], backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES)
        self.assertEqual(diagnostic["stderr_bytes"], backup_drill.SCRATCH_PRIVATE_STDERR_BYTES)
        self.assertTrue(diagnostic["stdout_truncated"])
        self.assertTrue(diagnostic["stderr_truncated"])
        self.assertEqual(
            diagnostic["stdout_sha256"],
            hashlib.sha256(stdout[: backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES]).hexdigest(),
        )
        envelope = backup_drill._scratch_private_output_envelope(
            stdout[: backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES],
            stderr[: backup_drill.SCRATCH_PRIVATE_STDERR_BYTES],
        )
        self.assertLessEqual(len(envelope), 48 * 1024)
        self.assertLessEqual(len(envelope), 65536)
        framed_payload_bytes = 2 + backup_drill.SCRATCH_PRIVATE_RAW_BYTES
        self.assertEqual(
            len(envelope),
            len(backup_drill.SCRATCH_PRIVATE_OUTPUT_MARKER)
            + 4 * ((framed_payload_bytes + 2) // 3)
            + 1,
        )
        with self.assertRaisesRegex(ValueError, "closed envelope bound"):
            backup_drill._scratch_private_output_envelope(
                b"x" * (backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES + 1), b""
            )
        self.assertTrue(envelope.startswith(backup_drill.SCRATCH_PRIVATE_OUTPUT_MARKER))
        encoded_line = envelope[len(backup_drill.SCRATCH_PRIVATE_OUTPUT_MARKER) :].rstrip(b"\n")
        decoded = backup_drill.base64.b64decode(encoded_line, validate=True)
        stdout_size = int.from_bytes(decoded[:2], "big")
        self.assertEqual(stdout_size, backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES)
        self.assertEqual(decoded[2 : 2 + stdout_size], stdout[:stdout_size])
        self.assertEqual(decoded[2 + stdout_size :], stderr[: backup_drill.SCRATCH_PRIVATE_STDERR_BYTES])
        overflow = backup_drill._scratch_unit_diagnostic(
            {
                "properties": {
                    "ActiveState": "failed",
                    "SubState": "failed",
                    "Result": "exit-code",
                    "ExecMainStatus": "203",
                }
            },
            launcher_exit_status=1,
            stdout_prefix=b"x" * backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES,
            stdout_observed=backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES,
            stdout_complete=False,
            stderr_prefix=b"y" * 7,
            stderr_observed=7,
            stderr_complete=True,
        )
        self.assertIsNotNone(overflow)
        assert overflow is not None
        self.assertTrue(overflow["stdout_truncated"])
        self.assertEqual(overflow["stdout_bytes"], backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES)

        malformed = dict(diagnostic)
        malformed["stdout_bytes"] = backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES + 1
        self.assertFalse(backup_drill.valid_scratch_unit_diagnostic(malformed))
        malformed = dict(diagnostic)
        malformed["active_state"] = []
        self.assertFalse(backup_drill.valid_scratch_unit_diagnostic(malformed))
        malformed = dict(diagnostic)
        malformed["stdout_truncated"] = 1
        self.assertFalse(backup_drill.valid_scratch_unit_diagnostic(malformed))
        verification_failure = dict(diagnostic)
        verification_failure.update(
            phase="confinement_verification_failed",
            active_state="active",
            verification_error="os_error",
            verification_errno=2,
        )
        self.assertTrue(backup_drill.valid_scratch_unit_diagnostic(verification_failure))
        terminal = {
            "load_state": "loaded",
            "active_state": "inactive",
            "sub_state": "dead",
            "result": "success",
            "exec_main_status": 0,
            "exec_main_code": "exited",
            "start_token_sent": False,
            "completion_token_sent": False,
        }
        schema2 = backup_drill._scratch_unit_diagnostic(
            {
                "properties": {
                    "ActiveState": "active",
                    "SubState": "running",
                    "Result": "success",
                    "ExecMainStatus": "0",
                }
            },
            launcher_exit_status=1,
            stdout_prefix=b"",
            stdout_observed=0,
            stdout_complete=True,
            stderr_prefix=b"",
            stderr_observed=0,
            stderr_complete=True,
            phase="confinement_verification_failed",
            verification_error="os_error",
            verification_errno=2,
            terminal_snapshot=terminal,
        )
        self.assertIsNotNone(schema2)
        assert schema2 is not None
        self.assertEqual(schema2["schema"], 2)
        self.assertEqual(schema2["active_state"], "active")
        self.assertEqual(schema2["terminal_snapshot"], terminal)
        verification_site = backup_drill._scratch_unit_diagnostic(
            {
                "properties": {
                    "ActiveState": "active",
                    "SubState": "running",
                    "Result": "success",
                    "ExecMainStatus": "0",
                }
            },
            launcher_exit_status=1,
            stdout_prefix=b"bounded",
            stdout_observed=7,
            stdout_complete=True,
            stderr_prefix=b"",
            stderr_observed=0,
            stderr_complete=True,
            phase="authorization_failed",
            verification_error="os_error",
            verification_errno=2,
            verification_site="proc_exe_stat",
            terminal_snapshot=terminal,
        )
        self.assertIsNotNone(verification_site)
        assert verification_site is not None
        self.assertEqual(verification_site["schema"], 3)
        self.assertEqual(verification_site["verification_site"], "proc_exe_stat")
        self.assertEqual(verification_site["terminal_snapshot"], terminal)
        self.assertTrue(backup_drill.valid_scratch_unit_diagnostic(verification_site))
        for site in (
            "controller_cgroup_identity_compare",
            "controller_process_start_time_compare",
            "controller_completion_unit_state",
        ):
            with self.subTest(verification_site=site):
                site_diagnostic = backup_drill._scratch_unit_diagnostic(
                    {"properties": {"ActiveState": "active", "SubState": "running", "Result": "success"}},
                    launcher_exit_status=1,
                    stdout_prefix=b"",
                    stdout_observed=0,
                    stdout_complete=True,
                    stderr_prefix=b"",
                    stderr_observed=0,
                    stderr_complete=True,
                    phase="authorization_failed",
                    verification_error="other",
                    verification_site=site,
                )
                self.assertIsNotNone(site_diagnostic)
                assert site_diagnostic is not None
                self.assertEqual(site_diagnostic["verification_site"], site)
                self.assertTrue(backup_drill.valid_scratch_unit_diagnostic(site_diagnostic))
        malformed = dict(verification_site)
        malformed["verification_site"] = "/proc/private"
        self.assertFalse(backup_drill.valid_scratch_unit_diagnostic(malformed))
        malformed = dict(schema2)
        malformed["terminal_snapshot"] = {**terminal, "private_path": "/secret"}
        self.assertFalse(backup_drill.valid_scratch_unit_diagnostic(malformed))
        malformed = dict(verification_failure)
        malformed["verification_errno"] = True
        self.assertFalse(backup_drill.valid_scratch_unit_diagnostic(malformed))
        malformed = dict(verification_failure)
        malformed["verification_error"] = "other"
        self.assertFalse(backup_drill.valid_scratch_unit_diagnostic(malformed))
        malformed = dict(verification_failure)
        malformed["active_state"] = "failed"
        self.assertFalse(backup_drill.valid_scratch_unit_diagnostic(malformed))
        after_confinement = dict(diagnostic)
        after_confinement.update(
            phase="unit_exited_after_confinement",
            active_state="inactive",
            sub_state="dead",
            result="exit-code",
            verification_error="none",
            verification_errno=None,
        )
        self.assertTrue(backup_drill.valid_scratch_unit_diagnostic(after_confinement))
        malformed = dict(after_confinement)
        malformed["active_state"] = "active"
        self.assertFalse(backup_drill.valid_scratch_unit_diagnostic(malformed))

        private_failure = RuntimeError("fixed backup failure")
        private_failure.scratch_unit_diagnostic = diagnostic
        private_failure.scratch_admission_diagnostic = admission_diagnostic
        private_failure.scratch_unit_private_output = (
            stdout[: backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES],
            stderr[: backup_drill.SCRATCH_PRIVATE_STDERR_BYTES],
        )
        cli_args = argparse.Namespace(
            as_json=True,
            private_scratch_diagnostics=True,
            check_latest=False,
            verify_latest_existing=False,
            dump_only=False,
            verify_dump=None,
        )
        private_stdout = io.StringIO()
        private_stderr = io.StringIO()
        with (
            mock.patch.object(backup_drill, "parse_args", return_value=cli_args),
            mock.patch.object(backup_drill, "create_backup", side_effect=private_failure),
            mock.patch.object(backup_drill.sys, "stdout", private_stdout),
            mock.patch.object(backup_drill.sys, "stderr", private_stderr),
        ):
            self.assertEqual(backup_drill.main(), 1)
        private_report = json.loads(private_stdout.getvalue())
        self.assertEqual(private_report["scratch_unit_diagnostic"], diagnostic)
        self.assertEqual(private_report["scratch_admission_diagnostic"], admission_diagnostic)
        self.assertNotIn(stdout[:32].decode("ascii"), private_stdout.getvalue())
        self.assertEqual(
            private_stderr.getvalue().encode("ascii"),
            backup_drill._scratch_private_output_envelope(
                stdout[: backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES],
                stderr[: backup_drill.SCRATCH_PRIVATE_STDERR_BYTES],
            ),
        )

        # A collected transient unit can have no typed systemd properties.
        # Its bounded output still travels only through the private frame.
        private_failure.scratch_unit_diagnostic = None
        private_failure.scratch_admission_diagnostic = None
        missing_state_stdout = io.StringIO()
        missing_state_stderr = io.StringIO()
        with (
            mock.patch.object(backup_drill, "parse_args", return_value=cli_args),
            mock.patch.object(backup_drill, "create_backup", side_effect=private_failure),
            mock.patch.object(backup_drill.sys, "stdout", missing_state_stdout),
            mock.patch.object(backup_drill.sys, "stderr", missing_state_stderr),
        ):
            self.assertEqual(backup_drill.main(), 1)
        missing_state_report = json.loads(missing_state_stdout.getvalue())
        self.assertNotIn("scratch_unit_diagnostic", missing_state_report)
        self.assertNotIn("scratch_admission_diagnostic", missing_state_report)
        self.assertNotIn(stdout[:32].decode("ascii"), missing_state_stdout.getvalue())
        self.assertEqual(
            missing_state_stderr.getvalue().encode("ascii"),
            backup_drill._scratch_private_output_envelope(
                stdout[: backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES],
                stderr[: backup_drill.SCRATCH_PRIVATE_STDERR_BYTES],
            ),
        )

        public_args = argparse.Namespace(**{**vars(cli_args), "private_scratch_diagnostics": False})
        public_stdout = io.StringIO()
        public_stderr = io.StringIO()
        with (
            mock.patch.object(backup_drill, "parse_args", return_value=public_args),
            mock.patch.object(backup_drill, "create_backup", side_effect=private_failure),
            mock.patch.object(backup_drill.sys, "stdout", public_stdout),
            mock.patch.object(backup_drill.sys, "stderr", public_stderr),
        ):
            self.assertEqual(backup_drill.main(), 1)
        self.assertNotIn("scratch_unit_diagnostic", public_stdout.getvalue())
        self.assertNotIn("scratch_admission_diagnostic", public_stdout.getvalue())
        self.assertEqual(public_stderr.getvalue(), "")

    def _assert_scratch_early_exit_output_is_drained(self) -> None:
        class LauncherProcess:
            def __init__(self, stdout, stderr) -> None:
                self.stdout = stdout
                self.stderr = stderr
                self.returncode = None

            def poll(self):
                return self.returncode

            def wait(self, *, timeout=None):
                return self.returncode

        with tempfile.TemporaryDirectory() as temporary_dir:
            archive_path = pathlib.Path(temporary_dir) / "platformdb.dump"
            archive_path.write_bytes(b"PGDMPsynthetic archive")
            archive_path.chmod(0o600)
            stdout_read, stdout_write = os.pipe()
            stderr_read, stderr_write = os.pipe()
            pipe_capacities = []
            for read_fd in (stdout_read, stderr_read):
                pipe_capacities.append(fcntl.fcntl(read_fd, fcntl.F_SETPIPE_SZ, 4096))
            self.assertTrue(
                all(capacity < backup_drill.SCRATCH_PRIVATE_STDERR_BYTES for capacity in pipe_capacities)
            )
            stdout_reader = os.fdopen(stdout_read, "rb", buffering=0)
            stderr_reader = os.fdopen(stderr_read, "rb", buffering=0)
            stdout_payload = b"o" * (backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES + 1)
            stderr_payload = b"reason:" + b"e" * backup_drill.SCRATCH_PRIVATE_STDERR_BYTES

            def write_payload(fd, payload):
                try:
                    view = memoryview(payload)
                    while view:
                        view = view[os.write(fd, view) :]
                finally:
                    os.close(fd)

            stdout_writer = threading.Thread(target=write_payload, args=(stdout_write, stdout_payload))
            stderr_writer = threading.Thread(target=write_payload, args=(stderr_write, stderr_payload))
            stdout_writer.start()
            stderr_writer.start()
            launcher = LauncherProcess(stdout_reader, stderr_reader)
            snapshots = [
                {
                    "properties": {
                        "LoadState": "loaded",
                        "ActiveState": "failed",
                        "SubState": "failed",
                        "Result": "exit-code",
                        "ExecMainPID": "0",
                        "ExecMainStatus": "203",
                        "ControlGroup": "/system.slice/scratch-test.service",
                    }
                },
                {
                    "properties": {
                        "LoadState": "not-found",
                        "ActiveState": "inactive",
                        "SubState": "dead",
                        "Result": "success",
                        "ExecMainStatus": "0",
                        "ExecMainPID": "0",
                        "ControlGroup": "",
                    }
                },
                {
                    "properties": {
                        "LoadState": "not-found",
                        "ActiveState": "inactive",
                        "SubState": "dead",
                        "Result": "success",
                        "ExecMainStatus": "0",
                        "ExecMainPID": "0",
                        "ControlGroup": "",
                    }
                },
            ]

            def snapshot(_unit_name):
                value = snapshots.pop(0)
                return value

            def stop_unit(_unit_name):
                launcher.returncode = 1

            unit_command = mock.Mock(return_value=["systemd-run"])
            try:
                with (
                    mock.patch.object(backup_drill, "require_commands"),
                    mock.patch.object(backup_drill, "_scratch_host_guard"),
                    mock.patch.object(backup_drill, "_scratch_unit_command", unit_command),
                    mock.patch.object(backup_drill, "_scratch_mountpoint_is_mounted", return_value=False),
                    mock.patch.object(backup_drill, "_scratch_service_snapshot", side_effect=snapshot),
                    mock.patch.object(backup_drill, "_scratch_stop_unit", side_effect=stop_unit),
                    mock.patch.object(backup_drill.subprocess, "Popen", return_value=launcher),
                ):
                    with self.assertRaisesRegex(
                        RuntimeError, "failed before its confinement was verified"
                    ) as failure:
                        backup_drill._run_isolated_restore_drill(
                            archive_path,
                            timestamp_slug="20261012T010203Z",
                        )
                stdout_writer.join(timeout=2)
                stderr_writer.join(timeout=2)

                self.assertFalse(stdout_writer.is_alive())
                self.assertFalse(stderr_writer.is_alive())
                diagnostic = failure.exception.scratch_unit_diagnostic
                self.assertTrue(backup_drill.valid_scratch_unit_diagnostic(diagnostic))
                self.assertEqual(diagnostic["exec_main_status"], 203)
                self.assertEqual(diagnostic["launcher_exit_status"], 1)
                self.assertEqual(diagnostic["stdout_bytes"], backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES)
                self.assertTrue(diagnostic["stdout_truncated"])
                self.assertEqual(diagnostic["stderr_bytes"], backup_drill.SCRATCH_PRIVATE_STDERR_BYTES)
                self.assertTrue(diagnostic["stderr_truncated"])
                raw_stdout, raw_stderr = failure.exception.scratch_unit_private_output
                self.assertEqual(raw_stdout, stdout_payload[: backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES])
                self.assertEqual(raw_stderr, stderr_payload[: backup_drill.SCRATCH_PRIVATE_STDERR_BYTES])
                self.assertEqual(diagnostic["terminal_snapshot"]["load_state"], "not-found")
                mountpoint = pathlib.Path(unit_command.call_args.kwargs["mountpoint"])
                self.assertFalse(mountpoint.exists())
            finally:
                stdout_reader.close()
                stderr_reader.close()
                stdout_writer.join(timeout=2)
                stderr_writer.join(timeout=2)

        # A secondary failure while collecting diagnostics must not replace
        # the original controller failure or its first-fault projection.
        with tempfile.TemporaryDirectory() as temporary_dir:
            archive_path = pathlib.Path(temporary_dir) / "platformdb.dump"
            archive_path.write_bytes(b"PGDMPsynthetic archive")
            archive_path.chmod(0o600)
            failed = {
                "properties": {
                    "LoadState": "loaded",
                    "ActiveState": "failed",
                    "SubState": "failed",
                    "Result": "exit-code",
                    "ExecMainPID": "0",
                    "ExecMainStatus": "203",
                    "ControlGroup": "/system.slice/scratch-diagnostic-failure.service",
                }
            }
            absent = {
                "properties": {
                    "LoadState": "not-found",
                    "ActiveState": "inactive",
                    "SubState": "dead",
                    "Result": "success",
                    "ExecMainStatus": "0",
                    "ExecMainPID": "0",
                    "ControlGroup": "",
                }
            }
            snapshots = [failed, absent, absent, absent]
            launched: list[subprocess.Popen[bytes]] = []
            real_popen = subprocess.Popen

            def launch(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                launched.append(process)
                return process

            def stop_unit(_unit_name: str) -> None:
                if launched and launched[0].poll() is None:
                    launched[0].terminate()
                    launched[0].wait(timeout=2)

            def snapshot(_unit_name: str) -> dict[str, Any]:
                return snapshots.pop(0)

            command = [sys.executable, "-c", "import time; time.sleep(10)"]
            unit_command = mock.Mock(return_value=command)
            with (
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "_scratch_host_guard"),
                mock.patch.object(backup_drill, "_scratch_unit_command", unit_command),
                mock.patch.object(backup_drill, "_scratch_mountpoint_is_mounted", return_value=False),
                mock.patch.object(backup_drill, "_scratch_service_snapshot", side_effect=snapshot),
                mock.patch.object(backup_drill, "_scratch_stop_unit", side_effect=stop_unit),
                mock.patch.object(
                    backup_drill,
                    "_scratch_drain_early_exit_output",
                    side_effect=ValueError("diagnostic-only failure"),
                ),
                mock.patch.object(backup_drill.subprocess, "Popen", side_effect=launch),
                mock.patch.object(backup_drill.secrets, "token_hex", return_value="d" * 24),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "failed before its confinement was verified",
                ) as failure:
                    backup_drill._run_isolated_restore_drill(
                        archive_path,
                        timestamp_slug="20261013T010204Z",
                        private_scratch_diagnostics=True,
                    )
            self.assertEqual(len(launched), 1)
            self.assertIsNotNone(launched[0].returncode)
            self.assertEqual(
                failure.exception.scratch_first_fault,
                {
                    "schema": 1,
                    "origin": "controller",
                    "site": "controller_unit_snapshot",
                    "exception_kind": "runtime",
                    "errno": None,
                },
            )
            self.assertFalse(pathlib.Path(unit_command.call_args.kwargs["mountpoint"]).exists())

    def _assert_scratch_live_identity_failure_preserves_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            archive_path = pathlib.Path(temporary_dir) / "platformdb.dump"
            archive_path.write_bytes(b"PGDMPsynthetic archive")
            archive_path.chmod(0o600)
            expected_stdout = b"worker reached restore preflight\n"
            expected_stderr = b"worker failure detail\n"
            child_code = (
                "import sys,time; "
                f"sys.stdout.buffer.write({expected_stdout!r}); sys.stdout.flush(); "
                f"sys.stderr.buffer.write({expected_stderr!r}); sys.stderr.flush(); "
                "time.sleep(10)"
            )
            command = [sys.executable, "-c", child_code]
            unit_command = mock.Mock(return_value=command)
            real_popen = subprocess.Popen
            launched: list[subprocess.Popen[bytes]] = []
            snapshots = [
                {
                    "properties": {
                        "LoadState": "loaded",
                        "ActiveState": "active",
                        "SubState": "running",
                        "Result": "success",
                        "ExecMainPID": "424242",
                        "InvocationID": "0123456789abcdef0123456789abcdef",
                        "ExecMainStatus": "0",
                        "ControlGroup": "/system.slice/scratch-identity-race.service",
                    }
                },
                {
                    "properties": {
                        "LoadState": "not-found",
                        "ActiveState": "inactive",
                        "SubState": "dead",
                        "Result": "success",
                        "ExecMainPID": "0",
                        "ControlGroup": "",
                    }
                },
                {
                    "properties": {
                        "LoadState": "not-found",
                        "ActiveState": "inactive",
                        "SubState": "dead",
                        "Result": "success",
                        "ExecMainPID": "0",
                        "ControlGroup": "",
                    }
                },
            ]

            def launch(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                launched.append(process)
                return process

            def snapshot(_unit_name):
                return snapshots.pop(0)

            def stop_unit(_unit_name):
                if launched and launched[0].poll() is None:
                    launched[0].terminate()
                    launched[0].wait(timeout=2)

            vanished = FileNotFoundError(
                2, "synthetic worker executable disappeared", "/proc/424242/exe"
            )
            with (
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "_scratch_host_guard"),
                mock.patch.object(backup_drill, "_scratch_unit_command", unit_command),
                mock.patch.object(backup_drill, "_scratch_mountpoint_is_mounted", return_value=False),
                mock.patch.object(backup_drill, "_scratch_service_snapshot", side_effect=snapshot),
                mock.patch.object(backup_drill, "_scratch_expected_properties", side_effect=vanished),
                mock.patch.object(backup_drill, "_scratch_stop_unit", side_effect=stop_unit) as stop,
                mock.patch.object(backup_drill.subprocess, "Popen", side_effect=launch),
                mock.patch.object(backup_drill.secrets, "token_hex", return_value=uuid.uuid4().hex[:24]),
            ):
                with self.assertRaises(FileNotFoundError) as failure:
                    backup_drill._run_isolated_restore_drill(
                        archive_path,
                        timestamp_slug="20261012T010206Z",
                    )

            self.assertIs(failure.exception, vanished)
            self.assertEqual(failure.exception.errno, 2)
            stop.assert_called_once()
            self.assertEqual(len(launched), 1)
            self.assertIsNotNone(launched[0].returncode)
            diagnostic = failure.exception.scratch_unit_diagnostic
            self.assertTrue(backup_drill.valid_scratch_unit_diagnostic(diagnostic))
            self.assertEqual(diagnostic["phase"], "confinement_verification_failed")
            self.assertEqual(diagnostic["active_state"], "active")
            self.assertEqual(diagnostic["verification_error"], "os_error")
            self.assertEqual(diagnostic["verification_errno"], 2)
            captured_stdout, captured_stderr = failure.exception.scratch_unit_private_output
            self.assertEqual(captured_stdout, expected_stdout)
            self.assertEqual(captured_stderr, expected_stderr)
            mountpoint = pathlib.Path(unit_command.call_args.kwargs["mountpoint"])
            self.assertFalse(mountpoint.exists())

    def _assert_scratch_identity_replacement_cannot_receive_authorization(self) -> None:
        for replacement in ("pid", "invocation"):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as temporary_dir:
                archive_path = pathlib.Path(temporary_dir) / "platformdb.dump"
                archive_path.write_bytes(b"PGDMPsynthetic archive")
                archive_path.chmod(0o600)
                command = [
                    sys.executable,
                    "-c",
                    "import sys; "
                    f"token={backup_drill.SCRATCH_START_TOKEN!r}; "
                    "ack=sys.stdin.buffer.read(len(token)); "
                    "assert ack == token; print('authorized',flush=True)",
                ]
                unit_command = mock.Mock(return_value=command)
                first_properties = {
                    "LoadState": "loaded",
                    "ActiveState": "active",
                    "SubState": "running",
                    "Result": "success",
                    "ExecMainPID": "424242",
                    "InvocationID": "0123456789abcdef0123456789abcdef",
                    "ExecMainStatus": "0",
                    "ControlGroup": "/system.slice/scratch-identity-barrier.service",
                }
                second_properties = dict(first_properties)
                if replacement == "pid":
                    second_properties["ExecMainPID"] = "424243"
                else:
                    second_properties["InvocationID"] = "1123456789abcdef0123456789abcdef"
                terminal = {
                    "properties": {
                        "LoadState": "not-found",
                        "ActiveState": "inactive",
                        "SubState": "dead",
                        "Result": "success",
                        "ExecMainPID": "0",
                        "ExecMainStatus": "0",
                        "ControlGroup": "",
                    }
                }
                snapshots = [
                    {"properties": first_properties},
                    {"properties": second_properties},
                    terminal,
                    terminal,
                ]
                real_popen = subprocess.Popen
                launched = []

                def launch(*args, **kwargs):
                    process = real_popen(*args, **kwargs)
                    launched.append(process)
                    return process

                with (
                    mock.patch.object(backup_drill, "require_commands"),
                    mock.patch.object(backup_drill, "_scratch_host_guard"),
                    mock.patch.object(backup_drill, "_scratch_unit_command", unit_command),
                    mock.patch.object(backup_drill, "_scratch_mountpoint_is_mounted", return_value=False),
                    mock.patch.object(
                        backup_drill,
                        "_scratch_service_snapshot",
                        side_effect=lambda _name: snapshots.pop(0) if snapshots else terminal,
                    ),
                    mock.patch.object(
                        backup_drill,
                        "_scratch_expected_properties",
                        return_value="/system.slice/scratch-identity-barrier.service",
                    ) as expected_properties,
                    mock.patch.object(backup_drill, "_scratch_control_group_snapshot", return_value={}),
                    mock.patch.object(backup_drill, "_scratch_send_worker_token") as authorize,
                    mock.patch.object(backup_drill, "_scratch_stop_unit"),
                    mock.patch.object(backup_drill.subprocess, "Popen", side_effect=launch),
                    mock.patch.object(backup_drill.secrets, "token_hex", return_value=uuid.uuid4().hex[:24]),
                ):
                    with self.assertRaisesRegex(RuntimeError, "identity changed before authorization") as failure:
                        backup_drill._run_isolated_restore_drill(
                            archive_path,
                            timestamp_slug="20261012T010209Z",
                            private_scratch_diagnostics=True,
                        )

                authorize.assert_not_called()
                self.assertEqual(expected_properties.call_count, 1)
                captured_stdout, _captured_stderr = failure.exception.scratch_unit_private_output
                self.assertEqual(captured_stdout, b"")
                diagnostic = failure.exception.scratch_unit_diagnostic
                self.assertTrue(backup_drill.valid_scratch_unit_diagnostic(diagnostic))
                self.assertEqual(diagnostic["phase"], "confinement_verification_failed")
                self.assertEqual(diagnostic["active_state"], "active")
                self.assertFalse(diagnostic["terminal_snapshot"]["start_token_sent"])
                self.assertFalse(diagnostic["terminal_snapshot"]["completion_token_sent"])
                self.assertNotIn("ExecMainPID", json.dumps(diagnostic))

    def _assert_scratch_after_confinement_failure_preserves_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            archive_path = pathlib.Path(temporary_dir) / "platformdb.dump"
            archive_path.write_bytes(b"PGDMPsynthetic archive")
            archive_path.chmod(0o600)
            stdout_payload = b"restore stdout:" + b"o" * 3000
            stderr_payload = b"pg_restore detail:" + b"e" * 40000
            child_code = (
                "import sys,time; "
                f"start={backup_drill.SCRATCH_START_TOKEN!r}; "
                "assert sys.stdin.buffer.read(len(start)) == start; "
                f"sys.stdout.buffer.write({stdout_payload!r}); sys.stdout.flush(); "
                f"sys.stderr.buffer.write({stderr_payload!r}); sys.stderr.flush(); "
                "time.sleep(0.35); sys.exit(17)"
            )
            command = [sys.executable, "-c", child_code]
            unit_command = mock.Mock(return_value=command)
            active = {
                "properties": {
                    "LoadState": "loaded",
                    "ActiveState": "active",
                    "SubState": "running",
                    "Result": "success",
                    "ExecMainPID": "424242",
                    "InvocationID": "0123456789abcdef0123456789abcdef",
                    "ExecMainStatus": "0",
                    "ControlGroup": "/system.slice/scratch-after-confinement.service",
                }
            }
            final = {
                "properties": {
                    "LoadState": "not-found",
                    "ActiveState": "failed",
                    "SubState": "dead",
                    "Result": "exit-code",
                    "ExecMainPID": "0",
                    "ExecMainStatus": "17",
                    "ControlGroup": "",
                }
            }
            snapshots = [active, active]
            launched: list[subprocess.Popen[bytes]] = []

            def service_snapshot(_unit_name: str) -> dict[str, object]:
                if snapshots:
                    return snapshots.pop(0)
                if launched and launched[0].poll() is None:
                    return active
                return final

            real_popen = subprocess.Popen

            def launch(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                launched.append(process)
                return process

            with (
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "_scratch_host_guard"),
                mock.patch.object(backup_drill, "_scratch_unit_command", unit_command),
                mock.patch.object(backup_drill, "_scratch_mountpoint_is_mounted", return_value=False),
                mock.patch.object(backup_drill, "_scratch_service_snapshot", side_effect=service_snapshot),
                mock.patch.object(
                    backup_drill,
                    "_scratch_expected_properties",
                    return_value="/system.slice/scratch-after-confinement.service",
                ),
                mock.patch.object(backup_drill, "_scratch_control_group_snapshot", return_value={}),
                mock.patch.object(backup_drill, "_scratch_stop_unit"),
                mock.patch.object(backup_drill.subprocess, "Popen", side_effect=launch),
                mock.patch.object(backup_drill.secrets, "token_hex", return_value=uuid.uuid4().hex[:24]),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "completion was not fully authorized and verified"
                ) as failure:
                    backup_drill._run_isolated_restore_drill(
                        archive_path,
                        timestamp_slug="20261012T010207Z",
                        private_scratch_diagnostics=True,
                    )

            diagnostic = failure.exception.scratch_unit_diagnostic
            self.assertTrue(backup_drill.valid_scratch_unit_diagnostic(diagnostic))
            self.assertEqual(diagnostic["phase"], "authorization_failed")
            self.assertEqual(diagnostic["active_state"], "active")
            self.assertEqual(diagnostic["terminal_snapshot"]["load_state"], "not-found")
            self.assertTrue(diagnostic["terminal_snapshot"]["start_token_sent"])
            self.assertFalse(diagnostic["terminal_snapshot"]["completion_token_sent"])
            self.assertEqual(diagnostic["launcher_exit_status"], 17)
            self.assertEqual(diagnostic["verification_error"], "other")
            self.assertIsNone(diagnostic["verification_errno"])
            self.assertEqual(
                failure.exception.scratch_unit_private_output,
                (
                    stdout_payload[: backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES],
                    stderr_payload[: backup_drill.SCRATCH_PRIVATE_STDERR_BYTES],
                ),
            )
            self.assertTrue(diagnostic["stdout_truncated"])
            self.assertTrue(diagnostic["stderr_truncated"])
            self.assertTrue(unit_command.call_args.kwargs["private_scratch_diagnostics"])
            self.assertFalse(
                pathlib.Path(unit_command.call_args.kwargs["mountpoint"]).exists(),
                str(unit_command.call_args.kwargs["mountpoint"]),
            )

    def _assert_collected_unit_without_properties_preserves_output(self) -> None:
        class LauncherProcess:
            def __init__(self, stdin, stdout, stderr) -> None:
                self.stdin = stdin
                self.stdout = stdout
                self.stderr = stderr
                self.returncode = None
                self.wait_calls = 0

            def poll(self):
                return self.returncode

            def wait(self, *, timeout=None):
                self.wait_calls += 1
                return self.returncode

        with tempfile.TemporaryDirectory() as temporary_dir:
            archive_path = pathlib.Path(temporary_dir) / "platformdb.dump"
            archive_path.write_bytes(b"PGDMPsynthetic archive")
            archive_path.chmod(0o600)
            stdout_payload = b"restore completed with failure"
            stderr_payload = b"nested pg_restore failure detail"
            stdout_read, stdout_write = os.pipe()
            stderr_read, stderr_write = os.pipe()
            for write_fd, payload in (
                (stdout_write, stdout_payload),
                (stderr_write, stderr_payload),
            ):
                os.write(write_fd, payload)
                os.close(write_fd)
            stdin_read, stdin_write = os.pipe()
            stdin_reader = os.fdopen(stdin_read, "rb", buffering=0)
            stdin_writer = os.fdopen(stdin_write, "wb", buffering=0)
            stdout_reader = os.fdopen(stdout_read, "rb", buffering=0)
            stderr_reader = os.fdopen(stderr_read, "rb", buffering=0)
            launcher = LauncherProcess(stdin_writer, stdout_reader, stderr_reader)
            command = ["systemd-run"]
            unit_command = mock.Mock(return_value=command)
            active = {
                "properties": {
                    "LoadState": "loaded",
                    "ActiveState": "active",
                    "SubState": "running",
                    "Result": "success",
                    "ExecMainPID": "424242",
                    "InvocationID": "0123456789abcdef0123456789abcdef",
                    "ExecMainStatus": "0",
                    "ControlGroup": "/system.slice/scratch-collected.service",
                }
            }
            collected = {"properties": {"LoadState": "not-found"}}
            snapshots = [active, active]
            snapshot_count = 0

            def snapshot(_unit_name: str) -> dict[str, object]:
                nonlocal snapshot_count
                snapshot_count += 1
                if snapshots:
                    return snapshots.pop(0)
                if snapshot_count == 3:
                    # START has been sent after two matching identity checks;
                    # now model the launcher exiting before unit properties
                    # can be collected by the completion monitor.
                    launcher.returncode = 17
                    return active
                return collected

            with (
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "_scratch_host_guard"),
                mock.patch.object(backup_drill, "_scratch_unit_command", unit_command),
                mock.patch.object(backup_drill, "_scratch_mountpoint_is_mounted", return_value=False),
                mock.patch.object(backup_drill, "_scratch_service_snapshot", side_effect=snapshot),
                mock.patch.object(
                    backup_drill,
                    "_scratch_expected_properties",
                    return_value="/system.slice/scratch-collected.service",
                ),
                mock.patch.object(backup_drill, "_scratch_control_group_snapshot", return_value={}),
                mock.patch.object(backup_drill, "_scratch_stop_unit") as stop_unit,
                mock.patch.object(backup_drill.secrets, "token_hex", return_value=uuid.uuid4().hex[:24]),
                mock.patch.object(backup_drill.subprocess, "Popen", return_value=launcher),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "completion was not fully authorized and verified"
                ) as failure:
                    backup_drill._run_isolated_restore_drill(
                        archive_path,
                        timestamp_slug="20261012T010208Z",
                        private_scratch_diagnostics=True,
                    )
                diagnostic = failure.exception.scratch_unit_diagnostic
                self.assertTrue(backup_drill.valid_scratch_unit_diagnostic(diagnostic))
                self.assertEqual(diagnostic["phase"], "authorization_failed")
                self.assertEqual(diagnostic["terminal_snapshot"]["load_state"], "not-found")
                self.assertTrue(diagnostic["terminal_snapshot"]["start_token_sent"])
                self.assertFalse(diagnostic["terminal_snapshot"]["completion_token_sent"])
                self.assertEqual(
                    failure.exception.scratch_unit_private_output,
                    (stdout_payload, stderr_payload),
                )
                self.assertEqual(stdin_reader.read(len(backup_drill.SCRATCH_START_TOKEN)), backup_drill.SCRATCH_START_TOKEN)
                self.assertGreaterEqual(stop_unit.call_count, 1)
                self.assertTrue(
                    all(len(call.args) == 1 and call.args[0] for call in stop_unit.call_args_list)
                )
                self.assertGreater(launcher.wait_calls, 0)
                self.assertEqual(launcher.returncode, 17)
                self.assertFalse(pathlib.Path(unit_command.call_args.kwargs["mountpoint"]).exists())
            stdin_reader.close()
            stdout_reader.close()
            stderr_reader.close()

    def _assert_worker_authorization_barrier(self) -> None:
        scratch_root = pathlib.Path("/run") / f"oldsparky-backup-restore-{uuid.uuid4().hex[:24]}"
        scratch_root.mkdir(mode=0o700)
        args = argparse.Namespace(_scratch_root=str(scratch_root))
        try:
            for payload in (
                b"",
                backup_drill.SCRATCH_START_TOKEN[:-1],
                backup_drill.SCRATCH_START_TOKEN,
                backup_drill.SCRATCH_START_TOKEN + b"x",
            ):
                read_fd, write_fd = os.pipe()
                try:
                    if payload:
                        os.write(write_fd, payload)
                    os.close(write_fd)
                    stdin_stream = types.SimpleNamespace(
                        fileno=lambda: read_fd,
                        buffer=io.BytesIO(payload),
                    )
                    with (
                        mock.patch.dict(backup_drill.os.environ, {"PG_START_BARRIER_SENTINEL": "preserve"}),
                        mock.patch.object(backup_drill.sys, "stdin", stdin_stream),
                        mock.patch.object(backup_drill, "_scratch_mount_snapshot") as mount_snapshot,
                        mock.patch.object(backup_drill, "_scratch_cgroup_snapshot") as cgroup_snapshot,
                        mock.patch.object(backup_drill, "_scratch_mem_available_bytes") as memory_snapshot,
                        mock.patch.object(backup_drill, "_open_verified_scratch_archive") as open_archive,
                        mock.patch.object(backup_drill, "_scratch_create_application_role") as create_role,
                        mock.patch.object(backup_drill, "run_command") as run_command,
                    ):
                        with self.assertRaisesRegex(RuntimeError, "authorization was not received"):
                            backup_drill._scratch_worker_main(args)
                        self.assertEqual(os.environ.get("PG_START_BARRIER_SENTINEL"), "preserve")
                    mount_snapshot.assert_not_called()
                    cgroup_snapshot.assert_not_called()
                    memory_snapshot.assert_not_called()
                    open_archive.assert_not_called()
                    create_role.assert_not_called()
                    run_command.assert_not_called()
                finally:
                    os.close(read_fd)
        finally:
            scratch_root.rmdir()

    def _assert_worker_token_raw_pipe_framing(self) -> None:
        cases = (
            (backup_drill.SCRATCH_START_TOKEN, False, True),
            (b"wrong", False, False),
            (b"X" * len(backup_drill.SCRATCH_START_TOKEN), False, False),
            (backup_drill.SCRATCH_START_TOKEN[:-1], False, False),
            (backup_drill.SCRATCH_START_TOKEN + b"x", False, False),
            (backup_drill.SCRATCH_DONE_TOKEN, True, True),
            (b"", True, False),
            (b"wrong", True, False),
            (b"X" * len(backup_drill.SCRATCH_DONE_TOKEN), True, False),
            (backup_drill.SCRATCH_DONE_TOKEN + b"x", True, False),
            (backup_drill.SCRATCH_DONE_TOKEN[:-1], True, False),
        )
        for payload, require_eof, succeeds in cases:
            with self.subTest(require_eof=require_eof, payload_suffix=payload[-3:]):
                read_fd, write_fd = os.pipe()
                reader = os.fdopen(read_fd, "rb", buffering=0)
                try:
                    os.write(write_fd, payload)
                    if require_eof or not succeeds:
                        os.close(write_fd)
                        write_fd = -1
                    expected = (
                        backup_drill.SCRATCH_DONE_TOKEN
                        if require_eof
                        else backup_drill.SCRATCH_START_TOKEN
                    )
                    if succeeds:
                        self.assertEqual(
                            backup_drill._scratch_read_worker_token(
                                reader,
                                expected,
                                require_eof=require_eof,
                                deadline=time.monotonic() + (
                                    backup_drill.SCRATCH_RUNTIME_SECONDS
                                    if require_eof
                                    else backup_drill.SCRATCH_STARTUP_SECONDS
                                ),
                            ),
                            expected,
                        )
                    else:
                        with self.assertRaisesRegex(RuntimeError, "authorization|completion"):
                            backup_drill._scratch_read_worker_token(
                                reader,
                                expected,
                                require_eof=require_eof,
                                deadline=time.monotonic() + (
                                    backup_drill.SCRATCH_RUNTIME_SECONDS
                                    if require_eof
                                    else backup_drill.SCRATCH_STARTUP_SECONDS
                                ),
                            )
                    self.assertTrue(os.get_blocking(reader.fileno()))
                finally:
                    reader.close()
                    if write_fd >= 0:
                        os.close(write_fd)

        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, "rb", buffering=0)
        try:
            os.write(write_fd, backup_drill.SCRATCH_START_TOKEN)
            os.close(write_fd)
            with self.assertRaisesRegex(RuntimeError, "authorization was not received"):
                backup_drill._scratch_read_worker_token(
                    reader,
                    backup_drill.SCRATCH_START_TOKEN,
                    require_eof=False,
                    deadline=time.monotonic() + backup_drill.SCRATCH_STARTUP_SECONDS,
                )
        finally:
            reader.close()

        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, "rb", buffering=0)
        try:
            with (
                mock.patch.object(backup_drill, "SCRATCH_STARTUP_SECONDS", 0.02),
                self.assertRaisesRegex(RuntimeError, "authorization was not received"),
            ):
                backup_drill._scratch_read_worker_token(
                    reader,
                    backup_drill.SCRATCH_START_TOKEN,
                    require_eof=False,
                    deadline=time.monotonic() + backup_drill.SCRATCH_STARTUP_SECONDS,
                )
        finally:
            reader.close()
            os.close(write_fd)

        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, "rb", buffering=0)
        try:
            with (
                mock.patch.object(backup_drill, "SCRATCH_RUNTIME_SECONDS", 0.02),
                self.assertRaisesRegex(RuntimeError, "completion was not acknowledged"),
            ):
                backup_drill._scratch_read_worker_token(
                    reader,
                    backup_drill.SCRATCH_DONE_TOKEN,
                    require_eof=True,
                    deadline=time.monotonic() + backup_drill.SCRATCH_RUNTIME_SECONDS,
                )
        finally:
            reader.close()
            os.close(write_fd)

    def _assert_scratch_worker_forwards_output_only_when_private(self) -> None:
        class BinaryTextCapture:
            def __init__(self) -> None:
                self.buffer = io.BytesIO()

            def write(self, value: str) -> int:
                return self.buffer.write(value.encode("utf-8"))

            def flush(self) -> None:
                return None

        child_output = b"known pg_restore stdoutknown pg_restore stderr"
        expected_events = [
            ("archive_verify", "start"), ("archive_verify", "end"),
            ("initdb", "start"), ("initdb", "end"),
            ("postgres_start", "start"), ("postgres_start", "end"),
            ("restore_platform", "start"),
        ]

        def assert_private_output(
            forwarded: bytes,
            launch_monotonic_ns: int,
            *,
            expect_worker_allocation: bool = False,
        ) -> None:
            phase_prefix, separator, forwarded_child_output = forwarded.partition(
                child_output
            )
            self.assertEqual(separator, child_output)
            self.assertEqual(forwarded_child_output, b"")
            phase = backup_drill._scratch_parse_phase_attribution(
                phase_prefix,
                truncated=False,
                complete=False,
                launch_monotonic_ns=launch_monotonic_ns,
            )
            self.assertTrue(backup_drill.valid_scratch_phase_attribution(phase))
            self.assertEqual(phase["status"], "unknown")
            self.assertEqual(phase["unknown_reason"], "capture_prefix")
            self.assertEqual(
                [
                    (frame["phase"], frame["event"])
                    for frame in phase["frames"]
                ],
                expected_events,
            )
            if expect_worker_allocation:
                allocation = backup_drill._scratch_parse_allocation_attribution(
                    phase_prefix,
                    truncated=False,
                    archive_size_bytes=23,
                    archive_sha256="f" * 64,
                )
                self.assertTrue(backup_drill.valid_scratch_allocation_attribution(allocation))
                self.assertEqual(allocation["status"], "partial")
                self.assertEqual(
                    [row["phase"] for row in allocation["samples"]],
                    [
                        "initdb_end",
                        "postgres_start_end",
                        "restore_platform_start",
                        "worker_failure",
                    ],
                )

        for private in (True, False):
            scratch_root = pathlib.Path("/run") / f"oldsparky-backup-restore-{uuid.uuid4().hex[:24]}"
            scratch_root.mkdir(mode=0o700)
            try:
                archive_path = scratch_root / "archive.dump"
                archive_bytes = b"PGDMPsynthetic worker archive"
                archive_path.write_bytes(archive_bytes)
                archive_path.chmod(0o600)
                archive_stat = archive_path.stat()
                args = argparse.Namespace(
                    _scratch_root=str(scratch_root),
                    _scratch_archive=str(archive_path),
                    _scratch_archive_sha256=hashlib.sha256(archive_bytes).hexdigest(),
                    _scratch_archive_dev=archive_stat.st_dev,
                    _scratch_archive_ino=archive_stat.st_ino,
                    _scratch_archive_size=archive_stat.st_size,
                    _scratch_timestamp_slug="20261012T010208Z",
                    private_scratch_diagnostics=private,
                )
                worker_failure = backup_drill.RestoreGuardStop("command_failed")
                worker_failure.scratch_worker_private_output = (
                    b"known pg_restore stdout",
                    b"known pg_restore stderr",
                    False,
                    False,
                )

                def fail_during_restore(*_args, **kwargs):
                    kwargs["phase_observer"]("restore_platform", "start")
                    raise worker_failure

                command_results = [
                    subprocess.CompletedProcess([], 0, "/usr/lib/postgresql/16/bin\n", ""),
                    subprocess.CompletedProcess([], 0, "postgres (PostgreSQL) 16.10\n", ""),
                ]

                def fake_run_command(command, **_kwargs):
                    if command[:2] == ["pg_config", "--bindir"]:
                        return command_results[0]
                    if command[-1:] == ["--version"]:
                        return command_results[1]
                    return subprocess.CompletedProcess(command, 0, "", "")

                stdout_capture = BinaryTextCapture()
                stderr_capture = BinaryTextCapture()
                with (
                    mock.patch.object(backup_drill, "_scratch_mount_snapshot"),
                    mock.patch.object(backup_drill, "_scratch_cgroup_snapshot"),
                    mock.patch.object(backup_drill, "_scratch_mem_available_bytes", return_value=2**30),
                    mock.patch.object(backup_drill, "_scratch_resource_guard"),
                    mock.patch.object(backup_drill, "require_commands"),
                    mock.patch.object(
                        backup_drill.pwd,
                        "getpwnam",
                        return_value=types.SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid()),
                    ),
                    mock.patch.object(backup_drill.os, "chown"),
                    mock.patch.object(backup_drill, "run_command", side_effect=fake_run_command),
                    mock.patch.object(backup_drill, "_scratch_create_application_role"),
                    mock.patch.object(
                        backup_drill, "perform_restore_drill", side_effect=fail_during_restore
                    ),
                    mock.patch.object(
                        backup_drill.sys,
                        "stdin",
                        types.SimpleNamespace(buffer=StagedWorkerInput()),
                    ),
                    mock.patch.object(backup_drill.sys, "stdout", stdout_capture),
                    mock.patch.object(backup_drill.sys, "stderr", stderr_capture),
                ):
                    with self.assertRaises(backup_drill.RestoreGuardStop) as raised:
                        backup_drill._scratch_worker_main(args)

                self.assertIs(raised.exception, worker_failure)
                if private:
                    self.assertEqual(stdout_capture.buffer.getvalue(), b"")
                    forwarded = stderr_capture.buffer.getvalue()
                    assert_private_output(
                        forwarded,
                        args._scratch_phase_origin_monotonic_ns,
                        expect_worker_allocation=True,
                    )
                else:
                    self.assertEqual(stdout_capture.buffer.getvalue(), b"")
                    private_stderr = stderr_capture.buffer.getvalue()
                    self.assertEqual(private_stderr, b"")
                    self.assertNotIn(backup_drill.SCRATCH_PHASE_FRAME_MARKER, private_stderr)
            finally:
                shutil.rmtree(scratch_root)

        launch_monotonic_ns = time.monotonic_ns() - 100_000_000

        def emit(events: list[tuple[str, str]], *, private: bool = True) -> bytes:
            args = argparse.Namespace(
                private_scratch_diagnostics=private,
                _scratch_authorization_probe=False,
                _scratch_phase_origin_monotonic_ns=launch_monotonic_ns,
                _scratch_phase_ordinal=0,
            )
            capture = BinaryTextCapture()
            with mock.patch.object(backup_drill.sys, "stderr", capture):
                for phase_name, event_name in events:
                    backup_drill._scratch_emit_phase_boundary(
                        args, phase_name, event_name
                    )
            return capture.buffer.getvalue()

        phase_prefix = emit(expected_events)
        assert_private_output(phase_prefix + child_output, launch_monotonic_ns)
        with self.assertRaises(AssertionError):
            assert_private_output(
                phase_prefix + child_output + b"unexpected", launch_monotonic_ns
            )
        with self.assertRaises(AssertionError):
            assert_private_output(
                phase_prefix + b"different child payload", launch_monotonic_ns
            )
        invalid_order = emit(
            [
                ("archive_verify", "start"), ("archive_verify", "end"),
                ("initdb", "end"), ("initdb", "start"),
                ("postgres_start", "start"), ("postgres_start", "end"),
            ]
        )
        with self.assertRaises(AssertionError):
            assert_private_output(invalid_order + child_output, launch_monotonic_ns)
        private_disabled = emit([("archive_verify", "start")], private=False)
        self.assertEqual(private_disabled, b"")
        self.assertNotIn(backup_drill.SCRATCH_PHASE_FRAME_MARKER, private_disabled)

    def test_run_disk_guarded_command_captures_only_opted_in_failure_output(self) -> None:
        target = backup_drill.DatabaseTarget(
            "127.0.0.1", 5432, "platform_user", "synthetic-secret", "platformdb"
        )
        stdout_payload = b"pg_restore stdout:" + b"o" * 9000
        stderr_payload = b"restore failure marker:" + b"e" * 40000
        child_code = (
            "import sys; "
            f"sys.stdout.buffer.write({stdout_payload!r}); sys.stdout.flush(); "
            f"sys.stderr.buffer.write({stderr_payload!r}); sys.stderr.flush(); sys.exit(23)"
        )
        with (
            mock.patch.object(backup_drill, "require_restore_headroom"),
            mock.patch.object(backup_drill, "command_env", return_value={"PATH": os.environ["PATH"]}),
        ):
            with self.assertRaises(backup_drill.RestoreGuardStop) as failure:
                backup_drill.run_disk_guarded_command(
                    [sys.executable, "-c", child_code],
                    target=target,
                    stage="pg_restore",
                    capture_private_output=True,
                )
        self.assertEqual(failure.exception.reason, "command_failed")
        private_output = failure.exception.scratch_worker_private_output
        self.assertEqual(private_output[0], stdout_payload[: backup_drill.SCRATCH_PRIVATE_STDOUT_BYTES])
        self.assertEqual(private_output[1], stderr_payload[: backup_drill.SCRATCH_PRIVATE_STDERR_BYTES])
        self.assertTrue(private_output[2])
        self.assertTrue(private_output[3])

        benign_code = (
            "import sys; sys.stdout.write('result'); sys.stderr.write('benign notice')"
        )
        with (
            mock.patch.object(backup_drill, "require_restore_headroom"),
            mock.patch.object(backup_drill, "command_env", return_value={"PATH": os.environ["PATH"]}),
        ):
            backup_drill.run_disk_guarded_command(
                [sys.executable, "-c", benign_code],
                target=target,
                stage="pg_restore",
                capture_private_output=True,
            )

    def test_run_disk_guarded_command_preserves_primary_when_reap_fails(self) -> None:
        target = backup_drill.DatabaseTarget(
            "127.0.0.1", 5432, "platform_user", "synthetic-secret", "platformdb"
        )
        stdout_payload = b"restore reached child\n"
        stderr_payload = b"bounded private failure detail\n"
        child_code = (
            "import sys,time; "
            f"sys.stdout.buffer.write({stdout_payload!r}); sys.stdout.flush(); "
            f"sys.stderr.buffer.write({stderr_payload!r}); sys.stderr.flush(); "
            "time.sleep(30)"
        )
        resource_guard_calls = 0

        def fail_after_spawn() -> None:
            nonlocal resource_guard_calls
            resource_guard_calls += 1
            if resource_guard_calls == 2:
                time.sleep(0.1)
                raise RuntimeError("primary restore resource failure")

        cleanup_calls: list[int] = []

        def stop_then_report_failure(process, *, phase=None) -> None:
            cleanup_calls.append(process.pid)
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise backup_drill.RestoreGuardStop("child_unstopped", phase=phase)

        with (
            mock.patch.object(backup_drill, "require_restore_headroom"),
            mock.patch.object(backup_drill, "command_env", return_value={"PATH": os.environ["PATH"]}),
            mock.patch.object(backup_drill, "_stop_owned_child", side_effect=stop_then_report_failure),
        ):
            with self.assertRaisesRegex(RuntimeError, "primary restore resource failure") as failure:
                backup_drill.run_disk_guarded_command(
                    [sys.executable, "-c", child_code],
                    target=target,
                    stage="pg_restore",
                    resource_guard=fail_after_spawn,
                    capture_private_output=True,
                )

        self.assertEqual(len(cleanup_calls), 1)
        self.assertEqual(failure.exception.scratch_worker_cleanup_error, "child_unstopped")
        self.assertEqual(
            failure.exception.scratch_worker_private_output,
            (stdout_payload, stderr_payload, False, False),
        )

    def _assert_scratch_startup_inactive_race_is_handled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            archive_path = pathlib.Path(temporary_dir) / "platformdb.dump"
            archive_path.write_bytes(b"PGDMPsynthetic archive")
            archive_path.chmod(0o600)
            command = [
                sys.executable,
                "-c",
                "import json,sys; "
                f"start={backup_drill.SCRATCH_START_TOKEN!r}; "
                f"done={backup_drill.SCRATCH_DONE_TOKEN!r}; "
                "ack=sys.stdin.buffer.read(len(start)); assert ack == start; "
                "sys.stdout.write(json.dumps({'ok':True,'restored_table_count':1})+'\\n'); sys.stdout.flush(); "
                "assert sys.stdin.buffer.read(len(done)) == done",
            ]
            unit_command = mock.Mock(return_value=command)
            real_popen = subprocess.Popen
            launched = []

            def launch(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                launched.append(process)
                return process

            snapshots_seen = []

            def service_snapshot(_unit_name):
                if not snapshots_seen:
                    state = {
                        "properties": {
                            "LoadState": "not-found",
                            "ActiveState": "inactive",
                            "SubState": "dead",
                            "Result": "success",
                            "ExecMainPID": "0",
                            "ControlGroup": "",
                        }
                    }
                elif len(snapshots_seen) == 1:
                    state = {
                        "properties": {
                            "LoadState": "loaded",
                            "ActiveState": "inactive",
                            "SubState": "dead",
                            "Result": "success",
                            "ExecMainPID": "0",
                            "ControlGroup": "",
                        }
                    }
                elif launched and launched[0].poll() is None:
                    state = {
                        "properties": {
                            "LoadState": "loaded",
                            "ActiveState": "active",
                            "ExecMainPID": "123",
                            "InvocationID": "0123456789abcdef0123456789abcdef",
                            "ControlGroup": "/system.slice/scratch-startup-race.service",
                        }
                    }
                else:
                    state = {
                        "properties": {
                            "LoadState": "not-found",
                            "ActiveState": "inactive",
                            "ExecMainPID": "0",
                            "ControlGroup": "",
                        }
                    }
                snapshots_seen.append(state["properties"]["ActiveState"])
                return state

            with (
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "_scratch_host_guard"),
                mock.patch.object(backup_drill, "_scratch_unit_command", unit_command),
                mock.patch.object(backup_drill, "_scratch_mountpoint_is_mounted", return_value=False),
                mock.patch.object(backup_drill, "_scratch_service_snapshot", side_effect=service_snapshot),
                mock.patch.object(backup_drill, "_scratch_expected_properties", return_value="/system.slice/scratch-startup-race.service"),
                mock.patch.object(backup_drill, "_scratch_control_group_snapshot", return_value={}),
                mock.patch.object(backup_drill.subprocess, "Popen", side_effect=launch),
                mock.patch.object(backup_drill.secrets, "token_hex", return_value=uuid.uuid4().hex[:24]),
            ):
                result = backup_drill._run_isolated_restore_drill(
                    archive_path,
                    timestamp_slug="20261012T010203Z",
                )

            self.assertEqual(result, 1)
            self.assertEqual(snapshots_seen[0], "inactive")
            self.assertEqual(snapshots_seen[1], "inactive")
            self.assertIn("active", snapshots_seen)
            self.assertFalse(launched[0].poll() is None)
            mountpoint = pathlib.Path(unit_command.call_args.kwargs["mountpoint"])
            self.assertFalse(mountpoint.exists())

    def _assert_synthetic_worker_failure_pair_reaches_parent(self) -> None:
        """Exercise child main(), pipe ordering, parent binding, and cleanup."""
        self.assertEqual(os.geteuid(), 0)
        real_builder = backup_drill._scratch_unit_command
        launched: list[subprocess.Popen[bytes]] = []
        stop_requested = False
        invocation_id = "0123456789abcdef0123456789abcdef"
        control_group = "/system.slice/scratch-synthetic-failure.service"

        child_source = f"""\
import importlib.util
import pathlib
import sys
import time

path = pathlib.Path({str(SCRIPT_PATH)!r})
spec = importlib.util.spec_from_file_location("scratch_child", path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

def fail_mount(*args, **kwargs):
    raise RuntimeError("private child detail")

module._scratch_mount_snapshot = fail_mount
module._scratch_emit_allocation_sample = lambda *args, **kwargs: None
sys.argv = [str(path), *sys.argv[1:]]
status = module.main()
time.sleep(2)
raise SystemExit(status)
"""

        def unit_command(**kwargs: Any) -> list[str]:
            built = real_builder(**kwargs)
            worker_args = built[built.index(backup_drill.SCRATCH_WORKER_MODE):]
            return [sys.executable, "-c", child_source, *worker_args]

        real_popen = subprocess.Popen

        def launch(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
            process = real_popen(*args, **kwargs)
            launched.append(process)
            return process

        def service_snapshot(_unit_name: str) -> dict[str, Any]:
            process = launched[0] if launched else None
            active = process is not None and process.poll() is None and not stop_requested
            properties = {
                "LoadState": "loaded" if active else "not-found",
                "ActiveState": "active" if active else "inactive",
                "SubState": "running" if active else "dead",
                "Result": "success" if active else "exit-code",
                "ExecMainPID": str(process.pid) if active and process is not None else "0",
                "InvocationID": invocation_id,
                "ExecMainCode": "1" if not active else "0",
                "ExecMainStatus": "1" if not active else "0",
                "ControlGroup": control_group if active else "",
            }
            return {"properties": properties, "load_state": properties["LoadState"]}

        def stop_unit(_unit_name: str) -> None:
            nonlocal stop_requested
            stop_requested = True
            if launched and launched[0].poll() is None:
                launched[0].terminate()
                launched[0].wait(timeout=3)

        def no_mount(_mountpoint: pathlib.Path) -> bool:
            return False

        def run_parent() -> BaseException:
            with self.assertRaises(RuntimeError) as failure:
                backup_drill._run_isolated_restore_drill(
                    None,
                    timestamp_slug="20261018T120000Z",
                    synthetic_restore_diagnostic=True,
                    scratch_backend=backup_drill.SCRATCH_BACKEND_TMPFS_V1,
                )
            return failure.exception

        with (
            mock.patch.object(backup_drill, "require_commands"),
            mock.patch.object(backup_drill, "_scratch_host_guard"),
            mock.patch.object(
                backup_drill,
                "_scratch_prepare_synthetic_protected_paths",
                return_value=((), None),
            ),
            mock.patch.object(
                backup_drill, "_scratch_remove_created_protected_path_directories"
            ) as cleanup_fixture_paths,
            mock.patch.object(backup_drill, "_scratch_unit_command", side_effect=unit_command),
            mock.patch.object(backup_drill, "_scratch_mountpoint_is_mounted", side_effect=no_mount),
            mock.patch.object(backup_drill, "_scratch_service_snapshot", side_effect=service_snapshot),
            mock.patch.object(
                backup_drill, "_scratch_expected_properties", return_value=control_group
            ),
            mock.patch.object(backup_drill, "_scratch_control_group_snapshot", return_value={}),
            mock.patch.object(backup_drill, "_scratch_stop_unit", side_effect=stop_unit),
            mock.patch.object(
                backup_drill,
                "_scratch_unit_journal_diagnostic",
                return_value=backup_drill._scratch_empty_journal_diagnostic("unavailable"),
            ),
            mock.patch.object(backup_drill, "_scratch_reset_failed_unit"),
            mock.patch.object(
                backup_drill,
                "_scratch_wait_unit_not_found",
                return_value={"properties": {"LoadState": "not-found"}, "load_state": "not-found"},
            ),
            mock.patch.object(backup_drill.subprocess, "Popen", side_effect=launch),
            mock.patch.object(backup_drill.secrets, "token_hex", return_value=uuid.uuid4().hex[:24]),
        ):
            failure = run_parent()
            signal_mask_before_cleanup_failure = signal.pthread_sigmask(
                signal.SIG_BLOCK, set()
            )
            launched.clear()
            stop_requested = False
            cleanup_fixture_paths.side_effect = RuntimeError(
                "injected synthetic fixture cleanup failure"
            )
            cleanup_failure = run_parent()
            self.assertEqual(
                signal.pthread_sigmask(signal.SIG_BLOCK, set()),
                signal_mask_before_cleanup_failure,
            )
            self.assertIsNotNone(
                getattr(cleanup_failure, "scratch_cleanup_status", None)
            )

        self.assertEqual(len(launched), 1)
        self.assertIsNotNone(launched[0].returncode)
        self.assertTrue(stop_requested)
        self.assertEqual(
            getattr(failure, "scratch_first_fault", None),
            {
                "schema": 1,
                "origin": "worker",
                "site": "worker_mount_snapshot",
                "exception_kind": "runtime",
                "errno": None,
                "verification_site": "worker_mount_identity",
            },
            msg=f"parent failure={failure!r}; unit={getattr(failure, 'scratch_unit_diagnostic', None)!r}",
        )
        unit = getattr(failure, "scratch_unit_diagnostic", None)
        self.assertTrue(backup_drill.valid_scratch_unit_diagnostic(unit))
        assert isinstance(unit, dict)
        self.assertEqual(unit["worker_frame_status"], "worker_failure_report")
        self.assertEqual(unit["phase"], "authorization_failed")
        self.assertTrue(unit["terminal_snapshot"]["start_token_sent"])
        self.assertFalse(unit["terminal_snapshot"]["completion_token_sent"])
        self.assertEqual(getattr(failure, "scratch_cleanup_status", None), "confirmed")

    def _assert_scratch_final_identity_loss_blocks_done(self) -> None:
        for replacement in (
            "pid", "invocation", "starttime", "cgroup", "missing", "late_start", "late_done",
            "poll_error", "read_error", "unregister_error", "host_guard_error", "output_bound_error",
        ):
            with self.subTest(final_identity=replacement), tempfile.TemporaryDirectory() as temporary_dir:
                archive_path = pathlib.Path(temporary_dir) / "platformdb.dump"
                archive_path.write_bytes(b"PGDMPsynthetic archive")
                archive_path.chmod(0o600)
                command = [
                    sys.executable,
                    "-c",
                    "import sys; "
                    f"start={backup_drill.SCRATCH_START_TOKEN!r}; "
                    f"done={backup_drill.SCRATCH_DONE_TOKEN!r}; "
                    "assert sys.stdin.buffer.read(len(start)) == start; "
                    "sys.stdout.write('{\\\"ok\\\":true,\\\"restored_table_count\\\":1}\\n'); sys.stdout.flush(); "
                    "assert sys.stdin.buffer.read(len(done)) == done; "
                    "sys.stderr.write('DONE-RECEIVED'); sys.stderr.flush()",
                ]
                launched: list[subprocess.Popen[bytes]] = []
                real_popen = subprocess.Popen

                def launch(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
                    process = real_popen(*args, **kwargs)
                    launched.append(process)
                    return process

                snapshots_seen = 0
                base_properties = {
                    "LoadState": "loaded",
                    "ActiveState": "active",
                    "SubState": "running",
                    "Result": "success",
                    "ExecMainPID": "424242",
                    "InvocationID": "0123456789abcdef0123456789abcdef",
                    "ExecMainStatus": "0",
                    "ControlGroup": "/system.slice/scratch-final-identity.service",
                }

                def service_snapshot(_unit_name: str) -> dict[str, Any]:
                    nonlocal snapshots_seen
                    snapshots_seen += 1
                    if snapshots_seen == 1:
                        return {"properties": {**base_properties, "LoadState": "not-found", "ActiveState": "inactive", "ExecMainPID": "0", "ControlGroup": ""}}
                    if snapshots_seen == 2:
                        return {"properties": {**base_properties, "ActiveState": "inactive", "ExecMainPID": "0", "ControlGroup": ""}}
                    if snapshots_seen == 5 and replacement == "missing":
                        raise FileNotFoundError("synthetic final unit snapshot loss")
                    properties = dict(base_properties)
                    if snapshots_seen == 5:
                        if replacement == "pid":
                            properties["ExecMainPID"] = "424243"
                        elif replacement == "invocation":
                            properties["InvocationID"] = "1123456789abcdef0123456789abcdef"
                    if snapshots_seen >= 6:
                        properties.update({"LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead", "ExecMainPID": "0", "ControlGroup": ""})
                    return {"properties": properties}

                expected_properties_calls = 0

                def expected_properties(snapshot: dict[str, Any], **_kwargs: Any) -> str:
                    nonlocal expected_properties_calls
                    expected_properties_calls += 1
                    if replacement == "cgroup" and expected_properties_calls == 3:
                        return "/system.slice/replaced-cgroup.service"
                    return snapshot["properties"]["ControlGroup"]

                start_tokens: list[bytes] = []
                host_guard_injected: list[bool] = []
                selector_instances = []
                send_token = backup_drill._scratch_send_worker_token

                def capture_token(process: subprocess.Popen[bytes], *, token: bytes, deadline: float, close_after: bool) -> None:
                    start_tokens.append(token)
                    send_token(process, token=token, deadline=deadline, close_after=close_after)
                    if replacement == "late_start" and token == backup_drill.SCRATCH_START_TOKEN:
                        raise backup_drill._ScratchTokenDeliveryError(
                            "synthetic deadline after complete START write", complete=True
                        )
                    if replacement == "late_done" and token == backup_drill.SCRATCH_DONE_TOKEN:
                        raise backup_drill._ScratchTokenDeliveryError(
                            "synthetic deadline after complete DONE write", complete=True
                        )

                def stop_unit(_unit_name: str) -> None:
                    if launched and launched[0].poll() is None:
                        launched[0].kill()
                        launched[0].wait(timeout=2)

                class PollFailureSelector:
                    def __init__(self) -> None:
                        self.registered: dict[int, object] = {}
                        self.read_failure_sent = False
                        self.unregister_failure_sent = False
                        selector_instances.append(self)

                    def __enter__(self):
                        return self

                    def __exit__(self, *_args: Any) -> None:
                        return None

                    def register(self, fileobj: object, events: int, data: object = None) -> None:
                        self.registered[id(fileobj)] = types.SimpleNamespace(
                            fileobj=fileobj, events=events, data=data
                        )

                    def select(self, timeout: float | None = None) -> list[object]:
                        write_events = [
                            key for key in self.registered.values()
                            if key.events & selectors.EVENT_WRITE
                        ]
                        if write_events:
                            return [(key, selectors.EVENT_WRITE) for key in write_events]
                        if (
                            backup_drill.SCRATCH_START_TOKEN in start_tokens
                            and not self.read_failure_sent
                            and replacement == "poll_error"
                        ):
                            self.read_failure_sent = True
                            raise OSError(5, "synthetic poll failure")
                        if (
                            backup_drill.SCRATCH_START_TOKEN in start_tokens
                            and replacement == "host_guard_error"
                        ):
                            # Keep the worker result unread so the next outer
                            # iteration reaches the injected host guard before
                            # any completion candidate can authorize DONE.
                            return []
                        if backup_drill.SCRATCH_START_TOKEN in start_tokens:
                            return [
                                (key, selectors.EVENT_READ) for key in self.registered.values()
                                if key.events & selectors.EVENT_READ
                            ]
                        return []

                    def unregister(self, *_args: Any) -> None:
                        if replacement == "unregister_error" and not self.unregister_failure_sent:
                            self.unregister_failure_sent = True
                            raise OSError(5, "synthetic unregister failure")
                        return None

                    def get_key(self, fileobj: object) -> object:
                        return self.registered[id(fileobj)]

                    def get_map(self) -> dict[object, object]:
                        return {}

                    def close(self) -> None:
                        return None

                selector_factory = (
                    PollFailureSelector
                    if replacement in {
                        "poll_error", "read_error", "unregister_error", "host_guard_error",
                        "output_bound_error",
                    }
                    else backup_drill.selectors.DefaultSelector
                )

                original_os_read = os.read
                injected_read = False
                injected_read_streams: list[str] = []

                def read_with_injected_failure(fd: int, size: int) -> bytes:
                    nonlocal injected_read
                    if launched and fd in {
                        launched[0].stdout.fileno(), launched[0].stderr.fileno(),
                    } and not injected_read:
                        if replacement == "read_error":
                            injected_read = True
                            raise OSError(5, "synthetic stream read failure")
                        if replacement == "unregister_error":
                            injected_read = True
                            return b""
                        if (
                            replacement == "output_bound_error"
                            and fd == launched[0].stdout.fileno()
                        ):
                            injected_read = True
                            injected_read_streams.append("stdout")
                            return b"x" * 4097  # Normal restore stdout limit is 4096 bytes.
                    return original_os_read(fd, size)

                def host_guard(site: str) -> None:
                    if (
                        replacement == "host_guard_error"
                        and site == "parent_active_poll"
                        and backup_drill.SCRATCH_START_TOKEN in start_tokens
                    ):
                        host_guard_injected.append(True)
                        raise OSError(5, "synthetic host guard failure")

                unit_command = mock.Mock(return_value=command)
                with (
                    mock.patch.object(backup_drill, "require_commands"),
                    mock.patch.object(backup_drill, "_scratch_host_guard", side_effect=host_guard),
                    mock.patch.object(backup_drill, "_scratch_unit_command", unit_command),
                    mock.patch.object(backup_drill, "_scratch_mountpoint_is_mounted", return_value=False),
                    mock.patch.object(backup_drill, "_scratch_service_snapshot", side_effect=service_snapshot),
                    mock.patch.object(backup_drill, "_scratch_expected_properties", side_effect=expected_properties),
                    mock.patch.object(backup_drill, "_scratch_control_group_snapshot", return_value={}),
                    mock.patch.object(backup_drill, "_scratch_process_start_time", side_effect=("123456", "123456", "999999" if replacement == "starttime" else "123456")),
                    mock.patch.object(backup_drill, "_scratch_send_worker_token", side_effect=capture_token),
                    mock.patch.object(backup_drill, "_scratch_stop_unit", side_effect=stop_unit),
                    mock.patch.object(backup_drill.subprocess, "Popen", side_effect=launch),
                    mock.patch.object(backup_drill.secrets, "token_hex", return_value=uuid.uuid4().hex[:24]),
                    mock.patch.object(backup_drill.selectors, "DefaultSelector", new=selector_factory),
                    mock.patch.object(backup_drill.os, "read", side_effect=read_with_injected_failure),
                ):
                    with self.assertRaises(Exception) as failure:
                        backup_drill._run_isolated_restore_drill(
                            archive_path,
                            timestamp_slug="20261012T010206Z",
                            private_scratch_diagnostics=True,
                        )
                self.assertEqual(
                    start_tokens,
                    [backup_drill.SCRATCH_START_TOKEN]
                    + ([backup_drill.SCRATCH_DONE_TOKEN] if replacement == "late_done" else []),
                )
                self.assertTrue(launched)
                self.assertIsNotNone(launched[0].returncode)
                if replacement != "poll_error":
                    self.assertEqual(
                        getattr(failure.exception, "scratch_unit_diagnostic", {}).get("phase"),
                        "authorization_failed",
                    )
                self.assertEqual(
                    getattr(failure.exception, "scratch_unit_diagnostic", {}).get("terminal_snapshot", {}).get(
                        "completion_token_sent", False
                    ),
                    replacement == "late_done",
                )
                if replacement in {
                    "poll_error", "read_error", "unregister_error", "host_guard_error", "output_bound_error",
                }:
                    first_fault = getattr(failure.exception, "scratch_first_fault", None)
                    self.assertIsInstance(first_fault, dict)
                    assert isinstance(first_fault, dict)
                    self.assertEqual(first_fault.get("site"), "controller_active_poll")
                    expected_site = {
                        "poll_error": "active_poll_selector_wait",
                        "read_error": "active_poll_stream_read",
                        "unregister_error": "active_poll_stream_unregister",
                        "host_guard_error": "active_poll_host_guard",
                        "output_bound_error": "active_poll_output_bound",
                    }[replacement]
                    if replacement == "output_bound_error":
                        self.assertTrue(injected_read, "oversized stdout injection was not reached")
                        self.assertEqual(injected_read_streams, ["stdout"])
                    self.assertEqual(
                        first_fault.get("verification_site"),
                        expected_site,
                        msg=f"{replacement}: {first_fault!r}",
                    )
                    expected_kind, expected_errno = (
                        ("runtime", None)
                        if replacement == "output_bound_error"
                        else ("os_error", 5)
                    )
                    self.assertEqual(first_fault.get("exception_kind"), expected_kind)
                    self.assertEqual(first_fault.get("errno"), expected_errno)
                    self.assertIn(backup_drill.SCRATCH_START_TOKEN, start_tokens)
                    if replacement == "poll_error":
                        self.assertTrue(selector_instances[0].read_failure_sent)
                    elif replacement == "unregister_error":
                        self.assertTrue(selector_instances[0].unregister_failure_sent)
                    elif replacement in {"read_error", "output_bound_error"}:
                        self.assertTrue(injected_read)
                    elif replacement == "host_guard_error":
                        self.assertEqual(host_guard_injected, [True])
                    unit_diagnostic = getattr(failure.exception, "scratch_unit_diagnostic", None)
                    self.assertTrue(backup_drill.valid_scratch_unit_diagnostic(unit_diagnostic))
                    assert isinstance(unit_diagnostic, dict)
                    self.assertIn(unit_diagnostic["schema"], {4, 5})
                    if unit_diagnostic["schema"] == 5:
                        self.assertTrue(unit_diagnostic["terminal_snapshot"]["start_token_sent"])
                    self.assertEqual(unit_diagnostic["verification_site"], expected_site)
                    self.assertEqual(
                        unit_diagnostic["worker_frame_status"],
                        "missing" if unit_diagnostic["stdout_bytes"] == 0 else "invalid",
                    )
                    if replacement == "output_bound_error":
                        self.assertGreater(unit_diagnostic["stdout_bytes"], 0)
                self.assertFalse(pathlib.Path(unit_command.call_args.kwargs["mountpoint"]).exists())

    def _assert_worker_token_deadline_delivery_is_truthful(self) -> None:
        class WritableSelector:
            def __enter__(self):
                return self

            def __exit__(self, *_args: Any) -> None:
                return None

            def register(self, *_args: Any, **_kwargs: Any) -> None:
                return None

            def select(self, timeout: float | None = None) -> list[object]:
                return [object()]

        for token, close_after in (
            (backup_drill.SCRATCH_START_TOKEN, False),
            (backup_drill.SCRATCH_DONE_TOKEN, True),
        ):
            read_fd, write_fd = os.pipe()
            stream = os.fdopen(write_fd, "wb", buffering=0)
            process = types.SimpleNamespace(stdin=stream, poll=lambda: None)
            clock = [9.0]
            original_write = os.write

            def write_then_cross_deadline(fd: int, data: bytes | memoryview) -> int:
                written = original_write(fd, data)
                clock[0] = 11.0
                return written

            try:
                with (
                    mock.patch.object(backup_drill.selectors, "DefaultSelector", return_value=WritableSelector()),
                    mock.patch.object(backup_drill.time, "monotonic", side_effect=lambda: clock[0]),
                    mock.patch.object(backup_drill.os, "write", side_effect=write_then_cross_deadline),
                ):
                    with self.assertRaises(backup_drill._ScratchTokenDeliveryError) as failure:
                        backup_drill._scratch_send_worker_token(
                            process,
                            token=token,
                            deadline=10.0,
                            close_after=close_after,
                        )
                self.assertTrue(failure.exception.complete)
                self.assertEqual(os.read(read_fd, len(token)), token)
            finally:
                if not stream.closed:
                    stream.close()
                os.close(read_fd)

        read_fd, write_fd = os.pipe()
        stream = os.fdopen(write_fd, "wb", buffering=0)
        process = types.SimpleNamespace(stdin=stream, poll=lambda: None)
        original_write = os.write
        write_count = 0

        def partial_then_fail(fd: int, data: bytes | memoryview) -> int:
            nonlocal write_count
            write_count += 1
            if write_count == 1:
                return original_write(fd, data[:1])
            raise BrokenPipeError("synthetic partial token channel failure")

        try:
            with (
                mock.patch.object(backup_drill.selectors, "DefaultSelector", return_value=WritableSelector()),
                mock.patch.object(backup_drill.time, "monotonic", return_value=9.0),
                mock.patch.object(backup_drill.os, "write", side_effect=partial_then_fail),
            ):
                with self.assertRaises(backup_drill._ScratchTokenDeliveryError) as failure:
                    backup_drill._scratch_send_worker_token(
                        process,
                        token=backup_drill.SCRATCH_START_TOKEN,
                        deadline=10.0,
                        close_after=False,
                    )
            self.assertFalse(failure.exception.complete)
            self.assertEqual(os.read(read_fd, 1), backup_drill.SCRATCH_START_TOKEN[:1])
        finally:
            if not stream.closed:
                stream.close()
            os.close(read_fd)

    def _assert_scratch_fast_launcher_cannot_skip_active_confinement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            archive_path = pathlib.Path(temporary_dir) / "platformdb.dump"
            archive_path.write_bytes(b"PGDMPsynthetic archive")
            archive_path.chmod(0o600)
            command = [sys.executable, "-c", "print('{\"ok\":true,\"restored_table_count\":1}')"]
            real_popen = subprocess.Popen
            launched = []

            def launch(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                process.wait(timeout=2)
                launched.append(process)
                return process

            snapshots = []

            def service_snapshot(_unit_name):
                snapshots.append(True)
                return {
                    "properties": {
                        "LoadState": "not-found",
                        "ActiveState": "inactive",
                        "ExecMainPID": "0",
                        "ControlGroup": "",
                    }
                }

            unit_command = mock.Mock(return_value=command)
            with (
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "_scratch_host_guard"),
                mock.patch.object(backup_drill, "_scratch_unit_command", unit_command),
                mock.patch.object(backup_drill, "_scratch_mountpoint_is_mounted", return_value=False),
                mock.patch.object(backup_drill, "_scratch_service_snapshot", side_effect=service_snapshot),
                mock.patch.object(backup_drill.subprocess, "Popen", side_effect=launch),
                mock.patch.object(backup_drill.secrets, "token_hex", return_value=uuid.uuid4().hex[:24]),
            ):
                with self.assertRaisesRegex(RuntimeError, "exited before its confinement was verified"):
                    backup_drill._run_isolated_restore_drill(
                        archive_path,
                        timestamp_slug="20261012T010204Z",
                    )

            self.assertTrue(launched)
            self.assertTrue(snapshots)
            self.assertIsNotNone(launched[0].returncode)
            mountpoint = pathlib.Path(unit_command.call_args.kwargs["mountpoint"])
            self.assertFalse(mountpoint.exists())

    def _assert_scratch_absent_unit_has_bounded_startup_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            archive_path = pathlib.Path(temporary_dir) / "platformdb.dump"
            archive_path.write_bytes(b"PGDMPsynthetic archive")
            archive_path.chmod(0o600)
            command = [sys.executable, "-c", "import time; time.sleep(10)"]
            real_popen = subprocess.Popen
            launched = []
            snapshots = []

            def launch(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                launched.append(process)
                return process

            def service_snapshot(_unit_name):
                snapshots.append(True)
                return {
                    "properties": {
                        "LoadState": "not-found",
                        "ActiveState": "inactive",
                        "SubState": "dead",
                        "Result": "success",
                        "ExecMainPID": "0",
                        "ControlGroup": "",
                    }
                }

            def stop_unit(_unit_name):
                if launched and launched[0].poll() is None:
                    launched[0].kill()
                    launched[0].wait(timeout=2)

            unit_command = mock.Mock(return_value=command)
            started = time.monotonic()
            with (
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "_scratch_host_guard"),
                mock.patch.object(backup_drill, "_scratch_unit_command", unit_command),
                mock.patch.object(backup_drill, "_scratch_mountpoint_is_mounted", return_value=False),
                mock.patch.object(backup_drill, "_scratch_service_snapshot", side_effect=service_snapshot),
                mock.patch.object(backup_drill, "_scratch_stop_unit", side_effect=stop_unit),
                mock.patch.object(backup_drill.subprocess, "Popen", side_effect=launch),
                mock.patch.object(backup_drill.secrets, "token_hex", return_value=uuid.uuid4().hex[:24]),
                mock.patch.object(backup_drill, "SCRATCH_STARTUP_SECONDS", 0.05),
            ):
                with self.assertRaisesRegex(RuntimeError, "startup deadline"):
                    backup_drill._run_isolated_restore_drill(
                        archive_path,
                        timestamp_slug="20261012T010205Z",
                    )
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 2)
            self.assertGreaterEqual(len(snapshots), 2)
            self.assertEqual(launched[0].returncode, -signal.SIGKILL)
            self.assertFalse(pathlib.Path(unit_command.call_args.kwargs["mountpoint"]).exists())
    def test_local_admin_commands_use_postgres_os_user(self) -> None:
        target = backup_drill.DatabaseTarget("127.0.0.1", 5432, "platform_user", None, "platformdb")

        self.assertEqual(
            backup_drill.local_postgres_admin_command("create", target, "platform_restore_drill_test"),
            [
                "runuser",
                "-u",
                "postgres",
                "--",
                "createdb",
                "--owner",
                "platform_user",
                "platform_restore_drill_test",
            ],
        )

    def test_restore_drill_captures_extension_output_for_json_callers(self) -> None:
        target = backup_drill.DatabaseTarget(
            "127.0.0.1", 5432, "platform_user", "secret", "platformdb"
        )
        admin_target = backup_drill.DatabaseTarget(
            "127.0.0.1", 5432, "postgres", None, "postgres"
        )
        responses = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "CREATE EXTENSION\n", ""),
            subprocess.CompletedProcess([], 0, "CREATE SCHEMA\n", ""),
            subprocess.CompletedProcess([], 0, "22\n", ""),
            subprocess.CompletedProcess([], 0, "1\n", ""),
            subprocess.CompletedProcess([], 0, "20260801_0036\n", ""),
            subprocess.CompletedProcess([], 0, "1\n", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "0\n", ""),
        ]

        archive = io.BytesIO(b"PGDMPsynthetic archive")
        resource_guard = mock.Mock()
        observed_phases: list[tuple[str, str]] = []
        self.assertEqual(
            inspect.signature(backup_drill.perform_restore_drill)
            .parameters["psql_binary"].default,
            "psql",
        )
        self.assertEqual(
            inspect.signature(backup_drill.perform_restore_drill)
            .parameters["pg_restore_binary"].default,
            "pg_restore",
        )
        with tempfile.TemporaryDirectory() as temporary_dir, mock.patch.object(
            backup_drill, "run_command", side_effect=responses
        ) as run_command, mock.patch.object(
            backup_drill, "run_restore_command", return_value=None
        ) as run_restore_command, mock.patch.object(
            backup_drill, "require_restore_headroom"
        ):
            table_count = backup_drill.perform_restore_drill(
                pathlib.Path(temporary_dir) / "backup.dump",
                app_target=target,
                admin_target=admin_target,
                timestamp_slug="20260720T120000Z",
                archive_stdin=archive,
                restore_as_postgres=True,
                restore_no_tablespaces=True,
                psql_binary="/validated/pg16/bin/psql",
                pg_restore_binary="/validated/pg16/bin/pg_restore",
                resource_guard=resource_guard,
                phase_observer=lambda phase, event: observed_phases.append((phase, event)),
            )

        self.assertEqual(table_count, 22)
        self.assertEqual(run_command.call_args_list[0].kwargs["target"], admin_target)
        self.assertIn("--owner", run_command.call_args_list[0].args[0])
        self.assertIn("platform_user", run_command.call_args_list[0].args[0])
        for index in (1, 2, 3, 4, 5, 6):
            self.assertEqual(run_command.call_args_list[index].kwargs["target"].username, "platform_user")
            self.assertEqual(
                run_command.call_args_list[index].args[0][0],
                "/validated/pg16/bin/psql",
            )
        self.assertEqual(run_command.call_args_list[7].kwargs["target"], admin_target)
        self.assertIn("postgres", run_command.call_args_list[7].args[0])
        self.assertTrue(run_command.call_args_list[1].kwargs["capture_output"])
        self.assertTrue(run_command.call_args_list[2].kwargs["capture_output"])
        self.assertIn("CREATE SCHEMA platform", run_command.call_args_list[2].args[0][-1])
        self.assertEqual(run_restore_command.call_count, 2)
        self.assertEqual(
            observed_phases,
            [
                ("restore_platform", "start"),
                ("restore_platform", "end"),
                ("restore_public", "start"),
                ("restore_public", "end"),
                ("alembic_verify", "start"),
                ("alembic_verify", "end"),
            ],
        )
        for index, schema in enumerate(("--schema=platform", "--schema=public")):
            command = run_restore_command.call_args_list[index].args[0]
            self.assertEqual(command[:4], ["runuser", "-u", "postgres", "--"])
            self.assertEqual(command[4], "/validated/pg16/bin/pg_restore")
            self.assertEqual(run_restore_command.call_args_list[index].kwargs["target"].username, "platform_user")
            self.assertIn(["--username", "platform_user"], [command[i : i + 2] for i in range(len(command) - 1)])
            self.assertIn(schema, command)
            self.assertIn("--no-tablespaces", command)
            self.assertNotIn("--single-transaction", command)
            self.assertNotIn(str(pathlib.Path(temporary_dir) / "backup.dump"), command)
            self.assertIs(run_restore_command.call_args_list[index].kwargs["stdin_handle"], archive)
            self.assertIs(run_restore_command.call_args_list[index].kwargs["resource_guard"], resource_guard)
        self.assertEqual(archive.tell(), 0)

        with mock.patch.object(backup_drill, "run_command") as role_command:
            backup_drill._scratch_create_application_role(
                admin_target, psql_binary="/validated/pg16/bin/psql"
            )
        self.assertEqual(role_command.call_args.kwargs["target"], admin_target)
        self.assertEqual(
            role_command.call_args.args[0][0], "/validated/pg16/bin/psql"
        )
        self.assertEqual(
            role_command.call_args.args[0][-1],
            "CREATE ROLE platform_user LOGIN;",
        )

        # The maintenance entrypoint must reject an unprivileged caller before
        # inspecting an archive or starting any host-level scratch operation.
        with (
            mock.patch.object(backup_drill.os, "geteuid", return_value=1000),
            mock.patch.object(backup_drill, "require_commands") as require_commands,
            mock.patch.object(backup_drill, "_scratch_host_guard") as host_guard,
        ):
            with self.assertRaisesRegex(
                RuntimeError, "requires the root maintenance owner"
            ):
                backup_drill._run_isolated_restore_drill(
                    pathlib.Path("/does-not-need-to-exist.dump"),
                    timestamp_slug="20261012T000000Z",
                )
        require_commands.assert_not_called()
        host_guard.assert_not_called()

    def test_isolated_restore_signal_cleanup_under_root(self) -> None:
        initdb_command = backup_drill._scratch_initdb_command(
            "/usr/lib/postgresql/16/bin/initdb",
            pathlib.Path("/run/oldsparky-backup-restore-deadbeef/pgdata"),
        )
        self.assertEqual(
            initdb_command,
            [
                "/usr/lib/postgresql/16/bin/initdb",
                "--pgdata",
                "/run/oldsparky-backup-restore-deadbeef/pgdata",
                "--username=postgres",
                "--no-locale",
                "--encoding=UTF8",
                "--set",
                "shared_buffers=16MB",
                "--auth-local=trust",
                "--auth-host=reject",
            ],
        )
        original_handlers = {
            signum: signal.getsignal(signum)
            for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
        }
        original_signal_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())

        def current_signal_mask() -> set[signal.Signals]:
            return signal.pthread_sigmask(signal.SIG_BLOCK, set())

        def run_cleanup_assertion(assertion) -> None:
            # Production wraps this entrypoint in the signal scope. These
            # helpers call it directly, so each must model that scope and
            # prove cleanup did not leak SIG_IGN into later child processes.
            with backup_drill._termination_signal_scope():
                assertion()
            self.assertEqual(
                {signum: signal.getsignal(signum) for signum in original_handlers},
                original_handlers,
            )
            self.assertEqual(current_signal_mask(), original_signal_mask)

        for assertion in (
            self._assert_parent_active_poll_sampler_failure_preserves_guard_and_cleanup,
            self._assert_low_space_abort_kills_owned_restore_and_drops_only_drill_db,
            self._assert_scratch_sigterm_stops_and_reaps_launched_unit,
            self._assert_scratch_worker_uses_pinned_archive_fd,
            self._assert_scratch_unit_diagnostic_caps_and_private_envelope,
            self._assert_scratch_early_exit_output_is_drained,
            self._assert_scratch_live_identity_failure_preserves_output,
            self._assert_scratch_identity_replacement_cannot_receive_authorization,
            self._assert_scratch_startup_inactive_race_is_handled,
            self._assert_synthetic_worker_failure_pair_reaches_parent,
            self._assert_scratch_final_identity_loss_blocks_done,
            self._assert_worker_token_deadline_delivery_is_truthful,
            self._assert_scratch_fast_launcher_cannot_skip_active_confinement,
            self._assert_scratch_absent_unit_has_bounded_startup_deadline,
            self._assert_scratch_after_confinement_failure_preserves_output,
        ):
            run_cleanup_assertion(assertion)
        self._assert_collected_unit_without_properties_preserves_output()
        self._assert_scratch_worker_forwards_output_only_when_private()
        self._assert_worker_authorization_barrier()
        self._assert_worker_token_raw_pipe_framing()
        self.assertEqual(current_signal_mask(), original_signal_mask)

        with tempfile.TemporaryDirectory() as temporary_dir:
            scratch_root = pathlib.Path(temporary_dir) / "scratch"
            scratch_root.mkdir(mode=0o700)
            archive_path = pathlib.Path(temporary_dir) / "platformdb.dump"
            archive_path.write_bytes(b"PGDMPsynthetic archive")
            command = backup_drill._scratch_unit_command(
                mountpoint=scratch_root,
                archive_path=archive_path,
                archive_stat=archive_path.stat(),
                archive_sha256=hashlib.sha256(archive_path.read_bytes()).hexdigest(),
                timestamp_slug="20261012T000000Z",
                unit_name="oldsparky-backup-restore-0123456789abcdef01234567.service",
            )
        self.assertIn("--property=MemoryMax=671088640", command)
        self.assertIn("--property=MemorySwapMax=0", command)
        self.assertIn("--collect", command)
        self.assertIn("--property=RestrictAddressFamilies=AF_UNIX", command)
        self.assertIn("--property=PrivateNetwork=yes", command)
        self.assertIn("--property=OOMPolicy=kill", command)
        self.assertIn("--property=TemporaryFileSystem=" + str(scratch_root) + ":rw,size=536870912,mode=0700", command)
        self.assertTrue(any(item.startswith("--property=BindReadOnlyPaths=") and str(archive_path) in item for item in command))
        separator = command.index("--")
        expected_python = pathlib.Path(
            getattr(backup_drill.sys, "_base_executable", backup_drill.sys.executable)
        ).resolve(strict=True)
        self.assertEqual(command[separator + 1], str(expected_python))
        self.assertTrue(any(
            item.startswith("--property=BindReadOnlyPaths=")
            and str(scratch_root / "platform_backup_restore_drill.py") in item
            for item in command
        ))
        self.assertFalse(any(
            item.startswith("--property=BindReadOnlyPaths=")
            and str(scratch_root / "python3") in item
            for item in command
        ))
        self.assertFalse(any("ReadWritePaths=" in item for item in command))
        self.assertFalse(any("PGPASSWORD" in item or "PLATFORM_DATABASE_URL" in item for item in command))

        setup_failure_mask = current_signal_mask()
        with (
            mock.patch.object(backup_drill, "require_commands"),
            mock.patch.object(backup_drill, "_scratch_host_guard"),
            mock.patch.object(
                backup_drill,
                "_scratch_prepare_synthetic_protected_paths",
                return_value=((), None),
            ),
            mock.patch.object(
                backup_drill.os,
                "mkdir",
                side_effect=OSError("injected mountpoint setup failure"),
            ),
            mock.patch.object(
                backup_drill,
                "_scratch_remove_created_protected_path_directories",
            ),
            mock.patch.object(
                backup_drill,
                "_scratch_cleanup_failure",
                side_effect=RuntimeError("injected cleanup report failure"),
            ),
            self.assertRaisesRegex(RuntimeError, "injected cleanup report failure"),
        ):
            backup_drill._run_isolated_restore_drill(
                None,
                timestamp_slug="20261018T120000Z",
                synthetic_restore_diagnostic=True,
                scratch_backend=backup_drill.SCRATCH_BACKEND_TMPFS_V1,
            )
        self.assertEqual(current_signal_mask(), setup_failure_mask)

    def _assert_scratch_worker_uses_pinned_archive_fd(self) -> None:
        self.assertEqual(backup_drill.os.geteuid(), 0)
        scratch_root = pathlib.Path(
            "/run/oldsparky-backup-restore-" + uuid.uuid4().hex[:24]
        )
        scratch_root.mkdir(mode=0o700)
        archive_path = scratch_root / "archive.dump"
        archive_bytes = b"PGDMPsynthetic worker archive"
        archive_path.write_bytes(archive_bytes)
        archive_path.chmod(0o600)
        archive_stat = archive_path.stat()
        args = argparse.Namespace(
            _scratch_root=str(scratch_root),
            _scratch_archive=str(archive_path),
            _scratch_archive_sha256=hashlib.sha256(archive_bytes).hexdigest(),
            _scratch_archive_dev=archive_stat.st_dev,
            _scratch_archive_ino=archive_stat.st_ino,
            _scratch_archive_size=archive_stat.st_size,
            _scratch_timestamp_slug="20261012T010203Z",
        )
        command_results = [
            subprocess.CompletedProcess(["pg_config"], 0, "/usr/lib/postgresql/16/bin\n", ""),
            subprocess.CompletedProcess(["postgres"], 0, "postgres (PostgreSQL) 16.10\n", ""),
        ]
        command_calls: list[tuple[list[str], dict[str, object]]] = []

        def fake_run_command(command, **kwargs):
            command_calls.append((command, kwargs))
            if command[:2] == ["pg_config", "--bindir"]:
                return command_results[0]
            if command[-1:] == ["--version"]:
                return command_results[1]
            return subprocess.CompletedProcess(command, 0, "", "")

        observed: dict[str, object] = {}
        stdout_buffer = io.BytesIO()
        stdout = types.SimpleNamespace(buffer=stdout_buffer)

        def fake_restore(dump_path, **kwargs):
            observed["dump_path"] = dump_path
            observed.update(kwargs)
            handle = kwargs["archive_stdin"]
            handle_stat = os.fstat(handle.fileno())
            observed["archive_identity"] = (handle_stat.st_dev, handle_stat.st_ino)
            observed["archive_offset"] = handle.tell()
            observed["archive_open_during_restore"] = not handle.closed
            observed["archive_mode"] = handle.mode
            return 7

        fake_postgres = types.SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
        try:
            with (
                mock.patch.object(backup_drill, "_scratch_mount_snapshot"),
                mock.patch.object(backup_drill, "_scratch_cgroup_snapshot"),
                mock.patch.object(backup_drill, "_scratch_mem_available_bytes", return_value=2**30),
                mock.patch.object(backup_drill, "_scratch_resource_guard"),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill.pwd, "getpwnam", return_value=fake_postgres),
                mock.patch.object(backup_drill.os, "chown"),
                mock.patch.object(backup_drill, "run_command", side_effect=fake_run_command),
                mock.patch.object(backup_drill, "_scratch_create_application_role") as create_role,
                mock.patch.object(backup_drill, "perform_restore_drill", side_effect=fake_restore),
                mock.patch.object(
                    backup_drill.sys,
                    "stdin",
                    types.SimpleNamespace(buffer=StagedWorkerInput()),
                ),
                mock.patch.object(backup_drill.sys, "stdout", stdout),
            ):
                self.assertEqual(backup_drill._scratch_worker_main(args), 0)

            self.assertEqual(
                json.loads(stdout_buffer.getvalue().decode("ascii")),
                {"ok": True, "restored_table_count": 7},
            )
            self.assertEqual(observed["dump_path"], scratch_root / "archive.dump")
            self.assertEqual(
                observed["archive_identity"],
                (archive_stat.st_dev, archive_stat.st_ino),
            )
            self.assertEqual(observed["archive_offset"], 0)
            self.assertTrue(observed["archive_open_during_restore"])
            self.assertEqual(observed["archive_mode"], "rb")
            self.assertEqual(observed["app_target"].username, backup_drill.SCRATCH_APP_ROLE)
            self.assertEqual(observed["admin_target"].username, "postgres")
            self.assertTrue(observed["restore_as_postgres"])
            self.assertTrue(observed["restore_no_tablespaces"])
            self.assertTrue(callable(observed["resource_guard"]))
            create_role.assert_called_once()
            self.assertEqual(create_role.call_args.args[0].username, "postgres")
            self.assertTrue(any(command[:2] == ["pg_config", "--bindir"] for command, _ in command_calls))
            initdb_call = next(
                command
                for command, _ in command_calls
                if "--pgdata" in command
                and any(pathlib.Path(token).name == "initdb" for token in command)
            )
            self.assertEqual(
                initdb_call[initdb_call.index("--set") + 1],
                "shared_buffers=16MB",
            )
            pgctl_start = next(
                command
                for command, _ in command_calls
                if command[-1:] == ["start"] and "--options" in command
            )
            actual_start_options = shlex.split(
                pgctl_start[pgctl_start.index("--options") + 1]
            )
            actual_start_settings = {
                actual_start_options[index + 1].split("=", 1)[0]: actual_start_options[index + 1].split("=", 1)[1]
                for index, argument in enumerate(actual_start_options[:-1])
                if argument == "-c"
            }
            self.assertEqual(
                actual_start_settings,
                {
                    "listen_addresses": "",
                    "unix_socket_directories": str(scratch_root / "socket"),
                    "port": str(backup_drill.SCRATCH_PG_PORT),
                    "fsync": "on",
                    "full_page_writes": "on",
                    "synchronous_commit": "on",
                    "dynamic_shared_memory_type": "mmap",
                    "shared_buffers": "16MB",
                    "work_mem": "4MB",
                    "maintenance_work_mem": "32MB",
                    "max_connections": "10",
                    "max_wal_size": "64MB",
                    "min_wal_size": "32MB",
                },
            )
            self.assertTrue(any(command[-1:] == ["stop"] for command, _ in command_calls))
            self.assertFalse((scratch_root / "data" / "postmaster.pid").exists())
        finally:
            shutil.rmtree(scratch_root)

    def _assert_scratch_sigterm_stops_and_reaps_launched_unit(self) -> None:
        class LauncherProcess:
            def __init__(self) -> None:
                self.returncode: int | None = None
                stdout_fd, stdout_write_fd = os.pipe()
                stderr_fd, stderr_write_fd = os.pipe()
                os.close(stdout_write_fd)
                os.close(stderr_write_fd)
                self.stdout = os.fdopen(stdout_fd, "rb", buffering=0)
                self.stderr = os.fdopen(stderr_fd, "rb", buffering=0)
                self.wait_calls = 0

            def close_streams(self) -> None:
                self.stdout.close()
                self.stderr.close()

            def poll(self) -> int | None:
                return self.returncode

            def wait(self, *, timeout: float | None = None) -> int:
                self.wait_calls += 1
                if self.returncode is None:
                    raise subprocess.TimeoutExpired(["systemd-run"], timeout)
                return self.returncode

            def kill(self) -> None:
                self.returncode = -signal.SIGKILL

        with tempfile.TemporaryDirectory() as temporary_dir:
            archive_path = pathlib.Path(temporary_dir) / "platformdb.dump"
            archive_path.write_bytes(b"PGDMPsynthetic archive")
            archive_path.chmod(0o600)
            launcher = LauncherProcess()
            prior_handlers = {
                signum: signal.getsignal(signum)
                for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
            }

            def launch(*_args, **_kwargs):
                # The implementation blocks these signals until it has stored
                # the Popen handle and marked the unique unit as possibly live.
                signal.raise_signal(signal.SIGTERM)
                return launcher

            unit_command = mock.Mock(return_value=["systemd-run"])

            def stop_unit(_unit_name: str) -> None:
                launcher.returncode = 0

            with (
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "_scratch_host_guard"),
                mock.patch.object(backup_drill, "_scratch_unit_command", unit_command),
                mock.patch.object(backup_drill, "_scratch_mountpoint_is_mounted", return_value=False),
                mock.patch.object(
                    backup_drill,
                    "_scratch_service_snapshot",
                    return_value={
                        "properties": {
                            "LoadState": "not-found",
                            "ActiveState": "inactive",
                            "ExecMainPID": "0",
                            "ControlGroup": "",
                        }
                    },
                ),
                mock.patch.object(backup_drill, "_scratch_stop_unit", side_effect=stop_unit) as stop,
                mock.patch.object(backup_drill.subprocess, "Popen", side_effect=launch) as popen,
            ):
                try:
                    with backup_drill._termination_signal_scope():
                        with self.assertRaisesRegex(backup_drill.BackupOperationInterrupted, "SIGTERM"):
                            backup_drill._run_isolated_restore_drill(
                                archive_path,
                                timestamp_slug="20261012T010203Z",
                            )
                finally:
                    launcher.close_streams()

            self.assertEqual(popen.call_count, 1)
            self.assertEqual(stop.call_count, 1)
            self.assertEqual(launcher.wait_calls, 1)
            mountpoint = unit_command.call_args.kwargs["mountpoint"]
            self.assertFalse(mountpoint.exists())
            self.assertEqual(
                {signum: signal.getsignal(signum) for signum in prior_handlers},
                prior_handlers,
            )

            # A stop failure must not bypass the independent launcher reap.
            failed_stop_launcher = LauncherProcess()

            def launch_then_term(*_args, **_kwargs):
                signal.raise_signal(signal.SIGHUP)
                return failed_stop_launcher

            with (
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "_scratch_host_guard"),
                mock.patch.object(backup_drill, "_scratch_unit_command", unit_command),
                mock.patch.object(backup_drill, "_scratch_mountpoint_is_mounted", return_value=False),
                mock.patch.object(
                    backup_drill,
                    "_scratch_service_snapshot",
                    return_value={
                        "properties": {
                            "LoadState": "loaded",
                            "ActiveState": "active",
                            "ExecMainPID": "123",
                            "InvocationID": "0123456789abcdef0123456789abcdef",
                            "ControlGroup": "/synthetic/scratch.service",
                        }
                    },
                ),
                mock.patch.object(
                    backup_drill,
                    "_scratch_stop_unit",
                    side_effect=RuntimeError("synthetic stop failure"),
                ),
                mock.patch.object(backup_drill.subprocess, "Popen", side_effect=launch_then_term),
            ):
                try:
                    with backup_drill._termination_signal_scope():
                        with self.assertRaisesRegex(
                            backup_drill.BackupOperationInterrupted, "SIGHUP"
                        ) as interrupted:
                            backup_drill._run_isolated_restore_drill(
                                archive_path,
                                timestamp_slug="20261012T010204Z",
                            )
                finally:
                    failed_stop_launcher.close_streams()
            self.assertEqual(failed_stop_launcher.wait_calls, 2)
            self.assertIsNotNone(failed_stop_launcher.returncode)
            self.assertTrue(
                any("RuntimeError" in note for note in interrupted.exception.__notes__)
            )
            self.assertEqual(
                {signum: signal.getsignal(signum) for signum in prior_handlers},
                prior_handlers,
            )
            failed_mountpoint = unit_command.call_args.kwargs["mountpoint"]
            self.assertTrue(failed_mountpoint.exists())
            mountpoint_stat = failed_mountpoint.lstat()
            self.assertEqual(mountpoint_stat.st_mode & 0o777, 0o700)
            self.assertEqual((mountpoint_stat.st_uid, mountpoint_stat.st_gid), (0, 0))
            self.assertEqual(list(failed_mountpoint.iterdir()), [])
            failed_mountpoint.rmdir()

    def _assert_low_space_abort_kills_owned_restore_and_drops_only_drill_db(self) -> None:
        target = backup_drill.DatabaseTarget(
            "127.0.0.1", 5432, "platform_user", "synthetic-secret", "platformdb"
        )
        total_bytes = 100 * 1024**3
        floor = backup_drill._required_disk_floor(total_bytes)
        self.assertEqual(floor, 15 * 1024**3)
        self.assertEqual(
            backup_drill._required_disk_floor(20 * 1024**3), 5 * 1024**3
        )
        self.assertEqual(
            backup_drill._required_disk_floor(40 * 1024**3 + 1),
            6 * 1024**3 + 1,
        )
        high = type(
            "Disk",
            (),
            {
                "valid": True,
                "total_bytes": total_bytes,
                "free_bytes": floor + backup_drill.RESTORE_DISK_LEAD_BYTES,
            },
        )()
        low = type(
            "Disk",
            (),
            {
                "valid": True,
                "total_bytes": total_bytes,
                "free_bytes": floor + backup_drill.RESTORE_DISK_LEAD_BYTES - 1,
            },
        )()

        class RestoreChild:
            def __init__(self) -> None:
                self.stopped = False
                self.wait_calls = 0
                self.wait_timeouts: list[float | None] = []
                self.terminated = False
                self.killed = False

            def poll(self) -> int | None:
                return -signal.SIGKILL if self.stopped else None

            def terminate(self) -> None:
                self.terminated = True

            def kill(self) -> None:
                self.killed = True
                self.stopped = True

            def wait(self, *, timeout: float | None = None) -> int:
                self.wait_calls += 1
                self.wait_timeouts.append(timeout)
                if self.wait_calls == 1:
                    raise subprocess.TimeoutExpired("pg_restore", timeout)
                self.stopped = True
                return -signal.SIGKILL

        child = RestoreChild()
        command_results = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "0\n", ""),
        ]
        with (
            tempfile.TemporaryDirectory() as temporary_dir,
            mock.patch.object(backup_drill, "snapshot_for_path", side_effect=(high, high, low)),
            mock.patch.object(backup_drill.subprocess, "Popen", return_value=child) as popen,
            mock.patch.object(backup_drill.time, "sleep"),
            mock.patch.object(
                backup_drill, "run_command", side_effect=command_results
            ) as run_command,
        ):
            with self.assertRaisesRegex(backup_drill.RestoreGuardStop, "low-disk safety guard") as failure:
                backup_drill.perform_restore_drill(
                    pathlib.Path(temporary_dir) / "backup.dump",
                    app_target=target,
                    admin_target=None,
                    timestamp_slug="20260720T120000Z",
                )

        popen.assert_called_once()
        self.assertIs(popen.call_args.kwargs["stdout"], backup_drill.subprocess.DEVNULL)
        self.assertIs(popen.call_args.kwargs["stderr"], backup_drill.subprocess.DEVNULL)
        self.assertTrue(child.terminated)
        self.assertTrue(child.killed)
        self.assertEqual(child.wait_calls, 2)
        self.assertEqual(child.wait_timeouts, [1.0, None])
        drop_command = run_command.call_args_list[-2].args[0]
        self.assertIn("dropdb", drop_command)
        self.assertEqual(
            drop_command[-1],
            f"platform_restore_drill_20260720t120000z_{os.getpid()}",
        )
        diagnostic = failure.exception.restore_diagnostic
        self.assertEqual(diagnostic["restore_stage"], "restore_platform")
        self.assertEqual(diagnostic["guard_reason"], "disk_floor")
        self.assertEqual(diagnostic["free_bytes"], low.free_bytes)
        self.assertEqual(
            diagnostic["required_free_bytes"],
            floor + backup_drill.RESTORE_DISK_LEAD_BYTES,
        )
        self.assertTrue(diagnostic["temporary_database_created"])
        self.assertEqual(diagnostic["drop_outcome"], "confirmed_absent")
        self.assertIs(diagnostic["temporary_database_absent"], True)

        before_create = backup_drill.RestoreGuardStop(
            "disk_floor", free_bytes=10, required_free_bytes=20
        )
        with (
            mock.patch.object(
                backup_drill, "require_restore_headroom", side_effect=before_create
            ),
            mock.patch.object(backup_drill, "run_command") as preflight_commands,
        ):
            with self.assertRaises(backup_drill.RestoreGuardStop) as preflight_failure:
                backup_drill.perform_restore_drill(
                    pathlib.Path("/unused.dump"),
                    app_target=target,
                    admin_target=None,
                    timestamp_slug="20260720T120000Z",
                )
        preflight_commands.assert_not_called()
        preflight_diagnostic = preflight_failure.exception.restore_diagnostic
        self.assertEqual(preflight_diagnostic["restore_stage"], "pre_create_admission")
        self.assertEqual(preflight_diagnostic["guard_reason"], "disk_floor")
        self.assertIs(preflight_diagnostic["temporary_database_created"], False)
        self.assertEqual(preflight_diagnostic["drop_outcome"], "not_required")
        self.assertIsNone(preflight_diagnostic["temporary_database_absent"])

        with (
            mock.patch.object(backup_drill, "require_restore_headroom"),
            mock.patch.object(
                backup_drill,
                "run_command",
                side_effect=subprocess.CalledProcessError(1, ["createdb"]),
            ) as create_command,
        ):
            with self.assertRaises(subprocess.CalledProcessError) as create_failure:
                backup_drill.perform_restore_drill(
                    pathlib.Path("/unused.dump"),
                    app_target=target,
                    admin_target=None,
                    timestamp_slug="20260720T120000Z",
                )
        create_command.assert_called_once()
        create_diagnostic = create_failure.exception.restore_diagnostic
        self.assertEqual(create_diagnostic["restore_stage"], "create_database")
        self.assertIsNone(create_diagnostic["temporary_database_created"])
        self.assertEqual(create_diagnostic["drop_outcome"], "not_required")
        self.assertIsNone(create_diagnostic["temporary_database_absent"])

        drop_failure_commands = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CalledProcessError(1, ["dropdb"]),
        ]
        with (
            mock.patch.object(backup_drill, "require_restore_headroom"),
            mock.patch.object(
                backup_drill,
                "run_command",
                side_effect=drop_failure_commands,
            ),
            mock.patch.object(
                backup_drill,
                "run_restore_command",
                side_effect=backup_drill.RestoreGuardStop(
                    "disk_floor", free_bytes=10, required_free_bytes=20
                ),
            ),
        ):
            with self.assertRaises(backup_drill.RestoreGuardStop) as drop_failure:
                backup_drill.perform_restore_drill(
                    pathlib.Path("/unused.dump"),
                    app_target=target,
                    admin_target=None,
                    timestamp_slug="20260720T120000Z",
                )
        drop_diagnostic = drop_failure.exception.restore_diagnostic
        self.assertEqual(drop_diagnostic["restore_stage"], "restore_platform")
        self.assertEqual(drop_diagnostic["guard_reason"], "disk_floor")
        self.assertEqual(drop_diagnostic["drop_outcome"], "drop_failed")
        self.assertIsNone(drop_diagnostic["temporary_database_absent"])

        serialized_error = backup_drill.RestoreGuardStop("disk_floor")
        serialized_diagnostic = {
            **diagnostic,
            "archive_sha256": "a" * 64,
            "archive_size_bytes": 120,
        }
        serialized_error.restore_diagnostic = serialized_diagnostic
        cli_args = argparse.Namespace(
            check_latest=False,
            verify_latest_existing=True,
            dump_only=False,
            verify_dump=None,
            output_dir="/private/backups",
            env_file="/private/env",
            max_age_hours=24.0,
            admin_database_url=None,
            as_json=True,
        )
        output = io.StringIO()
        with (
            mock.patch.object(backup_drill, "parse_args", return_value=cli_args),
            mock.patch.object(
                backup_drill,
                "verify_latest_existing_backup",
                side_effect=serialized_error,
            ),
            mock.patch("sys.stdout", output),
        ):
            self.assertEqual(backup_drill.main(), 1)
        serialized = json.loads(output.getvalue())
        self.assertEqual(serialized["restore_diagnostic"], serialized_diagnostic)
        self._assert_unavailable_disk_fails_before_child_start(target)
        self._assert_normal_guarded_child_completes(target, high)
        self._assert_low_space_abort_stops_only_owned_dump()
        self._assert_backup_admission_reports_private_disk_phase()

    def _assert_unavailable_disk_fails_before_child_start(
        self, target: backup_drill.DatabaseTarget
    ) -> None:
        invalid = type(
            "Disk", (), {"valid": False, "total_bytes": 0, "free_bytes": 0}
        )()
        with (
            mock.patch.object(backup_drill, "snapshot_for_path", return_value=invalid),
            mock.patch.object(backup_drill.subprocess, "Popen") as popen,
        ):
            with self.assertRaisesRegex(
                backup_drill.RestoreGuardStop, "disk space could not be verified"
            ):
                backup_drill.run_disk_guarded_command(
                    ["pg_dump", "--file", "/private/temp.dump"],
                    target=target,
                    stage="pg_dump",
                )
        popen.assert_not_called()

    def _assert_normal_guarded_child_completes(
        self, target: backup_drill.DatabaseTarget, disk_snapshot: object
    ) -> None:
        class CompletedChild:
            def __init__(self) -> None:
                self.poll_calls = 0

            def poll(self) -> int | None:
                self.poll_calls += 1
                return None if self.poll_calls == 1 else 0

            def wait(self, *, timeout: float | None = None) -> int:
                return 0

        child = CompletedChild()
        with (
            mock.patch.object(
                backup_drill,
                "snapshot_for_path",
                return_value=disk_snapshot,
            ),
            mock.patch.object(backup_drill.subprocess, "Popen", return_value=child) as popen,
            mock.patch.object(backup_drill.time, "sleep"),
        ):
            backup_drill.run_disk_guarded_command(
                ["pg_dump", "--file", "/private/temp.dump"],
                target=target,
                stage="pg_dump",
            )
        popen.assert_called_once()
        self.assertEqual(popen.call_args.kwargs["stdout"], backup_drill.subprocess.DEVNULL)
        self.assertEqual(popen.call_args.kwargs["stderr"], backup_drill.subprocess.DEVNULL)

    def _assert_low_space_abort_stops_only_owned_dump(self) -> None:
        target = backup_drill.DatabaseTarget(
            "127.0.0.1", 5432, "platform_user", "synthetic-secret", "platformdb"
        )
        total_bytes = 100 * 1024**3
        floor = backup_drill._required_disk_floor(total_bytes)
        high = type(
            "Disk",
            (),
            {
                "valid": True,
                "total_bytes": total_bytes,
                "free_bytes": floor + backup_drill.RESTORE_DISK_LEAD_BYTES,
            },
        )()
        low = type(
            "Disk",
            (),
            {
                "valid": True,
                "total_bytes": total_bytes,
                "free_bytes": floor + backup_drill.RESTORE_DISK_LEAD_BYTES - 1,
            },
        )()

        class DumpChild:
            def __init__(self) -> None:
                self.stopped = False
                self.terminated = False
                self.wait_calls = 0

            def poll(self) -> int | None:
                return 0 if self.stopped else None

            def terminate(self) -> None:
                self.terminated = True
                self.stopped = True

            def kill(self) -> None:
                raise AssertionError("graceful TERM should stop the fake pg_dump child")

            def wait(self, *, timeout: float | None = None) -> int:
                self.wait_calls += 1
                return 0

        child = DumpChild()
        with (
            mock.patch.object(backup_drill, "snapshot_for_path", side_effect=(high, high, low)),
            mock.patch.object(backup_drill.subprocess, "Popen", return_value=child) as popen,
            mock.patch.object(backup_drill.time, "sleep"),
        ):
            with self.assertRaisesRegex(
                backup_drill.RestoreGuardStop, "low-disk safety guard"
            ) as raised:
                backup_drill.run_disk_guarded_command(
                    ["pg_dump", "--file", "/private/temp.dump"],
                    target=target,
                    stage="pg_dump",
                )
        failure = raised.exception

        popen.assert_called_once()
        self.assertIs(popen.call_args.kwargs["stdout"], backup_drill.subprocess.DEVNULL)
        self.assertIs(popen.call_args.kwargs["stderr"], backup_drill.subprocess.DEVNULL)
        self.assertTrue(child.terminated)
        self.assertEqual(child.wait_calls, 1)
        self.assertEqual(
            failure.private_backup_disk_diagnostic(),
            {
                "schema": 1,
                "phase": "pg_dump_monitor",
                "guard_reason": "disk_floor",
                "free_bytes": low.free_bytes,
                "required_free_bytes": floor + backup_drill.RESTORE_DISK_LEAD_BYTES,
            },
        )

    def _assert_backup_admission_reports_private_disk_phase(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            old_dump, old_sidecar = self._write_backup_pair(
                output_dir, "platformdb-existing.dump", b"retained backup"
            )
            before = {
                path: (path.read_bytes(), path.lstat().st_ino, path.lstat().st_mode)
                for path in (old_dump, old_sidecar)
            }
            args = argparse.Namespace(
                env_file=str(output_dir / ".env.platform"),
                output_dir=str(output_dir),
                keep=1,
                admin_database_url=None,
                dump_only=True,
                rotate_existing=False,
            )
            guard = backup_drill.RestoreGuardStop(
                "disk_floor",
                free_bytes=100,
                required_free_bytes=200,
                phase="backup_admission",
            )
            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {"PLATFORM_DATABASE_URL": "postgresql://platform_user@localhost/platformdb"},
                ),
                mock.patch.object(backup_drill, "load_env", return_value={}),
                mock.patch.object(backup_drill, "require_restore_headroom", side_effect=guard),
                mock.patch.object(backup_drill, "require_commands") as require_commands,
                mock.patch.object(backup_drill.tempfile, "mkstemp") as mkstemp,
            ):
                with self.assertRaises(backup_drill.RestoreGuardStop) as raised:
                    backup_drill.create_backup(args)

            require_commands.assert_not_called()
            mkstemp.assert_not_called()
            self.assertEqual(
                raised.exception.private_backup_disk_diagnostic(),
                {
                    "schema": 1,
                    "phase": "backup_admission",
                    "guard_reason": "disk_floor",
                    "free_bytes": 100,
                    "required_free_bytes": 200,
                },
            )
            for path, expected in before.items():
                metadata = path.lstat()
                self.assertEqual(
                    (path.read_bytes(), metadata.st_ino, metadata.st_mode), expected
                )

    def test_check_latest_validates_restore_age_and_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            env_file = output_dir / ".env.platform"
            env_file.write_text(
                "PLATFORM_DATABASE_URL=postgresql://platform_user:synthetic@127.0.0.1/platformdb\n",
                encoding="utf-8",
            )
            dump_path = output_dir / "platformdb-20260714T120000Z.dump"
            dump_path.write_bytes(b"PGDMPcustom-format-backup")
            dump_path.chmod(0o600)
            metadata_path = dump_path.with_suffix(".json")
            original_created_at = dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
            metadata_path.write_text(
                json.dumps(
                    {
                        "format_version": 2,
                        "database": "platformdb",
                        "schemas": ["platform", "public"],
                        "required_extensions": ["pg_trgm"],
                        "dump_file": dump_path.name,
                        "size_bytes": dump_path.stat().st_size,
                        "sha256": hashlib.sha256(dump_path.read_bytes()).hexdigest(),
                        "completed_at_utc": original_created_at,
                        "restore_verified": False,
                        "alembic_revision_verified": False,
                        "restored_table_count": 31,
                        "restore_error": "historical closed diagnostic",
                    }
                ),
                encoding="utf-8",
            )
            metadata_path.chmod(0o600)

            original_dump_stat = dump_path.stat()
            with mock.patch.dict(
                backup_drill.os.environ,
                {"PLATFORM_DATABASE_URL": "postgresql://platform_user:synthetic@127.0.0.1/platformdb"},
            ), mock.patch.object(backup_drill, "require_commands"), mock.patch.object(
                backup_drill,
                "run_command",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ) as run_command, mock.patch.object(
                backup_drill, "_run_isolated_restore_drill", return_value=31
            ) as perform_restore:
                result = backup_drill.verify_latest_existing_backup(
                    output_dir, env_file, max_age_hours=24
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["restored_table_count"], 31)
            run_command.assert_not_called()
            perform_restore.assert_called_once()
            updated = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["completed_at_utc"], original_created_at)
            self.assertEqual(updated["restore_error"], "historical closed diagnostic")
            self.assertIs(updated["restore_verified"], True)
            self.assertIs(updated["alembic_revision_verified"], True)
            self.assertIsInstance(updated["restore_verified_at_utc"], str)
            self.assertEqual(
                backup_drill.check_latest_backup(output_dir, max_age_hours=24)["sha256"],
                hashlib.sha256(b"PGDMPcustom-format-backup").hexdigest(),
            )
            self.assertEqual(dump_path.read_bytes(), b"PGDMPcustom-format-backup")
            self.assertEqual(
                (dump_path.stat().st_ino, dump_path.stat().st_mtime_ns),
                (original_dump_stat.st_ino, original_dump_stat.st_mtime_ns),
            )

            updated_sidecar_bytes = metadata_path.read_bytes()
            with mock.patch.dict(
                backup_drill.os.environ,
                {"PLATFORM_DATABASE_URL": "postgresql://platform_user:synthetic@127.0.0.1/platformdb"},
            ), mock.patch.object(backup_drill, "require_commands"), mock.patch.object(
                backup_drill,
                "run_command",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ), mock.patch.object(
                backup_drill,
                "_run_isolated_restore_drill",
                side_effect=backup_drill.RestoreGuardStop("command_failed"),
            ):
                with self.assertRaises(backup_drill.RestoreGuardStop):
                    backup_drill.verify_latest_existing_backup(
                        output_dir, env_file, max_age_hours=24
                    )
            self.assertEqual(metadata_path.read_bytes(), updated_sidecar_bytes)
            self.assertEqual(dump_path.read_bytes(), b"PGDMPcustom-format-backup")

            with mock.patch.dict(
                backup_drill.os.environ,
                {"PLATFORM_DATABASE_URL": "postgresql://platform_user:synthetic@127.0.0.1/platformdb"},
            ), mock.patch.object(backup_drill, "require_commands"), mock.patch.object(
                backup_drill,
                "_run_isolated_restore_drill",
                side_effect=subprocess.CalledProcessError(1, ["systemd-run"]),
            ):
                with self.assertRaises(subprocess.CalledProcessError):
                    backup_drill.verify_latest_existing_backup(
                        output_dir, env_file, max_age_hours=24
                    )
            self.assertEqual(metadata_path.read_bytes(), updated_sidecar_bytes)
            self.assertEqual(dump_path.read_bytes(), b"PGDMPcustom-format-backup")

            crossing = dict(updated)
            start_now = dt.datetime.now(dt.UTC)
            crossing["completed_at_utc"] = (
                start_now - dt.timedelta(hours=23, minutes=59)
            ).isoformat().replace("+00:00", "Z")
            metadata_path.write_text(json.dumps(crossing), encoding="utf-8")
            metadata_path.chmod(0o600)
            crossing_bytes = metadata_path.read_bytes()
            with mock.patch.dict(
                backup_drill.os.environ,
                {"PLATFORM_DATABASE_URL": "postgresql://platform_user:synthetic@127.0.0.1/platformdb"},
            ), mock.patch.object(backup_drill, "require_commands"), mock.patch.object(
                backup_drill,
                "run_command",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ), mock.patch.object(
                backup_drill, "_run_isolated_restore_drill", return_value=31
            ), mock.patch.object(
                backup_drill,
                "utc_now",
                side_effect=[start_now, start_now, start_now + dt.timedelta(hours=2)],
            ):
                with self.assertRaisesRegex(RuntimeError, "creation-age window"):
                    backup_drill.verify_latest_existing_backup(
                        output_dir, env_file, max_age_hours=24
                    )
            self.assertEqual(metadata_path.read_bytes(), crossing_bytes)
            self.assertEqual(dump_path.read_bytes(), b"PGDMPcustom-format-backup")

            # A directory-fsync failure after rename must restore the exact old sidecar.
            stable_bytes = metadata_path.read_bytes()
            with mock.patch.dict(
                backup_drill.os.environ,
                {"PLATFORM_DATABASE_URL": "postgresql://platform_user:synthetic@127.0.0.1/platformdb"},
            ), mock.patch.object(backup_drill, "require_commands"), mock.patch.object(
                backup_drill,
                "run_command",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ), mock.patch.object(
                backup_drill, "_run_isolated_restore_drill", return_value=31
            ), mock.patch.object(
                backup_drill.os,
                "fsync",
                side_effect=[None, OSError("synthetic directory fsync failure"), None, None],
            ):
                with self.assertRaisesRegex(OSError, "synthetic directory fsync failure"):
                    backup_drill.verify_latest_existing_backup(
                        output_dir, env_file, max_age_hours=24
                    )
            self.assertEqual(metadata_path.read_bytes(), stable_bytes)
            self.assertEqual(dump_path.read_bytes(), b"PGDMPcustom-format-backup")

            def write_metadata(value: dict[str, object]) -> None:
                metadata_path.write_text(json.dumps(value), encoding="utf-8")
                metadata_path.chmod(0o600)

            def restore_hook(callback):
                with mock.patch.dict(
                    backup_drill.os.environ,
                    {
                        "PLATFORM_DATABASE_URL": (
                            "postgresql://platform_user:synthetic@127.0.0.1/platformdb"
                        )
                    },
                ), mock.patch.object(backup_drill, "require_commands"), mock.patch.object(
                    backup_drill,
                    "run_command",
                    return_value=subprocess.CompletedProcess([], 0, "", ""),
                ), mock.patch.object(
                backup_drill, "_run_isolated_restore_drill", side_effect=callback
                ):
                    return backup_drill.verify_latest_existing_backup(
                        output_dir, env_file, max_age_hours=24
                    )

            size_omitted_v1 = dict(updated)
            size_omitted_v1["format_version"] = 1
            size_omitted_v1.pop("size_bytes", None)
            write_metadata(size_omitted_v1)
            original_v1_bytes = metadata_path.read_bytes()
            failure = subprocess.CalledProcessError(1, ["createdb"])
            failure.restore_diagnostic = {
                "schema": 1,
                "restore_stage": "create_database",
                "guard_reason": "none",
                "free_bytes": None,
                "required_free_bytes": None,
                "temporary_database_created": None,
                "drop_outcome": "not_required",
                "temporary_database_absent": None,
            }
            with self.assertRaises(subprocess.CalledProcessError) as failed_v1:
                restore_hook(lambda *_args, **_kwargs: (_ for _ in ()).throw(failure))
            self.assertEqual(
                failed_v1.exception.restore_diagnostic["archive_size_bytes"],
                dump_path.stat().st_size,
            )
            self.assertEqual(
                failed_v1.exception.restore_diagnostic["archive_sha256"],
                hashlib.sha256(dump_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(metadata_path.read_bytes(), original_v1_bytes)

            baseline = dict(updated)
            baseline["completed_at_utc"] = original_created_at
            write_metadata(baseline)
            malformed = b'{"dump_file":"one","dump_file":"two"}'
            metadata_path.write_bytes(malformed)
            metadata_path.chmod(0o600)
            self._assert_latest_verification_preflight_rejects(
                output_dir, env_file, "malformed"
            )

            metadata_path.write_bytes(b" " * (1_048_577))
            metadata_path.chmod(0o600)
            self._assert_latest_verification_preflight_rejects(
                output_dir, env_file, "unsafe file metadata"
            )

            wrong_schema = dict(baseline)
            wrong_schema["schemas"] = ["platform", "sparkydb"]
            write_metadata(wrong_schema)
            self._assert_latest_verification_preflight_rejects(
                output_dir, env_file, "unsupported schema set"
            )

            naive_timestamp = dict(baseline)
            naive_timestamp["completed_at_utc"] = dt.datetime.now(dt.UTC).replace(
                tzinfo=None
            ).isoformat()
            write_metadata(naive_timestamp)
            self._assert_latest_verification_preflight_rejects(
                output_dir, env_file, "creation timestamp is invalid"
            )

            write_metadata(baseline)
            saved_metadata = output_dir / ".platformdb-metadata.saved"
            metadata_path.rename(saved_metadata)
            metadata_path.symlink_to(saved_metadata.name)
            self._assert_latest_verification_preflight_rejects(
                output_dir, env_file, "Too many levels|unsafe file metadata"
            )
            metadata_path.unlink()
            saved_metadata.rename(metadata_path)

            hardlink_path = output_dir / ".platformdb-metadata.hardlink"
            os.link(metadata_path, hardlink_path)
            self._assert_latest_verification_preflight_rejects(
                output_dir, env_file, "unsafe file metadata"
            )
            hardlink_path.unlink()

            dump_hardlink = output_dir / ".platformdb-dump.hardlink"
            os.link(dump_path, dump_hardlink)
            self._assert_latest_verification_preflight_rejects(
                output_dir, env_file, "unsafe file metadata"
            )
            dump_hardlink.unlink()

            metadata_path.chmod(0o640)
            self._assert_latest_verification_preflight_rejects(
                output_dir, env_file, "unsafe file metadata"
            )
            metadata_path.chmod(0o600)

            if os.geteuid() == 0:
                os.chown(metadata_path, 65534, metadata_path.stat().st_gid)
                self._assert_latest_verification_preflight_rejects(
                    output_dir, env_file, "unsafe file metadata"
                )
                os.chown(metadata_path, 0, 0)
                metadata_path.chmod(0o600)
                os.chown(metadata_path, 0, 65534)
                self._assert_latest_verification_preflight_rejects(
                    output_dir, env_file, "unsafe file metadata"
                )
                os.chown(metadata_path, 0, 0)

            original_archive_bytes = dump_path.read_bytes()
            original_archive_stat = dump_path.stat()

            def mutate_archive_during_restore(*_args, **_kwargs):
                dump_path.write_bytes(b"mutated same inode")
                os.utime(
                    dump_path,
                    ns=(original_archive_stat.st_atime_ns, original_archive_stat.st_mtime_ns),
                )
                return 31

            original_sidecar_bytes = metadata_path.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "checksum does not match"):
                restore_hook(mutate_archive_during_restore)
            self.assertEqual(metadata_path.read_bytes(), original_sidecar_bytes)
            self.assertEqual(dump_path.stat().st_ino, original_archive_stat.st_ino)
            dump_path.write_bytes(original_archive_bytes)
            dump_path.chmod(0o600)

            def replace_metadata_during_restore(*_args, **_kwargs):
                replacement = dict(baseline)
                replacement["restore_error"] = "external metadata replacement"
                replacement.pop("restore_verified_at_utc", None)
                replacement_path = output_dir / ".replacement.json"
                replacement_path.write_text(json.dumps(replacement), encoding="utf-8")
                replacement_path.chmod(0o600)
                os.replace(replacement_path, metadata_path)
                return 31

            with self.assertRaisesRegex(RuntimeError, "metadata changed during restore"):
                restore_hook(replace_metadata_during_restore)
            replacement_after = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(replacement_after["restore_error"], "external metadata replacement")
            self.assertNotIn("restore_verified_at_utc", replacement_after)

            write_metadata(baseline)

            def add_newer_pair_during_restore(*_args, **_kwargs):
                newer_dump = output_dir / "platformdb-newer.dump"
                newer_dump.write_bytes(b"newer archive")
                newer_dump.chmod(0o600)
                newer_metadata = newer_dump.with_suffix(".json")
                newer_metadata.write_text(
                    json.dumps(
                        {
                            "dump_file": newer_dump.name,
                            "sha256": hashlib.sha256(newer_dump.read_bytes()).hexdigest(),
                            "size_bytes": newer_dump.stat().st_size,
                            "format_version": 2,
                            "database": "platformdb",
                            "schemas": ["platform", "public"],
                            "required_extensions": ["pg_trgm"],
                            "completed_at_utc": original_created_at,
                            "restore_verified": False,
                            "alembic_revision_verified": False,
                        }
                    ),
                    encoding="utf-8",
                )
                newer_metadata.chmod(0o600)
                future_ns = metadata_path.stat().st_mtime_ns + 10_000_000
                os.utime(newer_metadata, ns=(future_ns, future_ns))
                return 31

            original_sidecar_bytes = metadata_path.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "metadata changed during restore"):
                restore_hook(add_newer_pair_during_restore)
            self.assertEqual(metadata_path.read_bytes(), original_sidecar_bytes)

            too_old = dict(updated)
            too_old["completed_at_utc"] = (
                dt.datetime.now(dt.UTC) - dt.timedelta(hours=25)
            ).isoformat().replace("+00:00", "Z")
            metadata_path.write_text(json.dumps(too_old), encoding="utf-8")
            metadata_path.chmod(0o600)
            rewritten_stat = metadata_path.stat()
            os.utime(
                metadata_path,
                ns=(rewritten_stat.st_atime_ns, rewritten_stat.st_mtime_ns + 1_000_000_000),
            )
            expired_bytes = metadata_path.read_bytes()
            with mock.patch.object(backup_drill, "require_commands") as require_commands:
                with self.assertRaisesRegex(RuntimeError, "creation-age window"):
                    backup_drill.verify_latest_existing_backup(
                        output_dir, env_file, max_age_hours=24
                    )
            require_commands.assert_not_called()
            self.assertEqual(metadata_path.read_bytes(), expired_bytes)

    def test_check_latest_cli_rejects_unverified_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            dump_path = output_dir / "platformdb-20260714T120000Z.dump"
            dump_path.write_bytes(b"PGDMPcustom-format-backup")
            dump_path.with_suffix(".json").write_text(
                json.dumps(
                    {
                        "dump_file": dump_path.name,
                        "sha256": hashlib.sha256(dump_path.read_bytes()).hexdigest(),
                        "completed_at_utc": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
                        "restore_verified": False,
                    }
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "--output-dir",
                    str(output_dir),
                    "--check-latest",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("not restore-verified", result.stderr)

    def test_prune_unverified_backups_keeps_verified_archive(self) -> None:
        with mock.patch.object(backup_drill.sys, "argv", ["platform_backup_restore_drill.py"]):
            defaults = backup_drill.parse_args()
            self.assertTrue(defaults.rotate_existing)
            self.assertIsNone(defaults.protected_backup_manifest)
        with mock.patch.object(
            backup_drill.sys,
            "argv",
            ["platform_backup_restore_drill.py", "--preserve-existing"],
        ):
            self.assertFalse(backup_drill.parse_args().rotate_existing)
        with (
            mock.patch.object(
                backup_drill.sys,
                "argv",
                [
                    "platform_backup_restore_drill.py",
                    "--protected-backup-manifest",
                    "/private/manifest.json",
                    "--check-latest",
                ],
            ),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            backup_drill.parse_args()

        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            verified_dump = output_dir / "platformdb-20200101T000000Z.dump"
            failed_dump = output_dir / "platformdb-20200102T000000Z.dump"
            verified_dump.write_bytes(b"verified")
            failed_dump.write_bytes(b"failed")
            verified_metadata = verified_dump.with_suffix(".json")
            failed_metadata = failed_dump.with_suffix(".json")
            verified_metadata.write_text(
                json.dumps({"dump_file": verified_dump.name, "restore_verified": True}),
                encoding="utf-8",
            )
            failed_metadata.write_text(
                json.dumps({"dump_file": failed_dump.name, "restore_verified": False}),
                encoding="utf-8",
            )

            removed = backup_drill.prune_unverified_backups(
                output_dir,
                preserve_metadata=verified_metadata,
            )

            self.assertTrue(verified_dump.exists())
            self.assertTrue(verified_metadata.exists())
            self.assertFalse(failed_dump.exists())
            self.assertFalse(failed_metadata.exists())
            self.assertEqual(len(removed), 2)

    def test_unverified_prune_rejects_untrusted_dump_paths_before_any_unlink(self) -> None:
        unsafe_names = (
            "../sentinel.dump",
            "{absolute}",
            "platformdb-20200101T000000Z.dump",
        )
        for unsafe_name in unsafe_names:
            with self.subTest(unsafe_name=unsafe_name), tempfile.TemporaryDirectory() as temporary_dir:
                root = pathlib.Path(temporary_dir)
                output_dir = root / "backups"
                output_dir.mkdir()
                sentinel = root / "sentinel.dump"
                sentinel.write_bytes(b"outside sentinel")
                unrelated_dump, unrelated_metadata = self._write_backup_pair(
                    output_dir,
                    "platformdb-20200101T000000Z.dump",
                    b"unrelated backup",
                    restore_verified=False,
                )
                candidate_dump, candidate_metadata = self._write_backup_pair(
                    output_dir,
                    "platformdb-20200102T000000Z.dump",
                    b"unverified candidate",
                    restore_verified=False,
                )
                dump_value = str(sentinel) if unsafe_name == "{absolute}" else unsafe_name
                candidate_document = json.loads(candidate_metadata.read_text(encoding="utf-8"))
                candidate_document["dump_file"] = dump_value
                candidate_metadata.write_text(json.dumps(candidate_document), encoding="utf-8")
                before_sentinel = sentinel.read_bytes()

                with self.assertRaisesRegex(RuntimeError, "mismatched dump name"):
                    backup_drill.prune_unverified_backups(
                        output_dir,
                        preserve_metadata=output_dir / "not-current.json",
                    )

                self.assertEqual(sentinel.read_bytes(), before_sentinel)
                self.assertTrue(unrelated_dump.exists())
                self.assertTrue(unrelated_metadata.exists())
                self.assertTrue(candidate_dump.exists())
                self.assertTrue(candidate_metadata.exists())

    def test_named_protection_is_union_with_latest_fifteen_verified_and_preserves_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            verified_names: list[str] = []
            for index in range(18):
                dump_name = f"platformdb-202001{index + 1:02d}T000000Z.dump"
                dump_path, _metadata_path = self._write_backup_pair(
                    output_dir, dump_name, f"verified-{index}".encode()
                )
                timestamp = 1_600_000_000 + index
                os.utime(dump_path, (timestamp, timestamp))
                os.utime(dump_path.with_suffix(".json"), (timestamp, timestamp))
                verified_names.append(dump_name)
            unverified_name = "platformdb-20200201T000000Z.dump"
            self._write_backup_pair(
                output_dir, unverified_name, b"protected-unverified", restore_verified=False
            )
            unknown_name = "platformdb-20200202T000000Z.dump"
            unknown_dump = output_dir / unknown_name
            unknown_dump.write_bytes(b"protected-unknown")
            unknown_dump.with_suffix(".json").write_text(
                json.dumps(
                    {
                        "dump_file": unknown_name,
                        "size_bytes": unknown_dump.stat().st_size,
                        "sha256": hashlib.sha256(unknown_dump.read_bytes()).hexdigest(),
                    }
                ),
                encoding="utf-8",
            )
            protected = frozenset({verified_names[0], unverified_name, unknown_name})

            removed_unverified = backup_drill.prune_unverified_backups(
                output_dir,
                preserve_metadata=output_dir / "not-current.json",
                protected_dump_files=protected,
            )
            removed_verified = backup_drill.prune_backups(
                output_dir, keep=15, protected_dump_files=protected, verified_only=True
            )

            retained_verified = {path.name for path in output_dir.glob("*.dump")} & set(verified_names)
            self.assertEqual(retained_verified, set(verified_names[-15:]) | {verified_names[0]})
            self.assertTrue((output_dir / unverified_name).exists())
            self.assertTrue((output_dir / unverified_name.replace(".dump", ".json")).exists())
            self.assertTrue(unknown_dump.exists())
            self.assertTrue(unknown_dump.with_suffix(".json").exists())
            self.assertEqual(len(removed_unverified), 0)
            self.assertEqual(len(removed_verified), 4)

    def test_protected_rotation_emits_ordered_identity_proof_and_rejects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = pathlib.Path(temporary_dir)
            output_dir = root / "backups"
            output_dir.mkdir()
            verified_names: list[str] = []
            for index in range(17):
                name = f"platformdb-202001{index + 1:02d}T000000Z.dump"
                dump_path, sidecar_path = self._write_backup_pair(
                    output_dir, name, f"verified-{index}".encode()
                )
                verified_names.append(name)
                # The first four establish a sub-float mtime ordering and a
                # deterministic equal-nanosecond basename tie at the retention edge.
                base_ns = 1_700_000_000_000_000_000
                timestamp_ns = (
                    base_ns - 1_000_000_000
                    if index == 0
                    else base_ns + 10
                    if index in {1, 3}
                    else base_ns
                    if index == 2
                    else base_ns + index * 1_000_000_000
                )
                os.utime(dump_path, ns=(timestamp_ns, timestamp_ns))
                os.utime(sidecar_path, ns=(timestamp_ns, timestamp_ns))

            unverified_name = "platformdb-20200201T000000Z.dump"
            self._write_backup_pair(
                output_dir,
                unverified_name,
                b"protected-unverified",
                restore_verified=False,
            )
            unknown_name = "platformdb-20200202T000000Z.dump"
            unknown_dump, unknown_sidecar = self._write_backup_pair(
                output_dir,
                unknown_name,
                b"protected-unknown",
                restore_verified=False,
            )
            unknown_metadata = json.loads(unknown_sidecar.read_text(encoding="utf-8"))
            unknown_metadata.pop("restore_verified")
            unknown_sidecar.write_text(json.dumps(unknown_metadata), encoding="utf-8")
            unverified_removed_name = "platformdb-20200203T000000Z.dump"
            self._write_backup_pair(
                output_dir,
                unverified_removed_name,
                b"unverified-removal",
                restore_verified=False,
            )
            unknown_removed_name = "platformdb-20200204T000000Z.dump"
            _unknown_removed_dump, unknown_removed_sidecar = self._write_backup_pair(
                output_dir,
                unknown_removed_name,
                b"unknown-removal",
                restore_verified=False,
            )
            unknown_removed_metadata = json.loads(
                unknown_removed_sidecar.read_text(encoding="utf-8")
            )
            unknown_removed_metadata.pop("restore_verified")
            unknown_removed_sidecar.write_text(
                json.dumps(unknown_removed_metadata), encoding="utf-8"
            )
            truthy_unknown_name = "platformdb-20200205T000000Z.dump"
            _truthy_unknown_dump, truthy_unknown_sidecar = self._write_backup_pair(
                output_dir,
                truthy_unknown_name,
                b"truthy-unknown",
                restore_verified=False,
            )
            truthy_unknown_metadata = json.loads(
                truthy_unknown_sidecar.read_text(encoding="utf-8")
            )
            truthy_unknown_metadata["restore_verified"] = "false"
            truthy_unknown_sidecar.write_text(
                json.dumps(truthy_unknown_metadata), encoding="utf-8"
            )
            protected_names = [verified_names[0], unverified_name, unknown_name]
            manifest = self._write_protected_backup_manifest(
                root / "protected.json", protected_names
            )
            dump_body_inventory_reads: list[pathlib.Path] = []
            real_inventory_file = backup_drill._inventory_file

            def record_inventory_file(path: pathlib.Path):
                if path.suffix == ".dump":
                    dump_body_inventory_reads.append(path)
                return real_inventory_file(path)

            with mock.patch.object(
                backup_drill, "_inventory_file", side_effect=record_inventory_file
            ):
                result = self._create_protected_backup(
                    output_dir, manifest, keep=5
                )

            proof = result["protected_retention_proof"]
            self.assertEqual(result["rotation_mode"], "rotate-existing")
            self.assertEqual(proof["requested_keep"], 5)
            self.assertEqual(proof["effective_verified_keep"], 15)
            selection = proof["verified_selection"]
            self.assertEqual(selection["available_count"], 18)
            self.assertEqual(selection["retained_count"], 15)
            self.assertTrue(
                backup_drill.valid_protected_retention_proof(
                    proof,
                    requested_keep=5,
                    new_dump_file=result["dump_file"],
                )
            )
            self.assertEqual(
                [row["dump_file"] for row in selection["candidate_order"][:4]],
                [verified_names[0], verified_names[2], verified_names[1], verified_names[3]],
            )
            self.assertEqual(
                [row["dump_file"] for row in selection["selected_order"]],
                [row["dump_file"] for row in selection["candidate_order"][-15:]],
            )
            self.assertIn(result["dump_file"], {row["dump_file"] for row in selection["selected_order"]})
            self.assertEqual(selection["selected_order"], selection["retained_after"])
            self.assertEqual(
                proof["protected_pairs"]["before"], proof["protected_pairs"]["after"]
            )
            protected_status = {
                row["dump_file"]: row["restore_verified"]
                for row in proof["protected_pairs"]["after"]
            }
            self.assertEqual(protected_status[unverified_name], "false")
            self.assertEqual(protected_status[unknown_name], "unknown")
            self.assertTrue(unknown_dump.exists())
            self.assertNotIn("restore_verified", json.loads(unknown_sidecar.read_text()))
            self.assertTrue((output_dir / truthy_unknown_name).exists())
            self.assertTrue(truthy_unknown_sidecar.exists())
            self.assertEqual(
                {row["dump_file"] for row in proof["removed_pairs"]},
                {
                    verified_names[1],
                    verified_names[2],
                    unverified_removed_name,
                    unknown_removed_name,
                },
            )
            removed_by_name = {
                row["dump_file"]: row for row in proof["removed_pairs"]
            }
            self.assertEqual(
                removed_by_name[unverified_removed_name]["restore_status_class"],
                "false",
            )
            self.assertEqual(
                removed_by_name[unknown_removed_name]["restore_status_class"],
                "missing",
            )
            self.assertEqual(
                removed_by_name[unverified_removed_name]["reason"], "unverified"
            )
            self.assertEqual(
                removed_by_name[verified_names[1]]["reason"], "verified-retention"
            )
            self.assertEqual(
                proof["removed_pairs_digest"],
                backup_drill._retention_domain_digest(
                    "backup-retention-removals", proof["removed_pairs"]
                ),
            )
            self.assertEqual(proof["protected_removed_count"], 0)
            self.assertEqual(dump_body_inventory_reads, [])

            for mutation in (
                "reordered",
                "digest",
                "count",
                "eligibility",
                "malformed_reason",
            ):
                with self.subTest(mutation=mutation):
                    tampered = json.loads(json.dumps(proof))
                    if mutation == "reordered":
                        tampered["verified_selection"]["selected_order"].reverse()
                    elif mutation == "digest":
                        tampered["verified_selection"]["selected_after_digest"] = "0" * 64
                    elif mutation == "count":
                        tampered["verified_selection"]["available_count"] += 1
                    elif mutation == "eligibility":
                        unverified_row = next(
                            row
                            for row in tampered["removed_pairs"]
                            if row["reason"] == "unverified"
                        )
                        unverified_row["restore_status_class"] = "truthy_or_unsupported"
                        tampered["removed_pairs_digest"] = backup_drill._retention_domain_digest(
                            "backup-retention-removals", tampered["removed_pairs"]
                        )
                    else:
                        unverified_row = next(
                            row
                            for row in tampered["removed_pairs"]
                            if row["reason"] == "unverified"
                        )
                        unverified_row["reason"] = []
                        tampered["removed_pairs_digest"] = backup_drill._retention_domain_digest(
                            "backup-retention-removals", tampered["removed_pairs"]
                        )
                    self.assertFalse(
                        backup_drill.valid_protected_retention_proof(
                            tampered,
                            requested_keep=5,
                            new_dump_file=result["dump_file"],
                        )
                    )

    def test_protected_rotation_keeps_fewer_than_the_effective_minimum(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = pathlib.Path(temporary_dir)
            output_dir = root / "backups"
            output_dir.mkdir()
            names = []
            for index in range(2):
                name = f"platformdb-2020010{index + 1}T000000Z.dump"
                dump, sidecar = self._write_backup_pair(output_dir, name, b"verified")
                old_ns = 1_600_000_000_000_000_000 + index * 1_000_000_000
                os.utime(dump, ns=(old_ns, old_ns))
                os.utime(sidecar, ns=(old_ns, old_ns))
                names.append(name)
            manifest = self._write_protected_backup_manifest(
                root / "protected.json", [names[0]]
            )

            result = self._create_protected_backup(output_dir, manifest, keep=4)
            selection = result["protected_retention_proof"]["verified_selection"]

            self.assertEqual(selection["available_count"], 3)
            self.assertEqual(selection["retained_count"], 3)
            self.assertEqual(
                {row["dump_file"] for row in selection["selected_order"]},
                set(names) | {result["dump_file"]},
            )
            self.assertEqual(result["removed"], [])

    def test_protected_manifest_or_pair_drift_stops_before_pruning(self) -> None:
        for drift in ("manifest", "pair"):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as temporary_dir:
                root = pathlib.Path(temporary_dir)
                output_dir = root / "backups"
                output_dir.mkdir()
                protected_name = "platformdb-20200101T000000Z.dump"
                _dump, sidecar = self._write_backup_pair(
                    output_dir, protected_name, b"protected"
                )
                manifest = self._write_protected_backup_manifest(
                    root / "protected.json", [protected_name]
                )

                def mutate_after_restore(*_args: object, **_kwargs: object) -> int:
                    if drift == "manifest":
                        manifest.write_text(
                            json.dumps(
                                {"schema_version": 1, "protected_dump_files": [protected_name]},
                                indent=1,
                            ),
                            encoding="utf-8",
                        )
                        manifest.chmod(0o600)
                    else:
                        metadata = json.loads(sidecar.read_text(encoding="utf-8"))
                        metadata["sha256"] = "a" * 64
                        sidecar.write_text(json.dumps(metadata), encoding="utf-8")
                    return 12

                prune_unverified = mock.Mock()
                prune_verified = mock.Mock()
                args = argparse.Namespace(
                    env_file=root / ".env.platform",
                    output_dir=output_dir,
                    keep=14,
                    dump_only=False,
                    rotate_existing=True,
                    protected_backup_manifest=manifest,
                    private_scratch_diagnostics=False,
                )
                with self._project_manifest_owner(manifest, 0):
                    with (
                        mock.patch.dict(
                            backup_drill.os.environ,
                            {"PLATFORM_DATABASE_URL": "postgresql://platform_user@localhost/platformdb"},
                        ),
                        mock.patch.object(backup_drill, "load_env", return_value={}),
                        mock.patch.object(backup_drill, "require_commands"),
                        mock.patch.object(
                            backup_drill,
                            "run_disk_guarded_command",
                            side_effect=self._fake_guarded_dump,
                        ),
                        mock.patch.object(backup_drill, "require_restore_headroom"),
                        mock.patch.object(
                            backup_drill,
                            "_run_isolated_restore_drill",
                            side_effect=mutate_after_restore,
                        ),
                        mock.patch.object(
                            backup_drill,
                            "prune_unverified_backups",
                            prune_unverified,
                        ),
                        mock.patch.object(
                            backup_drill, "prune_backups", prune_verified
                        ),
                    ):
                        with self.assertRaisesRegex(RuntimeError, "changed during restore verification"):
                            backup_drill.create_backup(args)
                prune_unverified.assert_not_called()
                prune_verified.assert_not_called()

        for prune_kind in ("unverified", "verified"):
            with (
                self.subTest(prune_kind=prune_kind),
                tempfile.TemporaryDirectory() as temporary_dir,
            ):
                output_dir = pathlib.Path(temporary_dir) / "backups"
                output_dir.mkdir()
                preserve_metadata = output_dir / "current-backup.json"
                if prune_kind == "unverified":
                    target_name = "platformdb-20200101T000000Z.dump"
                    target_dump, target_sidecar = self._write_backup_pair(
                        output_dir,
                        target_name,
                        b"unverified-to-prune",
                        restore_verified=False,
                    )
                    expected_unverified = backup_drill._unverified_backup_candidate_order(
                        output_dir,
                        preserve_metadata=preserve_metadata,
                        protected_dump_files=frozenset(),
                    )
                    self.assertEqual(
                        [row["pair"]["dump_file"] for row in expected_unverified],
                        [target_name],
                    )
                    expected_verified: tuple[dict[str, Any], ...] = ()
                else:
                    target_name = "platformdb-20200101T000000Z.dump"
                    newer_name = "platformdb-20200102T000000Z.dump"
                    target_dump, target_sidecar = self._write_backup_pair(
                        output_dir, target_name, b"old-verified"
                    )
                    newer_dump, newer_sidecar = self._write_backup_pair(
                        output_dir, newer_name, b"new-verified"
                    )
                    old_ns = 1_600_000_000_000_000_000
                    new_ns = old_ns + 1_000_000_000
                    os.utime(target_dump, ns=(old_ns, old_ns))
                    os.utime(target_sidecar, ns=(old_ns, old_ns))
                    os.utime(newer_dump, ns=(new_ns, new_ns))
                    os.utime(newer_sidecar, ns=(new_ns, new_ns))
                    expected_verified = backup_drill._verified_backup_candidate_order(
                        output_dir
                    )
                    self.assertEqual(expected_verified[0]["dump_file"], target_name)
                    expected_unverified = ()

                real_pair_row = backup_drill._backup_retention_pair_row
                pair_row_checks = 0

                def mutate_sidecar_after_pair_check(
                    candidate_dir: pathlib.Path, dump_name: str
                ) -> dict[str, Any]:
                    nonlocal pair_row_checks
                    row = real_pair_row(candidate_dir, dump_name)
                    if dump_name == target_name:
                        pair_row_checks += 1
                        if pair_row_checks == 2:
                            metadata = json.loads(target_sidecar.read_text(encoding="utf-8"))
                            metadata["sha256"] = "b" * 64
                            target_sidecar.write_text(json.dumps(metadata), encoding="utf-8")
                    return row

                with mock.patch.object(
                    backup_drill,
                    "_backup_retention_pair_row",
                    side_effect=mutate_sidecar_after_pair_check,
                ):
                    with self.assertRaisesRegex(RuntimeError, "sidecar changed before retention"):
                        if prune_kind == "unverified":
                            backup_drill.prune_unverified_backups(
                                output_dir,
                                preserve_metadata=preserve_metadata,
                                expected_candidate_order=expected_unverified,
                            )
                        else:
                            backup_drill.prune_backups(
                                output_dir,
                                keep=1,
                                verified_only=True,
                                expected_verified_order=expected_verified,
                            )

                self.assertEqual(pair_row_checks, 2)
                self.assertTrue(target_dump.exists())
                self.assertTrue(target_sidecar.exists())

    def test_unconfigured_pruning_keeps_historical_keep_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            old = output_dir / "platformdb-old.dump"
            middle = output_dir / "platformdb-middle.dump"
            newest = output_dir / "platformdb-newest.dump"
            for index, path in enumerate((old, middle, newest)):
                path.write_bytes(b"archive")
                os.utime(path, (1_600_000_000 + index, 1_600_000_000 + index))
            removed = backup_drill.prune_backups(output_dir, keep=2)
            self.assertEqual(removed, [str(old)])
            self.assertFalse(old.exists())
            self.assertTrue(middle.exists())
            self.assertTrue(newest.exists())

    def test_invalid_or_incomplete_named_manifest_fails_before_database_or_pruning(self) -> None:
        valid_name = "platformdb-20200101T000000Z.dump"
        invalid_documents = (
            {"schema_version": 1, "protected_dump_files": [valid_name, valid_name]},
            {"schema_version": 1, "protected_dump_files": ["../outside.dump"]},
        )
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = pathlib.Path(temporary_dir)
            output_dir = root / "backups"
            output_dir.mkdir()
            manifest = root / "protected.json"
            for document in invalid_documents:
                with self.subTest(document=document):
                    manifest.write_text(json.dumps(document), encoding="utf-8")
                    manifest.chmod(0o600)
                    with self._project_manifest_owner(manifest, 0):
                        with mock.patch.object(
                            backup_drill, "load_env", side_effect=AssertionError("database path reached")
                        ), mock.patch.object(backup_drill, "prune_backups") as prune:
                            with self.assertRaises(RuntimeError):
                                backup_drill.create_backup(
                                    argparse.Namespace(
                                        env_file=root / ".env",
                                        output_dir=output_dir,
                                        keep=14,
                                        dump_only=True,
                                        rotate_existing=True,
                                        protected_backup_manifest=manifest,
                                    )
                                )
                    prune.assert_not_called()

            self._write_protected_backup_manifest(manifest, [valid_name])
            with self._project_manifest_owner(manifest, 0):
                with mock.patch.object(
                    backup_drill, "load_env", side_effect=AssertionError("database path reached")
                ) as load_env, mock.patch.object(backup_drill, "prune_backups") as prune:
                    with self.assertRaisesRegex(RuntimeError, "missing or unsafe"):
                        backup_drill.create_backup(
                            argparse.Namespace(
                                env_file=root / ".env",
                                output_dir=output_dir,
                                keep=14,
                                dump_only=True,
                                rotate_existing=True,
                                protected_backup_manifest=manifest,
                            )
                        )
            load_env.assert_not_called()
            prune.assert_not_called()

            dump_path, metadata_path = self._write_backup_pair(
                output_dir, valid_name, b"protected"
            )
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["size_bytes"] += 1
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            with self._project_manifest_owner(manifest, 0):
                with mock.patch.object(
                    backup_drill, "load_env", side_effect=AssertionError("database path reached")
                ) as load_env:
                    with self.assertRaisesRegex(RuntimeError, "incomplete or mismatched"):
                        backup_drill.create_backup(
                            argparse.Namespace(
                                env_file=root / ".env",
                                output_dir=output_dir,
                                keep=14,
                                dump_only=True,
                                rotate_existing=True,
                                protected_backup_manifest=manifest,
                            )
                        )
            load_env.assert_not_called()
            self.assertTrue(dump_path.exists())

    def test_named_manifest_rejects_non_private_or_symlink_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = pathlib.Path(temporary_dir)
            manifest = self._write_protected_backup_manifest(
                root / "manifest.json", ["platformdb-20200101T000000Z.dump"]
            )
            with self.assertRaisesRegex(RuntimeError, "missing or unsafe"):
                backup_drill._load_protected_backup_manifest(root / "absent.json")
            manifest.chmod(0o640)
            with self._project_manifest_owner(manifest, 0):
                with self.assertRaisesRegex(RuntimeError, "unsafe metadata"):
                    backup_drill._load_protected_backup_manifest(manifest)
            manifest.chmod(0o600)
            with self._project_manifest_owner(manifest, os.geteuid() + 1):
                with self.assertRaisesRegex(RuntimeError, "unsafe metadata"):
                    backup_drill._load_protected_backup_manifest(manifest)
            link = root / "linked.json"
            link.symlink_to(manifest)
            with self.assertRaisesRegex(RuntimeError, "missing or unsafe"):
                backup_drill._load_protected_backup_manifest(link)
            hardlink = root / "hardlinked.json"
            os.link(manifest, hardlink)
            with self._project_manifest_owner(manifest, 0):
                with self.assertRaisesRegex(RuntimeError, "unsafe metadata"):
                    backup_drill._load_protected_backup_manifest(manifest)

    def test_restore_failure_with_named_protection_does_not_prune(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = pathlib.Path(temporary_dir)
            output_dir = root / "backups"
            output_dir.mkdir()
            old_dump, old_metadata = self._write_backup_pair(
                output_dir, "platformdb-20200101T000000Z.dump", b"retained"
            )
            manifest = self._write_protected_backup_manifest(
                root / "protected.json", [old_dump.name]
            )

            def write_dump(command: list[str], **_kwargs: object) -> None:
                dump_path = pathlib.Path(command[command.index("--file") + 1])
                dump_path.write_bytes(b"new dump bytes")

            args = argparse.Namespace(
                env_file=root / ".env",
                output_dir=output_dir,
                keep=14,
                dump_only=False,
                rotate_existing=True,
                protected_backup_manifest=manifest,
                private_scratch_diagnostics=False,
            )
            with self._project_manifest_owner(manifest, 0):
                with (
                    mock.patch.object(
                        backup_drill,
                        "load_env",
                        return_value={
                            "PLATFORM_DATABASE_URL": (
                                "postgresql://u:p@127.0.0.1:5432/platformdb"
                            )
                        },
                    ),
                    mock.patch.dict(
                        backup_drill.os.environ,
                        {
                            "PLATFORM_DATABASE_URL": (
                                "postgresql://u:p@127.0.0.1:5432/platformdb"
                            )
                        },
                    ),
                    mock.patch.object(backup_drill, "require_restore_headroom"),
                    mock.patch.object(backup_drill, "require_commands"),
                    mock.patch.object(backup_drill, "run_disk_guarded_command", side_effect=write_dump),
                    mock.patch.object(
                        backup_drill,
                        "_run_isolated_restore_drill",
                        side_effect=RuntimeError("synthetic restore failure"),
                    ),
                    mock.patch.object(backup_drill, "prune_unverified_backups") as prune_unverified,
                    mock.patch.object(backup_drill, "prune_backups") as prune_verified,
                ):
                    with self.assertRaisesRegex(RuntimeError, "synthetic restore failure"):
                        backup_drill.create_backup(args)

            prune_unverified.assert_not_called()
            prune_verified.assert_not_called()
            self.assertEqual(old_dump.read_bytes(), b"retained")
            self.assertTrue(old_metadata.exists())

    def test_restore_failure_does_not_prune_existing_backups(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            old_dump, old_metadata = self._write_backup_pair(
                output_dir, "platformdb-old.dump", b"old-backup"
            )
            args = argparse.Namespace(
                env_file=str(output_dir / ".env.platform"),
                output_dir=str(output_dir),
                keep=1,
                admin_database_url=None,
                dump_only=False,
                rotate_existing=False,
            )
            old_entries = []
            for index in range(15):
                dump, metadata = self._write_backup_pair(
                    output_dir,
                    f"platformdb-old-{index:02}.dump",
                    f"old-{index}".encode(),
                )
                old_entries.extend((dump, metadata))
            unverified_dump, unverified_metadata = self._write_backup_pair(
                output_dir,
                "platformdb-unverified.dump",
                b"unverified-old",
                restore_verified=False,
            )
            old_entries.extend((unverified_dump, unverified_metadata))
            old_entries.extend((old_dump, old_metadata))
            before = {
                path: (path.read_bytes(), path.lstat().st_mode, path.lstat().st_ino)
                for path in old_entries
            }

            with tempfile.TemporaryDirectory() as owner_dir:
                owner_probe = pathlib.Path(owner_dir) / "owner-probe.dump"
                owner_probe.write_bytes(b"bounded owner check")
                owner_probe.chmod(0o600)
                owner_probe_stat = owner_probe.lstat()
                owner_identity = (owner_probe_stat.st_dev, owner_probe_stat.st_ino)
                backup_drill._fsync_backup_dump(
                    owner_probe,
                    expected_identity=owner_identity,
                    expected_uid=os.geteuid(),
                )
                with (
                    mock.patch.object(backup_drill.os, "fsync") as rejected_fsync,
                    self.assertRaisesRegex(
                        RuntimeError, "Temporary backup archive identity is unsafe before fsync"
                    ),
                ):
                    backup_drill._fsync_backup_dump(
                        owner_probe,
                        expected_identity=owner_identity,
                        expected_uid=os.geteuid() + 1,
                    )
                rejected_fsync.assert_not_called()

            initial_inventory = backup_drill.backup_inventory(output_dir)
            fixed_backup_time = dt.datetime(2026, 10, 6, 12, 0, 0, tzinfo=dt.UTC)
            operation_order: list[str] = []
            real_fsync_dump = backup_drill._fsync_backup_dump

            def record_dump_fsync(
                path: pathlib.Path,
                *,
                expected_identity: tuple[int, int],
                expected_uid: int,
            ) -> None:
                operation_order.append("dump_fsync")
                real_fsync_dump(
                    path, expected_identity=expected_identity, expected_uid=expected_uid
                )

            def fail_restore_after_fsync(*_args: object, **_kwargs: object) -> int:
                operation_order.append("restore")
                self.assertEqual(operation_order, ["dump_fsync", "restore"])
                raise RuntimeError("restore failed")

            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {
                        "PLATFORM_DATABASE_URL": (
                            "postgresql://platform_user@127.0.0.1:5432/platformdb"
                        )
                    },
                    clear=False,
                ),
                mock.patch.object(
                    backup_drill,
                    "load_env",
                    return_value={
                        "PLATFORM_DATABASE_URL": (
                            "postgresql://platform_user@127.0.0.1:5432/platformdb"
                        )
                    },
                ),
                mock.patch.object(backup_drill, "utc_now", return_value=fixed_backup_time),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(
                    backup_drill,
                    "run_disk_guarded_command",
                    side_effect=self._fake_guarded_dump,
                ),
                mock.patch.object(
                    backup_drill,
                    "_fsync_backup_dump",
                    side_effect=record_dump_fsync,
                ),
                mock.patch.object(backup_drill, "require_restore_headroom"),
                mock.patch.object(
                    backup_drill,
                    "_run_isolated_restore_drill",
                    side_effect=fail_restore_after_fsync,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "restore failed") as raised:
                    backup_drill.create_backup(args)
            self.assertEqual(operation_order, ["dump_fsync", "restore"])
            output = io.StringIO()
            main_args = argparse.Namespace(
                check_latest=False,
                dump_only=False,
                verify_dump=None,
                output_dir=str(output_dir),
                max_age_hours=24,
                as_json=True,
            )
            with (
                mock.patch.object(backup_drill, "parse_args", return_value=main_args),
                mock.patch.object(backup_drill, "create_backup", side_effect=raised.exception),
                mock.patch.object(backup_drill.sys, "stdout", output),
            ):
                self.assertEqual(backup_drill.main(), 1)
            self.assertEqual(json.loads(output.getvalue())["error"], "restore failed")

            self.assertTrue(old_dump.exists())
            self.assertTrue(old_metadata.exists())
            self.assertEqual(backup_drill.backup_inventory(output_dir), initial_inventory)
            self.assertEqual(
                {path.name for path in output_dir.iterdir()},
                {path.name for path in old_entries},
            )
            for path, expected in before.items():
                metadata = path.lstat()
                self.assertEqual((path.read_bytes(), metadata.st_mode, metadata.st_ino), expected)

            restore_after_fsync_failure = mock.Mock()
            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {
                        "PLATFORM_DATABASE_URL": (
                            "postgresql://platform_user@127.0.0.1:5432/platformdb"
                        )
                    },
                    clear=False,
                ),
                mock.patch.object(
                    backup_drill,
                    "load_env",
                    return_value={
                        "PLATFORM_DATABASE_URL": (
                            "postgresql://platform_user@127.0.0.1:5432/platformdb"
                        )
                    },
                ),
                mock.patch.object(
                    backup_drill,
                    "utc_now",
                    return_value=fixed_backup_time + dt.timedelta(seconds=1),
                ),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(
                    backup_drill,
                    "run_disk_guarded_command",
                    side_effect=self._fake_guarded_dump,
                ),
                mock.patch.object(backup_drill, "require_restore_headroom"),
                mock.patch.object(
                    backup_drill, "_run_isolated_restore_drill", restore_after_fsync_failure
                ),
                mock.patch.object(
                    backup_drill.os, "fsync", side_effect=OSError("synthetic dump fsync failure")
                ),
            ):
                with self.assertRaisesRegex(OSError, "synthetic dump fsync failure"):
                    backup_drill.create_backup(args)

            restore_after_fsync_failure.assert_not_called()
            self.assertEqual(backup_drill.backup_inventory(output_dir), initial_inventory)
            self.assertEqual(
                {path.name for path in output_dir.iterdir()},
                {path.name for path in old_entries},
            )

            dump_only_args = argparse.Namespace(
                env_file=str(output_dir / ".env.platform"),
                output_dir=str(output_dir),
                keep=1,
                admin_database_url=None,
                dump_only=True,
                rotate_existing=False,
            )
            dump_only_events: list[str] = []

            def record_dump_only_fsync(
                path: pathlib.Path,
                *,
                expected_identity: tuple[int, int],
                expected_uid: int,
            ) -> None:
                dump_only_events.append("dump_fsync")
                real_fsync_dump(
                    path, expected_identity=expected_identity, expected_uid=expected_uid
                )

            dump_only_restore = mock.Mock()
            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {
                        "PLATFORM_DATABASE_URL": (
                            "postgresql://platform_user@127.0.0.1:5432/platformdb"
                        )
                    },
                    clear=False,
                ),
                mock.patch.object(
                    backup_drill,
                    "load_env",
                    return_value={
                        "PLATFORM_DATABASE_URL": (
                            "postgresql://platform_user@127.0.0.1:5432/platformdb"
                        )
                    },
                ),
                mock.patch.object(
                    backup_drill,
                    "utc_now",
                    side_effect=[
                        fixed_backup_time + dt.timedelta(seconds=2),
                        fixed_backup_time + dt.timedelta(seconds=3),
                        fixed_backup_time + dt.timedelta(seconds=4),
                    ],
                ),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(
                    backup_drill,
                    "run_disk_guarded_command",
                    side_effect=self._fake_guarded_dump,
                ),
                mock.patch.object(
                    backup_drill,
                    "_fsync_backup_dump",
                    side_effect=record_dump_only_fsync,
                ),
                mock.patch.object(backup_drill, "require_restore_headroom"),
                mock.patch.object(
                    backup_drill, "_run_isolated_restore_drill", dump_only_restore
                ),
            ):
                dump_only_result = backup_drill.create_backup(dump_only_args)

            self.assertEqual(dump_only_events, ["dump_fsync"])
            dump_only_restore.assert_not_called()
            self.assertTrue(dump_only_result["ok"])
            self.assertIs(dump_only_result["restore_verified"], False)
            initial_inventory = backup_drill.backup_inventory(output_dir)
            old_entries = list(output_dir.iterdir())

            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {
                        "PLATFORM_DATABASE_URL": (
                            "postgresql://platform_user@127.0.0.1:5432/platformdb"
                        )
                    },
                    clear=False,
                ),
                mock.patch.object(
                    backup_drill,
                    "load_env",
                    return_value={
                        "PLATFORM_DATABASE_URL": (
                            "postgresql://platform_user@127.0.0.1:5432/platformdb"
                        )
                    },
                ),
                mock.patch.object(
                    backup_drill,
                    "utc_now",
                    return_value=fixed_backup_time + dt.timedelta(seconds=5),
                ),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(
                    backup_drill,
                    "run_disk_guarded_command",
                    side_effect=self._fake_guarded_dump,
                ),
                mock.patch.object(backup_drill, "require_restore_headroom"),
                mock.patch.object(
                    backup_drill,
                    "_run_isolated_restore_drill",
                    side_effect=backup_drill.BackupOperationInterrupted(signal.SIGTERM),
                ),
            ):
                with self.assertRaises(backup_drill.BackupOperationInterrupted):
                    backup_drill.create_backup(args)
            self.assertEqual(backup_drill.backup_inventory(output_dir), initial_inventory)
            self.assertEqual(
                {path.name for path in output_dir.iterdir()},
                {path.name for path in old_entries},
            )

            self._assert_postpublication_inventory_failure_rolls_back_only_new_pair()
            self._assert_directory_fsync_failure_rolls_back_only_new_pair()

            self._assert_preserve_existing_keeps_all_archives_after_success()
            self._assert_destination_collision_fails_before_dump_and_preserves_sidecar()
            self._assert_explicit_rotation_retains_keep_policy()

    def _assert_postpublication_inventory_failure_rolls_back_only_new_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            old_dump, old_metadata = self._write_backup_pair(
                output_dir, "platformdb-old.dump", b"old-backup"
            )
            before = {
                path: (path.read_bytes(), path.lstat().st_mode, path.lstat().st_ino)
                for path in (old_dump, old_metadata)
            }
            initial_inventory = backup_drill.backup_inventory(output_dir)
            args = argparse.Namespace(
                env_file=str(output_dir / ".env.platform"),
                output_dir=str(output_dir),
                keep=14,
                admin_database_url=None,
                dump_only=False,
                rotate_existing=False,
            )
            real_inventory = backup_drill.backup_inventory
            inventory_calls = 0

            def fail_after_publication(path: pathlib.Path) -> tuple[object, ...]:
                nonlocal inventory_calls
                inventory_calls += 1
                if inventory_calls == 3:
                    raise RuntimeError("synthetic post-publication inventory failure")
                return real_inventory(path)

            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {"PLATFORM_DATABASE_URL": "postgresql://platform_user@localhost/platformdb"},
                ),
                mock.patch.object(backup_drill, "load_env", return_value={}),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(
                    backup_drill,
                    "run_disk_guarded_command",
                    side_effect=self._fake_guarded_dump,
                ),
                mock.patch.object(backup_drill, "require_restore_headroom"),
                mock.patch.object(backup_drill, "_run_isolated_restore_drill", return_value=1),
                mock.patch.object(backup_drill, "backup_inventory", side_effect=fail_after_publication),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "synthetic post-publication inventory failure"
                ):
                    backup_drill.create_backup(args)

            self.assertEqual(inventory_calls, 4)
            self.assertEqual(backup_drill.backup_inventory(output_dir), initial_inventory)
            self.assertEqual(
                {path.name for path in output_dir.iterdir()},
                {old_dump.name, old_metadata.name},
            )
            for path, expected in before.items():
                metadata = path.lstat()
                self.assertEqual((path.read_bytes(), metadata.st_mode, metadata.st_ino), expected)

    def _assert_directory_fsync_failure_rolls_back_only_new_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            old_entries: list[pathlib.Path] = []
            for index in range(20):
                dump, sidecar = self._write_backup_pair(
                    output_dir,
                    f"platformdb-existing-{index:02}.dump",
                    f"prior-{index}".encode(),
                )
                old_entries.extend((dump, sidecar))
            initial_inventory = backup_drill.backup_inventory(output_dir)
            args = argparse.Namespace(
                env_file=str(output_dir / ".env.platform"),
                output_dir=str(output_dir),
                keep=14,
                admin_database_url=None,
                dump_only=True,
                rotate_existing=False,
            )
            real_fsync_directory = backup_drill._fsync_directory
            fsync_calls = 0

            def fail_publication_fsync(path: pathlib.Path) -> None:
                nonlocal fsync_calls
                fsync_calls += 1
                if fsync_calls == 1:
                    raise OSError("synthetic directory fsync failure")
                real_fsync_directory(path)

            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {"PLATFORM_DATABASE_URL": "postgresql://platform_user@localhost/platformdb"},
                ),
                mock.patch.object(backup_drill, "load_env", return_value={}),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(
                    backup_drill,
                    "run_disk_guarded_command",
                    side_effect=self._fake_guarded_dump,
                ),
                mock.patch.object(backup_drill, "require_restore_headroom"),
                mock.patch.object(
                    backup_drill, "_fsync_directory", side_effect=fail_publication_fsync
                ),
            ):
                with self.assertRaisesRegex(OSError, "synthetic directory fsync failure"):
                    backup_drill.create_backup(args)

            self.assertEqual(fsync_calls, 2)
            self.assertEqual(backup_drill.backup_inventory(output_dir), initial_inventory)
            self.assertEqual(
                {path.name for path in output_dir.iterdir()},
                {path.name for path in old_entries},
            )

            fsync_calls = 0

            def interrupt_after_publication(path: pathlib.Path) -> None:
                nonlocal fsync_calls
                fsync_calls += 1
                if fsync_calls == 1:
                    signal.raise_signal(signal.SIGTERM)
                real_fsync_directory(path)

            cli_args = argparse.Namespace(
                check_latest=False,
                verify_latest_existing=False,
                dump_only=True,
                verify_dump=None,
                output_dir=str(output_dir),
                env_file=str(output_dir / ".env.platform"),
                max_age_hours=24,
                admin_database_url=None,
                keep=14,
                rotate_existing=False,
                as_json=True,
            )
            signal_output = io.StringIO()

            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {"PLATFORM_DATABASE_URL": "postgresql://platform_user@localhost/platformdb"},
                ),
                mock.patch.object(backup_drill, "load_env", return_value={}),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(
                    backup_drill,
                    "run_disk_guarded_command",
                    side_effect=self._fake_guarded_dump,
                ),
                mock.patch.object(backup_drill, "require_restore_headroom"),
                mock.patch.object(
                    backup_drill, "_fsync_directory", side_effect=interrupt_after_publication
                ),
                mock.patch.object(backup_drill, "parse_args", return_value=cli_args),
                mock.patch.object(backup_drill.sys, "stdout", signal_output),
            ):
                self.assertEqual(backup_drill.main(), 1)

            self.assertGreaterEqual(fsync_calls, 2)
            self.assertIn("Backup operation interrupted by SIGTERM.", signal_output.getvalue())
            self.assertEqual(backup_drill.backup_inventory(output_dir), initial_inventory)
            self.assertEqual(
                {path.name for path in output_dir.iterdir()},
                {path.name for path in old_entries},
            )

    def _assert_preserve_existing_keeps_all_archives_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            old_entries: list[pathlib.Path] = []
            for index in range(16):
                dump, sidecar = self._write_backup_pair(
                    output_dir,
                    f"platformdb-old-{index:02}.dump",
                    f"verified-{index}".encode(),
                )
                old_entries.extend((dump, sidecar))
            unverified_dump, unverified_sidecar = self._write_backup_pair(
                output_dir,
                "platformdb-old-unverified.dump",
                b"old-unverified",
                restore_verified=False,
            )
            old_entries.extend((unverified_dump, unverified_sidecar))
            before = {
                path: (path.read_bytes(), path.lstat().st_mode, path.lstat().st_ino)
                for path in old_entries
            }
            args = argparse.Namespace(
                env_file=str(output_dir / ".env.platform"),
                output_dir=str(output_dir),
                keep=1,
                admin_database_url=None,
                dump_only=False,
                rotate_existing=False,
            )

            def fake_run_command(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                if command[0] == "pg_dump":
                    pathlib.Path(command[command.index("--file") + 1]).write_bytes(b"new-archive")
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command[0] == "pg_restore":
                    return subprocess.CompletedProcess(command, 0, "", "")
                raise AssertionError(f"Unexpected command: {command[0]}")

            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {"PLATFORM_DATABASE_URL": "postgresql://platform_user@localhost/platformdb"},
                ),
                mock.patch.object(backup_drill, "load_env", return_value={}),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "run_command", side_effect=fake_run_command),
                mock.patch.object(
                    backup_drill,
                    "run_disk_guarded_command",
                    side_effect=self._fake_guarded_dump,
                ),
                mock.patch.object(backup_drill, "require_restore_headroom"),
                mock.patch.object(backup_drill, "_run_isolated_restore_drill", return_value=16),
                mock.patch.object(
                    backup_drill,
                    "utc_now",
                    side_effect=[dt.datetime(2026, 10, 6, 12, 0, 0, tzinfo=dt.UTC)] * 3,
                ),
            ):
                result = backup_drill.create_backup(args)

            self.assertTrue(result["restore_verified"])
            self.assertEqual(result["rotation_mode"], "preserve-existing")
            self.assertEqual(result["preexisting_archive_count"], 17)
            self.assertEqual(result["preexisting_sidecar_count"], 17)
            self.assertEqual(result["preexisting_archives_preserved"], True)
            self.assertEqual(result["removed"], [])
            for path, expected in before.items():
                metadata = path.lstat()
                self.assertEqual((path.read_bytes(), metadata.st_mode, metadata.st_ino), expected)

    def test_preserve_existing_rejects_incomplete_or_mismatched_pairs(self) -> None:
        for malformed in ("orphan_dump", "orphan_sidecar", "wrong_digest", "wrong_dump_name"):
            with self.subTest(malformed=malformed), tempfile.TemporaryDirectory() as temporary_dir:
                output_dir = pathlib.Path(temporary_dir)
                dump_path, sidecar_path = self._write_backup_pair(
                    output_dir, "platformdb-existing.dump", b"existing-archive"
                )
                if malformed == "orphan_dump":
                    sidecar_path.unlink()
                elif malformed == "orphan_sidecar":
                    dump_path.unlink()
                else:
                    metadata = json.loads(sidecar_path.read_text(encoding="utf-8"))
                    metadata["sha256" if malformed == "wrong_digest" else "dump_file"] = (
                        "0" * 64 if malformed == "wrong_digest" else "platformdb-other.dump"
                    )
                    sidecar_path.write_text(json.dumps(metadata), encoding="utf-8")
                args = argparse.Namespace(
                    env_file=str(output_dir / ".env.platform"),
                    output_dir=str(output_dir),
                    keep=1,
                    admin_database_url=None,
                    dump_only=True,
                    rotate_existing=False,
                )
                with (
                    mock.patch.dict(
                        backup_drill.os.environ,
                        {"PLATFORM_DATABASE_URL": "postgresql://platform_user@localhost/platformdb"},
                    ),
                    mock.patch.object(backup_drill, "load_env", return_value={}),
                    mock.patch.object(backup_drill, "require_commands"),
                    mock.patch.object(backup_drill, "run_disk_guarded_command") as dump_command,
                    self.assertRaisesRegex(RuntimeError, "incomplete|mismatched|invalid"),
                ):
                    backup_drill.create_backup(args)
                dump_command.assert_not_called()

    def test_preserve_existing_detects_same_inode_content_tamper_with_restored_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            old_dump, _ = self._write_backup_pair(
                output_dir, "platformdb-existing.dump", b"original-payload"
            )
            original_stat = old_dump.stat()
            original_inode = old_dump.lstat().st_ino
            original_mode = old_dump.lstat().st_mode
            original_entry = next(
                entry
                for entry in backup_drill.backup_inventory(output_dir)
                if entry[0] == old_dump.name
            )
            args = argparse.Namespace(
                env_file=str(output_dir / ".env.platform"),
                output_dir=str(output_dir),
                keep=1,
                admin_database_url=None,
                dump_only=False,
                rotate_existing=False,
            )

            def mutate_existing_archive(*_: object, **__: object) -> int:
                with old_dump.open("r+b") as handle:
                    handle.write(b"tampered-payload")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.utime(old_dump, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
                mutated_entry = next(
                    entry
                    for entry in backup_drill.backup_inventory(output_dir)
                    if entry[0] == old_dump.name
                )
                self.assertEqual(original_entry[:7], mutated_entry[:7])
                self.assertNotEqual(original_entry[7], mutated_entry[7])
                return 1

            def fake_run_command(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                if command[0] == "pg_dump":
                    pathlib.Path(command[command.index("--file") + 1]).write_bytes(b"new-archive")
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command[0] == "pg_restore":
                    return subprocess.CompletedProcess(command, 0, "", "")
                raise AssertionError(f"Unexpected command: {command[0]}")

            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {"PLATFORM_DATABASE_URL": "postgresql://platform_user@localhost/platformdb"},
                ),
                mock.patch.object(backup_drill, "load_env", return_value={}),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "run_command", side_effect=fake_run_command),
                mock.patch.object(backup_drill, "run_disk_guarded_command", side_effect=self._fake_guarded_dump),
                mock.patch.object(backup_drill, "require_restore_headroom"),
                mock.patch.object(backup_drill, "_run_isolated_restore_drill", side_effect=mutate_existing_archive),
                mock.patch.object(
                    backup_drill,
                    "utc_now",
                    side_effect=[dt.datetime(2026, 10, 6, 12, 0, 0, tzinfo=dt.UTC)] * 3,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "mismatched existing backup pair"):
                    backup_drill.create_backup(args)

            self.assertEqual(old_dump.lstat().st_ino, original_inode)
            self.assertEqual(old_dump.lstat().st_mode, original_mode)
            self.assertEqual(old_dump.stat().st_mtime_ns, original_stat.st_mtime_ns)
            self.assertFalse((output_dir / "platformdb-20261006T120000Z.dump").exists())

    def test_preserve_existing_post_publish_member_change_rolls_back_only_new_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            old_dump, old_sidecar = self._write_backup_pair(
                output_dir, "platformdb-existing.dump", b"existing-archive"
            )
            original_dump = (old_dump.read_bytes(), old_dump.lstat().st_ino)
            original_sidecar = (old_sidecar.read_bytes(), old_sidecar.lstat().st_ino)
            original_mtime_ns = old_dump.stat().st_mtime_ns
            initial_names = {old_dump.name, old_sidecar.name}
            args = argparse.Namespace(
                env_file=str(output_dir / ".env.platform"),
                output_dir=str(output_dir),
                keep=14,
                admin_database_url=None,
                dump_only=True,
                rotate_existing=False,
            )
            real_inventory = backup_drill.backup_inventory
            inventory_calls = 0

            def mutate_old_member_after_publish(path: pathlib.Path):
                nonlocal inventory_calls
                inventory_calls += 1
                # Calls 1 and 2 are the initial and pre-publication snapshots;
                # call 3 is the post-publication inventory with both new files present.
                if inventory_calls == 3:
                    os.utime(
                        old_dump,
                        ns=(old_dump.stat().st_atime_ns, original_mtime_ns + 1_000_000),
                    )
                return real_inventory(path)

            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {"PLATFORM_DATABASE_URL": "postgresql://platform_user@localhost/platformdb"},
                ),
                mock.patch.object(backup_drill, "load_env", return_value={}),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(
                    backup_drill,
                    "run_disk_guarded_command",
                    side_effect=self._fake_guarded_dump,
                ),
                mock.patch.object(backup_drill, "require_restore_headroom"),
                mock.patch.object(
                    backup_drill, "backup_inventory", side_effect=mutate_old_member_after_publish
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "Pre-existing backup inventory changed during preserve mode"
                ) as raised:
                    backup_drill.create_backup(args)

            self.assertEqual(inventory_calls, 4)
            self.assertEqual(
                getattr(raised.exception, "_backup_preservation_integrity", None),
                "preexisting_inventory_changed",
            )
            self.assertEqual({path.name for path in output_dir.iterdir()}, initial_names)
            self.assertEqual((old_dump.read_bytes(), old_dump.lstat().st_ino), original_dump)
            self.assertEqual((old_sidecar.read_bytes(), old_sidecar.lstat().st_ino), original_sidecar)
            self.assertEqual(old_dump.stat().st_mtime_ns, original_mtime_ns + 1_000_000)

    def _assert_destination_collision_fails_before_dump_and_preserves_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            timestamp = dt.datetime(2026, 10, 6, 12, 0, 0, tzinfo=dt.UTC)
            dump_path = output_dir / "platformdb-20261006T120000Z.dump"
            dump_path, sidecar_path = self._write_backup_pair(
                output_dir, dump_path.name, b"pre-existing-target"
            )
            os.chmod(dump_path, 0o640)
            os.chmod(sidecar_path, 0o640)
            before = (sidecar_path.read_bytes(), sidecar_path.lstat().st_mode, sidecar_path.lstat().st_ino)
            args = argparse.Namespace(
                env_file=str(output_dir / ".env.platform"),
                output_dir=str(output_dir),
                keep=14,
                admin_database_url=None,
                dump_only=True,
                rotate_existing=False,
            )
            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {"PLATFORM_DATABASE_URL": "postgresql://platform_user@localhost/platformdb"},
                ),
                mock.patch.object(backup_drill, "load_env", return_value={}),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "utc_now", return_value=timestamp),
                mock.patch.object(backup_drill, "run_command") as run_command,
            ):
                with self.assertRaisesRegex(RuntimeError, "refusing to replace"):
                    backup_drill.create_backup(args)

            run_command.assert_not_called()
            metadata = sidecar_path.lstat()
            self.assertEqual((sidecar_path.read_bytes(), metadata.st_mode, metadata.st_ino), before)
            self.assertTrue(dump_path.exists())

        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            timestamp = dt.datetime(2026, 10, 6, 12, 0, 0, tzinfo=dt.UTC)
            dump_path = output_dir / "platformdb-20261006T120000Z.dump"
            dump_path, sidecar_path = self._write_backup_pair(
                output_dir, dump_path.name, b"pre-existing-target"
            )
            os.chmod(dump_path, 0o640)
            os.chmod(sidecar_path, 0o640)
            before = (dump_path.read_bytes(), dump_path.lstat().st_mode, dump_path.lstat().st_ino)
            args = argparse.Namespace(
                env_file=str(output_dir / ".env.platform"),
                output_dir=str(output_dir),
                keep=14,
                admin_database_url=None,
                dump_only=True,
                rotate_existing=False,
            )
            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {"PLATFORM_DATABASE_URL": "postgresql://platform_user@localhost/platformdb"},
                ),
                mock.patch.object(backup_drill, "load_env", return_value={}),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "utc_now", return_value=timestamp),
                mock.patch.object(backup_drill, "run_command") as run_command,
            ):
                with self.assertRaisesRegex(RuntimeError, "refusing to replace"):
                    backup_drill.create_backup(args)
            run_command.assert_not_called()
            metadata = dump_path.lstat()
            self.assertEqual((dump_path.read_bytes(), metadata.st_mode, metadata.st_ino), before)
            self.assertTrue(sidecar_path.exists())

        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            timestamp = dt.datetime(2026, 10, 6, 12, 0, 0, tzinfo=dt.UTC)
            dump_path = output_dir / "platformdb-20261006T120000Z.dump"
            sidecar_path = dump_path.with_suffix(".json")
            args = argparse.Namespace(
                env_file=str(output_dir / ".env.platform"),
                output_dir=str(output_dir),
                keep=14,
                admin_database_url=None,
                dump_only=True,
                rotate_existing=False,
            )

            def fake_run_command(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                if command[0] == "pg_dump":
                    pathlib.Path(command[command.index("--file") + 1]).write_bytes(b"new-archive")
                return subprocess.CompletedProcess(command, 0, "", "")

            real_link = backup_drill.os.link
            link_count = 0

            def race_sidecar_collision(source: object, destination: object, **kwargs: object) -> None:
                nonlocal link_count
                link_count += 1
                if link_count == 1:
                    real_link(source, destination, **kwargs)
                    return
                sidecar_path.write_bytes(b"racing-sidecar")
                os.chmod(sidecar_path, 0o640)
                raise FileExistsError

            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {"PLATFORM_DATABASE_URL": "postgresql://platform_user@localhost/platformdb"},
                ),
                mock.patch.object(backup_drill, "load_env", return_value={}),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "utc_now", return_value=timestamp),
                mock.patch.object(backup_drill, "run_command", side_effect=fake_run_command),
                mock.patch.object(
                    backup_drill,
                    "run_disk_guarded_command",
                    side_effect=self._fake_guarded_dump,
                ),
                mock.patch.object(backup_drill, "require_restore_headroom"),
                mock.patch.object(backup_drill.os, "link", side_effect=race_sidecar_collision),
            ):
                with self.assertRaisesRegex(RuntimeError, "destination already exists") as raised:
                    backup_drill.create_backup(args)

            self.assertEqual(
                getattr(raised.exception, "_backup_preservation_integrity", None),
                "preexisting_inventory_changed",
            )
            self.assertEqual(link_count, 2)
            self.assertEqual(sidecar_path.read_bytes(), b"racing-sidecar")
            self.assertEqual(sidecar_path.stat().st_mode & 0o777, 0o640)
            self.assertFalse(dump_path.exists())

    def _assert_explicit_rotation_retains_keep_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            output_dir = pathlib.Path(temporary_dir)
            old_verified: list[pathlib.Path] = []
            base_time = dt.datetime(2026, 9, 1, tzinfo=dt.UTC).timestamp()
            for index in range(16):
                timestamp = dt.datetime(2026, 9, 1, tzinfo=dt.UTC) + dt.timedelta(seconds=index)
                dump = output_dir / f"platformdb-{timestamp.strftime('%Y%m%dT%H%M%SZ')}.dump"
                sidecar = dump.with_suffix(".json")
                dump.write_bytes(f"old-{index}".encode())
                sidecar.write_text(
                    json.dumps({"dump_file": dump.name, "restore_verified": True}),
                    encoding="utf-8",
                )
                old_time = base_time + index
                os.utime(dump, (old_time, old_time))
                os.utime(sidecar, (old_time, old_time))
                old_verified.append(dump)
            failed_dump = output_dir / "platformdb-20260901T000100Z.dump"
            failed_metadata = failed_dump.with_suffix(".json")
            failed_dump.write_bytes(b"old-unverified")
            failed_metadata.write_text(
                json.dumps({"dump_file": failed_dump.name, "restore_verified": False}),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                env_file=str(output_dir / ".env.platform"),
                output_dir=str(output_dir),
                keep=14,
                admin_database_url=None,
                dump_only=False,
                rotate_existing=True,
            )

            def fake_run_command(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                if command[0] == "pg_dump":
                    pathlib.Path(command[command.index("--file") + 1]).write_bytes(b"new-archive")
                return subprocess.CompletedProcess(command, 0, "", "")

            events: list[str] = []
            real_fsync_directory = backup_drill._fsync_directory
            real_prune_unverified = backup_drill.prune_unverified_backups
            real_prune_backups = backup_drill.prune_backups

            def record_fsync(path: pathlib.Path) -> None:
                events.append("directory-fsync")
                real_fsync_directory(path)

            def record_prune_unverified(*args: object, **kwargs: object) -> list[str]:
                events.append("prune-unverified")
                return real_prune_unverified(*args, **kwargs)

            def record_prune_backups(*args: object, **kwargs: object) -> list[str]:
                events.append("prune-backups")
                return real_prune_backups(*args, **kwargs)

            with (
                mock.patch.dict(
                    backup_drill.os.environ,
                    {"PLATFORM_DATABASE_URL": "postgresql://platform_user@localhost/platformdb"},
                ),
                mock.patch.object(backup_drill, "load_env", return_value={}),
                mock.patch.object(backup_drill, "require_commands"),
                mock.patch.object(backup_drill, "run_command", side_effect=fake_run_command),
                mock.patch.object(
                    backup_drill,
                    "run_disk_guarded_command",
                    side_effect=self._fake_guarded_dump,
                ),
                mock.patch.object(backup_drill, "require_restore_headroom"),
                mock.patch.object(backup_drill, "_run_isolated_restore_drill", return_value=20),
                mock.patch.object(backup_drill, "_fsync_directory", side_effect=record_fsync),
                mock.patch.object(
                    backup_drill, "prune_unverified_backups", side_effect=record_prune_unverified
                ),
                mock.patch.object(backup_drill, "prune_backups", side_effect=record_prune_backups),
            ):
                result = backup_drill.create_backup(args)

            self.assertEqual(result["rotation_mode"], "rotate-existing")
            self.assertNotIn("protected_retention_proof", result)
            self.assertEqual(
                events, ["directory-fsync", "prune-unverified", "prune-backups"]
            )
            self.assertEqual(len(result["removed"]), 8)
            self.assertFalse(failed_dump.exists())
            self.assertFalse(failed_metadata.exists())
            self.assertEqual(
                sum(dump.exists() for dump in old_verified),
                13,
            )
            self.assertEqual(
                len(tuple(output_dir.glob("platformdb-*.dump"))),
                14,
            )

    @staticmethod
    def _fake_guarded_dump(
        command: list[str], *, target: object, stage: str, disk_paths: tuple[pathlib.Path, ...]
    ) -> None:
        del target, disk_paths
        if stage != "pg_dump":
            raise AssertionError("unexpected guarded command in backup unit test")
        dump_path = pathlib.Path(command[command.index("--file") + 1])
        dump_path.write_bytes(b"PGDMPnew-archive")


if __name__ == "__main__":
    unittest.main()
