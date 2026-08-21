from __future__ import annotations

import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


_slug_pattern = re.compile(r"[^a-z0-9]+")

_CYRILLIC_TRANSLIT = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "yo",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
    "і": "i",
    "ї": "yi",
    "є": "ye",
    "ґ": "g",
}


def _transliterate_to_ascii(value: str) -> str:
    characters: list[str] = []
    for raw_char in value.strip().lower():
        transliterated = _CYRILLIC_TRANSLIT.get(raw_char)
        if transliterated is not None:
            characters.append(transliterated)
            continue
        if raw_char.isascii():
            characters.append(raw_char)
            continue
        for normalized_char in unicodedata.normalize("NFKD", raw_char):
            if normalized_char.isascii():
                characters.append(normalized_char)
    return "".join(characters)


def slugify(value: str) -> str:
    slug_source = _transliterate_to_ascii(value)
    slug = _slug_pattern.sub("-", slug_source).strip("-")
    return slug or "tournament"


async def unique_slug_from_name(db_session: AsyncSession, model, value: str) -> str:
    base_slug = slugify(value)
    candidate = base_slug
    counter = 2
    while True:
        existing = await db_session.scalar(select(model).where(model.slug == candidate))
        if existing is None:
            return candidate
        candidate = f"{base_slug}-{counter}"
        counter += 1
