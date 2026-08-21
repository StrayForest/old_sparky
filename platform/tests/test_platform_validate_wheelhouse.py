from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

from tools import platform_validate_wheelhouse as validator


class PlatformWheelhouseValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.wheelhouse = self.root / "wheelhouse"
        self.wheelhouse.mkdir()
        self.requirements = self.root / "requirements-platform.txt"
        self.requirements.write_text("pip==26.1.2\ndemo==1.0\n")
        self.freeze = self.root / "requirements-platform.freeze.txt"
        self.freeze.write_text("demo==1.0\npip==26.1.2\n")
        self.freeze.chmod(0o444)
        self.lock = self.root / "requirements-platform.lock.txt"
        self.lock.write_bytes(self.freeze.read_bytes())
        self.lock.chmod(0o644)
        self.add_wheel("demo", "1.0")
        self.add_wheel("pip", "26.1.2")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def add_wheel(
        self,
        name: str,
        version: str,
        *,
        metadata: bytes | None = None,
    ) -> Path:
        filename_name = name.replace("-", "_")
        wheel_path = self.wheelhouse / f"{filename_name}-{version}-py3-none-any.whl"
        dist_info = f"{filename_name}-{version}.dist-info"
        with zipfile.ZipFile(
            wheel_path, "w", compression=zipfile.ZIP_DEFLATED
        ) as wheel:
            wheel.writestr(
                f"{dist_info}/METADATA",
                metadata
                or (
                    f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n"
                ).encode(),
            )
            wheel.writestr(
                f"{dist_info}/WHEEL",
                "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n"
                "Tag: py3-none-any\n",
            )
            wheel.writestr(f"{dist_info}/RECORD", "")
        return wheel_path

    def make_manifest(self) -> None:
        validator.create_manifest(
            self.wheelhouse,
            self.requirements,
            self.lock,
            self.freeze,
        )

    def test_create_and_verify_bind_exact_resolved_wheels(self) -> None:
        self.make_manifest()

        validator.verify_manifest(
            self.wheelhouse,
            self.requirements,
            self.lock,
            self.freeze,
        )

        manifest = self.wheelhouse / validator.MANIFEST_NAME
        self.assertEqual(self.wheelhouse.stat().st_mode & 0o777, 0o555)
        self.assertEqual(manifest.stat().st_mode & 0o777, 0o444)
        lines = manifest.read_text().splitlines()
        self.assertEqual(lines, sorted(lines, key=lambda line: line.split("  ", 1)[1]))
        for wheel in self.wheelhouse.glob("*.whl"):
            self.assertEqual(wheel.stat().st_mode & 0o777, 0o444)

    def test_tampered_wheel_is_rejected_by_manifest(self) -> None:
        self.make_manifest()
        wheel = next(self.wheelhouse.glob("demo-*.whl"))
        wheel.write_bytes(wheel.read_bytes() + b"tampered")

        with self.assertRaisesRegex(validator.WheelhouseError, "checksum mismatch"):
            validator.verify_manifest(
                self.wheelhouse,
                self.requirements,
                self.lock,
                self.freeze,
            )

    def test_manifest_must_be_complete_unique_and_canonical(self) -> None:
        self.make_manifest()
        manifest = self.wheelhouse / validator.MANIFEST_NAME
        original = manifest.read_text()
        first = original.splitlines()[0]
        manifest.write_text(f"{original}{first}\n")

        with self.assertRaisesRegex(validator.WheelhouseError, "format is invalid"):
            validator.verify_manifest(
                self.wheelhouse,
                self.requirements,
                self.lock,
                self.freeze,
            )

        manifest.write_text("\n".join(reversed(original.splitlines())) + "\n")
        with self.assertRaisesRegex(
            validator.WheelhouseError, "file set is incomplete"
        ):
            validator.verify_manifest(
                self.wheelhouse,
                self.requirements,
                self.lock,
                self.freeze,
            )

    def test_freeze_and_wheelhouse_must_describe_the_same_packages(self) -> None:
        self.freeze.chmod(0o644)
        self.freeze.write_text("demo==2.0\npip==26.1.2\n")
        self.freeze.chmod(0o444)

        with self.assertRaises(validator.WheelhouseError):
            validator.create_manifest(
                self.wheelhouse,
                self.requirements,
                self.lock,
                self.freeze,
            )

    def test_requirements_must_be_exact_unique_pins_satisfied_by_freeze(self) -> None:
        invalid = (
            "pip>=26.1.2\n",
            "pip==26.1.2\nPIP==26.1.2\n",
            "pip==26.1.2\nmissing==1.0\n",
            "demo==1.0\n",
        )
        for index, content in enumerate(invalid):
            with self.subTest(index=index):
                self.requirements.write_text(content)
                with self.assertRaises(validator.WheelhouseError):
                    validator.create_manifest(
                        self.wheelhouse,
                        self.requirements,
                        self.lock,
                        self.freeze,
                    )

    def test_freeze_must_be_read_only_exact_and_sorted(self) -> None:
        cases = (
            ("pip==26.1.2\ndemo==1.0\n", 0o444),
            ("demo @ https://example.invalid/demo.whl\npip==26.1.2\n", 0o444),
            ("demo==1.0\npip==26.1.2\n", 0o644),
        )
        for index, (content, mode) in enumerate(cases):
            with self.subTest(index=index):
                self.freeze.chmod(0o644)
                self.freeze.write_text(content)
                self.freeze.chmod(mode)
                with self.assertRaises(validator.WheelhouseError):
                    validator.create_manifest(
                        self.wheelhouse,
                        self.requirements,
                        self.lock,
                        self.freeze,
                    )
        self.requirements.write_text("pip==26.1.2\ndemo==1.0\n")

    def test_tracked_lock_must_be_canonical_and_byte_equal_to_freeze(self) -> None:
        self.lock.chmod(0o600)
        with self.assertRaisesRegex(validator.WheelhouseError, "metadata is unsafe"):
            self.make_manifest()

        self.lock.chmod(0o644)
        self.lock.write_text("demo==2.0\npip==26.1.2\n")
        with self.assertRaisesRegex(validator.WheelhouseError, "tracked lock"):
            self.make_manifest()

    def test_wheel_metadata_identity_is_authoritative(self) -> None:
        wheel = next(self.wheelhouse.glob("demo-*.whl"))
        wheel.unlink()
        self.add_wheel(
            "demo",
            "1.0",
            metadata=b"Metadata-Version: 2.1\nName: other\nVersion: 1.0\n\n",
        )

        with self.assertRaisesRegex(validator.WheelhouseError, "exactly match"):
            validator.create_manifest(
                self.wheelhouse,
                self.requirements,
                self.lock,
                self.freeze,
            )

    def test_unexpected_symlink_and_file_count_are_rejected(self) -> None:
        (self.wheelhouse / "unexpected.txt").write_text("x")
        with self.assertRaisesRegex(validator.WheelhouseError, "unexpected entry"):
            validator.create_manifest(
                self.wheelhouse,
                self.requirements,
                self.lock,
                self.freeze,
            )
        (self.wheelhouse / "unexpected.txt").unlink()

        target = next(self.wheelhouse.glob("demo-*.whl"))
        symlink = self.wheelhouse / "linked-1.0-py3-none-any.whl"
        symlink.symlink_to(target)
        with self.assertRaisesRegex(validator.WheelhouseError, "metadata is unsafe"):
            validator.create_manifest(
                self.wheelhouse,
                self.requirements,
                self.lock,
                self.freeze,
            )
        symlink.unlink()

        with mock.patch.object(validator, "MAX_WHEELS", 1):
            with self.assertRaisesRegex(validator.WheelhouseError, "file-count"):
                validator.create_manifest(
                    self.wheelhouse,
                    self.requirements,
                    self.lock,
                    self.freeze,
                )

    def test_manifest_records_are_actual_sha256_values(self) -> None:
        self.make_manifest()
        records = {
            name: digest
            for digest, name in (
                line.split("  ", 1)
                for line in (self.wheelhouse / validator.MANIFEST_NAME)
                .read_text()
                .splitlines()
            )
        }
        for wheel in self.wheelhouse.glob("*.whl"):
            self.assertEqual(
                hashlib.sha256(wheel.read_bytes()).hexdigest(), records[wheel.name]
            )


if __name__ == "__main__":
    unittest.main()
