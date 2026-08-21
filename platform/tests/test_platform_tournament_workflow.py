from __future__ import annotations

from datetime import UTC, datetime, timedelta
import unittest

from python_packages.platform_domain.tournaments import (
    ExistingBracketMatchState,
    TournamentWorkflowError,
    available_match_statuses,
    available_tournament_statuses,
    build_next_round_matches,
    build_seeded_opening_round_matches,
    can_view_tournament_summary,
    can_view_tournament_workspace,
    can_organizer_manage_participants,
    can_organizer_moderate_participants,
    can_self_join_tournament,
    can_self_leave_tournament,
    ensure_deadlock_match_staging_allowed,
    eliminated_team_id_for_single_elimination,
    ensure_completed_match_reopen_allowed,
    ensure_match_admin_actions_allowed,
    ensure_match_deletion_allowed,
    ensure_match_report_allowed,
    ensure_match_round_staging_allowed,
    ensure_match_schedule_allowed,
    ensure_tournament_completion_has_final_result,
    ensure_deadlock_registration_changes_allowed,
    ensure_deadlock_roster_staging_allowed,
    ensure_invite_claimable,
    next_match_statuses,
    next_participant_statuses,
    next_tournament_statuses,
    resolve_match_report,
    transition_participant_status,
    transition_match_status,
    transition_tournament_status,
)


class PlatformTournamentWorkflowTests(unittest.TestCase):
    def test_single_elimination_returns_only_the_losing_team(self):
        self.assertEqual(
            eliminated_team_id_for_single_elimination(
                home_team_id="home",
                away_team_id="away",
                winner_team_id="home",
            ),
            "away",
        )
        with self.assertRaises(TournamentWorkflowError):
            eliminated_team_id_for_single_elimination(
                home_team_id="home",
                away_team_id="home",
                winner_team_id="home",
            )

    def test_tournament_status_flow_exposes_next_steps(self):
        self.assertEqual(
            next_tournament_statuses("registration_closed"),
            ("registration_open", "in_progress", "cancelled"),
        )
        self.assertEqual(
            transition_tournament_status("registration_closed", "in_progress"),
            "in_progress",
        )

    def test_deadlock_handoff_requires_locked_roster_before_in_progress(self):
        self.assertEqual(
            available_tournament_statuses(
                "registration_closed",
                format_slug="solo",
                has_locked_deadlock_roster=False,
            ),
            ("registration_open", "cancelled"),
        )
        with self.assertRaises(TournamentWorkflowError):
            transition_tournament_status(
                "registration_closed",
                "in_progress",
                format_slug="solo",
                has_locked_deadlock_roster=False,
            )

    def test_deadlock_locked_roster_blocks_reopening_registration(self):
        self.assertEqual(
            available_tournament_statuses(
                "registration_closed",
                format_slug="solo",
                has_locked_deadlock_roster=True,
            ),
            ("in_progress", "cancelled"),
        )
        with self.assertRaises(TournamentWorkflowError):
            transition_tournament_status(
                "registration_closed",
                "registration_open",
                format_slug="solo",
                has_locked_deadlock_roster=True,
            )

    def test_tournament_status_flow_rejects_invalid_jump(self):
        with self.assertRaises(TournamentWorkflowError):
            transition_tournament_status("registration_open", "completed")

    def test_join_and_leave_rules_follow_registration_window(self):
        self.assertTrue(can_self_join_tournament("registration_open"))
        self.assertFalse(can_self_join_tournament("registration_closed"))
        self.assertTrue(can_self_leave_tournament("registration_closed"))
        self.assertFalse(can_self_leave_tournament("in_progress"))

    def test_tournament_summary_visibility_requires_auth_for_invite_only(self):
        self.assertTrue(
            can_view_tournament_summary(
                tournament_visibility="public",
                has_authenticated_user=False,
            )
        )
        self.assertFalse(
            can_view_tournament_summary(
                tournament_visibility="invite_only",
                has_authenticated_user=False,
            )
        )
        self.assertTrue(
            can_view_tournament_summary(
                tournament_visibility="invite_only",
                has_authenticated_user=True,
            )
        )

    def test_tournament_workspace_visibility_stays_scoped_for_invite_only(self):
        self.assertTrue(
            can_view_tournament_workspace(
                tournament_visibility="public",
                is_participant=False,
                is_organizer=False,
                is_admin=False,
            )
        )
        self.assertFalse(
            can_view_tournament_workspace(
                tournament_visibility="invite_only",
                is_participant=False,
                is_organizer=False,
                is_admin=False,
            )
        )
        self.assertTrue(
            can_view_tournament_workspace(
                tournament_visibility="invite_only",
                is_participant=True,
                is_organizer=False,
                is_admin=False,
            )
        )
        self.assertTrue(
            can_view_tournament_workspace(
                tournament_visibility="invite_only",
                is_participant=False,
                is_organizer=True,
                is_admin=False,
            )
        )
        self.assertTrue(
            can_view_tournament_workspace(
                tournament_visibility="invite_only",
                is_participant=False,
                is_organizer=False,
                is_admin=True,
            )
        )

    def test_organizer_can_manage_participants_only_before_start(self):
        self.assertTrue(can_organizer_manage_participants("registration_open"))
        self.assertTrue(can_organizer_manage_participants("registration_closed"))
        self.assertFalse(can_organizer_manage_participants("in_progress"))

    def test_deadlock_registration_changes_block_after_locked_roster(self):
        with self.assertRaises(TournamentWorkflowError):
            ensure_deadlock_registration_changes_allowed(
                format_slug="solo",
                has_locked_deadlock_roster=True,
            )

    def test_organizer_can_moderate_participants_until_tournament_ends(self):
        self.assertTrue(can_organizer_moderate_participants("registration_closed"))
        self.assertTrue(can_organizer_moderate_participants("in_progress"))
        self.assertFalse(can_organizer_moderate_participants("completed"))

    def test_deadlock_staging_requires_closed_registration_and_unlocked_roster(self):
        with self.assertRaises(TournamentWorkflowError):
            ensure_deadlock_roster_staging_allowed(
                format_slug="solo",
                tournament_status="registration_open",
                has_locked_deadlock_roster=False,
                action_name="Deadlock ready-check",
            )

        with self.assertRaises(TournamentWorkflowError):
            ensure_deadlock_roster_staging_allowed(
                format_slug="solo",
                tournament_status="registration_closed",
                has_locked_deadlock_roster=True,
                action_name="Deadlock ready-check",
            )

        ensure_deadlock_roster_staging_allowed(
            format_slug="solo",
            tournament_status="registration_closed",
            has_locked_deadlock_roster=False,
            action_name="Deadlock ready-check",
        )

    def test_deadlock_match_creation_requires_locked_roster_handoff(self):
        with self.assertRaises(TournamentWorkflowError):
            ensure_deadlock_match_staging_allowed(
                format_slug="solo",
                has_locked_deadlock_roster=False,
            )

        ensure_deadlock_match_staging_allowed(
            format_slug="solo",
            has_locked_deadlock_roster=True,
        )

    def test_opening_round_seeding_uses_deadlock_team_pairing(self):
        seeded_matches = build_seeded_opening_round_matches(
            ("Team 1", "Team 2", "Team 3", "Team 4")
        )

        self.assertEqual(
            [(match.home_label, match.away_label, match.title) for match in seeded_matches],
            [
                ("Team 1", "Team 4", "Semifinal 1"),
                ("Team 2", "Team 3", "Semifinal 2"),
            ],
        )

    def test_opening_round_seeding_supports_eight_team_bracket(self):
        seeded_matches = build_seeded_opening_round_matches(
            tuple(f"Team {index}" for index in range(1, 9))
        )

        self.assertEqual(
            [(match.home_label, match.away_label) for match in seeded_matches],
            [
                ("Team 1", "Team 8"),
                ("Team 4", "Team 5"),
                ("Team 3", "Team 6"),
                ("Team 2", "Team 7"),
            ],
        )

    def test_opening_round_seeding_rejects_non_power_of_two_team_count(self):
        with self.assertRaises(TournamentWorkflowError):
            build_seeded_opening_round_matches(("Team 1", "Team 2", "Team 3"))

    def test_next_round_progression_uses_completed_winners_in_order(self):
        next_round = build_next_round_matches(
            1,
            ("Team 1", "Team 4", "Team 2", "Team 3"),
        )

        self.assertEqual(
            [(match.round_number, match.sequence_number, match.home_label, match.away_label, match.title) for match in next_round],
            [
                (2, 1, "Team 1", "Team 4", "Semifinal 1"),
                (2, 2, "Team 2", "Team 3", "Semifinal 2"),
            ],
        )

    def test_next_round_progression_creates_grand_final_from_semifinal_winners(self):
        next_round = build_next_round_matches(
            2,
            ("Team 1", "Team 2"),
        )

        self.assertEqual(len(next_round), 1)
        self.assertEqual(next_round[0].round_number, 3)
        self.assertEqual(next_round[0].title, "Grand Final")
        self.assertEqual((next_round[0].home_label, next_round[0].away_label), ("Team 1", "Team 2"))

    def test_next_round_progression_rejects_incomplete_pairing(self):
        with self.assertRaises(TournamentWorkflowError):
            build_next_round_matches(1, ("Team 1", "Team 2", "Team 3"))

    def test_manual_match_staging_blocks_future_round_until_latest_round_completes(self):
        with self.assertRaises(TournamentWorkflowError):
            ensure_match_round_staging_allowed(
                2,
                (
                    ExistingBracketMatchState(round_number=1, status="scheduled"),
                    ExistingBracketMatchState(round_number=1, status="completed"),
                ),
            )

    def test_manual_match_staging_blocks_backfill_after_downstream_round_exists(self):
        with self.assertRaises(TournamentWorkflowError):
            ensure_match_round_staging_allowed(
                1,
                (
                    ExistingBracketMatchState(round_number=1, status="completed"),
                    ExistingBracketMatchState(round_number=2, status="scheduled"),
                ),
            )

    def test_manual_match_staging_requires_sequential_round_creation(self):
        with self.assertRaises(TournamentWorkflowError):
            ensure_match_round_staging_allowed(
                4,
                (
                    ExistingBracketMatchState(round_number=1, status="completed"),
                    ExistingBracketMatchState(round_number=1, status="completed"),
                    ExistingBracketMatchState(round_number=2, status="completed"),
                    ExistingBracketMatchState(round_number=2, status="completed"),
                ),
            )

    def test_manual_match_staging_blocks_later_round_after_single_completed_final(self):
        with self.assertRaises(TournamentWorkflowError):
            ensure_match_round_staging_allowed(
                3,
                (ExistingBracketMatchState(round_number=2, status="completed"),),
            )

    def test_manual_match_staging_surfaces_cancelled_round_recovery_hint(self):
        with self.assertRaisesRegex(
            TournamentWorkflowError,
            "Round 1 has cancelled matches. Reset them to scheduled before staging round 2.",
        ):
            ensure_match_round_staging_allowed(
                2,
                (
                    ExistingBracketMatchState(round_number=1, status="cancelled"),
                    ExistingBracketMatchState(round_number=1, status="completed"),
                ),
            )

    def test_tournament_completion_requires_single_completed_latest_round_match(self):
        with self.assertRaises(TournamentWorkflowError):
            ensure_tournament_completion_has_final_result(
                (
                    ExistingBracketMatchState(round_number=1, status="completed"),
                    ExistingBracketMatchState(round_number=1, status="completed"),
                )
            )

        with self.assertRaises(TournamentWorkflowError):
            ensure_tournament_completion_has_final_result(
                (ExistingBracketMatchState(round_number=2, status="cancelled"),)
            )

        ensure_tournament_completion_has_final_result(
            (ExistingBracketMatchState(round_number=2, status="completed"),)
        )

    def test_tournament_completion_surfaces_cancelled_round_recovery_hint(self):
        with self.assertRaisesRegex(
            TournamentWorkflowError,
            "Round 2 has cancelled matches. Reset them to scheduled before marking the tournament completed.",
        ):
            ensure_tournament_completion_has_final_result(
                (
                    ExistingBracketMatchState(round_number=2, status="cancelled"),
                    ExistingBracketMatchState(round_number=2, status="completed"),
                )
            )

    def test_match_admin_actions_block_after_terminal_tournament_state(self):
        with self.assertRaisesRegex(
            TournamentWorkflowError,
            "Match administration is unavailable after the tournament is completed or cancelled.",
        ):
            ensure_match_admin_actions_allowed("completed")

        with self.assertRaisesRegex(
            TournamentWorkflowError,
            "Match administration is unavailable after the tournament is completed or cancelled.",
        ):
            ensure_match_admin_actions_allowed("cancelled")

        ensure_match_admin_actions_allowed("registration_closed")
        ensure_match_admin_actions_allowed("in_progress")

    def test_match_report_allowed_after_bracket_is_ready(self):
        ensure_match_report_allowed("registration_closed")
        ensure_match_report_allowed("in_progress")
        with self.assertRaisesRegex(
            TournamentWorkflowError,
            "Match results can be reported after registration is closed",
        ):
            ensure_match_report_allowed("registration_open")
        with self.assertRaisesRegex(
            TournamentWorkflowError,
            "Match administration is unavailable",
        ):
            ensure_match_report_allowed("completed")

    def test_match_schedule_enforces_future_and_bracket_order(self):
        now = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
        source_start = now + timedelta(hours=1)
        next_round_start = now + timedelta(hours=3)

        ensure_match_schedule_allowed(
            scheduled_at=now + timedelta(hours=2),
            now=now,
            source_scheduled_at=(source_start,),
            dependent_scheduled_at=(next_round_start,),
        )
        ensure_match_schedule_allowed(scheduled_at=None, now=now)

        with self.assertRaisesRegex(TournamentWorkflowError, "must be in the future"):
            ensure_match_schedule_allowed(scheduled_at=now, now=now)
        with self.assertRaisesRegex(TournamentWorkflowError, "cannot start before"):
            ensure_match_schedule_allowed(
                scheduled_at=now + timedelta(minutes=30),
                now=now,
                source_scheduled_at=(source_start,),
            )
        with self.assertRaisesRegex(TournamentWorkflowError, "cannot start after"):
            ensure_match_schedule_allowed(
                scheduled_at=now + timedelta(hours=4),
                now=now,
                dependent_scheduled_at=(next_round_start,),
            )

    def test_completed_latest_round_match_can_be_reopened(self):
        self.assertEqual(
            available_match_statuses(
                "completed",
                tournament_status="in_progress",
                current_round_number=2,
                latest_round_number=2,
            ),
            ("scheduled",),
        )
        ensure_completed_match_reopen_allowed(current_round_number=2, latest_round_number=2)

    def test_completed_match_reopen_blocks_when_later_round_exists(self):
        self.assertEqual(
            available_match_statuses(
                "completed",
                tournament_status="in_progress",
                current_round_number=1,
                latest_round_number=2,
            ),
            (),
        )
        with self.assertRaisesRegex(
            TournamentWorkflowError,
            "Completed matches in round 1 cannot be reset while round 2 matches already exist. Delete later-round matches first.",
        ):
            ensure_completed_match_reopen_allowed(current_round_number=1, latest_round_number=2)

    def test_latest_round_match_delete_allows_only_scheduled_or_cancelled(self):
        ensure_match_deletion_allowed(
            tournament_status="registration_closed",
            current_status="scheduled",
            current_round_number=2,
            latest_round_number=2,
        )
        ensure_match_deletion_allowed(
            tournament_status="in_progress",
            current_status="cancelled",
            current_round_number=2,
            latest_round_number=2,
        )
        with self.assertRaisesRegex(
            TournamentWorkflowError,
            "Only scheduled or cancelled latest-round matches can be deleted for bracket recovery.",
        ):
            ensure_match_deletion_allowed(
                tournament_status="in_progress",
                current_status="completed",
                current_round_number=2,
                latest_round_number=2,
            )

    def test_match_delete_blocks_earlier_round_or_terminal_tournament(self):
        with self.assertRaisesRegex(
            TournamentWorkflowError,
            "Only latest-round matches can be deleted for bracket recovery. Delete round 2 matches before removing round 1.",
        ):
            ensure_match_deletion_allowed(
                tournament_status="in_progress",
                current_status="scheduled",
                current_round_number=1,
                latest_round_number=2,
            )

        with self.assertRaisesRegex(
            TournamentWorkflowError,
            "Match administration is unavailable after the tournament is completed or cancelled.",
        ):
            ensure_match_deletion_allowed(
                tournament_status="completed",
                current_status="scheduled",
                current_round_number=2,
                latest_round_number=2,
            )

    def test_participant_status_validation_accepts_known_states(self):
        self.assertIn("checked_in", next_participant_statuses("registered"))
        self.assertEqual(transition_participant_status("confirmed", "disqualified"), "disqualified")

    def test_participant_status_validation_rejects_unknown_state(self):
        with self.assertRaises(TournamentWorkflowError):
            transition_participant_status("registered", "banned")

    def test_match_status_flow_supports_reopen_after_cancel(self):
        self.assertEqual(next_match_statuses("cancelled"), ("scheduled",))
        self.assertEqual(transition_match_status("cancelled", "scheduled"), "scheduled")

    def test_match_report_picks_winner_and_marks_completed(self):
        report = resolve_match_report(
            current_status="live",
            home_score=2,
            away_score=1,
            note="Upper bracket semifinal",
        )

        self.assertEqual(report.status, "completed")
        self.assertEqual(report.winner_side, "home")
        self.assertEqual(report.note, "Upper bracket semifinal")

    def test_match_report_rejects_draws(self):
        with self.assertRaises(TournamentWorkflowError):
            resolve_match_report(
                current_status="scheduled",
                home_score=1,
                away_score=1,
                note=None,
            )

    def test_invite_claim_requires_open_registration(self):
        with self.assertRaises(TournamentWorkflowError):
            ensure_invite_claimable(
                tournament_visibility="invite_only",
                tournament_status="registration_closed",
                max_uses=1,
                use_count=0,
                revoked_at=None,
                expires_at=None,
                now=datetime.now(UTC),
            )

    def test_invite_claim_rejects_expired_or_used_invite(self):
        now = datetime.now(UTC)
        with self.assertRaises(TournamentWorkflowError):
            ensure_invite_claimable(
                tournament_visibility="invite_only",
                tournament_status="registration_open",
                max_uses=1,
                use_count=1,
                revoked_at=None,
                expires_at=None,
                now=now,
            )

        with self.assertRaises(TournamentWorkflowError):
            ensure_invite_claimable(
                tournament_visibility="invite_only",
                tournament_status="registration_open",
                max_uses=2,
                use_count=0,
                revoked_at=None,
                expires_at=now - timedelta(minutes=1),
                now=now,
            )

    def test_invite_claim_accepts_active_invite_only_tournament(self):
        ensure_invite_claimable(
            tournament_visibility="invite_only",
            tournament_status="registration_open",
            max_uses=2,
            use_count=1,
            revoked_at=None,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            now=datetime.now(UTC),
        )
