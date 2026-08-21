from __future__ import annotations

from collections import Counter
import re
from typing import Any


ENTITY_PLACEHOLDER_RE = re.compile(r"\[\[ENTITY_\d{4}\]\]")
FACT_PLACEHOLDER_RE = re.compile(r"\[\[FACT_\d{4}\]\]")
# Only immutable numeric values and ability tiers are protected. Units and prose
# stay visible to the model so Russian localization can use Valve-style wording.
FACT_TOKEN_RE = re.compile(
    r"(?:"
    r"(?<![A-Za-z0-9])T\d+(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])[+-]?\d+(?:[.,]\d+)?%?"
    r")",
    re.IGNORECASE,
)
_DECIMAL_RE = re.compile(r"(?<![A-Za-z0-9])([+-]?\d+)\.(\d+)")


def term_pattern(term: str) -> str:
    pieces = [
        re.escape(piece)
        for piece in re.split(r"[\s_\-‐-―]+", term.strip())
        if piece
    ]
    if not pieces:
        return ""
    body = r"[\s_\-‐-―]*".join(pieces)
    if term[0].isalnum():
        body = rf"(?<![A-Za-z0-9]){body}"
    if term[-1].isalnum():
        body = rf"{body}(?![A-Za-z0-9])"
    return body


def catalog_protected_terms(catalog: dict[str, Any]) -> list[str]:
    terms: set[str] = {"Urn", "Unstable Rift"}
    for hero in (catalog.get("heroes") or {}).values():
        if not isinstance(hero, dict):
            continue
        name = str(hero.get("name") or "").strip()
        if name:
            terms.add(name)
        for ability in (hero.get("abilities") or {}).values():
            if isinstance(ability, dict):
                name = str(ability.get("name") or "").strip()
                if name:
                    terms.add(name)
    for item in (catalog.get("items") or {}).values():
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            if name:
                terms.add(name)
    for rank in (catalog.get("ranks") or {}).values():
        if isinstance(rank, dict):
            name = str(rank.get("name") or "").strip()
        else:
            name = str(rank or "").strip()
        if name:
            terms.add(name)
    return sorted(terms, key=lambda value: (-len(value), value.casefold()))


def protect_entities(
    text: str,
    protected_terms: list[str],
) -> tuple[str, dict[str, str]]:
    patterns = [
        pattern
        for term in protected_terms
        if (pattern := term_pattern(term))
    ]
    if not patterns:
        return text, {}
    matcher = re.compile(
        "|".join(f"(?:{pattern})" for pattern in patterns),
        re.IGNORECASE,
    )
    replacements: dict[str, str] = {}

    def replace(match: re.Match[str]) -> str:
        placeholder = f"[[ENTITY_{len(replacements):04d}]]"
        replacements[placeholder] = match.group(0)
        return placeholder

    return matcher.sub(replace, text), replacements


def protect_facts(text: str) -> tuple[str, dict[str, str]]:
    """Hide immutable numeric values and T1/T2/T3-style tiers from the model."""

    replacements: dict[str, str] = {}

    def replace(match: re.Match[str]) -> str:
        placeholder = f"[[FACT_{len(replacements):04d}]]"
        replacements[placeholder] = match.group(0)
        return placeholder

    return FACT_TOKEN_RE.sub(replace, text), replacements


def restore_placeholders(text: str, replacements: dict[str, str]) -> str:
    for placeholder, source in replacements.items():
        text = text.replace(placeholder, source)
    return text


def _canonical_fact(token: str) -> str:
    if token[:1].casefold() == "t":
        return token.casefold()
    return token.replace(",", ".")


def fact_fingerprint(text: str) -> Counter[str]:
    return Counter(_canonical_fact(token) for token in FACT_TOKEN_RE.findall(text))


def validate_prepared_translation(source: str, translated: str) -> None:
    if not translated.strip():
        raise ValueError("OpenAI returned an empty translation.")
    if Counter(ENTITY_PLACEHOLDER_RE.findall(source)) != Counter(
        ENTITY_PLACEHOLDER_RE.findall(translated)
    ):
        raise ValueError("OpenAI changed a protected Deadlock entity placeholder.")
    if Counter(FACT_PLACEHOLDER_RE.findall(source)) != Counter(
        FACT_PLACEHOLDER_RE.findall(translated)
    ):
        raise ValueError("OpenAI changed a protected numeric/tier placeholder.")


def localize_russian_notation(text: str) -> str:
    """Apply deterministic Russian number/unit typography used in Valve notes."""

    text = _DECIMAL_RE.sub(r"\1,\2", text)
    text = re.sub(r"(?<=\d)\s*ms\b", " мс", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=\d)\s*m/s\b", " м/с", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=\d)\s*s\b", " с", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=\d)\s*m\b", " м", text, flags=re.IGNORECASE)
    return text
