from __future__ import annotations

from contextlib import contextmanager, redirect_stderr
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import tempfile
import unittest
from unittest import mock
from uuid import uuid4


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "platform_live_qa_runtime_install.py"
SPEC = importlib.util.spec_from_file_location("platform_live_qa_runtime_install_tested", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)
SAFE_ENV_SCRIPT = SCRIPT.with_name("platform_safe_env_exec.py")
SAFE_ENV_SPEC = importlib.util.spec_from_file_location(
    "platform_safe_env_exec_runtime_install_tested", SAFE_ENV_SCRIPT
)
assert SAFE_ENV_SPEC is not None and SAFE_ENV_SPEC.loader is not None
safe_env = importlib.util.module_from_spec(SAFE_ENV_SPEC)
SAFE_ENV_SPEC.loader.exec_module(safe_env)


@unittest.skipUnless(os.geteuid() == 0, "installer contract requires root-owned test paths")
class LiveQaRuntimeInstallTests(unittest.TestCase):
    @contextmanager
    def runtime_tree(self, root: Path, source_sha: str):
        trusted = root / "liveqa"
        payload_root = trusted / "releases"
        app_dir = root / "platform"
        releases = app_dir / "releases"
        release = releases / f"release-{source_sha[:8]}"
        tools = release / "tools"
        tools.mkdir(parents=True, mode=0o755)
        releases.mkdir(parents=True, exist_ok=True)
        (release / "RELEASE.json").write_text(
            json.dumps({"source_git_commit": source_sha, "release_slug": release.name}) + "\n",
            encoding="ascii",
        )
        os.chmod(release / "RELEASE.json", 0o444)
        app_dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(app_dir, 0o700)
        (app_dir / "shared").mkdir(mode=0o700)
        (app_dir / "current").symlink_to(release)

        entrypoints = (
            "platform_live_user_qa_trusted.sh",
            "platform_live_launch_trusted.sh",
            "platform_live_user_qa_dispatch.py",
            "platform_workflow_remote_dispatch.py",
            "platform_workflow_input_guard.py",
            "platform_release_lock_exec.sh",
            "platform_release_lock.sh",
            "platform_live_qa_mailbox_helper.py",
        )
        for name in entrypoints:
            path = tools / name
            path.write_text(f"#!/bin/sh\n# {name}\n", encoding="ascii")
            os.chmod(path, 0o755 if name.endswith(".sh") else 0o555)
        runtime_source = release / "liveqa-runtime"
        runtime_source.mkdir(mode=0o555)

        constants = {
            "TRUSTED_ROOT": trusted,
            "PAYLOAD_ROOT": payload_root,
            "ACTIVE_MANIFEST": trusted / "active-manifest.json",
            "ACTIVE_POINTER": trusted / "active",
            "HELPER_PATH": trusted / entrypoints[0],
            "LAUNCH_HELPER_PATH": trusted / entrypoints[1],
            "DISPATCHER_PATH": trusted / entrypoints[2],
            "REMOTE_DISPATCHER_PATH": trusted / entrypoints[3],
            "REMOTE_INPUT_GUARD_PATH": trusted / entrypoints[4],
            "RELEASE_LOCK_EXEC_PATH": trusted / entrypoints[5],
            "RELEASE_LOCK_HELPER_PATH": trusted / entrypoints[6],
            "MAILBOX_HELPER_PATH": trusted / entrypoints[7],
            "TOOL_FILES": entrypoints,
            "SOURCE_TREES": (),
            "SOURCE_FILES": (),
        }
        with mock.patch.multiple(runtime, **constants), \
            mock.patch.object(runtime, "_require_release_lock"), \
            mock.patch.object(runtime, "_validate_runtime_source"), \
            mock.patch.object(runtime, "_copy_tree", side_effect=self._copy_empty_tree), \
            mock.patch.object(runtime, "_validate_payload"), \
            mock.patch.object(runtime, "_retention", return_value=0), \
            mock.patch.object(runtime, "_cleanup_staging", return_value=0):
            yield app_dir, release, trusted, payload_root

    @contextmanager
    def canonical_runtime_tree(self, root: Path, source_sha: str):
        """Build the installer fixture from the production TOOL_FILES set."""

        trusted = root / "liveqa"
        payload_root = trusted / "releases"
        app_dir = root / "platform"
        releases = app_dir / "releases"
        release = releases / f"release-{source_sha[:8]}"
        tools = release / "tools"
        tools.mkdir(mode=0o755, parents=True)
        os.chmod(releases, 0o755)
        os.chmod(release, 0o755)
        os.chmod(tools, 0o755)
        app_dir.mkdir(mode=0o755, exist_ok=True)
        os.chmod(app_dir, 0o755)
        (release / "RELEASE.json").write_text(
            json.dumps({"source_git_commit": source_sha, "release_slug": release.name})
            + "\n",
            encoding="ascii",
        )
        os.chmod(release / "RELEASE.json", 0o444)
        (app_dir / "current").symlink_to(release)
        runtime_source = release / "liveqa-runtime"
        runtime_source.mkdir(mode=0o755)

        source_tools = SCRIPT.parent
        for name in runtime.TOOL_FILES:
            source = source_tools / name
            destination = tools / name
            destination.write_bytes(source.read_bytes())
            os.chmod(destination, 0o555 if name.endswith(".sh") else 0o444)

        source_files = runtime.SOURCE_FILES
        for relative in source_files:
            source = SCRIPT.parents[1] / relative
            destination = release / relative
            destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())
            os.chmod(destination, 0o644)

        constants = {
            "TRUSTED_ROOT": trusted,
            "PAYLOAD_ROOT": payload_root,
            "ACTIVE_MANIFEST": trusted / "active-manifest.json",
            "ACTIVE_POINTER": trusted / "active",
            "HELPER_PATH": trusted / "platform_live_user_qa_trusted.sh",
            "LAUNCH_HELPER_PATH": trusted / "platform_live_launch_trusted.sh",
            "DISPATCHER_PATH": trusted / "platform_live_user_qa_dispatch.py",
            "REMOTE_DISPATCHER_PATH": trusted / "platform_workflow_remote_dispatch.py",
            "REMOTE_INPUT_GUARD_PATH": trusted / "platform_workflow_input_guard.py",
            "RELEASE_LOCK_EXEC_PATH": trusted / "platform_release_lock_exec.sh",
            "RELEASE_LOCK_HELPER_PATH": trusted / "platform_release_lock.sh",
            "MAILBOX_HELPER_PATH": trusted / "platform_live_qa_mailbox_helper.py",
            "SOURCE_TREES": (),
            "SOURCE_FILES": source_files,
        }
        with mock.patch.multiple(runtime, **constants), \
            mock.patch.object(runtime, "_require_release_lock"), \
            mock.patch.object(runtime, "_validate_runtime_source"), \
            mock.patch.object(runtime, "_retention", return_value=0), \
            mock.patch.object(runtime, "_cleanup_staging", return_value=0):
            yield app_dir, release, trusted, payload_root

    @staticmethod
    def _copy_empty_tree(source: Path, destination: Path, **_kwargs: object) -> dict[str, str]:
        del source
        destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        return {}

    @unittest.skipUnless(os.geteuid() == 0, "release lock probe requires root-owned /run/lock")
    def test_release_lock_identity_is_not_shadowed_by_payload_helper(self) -> None:
        """The copied helper is payload data, not the canonical mutex path."""

        source = SCRIPT.read_text(encoding="utf-8")
        canonical = 'Path("/run/lock/oldsparky-platform-release.lock")'
        self.assertIn(canonical, source)
        helper_source_path = SCRIPT.with_name("platform_release_lock.sh")
        helper_source = helper_source_path.read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            fixture = Path(temporary)
            trusted = fixture / "liveqa"
            trusted.mkdir(mode=0o700)
            lock_path = Path("/run/lock") / f"oldsparky-platform-release-test-{uuid4().hex}.lock"
            self.assertFalse(lock_path.exists())

            helper_source = helper_source.replace(
                "/run/lock/oldsparky-platform-release.lock", str(lock_path)
            ).replace(
                "/run/lock/oldsparky-retained-load-matrix.lock",
                "/run/lock/oldsparky-retained-load-test-unused.lock",
            )
            helper_path = fixture / "platform_release_lock.sh"
            helper_path.write_text(helper_source, encoding="utf-8")
            os.chmod(helper_path, 0o500)

            installer_source = source.replace(
                canonical, f'Path("{lock_path}")'
            ).replace(
                'Path("/root/.oldsparky/liveqa")', f'Path("{trusted}")'
            )
            installer_path = fixture / "platform_live_qa_runtime_install.py"
            installer_path.write_text(installer_source, encoding="utf-8")
            os.chmod(installer_path, 0o500)

            callback = fixture / "probe.py"
            callback.write_text(
                "import importlib.util, sys\n"
                "spec = importlib.util.spec_from_file_location('lock_probe', sys.argv[1])\n"
                "module = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(module)\n"
                "module._require_release_lock()\n"
                "print('LOCK_PROBE_OK')\n",
                encoding="ascii",
            )
            runner = fixture / "probe.sh"
            runner.write_text(
                "#!/usr/bin/env bash\n"
                "set -Eeuo pipefail\n"
                f"source {shlex.quote(str(helper_path))}\n"
                "platform_release_lock_supervise \"$@\"\n"
                'if [[ "${PLATFORM_RELEASE_LOCK_SUPERVISED:-}" != "1" ]]; then exit 0; fi\n'
                "platform_release_lock_open\n"
                "exec /usr/bin/python3 -I -B \"$2\" \"$1\"\n",
                encoding="ascii",
            )
            os.chmod(runner, 0o500)

            def run_probe() -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [str(runner), str(installer_path), str(callback)],
                    env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=15,
                )

            def assert_probe_passes() -> None:
                completed = run_probe()
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(completed.stdout, "LOCK_PROBE_OK\n")
                self.assertEqual(completed.stderr, "")

            try:
                # The runtime predicate must still reject a caller with no
                # live pathname-form lock owner.
                unlocked = subprocess.run(
                    ["/usr/bin/python3", "-I", "-B", str(callback), str(installer_path)],
                    env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
                self.assertNotEqual(unlocked.returncode, 0)
                self.assertNotIn("LOCK_PROBE_OK", unlocked.stdout)

                # First creation has no copied payload helper yet.
                assert_probe_passes()

                # After publication, the copied helper is immutable mode 0444;
                # it must not be mistaken for the active lock file.
                payload_helper = trusted / "platform_release_lock.sh"
                payload_helper.write_text("# immutable payload lock helper\n", encoding="ascii")
                os.chmod(payload_helper, 0o444)
                assert_probe_passes()
            finally:
                if lock_path.exists() and not lock_path.is_symlink():
                    metadata = lock_path.lstat()
                    if metadata.st_uid == 0 and metadata.st_nlink == 1:
                        lock_path.unlink()

    def test_install_writes_the_exact_relative_generation_pointer(self) -> None:
        source_sha = "a" * 40
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self.runtime_tree(Path(temporary), source_sha) as (app_dir, release, trusted, payload_root):
                previous_umask = os.umask(0o077)
                try:
                    manifest = runtime.install(app_dir, release)
                finally:
                    os.umask(previous_umask)
                self.assertEqual(manifest["source_sha"], source_sha)
                self.assertEqual(os.readlink(trusted / "active"), f"releases/{source_sha}")
                self.assertEqual((trusted / "active").resolve(), payload_root / source_sha)
                self.assertEqual(stat.S_IMODE(payload_root.stat().st_mode), 0o755)
                self.assertEqual(stat.S_IMODE(trusted.stat().st_mode), 0o700)
                runtime._validate_active_pointer(source_sha)

        # Exercise the real fixed TOOL_FILES list and installer copy/digest
        # path: retained cleanup must be present in the installed generation,
        # not just in a hand-built test manifest.
        source_sha = "f" * 40
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self.canonical_runtime_tree(Path(temporary), source_sha) as (
                app_dir,
                release,
                trusted,
                payload_root,
            ):
                manifest = runtime.install(app_dir, release)
                payload = payload_root / source_sha
                cleanup_tools = (
                    "platform_cleanup_retained_orphan.py",
                    "platform_recover_retained_report.py",
                    "platform_cleanup_retained_matrix.py",
                )
                for name in cleanup_tools:
                    relative = f"platform/tools/{name}"
                    installed = payload / relative
                    self.assertTrue(installed.is_file(), name)
                    self.assertEqual(
                        runtime._digest_regular(installed), manifest["files"][relative]
                    )

                # Every direct supervisor/provisioning dependency must be
                # copied into and recorded by the immutable runtime manifest.
                launch_members = (
                    "platform/tools/platform_live_launch_supervisor.sh",
                    "platform/tools/platform_live_qa_guard.py",
                    "platform/tools/platform_safe_env_exec.py",
                    "platform/tools/platform_live_browser_qa.sh",
                    "platform/tools/platform_provision_live_csp_qa.sh",
                    "platform/tools/platform_provision_live_csp_qa.py",
                    "platform/tools/platform_install_live_qa_user.sh",
                    "platform/tools/platform_release_lock_exec.sh",
                    "platform/tools/platform_release_lock.sh",
                    "platform/deploy/apparmor/oldsparky-liveqa-chromium",
                )
                for relative in launch_members:
                    installed = payload / relative
                    self.assertIn(relative, manifest["files"])
                    self.assertTrue(installed.is_file(), relative)
                    expected_mode = 0o555 if relative.endswith(".sh") else 0o444
                    self.assertEqual(
                        stat.S_IMODE(installed.stat().st_mode), expected_mode
                    )
                    self.assertEqual(
                        runtime._digest_regular(installed), manifest["files"][relative]
                    )

                fake_python_target = Path(temporary) / "trusted-python-target"
                fake_python_target.write_bytes(b"#!/bin/sh\nexit 0\n")
                os.chmod(fake_python_target, 0o755)
                os.chown(fake_python_target, 0, 0)
                fake_python = Path(temporary) / "venv/bin/python"
                fake_python.parent.mkdir(parents=True, mode=0o755)
                fake_python.symlink_to(fake_python_target)
                os.chown(fake_python, 0, 0, follow_symlinks=False)
                selected_tool = payload / "platform/tools/platform_cleanup_retained_matrix.py"

                def execute_selected() -> int:
                    return safe_env.main(
                        [
                            "exec",
                            "--pythonpath",
                            str(payload),
                            "--",
                            str(fake_python),
                            str(selected_tool),
                            "--help",
                        ]
                    )

                with (
                    mock.patch.multiple(
                        safe_env,
                        PRODUCTION_RUNTIME_ROOT=app_dir,
                        ACTIVE_PLATFORM_ROOT=app_dir / "current",
                        LIVE_QA_ROOT=trusted,
                        LIVE_QA_RELEASE_ROOT=payload_root,
                        LIVE_QA_ACTIVE_MANIFEST=trusted / "active-manifest.json",
                        LIVE_QA_ACTIVE_POINTER=trusted / "active",
                        ACTIVE_PYTHON=fake_python,
                        TRUSTED_SYSTEM_PYTHON=fake_python_target,
                    ),
                    mock.patch.object(safe_env, "validate_active_runtime"),
                    mock.patch.object(
                        safe_env,
                        "read_production_env_bytes",
                        return_value=b"PLATFORM_ENVIRONMENT=production\n",
                    ),
                ):
                    # Exercise the unchanged safe-env exec boundary against
                    # the actual installed helper path.  Intercept execve so
                    # no database tool or production environment is run.
                    with (
                        mock.patch.object(
                            safe_env.os, "execve", side_effect=SystemExit(0)
                        ) as execve,
                        self.assertRaises(SystemExit),
                    ):
                        execute_selected()
                    execve.assert_called_once()
                    self.assertEqual(
                        execve.call_args.args[1][1], str(selected_tool)
                    )

                    with (
                        mock.patch.object(safe_env.os, "execve") as execve,
                        redirect_stderr(io.StringIO()),
                    ):
                        self.assertEqual(
                            safe_env.main(
                                [
                                    "exec",
                                    "--pythonpath",
                                    str(payload),
                                    "--",
                                    str(fake_python),
                                    str(release / "tools/platform_cleanup_retained_matrix.py"),
                                ]
                            ),
                            2,
                        )
                    execve.assert_not_called()

                    release_json = release / "RELEASE.json"
                    release_json.chmod(0o644)
                    release_json.write_text(
                        json.dumps(
                            {"source_git_commit": "e" * 40, "release_slug": release.name}
                        )
                        + "\n",
                        encoding="ascii",
                    )
                    release_json.chmod(0o444)
                    with (
                        mock.patch.object(safe_env.os, "execve") as execve,
                        redirect_stderr(io.StringIO()),
                    ):
                        self.assertEqual(execute_selected(), 2)
                    execve.assert_not_called()

                    # A self-consistent tree missing the provision wrapper
                    # still fails the installer's required-entrypoint check;
                    # the active safe-env manifest also rejects the changed
                    # generation digest.
                    release_json.chmod(0o644)
                    release_json.write_text(
                        json.dumps(
                            {"source_git_commit": source_sha, "release_slug": release.name}
                        )
                        + "\n",
                        encoding="ascii",
                    )
                    release_json.chmod(0o444)
                    (payload / "platform/tools/platform_provision_live_csp_qa.sh").unlink()
                    changed_digest, changed_files = runtime._tree_digest(payload)
                    changed_manifest = dict(manifest)
                    changed_manifest["payload_tree_sha256"] = changed_digest
                    changed_manifest["files"] = changed_files
                    with self.assertRaisesRegex(
                        runtime.InstallerError, "missing a required entrypoint"
                    ):
                        runtime._validate_payload(changed_manifest)
                    with (
                        mock.patch.object(safe_env.os, "execve") as execve,
                        redirect_stderr(io.StringIO()),
                    ):
                        self.assertEqual(execute_selected(), 2)
                    execve.assert_not_called()

    def test_postpromotion_retention_failure_reports_closed_stage_and_keeps_pointer(self) -> None:
        source_sha = "e" * 40
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self.runtime_tree(Path(temporary), source_sha) as (
                app_dir,
                release,
                trusted,
                payload_root,
            ):
                stderr = io.StringIO()
                with mock.patch.object(
                    runtime,
                    "_retention",
                    side_effect=runtime.InstallerError("private diagnostic detail"),
                ), redirect_stderr(stderr), self.assertRaises(runtime.InstallerError):
                    runtime.install(app_dir, release)

                diagnostic = stderr.getvalue()
                self.assertIn(
                    "LIVE_QA_INSTALL_STAGE stage=retention status=failed ",
                    diagnostic,
                )
                self.assertNotIn("private diagnostic detail", diagnostic)
                self.assertEqual(os.readlink(trusted / "active"), f"releases/{source_sha}")
                manifest = json.loads((trusted / "active-manifest.json").read_text(encoding="ascii"))
                self.assertEqual(manifest["source_sha"], source_sha)
                self.assertTrue((payload_root / source_sha).is_dir())

    def test_install_repairs_only_legacy_private_payload_root_mode(self) -> None:
        source_sha = "b" * 40
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self.runtime_tree(Path(temporary), source_sha) as (app_dir, release, trusted, payload_root):
                trusted.mkdir(mode=0o700)
                payload_root.mkdir(mode=0o700)
                previous_umask = os.umask(0o077)
                try:
                    runtime.install(app_dir, release)
                finally:
                    os.umask(previous_umask)
                self.assertEqual(stat.S_IMODE(payload_root.stat().st_mode), 0o755)
                self.assertEqual(stat.S_IMODE(trusted.stat().st_mode), 0o700)

    def test_install_preserves_canonical_payload_root_mode(self) -> None:
        source_sha = "d" * 40
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self.runtime_tree(Path(temporary), source_sha) as (app_dir, release, trusted, payload_root):
                trusted.mkdir(mode=0o700)
                payload_root.mkdir(mode=0o755)
                payload_root.chmod(0o755)
                runtime.install(app_dir, release)
                self.assertEqual(stat.S_IMODE(payload_root.stat().st_mode), 0o755)
                self.assertEqual(stat.S_IMODE(trusted.stat().st_mode), 0o700)

    def test_install_rejects_unsupported_payload_root_metadata_before_cleanup(self) -> None:
        source_sha = "c" * 40
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self.runtime_tree(Path(temporary), source_sha) as (app_dir, release, _trusted, payload_root):
                _trusted.mkdir(mode=0o700)
                payload_root.mkdir(mode=0o750)
                payload_root.chmod(0o750)
                with mock.patch.object(runtime, "_cleanup_temporary_files") as cleanup, self.assertRaisesRegex(
                    runtime.InstallerError,
                    "payload root mode is unsupported",
                ):
                    runtime.install(app_dir, release)
                cleanup.assert_not_called()
                self.assertEqual(stat.S_IMODE(payload_root.stat().st_mode), 0o750)

                payload_root.rmdir()
                target = Path(temporary) / "outside-releases"
                target.mkdir(mode=0o755)
                payload_root.symlink_to(target)
                with self.assertRaisesRegex(runtime.InstallerError, "payload root is unavailable"):
                    runtime.install(app_dir, release)
                payload_root.unlink()

                payload_root.mkdir(mode=0o755)
                payload_root.chmod(0o755)
                os.chown(payload_root, 65534, 65534)
                with self.assertRaisesRegex(runtime.InstallerError, "payload root metadata is unsafe"):
                    runtime.install(app_dir, release)

    def test_verify_rejects_a_crash_left_noncanonical_pointer(self) -> None:
        source_sha = "b" * 40
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self.runtime_tree(Path(temporary), source_sha) as (app_dir, release, trusted, _payload_root):
                runtime.install(app_dir, release)
                active = trusted / "active"
                active.unlink()
                active.symlink_to(source_sha)
                with self.assertRaisesRegex(runtime.InstallerError, "active generation pointer"):
                    runtime.verify(app_dir, source_sha)

    def test_reconcile_repairs_a_crash_pointer_and_verify_accepts(self) -> None:
        source_sha = "c" * 40
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self.runtime_tree(Path(temporary), source_sha) as (app_dir, release, trusted, payload_root):
                runtime.install(app_dir, release)
                active = trusted / "active"
                active.unlink()
                active.symlink_to(source_sha)
                manifest = runtime.reconcile(app_dir)
                self.assertEqual(manifest["source_sha"], source_sha)
                self.assertEqual(os.readlink(active), f"releases/{source_sha}")
                self.assertEqual(active.resolve(), payload_root / source_sha)
                verified = runtime.verify(app_dir, source_sha)
                self.assertEqual(verified["source_sha"], source_sha)

    def test_active_manifest_between_legacy_and_active_bounds_is_accepted(self) -> None:
        source_sha = "d" * 40
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self.runtime_tree(Path(temporary), source_sha) as (
                app_dir,
                release,
                trusted,
                _payload_root,
            ):
                runtime.install(app_dir, release)
                manifest_path = trusted / "active-manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="ascii"))
                manifest["files"].update(
                    {
                        f"manifest-padding-{index:04d}-{'x' * 70}": "0" * 64
                        for index in range(900)
                    }
                )
                manifest_path.write_text(
                    json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="ascii",
                )
                os.chmod(manifest_path, 0o444)
                self.assertGreater(manifest_path.stat().st_size, 64 * 1024)
                self.assertLessEqual(
                    manifest_path.stat().st_size,
                    runtime.MAX_ACTIVE_MANIFEST_BYTES,
                )
                self.assertEqual(runtime._read_manifest()["source_sha"], source_sha)

    def test_active_manifest_over_active_bound_is_rejected(self) -> None:
        source_sha = "e" * 40
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self.runtime_tree(Path(temporary), source_sha) as (
                app_dir,
                release,
                trusted,
                _payload_root,
            ):
                runtime.install(app_dir, release)
                manifest_path = trusted / "active-manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="ascii"))
                manifest["files"].update(
                    {
                        f"manifest-padding-{index:04d}-{'x' * 70}": "0" * 64
                        for index in range(3200)
                    }
                )
                manifest_path.write_text(
                    json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="ascii",
                )
                os.chmod(manifest_path, 0o444)
                self.assertGreater(
                    manifest_path.stat().st_size,
                    runtime.MAX_ACTIVE_MANIFEST_BYTES,
                )
                with self.assertRaisesRegex(
                    runtime.InstallerError,
                    "metadata is unsafe|size bound",
                ):
                    runtime._read_manifest()

    def test_release_json_reader_keeps_the_canonical_64k_bound(self) -> None:
        source_sha = "f" * 40
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self.runtime_tree(Path(temporary), source_sha) as (
                app_dir,
                release,
                _trusted,
                _payload_root,
            ):
                release_json = release / "RELEASE.json"
                release_json.write_text(
                    json.dumps(
                        {
                            "source_git_commit": source_sha,
                            "release_slug": release.name,
                            "padding": "x" * (runtime.MAX_RELEASE_JSON_BYTES + 1),
                        }
                    ),
                    encoding="ascii",
                )
                os.chmod(release_json, 0o444)
                with self.assertRaisesRegex(
                    runtime.InstallerError,
                    "metadata is unsafe|size bound",
                ):
                    runtime._safe_release(app_dir, release)

    def test_active_manifest_reader_rejects_symlink_hardlink_and_wrong_mode(self) -> None:
        cases = ("symlink", "hardlink", "mode")
        for case in cases:
            with self.subTest(case=case):
                source_sha = ("1" if case == "symlink" else "2" if case == "hardlink" else "3") * 40
                with tempfile.TemporaryDirectory(dir="/root") as temporary:
                    with self.runtime_tree(Path(temporary), source_sha) as (
                        app_dir,
                        release,
                        trusted,
                        _payload_root,
                    ):
                        runtime.install(app_dir, release)
                        manifest_path = trusted / "active-manifest.json"
                        if case == "symlink":
                            target = trusted / "manifest-target.json"
                            target.write_bytes(manifest_path.read_bytes())
                            os.chmod(target, 0o444)
                            manifest_path.unlink()
                            manifest_path.symlink_to(target)
                        elif case == "hardlink":
                            os.link(manifest_path, trusted / "manifest-second-link")
                        else:
                            os.chmod(manifest_path, 0o644)
                        with self.assertRaisesRegex(
                            runtime.InstallerError,
                            "metadata is unsafe",
                        ):
                            runtime._read_manifest()

    def test_cleanup_staging_accepts_real_nested_directories(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            payload_root = root / "releases"
            payload_root.mkdir(mode=0o755)
            stage = payload_root / f".{('a' * 40)}.install-{('b' * 32)}"
            nested = stage / "runtime" / "browsers" / "chromium"
            nested.mkdir(mode=0o700, parents=True)
            (nested / "node").write_bytes(b"nested-runtime")
            for directory in (stage, stage / "runtime", stage / "runtime" / "browsers", nested):
                os.chmod(directory, 0o700)
            os.chmod(nested / "node", 0o600)

            with mock.patch.object(runtime, "PAYLOAD_ROOT", payload_root):
                self.assertEqual(runtime._cleanup_staging(apply=True), 1)
            self.assertFalse(stage.exists())

    def test_cleanup_staging_rejects_real_nested_hardlinks(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            payload_root = root / "releases"
            payload_root.mkdir(mode=0o755)
            stage = payload_root / f".{('c' * 40)}.install-{('d' * 32)}"
            nested = stage / "runtime" / "browsers"
            nested.mkdir(mode=0o700, parents=True)
            source = nested / "node"
            source.write_bytes(b"hard-linked")
            os.link(source, nested / "node-alias")
            for directory in (stage, stage / "runtime", nested):
                os.chmod(directory, 0o700)
            os.chmod(source, 0o600)
            os.chmod(nested / "node-alias", 0o600)

            with mock.patch.object(runtime, "PAYLOAD_ROOT", payload_root):
                with self.assertRaisesRegex(
                    runtime.InstallerError,
                    "link count is unsafe",
                ):
                    runtime._cleanup_staging(apply=True)
            self.assertTrue(stage.exists())

    @staticmethod
    def _make_retention_entry(root: Path, source_sha: str, mtime: int, *, hardlink: bool = False) -> Path:
        entry = root / source_sha
        nested = entry / "runtime" / "browsers"
        nested.mkdir(mode=0o555, parents=True)
        source = nested / "node"
        source.write_bytes(b"retained-runtime")
        os.chmod(source, 0o444)
        if hardlink:
            os.link(source, nested / "node-alias")
            os.chmod(nested / "node-alias", 0o444)
        for directory in (entry, entry / "runtime", nested):
            os.chmod(directory, 0o555)
        os.utime(entry, (mtime, mtime))
        return entry

    def test_retention_accepts_real_nested_directories(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            payload_root = root / "releases"
            payload_root.mkdir(mode=0o755)
            old = self._make_retention_entry(payload_root, "e" * 40, 1)
            newest = self._make_retention_entry(payload_root, "f" * 40, 2)

            with (
                mock.patch.object(runtime, "PAYLOAD_ROOT", payload_root),
                mock.patch.object(runtime, "ACTIVE_POINTER", root / "active"),
            ):
                self.assertEqual(runtime._retention(root / "app", apply=True), 1)
            self.assertFalse(old.exists())
            self.assertTrue(newest.exists())

    def test_retention_rejects_real_nested_hardlinks(self) -> None:
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            root = Path(temporary)
            payload_root = root / "releases"
            payload_root.mkdir(mode=0o755)
            old = self._make_retention_entry(payload_root, "1" * 40, 1, hardlink=True)
            self._make_retention_entry(payload_root, "2" * 40, 2)

            with (
                mock.patch.object(runtime, "PAYLOAD_ROOT", payload_root),
                mock.patch.object(runtime, "ACTIVE_POINTER", root / "active"),
            ):
                with self.assertRaisesRegex(
                    runtime.InstallerError,
                    "link count is unsafe",
                ):
                    runtime._retention(root / "app", apply=True)
            self.assertTrue(old.exists())


if __name__ == "__main__":
    unittest.main()
