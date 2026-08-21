from __future__ import annotations

import unittest

from python_packages.platform_infra.slugs import slugify


class PlatformSlugTests(unittest.TestCase):
    def test_slugify_transliterates_cyrillic_title(self) -> None:
        self.assertEqual(slugify("Майский турнир #5"), "mayskiy-turnir-5")

    def test_slugify_keeps_ascii_and_collapses_unsafe_characters(self) -> None:
        self.assertEqual(slugify("Night Veil Open #5!"), "night-veil-open-5")


if __name__ == "__main__":
    unittest.main()
