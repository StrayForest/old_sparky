from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from python_packages.platform_domain.tournaments import (
    TournamentWorkflowError,
    bracket_round_count,
    match_title_for_round,
    strength_seed_team_ids,
)
from python_packages.platform_infra.models import (
    Tournament,
    TournamentDeadlockAssignmentRun,
    TournamentMatch,
    new_uuid,
)


@dataclass(frozen=True, slots=True)
class BracketTeamSnapshot:
    team_id: str
    starter_strength: float
    starter_average_strength: float


def team_label(team_id: str | None) -> str:
    return f"Team {team_id}" if team_id else "TBD"


def locked_team_snapshots(
    run_row: TournamentDeadlockAssignmentRun,
) -> tuple[BracketTeamSnapshot, ...]:
    snapshot = dict(run_row.result_snapshot or {})
    teams: list[BracketTeamSnapshot] = []
    for raw_team in list(snapshot.get("teams") or []):
        if not isinstance(raw_team, dict) or raw_team.get("team_id") is None:
            continue
        teams.append(
            BracketTeamSnapshot(
                team_id=str(raw_team["team_id"]).strip(),
                starter_strength=float(raw_team.get("starter_strength") or 0.0),
                starter_average_strength=float(
                    raw_team.get("starter_average_strength") or 0.0
                ),
            )
        )
    return tuple(teams)


def automatic_opening_team_ids(
    run_row: TournamentDeadlockAssignmentRun,
) -> tuple[str, ...]:
    return strength_seed_team_ids(
        [
            {
                "team_id": team.team_id,
                "starter_strength": team.starter_strength,
            }
            for team in locked_team_snapshots(run_row)
        ]
    )


async def lock_tournament_for_bracket(
    db_session: AsyncSession,
    tournament_id: str,
) -> Tournament:
    tournament = await db_session.scalar(
        select(Tournament)
        .where(Tournament.id == tournament_id)
        .with_for_update()
    )
    if tournament is None:
        raise TournamentWorkflowError("Tournament no longer exists.")
    return tournament


def ensure_expected_revision(
    tournament: Tournament,
    expected_revision: int | None,
) -> None:
    if expected_revision is None:
        return
    if int(tournament.bracket_revision or 0) != expected_revision:
        raise TournamentWorkflowError(
            "Bracket changed in another session. Refresh it and retry."
        )


def advance_revision(tournament: Tournament) -> int:
    tournament.bracket_revision = int(tournament.bracket_revision or 0) + 1
    return tournament.bracket_revision


async def create_full_bracket_graph(
    db_session: AsyncSession,
    *,
    tournament: Tournament,
    locked_run: TournamentDeadlockAssignmentRun,
) -> tuple[list[TournamentMatch], list[TournamentMatch]]:
    ordered_team_ids = automatic_opening_team_ids(locked_run)
    total_rounds = bracket_round_count(len(ordered_team_ids))

    all_matches: list[TournamentMatch] = []
    previous_round: list[TournamentMatch] = []
    opening_round: list[TournamentMatch] = []
    opening_match_count = len(ordered_team_ids) // 2
    for index in range(opening_match_count):
        home_team_id = ordered_team_ids[index * 2]
        away_team_id = ordered_team_ids[index * 2 + 1]
        match = TournamentMatch(
            id=new_uuid(),
            tournament_id=tournament.id,
            title=match_title_for_round(
                round_number=1,
                total_rounds=total_rounds,
                sequence_number=index + 1,
            ),
            round_number=1,
            sequence_number=index + 1,
            home_label=team_label(home_team_id),
            away_label=team_label(away_team_id),
            home_team_id=home_team_id,
            away_team_id=away_team_id,
            status="scheduled",
        )
        db_session.add(match)
        all_matches.append(match)
        opening_round.append(match)
        previous_round.append(match)

    for round_number in range(2, total_rounds + 1):
        current_round: list[TournamentMatch] = []
        for index in range(len(previous_round) // 2):
            home_source = previous_round[index * 2]
            away_source = previous_round[index * 2 + 1]
            match = TournamentMatch(
                id=new_uuid(),
                tournament_id=tournament.id,
                title=match_title_for_round(
                    round_number=round_number,
                    total_rounds=total_rounds,
                    sequence_number=index + 1,
                ),
                round_number=round_number,
                sequence_number=index + 1,
                home_label=f"Winner R{home_source.round_number}M{home_source.sequence_number}",
                away_label=f"Winner R{away_source.round_number}M{away_source.sequence_number}",
                home_source_match_id=home_source.id,
                away_source_match_id=away_source.id,
                status="scheduled",
            )
            db_session.add(match)
            all_matches.append(match)
            current_round.append(match)
        previous_round = current_round

    advance_revision(tournament)
    await db_session.flush()
    return all_matches, opening_round


async def destination_match_for_source(
    db_session: AsyncSession,
    *,
    tournament_id: str,
    source_match_id: str,
) -> TournamentMatch | None:
    return await db_session.scalar(
        select(TournamentMatch).where(
            TournamentMatch.tournament_id == tournament_id,
            or_(
                TournamentMatch.home_source_match_id == source_match_id,
                TournamentMatch.away_source_match_id == source_match_id,
            ),
        )
    )


async def propagate_match_winner(
    db_session: AsyncSession,
    *,
    match: TournamentMatch,
) -> TournamentMatch | None:
    destination = await destination_match_for_source(
        db_session,
        tournament_id=match.tournament_id,
        source_match_id=match.id,
    )
    if destination is None:
        return None
    if destination.status != "scheduled":
        raise TournamentWorkflowError(
            "The dependent match has already started and cannot accept a different winner."
        )
    if not match.winner_team_id:
        raise TournamentWorkflowError("Completed match is missing a stable winner team id.")
    winner_label = team_label(match.winner_team_id)
    if destination.home_source_match_id == match.id:
        destination.home_team_id = match.winner_team_id
        destination.home_label = winner_label
    if destination.away_source_match_id == match.id:
        destination.away_team_id = match.winner_team_id
        destination.away_label = winner_label
    return destination

async def clear_match_result_and_progression(
    db_session: AsyncSession,
    *,
    match: TournamentMatch,
) -> TournamentMatch | None:
    destination = await destination_match_for_source(
        db_session,
        tournament_id=match.tournament_id,
        source_match_id=match.id,
    )
    if destination is not None:
        if (
            destination.status != "scheduled"
            or destination.home_score is not None
            or destination.away_score is not None
            or destination.winner_team_id is not None
        ):
            raise TournamentWorkflowError(
                "The dependent match has already started. Reopen it before changing this result."
            )
        if destination.home_source_match_id == match.id:
            destination.home_team_id = None
            destination.home_label = (
                f"Winner R{match.round_number}M{match.sequence_number}"
            )
        if destination.away_source_match_id == match.id:
            destination.away_team_id = None
            destination.away_label = (
                f"Winner R{match.round_number}M{match.sequence_number}"
            )

    match.status = "scheduled"
    match.home_score = None
    match.away_score = None
    match.winner_side = None
    match.winner_team_id = None
    match.report_note = None
    match.reported_at = None
    match.reported_by_user_id = None
    return destination
