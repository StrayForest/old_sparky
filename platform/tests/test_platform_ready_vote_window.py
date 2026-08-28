from __future__ import annotations

from datetime import UTC, datetime, timedelta
import unittest

from apps.platform_api.app.services.tournament_workflow import (
    ReadyCheckVoteWindowError,
    ensure_ready_check_vote_window,
)


class PlatformReadyVoteWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.starts_at = datetime(2026, 6, 26, 12, 0, tzinfo=UTC)
        self.ends_at = self.starts_at + timedelta(minutes=10)

    def test_server_time_boundaries_are_exact(self) -> None:
        accepted = (
            self.starts_at,
            self.starts_at + timedelta(milliseconds=1),
            self.ends_at - timedelta(milliseconds=1),
        )
        for now in accepted:
            with self.subTest(now=now):
                ensure_ready_check_vote_window(
                    starts_at=self.starts_at,
                    ends_at=self.ends_at,
                    now=now,
                )

        for now in (
            self.starts_at - timedelta(milliseconds=1),
            self.ends_at,
            self.ends_at + timedelta(milliseconds=1),
        ):
            with self.subTest(now=now), self.assertRaises(ReadyCheckVoteWindowError):
                ensure_ready_check_vote_window(
                    starts_at=self.starts_at,
                    ends_at=self.ends_at,
                    now=now,
                )

    def test_missing_or_invalid_schedule_is_rejected(self) -> None:
        for starts_at, ends_at in (
            (None, self.ends_at),
            (self.starts_at, None),
            (self.starts_at, self.starts_at),
        ):
            with self.subTest(starts_at=starts_at, ends_at=ends_at):
                with self.assertRaises(ReadyCheckVoteWindowError):
                    ensure_ready_check_vote_window(
                        starts_at=starts_at,
                        ends_at=ends_at,
                        now=self.starts_at,
                    )

if __name__ == "__main__":
    unittest.main()
