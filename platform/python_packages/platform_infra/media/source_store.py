from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
from hashlib import sha256
import mmap
import os
from pathlib import Path
import tempfile
from uuid import UUID
import zlib

from python_packages.platform_infra.media.errors import (
    MediaStateError,
    MediaValidationError,
)


ALLOWED_SOURCE_MIMES = frozenset({"image/jpeg", "image/png", "image/webp"})


@dataclass(frozen=True)
class StagedSource:
    asset_id: str
    path: Path
    mime_type: str
    byte_size: int
    sha256: str


def validate_asset_id(asset_id: str) -> str:
    try:
        parsed = UUID(asset_id)
    except (ValueError, TypeError, AttributeError):
        raise ValueError("asset_id must be a UUID") from None
    canonical = str(parsed)
    if canonical != asset_id.lower():
        raise ValueError("asset_id must use canonical UUID syntax")
    return canonical


def normalize_declared_mime(declared_mime: str | None) -> str | None:
    if declared_mime is None:
        return None
    value = declared_mime.partition(";")[0].strip().lower()
    if value not in ALLOWED_SOURCE_MIMES:
        raise MediaValidationError(
            "unsupported_media_type", "Only JPEG, PNG and WebP are accepted"
        )
    return value


class MediaSourceStore:
    """Durable private staging for source images.

    The directory must live in shared storage outside every public/static path.
    Files are named by server-generated UUIDs and never by user filenames.
    """

    def __init__(
        self,
        root: Path,
        *,
        max_input_bytes: int = 5 * 1024 * 1024,
        max_staged_bytes: int = 512 * 1024 * 1024,
        max_staged_files: int = 256,
    ) -> None:
        if max_input_bytes <= 0 or max_staged_bytes < max_input_bytes:
            raise ValueError("Media source byte limits are invalid")
        if max_staged_files <= 0:
            raise ValueError("max_staged_files must be positive")
        self.root = root
        self.max_input_bytes = max_input_bytes
        self.max_staged_bytes = max_staged_bytes
        self.max_staged_files = max_staged_files
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def path_for(self, asset_id: str) -> Path:
        return self.root / f"{validate_asset_id(asset_id)}.source"

    def stage(
        self,
        asset_id: str,
        chunks: Iterable[bytes],
        *,
        declared_mime: str | None = None,
    ) -> StagedSource:
        with self._quota_lock():
            return self._stage_locked(
                asset_id,
                chunks,
                declared_mime=declared_mime,
            )

    def _stage_locked(
        self,
        asset_id: str,
        chunks: Iterable[bytes],
        *,
        declared_mime: str | None,
    ) -> StagedSource:
        canonical_id = validate_asset_id(asset_id)
        expected_mime = normalize_declared_mime(declared_mime)
        target = self.path_for(canonical_id)
        staged_bytes, staged_files = self._staging_usage()
        if staged_files >= self.max_staged_files:
            raise MediaStateError(
                "media_staging_full",
                "Private media staging has reached its file limit",
            )
        digest = sha256()
        total = 0
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{canonical_id}.",
                suffix=".tmp",
                dir=self.root,
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                os.chmod(temporary.name, 0o600)
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise TypeError("Media upload chunks must be bytes")
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > self.max_input_bytes:
                        raise MediaValidationError(
                            "media_too_large",
                            f"Media source exceeds {self.max_input_bytes} bytes",
                        )
                    if staged_bytes + total > self.max_staged_bytes:
                        raise MediaStateError(
                            "media_staging_full",
                            "Private media staging has reached its byte limit",
                        )
                    temporary.write(chunk)
                    digest.update(chunk)
                if total == 0:
                    raise MediaValidationError("empty_media", "Media source is empty")
                temporary.flush()
                os.fsync(temporary.fileno())

            assert temporary_path is not None
            detected_mime = inspect_container(temporary_path)
            if expected_mime is not None and detected_mime != expected_mime:
                raise MediaValidationError(
                    "media_type_mismatch",
                    "Declared media type does not match the decoded container",
                )
            if target.exists():
                existing = self.describe(canonical_id)
                if (
                    existing.byte_size == total
                    and existing.sha256 == digest.hexdigest()
                ):
                    temporary_path.unlink(missing_ok=True)
                    return existing
                raise MediaStateError(
                    "staged_source_conflict",
                    "A different source already exists for this media asset",
                )
            os.replace(temporary_path, target)
            temporary_path = None
            self._sync_directory()
            return StagedSource(
                asset_id=canonical_id,
                path=target,
                mime_type=detected_mime,
                byte_size=total,
                sha256=digest.hexdigest(),
            )
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def describe(self, asset_id: str) -> StagedSource:
        canonical_id = validate_asset_id(asset_id)
        path = self.path_for(canonical_id)
        if not path.is_file():
            raise MediaStateError(
                "media_source_missing", "Staged media source is missing"
            )
        digest = sha256()
        total = 0
        with path.open("rb") as source:
            while chunk := source.read(64 * 1024):
                total += len(chunk)
                if total > self.max_input_bytes:
                    raise MediaValidationError(
                        "media_too_large",
                        f"Media source exceeds {self.max_input_bytes} bytes",
                    )
                digest.update(chunk)
        mime_type = inspect_container(path)
        return StagedSource(
            asset_id=canonical_id,
            path=path,
            mime_type=mime_type,
            byte_size=total,
            sha256=digest.hexdigest(),
        )

    def delete(self, asset_id: str) -> None:
        self.path_for(asset_id).unlink(missing_ok=True)

    def staged_asset_ids(self, *, limit: int) -> Iterator[str]:
        if limit <= 0:
            return
        yielded = 0
        with os.scandir(self.root) as entries:
            for entry in entries:
                if yielded >= limit:
                    break
                if not entry.is_file(follow_symlinks=False) or not entry.name.endswith(
                    ".source"
                ):
                    continue
                candidate = entry.name.removesuffix(".source")
                try:
                    canonical = validate_asset_id(candidate)
                except ValueError:
                    continue
                yielded += 1
                yield canonical

    def cleanup_stale_temporary_files(
        self, *, older_than_epoch: float, limit: int
    ) -> int:
        if limit <= 0:
            return 0
        removed = 0
        with self._quota_lock():
            with os.scandir(self.root) as entries:
                for entry in entries:
                    if removed >= limit:
                        break
                    if (
                        not entry.name.startswith(".")
                        or not entry.name.endswith(".tmp")
                        or not entry.is_file(follow_symlinks=False)
                    ):
                        continue
                    if entry.stat(follow_symlinks=False).st_mtime > older_than_epoch:
                        continue
                    Path(entry.path).unlink(missing_ok=True)
                    removed += 1
            if removed:
                self._sync_directory()
        return removed

    def _sync_directory(self) -> None:
        descriptor = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _staging_usage(self) -> tuple[int, int]:
        byte_size = 0
        file_count = 0
        with os.scandir(self.root) as entries:
            for entry in entries:
                if entry.name == ".quota.lock" or not entry.is_file(
                    follow_symlinks=False
                ):
                    continue
                stat = entry.stat(follow_symlinks=False)
                byte_size += stat.st_size
                file_count += 1
        return byte_size, file_count

    @contextmanager
    def _quota_lock(self) -> Iterator[None]:
        lock_path = self.root / ".quota.lock"
        with lock_path.open("a+b") as lock_file:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def inspect_container(path: Path) -> str:
    try:
        size = path.stat().st_size
        if size < 4:
            raise MediaValidationError("invalid_image", "Image container is incomplete")
        with path.open("rb") as source:
            with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as data:
                if data[:8] == b"\x89PNG\r\n\x1a\n":
                    _validate_png(data)
                    return "image/png"
                if data[:2] == b"\xff\xd8":
                    _validate_jpeg(data)
                    return "image/jpeg"
                if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
                    _validate_webp(data)
                    return "image/webp"
    except MediaValidationError:
        raise
    except (OSError, ValueError, zlib.error):
        raise MediaValidationError(
            "invalid_image", "Image container is damaged"
        ) from None
    raise MediaValidationError(
        "unsupported_media_type", "Only JPEG, PNG and WebP are accepted"
    )


def _validate_png(data: mmap.mmap) -> None:
    offset = 8
    seen_ihdr = False
    seen_idat = False
    seen_iend = False
    while offset < len(data):
        if offset + 12 > len(data):
            raise MediaValidationError("invalid_png", "PNG chunk is truncated")
        chunk_length = int.from_bytes(data[offset : offset + 4], "big")
        chunk_type = data[offset + 4 : offset + 8]
        chunk_end = offset + 12 + chunk_length
        if chunk_end > len(data):
            raise MediaValidationError(
                "invalid_png", "PNG chunk exceeds the container boundary"
            )
        if len(chunk_type) != 4 or not all(
            65 <= character <= 90 or 97 <= character <= 122 for character in chunk_type
        ):
            raise MediaValidationError("invalid_png", "PNG chunk type is invalid")
        chunk_data = data[offset + 8 : offset + 8 + chunk_length]
        expected_crc = int.from_bytes(
            data[offset + 8 + chunk_length : chunk_end], "big"
        )
        actual_crc = zlib.crc32(chunk_type)
        actual_crc = zlib.crc32(chunk_data, actual_crc) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise MediaValidationError("invalid_png", "PNG chunk checksum is invalid")
        if not seen_ihdr:
            if chunk_type != b"IHDR" or chunk_length != 13:
                raise MediaValidationError(
                    "invalid_png", "PNG must begin with one IHDR chunk"
                )
            seen_ihdr = True
        elif chunk_type == b"IHDR":
            raise MediaValidationError(
                "invalid_png", "PNG contains multiple IHDR chunks"
            )
        if chunk_type in {b"acTL", b"fcTL", b"fdAT"}:
            raise MediaValidationError(
                "animated_image", "Animated images are not accepted"
            )
        if chunk_type == b"IDAT":
            seen_idat = True
        if chunk_type == b"IEND":
            if chunk_length != 0 or not seen_idat:
                raise MediaValidationError(
                    "invalid_png", "PNG has an invalid IEND chunk"
                )
            seen_iend = True
            offset = chunk_end
            break
        offset = chunk_end
    if not seen_ihdr or not seen_idat or not seen_iend or offset != len(data):
        raise MediaValidationError(
            "invalid_png_boundary",
            "PNG has trailing data or an incomplete container",
        )


def _validate_jpeg(data: mmap.mmap) -> None:
    length = len(data)
    if length < 4 or data[:2] != b"\xff\xd8":
        raise MediaValidationError("invalid_jpeg", "JPEG start marker is missing")
    offset = 2
    in_scan = False
    while offset < length:
        if in_scan:
            marker_start = data.find(b"\xff", offset)
            if marker_start < 0 or marker_start + 1 >= length:
                raise MediaValidationError("invalid_jpeg", "JPEG scan is incomplete")
            marker_offset = marker_start + 1
            while marker_offset < length and data[marker_offset] == 0xFF:
                marker_offset += 1
            if marker_offset >= length:
                raise MediaValidationError("invalid_jpeg", "JPEG marker is incomplete")
            marker = data[marker_offset]
            if marker == 0x00 or 0xD0 <= marker <= 0xD7:
                offset = marker_offset + 1
                continue
            offset = marker_start
            in_scan = False
            continue

        if data[offset] != 0xFF:
            raise MediaValidationError(
                "invalid_jpeg", "JPEG marker boundary is invalid"
            )
        while offset < length and data[offset] == 0xFF:
            offset += 1
        if offset >= length:
            raise MediaValidationError("invalid_jpeg", "JPEG marker is incomplete")
        marker = data[offset]
        offset += 1
        if marker == 0xD9:
            if offset != length:
                raise MediaValidationError(
                    "invalid_jpeg_boundary",
                    "JPEG has data after its end marker",
                )
            return
        if marker in {0x00, 0xD8} or 0xD0 <= marker <= 0xD7:
            raise MediaValidationError(
                "invalid_jpeg", "JPEG contains an invalid marker"
            )
        if offset + 2 > length:
            raise MediaValidationError("invalid_jpeg", "JPEG segment length is missing")
        segment_length = int.from_bytes(data[offset : offset + 2], "big")
        if segment_length < 2 or offset + segment_length > length:
            raise MediaValidationError("invalid_jpeg", "JPEG segment is truncated")
        offset += segment_length
        if marker == 0xDA:
            in_scan = True
    raise MediaValidationError("invalid_jpeg_boundary", "JPEG end marker is missing")


def _validate_webp(data: mmap.mmap) -> None:
    if len(data) < 20 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise MediaValidationError("invalid_webp", "WebP RIFF header is invalid")
    declared_size = int.from_bytes(data[4:8], "little") + 8
    if declared_size != len(data):
        raise MediaValidationError(
            "invalid_webp_boundary",
            "WebP has trailing data or a truncated RIFF container",
        )
    allowed_chunks = {b"VP8 ", b"VP8L", b"VP8X", b"ALPH", b"ICCP", b"EXIF", b"XMP "}
    image_chunks = 0
    offset = 12
    while offset < len(data):
        if offset + 8 > len(data):
            raise MediaValidationError("invalid_webp", "WebP chunk header is truncated")
        chunk_type = data[offset : offset + 4]
        chunk_length = int.from_bytes(data[offset + 4 : offset + 8], "little")
        data_start = offset + 8
        data_end = data_start + chunk_length
        padded_end = data_end + (chunk_length & 1)
        if padded_end > len(data):
            raise MediaValidationError(
                "invalid_webp", "WebP chunk exceeds the RIFF boundary"
            )
        if chunk_type in {b"ANIM", b"ANMF"}:
            raise MediaValidationError(
                "animated_image", "Animated images are not accepted"
            )
        if chunk_type not in allowed_chunks:
            raise MediaValidationError(
                "invalid_webp", "WebP contains an unsupported chunk"
            )
        if chunk_type == b"VP8X":
            if chunk_length != 10:
                raise MediaValidationError("invalid_webp", "WebP VP8X chunk is invalid")
            if data[data_start] & 0x02:
                raise MediaValidationError(
                    "animated_image", "Animated images are not accepted"
                )
        if chunk_type in {b"VP8 ", b"VP8L"}:
            image_chunks += 1
        offset = padded_end
    if offset != len(data) or image_chunks != 1:
        raise MediaValidationError(
            "invalid_webp", "WebP must contain exactly one image bitstream"
        )
