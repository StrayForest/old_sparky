from __future__ import annotations

from pathlib import Path
import unittest
from unittest import mock

from tools import platform_validate_wheelhouse as validator


REPO_ROOT = Path(__file__).resolve().parents[2]


class PlatformPipPinContractTests(unittest.TestCase):
    def test_validator_accepts_any_exact_pip_version_from_inputs(self) -> None:
        pins = {"demo": "1.0", "pip": "999.88.77"}
        with mock.patch.object(validator, "_read_pins", return_value=pins):
            self.assertEqual(
                validator._validate_requirements(Path("requirements-platform.txt")),
                pins,
            )
            self.assertEqual(
                validator._validate_lock(Path("requirements-platform.lock.txt")),
                pins,
            )
            self.assertEqual(
                validator._validate_freeze(Path("requirements-platform.freeze.txt")),
                pins,
            )

    def test_validator_still_requires_an_exact_pip_pin(self) -> None:
        with mock.patch.object(
            validator,
            "_read_pins",
            return_value={"demo": "1.0"},
        ):
            with self.assertRaisesRegex(validator.WheelhouseError, "exact pip pin"):
                validator._validate_requirements(Path("requirements-platform.txt"))

    def test_validator_does_not_duplicate_the_pip_version(self) -> None:
        source = Path(validator.__file__).read_text(encoding="utf-8")
        self.assertNotIn("26.1.2", source)
        self.assertNotIn("26.2", source)

    def test_release_installer_uses_the_verified_pip_wheel_without_version_hardcode(
        self,
    ) -> None:
        installer = (
            REPO_ROOT / "platform/tools/platform_release_install.sh"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'PIP_WHEELS=("$RELEASE_DIR"/wheelhouse/pip-*.whl)', installer
        )
        self.assertIn(
            "Release wheelhouse must contain exactly one pinned pip wheel", installer
        )
        self.assertNotIn("pip-26.1.2-", installer)
        self.assertNotIn("pip==26.1.2", installer)
        self.assertNotIn("pip-26.2-", installer)
        self.assertNotIn("pip==26.2", installer)


if __name__ == "__main__":
    unittest.main()
