from __future__ import annotations

import unittest

from apps.platform_api.app.services import patch_translation as translation


class PatchTranslationNumericFactPolicyTests(unittest.TestCase):
    def test_operators_are_not_protected_facts(self) -> None:
        source = "Mystic VI ->Ritualist I requires 2000 points."
        protected, replacements = translation.protect_facts(source)
        self.assertNotIn("->", replacements.values())
        self.assertIn("->", protected)
        self.assertEqual(list(replacements.values()), ["2000"])

    def test_units_remain_visible_for_russian_localization(self) -> None:
        source = "Falloff reduced from 20m->58m to 18m→54m; cooldown 8s."
        protected, replacements = translation.protect_facts(source)
        self.assertEqual(list(replacements.values()), ["20", "58", "18", "54", "8"])
        self.assertIn("m->", protected)
        self.assertIn("m→", protected)
        self.assertIn("s.", protected)

    def test_numeric_values_survive_russian_notation(self) -> None:
        source = "Cooldown reduced from 9.5s to 8s; range 20m."
        localized = translation.localize_russian_notation(source)
        self.assertEqual(
            translation._fact_fingerprint(source),
            translation._fact_fingerprint(localized),
        )
        self.assertIn("9,5 с", localized)
        self.assertIn("20 м", localized)


if __name__ == "__main__":
    unittest.main()
