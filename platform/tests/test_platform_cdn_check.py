from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "tools" / "platform_check_cdn.py"
SPEC = importlib.util.spec_from_file_location("platform_check_cdn", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class CdnCheckTests(unittest.TestCase):
    def test_url_must_be_direct_https_cdn_path_without_query(self) -> None:
        MODULE.validate_url("https://cdn.old-sparky.com/public/a.webp", "cdn.old-sparky.com")
        for invalid in (
            "http://cdn.old-sparky.com/public/a.webp",
            "https://old-sparky.com/public/a.webp",
            "https://cdn.old-sparky.com/api/v1/uploads/a.webp",
            "https://cdn.old-sparky.com/public/a.webp?rev=1",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                MODULE.validate_url(invalid, "cdn.old-sparky.com")

    def test_response_rejects_private_cache_and_cookies(self) -> None:
        valid = MODULE.CdnResponse(
            status=200,
            cache_status="HIT",
            age="3",
            cache_control="public, max-age=31536000, immutable",
            content_type="image/webp",
            content_length=10,
            etag='"etag"',
            cf_ray="ray",
            sha256="a" * 64,
            has_set_cookie=False,
            server="cloudflare",
        )
        MODULE.validate_response(valid, expected_sha256="a" * 64)
        with self.assertRaises(RuntimeError):
            MODULE.validate_response(
                MODULE.CdnResponse(**{**valid.__dict__, "cache_control": "private, no-store"})
            )
        with self.assertRaises(RuntimeError):
            MODULE.validate_response(MODULE.CdnResponse(**{**valid.__dict__, "has_set_cookie": True}))


if __name__ == "__main__":
    unittest.main()
