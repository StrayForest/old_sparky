from __future__ import annotations

from pathlib import Path
import unittest

from apps.platform_api.app.api.schemas import MediaDescriptorResponse, MediaVariantResponse
from apps.platform_api.app.main import create_app
from apps.platform_api.app.services.media import compatibility_media_url


class MediaRuntimeCleanupTests(unittest.TestCase):
    def test_nginx_does_not_publish_legacy_uploads(self) -> None:
        platform_root = Path(__file__).resolve().parents[1]
        nginx_config = (
            platform_root / "deploy" / "nginx" / "deadlock-platform.conf"
        ).read_text(encoding="utf-8")

        self.assertNotIn("location /api/v1/uploads/", nginx_config)

    def test_legacy_upload_route_is_not_registered(self) -> None:
        route_paths = {
            route.path
            for route in create_app().routes
            if isinstance(getattr(route, "path", None), str)
        }
        legacy_paths = sorted(
            path
            for path in route_paths
            if path == "/api/v1/uploads" or path.startswith("/api/v1/uploads/")
        )
        self.assertEqual(legacy_paths, [])

    def test_no_ready_media_returns_no_url(self) -> None:
        self.assertIsNone(
            compatibility_media_url(
                None,
                preferred_variant="avatar-256",
            )
        )

    def test_ready_media_returns_cdn_variant(self) -> None:
        descriptor = MediaDescriptorResponse(
            asset_id="asset-1",
            purpose="profile_avatar",
            status="ready",
            error_code=None,
            variants=[
                MediaVariantResponse(
                    name="avatar-256",
                    width=256,
                    height=256,
                    byte_size=123,
                    url="https://cdn.old-sparky.com/media/avatar.webp",
                )
            ],
        )
        self.assertEqual(
            compatibility_media_url(
                descriptor,
                preferred_variant="avatar-256",
            ),
            "https://cdn.old-sparky.com/media/avatar.webp",
        )


if __name__ == "__main__":
    unittest.main()
