from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from python_packages.platform_infra.models import (
    ApiMutationIdempotencyKey,
    new_uuid,
)


@dataclass(frozen=True, slots=True)
class MutationIdempotencyReservation:
    record: ApiMutationIdempotencyKey
    replay: bool


def request_idempotency_key(request: Request) -> str | None:
    raw_key = request.headers.get("Idempotency-Key")
    if raw_key is None:
        return None
    key = raw_key.strip()
    if (
        not key
        or len(key) > 128
        or any(ord(char) < 0x21 or ord(char) > 0x7E for char in key)
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Idempotency-Key must contain 1..128 visible ASCII characters.",
        )
    return key


def mutation_payload_fingerprint(payload: Any) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


async def reserve_mutation_idempotency(
    db_session: AsyncSession,
    *,
    actor_user_id: str,
    scope: str,
    key: str | None,
    request_fingerprint: str,
) -> MutationIdempotencyReservation | None:
    if key is None:
        return None

    record_id = new_uuid()
    inserted_id = await db_session.scalar(
        postgresql.insert(ApiMutationIdempotencyKey)
        .values(
            id=record_id,
            actor_user_id=actor_user_id,
            scope=scope,
            key=key,
            request_fingerprint=request_fingerprint,
        )
        .on_conflict_do_nothing(
            constraint="uq_api_mutation_idempotency_keys_actor_scope_key"
        )
        .returning(ApiMutationIdempotencyKey.id)
    )
    record = await db_session.scalar(
        select(ApiMutationIdempotencyKey)
        .where(
            ApiMutationIdempotencyKey.actor_user_id == actor_user_id,
            ApiMutationIdempotencyKey.scope == scope,
            ApiMutationIdempotencyKey.key == key,
        )
        .with_for_update()
    )
    if record is None:
        raise RuntimeError("Idempotency reservation disappeared during the transaction.")
    if record.request_fingerprint != request_fingerprint:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency-Key was already used with a different request payload.",
        )
    return MutationIdempotencyReservation(
        record=record,
        replay=inserted_id is None,
    )


def bind_mutation_idempotency_resource(
    reservation: MutationIdempotencyReservation | None,
    resource_id: str,
) -> None:
    if reservation is None:
        return
    existing_resource_id = reservation.record.resource_id
    if existing_resource_id is not None and existing_resource_id != resource_id:
        raise RuntimeError("Idempotency reservation is already bound to another resource.")
    reservation.record.resource_id = resource_id
