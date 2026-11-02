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
import shutil
import tempfile
import textwrap
import unittest
from unittest import mock
import zipfile

from tools.codex_archive_retention import capture_pr_association, cleanup, verify_artifact
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

    def test_native_test_filters_aggregate_failures_and_reject_empty_matches(self) -> None:
        repository_root = Path(__file__).resolve().parents[2]
        workflow = (
            repository_root
            / ".github/workflows/platform-codex-archive-retention-build.yml"
        ).read_text(encoding="utf-8")
        step = workflow.split(
            "      - name: Run focused archived-only native lifecycle tests\n", 1
        )[1].split("      - name: Build and bind private companion binary\n", 1)[0]
        shell = textwrap.dedent(step.split("        run: |\n", 1)[1])
        lines = shell.splitlines()
        start = lines.index("run_focused_test() {")
        end = lines.index("}", start) + 1
        helper = "\n".join(lines[start:end])

        with tempfile.TemporaryDirectory(prefix="codex-native-test-aggregate-") as temp:
            root = Path(temp)
            runner_temp = root / "runner-temp"
            runner_temp.mkdir()
            fake_bin = root / "bin"
            fake_bin.mkdir()
            cargo = fake_bin / "cargo"
            cargo.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  fail) printf '%s\\n' \\\n"
                "    'test result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out'; \\\n"
                "    exit 101 ;;\n"
                "  pass) printf '%s\\n' \\\n"
                "    'test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out'; \\\n"
                "    exit 0 ;;\n"
                "  empty) printf '%s\\n' \\\n"
                "    'test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out'; \\\n"
                "    exit 0 ;;\n"
                "  *) exit 98 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            cargo.chmod(0o700)
            script = (
                "set -euo pipefail\n"
                "failed=0\n"
                f"RUNNER_TEMP={str(runner_temp)!r}\n"
                f"{helper}\n"
                "run_focused_test first-fails cargo fail\n"
                "run_focused_test second-passes cargo pass\n"
                "run_focused_test third-empty cargo empty\n"
                "test \"$failed\" -eq 1\n"
            )
            result = subprocess.run(
                ["bash", "-c", script],
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Focused native test failed: first-fails", result.stdout)
        self.assertIn("Focused native test passed: second-passes", result.stdout)
        self.assertIn("Focused native test failed: third-empty", result.stdout)

    def test_release_build_profile_and_diagnostics_are_explicit_and_bounded(self) -> None:
        repository_root = Path(__file__).resolve().parents[2]
        workflow = (
            repository_root
            / ".github/workflows/platform-codex-archive-retention-build.yml"
        ).read_text(encoding="utf-8")
        build_step = workflow.split(
            "      - name: Build and bind private companion binary\n", 1
        )[1].split("      - name: Upload bounded native build diagnostics\n", 1)[0]
        diagnostics_step = workflow.split(
            "      - name: Upload bounded native build diagnostics\n", 1
        )[1].split("      - name: Upload non-deployable companion evidence artifact\n", 1)[0]

        self.assertIn('CARGO_PROFILE_RELEASE_LTO: "false"', build_step)
        self.assertIn('CARGO_BUILD_JOBS: "1"', build_step)
        self.assertEqual(workflow.count('CARGO_BUILD_JOBS: "1"'), 1)
        self.assertIn("timeout-minutes: 150", workflow)
        self.assertNotIn("CARGO_PROFILE_RELEASE_CODEGEN_UNITS", workflow)
        self.assertIn("umask 077", build_step)
        self.assertIn("build --locked --release --timings", build_step)
        self.assertIn("/usr/bin/time -v", build_step)
        self.assertIn('"release_profile": "release"', build_step)
        self.assertIn('"release_lto": False', build_step)
        self.assertIn('"release_codegen_units": 4', build_step)
        self.assertIn('"sample_limit": sample_limit', build_step)
        self.assertIn('"sample_interval_seconds": interval_seconds', build_step)
        self.assertIn('"resource_sample_duration_seconds": sample_limit * interval_seconds', build_step)
        self.assertIn('600 15 <<\'PY\' &', build_step)
        self.assertIn('print("codex-build-resource " + line, flush=True)', build_step)
        self.assertIn('"nproc": command_integer("nproc")', build_step)
        self.assertIn('"rustc_process_count": len(values)', build_step)
        self.assertIn('"rustc_rss_sum_bytes": sum(values)', build_step)
        self.assertIn('"rustc_rss_max_bytes": max(values, default=0)', build_step)
        self.assertIn('"workflow_run_attempt": run_attempt', build_step)
        self.assertIn('"tested_merge_sha": merge_sha', build_step)
        self.assertIn("samples.flush()", build_step)
        self.assertIn("os.fsync(samples.fileno())", build_step)
        self.assertIn("if (( sampler_status != 0 )); then", build_step)
        self.assertIn("refusing binary artifact", build_step)
        self.assertIn('test "$binary_bytes" -le "$MAX_BINARY_BYTES"', build_step)
        self.assertIn("cargo-timing.html", build_step)
        self.assertIn("timing_bytes <= 10485760", build_step)
        self.assertIn("if: always()", diagnostics_step)
        self.assertIn(
            "codex-archive-retention-build-diagnostics-${{ github.run_id }}-${{ github.run_attempt }}",
            diagnostics_step,
        )
        self.assertIn("${{ runner.temp }}/codex-build-diagnostics", diagnostics_step)
        self.assertLess(
            workflow.index("      - name: Upload bounded native build diagnostics\n"),
            workflow.index("      - name: Upload non-deployable companion evidence artifact\n"),
        )
        self.assertIn(
            'name: codex-archive-retention-${{ github.run_id }}-${{ github.run_attempt }}',
            workflow,
        )

    def test_schema_fixture_diagnostic_is_closed_and_precedes_native_tests(self) -> None:
        repository_root = Path(__file__).resolve().parents[2]
        workflow = (
            repository_root
            / ".github/workflows/platform-codex-archive-retention-build.yml"
        ).read_text(encoding="utf-8")
        generation_step = workflow.index(
            "      - name: Generate pinned schema fixtures in an isolated copy\n"
        )
        compare_step = workflow.index(
            "      - name: Package generated schema fixture diagnostics\n"
        )
        native_tests_step = workflow.index(
            "      - name: Run focused archived-only native lifecycle tests\n"
        )
        self.assertLess(generation_step, compare_step)
        self.assertLess(compare_step, native_tests_step)
        self.assertIn("if: steps.schema.outputs.schema_changed == 'false'", workflow)
        self.assertIn("if: steps.schema.outputs.schema_changed == 'true'", workflow)
        self.assertIn("schema_fixtures_tests::write_schema_fixtures_from_env", workflow)
        compare_text = workflow.split(
            "      - name: Package generated schema fixture diagnostics\n", 1
        )[1].split("      - name: Upload non-executable schema fixture diagnostic\n", 1)[0]
        script = textwrap.dedent(compare_text.split("<<'PY'\n", 1)[1].split("\n          PY", 1)[0])
        lock_path = repository_root / "platform/tools/codex_archive_retention/codex-source.lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        common_env = {
            **os.environ,
            "GITHUB_REPOSITORY": "StrayForest/old_sparky",
            "PLATFORM_SOURCE_SHA": "a" * 40,
            "PLATFORM_SOURCE_HEAD_SHA": "a" * 40,
            "PLATFORM_TESTED_MERGE_SHA": "b" * 40,
            "PLATFORM_PR_NUMBER": "123",
            "GITHUB_RUN_ID": "12345",
            "GITHUB_RUN_ATTEMPT": "1",
            "CODEX_SOURCE_COMMIT": lock["source_commit"],
            "CODEX_SOURCE_TAG": lock["source_tag"],
            "CODEX_RUST_TOOLCHAIN": lock["rust_toolchain"],
            "CODEX_TARGET": lock["target"],
        }

        def run_diagnostic(baseline: Path, generated: Path, output: Path, diagnostic: Path):
            output.touch()
            return subprocess.run(
                [
                    "/usr/bin/python3",
                    "-I",
                    "-B",
                    "-",
                    str(lock_path),
                    str(repository_root / "platform/tools/codex_archive_retention/patches/codex-0.166.0.patch"),
                    str(baseline),
                    str(generated),
                    str(diagnostic),
                    str(output),
                ],
                input=script,
                text=True,
                capture_output=True,
                check=False,
                cwd=repository_root,
                env=common_env,
            )

        with tempfile.TemporaryDirectory(prefix="codex-schema-diagnostic-") as temp:
            root = Path(temp)
            baseline = root / "baseline"
            baseline_file = baseline / "typescript/v2/ThreadDeleteParams.ts"
            baseline_file.parent.mkdir(parents=True)
            baseline_file.write_text("baseline fixture\n", encoding="ascii")
            generated = root / "generated"
            shutil.copytree(baseline, generated)
            (generated / "typescript/v2/ThreadDeleteParams.ts").write_text(
                "generated fixture\n", encoding="ascii"
            )
            changed_output = root / "changed.outputs"
            changed_diagnostic = root / "changed"
            changed_result = run_diagnostic(
                baseline, generated, changed_output, changed_diagnostic
            )
            self.assertEqual(changed_result.returncode, 0, changed_result.stderr)
            self.assertIn("schema_changed=true", changed_output.read_text(encoding="ascii"))
            payload_paths = {
                path.relative_to(changed_diagnostic).as_posix()
                for path in changed_diagnostic.rglob("*")
                if path.is_file()
            }
            self.assertEqual(
                payload_paths,
                {
                    "schema-fixture-diagnostic.json",
                    "typescript/v2/ThreadDeleteParams.ts",
                },
            )
            manifest = json.loads(
                (changed_diagnostic / "schema-fixture-diagnostic.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["patch_sha256"], lock["patch_sha256"])
            self.assertEqual(manifest["workflow_run_id"], "12345")
            self.assertEqual(manifest["changed_files"][0]["path"], "typescript/v2/ThreadDeleteParams.ts")
            self.assertEqual(
                stat.S_IMODE((changed_diagnostic / "typescript/v2/ThreadDeleteParams.ts").stat().st_mode),
                0o600,
            )

            unknown = root / "unknown"
            shutil.copytree(baseline, unknown)
            (unknown / "unexpected.txt").write_text("not allowlisted", encoding="ascii")
            unknown_output = root / "unknown.outputs"
            unknown_diagnostic = root / "unknown-diagnostic"
            unknown_result = run_diagnostic(
                baseline, unknown, unknown_output, unknown_diagnostic
            )
            self.assertNotEqual(unknown_result.returncode, 0)
            self.assertFalse(unknown_diagnostic.exists())

            unchanged_output = root / "unchanged.outputs"
            unchanged_diagnostic = root / "unchanged"
            unchanged_result = run_diagnostic(
                baseline, baseline, unchanged_output, unchanged_diagnostic
            )
            self.assertEqual(unchanged_result.returncode, 0, unchanged_result.stderr)
            self.assertIn("schema_changed=false", unchanged_output.read_text(encoding="ascii"))
            self.assertFalse(unchanged_diagnostic.exists())

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
            "release_profile": "release",
            "release_lto": False,
            "release_codegen_units": 4,
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
            "build_run_api_sha256": "e" * 64,
            "build_pr_association_sha256": "a" * 64,
            "build_pr_association_source": "current-api",
            "build_artifact_id": "34567",
            "build_artifact_sha256": "c" * 64,
            "security_workflow_name": "Platform security and build",
            "security_workflow_path": ".github/workflows/platform-security.yml",
            "security_run_id": "23456",
            "security_run_attempt": "1",
            "security_run_api_sha256": "f" * 64,
            "security_pr_association_sha256": "b" * 64,
            "security_pr_association_source": "current-api",
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

    def test_activation_receipt_requires_association_hashes_and_sources(self) -> None:
        thread_id = "17171717-1717-4717-8717-171717171717"
        rollout = self.insert_thread(thread_id)
        self.write_rollout(rollout)
        original = json.loads(self.activation.read_text(encoding="utf-8"))
        for field, value in (
            ("build_run_api_sha256", "bad"),
            ("security_run_api_sha256", "0" * 63),
            ("build_pr_association_sha256", "bad"),
            ("security_pr_association_sha256", "0" * 63),
            ("build_pr_association_source", "caller-authored"),
            ("security_pr_association_source", None),
        ):
            changed = dict(original)
            changed[field] = value
            self.activation.write_text(json.dumps(changed), encoding="utf-8")
            with self.subTest(field=field), self.assertRaises(cleanup.UnsafeState):
                self.run_retention()
            self.assertEqual(self.call_lines(), [])
            self.assertTrue(rollout.exists())
        self.activation.write_text(json.dumps(original), encoding="utf-8")

    def test_runtime_rejects_companion_with_unreviewed_release_lto(self) -> None:
        for invalid_lto in (True, 0):
            manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
            manifest["release_lto"] = invalid_lto
            self.manifest.write_text(json.dumps(manifest), encoding="utf-8")

            with self.subTest(release_lto=invalid_lto), self.assertRaises(cleanup.UnsafeState):
                self.run_retention()

            self.assertEqual(self.call_lines(), [])

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
            "release_profile": "release",
            "release_lto": False,
            "release_codegen_units": 4,
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

        for field, invalid_value in (
            ("workflow_run_attempt", "2"),
            ("release_profile", "dev"),
            ("release_lto", True),
            ("release_lto", 0),
            ("release_codegen_units", 1),
            ("release_codegen_units", True),
        ):
            invalid_manifest = {**manifest, field: invalid_value}
            with self.subTest(field=field), self.assertRaises(verify_artifact.VerificationError):
                verify_artifact._validate_build_artifact(
                    {
                        "codex": binary,
                        "codex.manifest.json": json.dumps(invalid_manifest).encode(),
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

    def test_verified_extraction_rechecks_identity_and_available_space(self) -> None:
        binary = b"synthetic verified private binary"
        manifest = {
            "binary_bytes": len(binary),
            "binary_sha256": hashlib.sha256(binary).hexdigest(),
            "cli_version": "codex-cli 0.166.0",
        }
        members = {
            "codex": binary,
            "codex.manifest.json": json.dumps(manifest).encode("ascii"),
            "version.txt": b"codex-cli 0.166.0\n",
        }

        bad_identity = self.root / "bad-identity"
        wrong_manifest = {**manifest, "binary_sha256": "0" * 64}
        with self.assertRaises(verify_artifact.VerificationError):
            verify_artifact._extract_verified_build_members(
                bad_identity,
                members,
                manifest=wrong_manifest,
            )
        self.assertFalse(bad_identity.exists())

        no_space = self.root / "no-space"
        with mock.patch.object(
            verify_artifact.os,
            "statvfs",
            return_value=mock.Mock(f_frsize=4096, f_bavail=0),
        ):
            with self.assertRaises(verify_artifact.VerificationError):
                verify_artifact._extract_verified_build_members(
                    no_space,
                    members,
                    manifest=manifest,
                )
        self.assertFalse(no_space.exists())

        payload = self.root / "codex"
        payload.write_bytes(binary)
        install_root = self.root / "install-root"
        install_root.mkdir(mode=0o700)
        block_size = 4096
        total_bytes = 100 * 1024**3
        floor_bytes = 10 * 1024**3
        allocation_bytes = ((len(binary) + block_size - 1) // block_size) * block_size
        allocation_bytes += 2 * block_size
        with mock.patch.object(
            verify_artifact.os,
            "statvfs",
            return_value=mock.Mock(
                f_frsize=block_size,
                f_blocks=total_bytes // block_size,
                f_bavail=(floor_bytes + allocation_bytes) // block_size,
            ),
        ):
            verify_artifact.check_persistent_install_space(install_root, [payload])
        with mock.patch.object(
            verify_artifact.os,
            "statvfs",
            return_value=mock.Mock(
                f_frsize=block_size,
                f_blocks=total_bytes // block_size,
                f_bavail=(floor_bytes + allocation_bytes - 1) // block_size,
            ),
        ):
            with self.assertRaises(verify_artifact.VerificationError):
                verify_artifact.check_persistent_install_space(install_root, [payload])

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

    def test_postmerge_empty_association_requires_exact_open_pr_capture(self) -> None:
        run_id, attempt, pr_number = "23456", "2", "123"
        head, base_sha = "a" * 40, "c" * 40
        run_api = {
            "id": int(run_id),
            "run_attempt": int(attempt),
            "name": verify_artifact.BUILD_WORKFLOW_NAME,
            "path": verify_artifact.BUILD_WORKFLOW_PATH,
            "event": "pull_request",
            "status": "in_progress",
            "conclusion": None,
            "head_sha": head,
            "repository_full_name": verify_artifact.REPOSITORY,
            "head_repository_full_name": verify_artifact.REPOSITORY,
            "pull_requests": [
                {
                    "number": int(pr_number),
                    "head_sha": head,
                    "head_repository_full_name": verify_artifact.REPOSITORY,
                    "base_ref": "dev",
                    "base_sha": base_sha,
                    "base_repository_full_name": verify_artifact.REPOSITORY,
                }
            ],
        }
        pr_api = {
            "number": int(pr_number),
            "state": "open",
            "merged_at": None,
            "head_sha": head,
            "head_repository_full_name": verify_artifact.REPOSITORY,
            "base_ref": "dev",
            "base_sha": base_sha,
            "base_repository_full_name": verify_artifact.REPOSITORY,
        }
        capture = capture_pr_association._capture(
            run_api,
            pr_api,
            run_id=run_id,
            attempt=attempt,
            pr_number=pr_number,
        )
        completed = {
            "id": int(run_id),
            "run_attempt": int(attempt),
            "name": verify_artifact.BUILD_WORKFLOW_NAME,
            "path": verify_artifact.BUILD_WORKFLOW_PATH,
            "event": "pull_request",
            "status": "completed",
            "conclusion": "success",
            "head_sha": head,
            "repository": {"full_name": verify_artifact.REPOSITORY},
            "head_repository": {"full_name": verify_artifact.REPOSITORY},
            "pull_requests": [],
        }
        identity_args = {
            "run_id": run_id,
            "attempt": attempt,
            "workflow_name": verify_artifact.BUILD_WORKFLOW_NAME,
            "workflow_path": verify_artifact.BUILD_WORKFLOW_PATH,
            "expected_head": head,
            "pull_request": pr_number,
        }
        with self.assertRaisesRegex(verify_artifact.VerificationError, "empty"):
            verify_artifact._run_identity(completed, **identity_args)
        identity, source, _ = verify_artifact._run_identity(
            completed, association_capture=capture, **identity_args
        )
        self.assertEqual(identity["conclusion"], "success")
        self.assertEqual(source, "premerge-capture")
        for captured_status in ("queued", "pending", "waiting", "requested", "in_progress"):
            active_capture = {
                **capture,
                "run": {**capture["run"], "status": captured_status, "conclusion": None},
            }
            _, active_source, _ = verify_artifact._run_identity(
                completed, association_capture=active_capture, **identity_args
            )
            self.assertEqual(active_source, "premerge-capture")
        with self.assertRaisesRegex(verify_artifact.VerificationError, "status and conclusion"):
            verify_artifact._run_identity(
                completed,
                association_capture={
                    **capture,
                    "run": {**capture["run"], "status": "pending", "conclusion": "success"},
                },
                **identity_args,
            )
        with self.assertRaisesRegex(verify_artifact.VerificationError, "successful same-repository"):
            verify_artifact._run_identity(
                {**completed, "conclusion": "failure"},
                association_capture=capture,
                **identity_args,
            )

        direct_payload = {**completed, "pull_requests": [{
                "number": int(pr_number),
                "head": {"sha": head, "repo": {"full_name": verify_artifact.REPOSITORY}},
                "base": {"ref": "dev", "sha": base_sha, "repo": {"full_name": verify_artifact.REPOSITORY}},
            }]}
        _, source, _ = verify_artifact._run_identity(
            direct_payload,
            association_capture=capture,
            **identity_args,
        )
        self.assertEqual(source, "current-api+premerge-capture")

        mutations = (
            ("run id", {**capture, "run": {**capture["run"], "id": int(run_id) + 1}}),
            ("attempt", {**capture, "run": {**capture["run"], "run_attempt": 3}}),
            ("workflow", {**capture, "run": {**capture["run"], "name": "other"}}),
            ("workflow path", {**capture, "run": {**capture["run"], "path": "other.yml"}}),
            ("event", {**capture, "run": {**capture["run"], "event": "push"}}),
            ("repository", {**capture, "run": {**capture["run"], "repository": "fork/repo"}}),
            ("head repository", {**capture, "run": {**capture["run"], "head_repository": "fork/repo"}}),
            ("head", {**capture, "run": {**capture["run"], "head_sha": "d" * 40}}),
            ("associated PR head", {**capture, "run": {**capture["run"], "pull_request_head_sha": "d" * 40}}),
            ("associated PR head repository", {**capture, "run": {**capture["run"], "pull_request_head_repository": "fork/repo"}}),
            ("associated PR base ref", {**capture, "run": {**capture["run"], "pull_request_base_ref": "main"}}),
            ("associated PR base repository", {**capture, "run": {**capture["run"], "pull_request_base_repository": "fork/repo"}}),
            ("PR number", {**capture, "pull_request": {**capture["pull_request"], "number": 124}}),
            ("string PR number", {**capture, "pull_request": {**capture["pull_request"], "number": "123"}}),
            ("PR head", {**capture, "pull_request": {**capture["pull_request"], "head_sha": "d" * 40}}),
            ("base ref", {**capture, "pull_request": {**capture["pull_request"], "base_ref": "main"}}),
            ("base", {**capture, "pull_request": {**capture["pull_request"], "base_sha": "e" * 40}}),
            ("base repository", {**capture, "pull_request": {**capture["pull_request"], "base_repository": "fork/repo"}}),
            ("closed PR", {**capture, "pull_request": {**capture["pull_request"], "state": "closed"}}),
            ("merged PR", {**capture, "pull_request": {**capture["pull_request"], "merged_at": "now"}}),
        )
        for label, malformed in mutations:
            with self.subTest(label=label), self.assertRaises(verify_artifact.VerificationError):
                verify_artifact._run_identity(
                    completed, association_capture=malformed, **identity_args
                )
        with self.assertRaisesRegex(verify_artifact.VerificationError, "singular"):
            verify_artifact._run_identity(
                {**completed, "pull_requests": [{}, {}]},
                association_capture=capture,
                **identity_args,
            )
        with self.assertRaisesRegex(verify_artifact.VerificationError, "malformed"):
            verify_artifact._run_identity(
                {**completed, "pull_requests": None},
                association_capture=capture,
                **identity_args,
            )
        with self.assertRaisesRegex(verify_artifact.VerificationError, "conflicts"):
            verify_artifact._run_identity(
                {**direct_payload, "pull_requests": [{
                    "number": int(pr_number),
                    "head": {"sha": head, "repo": {"full_name": verify_artifact.REPOSITORY}},
                    "base": {"ref": "dev", "sha": "f" * 40, "repo": {"full_name": verify_artifact.REPOSITORY}},
                }]},
                association_capture=capture,
                **identity_args,
            )

    def test_authenticated_capture_projects_only_allowlisted_metadata_and_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory(prefix="codex-pr-capture-") as temporary:
            root = Path(temporary)
            run_payload = {
                "id": 23456,
                "run_attempt": 2,
                "name": verify_artifact.BUILD_WORKFLOW_NAME,
                "path": verify_artifact.BUILD_WORKFLOW_PATH,
                "event": "pull_request",
                "status": "in_progress",
                "conclusion": None,
                "head_sha": "a" * 40,
                "repository_full_name": verify_artifact.REPOSITORY,
                "head_repository_full_name": verify_artifact.REPOSITORY,
                "pull_requests": [{
                    "number": 123,
                    "head_sha": "a" * 40,
                    "head_repository_full_name": verify_artifact.REPOSITORY,
                    "base_ref": "dev",
                    "base_sha": "c" * 40,
                    "base_repository_full_name": verify_artifact.REPOSITORY,
                }],
            }
            pr_payload = {
                "number": 123,
                "state": "open",
                "merged_at": None,
                "head_sha": "a" * 40,
                "head_repository_full_name": verify_artifact.REPOSITORY,
                "base_ref": "dev",
                "base_sha": "c" * 40,
                "base_repository_full_name": verify_artifact.REPOSITORY,
            }
            responses = iter((run_payload, pr_payload, run_payload, pr_payload))

            def gh_api(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                if argv[:3] == ["gh", "auth", "status"]:
                    self.assertEqual(argv[-1], "github.com")
                    return subprocess.CompletedProcess(argv, 0, "", "")
                self.assertEqual(argv[:2], ["gh", "api"])
                self.assertIn("--jq", argv)
                return subprocess.CompletedProcess(argv, 0, json.dumps(next(responses)), "")

            output = root / "association.json"
            with mock.patch.object(capture_pr_association.subprocess, "run", side_effect=gh_api):
                capture_pr_association.capture("23456", "2", "123", output)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            rendered = output.read_text(encoding="ascii")
            self.assertNotIn("body", rendered)
            self.assertEqual(
                set(json.loads(rendered)),
                {"schema_version", "run", "pull_request"},
            )
            for status in ("queued", "pending", "waiting", "requested", "in_progress"):
                active_run = {**run_payload, "status": status, "conclusion": None}
                status_capture = capture_pr_association._capture(
                    active_run,
                    pr_payload,
                    run_id="23456",
                    attempt="2",
                    pr_number="123",
                )
                self.assertEqual(status_capture["run"]["status"], status)
            with self.assertRaises(capture_pr_association.CaptureError):
                capture_pr_association._capture(
                    {**run_payload, "status": "pending", "conclusion": "success"},
                    pr_payload,
                    run_id="23456",
                    attempt="2",
                    pr_number="123",
                )
            null_repositories = json.loads(json.dumps(run_payload))
            null_repositories["pull_requests"][0]["head_repository_full_name"] = None
            null_repositories["pull_requests"][0]["base_repository_full_name"] = None
            responses = iter((null_repositories, pr_payload))
            null_output = root / "association-null-nested-repos.json"
            with mock.patch.object(capture_pr_association.subprocess, "run", side_effect=gh_api):
                capture_pr_association.capture("23456", "2", "123", null_output)
            null_capture = json.loads(null_output.read_text(encoding="ascii"))
            self.assertEqual(
                null_capture["run"]["pull_request_head_repository"],
                verify_artifact.REPOSITORY,
            )
            self.assertEqual(
                null_capture["run"]["pull_request_base_repository"],
                verify_artifact.REPOSITORY,
            )
            wrong_repository = json.loads(json.dumps(null_repositories))
            wrong_repository["repository_full_name"] = "fork/repo"
            with self.assertRaises(capture_pr_association.CaptureError):
                capture_pr_association._capture(
                    wrong_repository,
                    pr_payload,
                    run_id="23456",
                    attempt="2",
                    pr_number="123",
                )
            wrong_nested = json.loads(json.dumps(run_payload))
            wrong_nested["pull_requests"][0]["head_repository_full_name"] = "fork/repo"
            with self.assertRaises(capture_pr_association.CaptureError):
                capture_pr_association._capture(
                    wrong_nested,
                    pr_payload,
                    run_id="23456",
                    attempt="2",
                    pr_number="123",
                )
            responses = iter((run_payload, pr_payload))
            with mock.patch.object(capture_pr_association.subprocess, "run", side_effect=gh_api):
                with self.assertRaises(FileExistsError):
                    capture_pr_association.capture("23456", "2", "123", output)

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
            "release_profile": "release",
            "release_lto": False,
            "release_codegen_units": 4,
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

        def run_record(
            run_id: str, attempt: str, name: str, path: str, *, include_pr: bool = True
        ) -> Path:
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
                    {
                        "number": int(pr),
                        "head": {
                            "sha": head,
                            "repo": {"full_name": "StrayForest/old_sparky"},
                        },
                        "base": {
                            "ref": "dev",
                            "sha": "c" * 40,
                            "repo": {"full_name": "StrayForest/old_sparky"},
                        },
                    }
                ] if include_pr else [],
            }
            result = self.root / f"run-{run_id}-{'direct' if include_pr else 'empty'}.json"
            result.write_text(json.dumps(payload), encoding="utf-8")
            return result

        build_name = f"codex-archive-retention-{build_id}-{build_attempt}"
        security_name = f"platform-ci-route-{security_id}-{security_attempt}"
        build_artifacts = artifact_metadata(build_name, build_id, build_attempt, 456, build_zip)
        security_artifacts = artifact_metadata(security_name, security_id, security_attempt, 567, security_zip)
        output = self.root / "verified.activation.json"
        extraction_directory = self.root / "verified-build"
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
            build_pr_association=None,
            security_artifacts=security_artifacts,
            security_pr_association=None,
            security_zip=security_zip,
            extract_directory=extraction_directory,
            activation_output=output,
        )

        verify_artifact.verify(args)

        activation = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(activation["classifier_reason"], verify_artifact.STANDARD_FULL_REASON)
        self.assertEqual(activation["build_artifact_id"], "456")
        self.assertEqual(activation["security_route_artifact_id"], "567")
        self.assertEqual(activation["build_pr_association_source"], "current-api")
        self.assertEqual(activation["security_pr_association_source"], "current-api")
        self.assertEqual(
            activation["build_run_api_sha256"],
            hashlib.sha256(args.build_run.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            activation["security_run_api_sha256"],
            hashlib.sha256(args.security_run.read_bytes()).hexdigest(),
        )
        self.assertEqual(activation["build_artifact_sha256"], hashlib.sha256(build_zip.read_bytes()).hexdigest())
        self.assertEqual(activation["security_route_sha256"], hashlib.sha256(security_zip.read_bytes()).hexdigest())
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        self.assertEqual((extraction_directory / "codex").read_bytes(), binary)
        self.assertEqual(
            set(path.name for path in extraction_directory.iterdir()),
            {"codex", "codex.manifest.json", "version.txt"},
        )
        self.assertEqual(stat.S_IMODE(extraction_directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((extraction_directory / "codex").stat().st_mode), 0o600)

        def association_snapshot(run_path: Path, *, run_id: str, attempt: str) -> Path:
            current = json.loads(run_path.read_text(encoding="utf-8"))
            association = current["pull_requests"][0]
            run_api = {
                "id": current["id"],
                "run_attempt": current["run_attempt"],
                "name": current["name"],
                "path": current["path"],
                "event": current["event"],
                "status": "in_progress",
                "conclusion": None,
                "head_sha": current["head_sha"],
                "repository_full_name": current["repository"]["full_name"],
                "head_repository_full_name": current["head_repository"]["full_name"],
                "pull_requests": [
                    {
                        "number": association["number"],
                        "head_sha": association["head"]["sha"],
                        "head_repository_full_name": association["head"]["repo"]["full_name"],
                        "base_ref": association["base"]["ref"],
                        "base_sha": association["base"]["sha"],
                        "base_repository_full_name": association["base"]["repo"]["full_name"],
                    }
                ],
            }
            pr_api = {
                "number": int(pr),
                "state": "open",
                "merged_at": None,
                "head_sha": head,
                "head_repository_full_name": "StrayForest/old_sparky",
                "base_ref": "dev",
                "base_sha": "c" * 40,
                "base_repository_full_name": "StrayForest/old_sparky",
            }
            snapshot = capture_pr_association._capture(
                run_api,
                pr_api,
                run_id=run_id,
                attempt=attempt,
                pr_number=pr,
            )
            path = self.root / f"capture-{run_id}-{attempt}.json"
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            path.chmod(0o600)
            return path

        args.build_pr_association = association_snapshot(
            args.build_run,
            run_id=build_id,
            attempt=build_attempt,
        )
        args.security_pr_association = association_snapshot(
            args.security_run,
            run_id=security_id,
            attempt=security_attempt,
        )
        direct_capture_output = self.root / "verified-direct-capture.activation.json"
        args.activation_output = direct_capture_output
        args.extract_directory = None
        verify_artifact.verify(args)
        direct_capture_activation = json.loads(
            direct_capture_output.read_text(encoding="utf-8")
        )
        self.assertEqual(
            direct_capture_activation["build_pr_association_source"],
            "current-api+premerge-capture",
        )
        self.assertEqual(
            direct_capture_activation["build_run_api_sha256"],
            hashlib.sha256(args.build_run.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            direct_capture_activation["security_run_api_sha256"],
            hashlib.sha256(args.security_run.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            direct_capture_activation["build_pr_association_sha256"],
            hashlib.sha256(args.build_pr_association.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            direct_capture_activation["security_pr_association_sha256"],
            hashlib.sha256(args.security_pr_association.read_bytes()).hexdigest(),
        )

        fallback_output = self.root / "verified-after-merge.activation.json"
        args.build_run = run_record(
            build_id,
            build_attempt,
            verify_artifact.BUILD_WORKFLOW_NAME,
            verify_artifact.BUILD_WORKFLOW_PATH,
            include_pr=False,
        )
        args.security_run = run_record(
            security_id,
            security_attempt,
            verify_artifact.SECURITY_WORKFLOW_NAME,
            verify_artifact.SECURITY_WORKFLOW_PATH,
            include_pr=False,
        )
        args.build_pr_association = association_snapshot(
            run_record(build_id, build_attempt, verify_artifact.BUILD_WORKFLOW_NAME, verify_artifact.BUILD_WORKFLOW_PATH),
            run_id=build_id,
            attempt=build_attempt,
        )
        args.security_pr_association = association_snapshot(
            run_record(security_id, security_attempt, verify_artifact.SECURITY_WORKFLOW_NAME, verify_artifact.SECURITY_WORKFLOW_PATH),
            run_id=security_id,
            attempt=security_attempt,
        )
        args.activation_output = fallback_output
        args.extract_directory = None
        original_security_capture = args.security_pr_association.read_bytes()
        changed_security_capture = json.loads(original_security_capture)
        changed_security_capture["run"]["pull_request_base_sha"] = "d" * 40
        changed_security_capture["pull_request"]["base_sha"] = "d" * 40
        args.security_pr_association.write_text(
            json.dumps(changed_security_capture), encoding="utf-8"
        )
        with self.assertRaisesRegex(
            verify_artifact.VerificationError, "do not share one PR identity"
        ):
            verify_artifact.verify(args)
        self.assertFalse(fallback_output.exists())
        args.security_pr_association.write_bytes(original_security_capture)
        verify_artifact.verify(args)
        fallback_activation = json.loads(fallback_output.read_text(encoding="utf-8"))
        self.assertEqual(fallback_activation["build_pr_association_source"], "premerge-capture")
        self.assertEqual(fallback_activation["security_pr_association_source"], "premerge-capture")
        self.assertEqual(
            fallback_activation["build_run_api_sha256"],
            hashlib.sha256(args.build_run.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            fallback_activation["security_run_api_sha256"],
            hashlib.sha256(args.security_run.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            fallback_activation["build_pr_association_sha256"],
            hashlib.sha256(args.build_pr_association.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            fallback_activation["security_pr_association_sha256"],
            hashlib.sha256(args.security_pr_association.read_bytes()).hexdigest(),
        )

        existing_output = self.root / "preexisting.activation.json"
        existing_output.write_text("preserve existing receipt", encoding="ascii")
        blocked_extraction = self.root / "blocked-build"
        args.activation_output = existing_output
        args.extract_directory = blocked_extraction
        with self.assertRaises(FileExistsError):
            verify_artifact.verify(args)
        self.assertEqual(
            existing_output.read_text(encoding="ascii"),
            "preserve existing receipt",
        )
        self.assertFalse(blocked_extraction.exists())

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
        self.activation = fallback_output
        thread_id = "abababab-abab-4bab-8bab-abababababab"
        rollout = self.insert_thread(thread_id)
        self.write_rollout(rollout)
        self.assertEqual(self.run_retention(), 0)
        self.assertEqual(self.call_lines(), [f"delete --force --archived-only {thread_id}"])
        self.assertFalse(rollout.exists())


if __name__ == "__main__":
    unittest.main()
