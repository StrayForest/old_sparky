from __future__ import annotations

import unittest

from python_packages.platform_domain.deadlock import (
    assign_captain_team_numbers,
    prepare_ready_check_start,
    prepare_captain_round_entries,
    resolve_effective_teams_count,
    select_captain_offer_candidates,
    sort_captain_candidates,
)
from python_packages.platform_domain.deadlock.ready_check import ReadyCheckRoundState

class PlatformDeadlockWorkflowStateTests(unittest.TestCase):
    @staticmethod
    def _captain_rows() -> list[dict[str, object]]:
        return [
            {
                "user_id": 22,
                "rank": "Владыка",
                "subrank": 2,
                "playtime": "1001-1500",
                "captain_priority_bucket": 0,
                "strength": 11.0,
            },
            {
                "user_id": 11,
                "rank": "Владыка",
                "subrank": 1,
                "playtime": "3000+",
                "captain_priority_bucket": 0,
                "strength": 10.5,
            },
            {
                "user_id": 33,
                "rank": "Этернус",
                "subrank": 1,
                "playtime": "1001-1500",
                "captain_priority_bucket": 1,
                "strength": 12.0,
            },
        ]

    def test_prepare_ready_check_start_deduplicates_users_and_rejects_empty_or_duplicate_rounds(self):
        active_decision = prepare_ready_check_start([7, 7, 9], has_active_round=True)
        self.assertEqual(active_decision.status, "already_active")
        self.assertEqual(active_decision.user_ids, ("7", "9"))

        empty_decision = prepare_ready_check_start([], has_active_round=False)
        self.assertEqual(empty_decision.status, "empty")
        self.assertEqual(empty_decision.user_ids, ())

        created_decision = prepare_ready_check_start([9, 7, 9], has_active_round=False)
        self.assertEqual(created_decision.status, "created")
        self.assertEqual(created_decision.user_ids, ("9", "7"))
        self.assertTrue(created_decision.should_create_round)

    def test_ready_check_round_state_tracks_votes_and_round_mismatch(self):
        state = ReadyCheckRoundState.active(round_id=55, eligible_user_ids=[10, 11])

        next_state, vote = state.record_vote(10, "yes", round_id=55)
        self.assertEqual(vote.status, "updated")
        self.assertEqual(vote.ready_count, 1)
        self.assertEqual(next_state.ready_user_ids, ("10",))

        same_state, repeated_vote = next_state.record_vote(10, "yes", round_id=55)
        self.assertEqual(repeated_vote.status, "unchanged")
        self.assertEqual(same_state, next_state)

        declined_state, decline_vote = next_state.record_vote(11, "no", round_id=55)
        self.assertEqual(decline_vote.status, "updated")
        self.assertEqual(decline_vote.declined_count, 1)
        self.assertEqual(declined_state.declined_user_ids, ("11",))

        mismatched_state, mismatch_vote = declined_state.record_vote(11, "yes", round_id=99)
        self.assertEqual(mismatch_vote.status, "round_mismatch")
        self.assertEqual(mismatched_state, declined_state)

        closed_state = declined_state.close()
        _, closed_vote = closed_state.record_vote(10, "no", round_id=55)
        self.assertEqual(closed_vote.status, "closed")

    def test_ready_check_round_state_excludes_removed_users_and_stops_when_empty(self):
        state = ReadyCheckRoundState.active(
            round_id=55,
            eligible_user_ids=[10, 11],
            votes=[{"user_id": 10, "choice": "yes"}, {"user_id": 11, "choice": "no"}],
        )

        pruned_state = state.exclude_user(11)
        self.assertEqual(pruned_state.status, "active")
        self.assertEqual(pruned_state.eligible_user_ids, ("10",))
        self.assertEqual(pruned_state.ready_user_ids, ("10",))
        self.assertEqual(pruned_state.declined_user_ids, ())

        stopped_state = pruned_state.exclude_user(10)
        self.assertEqual(stopped_state.status, "stopped")
        self.assertEqual(stopped_state.eligible_user_ids, ())
        self.assertEqual(stopped_state.votes, ())

    def test_captain_candidate_sorting_uses_priority_then_strength(self):
        shared_result = sort_captain_candidates(self._captain_rows())

        self.assertEqual(
            [candidate.user_id for candidate in shared_result],
            ["22", "11", "33"],
        )

    def test_captain_team_assignment_uses_strength_order(self):
        shared_assignments = assign_captain_team_numbers(self._captain_rows())

        self.assertEqual(
            [(assignment.user_id, assignment.team_id) for assignment in shared_assignments],
            [("33", "1"), ("22", "2"), ("11", "3")],
        )

    def test_captain_offer_selection_uses_sorted_candidate_slice(self):
        shared_selected = select_captain_offer_candidates(self._captain_rows(), 2)

        self.assertEqual(
            [candidate.user_id for candidate in shared_selected],
            ["22", "11"],
        )

    def test_effective_team_count_uses_seven_player_teams_and_power_of_two_cap(self):
        self.assertEqual(
            resolve_effective_teams_count(requested_teams_count=64, ready_player_count=60),
            8,
        )
        self.assertEqual(
            resolve_effective_teams_count(requested_teams_count=None, ready_player_count=900),
            128,
        )
        self.assertEqual(
            resolve_effective_teams_count(requested_teams_count=3, ready_player_count=60),
            4,
        )
        self.assertEqual(
            resolve_effective_teams_count(requested_teams_count=8191, ready_player_count=60_000),
            8192,
        )
        with self.assertRaises(ValueError):
            resolve_effective_teams_count(requested_teams_count=2, ready_player_count=13)

    def test_auto_assigned_captain_entries_skip_offer_confirmation(self):
        entries = prepare_captain_round_entries(self._captain_rows(), teams_count=2, auto_assign=True)

        assigned = [entry for entry in entries if entry.state == "assigned"]
        self.assertEqual(len(assigned), 2)
        self.assertEqual({entry.assigned_team_id for entry in assigned}, {"1", "2"})
        self.assertEqual(entries[-1].state, "queued")
