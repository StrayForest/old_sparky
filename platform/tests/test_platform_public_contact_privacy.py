from __future__ import annotations

from pathlib import Path
import re
import unittest


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = PLATFORM_ROOT / "apps/platform_web"
PUBLIC_SOURCE_ROOTS = (
    WEB_ROOT / "app",
    WEB_ROOT / "components",
    WEB_ROOT / "lib",
    WEB_ROOT / "public",
)
TEXT_SUFFIXES = {".css", ".js", ".json", ".mjs", ".ts", ".tsx", ".txt", ".xml"}
EMAIL_ADDRESS = re.compile(
    r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+",
    re.IGNORECASE,
)


class PlatformPublicContactPrivacyTests(unittest.TestCase):
    def test_deployable_web_source_contains_no_literal_email_address(self) -> None:
        exposed: list[str] = []
        for root in PUBLIC_SOURCE_ROOTS:
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
                    continue
                if EMAIL_ADDRESS.search(path.read_text(encoding="utf-8")):
                    exposed.append(str(path.relative_to(WEB_ROOT)))

        self.assertEqual(exposed, [])

    def test_security_txt_uses_https_support_form(self) -> None:
        security_text = (WEB_ROOT / "public/.well-known/security.txt").read_text(
            encoding="utf-8"
        )

        self.assertIn("Contact: https://old-sparky.com/info#support", security_text)
        self.assertNotIn("mailto:", security_text)
        self.assertIsNone(EMAIL_ADDRESS.search(security_text))


if __name__ == "__main__":
    unittest.main()
