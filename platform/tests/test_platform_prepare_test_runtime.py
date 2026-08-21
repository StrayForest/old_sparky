from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import unquote, urlsplit

from tools import platform_prepare_test_runtime as prepare


class PlatformPrepareTestRuntimeTests(unittest.TestCase):
    def test_rewrites_only_local_runtime_and_removes_production_integrations(self) -> None:
        lines = [
            "PLATFORM_ENVIRONMENT=development",
            "PLATFORM_DATABASE_URL=postgresql+asyncpg://prod:secret@127.0.0.1:5432/platformdb",
            "PLATFORM_REDIS_URL=redis://127.0.0.1:6379/0",
            "PLATFORM_SECRET_KEY=production-secret",
            "PLATFORM_OBJECT_STORAGE_BACKEND=r2",
            "PLATFORM_R2_SECRET_ACCESS_KEY=must-disappear",
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
        self.assertIn("UNRELATED_SETTING=retained", content)
        self.assertNotIn("must-disappear", content)
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
