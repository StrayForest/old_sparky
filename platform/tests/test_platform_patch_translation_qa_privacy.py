"""Adversarial public-channel checks for translation QA workflows."""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from tools.platform_translation_qa_summary import public_summary


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_ROOT = REPO_ROOT / ".github/workflows"
FORBIDDEN = (
    "alice@example.test",
    "Authorization: Bearer secret-token",
    "Cookie=session=secret-session",
    "198.51.100.42",
    "2001:db8::42",
    "https://private.invalid/invite?token=secret-token",
    "SELECT email FROM users WHERE password='secret-password'",
    "/home/operator/private-report.json",
    "/root/.ssh/id_ed25519",
    "".join(("-----BEGIN ", "OPENSSH ", "PRIVATE ", "KEY-----")),
    "ssh -i /root/.ssh/id_ed25519 operator@example.test",
)


class PlatformPatchTranslationQAPrivacyTests(unittest.TestCase):
    def test_adversarial_metrics_are_bounded_to_closed_schema(self) -> None:
        report = public_summary(
            status="failed",
            error_class="unexpected private exception",
            metrics={
                "patches_checked": "alice@example.test",
                "segments_checked": -4,
                "watched_segments": 10**30,
                "translation_calls": True,
                "max_source_length": "https://private.invalid/?token=secret-token",
                "max_translation_length": 12,
                "raw": "SELECT email FROM users WHERE password='secret-password'",
            },
        )
        serialized = json.dumps(report, sort_keys=True, ensure_ascii=True)
        self.assertEqual(
            set(report),
            {
                "schema",
                "kind",
                "status",
                "error_class",
                "patches_checked",
                "segments_checked",
                "watched_segments",
                "translation_calls",
                "max_source_length",
                "max_translation_length",
            },
        )
        self.assertEqual(report["error_class"], "internal")
        self.assertEqual(report["patches_checked"], 0)
        self.assertEqual(report["watched_segments"], 1_000_000)
        for value in FORBIDDEN:
            self.assertNotIn(value.lower(), serialized.lower())

    def test_workflows_keep_raw_translation_and_journal_private(self) -> None:
        warmup = (WORKFLOW_ROOT / "platform-patch-translation-qa.yml").read_text(
            encoding="utf-8"
        )
        content_diagnostics = (
            WORKFLOW_ROOT / "platform-production-content-diagnostics.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("translation-qa-private", warmup)
        self.assertIn("translation-qa-public", warmup)
        self.assertNotIn("PATCH_TRANSLATION_SAMPLE", warmup)
        self.assertNotIn("PATCH_TRANSLATION_LONGEST", warmup)
        self.assertNotIn("journalctl -u", warmup)
        self.assertNotIn('cat \"$remote_log\"', warmup)
        self.assertNotIn('cat \"$remote_log\"', content_diagnostics)
        self.assertIn('kind": "translation_qa"', warmup)
        self.assertNotIn('kind": "translation_qa"', content_diagnostics)
        self.assertNotIn("translate_patch_to_russian", content_diagnostics)
        self.assertNotIn("MAX_OPENAI_CALLS", content_diagnostics)
        self.assertIn("patches_available", content_diagnostics)
        self.assertIn("platform_content_diagnostics.py", content_diagnostics)
        self.assertIn("--require-passed", content_diagnostics)
        self.assertIn("retention-days: 14", content_diagnostics)
        self.assertFalse(
            (WORKFLOW_ROOT / "platform-production-diagnostics.yml").exists()
        )

    def test_production_translation_owner_is_unique(self) -> None:
        owners: list[str] = []
        for path in sorted((*WORKFLOW_ROOT.glob("*.yml"), *WORKFLOW_ROOT.glob("*.yaml"))):
            source = path.read_text(encoding="utf-8")
            if "translate_patch_to_russian" in source or "MAX_OPENAI_CALLS" in source:
                owners.append(path.name)
        self.assertEqual(owners, ["platform-patch-translation-qa.yml"])


if __name__ == "__main__":
    unittest.main()
