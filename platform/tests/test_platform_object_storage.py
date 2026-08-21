from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from python_packages.platform_infra.config import PlatformSettings
from python_packages.platform_infra.object_storage import ObjectStorage, object_key_from_upload_url


class ObjectStorageTests(unittest.TestCase):
    def test_local_storage_round_trip(self) -> None:
        with TemporaryDirectory() as directory:
            storage = ObjectStorage(
                PlatformSettings(
                    platform_object_storage_backend="local",
                    platform_upload_dir=Path(directory),
                )
            )
            storage.put("avatars/example.png", b"image", "image/png")

            stored = storage.get("avatars/example.png")
            self.assertIsNotNone(stored)
            self.assertEqual(stored.content, b"image")

            storage.delete("avatars/example.png")
            self.assertIsNone(storage.get("avatars/example.png"))

    def test_upload_url_only_accepts_safe_object_keys(self) -> None:
        self.assertEqual(
            object_key_from_upload_url("/api/v1/uploads/tournament-covers/example.webp"),
            "tournament-covers/example.webp",
        )
        self.assertIsNone(object_key_from_upload_url("/api/v1/uploads/../secret"))
        self.assertIsNone(object_key_from_upload_url("https://example.com/image.webp"))


if __name__ == "__main__":
    unittest.main()
