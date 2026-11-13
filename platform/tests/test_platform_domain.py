from __future__ import annotations

import unittest
import json
from pathlib import Path
import re

from apps.platform_api.app.api.schemas import AdminAppearanceUpdateRequest
from python_packages.platform_domain.appearance_palette import (
    APPEARANCE_COLOR_KEYS,
    APPEARANCE_GROUPS,
    DEFAULT_APPEARANCE_COLORS,
    appearance_manifest,
    normalize_appearance_colors,
    resolve_appearance_colors,
)

from python_packages.platform_domain.deadlock import (
    RegistrationPayload,
    RegistrationValidationError,
    prepare_ready_check_start,
    calculate_player_strength,
    validate_registration_payload,
)


class PlatformDomainTests(unittest.TestCase):
    def test_appearance_manifest_has_three_seeds_and_four_system_colors(self):
        self.assertEqual(APPEARANCE_GROUPS["theme"], ("background", "accent", "foreground"))
        self.assertEqual(APPEARANCE_GROUPS["system"], ("secondary", "warning", "danger", "success"))
        self.assertEqual(APPEARANCE_COLOR_KEYS, set().union(*map(set, APPEARANCE_GROUPS.values())))
        self.assertEqual(
            {key: DEFAULT_APPEARANCE_COLORS[key] for key in ("background", "accent", "foreground")},
            {"background": "#050813", "accent": "#8B5CF6", "foreground": "#F7F8FC"},
        )

    def test_appearance_derived_roles_follow_seeds_but_keep_system_colors(self):
        defaults = resolve_appearance_colors(DEFAULT_APPEARANCE_COLORS)
        changed = {**DEFAULT_APPEARANCE_COLORS, "background": "#490000", "accent": "#BCA678", "foreground": "#F1E9D8"}
        resolved = resolve_appearance_colors(changed)
        self.assertEqual((resolved["background"], resolved["accent"], resolved["foreground"]), ("#490000", "#BCA678", "#F1E9D8"))
        self.assertEqual(resolved["success"], defaults["success"])
        self.assertNotEqual(resolved["surface"], defaults["surface"])
        self.assertNotEqual(resolved["accentBright"], defaults["accentBright"])

    def test_appearance_secondary_text_recipes_remain_readable_in_dark_and_light_themes(self):
        palettes = (
            DEFAULT_APPEARANCE_COLORS,
            {**DEFAULT_APPEARANCE_COLORS, "background": "#F1E9D8", "foreground": "#000000"},
        )
        for editable in palettes:
            with self.subTest(background=editable["background"]):
                resolved = resolve_appearance_colors(editable)
                soft_contrast = _appearance_contrast(resolved["foregroundSoft"], resolved["surfaceStrong"])
                muted_contrast = _appearance_contrast(resolved["foregroundMuted"], resolved["backgroundDeep"])
                self.assertGreaterEqual(soft_contrast, 4.5)
                self.assertGreaterEqual(muted_contrast, 4.5)
                self.assertGreaterEqual(_appearance_contrast(resolved["foregroundMuted"], resolved["background"]), 4.5)
                self.assertGreater(soft_contrast, muted_contrast)

    def test_appearance_on_primary_uses_wcag_linear_luminance(self):
        for accent in ("#2030E0", "#757575", "#767676", "#777777", "#FFD000"):
            resolved = resolve_appearance_colors({**DEFAULT_APPEARANCE_COLORS, "accent": accent})
            self.assertGreaterEqual(_appearance_contrast(resolved["onPrimary"], accent), 4.5)

    def test_appearance_colors_require_exact_supported_hex_values(self):
        normalized = normalize_appearance_colors({key: value.lower() for key, value in DEFAULT_APPEARANCE_COLORS.items()})
        self.assertEqual(normalized, DEFAULT_APPEARANCE_COLORS)
        invalid_values = (
            {**DEFAULT_APPEARANCE_COLORS, "background": "red"},
            {**DEFAULT_APPEARANCE_COLORS, "background": "#12345"},
            {**DEFAULT_APPEARANCE_COLORS, "background": "#112233;body{display:none}"},
            {**DEFAULT_APPEARANCE_COLORS, "extra": "#123456"},
        )
        for invalid in invalid_values:
            with self.subTest(invalid=invalid.get("background")):
                with self.assertRaises(ValueError):
                    normalize_appearance_colors(invalid)

    def test_appearance_update_schema_rejects_css_payload(self):
        payload = {"colors": DEFAULT_APPEARANCE_COLORS, "expected_revision": 1}
        self.assertEqual(AdminAppearanceUpdateRequest.model_validate(payload).colors, DEFAULT_APPEARANCE_COLORS)
        with self.assertRaises(ValueError):
            AdminAppearanceUpdateRequest.model_validate({**payload, "style": "body { color: red; }"})
        with self.assertRaises(ValueError):
            AdminAppearanceUpdateRequest.model_validate({
                **payload,
                "colors": {**DEFAULT_APPEARANCE_COLORS, "background": "#123456;body{}"},
            })

    def test_appearance_manifest_is_shared_machine_readable_role_source(self):
        manifest = appearance_manifest()
        self.assertEqual(manifest["defaults"], DEFAULT_APPEARANCE_COLORS)
        self.assertEqual(set(manifest["derived"]), set(resolve_appearance_colors(DEFAULT_APPEARANCE_COLORS)) - APPEARANCE_COLOR_KEYS)
        root = Path(__file__).resolve().parents[1]
        web_roles = (root / "apps/platform_web/lib/appearance-palette.ts").read_text(encoding="utf-8")
        for role in resolve_appearance_colors(DEFAULT_APPEARANCE_COLORS):
            self.assertIn(role, web_roles)
        self.assertEqual(json.loads((root / "python_packages/platform_domain/appearance_palette.json").read_text(encoding="utf-8")), manifest)

    def test_canonical_theme_tokens_are_not_redeclared_as_css_literals(self):
        root = Path(__file__).resolve().parents[1]
        sources = (
            root / "apps/platform_web/app/globals.css",
            root / "apps/platform_web/app/theme-modern.css",
            root / "apps/platform_web/components/profile/account-identities.css",
            root / "apps/platform_web/components/profile/account-identities.tsx",
        )
        hardcoded_role = re.compile(r"--ui-[a-z-]+\s*:\s*(?:#[\da-f]{3,8}\b|rgba?\(|hsla?\()", re.IGNORECASE)
        for path in sources:
            with self.subTest(path=path.name):
                self.assertIsNone(hardcoded_role.search(path.read_text(encoding="utf-8")))

    def test_strength_bonus_is_smaller_than_subrank_step(self):
        low_hours = calculate_player_strength("Phantom", 2, "0-500")
        high_hours = calculate_player_strength("Phantom", 2, "3000+")
        next_subrank = calculate_player_strength("Phantom", 3, "0-500")

        self.assertGreater(high_hours, low_hours)
        self.assertLess(high_hours - low_hours, next_subrank - low_hours)

    def test_registration_payload_requires_valid_rank(self):
        payload = RegistrationPayload(
            rank="Bad Rank",
            subrank=2,
            playtime="1001-1500",
            roles=["Carry"],
        )

        with self.assertRaises(RegistrationValidationError):
            validate_registration_payload(payload)

    def test_registration_payload_accepts_high_rank_captain_priority(self):
        payload = RegistrationPayload(
            rank="Eternus",
            subrank=2,
            playtime="1001-1500",
            roles=["Carry"],
            captain_priority="yes",
        )

        validate_registration_payload(payload)

    def test_ready_check_start_builder_rejects_empty_user_pool(self):
        decision = prepare_ready_check_start([], has_active_round=False)

        self.assertEqual(decision.status, "empty")
        self.assertFalse(decision.should_create_round)


def _appearance_contrast(first: str, second: str) -> float:
    def luminance(color: str) -> float:
        values = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in values]
        return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722

    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)
