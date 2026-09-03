from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools import platform_configure_shared_env as configure


class PlatformConfigureSharedEnvTests(unittest.TestCase):
    def test_merge_preserves_credentials_and_environment(self) -> None:
        lines = [
            "PLATFORM_ENVIRONMENT=development",
            "PLATFORM_DATABASE_URL=postgresql://user:secret@127.0.0.1/platformdb",
            "PLATFORM_SECRET_KEY=existing-secret",
            "PLATFORM_R2_ACCESS_KEY_ID=existing-access",
            "PLATFORM_R2_SECRET_ACCESS_KEY=existing-r2-secret",
            "PLATFORM_WEB_ORIGIN=http://127.0.0.1:3000",
            "PLATFORM_WEB_ORIGIN=http://duplicate.invalid",
            "PLATFORM_TURNSTILE_MODE=off",
            "PLATFORM_STEAM_LOGIN_ENABLED=true",
        ]
        content, changed = configure.merge_baseline(lines)
        self.assertIn("PLATFORM_ENVIRONMENT=development", content)
        self.assertIn("PLATFORM_SECRET_KEY=existing-secret", content)
        self.assertIn("PLATFORM_R2_SECRET_ACCESS_KEY=existing-r2-secret", content)
        self.assertIn("PLATFORM_TURNSTILE_MODE=off", content)
        self.assertIn("PLATFORM_WEB_ORIGIN=https://old-sparky.com", content)
        self.assertIn(
            "PLATFORM_EMAIL_SENDER_EMAIL='Old Sparky Arena <noreply@auth.old-sparky.com>'",
            content,
        )
        self.assertEqual(content.count("PLATFORM_WEB_ORIGIN="), 1)
        self.assertIn("PLATFORM_MEDIA_PUBLIC_BASE_URL", changed)
        self.assertIn("PLATFORM_STEAM_LOGIN_ENABLED=true", content)
        self.assertIn("PLATFORM_GOOGLE_LOGIN_ENABLED=false", content)
        self.assertIn(
            "PLATFORM_STEAM_CALLBACK_URL=https://old-sparky.com/api/v1/auth/steam/callback",
            content,
        )
        self.assertIn(
            "PLATFORM_GOOGLE_CALLBACK_URL=https://old-sparky.com/api/v1/auth/google/callback",
            content,
        )
        self.assertIn("PLATFORM_AUTH_DELIVERY_COOLDOWN_SECONDS=60", content)
        self.assertIn("PLATFORM_AUTH_HUMAN_VERIFICATION_TTL_SECONDS=900", content)
        self.assertIn(
            "PLATFORM_LOAD_TEST_SOURCE_IPS=95.217.190.107,2a01:4f9:c012:8011::1",
            content,
        )

    def test_merge_defaults_new_and_invalid_steam_rollout_flags_to_disabled(self) -> None:
        missing_content, _changed = configure.merge_baseline([])
        invalid_content, changed = configure.merge_baseline(
            ["PLATFORM_STEAM_LOGIN_ENABLED=unexpected"]
        )

        self.assertIn("PLATFORM_STEAM_LOGIN_ENABLED=false", missing_content)
        self.assertIn("PLATFORM_STEAM_LOGIN_ENABLED=false", invalid_content)
        self.assertIn("PLATFORM_STEAM_LOGIN_ENABLED", changed)

    def test_merge_defaults_new_and_invalid_google_rollout_flags_to_disabled(self) -> None:
        missing_content, _changed = configure.merge_baseline([])
        invalid_content, changed = configure.merge_baseline(
            ["PLATFORM_GOOGLE_LOGIN_ENABLED=unexpected"]
        )

        self.assertIn("PLATFORM_GOOGLE_LOGIN_ENABLED=false", missing_content)
        self.assertIn("PLATFORM_GOOGLE_LOGIN_ENABLED=false", invalid_content)
        self.assertIn("PLATFORM_GOOGLE_LOGIN_ENABLED", changed)

    def test_merge_can_select_one_reviewed_baseline_key(self) -> None:
        content, changed = configure.merge_baseline(
            ["PLATFORM_OPENAI_MODEL=existing-model"],
            baseline={"PLATFORM_LOAD_TEST_SOURCE_IPS": configure.PUBLIC_BASELINE["PLATFORM_LOAD_TEST_SOURCE_IPS"]},
        )

        self.assertEqual(changed, ["PLATFORM_LOAD_TEST_SOURCE_IPS"])
        self.assertIn(
            "PLATFORM_LOAD_TEST_SOURCE_IPS=95.217.190.107,2a01:4f9:c012:8011::1",
            content,
        )
        self.assertIn("PLATFORM_OPENAI_MODEL=existing-model", content)

    def test_load_baseline_matches_measured_10k_profile(self) -> None:
        self.assertEqual(configure.PUBLIC_BASELINE["PLATFORM_DB_POOL_SIZE"], "24")
        self.assertEqual(configure.PUBLIC_BASELINE["PLATFORM_DB_MAX_OVERFLOW"], "0")
        self.assertEqual(configure.PUBLIC_BASELINE["PLATFORM_DB_POOL_TIMEOUT_SECONDS"], "10")
        self.assertEqual(
            configure.PUBLIC_BASELINE["PLATFORM_DB_CONNECTION_BUDGET"],
            "52",
        )
        self.assertEqual(configure.PUBLIC_BASELINE["PLATFORM_GUNICORN_ACCESS_LOG"], "false")
        self.assertEqual(configure.PUBLIC_BASELINE["PLATFORM_WORKER_LOG_LEVEL"], "WARNING")
        self.assertEqual(configure.PUBLIC_BASELINE["PLATFORM_PERF_LOG_MUTATIONS"], "false")
        self.assertEqual(configure.PUBLIC_BASELINE["PLATFORM_DB_POOL_PRE_PING"], "true")
        self.assertEqual(configure.PUBLIC_BASELINE["PLATFORM_SSR_PERF_LOG_ENABLED"], "false")
        self.assertEqual(configure.PUBLIC_BASELINE["PLATFORM_SSR_PERF_SAMPLE_RATE"], "0.01")
        self.assertEqual(
            configure.PUBLIC_BASELINE["PLATFORM_SSR_PERF_EVENT_LOOP_INTERVAL_SECONDS"],
            "5",
        )
        self.assertEqual(
            configure.PUBLIC_BASELINE["PLATFORM_AUTHENTICATED_READ_ADMISSION_ENABLED"],
            "false",
        )
        self.assertEqual(configure.PUBLIC_BASELINE["PLATFORM_UVICORN_LOOP"], "auto")
        self.assertEqual(
            configure.PUBLIC_BASELINE["PLATFORM_READY_VOTE_ADMISSION_MIN_CONCURRENCY"],
            "4",
        )
        self.assertEqual(
            configure.PUBLIC_BASELINE["PLATFORM_READY_VOTE_ADMISSION_INITIAL_CONCURRENCY"],
            "8",
        )
        self.assertEqual(
            configure.PUBLIC_BASELINE["PLATFORM_READY_VOTE_ADMISSION_MAX_CONCURRENCY"],
            "16",
        )

    def test_capacity_profile_preserves_connection_budget(self) -> None:
        profile = configure.RUNTIME_PROFILES["api-3x16"]
        self.assertEqual(profile["PLATFORM_API_WORKERS"], "3")
        self.assertEqual(profile["PLATFORM_DB_POOL_SIZE"], "16")
        self.assertEqual(profile["PLATFORM_DB_MAX_OVERFLOW"], "0")
        self.assertEqual(profile["PLATFORM_DB_CONNECTION_BUDGET"], "52")

        single_worker = configure.RUNTIME_PROFILES["api-1x48"]
        self.assertEqual(single_worker["PLATFORM_API_WORKERS"], "1")
        self.assertEqual(single_worker["PLATFORM_DB_POOL_SIZE"], "48")
        self.assertEqual(single_worker["PLATFORM_DB_MAX_OVERFLOW"], "0")
        self.assertEqual(single_worker["PLATFORM_DB_CONNECTION_BUDGET"], "52")

    def test_ready_vote_static_profiles_are_limited_to_reviewed_sweep_points(self) -> None:
        expected_limits = (4, 6, 8, 12, 16)
        self.assertEqual(
            {
                int(name.rsplit("-", 1)[1])
                for name in configure.RUNTIME_PROFILES
                if name.startswith("ready-vote-static-")
            },
            set(expected_limits),
        )
        for limit in expected_limits:
            profile = configure.RUNTIME_PROFILES[f"ready-vote-static-{limit}"]
            self.assertEqual(
                profile,
                {
                    "PLATFORM_READY_VOTE_ADMISSION_MIN_CONCURRENCY": str(limit),
                    "PLATFORM_READY_VOTE_ADMISSION_INITIAL_CONCURRENCY": str(limit),
                    "PLATFORM_READY_VOTE_ADMISSION_MAX_CONCURRENCY": str(limit),
                },
            )

    def test_ready_vote_adaptive_v2_preserves_worker_and_database_envelope(self) -> None:
        profile = configure.RUNTIME_PROFILES["ready-vote-adaptive-v2"]
        self.assertEqual(profile["PLATFORM_READY_VOTE_ADMISSION_MIN_CONCURRENCY"], "4")
        self.assertEqual(profile["PLATFORM_READY_VOTE_ADMISSION_INITIAL_CONCURRENCY"], "8")
        self.assertEqual(profile["PLATFORM_READY_VOTE_ADMISSION_MAX_CONCURRENCY"], "12")
        self.assertEqual(profile["PLATFORM_READY_VOTE_ADMISSION_MAX_WAITERS"], "0")
        self.assertEqual(profile["PLATFORM_READY_VOTE_ADMISSION_WAIT_TIMEOUT_MS"], "0")
        self.assertNotIn("PLATFORM_API_WORKERS", profile)
        self.assertNotIn("PLATFORM_DB_POOL_SIZE", profile)
        self.assertNotIn("PLATFORM_DB_CONNECTION_BUDGET", profile)

    def test_read_path_candidate_profiles_are_explicit_and_bounded(self) -> None:
        self.assertEqual(
            configure.RUNTIME_PROFILES["authenticated-read-admission-32"],
            {
                "PLATFORM_AUTHENTICATED_READ_ADMISSION_ENABLED": "true",
                "PLATFORM_AUTHENTICATED_READ_ADMISSION_CONCURRENCY": "32",
                "PLATFORM_AUTHENTICATED_READ_ADMISSION_MAX_WAITERS": "0",
                "PLATFORM_AUTHENTICATED_READ_ADMISSION_WAIT_TIMEOUT_MS": "0",
            },
        )
        self.assertEqual(
            configure.RUNTIME_PROFILES["pool-pre-ping-off"],
            {"PLATFORM_DB_POOL_PRE_PING": "false"},
        )
        self.assertEqual(
            configure.RUNTIME_PROFILES["web-ssr-diagnostics"],
            {
                "PLATFORM_SSR_PERF_LOG_ENABLED": "true",
                "PLATFORM_SSR_PERF_SAMPLE_RATE": "0.01",
                "PLATFORM_SSR_PERF_EVENT_LOOP_INTERVAL_SECONDS": "5",
            },
        )
        for pool_size in (12, 16, 20, 24):
            self.assertEqual(
                configure.RUNTIME_PROFILES[f"api-pool-{pool_size}"][
                    "PLATFORM_DB_POOL_SIZE"
                ],
                str(pool_size),
            )

    def test_atomic_write_preserves_private_owner_group_and_mode(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / ".env.platform"
            path.write_text("OLD=value\n", encoding="utf-8")
            path.chmod(0o640)
            metadata = path.stat()
            configure.atomic_write(path, "NEW=value\n", metadata)
            updated = path.stat()
            self.assertEqual(path.read_text(encoding="utf-8"), "NEW=value\n")
            self.assertEqual(updated.st_uid, os.geteuid())
            self.assertEqual(updated.st_gid, metadata.st_gid)
            self.assertEqual(updated.st_mode & 0o777, 0o640)


if __name__ == "__main__":
    unittest.main()
