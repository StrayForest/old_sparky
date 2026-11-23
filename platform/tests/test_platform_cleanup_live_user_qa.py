from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch


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
    def test_platform_import_root_comes_from_installed_helper_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            platform_root = Path(temporary) / "release" / "platform"
            tools = platform_root / "tools"
            tools.mkdir(parents=True)
            helper = tools / "platform_cleanup_live_user_qa.py"
            helper.write_text("# installed helper\n", encoding="ascii")
            import_root = str(platform_root)
            prior_path = list(sys.path)
            try:
                sys.path[:] = [entry for entry in sys.path if entry != import_root]
                self.assertEqual(
                    MODULE._configure_platform_import_root(helper), platform_root
                )
                self.assertEqual(sys.path[0], import_root)
            finally:
                sys.path[:] = prior_path

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
        self.assertEqual(
            inventory.inventory_sha256,
            hashlib.sha256(self.path.read_bytes()).hexdigest(),
        )
        self.assertEqual(inventory.user_ids, (USER_ID,))
        self.assertEqual(inventory.tournament_ids, (TOURNAMENT_ID,))
        self.assertEqual(inventory.media_ids, ())

    def test_cleanup_publishes_one_query_bound_zero_receipt_and_fails_closed(self) -> None:
        class FakeSession:
            def __init__(self) -> None:
                self.scalar = AsyncMock(return_value=0)
                self.execute = AsyncMock(
                    return_value=SimpleNamespace(rowcount=0, all=lambda: [])
                )
                self.flush = AsyncMock()
                self.commit = AsyncMock()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

        def exercise(*, fail_verification: bool) -> tuple[list[FakeSession], Path]:
            with tempfile.TemporaryDirectory(dir="/root") as temporary:
                state_root = Path(temporary) / "states"
                state_root.mkdir(mode=0o700)
                state_dir = state_root / "live-user-qa.Abc123"
                state_dir.mkdir(mode=0o700)
                inventory_payload = {
                    "version": 1,
                    "marker": MARKER,
                    "user_ids": [USER_ID],
                    "tournament_ids": [],
                    "media_ids": [],
                }
                inventory_path = state_dir / "inventory.json"
                inventory_path.write_text(json.dumps(inventory_payload), encoding="ascii")
                inventory_path.chmod(0o600)
                inventory = MODULE.CleanupInventory(
                    marker=MARKER,
                    user_ids=(USER_ID,),
                    tournament_ids=(),
                    media_ids=(),
                    inventory_sha256=hashlib.sha256(inventory_path.read_bytes()).hexdigest(),
                )
                sessions = [FakeSession(), FakeSession()]
                pending = iter(sessions)

                def session_factory():
                    return lambda: next(pending)

                verifier = AsyncMock(
                    side_effect=(RuntimeError("verification failed") if fail_verification else None),
                    wraps=None if fail_verification else MODULE.verify_absence,
                )
                context = {
                    "state_dir": state_dir,
                    "runner_source_sha": "a" * 40,
                    "app_sha": "b" * 40,
                    "marker_sha256": hashlib.sha256(MARKER.encode("ascii")).hexdigest(),
                    "gate_id_sha256": hashlib.sha256(state_dir.name.encode("ascii")).hexdigest(),
                }
                with (
                    patch.object(MODULE, "get_settings", return_value=SimpleNamespace()),
                    patch.object(MODULE, "_validate_runtime_target"),
                    patch.object(MODULE, "LIVE_QA_STATE_ROOT", state_root),
                    patch.object(MODULE, "_validate_exact_scope", new_callable=AsyncMock),
                    patch.object(MODULE, "_audit_subject_ids_for_scope", new_callable=AsyncMock, return_value={}),
                    patch.object(MODULE, "session_factory", side_effect=session_factory) as session_factory_mock,
                    patch.object(MODULE, "verify_absence", verifier),
                ):
                    if fail_verification:
                        with self.assertRaisesRegex(RuntimeError, "verification failed"):
                            asyncio.run(MODULE.cleanup(
                                inventory,
                                _allow_test_environment=True,
                                absence_proof_context=context,
                            ))
                        self.assertFalse((state_dir / "cleanup-absence.json").exists())
                    else:
                        result = asyncio.run(MODULE.cleanup(
                            inventory,
                            _allow_test_environment=True,
                            absence_proof_context=context,
                        ))
                        self.assertEqual(result["users"], 0)
                        proof_path = state_dir / "cleanup-absence.json"
                        proof_bytes = proof_path.read_bytes()
                        proof = json.loads(proof_bytes)
                        self.assertEqual(proof["schema"], 1)
                        self.assertEqual(proof["status"], "verified_absent")
                        self.assertEqual(proof["runner_source_sha"], context["runner_source_sha"])
                        self.assertEqual(proof["app_sha"], context["app_sha"])
                        self.assertEqual(proof["marker_sha256"], context["marker_sha256"])
                        self.assertEqual(proof["gate_id_sha256"], context["gate_id_sha256"])
                        self.assertEqual(proof["inventory_sha256"], inventory.inventory_sha256)
                        self.assertEqual(set(proof["zero_counts"].values()), {0})
                        self.assertNotIn(USER_ID.encode("ascii"), proof_bytes)
                        self.assertEqual(proof_path.stat().st_mode & 0o777, 0o600)
                        self.assertEqual(proof_path.stat().st_nlink, 1)
                    self.assertEqual(session_factory_mock.call_count, 2)
                    self.assertEqual(verifier.await_count, 1)
                    self.assertIs(verifier.await_args.args[0], sessions[1])
                    if not fail_verification:
                        self.assertEqual(sessions[1].scalar.await_count, 5)
                # Preserve only values needed for assertions after temp teardown.
                return sessions, state_dir

        valid_sessions, _valid_state = exercise(fail_verification=False)
        self.assertEqual(valid_sessions[1].scalar.await_count, 5)
        exercise(fail_verification=True)

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
