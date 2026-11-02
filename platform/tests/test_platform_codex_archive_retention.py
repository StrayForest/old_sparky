from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from unittest import mock
import zipfile

from tools.codex_archive_retention import cleanup, verify_artifact
from tools.platform_ci_classifier import CANDIDATE_PACKAGING_FILES, classify, manifest_digest


class CodexArchiveRetentionTests(unittest.TestCase):
    def test_builder_uses_strict_context_free_patch_application(self) -> None:
        repository_root = Path(__file__).resolve().parents[2]
        workflow = (
            repository_root
            / ".github/workflows/platform-codex-archive-retention-build.yml"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "git -C codex-source apply --unidiff-zero --whitespace=error-all --check",
            workflow,
        )
        self.assertIn(
            "git -C codex-source apply --unidiff-zero --whitespace=error-all \"$patch\"",
            workflow,
        )
        self.assertIn("git -C codex-source diff --check", workflow)
        self.assertIn(
            "hashlib.sha256(patch_path.read_bytes()).hexdigest() != lock[\"patch_sha256\"]",
            workflow,
        )
        self.assertNotIn(
            '= "${{ steps.lock.outputs.patch_sha256 }}"',
            workflow,
        )

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="codex-archive-retention-")
        self.root = Path(self.temp.name)
        self.home = self.root / ".codex"
        self.archive = self.home / cleanup.ARCHIVE_DIR_NAME
        self.active = self.home / cleanup.ACTIVE_DIR_NAME
        self.archive.mkdir(parents=True, mode=0o700)
        self.active.mkdir(mode=0o700)
        self.database = self.home / cleanup.STATE_DB_NAME
        with sqlite3.connect(self.database) as connection:
            connection.executescript(
                """
                CREATE TABLE threads (
                    id TEXT PRIMARY KEY,
                    rollout_path TEXT NOT NULL,
                    archived INTEGER NOT NULL,
                    archived_at TEXT
                );
                CREATE TABLE thread_spawn_edges (
                    parent_thread_id TEXT NOT NULL,
                    child_thread_id TEXT NOT NULL
                );
                """
            )
        self.calls = self.root / "calls.txt"
        self.cli, self.manifest = self.make_companion()
        self.activation = self.make_activation()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def insert_thread(
        self,
        thread_id: str,
        *,
        archived: int = 1,
        archived_at: str | None = "2026-01-01T00:00:00Z",
        rollout_path: Path | None = None,
    ) -> Path:
        path = rollout_path or self.archive / f"rollout-2026-01-01T00-00-00-{thread_id}.jsonl"
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "INSERT INTO threads(id, rollout_path, archived, archived_at) VALUES (?, ?, ?, ?)",
                (thread_id, str(path), archived, archived_at),
            )
        return path

    def write_rollout(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Intentionally not JSON: discovery must rely on file metadata only.
        path.write_bytes(b"synthetic rollout payload that retention never parses\x00\xff")

    def make_companion(self) -> tuple[Path, Path]:
        cli = self.root / "private-codex"
        lock = json.loads(cleanup.SOURCE_LOCK.read_text(encoding="utf-8"))
        cli.write_text(
            "#!/usr/bin/python3\n"
            "import os, sqlite3, sys\n"
            "if sys.argv[1:] == ['--version']:\n"
            f"    print({lock['cli_version']!r})\n"
            "    raise SystemExit(0)\n"
            "if len(sys.argv) != 5 or sys.argv[1:4] != ['delete', '--force', '--archived-only']:\n"
            "    raise SystemExit(92)\n"
            "thread_id = sys.argv[4]\n"
            "with open(os.environ['CODEX_TEST_CALL_LOG'], 'a', encoding='utf-8') as stream:\n"
            "    stream.write(' '.join(sys.argv[1:]) + '\\n')\n"
            "if thread_id in os.environ.get('CODEX_TEST_FAIL_IDS', '').split(','):\n"
            "    raise SystemExit(9)\n"
            "with sqlite3.connect(os.path.join(os.environ['CODEX_HOME'], 'state_5.sqlite')) as connection:\n"
            "    connection.execute('DELETE FROM thread_spawn_edges WHERE parent_thread_id=? OR child_thread_id=?', (thread_id, thread_id))\n"
            "    connection.execute('DELETE FROM threads WHERE id=?', (thread_id,))\n"
            "for collection in ['archived_sessions', 'sessions']:\n"
            "    folder = os.path.join(os.environ['CODEX_HOME'], collection)\n"
            "    for name in os.listdir(folder):\n"
            "        if name.endswith('-' + thread_id + '.jsonl'):\n"
            "            os.unlink(os.path.join(folder, name))\n"
            "raise SystemExit(0)\n",
            encoding="utf-8",
        )
        cli.chmod(0o700)
        manifest_data = {
            "schema_version": 1,
            "source_repository": lock["source_repository"],
            "source_commit": lock["source_commit"],
            "source_tag": lock["source_tag"],
            "patch_sha256": lock["patch_sha256"],
            "rust_toolchain": lock["rust_toolchain"],
            "target": lock["target"],
            "cli_version": lock["cli_version"],
            "platform_repository": "StrayForest/old_sparky",
            "platform_event": "pull_request",
            "platform_ref": "refs/pull/123/merge",
            "platform_base_ref": "dev",
            "platform_head_repository": "StrayForest/old_sparky",
            "platform_pr_number": "123",
            "platform_source_sha": "a" * 40,
            "platform_source_head_sha": "a" * 40,
            "platform_tested_merge_sha": "b" * 40,
            "workflow_run_id": "12345",
            "workflow_run_attempt": "1",
            "binary_bytes": cli.stat().st_size,
            "binary_sha256": hashlib.sha256(cli.read_bytes()).hexdigest(),
        }
        manifest = self.root / "private-codex.manifest.json"
        manifest.write_text(json.dumps(manifest_data), encoding="utf-8")
        manifest.chmod(0o600)
        return cli, manifest

    def make_activation(self) -> Path:
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        activation = {
            "schema_version": 1,
            "platform_repository": manifest["platform_repository"],
            "pull_request": manifest["platform_pr_number"],
            "platform_source_sha": manifest["platform_source_sha"],
            "tested_merge_sha": manifest["platform_tested_merge_sha"],
            "build_run_id": manifest["workflow_run_id"],
            "build_run_attempt": manifest["workflow_run_attempt"],
            "build_artifact_id": "34567",
            "build_artifact_sha256": "c" * 64,
            "security_workflow_name": "Platform security and build",
            "security_workflow_path": ".github/workflows/platform-security.yml",
            "security_run_id": "23456",
            "security_run_attempt": "1",
            "security_conclusion": "success",
            "security_target_sha": manifest["platform_tested_merge_sha"],
            "security_route_artifact_id": "45678",
            "security_route_sha256": "d" * 64,
            "classifier_class": "full",
            "classifier_reason": verify_artifact.CANDIDATE_REASON,
            "classifier_runtime_sensitive": False,
            "classifier_fallback": False,
            "classifier_deployable": False,
        }
        path = self.root / "codex.activation.json"
        path.write_text(json.dumps(activation), encoding="utf-8")
        path.chmod(0o600)
        return path

    def run_retention(self) -> int:
        with mock.patch.dict(
            os.environ,
            {"CODEX_TEST_CALL_LOG": str(self.calls)},
        ):
            return cleanup.run_maintenance(
                self.home, self.cli, self.manifest, self.activation
            )

    def call_lines(self) -> list[str]:
        return self.calls.read_text(encoding="utf-8").splitlines() if self.calls.exists() else []

    def test_only_archive_metadata_candidates_use_native_archived_only_command(self) -> None:
        thread_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        rollout = self.insert_thread(thread_id)
        self.write_rollout(rollout)

        self.assertEqual(self.run_retention(), 0)

        self.assertEqual(self.call_lines(), [f"delete --force --archived-only {thread_id}"])
        self.assertFalse(rollout.exists())
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0], 0)

    def test_unreferenced_archived_row_without_file_is_submitted_to_native_guard(self) -> None:
        thread_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        self.insert_thread(thread_id)

        self.assertEqual(self.run_retention(), 0)

        self.assertEqual(self.call_lines(), [f"delete --force --archived-only {thread_id}"])
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0], 0)

    def test_stale_archive_file_without_archived_row_is_never_deleted(self) -> None:
        thread_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        rollout = self.archive / f"rollout-2026-01-01T00-00-00-{thread_id}.jsonl"
        self.write_rollout(rollout)

        self.assertEqual(self.run_retention(), 0)

        self.assertEqual(self.call_lines(), [])
        self.assertTrue(rollout.exists())

    def test_active_thread_and_active_rollout_are_preserved(self) -> None:
        thread_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        active_path = self.active / f"rollout-2026-01-01T00-00-00-{thread_id}.jsonl"
        self.write_rollout(active_path)
        self.insert_thread(thread_id, archived=1, rollout_path=active_path)

        self.assertEqual(self.run_retention(), 0)

        self.assertEqual(self.call_lines(), [])
        self.assertTrue(active_path.exists())

    def test_spawn_parent_waits_for_archived_leaf_then_reenumerates(self) -> None:
        parent = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
        child = "ffffffff-ffff-4fff-8fff-ffffffffffff"
        parent_path = self.insert_thread(parent)
        child_path = self.insert_thread(child)
        self.write_rollout(parent_path)
        self.write_rollout(child_path)
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "INSERT INTO thread_spawn_edges(parent_thread_id, child_thread_id) VALUES (?, ?)",
                (parent, child),
            )

        self.assertEqual(self.run_retention(), 0)

        self.assertEqual(
            self.call_lines(),
            [f"delete --force --archived-only {child}", f"delete --force --archived-only {parent}"],
        )
        self.assertFalse(parent_path.exists())
        self.assertFalse(child_path.exists())

    def test_native_guard_refusal_preserves_row_and_archive_file(self) -> None:
        thread_id = "12121212-1212-4212-8212-121212121212"
        rollout = self.insert_thread(thread_id)
        self.write_rollout(rollout)

        with mock.patch.dict(os.environ, {"CODEX_TEST_FAIL_IDS": thread_id}):
            self.assertEqual(self.run_retention(), 0)

        self.assertEqual(self.call_lines(), [f"delete --force --archived-only {thread_id}"])
        self.assertTrue(rollout.exists())
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0], 1)

    def test_native_refusal_is_reported_and_does_not_repeat_after_success(self) -> None:
        blocked_id = "00000000-0000-4000-8000-000000000001"
        removable_id = "00000000-0000-4000-8000-000000000002"
        for thread_id in (blocked_id, removable_id):
            self.write_rollout(self.insert_thread(thread_id))
        output = io.StringIO()

        with mock.patch.dict(os.environ, {"CODEX_TEST_FAIL_IDS": blocked_id}):
            with contextlib.redirect_stderr(output):
                self.assertEqual(self.run_retention(), 0)

        self.assertEqual(
            self.call_lines(),
            [
                f"delete --force --archived-only {blocked_id}",
                f"delete --force --archived-only {removable_id}",
            ],
        )
        self.assertIn("native attempts 2/16; deleted 1 archived session(s)", output.getvalue())
        self.assertIn(
            "deferred 1 candidate(s) (native refusals 1, metadata races 0)",
            output.getvalue(),
        )

    def test_sixteen_attempt_cap_rotates_to_later_ids_on_next_pass(self) -> None:
        thread_ids = [
            f"00000000-0000-4000-8000-{number:012x}"
            for number in range(1, cleanup.MAX_DELETES_PER_RUN + 2)
        ]
        for thread_id in thread_ids:
            self.write_rollout(self.insert_thread(thread_id))
        failures = ",".join(thread_ids[:-1])

        with mock.patch.dict(os.environ, {"CODEX_TEST_FAIL_IDS": failures}):
            self.assertEqual(self.run_retention(), 0)
            first_pass = self.call_lines()
            self.assertEqual(len(first_pass), cleanup.MAX_DELETES_PER_RUN)
            self.assertEqual(
                [line.rsplit(" ", 1)[-1] for line in first_pass], thread_ids[:-1]
            )

            self.assertEqual(self.run_retention(), 0)

        all_calls = self.call_lines()
        second_pass = all_calls[len(first_pass):]
        self.assertEqual(len(second_pass), cleanup.MAX_DELETES_PER_RUN)
        self.assertEqual(second_pass[0], f"delete --force --archived-only {thread_ids[-1]}")
        self.assertEqual(
            json.loads((self.home / cleanup.RETENTION_CURSOR_NAME).read_text()),
            {
                "after_thread_id": thread_ids[cleanup.MAX_DELETES_PER_RUN - 2],
                "schema_version": 1,
            },
        )
        self.assertEqual(
            stat.S_IMODE((self.home / cleanup.RETENTION_CURSOR_NAME).stat().st_mode),
            0o600,
        )

    def test_malformed_cursor_fails_closed_before_native_delete(self) -> None:
        thread_id = "16161616-1616-4616-8616-161616161616"
        rollout = self.insert_thread(thread_id)
        self.write_rollout(rollout)
        cursor = self.home / cleanup.RETENTION_CURSOR_NAME
        cursor.write_text("{not-json", encoding="ascii")
        cursor.chmod(0o600)

        with self.assertRaises(cleanup.UnsafeState):
            self.run_retention()

        self.assertEqual(self.call_lines(), [])
        self.assertTrue(rollout.exists())

    def test_companion_provenance_mismatch_fails_before_delete(self) -> None:
        thread_id = "13131313-1313-4313-8313-131313131313"
        rollout = self.insert_thread(thread_id)
        self.write_rollout(rollout)
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["source_commit"] = "0" * 40
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaises(cleanup.UnsafeState):
            self.run_retention()

        self.assertEqual(self.call_lines(), [])
        self.assertTrue(rollout.exists())

    def test_fork_companion_cannot_be_installed_as_service_binary(self) -> None:
        thread_id = "14141414-1414-4414-8414-141414141414"
        rollout = self.insert_thread(thread_id)
        self.write_rollout(rollout)
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["platform_head_repository"] = "attacker/fork"
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaises(cleanup.UnsafeState):
            self.run_retention()

        self.assertEqual(self.call_lines(), [])
        self.assertTrue(rollout.exists())

    def test_activation_receipt_requires_successful_exact_security_route(self) -> None:
        thread_id = "15151515-1515-4515-8515-151515151515"
        rollout = self.insert_thread(thread_id)
        self.write_rollout(rollout)
        activation = json.loads(self.activation.read_text(encoding="utf-8"))
        activation["classifier_fallback"] = True
        self.activation.write_text(json.dumps(activation), encoding="utf-8")

        with self.assertRaises(cleanup.UnsafeState):
            self.run_retention()

        self.assertEqual(self.call_lines(), [])
        self.assertTrue(rollout.exists())

    def test_build_artifact_manifest_binds_exact_reviewed_source_and_binary(self) -> None:
        lock = json.loads(cleanup.SOURCE_LOCK.read_text(encoding="utf-8"))
        binary = b"synthetic private companion binary"
        manifest = {
            "schema_version": 1,
            "platform_repository": "StrayForest/old_sparky",
            "platform_event": "pull_request",
            "platform_ref": "refs/pull/123/merge",
            "platform_base_ref": "dev",
            "platform_head_repository": "StrayForest/old_sparky",
            "platform_pr_number": "123",
            "platform_source_sha": "a" * 40,
            "platform_source_head_sha": "a" * 40,
            "platform_tested_merge_sha": "b" * 40,
            "workflow_run_id": "12345",
            "workflow_run_attempt": "1",
            "source_repository": lock["source_repository"],
            "source_tag": lock["source_tag"],
            "source_commit": lock["source_commit"],
            "patch_sha256": lock["patch_sha256"],
            "rust_toolchain": lock["rust_toolchain"],
            "target": lock["target"],
            "cli_version": lock["cli_version"],
            "binary_bytes": len(binary),
            "binary_sha256": hashlib.sha256(binary).hexdigest(),
        }
        root = Path(__file__).resolve().parents[2]
        verified = verify_artifact._validate_build_artifact(
            {
                "codex": binary,
                "codex.manifest.json": json.dumps(manifest).encode(),
                "version.txt": b"codex-cli 0.166.0\n",
            },
            source_root=root,
            lock=lock,
            pr="123",
            head="a" * 40,
            merge="b" * 40,
            build_run_id="12345",
            build_attempt="1",
        )
        self.assertEqual(verified, manifest)

        manifest["workflow_run_attempt"] = "2"
        with self.assertRaises(verify_artifact.VerificationError):
            verify_artifact._validate_build_artifact(
                {
                    "codex": binary,
                    "codex.manifest.json": json.dumps(manifest).encode(),
                    "version.txt": b"codex-cli 0.166.0\n",
                },
                source_root=root,
                lock=lock,
                pr="123",
                head="a" * 40,
                merge="b" * 40,
                build_run_id="12345",
                build_attempt="1",
            )

    def test_security_route_verifier_rejects_fallback_or_deployable_manifests(self) -> None:
        root = Path(__file__).resolve().parents[2]
        merge = "b" * 40
        manifest = classify(
            sorted(CANDIDATE_PACKAGING_FILES),
            event="pull_request",
            target_sha=merge,
            branch="feature/codex",
        )
        verify_artifact._validate_classifier_manifest(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(),
            source_root=root,
            head="a" * 40,
            merge=merge,
        )
        tampered = dict(manifest)
        tampered["deployable"] = True
        tampered["digest"] = manifest_digest(tampered)
        with self.assertRaises(verify_artifact.VerificationError):
            verify_artifact._validate_classifier_manifest(
                json.dumps(tampered, sort_keys=True, separators=(",", ":")).encode(),
                source_root=root,
                head="a" * 40,
                merge=merge,
            )

    def test_exact_api_runs_and_raw_zip_digests_produce_activation_receipt(self) -> None:
        source_root = self.root / "reviewed-source"
        classifier_path = source_root / "platform/tools/platform_ci_classifier.py"
        lock_path = source_root / "platform/tools/codex_archive_retention/codex-source.lock.json"
        patch_path = source_root / "platform/tools/codex_archive_retention/patches/codex-0.166.0.patch"
        classifier_path.parent.mkdir(parents=True)
        lock_path.parent.mkdir(parents=True)
        patch_path.parent.mkdir(parents=True)
        repo_root = Path(__file__).resolve().parents[2]
        classifier_path.write_bytes((repo_root / "platform/tools/platform_ci_classifier.py").read_bytes())
        lock_path.write_bytes(cleanup.SOURCE_LOCK.read_bytes())
        patch_path.write_bytes((cleanup.PACKAGE_DIR / "patches/codex-0.166.0.patch").read_bytes())
        subprocess.run(["git", "-C", str(source_root), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(source_root), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(source_root), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture"],
            check=True,
        )
        head = subprocess.check_output(
            ["git", "-C", str(source_root), "rev-parse", "HEAD"], text=True
        ).strip()
        merge = "b" * 40
        pr = "123"
        build_id, security_id = "23456", "34567"
        build_attempt = security_attempt = "1"

        def write_zip(path: Path, members: dict[str, bytes]) -> None:
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
                for name, contents in members.items():
                    info = zipfile.ZipInfo(name)
                    info.create_system = 3
                    info.external_attr = (stat.S_IFREG | 0o600) << 16
                    archive.writestr(info, contents)

        binary = b"synthetic non-executable fixture binary"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        build_manifest = {
            "schema_version": 1,
            "platform_repository": "StrayForest/old_sparky",
            "platform_event": "pull_request",
            "platform_ref": f"refs/pull/{pr}/merge",
            "platform_base_ref": "dev",
            "platform_head_repository": "StrayForest/old_sparky",
            "platform_pr_number": pr,
            "platform_source_sha": head,
            "platform_source_head_sha": head,
            "platform_tested_merge_sha": merge,
            "workflow_run_id": build_id,
            "workflow_run_attempt": build_attempt,
            "source_repository": lock["source_repository"],
            "source_tag": lock["source_tag"],
            "source_commit": lock["source_commit"],
            "patch_sha256": lock["patch_sha256"],
            "rust_toolchain": lock["rust_toolchain"],
            "target": lock["target"],
            "cli_version": lock["cli_version"],
            "binary_bytes": len(binary),
            "binary_sha256": hashlib.sha256(binary).hexdigest(),
        }
        build_zip = self.root / "build-artifact.zip"
        write_zip(
            build_zip,
            {
                "codex": binary,
                "codex.manifest.json": json.dumps(build_manifest).encode(),
                "version.txt": b"codex-cli 0.166.0\n",
            },
        )
        # The actual PR combines the Codex package with backup-retention work,
        # so the classifier takes its standard full route rather than the
        # candidate-only route.
        mixed_paths = sorted(CANDIDATE_PACKAGING_FILES | {"platform/tools/platform_backup_retention.py"})
        route = classify(
            mixed_paths,
            event="pull_request",
            target_sha=merge,
            branch="feature/codex",
        )
        security_zip = self.root / "security-route.zip"
        write_zip(
            security_zip,
            {"classifier-manifest.json": json.dumps(route).encode()},
        )

        def artifact_metadata(name: str, run_id: str, attempt: str, artifact_id: int, path: Path) -> Path:
            metadata = {
                "total_count": 1,
                "artifacts": [
                    {
                        "id": artifact_id,
                        "name": name,
                        "expired": False,
                        "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
                        "workflow_run": {
                            "id": int(run_id),
                            "run_attempt": int(attempt),
                            "head_sha": head,
                        },
                    }
                ],
            }
            result = self.root / f"{name}.json"
            result.write_text(json.dumps(metadata), encoding="utf-8")
            return result

        def run_record(run_id: str, attempt: str, name: str, path: str) -> Path:
            payload = {
                "id": int(run_id),
                "run_attempt": int(attempt),
                "name": name,
                "path": path,
                "event": "pull_request",
                "status": "completed",
                "conclusion": "success",
                "head_sha": head,
                "repository": {"full_name": "StrayForest/old_sparky"},
                "head_repository": {"full_name": "StrayForest/old_sparky"},
                "pull_requests": [
                    {"number": int(pr), "head": {"sha": head}, "base": {"ref": "dev"}}
                ],
            }
            result = self.root / f"run-{run_id}.json"
            result.write_text(json.dumps(payload), encoding="utf-8")
            return result

        build_name = f"codex-archive-retention-{build_id}-{build_attempt}"
        security_name = f"platform-ci-route-{security_id}-{security_attempt}"
        build_artifacts = artifact_metadata(build_name, build_id, build_attempt, 456, build_zip)
        security_artifacts = artifact_metadata(security_name, security_id, security_attempt, 567, security_zip)
        output = self.root / "verified.activation.json"
        args = mock.Mock(
            source_root=source_root,
            pull_request=pr,
            head_sha=head,
            merge_sha=merge,
            build_run_id=build_id,
            build_attempt=build_attempt,
            build_run=run_record(build_id, build_attempt, verify_artifact.BUILD_WORKFLOW_NAME, verify_artifact.BUILD_WORKFLOW_PATH),
            build_artifacts=build_artifacts,
            build_zip=build_zip,
            security_run_id=security_id,
            security_attempt=security_attempt,
            security_run=run_record(security_id, security_attempt, verify_artifact.SECURITY_WORKFLOW_NAME, verify_artifact.SECURITY_WORKFLOW_PATH),
            security_artifacts=security_artifacts,
            security_zip=security_zip,
            activation_output=output,
        )

        verify_artifact.verify(args)

        activation = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(activation["classifier_reason"], verify_artifact.STANDARD_FULL_REASON)
        self.assertEqual(activation["build_artifact_id"], "456")
        self.assertEqual(activation["security_route_artifact_id"], "567")
        self.assertEqual(activation["build_artifact_sha256"], hashlib.sha256(build_zip.read_bytes()).hexdigest())
        self.assertEqual(activation["security_route_sha256"], hashlib.sha256(security_zip.read_bytes()).hexdigest())
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

        # Prove the exact verifier-generated receipt is accepted by the runtime
        # wrapper for the same source/build identities.
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest.update(
            {
                "platform_source_sha": head,
                "platform_source_head_sha": head,
                "platform_tested_merge_sha": merge,
                "workflow_run_id": build_id,
                "workflow_run_attempt": build_attempt,
            }
        )
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        self.activation = output
        thread_id = "abababab-abab-4bab-8bab-abababababab"
        rollout = self.insert_thread(thread_id)
        self.write_rollout(rollout)
        self.assertEqual(self.run_retention(), 0)
        self.assertEqual(self.call_lines(), [f"delete --force --archived-only {thread_id}"])
        self.assertFalse(rollout.exists())


if __name__ == "__main__":
    unittest.main()
