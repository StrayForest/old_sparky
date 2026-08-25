from __future__ import annotations

from types import SimpleNamespace
import unittest

from sqlalchemy.sql import Delete, Select

from python_packages.platform_infra.media.hard_delete import (
    MEDIA_QUERY_CHUNK_SIZE,
    purge_deleted_media_metadata,
)


class _Rows:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    def all(self) -> list[SimpleNamespace]:
        return self._rows


class _Result:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _ChunkRecordingSession:
    def __init__(self, total_rows: int) -> None:
        self.total_rows = total_rows
        self.select_calls = 0
        self.delete_calls = 0
        self.flushed = False

    async def execute(self, statement):
        if isinstance(statement, Select):
            self.select_calls += 1
            start = (self.select_calls - 1) * MEDIA_QUERY_CHUNK_SIZE
            end = min(self.total_rows, start + MEDIA_QUERY_CHUNK_SIZE)
            return _Rows(
                [
                    SimpleNamespace(id=f"asset-{index}", status="deleted")
                    for index in range(start, end)
                ]
            )
        if isinstance(statement, Delete):
            self.delete_calls += 1
            start = (self.delete_calls - 1) * MEDIA_QUERY_CHUNK_SIZE
            remaining = max(0, self.total_rows - start)
            return _Result(min(MEDIA_QUERY_CHUNK_SIZE, remaining))
        raise AssertionError(f"unexpected statement: {statement!r}")

    async def flush(self) -> None:
        self.flushed = True


class PlatformMediaHardDeleteTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_owner_scope_stays_below_asyncpg_argument_limit(self) -> None:
        total_rows = MEDIA_QUERY_CHUNK_SIZE * 2 + 1
        session = _ChunkRecordingSession(total_rows)
        user_ids = [f"user-{index}" for index in range(total_rows)]

        deleted = await purge_deleted_media_metadata(
            session,
            owner_user_ids=user_ids,
        )

        self.assertEqual(session.select_calls, 3)
        self.assertEqual(session.delete_calls, 3)
        self.assertEqual(deleted, total_rows)
        self.assertTrue(session.flushed)


if __name__ == "__main__":
    unittest.main()
