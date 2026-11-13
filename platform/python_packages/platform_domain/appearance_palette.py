from __future__ import annotations

import json
import re
from importlib.resources import files
from typing import Any


_MANIFEST = json.loads(
    files(__package__).joinpath("appearance_palette.json").read_text(encoding="utf-8")
)
DEFAULT_APPEARANCE_COLORS: dict[str, str] = dict(_MANIFEST["defaults"])
APPEARANCE_COLOR_KEYS = frozenset(DEFAULT_APPEARANCE_COLORS)
APPEARANCE_GROUPS: dict[str, tuple[str, ...]] = {
    group: tuple(keys) for group, keys in _MANIFEST["groups"].items()
}
_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}\Z")


def normalize_appearance_colors(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != APPEARANCE_COLOR_KEYS:
        raise ValueError("Appearance colors must contain the supported palette keys.")
    normalized: dict[str, str] = {}
    for key in DEFAULT_APPEARANCE_COLORS:
        color = value[key]
        if not isinstance(color, str) or _HEX_COLOR.fullmatch(color) is None:
            raise ValueError(f"Appearance color {key} must be a six-digit HEX color.")
        normalized[key] = color.upper()
    return normalized


def resolve_appearance_colors(value: Any) -> dict[str, str]:
    colors = normalize_appearance_colors(value)
    resolved = dict(colors)
    pending = dict(_MANIFEST.get("derived", {}))

    def mix(first: str, second: str, amount: float) -> str:
        left = tuple(int(first[index : index + 2], 16) for index in (1, 3, 5))
        right = tuple(int(second[index : index + 2], 16) for index in (1, 3, 5))
        channels = tuple(round(a * (1 - amount) + b * amount) for a, b in zip(left, right, strict=True))
        return "#" + "".join(f"{channel:02X}" for channel in channels)

    def luminance(color: str) -> float:
        channels = tuple(int(color[index : index + 2], 16) / 255 for index in (1, 3, 5))
        linear = tuple(channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4 for channel in channels)
        return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722

    while pending:
        progressed = False
        for key, recipe in tuple(pending.items()):
            parts = recipe.get("mix") if isinstance(recipe, dict) else None
            contrast = recipe.get("contrast") if isinstance(recipe, dict) else None
            if isinstance(parts, list) and len(parts) == 3:
                first, second, amount = parts
                first_color = resolved.get(first, first) if isinstance(first, str) else None
                second_color = resolved.get(second, second) if isinstance(second, str) else None
                if first_color is None or second_color is None or not 0 <= amount <= 1:
                    raise ValueError(f"Invalid appearance derivation for {key}.")
                if first in resolved or _HEX_COLOR.fullmatch(first_color):
                    if second in resolved or _HEX_COLOR.fullmatch(second_color):
                        resolved[key] = mix(first_color, second_color, float(amount))
                        del pending[key]
                        progressed = True
            elif isinstance(contrast, dict):
                against = contrast.get("against")
                light = contrast.get("light")
                dark = contrast.get("dark")
                threshold = contrast.get("threshold")
                color = resolved.get(against) if isinstance(against, str) else None
                if (
                    color is not None
                    and isinstance(light, str)
                    and _HEX_COLOR.fullmatch(light)
                    and isinstance(dark, str)
                    and _HEX_COLOR.fullmatch(dark)
                    and isinstance(threshold, (int, float))
                    and 0 <= threshold <= 1
                ):
                    resolved[key] = light if luminance(color) < threshold else dark
                    del pending[key]
                    progressed = True
            else:
                raise ValueError(f"Invalid appearance derivation for {key}.")
        if not progressed:
            raise ValueError("Appearance derivations contain an unknown or cyclic role.")
    return resolved


def appearance_manifest() -> dict[str, Any]:
    return json.loads(json.dumps(_MANIFEST))
