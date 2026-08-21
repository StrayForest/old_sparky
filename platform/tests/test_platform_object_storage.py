from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from python_packages.platform_infra.config import PlatformSettings
from python_packages.platform_infra.object_storage import ObjectStorage, object_key_from_upload_url


class ObjectStorageTests(unittest.TestCase):
    def test_local_storage_supports_migration_write_and_delete_without_runtime_get(self) -> None:
        with TemporaryDirectory() as directory:
            storage = ObjectStorage(
                PlatformSettings(
                    platform_object_storage_backend="local",
                    platform_upload_dir=Path(directory),
                )
            )
            storage.put("avatars/example.png", b"image", "image/png")

            stored_path = Path(directory) / "avatars" / "example.png"
            self.assertEqual(stored_path.read_bytes(), b"image")
            self.assertFalse(hasattr(storage, "get"))

            storage.delete("avatars/example.png")
            self.assertFalse(stored_path.exists())

    def test_upload_url_only_accepts_safe_object_keys_for_legacy_migration(self) -> None:
        self.assertEqual(
            object_key_from_upload_url("/api/v1/uploads/tournament-covers/example.webp"),
            "tournament-covers/example.webp",
        )
        self.assertIsNone(object_key_from_upload_url("/api/v1/uploads/../secret"))
        self.assertIsNone(object_key_from_upload_url("https://example.com/image.webp"))


if __name__ == "__main__":
    unittest.main()
