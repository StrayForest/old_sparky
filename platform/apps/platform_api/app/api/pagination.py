from __future__ import annotations

from fastapi import Response


TOURNAMENT_LIST_DEFAULT_LIMIT = 50
TOURNAMENT_LIST_MAX_LIMIT = 100
PARTICIPANT_LIST_DEFAULT_LIMIT = 100
PARTICIPANT_LIST_MAX_LIMIT = 500


def set_pagination_headers(
    response: Response,
    *,
    total: int,
    limit: int,
    offset: int,
    returned: int,
) -> None:
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)
    response.headers["X-Has-More"] = str(offset + returned < total).lower()
