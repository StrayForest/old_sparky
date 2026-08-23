from __future__ import annotations

from importlib.util import find_spec
from io import BytesIO
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from python_packages.platform_infra.media.errors import MediaStateError, MediaValidationError
from python_packages.platform_infra.media.image_processor import ImagePolicy, ImageProcessor
from python_packages.platform_infra.media.source_store import MediaSourceStore


TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
    b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


class MediaSourceStoreTests(unittest.TestCase):
    def test_chunked_stage_is_private_atomic_and_hashes_valid_container(self) -> None:
        asset_id = str(uuid4())
        with TemporaryDirectory() as directory:
            store = MediaSourceStore(Path(directory) / "private", max_input_bytes=1024)
            staged = store.stage(
                asset_id,
                (TINY_PNG[:20], TINY_PNG[20:]),
                declared_mime="image/png",
            )

            self.assertEqual(staged.mime_type, "image/png")
            self.assertEqual(staged.byte_size, len(TINY_PNG))
            self.assertEqual(staged.path.read_bytes(), TINY_PNG)
            self.assertEqual(os.stat(store.root).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(staged.path).st_mode & 0o777, 0o600)
            self.assertEqual(tuple(store.staged_asset_ids(limit=4)), (asset_id,))

    def test_preserves_permissions_of_prepared_shared_staging_directory(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "shared"
            root.mkdir(mode=0o700)
            os.chmod(root, 0o2770)

            MediaSourceStore(root)

            self.assertEqual(os.stat(root).st_mode & 0o7777, 0o2770)

    def test_preserves_permissions_of_prepared_shared_quota_lock(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "shared"
            root.mkdir(mode=0o700)
            os.chmod(root, 0o2770)
            lock_path = root / ".quota.lock"
            lock_path.touch(mode=0o600)
            os.chmod(lock_path, 0o660)

            store = MediaSourceStore(root)
            store.cleanup_stale_temporary_files(older_than_epoch=0, limit=1)

            self.assertEqual(os.stat(lock_path).st_mode & 0o777, 0o660)

    def test_rejects_world_accessible_staging_directory(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "shared"
            root.mkdir(mode=0o700)
            os.chmod(root, 0o755)

            with self.assertRaisesRegex(PermissionError, "world-accessible"):
                MediaSourceStore(root)

    def test_rejects_trailing_polyglot_data_and_removes_temporary_file(self) -> None:
        with TemporaryDirectory() as directory:
            store = MediaSourceStore(Path(directory) / "private", max_input_bytes=1024)
            with self.assertRaisesRegex(MediaValidationError, "trailing data"):
                store.stage(
                    str(uuid4()),
                    (TINY_PNG + b"PK\x03\x04archive",),
                    declared_mime="image/png",
                )
            self.assertEqual(
                [path for path in store.root.iterdir() if path.name != ".quota.lock"],
                [],
            )

    def test_rejects_declared_mismatch_animation_format_and_byte_limit(self) -> None:
        with TemporaryDirectory() as directory:
            store = MediaSourceStore(Path(directory) / "private", max_input_bytes=len(TINY_PNG))
            with self.assertRaises(MediaValidationError) as mismatch:
                store.stage(str(uuid4()), (TINY_PNG,), declared_mime="image/jpeg")
            self.assertEqual(mismatch.exception.code, "media_type_mismatch")
            with self.assertRaises(MediaValidationError) as gif:
                store.stage(str(uuid4()), (b"GIF89a",), declared_mime=None)
            self.assertEqual(gif.exception.code, "unsupported_media_type")
            with self.assertRaises(MediaValidationError) as too_large:
                store.stage(str(uuid4()), (TINY_PNG, b"x"), declared_mime="image/png")
            self.assertEqual(too_large.exception.code, "media_too_large")

    def test_staging_has_cross_process_file_and_byte_quotas(self) -> None:
        with TemporaryDirectory() as directory:
            file_limited = MediaSourceStore(
                Path(directory) / "file-limited",
                max_input_bytes=1024,
                max_staged_bytes=4096,
                max_staged_files=1,
            )
            file_limited.stage(str(uuid4()), (TINY_PNG,), declared_mime="image/png")
            with self.assertRaisesRegex(MediaStateError, "file limit"):
                file_limited.stage(str(uuid4()), (TINY_PNG,), declared_mime="image/png")

            byte_limited = MediaSourceStore(
                Path(directory) / "byte-limited",
                max_input_bytes=80,
                max_staged_bytes=100,
                max_staged_files=10,
            )
            byte_limited.stage(str(uuid4()), (TINY_PNG,), declared_mime="image/png")
            with self.assertRaisesRegex(MediaStateError, "byte limit"):
                byte_limited.stage(str(uuid4()), (TINY_PNG,), declared_mime="image/png")

    def test_stale_crash_temporary_files_are_cleaned_but_recent_ones_are_kept(self) -> None:
        with TemporaryDirectory() as directory:
            store = MediaSourceStore(Path(directory) / "private")
            stale = store.root / ".asset.stale.tmp"
            recent = store.root / ".asset.recent.tmp"
            stale.write_bytes(b"partial")
            recent.write_bytes(b"active")
            os.utime(stale, (100, 100))

            removed = store.cleanup_stale_temporary_files(
                older_than_epoch=200,
                limit=4,
            )

            self.assertEqual(removed, 1)
            self.assertFalse(stale.exists())
            self.assertTrue(recent.exists())


@unittest.skipUnless(find_spec("PIL"), "Pillow is not installed in the current test environment")
class PillowImageProcessorTests(unittest.TestCase):
    def test_avatar_is_decoded_oriented_stripped_and_reencoded_to_exact_webp_sizes(self) -> None:
        from PIL import Image

        owner_id = str(uuid4())
        asset_id = str(uuid4())
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = root / "source.png"
            source = Image.new("RGBA", (640, 320), (255, 0, 0, 128))
            source.save(source_path, format="PNG", pnginfo=None)
            processor = ImageProcessor(ImagePolicy(max_variant_bytes=512 * 1024))

            variants = processor.process(
                source_path,
                purpose="profile_avatar",
                asset_id=asset_id,
                owner_id=owner_id,
            )

            self.assertEqual(
                [(variant.variant_name, variant.width, variant.height) for variant in variants],
                [
                    ("avatar-128", 128, 128),
                    ("avatar-256", 256, 256),
                    ("avatar-512", 512, 512),
                ],
            )
            for variant in variants:
                self.assertTrue(
                    variant.object_key.startswith(
                        f"public/avatars/{owner_id}/{asset_id}/"
                    )
                )
                with Image.open(BytesIO(variant.content)) as decoded:
                    self.assertEqual(decoded.format, "WEBP")
                    self.assertEqual(decoded.size, (variant.width, variant.height))
                    self.assertNotIn("exif", decoded.info)
                    self.assertNotIn("icc_profile", decoded.info)
                    self.assertEqual(getattr(decoded, "n_frames", 1), 1)

    def test_pixel_limit_is_enforced_before_variant_generation(self) -> None:
        from PIL import Image

        with TemporaryDirectory() as directory:
            source_path = Path(directory) / "source.png"
            Image.new("RGB", (10, 10), "red").save(source_path, format="PNG")
            processor = ImageProcessor(ImagePolicy(max_pixels=99))
            with self.assertRaises(MediaValidationError) as raised:
                processor.process(
                    source_path,
                    purpose="profile_avatar",
                    asset_id=str(uuid4()),
                    owner_id=str(uuid4()),
                )
            self.assertEqual(raised.exception.code, "image_pixel_limit")


if __name__ == "__main__":
    unittest.main()
