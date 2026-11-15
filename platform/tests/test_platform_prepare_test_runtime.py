from __future__ import annotations

import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

from python_packages.platform_infra.config import PlatformSettings
from tools import platform_prepare_test_runtime as prepare


class PlatformPrepareTestRuntimeTests(unittest.TestCase):
    def test_rewrites_only_local_runtime_and_removes_production_integrations(self) -> None:
        lines = [
            "PLATFORM_ENVIRONMENT=development",
            "PLATFORM_DATABASE_URL=postgresql+asyncpg://prod:secret@127.0.0.1:5432/platformdb",
            "PLATFORM_REDIS_URL=redis://127.0.0.1:6379/0",
            "PLATFORM_SECRET_KEY=production-secret",
            "PLATFORM_OBJECT_STORAGE_BACKEND=r2",
            "PLATFORM_COOKIE_SECURE=false",
            "PLATFORM_R2_SECRET_ACCESS_KEY=must-disappear",
            "PLATFORM_RESEND_API_KEY=must-disappear",
            "PLATFORM_EMAIL_SENDER_EMAIL=must-disappear",
            "PLATFORM_SUPPORT_RECIPIENT_EMAIL=must-disappear",
            "PLATFORM_TURNSTILE_SECRET_KEY=must-disappear",
            "UNRELATED_SETTING=retained",
        ]
        database_url = prepare.test_database_url(lines[1].split("=", 1)[1], "new secret")
        content = prepare.sanitized_env(
            lines,
            database_url=database_url,
            redis_url=prepare.test_redis_url(lines[2].split("=", 1)[1]),
            test_secret="test-only-secret",
        )
        self.assertIn("PLATFORM_ENVIRONMENT=test", content)
        self.assertIn("/platformdb_test", content)
        self.assertIn("PLATFORM_REDIS_URL=redis://127.0.0.1:6379/15", content)
        self.assertIn("PLATFORM_OBJECT_STORAGE_BACKEND=local", content)
        self.assertEqual(content.count("PLATFORM_COOKIE_SECURE=true"), 1)
        self.assertNotIn("PLATFORM_SESSION_COOKIE_SECURE", content)
        self.assertIn("UNRELATED_SETTING=retained", content)
        self.assertNotIn("must-disappear", content)
        with patch.dict(
            os.environ,
            {"PLATFORM_COOKIE_SECURE": "true"},
            clear=False,
        ):
            settings = PlatformSettings(_env_file=None)
        self.assertTrue(settings.platform_cookie_secure)
        parsed = urlsplit(database_url)
        self.assertEqual(parsed.username, prepare.TEST_DATABASE_USER)
        self.assertEqual(unquote(parsed.password or ""), "new secret")

    def test_refuses_remote_or_unrelated_databases(self) -> None:
        for url in (
            "postgresql+asyncpg://user:secret@db.example.test/platformdb",
            "postgresql+asyncpg://user:secret@127.0.0.1/sparkydb",
        ):
            with self.subTest(url=url), self.assertRaises(RuntimeError):
                prepare.loopback_database_parts(url)

    def test_postgres_bootstrap_binds_admin_commands_to_source_host_and_port(self) -> None:
        calls: list[list[str]] = []
        outputs = [
            "postgres|postgres|127.0.0.1|55432",
            "",
            "1",
            "",
        ]

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, outputs.pop(0), "")

        with patch.dict(os.environ, {"PGHOST": "127.0.0.1", "PGPORT": "5432"}), patch.object(
            prepare.subprocess, "run", side_effect=run,
        ):
            prepare.prepare_postgres("test-password", host="127.0.0.1", port=55432)

        self.assertGreaterEqual(len(calls), 4)
        for command in calls:
            self.assertIn("--host=127.0.0.1", command)
            self.assertIn("--port=55432", command)
            self.assertNotIn("--port=5432", command)
        self.assertIn("inet_server_port()", calls[0][-1])

    def test_postgres_bootstrap_fails_before_writes_when_bound_cluster_is_unavailable(self) -> None:
        with patch.object(
            prepare.subprocess,
            "run",
            side_effect=subprocess.CalledProcessError(2, ["psql"]),
        ) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                prepare.prepare_postgres("test-password", host="127.0.0.1", port=55432)
        run.assert_called_once()

    def test_database_target_requires_literal_loopback_and_no_query_override(self) -> None:
        for url in (
            "postgresql+asyncpg://user:secret@localhost:55432/platformdb_test",
            "postgresql+asyncpg://user:secret@127.0.0.1:55432/platformdb_test?host=127.0.0.1",
            "postgresql+asyncpg://user:secret@127.0.0.1:0/platformdb_test",
        ):
            with self.subTest(url=url), self.assertRaises(RuntimeError):
                prepare.loopback_database_parts(url)

    def test_private_atomic_env_write(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / ".env.platform"
            path.write_text("OLD=value\n", encoding="utf-8")
            path.chmod(0o600)
            prepare.atomic_write_private(path, "NEW=value\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "NEW=value\n")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.stat().st_uid, os.geteuid())


if __name__ == "__main__":
    unittest.main()
