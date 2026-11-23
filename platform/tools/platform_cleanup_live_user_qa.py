#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import hashlib
import json
import os
import pathlib
import re
import stat
import sys
from uuid import UUID


def _configure_platform_import_root(script_file: pathlib.Path) -> pathlib.Path:
    resolved_script = script_file.resolve(strict=True)
    tools_dir = resolved_script.parent
    platform_root = tools_dir.parent
    if tools_dir.name != "tools" or platform_root.name != "platform":
        raise RuntimeError("live-QA cleanup helper is outside its platform payload")
    import_root = str(platform_root)
    if import_root not in sys.path:
        sys.path.insert(0, import_root)
    return platform_root


_configure_platform_import_root(pathlib.Path(__file__))

from sqlalchemy import Text, and_, cast, delete, func, or_, select

from python_packages.platform_infra.config import get_settings, validate_platform_settings
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.models import (
    AuditLog,
    MediaAsset,
    PlayerProfile,
    PlayerTournamentCommitment,
    Tournament,
    TournamentDeadlockCaptainEntry,
    TournamentDeadlockAssignmentRun,
    TournamentDeadlockCaptainRound,
    TournamentDeadlockReadyVote,
    TournamentDeadlockReadyRound,
    TournamentInvite,
    TournamentMatch,
    TournamentParticipant,
    User,
    UserSession,
)


CONFIRMATION = "cleanup-live-user-qa"
EXPECTED_PRODUCTION_ORIGIN = "https://old-sparky.com"
MARKER_PATTERN = re.compile(r"^liveqa-[a-z0-9-]{6,56}$")
MAX_INVENTORY_BYTES = 64 * 1024
MAX_INVENTORY_IDS = {
    "user_ids": 32,
    "tournament_ids": 8,
    "media_ids": 32,
}
AUDIT_SCOPE_CHUNK_SIZE = 256
LIVE_QA_STATE_ROOT = pathlib.Path("/root/.oldsparky/liveqa")
LIVE_QA_STATE_NAME = re.compile(r"^live-user-qa\.[A-Za-z0-9]{6}$")


@dataclass(frozen=True)
class CleanupInventory:
    marker: str
    user_ids: tuple[str, ...]
    tournament_ids: tuple[str, ...]
    media_ids: tuple[str, ...]
    inventory_sha256: str = ""


def _validated_ids(payload: object, field: str) -> tuple[str, ...]:
    if not isinstance(payload, list) or len(payload) > MAX_INVENTORY_IDS[field]:
        raise ValueError(f"{field} must be a bounded list")
    values: list[str] = []
    for value in payload:
        if not isinstance(value, str):
            raise ValueError(f"{field} must contain UUID strings")
        try:
            normalized = str(UUID(value))
        except ValueError as exc:
            raise ValueError(f"{field} must contain canonical UUID strings") from exc
        if value != normalized:
            raise ValueError(f"{field} must contain canonical UUID strings")
        values.append(value)
    if len(values) != len(set(values)):
        raise ValueError(f"{field} must not contain duplicate IDs")
    return tuple(values)


def load_inventory(path: pathlib.Path, *, expected_marker: str) -> CleanupInventory:
    if not path.is_absolute():
        raise ValueError("inventory path must be absolute")
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("inventory must be a regular file, not a symlink")
    if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError("inventory must be root-owned with mode 0600")
    if metadata.st_size > MAX_INVENTORY_BYTES:
        raise ValueError("inventory exceeds the 64 KiB limit")
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("inventory must contain valid UTF-8 JSON") from exc
    after = path.lstat()
    if (
        stat.S_ISLNK(after.st_mode)
        or not stat.S_ISREG(after.st_mode)
        or after.st_uid != metadata.st_uid
        or stat.S_IMODE(after.st_mode) != stat.S_IMODE(metadata.st_mode)
        or after.st_size != metadata.st_size
        or len(raw) != metadata.st_size
    ):
        raise ValueError("inventory changed while it was being read")
    if not isinstance(payload, dict) or set(payload) != {
        "version",
        "marker",
        "user_ids",
        "tournament_ids",
        "media_ids",
    }:
        raise ValueError("inventory has an unexpected schema")
    if payload["version"] != 1:
        raise ValueError("inventory version must be 1")
    marker = payload["marker"]
    if marker != expected_marker or not isinstance(marker, str) or not MARKER_PATTERN.fullmatch(marker):
        raise ValueError("inventory marker does not match --marker")
    return CleanupInventory(
        marker=marker,
        user_ids=_validated_ids(payload["user_ids"], "user_ids"),
        tournament_ids=_validated_ids(payload["tournament_ids"], "tournament_ids"),
        media_ids=_validated_ids(payload["media_ids"], "media_ids"),
        inventory_sha256=hashlib.sha256(raw).hexdigest(),
    )


def _audit_subject_filters(
    audit_subject_ids: dict[str, set[str]],
) -> list[object]:
    return [
        and_(
            AuditLog.subject_type == subject_type,
            AuditLog.subject_id.in_(subject_ids),
        )
        for subject_type, subject_ids in audit_subject_ids.items()
        if subject_ids
    ]


async def _scalar_string_set(db_session, statement) -> set[str]:
    return {
        str(value)
        for value in (await db_session.scalars(statement)).all()
    }


def _json_user_filter(column, user_ids: set[str]):
    return or_(
        *(cast(column, Text).contains(f'"{user_id}"') for user_id in user_ids)
    )


def _audit_scope_chunks(values: set[str]):
    ordered_values = tuple(sorted(values))
    for start in range(0, len(ordered_values), AUDIT_SCOPE_CHUNK_SIZE):
        yield set(ordered_values[start : start + AUDIT_SCOPE_CHUNK_SIZE])


async def _user_linked_tournament_ids(
    db_session,
    user_ids: set[str],
) -> set[str]:
    if not user_ids:
        return set()
    statements = (
        select(Tournament.id).where(Tournament.organizer_user_id.in_(user_ids)),
        select(TournamentParticipant.tournament_id).where(
            or_(
                TournamentParticipant.user_id.in_(user_ids),
                TournamentParticipant.moderated_by_user_id.in_(user_ids),
            )
        ),
        select(TournamentMatch.tournament_id).where(
            TournamentMatch.reported_by_user_id.in_(user_ids)
        ),
        select(TournamentInvite.tournament_id).where(
            or_(
                TournamentInvite.created_by_user_id.in_(user_ids),
                TournamentInvite.last_claimed_by_user_id.in_(user_ids),
            )
        ),
        select(TournamentDeadlockReadyRound.tournament_id).where(
            TournamentDeadlockReadyRound.initiated_by_user_id.in_(user_ids)
        ),
        select(TournamentDeadlockReadyRound.tournament_id)
        .join(
            TournamentDeadlockReadyVote,
            TournamentDeadlockReadyVote.round_id == TournamentDeadlockReadyRound.id,
        )
        .where(TournamentDeadlockReadyVote.user_id.in_(user_ids)),
        select(TournamentDeadlockCaptainRound.tournament_id).where(
            TournamentDeadlockCaptainRound.initiated_by_user_id.in_(user_ids)
        ),
        select(TournamentDeadlockCaptainRound.tournament_id)
        .join(
            TournamentDeadlockCaptainEntry,
            TournamentDeadlockCaptainEntry.round_id
            == TournamentDeadlockCaptainRound.id,
        )
        .where(TournamentDeadlockCaptainEntry.user_id.in_(user_ids)),
        select(TournamentDeadlockAssignmentRun.tournament_id).where(
            or_(
                TournamentDeadlockAssignmentRun.created_by_user_id.in_(user_ids),
                TournamentDeadlockAssignmentRun.published_by_user_id.in_(user_ids),
                TournamentDeadlockAssignmentRun.locked_by_user_id.in_(user_ids),
            )
        ),
        select(PlayerTournamentCommitment.tournament_id).where(
            PlayerTournamentCommitment.user_id.in_(user_ids)
        ),
        select(TournamentDeadlockReadyRound.tournament_id).where(
            _json_user_filter(
                TournamentDeadlockReadyRound.eligible_user_ids,
                user_ids,
            )
        ),
        select(TournamentDeadlockAssignmentRun.tournament_id).where(
            or_(
                _json_user_filter(
                    TournamentDeadlockAssignmentRun.candidate_pool_user_ids,
                    user_ids,
                ),
                _json_user_filter(
                    TournamentDeadlockAssignmentRun.leftover_user_ids,
                    user_ids,
                ),
                _json_user_filter(
                    TournamentDeadlockAssignmentRun.result_snapshot,
                    user_ids,
                ),
            )
        ),
    )
    linked_ids: set[str] = set()
    for statement in statements:
        linked_ids.update(await _scalar_string_set(db_session, statement))
    return linked_ids


async def _tournament_graph_subject_ids(
    db_session,
    tournament_ids: set[str],
) -> dict[str, set[str]]:
    subjects: dict[str, set[str]] = {
        "tournament": set(tournament_ids),
    }
    for model, subject_type in (
        (TournamentParticipant, "tournament_participant"),
        (TournamentMatch, "tournament_match"),
        (TournamentInvite, "tournament_invite"),
        (TournamentDeadlockAssignmentRun, "tournament_deadlock_assignment_run"),
    ):
        subjects[subject_type] = (
            await _scalar_string_set(
                db_session,
                select(model.id).where(model.tournament_id.in_(tournament_ids)),
            )
            if tournament_ids
            else set()
        )
    subjects["tournament_deadlock_ready_round"] = (
        await _scalar_string_set(
            db_session,
            select(TournamentDeadlockReadyRound.id).where(
                TournamentDeadlockReadyRound.tournament_id.in_(tournament_ids)
            ),
        )
        if tournament_ids
        else set()
    )
    subjects["tournament_deadlock_captain_round"] = (
        await _scalar_string_set(
            db_session,
            select(TournamentDeadlockCaptainRound.id).where(
                TournamentDeadlockCaptainRound.tournament_id.in_(tournament_ids)
            ),
        )
        if tournament_ids
        else set()
    )
    return subjects


async def _audit_subject_ids_for_scope(
    db_session,
    user_ids: set[str],
    tournament_ids: set[str],
) -> dict[str, set[str]]:
    subjects = await _tournament_graph_subject_ids(db_session, tournament_ids)
    for subject_type in ("user", "profile", "deadlock_profile", "deadlock_dream_slots"):
        subjects[subject_type] = set(user_ids)
    subjects["session"] = (
        await _scalar_string_set(
            db_session,
            select(UserSession.id).where(UserSession.user_id.in_(user_ids)),
        )
        if user_ids
        else set()
    )
    return subjects


async def _validate_audit_scope(
    db_session,
    user_ids: set[str],
    audit_subject_ids: dict[str, set[str]],
    preserved_actor_ids: set[str] | None = None,
) -> None:
    allowed_actor_ids = user_ids | (preserved_actor_ids or set())
    if user_ids:
        for user_chunk in _audit_scope_chunks(user_ids):
            actor_rows = (
                await db_session.execute(
                    select(AuditLog.subject_type, AuditLog.subject_id).where(
                        AuditLog.actor_user_id.in_(user_chunk)
                    )
                )
            ).all()
            if any(
                row.subject_id is None
                or str(row.subject_id)
                not in audit_subject_ids.get(str(row.subject_type), set())
                for row in actor_rows
            ):
                raise RuntimeError(
                    "Refusing to delete a liveqa actor audit row outside the exact inventory."
                )
            payload_rows = (
                await db_session.execute(
                    select(AuditLog.subject_type, AuditLog.subject_id).where(
                        _json_user_filter(AuditLog.payload, user_chunk)
                    )
                )
            ).all()
            if any(
                row.subject_id is None
                or str(row.subject_id)
                not in audit_subject_ids.get(str(row.subject_type), set())
                for row in payload_rows
            ):
                raise RuntimeError(
                    "Refusing to leave a liveqa user reference in an out-of-scope audit payload."
                )

    for subject_type, subject_ids in audit_subject_ids.items():
        for subject_chunk in _audit_scope_chunks(subject_ids):
            outside_actors = (
                await db_session.scalars(
                    select(AuditLog.actor_user_id).where(
                        and_(
                            AuditLog.subject_type == subject_type,
                            AuditLog.subject_id.in_(subject_chunk),
                        ),
                        AuditLog.actor_user_id.is_not(None),
                    )
                )
            ).all()
            if any(str(actor_id) not in allowed_actor_ids for actor_id in outside_actors):
                raise RuntimeError(
                    "Refusing to delete a real actor's audit row for liveqa inventory objects."
                )


async def _validate_tournament_graph_boundary(
    db_session,
    tournament_ids: set[str],
) -> None:
    if not tournament_ids:
        return
    match_ids = select(TournamentMatch.id).where(
        TournamentMatch.tournament_id.in_(tournament_ids)
    )
    ready_round_ids = select(TournamentDeadlockReadyRound.id).where(
        TournamentDeadlockReadyRound.tournament_id.in_(tournament_ids)
    )
    captain_round_ids = select(TournamentDeadlockCaptainRound.id).where(
        TournamentDeadlockCaptainRound.tournament_id.in_(tournament_ids)
    )
    assignment_run_ids = select(TournamentDeadlockAssignmentRun.id).where(
        TournamentDeadlockAssignmentRun.tournament_id.in_(tournament_ids)
    )
    cross_scope_checks = (
        select(TournamentMatch.id).where(
            TournamentMatch.tournament_id.notin_(tournament_ids),
            or_(
                TournamentMatch.home_source_match_id.in_(match_ids),
                TournamentMatch.away_source_match_id.in_(match_ids),
            ),
        ),
        select(TournamentDeadlockCaptainRound.id).where(
            TournamentDeadlockCaptainRound.tournament_id.notin_(tournament_ids),
            TournamentDeadlockCaptainRound.source_ready_round_id.in_(ready_round_ids),
        ),
        select(TournamentDeadlockAssignmentRun.id).where(
            TournamentDeadlockAssignmentRun.tournament_id.notin_(tournament_ids),
            or_(
                TournamentDeadlockAssignmentRun.source_ready_round_id.in_(ready_round_ids),
                TournamentDeadlockAssignmentRun.source_captain_round_id.in_(
                    captain_round_ids
                ),
            ),
        ),
        select(PlayerTournamentCommitment.id).where(
            PlayerTournamentCommitment.tournament_id.notin_(tournament_ids),
            PlayerTournamentCommitment.assignment_run_id.in_(assignment_run_ids),
        ),
        select(TournamentMatch.id).where(
            TournamentMatch.tournament_id.in_(tournament_ids),
            or_(
                and_(
                    TournamentMatch.home_source_match_id.is_not(None),
                    TournamentMatch.home_source_match_id.notin_(match_ids),
                ),
                and_(
                    TournamentMatch.away_source_match_id.is_not(None),
                    TournamentMatch.away_source_match_id.notin_(match_ids),
                ),
            ),
        ),
        select(TournamentDeadlockCaptainRound.id).where(
            TournamentDeadlockCaptainRound.tournament_id.in_(tournament_ids),
            TournamentDeadlockCaptainRound.source_ready_round_id.notin_(ready_round_ids),
        ),
        select(TournamentDeadlockAssignmentRun.id).where(
            TournamentDeadlockAssignmentRun.tournament_id.in_(tournament_ids),
            or_(
                TournamentDeadlockAssignmentRun.source_ready_round_id.notin_(ready_round_ids),
                TournamentDeadlockAssignmentRun.source_captain_round_id.notin_(
                    captain_round_ids
                ),
            ),
        ),
        select(PlayerTournamentCommitment.id).where(
            PlayerTournamentCommitment.tournament_id.in_(tournament_ids),
            PlayerTournamentCommitment.assignment_run_id.notin_(assignment_run_ids),
        ),
    )
    for statement in cross_scope_checks:
        if await db_session.scalar(select(statement.exists())):
            raise RuntimeError(
                "Refusing to delete a liveqa tournament graph with cross-scope references."
            )


async def _validate_media_scope(
    db_session,
    user_ids: set[str],
    tournament_ids: set[str],
    media_ids: set[str],
) -> None:
    media_rows = (
        await db_session.execute(
            select(
                MediaAsset.id,
                MediaAsset.owner_user_id,
                MediaAsset.tournament_id,
            )
            .where(MediaAsset.id.in_(media_ids))
            .with_for_update()
        )
    ).all() if media_ids else []
    existing_media_ids = {str(row.id) for row in media_rows}
    for row in media_rows:
        owner_id = str(row.owner_user_id) if row.owner_user_id is not None else None
        tournament_id = str(row.tournament_id) if row.tournament_id is not None else None
        if not (
            (owner_id in user_ids and tournament_id is None)
            or (owner_id is None and tournament_id in tournament_ids)
        ):
            raise RuntimeError(
                "Refusing to delete an inventory media asset outside liveqa ownership."
            )

    owned_filters = []
    if user_ids:
        owned_filters.append(MediaAsset.owner_user_id.in_(user_ids))
    if tournament_ids:
        owned_filters.append(MediaAsset.tournament_id.in_(tournament_ids))
    owned_media_ids = (
        await _scalar_string_set(
            db_session,
            select(MediaAsset.id).where(or_(*owned_filters)),
        )
        if owned_filters
        else set()
    )
    if owned_media_ids != existing_media_ids:
        raise RuntimeError("Inventory does not exactly match liveqa-owned media assets.")

    media_by_id = {str(row.id): row for row in media_rows}
    profile_rows = (
        await db_session.execute(
            select(
                PlayerProfile.user_id,
                PlayerProfile.avatar_asset_id,
                PlayerProfile.banner_asset_id,
            ).where(
                or_(
                    PlayerProfile.avatar_asset_id.in_(media_ids),
                    PlayerProfile.banner_asset_id.in_(media_ids),
                    PlayerProfile.user_id.in_(user_ids),
                )
            )
        )
    ).all() if media_ids or user_ids else []
    for row in profile_rows:
        profile_user_id = str(row.user_id)
        for asset_id in (row.avatar_asset_id, row.banner_asset_id):
            if asset_id is None:
                continue
            normalized_asset_id = str(asset_id)
            if profile_user_id in user_ids and normalized_asset_id not in media_ids:
                raise RuntimeError(
                    "Refusing to delete a liveqa profile with unrecorded media."
                )
            if normalized_asset_id in media_ids:
                media_row = media_by_id.get(normalized_asset_id)
                if (
                    profile_user_id not in user_ids
                    or media_row is None
                    or str(media_row.owner_user_id) != profile_user_id
                ):
                    raise RuntimeError(
                        "Refusing to delete media attached outside its liveqa profile owner."
                    )

    tournament_rows = (
        await db_session.execute(
            select(Tournament.id, Tournament.banner_asset_id).where(
                or_(
                    Tournament.banner_asset_id.in_(media_ids),
                    Tournament.id.in_(tournament_ids),
                )
            )
        )
    ).all() if media_ids or tournament_ids else []
    for row in tournament_rows:
        tournament_id = str(row.id)
        if row.banner_asset_id is None:
            continue
        asset_id = str(row.banner_asset_id)
        if tournament_id in tournament_ids and asset_id not in media_ids:
            raise RuntimeError(
                "Refusing to delete a liveqa tournament with unrecorded media."
            )
        if asset_id in media_ids:
            media_row = media_by_id.get(asset_id)
            if (
                tournament_id not in tournament_ids
                or media_row is None
                or str(media_row.tournament_id) != tournament_id
            ):
                raise RuntimeError(
                    "Refusing to delete media attached outside its liveqa tournament owner."
                )


async def _validate_exact_scope(db_session, inventory: CleanupInventory) -> None:
    user_ids = set(inventory.user_ids)
    tournament_ids = set(inventory.tournament_ids)
    media_ids = set(inventory.media_ids)
    marker = inventory.marker.lower()

    users = (
        await db_session.execute(
            select(User.id, User.email)
            .where(User.id.in_(user_ids))
            .with_for_update()
        )
    ).all() if user_ids else []
    if any(marker not in str(row.email).lower() for row in users):
        raise RuntimeError("Refusing to delete an inventory user without the liveqa marker.")
    existing_user_ids = {str(row.id) for row in users}
    marker_user_ids = await _scalar_string_set(
        db_session,
        select(User.id)
        .where(func.lower(User.email).contains(marker))
        .with_for_update(),
    )

    expected_description = f"Accelerated live browser acceptance {inventory.marker}."
    tournaments = (
        await db_session.execute(
            select(Tournament.id, Tournament.description)
            .where(Tournament.id.in_(tournament_ids))
            .with_for_update()
        )
    ).all() if tournament_ids else []
    if any(row.description != expected_description for row in tournaments):
        raise RuntimeError("Refusing to delete an inventory tournament without the liveqa marker.")
    existing_tournament_ids = {str(row.id) for row in tournaments}
    marker_tournament_ids = await _scalar_string_set(
        db_session,
        select(Tournament.id)
        .where(Tournament.description == expected_description)
        .with_for_update(),
    )

    if marker_user_ids != existing_user_ids:
        raise RuntimeError("Inventory does not exactly match liveqa marker users.")
    if marker_tournament_ids != existing_tournament_ids:
        raise RuntimeError("Inventory does not exactly match liveqa marker tournaments.")
    linked_tournament_ids = await _user_linked_tournament_ids(db_session, user_ids)
    if not linked_tournament_ids.issubset(tournament_ids):
        raise RuntimeError(
            "Refusing to delete a liveqa user linked to a tournament outside the inventory."
        )
    await _validate_tournament_graph_boundary(db_session, tournament_ids)
    await _validate_media_scope(db_session, user_ids, tournament_ids, media_ids)
    audit_subject_ids = await _audit_subject_ids_for_scope(
        db_session,
        user_ids,
        tournament_ids,
    )
    await _validate_audit_scope(db_session, user_ids, audit_subject_ids)


ABSENCE_COUNT_FIELDS = (
    "users",
    "tournaments",
    "media",
    "sessions",
    "audit_logs",
    "marker_users",
    "marker_tournaments",
)


async def verify_absence(
    db_session,
    inventory: CleanupInventory,
    audit_subject_ids: dict[str, set[str]],
) -> dict[str, int]:
    """Return zero counts from the single post-cleanup verification session."""

    user_ids = list(inventory.user_ids)
    tournament_ids = list(inventory.tournament_ids)
    media_ids = list(inventory.media_ids)
    await _validate_exact_scope(db_session, inventory)
    remaining_users = await db_session.scalar(
        select(func.count()).select_from(User).where(User.id.in_(user_ids))
    ) if user_ids else 0
    remaining_tournaments = await db_session.scalar(
        select(func.count()).select_from(Tournament).where(Tournament.id.in_(tournament_ids))
    ) if tournament_ids else 0
    remaining_media = await db_session.scalar(
        select(func.count()).select_from(MediaAsset).where(MediaAsset.id.in_(media_ids))
    ) if media_ids else 0
    remaining_sessions = await db_session.scalar(
        select(func.count()).select_from(UserSession).where(
            UserSession.user_id.in_(user_ids)
        )
    ) if user_ids else 0
    marker_users = await db_session.scalar(
        select(func.count()).select_from(User).where(
            func.lower(User.email).contains(inventory.marker.lower())
        )
    )
    marker_tournaments = await db_session.scalar(
        select(func.count()).select_from(Tournament).where(
            Tournament.description
            == f"Accelerated live browser acceptance {inventory.marker}."
        )
    )
    remaining_audit_filters = []
    if user_ids:
        remaining_audit_filters.append(AuditLog.actor_user_id.in_(user_ids))
    remaining_audit_filters.extend(_audit_subject_filters(audit_subject_ids))
    remaining_audit_logs = await db_session.scalar(
        select(func.count())
        .select_from(AuditLog)
        .where(or_(*remaining_audit_filters))
    ) if remaining_audit_filters else 0
    counts = {
        "users": int(remaining_users or 0),
        "tournaments": int(remaining_tournaments or 0),
        "media": int(remaining_media or 0),
        "sessions": int(remaining_sessions or 0),
        "audit_logs": int(remaining_audit_logs or 0),
        "marker_users": int(marker_users or 0),
        "marker_tournaments": int(marker_tournaments or 0),
    }
    if any(counts.values()):
        raise RuntimeError("Live-user QA cleanup left recorded or marker-owned rows behind.")
    return counts


def _absence_proof_payload(
    inventory: CleanupInventory,
    *,
    runner_source_sha: str,
    app_sha: str,
    marker_sha256: str,
    gate_id_sha256: str,
    zero_counts: dict[str, int],
) -> dict[str, object]:
    if (
        not re.fullmatch(r"[0-9a-f]{40}", runner_source_sha)
        or not re.fullmatch(r"[0-9a-f]{40}", app_sha)
        or not re.fullmatch(r"[0-9a-f]{64}", marker_sha256)
        or not re.fullmatch(r"[0-9a-f]{64}", gate_id_sha256)
        or not re.fullmatch(r"[0-9a-f]{64}", inventory.inventory_sha256)
        or set(zero_counts) != set(ABSENCE_COUNT_FIELDS)
        or any(type(value) is not int or value != 0 for value in zero_counts.values())
        or hashlib.sha256(inventory.marker.encode("ascii")).hexdigest()
        != marker_sha256
    ):
        raise RuntimeError("Live-user QA absence proof binding is invalid.")
    return {
        "schema": 1,
        "status": "verified_absent",
        "runner_source_sha": runner_source_sha,
        "app_sha": app_sha,
        "marker_sha256": marker_sha256,
        "gate_id_sha256": gate_id_sha256,
        "inventory_sha256": inventory.inventory_sha256,
        "zero_counts": zero_counts,
    }


def _publish_state_absence_proof(
    state_dir: pathlib.Path,
    inventory: CleanupInventory,
    payload: dict[str, object],
) -> None:
    inventory_path = state_dir / "inventory.json"
    metadata = state_dir.lstat()
    inventory_metadata = inventory_path.lstat()
    if (
        not state_dir.is_absolute()
        or state_dir.resolve(strict=True) != state_dir
        or state_dir.parent != LIVE_QA_STATE_ROOT
        or not LIVE_QA_STATE_NAME.fullmatch(state_dir.name)
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or inventory_path.is_symlink()
        or not stat.S_ISREG(inventory_metadata.st_mode)
        or inventory_metadata.st_uid != 0
        or stat.S_IMODE(inventory_metadata.st_mode) != 0o600
        or payload.get("gate_id_sha256")
        != hashlib.sha256(state_dir.name.encode("ascii")).hexdigest()
    ):
        raise RuntimeError("Live-user QA absence proof target is unsafe.")
    inventory_fd = os.open(
        inventory_path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(inventory_fd)
        raw_inventory = b""
        while len(raw_inventory) <= MAX_INVENTORY_BYTES:
            chunk = os.read(
                inventory_fd,
                min(64 * 1024, MAX_INVENTORY_BYTES + 1 - len(raw_inventory)),
            )
            if not chunk:
                break
            raw_inventory += chunk
        after = os.fstat(inventory_fd)
    finally:
        os.close(inventory_fd)
    if (
        len(raw_inventory) > MAX_INVENTORY_BYTES
        or opened.st_dev != inventory_metadata.st_dev
        or opened.st_ino != inventory_metadata.st_ino
        or opened.st_size != inventory_metadata.st_size
        or after.st_size != opened.st_size
        or hashlib.sha256(raw_inventory).hexdigest() != inventory.inventory_sha256
    ):
        raise RuntimeError("Live-user QA inventory changed before absence proof.")
    path = state_dir / "cleanup-absence.json"
    encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode("ascii")
    if len(encoded) > 4096:
        raise RuntimeError("Live-user QA absence proof exceeds its bound.")
    if path.exists() or path.is_symlink():
        proof_metadata = path.lstat()
        if (
            stat.S_ISLNK(proof_metadata.st_mode)
            or not stat.S_ISREG(proof_metadata.st_mode)
            or proof_metadata.st_uid != 0
            or proof_metadata.st_nlink != 1
            or stat.S_IMODE(proof_metadata.st_mode) != 0o600
            or proof_metadata.st_size != len(encoded)
            or path.read_bytes() != encoded
        ):
            raise RuntimeError("A different live-user QA absence proof already exists.")
        return
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    directory_fd = os.open(state_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _validate_runtime_target(settings, *, allow_test_environment: bool) -> None:
    validate_platform_settings(settings)
    environment = settings.platform_environment.strip().lower()
    if allow_test_environment and environment == "test":
        return
    if environment != "production":
        raise RuntimeError("Live-user QA cleanup is forbidden outside production.")
    if settings.platform_web_origin != EXPECTED_PRODUCTION_ORIGIN:
        raise RuntimeError(
            "Live-user QA cleanup requires the canonical production origin."
        )


async def cleanup(
    inventory: CleanupInventory,
    *,
    _allow_test_environment: bool = False,
    absence_proof_context: dict[str, object] | None = None,
) -> dict[str, int]:
    if os.geteuid() != 0:
        raise RuntimeError("Live-user QA cleanup must run as root.")
    settings = get_settings()
    _validate_runtime_target(
        settings,
        allow_test_environment=_allow_test_environment,
    )

    user_ids = list(inventory.user_ids)
    tournament_ids = list(inventory.tournament_ids)
    media_ids = list(inventory.media_ids)
    async with session_factory()() as db_session:
        await _validate_exact_scope(db_session, inventory)
        session_count = await db_session.scalar(
            select(func.count()).select_from(UserSession).where(
                UserSession.user_id.in_(user_ids)
            )
        ) if user_ids else 0

        media_rows = (
            await db_session.execute(
                select(MediaAsset.id, MediaAsset.status)
                .where(MediaAsset.id.in_(media_ids))
                .with_for_update()
            )
        ).all() if media_ids else []
        blockers = [str(row.id) for row in media_rows if row.status != "deleted"]
        if blockers:
            raise RuntimeError(
                "Recorded media must finish durable object cleanup before QA cleanup."
            )
        media_result = await db_session.execute(
            delete(MediaAsset).where(MediaAsset.id.in_(media_ids))
        ) if media_ids else None

        audit_subject_ids = await _audit_subject_ids_for_scope(
            db_session,
            set(user_ids),
            set(tournament_ids),
        )
        audit_filters = []
        if user_ids:
            audit_filters.append(AuditLog.actor_user_id.in_(user_ids))
        audit_filters.extend(_audit_subject_filters(audit_subject_ids))
        audit_result = await db_session.execute(
            delete(AuditLog).where(or_(*audit_filters))
        ) if audit_filters else None
        tournament_result = await db_session.execute(
            delete(Tournament).where(Tournament.id.in_(tournament_ids))
        ) if tournament_ids else None
        await db_session.flush()
        user_result = await db_session.execute(
            delete(User).where(User.id.in_(user_ids))
        ) if user_ids else None
        await db_session.commit()

    async with session_factory()() as verify_session:
        zero_counts = await verify_absence(
            verify_session,
            inventory,
            audit_subject_ids,
        )
    if absence_proof_context is not None:
        required_context = {
            "state_dir",
            "runner_source_sha",
            "app_sha",
            "marker_sha256",
            "gate_id_sha256",
        }
        if set(absence_proof_context) != required_context:
            raise RuntimeError("Live-user QA absence proof context is invalid.")
        state_dir = absence_proof_context["state_dir"]
        if not isinstance(state_dir, pathlib.Path):
            raise RuntimeError("Live-user QA absence proof state path is invalid.")
        proof = _absence_proof_payload(
            inventory,
            runner_source_sha=str(absence_proof_context["runner_source_sha"]),
            app_sha=str(absence_proof_context["app_sha"]),
            marker_sha256=str(absence_proof_context["marker_sha256"]),
            gate_id_sha256=str(absence_proof_context["gate_id_sha256"]),
            zero_counts=zero_counts,
        )
        _publish_state_absence_proof(state_dir, inventory, proof)
    return {
        "tournaments": int(tournament_result.rowcount or 0) if tournament_result else 0,
        "users": int(user_result.rowcount or 0) if user_result else 0,
        "media": int(media_result.rowcount or 0) if media_result else 0,
        "sessions": int(session_count or 0),
        "audit_logs": int(audit_result.rowcount or 0) if audit_result else 0,
    }


async def run(
    inventory: CleanupInventory,
    *,
    absence_proof_context: dict[str, object],
) -> int:
    try:
        result = await cleanup(
            inventory,
            absence_proof_context=absence_proof_context,
        )
        print(
            "Live-user QA cleanup verified absent: "
            f"tournaments={result['tournaments']} users={result['users']} "
            f"media={result['media']} sessions={result['sessions']} "
            f"audit_logs={result['audit_logs']}"
        )
        return 0
    finally:
        await dispose_engine()


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean one exact live-user QA inventory.")
    parser.add_argument("--marker", required=True)
    parser.add_argument("--inventory", type=pathlib.Path, required=True)
    parser.add_argument("--state-dir", type=pathlib.Path, required=True)
    parser.add_argument("--runner-source-sha", required=True)
    parser.add_argument("--app-sha", required=True)
    parser.add_argument("--marker-sha256", required=True)
    parser.add_argument("--gate-id-sha256", required=True)
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    if args.confirm != CONFIRMATION:
        parser.error(f"cleanup requires --confirm {CONFIRMATION}")
    if not MARKER_PATTERN.fullmatch(args.marker):
        parser.error("marker must start with liveqa- and contain only lowercase letters, digits, and dashes")
    try:
        inventory = load_inventory(args.inventory, expected_marker=args.marker)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    try:
        context: dict[str, object] = {
            "state_dir": args.state_dir,
            "runner_source_sha": args.runner_source_sha,
            "app_sha": args.app_sha,
            "marker_sha256": args.marker_sha256,
            "gate_id_sha256": args.gate_id_sha256,
        }
        _absence_proof_payload(
            inventory,
            runner_source_sha=args.runner_source_sha,
            app_sha=args.app_sha,
            marker_sha256=args.marker_sha256,
            gate_id_sha256=args.gate_id_sha256,
            zero_counts={key: 0 for key in ABSENCE_COUNT_FIELDS},
        )
    except RuntimeError as exc:
        parser.error(str(exc))
    return asyncio.run(run(inventory, absence_proof_context=context))


if __name__ == "__main__":
    raise SystemExit(main())
