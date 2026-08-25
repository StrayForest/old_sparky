from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from python_packages.platform_infra.models import MediaAsset


class MediaCleanupRequired(RuntimeError):
    def __init__(self, status_counts: dict[str, int]) -> None:
        self.status_counts = dict(sorted(status_counts.items()))
        count = sum(self.status_counts.values())
        super().__init__(f"{count} media asset(s) must finish durable cleanup before hard delete")


MEDIA_QUERY_CHUNK_SIZE = 10_000


def _chunks(values: tuple[str, ...]) -> Iterable[tuple[str, ...]]:
    for start in range(0, len(values), MEDIA_QUERY_CHUNK_SIZE):
        yield values[start : start + MEDIA_QUERY_CHUNK_SIZE]


async def purge_deleted_media_metadata(
    db_session: AsyncSession,
    *,
    owner_user_ids: Iterable[str] = (),
    tournament_ids: Iterable[str] = (),
) -> int:
    """Remove only metadata whose R2 cleanup was durably acknowledged.

    `MediaService` deletes every deterministic object key before it changes an
    asset to `deleted`. Any other status is therefore a hard-delete blocker,
    not an invitation to orphan public objects.
    """

    user_ids = tuple(dict.fromkeys(str(value) for value in owner_user_ids if value))
    scoped_tournament_ids = tuple(
        dict.fromkeys(str(value) for value in tournament_ids if value)
    )
    if not user_ids and not scoped_tournament_ids:
        return 0

    # Keep every statement comfortably below asyncpg's 32,767 bind-parameter
    # limit. Querying the two scopes separately preserves the OR semantics and
    # avoids a cartesian product when both collections are large. A media row
    # matching both scopes is deduplicated by its primary key.
    rows_by_id: dict[str, str] = {}
    for column, values in (
        (MediaAsset.owner_user_id, user_ids),
        (MediaAsset.tournament_id, scoped_tournament_ids),
    ):
        for chunk in _chunks(values):
            rows = (
                await db_session.execute(
                    select(MediaAsset.id, MediaAsset.status)
                    .where(column.in_(chunk))
                    .with_for_update()
                )
            ).all()
            for row in rows:
                rows_by_id.setdefault(str(row.id), str(row.status))

    blockers = Counter(status for status in rows_by_id.values() if status != "deleted")
    if blockers:
        raise MediaCleanupRequired(dict(blockers))
    asset_ids = list(rows_by_id)
    if not asset_ids:
        return 0

    deleted = 0
    for chunk in _chunks(tuple(asset_ids)):
        result = await db_session.execute(delete(MediaAsset).where(MediaAsset.id.in_(chunk)))
        deleted += int(result.rowcount or 0)
    await db_session.flush()
    return deleted
