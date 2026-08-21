from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath

from python_packages.platform_infra.config import PlatformSettings, get_settings


def object_key_from_upload_url(url: str | None) -> str | None:
    prefix = "/api/v1/uploads/"
    if not url or not url.startswith(prefix):
        return None
    key = url.removeprefix(prefix)
    path = PurePosixPath(key)
    if not key or path.is_absolute() or ".." in path.parts:
        return None
    return str(path)


@dataclass(frozen=True)
class StoredObject:
    content: bytes
    content_type: str


class ObjectStorage:
    def __init__(self, settings: PlatformSettings) -> None:
        self.settings = settings
        self.backend = settings.platform_object_storage_backend.strip().lower()
        if self.backend not in {"local", "r2"}:
            raise RuntimeError("PLATFORM_OBJECT_STORAGE_BACKEND must be 'local' or 'r2'")
        self._client = None
        if self.backend == "r2":
            missing = [
                name
                for name, value in (
                    ("PLATFORM_R2_ENDPOINT_URL", settings.platform_r2_endpoint_url),
                    ("PLATFORM_R2_ACCESS_KEY_ID", settings.platform_r2_access_key_id),
                    ("PLATFORM_R2_SECRET_ACCESS_KEY", settings.platform_r2_secret_access_key),
                    ("PLATFORM_R2_BUCKET_NAME", settings.platform_r2_bucket_name),
                )
                if not value
            ]
            if missing:
                raise RuntimeError(f"Missing R2 configuration: {', '.join(missing)}")

            import boto3

            self._client = boto3.client(
                "s3",
                endpoint_url=settings.platform_r2_endpoint_url,
                aws_access_key_id=settings.platform_r2_access_key_id,
                aws_secret_access_key=settings.platform_r2_secret_access_key,
                region_name="auto",
            )

    def put(self, key: str, content: bytes, content_type: str) -> None:
        if self.backend == "r2":
            self._client.put_object(
                Bucket=self.settings.platform_r2_bucket_name,
                Key=key,
                Body=content,
                ContentType=content_type,
                CacheControl="public, max-age=31536000, immutable",
            )
            return
        path = Path(self.settings.platform_upload_dir) / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def get(self, key: str) -> StoredObject | None:
        if self.backend == "r2":
            from botocore.exceptions import ClientError

            try:
                response = self._client.get_object(
                    Bucket=self.settings.platform_r2_bucket_name,
                    Key=key,
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                    return self._get_local(key)
                raise
            return StoredObject(
                content=response["Body"].read(),
                content_type=response.get("ContentType") or "application/octet-stream",
            )
        return self._get_local(key)

    def _get_local(self, key: str) -> StoredObject | None:
        path = Path(self.settings.platform_upload_dir) / key
        if not path.is_file():
            return None
        return StoredObject(content=path.read_bytes(), content_type="application/octet-stream")

    def delete(self, key: str) -> None:
        if self.backend == "r2":
            self._client.delete_object(Bucket=self.settings.platform_r2_bucket_name, Key=key)
        (Path(self.settings.platform_upload_dir) / key).unlink(missing_ok=True)

    def check_connection(self) -> None:
        if self.backend == "r2":
            self._client.list_objects_v2(
                Bucket=self.settings.platform_r2_bucket_name,
                MaxKeys=1,
            )


@lru_cache(maxsize=1)
def get_object_storage() -> ObjectStorage:
    return ObjectStorage(get_settings())
