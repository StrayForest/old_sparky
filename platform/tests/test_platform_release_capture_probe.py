from __future__ import annotations

import base64
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
from tools import platform_release_systemd_state as systemd_state


class FakeSystemdState:
    SYSTEMCTL = "/usr/bin/systemctl"

    def __init__(self, *, fail_query: tuple[str, str] | None = None, operation: str = "install", transaction_operation: str | None = None, phase: str = "rollback-runtime-pending") -> None:
        self.events: list[str] = []
        self.fail_query = fail_query
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
                "current_before": {"tools/platform_install_nginx.py": "c" * 64},
                "previous_before": {"tools/platform_install_nginx.py": "c" * 64},
            },
        }

    def _read_active(self, unit: str) -> str:
        self.events.append("active:" + unit)
        if self.fail_query == ("active", unit):
            raise RuntimeError("private command output must not escape")
        return "active"

    def _read_enabled(self, unit: str) -> str:
        self.events.append("enabled:" + unit)
        if self.fail_query == ("enabled", unit):
            raise RuntimeError("private command output must not escape")
        return "enabled"

    def _unit_snapshot(self, active_overrides=None):
        self.events.append("unit_snapshot")
        return [{"name": unit, "active": self._read_active(unit), "enabled": self._read_enabled(unit)} for unit in self.OWNED_UNITS]

    def validate(self, path: Path, **kwargs) -> None:
        self.events.append("validate")

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

    def _run(self, fake: FakeSystemdState, transaction: Path, companion: Path):
        app_dir = transaction.parents[1]
        releases = app_dir / "releases"
        releases.mkdir()
        old_release = releases / "old"
        old_release.mkdir()
        (old_release / "RELEASE.json").write_text('{"source_git_commit":"' + "a" * 40 + '"}\n', encoding="ascii")
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
        with (
            patch.object(probe, "APP_DIR", app_dir),
            patch.object(probe, "SHARED_DIR", transaction.parent),
            patch.object(probe, "TRANSACTION", transaction),
            patch.object(probe, "SYSTEMD_RECEIPT", companion),
            patch.object(probe, "CURRENT_LINK", current_link),
            patch.object(probe, "SHARED_PYTHON", app_dir / "shared/venv/bin/python"),
            patch.object(probe, "_load_pinned_module", return_value=fake),
            patch.object(probe, "_health", return_value={"api_live": True, "api_ready": True, "web_local": True, "public": True}),
            patch.object(probe.os, "geteuid", return_value=0),
        ):
            return probe.probe(fake.__verified_sha256__)

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

    def test_existing_companion_uses_only_existing_read_validator(self) -> None:
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
        self.assertIn('set(observation) != {"command", "unit", "result", "returncode", "state"}', workflow)
        self.assertIn('result_class == "unsupported_state"', workflow)
        self.assertIn('result_class == "malformed_output"', workflow)
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
            self.assertEqual(calls[1]["command"], "chmod 0600 -- '/run/oldsparky-capture-probe-12345-1/probe.py'")
            self.assertEqual(calls[1]["body"], "")
            copy_calls = [json.loads(line) for line in scp_calls.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(copy_calls), 1)
            self.assertEqual(
                copy_calls[0][-1],
                "operator@host.invalid:/run/oldsparky-capture-probe-12345-1/probe.py",
            )
            self.assertIn("CAPTURE_PROBE_DIR_ID=2049:123", github_env.read_text(encoding="utf-8"))

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
