from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from time import monotonic
import warnings

from python_packages.platform_infra.media.errors import (
    MediaProcessingError,
    MediaValidationError,
)
from python_packages.platform_infra.media.source_store import inspect_container


@dataclass(frozen=True)
class ImagePolicy:
    max_pixels: int = 25_000_000
    max_dimension: int = 10_000
    max_variant_bytes: int = 512 * 1024
    processing_timeout_seconds: float = 60.0
    webp_quality: int = 82
    webp_min_quality: int = 70
    webp_quality_step: int = 4
    webp_max_attempts: int = 4

    def __post_init__(self) -> None:
        if (
            self.max_pixels <= 0
            or self.max_dimension <= 0
            or self.max_variant_bytes <= 0
        ):
            raise ValueError("Image limits must be positive")
        if self.processing_timeout_seconds <= 0:
            raise ValueError("processing_timeout_seconds must be positive")
        if not 1 <= self.webp_min_quality <= self.webp_quality <= 100:
            raise ValueError("WebP quality range is invalid")
        if self.webp_quality_step <= 0 or self.webp_max_attempts <= 0:
            raise ValueError("WebP quality search must be bounded and positive")


@dataclass(frozen=True)
class VariantSpec:
    name: str
    width: int
    height: int


@dataclass(frozen=True)
class ProcessedVariant:
    variant_name: str
    object_key: str
    mime_type: str
    width: int
    height: int
    content: bytes
    sha256: str

    @property
    def byte_size(self) -> int:
        return len(self.content)


VARIANT_SPECS: dict[str, tuple[VariantSpec, ...]] = {
    "profile_avatar": (
        VariantSpec("avatar-128", 128, 128),
        VariantSpec("avatar-256", 256, 256),
        VariantSpec("avatar-512", 512, 512),
    ),
    "profile_banner": (
        VariantSpec("banner-960", 960, 240),
        VariantSpec("banner-1920", 1920, 480),
    ),
    "tournament_banner": (
        VariantSpec("banner-560", 560, 140),
        VariantSpec("banner-1120", 1120, 280),
    ),
}


class ImageProcessor:
    def __init__(self, policy: ImagePolicy | None = None) -> None:
        self.policy = policy or ImagePolicy()

    def process(
        self,
        source_path: Path,
        *,
        purpose: str,
        asset_id: str,
        owner_id: str,
    ) -> tuple[ProcessedVariant, ...]:
        specs = VARIANT_SPECS.get(purpose)
        if specs is None:
            raise ValueError(f"Unsupported media purpose: {purpose}")
        detected_mime = inspect_container(source_path)
        deadline = monotonic() + self.policy.processing_timeout_seconds
        try:
            from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError, features

            if not features.check("webp"):
                raise MediaProcessingError(
                    "media_processor_unavailable",
                    "Pillow was built without WebP support",
                )

            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(source_path) as candidate:
                    if candidate.format not in {"JPEG", "PNG", "WEBP"}:
                        raise MediaValidationError(
                            "unsupported_media_type",
                            "Only JPEG, PNG and WebP are accepted",
                        )
                    expected_mime = {
                        "JPEG": "image/jpeg",
                        "PNG": "image/png",
                        "WEBP": "image/webp",
                    }[candidate.format]
                    if expected_mime != detected_mime:
                        raise MediaValidationError(
                            "media_type_mismatch",
                            "Decoded image type does not match its container",
                        )
                    if (
                        bool(getattr(candidate, "is_animated", False))
                        or int(getattr(candidate, "n_frames", 1)) != 1
                    ):
                        raise MediaValidationError(
                            "animated_image",
                            "Animated images are not accepted",
                        )
                    self._validate_dimensions(*candidate.size)
                    candidate.verify()

                self._check_deadline(deadline)
                with Image.open(source_path) as decoded:
                    if (
                        bool(getattr(decoded, "is_animated", False))
                        or int(getattr(decoded, "n_frames", 1)) != 1
                    ):
                        raise MediaValidationError(
                            "animated_image",
                            "Animated images are not accepted",
                        )
                    decoded.load()
                    self._validate_dimensions(*decoded.size)
                    oriented = ImageOps.exif_transpose(decoded)
                    normalized = self._to_srgb(oriented, Image, ImageCms)
                    self._check_deadline(deadline)

                variants: list[ProcessedVariant] = []
                for spec in specs:
                    self._check_deadline(deadline)
                    resized = self._cover_resize(
                        normalized, spec.width, spec.height, Image
                    )
                    content = self._encode_webp(resized, deadline=deadline)
                    object_key = media_object_key(
                        purpose=purpose,
                        owner_id=owner_id,
                        asset_id=asset_id,
                        variant_name=spec.name,
                    )
                    variants.append(
                        ProcessedVariant(
                            variant_name=spec.name,
                            object_key=object_key,
                            mime_type="image/webp",
                            width=spec.width,
                            height=spec.height,
                            content=content,
                            sha256=sha256(content).hexdigest(),
                        )
                    )
                return tuple(variants)
        except MediaValidationError:
            raise
        except MediaProcessingError:
            raise
        except ImportError:
            raise MediaProcessingError(
                "media_processor_unavailable",
                "Pillow with WebP support is required for media processing",
            ) from None
        except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
            bomb_error = getattr(Image, "DecompressionBombError", ())
            if bomb_error and isinstance(exc, bomb_error):
                raise MediaValidationError(
                    "image_pixel_limit",
                    "Image exceeds the decoded pixel limit",
                ) from None
            raise MediaValidationError("invalid_image", "Image decode failed") from None
        except Warning:
            raise MediaValidationError(
                "image_pixel_limit",
                "Image exceeds the decoded pixel limit",
            ) from None

    def _validate_dimensions(self, width: int, height: int) -> None:
        if width <= 0 or height <= 0:
            raise MediaValidationError(
                "invalid_dimensions", "Image dimensions are invalid"
            )
        if width > self.policy.max_dimension or height > self.policy.max_dimension:
            raise MediaValidationError(
                "image_dimension_limit",
                "Image width or height exceeds the configured limit",
            )
        if width * height > self.policy.max_pixels:
            raise MediaValidationError(
                "image_pixel_limit",
                "Image exceeds the decoded pixel limit",
            )

    @staticmethod
    def _to_srgb(image: object, image_module: object, image_cms: object) -> object:
        alpha = None
        if "A" in image.getbands() or "transparency" in image.info:
            alpha = image.convert("RGBA").getchannel("A")

        icc_profile = image.info.get("icc_profile")
        if icc_profile:
            try:
                source_profile = image_cms.ImageCmsProfile(BytesIO(icc_profile))
                target_profile = image_cms.createProfile("sRGB")
                color_input = image
                if image.mode not in {"RGB", "CMYK", "LAB"}:
                    color_input = image.convert("RGB")
                color = image_cms.profileToProfile(
                    color_input,
                    source_profile,
                    target_profile,
                    outputMode="RGB",
                )
            except (OSError, TypeError, ValueError):
                raise MediaValidationError(
                    "invalid_color_profile",
                    "Image color profile is invalid",
                ) from None
        else:
            color = image.convert("RGB")

        clean_mode = "RGBA" if alpha is not None else "RGB"
        clean = image_module.new(clean_mode, color.size)
        clean.paste(color)
        if alpha is not None:
            clean.putalpha(alpha)
        return clean

    @staticmethod
    def _cover_resize(
        image: object, width: int, height: int, image_module: object
    ) -> object:
        source_width, source_height = image.size
        target_ratio = width / height
        source_ratio = source_width / source_height
        if source_ratio > target_ratio:
            crop_width = max(1, round(source_height * target_ratio))
            left = (source_width - crop_width) // 2
            crop_box = (left, 0, left + crop_width, source_height)
        else:
            crop_height = max(1, round(source_width / target_ratio))
            top = (source_height - crop_height) // 2
            crop_box = (0, top, source_width, top + crop_height)
        cropped = image.crop(crop_box)
        return cropped.resize((width, height), image_module.Resampling.LANCZOS)

    def _encode_webp(self, image: object, *, deadline: float) -> bytes:
        quality = self.policy.webp_quality
        attempts = 0
        last_size = 0
        while attempts < self.policy.webp_max_attempts:
            self._check_deadline(deadline)
            output = BytesIO()
            image.save(
                output,
                format="WEBP",
                quality=quality,
                method=4,
            )
            content = output.getvalue()
            self._check_deadline(deadline)
            last_size = len(content)
            if last_size <= self.policy.max_variant_bytes:
                return content
            attempts += 1
            if quality <= self.policy.webp_min_quality:
                break
            quality = max(
                self.policy.webp_min_quality, quality - self.policy.webp_quality_step
            )
        raise MediaProcessingError(
            "variant_too_large",
            f"Prepared variant remains above {self.policy.max_variant_bytes} bytes ({last_size})",
        )

    @staticmethod
    def _check_deadline(deadline: float) -> None:
        if monotonic() > deadline:
            raise MediaProcessingError(
                "media_processing_timeout",
                "Media processing exceeded its deadline",
            )


def media_object_key(
    *,
    purpose: str,
    owner_id: str,
    asset_id: str,
    variant_name: str,
) -> str:
    from python_packages.platform_infra.media.source_store import validate_asset_id

    canonical_owner_id = validate_asset_id(owner_id)
    canonical_asset_id = validate_asset_id(asset_id)
    if purpose == "profile_avatar":
        prefix = "avatars"
        expected_prefix = "avatar-"
    elif purpose == "profile_banner":
        prefix = "profile-banners"
        expected_prefix = "banner-"
    elif purpose == "tournament_banner":
        prefix = "tournaments"
        expected_prefix = "banner-"
    else:
        raise ValueError(f"Unsupported media purpose: {purpose}")
    if not variant_name.startswith(expected_prefix) or not all(
        character.isalnum() or character == "-" for character in variant_name
    ):
        raise ValueError("Invalid media variant name")
    return (
        f"public/{prefix}/{canonical_owner_id}/{canonical_asset_id}/{variant_name}.webp"
    )


def expected_object_keys(
    *, purpose: str, owner_id: str, asset_id: str
) -> tuple[str, ...]:
    specs = VARIANT_SPECS.get(purpose)
    if specs is None:
        raise ValueError(f"Unsupported media purpose: {purpose}")
    return tuple(
        media_object_key(
            purpose=purpose,
            owner_id=owner_id,
            asset_id=asset_id,
            variant_name=spec.name,
        )
        for spec in specs
    )
