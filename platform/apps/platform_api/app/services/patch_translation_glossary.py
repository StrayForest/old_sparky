from __future__ import annotations

from python_packages.platform_infra.config import PlatformSettings


Glossary = dict[str, list[str]]
CanonicalGlossary = dict[str, tuple[str, ...]]

# Curated from Valve/Steam Russian Deadlock terminology used in patch notes and
# localization. Keep this intentionally small: canonical mechanics only, never
# hero/item/ability names or property-specific combinations.
CANONICAL_GLOSSARY: CanonicalGlossary = {
    "Weapon Damage": ("Урон от оружия",),
    "Bullet Damage": ("Урон от пуль",),
    "Spirit Damage": ("Спиритический урон",),
    "Melee Damage": ("Урон в ближнем бою",),
    "Damage Amplification": ("Увеличение урона",),
    "Damage Output": ("Наносимый урон",),
    "Damage Over Time": ("Периодический урон",),
    "DPS": ("Урон в секунду",),
    "Critical Damage": ("Критический урон",),
    "Fire Rate": ("Скорострельность",),
    "Bullet Velocity": ("Скорость пуль",),
    "Spirit Power": ("Спиритическая мощь",),
    "Spirit Scaling": ("Коэффициент масштабирования от спиритической мощи",),
    "Move Speed": ("Скорость передвижения",),
    "Sprint Speed": ("Скорость бега",),
    "Dash Speed": ("Скорость рывка",),
    "Stamina": ("Выносливость",),
    "Stamina Recovery": ("Восстановление выносливости",),
    "Max Health": ("Максимальное здоровье",),
    "Health Regen": ("Восстановление здоровья",),
    "Healing": ("Лечение",),
    "Healing Reduction": ("Снижение лечения",),
    "Bullet Resist": ("Сопротивляемость пулям",),
    "Spirit Resist": ("Сопротивляемость спиритизму",),
    "Melee Resist": ("Сопротивляемость в ближнем бою",),
    "Debuff Resist": ("Сопротивляемость отрицательным эффектам",),
    "Slow Resist": ("Сопротивляемость замедлению",),
    "Lifesteal": ("Кража здоровья",),
    "Bullet Lifesteal": ("Кража здоровья пулями",),
    "Spirit Lifesteal": ("Кража здоровья спиритизмом",),
    "Melee Lifesteal": ("Кража здоровья в ближнем бою",),
    "Cooldown": ("Перезарядка",),
    "Cooldown Reduction": ("Сокращение перезарядки",),
    "Duration": ("Длительность",),
    "Ability Duration": ("Длительность умений",),
    "Range": ("Дальность",),
    "Cast Range": ("Дальность применения",),
    "Falloff Range": ("Эффективная дальность",),
    "Radius": ("Радиус",),
    "Charge Time": ("Время зарядки",),
    "Recast Time": ("Время повторного применения",),
    "Cast Time": ("Время применения",),
    "Precast Time": ("Время предварительного применения",),
    "Stun Duration": ("Длительность оглушения",),
    "Silence Duration": ("Длительность безмолвия",),
    "Sleep Duration": ("Длительность сна",),
    "Slow Duration": ("Длительность замедления",),
    "Immobilize Duration": ("Длительность обездвиживания",),
    "Petrify Duration": ("Длительность окаменения",),
    "Lift Duration": ("Длительность подъёма",),
    "Bleed": ("Кровотечение",),
    "Burn": ("Горение",),
    "Slow": ("Замедление",),
    "Boon": ("Бонус",),
    "Souls": ("Души",),
}

# These strings are both actual Deadlock item names and gameplay mechanic names.
# They must remain visible to the model so segment context can decide which sense
# is intended. This is a tiny explicit ambiguity list, not a glossary alias table.
ENTITY_MECHANIC_COLLISIONS: tuple[str, ...] = (
    "Bullet Lifesteal",
    "Spirit Lifesteal",
    "Melee Lifesteal",
)


def canonical_glossary() -> Glossary:
    """Return an isolated mutable copy of the checked-in canonical glossary."""

    return {source: list(targets) for source, targets in CANONICAL_GLOSSARY.items()}


async def get_translation_glossary(
    settings: PlatformSettings | None = None,
    *,
    force_refresh: bool = False,
) -> Glossary:
    """Return the checked-in glossary; no network, Redis or runtime discovery."""

    del settings, force_refresh
    return canonical_glossary()


__all__ = [
    "CANONICAL_GLOSSARY",
    "ENTITY_MECHANIC_COLLISIONS",
    "CanonicalGlossary",
    "Glossary",
    "canonical_glossary",
    "get_translation_glossary",
]
