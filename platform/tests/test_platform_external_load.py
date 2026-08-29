from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tools.platform_external_load import (
    ExternalLoadError,
    RequestResult,
    load_manifest,
    spread_offsets,
    summarize_results,
)


def manifest_payload() -> dict[str, object]:
    return {
        "schema": 1,
        "purpose": "external_ready_vote",
        "origin": "https://old-sparky.com",
        "session_cookie_name": "deadlock_platform_session",
        "csrf_cookie_name": "deadlock_platform_session_csrf",
        "marker": "preprod26082900000000ab",
        "tournaments": [
            {"id": "tournament-1", "slug": "qa-tournament", "user_count": 2}
        ],
        "users": [
            {
                "user_id": "user-00000001",
                "tournament_slug": "qa-tournament",
                "session_token": "s" * 64,
                "csrf_token": "c" * 64,
            },
            {
                "user_id": "user-00000002",
                "tournament_slug": "qa-tournament",
                "session_token": "t" * 64,
                "csrf_token": "d" * 64,
            },
        ],
    }


class ExternalLoadTests(unittest.TestCase):
    def test_spread_offsets_are_bounded_and_deterministic(self) -> None:
        self.assertEqual(spread_offsets(5, 10), [0.0, 2.0, 4.0, 6.0, 8.0])
        self.assertEqual(spread_offsets(2, 0), [0.0, 0.0])
        self.assertEqual(spread_offsets(0, 10), [])

    def test_manifest_validates_identity_and_tournament_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(manifest_payload()), encoding="utf-8")
            payload, users = load_manifest(path)

        self.assertEqual(payload["marker"], "preprod26082900000000ab")
        self.assertEqual(len(users), 2)
        self.assertEqual(users[0].tournament_slug, "qa-tournament")

    def test_manifest_accepts_production_host_cookie_names(self) -> None:
        payload = manifest_payload()
        payload["session_cookie_name"] = "__Host-deadlock_platform_session"
        payload["csrf_cookie_name"] = "__Host-deadlock_platform_session_csrf"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            load_manifest(path)

    def test_manifest_rejects_duplicate_session_material(self) -> None:
        payload = manifest_payload()
        users = payload["users"]
        assert isinstance(users, list)
        users[1] = dict(users[1])
        users[1]["session_token"] = users[0]["session_token"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ExternalLoadError):
                load_manifest(path)

    def test_summary_contains_metrics_but_not_session_material(self) -> None:
        secret = "session-secret-that-must-not-be-serialized"
        result = RequestResult(
            phase="write_external_vote",
            method="POST",
            path="/tournaments/qa/deadlock/ready-check/vote",
            status=200,
            elapsed_ms=100.0,
            ok=True,
            response_bytes=120,
            response_json={"changed": True, "internal": secret},
        )
        summary = summarize_results([result])
        serialized = json.dumps(summary)
        self.assertEqual(summary["requests"], 1)
        self.assertEqual(summary["errors"], 0)
        self.assertNotIn(secret, serialized)


if __name__ == "__main__":
    unittest.main()
