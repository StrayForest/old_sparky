from __future__ import annotations

from hashlib import sha256
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from python_packages.platform_infra.media.r2_storage import (
    IMMUTABLE_CACHE_CONTROL,
    R2Storage,
    validate_public_object_key,
)


class FakeS3Client:
    def __init__(self) -> None:
        self.put_calls: list[dict] = []
        self.delete_calls: list[dict] = []
        self.delete_many_calls: list[dict] = []
        self.head_response: dict = {
            "ContentType": "image/webp",
            "ContentLength": 12,
            "CacheControl": IMMUTABLE_CACHE_CONTROL,
            "Metadata": {"sha256": "a" * 64},
        }

    def put_object(self, **kwargs: object) -> dict:
        self.put_calls.append(kwargs)
        return {}

    def delete_object(self, **kwargs: object) -> dict:
        self.delete_calls.append(kwargs)
        return {}

    def delete_objects(self, **kwargs: object) -> dict:
        self.delete_many_calls.append(kwargs)
        return {"Errors": []}

    def head_object(self, **_: object) -> dict:
        return self.head_response


class R2StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeS3Client()
        self.storage = R2Storage(client=self.client, bucket_name="oldsparky")

    def test_put_sets_sha256_and_immutable_public_cache_metadata(self) -> None:
        content = b"prepared-webp"
        digest = sha256(content).hexdigest()

        self.storage.put(
            "public/avatars/user/asset/avatar-128.webp",
            content,
            content_type="image/webp",
            content_sha256=digest,
        )

        self.assertEqual(
            self.client.put_calls,
            [
                {
                    "Bucket": "oldsparky",
                    "Key": "public/avatars/user/asset/avatar-128.webp",
                    "Body": content,
                    "ContentLength": len(content),
                    "ContentType": "image/webp",
                    "CacheControl": IMMUTABLE_CACHE_CONTROL,
                    "Metadata": {"sha256": digest},
                }
            ],
        )

    def test_put_rejects_hash_mismatch_and_non_webp_without_s3_call(self) -> None:
        with self.assertRaises(ValueError):
            self.storage.put(
                "public/avatars/user/asset/avatar-128.webp",
                b"content",
                content_type="image/webp",
                content_sha256="0" * 64,
            )
        with self.assertRaises(ValueError):
            self.storage.put(
                "public/avatars/user/asset/avatar-128.jpg",
                b"content",
                content_type="image/jpeg",
                content_sha256=sha256(b"content").hexdigest(),
            )
        self.assertEqual(self.client.put_calls, [])

    def test_keys_are_restricted_to_normalized_public_namespace(self) -> None:
        for unsafe in (
            "private/source.png",
            "/public/avatar.webp",
            "public/../secret",
            "public//avatar.webp",
            "public/avatar.webp\nheader",
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                validate_public_object_key(unsafe)

    def test_delete_many_is_deduplicated_and_bounded_to_s3_batch_limit(self) -> None:
        keys = [f"public/avatars/user/asset/avatar-{index}.webp" for index in range(1001)]
        keys.append(keys[0])

        self.storage.delete_many(keys)

        self.assertEqual(len(self.client.delete_many_calls), 2)
        first_objects = self.client.delete_many_calls[0]["Delete"]["Objects"]
        second_objects = self.client.delete_many_calls[1]["Delete"]["Objects"]
        self.assertEqual(len(first_objects), 1000)
        self.assertEqual(len(second_objects), 1)

    def test_head_returns_database_verification_metadata(self) -> None:
        metadata = self.storage.head("public/avatars/user/asset/avatar-128.webp")

        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertEqual(metadata.sha256, "a" * 64)
        self.assertEqual(metadata.cache_control, IMMUTABLE_CACHE_CONTROL)
        self.assertFalse(hasattr(self.storage, "get"))
        self.assertFalse(hasattr(self.storage, "list"))

    def test_factory_enforces_sigv4_auto_region_timeouts_and_bounded_retries(self) -> None:
        client = object()
        settings = SimpleNamespace(
            platform_environment="production",
            platform_r2_endpoint_url="https://account.r2.cloudflarestorage.com",
            platform_r2_access_key_id="access-key",
            platform_r2_secret_access_key="secret-key",
            platform_r2_bucket_name="oldsparky",
            platform_r2_region="auto",
            platform_r2_connect_timeout_seconds=2.5,
            platform_r2_read_timeout_seconds=8.0,
            platform_r2_max_attempts=3,
        )
        with patch("boto3.client", return_value=client) as build_client:
            storage = R2Storage.from_settings(settings)

        self.assertIs(storage._client, client)
        kwargs = build_client.call_args.kwargs
        self.assertEqual(kwargs["region_name"], "auto")
        config = kwargs["config"]
        self.assertEqual(config.signature_version, "s3v4")
        self.assertEqual(config.connect_timeout, 2.5)
        self.assertEqual(config.read_timeout, 8.0)
        self.assertEqual(config.retries["total_max_attempts"], 3)

    def test_factory_rejects_insecure_production_endpoint(self) -> None:
        settings = SimpleNamespace(
            platform_environment="production",
            platform_r2_endpoint_url="http://account.r2.cloudflarestorage.com",
            platform_r2_access_key_id="access-key",
            platform_r2_secret_access_key="secret-key",
            platform_r2_bucket_name="oldsparky",
        )
        with self.assertRaisesRegex(RuntimeError, "HTTPS"):
            R2Storage.from_settings(settings)


if __name__ == "__main__":
    unittest.main()
