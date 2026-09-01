from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from typing import Any

from fastapi import Response


TOURNAMENT_LIST_DEFAULT_LIMIT = 50
TOURNAMENT_LIST_MAX_LIMIT = 100
PARTICIPANT_LIST_DEFAULT_LIMIT = 100
PARTICIPANT_LIST_MAX_LIMIT = 500


class CursorDecodeError(ValueError):
    """Raised when an opaque pagination cursor is malformed."""


def encode_cursor(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        dict(payload),
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).rstrip(b"=").decode("ascii")


def decode_cursor(value: str) -> dict[str, Any]:
    if not value or len(value) > 2048:
        raise CursorDecodeError("Cursor is empty or too long.")
    try:
        padded = value.encode("ascii") + b"=" * (-len(value) % 4)
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
        payload = json.loads(decoded.decode("utf-8"))
    except (UnicodeEncodeError, UnicodeDecodeError, ValueError, TypeError, binascii.Error) as exc:
        raise CursorDecodeError("Cursor is not valid.") from exc
    if not isinstance(payload, dict):
        raise CursorDecodeError("Cursor payload must be an object.")
    return payload


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


def set_cursor_pagination_headers(
    response: Response,
    *,
    limit: int,
    has_more: bool,
    next_cursor: str | None,
) -> None:
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Has-More"] = str(has_more).lower()
    if next_cursor is not None:
        response.headers["X-Next-Cursor"] = next_cursor
