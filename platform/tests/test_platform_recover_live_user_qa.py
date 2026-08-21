from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path
import sys
import unittest
from unittest import mock


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = PLATFORM_ROOT / "tools"
sys.path.insert(0, str(TOOLS_ROOT))
SCRIPT_PATH = TOOLS_ROOT / "platform_recover_live_user_qa.py"
SPEC = importlib.util.spec_from_file_location(
    "platform_recover_live_user_qa_tested",
    SCRIPT_PATH,
)
assert SPEC is not None and SPEC.loader is not None
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)


class RecoverLiveUserQaTests(unittest.TestCase):
    def test_main_refuses_nonroot_before_database_access(self) -> None:
        argv = [
            str(SCRIPT_PATH),
            "--marker",
            "liveqa-recovery-unit",
            "--inventory",
            "/root/.oldsparky/liveqa/live-user-qa.Abc123/inventory.json",
            "--confirm",
            "recover-live-user-qa",
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(os, "geteuid", return_value=1000),
            mock.patch.object(recovery, "asyncio") as asyncio_module,
            mock.patch.object(sys, "stderr", io.StringIO()),
            self.assertRaises(SystemExit) as refused,
        ):
            recovery.main()
        self.assertEqual(refused.exception.code, 2)
        asyncio_module.run.assert_not_called()

    def test_runtime_validation_requires_exact_production_origin(self) -> None:
        settings = mock.Mock(
            platform_environment="production",
            platform_web_origin="https://lookalike.invalid",
        )
        with (
            mock.patch.object(recovery, "get_settings", return_value=settings),
            mock.patch.object(recovery, "validate_platform_settings"),
            self.assertRaisesRegex(recovery.RecoveryError, "canonical production"),
        ):
            recovery._validate_runtime()


if __name__ == "__main__":
    unittest.main()
