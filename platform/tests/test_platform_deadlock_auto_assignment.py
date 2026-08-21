from __future__ import annotations

import unittest
from unittest.mock import patch

from python_packages.platform_domain.deadlock import (
    AutoAssignmentEngine,
    AutoAssignmentError,
)
from python_packages.platform_domain.deadlock.auto_assignment import (
    Assignment,
    PlayerModel,
)
from python_packages.platform_domain.deadlock.team_names import TEAM_NAME_CATALOG


class PlatformDeadlockAutoAssignmentTests(unittest.TestCase):
    @staticmethod
    def _captain_rows() -> list[dict[str, object]]:
        return [
            {
                "team_id": "1",
                "user_id": 11,
                "username": "c1",
                "rank": "Владыка",
                "subrank": 1,
                "playtime": "1001-1500",
                "pool": "Abrams,Apollo,Bebop",
                "roles": ["Carry"],
            },
            {
                "team_id": "2",
                "user_id": 22,
                "username": "c2",
                "rank": "Владыка",
                "subrank": 2,
                "playtime": "1001-1500",
                "pool": "Kelvin,Mina,Seven",
                "roles": ["Support", "Semi-Support"],
            },
        ]

    @staticmethod
    def _dream_slot_rows() -> list[dict[str, object]]:
        return [
            {
                "team_id": "1",
                "slot_number": 1,
                "allowed_roles": ["Carry"],
                "desired_heroes": ["Abrams"],
            },
            {
                "team_id": "1",
                "slot_number": 2,
                "allowed_roles": ["Semi-Carry"],
                "desired_heroes": ["Apollo"],
            },
            {
                "team_id": "1",
                "slot_number": 3,
                "allowed_roles": ["Support"],
                "desired_heroes": ["Ivy"],
            },
            {
                "team_id": "1",
                "slot_number": 4,
                "allowed_roles": ["Semi-Support"],
                "desired_heroes": ["Kelvin"],
            },
            {
                "team_id": "1",
                "slot_number": 6,
                "allowed_roles": ["Support", "Semi-Support"],
                "desired_heroes": ["Seven"],
            },
            {
                "team_id": "2",
                "slot_number": 1,
                "allowed_roles": ["Carry"],
                "desired_heroes": ["Kelvin"],
            },
            {
                "team_id": "2",
                "slot_number": 2,
                "allowed_roles": ["Semi-Carry"],
                "desired_heroes": ["Seven"],
            },
            {
                "team_id": "2",
                "slot_number": 3,
                "allowed_roles": ["Support"],
                "desired_heroes": ["Ivy"],
            },
            {
                "team_id": "2",
                "slot_number": 4,
                "allowed_roles": ["Semi-Support"],
                "desired_heroes": ["Mina"],
            },
            {
                "team_id": "2",
                "slot_number": 6,
                "allowed_roles": ["Carry", "Semi-Carry"],
                "desired_heroes": ["Abrams"],
            },
        ]

    @staticmethod
    def _ready_player_rows() -> list[dict[str, object]]:
        return [
            {
                "user_id": 100,
                "username": "p0",
                "rank": "Фантом",
                "subrank": 3,
                "playtime": "1001-1500",
                "pool": "Abrams,Apollo",
                "roles": ["Carry", "Semi-Carry"],
            },
            {
                "user_id": 101,
                "username": "p1",
                "rank": "Фантом",
                "subrank": 2,
                "playtime": "1501-2000",
                "pool": "Kelvin,Seven",
                "roles": ["Carry"],
            },
            {
                "user_id": 102,
                "username": "p2",
                "rank": "Оракул",
                "subrank": 4,
                "playtime": "1001-1500",
                "pool": "Ivy,Mina",
                "roles": ["Support"],
            },
            {
                "user_id": 103,
                "username": "p3",
                "rank": "Оракул",
                "subrank": 3,
                "playtime": "2001-3000",
                "pool": "Kelvin,Mina",
                "roles": ["Semi-Support"],
            },
            {
                "user_id": 104,
                "username": "p4",
                "rank": "Архонт",
                "subrank": 5,
                "playtime": "501-1000",
                "pool": "Apollo,Seven",
                "roles": ["Semi-Carry"],
            },
            {
                "user_id": 105,
                "username": "p5",
                "rank": "Архонт",
                "subrank": 4,
                "playtime": "1001-1500",
                "pool": "Ivy,Abrams",
                "roles": ["Support", "Semi-Support"],
            },
            {
                "user_id": 106,
                "username": "p6",
                "rank": "Фантом",
                "subrank": 1,
                "playtime": "3000+",
                "pool": "Seven,Kelvin",
                "roles": ["Carry", "Support"],
            },
            {
                "user_id": 107,
                "username": "p7",
                "rank": "Оракул",
                "subrank": 2,
                "playtime": "1001-1500",
                "pool": "Mina,Apollo",
                "roles": ["Semi-Carry", "Semi-Support"],
            },
            {
                "user_id": 108,
                "username": "p8",
                "rank": "Архонт",
                "subrank": 3,
                "playtime": "1501-2000",
                "pool": "Ivy,Seven",
                "roles": ["Support"],
            },
            {
                "user_id": 109,
                "username": "p9",
                "rank": "Фантом",
                "subrank": 2,
                "playtime": "1001-1500",
                "pool": "Abrams,Kelvin",
                "roles": ["Carry", "Semi-Carry"],
            },
            {
                "user_id": 110,
                "username": "p10",
                "rank": "Оракул",
                "subrank": 1,
                "playtime": "0-500",
                "pool": "Seven,Mina",
                "roles": ["Semi-Support", "Support"],
            },
            {
                "user_id": 111,
                "username": "p11",
                "rank": "Архонт",
                "subrank": 2,
                "playtime": "1001-1500",
                "pool": "Apollo,Bebop",
                "roles": ["Semi-Carry"],
            },
            {
                "user_id": 112,
                "username": "p12",
                "rank": "Оракул",
                "subrank": 3,
                "playtime": "1001-1500",
                "pool": "Ivy,Kelvin",
                "roles": ["Support", "Carry"],
            },
            {
                "user_id": 113,
                "username": "p13",
                "rank": "Архонт",
                "subrank": 1,
                "playtime": "501-1000",
                "pool": "Abrams,Seven",
                "roles": ["Carry", "Semi-Support"],
            },
        ]

    def test_build_player_normalizes_strength_roles_pool_and_user_id(self):
        engine = AutoAssignmentEngine()
        row = self._ready_player_rows()[0]

        shared_player = engine.build_player(row)

        self.assertEqual(shared_player.roles, ("Carry", "Semi-Carry"))
        self.assertEqual(shared_player.pool, ("Abrams", "Apollo"))
        self.assertEqual(shared_player.user_id, "100")
        self.assertGreater(shared_player.strength, 0)

    def test_solver_builds_deterministic_team_snapshot_and_candidate_pool(self):
        engine = AutoAssignmentEngine()

        run = engine.solve(
            self._captain_rows(),
            self._ready_player_rows(),
            self._dream_slot_rows(),
        )

        self.assertEqual(len(run.result_snapshot["teams"]), 2)
        self.assertEqual([team["team_id"] for team in run.result_snapshot["teams"]], ["1", "2"])
        self.assertEqual(len(run.candidate_pool), 14)
        self.assertEqual(run.optimization_summary.candidate_pool_size, 14)
        self.assertGreater(run.target_strength, 0)
        self.assertIn("candidate pool", run.summary_text.lower())

    def test_solver_assigns_unique_catalog_names_when_captains_leave_names_empty(self):
        engine = AutoAssignmentEngine()

        first = engine.solve(
            self._captain_rows(),
            self._ready_player_rows(),
            self._dream_slot_rows(),
        )
        second = engine.solve(
            self._captain_rows(),
            self._ready_player_rows(),
            self._dream_slot_rows(),
        )

        first_names = [team["team_name"] for team in first.result_snapshot["teams"]]
        second_names = [team["team_name"] for team in second.result_snapshot["teams"]]
        self.assertEqual(len(TEAM_NAME_CATALOG), 256)
        self.assertEqual(len(set(TEAM_NAME_CATALOG)), 256)
        self.assertLessEqual(max(map(len, TEAM_NAME_CATALOG)), 15)
        self.assertEqual(first_names, second_names)
        self.assertEqual(len(set(first_names)), 2)
        self.assertTrue(set(first_names).issubset(set(TEAM_NAME_CATALOG)))

    def test_generated_team_name_does_not_reuse_an_explicit_catalog_name(self):
        engine = AutoAssignmentEngine()
        captain_rows = self._captain_rows()
        captain_rows[0] = {**captain_rows[0], "team_name": TEAM_NAME_CATALOG[0]}

        run = engine.solve(
            captain_rows,
            self._ready_player_rows(),
            self._dream_slot_rows(),
        )

        names = [team["team_name"] for team in run.result_snapshot["teams"]]
        self.assertEqual(names[0], TEAM_NAME_CATALOG[0])
        self.assertEqual(len(set(map(str.casefold, names))), 2)

    def test_solver_rejects_insufficient_players(self):
        engine = AutoAssignmentEngine()

        with self.assertRaises(AutoAssignmentError):
            engine.solve(
                self._captain_rows(),
                self._ready_player_rows()[:6],
                self._dream_slot_rows(),
            )

    def test_solver_accepts_string_user_ids_for_platform_native_runs(self):
        engine = AutoAssignmentEngine()
        captain_rows = [
            {**row, "user_id": f"captain-{row['user_id']}", "pool": row["pool"].split(",")}
            for row in self._captain_rows()
        ]
        ready_player_rows = [
            {**row, "user_id": f"player-{row['user_id']}", "pool": row["pool"].split(",")}
            for row in self._ready_player_rows()
        ]

        run = engine.solve(captain_rows, ready_player_rows, self._dream_slot_rows())

        self.assertTrue(all(isinstance(player.user_id, str) for player in run.candidate_pool))
        self.assertTrue(
            all(
                isinstance(team["captain"]["user_id"], str)
                for team in run.result_snapshot["teams"]
            )
        )
        self.assertTrue(run.result_snapshot["teams"][0]["captain"]["user_id"].startswith("captain-"))

    def test_solver_exposes_preference_metrics_for_shared_snapshot(self):
        engine = AutoAssignmentEngine()

        run = engine.solve(
            self._captain_rows(),
            self._ready_player_rows(),
            self._dream_slot_rows(),
        )

        metrics = run.result_snapshot["preference_metrics"]
        self.assertEqual(metrics["starter_slots_total"], 10)
        self.assertEqual(metrics["starter_preference_slots_total"], 8)
        self.assertEqual(metrics["starter_role_match_count"], 10)
        self.assertEqual(metrics["starter_desired_slots_with_any_match"], 6)
        self.assertEqual(metrics["starter_desired_heroes_hit_total"], 6)
        self.assertEqual(metrics["reserve_slots_total"], 2)
        self.assertIn("candidate pool", run.summary_text.lower())
        self.assertIn("mad силы команд", run.summary_text.lower())
        self.assertIn("std силы команд", run.summary_text.lower())

    def test_reserve_fallback_fills_slot_when_strength_cap_rejects_last_player(self):
        engine = AutoAssignmentEngine()
        team = engine.build_team_states(
            [self._captain_rows()[0]],
            [row for row in self._dream_slot_rows() if row["team_id"] == "1"],
        )[0]
        team.assignments = [
            Assignment(
                player=PlayerModel(
                    user_id=f"starter-{index}",
                    username=f"starter-{index}",
                    team_name=None,
                    rank="Acolyte",
                    subrank=1,
                    playtime="0-500",
                    pool=("Seven",),
                    roles=("Support",),
                    strength=1.0,
                ),
                slot=slot,
                assigned_role="Support",
                tier=0,
                desired_match_count=0,
                projected_spread_percent=0.0,
                cap_overflow=0,
            )
            for index, slot in enumerate(team.slots, start=1)
        ]
        strong_candidate = PlayerModel(
            user_id="strong-reserve",
            username="strong-reserve",
            team_name=None,
            rank="Eternus",
            subrank=6,
            playtime="3000+",
            pool=("Seven",),
            roles=("Support",),
            strength=1_000.0,
        )

        assignment = engine._choose_reserve_candidate(team, [strong_candidate])

        self.assertIsNotNone(assignment)
        self.assertEqual(assignment.player.user_id, "strong-reserve")

    def test_solver_computes_std_only_for_the_final_summary(self):
        engine = AutoAssignmentEngine()

        with patch.object(
            engine,
            "_compute_std_percent",
            wraps=engine._compute_std_percent,
        ) as compute_std:
            engine.solve(
                self._captain_rows(),
                self._ready_player_rows(),
                self._dream_slot_rows(),
            )

        self.assertEqual(compute_std.call_count, 1)

    def test_incremental_candidate_rank_matches_full_solution_rank(self):
        engine = AutoAssignmentEngine()
        run = engine.solve(
            self._captain_rows(),
            self._ready_player_rows(),
            self._dream_slot_rows(),
        )
        teams = list(run.teams)
        candidate_teams = engine._clone_selected_team_states(
            teams,
            {teams[0].team_id, teams[1].team_id},
        )
        left_assignment = candidate_teams[0].assignments[0]
        right_assignment = candidate_teams[1].assignments[0]
        candidate_teams[0].assignments[0] = right_assignment
        candidate_teams[1].assignments[0] = left_assignment

        search_metrics = engine._build_search_metrics(
            teams,
            run.target_strength,
        )
        candidate_metrics = engine._candidate_search_metrics(
            search_metrics,
            (candidate_teams[0], candidate_teams[1]),
            run.target_strength,
        )
        candidate_summary = engine._build_optimization_summary(
            candidate_teams,
            run.target_strength,
            threshold=engine.settings.balance_threshold_percent,
            candidate_pool_size=0,
            selected_player_count=sum(
                len(team.assignments) for team in candidate_teams
            ),
            source="rank regression",
            solver_improved=False,
            stage=None,
            include_std=False,
        )

        self.assertEqual(
            engine._solution_rank_from_search_metrics(candidate_metrics),
            engine._solution_rank(candidate_teams, candidate_summary),
        )
