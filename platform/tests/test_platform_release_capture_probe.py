from __future__ import annotations

import base64
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tools import platform_release_capture_probe as probe
from tools import platform_live_qa_runtime_install as runtime_installer
from tools import platform_release_systemd_state as systemd_state


class FakeSystemdState:
    SYSTEMCTL = "/usr/bin/systemctl"
    subprocess = subprocess

    def __init__(self, *, fail_query: tuple[str, str] | None = None, operation: str = "install", transaction_operation: str | None = None, phase: str = "rollback-runtime-pending") -> None:
        self.events: list[str] = []
        self.fail_query = fail_query
        self.snapshot_active = "active"
        self.snapshot_enabled = "enabled"
        self.operation = operation
        self.transaction_operation = transaction_operation or operation
        self.phase = phase
        self.__verified_sha256__ = "a" * 64
        self.OWNED_UNITS = ("deadlock-api.service",)

    @staticmethod
    def _strict_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate key")
            value[key] = item
        return value

    def _validate_systemctl_path(self, path: str) -> str:
        self.events.append("systemctl_path")
        return path

    def _read_transaction(self, path: Path, app_dir: Path):
        self.events.append("transaction")
        previous = str(app_dir / "releases" / "pending") if self.operation == "rollback" else ""
        return ("a" * 32, self.transaction_operation, str(app_dir / "releases" / "old"), previous, {}, {}, {"deadlock-api.service": "active"})

    def _helper_digests(self, release: Path):
        self.events.append("helper_digests")
        return {
            "tools/platform_release_systemd_state.py": self.__verified_sha256__,
            "tools/platform_install_nginx.py": "c" * 64,
            "tools/platform_live_qa_runtime_install.py": "d" * 64,
        }

    def _release_context(self, app_dir: Path, current: str, previous: str):
        self.events.append("capture_context")
        return True

    def _safe_receipt(self, path: Path):
        raise AssertionError("probe must not read or write receipt bytes")

    def _read_receipt(self, path: Path):
        self.events.append("receipt_read")
        return {
            "operation": "rollback",
            "operation_id": "a" * 32,
            "app_dir": str(path.parents[1]),
            "current_before": str(path.parents[1] / "releases" / "old"),
            "previous_before": str(path.parents[1] / "releases" / "pending"),
            "helper_digests": {
                "current_before": {
                    "tools/platform_release_systemd_state.py": self.__verified_sha256__,
                    "tools/platform_install_nginx.py": "c" * 64,
                    "tools/platform_live_qa_runtime_install.py": "d" * 64,
                },
                "previous_before": {
                    "tools/platform_release_systemd_state.py": self.__verified_sha256__,
                    "tools/platform_install_nginx.py": "c" * 64,
                    "tools/platform_live_qa_runtime_install.py": "d" * 64,
                },
            },
            "units": [
                {"name": unit, "active": "active", "enabled": "enabled"}
                for unit in self.OWNED_UNITS
            ],
        }

    def _read_active(self, unit: str) -> str:
        self.events.append("active:" + unit)
        if self.fail_query == ("active", unit):
            raise RuntimeError("private command output must not escape")
        return self.snapshot_active

    def _read_enabled(self, unit: str) -> str:
        self.events.append("enabled:" + unit)
        if self.fail_query == ("enabled", unit):
            raise RuntimeError("private command output must not escape")
        return self.snapshot_enabled

    def _unit_snapshot(self, active_overrides=None):
        self.events.append("unit_snapshot")
        return [{"name": unit, "active": self._read_active(unit), "enabled": self._read_enabled(unit)} for unit in self.OWNED_UNITS]

    def validate(self, path: Path, **kwargs) -> None:
        self.events.append("validate")

    def verify(self, path: Path, **kwargs) -> None:
        self.events.append("verify")
        for unit in self.OWNED_UNITS:
            if self._read_active(unit) != "active" or self._read_enabled(unit) != "enabled":
                raise RuntimeError("unit state mismatch")

    def capture_transaction(self, *args, **kwargs):  # pragma: no cover - must remain unreachable
        raise AssertionError("write-capable capture invoked")

    def capture(self, *args, **kwargs):  # pragma: no cover - must remain unreachable
        raise AssertionError("write-capable capture invoked")

    def _write_receipt(self, *args, **kwargs):  # pragma: no cover - must remain unreachable
        raise AssertionError("receipt writer invoked")

    def restore(self, *args, **kwargs):  # pragma: no cover - must remain unreachable
        raise AssertionError("systemd restore invoked")


class ReleaseCaptureProbeTests(unittest.TestCase):
    def _paths(self, temporary: Path) -> tuple[Path, Path]:
        shared = temporary / "shared"
        shared.mkdir()
        return shared / ".release-operation.json", shared / ".release-systemd-state.json"

    def _run(
        self,
        fake: FakeSystemdState,
        transaction: Path,
        companion: Path,
        *,
        diagnostic_systemd_state: bool = False,
        release_source: str = "a" * 40,
    ):
        app_dir = transaction.parents[1]
        releases = app_dir / "releases"
        releases.mkdir()
        old_release = releases / "old"
        old_release.mkdir()
        (old_release / "RELEASE.json").write_text('{"source_git_commit":"' + release_source + '"}\n', encoding="ascii")
        if fake.operation == "rollback":
            previous_release = releases / "pending"
            previous_release.mkdir()
            (previous_release / "RELEASE.json").write_text('{"source_git_commit":"' + "b" * 40 + '"}\n', encoding="ascii")
            current_release = old_release
            previous_path = str(previous_release)
            (transaction.parent / ".release-operation.json").write_text(
                json.dumps({
                    "operation_id": "a" * 32,
                    "operation": fake.transaction_operation,
                    "phase": fake.phase,
                    "current_before": str(old_release),
                    "previous_before": previous_path,
                }),
                encoding="ascii",
            )
            (transaction.parent / ".release-operation.json").chmod(0o600)
        else:
            current_release = old_release
        current_link = app_dir / "current"
        current_link.symlink_to(current_release)
        if fake.operation == "rollback":
            (app_dir / "previous").symlink_to(previous_release)
        actual_effective_uid = os.geteuid()
        actual_effective_gid = os.getegid()
        effective_uid_patch = patch.object(probe.os, "geteuid", return_value=0)
        with (
            patch.object(probe, "APP_DIR", app_dir),
            patch.object(probe, "SHARED_DIR", transaction.parent),
            patch.object(probe, "TRANSACTION", transaction),
            patch.object(probe, "SYSTEMD_RECEIPT", companion),
            patch.object(probe, "CURRENT_LINK", current_link),
            patch.object(probe, "SHARED_PYTHON", app_dir / "shared/venv/bin/python"),
            patch.object(probe, "_load_pinned_module", return_value=fake),
            patch.object(probe, "_health", return_value={"api_live": True, "api_ready": True, "web_local": True, "public": True}),
            effective_uid_patch,
            patch.object(probe, "_private_report_owner_uid", return_value=actual_effective_uid),
            patch.object(probe, "_trusted_state_owner", return_value=(actual_effective_uid, actual_effective_gid)),
        ):
            return probe.probe(
                fake.__verified_sha256__,
                diagnostic_systemd_state=diagnostic_systemd_state,
            )

    def test_absent_companion_runs_read_only_getters_and_fails_at_exact_unit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transaction, companion = self._paths(Path(directory))
            fake = FakeSystemdState(fail_query=("active", "deadlock-api.service"))
            with self.assertRaises(probe.ProbeFailure) as caught:
                self._run(fake, transaction, companion)
            self.assertEqual(caught.exception.check, "unit_active_deadlock-api_service")
            self.assertEqual(
                fake.events,
                ["systemctl_path", "transaction", "helper_digests", "capture_context", "unit_snapshot", "active:deadlock-api.service"],
            )
            self.assertFalse(companion.exists())

    def test_absent_companion_happy_path_runs_both_read_only_unit_getters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transaction, companion = self._paths(Path(directory))
            fake = FakeSystemdState()
            result = self._run(fake, transaction, companion)
            self.assertEqual(result["capture_predicate"], "prewrite_predicates_passed")
            self.assertEqual(result["service_active_count"], 1)
            self.assertEqual(result["service_enabled_count"], 1)
            self.assertEqual(
                fake.events,
                ["systemctl_path", "transaction", "helper_digests", "capture_context", "unit_snapshot", "active:deadlock-api.service", "enabled:deadlock-api.service"],
            )
            self.assertFalse(companion.exists())

    def test_probe_uses_actual_pinned_unit_snapshot_with_fixed_unit_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transaction, companion = self._paths(Path(directory))
            app_dir = transaction.parents[1]
            releases = app_dir / "releases"
            releases.mkdir()
            old_release = releases / "old"
            old_release.mkdir()
            (old_release / "RELEASE.json").write_text('{"source_git_commit":"' + "a" * 40 + '"}\n', encoding="ascii")
            current_link = app_dir / "current"
            current_link.symlink_to(old_release)
            state_tool = systemd_state
            events: list[tuple[str, str]] = []
            active_override = {"deadlock-api.service": "active"}
            transaction_data = ("a" * 32, "install", str(old_release), "", {}, {}, active_override)
            state_tool.__verified_sha256__ = "b" * 64
            with (
                patch.object(probe, "APP_DIR", app_dir),
                patch.object(probe, "CURRENT_LINK", current_link),
                patch.object(probe, "TRANSACTION", transaction),
                patch.object(probe, "SYSTEMD_RECEIPT", companion),
                patch.object(probe, "_load_pinned_module", return_value=state_tool),
                patch.object(state_tool, "_validate_systemctl_path", side_effect=lambda value: value),
                patch.object(state_tool, "_read_transaction", return_value=transaction_data),
                patch.object(state_tool, "_helper_digests", return_value={"tools/platform_release_systemd_state.py": "b" * 64}),
                patch.object(state_tool, "_release_context", return_value=True),
                patch.object(state_tool, "_read_active", side_effect=lambda unit: events.append(("active", unit)) or "active"),
                patch.object(state_tool, "_read_enabled", side_effect=lambda unit: events.append(("enabled", unit)) or "enabled"),
                patch.object(probe, "_health", return_value={"api_live": True, "api_ready": True, "web_local": True, "public": True}),
                patch.object(probe.os, "geteuid", return_value=0),
            ):
                result = probe.probe("b" * 64)
            self.assertEqual(result["service_active_count"], len(systemd_state.OWNED_UNITS))
            self.assertEqual(result["service_enabled_count"], len(systemd_state.OWNED_UNITS))
            self.assertEqual(len([event for event in events if event[0] == "active"]), len(systemd_state.OWNED_UNITS) - 1)
            self.assertEqual(len([event for event in events if event[0] == "enabled"]), len(systemd_state.OWNED_UNITS))
            self.assertTrue(all(unit in systemd_state.OWNED_UNITS for _, unit in events))
        self._assert_actual_pinned_active_getter_reports_only_closed_failure_observation()

    def _assert_actual_pinned_active_getter_reports_only_closed_failure_observation(self) -> None:
        cases = (
            ("failed", 3, "unsupported_state", "failed", 3),
            ("active", 3, "status_mismatch", "active", 3),
            ("active", -1, "status_mismatch", "active", "unavailable"),
            ("active\ninactive", 3, "malformed_output", "multiple", "unavailable"),
            ("raw private command output", OSError("raw private command output"), "command_error", "command_error", "unavailable"),
        )
        target = "deadlock-maintenance.service"
        for stdout, returncode, expected_result, expected_state, expected_rc in cases:
            with self.subTest(expected_result=expected_result), tempfile.TemporaryDirectory() as directory:
                transaction, companion = self._paths(Path(directory))
                app_dir = transaction.parents[1]
                releases = app_dir / "releases"
                releases.mkdir()
                old_release = releases / "old"
                old_release.mkdir()
                (old_release / "RELEASE.json").write_text(
                    '{"source_git_commit":"' + "a" * 40 + '"}\n', encoding="ascii"
                )
                current_link = app_dir / "current"
                current_link.symlink_to(old_release)
                overrides = {unit: "active" for unit in systemd_state.OWNED_UNITS if unit != target}
                transaction_data = ("a" * 32, "install", str(old_release), "", {}, {}, overrides)
                state_tool = systemd_state
                state_tool.__verified_sha256__ = "b" * 64
                systemctl_calls: list[tuple[str, ...]] = []

                def fake_systemctl(args, **kwargs):
                    call = tuple(args[1:])
                    systemctl_calls.append(call)
                    if call == ("is-active", target):
                        if isinstance(returncode, BaseException):
                            raise returncode
                        return SimpleNamespace(returncode=returncode, stdout=stdout)
                    if call[0] == "is-enabled":
                        return SimpleNamespace(returncode=0, stdout="enabled\n")
                    self.fail(f"unexpected systemctl query: {call!r}")

                with (
                    patch.object(probe, "APP_DIR", app_dir),
                    patch.object(probe, "CURRENT_LINK", current_link),
                    patch.object(probe, "TRANSACTION", transaction),
                    patch.object(probe, "SYSTEMD_RECEIPT", companion),
                    patch.object(probe, "_load_pinned_module", return_value=state_tool),
                    patch.object(state_tool, "_validate_systemctl_path", side_effect=lambda value: value),
                    patch.object(state_tool, "_read_transaction", return_value=transaction_data),
                    patch.object(
                        state_tool,
                        "_helper_digests",
                        return_value={"tools/platform_release_systemd_state.py": "b" * 64},
                    ),
                    patch.object(state_tool, "_release_context", return_value=True),
                    patch.object(state_tool.subprocess, "run", side_effect=fake_systemctl),
                    patch.object(probe, "_health", return_value={"api_live": True, "api_ready": True, "web_local": True, "public": True}),
                    patch.object(probe.os, "geteuid", return_value=0),
                ):
                    with self.assertRaises(probe.ProbeFailure) as caught:
                        probe.probe("b" * 64)

                self.assertEqual(caught.exception.check, "unit_active_deadlock-maintenance_service")
                self.assertEqual(caught.exception.error_class, "StateError")
                self.assertEqual(
                    caught.exception.systemd_observation,
                    {
                        "command": "is-active",
                        "unit": target,
                        "result": expected_result,
                        "returncode": expected_rc,
                        "state": expected_state,
                    },
                )
                self.assertEqual(systemctl_calls.count(("is-active", target)), 1)
                self.assertNotIn("raw private command output", json.dumps(caught.exception.systemd_observation))

    def test_enabled_query_failure_reports_exact_unit_and_getter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transaction, companion = self._paths(Path(directory))
            fake = FakeSystemdState(fail_query=("enabled", "deadlock-api.service"))
            with self.assertRaises(probe.ProbeFailure) as caught:
                self._run(fake, transaction, companion)
            self.assertEqual(caught.exception.check, "unit_enabled_deadlock-api_service")

    def test_diagnostic_systemd_state_emits_private_success_without_runtime_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transaction, companion = self._paths(Path(directory))
            companion.write_text("fixture", encoding="ascii")
            fake = FakeSystemdState(
                operation="rollback", transaction_operation="install",
                phase="recovery-restored",
            )
            fake.OWNED_UNITS = systemd_state.OWNED_UNITS
            emitted = BytesIO()
            with (
                patch.object(probe, "_private_capture_requested", return_value=True),
                patch.object(probe.sys, "stderr", SimpleNamespace(buffer=emitted)),
                patch.object(
                    probe,
                    "_runtime_payload_integrity",
                    side_effect=AssertionError("diagnostic route must skip the payload gate"),
                ),
            ):
                result = self._run(
                    fake,
                    transaction,
                    companion,
                    diagnostic_systemd_state=True,
                    release_source=probe.DIAGNOSTIC_TARGET_SOURCE_SHA,
                )

            self.assertEqual(
                result,
                {
                    "schema": 1,
                    "kind": "release_capture_probe",
                    "status": "passed",
                    "diagnostic_systemd_state": "passed",
                    "diagnostic_systemd_state_returncode": 0,
                },
            )
            self.assertEqual(fake.events.count("validate"), 1)
            self.assertEqual(fake.events.count("verify"), 1)
            self.assertNotIn("restore", fake.events)
            transaction_record = json.loads(transaction.read_text(encoding="ascii"))
            self.assertEqual(transaction_record["operation"], "install")
            self.assertEqual(transaction_record["phase"], "recovery-restored")
            self.assertTrue(transaction_record["current_before"])
            self.assertTrue(transaction_record["previous_before"])
            self.assertEqual(fake._read_receipt(companion)["operation"], "rollback")
            marker_line = emitted.getvalue().decode("ascii").strip()
            self.assertTrue(marker_line.startswith("RUNTIME_PRIVATE_CONTEXT "))
            marker = json.loads(marker_line.removeprefix("RUNTIME_PRIVATE_CONTEXT "))
            self.assertEqual(marker["component"], "systemd_state_probe")
            self.assertEqual(marker["returncode"], 0)
            detail = json.loads(base64.b64decode(marker["output_b64"]))
            self.assertTrue(detail["collection_completed"])
            self.assertEqual(detail["validate_status"], "passed")
            self.assertEqual(detail["verify_status"], "passed")
            self.assertEqual(detail["current_facts_status"], "matched")
            self.assertFalse(detail["historical_cause_proven"])
            self.assertEqual(len(detail["expected_units"]), 13)
            self.assertEqual(detail["expected_units"], detail["observed_units"])
            self.assertNotIn("expected_units", json.dumps(result))


    def test_diagnostic_systemd_state_preserves_bounded_private_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transaction, companion = self._paths(Path(directory))
            companion.write_text("fixture", encoding="ascii")
            fake = FakeSystemdState(operation="rollback")
            fake.OWNED_UNITS = systemd_state.OWNED_UNITS
            fake.snapshot_active = "inactive"
            emitted = BytesIO()
            with (
                patch.object(probe, "_private_capture_requested", return_value=True),
                patch.object(probe.sys, "stderr", SimpleNamespace(buffer=emitted)),
            ):
                result = self._run(
                    fake,
                    transaction,
                    companion,
                    diagnostic_systemd_state=True,
                    release_source=probe.DIAGNOSTIC_TARGET_SOURCE_SHA,
                )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["failed_check"], "diagnostic_mismatch")
            self.assertEqual(result["diagnostic_systemd_state"], "mismatch")
            self.assertEqual(result["diagnostic_systemd_state_returncode"], 1)
            marker_line = emitted.getvalue().decode("ascii").strip()
            marker = json.loads(marker_line.removeprefix("RUNTIME_PRIVATE_CONTEXT "))
            self.assertEqual(marker["component"], "systemd_state_probe")
            self.assertEqual(marker["returncode"], 1)
            detail = json.loads(base64.b64decode(marker["output_b64"]))
            self.assertTrue(detail["collection_completed"])
            self.assertEqual(detail["verify_status"], "failed")
            self.assertEqual(detail["current_facts_status"], "mismatch")
            self.assertEqual(detail["mismatch_count"], 13)
            self.assertEqual(detail["observed_units"][0]["active"], "inactive")
            self.assertFalse(detail["historical_cause_proven"])
            self.assertNotIn("inactive", json.dumps(result))

    def test_diagnostic_systemd_state_fails_closed_for_unbound_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transaction, companion = self._paths(Path(directory))
            companion.write_text("fixture", encoding="ascii")
            fake = FakeSystemdState(operation="rollback")
            fake.OWNED_UNITS = systemd_state.OWNED_UNITS
            emitted = BytesIO()
            with (
                patch.object(probe, "_private_capture_requested", return_value=True),
                patch.object(probe.sys, "stderr", SimpleNamespace(buffer=emitted)),
            ):
                result = self._run(
                    fake,
                    transaction,
                    companion,
                    diagnostic_systemd_state=True,
                    release_source="b" * 40,
                )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["diagnostic_systemd_state"], "unavailable")
            self.assertEqual(result["diagnostic_systemd_state_returncode"], "unavailable")
            detail = json.loads(
                base64.b64decode(
                    json.loads(
                        emitted.getvalue().decode("ascii").strip().removeprefix("RUNTIME_PRIVATE_CONTEXT ")
                    )["output_b64"]
                )
            )
            self.assertEqual(detail["validate_status"], "passed")
            self.assertEqual(detail["verify_status"], "unavailable")
            self.assertFalse(detail["collection_completed"])
            self.assertEqual(detail["expected_units"], [])
            self.assertEqual(detail["observed_units"], [])
            self.assertNotIn("unit_snapshot", fake.events)
            self.assertNotIn("verify", fake.events)

    def test_existing_companion_uses_only_existing_read_validator(self) -> None:
        expected_files = {"platform/tools/helper.py": "a" * 64, "source-sha": "b" * 64}
        matches, result = probe._compare_expected_payload(
            "c" * 64,
            expected_files,
            {"files": dict(expected_files), "payload_tree_sha256": "c" * 64},
        )
        self.assertTrue(matches)
        self.assertEqual(result["runtime_expected_tree"], "passed")
        for actual_files, actual_digest, missing, extra, changed in (
            ({"source-sha": "b" * 64}, "c" * 64, 1, 0, 0),
            ({**expected_files, "runtime/extra": "d" * 64}, "e" * 64, 0, 1, 0),
            ({**expected_files, "source-sha": "e" * 64}, "e" * 64, 0, 0, 1),
        ):
            with self.subTest(expected_tree=(missing, extra, changed)):
                matches, result = probe._compare_expected_payload(
                    "c" * 64,
                    expected_files,
                    {"files": actual_files, "payload_tree_sha256": actual_digest},
                )
                self.assertFalse(matches)
                self.assertEqual(result["runtime_expected_tree"], "failed")
                details = result["private_detail"]
                self.assertEqual((details["missing_count"], details["extra_count"], details["changed_count"]), (missing, extra, changed))

        mismatch_sha = "9ceaf09ab7ba4bf5c5e636865106910eaa034c8d"
        mismatch_target = Path("/releases/immutable-candidate")
        mismatch_manifest = {
            "source_sha": mismatch_sha,
            "release_slug": mismatch_target.name,
            "files": {"runtime/actual.py": "b" * 64},
            "payload_tree_sha256": "d" * 64,
        }
        mismatch_installer = SimpleNamespace(
            _active_release=lambda _app_dir: (mismatch_target, mismatch_sha, mismatch_target.name),
            _read_manifest=lambda: mismatch_manifest,
            _validate_runtime_source=lambda _root, *, source_sha: {"version": 1},
            verify=lambda _app_dir, _source_sha: mismatch_manifest,
        )
        with patch.object(probe, "APP_DIR", Path("/opt/oldsparky/platform")), patch.object(
            probe, "_load_pinned_runtime_installer", return_value=mismatch_installer
        ), patch.object(
            probe, "_expected_liveqa_payload_tree", return_value=("c" * 64, {"runtime/expected.py": "a" * 64})
        ):
            evidence, failed_stage, private_detail = probe._runtime_payload_integrity(
                mismatch_target,
                source_sha=mismatch_sha,
                expected_helper_sha256="e" * 64,
                deadline=probe.time.monotonic() + 30,
            )
        self.assertEqual(failed_stage, "runtime_expected_tree")
        self.assertEqual(evidence["runtime_payload_verify"], "passed")
        self.assertEqual(evidence["runtime_expected_tree"], "failed")
        self.assertEqual(private_detail["reason"], "expected_tree_mismatch")

        source_sha = "9ceaf09ab7ba4bf5c5e636865106910eaa034c8d"
        cache_source_files = {
            "platform/tools/__init__.py": "a" * 64,
            **{
                f"platform/tools/module_{index:02d}.py": f"{index:064x}"
                for index in range(57)
            },
        }
        cache_missing = [
            f"platform/tools/__pycache__/module_{index:02d}.cpython-312.pyc"
            for index in range(57)
        ]
        cache_expected_files = {
            **cache_source_files,
            **{path: "c" * 64 for path in cache_missing},
        }
        cache_manifest = {
            "source_sha": source_sha,
            "release_slug": mismatch_target.name,
            "files": dict(cache_source_files),
            "payload_tree_sha256": "d" * 64,
        }
        cache_installer = SimpleNamespace(
            _active_release=lambda _app_dir: (mismatch_target, source_sha, mismatch_target.name),
            _read_manifest=lambda: cache_manifest,
            _validate_runtime_source=lambda _root, *, source_sha: {"version": 1},
            verify=lambda _app_dir, _source_sha: cache_manifest,
        )
        self.assertTrue(
            probe._all_missing_python_bytecode(
                cache_missing, cache_source_files, source_sha=source_sha
            )
        )
        self.assertFalse(
            probe._all_missing_python_bytecode(
                [*cache_missing, "platform/tools/helper.py"],
                cache_source_files,
                source_sha=source_sha,
            )
        )
        self.assertFalse(
            probe._all_missing_python_bytecode(
                ["platform/tools/__pycache__/unpaired.cpython-311.pyc"],
                cache_source_files,
                source_sha=source_sha,
            )
        )
        with patch.object(probe, "APP_DIR", Path("/opt/oldsparky/platform")), patch.object(
            probe, "_load_pinned_runtime_installer", return_value=cache_installer
        ), patch.object(
            probe,
            "_expected_liveqa_payload_tree",
            return_value=("c" * 64, cache_expected_files),
        ):
            cache_evidence, cache_stage, cache_detail = probe._runtime_payload_integrity(
                mismatch_target,
                source_sha=source_sha,
                expected_helper_sha256="e" * 64,
                deadline=probe.time.monotonic() + 30,
            )
        self.assertEqual(cache_stage, "runtime_expected_tree")
        self.assertEqual(cache_evidence["runtime_payload_verify"], "passed")
        self.assertEqual(cache_detail["missing_count"], 57)
        self.assertEqual(len(cache_detail["missing"]), 57)
        self.assertFalse(cache_detail["missing_paths_truncated"])
        self.assertTrue(cache_detail["all_missing_python_bytecode"])
        self.assertEqual(cache_detail["missing_paths_source_sha"], mismatch_sha)
        many_missing = {
            f"platform/tools/module_{index:02d}.py": f"{index:064x}"
            for index in range(70)
        }
        matches, bounded = probe._compare_expected_payload(
            "c" * 64,
            many_missing,
            {"files": {}, "payload_tree_sha256": "d" * 64},
        )
        self.assertFalse(matches)
        bounded_detail = bounded["private_detail"]
        self.assertEqual(bounded_detail["missing_count"], 70)
        self.assertEqual(len(bounded_detail["missing"]), 64)
        self.assertTrue(bounded_detail["missing_paths_truncated"])
        self.assertLessEqual(
            len(json.dumps(bounded_detail, separators=(",", ":")).encode("ascii")),
            15 * 1024,
        )

        source_sha = "9ceaf09ab7ba4bf5c5e636865106910eaa034c8d"
        for runtime_version in (1, 2):
            with self.subTest(runtime_version=runtime_version), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                release = root / "release"
                (release / "tools").mkdir(parents=True)
                (release / "apps/platform_api/empty/nested").mkdir(parents=True)
                (release / "apps/platform_api/__pycache__").mkdir()
                (release / "deploy").mkdir()
                (release / "tools/helper.py").write_bytes(b"pinned helper\n")
                (release / "apps/platform_api/app.py").write_bytes(b"immutable API source\n")
                (release / "apps/platform_api/__pycache__/app.cpython-312.pyc").write_bytes(b"generated source cache\n")
                (release / "deploy/policy").write_bytes(b"policy\n")
                runtime_source = release / "liveqa-runtime"
                (runtime_source / "web/tests/empty").mkdir(parents=True)
                (runtime_source / "web/__pycache__").mkdir()
                (runtime_source / "node/bin").mkdir(parents=True)
                (runtime_source / "web/package-lock.json").write_bytes(b"lock\n")
                (runtime_source / "web/runtime.py").write_bytes(b"runtime source\n")
                (runtime_source / "web/__pycache__/runtime.cpython-312.pyc").write_bytes(b"runtime cache\n")
                (runtime_source / "web/tests/smoke.spec").write_bytes(b"runtime v" + str(runtime_version).encode() + b"\n")
                sandbox_rel = runtime_installer.RUNTIME_SANDBOX_RELATIVE
                sandbox_path = runtime_source / sandbox_rel
                sandbox_path.parent.mkdir(parents=True, exist_ok=True)
                sandbox_bytes = b"fixture sandbox\n"
                if runtime_version == 1:
                    sandbox_path.write_bytes(sandbox_bytes)
                    sandbox_path.chmod(0o4755)
                if runtime_version == 2:
                    (runtime_source / "node/bin/node").write_bytes(b"node\n")
                for path in release.rglob("*"):
                    if path.is_dir():
                        path.chmod(0o555)
                    elif path != sandbox_path:
                        path.chmod(0o444)

                original_metadata = runtime_installer._metadata

                def trusted_fixture_metadata(path: Path):
                    actual = original_metadata(path)
                    return SimpleNamespace(
                        st_dev=actual.st_dev,
                        st_ino=actual.st_ino,
                        st_mode=actual.st_mode,
                        st_uid=0,
                        st_gid=0,
                        st_nlink=actual.st_nlink,
                        st_size=actual.st_size,
                        st_mtime_ns=actual.st_mtime_ns,
                    )

                provider = None
                runtime_manifest = {"version": runtime_version}
                patchers = [
                    patch.object(runtime_installer, "TOOL_FILES", ("helper.py",)),
                    patch.object(runtime_installer, "SOURCE_TREES", ("apps/platform_api",)),
                    patch.object(runtime_installer, "SOURCE_FILES", ("deploy/policy",)),
                    patch.object(runtime_installer, "_metadata", side_effect=trusted_fixture_metadata),
                ]
                if runtime_version == 1:
                    patchers.extend(
                        [
                            patch.object(runtime_installer, "CHROMIUM_SANDBOX_SIZE", len(sandbox_bytes)),
                            patch.object(runtime_installer, "CHROMIUM_SANDBOX_SHA256", hashlib.sha256(sandbox_bytes).hexdigest()),
                        ]
                    )
                else:
                    provider = {
                        "version": 1,
                        "provider_sha": "a" * 40,
                        "engine_tree_sha256": "b" * 64,
                        "node_version": "26.3.1",
                        "package_lock_sha256": "c" * 64,
                        "engine_files": {"node/bin/node": "d" * 64},
                    }
                with patchers[0], patchers[1], patchers[2], patchers[3]:
                    extra = patchers[4:] if len(patchers) > 4 else []
                    for patcher in extra:
                        patcher.start()
                    try:
                        expected_digest, expected_files = probe._expected_liveqa_payload_tree(
                            runtime_installer,
                            release=release,
                            source_sha=source_sha,
                            runtime_provider=provider,
                            runtime_manifest=runtime_manifest,
                            deadline=probe.time.monotonic() + 30,
                        )
                        self.assertNotIn(
                            "platform/apps/platform_api/__pycache__/app.cpython-312.pyc",
                            expected_files,
                        )
                        self.assertIn(
                            "runtime/web/__pycache__/runtime.cpython-312.pyc",
                            expected_files,
                        )
                        legacy_patch = patch.object(
                            runtime_installer, "SOURCE_TREE_CACHE_POLICY", None
                        )
                        with legacy_patch:
                            _, legacy_files = probe._expected_liveqa_payload_tree(
                                runtime_installer,
                                release=release,
                                source_sha=source_sha,
                                runtime_provider=provider,
                                runtime_manifest=runtime_manifest,
                                deadline=probe.time.monotonic() + 30,
                            )
                        self.assertIn(
                            "platform/apps/platform_api/__pycache__/app.cpython-312.pyc",
                            legacy_files,
                        )
                        unknown_policy = patch.object(
                            runtime_installer, "SOURCE_TREE_CACHE_POLICY", "unknown-policy"
                        )
                        with unknown_policy, self.assertRaises(probe.ProbeFailure) as failure:
                            probe._expected_liveqa_payload_tree(
                                runtime_installer,
                                release=release,
                                source_sha=source_sha,
                                runtime_provider=provider,
                                runtime_manifest=runtime_manifest,
                                deadline=probe.time.monotonic() + 30,
                            )
                        self.assertEqual(
                            (failure.exception.check, failure.exception.error_class),
                            ("runtime_expected_tree", "cache_policy"),
                        )
                        payload = root / "payload"
                        for relative in ("platform", "platform/tools", "platform/apps", "platform/apps/platform_api", "platform/apps/platform_api/empty", "platform/apps/platform_api/empty/nested", "runtime", "runtime/web", "runtime/web/tests", "runtime/web/tests/empty", "runtime/node", "runtime/node/bin", "runtime/browsers", "runtime/browsers/chromium-1228", "runtime/browsers/chromium-1228/chrome-linux64"):
                            (payload / relative).mkdir(parents=True, exist_ok=True)
                        source_map = {
                            "platform/tools/helper.py": release / "tools/helper.py",
                            "platform/apps/platform_api/app.py": release / "apps/platform_api/app.py",
                            "platform/deploy/policy": release / "deploy/policy",
                            "runtime/web/package-lock.json": runtime_source / "web/package-lock.json",
                            "runtime/web/runtime.py": runtime_source / "web/runtime.py",
                            "runtime/web/__pycache__/runtime.cpython-312.pyc": runtime_source / "web/__pycache__/runtime.cpython-312.pyc",
                            "runtime/web/tests/smoke.spec": runtime_source / "web/tests/smoke.spec",
                        }
                        if runtime_version == 1:
                            source_map["runtime/" + sandbox_rel.as_posix()] = sandbox_path
                        else:
                            source_map["runtime/node/bin/node"] = runtime_source / "node/bin/node"
                            provider_record = {**provider, "source_sha": source_sha}
                            generated = (json.dumps(provider_record, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
                            (payload / "runtime-provider.json").write_bytes(generated)
                        for relative, source in source_map.items():
                            target = payload / relative
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_bytes(source.read_bytes())
                            target.chmod(0o4755 if relative == "runtime/" + sandbox_rel.as_posix() else 0o444)
                        (payload / "source-sha").write_text(source_sha + "\n", encoding="ascii")
                        (payload / "source-sha").chmod(0o444)
                        for path in sorted(payload.rglob("*"), reverse=True):
                            if path.is_dir():
                                path.chmod(0o555)
                        payload.chmod(0o555)
                        tree_digest, actual_files = runtime_installer._tree_digest(
                            payload,
                            sandbox_relative=runtime_installer.CHROMIUM_SANDBOX_RELATIVE,
                        )
                        matches, evidence = probe._compare_expected_payload(
                            expected_digest,
                            expected_files,
                            {"files": actual_files, "payload_tree_sha256": tree_digest},
                        )
                    finally:
                        for patcher in reversed(extra):
                            patcher.stop()
                self.assertEqual(actual_files, expected_files)
                expected_directories = {"platform", "platform/tools", "runtime"}
                for relative in expected_files:
                    parent = Path(relative).parent
                    while str(parent) != ".":
                        expected_directories.add(parent.as_posix())
                        parent = parent.parent
                expected_directories.update(
                    {
                        "platform/apps/platform_api/empty",
                        "platform/apps/platform_api/empty/nested",
                        "runtime/web/tests/empty",
                        "runtime/node",
                        "runtime/node/bin",
                    }
                )
                self.assertNotIn(
                    "platform/apps/platform_api/__pycache__", expected_directories
                )
                self.assertIn("runtime/web/__pycache__", expected_directories)
                actual_directories = {
                    path.relative_to(payload).as_posix()
                    for path in payload.rglob("*")
                    if path.is_dir()
                }
                expected_directories.update(
                    {
                        "runtime/browsers",
                        "runtime/browsers/chromium-1228",
                        "runtime/browsers/chromium-1228/chrome-linux64",
                    }
                )
                self.assertEqual(actual_directories, expected_directories)
                reconstructed_digest = hashlib.sha256()
                for relative in sorted(expected_files.keys() | expected_directories, key=runtime_installer._runtime_path_order_key):
                    reconstructed_digest.update(relative.encode("utf-8") + b"\0")
                    if relative in expected_directories:
                        reconstructed_digest.update(b"d\0")
                    else:
                        reconstructed_digest.update(b"f\0" + bytes.fromhex(expected_files[relative]))
                self.assertEqual(reconstructed_digest.hexdigest(), tree_digest)
                self.assertTrue(matches, evidence)
                self.assertEqual(expected_digest, tree_digest)
                self.assertIn("source-sha", expected_files)
                self.assertEqual("runtime-provider.json" in expected_files, runtime_version == 2)
                self.assertEqual("runtime/" + sandbox_rel.as_posix() in expected_files, runtime_version == 1)
                self.assertNotIn("platform/apps/platform_api/empty/nested", expected_files)
                self.assertNotIn("runtime/web/tests/empty", expected_files)

        private_details: list[tuple[str, dict[str, object]]] = []
        slow_module = SimpleNamespace(_unit_snapshot=lambda _overrides: probe.time.sleep(0.2))
        with patch.object(probe, "_private_capture_requested", return_value=True), patch.object(
            probe,
            "_private_runtime_detail",
            side_effect=lambda component, detail: private_details.append((component, detail)),
        ):
            with self.assertRaises(probe.ProbeFailure) as caught:
                probe._unit_snapshot_with_deadline(
                    slow_module,
                    deadline=probe.time.monotonic() + 0.02,
                    result={},
                )
        self.assertEqual((caught.exception.check, caught.exception.error_class), ("unit_snapshot", "timeout"))
        self.assertEqual(private_details[0][0], "systemd_unit_snapshot")
        with tempfile.TemporaryDirectory() as directory:
            transaction, companion = self._paths(Path(directory))
            companion.write_text("fixture", encoding="ascii")
            fake = FakeSystemdState(operation="rollback")
            fake.OWNED_UNITS = systemd_state.OWNED_UNITS
            nginx_calls: list[dict[str, object]] = []
            with patch.object(
                probe.subprocess,
                "run",
                side_effect=lambda argv, **kwargs: nginx_calls.append({"argv": argv, **kwargs}) or SimpleNamespace(returncode=0),
            ), patch.object(
                probe,
                "_private_capture_requested",
                return_value=False,
            ), patch.object(
                probe,
                "_runtime_payload_integrity",
                return_value=(
                    {
                        "runtime_payload_verify": "passed",
                        "runtime_payload_verify_returncode": 0,
                        "runtime_expected_tree": "passed",
                        "runtime_expected_tree_returncode": 0,
                    },
                    None,
                    None,
                ),
            ), patch.object(
                probe,
                "_read_stable_state",
                return_value={
                    "operation_id": "a" * 32,
                    "operation": "rollback",
                    "phase": "rollback-runtime-pending",
                    "current_before": str(transaction.parents[1] / "releases/old"),
                    "previous_before": str(transaction.parents[1] / "releases/pending"),
                },
            ):
                result = self._run(fake, transaction, companion)
            self.assertEqual(result["capture_predicate"], "existing_receipt_valid")
            self.assertEqual(result["receipt_present"], True)
            self.assertEqual(result["current_bound"], True)
            self.assertEqual(result["previous_bound"], True)
            self.assertEqual(result["runtime_target"], "old")
            self.assertEqual(result["runtime_target_source"], "a" * 40)
            self.assertEqual(result["runtime_target_helper_sha256"], "c" * 64)
            self.assertEqual(result["runtime_payload_verify"], "passed")
            self.assertEqual(result["runtime_expected_tree"], "passed")
            self.assertEqual(result["nginx_preflight"], "passed")
            self.assertEqual(result["nginx_preflight_returncode"], 0)
            self.assertEqual(result["service_active_count"], 13)
            self.assertEqual(result["service_enabled_count"], 13)
            self.assertIn("validate", fake.events)
            self.assertIn("receipt_read", fake.events)
            self.assertEqual(len(nginx_calls), 1)
            self.assertEqual(
                nginx_calls[0]["argv"],
                [str(transaction.parents[1] / "shared/venv/bin/python"), str(transaction.parents[1] / "releases/old/tools/platform_install_nginx.py"), "--json"],
            )
            self.assertEqual(nginx_calls[0]["env"], {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "PLATFORM_APP_DIR": str(transaction.parents[1]), "PYTHONDONTWRITEBYTECODE": "1"})
            self.assertEqual(nginx_calls[0]["stdout"], subprocess.DEVNULL)
            self.assertEqual(nginx_calls[0]["stderr"], subprocess.DEVNULL)
            self.assertEqual(nginx_calls[0]["timeout"], 60)
            self.assertFalse(any(flag in nginx_calls[0]["argv"] for flag in ("--apply", "--reload")))
            self.assertNotIn("--nginx-bin", nginx_calls[0]["argv"])
            self.assertFalse(any("platform_install_systemd_units" in str(arg) for arg in nginx_calls[0]["argv"]))

        with tempfile.TemporaryDirectory() as directory:
            transaction, companion = self._paths(Path(directory))
            companion.write_text("fixture", encoding="ascii")
            fake = FakeSystemdState(
                operation="rollback", transaction_operation="install", phase="recovery-restored",
            )
            fake.OWNED_UNITS = systemd_state.OWNED_UNITS
            nginx_calls: list[dict[str, object]] = []
            with patch.object(
                probe.subprocess,
                "run",
                side_effect=lambda argv, **kwargs: nginx_calls.append({"argv": argv, **kwargs}) or SimpleNamespace(returncode=0),
            ), patch.object(
                probe,
                "_private_capture_requested",
                return_value=False,
            ), patch.object(
                probe,
                "_runtime_payload_integrity",
                return_value=(
                    {
                        "runtime_payload_verify": "passed",
                        "runtime_payload_verify_returncode": 0,
                        "runtime_expected_tree": "passed",
                        "runtime_expected_tree_returncode": 0,
                    },
                    None,
                    None,
                ),
            ), patch.object(
                probe,
                "_read_stable_state",
                return_value={
                    "operation_id": "a" * 32,
                    "operation": "install",
                    "phase": "recovery-restored",
                    "current_before": str(transaction.parents[1] / "releases/old"),
                    "previous_before": str(transaction.parents[1] / "releases/pending"),
                },
            ):
                result = self._run(fake, transaction, companion)
            self.assertEqual(result["runtime_target"], "old")
            self.assertEqual(result["runtime_target_source"], "a" * 40)
            self.assertEqual(len(nginx_calls), 1)
            self.assertEqual(nginx_calls[0]["argv"][1], str(transaction.parents[1] / "releases/old/tools/platform_install_nginx.py"))
            self.assertEqual(fake._read_receipt(companion)["operation"], "rollback")

        with tempfile.TemporaryDirectory() as directory:
            transaction, companion = self._paths(Path(directory))
            companion.write_text("fixture", encoding="ascii")
            fake = FakeSystemdState(operation="rollback")
            fake.OWNED_UNITS = systemd_state.OWNED_UNITS
            with patch.object(
                probe.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=17, stdout="private stdout", stderr="private stderr"),
            ), patch.object(
                probe,
                "_private_capture_requested",
                return_value=False,
            ), patch.object(
                probe,
                "_runtime_payload_integrity",
                return_value=(
                    {
                        "runtime_payload_verify": "passed",
                        "runtime_payload_verify_returncode": 0,
                        "runtime_expected_tree": "passed",
                        "runtime_expected_tree_returncode": 0,
                    },
                    None,
                    None,
                ),
            ), patch.object(
                probe,
                "_read_stable_state",
                return_value={
                    "operation_id": "a" * 32,
                    "operation": "rollback",
                    "phase": "rollback-runtime-pending",
                    "current_before": str(transaction.parents[1] / "releases/old"),
                    "previous_before": str(transaction.parents[1] / "releases/pending"),
                },
            ):
                with self.assertRaises(probe.ProbeFailure) as caught:
                    self._run(fake, transaction, companion)
            self.assertEqual(caught.exception.check, "nginx_preflight")
            self.assertEqual(caught.exception.error_class, "child_failure")
            self.assertEqual(caught.exception.safe_fields["nginx_preflight"], "failed")
            self.assertEqual(caught.exception.safe_fields["nginx_preflight_returncode"], 17)
            self.assertNotIn("private", json.dumps(caught.exception.safe_fields))

        with tempfile.TemporaryDirectory() as directory:
            transaction, companion = self._paths(Path(directory))
            companion.write_text("fixture", encoding="ascii")
            fake = FakeSystemdState(operation="rollback")
            fake.OWNED_UNITS = systemd_state.OWNED_UNITS
            private_text = b"private nginx failure detail"
            private_output = tempfile.TemporaryFile(mode="w+b")
            private_output.write(private_text)
            private_output.flush()
            emitted = BytesIO()
            with (
                patch.object(probe, "_private_capture_requested", return_value=True),
                patch.object(
                    probe,
                    "_run_private_preflight",
                    return_value=(17, len(private_text), private_output, 0, len(private_text), False),
                ),
                patch.object(
                    probe,
                    "_runtime_payload_integrity",
                    return_value=(
                        {
                            "runtime_payload_verify": "passed",
                            "runtime_payload_verify_returncode": 0,
                            "runtime_expected_tree": "passed",
                            "runtime_expected_tree_returncode": 0,
                        },
                        None,
                        None,
                    ),
                ),
                patch.object(probe.sys, "stderr", SimpleNamespace(buffer=emitted)),
                patch.object(
                    probe,
                    "_read_stable_state",
                    return_value={
                        "operation_id": "a" * 32,
                        "operation": "rollback",
                        "phase": "rollback-runtime-pending",
                        "current_before": str(transaction.parents[1] / "releases/old"),
                        "previous_before": str(transaction.parents[1] / "releases/pending"),
                    },
                ),
            ):
                with self.assertRaises(probe.ProbeFailure):
                    self._run(fake, transaction, companion)
            private_output.close()
            marker = emitted.getvalue().decode("ascii").strip()
            self.assertTrue(marker.startswith("RUNTIME_PRIVATE_CONTEXT "))
            context = json.loads(marker.removeprefix("RUNTIME_PRIVATE_CONTEXT "))
            self.assertEqual(
                set(context),
                {"schema", "route", "component", "guard_id", "returncode", "stream", "output_b64", "total_bytes", "truncated", "retained"},
            )
            self.assertEqual(context["schema"], 1)
            self.assertEqual(context["route"], "capture_probe")
            self.assertEqual(context["component"], "nginx_preflight")
            self.assertIsNone(context["guard_id"])
            self.assertEqual(context["returncode"], 17)
            self.assertEqual(context["stream"], "stderr")
            self.assertEqual(base64.b64decode(context["output_b64"]), private_text)
            self.assertEqual(context["total_bytes"], len(private_text))
            self.assertFalse(context["truncated"])
            self.assertEqual(context["retained"], "tail")

        install_target, install_path, install_validation = probe._receipt_runtime_target({
            "operation": "install",
            "phase": "recovery-restored",
            "current_before": "/opt/oldsparky/platform/releases/current-before",
            "previous_before": "/opt/oldsparky/platform/releases/previous-before",
        })
        self.assertEqual(install_target, "current_before")
        self.assertEqual(install_path, "/opt/oldsparky/platform/releases/current-before")
        self.assertEqual(install_validation, "current_before")

        for invalid_operation in ([], {}):
            with self.assertRaises(probe.ProbeFailure) as caught:
                probe._receipt_runtime_target({
                    "operation": invalid_operation,
                    "phase": "recovery-restored",
                    "current_before": "/opt/oldsparky/platform/releases/current-before",
                    "previous_before": "/opt/oldsparky/platform/releases/previous-before",
                })
            self.assertEqual(caught.exception.check, "existing_receipt_transaction")

        with tempfile.TemporaryDirectory() as directory:
            transaction, companion = self._paths(Path(directory))
            companion.write_text("fixture", encoding="ascii")
            fake = FakeSystemdState(operation="rollback", phase="unknown-runtime-phase")
            with patch.object(
                probe,
                "_read_stable_state",
                return_value={
                    "operation_id": "a" * 32,
                    "operation": "rollback",
                    "phase": "unknown-runtime-phase",
                    "current_before": str(transaction.parents[1] / "releases/old"),
                    "previous_before": str(transaction.parents[1] / "releases/pending"),
                },
            ):
                with self.assertRaises(probe.ProbeFailure) as caught:
                    self._run(fake, transaction, companion)
            self.assertEqual(caught.exception.check, "existing_receipt_phase")
            self.assertNotIn("unit_snapshot", fake.events)
            self.assertNotIn("validate", fake.events)

    def test_non_install_transaction_fails_before_helper_manifest_or_unit_queries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transaction, companion = self._paths(Path(directory))
            fake = FakeSystemdState(operation="rollback")
            with self.assertRaises(probe.ProbeFailure) as caught:
                self._run(fake, transaction, companion)
            self.assertEqual(caught.exception.check, "install_operation")
            self.assertEqual(fake.events, ["systemctl_path", "transaction"])

    def test_module_symlink_is_rejected_before_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            actual = root / "actual.py"
            actual.write_text("pass\n", encoding="ascii")
            link = root / "module.py"
            link.symlink_to(actual)
            with self.assertRaises(probe.ProbeFailure) as caught:
                probe._safe_module_bytes(link)
            self.assertEqual(caught.exception.check, "module_metadata")

    def test_companion_symlink_fails_before_loading_systemd_module(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transaction, companion = self._paths(Path(directory))
            companion.symlink_to(transaction)
            with (
                patch.object(probe, "APP_DIR", transaction.parents[1]),
                patch.object(probe, "SYSTEMD_RECEIPT", companion),
                patch.object(probe.os, "geteuid", return_value=0),
                patch.object(probe, "_load_pinned_module") as load_module,
            ):
                with self.assertRaises(probe.ProbeFailure) as caught:
                    probe.probe("a" * 64)
            self.assertEqual(caught.exception.check, "companion_metadata")
            load_module.assert_not_called()

    def test_untrusted_module_digest_is_rejected_before_execution(self) -> None:
        from types import SimpleNamespace

        fake_metadata = SimpleNamespace(st_dev=1, st_ino=2)
        payload = b"raise AssertionError('must not execute')\n"
        with patch.object(probe, "_safe_module_bytes", return_value=(payload, fake_metadata)):
            with self.assertRaises(probe.ProbeFailure) as caught:
                probe._load_pinned_module("0" * 64)
        self.assertEqual(caught.exception.check, "systemd_module_identity")

    def test_workflow_keeps_existing_mode_default_and_isolates_read_only_path(self) -> None:
        workflow = (Path(__file__).resolve().parents[2] / ".github/workflows/platform-production-diagnostics.yml").read_text(encoding="utf-8")
        self.assertIn("default: existing", workflow)
        self.assertIn("if: ${{ inputs.mode != 'capture-probe' }}", workflow)
        self.assertIn("if: ${{ inputs.mode == 'capture-probe' }}", workflow)
        probe_path = workflow.split("      - name: Stage read-only probe in a private task directory", 1)[1].split(
            "      - name: Remove production SSH material", 1
        )[0]
        self.assertIn("platform_release_lock_open", probe_path)
        self.assertIn("platform_release_lock_close", probe_path)
        self.assertIn("platform_release_capture_probe.py", workflow)
        self.assertIn("runtime_expected_tree", workflow)
        self.assertIn('set(observation) != {"command", "unit", "result", "returncode", "state"}', workflow)
        self.assertIn('result_class == "unsupported_state"', workflow)
        self.assertIn('result_class == "malformed_output"', workflow)
        encrypt_step = workflow.split(
            "      - name: Encrypt private read-only preflight failure context", 1
        )[1].split("\n      - name: Upload encrypted runtime failure context", 1)[0]
        self.assertIn('if [[ ! -s "$remote_error" ]]; then exit 0; fi', encrypt_step)
        encrypt_script = textwrap.dedent(encrypt_step.split("        run: |\n", 1)[1])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner_temp = root / "runner"
            runner_temp.mkdir()
            remote_error = runner_temp / "platform-production-capture-probe-error.log"
            github_output = root / "github-output"
            for payload, expected_rc in ((b"", 0), (b"ordinary ssh diagnostic without private marker\n", 2)):
                with self.subTest(remote_error_size=len(payload)):
                    github_output.unlink(missing_ok=True)
                    remote_error.write_bytes(payload)
                    remote_error.chmod(0o600)
                    result = subprocess.run(
                        ["bash", "-c", encrypt_script],
                        capture_output=True,
                        text=True,
                        check=False,
                        env={
                            **os.environ,
                            "RUNNER_TEMP": str(runner_temp),
                            "GITHUB_OUTPUT": str(github_output),
                            "GITHUB_RUN_ID": "12345",
                            "GITHUB_RUN_ATTEMPT": "1",
                            "GITHUB_SHA": "a" * 40,
                            "TARGET_SHA": "a" * 40,
                            "RECIPIENT_CERTIFICATE_PATH": str(root / "unused-recipient.pem"),
                        },
                    )
                    self.assertEqual(result.returncode, expected_rc, result.stderr)
                    self.assertEqual(
                        github_output.read_text(encoding="ascii"),
                        "private_failure_ready=false\n",
                    )
                    self.assertFalse((runner_temp / "platform-release-runtime-failure").exists())
        self._assert_probe_validator_rejects_inconsistent_systemd_observation(workflow)
        for forbidden in (
            "home_content.refresh_home_content",
            "capture_transaction",
            "_write_receipt",
            "systemctl start",
            "systemctl stop",
            "systemctl restart",
            "platform_release_lock_exec.sh",
        ):
            self.assertNotIn(forbidden, probe_path)
        self._assert_capture_probe_stage_feeds_heredoc_to_ssh_before_copy()
        self._assert_remote_cleanup_failure_still_removes_local_ssh_material()

    def _assert_probe_validator_rejects_inconsistent_systemd_observation(self, workflow: str) -> None:
        validator = workflow.split(
            '          /usr/bin/python3 - "$remote_log" "$remote_status" <<\'PY\'\n', 1
        )[1].split("\n          PY\n", 1)[0]
        validator = textwrap.dedent(validator)
        base = {
            "schema": 1,
            "kind": "release_capture_probe",
            "status": "failed",
            "failed_check": "unit_active_deadlock-maintenance_service",
            "error_class": "StateError",
            "health": {"api_live": True, "api_ready": True, "web_local": True, "public": True},
        }
        cases = (
            ({"command": "is-active", "unit": "deadlock-maintenance.service", "result": "status_mismatch", "returncode": 3, "state": "active"}, True, "StateError"),
            ({"command": "is-active", "unit": "deadlock-maintenance.service", "result": "status_mismatch", "returncode": 0, "state": "active"}, False, "StateError"),
            ({"command": "is-active", "unit": "deadlock-maintenance.service", "result": "status_mismatch", "returncode": "unavailable", "state": "active"}, True, "StateError"),
            ({"command": "is-active", "unit": "deadlock-maintenance.service", "result": "unsupported_state", "returncode": 3, "state": "multiple"}, False, "StateError"),
            ({"command": "is-active", "unit": "deadlock-maintenance.service", "result": "status_mismatch", "returncode": 3, "state": "active"}, False, "exception"),
        )
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "report.json"
            for observation, accepted, error_class in cases:
                with self.subTest(observation=observation):
                    report_path.write_text(
                        json.dumps({**base, "error_class": error_class, "systemd_observation": observation}), encoding="ascii"
                    )
                    result = subprocess.run(
                        [sys.executable, "-c", validator, str(report_path), "1"],
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    output = result.stdout.removeprefix("PRODUCTION_CAPTURE_PROBE ").strip()
                    parsed = json.loads(output)
                self.assertEqual(
                    parsed.get("failed_check") == "remote_or_transport",
                    not accepted,
                )
            with self.assertRaises(probe.ProbeFailure) as timeout_failure:
                probe._unit_snapshot_with_deadline(
                    SimpleNamespace(_unit_snapshot=lambda _overrides: probe.time.sleep(0.2)),
                    deadline=probe.time.monotonic() + 0.02,
                    result={},
                )
            timeout_report = {
                **base,
                "failed_check": timeout_failure.exception.check,
                "error_class": timeout_failure.exception.error_class,
            }
            report_path.write_text(json.dumps(timeout_report), encoding="ascii")
            result = subprocess.run(
                [sys.executable, "-c", validator, str(report_path), "1"],
                capture_output=True,
                text=True,
                check=True,
            )
            parsed = json.loads(result.stdout.removeprefix("PRODUCTION_CAPTURE_PROBE ").strip())
            self.assertEqual(parsed["failed_check"], "unit_snapshot")
            self.assertEqual(parsed["error_class"], "timeout")
            failed_preflight = {
                "schema": 1,
                "kind": "release_capture_probe",
                "status": "failed",
                "failed_check": "nginx_preflight",
                "error_class": "child_failure",
                "health": base["health"],
                "capture_predicate": "existing_receipt_valid",
                "receipt_present": True,
                "current_bound": True,
                "previous_bound": True,
                "runtime_target": "gha-123-1-abcdef012345",
                "runtime_target_source": "a" * 40,
                "runtime_target_helper_sha256": "b" * 64,
                "service_active_count": 13,
                "service_enabled_count": 12,
                "runtime_payload_verify": "passed",
                "runtime_payload_verify_returncode": 0,
                "runtime_expected_tree": "passed",
                "runtime_expected_tree_returncode": 0,
                "nginx_preflight": "failed",
                "nginx_preflight_returncode": 17,
            }
            report_path.write_text(json.dumps(failed_preflight), encoding="ascii")
            result = subprocess.run(
                [sys.executable, "-c", validator, str(report_path), "1"],
                capture_output=True,
                text=True,
                check=True,
            )
            parsed = json.loads(result.stdout.removeprefix("PRODUCTION_CAPTURE_PROBE ").strip())
            self.assertEqual(parsed["failed_check"], "nginx_preflight")
            self.assertEqual(parsed["nginx_preflight"], "failed")
            self.assertEqual(parsed["nginx_preflight_returncode"], 17)
            failed_preflight["raw_stderr"] = "private command output"
            report_path.write_text(json.dumps(failed_preflight), encoding="ascii")
            result = subprocess.run(
                [sys.executable, "-c", validator, str(report_path), "1"],
                capture_output=True,
                text=True,
                check=True,
            )
            parsed = json.loads(result.stdout.removeprefix("PRODUCTION_CAPTURE_PROBE ").strip())
            self.assertEqual(parsed["failed_check"], "remote_or_transport")
            successful_probe = {
                "schema": 1,
                "kind": "release_capture_probe",
                "status": "passed",
                "health": base["health"],
                "capture_predicate": "existing_receipt_valid",
                "receipt_present": True,
                "current_bound": True,
                "previous_bound": True,
                "service_active_count": 13,
                "service_enabled_count": 12,
                "runtime_target": "gha-123-1-abcdef012345",
                "runtime_target_source": "a" * 40,
                "runtime_target_helper_sha256": "b" * 64,
                "runtime_payload_verify": "passed",
                "runtime_payload_verify_returncode": 0,
                "runtime_expected_tree": "passed",
                "runtime_expected_tree_returncode": 0,
                "nginx_preflight": "passed",
                "nginx_preflight_returncode": 0,
            }
            report_path.write_text(json.dumps(successful_probe), encoding="ascii")
            result = subprocess.run(
                [sys.executable, "-c", validator, str(report_path), "0"],
                capture_output=True,
                text=True,
                check=True,
            )
            parsed = json.loads(result.stdout.removeprefix("PRODUCTION_CAPTURE_PROBE ").strip())
            self.assertEqual(parsed["status"], "passed")
            self.assertEqual(parsed["runtime_expected_tree"], "passed")

    def _assert_capture_probe_stage_feeds_heredoc_to_ssh_before_copy(self) -> None:
        workflow = (Path(__file__).resolve().parents[2] / ".github/workflows/platform-production-diagnostics.yml").read_text(encoding="utf-8")
        stage_step = workflow.split("      - name: Stage read-only probe in a private task directory", 1)[1].split(
            "      - name: Read-only systemd capture precondition probe", 1
        )[0]
        script = textwrap.dedent(stage_step.split("        run: |\n", 1)[1])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            ssh_calls = root / "ssh-calls.jsonl"
            scp_calls = root / "scp-calls.jsonl"
            ssh_stub = fake_bin / "ssh"
            ssh_stub.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "from pathlib import Path\n"
                "command = sys.argv[-1]\n"
                "body = sys.stdin.read()\n"
                "with Path(os.environ['FAKE_SSH_CALLS']).open('a', encoding='utf-8') as stream:\n"
                "    stream.write(json.dumps({'command': command, 'body': body}) + '\\n')\n"
                "if command.startswith('bash -s -- '):\n"
                "    if 'probe_dir=\"/run/oldsparky-capture-probe-$run_id-$run_attempt\"' not in body or 'mkdir -m 0700' not in body:\n"
                "        raise SystemExit(42)\n"
                "    print('2049:123')\n",
                encoding="utf-8",
            )
            ssh_stub.chmod(0o755)
            scp_stub = fake_bin / "scp"
            scp_stub.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "from pathlib import Path\n"
                "with Path(os.environ['FAKE_SCP_CALLS']).open('a', encoding='utf-8') as stream:\n"
                "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n",
                encoding="utf-8",
            )
            scp_stub.chmod(0o755)
            github_env = root / "github-env"
            github_env.touch()
            result = subprocess.run(
                ["bash", "-euo", "pipefail", "-c", script],
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
                    "GITHUB_RUN_ID": "12345",
                    "GITHUB_RUN_ATTEMPT": "1",
                    "GITHUB_ENV": str(github_env),
                    "GITHUB_WORKSPACE": str(Path(__file__).resolve().parents[2]),
                    "SSH_DIR": str(root / "ssh"),
                    "PROD_SSH_HOST": "host.invalid",
                    "PROD_SSH_USER": "operator",
                    "FAKE_SSH_CALLS": str(ssh_calls),
                    "FAKE_SCP_CALLS": str(scp_calls),
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = [json.loads(line) for line in ssh_calls.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0]["command"], "bash -s -- '12345' '1'")
            self.assertIn("mkdir -m 0700 -- \"$probe_dir\"", calls[0]["body"])
            self.assertEqual(calls[1]["command"], "chmod 0600 -- '/run/oldsparky-capture-probe-12345-1/probe.py' '/run/oldsparky-capture-probe-12345-1/platform_release_lock.sh'")
            self.assertEqual(calls[1]["body"], "")
            copy_calls = [json.loads(line) for line in scp_calls.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(copy_calls), 2)
            self.assertEqual(
                copy_calls[0][-1],
                "operator@host.invalid:/run/oldsparky-capture-probe-12345-1/probe.py",
            )
            self.assertEqual(
                copy_calls[1][-1],
                "operator@host.invalid:/run/oldsparky-capture-probe-12345-1/platform_release_lock.sh",
            )
            self.assertIn("CAPTURE_PROBE_DIR_ID=2049:123", github_env.read_text(encoding="utf-8"))
        capture_step = workflow.split("      - name: Read-only systemd capture precondition probe", 1)[1].split(
            "      - name: Validate structured production capture report", 1
        )[0]
        self.assertIn("bash -c 'set -euo pipefail; lock=", capture_step)
        self.assertIn("/usr/bin/sha256sum -- \\\"\\$lock\\\"", capture_step)
        self.assertIn('exec bash \\\"\\$lock\\\" --run /usr/bin/bash -s -- \\\"\\$@\\\"', capture_step)
        self.assertIn("timeout --signal=TERM --kill-after=30s 560s", capture_step)
        self.assertNotIn("timeout --foreground", capture_step)
        capture_script = textwrap.dedent(capture_step.split("        run: |\n", 1)[1])
        syntax = subprocess.run(["bash", "-n"], input=capture_script, text=True, capture_output=True, check=False)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        self.assertIn("timeout-minutes: 15", workflow)

    def _assert_remote_cleanup_failure_still_removes_local_ssh_material(self) -> None:
        workflow = (Path(__file__).resolve().parents[2] / ".github/workflows/platform-production-diagnostics.yml").read_text(encoding="utf-8")
        cleanup_step = workflow.split("      - name: Remove production SSH material", 1)[1]
        script = textwrap.dedent(cleanup_step.split("        run: |\n", 1)[1]).replace("${{ inputs.mode }}", "capture-probe")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner_temp = root / "runner"
            ssh_dir = runner_temp / "platform-production-diagnostics-ssh"
            fake_bin = root / "bin"
            ssh_dir.mkdir(parents=True)
            fake_bin.mkdir()
            for name in ("id_ed25519", "known_hosts", "config"):
                (ssh_dir / name).write_text("fixture", encoding="ascii")
            ssh = fake_bin / "ssh"
            ssh.write_text("#!/bin/sh\nexit 42\n", encoding="ascii")
            ssh.chmod(0o700)
            github_env = root / "github-env"
            result = subprocess.run(
                ["bash", "-c", script],
                check=False,
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
                    "RUNNER_TEMP": str(runner_temp),
                    "GITHUB_ENV": str(github_env),
                    "GITHUB_RUN_ID": "12345",
                    "GITHUB_RUN_ATTEMPT": "1",
                    "SSH_DIR": str(ssh_dir),
                    "CAPTURE_PROBE_DIR_ID": "1:2",
                    "PROBE_SHA256": "a" * 64,
                    "PROD_SSH_HOST": "host.invalid",
                    "PROD_SSH_USER": "operator",
                },
            )
            self.assertEqual(result.returncode, 42, result.stderr)
            self.assertFalse(ssh_dir.exists())
            self.assertFalse((ssh_dir / "id_ed25519").exists())
            self.assertFalse((ssh_dir / "known_hosts").exists())
            self.assertFalse((ssh_dir / "config").exists())


if __name__ == "__main__":
    unittest.main()
