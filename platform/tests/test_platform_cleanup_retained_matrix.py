from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock
from uuid import uuid4


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "tools" / "platform_cleanup_retained_matrix.py"
)
SPEC = importlib.util.spec_from_file_location("platform_cleanup_retained_matrix_tested", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
cleanup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cleanup)


class RetainedMatrixManifestTests(unittest.TestCase):
    def test_read_model_cleanup_is_exact_and_verifies_no_projection_remains(self) -> None:
        class RedisStub:
            def __init__(self, remaining: int = 0):
                self.deleted_keys: tuple[str, ...] = ()
                self.remaining = remaining

            async def delete(self, *keys: str) -> int:
                self.deleted_keys = tuple(keys)
                return len(keys)

            async def exists(self, *keys: str) -> int:
                return self.remaining

        tournament_id = "00000000-0000-0000-0000-000000000002"
        tournament_ids = {tournament_id}
        redis_stub = RedisStub()
        with mock.patch.object(cleanup, "redis_client", return_value=redis_stub):
            result = asyncio.run(cleanup._delete_and_verify_read_models(tournament_ids))

        self.assertEqual(result["keys_expected"], 4)
        self.assertEqual(result["keys_deleted"], 4)
        self.assertEqual(result["keys_remaining"], 0)
        self.assertEqual(
            redis_stub.deleted_keys,
            tuple(
                cleanup.read_model_key(tournament_id, model)
                for model in cleanup.READ_MODEL_KINDS
            ),
        )

    def test_read_model_cleanup_fails_closed_when_a_projection_survives(self) -> None:
        class RedisStub:
            async def delete(self, *_keys: str) -> int:
                return 4

            async def exists(self, *_keys: str) -> int:
                return 1

        with mock.patch.object(cleanup, "redis_client", return_value=RedisStub()):
            with self.assertRaisesRegex(RuntimeError, "Redis read-model"):
                asyncio.run(
                    cleanup._delete_and_verify_read_models(
                        {"00000000-0000-0000-0000-000000000003"}
                    )
                )

    def test_write_burst_timeout_recovery_requires_marker_and_synthetic_owner(self) -> None:
        marker = "preprod260824120000abcd"
        user_id = str(uuid4())
        row = {"marker": marker, "tournament_ids": []}
        candidate = type(
            "Candidate",
            (),
            {
                "id": str(uuid4()),
                "description": f"Write burst profile {marker} ready_5s.",
                "organizer_user_id": user_id,
            },
        )()

        recovered = cleanup._merge_recovered_marker_tournaments(
            row,
            [candidate],
            user_ids={user_id},
            mode="write-burst",
        )

        self.assertEqual(recovered, {candidate.id})
        self.assertEqual(row["tournament_ids"], [candidate.id])

    def _write_manifest(self, root: Path, report_path: Path) -> Path:
        marker = "preprod260824120000abcd"
        user_id = str(uuid4())
        tournament_id = str(uuid4())
        report = {
            "marker": marker,
            "report_path": str(report_path),
            "mode": "scale",
            "origin": "https://old-sparky.com",
            "user_ids": [user_id],
            "tournament_ids": [tournament_id],
            "tournament_visibility": "public",
        }
        report_path.write_text(json.dumps(report), encoding="utf-8")
        summary_path = root / "matrix-summary.json"
        summary_path.write_text(
            json.dumps(
                {
                    "control_email": "qa@example.invalid",
                    "completed_tournaments": 1,
                    "rows": [
                        {
                            "synthetic_users": 1,
                            "report_path": str(report_path),
                            "result": {
                                "marker": marker,
                                "report_path": str(report_path),
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return summary_path

    def test_path_escape_is_rejected_before_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            summary_path = root / "matrix-summary.json"
            report_path = root.parent / "outside.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "control_email": "qa@example.invalid",
                        "completed_tournaments": 1,
                        "rows": [
                            {
                                "synthetic_users": 0,
                                "result": {
                                    "marker": "preprod260824120000abcd",
                                    "report_path": str(report_path),
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            summary_path.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "escapes"):
                cleanup.load_matrix_manifest(
                    summary_path,
                    run_root=root,
                    expected_control_email="qa@example.invalid",
                )

    @unittest.skipUnless(os.geteuid() == 0, "permission repair requires the root test user")
    def test_manifest_permission_repair_is_root_only_and_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_path = root / "batch" / "report.json"
            report_path.parent.mkdir()
            summary_path = self._write_manifest(root, report_path)

            summary_path.chmod(0o644)
            report_path.chmod(0o644)
            manifest = cleanup.load_matrix_manifest(
                summary_path,
                run_root=root,
                expected_control_email="qa@example.invalid",
                repair_permissions=True,
            )

            self.assertEqual(len(manifest["user_ids"]), 1)
            self.assertEqual(stat.S_IMODE(summary_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(report_path.stat().st_mode), 0o600)

    def test_valid_manifest_is_identity_bound_to_each_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_path = root / "batch" / "report.json"
            report_path.parent.mkdir()
            summary_path = self._write_manifest(root, report_path)

            def trusted_file(path: Path, *, root: Path, **_: object) -> Path:
                resolved = path.resolve()
                resolved.relative_to(root.resolve())
                return resolved

            with mock.patch.object(cleanup, "_regular_root_file", side_effect=trusted_file):
                manifest = cleanup.load_matrix_manifest(
                    summary_path,
                    run_root=root,
                    expected_control_email="qa@example.invalid",
                )
            self.assertEqual(len(manifest["markers"]), 1)
            self.assertEqual(len(manifest["user_ids"]), 1)
            self.assertEqual(len(manifest["tournament_ids"]), 1)

    @unittest.skipUnless(os.geteuid() == 0, "root-owned file contract")
    def test_read_mix_manifest_allows_tournament_id_recovery_after_gateway_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_path = root / "read-mix" / "read-mix.json"
            report_path.parent.mkdir()
            marker = "preprod260824120000abcd"
            user_id = str(uuid4())
            report_path.write_text(
                json.dumps(
                    {
                        "marker": marker,
                        "report_path": str(report_path),
                        "mode": "read-mix",
                        "origin": "https://old-sparky.com",
                        "user_ids": [user_id],
                        "tournament_ids": [],
                        "tournament_visibility": "public",
                    }
                ),
                encoding="utf-8",
            )
            summary_path = root / "matrix-summary.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "mode": "read-mix",
                        "control_email": "qa@example.invalid",
                        "completed_tournaments": 0,
                        "rows": [
                            {
                                "synthetic_users": 1,
                                "report_path": str(report_path),
                                "result": {
                                    "marker": marker,
                                    "report_path": str(report_path),
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            report_path.chmod(0o600)
            summary_path.chmod(0o600)

            manifest = cleanup.load_matrix_manifest(
                summary_path,
                run_root=root,
                expected_control_email="qa@example.invalid",
            )

            self.assertEqual(manifest["mode"], "read-mix")
            self.assertEqual(manifest["user_ids"], {user_id})
            self.assertEqual(manifest["tournament_ids"], set())

    def test_already_cleaned_state_is_only_an_artifact_cleanup_result(self) -> None:
        report_path = "/opt/oldsparky/platform/shared/production-retained-matrix/gha-12345/read-mix/read-mix.json"
        marker = "preprod260824120000abcd"
        user_id = "00000000-0000-0000-0000-000000000001"
        manifest = {
            "markers": {marker},
            "_control_email": "control@example.com",
            "user_ids": {user_id},
            "tournament_ids": set(),
            "rows": [{"marker": marker, "report_path": report_path}],
        }
        run = type(
            "Run",
            (),
            {
                "marker": marker,
                "origin": cleanup.EXPECTED_ORIGIN,
                "report_path": report_path,
                "report": {"marker": marker, "report_path": report_path},
                "status": "cleaned",
                "cleanup_state": {
                    "ok": True,
                    "cleaned_by": "platform_cleanup_retained_matrix.py",
                    "control_account_preserved": True,
                    "read_models": {
                        "keys_expected": 0,
                        "keys_deleted": 0,
                        "keys_remaining": 0,
                    },
                },
            },
        )()

        class ScalarResult:
            def all(self):
                return [run]

        class Session:
            def __init__(self):
                self.values = iter((0, 1))

            async def scalars(self, statement):
                return ScalarResult()

            async def scalar(self, statement):
                return next(self.values)

        result = asyncio.run(
            cleanup._already_cleaned_manifest_result(Session(), manifest)
        )
        self.assertIsNotNone(result)
        self.assertTrue(result["already_cleaned"])
        self.assertEqual(result["remaining_users"], 0)


if __name__ == "__main__":
    unittest.main()
