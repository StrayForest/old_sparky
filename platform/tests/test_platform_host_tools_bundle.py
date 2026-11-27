from __future__ import annotations

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
import re
import resource
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from types import SimpleNamespace
import unittest
import zipfile
from unittest.mock import patch

from tools import platform_host_tools_bundle as bundle
from tools import platform_host_tools_installed_check as installed_check
from tools import platform_host_tools_candidate as candidate
from tools import platform_host_tools_pin as pin
from tools import platform_host_tools_provision_operator as operator_provisioner
from tools import platform_readonly_live_readiness as live_readiness
from tools import platform_workflow_remote_dispatch as dispatcher
from tools.platform_verify_contract import (
    _workflow_step_blocks,
    host_tools_candidate_artifact_zip_curl_blocks,
    host_tools_candidate_workflow_issues,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = REPO_ROOT / "platform" / "tools"
SOURCE_SHA = "d974c8b0536683d0ca8d6f1aca8331a215023fd4"
# Keep the fixture anchored to the repository's reviewed generation.  The
# provenance lifecycle intentionally changes this value in a later pin-only
# commit, so a test-side hard-coded SHA would make that commit alter unrelated
# test code.
PIN_SHA = json.loads(
    (REPO_ROOT / pin.PIN_RELATIVE_PATH).read_text(encoding="utf-8")
)["host_tools_sha"]
ARTIFACT_DIGEST = "sha256:" + "e" * 64


def _release_receipt(
    slug: str,
    source_sha: str,
    *,
    legacy_runtime_layout: object | None = None,
    extra_field: bool = False,
) -> bytes:
    payload: dict[str, object] = {
        "artifact_format_version": 1,
        "release_slug": slug,
        "built_at_utc": "20260920T123932Z",
        "release_ref": slug,
        "source_git_commit": source_sha,
        "python_requirements_file": "requirements-platform.txt",
        "python_lock_file": "requirements-platform.lock.txt",
        "python_freeze_file": "requirements-platform.freeze.txt",
        "python_wheelhouse_dir": "wheelhouse",
        "python_wheelhouse_manifest_file": "wheelhouse/WHEELHOUSE.sha256",
        "web_package_lock_file": "apps/platform_web/package-lock.json",
        "web_build_id": "fixture-build-id",
        "node_version": "26.3.1",
        "npm_version": "11.16.0",
    }
    if legacy_runtime_layout is not None:
        payload["runtime_layout"] = legacy_runtime_layout
    if extra_field:
        payload["unexpected"] = "rejected"
    return (
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("ascii")


class HostToolsBundleTests(unittest.TestCase):
    @staticmethod
    def _preflight_jobs_fixture() -> list[dict[str, object]]:
        skipped = sorted(operator_provisioner.PREFLIGHT_ALLOWED_SKIPPED_JOBS)
        return [
            {"id": 1, "name": "Validate deployment dispatch", "status": "completed", "conclusion": "success"},
            {
                "id": 2, "name": "Build deterministic host-tools bundle in CI",
                "status": "completed", "conclusion": "success",
                "steps": [
                    {"name": name, "status": "completed", "conclusion": "success"}
                    for name in (
                        "Build and verify deterministic host-tools bundle",
                        "Publish exact host-tools bundle",
                        "Attest exact host-tools bundle",
                    )
                ],
            },
            {
                "id": 3, "name": "Verify immutable production host capabilities",
                "status": "completed", "conclusion": "failure",
                "steps": [{
                    "number": 5,
                    "name": "Validate root SSH identity and installed generation",
                    "status": "completed", "conclusion": "failure",
                }],
            },
            *[
                {"id": index + 4, "name": name, "status": "completed", "conclusion": "skipped"}
                for index, name in enumerate(skipped)
            ],
        ]

    @staticmethod
    def _operator_fixture() -> tuple[dict[str, object], dict[str, bytes]]:
        payloads = {
            "platform_workflow_remote_dispatch.py": b"raise RuntimeError('candidate execution is forbidden')\n",
            **{f"platform_operator_fixture_{index}.py": f"# fixture {index}\n".encode() for index in range(14)},
            "capabilities.txt": b"schema=1\ntoolset_version=production-host-tools-v4\n",
        }
        records = [
            {
                "path": name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "mode": 0o444 if name == "capabilities.txt" else 0o555,
            }
            for name, data in payloads.items()
        ]
        manifest: dict[str, object] = {
            "schema": 1,
            "source_sha": "a" * 40,
            "generation": "a" * 40,
            "toolset_version": "production-host-tools-v4",
            "files": records,
        }
        manifest_bytes = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
        return manifest, {**payloads, "manifest.json": manifest_bytes}

    def _trusted_source_fixture(self, root: Path) -> tuple[Path, str, Path]:
        source = root / "source"
        for relative in operator_provisioner.TRUSTED_SOURCE_PATHS:
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if relative.endswith(".json"):
                path.write_text('{"host_tools_sha":"' + "a" * 40 + '"}\n', encoding="ascii")
            else:
                path.write_text(f"# trusted fixture: {relative}\n", encoding="ascii")
        unrelated = source / "tracked.txt"
        unrelated.write_text("reviewed\n", encoding="ascii")
        subprocess.run(["/usr/bin/git", "-C", str(source), "init", "--quiet"], check=True)
        subprocess.run(["/usr/bin/git", "-C", str(source), "config", "user.name", "Fixture"], check=True)
        subprocess.run(
            ["/usr/bin/git", "-C", str(source), "config", "user.email", "fixture@example.invalid"], check=True,
        )
        subprocess.run(["/usr/bin/git", "-C", str(source), "add", "."], check=True)
        subprocess.run(["/usr/bin/git", "-C", str(source), "commit", "--quiet", "-m", "source"], check=True)
        target = subprocess.run(
            ["/usr/bin/git", "-C", str(source), "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
        ).stdout.strip()
        subprocess.run(["/usr/bin/git", "-C", str(source), "update-ref", "refs/remotes/origin/dev", target], check=True)
        return source, target, unrelated

    def test_operator_source_checkout_allows_untracked_unrelated_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="host-tools-source-clean-") as temporary:
            source, target, _ = self._trusted_source_fixture(Path(temporary))
            (source / "attachments").mkdir()
            (source / "attachments/private-report.txt").write_text("private\n", encoding="ascii")
            operator_provisioner._verify_source_checkout(source, target, trusted_uid=os.geteuid())

    def test_operator_source_checkout_rejects_staged_and_unstaged_tracked_changes(self) -> None:
        for staged in (False, True):
            with self.subTest(staged=staged), tempfile.TemporaryDirectory(prefix="host-tools-source-dirty-") as temporary:
                source, target, tracked = self._trusted_source_fixture(Path(temporary))
                tracked.write_text("changed\n", encoding="ascii")
                if staged:
                    subprocess.run(["/usr/bin/git", "-C", str(source), "add", "tracked.txt"], check=True)
                with self.assertRaisesRegex(operator_provisioner.ProvisionError, "reviewed_source_checkout_changed"):
                    operator_provisioner._verify_source_checkout(source, target, trusted_uid=os.geteuid())

        with tempfile.TemporaryDirectory(prefix="host-tools-source-assume-unchanged-") as temporary:
            source, target, _ = self._trusted_source_fixture(Path(temporary))
            relative = "platform/tools/platform_host_tools_pin.py"
            trusted = source / relative
            trusted.write_text("# altered trusted bytes\n", encoding="ascii")
            subprocess.run(
                ["/usr/bin/git", "-C", str(source), "update-index", "--assume-unchanged", relative], check=True,
            )
            with self.assertRaisesRegex(operator_provisioner.ProvisionError, "trusted_operator_source_changed"):
                operator_provisioner._verify_source_checkout(source, target, trusted_uid=os.geteuid())

        with tempfile.TemporaryDirectory(prefix="host-tools-source-mode-") as temporary:
            source, target, _ = self._trusted_source_fixture(Path(temporary))
            trusted = source / "platform/tools/platform_host_tools_pin.py"
            trusted.chmod(0o600)
            with self.assertRaisesRegex(operator_provisioner.ProvisionError, "trusted_operator_source_mode_changed"):
                operator_provisioner._verify_source_checkout(source, target, trusted_uid=os.geteuid())

    def test_operator_source_checkout_rejects_untracked_shadow_of_trusted_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="host-tools-source-shadow-") as temporary:
            source, target, _ = self._trusted_source_fixture(Path(temporary))
            trusted = source / "platform/tools/platform_host_tools_pin.py"
            subprocess.run(["/usr/bin/git", "-C", str(source), "rm", "--quiet", "platform/tools/platform_host_tools_pin.py"], check=True)
            trusted.write_text("# untracked replacement\n", encoding="ascii")
            with self.assertRaisesRegex(operator_provisioner.ProvisionError, "reviewed_source_checkout_changed"):
                operator_provisioner._verify_source_checkout(source, target, trusted_uid=os.geteuid())

    def test_operator_source_checkout_rejects_origin_identity_drift(self) -> None:
        with tempfile.TemporaryDirectory(prefix="host-tools-source-drift-") as temporary:
            source, target, tracked = self._trusted_source_fixture(Path(temporary))
            tracked.write_text("second reviewed commit\n", encoding="ascii")
            subprocess.run(["/usr/bin/git", "-C", str(source), "add", "tracked.txt"], check=True)
            subprocess.run(["/usr/bin/git", "-C", str(source), "commit", "--quiet", "-m", "drift"], check=True)
            operator_provisioner._verify_trusted_source_files(source, target, trusted_uid=os.geteuid())
            with self.assertRaisesRegex(operator_provisioner.ProvisionError, "reviewed_source_checkout_changed"):
                operator_provisioner._verify_source_checkout(source, target, trusted_uid=os.geteuid())

    def test_operator_rehearsal_publishes_exact_inventory_without_candidate_execution(self) -> None:
        manifest, files = self._operator_fixture()
        owner = (os.geteuid(), os.getegid())
        with tempfile.TemporaryDirectory(prefix="host-tools-operator-exact-") as temporary:
            root = Path(temporary)
            prior = root / ("b" * 40)
            prior.mkdir(mode=0o700)
            sentinel = prior / "prior-generation.txt"
            sentinel.write_bytes(b"preserve prior generation\n")
            os.chmod(prior, 0o555)
            result = operator_provisioner.publish(
                root, "a" * 40, "12345", manifest, files, rehearsal=True, owner=owner
            )
            target = Path(result["path"])
            self.assertEqual(result["self_test"], "not_run_rehearsal")
            self.assertTrue(operator_provisioner.tree_matches(target, files, owner=owner))
            self.assertEqual(sentinel.read_bytes(), b"preserve prior generation\n")
            self.assertFalse((target / "__pycache__").exists())
            self.assertEqual({item.name for item in target.iterdir()}, set(files))

    def test_operator_no_replace_refuses_existing_generation(self) -> None:
        manifest, files = self._operator_fixture()
        owner = (os.geteuid(), os.getegid())
        with tempfile.TemporaryDirectory(prefix="host-tools-operator-noreplace-") as temporary:
            root = Path(temporary)
            target = root / ("a" * 40)
            target.mkdir()
            sentinel = target / "existing.txt"
            sentinel.write_bytes(b"must stay intact\n")
            with self.assertRaisesRegex(operator_provisioner.ProvisionError, "publication_path_already_exists"):
                operator_provisioner.publish(
                    root, "a" * 40, "12346", manifest, files, rehearsal=True, owner=owner
                )
            self.assertEqual(sentinel.read_bytes(), b"must stay intact\n")
            self.assertFalse((root / (".stage-" + "a" * 40 + "-12346")).exists())

    def test_operator_post_rename_fsync_failure_quarantines_only_new_inode(self) -> None:
        manifest, files = self._operator_fixture()
        owner = (os.geteuid(), os.getegid())
        with tempfile.TemporaryDirectory(prefix="host-tools-operator-fsync-") as temporary:
            root = Path(temporary)
            prior = root / ("b" * 40)
            prior.mkdir()
            sentinel = prior / "prior.txt"
            sentinel.write_bytes(b"old generation remains\n")
            os.chmod(prior, 0o555)
            real_fsync = operator_provisioner.fsync_directory

            failures_left = [1]

            def fail_generation_root(path: Path) -> None:
                if path == root:
                    if failures_left[0]:
                        failures_left[0] -= 1
                        raise OSError("injected parent fsync failure")
                real_fsync(path)

            with patch.object(operator_provisioner, "fsync_directory", side_effect=fail_generation_root):
                with self.assertRaisesRegex(operator_provisioner.ProvisionError, "post_publish_failure_quarantined"):
                    operator_provisioner.publish(
                        root, "a" * 40, "12347", manifest, files, rehearsal=True, owner=owner
                    )
            target = root / ("a" * 40)
            quarantine = root / (".failed-" + "a" * 40 + "-12347")
            self.assertFalse(target.exists())
            self.assertTrue(operator_provisioner.tree_matches(quarantine, files, owner=owner))
            self.assertEqual(sentinel.read_bytes(), b"old generation remains\n")
            os.chmod(prior, 0o700)

    def test_operator_post_rename_tree_check_failure_quarantines_exact_inode(self) -> None:
        manifest, files = self._operator_fixture()
        owner = (os.geteuid(), os.getegid())
        with tempfile.TemporaryDirectory(prefix="host-tools-operator-tree-") as temporary:
            root = Path(temporary)
            prior = root / ("b" * 40)
            prior.mkdir()
            sentinel = prior / "prior.txt"
            sentinel.write_bytes(b"old generation remains\n")
            os.chmod(prior, 0o555)
            real_tree_matches = operator_provisioner.tree_matches
            failures_left = [1]

            def fail_once(path: Path, candidate_files: dict[str, bytes], *, owner: tuple[int, int] = (0, 0)) -> bool:
                if path == root / ("a" * 40) and failures_left[0]:
                    failures_left[0] -= 1
                    return False
                return real_tree_matches(path, candidate_files, owner=owner)

            with patch.object(operator_provisioner, "tree_matches", side_effect=fail_once):
                with self.assertRaisesRegex(operator_provisioner.ProvisionError, "post_publish_failure_quarantined"):
                    operator_provisioner.publish(
                        root, "a" * 40, "12349", manifest, files, rehearsal=True, owner=owner
                    )
            quarantine = root / (".failed-" + "a" * 40 + "-12349")
            self.assertFalse((root / ("a" * 40)).exists())
            self.assertTrue(real_tree_matches(quarantine, files, owner=owner))
            self.assertEqual(sentinel.read_bytes(), b"old generation remains\n")
            os.chmod(prior, 0o700)

    def test_operator_post_rename_unprovable_tree_stops_without_quarantine(self) -> None:
        manifest, files = self._operator_fixture()
        owner = (os.geteuid(), os.getegid())
        with tempfile.TemporaryDirectory(prefix="host-tools-operator-unprovable-") as temporary:
            root = Path(temporary)
            prior = root / ("b" * 40)
            prior.mkdir()
            sentinel = prior / "prior.txt"
            sentinel.write_bytes(b"old generation remains\n")
            os.chmod(prior, 0o555)
            real_fsync = operator_provisioner.fsync_directory

            def mutate_new_tree_after_rename(path: Path) -> None:
                if path == root:
                    target = root / ("a" * 40)
                    os.chmod(target, 0o755)
                    (target / "unexpected-member").write_bytes(b"unknown bytes\n")
                    os.chmod(target, 0o555)
                real_fsync(path)

            with patch.object(operator_provisioner, "fsync_directory", side_effect=mutate_new_tree_after_rename):
                with self.assertRaisesRegex(operator_provisioner.ProvisionError, "post_publish_identity_uncertain"):
                    operator_provisioner.publish(
                        root, "a" * 40, "12350", manifest, files, rehearsal=True, owner=owner
                    )
            target = root / ("a" * 40)
            self.assertTrue(target.exists())
            self.assertEqual((target / "unexpected-member").read_bytes(), b"unknown bytes\n")
            self.assertFalse((root / (".failed-" + "a" * 40 + "-12350")).exists())
            self.assertEqual(sentinel.read_bytes(), b"old generation remains\n")
            target.chmod(0o700)
            for child in target.iterdir():
                child.chmod(0o600)
                child.unlink()
            target.rmdir()
            prior.chmod(0o700)

    def test_operator_receipt_failure_quarantines_exact_published_inode(self) -> None:
        manifest, files = self._operator_fixture()
        owner = (os.geteuid(), os.getegid())
        with tempfile.TemporaryDirectory(prefix="host-tools-operator-receipt-") as temporary:
            root = Path(temporary)

            def fail_receipt(_: dict[str, object]) -> dict[str, object]:
                raise operator_provisioner.ProvisionError("injected_receipt_failure")

            with self.assertRaisesRegex(operator_provisioner.ProvisionError, "receipt_failure_quarantined"):
                operator_provisioner.publish_with_receipt(
                    root, "a" * 40, "12348", manifest, files, root / "receipt",
                    fail_receipt, rehearsal=True, owner=owner,
                )
            self.assertFalse((root / ("a" * 40)).exists())
            self.assertTrue(operator_provisioner.tree_matches(
                root / (".failed-" + "a" * 40 + "-12348"), files, owner=owner
            ))

    def test_operator_missing_generation_log_ignores_echoed_shell_source(self) -> None:
        echoed = (
            b"2026-11-27T00:00:00.000Z ##[group]Run set -euo pipefail\n"
            b"echo 'Exact host-tools generation is not installed yet.'\n"
            b"##[endgroup]\n"
            b"##[error]Process completed with exit code 10.\n"
        )
        with self.assertRaisesRegex(operator_provisioner.ProvisionError, "missing_generation_refusal_unproven"):
            operator_provisioner.parse_missing_generation_log(echoed)
        actual = (
            b"2026-11-27T00:00:00.000Z ##[group]Run set -euo pipefail\n"
            b"echo 'check generation'\n##[endgroup]\n"
            b"2026-11-27T00:00:01.000Z Exact host-tools generation is not installed yet.\n"
            b"2026-11-27T00:00:02.000Z ##[error]Process completed with exit code 10.\n"
        )
        self.assertEqual(operator_provisioner.parse_missing_generation_log(actual), hashlib.sha256(actual).hexdigest())
        per_step = (
            b"2026-11-27T00:00:00.000Z echo 'Exact host-tools generation is not installed yet.'\n"
            b"2026-11-27T00:00:00.000Z ##[endgroup]\n"
            b"2026-11-27T00:00:01.000Z Exact host-tools generation is not installed yet.\n"
            b"2026-11-27T00:00:02.000Z ##[error]Process completed with exit code 10.\n"
        )
        self.assertEqual(operator_provisioner.parse_missing_generation_log(per_step), hashlib.sha256(per_step).hexdigest())
        unframed = (
            b"2026-11-27T00:00:01.000Z Exact host-tools generation is not installed yet.\n"
            b"2026-11-27T00:00:02.000Z ##[error]Process completed with exit code 10.\n"
        )
        self.assertEqual(operator_provisioner.parse_missing_generation_log(unframed), hashlib.sha256(unframed).hexdigest())
        malformed = per_step.replace(b"##[endgroup]\n", b"##[endgroup]\n##[endgroup]\n")
        with self.assertRaisesRegex(operator_provisioner.ProvisionError, "failed_step_log_group_malformed"):
            operator_provisioner.parse_missing_generation_log(malformed)
        unexpected_before_group = per_step.replace(
            b"##[endgroup]\n", b"##[error]Unexpected failure\n##[endgroup]\n",
        )
        with self.assertRaisesRegex(operator_provisioner.ProvisionError, "failed_step_log_unexpected_error"):
            operator_provisioner.parse_missing_generation_log(unexpected_before_group)
        unexpected_after_group = per_step.replace(
            b"##[error]Process completed with exit code 10.\n",
            b"##[error]Unexpected failure\n##[error]Process completed with exit code 10.\n",
        )
        with self.assertRaisesRegex(operator_provisioner.ProvisionError, "failed_step_log_unexpected_error"):
            operator_provisioner.parse_missing_generation_log(unexpected_after_group)

    def test_operator_gh_binary_download_streams_exact_bytes_without_output_flag(self) -> None:
        with tempfile.TemporaryDirectory(prefix="host-tools-gh-output-") as temporary:
            root = Path(temporary)
            gh_root = root / "gh-root"
            gh_root.mkdir(mode=0o700)
            gh_directory = gh_root / "bin"
            gh_directory.mkdir(mode=0o700)
            gh = gh_directory / "gh"
            gh.write_text(
                "#!/bin/sh\n"
                "for arg do [ \"$arg\" = --output ] && { echo unsupported >&2; exit 90; }; done\n"
                "printf '\\000\\377\\033[31mbinary\\n'\n",
                encoding="ascii",
            )
            gh.chmod(0o700)
            output_scratch = root / "output-scratch"
            output_scratch.mkdir(mode=0o700)
            destination = output_scratch / "download.zip"
            args = ["api", "--hostname", "github.com", "repos/StrayForest/old_sparky/actions/artifacts/123/zip"]
            real_writer = operator_provisioner._run_bounded_output_to_file
            invoked: list[list[str]] = []

            def test_writer(argv: list[str], output: Path, **kwargs: object) -> None:
                invoked.append(argv)
                real_writer(argv, output, expected_uid=os.geteuid(), **kwargs)

            real_lstat = Path.lstat

            def root_owned_gh(path: Path) -> SimpleNamespace | os.stat_result:
                metadata = real_lstat(path)
                if path in {gh, gh.parent, gh_root}:
                    return SimpleNamespace(
                        st_mode=metadata.st_mode, st_uid=0, st_gid=0, st_nlink=metadata.st_nlink,
                        st_size=metadata.st_size, st_dev=metadata.st_dev, st_ino=metadata.st_ino,
                        st_mtime_ns=metadata.st_mtime_ns, st_ctime_ns=metadata.st_ctime_ns,
                    )
                return metadata

            with patch.object(Path, "lstat", autospec=True, side_effect=root_owned_gh), patch.object(
                operator_provisioner, "_run_bounded_output_to_file", side_effect=test_writer,
            ):
                gh.chmod(0o755)
                with self.assertRaisesRegex(operator_provisioner.ProvisionError, "pinned_gh_metadata"):
                    operator_provisioner.call_gh(gh, args, output=destination, output_limit=64)
                self.assertFalse(destination.exists())
                gh.chmod(0o700)
                self.assertEqual(
                    operator_provisioner.call_gh(gh, args, output=destination, output_limit=64), b"",
                )
            self.assertEqual(invoked, [[str(gh), *args, "--allow-escape-sequences"]])
            self.assertNotIn("--output", invoked[0])
            self.assertEqual(invoked[0].count("--allow-escape-sequences"), 1)
            self.assertEqual(destination.read_bytes(), b"\x00\xff\x1b[31mbinary\n")
            info = destination.lstat()
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
            self.assertEqual(info.st_nlink, 1)

    def test_operator_verify_gh_checks_private_binary_mode_before_execution(self) -> None:
        with tempfile.TemporaryDirectory(prefix="host-tools-gh-verify-") as temporary:
            root = Path(temporary)
            gh = root / "bin/gh"
            gh.parent.mkdir(mode=0o755)
            gh.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
            gh.chmod(0o700)
            real_lstat = Path.lstat

            def root_owned_gh(path: Path) -> SimpleNamespace | os.stat_result:
                metadata = real_lstat(path)
                if path in {gh, gh.parent, root}:
                    return SimpleNamespace(
                        st_mode=metadata.st_mode, st_uid=0, st_gid=0, st_nlink=metadata.st_nlink,
                        st_size=metadata.st_size, st_dev=metadata.st_dev, st_ino=metadata.st_ino,
                        st_mtime_ns=metadata.st_mtime_ns, st_ctime_ns=metadata.st_ctime_ns,
                    )
                return metadata

            with patch.object(Path, "lstat", autospec=True, side_effect=root_owned_gh):
                gh.chmod(0o755)
                with patch.object(operator_provisioner, "safe_run") as safe_run:
                    with self.assertRaisesRegex(operator_provisioner.ProvisionError, "pinned_gh_metadata"):
                        operator_provisioner.verify_gh(gh)
                    safe_run.assert_not_called()

                gh.chmod(0o700)
                with patch.object(
                    operator_provisioner,
                    "safe_run",
                    side_effect=[
                        subprocess.CompletedProcess([str(gh), "version"], 0, b"gh version 2.105.0 test\n", b""),
                        subprocess.CompletedProcess([str(gh), "attestation", "verify", "--help"], 0, b"help\n", b""),
                    ],
                ) as safe_run:
                    operator_provisioner.verify_gh(gh)
                    self.assertEqual(safe_run.call_count, 2)

    def test_operator_gh_binary_download_enforces_bound_during_stream(self) -> None:
        with tempfile.TemporaryDirectory(prefix="host-tools-gh-bound-") as temporary:
            root = Path(temporary)
            child = root / "emit"
            child.write_text("#!/bin/sh\nprintf 12345\n", encoding="ascii")
            child.chmod(0o755)
            output = root / "output.zip"
            with self.assertRaisesRegex(
                operator_provisioner.ProvisionError, "github_api_download_output_bound",
            ):
                operator_provisioner._run_bounded_output_to_file(
                    [str(child)], output, output_limit=4, timeout=5, env={}, expected_uid=os.geteuid(),
                )
            self.assertFalse(output.exists())

    def test_operator_gh_binary_download_cleans_failure_timeout_and_preserves_existing_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="host-tools-gh-failure-") as temporary:
            root = Path(temporary)
            child = root / "emit"
            child.write_text("#!/bin/sh\nprintf partial\necho private-diagnostic >&2\nexit 7\n", encoding="ascii")
            child.chmod(0o755)
            output = root / "failed.zip"
            with self.assertRaisesRegex(operator_provisioner.ProvisionError, "github_api_download_failed") as failure:
                operator_provisioner._run_bounded_output_to_file(
                    [str(child)], output, output_limit=32, timeout=5, env={}, expected_uid=os.geteuid(),
                )
            self.assertNotIn("private-diagnostic", str(failure.exception))
            self.assertFalse(output.exists())

            child.write_text("#!/bin/sh\nprintf partial\nsleep 5\n", encoding="ascii")
            with self.assertRaisesRegex(operator_provisioner.ProvisionError, "github_api_download_timeout"):
                operator_provisioner._run_bounded_output_to_file(
                    [str(child)], output, output_limit=32, timeout=1, env={}, expected_uid=os.geteuid(),
                )
            self.assertFalse(output.exists())

            sentinel = root / "sentinel.zip"
            sentinel.write_bytes(b"preserve me")
            with self.assertRaisesRegex(
                operator_provisioner.ProvisionError, "github_api_download_destination_exists",
            ):
                operator_provisioner._run_bounded_output_to_file(
                    [str(child)], sentinel, output_limit=32, timeout=1, env={}, expected_uid=os.geteuid(),
                )
            self.assertEqual(sentinel.read_bytes(), b"preserve me")

            target = root / "target.zip"
            target.write_bytes(b"preserve target")
            link = root / "link.zip"
            link.symlink_to(target)
            with self.assertRaisesRegex(
                operator_provisioner.ProvisionError, "github_api_download_destination_exists",
            ):
                operator_provisioner._run_bounded_output_to_file(
                    [str(child)], link, output_limit=32, timeout=1, env={}, expected_uid=os.geteuid(),
                )
            self.assertEqual(target.read_bytes(), b"preserve target")

    def test_operator_rejects_partial_artifact_owner_account(self) -> None:
        existing = SimpleNamespace(pw_uid=700, pw_gid=701, pw_dir="/nonexistent", pw_shell="/usr/sbin/nologin")
        with patch.object(operator_provisioner.pwd, "getpwnam", return_value=existing), patch.object(
            operator_provisioner.grp, "getgrnam", side_effect=KeyError
        ):
            with self.assertRaisesRegex(operator_provisioner.ProvisionError, "artifact_owner_partial_account"):
                operator_provisioner.validate_account_contract("b" * 40)

    def test_operator_preflight_accepts_only_closed_mode_specific_skips(self) -> None:
        jobs = self._preflight_jobs_fixture()
        result = operator_provisioner.verify_preflight_job_set(jobs, mode="preflight")
        self.assertEqual(result["failed_job_id"], 3)

        masked_jobs = self._preflight_jobs_fixture()
        masked_jobs[2]["steps"][0]["name"] = "Validate *** SSH identity and installed generation"
        self.assertEqual(
            operator_provisioner.verify_preflight_job_set(masked_jobs, mode="preflight")["failed_job_id"], 3,
        )

        for bad_name in (
            "Validate **** SSH identity and installed generation",
            "Validate root SSH identity and installed generation (masked)",
        ):
            invalid_jobs = self._preflight_jobs_fixture()
            invalid_jobs[2]["steps"][0]["name"] = bad_name
            with self.subTest(step_name=bad_name), self.assertRaisesRegex(
                operator_provisioner.ProvisionError, "preflight_failed_step_mismatch",
            ):
                operator_provisioner.verify_preflight_job_set(invalid_jobs, mode="preflight")

        duplicate_failed_steps = self._preflight_jobs_fixture()
        duplicate_failed_steps[2]["steps"].append({
            "number": 6,
            "name": "Validate *** SSH identity and installed generation",
            "status": "completed", "conclusion": "failure",
        })
        with self.assertRaisesRegex(operator_provisioner.ProvisionError, "preflight_failed_step_mismatch"):
            operator_provisioner.verify_preflight_job_set(duplicate_failed_steps, mode="preflight")

        cancelled_alias_step = self._preflight_jobs_fixture()
        cancelled_alias_step[2]["steps"].append({
            "number": 6,
            "name": "Validate *** SSH identity and installed generation",
            "status": "completed", "conclusion": "cancelled",
        })
        with self.assertRaisesRegex(operator_provisioner.ProvisionError, "preflight_other_step_failure"):
            operator_provisioner.verify_preflight_job_set(cancelled_alias_step, mode="preflight")

        self.assertEqual(result["failed_step_number"], 5)
        for number in (True, 0, -1, "5", 5.0, None):
            invalid_number_jobs = self._preflight_jobs_fixture()
            invalid_number_jobs[2]["steps"][0]["number"] = number
            with self.subTest(step_number=number), self.assertRaisesRegex(
                operator_provisioner.ProvisionError, "preflight_failed_step_number",
            ):
                operator_provisioner.verify_preflight_job_set(invalid_number_jobs, mode="preflight")

        high_step_number = self._preflight_jobs_fixture()
        high_step_number[2]["steps"][0]["number"] = 101
        self.assertEqual(
            operator_provisioner.verify_preflight_job_set(high_step_number, mode="preflight")["failed_step_number"],
            101,
        )

        duplicate_numbers = self._preflight_jobs_fixture()
        duplicate_numbers[2]["steps"].append({
            "number": 5, "name": "Another step", "status": "completed", "conclusion": "success",
        })
        with self.assertRaisesRegex(operator_provisioner.ProvisionError, "preflight_failed_step_number"):
            operator_provisioner.verify_preflight_job_set(duplicate_numbers, mode="preflight")

        with tempfile.TemporaryDirectory(prefix="host-tools-failed-step-log-") as temporary:
            scratch = Path(temporary)
            response = (
                b"##[group]Run verify generation\n##[endgroup]\n"
                b"Exact host-tools generation is not installed yet.\n"
                b"##[error]Process completed with exit code 10.\n"
            )
            calls: list[tuple[list[str], Path, int]] = []

            def write_step_response(
                gh: Path, args: list[str], *, output: Path, output_limit: int, **kwargs: object,
            ) -> bytes:
                calls.append((args, output, output_limit))
                output.write_bytes(response)
                output.chmod(0o600)
                return b""

            real_lstat = Path.lstat

            def root_owned_step_log(path: Path) -> SimpleNamespace | os.stat_result:
                metadata = real_lstat(path)
                if path == scratch / "failed-step.log":
                    return SimpleNamespace(
                        st_mode=metadata.st_mode, st_uid=0, st_gid=0, st_nlink=metadata.st_nlink,
                        st_size=metadata.st_size, st_dev=metadata.st_dev, st_ino=metadata.st_ino,
                        st_mtime_ns=metadata.st_mtime_ns, st_ctime_ns=metadata.st_ctime_ns,
                    )
                return metadata

            with patch.object(Path, "lstat", autospec=True, side_effect=root_owned_step_log), patch.object(
                operator_provisioner, "call_gh", side_effect=write_step_response,
            ):
                raw = operator_provisioner.fetch_failed_step_log(
                    Path("/pinned/gh"), scratch,
                    job_id=result["failed_job_id"], step_number=result["failed_step_number"],
                )
            self.assertEqual(raw, response)
            log_sha = hashlib.sha256(response).hexdigest()
            self.assertEqual(
                calls[0][0][-1], f"repos/{operator_provisioner.REPOSITORY}/actions/jobs/3/steps/5/logs",
            )
            self.assertEqual(calls[0][0][3:5], ["--header", "X-GitHub-Api-Version: 2026-03-10"])
            self.assertEqual(calls[0][2], 2 * 1024 * 1024)
            self.assertEqual(operator_provisioner.parse_missing_generation_log(raw), log_sha)

            (scratch / "failed-step.log").unlink()

            def write_oversized_step_response(
                gh: Path, args: list[str], *, output: Path, output_limit: int, **kwargs: object,
            ) -> bytes:
                output.write_bytes(b"x" * (output_limit + 1))
                output.chmod(0o600)
                return b""

            with patch.object(Path, "lstat", autospec=True, side_effect=root_owned_step_log), patch.object(
                operator_provisioner, "call_gh", side_effect=write_oversized_step_response,
            ), self.assertRaisesRegex(operator_provisioner.ProvisionError, "input_file_metadata"):
                operator_provisioner.fetch_failed_step_log(Path("/pinned/gh"), scratch, job_id=3, step_number=5)

        with self.assertRaisesRegex(operator_provisioner.ProvisionError, "preflight_mode_not_proven"):
            operator_provisioner.verify_preflight_job_set(jobs, mode="deploy")
        jobs.append({"id": 99, "name": "Unrelated skipped gate", "status": "completed", "conclusion": "skipped"})
        with self.assertRaisesRegex(operator_provisioner.ProvisionError, "preflight_unexpected_skipped_job"):
            operator_provisioner.verify_preflight_job_set(jobs, mode="preflight")

    def test_operator_attestation_source_mismatch_stops_before_publication(self) -> None:
        invocation = "https://github.com/StrayForest/old_sparky/actions/runs/12345/attempts/1"
        rows = [{"verificationResult": {
            "verifiedTimestamps": ["2026-11-27T00:00:00Z"],
            "statement": {
                "subject": [{"name": "platform-host-tools-bundle.zip", "digest": {"sha256": "c" * 64}}],
                "predicateType": "https://slsa.dev/provenance/v1",
                "predicate": {
                    "buildDefinition": {
                        "externalParameters": {"workflow": {
                            "repository": "https://github.com/StrayForest/old_sparky",
                            "path": ".github/workflows/platform-production-deploy.yml",
                            "ref": "refs/heads/dev",
                        }},
                        "resolvedDependencies": [{
                            "uri": "git+https://github.com/StrayForest/old_sparky@refs/heads/dev",
                            "digest": {"gitCommit": "a" * 40},
                        }],
                    },
                    "runDetails": {"metadata": {"invocationId": invocation}},
                },
            },
            "signature": {"certificate": {
                "sourceRepositoryURI": "https://github.com/StrayForest/old_sparky",
                "sourceRepositoryRef": "refs/heads/dev",
                "sourceRepositoryDigest": "d" * 40,
                "githubWorkflowRepository": "StrayForest/old_sparky",
                "githubWorkflowName": "Platform production deploy",
                "githubWorkflowRef": "refs/heads/dev",
                "githubWorkflowSHA": "a" * 40,
                "githubWorkflowTrigger": "workflow_dispatch",
                "runInvocationURI": invocation,
            }},
        }}]
        completed = subprocess.CompletedProcess([], 0, json.dumps(rows).encode(), b"")
        with tempfile.TemporaryDirectory(prefix="host-tools-attestation-source-") as temporary:
            with patch.object(operator_provisioner, "safe_run", return_value=completed):
                with self.assertRaisesRegex(operator_provisioner.ProvisionError, "attestation_certificate_binding"):
                    operator_provisioner.verify_attestation(
                        Path("/root/.private-gh/bin/gh"), Path(temporary) / "bundle.zip",
                        target_sha="a" * 40, preflight_run_id="12345", preflight_attempt="1",
                        bundle_sha256="c" * 64, scratch=Path(temporary),
                    )
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_installed_checker_reports_closed_stage_for_remote_nonzero(self) -> None:
        cases = (
            ("member_digest", 255, "reason=ssh_nonzero ssh_exit_code=255"),
            ("locked_final_check", -9, "reason=ssh_signal signal=9"),
        )
        for stage, returncode, detail in cases:
            with self.subTest(stage=stage, returncode=returncode):
                output = StringIO()
                failure = installed_check.RemoteCommandFailure(
                    returncode, stage=stage
                )
                with patch.object(
                    installed_check,
                    "verify_installed_generation",
                    side_effect=failure,
                ), redirect_stderr(output):
                    result = installed_check.main(
                        [
                            "--source-sha",
                            SOURCE_SHA,
                            "--toolset-version",
                            "production-host-tools-v4",
                            "--contract-dir",
                            "/contract-fixture",
                        ]
                    )
                self.assertEqual(result, 1)
                self.assertEqual(
                    output.getvalue(),
                    "HOST_TOOLS_VERIFY schema=1 status=failed "
                    f"stage={stage} {detail}\n",
                )
                self.assertNotIn("/contract-fixture", output.getvalue())

    def test_installed_checker_propagates_remote_stage_and_nonzero_status(self) -> None:
        with self.assertRaises(installed_check.RemoteCommandFailure) as raised:
            installed_check._run(
                ["/bin/sh", "-c", "exit 27"],
                timeout=3,
                diagnostic_stage="member_digest",
            )
        self.assertEqual(raised.exception.stage, "member_digest")
        self.assertEqual(raised.exception.returncode, 27)

    def test_installed_checker_rejects_unbounded_remote_diagnostic_values(self) -> None:
        for stage, returncode in (
            ("/tmp/secret", 1),
            ("member_digest", 0),
            ("member_digest", 256),
            ("member_digest", True),
        ):
            with self.subTest(stage=stage, returncode=returncode):
                with self.assertRaises(ValueError):
                    installed_check.RemoteCommandFailure(
                        returncode, stage=stage
                    )

    def test_installed_checker_caps_stdout_while_child_is_running(self) -> None:
        child = [
            sys.executable,
            "-c",
            "import os,time; os.write(1,b'x'*1048576); time.sleep(10)",
        ]
        started = time.monotonic()
        with self.assertRaises(installed_check.VerificationError):
            installed_check._run(child, timeout=3, max_output=128)
        self.assertLess(time.monotonic() - started, 2)

    def _assert_locked_final_check_executes_structured_v4_capabilities(self) -> None:
        with tempfile.TemporaryDirectory(prefix="locked-host-tools-") as temporary:
            root = Path(temporary)
            lock_dir = root / "locks"
            lock_dir.mkdir(mode=0o700)
            generation = root / "host-tools" / SOURCE_SHA
            generation.mkdir(parents=True)
            marker_dir = root / "shared"
            marker_dir.mkdir(mode=0o700)
            lock_paths = (
                lock_dir / "release.lock",
                lock_dir / "liveqa.lock",
            )
            for path in lock_paths:
                path.write_bytes(b"")
                path.chmod(0o600)

            layout = bundle._layout_for_files(bundle.CPU_DIAGNOSTIC_HOST_TOOL_FILES)
            expected_summary = (
                f"HOST_TOOLS schema=1 source_sha={SOURCE_SHA} generation={SOURCE_SHA} dispatcher=4 "
                "artifact_prepare=2 supervisor=3 input_guard=2 release_baseline=1 "
                "retained_load_export_cleanup=1 retained_load_source_binding=1 "
                "cpu_diagnostic_plan_control=1 python_isolated=1 python_bytecode_disabled=1\n"
            )
            structured_capabilities = bundle._capabilities_text(SOURCE_SHA, layout)
            self.assertIn(b"schema=1\ntoolset_version=production-host-tools-v4\n", structured_capabilities)
            self.assertIn(b"capability=cpu_diagnostic_plan_control\n", structured_capabilities)
            self.assertNotEqual(structured_capabilities, expected_summary.encode("ascii"))

            tool_contents: dict[str, bytes] = {}
            for name in layout["files"]:
                if name == "platform_workflow_remote_dispatch.py":
                    content = (
                        "import sys\n"
                        f"sys.stdout.write({expected_summary!r})\n"
                    ).encode("ascii")
                else:
                    content = f"fixture host tool {name}\n".encode("ascii")
                tool_contents[name] = content
                path = generation / name
                path.write_bytes(content)
                path.chmod(0o555)

            capability_hash = hashlib.sha256(structured_capabilities).hexdigest()
            records = [
                {"path": name, "sha256": hashlib.sha256(content).hexdigest(), "mode": 0o555}
                for name, content in tool_contents.items()
            ]
            records.append({"path": "capabilities.txt", "sha256": capability_hash, "mode": 0o444})
            records.sort(key=lambda row: row["path"])
            manifest = bundle._canonical_json(bundle._manifest(SOURCE_SHA, records, layout))
            (generation / "manifest.json").write_bytes(manifest)
            (generation / "manifest.json").chmod(0o444)
            (generation / "capabilities.txt").write_bytes(structured_capabilities)
            (generation / "capabilities.txt").chmod(0o444)
            generation.chmod(0o555)

            digests = [
                (hashlib.sha256(content).hexdigest(), name)
                for name, content in tool_contents.items()
            ]
            digests.append((capability_hash, "capabilities.txt"))
            digests.sort(key=lambda row: row[1])
            manifest_hash = hashlib.sha256(manifest).hexdigest()
            members = installed_check._locked_generation_members(
                digests,
                "production-host-tools-v4",
                manifest_hash,
                capability_hash,
            )
            payload = {
                "generation": str(generation),
                "source_sha": SOURCE_SHA,
                "toolset_version": "production-host-tools-v4",
                "manifest_sha256": manifest_hash,
                "capabilities_sha256": capability_hash,
                "expected_member_count": len(members),
                "members": members,
            }
            encoded_payload = base64.b64encode(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
            ).decode("ascii")

            check_code = installed_check.LOCKED_FINAL_CHECK
            replacements = {
                f'"{installed_check.RELEASE_LOCK_PATH}"': json.dumps(str(lock_paths[0])),
                f'"{installed_check.LIVE_QA_LOCK_PATH}"': json.dumps(str(lock_paths[1])),
                '"/opt/oldsparky/platform/shared/.release-operation.json"': json.dumps(
                    str(marker_dir / ".release-operation.json")
                ),
                '"/usr/bin/python3.12"': json.dumps(sys.executable),
                "st_uid != 0": f"st_uid != {os.geteuid()}",
                "st_gid != 0": f"st_gid != {os.getegid()}",
            }
            for original, replacement in replacements.items():
                self.assertIn(original, check_code)
                check_code = check_code.replace(original, replacement)
            encoded_code = base64.b64encode(check_code.encode("ascii")).decode("ascii")
            launcher = textwrap.dedent(
                """
                import base64, json, sys
                encoded_code = sys.argv[1]
                payload = sys.argv[2]
                exceptions = []
                def trace(frame, event, arg):
                    if frame.f_code.co_filename == "<locked-final-check>" and event == "exception":
                        exception_type, _exception_value, _traceback = arg
                        if exception_type is not SystemExit:
                            exceptions.append({"class": exception_type.__name__, "phase": frame.f_code.co_name, "line": frame.f_lineno})
                    return trace
                sys.argv = ["-c", payload]
                code = 1
                try:
                    sys.settrace(trace)
                    exec(compile(base64.b64decode(encoded_code), "<locked-final-check>", "exec"))
                except SystemExit as exc:
                    code = exc.code if isinstance(exc.code, int) else 1
                finally:
                    sys.settrace(None)
                if exceptions:
                    print(json.dumps(exceptions[-4:], separators=(",", ":")), file=sys.stderr)
                raise SystemExit(code)
                """
            )
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-c", launcher, encoded_code, encoded_payload],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout or "no verifier diagnostic")
            self.assertEqual(
                result.stdout,
                f"HOST_TOOLS_READY schema=1 source_sha={SOURCE_SHA} "
                f"manifest_sha256={manifest_hash}\n",
            )
            self.assertEqual(result.stderr, "")

    def _installed_checker_contract(self, root: Path) -> Path:
        contract = root / "contract"
        contract.mkdir()
        tool = b"fixture host tool\n"
        (contract / "platform_example.py").write_bytes(tool)
        (contract / "files.sha256").write_text(
            f"{hashlib.sha256(tool).hexdigest()} platform_example.py\n", encoding="ascii"
        )
        (contract / "files.modes").write_text("555 platform_example.py\n", encoding="ascii")
        (contract / "manifest.sha256").write_text(f"{'b' * 64}\n", encoding="ascii")
        (contract / "capabilities.sha256").write_text(f"{'c' * 64}\n", encoding="ascii")
        (contract / "source_sha").write_text(f"{SOURCE_SHA}\n", encoding="ascii")
        (contract / "toolset_version").write_text("production-host-tools-v4\n", encoding="ascii")
        for path in contract.iterdir():
            path.chmod(0o600)
        return contract

    def _temporary_ed25519_private_key(self, root: Path) -> str:
        key_path = root / "fixture-id-ed25519"
        subprocess.run(
            ["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=True,
        )
        key_path.chmod(0o600)
        return key_path.read_text(encoding="ascii")

    def test_host_generation_checker_removes_created_ssh_material_on_failure_before_export(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner_temp = root / "runner-temp"
            runner_temp.mkdir()
            contract = self._installed_checker_contract(root)
            ssh_dir = runner_temp / "platform-host-capability-ssh"
            github_env = root / "github-env"

            def create_ssh(*_args: object, **_kwargs: object) -> tuple[Path, list[str], bool]:
                ssh_dir.mkdir(mode=0o700)
                (ssh_dir / "id_ed25519").write_text("synthetic-private-key\n", encoding="ascii")
                (ssh_dir / "id_ed25519").chmod(0o600)
                return ssh_dir, ["ssh", "fixed"], True

            def remote(_argv: list[str], *args: str, **_kwargs: object) -> bytes:
                if args == ("/usr/bin/id", "-u"):
                    return b"0\n"
                if args[:3] == ("/usr/bin/test", "-d", f"{installed_check.GENERATION_ROOT}/{SOURCE_SHA}"):
                    raise installed_check.RemoteCommandFailure(
                        1, stage="generation_presence"
                    )
                raise AssertionError("unexpected remote operation")

            with patch.dict(
                os.environ,
                {
                    "RUNNER_TEMP": str(runner_temp),
                    "GITHUB_ENV": str(github_env),
                    "PROD_SSH_HOST": "production.example.test",
                    "PROD_SSH_USER": "operator",
                    "PROD_SSH_KEY": "synthetic-private-key",
                },
            ), patch.object(installed_check, "_safe_host_ssh", side_effect=create_ssh), patch.object(
                installed_check, "_remote", side_effect=remote
            ):
                output = StringIO()
                with redirect_stdout(output):
                    result = installed_check.main(
                        ["--source-sha", SOURCE_SHA, "--toolset-version", "production-host-tools-v4",
                         "--contract-dir", str(contract)]
                    )

            self.assertEqual(result, 10)
            self.assertFalse(ssh_dir.exists())
            self.assertFalse(github_env.exists())
            self.assertNotIn("synthetic-private-key", output.getvalue())

    def test_shared_host_generation_verifier_accepts_exact_generation_and_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner_temp = root / "runner-temp"
            runner_temp.mkdir()
            contract = self._installed_checker_contract(root)
            (contract / "platform_example.py").unlink()
            contract_rows: list[tuple[str, str]] = []
            for index in range(15):
                name = f"platform_tool_{index}.py"
                content = f"fixture host tool {index}\n".encode("ascii")
                (contract / name).write_bytes(content)
                contract_rows.append((hashlib.sha256(content).hexdigest(), name))
            contract_rows.append(("c" * 64, "capabilities.txt"))
            (contract / "files.sha256").write_text(
                "".join(f"{digest} {name}\n" for digest, name in contract_rows),
                encoding="ascii",
            )
            (contract / "files.modes").write_text(
                "".join(f"{'444' if name == 'capabilities.txt' else '555'} {name}\n" for _, name in contract_rows),
                encoding="ascii",
            )
            private_key = self._temporary_ed25519_private_key(root)
            private_key_path = root / "fixture-id-ed25519"
            public_key_fields = subprocess.run(
                ["/usr/bin/ssh-keygen", "-y", "-P", "", "-f", str(private_key_path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=True,
            ).stdout.decode("ascii").split()
            self.assertGreaterEqual(len(public_key_fields), 2)
            expected_host_key = f"production.example.test {public_key_fields[0]} {public_key_fields[1]}"
            scan_path = root / "host-key.scan"
            scan_path.write_text(expected_host_key + "\n", encoding="ascii")
            actual_fingerprint = subprocess.run(
                ["/usr/bin/ssh-keygen", "-lf", str(scan_path), "-E", "sha256"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=True,
            ).stdout.decode("ascii").split()[1]
            generation = f"{installed_check.GENERATION_ROOT}/{SOURCE_SHA}"
            digests, _modes, manifest_hash, capabilities_hash = installed_check._contract_rows(contract)
            expected_names = {"manifest.json", "capabilities.txt", *(path for _digest, path in digests)}
            payloads = {path: digest for digest, path in digests}
            payloads.update({"manifest.json": manifest_hash, "capabilities.txt": capabilities_hash})
            ssh_dir = runner_temp / "platform-host-capability-ssh"
            capability = (
                f"HOST_TOOLS schema=1 source_sha={SOURCE_SHA} generation={SOURCE_SHA} dispatcher=4 artifact_prepare=2 "
                "supervisor=3 input_guard=2 release_baseline=1 retained_load_export_cleanup=1 "
                "retained_load_source_binding=1 cpu_diagnostic_plan_control=1 python_isolated=1 python_bytecode_disabled=1\n"
            ).encode("ascii")

            def local_command(argv: list[str], **_kwargs: object) -> bytes:
                if argv[0] == "/usr/bin/timeout":
                    self.assertEqual(
                        argv,
                        ["/usr/bin/timeout", "--foreground", "4s", "/usr/bin/ssh-keyscan", "-T", "3", "-t", "ed25519", "production.example.test"],
                    )
                    return f"# SSH-2.0-OpenSSH_10.2\n\n{expected_host_key}\n".encode("ascii")
                if argv[0] == "/usr/bin/ssh":
                    remote_argv = shlex.split(argv[-1])
                    self.assertEqual(remote_argv[:3], ["/usr/bin/python3.12", "-I", "-B"])
                    self.assertEqual(remote_argv[3], "-c")
                    payload = json.loads(base64.b64decode(remote_argv[-1], validate=True))
                    members = payload["members"]
                    expected_count = 16 if payload["toolset_version"] == "production-host-tools-v3" else 17
                    self.assertEqual(payload["expected_member_count"], expected_count)
                    names = [row["name"] for row in members]
                    self.assertEqual(len(names), expected_count)
                    self.assertEqual(len(set(names)), expected_count)
                    self.assertEqual(names.count("capabilities.txt"), 1)
                    self.assertEqual(names.count("manifest.json"), 1)
                    self.assertEqual(
                        next(row["mode"] for row in members if row["name"] == "capabilities.txt"),
                        0o444,
                    )
                    self.assertEqual(
                        next(row["mode"] for row in members if row["name"] == "manifest.json"),
                        0o444,
                    )
                    self.assertEqual(sum(row["mode"] == 0o555 for row in members), expected_count - 2)
                    return (
                        f"HOST_TOOLS_READY schema=1 source_sha={SOURCE_SHA} "
                        f"manifest_sha256={manifest_hash}\n"
                    ).encode("ascii")
                if argv[:2] == ["/usr/bin/ssh-keygen", "-lf"]:
                    self.assertIn(argv[2], {str(ssh_dir / "known_hosts.scan"), str(ssh_dir / "known_hosts")})
                    self.assertEqual(argv[3:], ["-E", "sha256"])
                    return subprocess.run(
                        argv,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                        check=True,
                    ).stdout
                raise AssertionError(f"unexpected local SSH helper command: {argv!r}")

            def remote(_argv: list[str], *args: str, **kwargs: object) -> bytes:
                self.assertEqual(_argv[:4], ["/usr/bin/ssh", "-n", "-T", "-o"])
                self.assertIn("StrictHostKeyChecking yes", (ssh_dir / "config").read_text(encoding="ascii"))
                self.assertEqual(
                    installed_check._parse_scanned_host_key(
                        (ssh_dir / "known_hosts").read_bytes(), "production.example.test"
                    ),
                    expected_host_key,
                )
                if args == ("/usr/bin/id", "-u"):
                    return b"0\n"
                if args == ("/usr/bin/test", "-d", generation):
                    return b""
                if args == ("/usr/bin/test", "!", "-L", generation):
                    return b""
                if args[:3] == ("/usr/bin/stat", "-c", "%F:%u:%g:%h:%a"):
                    path = args[-1]
                    if path == generation:
                        return b"directory:0:0:2:555\n"
                    relative = path.removeprefix(generation + "/")
                    mode = "444" if relative in {"manifest.json", "capabilities.txt"} else "555"
                    return f"regular file:0:0:1:{mode}\n".encode("ascii")
                if args[:2] == ("/usr/bin/sha256sum", "--"):
                    path = args[2]
                    relative = path.removeprefix(generation + "/")
                    return f"{payloads[relative]}  {path}\n".encode("ascii")
                if args[:2] == ("/usr/bin/find", "--"):
                    return b"".join(
                        f"{generation}/{name}".encode("ascii") + b"\0"
                        for name in sorted(expected_names)
                    )
                if args[:3] == ("/usr/bin/python3.12", "-I", "-B"):
                    self.assertEqual(args[3:], (f"{generation}/platform_workflow_remote_dispatch.py", "host-capabilities"))
                    self.assertEqual(kwargs.get("max_output"), 512)
                    return capability
                raise AssertionError(f"unexpected fixed remote command: {args!r}")

            with patch.object(installed_check, "HOST_KEY_FINGERPRINT", actual_fingerprint), patch.dict(
                os.environ,
                {
                    "RUNNER_TEMP": str(runner_temp),
                    "PROD_SSH_HOST": "production.example.test",
                    "PROD_SSH_USER": "operator",
                    "PROD_SSH_KEY": private_key,
                },
            ), patch.object(installed_check, "_run", side_effect=local_command), patch.object(
                installed_check, "_remote", side_effect=remote
            ):
                checked_dir, checked_generation, created = installed_check.verify_installed_generation(
                    SOURCE_SHA, "production-host-tools-v4", contract
                )
            self.assertEqual(checked_dir, ssh_dir)
            self.assertEqual(str(checked_generation), generation)
            self.assertTrue(created)
            self.assertFalse(ssh_dir.exists())
            v3_rows = [
                (f"{index:064x}", f"platform_tool_{index}.py")
                for index in range(14)
            ] + [("d" * 64, "capabilities.txt")]
            with patch.object(installed_check, "_run", side_effect=local_command):
                self.assertTrue(installed_check._locked_final_generation_check(
                    ["/usr/bin/ssh", "-n", "-T", "-o", "BatchMode=yes"],
                    sha=SOURCE_SHA,
                    toolset_version="production-host-tools-v3",
                    generation=generation,
                    digests=v3_rows,
                    manifest_hash="b" * 64,
                    capabilities_hash="c" * 64,
                ))

            v4_rows = [
                (f"{index:064x}", f"platform_tool_{index}.py")
                for index in range(15)
            ] + [("c" * 64, "capabilities.txt")]
            locked_args = {
                "sha": SOURCE_SHA,
                "toolset_version": "production-host-tools-v4",
                "generation": generation,
                "digests": v4_rows,
                "manifest_hash": "b" * 64,
                "capabilities_hash": "c" * 64,
            }
            for returncode in (75, 1, 255):
                with self.subTest(locked_final_check_returncode=returncode):
                    with patch.object(
                        installed_check,
                        "_run",
                        side_effect=installed_check.RemoteCommandFailure(
                            returncode, stage="locked_final_check"
                        ),
                    ):
                        if returncode == 75:
                            self.assertFalse(
                                installed_check._locked_final_generation_check(
                                    ["/usr/bin/ssh"], **locked_args
                                )
                            )
                        else:
                            with self.assertRaises(installed_check.RemoteCommandFailure):
                                installed_check._locked_final_generation_check(
                                    ["/usr/bin/ssh"], **locked_args
                                )

            self._assert_locked_final_check_executes_structured_v4_capabilities()

            # The deploy workflow can precreate this directory from its own
            # banner-tolerant scan before calling the shared verifier. Reuse
            # that exact persisted-file shape and verify it against ssh-keygen.
            ssh_dir.mkdir(mode=0o700)
            (ssh_dir / "id_ed25519").write_text(private_key.rstrip("\n") + "\n", encoding="ascii")
            (ssh_dir / "id_ed25519").chmod(0o600)
            (ssh_dir / "known_hosts").write_text(
                f"# SSH-2.0-OpenSSH_10.2\n\n{expected_host_key}\n", encoding="ascii"
            )
            (ssh_dir / "known_hosts").chmod(0o600)
            (ssh_dir / "config").write_text(
                "Host *\n  StrictHostKeyChecking yes\n"
                f"  UserKnownHostsFile {ssh_dir / 'known_hosts'}\n",
                encoding="ascii",
            )
            (ssh_dir / "config").chmod(0o600)
            with patch.object(installed_check, "HOST_KEY_FINGERPRINT", actual_fingerprint), patch.dict(
                os.environ,
                {
                    "RUNNER_TEMP": str(runner_temp),
                    "PROD_SSH_HOST": "production.example.test",
                    "PROD_SSH_USER": "operator",
                    "PROD_SSH_KEY": private_key,
                },
            ), patch.object(installed_check, "_run", side_effect=local_command), patch.object(
                installed_check, "_remote", side_effect=remote
            ):
                checked_dir, checked_generation, created = installed_check.verify_installed_generation(
                    SOURCE_SHA, "production-host-tools-v4", contract
                )
            self.assertEqual(checked_dir, ssh_dir)
            self.assertEqual(str(checked_generation), generation)
            self.assertFalse(created)
            self.assertTrue(ssh_dir.exists())
            self.assertEqual(
                (ssh_dir / "known_hosts").read_text(encoding="ascii"),
                f"# SSH-2.0-OpenSSH_10.2\n\n{expected_host_key}\n",
            )

            with patch.object(
                installed_check,
                "_run",
                return_value=b"256 SHA256:wrong production.example.test (ED25519)\n",
            ):
                with self.assertRaisesRegex(
                    installed_check.VerificationError, "does not match the pinned fingerprint"
                ):
                    installed_check._safe_host_ssh(
                        "production.example.test", "operator", private_key, runner_temp
                    )

            known_hosts_path = ssh_dir / "known_hosts"
            config_path = ssh_dir / "config"
            original_known_hosts = known_hosts_path.read_bytes()
            original_config = config_path.read_bytes()
            for contents in (
                f"# SSH-2.0-OpenSSH_10.2\n{expected_host_key}\n{expected_host_key}\n".encode("ascii"),
                b"# SSH-2.0-OpenSSH_10.2\nother.example.test ssh-ed25519 AAAA\n",
            ):
                known_hosts_path.write_bytes(contents)
                known_hosts_path.chmod(0o600)
                with patch.object(installed_check, "_run") as run:
                    with self.assertRaises(installed_check.VerificationError):
                        installed_check._safe_host_ssh(
                            "production.example.test", "operator", private_key, runner_temp
                        )
                run.assert_not_called()
            known_hosts_path.write_bytes(original_known_hosts)
            known_hosts_path.chmod(0o600)

            config_path.write_text(
                "Host *\n  StrictHostKeyChecking no\n"
                f"  UserKnownHostsFile {known_hosts_path}\n",
                encoding="ascii",
            )
            config_path.chmod(0o600)
            with patch.object(installed_check, "_run") as run:
                with self.assertRaisesRegex(
                    installed_check.VerificationError, "config is not bound to known hosts"
                ):
                    installed_check._safe_host_ssh(
                        "production.example.test", "operator", private_key, runner_temp
                    )
            run.assert_not_called()
            config_path.write_bytes(original_config)
            config_path.chmod(0o600)

            known_hosts_path.chmod(0o640)
            with patch.object(installed_check, "_run") as run:
                with self.assertRaisesRegex(installed_check.VerificationError, "files are unsafe"):
                    installed_check._safe_host_ssh(
                        "production.example.test", "operator", private_key, runner_temp
                    )
            run.assert_not_called()
            valid = "production.example.test ssh-ed25519 AAAA"
        invalid_outputs = (
            b"",
            b"# SSH-2.0-OpenSSH_10.2\n",
            f"{valid}\n{valid}\n".encode("ascii"),
            b"other.example.test ssh-ed25519 AAAA\n",
            b"production.example.test ssh-rsa AAAA\n",
            b"production.example.test ssh-ed25519\n",
            b"production.example.test ssh-ed25519 AAAA extra\n",
            b"production.example.test ssh-ed25519 AA!A\n",
            b"production.example.test ssh-ed25519 AAAA\xff\n",
        )
        for output in invalid_outputs:
            with self.subTest(output=output):
                with self.assertRaises(installed_check.VerificationError):
                    installed_check._parse_scanned_host_key(output, "production.example.test")
        self.assertEqual(
            installed_check._parse_scanned_host_key(
                f"\n# SSH-2.0-OpenSSH_10.2\n{valid}\n".encode("ascii"),
                "production.example.test",
            ),
            valid,
        )
        def wrong_fingerprint(argv: list[str], **_kwargs: object) -> bytes:
            if argv[0] == "/usr/bin/timeout":
                return f"# SSH-2.0-OpenSSH_10.2\n{valid}\n".encode("ascii")
            if argv[:2] == ["/usr/bin/ssh-keygen", "-lf"]:
                return b"256 SHA256:wrong production.example.test (ED25519)\n"
            raise AssertionError(f"unexpected local SSH helper command: {argv!r}")

        with tempfile.TemporaryDirectory() as mismatch_root:
            mismatched_temp = Path(mismatch_root)
            mismatched_temp.chmod(0o700)
            with patch.object(installed_check, "_run", side_effect=wrong_fingerprint):
                with self.assertRaisesRegex(installed_check.VerificationError, "does not match the pinned fingerprint"):
                    installed_check._safe_host_ssh(
                        "production.example.test", "operator", private_key, mismatched_temp
                    )
            self.assertFalse((mismatched_temp / "platform-host-capability-ssh").exists())

    def test_host_generation_checker_retries_only_missing_generation_and_cleans_after_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner_temp = root / "runner-temp"
            runner_temp.mkdir()
            contract = self._installed_checker_contract(root)
            ssh_dir = runner_temp / "platform-host-capability-ssh"
            generation = f"{installed_check.GENERATION_ROOT}/{SOURCE_SHA}"
            generation_attempts = 0

            def create_ssh(*_args: object, **_kwargs: object) -> tuple[Path, list[str], bool]:
                ssh_dir.mkdir(mode=0o700)
                (ssh_dir / "id_ed25519").write_text("synthetic-private-key\n", encoding="ascii")
                (ssh_dir / "id_ed25519").chmod(0o600)
                return ssh_dir, ["ssh", "fixed"], True

            def remote(_argv: list[str], *args: str, **_kwargs: object) -> bytes:
                nonlocal generation_attempts
                if args == ("/usr/bin/id", "-u"):
                    return b"0\n"
                if args[:3] == ("/usr/bin/test", "-d", generation):
                    generation_attempts += 1
                    raise installed_check.RemoteCommandFailure(
                        1, stage="generation_presence"
                    )
                raise AssertionError("missing-generation retries must not run other remote checks")

            clock = iter((100.0, 100.5, 100.9, 101.1))
            with patch.dict(
                os.environ,
                {
                    "RUNNER_TEMP": str(runner_temp),
                    "PROD_SSH_HOST": "production.example.test",
                    "PROD_SSH_USER": "operator",
                    "PROD_SSH_KEY": "synthetic-private-key",
                },
            ), patch.object(installed_check, "_safe_host_ssh", side_effect=create_ssh), patch.object(
                installed_check, "_remote", side_effect=remote
            ), patch.object(installed_check.time, "monotonic", side_effect=lambda: next(clock)), patch.object(
                installed_check.time, "sleep"
            ):
                with self.assertRaises(installed_check.GenerationAbsent):
                    installed_check.verify_installed_generation(
                        SOURCE_SHA, "production-host-tools-v4", contract, wait_seconds=1
                    )

            self.assertEqual(generation_attempts, 2)
            self.assertFalse(ssh_dir.exists())

    def test_installed_generation_checker_closes_local_contract_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            contract = Path(temporary)
            payload = {
                "files.sha256": f"{'a' * 64} platform_example.py\n",
                "files.modes": "555 platform_example.py\n",
                "manifest.sha256": f"{'b' * 64}\n",
                "capabilities.sha256": f"{'c' * 64}\n",
            }
            for name, contents in payload.items():
                path = contract / name
                path.write_text(contents, encoding="ascii")
                path.chmod(0o600)
            digests, modes, manifest, capabilities = installed_check._contract_rows(contract)
            self.assertEqual(digests, [("a" * 64, "platform_example.py")])
            self.assertEqual(modes, [("555", "platform_example.py")])
            self.assertEqual(manifest, "b" * 64)
            self.assertEqual(capabilities, "c" * 64)

            v4_contract_rows = [
                (f"{index:064x}", f"platform_tool_{index}.py")
                for index in range(15)
            ] + [("d" * 64, "capabilities.txt")]
            locked_members = installed_check._locked_generation_members(
                v4_contract_rows, "production-host-tools-v4", "b" * 64, "c" * 64
            )
            self.assertEqual(len(locked_members), 17)
            self.assertEqual(len({row["name"] for row in locked_members}), 17)
            self.assertEqual(
                [row for row in locked_members if row["name"] == "capabilities.txt"],
                [{"name": "capabilities.txt", "sha256": "c" * 64, "mode": 0o444}],
            )
            self.assertEqual(
                [row for row in locked_members if row["name"] == "manifest.json"],
                [{"name": "manifest.json", "sha256": "b" * 64, "mode": 0o444}],
            )
            v3_contract_rows = [
                (f"{index:064x}", f"platform_tool_{index}.py")
                for index in range(14)
            ] + [("d" * 64, "capabilities.txt")]
            v3_members = installed_check._locked_generation_members(
                v3_contract_rows, "production-host-tools-v3", "b" * 64, "c" * 64
            )
            self.assertEqual(len(v3_members), 16)
            self.assertEqual(len({row["name"] for row in v3_members}), 16)
            with self.assertRaises(installed_check.VerificationError):
                installed_check._locked_generation_members(
                    v4_contract_rows, "production-host-tools-v3", "b" * 64, "c" * 64
                )
            (contract / "files.sha256").write_text(f"{'a' * 64} ../escape\n", encoding="ascii")
            (contract / "files.sha256").chmod(0o600)
            with self.assertRaises(installed_check.VerificationError):
                installed_check._contract_rows(contract)

    def test_host_generation_checker_reusable_wait_api_rejects_invalid_bounds(self) -> None:
        for wait_seconds in (-1, 1801, True, 1.5):
            with self.subTest(wait_seconds=wait_seconds):
                with self.assertRaises(installed_check.VerificationError):
                    installed_check.verify_installed_generation(
                        SOURCE_SHA,
                        "production-host-tools-v4",
                        Path("unused"),
                        wait_seconds=wait_seconds,  # type: ignore[arg-type]
                    )

    def test_existing_host_ssh_dir_never_writes_through_scan_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private_key = self._temporary_ed25519_private_key(root)

            fresh_root = root / "fresh-runner-temp"
            fresh_root.mkdir(mode=0o700)
            fresh_ssh_dir = fresh_root / "platform-host-capability-ssh"

            def fake_ssh_outputs(argv: list[str], **_kwargs: object) -> bytes:
                if argv[0] == "/usr/bin/timeout":
                    return b"production.example.test ssh-ed25519 AAAA\n"
                if argv[0] == "/usr/bin/ssh-keygen":
                    return f"256 {installed_check.HOST_KEY_FINGERPRINT} production.example.test (ED25519)\n".encode()
                raise AssertionError("unexpected SSH helper process")

            real_run = subprocess.run

            def record_private_key_parser(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
                if argv[:5] == ["/usr/bin/ssh-keygen", "-y", "-P", "", "-f"]:
                    parsed_path = Path(argv[5])
                    parsed_metadata = parsed_path.lstat()
                    self.assertEqual(parsed_metadata.st_mode & 0o777, 0o600)
                    self.assertEqual(parsed_metadata.st_nlink, 1)
                    self.assertIs(kwargs.get("stdin"), subprocess.DEVNULL)
                    self.assertIs(kwargs.get("stdout"), subprocess.DEVNULL)
                    self.assertIs(kwargs.get("stderr"), subprocess.DEVNULL)
                    self.assertEqual(kwargs.get("timeout"), installed_check.SSH_PRIVATE_KEY_PARSE_TIMEOUT_SECONDS)
                    self.assertFalse(kwargs.get("check"))
                return real_run(argv, **kwargs)

            with patch.object(installed_check, "_run", side_effect=fake_ssh_outputs), patch.object(
                installed_check.subprocess, "run", side_effect=record_private_key_parser
            ) as parser_run:
                returned_dir, remote, created = installed_check._safe_host_ssh(
                    "production.example.test", "operator", private_key, fresh_root
                )

            self.assertEqual(returned_dir, fresh_ssh_dir)
            self.assertTrue(created)
            self.assertIn("operator@production.example.test", remote)
            self.assertEqual((fresh_ssh_dir / "id_ed25519").stat().st_mode & 0o777, 0o600)
            self.assertEqual((fresh_ssh_dir / "id_ed25519").read_text(encoding="ascii"), private_key.rstrip("\n") + "\n")
            parser_calls = [call for call in parser_run.call_args_list if call.args[0][:2] == ["/usr/bin/ssh-keygen", "-y"]]
            self.assertEqual(len(parser_calls), 1)

            bad_inputs = (
                ("", "operator", private_key, "ssh_host_format", None, "production SSH configuration is malformed"),
                ("production.example.test:22", "operator", private_key, "ssh_host_format", None, "production SSH configuration is malformed"),
                ("production.example.test", "operator/user", private_key, "ssh_user_format", None, "production SSH configuration is malformed"),
                ("production.example.test", "operator", "", "ssh_key_shape", None, "production SSH configuration is malformed"),
                ("production.example.test", "operator", private_key.replace("\n", "\r\n"), "ssh_key_shape", None, "production SSH configuration is malformed"),
                ("production.example.test", "operator", private_key + "é", "ssh_key_shape", None, "production SSH configuration is malformed"),
                ("production.example.test", "operator", "x" * (installed_check.MAX_SSH_PRIVATE_KEY_BYTES + 1), "ssh_key_shape", None, "production SSH configuration is malformed"),
                ("production.example.test", "operator", "not an OpenSSH private key\n", "ssh_key_parser_invalid", None, "production SSH private key is invalid"),
                ("production.example.test", "operator", private_key, "ssh_key_parser_unavailable", FileNotFoundError(), "production SSH private key validation failed"),
                ("production.example.test", "operator", private_key, "ssh_key_parser_timeout", subprocess.TimeoutExpired("ssh-keygen", installed_check.SSH_PRIVATE_KEY_PARSE_TIMEOUT_SECONDS), "production SSH private key validation failed"),
            )
            for index, (bad_host, bad_user, bad_key, reason, parser_error, message) in enumerate(bad_inputs):
                with self.subTest(reason=reason, case=index):
                    case_root = root / f"rejected-{index}"
                    case_root.mkdir(mode=0o700)
                    runner_temp = case_root / "runner-temp"
                    runner_temp.mkdir(mode=0o700)
                    contract = self._installed_checker_contract(case_root)
                    stderr = StringIO()
                    with ExitStack() as stack:
                        stack.enter_context(patch.dict(os.environ, {
                            "RUNNER_TEMP": str(runner_temp),
                            "GITHUB_ENV": str(case_root / "github-env"),
                            "PROD_SSH_HOST": bad_host,
                            "PROD_SSH_USER": bad_user,
                            "PROD_SSH_KEY": bad_key,
                        }))
                        ssh_run = stack.enter_context(patch.object(installed_check, "_run"))
                        if parser_error is not None:
                            stack.enter_context(patch.object(installed_check.subprocess, "run", side_effect=parser_error))
                        with redirect_stderr(stderr):
                            result = installed_check.main([
                                "--source-sha", SOURCE_SHA,
                                "--toolset-version", "production-host-tools-v4",
                                "--contract-dir", str(contract),
                            ])
                    self.assertEqual(result, 1)
                    self.assertEqual(
                        stderr.getvalue(),
                        f"{message} [reason={reason}]\n",
                    )
                    if bad_key:
                        self.assertNotIn(bad_key, stderr.getvalue())
                    if bad_host:
                        self.assertNotIn(bad_host, stderr.getvalue())
                    if bad_user:
                        self.assertNotIn(bad_user, stderr.getvalue())
                    self.assertFalse((runner_temp / "platform-host-capability-ssh").exists())
                    ssh_run.assert_not_called()

            with self.subTest(reason="ssh_key_shape", case="embedded_nul"):
                nul_root = root / "rejected-embedded-nul"
                nul_root.mkdir(mode=0o700)
                with patch.object(installed_check, "_run") as ssh_run:
                    with self.assertRaises(installed_check.VerificationError) as failure:
                        installed_check._safe_host_ssh(
                            "production.example.test", "operator", private_key + "\x00", nul_root
                        )
                self.assertEqual(failure.exception.diagnostic_code, "ssh_key_shape")
                self.assertNotIn("\x00", str(failure.exception))
                self.assertFalse((nul_root / "platform-host-capability-ssh").exists())
                ssh_run.assert_not_called()

            ssh_dir = root / "platform-host-capability-ssh"
            ssh_dir.mkdir(mode=0o700)
            known_hosts = ssh_dir / "known_hosts"
            config = ssh_dir / "config"
            key = ssh_dir / "id_ed25519"
            for path, contents in (
                (known_hosts, "production.example.test ssh-ed25519 AAAA\n"),
                (config, f"Host *\n  StrictHostKeyChecking yes\n  UserKnownHostsFile {known_hosts}\n"),
                (key, private_key.rstrip("\n") + "\n"),
            ):
                path.write_text(contents, encoding="ascii")
                path.chmod(0o600)
            victim = root / "outside-target"
            victim.write_text("do not overwrite\n", encoding="ascii")
            scan_path = ssh_dir / "known_hosts.scan"
            scan_path.symlink_to(victim)

            with patch.object(
                installed_check,
                "_run",
                return_value=f"256 {installed_check.HOST_KEY_FINGERPRINT} production.example.test (ED25519)\n".encode(),
            ):
                returned_dir, remote, created = installed_check._safe_host_ssh(
                    "production.example.test", "operator", private_key, root
                )

            self.assertEqual(returned_dir, ssh_dir)
            self.assertFalse(created)
            self.assertIn("operator@production.example.test", remote)
            self.assertTrue(scan_path.is_symlink())
            self.assertEqual(victim.read_text(encoding="ascii"), "do not overwrite\n")
            known_hosts.write_text(
                "production.example.test ssh-ed25519 AAAA\nother.example.test ssh-ed25519 BBBB\n",
                encoding="ascii",
            )
            with patch.object(installed_check, "_run") as run:
                with self.assertRaises(installed_check.VerificationError):
                    installed_check._safe_host_ssh(
                        "production.example.test", "operator", private_key, root
                    )
            run.assert_not_called()
            self.assertEqual(victim.read_text(encoding="ascii"), "do not overwrite\n")

    def test_external_cleanup_dispatch_uses_validated_equal_run_ids(self) -> None:
        target_sha = "a" * 40
        load_run_id = "37556438370"
        cleanup_run_id = "37560000001"
        control_email = "synthetic-control@example.test"
        cleanup_helper = Path(
            "/fixed/app-release/tools/platform_production_retained_load_cleanup_qa.sh"
        )

        class FakeChild:
            def __init__(self, returncode: int, marker: bytes | None) -> None:
                self.returncode = returncode
                self.pid = 2_000_000_000
                stdin_read, stdin_write = os.pipe()
                stdout_read, stdout_write = os.pipe()
                self._stdin_read = stdin_read
                self.stdin = os.fdopen(stdin_write, "wb", buffering=0)
                self.stdout = os.fdopen(stdout_read, "rb", buffering=0)
                if marker is not None:
                    os.write(stdout_write, marker)
                os.close(stdout_write)
                self.input_bytes: bytes | None = None

            def poll(self) -> int:
                if self.input_bytes is None:
                    chunks: list[bytes] = []
                    while True:
                        chunk = os.read(self._stdin_read, 4096)
                        if not chunk:
                            break
                        chunks.append(chunk)
                    os.close(self._stdin_read)
                    self.input_bytes = b"".join(chunks)
                return self.returncode

            def wait(self, timeout: float | None = None) -> int:
                return self.returncode

        def invoke(
            command: list[str] | str,
            payload: dict[str, object],
            *,
            returncode: int = 0,
            marker: bytes | None = None,
        ) -> tuple[int, str, list[str], bytes | None]:
            raw = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
            child_args: list[str] = []
            child = FakeChild(
                returncode,
                marker
                if marker is not None
                else f"RETAINED_CLEANUP_STAGE schema=1 stage=complete exit_code={returncode}\n".encode()
                if returncode == 0
                else None,
            )

            def spawn(arguments: list[str], **_kwargs: object) -> FakeChild:
                child_args.extend(arguments)
                return child

            output = StringIO()
            with (
                patch.object(dispatcher.sys, "stdin", SimpleNamespace(buffer=BytesIO(raw))),
                patch.object(
                    dispatcher,
                    "_active_app_retained_cleanup_helper",
                    return_value=cleanup_helper,
                ),
                patch.object(dispatcher.subprocess, "Popen", side_effect=spawn),
                redirect_stdout(output),
            ):
                result = dispatcher.main(
                    [command] if isinstance(command, str) else command
                )
            return result, output.getvalue(), child_args, child.input_bytes

        common = {
            "schema": 1,
            "target_sha": target_sha,
            "control_email": control_email,
            "load_run_id": load_run_id,
        }
        external = {**common, "cleanup_run_id": load_run_id}
        retained = {**common, "cleanup_run_id": cleanup_run_id}
        expected_control_stdin = (
            json.dumps(
                {"schema": 1, "control_email": control_email},
                separators=(",", ":"),
            )
            + "\n"
        ).encode("ascii")

        for command, payload, expected_cleanup_id in (
            (
                [
                    "external-cleanup",
                    "--run-attempt",
                    "2",
                    "--profile-id",
                    "ready-vote-slo-v2",
                ],
                external,
                load_run_id,
            ),
            (["retained-cleanup"], retained, cleanup_run_id),
        ):
            with self.subTest(command=command):
                result, output, child_args, child_stdin = invoke(command, payload)
                self.assertEqual(result, 0)
                if command[0] == "external-cleanup":
                    self.assertEqual(
                        output,
                        "RETAINED_CLEANUP_DIAGNOSTIC schema=2 stage=complete "
                        "dispatcher_exit=0 child_state=exited child_exit=0 "
                        "stdout_eof=true timed_out=false "
                        f"source_sha={target_sha} app_sha={target_sha} "
                        f"run_id={load_run_id} run_attempt=2 load_run_id={load_run_id} "
                        "profile=ready-vote-slo-v2\n",
                    )
                else:
                    self.assertEqual(
                        output,
                        "RETAINED_CLEANUP_DIAGNOSTIC schema=1 "
                        "stage=complete child_exit=0\n",
                    )
                self.assertEqual(
                    child_args,
                    [
                        dispatcher.SUDO,
                        "-n",
                        "--",
                        str(cleanup_helper),
                        dispatcher.DELETE_CONFIRMATION,
                        target_sha,
                        load_run_id,
                        expected_cleanup_id,
                    ],
                )
                self.assertEqual(child_stdin, expected_control_stdin)
                self.assertNotIn(control_email, child_args)

        for command, payload, expected_cleanup_id in (
            ("external-cleanup-exports", external, load_run_id),
            ("retained-cleanup-exports", retained, cleanup_run_id),
        ):
            with self.subTest(command=command):
                raw = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
                executor_calls: list[tuple[str, dict[str, object]]] = []

                def run_executor(
                    operation: str, executor_payload: dict[str, object]
                ) -> int:
                    executor_calls.append((operation, executor_payload))
                    return 0

                with (
                    patch.object(
                        dispatcher.sys,
                        "stdin",
                        SimpleNamespace(buffer=BytesIO(raw)),
                    ),
                    patch.object(
                        dispatcher, "_current_pin_matches_host_generation", return_value=True
                    ),
                    patch.object(
                        dispatcher, "_run_retained_export_executor", side_effect=run_executor
                    ),
                    redirect_stdout(StringIO()),
                ):
                    result = dispatcher.main([command])

                self.assertEqual(result, 0)
                self.assertEqual(
                    executor_calls,
                    [
                        (
                            "remove",
                            {
                                "schema": 1,
                                "load_run_id": load_run_id,
                                "cleanup_run_id": expected_cleanup_id,
                            },
                        )
                    ],
                )

        failed_marker = (
            b"RETAINED_CLEANUP_STAGE schema=1 stage=matrix_cleanup exit_code=1\n"
        )
        result, output, _, _ = invoke(
            ["external-cleanup", "--run-attempt", "2", "--profile-id", "ready-vote-slo-v2"],
            external,
            returncode=1,
            marker=failed_marker,
        )
        self.assertEqual(result, 1)
        self.assertEqual(
            output,
                    "RETAINED_CLEANUP_DIAGNOSTIC schema=2 stage=matrix_cleanup "
                    "dispatcher_exit=1 child_state=exited child_exit=1 stdout_eof=true "
                    "timed_out=false source_sha="
            + target_sha
            + " app_sha="
            + target_sha
                    + f" run_id={load_run_id} run_attempt=2 load_run_id={load_run_id} profile=ready-vote-slo-v2\n",
        )
        result, output, _, _ = invoke(
            ["external-cleanup", "--run-attempt", "2", "--profile-id", "ready-vote-slo-v2"],
            external,
            returncode=1,
            marker=b"invalid marker\n",
        )
        self.assertEqual(result, 1)
        self.assertEqual(
            output,
                    "RETAINED_CLEANUP_DIAGNOSTIC schema=2 stage=unknown "
                    "dispatcher_exit=1 child_state=exited child_exit=1 stdout_eof=true "
                    "timed_out=false source_sha="
            + target_sha
            + " app_sha="
            + target_sha
                    + f" run_id={load_run_id} run_attempt=2 load_run_id={load_run_id} profile=ready-vote-slo-v2\n",
        )

    def test_active_release_baseline_reader_returns_stable_closed_tuple(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            release = runtime / "releases" / "gha-35511236041-1-87547df2abd4"
            active_tools = release / "tools"
            (runtime / "releases").mkdir(parents=True)
            (runtime / "shared").mkdir()
            release.mkdir()
            active_tools.mkdir()
            receipt = _release_receipt(
                release.name,
                "87547df2abd4aa06a07f4dd4b4f730e9912707e1",
                legacy_runtime_layout={
                    "app_dir": "/opt/oldsparky/platform",
                    "current_symlink": "/opt/oldsparky/platform/current",
                    "previous_symlink": "/opt/oldsparky/platform/previous",
                    "shared_dir": "/opt/oldsparky/platform/shared",
                    "shared_env_file": "/opt/oldsparky/platform/shared/.env.platform",
                    "shared_venv_dir": "/opt/oldsparky/platform/shared/venv",
                },
            )
            (release / "RELEASE.json").write_bytes(receipt)
            (release / "RELEASE.json").chmod(0o444)
            (runtime / "current").symlink_to("releases/gha-35511236041-1-87547df2abd4")

            with (
                patch.object(dispatcher, "RUNTIME_ROOT", runtime),
                patch.object(dispatcher, "ACTIVE_TOOLS_DIR", active_tools),
                patch.object(
                    dispatcher,
                    "_stable_host_file",
                    return_value=(TOOLS_ROOT / "platform_validate_release_artifact.py").read_bytes(),
                ) as stable_host_file,
            ):
                baseline = dispatcher._release_baseline()

            stable_host_file.assert_called_once_with(
                active_tools / "platform_validate_release_artifact.py",
                mode=0o755,
            )

            self.assertEqual(
                set(baseline),
                {
                    "schema", "source_sha", "release_slug", "release_json_sha256",
                    "current_link_dev", "current_link_ino", "release_dev", "release_ino",
                    "pending_operation",
                },
            )
            self.assertEqual(baseline["schema"], 1)
            self.assertEqual(baseline["source_sha"], "87547df2abd4aa06a07f4dd4b4f730e9912707e1")
            self.assertEqual(baseline["release_slug"], release.name)
            self.assertEqual(baseline["release_json_sha256"], hashlib.sha256(receipt).hexdigest())
            self.assertIs(baseline["pending_operation"], False)
            for key in ("current_link_dev", "current_link_ino", "release_dev", "release_ino"):
                self.assertIs(type(baseline[key]), int)
                self.assertGreaterEqual(baseline[key], 0)

    def test_release_validator_accepts_only_the_exact_legacy_runtime_layout(self) -> None:
        validator_path = TOOLS_ROOT / "platform_validate_release_artifact.py"
        namespace: dict[str, object] = {"__name__": "_release_validator_fixture"}
        exec(compile(validator_path.read_bytes(), str(validator_path), "exec"), namespace)
        parser = namespace["_parse_release_json"]
        artifact_error = namespace["ArtifactError"]
        source_sha = "87547df2abd4aa06a07f4dd4b4f730e9912707e1"
        slug = "gha-35511236041-1-87547df2abd4"
        valid_layout = {
            "app_dir": "/opt/oldsparky/platform",
            "current_symlink": "/opt/oldsparky/platform/current",
            "previous_symlink": "/opt/oldsparky/platform/previous",
            "shared_dir": "/opt/oldsparky/platform/shared",
            "shared_env_file": "/opt/oldsparky/platform/shared/.env.platform",
            "shared_venv_dir": "/opt/oldsparky/platform/shared/venv",
        }
        legacy_receipt = _release_receipt(
            slug, source_sha, legacy_runtime_layout=valid_layout
        )
        with self.assertRaises(artifact_error):
            parser(legacy_receipt, release_slug=slug)
        self.assertEqual(
            parser(
                legacy_receipt,
                release_slug=slug,
                allow_legacy_runtime_layout=True,
            )["runtime_layout"],
            valid_layout,
        )
        wrong_layout = {**valid_layout, "app_dir": "/tmp/platform"}
        for receipt in (
            _release_receipt(slug, source_sha, legacy_runtime_layout=wrong_layout),
            _release_receipt(
                slug,
                source_sha,
                legacy_runtime_layout=valid_layout,
                extra_field=True,
            ),
        ):
            with self.subTest(receipt=receipt), self.assertRaises(artifact_error):
                parser(
                    receipt,
                    release_slug=slug,
                    allow_legacy_runtime_layout=True,
                )

    def test_active_release_baseline_reader_rejects_duplicate_receipt_keys_and_pending_state(self) -> None:
        for receipt, pending_name in (
            (b'{"source_git_commit":"87547df2abd4aa06a07f4dd4b4f730e9912707e1",'
             b'"source_git_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}\n', False),
            (_release_receipt("gha-35511236041-1-87547df2abd4", "87547df2abd4aa06a07f4dd4b4f730e9912707e1"), ".release-operation.json"),
            (_release_receipt("gha-35511236041-1-87547df2abd4", "87547df2abd4aa06a07f4dd4b4f730e9912707e1"), ".release-systemd-state.json"),
        ):
            with self.subTest(pending=pending_name):
                with tempfile.TemporaryDirectory() as temporary:
                    runtime = Path(temporary) / "runtime"
                    release = runtime / "releases" / "gha-35511236041-1-87547df2abd4"
                    (runtime / "releases").mkdir(parents=True)
                    shared = runtime / "shared"
                    shared.mkdir()
                    release.mkdir()
                    (release / "RELEASE.json").write_bytes(receipt)
                    (release / "RELEASE.json").chmod(0o444)
                    (runtime / "current").symlink_to("releases/gha-35511236041-1-87547df2abd4")
                    if pending_name:
                        (shared / pending_name).write_text("{}", encoding="ascii")
                    with (
                        patch.object(dispatcher, "RUNTIME_ROOT", runtime),
                        patch.object(
                            dispatcher,
                            "_stable_host_file",
                            return_value=(TOOLS_ROOT / "platform_validate_release_artifact.py").read_bytes(),
                        ),
                    ):
                        with self.assertRaises(OSError):
                            dispatcher._release_baseline()

    def test_active_release_baseline_reader_rejects_mutable_or_misdirected_receipts(self) -> None:
        mutations = ("writable", "hardlink", "symlink", "wrong_slug", "outside_pointer")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                runtime = Path(temporary) / "runtime"
                releases = runtime / "releases"
                release = releases / "gha-35511236041-1-87547df2abd4"
                releases.mkdir(parents=True)
                (runtime / "shared").mkdir()
                release.mkdir()
                receipt_path = release / "RELEASE.json"
                receipt = _release_receipt(
                    release.name, "87547df2abd4aa06a07f4dd4b4f730e9912707e1"
                )
                receipt_path.write_bytes(receipt)
                receipt_path.chmod(0o444)
                current_target = "releases/gha-35511236041-1-87547df2abd4"
                if mutation == "writable":
                    receipt_path.chmod(0o644)
                elif mutation == "hardlink":
                    os.link(receipt_path, runtime / "shared" / "receipt-copy")
                elif mutation == "symlink":
                    receipt_path.unlink()
                    receipt_path.symlink_to("../shared/receipt-copy")
                    (runtime / "shared" / "receipt-copy").write_bytes(receipt)
                elif mutation == "wrong_slug":
                    receipt_path.chmod(0o644)
                    receipt_path.write_bytes(_release_receipt(
                        "gha-35511236041-1-aaaaaaaaaaaa",
                        "87547df2abd4aa06a07f4dd4b4f730e9912707e1",
                    ))
                    receipt_path.chmod(0o444)
                elif mutation == "outside_pointer":
                    current_target = "../outside-release"
                    (runtime / "outside-release").mkdir()
                (runtime / "current").symlink_to(current_target)

                with (
                    patch.object(dispatcher, "RUNTIME_ROOT", runtime),
                    patch.object(
                        dispatcher,
                        "_stable_host_file",
                        return_value=(TOOLS_ROOT / "platform_validate_release_artifact.py").read_bytes(),
                    ),
                ):
                    with self.assertRaises(OSError):
                        dispatcher._release_baseline()

    def test_active_release_baseline_identity_comparison_is_closed_and_typed(self) -> None:
        baseline: dict[str, object] = {
            "schema": 1,
            "source_sha": "87547df2abd4aa06a07f4dd4b4f730e9912707e1",
            "release_slug": "gha-35511236041-1-87547df2abd4",
            "release_json_sha256": "a" * 64,
            "current_link_dev": 8,
            "current_link_ino": 9,
            "release_dev": 8,
            "release_ino": 10,
            "pending_operation": False,
        }
        self.assertTrue(dispatcher._baseline_identity_matches(baseline, dict(baseline)))
        for field, changed in (
            ("source_sha", "a" * 40),
            ("release_slug", "gha-35511236041-1-aaaaaaaaaaaa"),
            ("release_json_sha256", "b" * 64),
            ("current_link_ino", 99),
            ("release_ino", 99),
            ("pending_operation", True),
            ("current_link_dev", True),
            ("schema", True),
        ):
            with self.subTest(field=field):
                self.assertFalse(
                    dispatcher._baseline_identity_matches(
                        baseline, {**baseline, field: changed}
                    )
                )
        self.assertFalse(
            dispatcher._baseline_identity_matches(
                baseline, {**baseline, "extra": "unexpected"}
            )
        )
        with self.assertRaises(OSError):
            dispatcher._strict_baseline_json(
                b'{"schema":1,"schema":1}'
            )

    def _assert_artifact_zip_transport_contract(self) -> None:
        with BytesIO() as buffer:
            with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("fixture.txt", b"artifact ZIP fixture")
            zip_payload = buffer.getvalue()

        endpoint = "/repos/StrayForest/old_sparky/actions/artifacts/123/zip"
        requests: list[dict[str, str | None]] = []

        class ArtifactHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                requests.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "accept": self.headers.get("Accept"),
                        "api_version": self.headers.get("X-GitHub-Api-Version"),
                    }
                )
                if self.path == endpoint:
                    if self.headers.get("Accept") != "application/vnd.github+json":
                        body = b"unsupported media type\n"
                        self.send_response(415)
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    self.send_response(302)
                    self.send_header("Location", "/fixture.zip")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path == "/fixture.zip":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/zip")
                    self.send_header("Content-Length", str(len(zip_payload)))
                    self.end_headers()
                    self.wfile.write(zip_payload)
                    return
                body = b"not found\n"
                self.send_response(404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), ArtifactHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)

                def download(accept: str, output: Path) -> subprocess.CompletedProcess[str]:
                    return subprocess.run(
                        [
                            "curl",
                            "--fail-with-body",
                            "--silent",
                            "--show-error",
                            "--location",
                            "--max-time",
                            "10",
                            "--max-filesize",
                            "8388608",
                            "--header",
                            "Authorization: Bearer fixture-token",
                            "--header",
                            f"Accept: {accept}",
                            "--header",
                            "X-GitHub-Api-Version: 2022-11-28",
                            f"http://127.0.0.1:{server.server_port}{endpoint}",
                            "--output",
                            str(output),
                        ],
                        capture_output=True,
                        text=True,
                        env={"PATH": os.environ.get("PATH", ""), "LC_ALL": "C"},
                        check=False,
                    )

                successful = download("application/vnd.github+json", root / "artifact.zip")
                self.assertEqual(successful.returncode, 0, successful.stderr)
                self.assertEqual((root / "artifact.zip").read_bytes(), zip_payload)
                self.assertEqual(
                    [request["path"] for request in requests],
                    [endpoint, "/fixture.zip"],
                )
                for request in requests:
                    self.assertEqual(request["authorization"], "Bearer fixture-token")
                    self.assertEqual(request["accept"], "application/vnd.github+json")
                    self.assertEqual(request["api_version"], "2022-11-28")

                requests.clear()
                rejected = download("application/zip", root / "rejected.zip")
                self.assertNotEqual(rejected.returncode, 0)
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0]["path"], endpoint)
                self.assertEqual(requests[0]["accept"], "application/zip")
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    def _current_target_sha(self) -> str:
        completed = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout.strip()

    def test_repository_pin_declares_installed_generation_and_closure_baseline(self) -> None:
        contract = json.loads(
            (REPO_ROOT / pin.PIN_RELATIVE_PATH).read_text(encoding="utf-8")
        )
        self.assertEqual(contract["schema"], 1)
        self.assertEqual(contract["repository"], pin.EXPECTED_REPOSITORY)
        self.assertEqual(contract["host_tools_sha"], PIN_SHA)
        self.assertEqual(
            tuple(record["path"] for record in contract["closure"]),
            tuple(f"platform/tools/{name}" for name in bundle.HOST_TOOL_FILES),
        )
        with tempfile.TemporaryDirectory() as temporary:
            source_root = Path(temporary)
            tools_dir = source_root / "platform" / "tools"
            tools_dir.mkdir(parents=True)
            bundle_source = tools_dir / "platform_host_tools_bundle.py"
            legacy_groups = (
                ("PREPARE_ARTIFACT_FILES", bundle.PREPARE_ARTIFACT_FILES),
                ("PRODUCTION_DEPLOY_CONTROL_FILES", bundle.PRODUCTION_DEPLOY_CONTROL_FILES),
                ("RETAINED_LOAD_ARTIFACT_FILES", bundle.RETAINED_LOAD_ARTIFACT_FILES),
            )
            extended_groups = (*legacy_groups, (
                "CPU_DIAGNOSTIC_CONTROL_FILES", ("platform_cpu_diagnostic_plan.py",),
            ))
            def group_declarations(groups):
                return "".join(
                    f"{group} = ({', '.join(json.dumps(name) for name in values)},)\n"
                    for group, values in groups
                )

            for groups in (legacy_groups, extended_groups):
                names = tuple(name for _group, values in groups for name in values)
                bundle_source.write_text(
                    group_declarations(groups)
                    + f"HOST_TOOL_FILES = ({' + '.join(group for group, _ in groups)})\n",
                    encoding="ascii",
                )
                self.assertEqual(pin._bundle_file_names(source_root), names)
            bundle_source.write_text(
                group_declarations(legacy_groups)
                + 'CPU_DIAGNOSTIC_CONTROL_FILES = ("platform_cpu_diagnostic_plan.py",)\n'
                + "HOST_TOOL_FILES = (PREPARE_ARTIFACT_FILES + "
                "PRODUCTION_DEPLOY_CONTROL_FILES + RETAINED_LOAD_ARTIFACT_FILES)\n",
                encoding="ascii",
            )
            self.assertEqual(
                pin._bundle_file_names(source_root),
                tuple(name for _group, values in legacy_groups for name in values),
            )
            bundle_source.write_text(
                group_declarations(legacy_groups)
                + 'CPU_DIAGNOSTIC_CONTROL_FILES = ("unapproved_helper.py",)\n'
                + "HOST_TOOL_FILES = (PREPARE_ARTIFACT_FILES + "
                "PRODUCTION_DEPLOY_CONTROL_FILES + RETAINED_LOAD_ARTIFACT_FILES)\n",
                encoding="ascii",
            )
            with self.assertRaises(pin.HostToolsPinError):
                pin._bundle_file_names(source_root)
            bundle_source.write_text(
                group_declarations(legacy_groups)
                + 'CPU_DIAGNOSTIC_CONTROL_FILES = ("platform_cpu_diagnostic_plan.py",)\n'
                + "HOST_TOOL_FILES = (PREPARE_ARTIFACT_FILES + "
                "PRODUCTION_DEPLOY_CONTROL_FILES + RETAINED_LOAD_ARTIFACT_FILES)\n",
                encoding="ascii",
            )
            valid_declaration = bundle_source.read_bytes()
            bundle_source.write_bytes(
                valid_declaration + b"#" + b"x" * pin.MAX_SOURCE_CONTRACT_BYTES
            )
            with self.assertRaises(pin.HostToolsPinError):
                pin._bundle_file_names(source_root)
            bundle_source.write_bytes(valid_declaration)
            linked_declaration = tools_dir / "linked_bundle.py"
            os.link(bundle_source, linked_declaration)
            try:
                with self.assertRaises(pin.HostToolsPinError):
                    pin._bundle_file_names(source_root)
            finally:
                linked_declaration.unlink()
            saved_declaration = tools_dir / "platform_host_tools_bundle.saved"
            bundle_source.replace(saved_declaration)
            try:
                bundle_source.symlink_to(saved_declaration.name)
                with self.assertRaises(pin.HostToolsPinError):
                    pin._bundle_file_names(source_root)
            finally:
                bundle_source.unlink(missing_ok=True)
                saved_declaration.replace(bundle_source)
            bundle_source.write_text(
                group_declarations(legacy_groups)
                + 'UNKNOWN_FILES = ("platform_cpu_diagnostic_plan.py",)\n'
                + "HOST_TOOL_FILES = PREPARE_ARTIFACT_FILES + UNKNOWN_FILES\n",
                encoding="ascii",
            )
            with self.assertRaises(pin.HostToolsPinError):
                pin._bundle_file_names(source_root)
            bundle_source.write_text(
                group_declarations(extended_groups)
                + 'HOST_TOOL_FILES = PREPARE_ARTIFACT_FILES + UNKNOWN_FILES\n',
                encoding="ascii",
            )
            with self.assertRaises(pin.HostToolsPinError):
                pin._bundle_file_names(source_root)
            bundle_source.write_text(
                group_declarations(extended_groups)
                + 'HOST_TOOL_FILES = PREPARE_ARTIFACT_FILES + CPU_DIAGNOSTIC_CONTROL_FILES\n',
                encoding="ascii",
            )
            with self.assertRaises(pin.HostToolsPinError):
                pin._bundle_file_names(source_root)
            cpu_layout = bundle.CPU_DIAGNOSTIC_HOST_TOOL_FILES
            cpu_pin = {
                "closure": [
                    {
                        "mode": 0o755,
                        "path": f"platform/tools/{name}",
                        "sha256": "a" * 64,
                    }
                    for name in cpu_layout
                ]
            }
            self.assertEqual(
                tuple(row["path"] for row in candidate._pin_closure(source_root, cpu_pin)),
                tuple(f"platform/tools/{name}" for name in cpu_layout),
            )
            bundle_source.write_text(
                'PREPARE_ARTIFACT_FILES = ("a.py",)\n'
                'PRODUCTION_DEPLOY_CONTROL_FILES = ("b.py",)\n'
                'CPU_DIAGNOSTIC_CONTROL_FILES = ("platform_cpu_diagnostic_plan.py",)\n'
                'HOST_TOOL_FILES = PREPARE_ARTIFACT_FILES + UNKNOWN_FILES\n',
                encoding="ascii",
            )
            with self.assertRaises(pin.HostToolsPinError):
                pin._bundle_file_names(source_root)

    def test_host_tools_provisioning_adr_matches_enforced_generation_pin(self) -> None:
        adr = (
            REPO_ROOT / "platform/docs/adr/production-host-tools-provisioning.md"
        ).read_text(encoding="utf-8")
        self.assertNotIn(PIN_SHA, adr)
        self.assertIn("platform/contracts/host_tools_pin.json", adr)
        self.assertIn("platform/tools/platform_host_tools_pin.py resolve", adr)
        self.assertIn("Record the resolver-selected source in the operation receipts.", adr)
        self.assertIn("`CURRENT.md` owns the installed active generation.", adr)
        self.assertNotIn("25c67089fdfca99f58d801cc529bb0e987f5ecf8", adr)

    def _installed_generation_fixture(self, root: Path) -> Path:
        archive = root / "host-tools.zip"
        bundle.build_bundle(REPO_ROOT, PIN_SHA, archive)
        verified = bundle.verify_bundle(archive, expected_source_sha=PIN_SHA)
        generation = root / "installed-generation"
        generation.mkdir()
        members = verified["members"]
        self.assertIsInstance(members, dict)
        for name, contents in members.items():
            path = generation / name
            path.write_bytes(contents)
            path.chmod(
                bundle.DATA_MODE
                if name in {"manifest.json", "capabilities.txt"}
                else bundle.EXECUTABLE_MODE
            )
        generation.chmod(0o555)
        return generation

    def _operator_lock_bootstrap_fixture(self, root: Path) -> tuple[Path, Path, Path, str, str]:
        source_root = root / "source-checkout"
        for relative in operator_provisioner.TRUSTED_SOURCE_PATHS:
            source = REPO_ROOT / relative
            target = source_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        subprocess.run(["/usr/bin/git", "-C", str(source_root), "init", "--quiet"], check=True)
        subprocess.run(
            ["/usr/bin/git", "-C", str(source_root), "config", "user.name", "Host-tools fixture"], check=True,
        )
        subprocess.run(
            ["/usr/bin/git", "-C", str(source_root), "config", "user.email", "fixture@example.invalid"], check=True,
        )
        subprocess.run(["/usr/bin/git", "-C", str(source_root), "add", "platform/tools", "platform/contracts"], check=True)
        subprocess.run(
            ["/usr/bin/git", "-C", str(source_root), "commit", "--quiet", "-m", "fixture source"], check=True,
        )
        source_sha = subprocess.run(
            ["/usr/bin/git", "-C", str(source_root), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        (source_root / "fixture-target.txt").write_text("target commit\n", encoding="ascii")
        subprocess.run(["/usr/bin/git", "-C", str(source_root), "add", "fixture-target.txt"], check=True)
        subprocess.run(
            ["/usr/bin/git", "-C", str(source_root), "commit", "--quiet", "-m", "fixture target"], check=True,
        )
        target_sha = subprocess.run(
            ["/usr/bin/git", "-C", str(source_root), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

        app = root / "app"
        releases = app / "releases"
        release = releases / "active-release"
        release.mkdir(parents=True)
        flattened = self._flattened_release_pin_fixture(root / "flattened")
        shutil.copytree(flattened / "tools", release / "tools")
        shutil.copytree(flattened / "contracts", release / "contracts")
        (release / "contracts/host_tools_pin.json").chmod(0o644)
        receipt = {"artifact_format_version": 1, "release_slug": release.name, "source_git_commit": source_sha}
        (release / "RELEASE.json").write_text(
            json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n", encoding="ascii",
        )
        (release / "RELEASE.json").chmod(0o444)
        (app / "current").symlink_to(release)
        generation_root = app / "shared/host-tools"
        generation_root.mkdir(parents=True)
        staged_root = root / "staged"
        staged_root.mkdir()
        staged = self._installed_generation_fixture(staged_root)
        shutil.copytree(staged, generation_root / PIN_SHA, copy_function=shutil.copy2)
        (generation_root / PIN_SHA).chmod(0o555)
        return source_root, app, generation_root, target_sha, "a" * 40

    @contextmanager
    def _operator_root_owner_fixture(
        self, source_root: Path, app: Path, *, owner_drift: Path | None = None,
    ):
        """Model root-owned release metadata while keeping bundle checks real."""
        real_lstat = Path.lstat
        trusted_sources = {
            source_root / relative for relative in operator_provisioner.TRUSTED_SOURCE_PATHS
        }
        trusted_parents = {
            source_root / Path(*Path(relative).parts[:index])
            for relative in operator_provisioner.TRUSTED_SOURCE_PATHS
            for index in range(1, len(Path(relative).parts))
        }
        real_loader = operator_provisioner._load_trusted_source_module

        def fixture_lstat(path: Path) -> SimpleNamespace | os.stat_result:
            metadata = real_lstat(path)
            if path != app and app not in path.parents and path not in trusted_sources | trusted_parents:
                return metadata
            wrong_owner = path == owner_drift
            uid = (os.geteuid() or 65534) if wrong_owner else 0
            gid = (os.getegid() or 65534) if wrong_owner else 0
            return SimpleNamespace(
                st_mode=metadata.st_mode, st_uid=uid, st_gid=gid, st_nlink=metadata.st_nlink,
                st_size=metadata.st_size, st_dev=metadata.st_dev, st_ino=metadata.st_ino,
                st_mtime_ns=metadata.st_mtime_ns, st_ctime_ns=metadata.st_ctime_ns,
            )

        def load_trusted(name: str, path: Path, *, source_root: Path, target_sha: str):
            module = real_loader(name, path, source_root=source_root, target_sha=target_sha)
            if name == "trusted_operator_host_tools_bundle":
                verify_generation = module.verify_installed_generation

                def verify_fixture_generation(
                    generation: Path, *, expected_source_sha: str, expected_toolset_version: str,
                ):
                    # Keep the verifier's real lstat/fstat identity and digest checks,
                    # using the actual owner of this temporary fixture.
                    with patch.object(Path, "lstat", new=real_lstat):
                        return verify_generation(
                            generation, expected_source_sha=expected_source_sha,
                            expected_toolset_version=expected_toolset_version,
                            expected_uid=os.geteuid(), expected_gid=os.getegid(),
                        )

                module.verify_installed_generation = verify_fixture_generation
            return module

        with ExitStack() as stack:
            stack.enter_context(patch.object(Path, "lstat", autospec=True, side_effect=fixture_lstat))
            stack.enter_context(
                patch.object(operator_provisioner, "_load_trusted_source_module", side_effect=load_trusted)
            )
            yield

    def _verify_installed_fixture(self, generation: Path) -> dict[str, object]:
        return bundle.verify_installed_generation(
            generation,
            expected_source_sha=PIN_SHA,
            expected_toolset_version=bundle.TOOLSET_VERSION,
            expected_uid=os.geteuid(),
            expected_gid=os.getegid(),
        )

    def _flattened_release_pin_fixture(self, root: Path) -> Path:
        release = root / "release"
        tools_dir = release / "tools"
        contracts_dir = release / "contracts"
        tools_dir.mkdir(parents=True)
        contracts_dir.mkdir()
        contract = pin._read_pin(REPO_ROOT)
        (contracts_dir / "host_tools_pin.json").write_text(
            json.dumps(contract, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="ascii",
        )
        for name in (*bundle.HOST_TOOL_FILES, "platform_host_tools_bundle.py"):
            source = TOOLS_ROOT / name
            target = tools_dir / name
            target.write_bytes(source.read_bytes())
            target.chmod(stat.S_IMODE(source.stat().st_mode))
        return release

    def test_operator_lock_bootstrap_binds_active_pin_and_immutable_generation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="host-tools-lock-bootstrap-") as temporary:
            source_root, app, generation_root, target_sha, target_host_sha = self._operator_lock_bootstrap_fixture(Path(temporary))
            if (os.geteuid(), os.getegid()) != (0, 0):
                with self.assertRaisesRegex(
                    operator_provisioner.ProvisionError, "active_lock_bootstrap_directory_metadata",
                ):
                    operator_provisioner.active_lock_bootstrap(
                        source_root, target_sha=target_sha, target_host_tools_sha=target_host_sha, app_root=app,
                        generation_root=generation_root,
                    )
            with self._operator_root_owner_fixture(source_root, app, owner_drift=app):
                with self.assertRaisesRegex(
                    operator_provisioner.ProvisionError, "active_lock_bootstrap_directory_metadata",
                ):
                    operator_provisioner.active_lock_bootstrap(
                        source_root, target_sha=target_sha, target_host_tools_sha=target_host_sha, app_root=app,
                        generation_root=generation_root,
                    )
            pin_path = app / "releases/active-release/contracts/host_tools_pin.json"
            for mode in (0o444, 0o600, 0o664):
                with self.subTest(pin_mode=oct(mode)):
                    pin_path.chmod(mode)
                    try:
                        with self._operator_root_owner_fixture(source_root, app):
                            with self.assertRaisesRegex(
                                operator_provisioner.ProvisionError, "input_file_metadata",
                            ):
                                operator_provisioner.active_lock_bootstrap(
                                    source_root, target_sha=target_sha, target_host_tools_sha=target_host_sha,
                                    app_root=app, generation_root=generation_root,
                                )
                    finally:
                        pin_path.chmod(0o644)
            with self._operator_root_owner_fixture(source_root, app):
                snapshot = operator_provisioner.active_lock_bootstrap(
                    source_root, target_sha=target_sha, target_host_tools_sha=target_host_sha, app_root=app,
                    generation_root=generation_root,
                )
            self.assertEqual(snapshot["active_pin_sha"], PIN_SHA)
            self.assertEqual(
                Path(snapshot["lock_helper_path"]), generation_root / PIN_SHA / "platform_release_lock.sh",
            )
            self.assertEqual(
                snapshot["lock_helper_sha256"],
                next(
                    record["sha256"] for record in json.loads(
                        (app / "current/contracts/host_tools_pin.json").read_text(encoding="ascii")
                    )["closure"] if record["path"].endswith("/platform_release_lock.sh")
                ),
            )

    def test_operator_lock_bootstrap_rejects_active_source_and_helper_drift(self) -> None:
        with tempfile.TemporaryDirectory(prefix="host-tools-lock-bootstrap-drift-") as temporary:
            source_root, app, generation_root, target_sha, target_host_sha = self._operator_lock_bootstrap_fixture(Path(temporary))
            with self._operator_root_owner_fixture(source_root, app):
                snapshot = operator_provisioner.active_lock_bootstrap(
                    source_root, target_sha=target_sha, target_host_tools_sha=target_host_sha, app_root=app,
                    generation_root=generation_root,
                )

                tree_sha = subprocess.run(
                    ["/usr/bin/git", "-C", str(source_root), "rev-parse", f"{target_sha}^{{tree}}"],
                    check=True, capture_output=True, text=True,
                ).stdout.strip()
                unrelated_sha = subprocess.run(
                    [
                        "/usr/bin/git", "-C", str(source_root), "-c", "user.name=Host-tools fixture",
                        "-c", "user.email=fixture@example.invalid", "commit-tree", tree_sha,
                        "-m", "unrelated source commit",
                    ],
                    check=True, capture_output=True, text=True,
                ).stdout.strip()
                source_drift = app / "releases/source-drift"
                shutil.copytree(app / "releases/active-release", source_drift, copy_function=shutil.copy2)
                source_receipt = source_drift / "RELEASE.json"
                source_receipt.chmod(0o644)
                source_receipt.write_text(
                    json.dumps({
                        "artifact_format_version": 1, "release_slug": "source-drift",
                        "source_git_commit": unrelated_sha,
                    }, sort_keys=True, separators=(",", ":")) + "\n", encoding="ascii",
                )
                source_receipt.chmod(0o444)
                source_replacement = app / "source-drift-current"
                source_replacement.symlink_to(source_drift)
                source_replacement.replace(app / "current")
                with self.assertRaisesRegex(
                    operator_provisioner.ProvisionError, "active_release_source_not_in_target_history",
                ):
                    operator_provisioner.active_lock_bootstrap(
                        source_root, target_sha=target_sha, target_host_tools_sha=target_host_sha, app_root=app,
                        generation_root=generation_root,
                    )
                (app / "current").unlink()
                (app / "current").symlink_to(app / "releases/active-release")

                alternate = app / "releases/alternate"
                shutil.copytree(app / "releases/active-release", alternate, copy_function=shutil.copy2)
                (alternate / "RELEASE.json").chmod(0o644)
                alternate_receipt = {
                    "artifact_format_version": 1, "release_slug": "alternate",
                    "source_git_commit": snapshot["active_release_source_sha"],
                }
                (alternate / "RELEASE.json").write_text(
                    json.dumps(alternate_receipt, sort_keys=True, separators=(",", ":")) + "\n", encoding="ascii",
                )
                (alternate / "RELEASE.json").chmod(0o444)
                replacement = app / "replacement-current"
                replacement.symlink_to(alternate)
                replacement.replace(app / "current")
                with self.assertRaisesRegex(operator_provisioner.ProvisionError, "active_lock_bootstrap_changed"):
                    operator_provisioner.revalidate_active_lock_bootstrap(
                        source_root, target_sha=target_sha, target_host_tools_sha=target_host_sha, expected=snapshot,
                        app_root=app, generation_root=generation_root,
                    )

                (app / "current").unlink()
                (app / "current").symlink_to(app / "releases/active-release")
                pin_path = app / "releases/active-release/contracts/host_tools_pin.json"
                active_pin = json.loads(pin_path.read_text(encoding="ascii"))
                active_pin["host_tools_sha"] = target_host_sha
                pin_path.write_text(json.dumps(active_pin, sort_keys=True, separators=(",", ":")) + "\n", encoding="ascii")
                with self.assertRaisesRegex(operator_provisioner.ProvisionError, "active_pin_target_mismatch"):
                    operator_provisioner.revalidate_active_lock_bootstrap(
                        source_root, target_sha=target_sha, target_host_tools_sha=target_host_sha, expected=snapshot,
                        app_root=app, generation_root=generation_root,
                    )
                pin_path.write_text(json.dumps(pin._read_pin(REPO_ROOT), sort_keys=True, separators=(",", ":")) + "\n", encoding="ascii")
                lock_helper = generation_root / PIN_SHA / "platform_release_lock.sh"
                os.chmod(lock_helper, 0o644)
                lock_helper.write_bytes(lock_helper.read_bytes() + b"\n# drift\n")
                os.chmod(lock_helper, 0o555)
                with self.assertRaisesRegex(operator_provisioner.ProvisionError, "active_host_generation_validation"):
                    operator_provisioner.revalidate_active_lock_bootstrap(
                        source_root, target_sha=target_sha, target_host_tools_sha=target_host_sha, expected=snapshot,
                        app_root=app, generation_root=generation_root,
                    )

    def test_installed_generation_verifies_manifest_normalized_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            generation = self._installed_generation_fixture(Path(temporary))
            try:
                result = self._verify_installed_fixture(generation)
                self.assertEqual(result["source_sha"], PIN_SHA)
                self.assertEqual(result["files_verified"], len(bundle.HOST_TOOL_FILES))
                self.assertIs(result["normalized_modes_verified"], True)
                source_member = REPO_ROOT / "platform/tools/platform_validate_edge_policy.py"
                installed_member = generation / source_member.name
                self.assertEqual(stat.S_IMODE(source_member.stat().st_mode), 0o644)
                self.assertEqual(
                    stat.S_IMODE(installed_member.stat().st_mode), bundle.EXECUTABLE_MODE
                )
            finally:
                generation.chmod(0o700)

    def test_readiness_source_binding_uses_the_active_release_pin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            release = self._flattened_release_pin_fixture(Path(temporary))
            payload = live_readiness._check_source_pin(release, PIN_SHA)
            self.assertEqual(payload["host_tools_sha"], PIN_SHA)
            with self.assertRaises(live_readiness.ReadinessError):
                live_readiness._check_source_pin(release, "0" * 40)

    def test_readiness_rejects_pending_marker_in_canonical_shared_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "current").mkdir()
            shared = root / "shared"
            shared.mkdir()
            with patch.object(live_readiness, "APP_ROOT", root), patch.object(
                live_readiness, "PLANS", ()
            ):
                live_readiness._check_operation_marker_and_plans()
                marker = shared / ".release-operation.json"
                marker.write_text("pending\n", encoding="ascii")
                with self.assertRaises(live_readiness.ReadinessError):
                    live_readiness._check_operation_marker_and_plans()
                self.assertEqual(marker.read_text(encoding="ascii"), "pending\n")

    def test_readiness_report_separates_verifier_and_app_source_and_is_read_only(self) -> None:
        verifier_sha = "a" * 40
        app_sha = "b" * 40
        with self.assertRaises(live_readiness.ReadinessError):
            live_readiness._provenance_fields(verifier_sha, app_sha)
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary) / "verifier"
            stage.mkdir(mode=0o700)
            tools_dir = stage / "platform" / "tools"
            tools_dir.mkdir(parents=True, mode=0o700)
            (stage / "platform").chmod(0o700)
            files = []
            for index, relative in enumerate(live_readiness.VERIFIER_FILES):
                contents = f"synthetic verifier module {index}\n".encode("ascii")
                path = tools_dir / Path(relative).name
                path.write_bytes(contents)
                path.chmod(0o644)
                files.append(
                    {
                        "path": relative,
                        "mode": "100644",
                        "size": len(contents),
                        "sha256": hashlib.sha256(contents).hexdigest(),
                        "git_blob": hashlib.sha1(
                            b"blob " + str(len(contents)).encode("ascii") + b"\0" + contents,
                            usedforsecurity=False,
                        ).hexdigest(),
                    }
                )
            manifest_path = stage / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {"schema": 1, "verifier_source_sha": verifier_sha, "files": files},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                encoding="ascii",
            )
            manifest_path.chmod(0o600)
            result = live_readiness._provenance_fields(
                verifier_sha,
                app_sha,
                manifest_path,
                tools_dir=tools_dir,
            )
            self.assertEqual(result["verifier_source_sha"], verifier_sha)
            self.assertEqual(result["app_target_sha"], app_sha)
            self.assertNotEqual(result["verifier_source_sha"], result["app_target_sha"])
            self.assertEqual(
                result["verifier_manifest_sha256"],
                hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(result["purpose"], "read_only_readiness")
            self.assertIs(result["runtime_or_fixture_mutations_authorized"], False)
            (tools_dir / Path(live_readiness.VERIFIER_FILES[0]).name).write_bytes(b"changed\n")
            with self.assertRaises(live_readiness.ReadinessError):
                live_readiness._provenance_fields(
                    verifier_sha,
                    app_sha,
                    manifest_path,
                    tools_dir=tools_dir,
                )
        with self.assertRaises(live_readiness.ReadinessError):
            live_readiness._provenance_fields("not-a-sha", app_sha)

    def test_readiness_runbook_documents_private_alternate_verifier_boundary(self) -> None:
        readiness_doc = (
            REPO_ROOT / "platform/docs/read-only-production-readiness.md"
        ).read_text(encoding="utf-8")
        runbook = (
            (REPO_ROOT / "platform/docs/ssr-capacity-diagnostic-runbook.md").read_text(encoding="utf-8")
            + readiness_doc
        )
        for required in (
            "platform/tools/platform_readonly_live_readiness.py",
            "platform/tools/platform_host_tools_pin.py",
            "platform/tools/platform_host_tools_bundle.py",
            "verifier_source_sha",
            "app_target_sha",
            "--verifier-manifest",
            "runtime changes",
            "fixture creation",
            "git ls-tree",
            "git cat-file blob",
            "SHA-256",
            "shared/.release-operation.json",
            "runtime_or_fixture_mutations_authorized",
        ):
            self.assertIn(required, runbook)
        marker = (
            '/usr/bin/python3.12 -I -B - "$SOURCE_REPO" "$VERIFIER_SOURCE_SHA" '
            '"$STAGE_DIR" <<\'PY\'\n'
        )
        script_start = readiness_doc.index(marker) + len(marker)
        script_end = readiness_doc.index("\nPY\n", script_start)
        stager = readiness_doc[script_start:script_end]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "source"
            repository.mkdir()
            subprocess.run(["git", "init", str(repository)], check=True, capture_output=True)
            subprocess.run(
                ["git", "-C", str(repository), "config", "user.name", "Readiness fixture"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(repository), "config", "user.email", "readiness@example.invalid"],
                check=True,
                capture_output=True,
            )
            for index, relative in enumerate(live_readiness.VERIFIER_FILES):
                source = repository / relative
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text(f"synthetic module {index}\n", encoding="ascii")
                source.chmod(0o644)
            subprocess.run(
                ["git", "-C", str(repository), "add", *live_readiness.VERIFIER_FILES],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(repository), "commit", "-m", "readiness fixture"],
                check=True,
                capture_output=True,
            )
            source_sha = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            evidence = root / "evidence"
            evidence.mkdir(mode=0o700)
            stage = evidence / "verifier"
            previous_umask = os.umask(0o077)
            try:
                staged = subprocess.run(
                    [sys.executable, "-I", "-B", "-", str(repository), source_sha, str(stage)],
                    input=stager,
                    capture_output=True,
                    text=True,
                    check=False,
                )
            finally:
                os.umask(previous_umask)
            self.assertEqual(staged.returncode, 0, staged.stderr)
            self.assertEqual(staged.stdout, "")
            verify_marker = (
                '/usr/bin/python3.12 -I -B - "$STAGE_DIR" "$VERIFIER_SOURCE_SHA" \\\n'
                '  > "$EVIDENCE_DIR/verifier-stage-verification.json" <<\'PY\'\n'
            )
            verify_start = readiness_doc.index(verify_marker) + len(verify_marker)
            verify_end = readiness_doc.index("\nPY\n", verify_start)
            verifier = readiness_doc[verify_start:verify_end]
            checked = subprocess.run(
                [sys.executable, "-I", "-B", "-", str(stage), source_sha],
                input=verifier,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertEqual(
                json.loads(checked.stdout),
                {"schema": 1, "status": "verified", "verifier_source_sha": source_sha, "files": 3},
            )
            provenance = live_readiness._provenance_fields(
                source_sha,
                "b" * 40,
                stage / "manifest.json",
                tools_dir=stage / "platform" / "tools",
            )
            self.assertEqual(provenance["verifier_source_sha"], source_sha)
            self.assertEqual(provenance["app_target_sha"], "b" * 40)
            self.assertIsNotNone(provenance["verifier_manifest_sha256"])

    def test_release_layout_pin_fails_closed_for_schema_identity_and_closure_drift(self) -> None:
        def mutate_contract(release: Path, mutation) -> None:
            path = release / "contracts/host_tools_pin.json"
            payload = json.loads(path.read_text(encoding="ascii"))
            mutation(payload)
            path.write_text(
                json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="ascii",
            )

        mutations = (
            ("unknown top-level field", lambda payload: payload.update(unexpected=True)),
            ("wrong schema", lambda payload: payload.update(schema=True)),
            ("wrong repository", lambda payload: payload.update(repository="other/repo")),
            ("wrong pinned host SHA", lambda payload: payload.update(host_tools_sha="0" * 40)),
            ("missing closure", lambda payload: payload.pop("closure")),
            ("reordered closure", lambda payload: payload.update(closure=list(reversed(payload["closure"]))),),
            (
                "invalid closure path",
                lambda payload: payload["closure"][0].update(path="platform/tools/../escape.py"),
            ),
            (
                "closure path order mismatch",
                lambda payload: payload["closure"][0].update(path="platform/tools/not-the-first-member.py"),
            ),
        )
        for label, mutation in mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                release = self._flattened_release_pin_fixture(Path(temporary))
                mutate_contract(release, mutation)
                with self.assertRaises(pin.HostToolsPinError):
                    pin.validate_release_pin(release, expected_host_tools_sha=PIN_SHA)

        with self.subTest(label="recorded closure digest mismatch"), tempfile.TemporaryDirectory() as temporary:
            release = self._flattened_release_pin_fixture(Path(temporary))
            mutate_contract(
                release,
                lambda payload: payload["closure"][0].update(sha256="a" * 64),
            )
            with self.assertRaises(pin.HostToolsPinError):
                pin.validate_release_pin(release, expected_host_tools_sha=PIN_SHA)

        with self.subTest(label="closure member content mismatch"), tempfile.TemporaryDirectory() as temporary:
            release = self._flattened_release_pin_fixture(Path(temporary))
            member = release / "tools" / Path(pin._read_pin(REPO_ROOT)["closure"][0]["path"]).name
            member.write_bytes(member.read_bytes() + b"\n")
            with self.assertRaises(pin.HostToolsPinError):
                pin.validate_release_pin(release, expected_host_tools_sha=PIN_SHA)

        with self.subTest(label="closure member mode mismatch"), tempfile.TemporaryDirectory() as temporary:
            release = self._flattened_release_pin_fixture(Path(temporary))
            first = pin._read_pin(REPO_ROOT)["closure"][0]
            member = release / "tools" / Path(first["path"]).name
            member.chmod(0o755 if first["mode"] == 0o644 else 0o644)
            with self.assertRaises(pin.HostToolsPinError):
                pin.validate_release_pin(release, expected_host_tools_sha=PIN_SHA)

        with self.subTest(label="closure member symlink"), tempfile.TemporaryDirectory() as temporary:
            release = self._flattened_release_pin_fixture(Path(temporary))
            first = pin._read_pin(REPO_ROOT)["closure"][0]
            member = release / "tools" / Path(first["path"]).name
            replacement = member.with_name(member.name + ".saved")
            member.replace(replacement)
            member.symlink_to(replacement.name)
            with self.assertRaises(pin.HostToolsPinError):
                pin.validate_release_pin(release, expected_host_tools_sha=PIN_SHA)

        with self.subTest(label="closure member hardlink"), tempfile.TemporaryDirectory() as temporary:
            release = self._flattened_release_pin_fixture(Path(temporary))
            first = pin._read_pin(REPO_ROOT)["closure"][0]
            member = release / "tools" / Path(first["path"]).name
            linked = release / "linked-copy"
            os.link(member, linked)
            with self.assertRaises(pin.HostToolsPinError):
                pin.validate_release_pin(release, expected_host_tools_sha=PIN_SHA)

    def test_readiness_release_pointer_requires_exact_source_and_manifest_digest(self) -> None:
        source_sha = "a" * 40
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            releases = root / "releases"
            release = releases / "release-a"
            release.mkdir(parents=True)
            release_json = release / "RELEASE.json"
            release_json.write_text(
                json.dumps({"release_slug": "release-a", "source_git_commit": source_sha}),
                encoding="ascii",
            )
            (root / "current").symlink_to(release)
            digest = hashlib.sha256(release_json.read_bytes()).hexdigest()
            resolved, info = live_readiness._release(root, source_sha, digest, "current")
            self.assertEqual(resolved, release)
            self.assertEqual(info["release_json_sha256"], digest)
            with self.assertRaises(live_readiness.ReadinessError):
                live_readiness._release(root, source_sha, "0" * 64, "current")

    def test_readiness_final_consistency_rejects_changed_pointer_or_service(self) -> None:
        current = (Path("/releases/current"), {"slug": "current"})
        previous = (Path("/releases/previous"), {"slug": "previous"})
        services = {"deadlock-api.service": {"main_pid": 101}}
        lock_results = [{"dev": 1, "ino": 2, "held": False}]
        expected = {
            "root": Path("/opt/oldsparky/platform"),
            "expected_app_sha": "a" * 40,
            "expected_previous_app_sha": "b" * 40,
            "expected_release_manifest_sha": "c" * 64,
            "expected_previous_release_manifest_sha": "d" * 64,
            "current_identity": current,
            "previous_identity": previous,
            "services": services,
            "lock_results": lock_results,
            "timer": {"ActiveState": "active"},
            "maintenance_service": {"SubState": "dead"},
        }
        with ExitStack() as stack:
            release = stack.enter_context(patch.object(
                live_readiness,
                "_release",
                side_effect=[(Path("/releases/replaced"), {"slug": "replaced"}), previous],
            ))
            stack.enter_context(patch.object(live_readiness, "_check_service_environment", return_value=services))
            stack.enter_context(patch.object(live_readiness, "_check_locks", return_value=lock_results))
            stack.enter_context(patch.object(live_readiness, "_check_operation_marker_and_plans"))
            stack.enter_context(patch.object(
                live_readiness,
                "_check_maintenance_state",
                return_value=(expected["timer"], expected["maintenance_service"]),
            ))
            with self.assertRaises(live_readiness.ReadinessError):
                live_readiness._check_final_consistency(**expected)
            self.assertEqual(release.call_count, 2)

        with ExitStack() as stack:
            stack.enter_context(patch.object(live_readiness, "_release", side_effect=[current, previous]))
            stack.enter_context(patch.object(
                live_readiness,
                "_check_service_environment",
                return_value={"deadlock-api.service": {"main_pid": 202}},
            ))
            stack.enter_context(patch.object(live_readiness, "_check_locks", return_value=lock_results))
            stack.enter_context(patch.object(live_readiness, "_check_operation_marker_and_plans"))
            stack.enter_context(patch.object(
                live_readiness,
                "_check_maintenance_state",
                return_value=(expected["timer"], expected["maintenance_service"]),
            ))
            with self.assertRaises(live_readiness.ReadinessError):
                live_readiness._check_final_consistency(**expected)

    def test_installed_generation_rejects_mode_digest_and_inventory_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            generation = self._installed_generation_fixture(root)
            member = generation / "platform_validate_edge_policy.py"
            try:
                generation.chmod(0o700)
                member.chmod(0o644)
                generation.chmod(0o555)
                with self.assertRaises(bundle.HostToolsBundleError):
                    self._verify_installed_fixture(generation)

                generation.chmod(0o700)
                member.chmod(0o644)
                member.write_bytes(member.read_bytes() + b"# tampered\n")
                member.chmod(bundle.EXECUTABLE_MODE)
                generation.chmod(0o555)
                with self.assertRaises(bundle.HostToolsBundleError):
                    self._verify_installed_fixture(generation)

                generation.chmod(0o700)
                member.chmod(0o644)
                member.write_bytes(bundle.verify_bundle(
                    root / "host-tools.zip", expected_source_sha=PIN_SHA
                )["members"][member.name])
                member.chmod(bundle.EXECUTABLE_MODE)
                (generation / "unexpected.txt").write_text("extra", encoding="ascii")
                generation.chmod(0o555)
                with self.assertRaises(bundle.HostToolsBundleError):
                    self._verify_installed_fixture(generation)
            finally:
                generation.chmod(0o700)

    def test_repository_pin_rejects_circular_generation_and_repository_tampering(self) -> None:
        with self.assertRaises(pin.HostToolsPinError):
            pin.resolve_pin(
                REPO_ROOT,
                target_sha="4b795dec048a9abda4577f32f36586bedfc39045",
                expected_repository=pin.EXPECTED_REPOSITORY,
            )
        with self.assertRaises(pin.HostToolsPinError):
            pin.resolve_pin(
                REPO_ROOT,
                target_sha=PIN_SHA,
                expected_repository="attacker/old_sparky",
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            full = root / "full"
            full.mkdir()
            subprocess.run(["git", "-C", str(full), "init", "-q"], check=True)
            subprocess.run(
                ["git", "-C", str(full), "config", "user.name", "Fixture"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(full), "config", "user.email", "fixture@example.invalid"],
                check=True,
            )
            closure_payload = json.loads(
                (REPO_ROOT / pin.PIN_RELATIVE_PATH).read_text(encoding="utf-8")
            )
            for record in closure_payload["closure"]:
                relative = str(record["path"])
                destination = full / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(REPO_ROOT / relative, destination)
                destination.chmod(int(record["mode"]))
            declaration = full / "platform/tools/platform_host_tools_bundle.py"
            declaration.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(
                REPO_ROOT / "platform/tools/platform_host_tools_bundle.py",
                declaration,
            )

            def fixture_git(repository: Path, *arguments: str) -> str:
                completed = subprocess.run(
                    ["git", "-C", str(repository), *arguments],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                return completed.stdout.strip()

            fixture_git(full, "add", "platform/tools")
            fixture_git(full, "commit", "-q", "-m", "host-tools generation")
            generation_sha = fixture_git(full, "rev-parse", "HEAD")
            contract = full / pin.PIN_RELATIVE_PATH
            contract.parent.mkdir(parents=True, exist_ok=True)
            closure_payload["host_tools_sha"] = generation_sha
            contract.write_text(
                json.dumps(closure_payload, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            fixture_git(full, "add", str(pin.PIN_RELATIVE_PATH))
            fixture_git(full, "commit", "-q", "-m", "pin host-tools generation")
            target_sha = fixture_git(full, "rev-parse", "HEAD")
            fixture_git(
                full,
                "remote",
                "add",
                "origin",
                "https://github.com/StrayForest/old_sparky.git",
            )
            self.assertEqual(
                pin.resolve_pin(full, target_sha=target_sha), generation_sha
            )
            closure_payload["host_tools_sha"] = "0" * 40
            contract.write_text(
                json.dumps(closure_payload, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(pin.HostToolsPinError) as missing_failure:
                pin.resolve_pin(full, target_sha=target_sha)
            self.assertEqual(
                missing_failure.exception.diagnostic_code,
                "missing_pinned_commit",
            )

            bare = root / "origin.git"
            shallow = root / "shallow"
            subprocess.run(
                ["git", "clone", "--bare", str(full), str(bare)],
                capture_output=True,
                text=True,
                check=True,
            )
            subprocess.run(
                ["git", "clone", "--depth", "1", f"file://{bare}", str(shallow)],
                capture_output=True,
                text=True,
                check=True,
            )
            fixture_git(
                shallow,
                "remote",
                "set-url",
                "origin",
                "https://github.com/StrayForest/old_sparky.git",
            )
            self.assertEqual(fixture_git(shallow, "rev-parse", "HEAD"), target_sha)
            self.assertEqual(
                fixture_git(shallow, "rev-parse", "--is-shallow-repository"),
                "true",
            )
            with self.assertRaises(pin.HostToolsPinError) as shallow_failure:
                pin.resolve_pin(shallow, target_sha=target_sha)
            self.assertEqual(
                shallow_failure.exception.diagnostic_code, "shallow_history"
            )
            stderr = StringIO()
            with redirect_stderr(stderr):
                result = pin.main(
                    [
                        "resolve",
                        "--source-root",
                        str(shallow),
                        "--target-sha",
                        target_sha,
                        "--expected-repository",
                        pin.EXPECTED_REPOSITORY,
                    ]
                )
            self.assertEqual(result, 2)
            self.assertEqual(
                stderr.getvalue(), "host-tools pin is invalid: shallow_history\n"
            )

    def test_repository_pin_rejects_closure_type_path_and_digest_tampering(self) -> None:
        payload = json.loads(
            (REPO_ROOT / pin.PIN_RELATIVE_PATH).read_text(encoding="utf-8")
        )
        for mutation in (
            lambda value: {**value, "schema": "1"},
            lambda value: {**value, "repository": "StrayForest/old_sparky/escape"},
            lambda value: {**value, "host_tools_sha": 4233},
            lambda value: {**value, "closure": "not-a-list"},
            lambda value: {
                **value,
                "closure": [
                    {**value["closure"][0], "path": "../outside.py"},
                    *value["closure"][1:],
                ],
            },
            lambda value: {
                **value,
                "closure": [
                    {**value["closure"][0], "sha256": "0" * 64},
                    *value["closure"][1:],
                ],
            },
        ):
            with self.subTest(mutation=mutation):
                candidate = mutation(payload)
                with patch.object(pin, "_read_pin", return_value=candidate):
                    with self.assertRaises(pin.HostToolsPinError):
                        pin.resolve_pin(
                            REPO_ROOT,
                            target_sha=self._current_target_sha(),
                            expected_repository=pin.EXPECTED_REPOSITORY,
                        )

    def test_repository_pin_path_and_duplicate_key_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "platform" / "contracts").mkdir(parents=True)
            pin_path = root / pin.PIN_RELATIVE_PATH
            pin_path.write_text('{"schema":1,"schema":1}\n', encoding="utf-8")
            with self.assertRaises(pin.HostToolsPinError):
                pin._read_pin(root)
            pin_path.unlink()
            pin_path.symlink_to(REPO_ROOT / pin.PIN_RELATIVE_PATH)
            with self.assertRaises(pin.HostToolsPinError):
                pin._read_pin(root)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stdout = StringIO()
            stderr = StringIO()
            target_sha = self._current_target_sha()
            arguments = [
                "resolve",
                "--source-root",
                str(REPO_ROOT),
                "--target-sha",
                target_sha,
                "--expected-repository",
                pin.EXPECTED_REPOSITORY,
            ]
            resolve_call = {
                "target_sha": target_sha,
                "expected_repository": pin.EXPECTED_REPOSITORY,
            }
            with redirect_stdout(stdout), redirect_stderr(stderr):
                with patch.object(pin, "resolve_pin", return_value=PIN_SHA) as resolver:
                    result = pin.main(arguments)
            resolver.assert_called_once_with(Path(REPO_ROOT), **resolve_call)
            self.assertEqual(result, 0)
            self.assertEqual(stdout.getvalue(), f"{PIN_SHA}\n")
            self.assertEqual(stderr.getvalue(), "")

            private_output = root / "pin-output"
            old_umask = os.umask(0o022)
            try:
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    stdout.seek(0)
                    stdout.truncate(0)
                    stderr.seek(0)
                    stderr.truncate(0)
                    with patch.object(
                        pin, "resolve_pin", return_value=PIN_SHA
                    ) as resolver:
                        result = pin.main(
                            [*arguments, "--private-output", str(private_output)]
                        )
            finally:
                os.umask(old_umask)
            resolver.assert_called_once_with(Path(REPO_ROOT), **resolve_call)
            self.assertEqual(result, 0)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "")
            metadata = private_output.stat()
            self.assertEqual(private_output.read_bytes(), f"{PIN_SHA}\n".encode("ascii"))
            self.assertTrue(stat.S_ISREG(metadata.st_mode))
            self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
            self.assertEqual(metadata.st_nlink, 1)
            self.assertEqual(metadata.st_uid, os.geteuid())

            sentinel = root / "sentinel"
            sentinel.write_bytes(b"preserve me")
            sentinel_metadata = sentinel.stat()
            for output_path, create_alias in (
                (root / "existing-output", lambda path: path.write_bytes(b"keep")),
                (root / "symlink-output", lambda path: path.symlink_to(sentinel)),
                (root / "hardlink-output", lambda path: os.link(sentinel, path)),
            ):
                with self.subTest(output=output_path.name):
                    create_alias(output_path)
                    with self.assertRaises(pin.HostToolsPinError):
                        pin._write_private_pin_output(output_path, PIN_SHA)
                    self.assertTrue(output_path.exists() or output_path.is_symlink())
                    self.assertEqual(sentinel.read_bytes(), b"preserve me")
                    self.assertEqual(
                        (sentinel.stat().st_dev, sentinel.stat().st_ino),
                        (sentinel_metadata.st_dev, sentinel_metadata.st_ino),
                    )

            partial_output = root / "partial-output"
            real_write = os.write
            calls = 0

            def fail_after_partial_write(descriptor: int, data: bytes) -> int:
                nonlocal calls
                calls += 1
                if calls == 1:
                    return real_write(descriptor, data[:5])
                raise OSError("synthetic write failure")

            with patch.object(pin.os, "write", side_effect=fail_after_partial_write):
                with self.assertRaises(pin.HostToolsPinError):
                    pin._write_private_pin_output(partial_output, PIN_SHA)
            self.assertFalse(partial_output.exists())

    def test_pin_bump_uses_prior_generation_and_rejects_unpinned_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = root / "repo"
            cloned = subprocess.run(
                ["git", "clone", "--no-local", str(REPO_ROOT), str(fixture)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(cloned.returncode, 0, cloned.stderr)
            commands = (
                ["remote", "set-url", "origin", "https://github.com/StrayForest/old_sparky.git"],
                ["config", "user.email", "host-tools-pin-test@example.invalid"],
                ["config", "user.name", "Host tools pin test"],
            )
            for command in commands:
                configured = subprocess.run(
                    ["git", "-C", str(fixture), *command],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(configured.returncode, 0, configured.stderr)

            def git(*arguments: str) -> str:
                completed = subprocess.run(
                    ["git", "-C", str(fixture), *arguments],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                return completed.stdout.strip()

            base_available = subprocess.run(
                ["git", "-C", str(fixture), "cat-file", "-e", f"{PIN_SHA}^{{commit}}"],
                capture_output=True,
                text=True,
                check=False,
            ).returncode == 0
            if base_available:
                subprocess.run(
                    ["git", "-C", str(fixture), "checkout", "--detach", PIN_SHA],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                generation_base = PIN_SHA
            else:
                # The CI checkout is intentionally shallow at the PR merge
                # commit. Build a local base generation when that exact
                # installed commit object is unavailable; the lifecycle
                # assertions below remain identical and network-free.
                for path in (
                    fixture / pin.PIN_RELATIVE_PATH,
                    fixture / "platform/tools/platform_host_tools_pin.py",
                ):
                    if path.exists() or path.is_symlink():
                        path.unlink()
                git("add", "-u")
                git("commit", "-m", "fixture host-tools base generation")
                generation_base = git("rev-parse", "HEAD")
            contract_path = fixture / pin.PIN_RELATIVE_PATH
            contract_path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.loads(
                (REPO_ROOT / pin.PIN_RELATIVE_PATH).read_text(encoding="utf-8")
            )
            closure_paths = tuple(
                str(record["path"]) for record in payload["closure"]
            )
            indexed_modes: dict[str, int] = {}
            for line in git("ls-files", "--stage", "--", *closure_paths).splitlines():
                metadata, tracked_path = line.split("\t", 1)
                mode, _object_id, stage = metadata.split()
                self.assertEqual(stage, "0")
                indexed_modes[tracked_path] = {
                    "100644": 0o644,
                    "100755": 0o755,
                }[mode]
            self.assertEqual(set(indexed_modes), set(closure_paths))
            for relative_path, expected_mode in indexed_modes.items():
                (fixture / relative_path).chmod(expected_mode)
            payload["host_tools_sha"] = generation_base
            contract_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            shutil.copyfile(
                REPO_ROOT / "platform/tools/platform_host_tools_pin.py",
                fixture / "platform/tools/platform_host_tools_pin.py",
            )

            git(
                "add",
                "platform/contracts/host_tools_pin.json",
                "platform/tools/platform_host_tools_pin.py",
            )
            git("commit", "-m", "add host-tools pin contract")
            baseline_target = git("rev-parse", "HEAD")
            self.assertEqual(
                pin.resolve_pin(fixture, target_sha=baseline_target),
                generation_base,
            )

            changed_file = fixture / "platform/tools/platform_storage_evidence_summary.py"
            changed_file.write_bytes(
                changed_file.read_bytes() + b"\n# intentional host-tools bump fixture\n"
            )
            git("add", str(changed_file.relative_to(fixture)))
            git("commit", "-m", "change host-tools closure")
            generation_a = git("rev-parse", "HEAD")
            with self.assertRaises(pin.HostToolsPinError):
                pin.resolve_pin(fixture, target_sha=generation_a)

            payload = json.loads(contract_path.read_text(encoding="utf-8"))
            payload["host_tools_sha"] = generation_a
            changed_record = next(
                record
                for record in payload["closure"]
                if record["path"] == "platform/tools/platform_storage_evidence_summary.py"
            )
            changed_record["sha256"] = hashlib.sha256(
                changed_file.read_bytes()
            ).hexdigest()
            contract_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            git("add", "platform/contracts/host_tools_pin.json")
            git("commit", "-m", "pin provisioned host-tools generation")
            pin_commit_b = git("rev-parse", "HEAD")
            self.assertEqual(
                git("diff-tree", "--no-commit-id", "--name-only", "-r", pin_commit_b),
                "platform/contracts/host_tools_pin.json",
            )
            self.assertEqual(pin.resolve_pin(fixture, target_sha=pin_commit_b), generation_a)

            changed_again = fixture / "platform/tools/platform_validate_edge_policy.py"
            changed_again.write_bytes(
                changed_again.read_bytes() + b"\n# unpinned closure drift fixture\n"
            )
            git("add", str(changed_again.relative_to(fixture)))
            git("commit", "-m", "change host-tools closure without pin")
            with self.assertRaises(pin.HostToolsPinError):
                pin.resolve_pin(fixture, target_sha=git("rev-parse", "HEAD"))

    def test_workflow_keeps_application_and_host_generation_sha_separate(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/platform-production-deploy.yml").read_text(
            encoding="utf-8"
        )
        host_build = workflow.split("  build-host-tools:", 1)[1].split(
            "  host-capability-preflight:", 1
        )[0]
        host_build_step = host_build.split(
            "      - name: Build and verify deterministic host-tools bundle", 1
        )[1].split("      - name: Publish exact host-tools bundle", 1)[0]
        host_preflight = workflow.split("  host-capability-preflight:", 1)[1].split(
            "  build-release:", 1
        )[0]
        preflight = workflow.split("  preflight:", 1)[1].split("  production:", 1)[0]
        production = workflow.split("  production:", 1)[1]
        self.assertIn("platform_host_tools_pin.py", host_build)
        self.assertIn("ref: ${{ steps.resolve_host_tools_pin.outputs.host_tools_sha }}", host_build)
        self.assertIn('--source-sha "$HOST_TOOLS_SHA"', host_build_step)
        self.assertNotIn('--source-sha "$TARGET_SHA"', host_build_step)
        self.assertNotIn("/opt/oldsparky/platform/shared/host-tools/${{ github.sha }}", workflow)
        self.assertIn('"$HOST_TOOLS_SHA"', host_preflight)
        self.assertIn('"$HOST_TOOLS_TOOLSET_VERSION"', host_preflight)
        self.assertIn("Checkout exact source containing shared host verifier", host_preflight)
        artifact_validation = host_preflight.split(
            "      - name: Validate host-tools artifact envelope and bundle", 1
        )[1].split("      - name: Validate root SSH identity and installed generation", 1)[0]
        self.assertIn(
            'if capabilities_path.read_text(encoding="ascii") != expected_capabilities:',
            artifact_validation,
        )
        self.assertIn("needs.host-capability-preflight.outputs.host_tools_sha", preflight)
        self.assertIn("needs.host-capability-preflight.outputs.host_tools_sha", production)

        security = (REPO_ROOT / ".github/workflows/platform-security.yml").read_text(
            encoding="utf-8"
        )
        pin_preflight = security.split("  host-tools-pin-preflight:", 1)[1].split(
            "  backend-static:", 1
        )[0]
        verification = security.split("  verification-contract:", 1)[1].split(
            "  release-runtime:", 1
        )[0]
        self.assertIn("needs: [classifier, baseline-runtime-guard]", pin_preflight)
        self.assertIn("fetch-depth: 0", pin_preflight)
        self.assertIn("ref: ${{ github.sha }}", pin_preflight)
        self.assertIn(
            "name: Resolve and verify canonical host-tools pin against full target history",
            pin_preflight,
        )
        self.assertIn("platform/tools/platform_host_tools_pin.py resolve", pin_preflight)
        self.assertIn('--target-sha "$TARGET_SHA"', pin_preflight)
        self.assertIn('--expected-repository "$EXPECTED_REPOSITORY"', pin_preflight)
        self.assertIn("test ! -e \"$pin_output\"", pin_preflight)
        self.assertIn(
            "needs: [classifier, baseline-runtime-guard, host-tools-pin-preflight]",
            verification,
        )

    def test_host_attestation_policy_accepts_flat_verified_certificate_claims(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/platform-production-deploy.yml").read_text(
            encoding="utf-8"
        )
        preflight = workflow.split("  host-capability-preflight:", 1)[1].split(
            "  validate-active-baseline:", 1
        )[0]
        handoff_steps = [
            step
            for step in _workflow_step_blocks(preflight)
            if "- name: Create closed host-tools handoff after final verification" in step
        ]
        self.assertEqual(len(handoff_steps), 1)
        marker = (
            "          /usr/bin/python3 - \"$attestation_json\" \"$bundle_sha256\" "
            "\"$GITHUB_RUN_ID\" \"$GITHUB_RUN_ATTEMPT\" \"$TARGET_SHA\" "
            "\"$HOST_TOOLS_ARTIFACT_NAME\" <<'PY'\n"
        )
        self.assertIn(marker, handoff_steps[0])
        policy = handoff_steps[0].split(marker, 1)[1].split("\n          PY", 1)[0]
        policy = "\n".join(
            line[10:] if line.startswith("          ") else line
            for line in policy.splitlines()
        )
        production = workflow.split("  production:", 1)[1]
        self.assertEqual(handoff_steps[0].count('certificate.get("extensions", certificate)'), 1)
        self.assertEqual(production.count('certificate.get("extensions", certificate)'), 1)
        production_policy_start = production.index("          attestation_matches = 0\n")
        production_policy_end = production.index("\n          common_files = {", production_policy_start)
        production_policy = "\n".join(
            line[10:] if line.startswith("          ") else line
            for line in production[production_policy_start:production_policy_end].splitlines()
        )

        source_sha = "a" * 40
        bundle_sha = "b" * 64
        run_id = "123456789"
        attempt = "2"
        artifact_name = f"platform-host-tools-bundle-{run_id}-{attempt}"
        build_uri = (
            "https://github.com/StrayForest/old_sparky/.github/workflows/"
            "platform-production-deploy.yml@refs/heads/dev"
        )
        stale_result = {
            "verificationResult": {
                "signature": {
                    "certificate": {
                        "issuer": "https://token.actions.githubusercontent.com",
                        "sourceRepositoryURI": "https://github.com/StrayForest/old_sparky",
                        "sourceRepositoryRef": "refs/heads/dev",
                        "sourceRepositoryDigest": "c" * 40,
                        "buildConfigURI": build_uri,
                        "buildSignerURI": build_uri,
                        "runInvocationURI": "https://github.com/StrayForest/old_sparky/actions/runs/1/attempts/1",
                    }
                },
                "statement": {"subject": [{"digest": {"sha256": bundle_sha}}]},
            }
        }
        current_result = {
            "verificationResult": {
                "signature": {
                    "certificate": {
                        "issuer": "https://token.actions.githubusercontent.com",
                        "sourceRepositoryURI": "https://github.com/StrayForest/old_sparky",
                        "sourceRepositoryRef": "refs/heads/dev",
                        "sourceRepositoryDigest": source_sha,
                        "buildConfigURI": build_uri,
                        "buildSignerURI": build_uri,
                        "runInvocationURI": (
                            "https://github.com/StrayForest/old_sparky/actions/runs/"
                            f"{run_id}/attempts/{attempt}"
                        ),
                    }
                },
                "statement": {"subject": [{"digest": {"sha256": bundle_sha}}]},
            }
        }
        legacy_stale_result = json.loads(json.dumps(stale_result))
        legacy_current_result = json.loads(json.dumps(current_result))
        for legacy_result in (legacy_stale_result, legacy_current_result):
            certificate = legacy_result["verificationResult"]["signature"]["certificate"]
            legacy_result["verificationResult"]["signature"]["certificate"] = {
                "extensions": certificate
            }

        def changed_result(path: tuple[object, ...], value: object) -> dict[str, object]:
            changed = json.loads(json.dumps(current_result))
            target: object = changed
            for part in path[:-1]:
                target = target[part]  # type: ignore[index]
            target[path[-1]] = value  # type: ignore[index]
            return changed

        arguments = [
            bundle_sha,
            run_id,
            attempt,
            source_sha,
            artifact_name,
        ]
        with tempfile.TemporaryDirectory() as temporary:
            attestation_path = Path(temporary) / "verified-attestations.json"

            def run_policy(values: list[dict[str, object]]) -> subprocess.CompletedProcess[str]:
                attestation_path.write_text(json.dumps(values), encoding="utf-8")
                return subprocess.run(
                    [sys.executable, "-c", policy, str(attestation_path), *arguments],
                    capture_output=True,
                    text=True,
                    check=False,
                )

            result = run_policy([stale_result, current_result])
            self.assertEqual(result.returncode, 0, result.stderr)
            legacy_result = run_policy([legacy_stale_result, legacy_current_result])
            self.assertEqual(legacy_result.returncode, 0, legacy_result.stderr)
            duplicate_result = run_policy([current_result, current_result])
            self.assertNotEqual(duplicate_result.returncode, 0)
            for invalid_result in (
                changed_result(
                    ("verificationResult", "signature", "certificate", "sourceRepositoryDigest"),
                    "c" * 40,
                ),
                changed_result(
                    ("verificationResult", "signature", "certificate", "runInvocationURI"),
                    "https://github.com/StrayForest/old_sparky/actions/runs/9/attempts/1",
                ),
                changed_result(
                    ("verificationResult", "statement", "subject", 0, "digest", "sha256"),
                    "d" * 64,
                ),
            ):
                self.assertNotEqual(run_policy([invalid_result]).returncode, 0)

        def run_production_policy(values: list[dict[str, object]]) -> None:
            namespace = {
                "attestations": values,
                "build_uri": build_uri,
                "repository_uri": "https://github.com/StrayForest/old_sparky",
                "invocation_uri": (
                    "https://github.com/StrayForest/old_sparky/actions/runs/"
                    f"{run_id}/attempts/{attempt}"
                ),
                "source_ref": "refs/heads/dev",
                "target_sha": source_sha,
                "bundle_sha": bundle_sha,
            }
            exec(production_policy, namespace)

        run_production_policy([stale_result, current_result])
        run_production_policy([legacy_stale_result, legacy_current_result])
        with self.assertRaisesRegex(
            SystemExit, "host-tools attestation certificate/source/run/subject policy failed"
        ):
            run_production_policy([current_result, current_result])
        for invalid_result in (
            changed_result(
                ("verificationResult", "signature", "certificate", "sourceRepositoryDigest"),
                "c" * 40,
            ),
            changed_result(
                ("verificationResult", "signature", "certificate", "runInvocationURI"),
                "https://github.com/StrayForest/old_sparky/actions/runs/9/attempts/1",
            ),
            changed_result(
                ("verificationResult", "statement", "subject", 0, "digest", "sha256"),
                "d" * 64,
            ),
        ):
            with self.assertRaisesRegex(
                SystemExit, "host-tools attestation certificate/source/run/subject policy failed"
            ):
                run_production_policy([invalid_result])

    def _source_fixture(self, root: Path) -> Path:
        source_root = root / "source"
        tools = source_root / "platform" / "tools"
        tools.mkdir(parents=True)
        for name in bundle.HOST_TOOL_FILES:
            destination = tools / name
            shutil.copyfile(TOOLS_ROOT / name, destination)
            os.chmod(destination, 0o755)
        shutil.copyfile(
            TOOLS_ROOT / "platform_host_tools_bundle.py",
            tools / "platform_host_tools_bundle.py",
        )
        os.chmod(tools / "platform_host_tools_bundle.py", 0o644)
        return source_root

    def test_bundle_is_deterministic_and_separates_closures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source_fixture(root)
            first = root / "first.zip"
            second = root / "second.zip"
            first_summary = bundle.build_bundle(source, SOURCE_SHA, first)
            bundle.build_bundle(source, SOURCE_SHA, second)

            self.assertEqual(first.read_bytes(), second.read_bytes())
            manifest = first_summary["manifest"]
            self.assertIsInstance(manifest, dict)
            self.assertEqual(manifest["source_sha"], SOURCE_SHA)
            self.assertEqual(candidate.MAX_BUNDLE_BYTES, bundle.MAX_BUNDLE_BYTES)
            self.assertEqual(manifest["limits"]["max_bundle_bytes"], bundle.MAX_BUNDLE_BYTES)
            self.assertEqual(
                manifest["components"],
                {key: list(value) for key, value in bundle.COMPONENT_FILES.items()},
            )
            trusted_v4_capabilities = (
                "artifact_prepare",
                "input_guard",
                "production_dispatcher",
                "production_supervisor",
                "production_deploy_control",
                "release_baseline",
                "retained_load_export_cleanup",
                "retained_load_source_binding",
                "python_isolated",
                "python_bytecode_disabled",
                "cpu_diagnostic_plan_control",
            )
            self.assertEqual(manifest["capabilities"], list(trusted_v4_capabilities))
            with zipfile.ZipFile(first) as archive:
                capability_lines = archive.read("platform-host-tools/capabilities.txt")
                self.assertEqual(
                    [
                        line.removeprefix("capability=")
                        for line in capability_lines.decode("ascii").splitlines()
                        if line.startswith("capability=")
                    ],
                    list(trusted_v4_capabilities),
                )
            self.assertNotIn("platform_release_deploy.sh", bundle.HOST_TOOL_FILES)
            self.assertNotIn("platform_run_api.sh", bundle.HOST_TOOL_FILES)
            contract = root / "contract"
            bundle.write_contract_files(first_summary, contract)
            self.assertIn(
                "platform_configure_shared_env.py", (contract / "files.sha256").read_text()
            )
            self.assertEqual((contract / "source_sha").read_text(), f"{SOURCE_SHA}\n")
            self.assertEqual(
                len(first_summary["manifest"]["files"]), len(bundle.HOST_TOOL_FILES) + 1
            )
            self.assertEqual(
                len((contract / "files.sha256").read_text().splitlines()),
                len(bundle.HOST_TOOL_FILES) + 1,
            )
            self.assertEqual(first_summary["manifest"]["toolset_version"], "production-host-tools-v4")
            self.assertIn(
                "platform_cpu_diagnostic_plan.py",
                [record["path"].removeprefix("platform-host-tools/") for record in manifest["files"]],
            )

            # The trusted T builder accepts only the fixed v4 capability
            # order. Reordered or extra capabilities must not be normalized
            # as an equivalent set.
            wrong_order_source = self._source_fixture(root / "wrong-order")
            wrong_order_helper = wrong_order_source / "platform" / "tools" / "platform_host_tools_bundle.py"
            wrong_order_text = wrong_order_helper.read_text(encoding="utf-8")
            ordered_capability_block = (
                '    "python_isolated",\n'
                '    "python_bytecode_disabled",\n'
                '    "cpu_diagnostic_plan_control",\n'
            )
            self.assertIn(ordered_capability_block, wrong_order_text)
            wrong_order_helper.write_text(
                wrong_order_text.replace(
                    ordered_capability_block,
                    '    "cpu_diagnostic_plan_control",\n'
                    '    "python_isolated",\n'
                    '    "python_bytecode_disabled",\n',
                    1,
                ),
                encoding="utf-8",
            )
            wrong_order_archive = root / "wrong-order.zip"
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle.build_bundle(wrong_order_source, SOURCE_SHA, wrong_order_archive)
            self.assertFalse(wrong_order_archive.exists())

            extra_capability_source = self._source_fixture(root / "extra-capability")
            extra_capability_helper = (
                extra_capability_source / "platform" / "tools" / "platform_host_tools_bundle.py"
            )
            extra_capability_text = extra_capability_helper.read_text(encoding="utf-8")
            self.assertIn(ordered_capability_block, extra_capability_text)
            extra_capability_helper.write_text(
                extra_capability_text.replace(
                    ordered_capability_block,
                    '    "python_isolated",\n'
                    '    "python_bytecode_disabled",\n'
                    '    "cpu_diagnostic_plan_control",\n'
                    '    "unapproved_capability",\n',
                    1,
                ),
                encoding="utf-8",
            )
            extra_capability_archive = root / "extra-capability.zip"
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle.build_bundle(extra_capability_source, SOURCE_SHA, extra_capability_archive)
            self.assertFalse(extra_capability_archive.exists())

            # Exercise the immutable v3 compatibility layout as an explicit
            # legacy declaration. The production v4 active declaration above
            # is never extended to manufacture its own expected layout.
            legacy_source = self._source_fixture(root / "legacy")
            legacy_tools = legacy_source / "platform" / "tools"
            helper = legacy_tools / "platform_host_tools_bundle.py"
            helper_text = helper.read_text(encoding="utf-8")
            self.assertIn(
                'CPU_DIAGNOSTIC_CONTROL_FILES = ("platform_cpu_diagnostic_plan.py",)',
                helper_text,
            )
            self.assertIn("+ CPU_DIAGNOSTIC_CONTROL_FILES", helper_text)
            helper_text = helper_text.replace(
                "    + RETAINED_LOAD_ARTIFACT_FILES\n    + CPU_DIAGNOSTIC_CONTROL_FILES\n)",
                "    + RETAINED_LOAD_ARTIFACT_FILES\n)",
                1,
            )
            self.assertIn('    "cpu_diagnostic_control": CPU_DIAGNOSTIC_CONTROL_FILES,\n', helper_text)
            helper_text = helper_text.replace(
                '    "cpu_diagnostic_control": CPU_DIAGNOSTIC_CONTROL_FILES,\n', "", 1
            )
            self.assertIn('    "cpu_diagnostic_plan_control",\n', helper_text)
            helper_text = helper_text.replace('    "cpu_diagnostic_plan_control",\n', "", 1)
            helper_text = helper_text.replace(
                'TOOLSET_VERSION = "production-host-tools-v4"',
                'TOOLSET_VERSION = "production-host-tools-v3"',
                1,
            )
            helper.write_text(helper_text, encoding="utf-8")
            legacy_archive = root / "legacy.zip"
            legacy_summary = bundle.build_bundle(legacy_source, SOURCE_SHA, legacy_archive)
            legacy_manifest = legacy_summary["manifest"]
            self.assertEqual(legacy_manifest["toolset_version"], "production-host-tools-v3")
            self.assertEqual(
                legacy_manifest["components"],
                {key: list(value) for key, value in bundle.LEGACY_COMPONENT_FILES.items()},
            )
            self.assertNotIn("cpu_diagnostic_plan_control", legacy_manifest["capabilities"])
            self.assertEqual(len(legacy_manifest["files"]), len(bundle.LEGACY_HOST_TOOL_FILES) + 1)
            self.assertFalse(
                any(record["path"].endswith("platform_cpu_diagnostic_plan.py") for record in legacy_manifest["files"])
            )
            self.assertEqual(
                tuple(record["path"] for record in bundle.verify_bundle(legacy_archive)["manifest"]["files"]),
                tuple(sorted((*bundle.LEGACY_HOST_TOOL_FILES, "capabilities.txt"))),
            )
            for contract_file in (
                "manifest.sha256",
                "capabilities.sha256",
                "files.sha256",
                "files.modes",
                "source_sha",
                "toolset_version",
            ):
                metadata = (contract / contract_file).lstat()
                self.assertEqual(metadata.st_nlink, 1)
                self.assertEqual(metadata.st_mode & 0o777, 0o600)
            self.assertFalse(any(contract.glob(".*.tmp")))

            oversized = root / "oversized.zip"
            with oversized.open("wb") as stream:
                stream.truncate(bundle.MAX_BUNDLE_BYTES + 1)
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle.verify_bundle(oversized, expected_source_sha=SOURCE_SHA)

    def test_generated_mode_contract_passes_workflow_consumer(self) -> None:
        """Keep files.modes aligned with the production shell consumer.

        The manifest intentionally uses JSON numeric Unix modes (292/365),
        while the workflow compares the text sidecar with `stat -c %a`
        (444/555).  Generate the real bundle/manifest/contract and execute
        the exact mode-validation loop extracted from the workflow so either
        representation cannot drift silently.
        """

        workflow = (REPO_ROOT / ".github/workflows/platform-production-deploy.yml").read_text(
            encoding="utf-8"
        )
        preflight = workflow.split("  host-capability-preflight:", 1)[1].split(
            "  build-release:", 1
        )[0]
        start_marker = '          while read -r expected_mode expected_path; do'
        end_marker = '          done < "$contract_dir/files.modes"'
        start = preflight.index(start_marker)
        end = preflight.index(end_marker, start) + len(end_marker)
        mode_consumer = textwrap.dedent(preflight[start:end])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source_fixture(root)
            archive = root / "bundle.zip"
            summary = bundle.build_bundle(source, SOURCE_SHA, archive)
            verified = bundle.verify_bundle(archive, expected_source_sha=SOURCE_SHA)
            contract = root / "contract"
            bundle.write_contract_files(verified, contract)
            raw_contracts = {
                f"contract/{path.name}": path.read_bytes()
                for path in contract.iterdir()
                if path.is_file()
            }
            validated = operator_provisioner.validate_bundle_contract(
                archive, raw_contracts, SOURCE_SHA, REPO_ROOT,
            )
            self.assertEqual(validated[0], verified["manifest"])

            mode_lines = raw_contracts["contract/files.modes"].splitlines()
            malformed_modes = dict(raw_contracts)
            malformed_modes["contract/files.modes"] = mode_lines[0].replace(b"  ", b" ", 1) + b"\n" + b"\n".join(mode_lines[1:]) + b"\n"
            with self.assertRaisesRegex(
                operator_provisioner.ProvisionError, "outer_modes_contract_invalid",
            ):
                operator_provisioner.validate_bundle_contract(archive, malformed_modes, SOURCE_SHA, REPO_ROOT)

            noncanonical_modes = dict(raw_contracts)
            noncanonical_modes["contract/files.modes"] = b"0" + raw_contracts["contract/files.modes"]
            with self.assertRaisesRegex(
                operator_provisioner.ProvisionError, "outer_modes_contract_invalid",
            ):
                operator_provisioner.validate_bundle_contract(archive, noncanonical_modes, SOURCE_SHA, REPO_ROOT)

            duplicate_modes = dict(raw_contracts)
            duplicate_modes["contract/files.modes"] += mode_lines[0] + b"\n"
            with self.assertRaisesRegex(
                operator_provisioner.ProvisionError, "outer_modes_contract_duplicate",
            ):
                operator_provisioner.validate_bundle_contract(archive, duplicate_modes, SOURCE_SHA, REPO_ROOT)

            manifest = summary["manifest"]
            self.assertIsInstance(manifest, dict)
            modes = (contract / "files.modes").read_text(encoding="ascii").splitlines()
            self.assertIn("444  capabilities.txt", modes)
            self.assertTrue(
                all(
                    line.startswith("555  ")
                    for line in modes
                    if not line.endswith("  capabilities.txt")
                )
            )
            self.assertEqual(
                next(
                    record["mode"]
                    for record in manifest["files"]
                    if record["path"] == "capabilities.txt"
                ),
                bundle.DATA_MODE,
            )
            self.assertEqual(
                next(
                    record["mode"]
                    for record in manifest["files"]
                    if record["path"] == bundle.HOST_TOOL_FILES[0]
                ),
                bundle.EXECUTABLE_MODE,
            )

            completed = subprocess.run(
                [
                    "bash",
                    "-c",
                    f'set -euo pipefail\ncontract_dir="$1"\n{mode_consumer}',
                    "mode-check",
                    str(contract),
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_manifest_and_archive_have_exact_closed_member_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source_fixture(root)
            archive = root / "bundle.zip"
            summary = bundle.build_bundle(source, SOURCE_SHA, archive)
            with zipfile.ZipFile(archive) as opened:
                self.assertEqual(len(opened.infolist()), len(bundle.HOST_TOOL_FILES) + 2)
                self.assertEqual(
                    {info.filename for info in opened.infolist()},
                    {
                        f"{bundle.MEMBER_ROOT}/{name}"
                        for name in (*bundle.HOST_TOOL_FILES, "capabilities.txt", "manifest.json")
                    },
                )
            self.assertEqual(
                [record["path"] for record in summary["manifest"]["files"]],
                sorted(record["path"] for record in summary["manifest"]["files"]),
            )

    def test_archive_member_set_accepts_shuffled_order_but_keeps_canonical_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source_fixture(root)
            original = root / "original.zip"
            bundle.build_bundle(source, SOURCE_SHA, original)
            shuffled = root / "shuffled.zip"
            with zipfile.ZipFile(original) as source_zip, zipfile.ZipFile(
                shuffled, "w", compression=zipfile.ZIP_STORED
            ) as target_zip:
                infos = list(reversed(source_zip.infolist()))
                for info in infos:
                    target_zip.writestr(info, source_zip.read(info))
            verified = bundle.verify_bundle(shuffled, expected_source_sha=SOURCE_SHA)
            paths = {record["path"] for record in verified["manifest"]["files"]}
            self.assertIn("platform_configure_shared_env.py", paths)
            self.assertIn("platform_update_cloudflare_ips.py", paths)

    def test_tampered_archive_and_metadata_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source_fixture(root)
            original = root / "original.zip"
            bundle.build_bundle(source, SOURCE_SHA, original)
            tampered = root / "tampered.zip"
            with zipfile.ZipFile(original) as source_zip, zipfile.ZipFile(
                tampered, "w", compression=zipfile.ZIP_STORED
            ) as target_zip:
                for info in source_zip.infolist():
                    payload = source_zip.read(info)
                    if info.filename.endswith("/manifest.json"):
                        manifest = json.loads(payload)
                        manifest["source_sha"] = "a" * 40
                        payload = (
                            json.dumps(manifest, sort_keys=True, separators=(",", ":"))
                            + "\n"
                        ).encode()
                    target_zip.writestr(info, payload)
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle.verify_bundle(tampered, expected_source_sha=SOURCE_SHA)

            duplicate = root / "duplicate.zip"
            with zipfile.ZipFile(original) as source_zip, zipfile.ZipFile(
                duplicate, "w", compression=zipfile.ZIP_STORED
            ) as target_zip:
                for info in source_zip.infolist():
                    target_zip.writestr(info, source_zip.read(info))
                info = source_zip.infolist()[0]
                target_zip.writestr(info, source_zip.read(info))
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle.verify_bundle(duplicate, expected_source_sha=SOURCE_SHA)

    def test_manifest_numeric_fields_reject_bool_float_and_string_coercion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source_fixture(root)
            archive = root / "bundle.zip"
            bundle.build_bundle(source, SOURCE_SHA, archive)
            with zipfile.ZipFile(archive) as source_zip:
                infos = list(source_zip.infolist())
                members = {info.filename: source_zip.read(info) for info in source_zip.infolist()}

            for field, values in (
                ("schema", (True, 1.0, "1")),
                ("limits", ({"max_bundle_bytes": True, "max_file_bytes": 512 * 1024, "max_file_count": 13},
                             {"max_bundle_bytes": 4 * 1024 * 1024, "max_file_bytes": 512 * 1024.0, "max_file_count": 13},
                             {"max_bundle_bytes": 4 * 1024 * 1024, "max_file_bytes": 512 * 1024, "max_file_count": "13"})),
            ):
                for value in values:
                    with self.subTest(field=field, value=value):
                        manifest = json.loads(members[f"{bundle.MEMBER_ROOT}/manifest.json"])
                        manifest[field] = value
                        tampered = root / f"tampered-{field}-{len(str(value))}.zip"
                        with zipfile.ZipFile(tampered, "w", compression=zipfile.ZIP_STORED) as target:
                            for info in infos:
                                payload = members[info.filename]
                                if info.filename.endswith("/manifest.json"):
                                    payload = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
                                target.writestr(info, payload)
                        with self.assertRaises(bundle.HostToolsBundleError):
                            bundle.verify_bundle(tampered, expected_source_sha=SOURCE_SHA)

    def test_source_links_and_special_files_fail_closed(self) -> None:
        for mutation in ("symlink", "hardlink", "fifo"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = self._source_fixture(root)
                target = source / "platform" / "tools" / bundle.HOST_TOOL_FILES[0]
                if mutation == "symlink":
                    target.unlink()
                    target.symlink_to("platform_workflow_input_guard.py")
                elif mutation == "hardlink":
                    peer = root / "peer"
                    peer.write_bytes(target.read_bytes())
                    target.unlink()
                    os.link(peer, target)
                else:
                    target.unlink()
                    os.mkfifo(target)
                with self.assertRaises(bundle.HostToolsBundleError):
                    bundle.build_bundle(source, SOURCE_SHA, root / "bundle.zip")

    def test_staged_isolated_dispatcher_import_has_no_ambient_dependency(self) -> None:
        self.assertEqual(sys.version_info[:2], (3, 12))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staged = root / "host-tools" / SOURCE_SHA
            staged.mkdir(parents=True)
            shutil.copyfile(
                TOOLS_ROOT / "platform_workflow_remote_dispatch.py",
                staged / "platform_workflow_remote_dispatch.py",
            )
            shutil.copyfile(
                TOOLS_ROOT / "platform_workflow_input_guard.py",
                staged / "platform_workflow_input_guard.py",
            )
            decoy = root / "decoy"
            decoy.mkdir()
            (decoy / "platform_workflow_input_guard.py").write_text(
                "raise RuntimeError('ambient guard loaded')\n", encoding="utf-8"
            )
            environment = {
                "HOME": str(root / "home"),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": "/usr/bin:/bin",
                "PYTHONPATH": str(decoy),
            }
            completed = subprocess.run(
                [
                    "/usr/bin/python3",
                    "-I",
                    "-B",
                    str(staged / "platform_workflow_remote_dispatch.py"),
                    "host-capabilities",
                ],
                cwd=decoy,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertNotIn("ModuleNotFoundError", completed.stderr)
            self.assertNotIn("ambient guard loaded", completed.stderr)

            # Exercise the real installed bundle contour in a writable
            # staging tree. The dispatcher is still invoked with the exact
            # production flags, while metadata checks are patched only in
            # this child so the test does not touch /opt or require root.
            source = self._source_fixture(root)
            archive = root / "platform-host-tools-bundle.zip"
            bundle.build_bundle(source, SOURCE_SHA, archive)
            installed = root / "shared" / "host-tools" / SOURCE_SHA
            installed.parent.mkdir(parents=True)
            with zipfile.ZipFile(archive) as source_zip:
                source_zip.extractall(installed.parent.parent.parent)
            extracted = installed.parent.parent.parent / bundle.MEMBER_ROOT
            extracted.rename(installed)
            for path in installed.iterdir():
                os.chmod(path, 0o755 if path.name in bundle.HOST_TOOL_FILES else 0o644)
            os.chmod(installed, 0o755)

            def inventory() -> dict[str, bytes]:
                return {
                    str(path.relative_to(installed)): path.read_bytes()
                    for path in installed.rglob("*")
                    if path.is_file()
                }

            before = inventory()
            child = """
from pathlib import Path
import sys
import types

dispatcher_path = Path(sys.argv[1])
host_tools_root = Path(sys.argv[2])
sys.path.insert(0, str(dispatcher_path.parent))
module = types.ModuleType("staged_dispatcher")
module.__file__ = str(dispatcher_path)
exec(compile(dispatcher_path.read_text(encoding="utf-8"), str(dispatcher_path), "exec"), module.__dict__)
module.HOST_TOOLS_ROOT = host_tools_root
module.ACTIVE_TOOLS_DIR = dispatcher_path.parent
module._trusted_generation = lambda: True
module._trusted_host_helper = lambda _path: True
module._trusted_data = lambda _path: True
module._retained_load_export_owner = lambda: {"uid": 65534, "gid": 65534}
raise SystemExit(module.main(["host-capabilities"]))
"""

            def limited_run(*flags: str) -> subprocess.CompletedProcess[str]:
                def limit_file_size() -> None:
                    resource.setrlimit(resource.RLIMIT_FSIZE, (512, 512))

                environment = os.environ.copy()
                # The defense environment is supplemental; the immutable
                # dispatcher must still see the literal interpreter flag.
                environment["PYTHONDONTWRITEBYTECODE"] = "1"
                return subprocess.run(
                    [
                        sys.executable,
                        *flags,
                        "-c",
                        child,
                        str(installed / "platform_workflow_remote_dispatch.py"),
                        str(installed.parent),
                    ],
                    cwd=root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                    preexec_fn=limit_file_size,
                )

            expected = (
                f"HOST_TOOLS schema=1 source_sha={SOURCE_SHA} generation={SOURCE_SHA} "
                "dispatcher=4 artifact_prepare=2 supervisor=3 input_guard=2 "
                "release_baseline=1 retained_load_export_cleanup=1 "
                "retained_load_source_binding=1 "
                "cpu_diagnostic_plan_control=1 "
                "python_isolated=1 python_bytecode_disabled=1\n"
            )
            bounded = limited_run("-I", "-B")
            self.assertEqual(bounded.returncode, 0, bounded.stderr)
            self.assertEqual(bounded.stdout, expected)
            self.assertEqual(inventory(), before)
            self.assertFalse(any(path.name == "__pycache__" for path in installed.rglob("*")))
            self.assertFalse(any(path.suffix == ".pyc" for path in installed.rglob("*")))

            # Omitting -B must be rejected before the sibling import can
            # create a truncated pyc under the same tight file-size limit.
            without_bytecode_flag = limited_run("-I")
            self.assertEqual(without_bytecode_flag.returncode, 2)
            self.assertEqual(inventory(), before)
            self.assertFalse(any(path.name == "__pycache__" for path in installed.rglob("*")))
            self.assertFalse(any(path.suffix == ".pyc" for path in installed.rglob("*")))

            extra_directory = installed / "__pycache__"
            extra_directory.mkdir()
            self.assertEqual(limited_run("-I", "-B").returncode, 2)
            extra_directory.rmdir()
            self.assertEqual(inventory(), before)

    def test_artifact_metadata_binding_is_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            metadata = Path(temporary) / "metadata.json"
            archive = Path(temporary) / "artifact.zip"
            archive.write_bytes(b"zip")
            metadata.write_text(
                json.dumps(
                    {
                        "id": 123,
                        "name": "platform-host-tools-bundle-99-1",
                        "expired": False,
                        "digest": ARTIFACT_DIGEST,
                        "size_in_bytes": 3,
                        "workflow_run": {
                            "id": 99,
                            "run_attempt": 1,
                            "head_branch": "dev",
                            "head_sha": SOURCE_SHA,
                        },
                    }
                ),
                encoding="utf-8",
            )
            bundle.verify_artifact_metadata(
                metadata,
                artifact_id="123",
                artifact_name="platform-host-tools-bundle-99-1",
                run_id="99",
                run_attempt="1",
                source_sha=SOURCE_SHA,
                expected_branch="dev",
                artifact_digest=ARTIFACT_DIGEST,
                archive_path=archive,
            )
            metadata.write_text(
                json.dumps(
                    {
                        "id": 123,
                        "name": "platform-host-tools-bundle-99-1",
                        "expired": False,
                        "digest": ARTIFACT_DIGEST,
                        "size_in_bytes": 3,
                        "workflow_run": {
                            "id": 99,
                            "head_branch": "dev",
                            "head_sha": SOURCE_SHA,
                        },
                    }
                ),
                encoding="utf-8",
            )
            # GitHub's artifact API omits workflow_run.run_attempt.  The
            # artifact envelope remains valid; the dedicated attempt payload
            # below is the authoritative source for that field.
            bundle.verify_artifact_metadata(
                metadata,
                artifact_id="123",
                artifact_name="platform-host-tools-bundle-99-1",
                run_id="99",
                run_attempt="1",
                source_sha=SOURCE_SHA,
                expected_branch="dev",
                artifact_digest=ARTIFACT_DIGEST,
                archive_path=archive,
            )
            null_attempt_payload = json.loads(metadata.read_text(encoding="utf-8"))
            null_attempt_payload["workflow_run"]["run_attempt"] = None
            metadata.write_text(json.dumps(null_attempt_payload), encoding="utf-8")
            bundle.verify_artifact_metadata(
                metadata,
                artifact_id="123",
                artifact_name="platform-host-tools-bundle-99-1",
                run_id="99",
                run_attempt="1",
                source_sha=SOURCE_SHA,
                expected_branch="dev",
                artifact_digest=ARTIFACT_DIGEST,
                archive_path=archive,
            )
            for invalid_attempt in (False, "1", 1.0, 2):
                invalid_payload = json.loads(metadata.read_text(encoding="utf-8"))
                invalid_payload["workflow_run"]["run_attempt"] = invalid_attempt
                metadata.write_text(json.dumps(invalid_payload), encoding="utf-8")
                with self.subTest(invalid_attempt=invalid_attempt):
                    with self.assertRaises(bundle.HostToolsBundleError):
                        bundle.verify_artifact_metadata(
                            metadata,
                            artifact_id="123",
                            artifact_name="platform-host-tools-bundle-99-1",
                            run_id="99",
                            run_attempt="1",
                            source_sha=SOURCE_SHA,
                            expected_branch="dev",
                            artifact_digest=ARTIFACT_DIGEST,
                            archive_path=archive,
                        )
            metadata.write_text(
                json.dumps(
                    {
                        "id": 123,
                        "name": "platform-host-tools-bundle-99-1",
                        "expired": False,
                        "digest": ARTIFACT_DIGEST,
                        "size_in_bytes": 3,
                        "workflow_run": {
                            "id": 99,
                            "run_attempt": 1,
                            "head_branch": "dev",
                            "head_sha": SOURCE_SHA,
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle.verify_artifact_metadata(
                    metadata,
                    artifact_id="123",
                    artifact_name="platform-host-tools-bundle-99-1",
                    run_id="99",
                    run_attempt="2",
                    source_sha=SOURCE_SHA,
                    expected_branch="dev",
                    artifact_digest=ARTIFACT_DIGEST,
                    archive_path=archive,
                )
            attempt = Path(temporary) / "attempt.json"
            attempt.write_text(
                json.dumps(
                    {
                        "id": 99,
                        "run_attempt": 1,
                        "head_sha": SOURCE_SHA,
                        "head_branch": "dev",
                        "event": "workflow_dispatch",
                        "ref": None,
                        "repository": {
                            "full_name": "StrayForest/old_sparky",
                            "name": "old_sparky",
                            "owner": {"login": "StrayForest"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            bundle.verify_workflow_attempt(
                attempt,
                run_id="99",
                run_attempt="1",
                source_sha=SOURCE_SHA,
                repository="StrayForest/old_sparky",
                expected_branch="dev",
                expected_event="workflow_dispatch",
            )
            for missing_field in ("id", "run_attempt", "head_sha", "repository", "head_branch", "event"):
                missing_payload = json.loads(attempt.read_text(encoding="utf-8"))
                missing_payload.pop(missing_field)
                attempt.write_text(json.dumps(missing_payload), encoding="utf-8")
                with self.subTest(missing_attempt_field=missing_field):
                    with self.assertRaises(bundle.HostToolsBundleError):
                        bundle.verify_workflow_attempt(
                            attempt,
                            run_id="99",
                            run_attempt="1",
                            source_sha=SOURCE_SHA,
                            repository="StrayForest/old_sparky",
                            expected_branch="dev",
                            expected_event="workflow_dispatch",
                        )
                attempt.write_text(
                    json.dumps(
                        {
                            "id": 99,
                            "run_attempt": 1,
                            "head_sha": SOURCE_SHA,
                            "head_branch": "dev",
                            "event": "workflow_dispatch",
                            "ref": None,
                            "repository": {
                                "full_name": "StrayForest/old_sparky",
                                "name": "old_sparky",
                                "owner": {"login": "StrayForest"},
                            },
                        }
                    ),
                    encoding="utf-8",
                )
            for field, invalid in (
                ("id", 100),
                ("run_attempt", False),
                ("run_attempt", "1"),
                ("run_attempt", 1.0),
                ("head_sha", "b" * 40),
                ("head_branch", "feature"),
                ("event", "push"),
                (
                    "repository",
                    {
                        "full_name": "attacker/old_sparky",
                        "name": "old_sparky",
                        "owner": {"login": "attacker"},
                    },
                ),
            ):
                invalid_payload = json.loads(attempt.read_text(encoding="utf-8"))
                invalid_payload[field] = invalid
                attempt.write_text(json.dumps(invalid_payload), encoding="utf-8")
                with self.subTest(attempt_field=field, invalid=invalid):
                    with self.assertRaises(bundle.HostToolsBundleError):
                        bundle.verify_workflow_attempt(
                            attempt,
                            run_id="99",
                            run_attempt="1",
                            source_sha=SOURCE_SHA,
                            repository="StrayForest/old_sparky",
                            expected_branch="dev",
                            expected_event="workflow_dispatch",
                        )
                attempt.write_text(
                    json.dumps(
                        {
                            "id": 99,
                            "run_attempt": 1,
                            "head_sha": SOURCE_SHA,
                            "head_branch": "dev",
                            "event": "workflow_dispatch",
                            "ref": None,
                            "repository": {
                                "full_name": "StrayForest/old_sparky",
                                "name": "old_sparky",
                                "owner": {"login": "StrayForest"},
                            },
                        }
                    ),
                    encoding="utf-8",
                )
            attempt.write_text("{" + "x" * (bundle.MAX_FILE_BYTES + 1), encoding="utf-8")
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle.verify_workflow_attempt(
                    attempt,
                    run_id="99",
                    run_attempt="1",
                    source_sha=SOURCE_SHA,
                    repository="StrayForest/old_sparky",
                    expected_branch="dev",
                    expected_event="workflow_dispatch",
                )
            attempt.unlink()
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle.verify_workflow_attempt(
                    attempt,
                    run_id="99",
                    run_attempt="1",
                    source_sha=SOURCE_SHA,
                    repository="StrayForest/old_sparky",
                    expected_branch="dev",
                    expected_event="workflow_dispatch",
                )
            metadata.write_text(
                '{"id":123,"id":124,"name":"platform-host-tools-bundle-99-1",'
                f'"expired":false,"digest":"{ARTIFACT_DIGEST}",'
                '"size_in_bytes":3,'
                '"workflow_run":{"id":99,"run_attempt":1,"head_branch":"dev",'
                f'"head_sha":"{SOURCE_SHA}"}}',
                encoding="utf-8",
            )
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle.verify_artifact_metadata(
                    metadata,
                    artifact_id="123",
                    artifact_name="platform-host-tools-bundle-99-1",
                    run_id="99",
                    run_attempt="1",
                    source_sha=SOURCE_SHA,
                    expected_branch="dev",
                    artifact_digest=ARTIFACT_DIGEST,
                    archive_path=archive,
                )

            valid_metadata = {
                "id": 123,
                "name": "platform-host-tools-bundle-99-1",
                "expired": False,
                "digest": ARTIFACT_DIGEST,
                "size_in_bytes": 3,
                "workflow_run": {
                    "id": 99,
                    "head_branch": "dev",
                    "head_sha": SOURCE_SHA,
                },
            }
            for missing_field in (
                "id",
                "name",
                "expired",
                "digest",
                "size_in_bytes",
                "workflow_run",
            ):
                missing_metadata = json.loads(json.dumps(valid_metadata))
                missing_metadata.pop(missing_field)
                metadata.write_text(json.dumps(missing_metadata), encoding="utf-8")
                with self.subTest(missing_artifact_field=missing_field):
                    with self.assertRaises(bundle.HostToolsBundleError):
                        bundle.verify_artifact_metadata(
                            metadata,
                            artifact_id="123",
                            artifact_name="platform-host-tools-bundle-99-1",
                            run_id="99",
                            run_attempt="1",
                            source_sha=SOURCE_SHA,
                            expected_branch="dev",
                            artifact_digest=ARTIFACT_DIGEST,
                            archive_path=archive,
                        )
            for field, invalid in (
                ("id", False),
                ("id", "123"),
                ("name", 123),
                ("expired", 0),
                ("expired", True),
                ("digest", "sha256:" + "f" * 64),
                ("digest", False),
                ("size_in_bytes", False),
                ("size_in_bytes", "3"),
                ("size_in_bytes", 3.0),
                ("size_in_bytes", 0),
                ("size_in_bytes", bundle.MAX_ARTIFACT_ARCHIVE_BYTES + 1),
                ("size_in_bytes", 4),
                (
                    "workflow_run",
                    {"id": 99, "head_branch": "feature", "head_sha": SOURCE_SHA},
                ),
                (
                    "workflow_run",
                    {"id": 100, "head_branch": "dev", "head_sha": SOURCE_SHA},
                ),
                (
                    "workflow_run",
                    {"id": "99", "head_branch": "dev", "head_sha": SOURCE_SHA},
                ),
                (
                    "workflow_run",
                    {"id": 99, "head_branch": True, "head_sha": SOURCE_SHA},
                ),
                (
                    "workflow_run",
                    {"id": 99, "head_branch": "dev", "head_sha": False},
                ),
            ):
                invalid_metadata = json.loads(json.dumps(valid_metadata))
                invalid_metadata[field] = invalid
                metadata.write_text(json.dumps(invalid_metadata), encoding="utf-8")
                with self.subTest(artifact_field=field, invalid=invalid):
                    with self.assertRaises(bundle.HostToolsBundleError):
                        bundle.verify_artifact_metadata(
                            metadata,
                            artifact_id="123",
                            artifact_name="platform-host-tools-bundle-99-1",
                            run_id="99",
                            run_attempt="1",
                            source_sha=SOURCE_SHA,
                            expected_branch="dev",
                            artifact_digest=ARTIFACT_DIGEST,
                            archive_path=archive,
                        )

            metadata.write_text("{", encoding="utf-8")
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle.verify_artifact_metadata(
                    metadata,
                    artifact_id="123",
                    artifact_name="platform-host-tools-bundle-99-1",
                    run_id="99",
                    run_attempt="1",
                    source_sha=SOURCE_SHA,
                    expected_branch="dev",
                    artifact_digest=ARTIFACT_DIGEST,
                    archive_path=archive,
                )
            metadata.write_bytes(b"{" + b"x" * bundle.MAX_FILE_BYTES)
            with self.assertRaises(bundle.HostToolsBundleError):
                bundle.verify_artifact_metadata(
                    metadata,
                    artifact_id="123",
                    artifact_name="platform-host-tools-bundle-99-1",
                    run_id="99",
                    run_attempt="1",
                    source_sha=SOURCE_SHA,
                    expected_branch="dev",
                    artifact_digest=ARTIFACT_DIGEST,
                    archive_path=archive,
                )

    def test_host_path_references_stay_inside_declared_components(self) -> None:
        supervisor = (TOOLS_ROOT / "platform_production_deploy_supervisor.sh").read_text()
        dispatcher_source = (TOOLS_ROOT / "platform_workflow_remote_dispatch.py").read_text()
        host_path_references = set(
            re.findall(r"\$host_tools_dir/(platform_[A-Za-z0-9_.-]+\.(?:py|sh))", supervisor)
        )
        self.assertTrue(host_path_references)
        self.assertTrue(host_path_references <= set(bundle.HOST_TOOL_FILES))
        self.assertTrue(
            all(name in supervisor for name in bundle.PRODUCTION_DEPLOY_CONTROL_FILES)
        )
        manifest_set = re.search(
            r"(?ms)^expected_files = \{\n(?P<body>.*?)^\}", supervisor
        )
        self.assertIsNotNone(manifest_set)
        manifest_names = set(
            re.findall(
                r'^\s+"(platform_[A-Za-z0-9_.-]+\.(?:py|sh)|capabilities\.txt)",$',
                manifest_set["body"],
                re.M,
            )
        )
        self.assertEqual(
            manifest_names, set(bundle.HOST_TOOL_FILES) | {"capabilities.txt"}
        )
        helper_loop = re.search(
            r"(?ms)^for host_helper in \\\n(?P<body>.*?)^done$", supervisor
        )
        self.assertIsNotNone(helper_loop)
        helper_names = set(
            re.findall(
                r"^\s+(platform_[A-Za-z0-9_.-]+\.(?:py|sh))",
                helper_loop["body"],
                re.M,
            )
        ) | {"platform_storage_evidence_summary.py"}
        self.assertEqual(helper_names, set(bundle.HOST_TOOL_FILES))
        self.assertTrue(set(bundle.PREPARE_ARTIFACT_FILES) <= set(bundle.HOST_TOOL_FILES))
        self.assertEqual(
            bundle.RETAINED_LOAD_ARTIFACT_FILES,
            ("platform_retained_load_export_executor.py",),
        )
        self.assertNotIn("platform_release_deploy.sh", bundle.HOST_TOOL_FILES)
        self.assertNotIn("platform_run_api.sh", bundle.HOST_TOOL_FILES)
        self.assertIn("platform_prepare_artifact_dir.py", dispatcher_source)
        self.assertIn("platform_retained_load_export_executor.py", dispatcher_source)

        class CompletedChild:
            returncode = 0

            def __init__(self) -> None:
                class InputStream:
                    def __init__(self) -> None:
                        self.buffer = bytearray()

                    def write(self, payload: bytes) -> int:
                        self.buffer.extend(payload)
                        return len(payload)

                    def flush(self) -> None:
                        return None

                    def close(self) -> None:
                        return None

                self.stdin = InputStream()
                read_fd, write_fd = os.pipe()
                os.write(
                    write_fd,
                    b"ARTIFACT_EXPORT_REMOVE schema=1 status=passed removed_roots=0\n",
                )
                os.close(write_fd)
                self.stdout = os.fdopen(read_fd, "rb", buffering=0)

            @property
            def input_bytes(self) -> bytes:
                return bytes(self.stdin.buffer)

            def poll(self) -> int:
                return self.returncode

            def wait(self, *, timeout: float) -> int:
                return self.returncode

            def communicate(self, *, input: bytes, timeout: float) -> tuple[bytes, bytes]:
                self.stdin.write(input)
                return b"", b""

        child = CompletedChild()
        with patch.object(dispatcher.os, "geteuid", return_value=0), \
            patch.object(dispatcher, "_trusted_generation", return_value=True), \
            patch.object(dispatcher, "_trusted_export_executor", return_value=True), \
            patch.object(dispatcher, "_current_pin_matches_host_generation", return_value=True), \
            patch.object(
                dispatcher,
                "_retained_load_export_owner",
                return_value={"uid": 987, "gid": 987},
            ), \
            patch.object(dispatcher.subprocess, "Popen", return_value=child) as popen:
            self.assertEqual(
                dispatcher._remove_exports(
                    load_run_id="12345",
                    cleanup_run_id="67890",
                    target_sha="a" * 40,
                ),
                0,
            )
        command = popen.call_args.args[0]
        self.assertEqual(
            command,
            [
                dispatcher.SETPRIV,
                "--reuid=987",
                "--regid=987",
                "--clear-groups",
                "--no-new-privs",
                "--bounding-set=-all",
                "--inh-caps=-all",
                "--ambient-caps=-all",
                "--",
                dispatcher.SYSTEM_PYTHON,
                "-I",
                "-B",
                str(dispatcher.RETAINED_LOAD_EXPORT_EXECUTOR),
                "remove",
            ],
        )
        child_options = popen.call_args.kwargs
        self.assertEqual(child_options["cwd"], "/")
        self.assertEqual(child_options["env"], dispatcher.EXPORT_EXECUTOR_ENV)
        self.assertEqual(child_options["umask"], 0o077)
        self.assertTrue(child_options["close_fds"])
        self.assertEqual(
            json.loads(child.input_bytes.decode("ascii")),
            {"schema": 1, "load_run_id": "12345", "cleanup_run_id": "67890"},
        )
        self.assertEqual(set(json.loads(child.input_bytes)), {"schema", "load_run_id", "cleanup_run_id"})
        self.assertEqual(
            dispatcher._remove_exports(
                load_run_id="../12345",
                cleanup_run_id="67890",
                target_sha="a" * 40,
            ),
            1,
        )

        real_popen = subprocess.Popen

        def run_cleanup_child(script: str, expected_exit: int) -> tuple[str, list[str]]:
            output = StringIO()
            observed_command: list[str] = []

            def spawn(command: list[str], **options: object) -> subprocess.Popen[bytes]:
                observed_command.extend(command)
                return real_popen(
                    [sys.executable, "-c", script],
                    **options,
                )

            with patch.object(dispatcher, "_trusted_helper", return_value=True), \
                patch.object(dispatcher.subprocess, "Popen", side_effect=spawn), \
                redirect_stdout(output):
                result = dispatcher._run_retained_cleanup_sudo(
                    Path("/fixed/cleanup-helper"),
                    ["closed", "target-sha", "12345", "67890"],
                    control_email="private@example.invalid",
                    diagnostic_binding={
                        "source_sha": "a" * 40,
                        "app_sha": "b" * 40,
                        "run_id": "12345",
                        "load_run_id": "12345",
                        "run_attempt": "1",
                        "profile": "ready-vote-slo-v2",
                    },
                )
            self.assertEqual(result, expected_exit)
            self.assertNotIn("private@example.invalid", observed_command)
            return output.getvalue(), observed_command

        failed_marker, _ = run_cleanup_child(
            "import json,sys; assert json.load(sys.stdin) == "
            "{'schema': 1, 'control_email': 'private@example.invalid'}; "
            "print('private output must not escape'); "
            "print('RETAINED_CLEANUP_STAGE schema=1 stage=external_vote_recovery exit_code=7'); "
            "raise SystemExit(7)",
            7,
        )
        self.assertEqual(
            failed_marker,
            "RETAINED_CLEANUP_DIAGNOSTIC schema=2 stage=external_vote_recovery "
            "dispatcher_exit=7 child_state=exited child_exit=7 stdout_eof=true "
            "timed_out=false source_sha="
            + "a" * 40
            + " app_sha="
            + "b" * 40
            + " run_id=12345 run_attempt=1 load_run_id=12345 profile=ready-vote-slo-v2\n",
        )
        missing_marker, _ = run_cleanup_child(
            "import json,sys; assert json.load(sys.stdin) == "
            "{'schema': 1, 'control_email': 'private@example.invalid'}; "
            "print('untrusted output'); raise SystemExit(1)",
            1,
        )
        self.assertEqual(
            missing_marker,
            "RETAINED_CLEANUP_DIAGNOSTIC schema=2 stage=unknown "
            "dispatcher_exit=1 child_state=exited child_exit=1 stdout_eof=true "
            "timed_out=false source_sha="
            + "a" * 40
            + " app_sha="
            + "b" * 40
            + " run_id=12345 run_attempt=1 load_run_id=12345 profile=ready-vote-slo-v2\n",
        )

        with (
            patch.object(dispatcher, "CLEANUP_OPERATION_TIMEOUT_SECONDS", 1.5),
            patch.object(dispatcher, "CHILD_TERMINATION_GRACE_SECONDS", 0.15),
        ):
            # Allow the parent interpreter to spawn under CI contention. The
            # ten-second descendant still holds stdout beyond this bounded
            # deadline, so the test keeps exercising inherited-pipe cleanup.
            timed_out_marker, _ = run_cleanup_child(
                "import subprocess,sys; "
                "print('RETAINED_CLEANUP_STAGE schema=1 stage=complete exit_code=0', flush=True); "
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)'], "
                "stdout=sys.stdout, stderr=subprocess.DEVNULL); sys.exit(0)",
                124,
            )
        self.assertEqual(
            timed_out_marker,
            "RETAINED_CLEANUP_DIAGNOSTIC schema=2 stage=timeout "
            "dispatcher_exit=124 child_state=exited child_exit=0 stdout_eof=false "
            "timed_out=true source_sha="
            + "a" * 40
            + " app_sha="
            + "b" * 40
            + " run_id=12345 run_attempt=1 load_run_id=12345 profile=ready-vote-slo-v2\n",
        )

    def test_declared_host_tools_are_the_recursive_static_runtime_closure(self) -> None:
        names = set(bundle.HOST_TOOL_FILES)
        tools = {name: (TOOLS_ROOT / name).read_text(encoding="utf-8") for name in names}
        # Ignore the supervisor's metadata-only helper inventory.  Dependencies
        # must instead be discovered from fixed host-tools path expressions and
        # imports; candidate/runtime paths are intentionally outside this set.
        supervisor = tools["platform_production_deploy_supervisor.sh"]
        supervisor = re.sub(r"for host_helper in \\\n.*?done\n", "", supervisor, flags=re.DOTALL)
        tools["platform_production_deploy_supervisor.sh"] = supervisor
        reference = re.compile(
            r"(?:\$host_tools_dir/|\$SCRIPT_DIR/|ACTIVE_TOOLS_DIR\s*/\s*['\"]|"
            r"with_name\(['\"]|from\s+(?:\.\s*)?)(?P<name>"
            r"platform_[A-Za-z0-9_.-]+\.(?:py|sh))"
        )
        python_import = re.compile(
            r"from\s+(?:\.\s*)?(?P<module>platform_[A-Za-z0-9_]+)\s+import"
        )
        boundary_allowlist = {
            # Candidate/runtime or separately provisioned operator helpers.
            "platform_release_deploy.sh",
            "platform_run_api.sh",
            "platform_run_worker.sh",
            "platform_run_web.sh",
            "platform_run_alembic.sh",
            "platform_deploy_smoke.py",
            "platform_backup_restore_drill.py",
            # Dispatcher-owned external workflows intentionally outside this bundle.
            "platform_production_external_fixture_qa.sh",
            "platform_production_retained_load_cleanup_qa.sh",
            "platform_live_launch_supervisor.sh",
            "platform_live_user_qa_dispatch.py",
            "platform_live_launch_trusted.sh",
        }
        self.assertNotIn(dispatcher.RETAINED_LOAD_CLEANUP_HELPER_NAME, names)
        self.assertIn(dispatcher.RETAINED_LOAD_CLEANUP_HELPER_NAME, boundary_allowlist)

        def build_graph(contents: dict[str, str]) -> dict[str, set[str]]:
            return {
                name: (
                    {match.group("name") for match in reference.finditer(text)}
                    | {f"{match.group('module')}.py" for match in python_import.finditer(text)}
                )
                for name, text in contents.items()
            }

        graph = build_graph(tools)
        discovered = set().union(*graph.values())
        local_tools = {path.name for path in TOOLS_ROOT.glob("platform_*")}
        self.assertTrue(discovered <= local_tools)
        self.assertEqual(discovered - names - boundary_allowlist, set())
        reachable = set()
        frontier = {"platform_workflow_remote_dispatch.py", "platform_production_deploy_supervisor.sh"}
        while frontier:
            name = frontier.pop()
            if name in reachable:
                continue
            reachable.add(name)
            frontier.update((graph[name] & names) - reachable)
        self.assertEqual(reachable, names)
        self.assertNotIn("platform_release_restore_runtime.sh", names)

        mutated = dict(tools)
        mutated["platform_release_preflight.sh"] += (
            '\n"$SCRIPT_DIR/platform_unlisted_local.py"\n'
        )
        mutated_discovered = set().union(*build_graph(mutated).values())
        self.assertIn("platform_unlisted_local.py", mutated_discovered - names)
        self.assertNotEqual(mutated_discovered - names - boundary_allowlist, set())

    def test_deploy_and_coordinator_use_shared_host_generation_verifier(self) -> None:
        deploy = (REPO_ROOT / ".github/workflows/platform-production-deploy.yml").read_text(encoding="utf-8")
        coordinator = (REPO_ROOT / ".github/workflows/platform-production-autodeploy.yml").read_text(encoding="utf-8")
        helper = (TOOLS_ROOT / "platform_host_tools_installed_check.py").read_text(encoding="utf-8")
        preflight = deploy.split("  host-capability-preflight:", 1)[1].split("  build-release:", 1)[0]
        self.assertIn("platform_host_tools_installed_check.py", preflight)
        self.assertIn("--contract-dir", preflight)
        self.assertIn("platform_host_tools_installed_check.py", coordinator)
        self.assertIn("subprocess.Popen", helper)
        self.assertIn("selectors.DefaultSelector", helper)
        self.assertIn("if len(output) > max_output", helper)
        steps = _workflow_step_blocks(preflight)
        cleanup = [step for step in steps if "- name: Remove host capability verifier material" in step]
        self.assertEqual(len(cleanup), 1)
        self.assertIn("if: ${{ always() }}", cleanup[0])


    def test_shared_host_generation_verifier_has_nul_safe_closed_inventory(self) -> None:
        helper = (TOOLS_ROOT / "platform_host_tools_installed_check.py").read_text(encoding="utf-8")
        self.assertRegex(
            helper,
            r'_remote\(\s*remote,\s*"/usr/bin/find",\s*"--",\s*generation,\s*"-mindepth",\s*"1",\s*"-maxdepth",\s*"1",\s*"-print0",\s*stage="inventory",\s*max_output=MAX_REMOTE_OUTPUT,\s*\)',
        )
        self.assertIn('inventory.split(b"\\x00")', helper)
        self.assertIn("len(names) != len(expected)", helper)
        self.assertIn("set(relatives) != expected", helper)


    def test_remote_inventory_shell_fixture_carries_dotfiles_and_extras_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            generation = Path(temporary) / "generation"
            generation.mkdir()
            (generation / "manifest.json").write_text("{}\n", encoding="ascii")
            (generation / "capabilities.txt").write_text("\n", encoding="ascii")
            (generation / ".unexpected").write_text("extra\n", encoding="ascii")
            remote_command = (
                "/usr/bin/find -- "
                + shlex.quote(str(generation))
                + " -mindepth 1 -maxdepth 1 -print0"
            )
            completed = subprocess.run(
                ["/bin/sh", "-c", remote_command],
                check=True,
                capture_output=True,
            )
            remote_paths = completed.stdout.rstrip(b"\0").split(b"\0")
            names = [
                path.decode("utf-8").removeprefix(f"{generation}/")
                for path in remote_paths
            ]
            self.assertIn(".unexpected", names)
            expected = {"manifest.json", "capabilities.txt"}
            self.assertNotEqual(set(names), expected)
            self.assertEqual(len(names), 3)

    def test_host_verifier_reuses_pinned_dispatcher_with_bounded_output(self) -> None:
        helper = (TOOLS_ROOT / "platform_host_tools_installed_check.py").read_text(encoding="utf-8")
        self.assertRegex(
            helper,
            r'_remote\(\s*remote,\s*"/usr/bin/python3\.12",\s*"-I",\s*"-B",\s*dispatcher,\s*"host-capabilities",\s*stage="capability_self_test",\s*timeout=20,\s*max_output=512,\s*\)',
        )
        self.assertIn("max_output=512", helper)
        self.assertIn("MAX_REMOTE_OUTPUT = 16384", helper)
        self.assertIn("HOST_KEY_FINGERPRINT =", helper)


    def test_host_capability_probe_checks_closed_generation_metadata(self) -> None:
        pin_payload = json.loads(
            (REPO_ROOT / pin.PIN_RELATIVE_PATH).read_text(encoding="utf-8")
        )
        expected_paths = {
            f"platform/tools/{name}" for name in bundle.HOST_TOOL_FILES
        }
        checked_in_pin_records = pin_payload["closure"]
        self.assertEqual(
            {record["path"] for record in checked_in_pin_records}, expected_paths
        )
        self.assertEqual(len(checked_in_pin_records), len(expected_paths))
        self.assertEqual(
            {record["mode"] for record in checked_in_pin_records}, {0o644, 0o755}
        )
        source_modes = {
            record["path"].removeprefix("platform/tools/"): record["mode"]
            for record in checked_in_pin_records
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source_fixture(root)
            for name, mode in source_modes.items():
                os.chmod(source / "platform" / "tools" / name, mode)
            built = bundle.build_bundle(source, SOURCE_SHA, root / "generation.zip")
        generation_manifest = built["manifest"]
        self.assertEqual(
            {record["path"] for record in generation_manifest["files"]},
            set(bundle.HOST_TOOL_FILES) | {"capabilities.txt"},
        )
        self.assertEqual(
            {
                record["mode"]
                for record in generation_manifest["files"]
                if record["path"] != "capabilities.txt"
            },
            {0o555},
        )
        self.assertEqual(
            next(
                record["mode"]
                for record in generation_manifest["files"]
                if record["path"] == "capabilities.txt"
            ),
            0o444,
        )
        pin_records = [
            {
                "path": f"platform/tools/{record['path']}",
                "sha256": record["sha256"],
                "mode": source_modes[record["path"]],
            }
            for record in generation_manifest["files"]
            if record["path"] != "capabilities.txt"
        ]
        test_pin = {**pin_payload, "closure": list(reversed(pin_records))}
        self.assertTrue(
            dispatcher._pin_closure_matches_generation(test_pin, generation_manifest)
        )

        def rejects(
            *,
            source_records: list[dict[str, object]] | None = None,
            installed: list[dict[str, object]] | None = None,
        ) -> None:
            candidate_pin = {
                **test_pin,
                "closure": (
                    source_records if source_records is not None else test_pin["closure"]
                ),
            }
            candidate_manifest = {
                "files": (
                    installed if installed is not None else generation_manifest["files"]
                )
            }
            self.assertFalse(
                dispatcher._pin_closure_matches_generation(
                    candidate_pin, candidate_manifest
                )
            )

        changed_digest = [dict(record) for record in test_pin["closure"]]
        changed_digest[0]["sha256"] = "b" * 64
        rejects(source_records=changed_digest)
        rejects(source_records=[dict(record) for record in test_pin["closure"][:-1]])
        duplicate_source = [dict(record) for record in test_pin["closure"]]
        duplicate_source[-1] = dict(duplicate_source[0])
        rejects(source_records=duplicate_source)
        unexpected_source = [dict(record) for record in test_pin["closure"]]
        unexpected_source[0]["path"] = "platform/tools/unapproved.py"
        rejects(source_records=unexpected_source)
        bad_source_mode = [dict(record) for record in test_pin["closure"]]
        bad_source_mode[0]["mode"] = 0o666
        rejects(source_records=bad_source_mode)

        changed_installed_digest = [dict(record) for record in generation_manifest["files"]]
        tool_record_index = next(
            index
            for index, record in enumerate(changed_installed_digest)
            if record["path"] != "capabilities.txt"
        )
        changed_installed_digest[tool_record_index]["sha256"] = "c" * 64
        rejects(installed=changed_installed_digest)
        missing_installed = [dict(record) for record in generation_manifest["files"][:-1]]
        rejects(installed=missing_installed)
        duplicate_installed = [dict(record) for record in generation_manifest["files"]]
        duplicate_installed[-1] = dict(duplicate_installed[0])
        rejects(installed=duplicate_installed)
        unexpected_installed = [dict(record) for record in generation_manifest["files"]]
        unexpected_installed[-1]["path"] = "unexpected.txt"
        rejects(installed=unexpected_installed)
        bad_installed_mode = [dict(record) for record in generation_manifest["files"]]
        bad_installed_mode[0]["mode"] = 0o755
        rejects(installed=bad_installed_mode)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host_root = root / "shared" / "host-tools"
            generation = host_root / SOURCE_SHA
            generation.mkdir(parents=True)
            os.chmod(host_root, 0o755)
            for name in bundle.HOST_TOOL_FILES:
                member = generation / name
                member.write_text("#!/usr/bin/python3\n", encoding="ascii")
                os.chmod(member, 0o555)
            for name in ("manifest.json", "capabilities.txt"):
                member = generation / name
                member.write_text("placeholder\n", encoding="ascii")
                os.chmod(member, 0o444)
            os.chmod(generation, 0o555)

            real_lstat = Path.lstat
            owner_overrides: dict[Path, int] = {}

            def root_owned_lstat(path: Path) -> SimpleNamespace | os.stat_result:
                metadata = real_lstat(path)
                if path == generation or path.parent == generation:
                    return SimpleNamespace(
                        st_mode=metadata.st_mode,
                        st_uid=owner_overrides.get(path, 0),
                        st_gid=0,
                        st_nlink=metadata.st_nlink,
                    )
                return metadata

            output = StringIO()
            with patch.object(Path, "lstat", autospec=True, side_effect=root_owned_lstat), \
                patch.object(dispatcher, "ACTIVE_TOOLS_DIR", generation), \
                patch.object(dispatcher, "HOST_TOOLS_ROOT", host_root), \
                patch.object(dispatcher, "__file__", str(generation / bundle.HOST_TOOL_FILES[0])), \
                patch.object(dispatcher, "_retained_load_export_owner", return_value={"uid": 65534, "gid": 65534}), \
                redirect_stdout(output):
                self.assertEqual(dispatcher._host_capabilities(), 0)
            self.assertRegex(output.getvalue(), r"^HOST_TOOLS schema=1 source_sha=[0-9a-f]{40} ")
            self.assertNotIn(str(generation), output.getvalue())
            owner_overrides[generation / bundle.HOST_TOOL_FILES[0]] = 1000
            with patch.object(Path, "lstat", autospec=True, side_effect=root_owned_lstat), \
                patch.object(dispatcher, "ACTIVE_TOOLS_DIR", generation), \
                patch.object(dispatcher, "HOST_TOOLS_ROOT", host_root), \
                patch.object(dispatcher, "__file__", str(generation / bundle.HOST_TOOL_FILES[0])):
                self.assertEqual(dispatcher._host_capabilities(), 2)
            owner_overrides.clear()
            os.chmod(generation / bundle.HOST_TOOL_FILES[-1], 0o554)
            with patch.object(Path, "lstat", autospec=True, side_effect=root_owned_lstat), \
                patch.object(dispatcher, "ACTIVE_TOOLS_DIR", generation), \
                patch.object(dispatcher, "HOST_TOOLS_ROOT", host_root), \
                patch.object(dispatcher, "__file__", str(generation / bundle.HOST_TOOL_FILES[0])):
                self.assertEqual(dispatcher._host_capabilities(), 2)

    def _candidate_event_fixture(self) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        base_sha = "1" * 40
        candidate_sha = "2" * 40
        merge_sha = "3" * 40
        repository_snapshot = {"name": "old_sparky"}
        pull_snapshot = {
            "number": 115,
            "head": {"ref": "codex/host-tools-bump", "sha": candidate_sha, "repo": repository_snapshot},
            "base": {"ref": "dev", "sha": base_sha, "repo": repository_snapshot},
        }
        run = {
            "id": 9001,
            "run_attempt": 2,
            "workflow_id": candidate.SECURITY_WORKFLOW_ID,
            "name": candidate.SECURITY_WORKFLOW_NAME,
            "path": candidate.SECURITY_WORKFLOW_PATH,
            "event": "pull_request",
            "status": "completed",
            "conclusion": "success",
            # GitHub's workflow_run and exact-attempt job/artifact identities
            # carry the source head E. The current PR merge ref/commit below
            # establishes the separately tested synthetic merge M.
            "head_sha": candidate_sha,
            "head_branch": "codex/host-tools-bump",
            "repository": {"full_name": candidate.REPOSITORY},
            "head_repository": {"full_name": candidate.REPOSITORY},
            "pull_requests": [pull_snapshot],
        }
        event = {"workflow_run": {**run, "pull_requests": [pull_snapshot]}}
        repository = {
            "full_name": candidate.REPOSITORY,
            "owner": {"login": "StrayForest"},
        }
        pr = {
            "number": 115,
            "state": "open",
            "draft": False,
            "base": {"ref": "dev", "sha": base_sha, "repo": repository},
            "head": {
                "ref": "codex/host-tools-bump",
                "sha": candidate_sha,
                "label": "StrayForest:codex/host-tools-bump",
                "repo": repository,
            },
            "merge_commit_sha": merge_sha,
        }
        return event, run, pr

    def _candidate_context_files(
        self,
        root: Path,
        event: dict[str, object],
        run: dict[str, object],
        pr: dict[str, object],
    ) -> dict[str, Path]:
        merge_sha = str(pr["merge_commit_sha"])
        base_sha = str(pr["base"]["sha"])
        head_sha = str(pr["head"]["sha"])
        files = {
            "event": root / "event.json",
            "run": root / "run.json",
            "latest": root / "latest.json",
            "pr": root / "pr.json",
            "merge_ref": root / "merge-ref.json",
            "commit": root / "commit.json",
            "context": root / "context.json",
            "output": root / "output",
        }
        files["event"].write_text(json.dumps(event), encoding="utf-8")
        files["run"].write_text(json.dumps(run), encoding="utf-8")
        files["latest"].write_text(json.dumps(run), encoding="utf-8")
        files["pr"].write_text(json.dumps(pr), encoding="utf-8")
        files["merge_ref"].write_text(
            json.dumps(
                [
                    {
                        "ref": "refs/pull/115/merge",
                        "object": {"type": "commit", "sha": merge_sha},
                    }
                ]
            ),
            encoding="utf-8",
        )
        files["commit"].write_text(
            json.dumps(
                {
                    "sha": merge_sha,
                    "commit": {"tree": {"sha": "4" * 40}},
                    "parents": [{"sha": base_sha}, {"sha": head_sha}],
                }
            ),
            encoding="utf-8",
        )
        return files

    def test_candidate_event_context_binds_canonical_run_pr_and_base(self) -> None:
        event, run, pr = self._candidate_event_fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._candidate_context_files(root, event, run, pr)
            context = candidate.validate_context(
                paths["event"],
                paths["run"],
                paths["pr"],
                paths["merge_ref"],
                paths["commit"],
                output=paths["context"],
                latest_run_path=paths["latest"],
            )
            self.assertEqual(context.run_id, "9001")
            self.assertEqual(context.run_attempt, "2")
            self.assertEqual(context.original_source_head_sha, "2" * 40)
            self.assertEqual(context.original_base_sha, "1" * 40)
            self.assertEqual(context.tested_merge_sha, "3" * 40)
            self.assertEqual(context.tested_parents, ("1" * 40, "2" * 40))
            payload = json.loads(paths["context"].read_text())
            self.assertEqual(payload["pull_request"]["number"], 115)
            self.assertEqual(payload["security_run"]["head_sha"], "2" * 40)
            self.assertEqual(payload["tested_merge"]["sha"], "3" * 40)

    def test_candidate_context_rejects_empty_multiple_and_wrong_pr_associations(self) -> None:
        event, run, pr = self._candidate_event_fixture()
        payload_mutations = (
            ("event-empty", [], run["pull_requests"]),
            ("event-multiple", [{"number": 115}, {"number": 115}], run["pull_requests"]),
            ("run-empty", event["workflow_run"]["pull_requests"], []),
            ("run-multiple", event["workflow_run"]["pull_requests"], [{"number": 115}, {"number": 115}]),
            ("event-wrong-association", [{"number": 116}], run["pull_requests"]),
            ("run-wrong-association", event["workflow_run"]["pull_requests"], [{"number": 116}]),
        )
        for label, event_rows, run_rows in payload_mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                mutated_event = {"workflow_run": {**event["workflow_run"], "pull_requests": event_rows}}
                mutated_run = {**run, "pull_requests": run_rows}
                paths = self._candidate_context_files(root, mutated_event, mutated_run, pr)
                with self.assertRaises(candidate.CandidateError):
                    candidate.validate_context(paths["event"], paths["run"], paths["pr"], paths["merge_ref"], paths["commit"], latest_run_path=paths["latest"], output=root / "context.json")

    def test_candidate_context_rejects_stale_fork_ref_tag_merge_and_pr_state(self) -> None:
        event, run, pr = self._candidate_event_fixture()
        mutations = (
            ("wrong-workflow", lambda value: {**value, "workflow_id": candidate.SECURITY_WORKFLOW_ID + 1}),
            ("wrong-conclusion", lambda value: {**value, "conclusion": "failure"}),
            ("wrong-event", lambda value: {**value, "event": "push"}),
            ("wrong-source-head", lambda value: {**value, "head_sha": "4" * 40}),
            ("merge-as-source-head", lambda value: {**value, "head_sha": "3" * 40}),
            ("fork-run", lambda value: {**value, "head_repository": {"full_name": "attacker/old_sparky"}}),
            ("missing-head-repository", lambda value: {key: item for key, item in value.items() if key != "head_repository"}),
        )
        for label, mutation in mutations:
            with self.subTest(label=label):
                mutated_run = mutation(run)
                mutated_event = {"workflow_run": {**mutated_run}}
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    paths = self._candidate_context_files(root, mutated_event, mutated_run, pr)
                    with self.assertRaises(candidate.CandidateError):
                        candidate.validate_context(
                            paths["event"],
                            paths["run"],
                            paths["pr"],
                            paths["merge_ref"],
                            paths["commit"],
                            latest_run_path=paths["latest"],
                            output=root / "context.json",
                        )

        for label, mutation in (
            ("fork-pr", lambda value: {**value, "head": {**value["head"], "repo": {"full_name": "attacker/old_sparky", "owner": {"login": "attacker"}}}}),
            ("tag-ref", lambda value: {**value, "head": {**value["head"], "ref": "refs/tags/v1"}}),
            ("synthetic-merge", lambda value: {**value, "merge_commit_sha": value["head"]["sha"]}),
            ("missing-merge", lambda value: {**value, "merge_commit_sha": None}),
            ("draft", lambda value: {**value, "draft": True}),
            ("closed", lambda value: {**value, "state": "closed"}),
            ("wrong-base", lambda value: {**value, "base": {**value["base"], "ref": "main"}}),
            ("wrong-pr-number", lambda value: {**value, "number": 116}),
            ("stale-direct-head", lambda value: {**value, "head": {**value["head"], "sha": "3" * 40}}),
            ("stale-direct-ref", lambda value: {**value, "head": {**value["head"], "ref": "old-host-tools"}}),
        ):
            with self.subTest(label=label):
                mutated_pr = mutation(pr)
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    paths = self._candidate_context_files(root, event, run, mutated_pr)
                    with self.assertRaises(candidate.CandidateError):
                        candidate.validate_context(
                            paths["event"],
                            paths["run"],
                            paths["pr"],
                            paths["merge_ref"],
                            paths["commit"],
                            latest_run_path=paths["latest"],
                            output=root / "context.json",
                        )

    def test_candidate_context_rejects_merge_ref_parent_tree_and_source_substitution(self) -> None:
        """The source head and tested synthetic merge are distinct authorities."""

        event, run, pr = self._candidate_event_fixture()
        for label, mutate in (
            (
                "head-substitution",
                lambda files, payload: payload["workflow_run"]["pull_requests"][0]["head"].update(
                    {"sha": "5" * 40}
                ),
            ),
            (
                "base-substitution",
                lambda files, payload: payload["workflow_run"]["pull_requests"][0]["base"].update(
                    {"sha": "6" * 40}
                ),
            ),
            (
                "merge-ref-mismatch",
                lambda files, payload: payload[0]["object"].update({"sha": "7" * 40}),
            ),
            ("empty-merge-ref", lambda files, payload: payload.clear()),
            (
                "multiple-merge-refs",
                lambda files, payload: payload.append(payload[0]),
            ),
            (
                "commit-sha-mismatch",
                lambda files, payload: payload.update({"sha": "8" * 40}),
            ),
            (
                "wrong-parent-order",
                lambda files, payload: payload["parents"].reverse(),
            ),
            (
                "wrong-parent-count",
                lambda files, payload: payload["parents"].append({"sha": "8" * 40}),
            ),
            (
                "invalid-tree",
                lambda files, payload: payload["commit"]["tree"].update({"sha": "not-a-sha"}),
            ),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                paths = self._candidate_context_files(root, event, run, pr)
                target = (
                    paths["event"]
                    if label in {"head-substitution", "base-substitution"}
                    else paths["merge_ref"]
                    if label in {"merge-ref-mismatch", "empty-merge-ref", "multiple-merge-refs"}
                    else paths["commit"]
                )
                payload = json.loads(target.read_text(encoding="utf-8"))
                mutate(paths, payload)
                target.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(candidate.CandidateError):
                    candidate.validate_context(
                        paths["event"],
                        paths["run"],
                        paths["pr"],
                        paths["merge_ref"],
                        paths["commit"],
                        latest_run_path=paths["latest"],
                        output=paths["output"],
                    )

    def test_candidate_context_recheck_rejects_pr_merge_tree_and_rerun_races(self) -> None:
        """A second API snapshot must remain byte-for-byte the same context."""

        event, run, pr = self._candidate_event_fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._candidate_context_files(root, event, run, pr)
            candidate.validate_context(
                paths["event"],
                paths["run"],
                paths["pr"],
                paths["merge_ref"],
                paths["commit"],
                latest_run_path=paths["latest"],
                output=paths["context"],
            )
            partial_latest = {**run, "pull_requests": [{"number": 115}]}
            paths["latest"].write_text(json.dumps(partial_latest), encoding="utf-8")
            candidate.validate_context(
                paths["event"],
                paths["run"],
                paths["pr"],
                paths["merge_ref"],
                paths["commit"],
                latest_run_path=paths["latest"],
                expected_context_path=paths["context"],
                output=paths["output"],
            )
            for label, path_key, mutate in (
                (
                    "rerun-attempt",
                    "latest",
                    lambda payload: payload.update({"run_attempt": 3}),
                ),
                (
                    "base-race",
                    "pr",
                    lambda payload: payload["base"].update({"sha": "5" * 40}),
                ),
                (
                    "head-race",
                    "pr",
                    lambda payload: payload["head"].update({"sha": "6" * 40}),
                ),
                (
                    "merge-race",
                    "pr",
                    lambda payload: payload.update({"merge_commit_sha": "7" * 40}),
                ),
                (
                    "merge-ref-race",
                    "merge_ref",
                    lambda payload: payload[0]["object"].update({"sha": "8" * 40}),
                ),
                (
                    "tree-race",
                    "commit",
                    lambda payload: payload["commit"]["tree"].update({"sha": "9" * 40}),
                ),
            ):
                with self.subTest(label=label):
                    # Restore every input to the original snapshot before
                    # introducing one race at a time.
                    fresh = self._candidate_context_files(root, event, run, pr)
                    payload = json.loads(fresh[path_key].read_text(encoding="utf-8"))
                    mutate(payload)
                    fresh[path_key].write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(candidate.CandidateError):
                        candidate.validate_context(
                            fresh["event"],
                            fresh["run"],
                            fresh["pr"],
                            fresh["merge_ref"],
                            fresh["commit"],
                            latest_run_path=fresh["latest"],
                            expected_context_path=paths["context"],
                            output=fresh["output"],
                        )

    def test_candidate_jobs_and_final_summary_are_closed_and_successful(self) -> None:
        context = candidate.RunContext(
            repository=candidate.REPOSITORY,
            workflow_id=candidate.SECURITY_WORKFLOW_ID,
            workflow_name=candidate.SECURITY_WORKFLOW_NAME,
            workflow_path=candidate.SECURITY_WORKFLOW_PATH,
            run_id="9001",
            run_attempt="2",
            pull_request="115",
            original_source_head_sha="2" * 40,
            original_base_sha="1" * 40,
            original_base_ref="dev",
            original_head_ref="codex/host-tools-bump",
            original_base_repository=candidate.REPOSITORY,
            original_head_repository=candidate.REPOSITORY,
            tested_merge_ref="refs/pull/115/merge",
            tested_merge_sha="3" * 40,
            tested_tree_sha="4" * 40,
            tested_parents=("1" * 40, "2" * 40),
        )
        summary = {
            "schema": 1,
            "tested_sha": context.tested_merge_sha,
            "event": "pull_request",
            "route_event": "pull_request",
            "class": "full",
            "reason": "trusted candidate-packaging change requires full verification and is non-deployable",
            "deployable": False,
            "fallback": False,
            "manifest_digest": "a" * 64,
            "expected_gates": list(candidate.FULL_GATE_IDS),
            "gate_results": {gate: "success" for gate in candidate.FULL_GATE_IDS},
            "conditional_gate_results": {"release-runtime": "skipped", "release-runtime-real": "skipped"},
            "runtime_sensitive": False,
            "requires_release_runtime": False,
            "requires_real_release_runtime": False,
            "missing_or_failed": [],
            "route_errors": [],
            "status_start_result": "skipped",
            "passed": True,
        }
        jobs = [
            {
                "id": index + 1,
                "name": name,
                "run_id": 9001,
                "run_attempt": 2,
                "head_sha": context.original_source_head_sha,
                "head_branch": context.original_head_ref,
                "workflow_name": candidate.SECURITY_WORKFLOW_NAME,
                "check_run_url": f"https://api.github.com/repos/StrayForest/old_sparky/check-runs/{index + 100}",
                "status": "completed",
                "conclusion": "skipped" if name in candidate.CONDITIONAL_JOB_NAMES else "success",
            }
            for index, name in enumerate(sorted(candidate.EXPECTED_JOB_NAMES))
        ]
        self.assertEqual(
            candidate.PR_ALWAYS_SKIPPED_JOB_NAMES,
            {
                "Authenticate internal baseline runtime proof",
                "Dispatch exact baseline proof finalizer",
            },
        )
        self.assertTrue(candidate.PR_ALWAYS_SKIPPED_JOB_NAMES.issubset(candidate.EXPECTED_JOB_NAMES))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "jobs.json"
            path.write_text(json.dumps({"total_count": len(jobs), "jobs": jobs}), encoding="utf-8")
            candidate.verify_jobs(path, context, summary)
            for name in sorted(candidate.PR_ALWAYS_SKIPPED_JOB_NAMES):
                changed_jobs = [
                    {**job, "conclusion": "success"} if job["name"] == name else job
                    for job in jobs
                ]
                path.write_text(json.dumps({"total_count": len(changed_jobs), "jobs": changed_jobs}), encoding="utf-8")
                with self.subTest(unexpected_pr_job=name):
                    with self.assertRaises(candidate.CandidateError):
                        candidate.verify_jobs(path, context, summary)
            for label, changed in (
                ("extra-job", {**jobs[0], "name": "attacker-job"}),
                ("failed-required", {**jobs[0], "conclusion": "failure"}),
            ):
                bad_jobs = list(jobs)
                bad_jobs[0] = changed
                path.write_text(json.dumps({"total_count": len(bad_jobs), "jobs": bad_jobs}), encoding="utf-8")
                with self.subTest(label=label):
                    with self.assertRaises(candidate.CandidateError):
                        candidate.verify_jobs(path, context, summary)
            for label, field, value in (
                ("deployable", "deployable", True),
                ("wrong-class", "class", "docs-only"),
                ("missing-failure", "missing_or_failed", ["security"]),
            ):
                bad_summary = {**summary, field: value}
                with self.subTest(label=label):
                    with self.assertRaises(candidate.CandidateError):
                        candidate._validate_summary(bad_summary, context)

            # The exact-attempt jobs endpoint is the authority for each row's
            # identity.  A successful row from another run/attempt, source,
            # branch, or workflow is not interchangeable with this run.
            for label, field, value in (
                ("cross-run", "run_id", 9002),
                ("cross-attempt", "run_attempt", 3),
                ("source-head", "head_sha", "5" * 40),
                ("merge-as-head", "head_sha", context.tested_merge_sha),
                ("source-branch", "head_branch", "other-branch"),
                ("workflow", "workflow_name", "Other workflow"),
                ("check-run-url", "check_run_url", "https://evil.example/check-runs/1"),
            ):
                bad_jobs = list(jobs)
                bad_jobs[0] = {**bad_jobs[0], field: value}
                path.write_text(json.dumps({"total_count": len(bad_jobs), "jobs": bad_jobs}), encoding="utf-8")
                with self.subTest(job_identity=label):
                    with self.assertRaises(candidate.CandidateError):
                        candidate.verify_jobs(path, context, summary)

            split_summary = {
                **summary,
                "source_head_sha": context.original_source_head_sha,
                "base_sha": context.original_base_sha,
                "tested_tree_sha": context.tested_tree_sha,
                "tested_parents": list(context.tested_parents),
            }
            candidate._validate_summary(split_summary, context)
            alias_split_summary = {
                **summary,
                "source_sha": context.original_source_head_sha,
                "base_sha": context.original_base_sha,
                "tree_sha": context.tested_tree_sha,
                "parents": list(context.tested_parents),
            }
            candidate._validate_summary(alias_split_summary, context)
            run_proof_summary = {
                **summary,
                "proof_mode": "standard",
                "proof_run_id": context.run_id,
                "proof_run_attempt": context.run_attempt,
                "baseline_guard_result": "skipped",
            }
            candidate._validate_summary(run_proof_summary, context)
            # The trusted same-repository/dev PR route may run the immutable
            # builder for runtime-sensitive or conservative-fallback changes.
            # Its success must be bound by both the closed summary and the
            # exact workflow-job row; legacy non-sensitive PRs retain the
            # skipped shape above.
            real_runtime_summary = {
                **run_proof_summary,
                "fallback": False,
                "runtime_sensitive": True,
                "requires_release_runtime": True,
                "requires_real_release_runtime": True,
                "conditional_gate_results": {
                    "release-runtime": "success",
                    "release-runtime-real": "success",
                },
            }
            candidate._validate_summary(real_runtime_summary, context)
            real_runtime_jobs = [
                {
                    **job,
                    "conclusion": (
                        "success"
                        if job["name"] in {
                            "Conditional release runtime fixture",
                            "Trusted dev immutable release runtime",
                        }
                        else job["conclusion"]
                    ),
                }
                for job in jobs
            ]
            path.write_text(
                json.dumps({"total_count": len(real_runtime_jobs), "jobs": real_runtime_jobs}),
                encoding="utf-8",
            )
            candidate.verify_jobs(path, context, real_runtime_summary)
            for label, changed_summary, changed_jobs, summary_is_valid in (
                (
                    "runtime-required-but-skipped",
                    {**real_runtime_summary, "requires_real_release_runtime": False},
                    real_runtime_jobs,
                    False,
                ),
                (
                    "runtime-job-summary-mismatch",
                    real_runtime_summary,
                    [
                        {**job, "conclusion": "skipped"}
                        if job["name"] == "Trusted dev immutable release runtime"
                        else job
                        for job in real_runtime_jobs
                    ],
                    True,
                ),
            ):
                with self.subTest(real_runtime=label):
                    if summary_is_valid:
                        candidate._validate_summary(changed_summary, context)
                    else:
                        with self.assertRaises(candidate.CandidateError):
                            candidate._validate_summary(changed_summary, context)
                    path.write_text(
                        json.dumps({"total_count": len(changed_jobs), "jobs": changed_jobs}),
                        encoding="utf-8",
                    )
                    with self.assertRaises(candidate.CandidateError):
                        candidate.verify_jobs(path, context, changed_summary)
            for label, changed in (
                ("arbitrary-tested-sha", {**summary, "tested_sha": "9" * 40}),
                ("source-as-tested-sha", {**summary, "tested_sha": context.original_source_head_sha}),
                ("malicious-extra", {**summary, "untrusted": "accepted"}),
                ("schema", {**summary, "schema": 2}),
                (
                    "split-source-mismatch",
                    {**split_summary, "source_head_sha": "5" * 40},
                ),
                (
                    "split-parent-order",
                    {**split_summary, "tested_parents": list(reversed(context.tested_parents))},
                ),
                (
                    "split-extra-key",
                    {**split_summary, "unexpected": True},
                ),
                (
                    "proof-run-id",
                    {**run_proof_summary, "proof_run_id": "9002"},
                ),
                (
                    "proof-run-id-type",
                    {**run_proof_summary, "proof_run_id": 9001},
                ),
                (
                    "proof-run-attempt",
                    {**run_proof_summary, "proof_run_attempt": "3"},
                ),
                (
                    "proof-run-attempt-bool",
                    {**run_proof_summary, "proof_run_attempt": True},
                ),
                (
                    "proof-mode",
                    {**run_proof_summary, "proof_mode": "baseline-reconcile"},
                ),
                (
                    "proof-mode-type",
                    {**run_proof_summary, "proof_mode": []},
                ),
                (
                    "proof-baseline-guard",
                    {**run_proof_summary, "baseline_guard_result": "success"},
                ),
                (
                    "proof-baseline-guard-type",
                    {**run_proof_summary, "baseline_guard_result": None},
                ),
                (
                    "proof-extra-key",
                    {**run_proof_summary, "untrusted": True},
                ),
            ):
                with self.subTest(summary_identity=label):
                    with self.assertRaises(candidate.CandidateError):
                        candidate._validate_summary(changed, context)

            def artifact_pair(root: Path, name: str, payload: dict[str, object], member: str, artifact_id: int) -> tuple[Path, Path]:
                fixture_name = f"{len(list(root.iterdir()))}-{name}"
                archive = root / f"{fixture_name}.zip"
                with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as opened:
                    opened.writestr(member, json.dumps(payload, sort_keys=True, separators=(",", ":")))
                digest = hashlib.sha256(archive.read_bytes()).hexdigest()
                metadata = root / f"{fixture_name}.json"
                metadata.write_text(
                    json.dumps(
                        {
                            "id": artifact_id,
                            "name": name,
                            "expired": False,
                            "size_in_bytes": archive.stat().st_size,
                            "digest": f"sha256:{digest}",
                            "workflow_run": {
                                "id": int(context.run_id),
                                "run_attempt": int(context.run_attempt),
                                "head_sha": context.original_source_head_sha,
                                "head_branch": context.original_head_ref,
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                return metadata, archive

            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                context_path = root / "context.json"
                context_path.write_text(json.dumps(context.as_payload()), encoding="utf-8")
                jobs_path = root / "jobs.json"
                output = root / "github-output"
                trusted_tools = root / "trusted-dev/platform/tools"
                trusted_tools.mkdir(parents=True)
                for filename in (
                    "platform_host_tools_candidate.py",
                    "platform_host_tools_bundle.py",
                    "platform_ci_classifier.py",
                    "platform_workflow_provenance.py",
                ):
                    shutil.copyfile(TOOLS_ROOT / filename, trusted_tools / filename)

                for route_class, changed_files in (
                    ("docs-only", ["platform/docs/example.md"]),
                    ("out-of-scope", [".gitignore"]),
                ):
                    manifest = candidate.classifier.classify(
                        changed_files,
                        event="pull_request",
                        target_sha=context.tested_merge_sha,
                    )
                    self.assertEqual(manifest["class"], route_class)
                    no_op_summary = {
                        **summary,
                        "class": route_class,
                        "reason": manifest["reason"],
                        "fallback": False,
                        "runtime_sensitive": False,
                        "manifest_digest": manifest["digest"],
                        "expected_gates": manifest["expected_gates"],
                        "gate_results": {
                            gate: ("success" if gate in manifest["expected_gates"] else "skipped")
                            for gate in candidate.FULL_GATE_IDS
                        },
                    }
                    successful_jobs = {
                        "CI route classifier",
                        "Exact-source host-tools pin preflight",
                        "status-final",
                    }
                    for gate in manifest["expected_gates"]:
                        successful_jobs.update(candidate.GATE_JOB_NAMES[gate])
                    no_op_jobs = [
                        {
                            **job,
                            "conclusion": "success" if job["name"] in successful_jobs else "skipped",
                        }
                        for job in jobs
                    ]
                    jobs_path.write_text(
                        json.dumps({"total_count": len(no_op_jobs), "jobs": no_op_jobs}),
                        encoding="utf-8",
                    )
                    summary_metadata, summary_archive = artifact_pair(
                        root,
                        f"platform-ci-summary-{context.run_id}-{context.run_attempt}",
                        no_op_summary,
                        "platform-security-summary.json",
                        123,
                    )
                    route_metadata, route_archive = artifact_pair(
                        root,
                        f"platform-ci-route-{context.run_id}-{context.run_attempt}",
                        manifest,
                        candidate.ROUTE_MANIFEST_MEMBER,
                        124,
                    )
                    self.assertEqual(
                        candidate.classify_security_disposition(
                            context_path,
                            jobs_path,
                            summary_metadata,
                            summary_archive,
                            route_metadata,
                            route_archive,
                            expected_summary_artifact_id="123",
                            expected_route_artifact_id="124",
                            github_output=output,
                        ),
                        "not_applicable",
                    )
                    self.assertIn("candidate=false\n", output.read_text(encoding="ascii"))
                    if route_class == "docs-only":
                        cli_output = root / "cli-github-output"
                        completed = subprocess.run(
                            [
                                "/usr/bin/python3", "-I", "-B",
                                str(trusted_tools / "platform_host_tools_candidate.py"),
                                "route-disposition",
                                "--context", str(context_path),
                                "--jobs", str(jobs_path),
                                "--summary-metadata", str(summary_metadata),
                                "--summary-archive", str(summary_archive),
                                "--summary-artifact-id", "123",
                                "--route-metadata", str(route_metadata),
                                "--route-archive", str(route_archive),
                                "--route-artifact-id", "124",
                                "--github-output", str(cli_output),
                            ],
                            capture_output=True,
                            text=True,
                            check=False,
                            cwd=root,
                            env={},
                        )
                        self.assertEqual(completed.returncode, 0, completed.stderr)
                        self.assertIn("disposition=not_applicable", completed.stdout)
                        self.assertIn("candidate=false\n", cli_output.read_text(encoding="ascii"))

                    # Matching summary/route digests are insufficient if an
                    # artifact's exact workflow run or a required job changes.
                    wrong_target = {**no_op_summary, "tested_sha": context.original_source_head_sha}
                    bad_summary_metadata, bad_summary_archive = artifact_pair(
                        root,
                        f"platform-ci-summary-{context.run_id}-{context.run_attempt}",
                        wrong_target,
                        "platform-security-summary.json",
                        123,
                    )
                    with self.assertRaises(candidate.CandidateError):
                        candidate.classify_security_disposition(
                            context_path, jobs_path, bad_summary_metadata, bad_summary_archive,
                            route_metadata, route_archive,
                            expected_summary_artifact_id="123", expected_route_artifact_id="124",
                        )
                    wrong_runner_metadata = root / f"wrong-runner-{route_class}.json"
                    wrong_runner = json.loads(summary_metadata.read_text(encoding="utf-8"))
                    wrong_runner["workflow_run"]["head_sha"] = "5" * 40
                    wrong_runner_metadata.write_text(json.dumps(wrong_runner), encoding="utf-8")
                    with self.assertRaises(candidate.CandidateError):
                        candidate.classify_security_disposition(
                            context_path, jobs_path, wrong_runner_metadata, summary_archive,
                            route_metadata, route_archive,
                            expected_summary_artifact_id="123", expected_route_artifact_id="124",
                        )
                    if route_class == "docs-only":
                        forged_manifest = {**manifest, "files": [".gitignore"]}
                        forged_manifest["digest"] = candidate._route_manifest_digest(forged_manifest)
                        forged_summary = {
                            **no_op_summary,
                            "manifest_digest": forged_manifest["digest"],
                        }
                        forged_route_metadata, forged_route_archive = artifact_pair(
                            root,
                            f"platform-ci-route-{context.run_id}-{context.run_attempt}",
                            forged_manifest,
                            candidate.ROUTE_MANIFEST_MEMBER,
                            124,
                        )
                        forged_summary_metadata, forged_summary_archive = artifact_pair(
                            root,
                            f"platform-ci-summary-{context.run_id}-{context.run_attempt}",
                            forged_summary,
                            "platform-security-summary.json",
                            123,
                        )
                        with self.assertRaises(candidate.CandidateError):
                            candidate.classify_security_disposition(
                                context_path, jobs_path, forged_summary_metadata, forged_summary_archive,
                                forged_route_metadata, forged_route_archive,
                                expected_summary_artifact_id="123", expected_route_artifact_id="124",
                            )
                    changed_jobs = [
                        {**job, "run_attempt": 3} if job["name"] == "Documentation consistency" else job
                        for job in no_op_jobs
                    ]
                    jobs_path.write_text(
                        json.dumps({"total_count": len(changed_jobs), "jobs": changed_jobs}),
                        encoding="utf-8",
                    )
                    with self.assertRaises(candidate.CandidateError):
                        candidate.classify_security_disposition(
                            context_path, jobs_path, summary_metadata, summary_archive,
                            route_metadata, route_archive,
                            expected_summary_artifact_id="123", expected_route_artifact_id="124",
                        )
                    jobs_path.write_text(
                        json.dumps({"total_count": len(no_op_jobs), "jobs": no_op_jobs}),
                        encoding="utf-8",
                    )
                    output.unlink(missing_ok=True)

                full_manifest = candidate.classifier.classify(
                    [".github/workflows/platform-host-tools-candidate.yml"],
                    event="pull_request",
                    target_sha=context.tested_merge_sha,
                )
                full_summary = {
                    **summary,
                    "reason": full_manifest["reason"],
                    "manifest_digest": full_manifest["digest"],
                }
                jobs_path.write_text(
                    json.dumps({"total_count": len(jobs), "jobs": jobs}),
                    encoding="utf-8",
                )
                summary_metadata, summary_archive = artifact_pair(
                    root,
                    f"platform-ci-summary-{context.run_id}-{context.run_attempt}",
                    full_summary,
                    "platform-security-summary.json",
                    123,
                )
                route_metadata, route_archive = artifact_pair(
                    root,
                    f"platform-ci-route-{context.run_id}-{context.run_attempt}",
                    full_manifest,
                    candidate.ROUTE_MANIFEST_MEMBER,
                    124,
                )
                self.assertEqual(
                    candidate.classify_security_disposition(
                        context_path, jobs_path, summary_metadata, summary_archive,
                        route_metadata, route_archive,
                        expected_summary_artifact_id="123", expected_route_artifact_id="124",
                        github_output=output,
                    ),
                    "full",
                )
                self.assertIn("candidate=true\n", output.read_text(encoding="ascii"))
                # The full route still proceeds through the existing strict
                # success verifier; disposition does not bless job evidence.
                candidate.verify_security_run(
                    context_path, jobs_path, summary_metadata, summary_archive,
                    route_metadata, route_archive,
                    expected_summary_artifact_id="123", expected_route_artifact_id="124",
                    output=root / "security-evidence.json",
                )

    def test_candidate_artifacts_bind_route_target_digest_and_attempt(self) -> None:
        """Classifier output is data-only and must target the tested merge."""

        context = candidate.RunContext(
            repository=candidate.REPOSITORY,
            workflow_id=candidate.SECURITY_WORKFLOW_ID,
            workflow_name=candidate.SECURITY_WORKFLOW_NAME,
            workflow_path=candidate.SECURITY_WORKFLOW_PATH,
            run_id="9001",
            run_attempt="2",
            pull_request="115",
            original_source_head_sha="2" * 40,
            original_base_sha="1" * 40,
            original_base_ref="dev",
            original_head_ref="codex/host-tools-bump",
            original_base_repository=candidate.REPOSITORY,
            original_head_repository=candidate.REPOSITORY,
            tested_merge_ref="refs/pull/115/merge",
            tested_merge_sha="3" * 40,
            tested_tree_sha="4" * 40,
            tested_parents=("1" * 40, "2" * 40),
        )
        manifest_base = {
            "schema": 1,
            "version": 1,
            "target_sha": context.tested_merge_sha,
            "event": "pull_request",
            "class": "full",
            "expected_gates": list(candidate.FULL_GATE_IDS),
            "runtime_sensitive": False,
            "deployable": False,
            "fallback": False,
            "reason": "candidate requires full verification",
            "files": ["platform-ci-route.json"],
        }

        def write_route_fixture(
            root: Path,
            manifest: dict[str, object],
            *,
            attempt: int = 2,
            artifact_head_sha: str | None = None,
        ) -> tuple[Path, Path]:
            manifest = {**manifest}
            manifest["digest"] = candidate._route_manifest_digest(manifest)
            archive = root / f"route-{attempt}.zip"
            member = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as opened:
                opened.writestr(candidate.ROUTE_MANIFEST_MEMBER, member)
            archive_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            metadata = root / f"route-{attempt}.json"
            metadata.write_text(
                json.dumps(
                    {
                        "id": 123,
                        "name": "platform-ci-route-9001-2",
                        "expired": False,
                        "size_in_bytes": archive.stat().st_size,
                        "digest": f"sha256:{archive_digest}",
                        "workflow_run": {
                            "id": 9001,
                            "run_attempt": attempt,
                            "head_sha": artifact_head_sha or context.original_source_head_sha,
                            "head_branch": context.original_head_ref,
                        },
                    }
                ),
                encoding="utf-8",
            )
            return metadata, archive

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context_path = root / "context.json"
            context_path.write_text(json.dumps(context.as_payload()), encoding="utf-8")
            metadata, archive = write_route_fixture(root, manifest_base)
            listing = root / "route-list.json"
            listing.write_text(
                json.dumps(
                    {
                        "artifacts": [
                            {
                                "id": 123,
                                "name": "platform-ci-route-9001-2",
                                "expired": False,
                                "workflow_run": {"id": 9001, "run_attempt": 2},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                candidate.select_artifact_id(
                    listing,
                    name="platform-ci-route-9001-2",
                    run_id="9001",
                    run_attempt="2",
                ),
                "123",
            )
            listing_payload = json.loads(listing.read_text(encoding="utf-8"))
            listing_payload["artifacts"][0]["workflow_run"]["run_attempt"] = None
            listing.write_text(json.dumps(listing_payload), encoding="utf-8")
            self.assertEqual(
                candidate.select_artifact_id(
                    listing,
                    name="platform-ci-route-9001-2",
                    run_id="9001",
                    run_attempt="2",
                ),
                "123",
            )
            for invalid_attempt in (False, "2", 2.0, 3):
                listing_payload["artifacts"][0]["workflow_run"]["run_attempt"] = invalid_attempt
                listing.write_text(json.dumps(listing_payload), encoding="utf-8")
                with self.subTest(list_attempt=repr(invalid_attempt)), self.assertRaises(
                    candidate.CandidateError
                ):
                    candidate.select_artifact_id(
                        listing,
                        name="platform-ci-route-9001-2",
                        run_id="9001",
                        run_attempt="2",
                    )
            listing_payload["artifacts"][0]["workflow_run"]["run_attempt"] = 2
            listing.write_text(json.dumps(listing_payload), encoding="utf-8")
            result = candidate.verify_route_artifact(
                context_path,
                metadata,
                archive,
                expected_artifact_id="123",
                expected_manifest_digest=candidate._route_manifest_digest(manifest_base),
            )
            self.assertEqual(result["manifest"]["target_sha"], context.tested_merge_sha)
            bad_metadata, bad_archive = write_route_fixture(
                root,
                manifest_base,
                artifact_head_sha=context.tested_merge_sha,
            )
            with self.assertRaises(candidate.CandidateError):
                candidate.verify_route_artifact(
                    context_path,
                    bad_metadata,
                    bad_archive,
                    expected_artifact_id="123",
                    expected_manifest_digest=candidate._route_manifest_digest(manifest_base),
                )
            for label, manifest, expected_digest, attempt in (
                (
                    "source-head-target",
                    {**manifest_base, "target_sha": context.original_source_head_sha},
                    candidate._route_manifest_digest(
                        {**manifest_base, "target_sha": context.original_source_head_sha}
                    ),
                    2,
                ),
                (
                    "summary-digest-mismatch",
                    manifest_base,
                    "f" * 64,
                    2,
                ),
                (
                    "cross-attempt-artifact",
                    manifest_base,
                    candidate._route_manifest_digest(manifest_base),
                    3,
                ),
                (
                    "extra-manifest-key",
                    {**manifest_base, "unexpected": True},
                    candidate._route_manifest_digest({**manifest_base, "unexpected": True}),
                    2,
                ),
            ):
                with self.subTest(route_identity=label):
                    bad_metadata, bad_archive = write_route_fixture(root, manifest, attempt=attempt)
                    with self.assertRaises(candidate.CandidateError):
                        candidate.verify_route_artifact(
                            context_path,
                            bad_metadata,
                            bad_archive,
                            expected_artifact_id="123",
                            expected_manifest_digest=expected_digest,
                        )

    def test_candidate_artifact_metadata_binds_outer_digest_size_and_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "artifact.zip"
            archive.write_bytes(b"exact outer artifact")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            payload = {
                "id": 123,
                "name": "candidate",
                "expired": False,
                "digest": f"sha256:{digest}",
                "size_in_bytes": archive.stat().st_size,
                "workflow_run": {
                    "id": 456,
                    "run_attempt": 3,
                    "head_sha": "a" * 40,
                    "head_branch": "dev",
                },
            }
            self.assertEqual(
                candidate._verify_artifact_metadata(
                    payload,
                    archive,
                    expected_id="123",
                    expected_name="candidate",
                    expected_run_id="456",
                    expected_run_attempt="3",
                    expected_head_sha="a" * 40,
                    expected_head_ref="dev",
                ),
                f"sha256:{digest}",
            )
            null_attempt_payload = json.loads(json.dumps(payload))
            null_attempt_payload["workflow_run"]["run_attempt"] = None
            self.assertEqual(
                candidate._verify_artifact_metadata(
                    null_attempt_payload,
                    archive,
                    expected_id="123",
                    expected_name="candidate",
                    expected_run_id="456",
                    expected_run_attempt="3",
                    expected_head_sha="a" * 40,
                    expected_head_ref="dev",
                ),
                f"sha256:{digest}",
            )
            outer = root / "outer.zip"
            with zipfile.ZipFile(outer, "w", compression=zipfile.ZIP_DEFLATED) as opened:
                opened.writestr("platform-host-tools-bundle.zip", b"inner bundle")
            candidate._verify_closed_archive(
                outer,
                expected_member="platform-host-tools-bundle.zip",
                maximum_member_bytes=1024,
                expected_member_digest=hashlib.sha256(b"inner bundle").hexdigest(),
            )
            with zipfile.ZipFile(outer, "a", compression=zipfile.ZIP_DEFLATED) as opened:
                opened.writestr("unexpected", b"extra")
            with self.assertRaises(candidate.CandidateError):
                candidate._verify_closed_archive(
                    outer,
                    expected_member="platform-host-tools-bundle.zip",
                    maximum_member_bytes=1024,
                )
            for label, mutation in (
                ("digest", lambda value: {**value, "digest": "sha256:" + "0" * 64}),
                ("size", lambda value: {**value, "size_in_bytes": value["size_in_bytes"] + 1}),
                ("attempt", lambda value: {**value, "workflow_run": {**value["workflow_run"], "run_attempt": 4}}),
            ):
                with self.subTest(label=label):
                    with self.assertRaises(candidate.CandidateError):
                        candidate._verify_artifact_metadata(
                            mutation(payload),
                            archive,
                            expected_id="123",
                            expected_name="candidate",
                            expected_run_id="456",
                            expected_run_attempt="3",
                            expected_head_sha="a" * 40,
                            expected_head_ref="dev",
                        )

    def test_candidate_ancestry_accepts_pr_introduced_pin_after_merge_sync(self) -> None:
        """Model PR115's post-dev-merge pin and reject PR116's old pin."""

        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "synthetic-repository"

            def git(*arguments: str) -> str:
                completed = subprocess.run(
                    ["git", "-C", str(repository), *arguments],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return completed.stdout.strip()

            def commit(name: str, filename: str, contents: str) -> str:
                (repository / filename).write_text(contents, encoding="ascii")
                git("add", filename)
                git("commit", "-m", name)
                return git("rev-parse", "HEAD")

            repository.mkdir()
            git("init", "--initial-branch=dev")
            git("config", "user.email", "candidate-tests@example.invalid")
            git("config", "user.name", "Candidate tests")
            root_commit = commit("synthetic root", "root.txt", "root\n")
            old_pin = commit("PR116 old pinned generation 4233", "old.txt", "old\n")
            base_before_sync = commit("base before PR branch", "base.txt", "base\n")

            git("switch", "-c", "pr115")
            introduced_pin = commit("PR115 introduces host-tools generation C", "introduced.txt", "C\n")
            git("switch", "dev")
            current_base = commit("current dev advances", "dev.txt", "dev\n")
            git("switch", "pr115")
            git("merge", "--no-ff", "dev", "-m", "PR115 merge-syncs current dev")
            candidate_head = git("rev-parse", "HEAD")

            # Merge-syncing current dev leaves base reachable from E, while
            # the newly introduced C remains outside base's reachable set.
            self.assertTrue(
                    candidate.verify_ancestry(
                        repository,
                        base_sha=current_base,
                        host_tools_sha=introduced_pin,
                        source_head_sha=candidate_head,
                )
            )

            # PR116's old 4233 pin is still an ancestor of E, but current dev
            # already reaches it; it is not a PR-introduced generation.
            self.assertFalse(
                    candidate.verify_ancestry(
                        repository,
                        base_sha=current_base,
                        host_tools_sha=old_pin,
                        source_head_sha=candidate_head,
                )
            )

            with self.assertRaises(candidate.CandidateError):
                    candidate.verify_ancestry(
                        repository,
                        base_sha=current_base,
                        host_tools_sha="malformed-host-tools-pin",
                        source_head_sha=candidate_head,
                )

            with self.assertRaises(candidate.CandidateError):
                    candidate.verify_ancestry(
                        repository,
                        base_sha=current_base,
                        host_tools_sha=candidate_head,
                        source_head_sha=candidate_head,
                )

            git("switch", "-c", "unrelated", root_commit)
            unrelated_pin = commit("unrelated host-tools generation", "unrelated.txt", "unrelated\n")
            unrelated_base = commit("unrelated current base", "other-base.txt", "other\n")
            git("switch", "pr115")
            with self.assertRaises(candidate.CandidateError):
                    candidate.verify_ancestry(
                        repository,
                        base_sha=current_base,
                        host_tools_sha=unrelated_pin,
                        source_head_sha=candidate_head,
                )
            with self.assertRaises(candidate.CandidateError):
                    candidate.verify_ancestry(
                        repository,
                        base_sha=unrelated_base,
                        host_tools_sha=introduced_pin,
                        source_head_sha=candidate_head,
                )

            # Every SHA is explicit; this test never depends on the checkout's
            # own history or on symbolic parent expressions.
            self.assertNotEqual(base_before_sync, current_base)

    def test_candidate_workflow_is_workflow_run_only_trusted_and_non_deployable(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/platform-host-tools-candidate.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_run:", workflow)
        self.assertNotIn("pull_request_target", workflow)
        self.assertNotIn("\n  pull_request:", workflow)
        self.assertIn("cancel-in-progress: true", workflow)
        self.assertIn("github.event.workflow_run.head_sha", workflow)
        self.assertIn("github.event.workflow_run.pull_requests[0].number", workflow)
        exact_jobs_endpoint = "/attempts/$SECURITY_RUN_ATTEMPT/jobs?per_page=100&page=1"
        self.assertIn(exact_jobs_endpoint, workflow)
        self.assertNotIn("actions/runs/$SECURITY_RUN_ID/jobs?filter=latest", workflow)
        self.assertNotIn("actions/runs/$SECURITY_RUN_ID/pull_requests", workflow)
        self.assertIn("actions: read", workflow)
        self.assertIn("pull-requests: read", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertIn("attestations: write", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertNotIn("environment:", workflow)
        self.assertNotIn("actions: write", workflow)
        self.assertNotIn("contents: write", workflow)
        self.assertNotIn("statuses: write", workflow)
        self.assertNotIn("pull_request_target", workflow)
        self.assertIn("overwrite: false", workflow)
        self.assertIn("retention-days: 30", workflow)
        self.assertIn("Attest exact inner host-tools ZIP", workflow)
        self.assertIn("Verify uploaded evidence artifact envelope", workflow)
        self.assertIn("github.ref == 'refs/heads/dev'", workflow)
        self.assertIn("Recheck PR, security run, attempt, and head before attestation", workflow)
        self.assertIn("Recheck PR, security run, attempt, and head before upload", workflow)
        self.assertNotRegex(workflow, r"python3[^\n]*candidate-data/platform/")
        latest_jobs_workflow = workflow.replace(
            "$api/actions/runs/$SECURITY_RUN_ID" + exact_jobs_endpoint,
            "$api/actions/runs/$SECURITY_RUN_ID/jobs?filter=latest&per_page=100",
        )
        self.assertNotEqual(latest_jobs_workflow, workflow)
        self.assertTrue(host_tools_candidate_workflow_issues(latest_jobs_workflow))
        production_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (REPO_ROOT / ".github/workflows").glob("platform-production-*.yml")
        )
        self.assertNotIn(candidate.CANDIDATE_ARTIFACT_PREFIX, production_text)
        self.assertNotIn(candidate.EVIDENCE_ARTIFACT_PREFIX, production_text)
        self.assertEqual(host_tools_candidate_workflow_issues(), [])
        candidate_job = re.search(
            r"^  build-candidate:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
            workflow,
            re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(candidate_job)
        assert candidate_job is not None
        blocks = list(_workflow_step_blocks(candidate_job.group("body")))
        names = [
            re.match(r"^      - name: (?P<name>[^\n]+)", block, re.MULTILINE).group("name")
            for block in blocks
        ]
        disposition_index = names.index("Authenticate route disposition for the exact security attempt")
        security_index = names.index("Verify allowlisted completed-success jobs and closed final summary")
        self.assertLess(disposition_index, security_index)
        self.assertIn("steps.disposition.outputs.candidate == 'true'", blocks[security_index])
        for guarded_step in (
            "Checkout exact PR head E as data only",
            "Resolve C from E host_tools_pin with the trusted parser",
            "Determine PR-introduced C eligibility",
        ):
            index = names.index(guarded_step)
            self.assertIn("steps.disposition.outputs.candidate == 'true'", blocks[index])
        for guarded_step in (
            "Build and verify deterministic existing host-tools bundle from T",
            "Attest exact inner host-tools ZIP",
            "Upload exact candidate inner ZIP without overwrite",
            "Verify uploaded candidate artifact outer digest and metadata",
            "Upload closed candidate evidence without overwrite",
            "Verify uploaded evidence artifact envelope",
            "Publish exact candidate and evidence receipt",
        ):
            index = names.index(guarded_step)
            self.assertIn("steps.eligibility.outputs.eligible == 'true'", blocks[index])
        self.assertIn(
            "Finish successfully without a candidate for a non-candidate route",
            names,
        )
        attestation_index = names.index("Attest exact inner host-tools ZIP")
        attestation_recheck_index = names.index(
            "Recheck PR, security run, attempt, and head before attestation"
        )
        blocks[attestation_index], blocks[attestation_recheck_index] = (
            blocks[attestation_recheck_index],
            blocks[attestation_index],
        )
        broken_order = (
            workflow[: candidate_job.start("body")]
            + "".join(blocks)
            + workflow[candidate_job.end("body") :]
        )
        self.assertTrue(
            any(
                "must recheck before Attest exact inner host-tools ZIP" in issue
                for issue in host_tools_candidate_workflow_issues(broken_order)
            )
        )
        artifact_zip_blocks = host_tools_candidate_artifact_zip_curl_blocks(workflow)
        self.assertEqual(len(artifact_zip_blocks), 4)
        for block in artifact_zip_blocks:
            broken_workflow = workflow.replace(
                block,
                block.replace("application/vnd.github+json", "application/zip", 1),
                1,
            )
            self.assertTrue(
                any(
                    "must request application/vnd.github+json" in issue
                    for issue in host_tools_candidate_workflow_issues(broken_workflow)
                ),
                block,
            )
        self._assert_artifact_zip_transport_contract()

        # Exercise the exact trusted workflow invocation in isolated,
        # bytecode-free mode.  A candidate-side module with the same name is
        # deliberately present and must never satisfy the trusted import.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trusted_tools = root / "trusted-dev/platform/tools"
            trusted_tools.mkdir(parents=True)
            shutil.copyfile(TOOLS_ROOT / "platform_host_tools_candidate.py", trusted_tools / "platform_host_tools_candidate.py")
            shutil.copyfile(TOOLS_ROOT / "platform_host_tools_bundle.py", trusted_tools / "platform_host_tools_bundle.py")
            shutil.copyfile(TOOLS_ROOT / "platform_ci_classifier.py", trusted_tools / "platform_ci_classifier.py")
            shutil.copyfile(TOOLS_ROOT / "platform_workflow_provenance.py", trusted_tools / "platform_workflow_provenance.py")
            candidate_tools = root / "candidate-data/platform/tools"
            candidate_tools.mkdir(parents=True)
            (candidate_tools / "platform_host_tools_bundle.py").write_text(
                "raise RuntimeError('candidate code must never load')\n",
                encoding="ascii",
            )
            event = root / "event.json"
            event.write_text(
                json.dumps(
                    {
                        "workflow_run": {
                            "repository": {"full_name": "StrayForest/old_sparky"},
                            "workflow_id": 339062797,
                            "name": "Platform security and build",
                            "path": ".github/workflows/platform-security.yml",
                            "event": "pull_request",
                            "status": "completed",
                            "conclusion": "success",
                            "id": 36289064582,
                            "run_attempt": 1,
                            "head_sha": "a" * 40,
                            "head_branch": "feature/candidate",
                            "head_repository": {"full_name": "StrayForest/old_sparky"},
                            "pull_requests": [
                                {
                                    "number": 117,
                                    "base": {
                                        "ref": "dev",
                                        "sha": "b" * 40,
                                        "repo": {"name": "old_sparky"},
                                    },
                                    "head": {
                                        "ref": "feature/candidate",
                                        "sha": "a" * 40,
                                        "repo": {"name": "old_sparky"},
                                    },
                                }
                            ],
                        }
                    }
                ),
                encoding="ascii",
            )
            completed = subprocess.run(
                [
                    "/usr/bin/python3",
                    "-I",
                    "-B",
                    "trusted-dev/platform/tools/platform_host_tools_candidate.py",
                    "inspect-event",
                    "--event",
                    str(event),
                ],
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONPATH": str(candidate_tools)},
                cwd=root,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn('"source_head_sha":"' + "a" * 40 + '"', completed.stdout)
            self.assertFalse((trusted_tools / "__pycache__").exists())


if __name__ == "__main__":
    unittest.main()
