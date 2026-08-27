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

# The proof is intentionally short-lived and only authorizes a new admission.
# It never defines the lifetime of an established stream: the stream keeps its
# renewable lease and periodic authoritative access revalidation instead.
SSE_ADMISSION_TICKET_TTL_SECONDS = 300
SSE_ADMISSION_TICKET_CLOCK_SKEW_SECONDS = 5
SSE_ADMISSION_TICKET_PREFIX = "platform-sse-ticket-v1"
SseTicketAccess = Literal["public", "organizer", "admin", "active_participant"]


class SseAdmissionTicketInvalid(ValueError):
    """A browser supplied an invalid or expired SSE admission ticket."""


@dataclass(frozen=True, slots=True)
class SseAdmissionTicket:
    tournament_id: str
    slug: str
    access: SseTicketAccess
    expires_at: int
    issued_at: int
    user_id: str | None = None
    session_id: str | None = None
    session_digest: str | None = None


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    if not value or len(value) > 4096:
        raise SseAdmissionTicketInvalid("SSE admission ticket is malformed.")
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise SseAdmissionTicketInvalid("SSE admission ticket is malformed.") from exc


def _signature(payload_segment: str) -> str:
    settings = get_settings()
    digest = hmac.new(
        settings.platform_secret_key.encode("utf-8"),
        f"{SSE_ADMISSION_TICKET_PREFIX}.{payload_segment}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    return _encode(digest)


def issue_sse_admission_ticket(
    *,
    tournament_id: str,
    slug: str,
    access: SseTicketAccess,
    user_id: str | None = None,
    session_id: str | None = None,
    session_token: str | None = None,
    now: datetime | None = None,
) -> str:
    issued_at = int((now or datetime.now(UTC)).timestamp())
    expires_at = issued_at + SSE_ADMISSION_TICKET_TTL_SECONDS
    user_fields = (user_id, session_id, session_token)
    if any(value is not None for value in user_fields) and not all(user_fields):
        raise ValueError("Authenticated SSE tickets require complete session binding.")
    if access != "public" and not all(user_fields):
        raise ValueError("Private SSE tickets require complete session binding.")
    payload: dict[str, object] = {
        "v": 1,
        "tournament_id": str(tournament_id),
        "slug": str(slug),
        "access": access,
        "iat": issued_at,
        "exp": expires_at,
    }
    if all(user_fields):
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


def verify_sse_admission_ticket(
    ticket: str,
    *,
    expected_slug: str,
    session_token: str | None = None,
    now: datetime | None = None,
) -> SseAdmissionTicket:
    if not isinstance(ticket, str) or len(ticket) > 8192:
        raise SseAdmissionTicketInvalid("SSE admission ticket is malformed.")
    segments = ticket.split(".")
    if len(segments) != 2:
        raise SseAdmissionTicketInvalid("SSE admission ticket is malformed.")
    payload_segment, supplied_signature = segments
    try:
        expected_signature = _signature(payload_segment)
    except (UnicodeEncodeError, ValueError) as exc:
        raise SseAdmissionTicketInvalid("SSE admission ticket is malformed.") from exc
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise SseAdmissionTicketInvalid("SSE admission ticket signature is invalid.")
    try:
        payload = json.loads(_decode(payload_segment))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SseAdmissionTicketInvalid("SSE admission ticket is malformed.") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise SseAdmissionTicketInvalid("SSE admission ticket version is invalid.")

    access = payload.get("access")
    tournament_id = payload.get("tournament_id")
    slug = payload.get("slug")
    issued_at = payload.get("iat")
    expires_at = payload.get("exp")
    if (
        access not in {"public", "organizer", "admin", "active_participant"}
        or not isinstance(tournament_id, str)
        or not tournament_id
        or not isinstance(slug, str)
        or not slug
        or slug != expected_slug
        or not isinstance(issued_at, int)
        or not isinstance(expires_at, int)
        or expires_at <= issued_at
        or expires_at - issued_at > SSE_ADMISSION_TICKET_TTL_SECONDS
    ):
        raise SseAdmissionTicketInvalid("SSE admission ticket claims are invalid.")

    current_epoch = int((now or datetime.now(UTC)).timestamp())
    if (
        issued_at > current_epoch + SSE_ADMISSION_TICKET_CLOCK_SKEW_SECONDS
        or expires_at <= current_epoch
    ):
        raise SseAdmissionTicketInvalid("SSE admission ticket has expired.")

    user_id = payload.get("user_id")
    session_id = payload.get("session_id")
    session_digest = payload.get("session_digest")
    user_fields = (user_id, session_id, session_digest)
    if any(value is not None for value in user_fields) and (
        not all(isinstance(value, str) and value for value in user_fields)
        or session_token is None
        or not hmac.compare_digest(session_digest, session_token_digest(session_token))
    ):
        raise SseAdmissionTicketInvalid("SSE admission ticket session binding is invalid.")
    if access != "public" and not all(isinstance(value, str) and value for value in user_fields):
        raise SseAdmissionTicketInvalid("Private SSE admission ticket binding is invalid.")

    return SseAdmissionTicket(
        tournament_id=tournament_id,
        slug=slug,
        access=access,
        expires_at=expires_at,
        issued_at=issued_at,
        user_id=user_id if isinstance(user_id, str) else None,
        session_id=session_id if isinstance(session_id, str) else None,
        session_digest=session_digest if isinstance(session_digest, str) else None,
    )
