from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from python_packages.platform_infra.media.errors import MediaStorageError


IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, s-maxage=31536000, immutable"


@dataclass(frozen=True)
class StoredObjectMetadata:
    object_key: str
    content_type: str
    byte_size: int
    sha256: str | None
    cache_control: str | None


@runtime_checkable
class MediaStorage(Protocol):
    """The deliberately small normal media-storage surface.

    There is no get/list method: request serialization constructs CDN URLs from
    database metadata and must never call the S3 API.
    """

    def put(
        self,
        object_key: str,
        content: bytes,
        *,
        content_type: str,
        content_sha256: str,
    ) -> None: ...

    def delete(self, object_key: str) -> None: ...

    def delete_many(self, object_keys: list[str] | tuple[str, ...]) -> None: ...

    def head(self, object_key: str) -> StoredObjectMetadata | None: ...


def validate_public_object_key(object_key: str) -> str:
    path = PurePosixPath(object_key)
    if (
        not object_key.startswith("public/")
        or path.is_absolute()
        or ".." in path.parts
        or "//" in object_key
        or len(object_key.encode("utf-8")) > 1024
        or any(ord(character) < 32 or ord(character) == 127 for character in object_key)
    ):
        raise ValueError("Media object keys must be normalized keys below public/")
    return str(path)


class R2Storage:
    def __init__(self, *, client: object, bucket_name: str) -> None:
        if (
            not bucket_name
            or "/" in bucket_name
            or bucket_name != bucket_name.strip()
            or any(
                ord(character) < 33 or ord(character) == 127
                for character in bucket_name
            )
        ):
            raise ValueError("R2 bucket name is invalid")
        self._client = client
        self._bucket_name = bucket_name

    @classmethod
    def from_settings(cls, settings: object) -> "R2Storage":
        endpoint_url = getattr(settings, "platform_r2_endpoint_url", None)
        access_key_id = getattr(settings, "platform_r2_access_key_id", None)
        secret_access_key = getattr(settings, "platform_r2_secret_access_key", None)
        bucket_name = getattr(settings, "platform_r2_bucket_name", None)
        missing = [
            name
            for name, value in (
                ("PLATFORM_R2_ENDPOINT_URL", endpoint_url),
                ("PLATFORM_R2_ACCESS_KEY_ID", access_key_id),
                ("PLATFORM_R2_SECRET_ACCESS_KEY", secret_access_key),
                ("PLATFORM_R2_BUCKET_NAME", bucket_name),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing R2 configuration: {', '.join(missing)}")

        parsed_endpoint = urlsplit(str(endpoint_url))
        if (
            parsed_endpoint.scheme not in {"https", "http"}
            or not parsed_endpoint.netloc
            or parsed_endpoint.path not in {"", "/"}
            or parsed_endpoint.query
            or parsed_endpoint.fragment
        ):
            raise RuntimeError(
                "PLATFORM_R2_ENDPOINT_URL must be an endpoint root without a bucket path"
            )
        if (
            str(getattr(settings, "platform_environment", "development")).lower()
            == "production"
            and parsed_endpoint.scheme != "https"
        ):
            raise RuntimeError("Production R2 endpoint must use HTTPS")

        import boto3
        from botocore.config import Config

        client = boto3.client(
            "s3",
            endpoint_url=str(endpoint_url).rstrip("/"),
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=str(getattr(settings, "platform_r2_region", "auto")),
            config=Config(
                signature_version="s3v4",
                connect_timeout=float(
                    getattr(settings, "platform_r2_connect_timeout_seconds", 3.0)
                ),
                read_timeout=float(
                    getattr(settings, "platform_r2_read_timeout_seconds", 10.0)
                ),
                retries={
                    "mode": "standard",
                    "total_max_attempts": int(
                        getattr(settings, "platform_r2_max_attempts", 4)
                    ),
                },
                s3={"addressing_style": "path"},
            ),
        )
        return cls(client=client, bucket_name=str(bucket_name))

    def put(
        self,
        object_key: str,
        content: bytes,
        *,
        content_type: str,
        content_sha256: str,
    ) -> None:
        key = validate_public_object_key(object_key)
        if content_type != "image/webp":
            raise ValueError("Prepared public media must be image/webp")
        actual_sha256 = sha256(content).hexdigest()
        if actual_sha256 != content_sha256:
            raise ValueError("Media content SHA-256 does not match its metadata")
        try:
            self._client.put_object(
                Bucket=self._bucket_name,
                Key=key,
                Body=content,
                ContentLength=len(content),
                ContentType=content_type,
                CacheControl=IMMUTABLE_CACHE_CONTROL,
                Metadata={"sha256": content_sha256},
            )
        except Exception as exc:
            raise self._storage_error("put", exc) from None

    def delete(self, object_key: str) -> None:
        key = validate_public_object_key(object_key)
        try:
            self._client.delete_object(Bucket=self._bucket_name, Key=key)
        except Exception as exc:
            raise self._storage_error("delete", exc) from None

    def delete_many(self, object_keys: list[str] | tuple[str, ...]) -> None:
        keys = tuple(
            dict.fromkeys(validate_public_object_key(key) for key in object_keys)
        )
        for offset in range(0, len(keys), 1000):
            batch = keys[offset : offset + 1000]
            if not batch:
                continue
            try:
                response = self._client.delete_objects(
                    Bucket=self._bucket_name,
                    Delete={
                        "Objects": [{"Key": key} for key in batch],
                        "Quiet": True,
                    },
                )
            except Exception as exc:
                raise self._storage_error("delete_many", exc) from None
            if response.get("Errors"):
                raise MediaStorageError("delete_many")

    def head(self, object_key: str) -> StoredObjectMetadata | None:
        key = validate_public_object_key(object_key)
        try:
            response = self._client.head_object(Bucket=self._bucket_name, Key=key)
        except Exception as exc:
            if self._is_not_found(exc):
                return None
            raise self._storage_error("head", exc) from None
        metadata = response.get("Metadata") or {}
        return StoredObjectMetadata(
            object_key=key,
            content_type=response.get("ContentType") or "application/octet-stream",
            byte_size=int(response.get("ContentLength") or 0),
            sha256=metadata.get("sha256"),
            cache_control=response.get("CacheControl"),
        )

    @staticmethod
    def _is_not_found(exc: Exception) -> bool:
        response = getattr(exc, "response", None)
        if not isinstance(response, dict):
            return False
        error = response.get("Error") or {}
        return str(error.get("Code")) in {"404", "NoSuchKey", "NotFound"}

    @staticmethod
    def _storage_error(operation: str, exc: Exception) -> MediaStorageError:
        response = getattr(exc, "response", None)
        code = ""
        if isinstance(response, dict):
            code = str((response.get("Error") or {}).get("Code") or "")
        non_retriable = code in {
            "AccessDenied",
            "InvalidAccessKeyId",
            "InvalidArgument",
            "SignatureDoesNotMatch",
        }
        return MediaStorageError(operation, retriable=not non_retriable)
