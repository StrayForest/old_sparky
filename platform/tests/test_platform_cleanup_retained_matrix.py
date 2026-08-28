from __future__ import annotations

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
                expected_control_email="aleksei.lisitsin1@gmail.com",
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
                    expected_control_email="aleksei.lisitsin1@gmail.com",
                )
            self.assertEqual(len(manifest["markers"]), 1)
            self.assertEqual(len(manifest["user_ids"]), 1)
            self.assertEqual(len(manifest["tournament_ids"]), 1)


if __name__ == "__main__":
    unittest.main()
