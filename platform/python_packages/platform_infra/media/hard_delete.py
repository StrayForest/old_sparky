from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from python_packages.platform_infra.models import MediaAsset


class MediaCleanupRequired(RuntimeError):
    def __init__(self, status_counts: dict[str, int]) -> None:
        self.status_counts = dict(sorted(status_counts.items()))
        count = sum(self.status_counts.values())
        super().__init__(f"{count} media asset(s) must finish durable cleanup before hard delete")


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
    filters = []
    if user_ids:
        filters.append(MediaAsset.owner_user_id.in_(user_ids))
    if scoped_tournament_ids:
        filters.append(MediaAsset.tournament_id.in_(scoped_tournament_ids))
    if not filters:
        return 0

    rows = (
        await db_session.execute(
            select(MediaAsset.id, MediaAsset.status)
            .where(or_(*filters))
            .with_for_update()
        )
    ).all()
    blockers = Counter(str(row.status) for row in rows if row.status != "deleted")
    if blockers:
        raise MediaCleanupRequired(dict(blockers))
    asset_ids = [str(row.id) for row in rows]
    if not asset_ids:
        return 0
    result = await db_session.execute(delete(MediaAsset).where(MediaAsset.id.in_(asset_ids)))
    await db_session.flush()
    return int(result.rowcount or 0)
