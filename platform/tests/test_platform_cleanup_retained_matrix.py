from __future__ import annotations

import importlib.util
import json
from pathlib import Path
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
    def test_browser_timeout_recovery_requires_exact_marker_and_synthetic_owner(self) -> None:
        marker = "preprod260824120000abcd"
        user_id = str(uuid4())
        row = {"marker": marker, "tournament_ids": []}

        candidate = type(
            "Candidate",
            (),
            {
                "id": str(uuid4()),
                "description": f"Browser polling profile {marker} registration_open.",
                "organizer_user_id": user_id,
            },
        )()
        recovered = cleanup._merge_recovered_browser_tournaments(
            row,
            [candidate],
            user_ids={user_id},
        )

        self.assertEqual(recovered, {candidate.id})
        self.assertEqual(row["tournament_ids"], [candidate.id])

        outsider = type(
            "Candidate",
            (),
            {
                "id": str(uuid4()),
                "description": f"Browser polling profile {marker} bracket_active.",
                "organizer_user_id": str(uuid4()),
            },
        )()
        with self.assertRaisesRegex(RuntimeError, "outside the exact inventory"):
            cleanup._merge_recovered_browser_tournaments(
                {"marker": marker, "tournament_ids": []},
                [outsider],
                user_ids={user_id},
            )

    def test_browser_timeout_recovery_rejects_wrong_marker_description(self) -> None:
        marker = "preprod260824120000abcd"
        candidate = type(
            "Candidate",
            (),
            {
                "id": str(uuid4()),
                "description": f"Browser polling profile {marker} unexpected.",
                "organizer_user_id": str(uuid4()),
            },
        )()
        with self.assertRaisesRegex(RuntimeError, "invalid marker-owned tournament"):
            cleanup._merge_recovered_browser_tournaments(
                {"marker": marker, "tournament_ids": []},
                [candidate],
                user_ids={candidate.organizer_user_id},
            )

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
                    "control_email": "aleksei.lisitsin1@gmail.com",
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
                        "control_email": "aleksei.lisitsin1@gmail.com",
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
                    expected_control_email="aleksei.lisitsin1@gmail.com",
                )

    def test_valid_manifest_is_identity_bound_to_each_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_path = root / "batch" / "report.json"
            report_path.parent.mkdir()
            summary_path = self._write_manifest(root, report_path)

            def trusted_file(path: Path, *, root: Path) -> Path:
                resolved = path.resolve()
                resolved.relative_to(root.resolve())
                return resolved

            with mock.patch.object(cleanup, "_regular_root_file", side_effect=trusted_file):
                manifest = cleanup.load_matrix_manifest(
                    summary_path,
                    run_root=root,
                    expected_control_email="aleksei.lisitsin1@gmail.com",
                )
            self.assertEqual(len(manifest["markers"]), 1)
            self.assertEqual(len(manifest["user_ids"]), 1)
            self.assertEqual(len(manifest["tournament_ids"]), 1)

    def test_browser_polling_manifest_can_finish_before_tournament_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_path = root / "browser" / "browser-polling.json"
            report_path.parent.mkdir()
            marker = "preprod260824120000abcd"
            report_path.write_text(
                json.dumps(
                    {
                        "marker": marker,
                        "report_path": str(report_path),
                        "mode": "browser-polling",
                        "origin": "https://old-sparky.com",
                        "user_ids": [str(uuid4())],
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
                        "mode": "browser-polling",
                        "control_email": "aleksei.lisitsin1@gmail.com",
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

            def trusted_file(path: Path, *, root: Path) -> Path:
                resolved = path.resolve()
                resolved.relative_to(root.resolve())
                return resolved

            with mock.patch.object(cleanup, "_regular_root_file", side_effect=trusted_file):
                manifest = cleanup.load_matrix_manifest(
                    summary_path,
                    run_root=root,
                    expected_control_email="aleksei.lisitsin1@gmail.com",
                )
            self.assertEqual(manifest["tournament_ids"], set())
            self.assertEqual(len(manifest["user_ids"]), 1)

    def test_origin_local_sse_manifest_requires_canonical_request_origin(self) -> None:
        marker = "preprod260825120000abcd"
        user_id = str(uuid4())
        tournament_id = str(uuid4())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_path = root / "sse" / "sse.json"
            report_path.parent.mkdir()
            report_path.write_text(
                json.dumps(
                    {
                        "marker": marker,
                        "report_path": str(report_path),
                        "mode": "sse",
                        "origin": "http://127.0.0.1:8010",
                        "request_origin": "https://old-sparky.com",
                        "user_ids": [user_id],
                        "tournament_ids": [tournament_id],
                    }
                ),
                encoding="utf-8",
            )
            summary_path = root / "matrix-summary.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "mode": "sse",
                        "control_email": "aleksei.lisitsin1@gmail.com",
                        "completed_tournaments": 1,
                        "rows": [
                            {
                                "synthetic_users": 1,
                                "report_path": str(report_path),
                                "result": {"marker": marker, "report_path": str(report_path)},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            report_path.chmod(0o600)
            summary_path.chmod(0o600)

            def trusted_file(path: Path, *, root: Path) -> Path:
                resolved = path.resolve()
                resolved.relative_to(root.resolve())
                return resolved

            with mock.patch.object(cleanup, "_regular_root_file", side_effect=trusted_file):
                manifest = cleanup.load_matrix_manifest(
                    summary_path,
                    run_root=root,
                    expected_control_email="aleksei.lisitsin1@gmail.com",
                )

            self.assertEqual(manifest["mode"], "sse")
            self.assertEqual(manifest["user_ids"], {user_id})

            report_path.write_text(
                report_path.read_text(encoding="utf-8").replace(
                    "https://old-sparky.com", "https://attacker.invalid"
                ),
                encoding="utf-8",
            )
            with mock.patch.object(cleanup, "_regular_root_file", side_effect=trusted_file):
                with self.assertRaisesRegex(ValueError, "canonical production retained-load report"):
                    cleanup.load_matrix_manifest(
                        summary_path,
                        run_root=root,
                        expected_control_email="aleksei.lisitsin1@gmail.com",
                    )


if __name__ == "__main__":
    unittest.main()
