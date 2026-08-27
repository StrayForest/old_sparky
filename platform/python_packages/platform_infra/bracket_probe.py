from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import hmac
import json
from typing import Literal

from python_packages.platform_infra.config import get_settings
from python_packages.platform_infra.security import session_token_digest


BRACKET_PROBE_PREFIX = "platform-bracket-probe-v1"
BRACKET_PROBE_TTL_SECONDS = 900
BRACKET_PROBE_CLOCK_SKEW_SECONDS = 5
BracketProbeStatus = Literal["pending", "teams_ready", "ready"]


class BracketProbeInvalid(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class BracketProbeTicket:
    tournament_id: str
    slug: str
    revision: int
    bracket_status: BracketProbeStatus
    user_id: str | None
    session_id: str | None
    issued_at: int
    expires_at: int


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    if not value or len(value) > 8192:
        raise BracketProbeInvalid("Bracket probe ticket is malformed.")
    try:
        return base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise BracketProbeInvalid("Bracket probe ticket is malformed.") from exc


def _signature(payload_segment: str) -> str:
    settings = get_settings()
    digest = hmac.new(
        settings.platform_secret_key.encode("utf-8"),
        f"{BRACKET_PROBE_PREFIX}.{payload_segment}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    return _encode(digest)


def issue_bracket_probe_ticket(
    *,
    tournament_id: str,
    slug: str,
    revision: int,
    bracket_status: BracketProbeStatus,
    user_id: str | None = None,
    session_id: str | None = None,
    session_token: str | None = None,
    now: datetime | None = None,
) -> str:
    binding = (user_id, session_id, session_token)
    if any(value is not None for value in binding) and not all(binding):
        raise ValueError("Authenticated bracket probe tickets require complete session binding.")
    issued_at = int((now or datetime.now(UTC)).timestamp())
    payload: dict[str, object] = {
        "v": 1,
        "tournament_id": str(tournament_id),
        "slug": str(slug),
        "revision": max(0, int(revision)),
        "status": bracket_status,
        "iat": issued_at,
        "exp": issued_at + BRACKET_PROBE_TTL_SECONDS,
    }
    if all(binding):
        payload.update(
            {
                "user_id": str(user_id),
                "session_id": str(session_id),
                "session_digest": session_token_digest(str(session_token)),
            }
        )
    payload_segment = _encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    return f"{payload_segment}.{_signature(payload_segment)}"


def verify_bracket_probe_ticket(
    ticket: str,
    *,
    expected_slug: str,
    session_token: str | None = None,
    now: datetime | None = None,
) -> BracketProbeTicket:
    if not isinstance(ticket, str) or len(ticket) > 16384:
        raise BracketProbeInvalid("Bracket probe ticket is malformed.")
    parts = ticket.split(".")
    if len(parts) != 2 or not hmac.compare_digest(parts[1], _signature(parts[0])):
        raise BracketProbeInvalid("Bracket probe ticket signature is invalid.")
    try:
        payload = json.loads(_decode(parts[0]))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BracketProbeInvalid("Bracket probe ticket is malformed.") from exc
    if not isinstance(payload, dict):
        raise BracketProbeInvalid("Bracket probe ticket is malformed.")
    issued_at = payload.get("iat")
    expires_at = payload.get("exp")
    status = payload.get("status")
    if (
        payload.get("v") != 1
        or not isinstance(payload.get("tournament_id"), str)
        or not isinstance(payload.get("slug"), str)
        or payload.get("slug") != expected_slug
        or not isinstance(payload.get("revision"), int)
        or payload["revision"] < 0
        or status not in {"pending", "teams_ready", "ready"}
        or not isinstance(issued_at, int)
        or not isinstance(expires_at, int)
        or expires_at <= issued_at
        or expires_at - issued_at > BRACKET_PROBE_TTL_SECONDS
    ):
        raise BracketProbeInvalid("Bracket probe ticket claims are invalid.")
    current_epoch = int((now or datetime.now(UTC)).timestamp())
    if issued_at > current_epoch + BRACKET_PROBE_CLOCK_SKEW_SECONDS or expires_at <= current_epoch:
        raise BracketProbeInvalid("Bracket probe ticket has expired.")

    user_id = payload.get("user_id")
    session_id = payload.get("session_id")
    session_digest = payload.get("session_digest")
    bound = (user_id, session_id, session_digest)
    if any(value is not None for value in bound):
        if (
            not all(isinstance(value, str) and value for value in bound)
            or session_token is None
            or not hmac.compare_digest(str(session_digest), session_token_digest(session_token))
        ):
            raise BracketProbeInvalid("Bracket probe ticket session binding is invalid.")
    return BracketProbeTicket(
        tournament_id=str(payload["tournament_id"]),
        slug=str(payload["slug"]),
        revision=int(payload["revision"]),
        bracket_status=status,
        user_id=user_id if isinstance(user_id, str) else None,
        session_id=session_id if isinstance(session_id, str) else None,
        issued_at=issued_at,
        expires_at=expires_at,
    )
