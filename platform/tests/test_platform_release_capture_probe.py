from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from tools import platform_release_capture_probe as probe
from tools import platform_release_systemd_state as systemd_state


class FakeSystemdState:
    SYSTEMCTL = "/usr/bin/systemctl"

    def __init__(self, *, fail_query: tuple[str, str] | None = None, operation: str = "install") -> None:
        self.events: list[str] = []
        self.fail_query = fail_query
        self.operation = operation
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
        return ("a" * 32, self.operation, str(app_dir / "releases" / "old"), "", {}, {}, {"deadlock-api.service": "active"})

    def _helper_digests(self, release: Path):
        self.events.append("helper_digests")
        return {"tools/platform_release_systemd_state.py": self.__verified_sha256__}

    def _release_context(self, app_dir: Path, current: str, previous: str):
        self.events.append("capture_context")
        return True

    def _safe_receipt(self, path: Path):
        raise AssertionError("probe must not read or write receipt bytes")

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
        current_link = app_dir / "current"
        current_link.symlink_to(old_release)
        with (
            patch.object(probe, "APP_DIR", app_dir),
            patch.object(probe, "SHARED_DIR", transaction.parent),
            patch.object(probe, "TRANSACTION", transaction),
            patch.object(probe, "SYSTEMD_RECEIPT", companion),
            patch.object(probe, "CURRENT_LINK", current_link),
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
            fake = FakeSystemdState()
            result = self._run(fake, transaction, companion)
            self.assertEqual(result["capture_predicate"], "existing_receipt_valid")
            self.assertEqual(fake.events, ["validate"])

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
        self._assert_remote_cleanup_failure_still_removes_local_ssh_material()

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
