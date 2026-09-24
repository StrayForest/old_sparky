from __future__ import annotations

from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "platform_live_qa_runtime_install.py"
SPEC = importlib.util.spec_from_file_location("platform_live_qa_runtime_install_tested", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


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
            "RELEASE_LOCK_PATH": trusted / entrypoints[6],
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

    @staticmethod
    def _copy_empty_tree(source: Path, destination: Path, **_kwargs: object) -> dict[str, str]:
        del source
        destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        return {}

    def test_install_writes_the_exact_relative_generation_pointer(self) -> None:
        source_sha = "a" * 40
        with tempfile.TemporaryDirectory(dir="/root") as temporary:
            with self.runtime_tree(Path(temporary), source_sha) as (app_dir, release, trusted, payload_root):
                manifest = runtime.install(app_dir, release)
                self.assertEqual(manifest["source_sha"], source_sha)
                self.assertEqual(os.readlink(trusted / "active"), f"releases/{source_sha}")
                self.assertEqual((trusted / "active").resolve(), payload_root / source_sha)
                runtime._validate_active_pointer(source_sha)

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


if __name__ == "__main__":
    unittest.main()
