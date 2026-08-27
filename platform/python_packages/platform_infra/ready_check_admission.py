from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import hmac
import json
from typing import Any

from python_packages.platform_domain.tournaments import READY_CHECK_MAX_DURATION_SECONDS
from python_packages.platform_infra.config import get_settings
from python_packages.platform_infra.security import session_token_digest


READY_CHECK_ADMISSION_PREFIX = "platform-ready-check-admission-v1"
READY_CHECK_ADMISSION_CLOCK_SKEW_SECONDS = 5
READY_CHECK_ADMISSION_MAX_TTL_SECONDS = (
    READY_CHECK_MAX_DURATION_SECONDS
    + (15 * 60)
    + READY_CHECK_ADMISSION_CLOCK_SKEW_SECONDS
)
READY_CHECK_STREAM_MAX_CHECKS = 128


class ReadyCheckAdmissionInvalid(ValueError):
    """A browser supplied an invalid or expired Ready Check proof."""


@dataclass(frozen=True, slots=True)
class ReadyCheckStateProof:
    tournament_id: str
    slug: str
    user_id: str
    session_id: str
    ready_check_starts_at: int
    ready_check_ends_at: int
    issued_at: int
    expires_at: int


@dataclass(frozen=True, slots=True)
class ReadyCheckStreamProof:
    user_id: str
    session_id: str
    tournament_ids: tuple[str, ...]
    admission_open_at: int
    ready_check_ends_at: int
    issued_at: int
    expires_at: int


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    if not value or len(value) > 8192:
        raise ReadyCheckAdmissionInvalid("Ready Check proof is malformed.")
    try:
        return base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise ReadyCheckAdmissionInvalid("Ready Check proof is malformed.") from exc


def _signature(payload_segment: str) -> str:
    settings = get_settings()
    digest = hmac.new(
        settings.platform_secret_key.encode("utf-8"),
        f"{READY_CHECK_ADMISSION_PREFIX}.{payload_segment}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    return _encode(digest)


def _issue(payload: dict[str, Any]) -> str:
    payload_segment = _encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    return f"{payload_segment}.{_signature(payload_segment)}"


def _parse(
    proof: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(proof, str) or len(proof) > 16384:
        raise ReadyCheckAdmissionInvalid("Ready Check proof is malformed.")
    segments = proof.split(".")
    if len(segments) != 2:
        raise ReadyCheckAdmissionInvalid("Ready Check proof is malformed.")
    payload_segment, supplied_signature = segments
    expected_signature = _signature(payload_segment)
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise ReadyCheckAdmissionInvalid("Ready Check proof signature is invalid.")
    try:
        payload = json.loads(_decode(payload_segment))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReadyCheckAdmissionInvalid("Ready Check proof is malformed.") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise ReadyCheckAdmissionInvalid("Ready Check proof version is invalid.")

    issued_at = payload.get("iat")
    expires_at = payload.get("exp")
    if (
        not isinstance(issued_at, int)
        or not isinstance(expires_at, int)
        or expires_at <= issued_at
        or expires_at - issued_at > READY_CHECK_ADMISSION_MAX_TTL_SECONDS
    ):
        raise ReadyCheckAdmissionInvalid("Ready Check proof claims are invalid.")
    current_epoch = int((now or datetime.now(UTC)).timestamp())
    if expires_at <= current_epoch:
        raise ReadyCheckAdmissionInvalid("Ready Check proof has expired.")
    return payload


def ready_check_proof_expiration(
    *,
    issued_at: int,
    ready_check_ends_at: int,
) -> int:
    """Bound a proof to its workflow end without allowing an unbounded TTL."""

    return min(
        int(ready_check_ends_at) + READY_CHECK_ADMISSION_CLOCK_SKEW_SECONDS,
        int(issued_at) + READY_CHECK_ADMISSION_MAX_TTL_SECONDS,
    )


def issue_ready_check_state_proof(
    *,
    tournament_id: str,
    slug: str,
    user_id: str,
    session_id: str,
    session_token: str,
    ready_check_starts_at: datetime,
    ready_check_ends_at: datetime,
    now: datetime | None = None,
) -> str:
    now_epoch = int((now or datetime.now(UTC)).timestamp())
    starts_at = int(ready_check_starts_at.astimezone(UTC).timestamp())
    issued_at = max(now_epoch, starts_at)
    ends_at = int(ready_check_ends_at.astimezone(UTC).timestamp())
    expires_at = ready_check_proof_expiration(
        issued_at=issued_at,
        ready_check_ends_at=ends_at,
    )
    if expires_at <= issued_at:
        expires_at = issued_at + READY_CHECK_ADMISSION_CLOCK_SKEW_SECONDS + 1
    return _issue(
        {
            "v": 1,
            "kind": "state",
            "tournament_id": str(tournament_id),
            "slug": str(slug),
            "user_id": str(user_id),
            "session_id": str(session_id),
            "session_digest": session_token_digest(session_token),
            "ready_check_starts_at": starts_at,
            "ready_check_ends_at": ends_at,
            "iat": issued_at,
            "exp": expires_at,
        }
    )


def verify_ready_check_state_proof(
    proof: str,
    *,
    expected_slug: str,
    session_token: str,
    now: datetime | None = None,
) -> ReadyCheckStateProof:
    payload = _parse(proof, now=now)
    required = (
        payload.get("kind") == "state",
        isinstance(payload.get("tournament_id"), str) and bool(payload.get("tournament_id")),
        isinstance(payload.get("slug"), str) and payload.get("slug") == expected_slug,
        isinstance(payload.get("user_id"), str) and bool(payload.get("user_id")),
        isinstance(payload.get("session_id"), str) and bool(payload.get("session_id")),
        isinstance(payload.get("session_digest"), str) and bool(payload.get("session_digest")),
        isinstance(payload.get("ready_check_starts_at"), int),
        isinstance(payload.get("ready_check_ends_at"), int),
    )
    if not all(required):
        raise ReadyCheckAdmissionInvalid("Ready Check state proof claims are invalid.")
    if not hmac.compare_digest(
        str(payload["session_digest"]),
        session_token_digest(session_token),
    ):
        raise ReadyCheckAdmissionInvalid("Ready Check state proof session binding is invalid.")
    if int(payload["ready_check_ends_at"]) <= int(payload["ready_check_starts_at"]):
        raise ReadyCheckAdmissionInvalid("Ready Check state proof schedule is invalid.")
    return ReadyCheckStateProof(
        tournament_id=str(payload["tournament_id"]),
        slug=str(payload["slug"]),
        user_id=str(payload["user_id"]),
        session_id=str(payload["session_id"]),
        ready_check_starts_at=int(payload["ready_check_starts_at"]),
        ready_check_ends_at=int(payload["ready_check_ends_at"]),
        issued_at=int(payload["iat"]),
        expires_at=int(payload["exp"]),
    )


def issue_ready_check_stream_proof(
    *,
    user_id: str,
    session_id: str,
    session_token: str,
    tournament_ids: list[str] | tuple[str, ...],
    admission_open_at: datetime | None = None,
    ready_check_ends_at: datetime | None = None,
    now: datetime | None = None,
) -> str:
    normalized_ids = tuple(dict.fromkeys(str(item) for item in tournament_ids if str(item)))
    if not normalized_ids or len(normalized_ids) > READY_CHECK_STREAM_MAX_CHECKS:
        raise ValueError("Ready Check stream proof must contain a bounded tournament set.")
    now_epoch = int((now or datetime.now(UTC)).timestamp())
    admission_open_epoch = int(
        (admission_open_at or now or datetime.now(UTC)).astimezone(UTC).timestamp()
    )
    if ready_check_ends_at is None:
        raise ValueError("Ready Check stream proof requires a workflow end.")
    ready_check_ends_epoch = int(ready_check_ends_at.astimezone(UTC).timestamp())
    if ready_check_ends_epoch <= admission_open_epoch:
        raise ValueError("Ready Check stream proof workflow end must follow admission.")
    issued_at = max(now_epoch, admission_open_epoch)
    expires_at = ready_check_proof_expiration(
        issued_at=issued_at,
        ready_check_ends_at=ready_check_ends_epoch,
    )
    return _issue(
        {
            "v": 1,
            "kind": "stream",
            "user_id": str(user_id),
            "session_id": str(session_id),
            "session_digest": session_token_digest(session_token),
            "tournament_ids": list(normalized_ids),
            "admission_open_at": admission_open_epoch,
            "ready_check_ends_at": ready_check_ends_epoch,
            "iat": issued_at,
            "exp": expires_at,
        }
    )


def verify_ready_check_stream_proof(
    proof: str,
    *,
    session_token: str,
    now: datetime | None = None,
) -> ReadyCheckStreamProof:
    payload = _parse(proof, now=now)
    raw_ids = payload.get("tournament_ids")
    if (
        payload.get("kind") != "stream"
        or not isinstance(payload.get("user_id"), str)
        or not isinstance(payload.get("session_id"), str)
        or not isinstance(payload.get("session_digest"), str)
        or not isinstance(raw_ids, list)
        or not raw_ids
        or len(raw_ids) > READY_CHECK_STREAM_MAX_CHECKS
        or any(not isinstance(item, str) or not item for item in raw_ids)
        or len(set(raw_ids)) != len(raw_ids)
        or not isinstance(payload.get("admission_open_at"), int)
        or not isinstance(payload.get("ready_check_ends_at"), int)
    ):
        raise ReadyCheckAdmissionInvalid("Ready Check stream proof claims are invalid.")
    admission_open_at = int(payload["admission_open_at"])
    ready_check_ends_at = int(payload["ready_check_ends_at"])
    issued_at = int(payload["iat"])
    expires_at = int(payload["exp"])
    if (
        admission_open_at < issued_at
        or admission_open_at >= ready_check_ends_at
        or ready_check_ends_at + READY_CHECK_ADMISSION_CLOCK_SKEW_SECONDS < expires_at
        or expires_at > issued_at + READY_CHECK_ADMISSION_MAX_TTL_SECONDS
    ):
        raise ReadyCheckAdmissionInvalid("Ready Check stream proof admission window is invalid.")
    if not hmac.compare_digest(
        str(payload["session_digest"]),
        session_token_digest(session_token),
    ):
        raise ReadyCheckAdmissionInvalid("Ready Check stream proof session binding is invalid.")
    return ReadyCheckStreamProof(
        user_id=str(payload["user_id"]),
        session_id=str(payload["session_id"]),
        tournament_ids=tuple(raw_ids),
        admission_open_at=admission_open_at,
        ready_check_ends_at=ready_check_ends_at,
        issued_at=issued_at,
        expires_at=expires_at,
    )
