from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from statistics import pstdev
from typing import Any

from python_packages.platform_domain.deadlock.constants import ROLE_OPTIONS
from python_packages.platform_domain.deadlock.dream_slots import expand_dream_slot_payloads
from python_packages.platform_domain.deadlock.strength import calculate_player_strength
from python_packages.platform_domain.deadlock.team_names import choose_available_team_name

ROLE_ORDER = {
    "Carry": 0,
    "Semi-Carry": 1,
    "Support": 2,
    "Semi-Support": 3,
}

ROLE_PRIORITY_WEIGHTS = {
    "Carry": 4,
    "Semi-Carry": 3,
    "Support": 1,
    "Semi-Support": 1,
}


class AutoAssignmentError(ValueError):
    """Raised when the shared auto-assignment engine cannot produce a valid solution."""


@dataclass(frozen=True, slots=True)
class AutoAssignmentSettings:
    balance_threshold_percent: float = 5.0
    pool_steps: tuple[float, ...] = (1.20, 1.35, 1.50)
    role_slack: int = 8
    role_rescue_per_role: int = 12
    team_pair_window: int = 4
    swap_candidates_per_role: int = 2
    leftover_k_per_role: int = 8
    vnd_passes: int = 3
    rank_power: Mapping[str, int] | None = None


@dataclass(frozen=True)
class PlayerModel:
    user_id: str
    username: str | None
    team_name: str | None
    rank: str
    subrank: int
    playtime: str | None
    pool: tuple[str, ...]
    roles: tuple[str, ...]
    strength: float


@dataclass(frozen=True)
class SlotPreference:
    team_id: str
    slot_number: int
    allowed_roles: tuple[str, ...]
    desired_heroes: tuple[str, ...]


@dataclass
class Assignment:
    player: PlayerModel
    slot: SlotPreference
    assigned_role: str
    tier: int
    desired_match_count: int
    projected_spread_percent: float
    cap_overflow: int
    projected_mad_percent: float = 0.0
    hierarchy_penalty: float = 0.0


@dataclass
class TeamState:
    team_id: str
    captain: PlayerModel
    captain_assigned_role: str
    all_slots: list[SlotPreference]
    slots: list[SlotPreference]
    reserve_slot: SlotPreference | None = None
    assignments: list[Assignment] = field(default_factory=list)
    reserve_assignment: Assignment | None = None

    @property
    def current_strength(self) -> float:
        return self.captain.strength + sum(item.player.strength for item in self.assignments)

    def role_counts(self) -> dict[str, int]:
        counts = {role: 0 for role in ROLE_OPTIONS}
        counts[self.captain_assigned_role] += 1
        for assignment in self.assignments:
            counts[assignment.assigned_role] += 1
        return counts

    @property
    def starter_count(self) -> int:
        return 1 + len(self.assignments)


@dataclass(frozen=True)
class CorrectionSummary:
    stage: int
    threshold: float
    spread: float
    accepted_swap_moves: int = 0
    accepted_replacement_moves: int = 0
    accepted_hierarchy_moves: int = 0


@dataclass(frozen=True)
class OptimizationSummary:
    threshold: float
    spread: float
    candidate_pool_size: int
    selected_player_count: int
    source: str
    solver_improved: bool
    stage: int | None = None
    mad_percent: float = 0.0
    std_percent: float = 0.0
    pool_step: float | None = None
    role_rescue_used: bool = False
    accepted_swap_moves: int = 0
    accepted_replacement_moves: int = 0
    accepted_hierarchy_moves: int = 0


@dataclass(frozen=True)
class AutoAssignmentRun:
    teams: tuple[TeamState, ...]
    optimization_summary: OptimizationSummary
    result_snapshot: dict[str, Any]
    summary_text: str
    target_strength: float
    candidate_pool: tuple[PlayerModel, ...]
    leftovers: tuple[PlayerModel, ...]


@dataclass(frozen=True, slots=True)
class TeamSearchMetrics:
    strength: float
    absolute_deviation: float
    cap_overflow: int
    hierarchy_penalty: float
    selected_strengths: tuple[float, ...]
    core_quality_values: tuple[float, ...]
    desired_score: int


@dataclass(frozen=True, slots=True)
class SearchMetrics:
    team_ids: tuple[str, ...]
    teams_by_id: Mapping[str, TeamSearchMetrics]
    strengths_ascending: tuple[tuple[float, str], ...]


@dataclass(frozen=True, slots=True)
class CandidateSearchMetrics:
    spread: float
    mad_percent: float
    cap_overflow: int
    hierarchy_penalty: float
    selected_strength: float
    core_quality: float
    desired_score: int


@dataclass(frozen=True, slots=True)
class PrimaryProjectionContext:
    team_id: str
    base_strength: float
    minimum_other_strength: float | None
    maximum_other_strength: float | None
    absolute_deviation_total: float
    team_count: int


class AutoAssignmentEngine:
    def __init__(self, settings: AutoAssignmentSettings | None = None) -> None:
        self._settings = settings or AutoAssignmentSettings()
        self._last_result_snapshot: dict[str, Any] | None = None
        self._validate_settings()

    @property
    def settings(self) -> AutoAssignmentSettings:
        return self._settings

    def get_last_result_snapshot(self) -> dict[str, Any] | None:
        return self._last_result_snapshot

    def build_player(self, row: Mapping[str, Any]) -> PlayerModel:
        return self._build_player(row)

    def build_team_states(
        self,
        captain_rows: Sequence[Mapping[str, Any]],
        dream_slot_rows: Sequence[Mapping[str, Any]],
    ) -> list[TeamState]:
        return self._build_team_states(captain_rows, dream_slot_rows)

    def solve(
        self,
        captain_rows: Sequence[Mapping[str, Any]],
        ready_player_rows: Sequence[Mapping[str, Any]],
        dream_slot_rows: Sequence[Mapping[str, Any]],
    ) -> AutoAssignmentRun:
        self._last_result_snapshot = None
        ready_players = [self._build_player(row) for row in ready_player_rows]
        template_teams = self._build_team_states(captain_rows, dream_slot_rows)
        required_slots = sum(len(team.slots) for team in template_teams)
        if len(ready_players) < required_slots:
            raise AutoAssignmentError("Not enough players to fill the requested teams.")

        prepared_seed = self._prepare_seed_solution(template_teams, ready_players, required_slots)
        if prepared_seed is None:
            raise AutoAssignmentError("Unable to produce a valid team assignment for the current constraints.")

        candidate_pool, target_strength, seed_teams, leftovers, pool_step, role_rescue_used = prepared_seed
        correction_summary = self._run_correction_pass(seed_teams, leftovers, target_strength)
        final_teams = seed_teams
        final_summary = self._build_optimization_summary(
            final_teams,
            target_strength,
            threshold=correction_summary.threshold,
            candidate_pool_size=len(candidate_pool),
            selected_player_count=required_slots,
            source="bounded deterministic VND",
            solver_improved=False,
            stage=correction_summary.stage,
            pool_step=pool_step,
            role_rescue_used=role_rescue_used,
            accepted_swap_moves=correction_summary.accepted_swap_moves,
            accepted_replacement_moves=correction_summary.accepted_replacement_moves,
            accepted_hierarchy_moves=correction_summary.accepted_hierarchy_moves,
        )

        reserve_candidates = self._build_reserve_pool(ready_players, final_teams)
        self._assign_reserves(final_teams, reserve_candidates)
        self._last_result_snapshot = self._build_result_snapshot(final_teams, final_summary)
        return AutoAssignmentRun(
            teams=tuple(final_teams),
            optimization_summary=final_summary,
            result_snapshot=self._last_result_snapshot,
            summary_text=self._build_summary(final_teams, final_summary),
            target_strength=target_strength,
            candidate_pool=tuple(candidate_pool),
            leftovers=tuple(leftovers),
        )

    def _validate_settings(self) -> None:
        if self._settings.balance_threshold_percent <= 0:
            raise AutoAssignmentError("balance_threshold_percent must be greater than 0.")
        if not self._settings.pool_steps:
            raise AutoAssignmentError("pool_steps must not be empty.")
        if any(step < 1.0 for step in self._settings.pool_steps):
            raise AutoAssignmentError("pool_steps values must be greater than or equal to 1.0.")
        if self._settings.role_slack < 0:
            raise AutoAssignmentError("role_slack cannot be negative.")
        if self._settings.role_rescue_per_role < 0:
            raise AutoAssignmentError("role_rescue_per_role cannot be negative.")
        if self._settings.team_pair_window < 1:
            raise AutoAssignmentError("team_pair_window must be greater than or equal to 1.")
        if self._settings.swap_candidates_per_role < 1:
            raise AutoAssignmentError("swap_candidates_per_role must be greater than or equal to 1.")
        if self._settings.leftover_k_per_role < 1:
            raise AutoAssignmentError("leftover_k_per_role must be greater than or equal to 1.")
        if self._settings.vnd_passes < 1:
            raise AutoAssignmentError("vnd_passes must be greater than or equal to 1.")

    def _build_team_states(
        self,
        captain_rows: Sequence[Mapping[str, Any]],
        dream_slot_rows: Sequence[Mapping[str, Any]],
    ) -> list[TeamState]:
        team_ids = [str(row["team_id"]) for row in captain_rows]
        expanded_slots = expand_dream_slot_payloads(team_ids, dream_slot_rows, total_slots=6)

        team_states: list[TeamState] = []
        for row in captain_rows:
            team_id = str(row["team_id"])
            full_slots = [
                SlotPreference(
                    team_id=team_id,
                    slot_number=slot.slot_number,
                    allowed_roles=tuple(slot.allowed_roles or ROLE_OPTIONS),
                    desired_heroes=tuple(slot.desired_heroes or ()),
                )
                for slot in expanded_slots.get(team_id, ())
            ]
            starter_slots = list(full_slots[:5])
            reserve_slot = full_slots[5] if len(full_slots) > 5 else None
            captain = self._build_player(row)
            captain_role = self._choose_captain_role(captain.roles)
            team_states.append(
                TeamState(
                    team_id=team_id,
                    captain=captain,
                    captain_assigned_role=captain_role,
                    all_slots=starter_slots,
                    slots=list(starter_slots),
                    reserve_slot=reserve_slot,
                )
            )

        return sorted(team_states, key=lambda item: self._team_sort_key(item.team_id))

    def _build_optimization_summary(
        self,
        teams: list[TeamState],
        target_strength: float,
        *,
        threshold: float,
        candidate_pool_size: int,
        selected_player_count: int,
        source: str,
        solver_improved: bool,
        stage: int | None,
        pool_step: float | None = None,
        role_rescue_used: bool = False,
        accepted_swap_moves: int = 0,
        accepted_replacement_moves: int = 0,
        accepted_hierarchy_moves: int = 0,
        include_std: bool = True,
    ) -> OptimizationSummary:
        return OptimizationSummary(
            threshold=threshold,
            spread=self._compute_spread_percent(teams, target_strength),
            candidate_pool_size=candidate_pool_size,
            selected_player_count=selected_player_count,
            source=source,
            solver_improved=solver_improved,
            stage=stage,
            mad_percent=self._compute_mad_percent(teams, target_strength),
            std_percent=(
                self._compute_std_percent(teams, target_strength)
                if include_std
                else 0.0
            ),
            pool_step=pool_step,
            role_rescue_used=role_rescue_used,
            accepted_swap_moves=accepted_swap_moves,
            accepted_replacement_moves=accepted_replacement_moves,
            accepted_hierarchy_moves=accepted_hierarchy_moves,
        )

    @staticmethod
    def _clone_team_state(team: TeamState) -> TeamState:
        return TeamState(
            team_id=team.team_id,
            captain=team.captain,
            captain_assigned_role=team.captain_assigned_role,
            all_slots=list(team.all_slots),
            slots=list(team.slots),
            reserve_slot=team.reserve_slot,
            assignments=list(team.assignments),
            reserve_assignment=team.reserve_assignment,
        )

    @classmethod
    def _clone_team_states(cls, teams: list[TeamState]) -> list[TeamState]:
        return [cls._clone_team_state(team) for team in teams]

    @classmethod
    def _clone_selected_team_states(
        cls,
        teams: list[TeamState],
        team_ids: set[str],
    ) -> list[TeamState]:
        return [
            cls._clone_team_state(team) if team.team_id in team_ids else team
            for team in teams
        ]

    def _build_candidate_pool(
        self,
        ready_players: list[PlayerModel],
        required_slots: int,
        *,
        step_multiplier: float | None = None,
        singleton_requirements: dict[str, int] | None = None,
    ) -> list[PlayerModel]:
        sorted_players = sorted(ready_players, key=lambda item: (-item.strength, item.user_id))
        if len(sorted_players) <= required_slots:
            return sorted_players

        requested_size = min(
            len(sorted_players),
            max(
                required_slots,
                math.ceil(required_slots * float(step_multiplier or self._settings.pool_steps[0])),
            ),
        )
        role_floor = {
            role: max(
                int(singleton_requirements.get(role, 0) if singleton_requirements else 0)
                + int(self._settings.role_slack),
                int(self._settings.role_slack),
            )
            for role in ROLE_OPTIONS
        }
        role_ranked = {
            role: [
                player for player in sorted_players if role in tuple(player.roles or ROLE_OPTIONS)
            ]
            for role in ROLE_OPTIONS
        }

        pool: list[PlayerModel] = []
        seen: set[str] = set()

        def add_players(players: list[PlayerModel], limit: int | None = None) -> None:
            added = 0
            for player in players:
                if player.user_id in seen:
                    continue
                pool.append(player)
                seen.add(player.user_id)
                added += 1
                if limit is not None and added >= limit:
                    break

        add_players(sorted_players[:requested_size])
        for role in ROLE_OPTIONS:
            current_role_count = sum(
                1 for player in pool if role in tuple(player.roles or ROLE_OPTIONS)
            )
            missing_role_count = max(role_floor[role] - current_role_count, 0)
            if missing_role_count > 0:
                add_players(role_ranked[role], missing_role_count)
        return pool

    def _build_correction_thresholds(self) -> list[float]:
        return [float(self._settings.balance_threshold_percent)] * int(self._settings.vnd_passes)

    def _singleton_role_requirements(self, template_teams: list[TeamState]) -> dict[str, int]:
        requirements = {role: 0 for role in ROLE_OPTIONS}
        for team in template_teams:
            for slot in team.all_slots:
                allowed_roles = tuple(slot.allowed_roles or ROLE_OPTIONS)
                if len(allowed_roles) == 1:
                    requirements[allowed_roles[0]] += 1
        return requirements

    def _build_candidate_pool_steps(
        self,
        template_teams: list[TeamState],
        ready_players: list[PlayerModel],
        required_slots: int,
    ) -> list[tuple[float, list[PlayerModel], bool]]:
        sorted_players = sorted(ready_players, key=lambda item: (-item.strength, item.user_id))
        singleton_requirements = self._singleton_role_requirements(template_teams)
        steps: list[tuple[float, list[PlayerModel], bool]] = []
        for step_multiplier in self._settings.pool_steps:
            pool = self._build_candidate_pool(
                sorted_players,
                required_slots,
                step_multiplier=step_multiplier,
                singleton_requirements=singleton_requirements,
            )
            steps.append((float(step_multiplier), pool, False))

        if steps:
            rescue_roles = self._roles_missing_floor_coverage(
                steps[-1][1],
                singleton_requirements,
            )
            if rescue_roles:
                rescued_pool = self._apply_role_rescue(
                    steps[-1][1],
                    sorted_players,
                    rescue_roles,
                )
                steps.append((float(self._settings.pool_steps[-1]), rescued_pool, True))
        return steps

    def _roles_missing_floor_coverage(
        self,
        candidate_pool: list[PlayerModel],
        singleton_requirements: dict[str, int],
    ) -> list[str]:
        counts = {role: 0 for role in ROLE_OPTIONS}
        for player in candidate_pool:
            for role in tuple(player.roles or ROLE_OPTIONS):
                counts[role] += 1
        return [
            role
            for role, required_count in singleton_requirements.items()
            if required_count > 0 and counts.get(role, 0) < required_count
        ]

    def _apply_role_rescue(
        self,
        candidate_pool: list[PlayerModel],
        ready_players: list[PlayerModel],
        rescue_roles: list[str],
    ) -> list[PlayerModel]:
        rescued = list(candidate_pool)
        seen: set[str] = {player.user_id for player in candidate_pool}
        rescue_limit = int(self._settings.role_rescue_per_role)
        sorted_players = sorted(ready_players, key=lambda item: (-item.strength, item.user_id))
        for role in rescue_roles:
            added = 0
            for player in sorted_players:
                if player.user_id in seen:
                    continue
                if role not in tuple(player.roles or ROLE_OPTIONS):
                    continue
                rescued.append(player)
                seen.add(player.user_id)
                added += 1
                if added >= rescue_limit:
                    break
        return rescued

    def _prepare_seed_solution(
        self,
        template_teams: list[TeamState],
        ready_players: list[PlayerModel],
        required_slots: int,
    ) -> tuple[list[PlayerModel], float, list[TeamState], list[PlayerModel], float, bool] | None:
        for pool_step, candidate_pool, role_rescue_used in self._build_candidate_pool_steps(
            template_teams,
            ready_players,
            required_slots,
        ):
            target_strength = self._estimate_target_strength(
                template_teams,
                candidate_pool,
                required_slots,
            )
            seed_teams = self._clone_team_states(template_teams)
            assignments, leftovers = self._run_primary_pass(
                seed_teams,
                candidate_pool,
                target_strength,
            )
            if assignments is not None:
                return (
                    candidate_pool,
                    target_strength,
                    seed_teams,
                    leftovers,
                    pool_step,
                    role_rescue_used,
                )

        return None

    @staticmethod
    def _build_reserve_pool(
        ready_players: list[PlayerModel],
        teams: list[TeamState],
    ) -> list[PlayerModel]:
        starter_ids = {
            assignment.player.user_id for team in teams for assignment in team.assignments
        }
        return [
            player
            for player in sorted(ready_players, key=lambda item: (-item.strength, item.user_id))
            if player.user_id not in starter_ids
        ]

    def _assign_reserves(
        self,
        teams: list[TeamState],
        reserve_candidates: list[PlayerModel],
    ) -> None:
        remaining = list(reserve_candidates)
        for team in sorted(
            teams,
            key=lambda item: (-item.current_strength, self._team_sort_key(item.team_id)),
        ):
            if team.reserve_slot is None or not remaining:
                continue

            chosen = self._choose_reserve_candidate(team, remaining)
            if chosen is None:
                continue

            team.reserve_assignment = chosen
            remaining = [
                player for player in remaining if player.user_id != chosen.player.user_id
            ]

    def _choose_reserve_candidate(
        self,
        team: TeamState,
        candidates: list[PlayerModel],
    ) -> Assignment | None:
        if team.reserve_slot is None:
            return None

        reserve_limit_percent = float(self._settings.balance_threshold_percent)
        starter_average = team.current_strength / max(team.starter_count, 1)
        allowed_average = starter_average * (1.0 + reserve_limit_percent / 100.0)
        balanced_candidates = [
            player
            for player in candidates
            if (team.current_strength + player.strength) / (team.starter_count + 1)
            <= allowed_average
        ]
        # Team-count resolution reserves one substitute per team. Keep the
        # strength cap as the first choice, but never leave that slot empty
        # when enough confirmed players remain.
        eligible_candidates = balanced_candidates or candidates

        allowed_roles = tuple(team.reserve_slot.allowed_roles or ROLE_OPTIONS)
        role_matched = [
            player
            for player in eligible_candidates
            if any(role in allowed_roles for role in tuple(player.roles or ROLE_OPTIONS))
        ]
        pool = role_matched or eligible_candidates
        desired_heroes = set(team.reserve_slot.desired_heroes)

        def reserve_key(player: PlayerModel) -> tuple[bool, int, float, int]:
            desired_match_count = len(desired_heroes.intersection(player.pool))
            return (
                desired_match_count == 0,
                -desired_match_count,
                -player.strength,
                player.user_id,
            )

        chosen_player = min(pool, key=reserve_key)
        matched_roles = tuple(
            role
            for role in tuple(chosen_player.roles or ROLE_OPTIONS)
            if role in allowed_roles
        )
        assigned_role = (
            matched_roles[0]
            if matched_roles
            else self._choose_captain_role(tuple(chosen_player.roles))
        )
        return Assignment(
            player=chosen_player,
            slot=team.reserve_slot,
            assigned_role=assigned_role,
            tier=0,
            desired_match_count=len(desired_heroes.intersection(chosen_player.pool)),
            projected_spread_percent=0.0,
            cap_overflow=0,
        )

    def _run_primary_pass(
        self,
        teams: list[TeamState],
        remaining_players: list[PlayerModel],
        target_strength: float,
    ) -> tuple[list[Assignment] | None, list[PlayerModel]]:
        all_assignments: list[Assignment] = []
        remaining = list(remaining_players)
        team_strengths = {team.team_id: team.current_strength for team in teams}

        while any(team.slots for team in teams):
            incomplete = [team for team in teams if team.slots]
            team = min(
                incomplete,
                key=lambda item: (
                    team_strengths[item.team_id],
                    self._team_sort_key(item.team_id),
                ),
            )
            slot = self._select_next_slot(team)
            projection_context = self._build_primary_projection_context(
                team.team_id,
                team_strengths,
                target_strength,
            )
            evaluations = [
                self._evaluate_candidate(
                    candidate,
                    slot,
                    team,
                    teams,
                    target_strength,
                    team_strengths=team_strengths,
                    projection_context=projection_context,
                )
                for candidate in remaining
            ]
            evaluations = [item for item in evaluations if item is not None]
            if not evaluations:
                return None, remaining

            chosen = self._choose_candidate(evaluations)
            team.assignments.append(chosen)
            team.slots = [
                item for item in team.slots if item.slot_number != slot.slot_number
            ]
            remaining = [
                item for item in remaining if item.user_id != chosen.player.user_id
            ]
            team_strengths[team.team_id] += chosen.player.strength
            all_assignments.append(chosen)

        return all_assignments, remaining

    @staticmethod
    def _build_primary_projection_context(
        team_id: str,
        team_strengths: Mapping[str, float],
        target_strength: float,
    ) -> PrimaryProjectionContext:
        other_strengths = [
            strength
            for current_team_id, strength in team_strengths.items()
            if current_team_id != team_id
        ]
        return PrimaryProjectionContext(
            team_id=team_id,
            base_strength=team_strengths[team_id],
            minimum_other_strength=min(other_strengths) if other_strengths else None,
            maximum_other_strength=max(other_strengths) if other_strengths else None,
            absolute_deviation_total=sum(
                abs(strength - target_strength) for strength in team_strengths.values()
            ),
            team_count=len(team_strengths),
        )

    def _run_correction_pass(
        self,
        teams: list[TeamState],
        leftovers: list[PlayerModel],
        target_strength: float,
    ) -> CorrectionSummary:
        threshold = float(self._settings.balance_threshold_percent)
        accepted_swaps = 0
        accepted_replacements = 0
        accepted_hierarchy = 0
        passes_completed = 0

        for _ in range(int(self._settings.vnd_passes)):
            passes_completed += 1
            pass_improved = False

            swap_move = self._find_best_swap_move(
                teams,
                leftovers,
                target_strength,
            )
            if swap_move is not None:
                teams[:] = swap_move[0]
                leftovers[:] = swap_move[1]
                accepted_swaps += 1
                pass_improved = True

            replacement_move = self._find_best_replacement_move(
                teams,
                leftovers,
                target_strength,
            )
            if replacement_move is not None:
                teams[:] = replacement_move[0]
                leftovers[:] = replacement_move[1]
                accepted_replacements += 1
                pass_improved = True

            hierarchy_move = self._find_best_hierarchy_move(
                teams,
                leftovers,
                target_strength,
            )
            if hierarchy_move is not None:
                teams[:] = hierarchy_move[0]
                leftovers[:] = hierarchy_move[1]
                accepted_hierarchy += 1
                pass_improved = True

            if not pass_improved:
                break

        return CorrectionSummary(
            stage=passes_completed,
            threshold=threshold,
            spread=self._compute_spread_percent(teams, target_strength),
            accepted_swap_moves=accepted_swaps,
            accepted_replacement_moves=accepted_replacements,
            accepted_hierarchy_moves=accepted_hierarchy,
        )

    def _find_best_swap_move(
        self,
        teams: list[TeamState],
        leftovers: list[PlayerModel],
        target_strength: float,
    ) -> tuple[list[TeamState], list[PlayerModel]] | None:
        search_metrics = self._build_search_metrics(teams, target_strength)
        current_metrics = self._candidate_search_metrics(
            search_metrics,
            (),
            target_strength,
        )
        current_rank = self._solution_rank_from_search_metrics(current_metrics)
        best_move: tuple[list[TeamState], list[PlayerModel]] | None = None
        best_rank = current_rank

        for left_team, right_team in self._candidate_team_pairs(teams):
            left_shortlist = self._assignment_shortlist(left_team)
            right_shortlist = self._assignment_shortlist(right_team)
            for left_assignment in left_shortlist:
                for right_assignment in right_shortlist:
                    simulated = self._simulate_swap_move(
                        teams,
                        leftovers,
                        target_strength,
                        left_team,
                        left_assignment,
                        right_team,
                        right_assignment,
                    )
                    if simulated is None:
                        continue
                    candidate_teams, candidate_leftovers, changed_teams = simulated
                    candidate_metrics = self._candidate_search_metrics(
                        search_metrics,
                        changed_teams,
                        target_strength,
                    )
                    candidate_rank = self._solution_rank_from_search_metrics(
                        candidate_metrics
                    )
                    if candidate_rank < best_rank:
                        best_rank = candidate_rank
                        best_move = (candidate_teams, candidate_leftovers)
        return best_move

    def _find_best_replacement_move(
        self,
        teams: list[TeamState],
        leftovers: list[PlayerModel],
        target_strength: float,
    ) -> tuple[list[TeamState], list[PlayerModel]] | None:
        search_metrics = self._build_search_metrics(teams, target_strength)
        current_metrics = self._candidate_search_metrics(
            search_metrics,
            (),
            target_strength,
        )
        current_rank = self._solution_rank_from_search_metrics(current_metrics)
        best_move: tuple[list[TeamState], list[PlayerModel]] | None = None
        best_rank = current_rank

        for team in teams:
            for assignment in self._assignment_shortlist(team):
                for leftover in self._leftover_shortlist(
                    leftovers,
                    assignment.slot.allowed_roles,
                ):
                    simulated = self._simulate_replacement_move(
                        teams,
                        leftovers,
                        target_strength,
                        team,
                        assignment,
                        leftover,
                    )
                    if simulated is None:
                        continue
                    candidate_teams, candidate_leftovers, changed_teams = simulated
                    candidate_metrics = self._candidate_search_metrics(
                        search_metrics,
                        changed_teams,
                        target_strength,
                    )
                    candidate_rank = self._solution_rank_from_search_metrics(
                        candidate_metrics
                    )
                    if candidate_rank < best_rank:
                        best_rank = candidate_rank
                        best_move = (candidate_teams, candidate_leftovers)
        return best_move

    def _find_best_hierarchy_move(
        self,
        teams: list[TeamState],
        leftovers: list[PlayerModel],
        target_strength: float,
    ) -> tuple[list[TeamState], list[PlayerModel]] | None:
        search_metrics = self._build_search_metrics(teams, target_strength)
        current_metrics = self._candidate_search_metrics(
            search_metrics,
            (),
            target_strength,
        )
        current_spread = current_metrics.spread
        current_mad = current_metrics.mad_percent
        current_hierarchy = current_metrics.hierarchy_penalty
        best_move: tuple[list[TeamState], list[PlayerModel]] | None = None
        best_rank = (current_hierarchy, current_mad, current_spread)

        for left_team, right_team in self._candidate_team_pairs(teams):
            for left_assignment in self._assignment_shortlist(left_team):
                for right_assignment in self._assignment_shortlist(right_team):
                    simulated = self._simulate_swap_move(
                        teams,
                        leftovers,
                        target_strength,
                        left_team,
                        left_assignment,
                        right_team,
                        right_assignment,
                    )
                    if simulated is None:
                        continue
                    candidate_teams, candidate_leftovers, changed_teams = simulated
                    candidate_metrics = self._candidate_search_metrics(
                        search_metrics,
                        changed_teams,
                        target_strength,
                    )
                    candidate_spread = candidate_metrics.spread
                    if candidate_spread > current_spread:
                        continue
                    candidate_rank = (
                        candidate_metrics.hierarchy_penalty,
                        candidate_metrics.mad_percent,
                        candidate_spread,
                    )
                    if candidate_rank < best_rank:
                        best_rank = candidate_rank
                        best_move = (candidate_teams, candidate_leftovers)

        for team in teams:
            for assignment in self._assignment_shortlist(team):
                for leftover in self._leftover_shortlist(
                    leftovers,
                    assignment.slot.allowed_roles,
                ):
                    simulated = self._simulate_replacement_move(
                        teams,
                        leftovers,
                        target_strength,
                        team,
                        assignment,
                        leftover,
                    )
                    if simulated is None:
                        continue
                    candidate_teams, candidate_leftovers, changed_teams = simulated
                    candidate_metrics = self._candidate_search_metrics(
                        search_metrics,
                        changed_teams,
                        target_strength,
                    )
                    candidate_spread = candidate_metrics.spread
                    if candidate_spread > current_spread:
                        continue
                    candidate_rank = (
                        candidate_metrics.hierarchy_penalty,
                        candidate_metrics.mad_percent,
                        candidate_spread,
                    )
                    if candidate_rank < best_rank:
                        best_rank = candidate_rank
                        best_move = (candidate_teams, candidate_leftovers)
        return best_move

    def _candidate_team_pairs(
        self,
        teams: list[TeamState],
    ) -> list[tuple[TeamState, TeamState]]:
        ordered = sorted(
            teams,
            key=lambda item: (item.current_strength, self._team_sort_key(item.team_id)),
        )
        window = min(int(self._settings.team_pair_window), len(ordered))
        pairs: dict[tuple[str, str], tuple[TeamState, TeamState]] = {}

        def add_pair(left: TeamState, right: TeamState) -> None:
            if left.team_id == right.team_id:
                return
            key = tuple(sorted((left.team_id, right.team_id)))
            if key not in pairs:
                pairs[key] = (left, right)

        if len(ordered) >= 2:
            weakest = ordered[0]
            strongest_slice = list(reversed(ordered[-window:]))
            for candidate in strongest_slice:
                add_pair(weakest, candidate)

        for index in range(len(ordered) - 1):
            add_pair(ordered[index], ordered[index + 1])

        weakest_window = ordered[:window]
        strongest_window = ordered[-window:]
        for weak_team in weakest_window:
            for strong_team in strongest_window:
                add_pair(weak_team, strong_team)

        return list(pairs.values())

    def _assignment_shortlist(self, team: TeamState) -> list[Assignment]:
        limit = int(self._settings.swap_candidates_per_role)
        core = sorted(
            [
                assignment
                for assignment in team.assignments
                if assignment.assigned_role in {"Carry", "Semi-Carry"}
            ],
            key=lambda item: (item.player.strength, item.player.user_id),
        )
        support = sorted(
            [
                assignment
                for assignment in team.assignments
                if assignment.assigned_role in {"Support", "Semi-Support"}
            ],
            key=lambda item: (item.player.strength, item.player.user_id),
        )
        selected: list[Assignment] = []
        seen: set[tuple[int, str]] = set()

        def add_assignments(items: list[Assignment]) -> None:
            for assignment in items:
                key = (assignment.slot.slot_number, assignment.player.user_id)
                if key in seen:
                    continue
                seen.add(key)
                selected.append(assignment)

        add_assignments(core[:limit])
        add_assignments(list(reversed(core[-limit:])))
        add_assignments(support[:limit])
        add_assignments(list(reversed(support[-limit:])))
        return selected or list(team.assignments)

    def _leftover_shortlist(
        self,
        leftovers: list[PlayerModel],
        allowed_roles: tuple[str, ...],
    ) -> list[PlayerModel]:
        limit = int(self._settings.leftover_k_per_role)
        selected: list[PlayerModel] = []
        seen: set[str] = set()
        sorted_leftovers = sorted(leftovers, key=lambda item: (-item.strength, item.user_id))
        for role in tuple(allowed_roles or ROLE_OPTIONS):
            added = 0
            for player in sorted_leftovers:
                if player.user_id in seen or role not in tuple(player.roles or ROLE_OPTIONS):
                    continue
                selected.append(player)
                seen.add(player.user_id)
                added += 1
                if added >= limit:
                    break
        if selected:
            return selected
        return sorted_leftovers[:limit]

    def _simulate_swap_move(
        self,
        teams: list[TeamState],
        leftovers: list[PlayerModel],
        target_strength: float,
        left_team: TeamState,
        left_assignment: Assignment,
        right_team: TeamState,
        right_assignment: Assignment,
    ) -> tuple[
        list[TeamState],
        list[PlayerModel],
        tuple[TeamState, TeamState],
    ] | None:
        left_replacement = self._evaluate_candidate(
            right_assignment.player,
            left_assignment.slot,
            left_team,
            teams,
            target_strength,
            removing_assignment=left_assignment,
            include_projection=False,
        )
        right_replacement = self._evaluate_candidate(
            left_assignment.player,
            right_assignment.slot,
            right_team,
            teams,
            target_strength,
            removing_assignment=right_assignment,
            include_projection=False,
        )
        if left_replacement is None or right_replacement is None:
            return None

        candidate_teams = self._clone_selected_team_states(
            teams,
            {left_team.team_id, right_team.team_id},
        )
        candidate_leftovers = list(leftovers)
        left_clone = self._find_team(candidate_teams, left_team.team_id)
        right_clone = self._find_team(candidate_teams, right_team.team_id)
        left_index = self._find_assignment_index(left_clone, left_assignment)
        right_index = self._find_assignment_index(right_clone, right_assignment)
        left_clone.assignments[left_index] = left_replacement
        right_clone.assignments[right_index] = right_replacement
        return (
            candidate_teams,
            candidate_leftovers,
            (left_clone, right_clone),
        )

    def _simulate_replacement_move(
        self,
        teams: list[TeamState],
        leftovers: list[PlayerModel],
        target_strength: float,
        team: TeamState,
        assignment: Assignment,
        replacement_player: PlayerModel,
    ) -> tuple[list[TeamState], list[PlayerModel], tuple[TeamState]] | None:
        replacement = self._evaluate_candidate(
            replacement_player,
            assignment.slot,
            team,
            teams,
            target_strength,
            removing_assignment=assignment,
            include_projection=False,
        )
        if replacement is None:
            return None

        candidate_teams = self._clone_selected_team_states(
            teams,
            {team.team_id},
        )
        candidate_leftovers = list(leftovers)
        team_clone = self._find_team(candidate_teams, team.team_id)
        assignment_index = self._find_assignment_index(team_clone, assignment)
        team_clone.assignments[assignment_index] = replacement
        candidate_leftovers = [
            player
            for player in candidate_leftovers
            if player.user_id != replacement_player.user_id
        ]
        candidate_leftovers.append(assignment.player)
        candidate_leftovers.sort(key=lambda item: (-item.strength, item.user_id))
        return (
            candidate_teams,
            candidate_leftovers,
            (team_clone,),
        )

    def _build_search_metrics(
        self,
        teams: list[TeamState],
        target_strength: float,
    ) -> SearchMetrics:
        team_metrics = {
            team.team_id: self._build_team_search_metrics(team, target_strength)
            for team in teams
        }
        team_ids = tuple(team.team_id for team in teams)
        return SearchMetrics(
            team_ids=team_ids,
            teams_by_id=team_metrics,
            strengths_ascending=tuple(
                sorted(
                    (
                        (metrics.strength, team_id)
                        for team_id, metrics in team_metrics.items()
                    ),
                    key=lambda item: (item[0], self._team_sort_key(item[1])),
                )
            ),
        )

    def _build_team_search_metrics(
        self,
        team: TeamState,
        target_strength: float,
    ) -> TeamSearchMetrics:
        strength = team.current_strength
        return TeamSearchMetrics(
            strength=strength,
            absolute_deviation=abs(strength - target_strength),
            cap_overflow=self._team_cap_overflow(team),
            hierarchy_penalty=self._team_starter_hierarchy_penalty(team),
            selected_strengths=tuple(
                assignment.player.strength for assignment in team.assignments
            ),
            core_quality_values=tuple(
                self._core_quality_value(
                    assignment.player.strength,
                    assignment.assigned_role,
                )
                for assignment in team.assignments
            ),
            desired_score=self._team_desired_score(team),
        )

    def _candidate_search_metrics(
        self,
        search_metrics: SearchMetrics,
        changed_teams: Sequence[TeamState],
        target_strength: float,
    ) -> CandidateSearchMetrics:
        changed_metrics = {
            team.team_id: self._build_team_search_metrics(team, target_strength)
            for team in changed_teams
        }
        changed_ids = set(changed_metrics)
        metrics_by_id = {
            team_id: changed_metrics.get(
                team_id,
                search_metrics.teams_by_id[team_id],
            )
            for team_id in search_metrics.team_ids
        }
        absolute_deviation_total = sum(
            metrics_by_id[team_id].absolute_deviation
            for team_id in search_metrics.team_ids
        )
        cap_overflow = sum(
            metrics_by_id[team_id].cap_overflow
            for team_id in search_metrics.team_ids
        )
        hierarchy_penalty = sum(
            metrics_by_id[team_id].hierarchy_penalty
            for team_id in search_metrics.team_ids
        )
        selected_strength = sum(
            strength
            for team_id in search_metrics.team_ids
            for strength in metrics_by_id[team_id].selected_strengths
        )
        core_quality = sum(
            quality
            for team_id in search_metrics.team_ids
            for quality in metrics_by_id[team_id].core_quality_values
        )
        desired_score = sum(
            metrics_by_id[team_id].desired_score
            for team_id in search_metrics.team_ids
        )

        changed_strengths = [
            metrics.strength for metrics in changed_metrics.values()
        ]
        minimum_unchanged = next(
            (
                strength
                for strength, team_id in search_metrics.strengths_ascending
                if team_id not in changed_ids
            ),
            None,
        )
        maximum_unchanged = next(
            (
                strength
                for strength, team_id in reversed(
                    search_metrics.strengths_ascending
                )
                if team_id not in changed_ids
            ),
            None,
        )
        extrema = list(changed_strengths)
        if minimum_unchanged is not None:
            extrema.append(minimum_unchanged)
        if maximum_unchanged is not None:
            extrema.append(maximum_unchanged)

        spread = self._spread_percent_from_strengths(extrema, target_strength)
        mad_percent = 0.0
        if search_metrics.team_ids and target_strength > 0:
            mad_percent = (
                absolute_deviation_total
                / len(search_metrics.team_ids)
                / target_strength
                * 100
            )
        return CandidateSearchMetrics(
            spread=spread,
            mad_percent=mad_percent,
            cap_overflow=cap_overflow,
            hierarchy_penalty=hierarchy_penalty,
            selected_strength=selected_strength,
            core_quality=core_quality,
            desired_score=desired_score,
        )

    @staticmethod
    def _find_team(teams: list[TeamState], team_id: str) -> TeamState:
        return next(team for team in teams if team.team_id == team_id)

    @staticmethod
    def _find_assignment_index(team: TeamState, assignment: Assignment) -> int:
        return next(
            index
            for index, item in enumerate(team.assignments)
            if item.slot.slot_number == assignment.slot.slot_number
            and item.player.user_id == assignment.player.user_id
        )

    def _team_sort_key(self, team_id: str) -> tuple[bool, int | str]:
        return (not team_id.isdigit(), int(team_id) if team_id.isdigit() else team_id)

    @staticmethod
    def _parse_pool(raw_pool: str | Sequence[str] | None) -> tuple[str, ...]:
        if not raw_pool:
            return ()
        if isinstance(raw_pool, str):
            return tuple(hero.strip() for hero in raw_pool.split(",") if hero and hero.strip())
        return tuple(str(hero).strip() for hero in raw_pool if hero and str(hero).strip())

    @staticmethod
    def _normalize_roles(raw_roles: Sequence[str] | None) -> tuple[str, ...]:
        roles = [role for role in raw_roles or [] if role in ROLE_OPTIONS]
        return tuple(roles or ROLE_OPTIONS)

    def _build_player(self, row: Mapping[str, Any]) -> PlayerModel:
        rank = str(row["rank"])
        subrank = int(row["subrank"] or 0)
        playtime = row["playtime"] if "playtime" in row else None
        strength = calculate_player_strength(
            rank,
            subrank,
            playtime,
            rank_power=self._settings.rank_power,
        )
        return PlayerModel(
            user_id=str(row["user_id"]),
            username=row["username"] if "username" in row else None,
            team_name=str(row["team_name"]).strip() if row.get("team_name") else None,
            rank=rank,
            subrank=subrank,
            playtime=playtime,
            pool=self._parse_pool(row["pool"] if "pool" in row else None),
            roles=self._normalize_roles(row["roles"] if "roles" in row else None),
            strength=strength,
        )

    @staticmethod
    def _choose_captain_role(roles: tuple[str, ...]) -> str:
        for role in ("Carry", "Semi-Carry", "Support", "Semi-Support"):
            if role in roles:
                return role
        return "Carry"

    @staticmethod
    def _select_next_slot(team: TeamState) -> SlotPreference:
        return min(
            team.slots,
            key=lambda slot: (
                len(slot.allowed_roles or ROLE_OPTIONS),
                -len(slot.desired_heroes),
                slot.slot_number,
            ),
        )

    def _effective_caps(self, team: TeamState) -> tuple[int, int]:
        support_singletons = sum(
            1 for slot in team.all_slots if tuple(slot.allowed_roles) == ("Support",)
        )
        semisupport_singletons = sum(
            1
            for slot in team.all_slots
            if tuple(slot.allowed_roles) == ("Semi-Support",)
        )
        if team.captain_assigned_role == "Support" and tuple(team.captain.roles) == ("Support",):
            support_singletons += 1
        if team.captain_assigned_role == "Semi-Support" and tuple(team.captain.roles) == ("Semi-Support",):
            semisupport_singletons += 1
        return max(2, support_singletons), max(2, semisupport_singletons)

    def _evaluate_candidate(
        self,
        player: PlayerModel,
        slot: SlotPreference,
        team: TeamState,
        teams: list[TeamState],
        target_strength: float,
        *,
        removing_assignment: Assignment | None = None,
        include_projection: bool = True,
        team_strengths: Mapping[str, float] | None = None,
        projection_context: PrimaryProjectionContext | None = None,
    ) -> Assignment | None:
        allowed_roles = tuple(slot.allowed_roles or ROLE_OPTIONS)
        player_roles = tuple(player.roles or ROLE_OPTIONS)
        matched_roles = tuple(role for role in player_roles if role in allowed_roles)
        if not matched_roles:
            return None
        desired_match_count = len(set(slot.desired_heroes).intersection(player.pool))

        base_counts = team.role_counts()
        base_strength = (
            team_strengths[team.team_id]
            if team_strengths is not None
            else team.current_strength
        )
        if removing_assignment is not None:
            base_counts[removing_assignment.assigned_role] -= 1
            base_strength -= removing_assignment.player.strength

        support_cap, semisupport_cap = self._effective_caps(team)
        best_role = None
        best_overflow = None
        best_role_quality = None
        for role in matched_roles:
            support_overflow = max(
                base_counts.get("Support", 0) + (1 if role == "Support" else 0) - support_cap,
                0,
            )
            semisupport_overflow = max(
                base_counts.get("Semi-Support", 0)
                + (1 if role == "Semi-Support" else 0)
                - semisupport_cap,
                0,
            )
            overflow = support_overflow + semisupport_overflow
            role_quality = self._core_quality_value(player.strength, role)
            if (
                best_overflow is None
                or overflow < best_overflow
                or (
                    overflow == best_overflow
                    and (best_role_quality is None or role_quality > best_role_quality)
                )
                or (
                    overflow == best_overflow
                    and role_quality == best_role_quality
                    and ROLE_ORDER.get(role, 99) < ROLE_ORDER.get(best_role, 99)
                )
            ):
                best_role = role
                best_overflow = overflow
                best_role_quality = role_quality

        if best_role is None or best_overflow is None:
            return None

        projected_spread = 0.0
        projected_mad = 0.0
        hierarchy_penalty = 0.0
        if include_projection:
            new_strength = base_strength + player.strength
            used_primary_projection = False
            if (
                projection_context is not None
                and removing_assignment is None
                and projection_context.team_id == team.team_id
                and projection_context.team_count > 0
            ):
                min_strength = new_strength
                max_strength = new_strength
                if projection_context.minimum_other_strength is not None:
                    min_strength = min(
                        min_strength,
                        projection_context.minimum_other_strength,
                    )
                if projection_context.maximum_other_strength is not None:
                    max_strength = max(
                        max_strength,
                        projection_context.maximum_other_strength,
                    )
                projected_spread = self._spread_percent_from_strengths(
                    [min_strength, max_strength],
                    target_strength,
                )
                projected_mad = (
                    (
                        projection_context.absolute_deviation_total
                        - abs(projection_context.base_strength - target_strength)
                        + abs(new_strength - target_strength)
                    )
                    / projection_context.team_count
                    / target_strength
                    * 100
                    if target_strength > 0
                    else 0.0
                )
                used_primary_projection = True
            elif team_strengths is None:
                projected_strengths = [
                    (
                        new_strength
                        if current_team.team_id == team.team_id
                        else current_team.current_strength
                    )
                    for current_team in teams
                ]
            else:
                projected_strengths = [
                    new_strength
                    if team_id == team.team_id
                    else strength
                    for team_id, strength in team_strengths.items()
                ]
            if not used_primary_projection:
                projected_spread = self._spread_percent_from_strengths(
                    projected_strengths,
                    target_strength,
                )
                projected_mad = self._mad_percent_from_strengths(
                    projected_strengths,
                    target_strength,
                )
            hierarchy_penalty = self._team_starter_hierarchy_penalty(
                team,
                candidate_player=player,
                candidate_role=best_role,
                removing_assignment=removing_assignment,
            )

        return Assignment(
            player=player,
            slot=slot,
            assigned_role=best_role,
            tier=0 if best_overflow == 0 else 1,
            desired_match_count=desired_match_count,
            projected_spread_percent=projected_spread,
            cap_overflow=best_overflow,
            projected_mad_percent=projected_mad,
            hierarchy_penalty=hierarchy_penalty,
        )

    def _choose_candidate(self, evaluations: list[Assignment]) -> Assignment:
        min_tier = min(item.tier for item in evaluations)
        candidates = [item for item in evaluations if item.tier == min_tier]
        min_spread = min(item.projected_spread_percent for item in candidates)
        spread_window = min_spread + float(self._settings.balance_threshold_percent)
        candidates = [
            item for item in candidates if item.projected_spread_percent <= spread_window
        ]
        candidates.sort(
            key=lambda item: (
                item.projected_mad_percent,
                item.cap_overflow,
                item.hierarchy_penalty,
                -ROLE_PRIORITY_WEIGHTS.get(item.assigned_role, 1),
                self._seed_strength_bias(item),
                -item.desired_match_count,
                item.projected_spread_percent,
                item.player.user_id,
            )
        )
        return candidates[0]

    def _compute_spread_percent(self, teams: list[TeamState], target_strength: float) -> float:
        strengths = self._compute_team_strengths(teams)
        return self._spread_percent_from_strengths(strengths, target_strength)

    def _compute_mad_percent(self, teams: list[TeamState], target_strength: float) -> float:
        strengths = self._compute_team_strengths(teams)
        return self._mad_percent_from_strengths(strengths, target_strength)

    def _compute_std_percent(self, teams: list[TeamState], target_strength: float) -> float:
        strengths = self._compute_team_strengths(teams)
        return self._std_percent_from_strengths(strengths, target_strength)

    @staticmethod
    def _compute_team_strengths(teams: list[TeamState]) -> list[float]:
        return [team.current_strength for team in teams]

    @staticmethod
    def _spread_percent_from_strengths(strengths: list[float], target_strength: float) -> float:
        if not strengths or target_strength <= 0:
            return 0.0
        return (max(strengths) - min(strengths)) / target_strength * 100

    @staticmethod
    def _mad_percent_from_strengths(strengths: list[float], target_strength: float) -> float:
        if not strengths or target_strength <= 0:
            return 0.0
        mean_absolute_deviation = sum(
            abs(strength - target_strength) for strength in strengths
        ) / len(strengths)
        return mean_absolute_deviation / target_strength * 100

    @staticmethod
    def _std_percent_from_strengths(strengths: list[float], target_strength: float) -> float:
        if len(strengths) < 2 or target_strength <= 0:
            return 0.0
        return pstdev(strengths) / target_strength * 100

    @staticmethod
    def _estimate_target_strength(
        teams: list[TeamState],
        remaining_players: list[PlayerModel],
        required_slots: int,
    ) -> float:
        captain_total = sum(team.captain.strength for team in teams)
        projected_players = sorted(
            (player.strength for player in remaining_players),
            reverse=True,
        )[:required_slots]
        total_strength = captain_total + sum(projected_players)
        return total_strength / len(teams) if teams else 0.0

    def _evaluate_swap(
        self,
        teams: list[TeamState],
        left_team: TeamState,
        left_assignment: Assignment,
        right_team: TeamState,
        right_assignment: Assignment,
        target_strength: float,
    ) -> tuple[float, float, int, float, TeamState, int, TeamState, int, Assignment, Assignment] | None:
        left_eval = self._evaluate_candidate(
            right_assignment.player,
            left_assignment.slot,
            left_team,
            teams,
            target_strength,
            removing_assignment=left_assignment,
        )
        right_eval = self._evaluate_candidate(
            left_assignment.player,
            right_assignment.slot,
            right_team,
            teams,
            target_strength,
            removing_assignment=right_assignment,
        )
        if left_eval is None or right_eval is None:
            return None

        strengths = []
        for team in teams:
            if team.team_id == left_team.team_id:
                strengths.append(
                    left_team.current_strength
                    - left_assignment.player.strength
                    + right_assignment.player.strength
                )
            elif team.team_id == right_team.team_id:
                strengths.append(
                    right_team.current_strength
                    - right_assignment.player.strength
                    + left_assignment.player.strength
                )
            else:
                strengths.append(team.current_strength)
        spread = self._spread_percent_from_strengths(strengths, target_strength)
        mad = self._mad_percent_from_strengths(strengths, target_strength)
        penalty = left_eval.tier + right_eval.tier + left_eval.cap_overflow + right_eval.cap_overflow
        hierarchy_penalty = self._total_hierarchy_penalty_after_swap(
            teams,
            left_team,
            left_assignment,
            left_eval,
            right_team,
            right_assignment,
            right_eval,
        )
        return (
            spread,
            mad,
            penalty,
            hierarchy_penalty,
            left_team,
            left_team.assignments.index(left_assignment),
            right_team,
            right_team.assignments.index(right_assignment),
            left_eval,
            right_eval,
        )

    def _evaluate_single_replacement(
        self,
        teams: list[TeamState],
        team: TeamState,
        assignment_index: int,
        replacement_player: PlayerModel,
        target_strength: float,
    ) -> tuple[float, float, int, float, TeamState, int, Assignment, PlayerModel] | None:
        current_assignment = team.assignments[assignment_index]
        replacement = self._evaluate_candidate(
            replacement_player,
            current_assignment.slot,
            team,
            teams,
            target_strength,
            removing_assignment=current_assignment,
        )
        if replacement is None:
            return None

        strengths = []
        for item in teams:
            if item.team_id == team.team_id:
                strengths.append(
                    team.current_strength
                    - current_assignment.player.strength
                    + replacement_player.strength
                )
            else:
                strengths.append(item.current_strength)
        spread = self._spread_percent_from_strengths(strengths, target_strength)
        mad = self._mad_percent_from_strengths(strengths, target_strength)
        penalty = replacement.tier + replacement.cap_overflow
        hierarchy_penalty = self._total_hierarchy_penalty_after_replacement(
            teams,
            team,
            current_assignment,
            replacement,
        )
        return (
            spread,
            mad,
            penalty,
            hierarchy_penalty,
            team,
            assignment_index,
            replacement,
            current_assignment.player,
        )

    def _is_better_solution(
        self,
        left_teams: list[TeamState],
        left_summary: OptimizationSummary,
        right_teams: list[TeamState],
        right_summary: OptimizationSummary,
    ) -> bool:
        return self._solution_rank(left_teams, left_summary) < self._solution_rank(
            right_teams,
            right_summary,
        )

    def _solution_rank(
        self,
        teams: list[TeamState],
        summary: OptimizationSummary,
    ) -> tuple[Any, ...]:
        return self._solution_rank_from_search_metrics(
            CandidateSearchMetrics(
                spread=summary.spread,
                mad_percent=summary.mad_percent,
                cap_overflow=self._total_cap_overflow(teams),
                hierarchy_penalty=self._total_hierarchy_penalty(teams),
                selected_strength=self._total_selected_strength(teams),
                core_quality=self._total_core_quality(teams),
                desired_score=self._total_desired_score(teams),
            )
        )

    def _solution_rank_from_search_metrics(
        self,
        metrics: CandidateSearchMetrics,
    ) -> tuple[Any, ...]:
        threshold = float(self._settings.balance_threshold_percent)
        spread = metrics.spread
        mad_percent = metrics.mad_percent
        overflow = metrics.cap_overflow
        hierarchy_penalty = metrics.hierarchy_penalty
        selected_strength = metrics.selected_strength
        core_quality = metrics.core_quality
        desired_score = metrics.desired_score
        if spread <= threshold:
            return (
                0,
                mad_percent,
                overflow,
                hierarchy_penalty,
                -core_quality,
                -selected_strength,
                -desired_score,
                spread,
            )
        return (
            1,
            spread,
            mad_percent,
            overflow,
            hierarchy_penalty,
            -core_quality,
            -selected_strength,
            -desired_score,
        )

    def _total_cap_overflow(self, teams: list[TeamState]) -> int:
        return sum(self._team_cap_overflow(team) for team in teams)

    @staticmethod
    def _total_selected_strength(teams: list[TeamState]) -> float:
        return sum(
            assignment.player.strength
            for team in teams
            for assignment in team.assignments
        )

    def _team_cap_overflow(self, team: TeamState) -> int:
        support_cap, semisupport_cap = self._effective_caps(team)
        counts = team.role_counts()
        return max(counts.get("Support", 0) - support_cap, 0) + max(
            counts.get("Semi-Support", 0) - semisupport_cap,
            0,
        )

    def _total_core_quality(self, teams: list[TeamState]) -> float:
        return sum(
            self._core_quality_value(assignment.player.strength, assignment.assigned_role)
            for team in teams
            for assignment in team.assignments
        )

    def _total_hierarchy_penalty(self, teams: list[TeamState]) -> float:
        return sum(self._team_starter_hierarchy_penalty(team) for team in teams)

    def _total_hierarchy_penalty_after_swap(
        self,
        teams: list[TeamState],
        left_team: TeamState,
        left_assignment: Assignment,
        left_replacement: Assignment,
        right_team: TeamState,
        right_assignment: Assignment,
        right_replacement: Assignment,
    ) -> float:
        total = 0.0
        for team in teams:
            if team.team_id == left_team.team_id:
                total += self._team_starter_hierarchy_penalty(
                    team,
                    candidate_player=left_replacement.player,
                    candidate_role=left_replacement.assigned_role,
                    removing_assignment=left_assignment,
                )
            elif team.team_id == right_team.team_id:
                total += self._team_starter_hierarchy_penalty(
                    team,
                    candidate_player=right_replacement.player,
                    candidate_role=right_replacement.assigned_role,
                    removing_assignment=right_assignment,
                )
            else:
                total += self._team_starter_hierarchy_penalty(team)
        return total

    def _total_hierarchy_penalty_after_replacement(
        self,
        teams: list[TeamState],
        target_team: TeamState,
        removed_assignment: Assignment,
        replacement: Assignment,
    ) -> float:
        total = 0.0
        for team in teams:
            if team.team_id == target_team.team_id:
                total += self._team_starter_hierarchy_penalty(
                    team,
                    candidate_player=replacement.player,
                    candidate_role=replacement.assigned_role,
                    removing_assignment=removed_assignment,
                )
            else:
                total += self._team_starter_hierarchy_penalty(team)
        return total

    def _team_starter_hierarchy_penalty(
        self,
        team: TeamState,
        *,
        candidate_player: PlayerModel | None = None,
        candidate_role: str | None = None,
        removing_assignment: Assignment | None = None,
    ) -> float:
        assignments = [
            assignment
            for assignment in team.assignments
            if removing_assignment is None or assignment is not removing_assignment
        ]
        core_strengths = sorted(
            assignment.player.strength
            for assignment in assignments
            if assignment.assigned_role in {"Carry", "Semi-Carry"}
        )
        support_strengths = sorted(
            (
                assignment.player.strength
                for assignment in assignments
                if assignment.assigned_role in {"Support", "Semi-Support"}
            ),
            reverse=True,
        )
        if candidate_player is not None and candidate_role is not None:
            if candidate_role in {"Carry", "Semi-Carry"}:
                core_strengths.append(candidate_player.strength)
                core_strengths.sort()
            elif candidate_role in {"Support", "Semi-Support"}:
                support_strengths.append(candidate_player.strength)
                support_strengths.sort(reverse=True)
        if not core_strengths or not support_strengths:
            return 0.0

        pair_count = min(len(core_strengths), len(support_strengths))
        return sum(
            max(support_strengths[index] - core_strengths[index], 0.0)
            for index in range(pair_count)
        )

    @staticmethod
    def _total_desired_score(teams: list[TeamState]) -> int:
        return sum(
            assignment.desired_match_count
            for team in teams
            for assignment in team.assignments
        )

    @staticmethod
    def _team_desired_score(team: TeamState) -> int:
        return sum(
            assignment.desired_match_count for assignment in team.assignments
        )

    @staticmethod
    def _core_quality_value(strength: float, role: str) -> float:
        return strength * ROLE_PRIORITY_WEIGHTS.get(role, 1)

    @staticmethod
    def _seed_strength_bias(assignment: Assignment) -> float:
        if assignment.assigned_role in {"Carry", "Semi-Carry"}:
            return -assignment.player.strength
        return assignment.player.strength

    def _build_summary(
        self,
        teams: list[TeamState],
        optimization_summary: OptimizationSummary,
    ) -> str:
        reserve_count = sum(1 for team in teams if team.reserve_assignment is not None)
        lines = [
            "Автоматическая сборка команд завершена.",
            f"Источник результата: {optimization_summary.source}.",
            f"Размер candidate pool: {optimization_summary.candidate_pool_size}.",
            f"В итоговый основной состав вошло игроков: {optimization_summary.selected_player_count}.",
            f"Назначено замен: {reserve_count}.",
            f"Использованный порог: {optimization_summary.threshold:.2f}%.",
            f"Итоговый разброс силы: {optimization_summary.spread:.2f}%.",
            f"MAD силы команд: {optimization_summary.mad_percent:.2f}%.",
            f"STD силы команд: {optimization_summary.std_percent:.2f}%.",
            f"Шаг candidate pool: {(optimization_summary.pool_step or 0.0):.2f}x.",
            f"Role rescue включён: {'да' if optimization_summary.role_rescue_used else 'нет'}.",
            f"Принято swap moves: {optimization_summary.accepted_swap_moves}.",
            f"Принято replacement moves: {optimization_summary.accepted_replacement_moves}.",
            f"Принято hierarchy moves: {optimization_summary.accepted_hierarchy_moves}.",
        ]
        if optimization_summary.stage is not None:
            lines.append(f"VND-проходов выполнено: {optimization_summary.stage}.")
        for team in teams:
            lines.append(f"{team.team_id}: {team.current_strength:.2f}")
        return "\n".join(lines)

    def _build_result_snapshot(
        self,
        teams: list[TeamState],
        optimization_summary: OptimizationSummary,
    ) -> dict[str, Any]:
        ordered_teams = sorted(teams, key=lambda item: self._team_sort_key(item.team_id))
        unavailable_names = {
            team.captain.team_name.strip().casefold()
            for team in ordered_teams
            if team.captain.team_name and team.captain.team_name.strip()
        }
        resolved_names: dict[str, str] = {}
        for team in ordered_teams:
            explicit_name = (team.captain.team_name or "").strip()
            if explicit_name:
                resolved_names[team.team_id] = explicit_name
                continue
            generated_name = choose_available_team_name(
                f"{team.captain.user_id}:{team.team_id}",
                unavailable_names,
            )
            resolved_names[team.team_id] = generated_name
            unavailable_names.add(generated_name.casefold())
        team_payloads = [
            self._build_team_snapshot(team, team_name=resolved_names[team.team_id])
            for team in ordered_teams
        ]
        return {
            "optimization_summary": {
                "threshold": optimization_summary.threshold,
                "spread_percent": optimization_summary.spread,
                "mad_percent": optimization_summary.mad_percent,
                "std_percent": optimization_summary.std_percent,
                "candidate_pool_size": optimization_summary.candidate_pool_size,
                "selected_player_count": optimization_summary.selected_player_count,
                "source": optimization_summary.source,
                "pool_step": optimization_summary.pool_step,
                "role_rescue_used": optimization_summary.role_rescue_used,
                "accepted_swap_moves": optimization_summary.accepted_swap_moves,
                "accepted_replacement_moves": optimization_summary.accepted_replacement_moves,
                "accepted_hierarchy_moves": optimization_summary.accepted_hierarchy_moves,
                "stage": optimization_summary.stage,
            },
            "teams": team_payloads,
            "preference_metrics": self._build_preference_metrics(team_payloads),
        }

    def _build_team_snapshot(self, team: TeamState, *, team_name: str) -> dict[str, Any]:
        starter_assignments = sorted(team.assignments, key=lambda item: item.slot.slot_number)
        starter_strength = team.captain.strength + sum(
            item.player.strength for item in starter_assignments
        )
        starter_average_strength = starter_strength / max(1, 1 + len(starter_assignments))
        return {
            "team_id": team.team_id,
            "team_name": team_name,
            "starter_strength": round(starter_strength, 4),
            "starter_average_strength": round(starter_average_strength, 4),
            "captain": {
                "user_id": team.captain.user_id,
                "username": team.captain.username,
                "team_name": team.captain.team_name,
                "rank": team.captain.rank,
                "subrank": team.captain.subrank,
                "playtime": team.captain.playtime,
                "assigned_role": team.captain_assigned_role,
                "strength": round(team.captain.strength, 4),
                "pool": list(team.captain.pool),
                "roles": list(team.captain.roles),
            },
            "starter_slots": [
                self._build_slot_snapshot(assignment) for assignment in starter_assignments
            ],
            "reserve_slot": self._build_slot_snapshot(team.reserve_assignment)
            if team.reserve_assignment is not None
            else None,
        }

    @staticmethod
    def _build_slot_snapshot(assignment: Assignment) -> dict[str, Any]:
        matched_heroes = sorted(
            set(assignment.slot.desired_heroes).intersection(assignment.player.pool)
        )
        return {
            "slot_number": assignment.slot.slot_number,
            "allowed_roles": list(assignment.slot.allowed_roles),
            "desired_heroes": list(assignment.slot.desired_heroes),
            "assigned_player": {
                "user_id": assignment.player.user_id,
                "username": assignment.player.username,
                "rank": assignment.player.rank,
                "subrank": assignment.player.subrank,
                "playtime": assignment.player.playtime,
                "strength": round(assignment.player.strength, 4),
                "pool": list(assignment.player.pool),
                "roles": list(assignment.player.roles),
            },
            "assigned_role": assignment.assigned_role,
            "matched_desired_heroes": matched_heroes,
            "desired_match_count": len(matched_heroes),
            "role_match": assignment.assigned_role
            in tuple(assignment.slot.allowed_roles or ROLE_OPTIONS),
        }

    @staticmethod
    def _build_preference_metrics(team_payloads: list[dict[str, Any]]) -> dict[str, Any]:
        starter_slots = [slot for team in team_payloads for slot in team["starter_slots"]]
        reserve_slots = [
            team["reserve_slot"]
            for team in team_payloads
            if team.get("reserve_slot") is not None
        ]
        preferred_starter_slots = [
            slot
            for slot in starter_slots
            if len(slot["allowed_roles"]) < len(ROLE_OPTIONS)
            or len(slot["desired_heroes"]) > 0
        ]
        desired_starter_slots = [slot for slot in starter_slots if slot["desired_heroes"]]
        role_restricted_starter_slots = [
            slot for slot in starter_slots if len(slot["allowed_roles"]) < len(ROLE_OPTIONS)
        ]
        desired_requested_total = sum(
            len(slot["desired_heroes"]) for slot in desired_starter_slots
        )
        desired_hit_total = sum(slot["desired_match_count"] for slot in desired_starter_slots)
        fully_honored = [
            slot
            for slot in preferred_starter_slots
            if slot["role_match"]
            and (not slot["desired_heroes"] or slot["desired_match_count"] > 0)
        ]
        reserve_desired_slots = [slot for slot in reserve_slots if slot["desired_heroes"]]

        starter_role_match_count = sum(1 for slot in starter_slots if slot["role_match"])
        desired_slots_with_match = sum(
            1 for slot in desired_starter_slots if slot["desired_match_count"] > 0
        )
        reserve_desired_slots_with_match = sum(
            1 for slot in reserve_desired_slots if slot["desired_match_count"] > 0
        )
        return {
            "starter_slots_total": len(starter_slots),
            "starter_preference_slots_total": len(preferred_starter_slots),
            "starter_role_restricted_slots_total": len(role_restricted_starter_slots),
            "starter_role_match_count": starter_role_match_count,
            "starter_role_match_rate_percent": round(
                (starter_role_match_count / len(starter_slots) * 100)
                if starter_slots
                else 0.0,
                2,
            ),
            "starter_desired_slots_total": len(desired_starter_slots),
            "starter_desired_slots_with_any_match": desired_slots_with_match,
            "starter_desired_slot_hit_rate_percent": round(
                (desired_slots_with_match / len(desired_starter_slots) * 100)
                if desired_starter_slots
                else 0.0,
                2,
            ),
            "starter_desired_heroes_requested_total": desired_requested_total,
            "starter_desired_heroes_hit_total": desired_hit_total,
            "starter_desired_hero_hit_rate_percent": round(
                (desired_hit_total / desired_requested_total * 100)
                if desired_requested_total
                else 0.0,
                2,
            ),
            "starter_preference_slots_fully_honored": len(fully_honored),
            "starter_preference_slots_fully_honored_rate_percent": round(
                (len(fully_honored) / len(preferred_starter_slots) * 100)
                if preferred_starter_slots
                else 0.0,
                2,
            ),
            "reserve_slots_total": len(reserve_slots),
            "reserve_desired_slots_total": len(reserve_desired_slots),
            "reserve_desired_slots_with_any_match": reserve_desired_slots_with_match,
        }
