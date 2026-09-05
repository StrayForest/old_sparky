from __future__ import annotations

import unittest
from collections import Counter
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from tests.platform_async_case import PlatformIsolatedAsyncioTestCase
from tools import platform_migrate_legacy_r2 as cutover
from tools import platform_migrate_media as migration


class FakeBody:
    def __init__(self, payload: bytes) -> None:
        self.stream = BytesIO(payload)
        self.closed = False
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self.stream.read(size)

    def close(self) -> None:
        self.closed = True
        self.stream.close()


class FakeR2NotFound(Exception):
    def __init__(self) -> None:
        super().__init__("missing")
        self.response = {"Error": {"Code": "NoSuchKey"}}


class FakeLegacyClient:
    def __init__(self, payload: bytes | None) -> None:
        self.payload = payload
        self.get_calls: list[tuple[str, str]] = []
        self.bodies: list[FakeBody] = []

    def get_object(self, *, Bucket: str, Key: str):
        self.get_calls.append((Bucket, Key))
        if self.payload is None:
            raise FakeR2NotFound()
        body = FakeBody(self.payload)
        self.bodies.append(body)
        return {
            "Body": body,
            "ContentLength": len(self.payload),
        }


class FakeSettings:
    def __init__(self, upload_root: Path) -> None:
        self.platform_upload_dir = upload_root
        self.platform_media_max_input_bytes = 1024 * 1024
        self.platform_r2_bucket_name = "oldsparky"

    def model_copy(self, *, update: dict[str, object]):
        copied = FakeSettings(self.platform_upload_dir)
        copied.platform_media_max_input_bytes = self.platform_media_max_input_bytes
        copied.platform_r2_bucket_name = self.platform_r2_bucket_name
        for key, value in update.items():
            setattr(copied, key, value)
        return copied


class LegacyR2CutoverTests(PlatformIsolatedAsyncioTestCase):
    def candidate(self, key: str = "avatars/legacy.png") -> migration.LegacyMediaCandidate:
        return migration.build_candidate(
            purpose="profile_avatar",
            owner_id=str(uuid4()),
            subject_type="profile",
            legacy_url=f"/api/v1/uploads/{key}",
            active_asset_id=None,
        )

    def test_stage_reads_only_exact_db_referenced_key_in_bounded_chunks(self) -> None:
        payload = b"legacy-object-bytes"
        client = FakeLegacyClient(payload)
        candidate = self.candidate()
        with TemporaryDirectory() as directory:
            staged = cutover.stage_legacy_r2_original(
                client=client,
                bucket_name="oldsparky",
                candidate=candidate,
                destination_root=Path(directory),
                max_bytes=1024,
            )
            self.assertEqual(staged.read_bytes(), payload)
            self.assertEqual(client.get_calls, [("oldsparky", "avatars/legacy.png")])
            self.assertEqual(staged.stat().st_mode & 0o777, 0o600)
            self.assertTrue(client.bodies[0].closed)
            self.assertTrue(client.bodies[0].read_sizes)
            self.assertTrue(
                all(size == cutover.FILE_CHUNK_BYTES for size in client.bodies[0].read_sizes)
            )
            self.assertFalse(hasattr(client, "list_objects_v2"))

    def test_stage_rejects_key_outside_approved_legacy_prefixes(self) -> None:
        candidate = self.candidate()
        candidate = migration.LegacyMediaCandidate(
            identity=candidate.identity,
            cursor=candidate.cursor,
            purpose=candidate.purpose,
            owner_id=candidate.owner_id,
            subject_type=candidate.subject_type,
            legacy_url=candidate.legacy_url,
            active_asset_id=candidate.active_asset_id,
            source_kind=candidate.source_kind,
            source_key="private/secret.png",
            conflict_code=candidate.conflict_code,
        )
        with TemporaryDirectory() as directory:
            with self.assertRaises(migration.MigrationError) as blocked:
                cutover.stage_legacy_r2_original(
                    client=FakeLegacyClient(b"x"),
                    bucket_name="oldsparky",
                    candidate=candidate,
                    destination_root=Path(directory),
                    max_bytes=1024,
                )
        self.assertEqual(blocked.exception.code, "legacy_r2_key_invalid")

    def test_stage_fails_closed_when_declared_object_exceeds_media_bound(self) -> None:
        client = FakeLegacyClient(b"too-large")
        with TemporaryDirectory() as directory:
            with self.assertRaises(migration.MigrationError) as blocked:
                cutover.stage_legacy_r2_original(
                    client=client,
                    bucket_name="oldsparky",
                    candidate=self.candidate(),
                    destination_root=Path(directory),
                    max_bytes=4,
                )
        self.assertEqual(blocked.exception.code, "media_too_large")
        self.assertTrue(client.bodies[0].closed)

    def test_materialize_prefers_r2_when_only_r2_exists(self) -> None:
        payload = b"r2-source"
        with TemporaryDirectory() as directory:
            uploads = Path(directory) / "uploads"
            settings = FakeSettings(uploads)
            with cutover.materialize_legacy_source(
                client=FakeLegacyClient(payload),
                bucket_name="oldsparky",
                candidate=self.candidate(),
                settings=settings,
            ) as selected:
                self.assertEqual(selected.location, "r2")
                self.assertEqual(selected.source_path.read_bytes(), payload)
                self.assertEqual(selected.r2_gets, 1)
                self.assertEqual(selected.local_reads, 0)
                self.assertNotEqual(selected.analysis_settings.platform_upload_dir, uploads)

    def test_materialize_uses_local_only_as_migration_source_when_r2_is_missing(self) -> None:
        payload = b"local-source"
        with TemporaryDirectory() as directory:
            uploads = Path(directory) / "uploads"
            source = uploads / "avatars" / "legacy.png"
            source.parent.mkdir(parents=True)
            source.write_bytes(payload)
            settings = FakeSettings(uploads)
            with cutover.materialize_legacy_source(
                client=FakeLegacyClient(None),
                bucket_name="oldsparky",
                candidate=self.candidate(),
                settings=settings,
            ) as selected:
                self.assertEqual(selected.location, "local")
                self.assertEqual(selected.source_path, source.resolve())
                self.assertEqual(selected.source_path.read_bytes(), payload)
                self.assertEqual(selected.local_reads, 1)
                self.assertIs(selected.analysis_settings, settings)

    def test_materialize_prefers_r2_when_local_duplicate_matches(self) -> None:
        payload = b"same-source"
        with TemporaryDirectory() as directory:
            uploads = Path(directory) / "uploads"
            source = uploads / "avatars" / "legacy.png"
            source.parent.mkdir(parents=True)
            source.write_bytes(payload)
            settings = FakeSettings(uploads)
            with cutover.materialize_legacy_source(
                client=FakeLegacyClient(payload),
                bucket_name="oldsparky",
                candidate=self.candidate(),
                settings=settings,
            ) as selected:
                self.assertEqual(selected.location, "both")
                self.assertNotEqual(selected.source_path, source.resolve())
                self.assertEqual(selected.source_path.read_bytes(), payload)
                self.assertEqual(selected.r2_gets, 1)
                self.assertEqual(selected.local_reads, 1)

    def test_materialize_blocks_when_r2_and_local_originals_differ(self) -> None:
        with TemporaryDirectory() as directory:
            uploads = Path(directory) / "uploads"
            source = uploads / "avatars" / "legacy.png"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"local")
            settings = FakeSettings(uploads)
            with self.assertRaises(migration.MigrationError) as blocked:
                with cutover.materialize_legacy_source(
                    client=FakeLegacyClient(b"r2"),
                    bucket_name="oldsparky",
                    candidate=self.candidate(),
                    settings=settings,
                ):
                    pass
        self.assertEqual(blocked.exception.code, "legacy_source_conflict")

    def test_materialize_blocks_when_both_historical_sources_are_missing(self) -> None:
        with TemporaryDirectory() as directory:
            settings = FakeSettings(Path(directory) / "uploads")
            with self.assertRaises(migration.MigrationError) as blocked:
                with cutover.materialize_legacy_source(
                    client=FakeLegacyClient(None),
                    bucket_name="oldsparky",
                    candidate=self.candidate(),
                    settings=settings,
                ):
                    pass
        self.assertEqual(blocked.exception.code, "legacy_source_missing")

    async def test_check_mode_inspects_sources_without_mutating(self) -> None:
        candidate = self.candidate()
        args = cutover.parse_args(["--max-records", "10"])
        operations = cutover._base_operations()
        operations["r2_gets"] = 1
        inspection = [
            {
                "cursor": candidate.cursor,
                "ok": True,
                "location": "r2",
                "source_bytes": 10,
                "source_sha256": "a" * 64,
            }
        ]
        with (
            patch.object(
                cutover,
                "load_inventory",
                new=AsyncMock(return_value=[candidate]),
            ),
            patch.object(
                cutover,
                "inspect_sources",
                new=AsyncMock(
                    return_value=(inspection, Counter({"r2": 1}), operations)
                ),
            ),
        ):
            report, exit_code = await cutover.run_cutover(
                args,
                settings=object(),
                legacy_client=FakeLegacyClient(b"unused"),
            )
        self.assertEqual(exit_code, 2)
        self.assertFalse(report["ok"])
        self.assertFalse(report["mutated"])
        self.assertEqual(report["code"], "legacy_media_cutover_required")
        self.assertEqual(report["source_locations"], {"r2": 1})
        self.assertEqual(report["operations"]["r2_gets"], 1)
        self.assertEqual(report["operations"]["list_objects"], 0)

    async def test_check_mode_surfaces_source_conflicts_without_mutating(self) -> None:
        candidate = self.candidate()
        args = cutover.parse_args(["--max-records", "10"])
        with (
            patch.object(
                cutover,
                "load_inventory",
                new=AsyncMock(return_value=[candidate]),
            ),
            patch.object(
                cutover,
                "inspect_sources",
                new=AsyncMock(
                    return_value=(
                        [
                            {
                                "cursor": candidate.cursor,
                                "ok": False,
                                "code": "legacy_source_conflict",
                            }
                        ],
                        Counter(),
                        cutover._base_operations(),
                    )
                ),
            ),
        ):
            report, exit_code = await cutover.run_cutover(
                args,
                settings=object(),
                legacy_client=FakeLegacyClient(b"unused"),
            )
        self.assertEqual(exit_code, 2)
        self.assertEqual(report["code"], "legacy_source_conflicts_present")
        self.assertFalse(report["mutated"])

    async def test_check_mode_passes_when_no_legacy_upload_reference_remains(self) -> None:
        args = cutover.parse_args(["--max-records", "10"])
        with patch.object(
            cutover,
            "load_inventory",
            new=AsyncMock(return_value=[]),
        ):
            report, exit_code = await cutover.run_cutover(args, settings=object())
        self.assertEqual(exit_code, 0)
        self.assertTrue(report["ok"])
        self.assertFalse(report["mutated"])
        self.assertEqual(report["code"], "ready")
        self.assertEqual(report["operations"]["r2_gets"], 0)
        self.assertEqual(report["operations"]["list_objects"], 0)


if __name__ == "__main__":
    unittest.main()
