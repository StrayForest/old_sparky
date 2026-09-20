from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools import platform_web_hermetic_browsers as provisioner


class PlatformWebHermeticBrowserProvisioningTests(unittest.TestCase):
    def _fake_download(
        self,
        calls: list[tuple[str, str, int, str]],
        *,
        fail_on: str | None = None,
    ):
        expected_executables = {
            "chromium-1228": "chrome-linux64/chrome",
            "chromium_headless_shell-1228": (
                "chrome-headless-shell-linux64/chrome-headless-shell"
            ),
            "ffmpeg-1011": "ffmpeg-linux",
        }

        def download(
            url: str,
            checksum: str,
            byte_size: int,
            target: Path,
        ) -> None:
            calls.append((url, checksum, byte_size, target.name))
            target.mkdir(parents=True)
            if target.name == fail_on:
                (target / "partial-download").write_bytes(b"partial")
                raise provisioner.guard.GuardError("injected archive failure")
            executable = target / expected_executables[target.name]
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_bytes(b"pinned executable")
            executable.chmod(0o755)
            (target / "INSTALLATION_COMPLETE").touch(mode=0o644)

        return download

    def test_provisioning_promotes_exact_chromium_headless_and_ffmpeg_inventory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            web_root = root / "web"
            web_root.mkdir()
            output = root / "run" / "browsers"
            output.parent.mkdir()
            calls: list[tuple[str, str, int, str]] = []

            with (
                mock.patch.object(
                    provisioner.guard, "_assert_playwright_revision"
                ),
                mock.patch.object(
                    provisioner.guard,
                    "_download_pinned_zip",
                    side_effect=self._fake_download(calls),
                ),
            ):
                provisioner.provision(web_root, output)

            expected = [
                (url, checksum, byte_size, name)
                for name, url, checksum, byte_size in provisioner._selected_archives()
            ]
            self.assertEqual(calls, expected)
            self.assertEqual(
                {entry.name for entry in output.iterdir()},
                set(provisioner.HERMETIC_ARCHIVE_NAMES),
            )
            for relative in provisioner.EXPECTED_EXECUTABLES:
                executable = output / relative
                self.assertTrue(executable.is_file())
                self.assertEqual(executable.stat().st_mode & 0o777, 0o755)
            self.assertEqual(
                [entry.name for entry in output.parent.iterdir()], ["browsers"]
            )

    def test_failed_archive_leaves_no_partial_or_promoted_browser_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            web_root = root / "web"
            web_root.mkdir()
            output = root / "run" / "browsers"
            output.parent.mkdir()
            calls: list[tuple[str, str, int, str]] = []

            with (
                mock.patch.object(
                    provisioner.guard, "_assert_playwright_revision"
                ),
                mock.patch.object(
                    provisioner.guard,
                    "_download_pinned_zip",
                    side_effect=self._fake_download(
                        calls, fail_on="ffmpeg-1011"
                    ),
                ),
                self.assertRaisesRegex(
                    provisioner.guard.GuardError, "injected archive failure"
                ),
            ):
                provisioner.provision(web_root, output)

            self.assertFalse(output.exists())
            self.assertFalse(output.is_symlink())
            self.assertEqual(tuple(output.parent.iterdir()), ())

    def test_concurrent_parent_promotion_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            web_root = root / "web"
            web_root.mkdir()
            output = root / "run" / "browsers"
            output.parent.mkdir()

            with (
                mock.patch.object(
                    provisioner.guard, "_assert_playwright_revision"
                ),
                provisioner._exclusive_output_parent(output.parent),
                self.assertRaisesRegex(
                    provisioner.guard.GuardError, "output parent is busy"
                ),
            ):
                provisioner.provision(web_root, output)


if __name__ == "__main__":
    unittest.main()
