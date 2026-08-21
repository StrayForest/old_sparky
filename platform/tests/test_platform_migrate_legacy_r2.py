from __future__ import annotations

from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from tools import platform_migrate_legacy_r2 as cutover
from tools import platform_migrate_media as migration


class FakeBody:
    def __init__(self, payload: bytes) -> None:
        self.stream = BytesIO(payload)
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        return self.stream.read(size)

    def close(self) -> None:
        self.closed = True
        self.stream.close()


class FakeLegacyClient:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.get_calls: list[tuple[str, str]] = []

    def get_object(self, *, Bucket: str, Key: str):
        self.get_calls.append((Bucket, Key))
        return {
            "Body": FakeBody(self.payload),
            "ContentLength": len(self.payload),
        }


class LegacyR2CutoverTests(unittest.IsolatedAsyncioTestCase):
    def candidate(self, key: str = "avatars/legacy.png") -> migration.LegacyMediaCandidate:
        return migration.build_candidate(
            purpose="profile_avatar",
            owner_id=str(uuid4()),
            subject_type="profile",
            legacy_url=f"/api/v1/uploads/{key}",
            active_asset_id=None,
        )

    def test_stage_reads_only_exact_db_referenced_key_and_never_lists_bucket(self) -> None:
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
        with TemporaryDirectory() as directory:
            with self.assertRaises(migration.MigrationError) as blocked:
                cutover.stage_legacy_r2_original(
                    client=FakeLegacyClient(b"too-large"),
                    bucket_name="oldsparky",
                    candidate=self.candidate(),
                    destination_root=Path(directory),
                    max_bytes=4,
                )
        self.assertEqual(blocked.exception.code, "media_too_large")

    async def test_check_mode_is_non_mutating_and_blocks_when_legacy_refs_remain(self) -> None:
        candidate = self.candidate()
        args = cutover.parse_args(["--max-records", "10"])
        with patch.object(
            cutover,
            "load_inventory",
            new=AsyncMock(return_value=[candidate]),
        ):
            report, exit_code = await cutover.run_cutover(args, settings=object())
        self.assertEqual(exit_code, 2)
        self.assertFalse(report["ok"])
        self.assertFalse(report["mutated"])
        self.assertEqual(report["code"], "legacy_r2_cutover_required")
        self.assertEqual(report["operations"]["list_objects"], 0)

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


if __name__ == "__main__":
    unittest.main()
