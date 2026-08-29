from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from apps.platform_api.app.services import patch_translation_runtime as runtime
from apps.platform_worker import worker
from python_packages.platform_infra.config import PlatformSettings
from python_packages.platform_infra.models import PatchTranslation


class _SessionContext:
    def __init__(self, session: object) -> None:
        self.session = session

    async def __aenter__(self) -> object:
        return self.session

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _FakeRedis:
    def __init__(self, value: str | None = None) -> None:
        self.value = value
        self.writes: list[tuple[tuple, dict]] = []

    async def get(self, _key: str) -> str | None:
        return self.value

    async def set(self, *args, **kwargs) -> bool:
        self.writes.append((args, kwargs))
        return True

    async def aclose(self) -> None:
        return None


class _FailingRedis(_FakeRedis):
    async def get(self, _key: str) -> str | None:
        raise ConnectionError("redis unavailable")


class _FakeDbSession:
    def __init__(self, record: object) -> None:
        self.record = record
        self.execute_calls: list[object] = []
        self.commit_count = 0

    async def scalar(self, _statement: object) -> object:
        return self.record

    async def execute(self, statement: object) -> SimpleNamespace:
        self.execute_calls.append(statement)
        return SimpleNamespace(rowcount=1)

    async def commit(self) -> None:
        self.commit_count += 1


def _patch_detail() -> dict[str, object]:
    return {
        "id": "123",
        "title": "Patch",
        "sections": [
            {
                "kind": "general",
                "title": "Общие изменения",
                "hero_name": None,
                "changes": ["Damage increased from 10 to 12"],
                "abilities": [],
            }
        ],
    }


class PatchTranslationPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_translation_warms_redis_after_cache_miss(self) -> None:
        patch_detail = _patch_detail()
        segments = runtime.extract_translation_segments(patch_detail)
        translated_segments = [{"id": segments[0]["id"], "text": "Урон увеличен с 10 до 12"}]
        record = SimpleNamespace(
            status=runtime.TRANSLATION_STATUS_COMPLETED,
            translated_segments=translated_segments,
            translated_at=datetime(2026, 8, 29, tzinfo=UTC),
        )
        redis = _FakeRedis()
        db_session = _FakeDbSession(record)
        settings = PlatformSettings(platform_openai_model="test-model")

        with (
            patch.object(runtime, "redis_client", return_value=redis),
            patch.object(
                runtime,
                "session_factory",
                return_value=lambda: _SessionContext(db_session),
            ),
            patch.object(runtime, "_write_translation_cache", new=AsyncMock()) as warm,
        ):
            result = await runtime.get_cached_patch_translation(
                patch_detail,
                settings=settings,
            )

        self.assertEqual(result, {segments[0]["id"]: "Урон увеличен с 10 до 12"})
        warm.assert_awaited_once()
        self.assertEqual(db_session.commit_count, 0)

    async def test_redis_failure_still_falls_back_to_database(self) -> None:
        patch_detail = _patch_detail()
        segments = runtime.extract_translation_segments(patch_detail)
        record = SimpleNamespace(
            status=runtime.TRANSLATION_STATUS_COMPLETED,
            translated_segments=[
                {"id": segments[0]["id"], "text": "Урон увеличен с 10 до 12"}
            ],
            translated_at=datetime(2026, 8, 29, tzinfo=UTC),
        )
        db_session = _FakeDbSession(record)
        settings = PlatformSettings(platform_openai_model="test-model")

        with (
            patch.object(runtime, "redis_client", return_value=_FailingRedis()),
            patch.object(
                runtime,
                "session_factory",
                return_value=lambda: _SessionContext(db_session),
            ),
            patch.object(runtime, "_write_translation_cache", new=AsyncMock()),
        ):
            result = await runtime.get_cached_patch_translation(
                patch_detail,
                settings=settings,
            )

        self.assertEqual(result, {segments[0]["id"]: "Урон увеличен с 10 до 12"})

    async def test_pending_database_translation_is_not_used_as_completed(self) -> None:
        patch_detail = _patch_detail()
        record = SimpleNamespace(
            status=runtime.TRANSLATION_STATUS_PENDING,
            translated_segments=[],
            translated_at=None,
        )
        redis = _FakeRedis()
        db_session = _FakeDbSession(record)

        with (
            patch.object(runtime, "redis_client", return_value=redis),
            patch.object(
                runtime,
                "session_factory",
                return_value=lambda: _SessionContext(db_session),
            ),
        ):
            result = await runtime.get_cached_patch_translation(patch_detail)

        self.assertIsNone(result)

    async def test_same_source_version_is_enqueued_only_once(self) -> None:
        record = SimpleNamespace(
            id="translation-1",
            status=runtime.TRANSLATION_STATUS_PENDING,
            last_enqueued_at=None,
            processing_started_at=None,
        )
        db_session = _FakeDbSession(record)
        settings = PlatformSettings(platform_openai_model="test-model")
        enqueue = Mock(return_value="celery-task-1")

        with (
            patch.object(runtime, "get_settings", return_value=settings),
            patch.object(
                runtime,
                "session_factory",
                return_value=lambda: _SessionContext(db_session),
            ),
            patch.object(runtime, "_enqueue_translation_task", enqueue),
        ):
            first = await runtime.ensure_patch_translation_records(
                {"123": _patch_detail()}
            )
            second = await runtime.ensure_patch_translation_records(
                {"123": _patch_detail()}
            )

        self.assertEqual(first["enqueued"], 1)
        self.assertEqual(second["enqueued"], 0)
        enqueue.assert_called_once()

    async def test_completed_source_version_is_never_requeued(self) -> None:
        record = SimpleNamespace(
            id="translation-1",
            status=runtime.TRANSLATION_STATUS_COMPLETED,
            last_enqueued_at=None,
            processing_started_at=None,
        )
        db_session = _FakeDbSession(record)
        enqueue = Mock()

        with (
            patch.object(runtime, "session_factory", return_value=lambda: _SessionContext(db_session)),
            patch.object(runtime, "_enqueue_translation_task", enqueue),
        ):
            result = await runtime.ensure_patch_translation_records(
                {"123": _patch_detail()}
            )

        self.assertEqual(result["enqueued"], 0)
        enqueue.assert_not_called()

    async def test_worker_passes_source_hash_to_translation_task(self) -> None:
        patch_detail = _patch_detail()
        translated = {"ok": True, "status": "translated", "patch_id": "123"}

        with (
            patch.object(
                worker,
                "get_patch_detail_source",
                new=AsyncMock(return_value=patch_detail),
            ),
            patch.object(
                worker,
                "get_deadlock_asset_catalog",
                new=AsyncMock(return_value={}),
            ),
            patch.object(
                worker,
                "translate_patch_to_russian",
                new=AsyncMock(return_value=translated),
            ) as translate,
        ):
            result = await worker._run_patch_translation("123", "source-hash")

        self.assertEqual(result, translated)
        translate.assert_awaited_once_with(
            patch_detail,
            {},
            settings=worker.settings,
            expected_source_hash="source-hash",
        )

    async def test_changed_source_does_not_translate_an_old_queue_entry(self) -> None:
        settings = PlatformSettings(platform_openai_model="test-model")
        supersede = AsyncMock()
        patch_detail = _patch_detail()

        with (
            patch.object(runtime, "_mark_translation_superseded", supersede),
            patch.object(runtime, "get_settings", return_value=settings),
        ):
            result = await runtime.translate_patch_to_russian(
                patch_detail,
                {},
                settings=settings,
                expected_source_hash="0" * 64,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], runtime.TRANSLATION_STATUS_SUPERSEDED)
        supersede.assert_awaited_once_with(
            patch_id="123",
            source_hash="0" * 64,
            settings=settings,
        )


class PatchTranslationPersistenceModelTests(unittest.TestCase):
    def test_model_has_durable_identity_and_state_columns(self) -> None:
        table = PatchTranslation.__table__
        self.assertEqual(
            {
                "patch_id",
                "source_hash",
                "locale",
                "translation_version",
                "model",
            },
            {
                column.name
                for constraint in table.constraints
                if constraint.__class__.__name__ == "UniqueConstraint"
                for column in constraint.columns
            },
        )
        self.assertIn("status", table.c)
        self.assertIn("translated_segments", table.c)
        self.assertIn("last_enqueued_at", table.c)
        self.assertIn("processing_started_at", table.c)

    def test_migration_creates_and_removes_translation_table(self) -> None:
        migration_path = (
            Path(__file__).resolve().parents[1]
            / "alembic"
            / "versions"
            / "20260829_0045_patch_translations.py"
        )
        migration_source = migration_path.read_text(encoding="utf-8")
        self.assertIn('revision = "20260829_0045"', migration_source)
        self.assertIn('op.create_table(\n        "patch_translations"', migration_source)
        self.assertIn('op.drop_table("patch_translations", schema="platform")', migration_source)


if __name__ == "__main__":
    unittest.main()
