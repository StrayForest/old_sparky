from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "tools" / "platform_cleanup_live_user_qa.py"
)
SPEC = importlib.util.spec_from_file_location("platform_cleanup_live_user_qa", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

MARKER = "liveqa-20260809-csp"
USER_ID = "00000000-0000-4000-8000-000000000001"
TOURNAMENT_ID = "00000000-0000-4000-8000-000000000002"
ORIGINAL_LSTAT = Path.lstat


class CleanupLiveUserQaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "inventory.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_inventory(self, **updates: object) -> None:
        payload: dict[str, object] = {
            "version": 1,
            "marker": MARKER,
            "user_ids": [USER_ID],
            "tournament_ids": [TOURNAMENT_ID],
            "media_ids": [],
        }
        payload.update(updates)
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        self.path.chmod(0o600)

    def load(self, path: Path | None = None, *, owner_uid: int = 0):
        target_path = path or self.path

        def metadata():
            actual = ORIGINAL_LSTAT(target_path)
            return SimpleNamespace(
                st_mode=actual.st_mode,
                st_size=actual.st_size,
                st_uid=owner_uid,
            )

        with patch.object(Path, "lstat", side_effect=metadata):
            return MODULE.load_inventory(target_path, expected_marker=MARKER)

    def test_exact_root_only_inventory_is_loaded(self) -> None:
        self.write_inventory()

        inventory = self.load()

        self.assertEqual(inventory.marker, MARKER)
        self.assertEqual(inventory.user_ids, (USER_ID,))
        self.assertEqual(inventory.tournament_ids, (TOURNAMENT_ID,))
        self.assertEqual(inventory.media_ids, ())

    def test_inventory_rejects_wrong_mode_symlink_and_relative_path(self) -> None:
        self.write_inventory()
        self.path.chmod(0o640)
        with self.assertRaisesRegex(ValueError, "mode 0600"):
            self.load()

        self.path.chmod(0o600)
        link = self.path.with_name("inventory-link.json")
        link.symlink_to(self.path)
        with self.assertRaisesRegex(ValueError, "not a symlink"):
            self.load(link)

        with self.assertRaisesRegex(ValueError, "must be absolute"):
            MODULE.load_inventory(Path("inventory.json"), expected_marker=MARKER)

        with self.assertRaisesRegex(ValueError, "root-owned"):
            self.load(owner_uid=1000)

    def test_inventory_rejects_marker_schema_and_id_drift(self) -> None:
        self.write_inventory(marker="liveqa-another-marker")
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.load()

        self.write_inventory(extra=True)
        with self.assertRaisesRegex(ValueError, "unexpected schema"):
            self.load()

        self.write_inventory(user_ids=[USER_ID, USER_ID])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.load()

        self.write_inventory(media_ids=["not-a-uuid"])
        with self.assertRaisesRegex(ValueError, "canonical UUID"):
            self.load()

    def test_audit_cleanup_scopes_colliding_ids_by_subject_type(self) -> None:
        filters = MODULE._audit_subject_filters(
            {
                "tournament_deadlock_ready_round": {"7"},
                "tournament_deadlock_captain_round": {"7"},
            }
        )

        rendered = [
            str(predicate.compile(compile_kwargs={"literal_binds": True}))
            for predicate in filters
        ]
        self.assertEqual(len(rendered), 2)
        self.assertTrue(
            any("tournament_deadlock_ready_round" in value for value in rendered)
        )
        self.assertTrue(
            any("tournament_deadlock_captain_round" in value for value in rendered)
        )
        self.assertTrue(all("audit_logs.subject_type" in value for value in rendered))
        self.assertTrue(all("audit_logs.subject_id" in value for value in rendered))

    def test_large_audit_scope_is_chunked_for_database_queries(self) -> None:
        values = {f"00000000-0000-4000-8000-{index:012d}" for index in range(513)}

        chunks = list(MODULE._audit_scope_chunks(values))

        self.assertEqual([len(chunk) for chunk in chunks], [256, 256, 1])
        self.assertEqual(set().union(*chunks), values)

    def test_runtime_target_requires_canonical_production_or_explicit_test(self) -> None:
        with patch.object(MODULE, "validate_platform_settings", return_value=None):
            MODULE._validate_runtime_target(
                SimpleNamespace(
                    platform_environment="test",
                    platform_web_origin="http://127.0.0.1:3000",
                ),
                allow_test_environment=True,
            )
            with self.assertRaisesRegex(RuntimeError, "outside production"):
                MODULE._validate_runtime_target(
                    SimpleNamespace(
                        platform_environment="development",
                        platform_web_origin="https://old-sparky.com",
                    ),
                    allow_test_environment=False,
                )
            with self.assertRaisesRegex(RuntimeError, "canonical production"):
                MODULE._validate_runtime_target(
                    SimpleNamespace(
                        platform_environment="production",
                        platform_web_origin="https://attacker.example",
                    ),
                    allow_test_environment=False,
                )


if __name__ == "__main__":
    unittest.main()
