from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from tools import platform_web_hermetic_matrix as matrix
from tools import platform_web_hermetic_timing as timing


class PlatformWebHermeticMatrixTests(unittest.TestCase):
    def test_listing_digest_is_order_independent_and_covers_all_tuple_fields(self) -> None:
        entries = [
            matrix.ListedTest("desktop", "a.spec.ts", "alpha"),
            matrix.ListedTest("mobile-layout", "a.spec.ts", "alpha"),
        ]
        reversed_entries = list(reversed(entries))
        self.assertEqual(matrix.listing_digest(entries), matrix.listing_digest(reversed_entries))
        self.assertNotEqual(
            matrix.listing_digest(entries),
            matrix.listing_digest(
                [matrix.ListedTest("desktop", "a.spec.ts", "beta"), entries[1]]
            ),
        )

    def test_duplicate_file_title_project_is_rejected_before_digest_acceptance(self) -> None:
        entry = matrix.ListedTest("desktop", "a.spec.ts", "alpha")
        with self.assertRaisesRegex(RuntimeError, "duplicate Playwright test ownership"):
            matrix.validate_listing("playwright.config.ts", [entry, entry])

    def test_matrix_contract_keeps_all_three_playwright_contours_pinned(self) -> None:
        self.assertEqual(
            set(matrix.EXPECTED_LISTINGS),
            {
                "playwright.config.ts",
                "playwright.source-contract.config.ts",
                "playwright.participant.config.ts",
            },
        )
        for expected in matrix.EXPECTED_LISTINGS.values():
            self.assertGreater(expected["count"], 0)
            self.assertRegex(expected["digest"], r"^[0-9a-f]{64}$")

    def test_runner_fails_closed_on_missing_ci_timing_destination(self) -> None:
        runner = (
            Path(__file__).resolve().parents[1]
            / "tools"
            / "platform_web_hermetic.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('GITHUB_ACTIONS:-', runner)
        self.assertIn("missing-output-path", runner)
        self.assertIn("timing_status=$?", runner)
        self.assertNotIn("|| true", runner)

    def test_timing_writer_emits_green_summary_even_when_a_phase_has_no_reporter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            phase_log = root / "phases.tsv"
            phase_log.write_text("build\t10\t25\t0\n", encoding="utf-8")
            output = root / "summary.json"
            timing.write_summary(
                output=output,
                phase_log=phase_log,
                exit_status=0,
                playwright_runs=[("source-contract", root / "missing.json")],
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "passed")
            self.assertEqual(payload["phase_count"], 1)
            self.assertEqual(payload["playwright"][0]["status"], "missing")
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
