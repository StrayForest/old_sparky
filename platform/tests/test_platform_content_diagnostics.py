from __future__ import annotations

import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

from tools.platform_content_diagnostics import canonical_detail


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "platform/tools/platform_content_diagnostics.py"
PASSING_SUMMARY = (
    "PRODUCTION_PATCH_DISTRIBUTION schema=1 status=passed error_class=none "
    "latest_patch_id=1001 internal_section_count=2 internal_api_section_count=2 "
    "public_api_section_count=2"
)


def _home(*, available: bool = True, patch_id: str = "1001") -> dict[str, object]:
    return {
        "patches_available": available,
        "patches": [{"id": patch_id, "title": "Patch", "published_at": "2026-09-01T00:00:00+00:00"}],
    }


def _detail(*, content: str = "Damage increased") -> dict[str, object]:
    return {
        "id": "1001",
        "title": "Patch 1001",
        "published_at": "2026-09-01T00:00:00+00:00",
        "url": "https://store.steampowered.com/news/app/1422450/view/1001",
        "content": content,
        "sections": [
            {
                "kind": "general",
                "title": "Общие изменения",
                "hero_name": None,
                "item_name": None,
                "item_category": None,
                "item_icon_url": None,
                "objective_key": None,
                "objective_icon_url": None,
                "changes": ["Damage increased"],
                "abilities": [],
            },
            {
                "kind": "hero",
                "title": "Abrams",
                "hero_name": "Abrams",
                "item_name": None,
                "item_category": None,
                "item_icon_url": None,
                "objective_key": None,
                "objective_icon_url": None,
                "changes": ["Health increased"],
                "abilities": [
                    {
                        "name": "Shoulder Charge",
                        "icon_url": "/assets/shoulder.png",
                        "changes": ["Cooldown reduced"],
                    }
                ],
            },
        ],
    }


class PlatformContentDiagnosticsTests(unittest.TestCase):
    @staticmethod
    def _write_inputs(root: Path, *, home: object | None = None, details: list[object] | None = None) -> None:
        (root / "home.json").write_text(
            json.dumps(_home() if home is None else home), encoding="utf-8"
        )
        values = details or [_detail(), _detail(), _detail()]
        for name, value in zip(("internal", "internal-api", "public-api"), values):
            (root / f"{name}.json").write_text(json.dumps(value), encoding="utf-8")

    @staticmethod
    def _run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(TOOL), *args],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_manual_dispatch_requires_exact_expected_sha_and_trusted_source(self) -> None:
        workflow = (
            REPO_ROOT / ".github/workflows/platform-production-content-diagnostics.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("inputs.expected_sha", workflow)
        self.assertIn("required: true", workflow)
        self.assertIn('test "$GITHUB_REF" = "refs/heads/dev"', workflow)
        self.assertIn("platform_workflow_input_guard.py sha", workflow)
        self.assertIn("trusted_source_sha", workflow)
        self.assertIn("--expected-sha \"$target_sha\"", workflow)
        self.assertNotIn("github.event_name == 'workflow_dispatch' && github.sha", workflow)

    def test_verify_subprocess_emits_one_closed_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_inputs(root)
            result = self._run(
                "verify",
                "--home",
                str(root / "home.json"),
                "--internal",
                str(root / "internal.json"),
                "--internal-api",
                str(root / "internal-api.json"),
                "--public-api",
                str(root / "public-api.json"),
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [PASSING_SUMMARY])
        self.assertEqual(result.stderr, "")

    def test_verify_fails_closed_on_stable_field_or_section_parity_mismatch(self) -> None:
        for mutation in ("field", "order", "kind"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                changed = _detail()
                if mutation == "field":
                    changed["content"] = "secretly different content"
                elif mutation == "order":
                    changed["sections"] = list(reversed(changed["sections"]))
                else:
                    changed["sections"][1]["kind"] = "item"
                    changed["sections"][1]["hero_name"] = None
                    changed["sections"][1]["item_name"] = "Abrams"
                    changed["sections"][1]["item_category"] = "weapon"
                    changed["sections"][1]["abilities"] = []
                self._write_inputs(root, details=[_detail(), changed, _detail()])
                result = self._run(
                    "verify",
                    "--home",
                    str(root / "home.json"),
                    "--internal",
                    str(root / "internal.json"),
                    "--internal-api",
                    str(root / "internal-api.json"),
                    "--public-api",
                    str(root / "public-api.json"),
                )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(
                result.stdout.splitlines(),
                [
                    "PRODUCTION_PATCH_DISTRIBUTION schema=1 status=failed "
                    "error_class=parity latest_patch_id=unavailable "
                    "internal_section_count=0 internal_api_section_count=0 "
                    "public_api_section_count=0"
                ],
            )

    def test_verify_rejects_empty_false_status_and_malformed_latest_patch(self) -> None:
        for home in (
            _home(available=False),
            _home(available=True, patch_id=""),
            {"patches_available": True, "patches": []},
        ):
            with self.subTest(home=home), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self._write_inputs(root, home=home)
                result = self._run(
                    "verify",
                    "--home",
                    str(root / "home.json"),
                    "--internal",
                    str(root / "internal.json"),
                    "--internal-api",
                    str(root / "internal-api.json"),
                    "--public-api",
                    str(root / "public-api.json"),
                )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("status=failed error_class=producer", result.stdout)

    def test_summary_parser_rejects_duplicate_malformed_failed_and_nonzero(self) -> None:
        cases = (
            (PASSING_SUMMARY + "\n" + PASSING_SUMMARY, "malformed", 0),
            ("not a production summary", "malformed", 0),
            (PASSING_SUMMARY, "remote_or_transport", 7),
            (
                PASSING_SUMMARY.replace("internal_api_section_count=2", "internal_api_section_count=3"),
                "status",
                0,
            ),
            (
                PASSING_SUMMARY.replace("status=passed error_class=none", "status=failed error_class=producer")
                .replace("latest_patch_id=1001", "latest_patch_id=unavailable")
                .replace("internal_section_count=2", "internal_section_count=0")
                .replace("internal_api_section_count=2", "internal_api_section_count=0")
                .replace("public_api_section_count=2", "public_api_section_count=0"),
                "status",
                0,
            ),
        )
        for text, error_class, exit_code in cases:
            with self.subTest(error_class=error_class), tempfile.TemporaryDirectory() as temporary:
                summary_file = Path(temporary) / "summary.log"
                summary_file.write_text(text, encoding="utf-8")
                result = self._run(
                    "parse-summary",
                    "--file",
                    str(summary_file),
                    "--require-passed",
                    "--exit-code",
                    str(exit_code),
                )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(f"error_class={error_class}", result.stdout)

    def test_evidence_is_closed_sanitized_and_mode_600(self) -> None:
        secret = "Authorization: Bearer secret-token"
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "aggregate.json"
            result = self._run(
                "evidence",
                "--output",
                str(output),
                "--target-sha",
                secret,
                "--event",
                "workflow_dispatch",
                "--run-id",
                "44",
                "--run-attempt",
                "1",
                "--patch-outcome",
                "success",
                "--patch-summary",
                PASSING_SUMMARY,
                "--content-outcome",
                "success",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(
                set(payload),
                {
                    "schema",
                    "kind",
                    "status",
                    "error_class",
                    "event",
                    "run_id",
                    "run_attempt",
                    "target_sha",
                    "patch_distribution_status",
                    "content_status",
                    "latest_patch_id",
                    "internal_section_count",
                    "internal_api_section_count",
                    "public_api_section_count",
                },
            )
            serialized = json.dumps(payload, sort_keys=True)
            self.assertNotIn(secret, serialized)
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["target_sha"], "unavailable")
            valid_output = Path(temporary) / "valid-aggregate.json"
            valid_result = self._run(
                "evidence",
                "--output",
                str(valid_output),
                "--target-sha",
                "a" * 40,
                "--event",
                "workflow_dispatch",
                "--run-id",
                "44",
                "--run-attempt",
                "1",
                "--patch-outcome",
                "success",
                "--patch-summary",
                PASSING_SUMMARY,
                "--content-outcome",
                "success",
            )
            self.assertEqual(valid_result.returncode, 0, valid_result.stderr)
            self.assertEqual(json.loads(valid_output.read_text())["status"], "passed")

    def test_evidence_rejects_symlink_and_unsafe_existing_mode(self) -> None:
        for kind in ("symlink", "mode"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                output = root / "aggregate.json"
                secret = root / "secret.txt"
                secret.write_text("private", encoding="utf-8")
                if kind == "symlink":
                    output.symlink_to(secret)
                else:
                    output.write_text("{}\n", encoding="utf-8")
                    output.chmod(0o644)
                result = self._run(
                    "evidence",
                    "--output",
                    str(output),
                    "--target-sha",
                    "a" * 40,
                    "--event",
                    "workflow_run",
                    "--run-id",
                    "44",
                    "--run-attempt",
                    "1",
                    "--patch-outcome",
                    "success",
                    "--patch-summary",
                    PASSING_SUMMARY,
                    "--content-outcome",
                    "success",
                )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("error_class=artifact", result.stdout)

    def test_canonical_projection_contains_all_stable_public_fields(self) -> None:
        projection = canonical_detail(_detail())
        self.assertEqual(
            set(projection["sections"][0]),
            {
                "kind",
                "title",
                "hero_name",
                "item_name",
                "item_category",
                "item_icon_url",
                "objective_key",
                "objective_icon_url",
                "changes",
                "abilities",
            },
        )
        self.assertEqual(set(projection["sections"][1]["abilities"][0]), {"name", "icon_url", "changes"})


if __name__ == "__main__":
    unittest.main()
