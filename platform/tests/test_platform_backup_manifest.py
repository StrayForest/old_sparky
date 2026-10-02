from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "platform" / "tools" / "platform_backup_manifest.py"
SPEC = importlib.util.spec_from_file_location("platform_backup_manifest", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
manifest = importlib.util.module_from_spec(SPEC)

sys.modules[SPEC.name] = manifest
SPEC.loader.exec_module(manifest)


def _payload(*, run_id: str = "d" * 32) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as temporary_dir:
        dump_name = f"platformdb-20261001T120000Z-{run_id}.dump"
        dump = Path(temporary_dir) / dump_name
        dump.write_bytes(b"PGDMP manifest fixture")
        timestamp = dt.datetime(2026, 10, 1, 12, tzinfo=dt.UTC)
        return manifest.build_manifest(
            run_id=run_id,
            dump_file=dump_name,
            size_bytes=dump.stat().st_size,
            sha256=hashlib.sha256(dump.read_bytes()).hexdigest(),
            started_at_utc=timestamp,
            completed_at_utc=timestamp,
            duration_seconds=0,
            restore_verified=True,
            alembic_revision_verified=True,
            restored_table_count=10,
            restore_error=None,
        )


class PlatformBackupManifestTests(unittest.TestCase):
    def test_valid_v3_manifest_models_ordered_production_schemas(self) -> None:
        payload = _payload()

        parsed = manifest.parse_manifest_payload(payload)

        self.assertEqual(parsed.format_version, 3)
        self.assertEqual(parsed.schemas, ("platform", "public"))
        self.assertEqual(parsed.required_extensions, ("pg_trgm",))
        self.assertEqual(parsed.run_id, "d" * 32)

        with self.assertRaisesRegex(manifest.BackupManifestError, "selected archive"):
            manifest.parse_manifest_payload(payload, expected_dump_file="other.dump")

    def test_v2_manifest_is_read_only_legacy_shape(self) -> None:
        payload = _payload()
        legacy = {key: value for key, value in payload.items() if key in manifest.LEGACY_MANIFEST_KEY_SET}
        legacy["format_version"] = 2

        parsed = manifest.parse_manifest_payload(legacy)

        self.assertEqual(parsed.format_version, 2)
        self.assertIsNone(parsed.cleanup_status)
        with tempfile.TemporaryDirectory() as temporary_dir:
            path = Path(temporary_dir) / payload["dump_file"].replace(".dump", ".json")
            with self.assertRaisesRegex(manifest.BackupManifestError, "writer accepts"):
                manifest.write_manifest(path, legacy)

    def test_v3_manifest_requires_explicit_cleanup_fields(self) -> None:
        payload = _payload()
        payload.pop("cleanup_status")

        with self.assertRaisesRegex(manifest.BackupManifestError, "closed"):
            manifest.parse_manifest_payload(payload)

    def test_restore_error_is_a_bounded_allowlisted_code(self) -> None:
        payload = _payload()
        payload["restore_verified"] = False
        payload["alembic_revision_verified"] = False
        payload["restored_table_count"] = None
        payload["restore_error"] = "secret database URL"

        with self.assertRaisesRegex(manifest.BackupManifestError, "allowlisted"):
            manifest.parse_manifest_payload(payload)

    def test_singular_legacy_schema_is_rejected_explicitly(self) -> None:
        payload = _payload()
        payload.pop("schemas")
        payload["schema"] = "platform"

        with self.assertRaisesRegex(manifest.BackupManifestError, "singular"):
            manifest.parse_manifest_payload(payload)

    def test_extra_keys_are_rejected_by_closed_contract(self) -> None:
        payload = {**_payload(), "operator_note": "unexpected"}

        with self.assertRaisesRegex(manifest.BackupManifestError, "closed"):
            manifest.parse_manifest_payload(payload)

    def test_schema_order_is_semantic(self) -> None:
        payload = _payload()
        payload["schemas"] = ["public", "platform"]

        with self.assertRaisesRegex(manifest.BackupManifestError, "ordered"):
            manifest.parse_manifest_payload(payload)

    def test_manifest_types_are_strict_and_boolean_is_not_an_integer(self) -> None:
        payload = _payload()
        payload["format_version"] = True

        with self.assertRaisesRegex(manifest.BackupManifestError, "format_version"):
            manifest.parse_manifest_payload(payload)

    def test_timestamps_use_canonical_utc_shape(self) -> None:
        payload = _payload()
        payload["started_at_utc"] = "2026-10-01 12:00:00Z"

        with self.assertRaisesRegex(manifest.BackupManifestError, "canonical UTC"):
            manifest.parse_manifest_payload(payload)

    def test_duplicate_json_keys_are_rejected(self) -> None:
        raw = json.dumps(_payload()).replace(
            '"format_version": 3,', '"format_version": 3, "format_version": 3,', 1
        )

        with self.assertRaisesRegex(manifest.BackupManifestError, "duplicate"):
            manifest.parse_manifest_bytes(raw.encode("utf-8"))

    def test_atomic_writer_round_trips_protected_manifest_without_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            payload = _payload()
            manifest_path = root / f"{payload['dump_file'][:-5]}.json"

            parsed = manifest.write_manifest(manifest_path, payload)

            self.assertEqual(parsed.run_id, "d" * 32)
            self.assertEqual(manifest_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                manifest.read_manifest_file(
                    manifest_path,
                    expected_owner=os.geteuid(),
                    expected_group=os.getegid(),
                ).manifest,
                parsed,
            )
            self.assertEqual(list(root.glob(".*.tmp")), [])

    def test_partial_metadata_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            path = Path(temporary_dir) / "partial.json"
            path.write_text('{"format_version": 3', encoding="utf-8")
            path.chmod(0o600)

            with self.assertRaises(manifest.BackupManifestError):
                manifest.read_manifest_file(
                    path,
                    expected_owner=os.geteuid(),
                    expected_group=os.getegid(),
                )

    def test_symlink_and_hardlink_metadata_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            payload = _payload()
            target = root / "target.json"
            target.write_text(json.dumps(payload), encoding="utf-8")
            target.chmod(0o600)
            symlink = root / "platformdb-link.json"
            symlink.symlink_to(target)
            with self.assertRaisesRegex(manifest.BackupManifestError, "symlink"):
                manifest.read_manifest_file(
                    symlink,
                    expected_owner=os.geteuid(),
                    expected_group=os.getegid(),
                )

            hardlink = root / "platformdb-hardlink.json"
            os.link(target, hardlink)
            with self.assertRaisesRegex(manifest.BackupManifestError, "hardlink"):
                manifest.read_manifest_file(
                    hardlink,
                    expected_owner=os.geteuid(),
                    expected_group=os.getegid(),
                )


if __name__ == "__main__":
    unittest.main()
