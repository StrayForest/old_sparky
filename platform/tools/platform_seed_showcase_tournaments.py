#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import sys

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM_ROOT))

from sqlalchemy import func, select

from python_packages.platform_domain.deadlock.constants import RANKS
from python_packages.platform_infra.config import get_settings
from python_packages.platform_infra.db import dispose_engine, session_factory
from apps.platform_api.app.services.tournament_participant_capacity import (
    ensure_participant_slot_claimed,
)
from python_packages.platform_infra.models import (
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
SHOWCASE_USER_COUNT = 192
TEAM_SIZE = 6


@dataclass(frozen=True, slots=True)
class ShowcaseTournament:
    slug: str
    name: str
    description: str
    cover_index: int
    state: str
    starts_in_days: int
    teams_count: int
    participant_limit: int
    participant_count: int
    match_format: str = "bo1"
    final_format: str = "bo3"
    team_names: tuple[str, ...] = ()


TOURNAMENTS = (
    ShowcaseTournament(
        "showcase-citadel-showdown",
        "Citadel Showdown",
        "Вечерний турнир для игроков всех рангов. Команды уже сформированы, первые матчи доступны в сетке.",
        1,
        "in_progress",
        -1,
        8,
        64,
        52,
        team_names=(
            "Night Wardens",
            "Violet Guard",
            "Rift Hunters",
            "Iron Lanterns",
            "Astral Echo",
            "Veil Runners",
            "Obsidian Order",
            "Neon Revenants",
        ),
    ),
    ShowcaseTournament(
        "showcase-nightfall-gauntlet",
        "Nightfall Gauntlet",
        "Турнир с автоматическим формированием равных составов и сеткой на восемь команд.",
        2,
        "in_progress",
        0,
        8,
        64,
        49,
        "bo3",
        "bo5",
        (
            "Phantom Forge",
            "Citadel Keepers",
            "Midnight Pulse",
            "Eclipse Crew",
            "Arc Sentinels",
            "Hollow Crown",
            "Ether Wolves",
            "Crimson Orbit",
        ),
    ),
    ShowcaseTournament(
        "showcase-abyssal-clash",
        "Abyssal Clash",
        "Открытая регистрация на быстрый турнир выходного дня. Подтверждение готовности пройдёт перед формированием команд.",
        3,
        "registration_open",
        4,
        16,
        128,
        73,
    ),
    ShowcaseTournament(
        "showcase-vaultbreaker-cup",
        "Vaultbreaker Cup",
        "Соревновательный турнир с автоматическим балансом составов и финалом до трёх побед.",
        1,
        "registration_open",
        6,
        32,
        256,
        118,
        "bo3",
        "bo5",
    ),
    ShowcaseTournament(
        "showcase-eclipse-trials",
        "Eclipse Trials",
        "Камерный вечерний турнир для тех, кто хочет сыграть в организованной команде без предварительного состава.",
        2,
        "registration_open",
        8,
        8,
        64,
        37,
    ),
    ShowcaseTournament(
        "showcase-spire-ascension",
        "Spire Ascension",
        "Большой открытый турнир сообщества. Регистрация доступна игрокам любого уровня.",
        3,
        "registration_open",
        10,
        64,
        512,
        164,
        "bo3",
        "bo5",
    ),
    ShowcaseTournament(
        "showcase-astral-relay",
        "Astral Relay",
        "Динамичный турнир в формате BO1 с автоматическим посевом команд по силе состава.",
        1,
        "registration_open",
        12,
        16,
        128,
        68,
    ),
    ShowcaseTournament(
        "showcase-obsidian-crown",
        "Obsidian Crown",
        "Серия матчей для игроков, готовых проверить себя в сбалансированном составе.",
        2,
        "registration_open",
        14,
        32,
        256,
        91,
    ),
    ShowcaseTournament(
        "showcase-phantom-circuit",
        "Phantom Circuit",
        "Ночной кубок сообщества с короткими матчами и расширенным финалом.",
        3,
        "registration_open",
        16,
        16,
        128,
        44,
        "bo1",
        "bo5",
    ),
    ShowcaseTournament(
        "showcase-riftwalker-open",
        "Riftwalker Open",
        "Открытый турнир без готовых команд: зарегистрируйтесь, подтвердите участие и дождитесь распределения.",
        1,
        "registration_open",
        18,
        64,
        512,
        132,
    ),
    ShowcaseTournament(
        "showcase-midnight-protocol",
        "Midnight Protocol",
        "Регистрация завершена. Участники готовятся к подтверждению и автоматическому формированию составов.",
        2,
        "registration_closed",
        2,
        16,
        128,
        96,
    ),
    ShowcaseTournament(
        "showcase-veilforge-masters",
        "Veilforge Masters",
        "Регистрация завершена. Скоро начнётся формирование команд для основной стадии турнира.",
        3,
        "registration_closed",
        3,
        32,
        256,
        141,
        "bo3",
        "bo5",
    ),
)


def load_env_file(path: Path) -> None:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip("'").strip('"')


def tournament_cover_url(index: int) -> str:
    return f"/assets/tournament-covers/tournament-cover-template-{index}-v1.webp"


def showcase_handle(index: int) -> str:
    stems = (
        "Vanta",
        "Nexus",
        "Ember",
        "Shade",
        "Astra",
        "Volt",
        "Raven",
        "Pulse",
        "Frost",
        "Drift",
        "Nova",
        "Onyx",
        "Vesper",
        "Flare",
        "Cipher",
        "Echo",
    )
    return f"{stems[(index - 1) % len(stems)]}{index:03d}"


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


async def ensure_showcase_users(db_session) -> list[User]:
    emails = [f"showcase-player-{index:03d}@oldsparky.invalid" for index in range(1, SHOWCASE_USER_COUNT + 1)]
    existing_rows = (
        await db_session.scalars(select(User).where(User.email.in_(emails)))
    ).all()
    by_email = {row.email: row for row in existing_rows}
    users: list[User] = []
    for index, email in enumerate(emails, start=1):
        user = by_email.get(email)
        handle = showcase_handle(index)
        if user is None:
            user = User(email=email, display_name=handle)
            db_session.add(user)
            await db_session.flush()
        users.append(user)

        player_profile = await db_session.get(PlayerProfile, user.id)
        if player_profile is None:
            db_session.add(
                PlayerProfile(
                    user_id=user.id,
                    display_name=handle,
                    handle=handle.casefold(),
                )
            )
        deadlock_profile = await db_session.get(DeadlockProfile, user.id)
        if deadlock_profile is None:
            db_session.add(
                DeadlockProfile(
                    user_id=user.id,
                    rank=RANKS[(index - 1) % len(RANKS)],
                    subrank=((index - 1) % 6) + 1,
                    playtime="501-1000",
                    roles=["Carry", "Support"] if index % 2 else ["Semi-Carry", "Semi-Support"],
                    pool=["Haze", "Abrams", "Dynamo", "Warden", "Ivy"],
                    captain_priority="yes" if index % 9 == 0 else "neutral",
                )
            )
    await db_session.flush()
    return users


def schedule_for(config: ShowcaseTournament, now: datetime) -> dict[str, datetime]:
    starts_at = now.replace(second=0, microsecond=0) + timedelta(days=config.starts_in_days)
    if config.state == "registration_open":
        registration_closes_at = starts_at - timedelta(days=2)
        return {
            "registration_starts_at": now - timedelta(days=2),
            "registration_closes_at": registration_closes_at,
            "ready_check_starts_at": registration_closes_at + timedelta(hours=2),
            "ready_check_ends_at": registration_closes_at + timedelta(hours=3),
            "captain_selection_starts_at": registration_closes_at + timedelta(hours=3, minutes=10),
            "starts_at": starts_at,
        }
    if config.state == "registration_closed":
        registration_closes_at = now - timedelta(hours=4)
        return {
            "registration_starts_at": now - timedelta(days=5),
            "registration_closes_at": registration_closes_at,
            "ready_check_starts_at": now + timedelta(hours=4),
            "ready_check_ends_at": now + timedelta(hours=5),
            "captain_selection_starts_at": now + timedelta(hours=5, minutes=10),
            "starts_at": starts_at,
        }
    return {
        "registration_starts_at": now - timedelta(days=10),
        "registration_closes_at": now - timedelta(days=3),
        "ready_check_starts_at": now - timedelta(days=3, hours=-2),
        "ready_check_ends_at": now - timedelta(days=3, hours=-3),
        "captain_selection_starts_at": now - timedelta(days=3, hours=-4),
        "starts_at": starts_at,
    }


async def ensure_tournament(db_session, organizer: User, config: ShowcaseTournament, now: datetime) -> Tournament:
    tournament = await db_session.scalar(select(Tournament).where(Tournament.slug == config.slug))
    if tournament is None:
        tournament = Tournament(
            slug=config.slug,
            name=config.name,
            format_slug="solo",
            organizer_user_id=organizer.id,
        )
        db_session.add(tournament)
        await db_session.flush()

    tournament.name = config.name
    tournament.description = config.description
    tournament.cover_url = tournament_cover_url(config.cover_index)
    tournament.visibility = "public"
    tournament.status = config.state
    tournament.format_slug = "solo"
    tournament.allowed_ranks = list(RANKS)
    tournament.max_participants = config.participant_limit
    tournament.match_format = config.match_format
    tournament.final_format = config.final_format
    tournament.teams_count = config.teams_count
    tournament.organizer_user_id = organizer.id
    for field, value in schedule_for(config, now).items():
        setattr(tournament, field, value)
    return tournament


async def ensure_participants(db_session, tournament: Tournament, users: list[User], count: int) -> None:
    selected = users[:count]
    existing_user_ids = set(
        (
            await db_session.scalars(
                select(TournamentParticipant.user_id).where(
                    TournamentParticipant.tournament_id == tournament.id,
                    TournamentParticipant.user_id.in_([user.id for user in selected]),
                )
            )
        ).all()
    )
    participants: list[TournamentParticipant] = []
    for user in selected:
        if user.id in existing_user_ids:
            continue
        participants.append(
            TournamentParticipant(
                tournament_id=tournament.id,
                user_id=user.id,
                entry_type="solo",
                status="confirmed" if tournament.status == "in_progress" else "registered",
            )
        )
    db_session.add_all(participants)
    await db_session.flush()
    for participant in participants:
        await ensure_participant_slot_claimed(
            db_session,
            tournament_id=tournament.id,
            max_participants=tournament.max_participants,
            participant_id=participant.id,
        )


def team_snapshot(team_id: str, team_name: str, members: list[User], seed: int) -> dict[str, object]:
    captain, *starters = members
    strength = 6900 - seed * 73
    return {
        "team_id": team_id,
        "team_name": team_name,
        "starter_strength": strength,
        "starter_average_strength": round(strength / TEAM_SIZE, 4),
        "captain": {
            "user_id": captain.id,
            "username": captain.display_name,
            "assigned_role": "captain",
        },
        "starter_slots": [
            {
                "slot_number": slot,
                "assigned_role": "player",
                "assigned_player": {
                    "user_id": member.id,
                    "username": member.display_name,
                },
            }
            for slot, member in enumerate(starters, start=1)
        ],
    }


async def ensure_bracket(
    db_session,
    tournament: Tournament,
    organizer: User,
    team_names: tuple[str, ...],
    team_users: list[User],
    now: datetime,
) -> None:
    existing_run = await db_session.scalar(
        select(TournamentDeadlockAssignmentRun.id).where(
            TournamentDeadlockAssignmentRun.tournament_id == tournament.id
        )
    )
    existing_match_count = int(
        await db_session.scalar(
            select(func.count(TournamentMatch.id)).where(TournamentMatch.tournament_id == tournament.id)
        )
        or 0
    )
    if existing_run is not None or existing_match_count:
        return

    roster_size = len(team_names) * TEAM_SIZE
    members = team_users[:roster_size]
    if len(members) != roster_size:
        raise RuntimeError(f"Not enough showcase players for {tournament.name}")

    ready_round = TournamentDeadlockReadyRound(
        tournament_id=tournament.id,
        status="closed",
        eligible_user_ids=[user.id for user in members],
        initiated_by_user_id=organizer.id,
        closed_at=now - timedelta(days=2),
    )
    db_session.add(ready_round)
    await db_session.flush()
    captain_round = TournamentDeadlockCaptainRound(
        tournament_id=tournament.id,
        source_ready_round_id=ready_round.id,
        teams_count=len(team_names),
        status="finalized",
        initiated_by_user_id=organizer.id,
        closed_at=now - timedelta(days=2, hours=-1),
        finalized_at=now - timedelta(days=2, hours=-1),
    )
    db_session.add(captain_round)
    await db_session.flush()

    teams = [
        team_snapshot(
            str(index),
            team_name,
            members[(index - 1) * TEAM_SIZE : index * TEAM_SIZE],
            index,
        )
        for index, team_name in enumerate(team_names, start=1)
    ]
    assignment_run = TournamentDeadlockAssignmentRun(
        tournament_id=tournament.id,
        source_captain_round_id=captain_round.id,
        source_ready_round_id=ready_round.id,
        created_by_user_id=organizer.id,
        status="locked",
        published_at=now - timedelta(days=1, hours=4),
        published_by_user_id=organizer.id,
        locked_at=now - timedelta(days=1, hours=4),
        locked_by_user_id=organizer.id,
        summary_text=f"Сформировано {len(teams)} команд.",
        result_snapshot={"teams": teams},
        candidate_pool_user_ids=[user.id for user in members],
        leftover_user_ids=[],
    )
    db_session.add(assignment_run)
    await db_session.flush()

    for team, team_name in zip(teams, team_names, strict=True):
        for user_id in [
            str(team["captain"]["user_id"]),
            *[
                str(slot["assigned_player"]["user_id"])
                for slot in team["starter_slots"]
            ],
        ]:
            db_session.add(
                PlayerTournamentCommitment(
                    user_id=user_id,
                    tournament_id=tournament.id,
                    assignment_run_id=assignment_run.id,
                    team_id=str(team["team_id"]),
                    team_name=team_name,
                    activated_at=now - timedelta(days=1, hours=4),
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
            home_label=team_names[home_seed - 1],
            away_label=team_names[away_seed - 1],
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
            home_label=f"Победитель четвертьфинала {sequence * 2 + 1}",
            away_label=f"Победитель четвертьфинала {sequence * 2 + 2}",
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
            home_label="Победитель полуфинала 1",
            away_label="Победитель полуфинала 2",
            home_source_match_id=semifinal_matches[0].id,
            away_source_match_id=semifinal_matches[1].id,
            scheduled_at=now + timedelta(days=2),
            status="scheduled",
        )
    )
    tournament.bracket_revision = 1


async def seed() -> dict[str, object]:
    settings = get_settings()
    if settings.platform_db_schema != "platform":
        raise RuntimeError(f"Refusing to seed non-platform schema: {settings.platform_db_schema}")
    if "platformdb" not in settings.platform_database_url:
        raise RuntimeError("Refusing to seed a database URL that does not point at platformdb.")

    now = datetime.now(UTC)
    async with session_factory()() as db_session:
        organizer = await ensure_organizer(db_session)
        users = await ensure_showcase_users(db_session)
        seeded: list[dict[str, object]] = []
        bracket_index = 0
        for config in TOURNAMENTS:
            tournament = await ensure_tournament(db_session, organizer, config, now)
            if config.team_names:
                roster_start = bracket_index * len(config.team_names) * TEAM_SIZE
                roster_end = roster_start + len(config.team_names) * TEAM_SIZE
                roster_users = users[roster_start:roster_end]
                roster_user_ids = {user.id for user in roster_users}
                participant_users = roster_users + [
                    user for user in users if user.id not in roster_user_ids
                ]
                await ensure_participants(
                    db_session,
                    tournament,
                    participant_users,
                    config.participant_count,
                )
                await ensure_bracket(
                    db_session,
                    tournament,
                    organizer,
                    config.team_names,
                    roster_users,
                    now,
                )
                bracket_index += 1
            else:
                await ensure_participants(db_session, tournament, users, config.participant_count)
            seeded.append(
                {
                    "slug": config.slug,
                    "name": config.name,
                    "status": config.state,
                    "participants": config.participant_count,
                    "bracket_teams": len(config.team_names),
                }
            )
        await db_session.commit()
        return {
            "passed": True,
            "organizer": ORGANIZER_NAME,
            "organizer_avatar_url": ORGANIZER_AVATAR_URL,
            "tournaments": seeded,
        }


async def run_seed() -> int:
    try:
        result = await seed()
        print(json.dumps(result, ensure_ascii=False))
        return 0
    finally:
        await dispose_engine()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create the retained Old Sparky showcase tournament catalog."
    )
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        print(
            json.dumps(
                {
                    "passed": True,
                    "organizer": ORGANIZER_NAME,
                    "tournaments": [
                        {
                            "slug": row.slug,
                            "name": row.name,
                            "status": row.state,
                            "bracket_teams": len(row.team_names),
                        }
                        for row in TOURNAMENTS
                    ],
                },
                ensure_ascii=False,
            )
        )
        return 0
    if args.env_file is not None:
        load_env_file(args.env_file)
    return asyncio.run(run_seed())


if __name__ == "__main__":
    raise SystemExit(main())
