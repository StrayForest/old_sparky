#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import sys

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM_ROOT))

from sqlalchemy import delete, func, select

from python_packages.platform_domain.deadlock.constants import RANKS
from python_packages.platform_infra.config import get_settings
from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.media.hard_delete import purge_deleted_media_metadata
from python_packages.platform_infra.models import (
    DeadlockDreamSlot,
    DeadlockProfile,
    PlayerProfile,
    PlayerTournamentCommitment,
    Tournament,
    TournamentDeadlockAssignmentRun,
    TournamentDeadlockCaptainRound,
    TournamentDeadlockReadyRound,
    TournamentMatch,
    TournamentParticipant,
    User,
)


ORGANIZER_EMAIL = "showcase-organizer@oldsparky.invalid"
ORGANIZER_NAME = "Old Sparky"
ORGANIZER_AVATAR_URL = "/assets/main_logo/old-sparky-arena-logo-v3.webp"
TOURNAMENT_SLUG = "old-sparky-profile-cup-8"
TOURNAMENT_NAME = "Old Sparky Profile Cup"
TEAM_SIZE = 7
STARTER_TEAM_SIZE = 6
TEAM_NAMES = (
    "Night Wardens",
    "Violet Guard",
    "Rift Hunters",
    "Iron Lanterns",
    "Astral Echo",
    "Veil Runners",
    "Neon Order",
    "Arc Sentinels",
)
HEROES = (
    "Abrams",
    "Kelvin",
    "Warden",
    "Viscous",
    "Pocket",
    "Vindicta",
    "Infernus",
    "Lash",
    "McGinnis",
    "Mirage",
    "Seven",
    "Bebop",
    "Ivy",
    "Yamato",
    "Shiv",
    "Dynamo",
    "Haze",
    "Paradox",
)
ROLES = ("Carry", "Semi-Carry", "Support", "Semi-Support")
FAKE_PLAYER_COUNT = len(TEAM_NAMES) * TEAM_SIZE - 1


def load_env_file(path: Path) -> None:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip("'").strip('"')


def fake_handle(index: int) -> str:
    stems = ("Vanta", "Nexus", "Ember", "Shade", "Astra", "Volt", "Raven", "Pulse")
    return f"{stems[(index - 1) % len(stems)]}{index:02d}"


async def ensure_organizer(db_session) -> User:
    organizer = await db_session.scalar(select(User).where(User.email == ORGANIZER_EMAIL))
    if organizer is None:
        organizer = User(email=ORGANIZER_EMAIL, display_name=ORGANIZER_NAME)
        db_session.add(organizer)
        await db_session.flush()
    organizer.display_name = ORGANIZER_NAME
    profile = await db_session.get(PlayerProfile, organizer.id)
    if profile is None:
        profile = PlayerProfile(
            user_id=organizer.id,
            display_name=ORGANIZER_NAME,
            handle="oldsparkyarena",
        )
        db_session.add(profile)
    profile.display_name = ORGANIZER_NAME
    profile.avatar_url = ORGANIZER_AVATAR_URL
    return organizer


async def ensure_fake_players(db_session) -> list[User]:
    emails = [
        f"profile-review-player-{index:02d}@oldsparky.invalid"
        for index in range(1, FAKE_PLAYER_COUNT + 1)
    ]
    existing = (
        await db_session.scalars(select(User).where(User.email.in_(emails)))
    ).all()
    by_email = {user.email: user for user in existing}
    users: list[User] = []
    for index, email in enumerate(emails, start=1):
        handle = fake_handle(index)
        user = by_email.get(email)
        if user is None:
            user = User(email=email, display_name=handle)
            db_session.add(user)
            await db_session.flush()
        user.display_name = handle
        user.status = "active"
        users.append(user)

        profile = await db_session.get(PlayerProfile, user.id)
        if profile is None:
            profile = PlayerProfile(user_id=user.id, display_name=handle)
            db_session.add(profile)
        profile.display_name = handle
        profile.handle = handle.casefold()
        profile.avatar_url = f"/assets/heroes/{HEROES[(index - 1) % len(HEROES)]}.png"
        profile.banner_url = None
        profile.bio = "Игрок сообщества Old Sparky. Профиль подготовлен для проверки турнирных составов."
        profile.contact_email = email
        profile.region = ("Europe", "Finland", "Germany", "Poland")[(index - 1) % 4]
        profile.discord_account = handle.casefold()

        deadlock_profile = await db_session.get(DeadlockProfile, user.id)
        if deadlock_profile is None:
            deadlock_profile = DeadlockProfile(
                user_id=user.id,
                rank=RANKS[(index - 1) % len(RANKS)],
                subrank=((index - 1) % 6) + 1,
                playtime="1501-2000",
                roles=[],
                pool=[],
            )
            db_session.add(deadlock_profile)
        deadlock_profile.rank = RANKS[(index - 1) % len(RANKS)]
        deadlock_profile.subrank = ((index - 1) % 6) + 1
        deadlock_profile.playtime = ("501-1000", "1001-1500", "1501-2000", "2001-3000")[(index - 1) % 4]
        deadlock_profile.roles = [ROLES[(index - 1) % len(ROLES)], ROLES[index % len(ROLES)]]
        deadlock_profile.pool = [HEROES[(index + offset) % len(HEROES)] for offset in range(5)]
        deadlock_profile.captain_priority = "yes" if (index - 1) % TEAM_SIZE == 0 else "neutral"

        existing_slots = {
            slot.slot_number: slot
            for slot in (
                await db_session.scalars(
                    select(DeadlockDreamSlot).where(DeadlockDreamSlot.user_id == user.id)
                )
            ).all()
        }
        for slot_number in range(1, 7):
            dream_slot = existing_slots.get(slot_number)
            if dream_slot is None:
                dream_slot = DeadlockDreamSlot(user_id=user.id, slot_number=slot_number)
                db_session.add(dream_slot)
            dream_slot.allowed_roles = [ROLES[(index + slot_number - 2) % len(ROLES)]]
            dream_slot.desired_heroes = [
                HEROES[(index + slot_number + offset - 2) % len(HEROES)]
                for offset in range(5)
            ]
    await db_session.flush()
    return users


def team_snapshot(team_id: str, team_name: str, members: list[User], seed: int) -> dict[str, object]:
    captain = members[0]
    starters = members[1:STARTER_TEAM_SIZE]
    reserve = members[STARTER_TEAM_SIZE]
    strength = round(7015.0 - seed * 71.7, 1)
    return {
        "team_id": team_id,
        "team_name": team_name,
        "starter_strength": strength,
        "starter_average_strength": round(strength / STARTER_TEAM_SIZE, 1),
        "captain": {
            "user_id": captain.id,
            "username": captain.display_name,
            "assigned_role": "captain",
        },
        "starter_slots": [
            {
                "slot_number": slot_number,
                "assigned_role": "player",
                "assigned_player": {
                    "user_id": player.id,
                    "username": player.display_name,
                },
            }
            for slot_number, player in enumerate(starters, start=1)
        ],
        "reserve_slot": {
            "slot_number": 6,
            "assigned_role": "substitute",
            "assigned_player": {
                "user_id": reserve.id,
                "username": reserve.display_name,
            },
        },
    }


async def assert_roster_is_available(
    db_session,
    *,
    user_ids: list[str],
    replaceable_tournament_id: str | None,
) -> None:
    statement = (
        select(PlayerTournamentCommitment, Tournament.name)
        .join(Tournament, Tournament.id == PlayerTournamentCommitment.tournament_id)
        .where(
            PlayerTournamentCommitment.user_id.in_(user_ids),
            PlayerTournamentCommitment.released_at.is_(None),
        )
    )
    if replaceable_tournament_id is not None:
        statement = statement.where(
            PlayerTournamentCommitment.tournament_id != replaceable_tournament_id
        )
    conflicts = (await db_session.execute(statement)).all()
    if conflicts:
        conflict_summary = ", ".join(
            f"{commitment.user_id}:{tournament_name}"
            for commitment, tournament_name in conflicts[:8]
        )
        raise RuntimeError(f"Roster contains active commitments: {conflict_summary}")


async def release_operator_qa_commitments(
    db_session,
    *,
    operator: User,
    released_at: datetime,
) -> int:
    rows = (
        await db_session.execute(
            select(PlayerTournamentCommitment, Tournament)
            .join(Tournament, Tournament.id == PlayerTournamentCommitment.tournament_id)
            .where(
                PlayerTournamentCommitment.user_id == operator.id,
                PlayerTournamentCommitment.released_at.is_(None),
            )
            .with_for_update()
        )
    ).all()
    unexpected = [
        tournament.slug
        for _, tournament in rows
        if not tournament.slug.startswith("qa-")
    ]
    if unexpected:
        raise RuntimeError(
            "Operator has active commitments outside retained qa-* tournaments: "
            + ", ".join(sorted(unexpected))
        )
    for commitment, _ in rows:
        commitment.released_at = released_at
        commitment.release_reason = "profile_review_fixture_reassigned"
    return len(rows)


async def seed(operator_email: str) -> dict[str, object]:
    settings = get_settings()
    if settings.platform_db_schema != "platform":
        raise RuntimeError(f"Refusing to seed non-platform schema: {settings.platform_db_schema}")
    if "platformdb" not in settings.platform_database_url:
        raise RuntimeError("Refusing to seed a database URL that does not point at platformdb.")

    now = datetime.now(UTC).replace(second=0, microsecond=0)
    async with session_factory()() as db_session:
        organizer = await ensure_organizer(db_session)
        operator = await db_session.scalar(select(User).where(User.email == operator_email))
        if operator is None:
            raise RuntimeError(f"Operator account not found: {operator_email}")
        fake_players = await ensure_fake_players(db_session)
        released_operator_commitments = await release_operator_qa_commitments(
            db_session,
            operator=operator,
            released_at=now,
        )

        # Keep the operator in the first roster as a regular player, not its captain.
        roster = [*fake_players[:5], operator, *fake_players[5:]]
        expected_roster_size = len(TEAM_NAMES) * TEAM_SIZE
        if len(roster) != expected_roster_size or len({user.id for user in roster}) != expected_roster_size:
            raise RuntimeError("Profile review roster must contain 56 unique players.")

        existing_tournament = await db_session.scalar(
            select(Tournament).where(Tournament.slug == TOURNAMENT_SLUG)
        )
        await assert_roster_is_available(
            db_session,
            user_ids=[user.id for user in roster],
            replaceable_tournament_id=existing_tournament.id if existing_tournament else None,
        )
        if existing_tournament is not None:
            await purge_deleted_media_metadata(
                db_session,
                tournament_ids=(existing_tournament.id,),
            )
            await db_session.execute(delete(Tournament).where(Tournament.id == existing_tournament.id))
            await db_session.flush()

        tournament = Tournament(
            slug=TOURNAMENT_SLUG,
            name=TOURNAMENT_NAME,
            description=(
                "Публичный турнир Old Sparky с восемью сформированными командами. "
                "Составы и профили игроков сохранены для ручной проверки интерфейса."
            ),
            cover_url="/assets/tournament-covers/tournament-cover-template-2-v1.webp",
            visibility="public",
            status="in_progress",
            format_slug="solo",
            allowed_ranks=list(RANKS),
            max_participants=expected_roster_size,
            registration_starts_at=now - timedelta(days=10),
            registration_closes_at=now - timedelta(days=3),
            ready_check_starts_at=now - timedelta(days=3, hours=-1),
            ready_check_ends_at=now - timedelta(days=3, hours=-2),
            captain_selection_starts_at=now - timedelta(days=3, hours=-3),
            starts_at=now - timedelta(hours=12),
            match_format="bo1",
            final_format="bo3",
            bracket_revision=1,
            teams_count=len(TEAM_NAMES),
            organizer_user_id=organizer.id,
        )
        db_session.add(tournament)
        await db_session.flush()

        for user in roster:
            db_session.add(
                TournamentParticipant(
                    tournament_id=tournament.id,
                    user_id=user.id,
                    entry_type="solo",
                    status="confirmed",
                )
            )

        ready_round = TournamentDeadlockReadyRound(
            tournament_id=tournament.id,
            status="closed",
            eligible_user_ids=[user.id for user in roster],
            initiated_by_user_id=organizer.id,
            closed_at=now - timedelta(days=2),
        )
        db_session.add(ready_round)
        await db_session.flush()
        captain_round = TournamentDeadlockCaptainRound(
            tournament_id=tournament.id,
            source_ready_round_id=ready_round.id,
            teams_count=len(TEAM_NAMES),
            status="finalized",
            initiated_by_user_id=organizer.id,
            closed_at=now - timedelta(days=2, hours=-1),
            finalized_at=now - timedelta(days=2, hours=-1),
        )
        db_session.add(captain_round)
        await db_session.flush()

        teams = [
            team_snapshot(
                str(seed),
                team_name,
                roster[(seed - 1) * TEAM_SIZE : seed * TEAM_SIZE],
                seed,
            )
            for seed, team_name in enumerate(TEAM_NAMES, start=1)
        ]
        for seed, team_name in enumerate(TEAM_NAMES, start=1):
            captain = roster[(seed - 1) * TEAM_SIZE]
            captain_profile = await db_session.get(PlayerProfile, captain.id)
            if captain_profile is not None and captain.id != operator.id:
                captain_profile.captain_team_name = team_name

        assignment_run = TournamentDeadlockAssignmentRun(
            tournament_id=tournament.id,
            source_captain_round_id=captain_round.id,
            source_ready_round_id=ready_round.id,
            created_by_user_id=organizer.id,
            status="locked",
            published_at=now - timedelta(days=1),
            published_by_user_id=organizer.id,
            locked_at=now - timedelta(days=1),
            locked_by_user_id=organizer.id,
            summary_text="Сформировано 8 команд по 7 игроков.",
            result_snapshot={"teams": teams},
            candidate_pool_user_ids=[user.id for user in roster],
            leftover_user_ids=[],
        )
        db_session.add(assignment_run)
        await db_session.flush()

        for team, team_name in zip(teams, TEAM_NAMES, strict=True):
            member_ids = [
                str(team["captain"]["user_id"]),
                *[
                    str(slot["assigned_player"]["user_id"])
                    for slot in team["starter_slots"]
                ],
                str(team["reserve_slot"]["assigned_player"]["user_id"]),
            ]
            for user_id in member_ids:
                db_session.add(
                    PlayerTournamentCommitment(
                        user_id=user_id,
                        tournament_id=tournament.id,
                        assignment_run_id=assignment_run.id,
                        team_id=str(team["team_id"]),
                        team_name=team_name,
                        activated_at=now - timedelta(days=1),
                    )
                )

        opening_pairs = ((1, 8), (4, 5), (3, 6), (2, 7))
        opening_matches: list[TournamentMatch] = []
        for sequence, (home_seed, away_seed) in enumerate(opening_pairs, start=1):
            match = TournamentMatch(
                tournament_id=tournament.id,
                title=f"Четвертьфинал {sequence}",
                round_number=1,
                sequence_number=sequence,
                home_label=TEAM_NAMES[home_seed - 1],
                away_label=TEAM_NAMES[away_seed - 1],
                home_team_id=str(home_seed),
                away_team_id=str(away_seed),
                scheduled_at=now + timedelta(hours=sequence * 2),
                status="scheduled",
            )
            db_session.add(match)
            opening_matches.append(match)
        await db_session.flush()

        semifinal_matches: list[TournamentMatch] = []
        for sequence in range(2):
            match = TournamentMatch(
                tournament_id=tournament.id,
                title=f"Полуфинал {sequence + 1}",
                round_number=2,
                sequence_number=sequence + 1,
                home_label="",
                away_label="",
                home_source_match_id=opening_matches[sequence * 2].id,
                away_source_match_id=opening_matches[sequence * 2 + 1].id,
                scheduled_at=now + timedelta(days=1, hours=sequence * 2),
                status="scheduled",
            )
            db_session.add(match)
            semifinal_matches.append(match)
        await db_session.flush()
        db_session.add(
            TournamentMatch(
                tournament_id=tournament.id,
                title="Финал",
                round_number=3,
                sequence_number=1,
                home_label="",
                away_label="",
                home_source_match_id=semifinal_matches[0].id,
                away_source_match_id=semifinal_matches[1].id,
                scheduled_at=now + timedelta(days=2),
                status="scheduled",
            )
        )

        await db_session.commit()
        return {
            "passed": True,
            "slug": TOURNAMENT_SLUG,
            "name": TOURNAMENT_NAME,
            "teams": len(TEAM_NAMES),
            "players": len(roster),
            "fake_profiles": len(fake_players),
            "operator_email": operator_email,
            "operator_team": TEAM_NAMES[0],
            "released_operator_commitments": released_operator_commitments,
        }


async def verify(operator_email: str) -> dict[str, object]:
    settings = get_settings()
    if settings.platform_db_schema != "platform" or "platformdb" not in settings.platform_database_url:
        raise RuntimeError("Refusing to verify a database outside platform.platformdb.")
    async with session_factory()() as db_session:
        tournament = await db_session.scalar(
            select(Tournament).where(Tournament.slug == TOURNAMENT_SLUG)
        )
        if tournament is None:
            raise RuntimeError(f"Tournament not found: {TOURNAMENT_SLUG}")
        assignment_run = await db_session.scalar(
            select(TournamentDeadlockAssignmentRun)
            .where(
                TournamentDeadlockAssignmentRun.tournament_id == tournament.id,
                TournamentDeadlockAssignmentRun.status == "locked",
            )
            .order_by(TournamentDeadlockAssignmentRun.created_at.desc())
            .limit(1)
        )
        if assignment_run is None:
            raise RuntimeError("Locked assignment run is missing.")
        participant_count = int(
            await db_session.scalar(
                select(func.count(TournamentParticipant.id)).where(
                    TournamentParticipant.tournament_id == tournament.id
                )
            )
            or 0
        )
        commitment_count = int(
            await db_session.scalar(
                select(func.count(PlayerTournamentCommitment.id)).where(
                    PlayerTournamentCommitment.tournament_id == tournament.id,
                    PlayerTournamentCommitment.released_at.is_(None),
                )
            )
            or 0
        )
        match_count = int(
            await db_session.scalar(
                select(func.count(TournamentMatch.id)).where(
                    TournamentMatch.tournament_id == tournament.id
                )
            )
            or 0
        )
        operator_team = await db_session.scalar(
            select(PlayerTournamentCommitment.team_name)
            .join(User, User.id == PlayerTournamentCommitment.user_id)
            .where(
                PlayerTournamentCommitment.tournament_id == tournament.id,
                PlayerTournamentCommitment.released_at.is_(None),
                User.email == operator_email,
            )
        )
        fake_profile_count = int(
            await db_session.scalar(
                select(func.count(PlayerProfile.user_id))
                .join(User, User.id == PlayerProfile.user_id)
                .where(
                    User.email.like("profile-review-player-%@oldsparky.invalid"),
                    PlayerProfile.handle.is_not(None),
                    PlayerProfile.avatar_url.is_not(None),
                    PlayerProfile.bio.is_not(None),
                    PlayerProfile.contact_email.is_not(None),
                    PlayerProfile.region.is_not(None),
                    PlayerProfile.discord_account.is_not(None),
                )
            )
            or 0
        )
        dream_slot_count = int(
            await db_session.scalar(
                select(func.count(DeadlockDreamSlot.id))
                .join(User, User.id == DeadlockDreamSlot.user_id)
                .where(User.email.like("profile-review-player-%@oldsparky.invalid"))
            )
            or 0
        )
        team_count = len((assignment_run.result_snapshot or {}).get("teams") or [])
        expected = {
            "participants": len(TEAM_NAMES) * TEAM_SIZE,
            "commitments": len(TEAM_NAMES) * TEAM_SIZE,
            "teams": len(TEAM_NAMES),
            "matches": len(TEAM_NAMES) - 1,
            "fake_profiles": FAKE_PLAYER_COUNT,
            "dream_slots": FAKE_PLAYER_COUNT * 6,
        }
        actual = {
            "participants": participant_count,
            "commitments": commitment_count,
            "teams": team_count,
            "matches": match_count,
            "fake_profiles": fake_profile_count,
            "dream_slots": dream_slot_count,
        }
        passed = (
            tournament.status == "in_progress"
            and tournament.visibility == "public"
            and operator_team == TEAM_NAMES[0]
            and actual == expected
        )
        return {
            "passed": passed,
            "slug": tournament.slug,
            "status": tournament.status,
            "visibility": tournament.visibility,
            "operator_team": operator_team,
            "expected": expected,
            "actual": actual,
        }


async def run_seed(operator_email: str) -> int:
    try:
        print(json.dumps(await seed(operator_email), ensure_ascii=False))
        return 0
    finally:
        await dispose_engine()


async def run_verify(operator_email: str) -> int:
    try:
        result = await verify(operator_email)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["passed"] else 1
    finally:
        await dispose_engine()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create the retained eight-team profile review tournament."
    )
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument(
        "--operator-email",
        required=True,
        help="Existing operator account to include in the retained review roster.",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--dry-run", action="store_true")
    action.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    operator_email = args.operator_email.strip().lower()
    if "@" not in operator_email or len(operator_email) > 254:
        parser.error("--operator-email must be a valid email address")
    if args.dry_run:
        print(
            json.dumps(
                {
                    "passed": True,
                    "slug": TOURNAMENT_SLUG,
                    "teams": list(TEAM_NAMES),
                    "players": len(TEAM_NAMES) * TEAM_SIZE,
                    "fake_profiles": FAKE_PLAYER_COUNT,
                    "operator_email": operator_email,
                },
                ensure_ascii=False,
            )
        )
        return 0
    if args.env_file is not None:
        load_env_file(args.env_file)
    if args.verify_only:
        return asyncio.run(run_verify(operator_email))
    return asyncio.run(run_seed(operator_email))


if __name__ == "__main__":
    raise SystemExit(main())
