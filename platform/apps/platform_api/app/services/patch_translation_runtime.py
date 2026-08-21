from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
import logging
import re
from secrets import token_urlsafe
from typing import Any

import httpx

from apps.platform_api.app.services.patch_translation_config import (
    OPENAI_RESPONSES_URL,
    PATCH_TRANSLATION_CACHE_TTL_SECONDS,
    PATCH_TRANSLATION_LOCALE,
    PATCH_TRANSLATION_LOCK_TTL_SECONDS,
    PATCH_TRANSLATION_TASK_NAME,
    PATCH_TRANSLATION_VERSION,
)
from apps.platform_api.app.services.patch_translation_glossary import (
    ENTITY_MECHANIC_COLLISIONS,
    Glossary,
    get_translation_glossary,
)
from apps.platform_api.app.services.patch_translation_terms import (
    catalog_protected_terms,
    fact_fingerprint,
    localize_russian_notation,
    protect_entities,
    protect_facts,
    restore_placeholders,
    validate_prepared_translation,
)
from python_packages.platform_infra.config import PlatformSettings, get_settings
from python_packages.platform_infra.redis import redis_client


logger = logging.getLogger(__name__)

_IMAGE_URL_RE = re.compile(
    r"^https://clan\.fastly\.steamstatic\.com/images/\d+/"
    r"[a-f0-9]{32,64}\.(?:avif|gif|jpe?g|png|webp)$",
    re.IGNORECASE,
)
_CACHE_PREFIX = (
    f"platform:patch-translation:{PATCH_TRANSLATION_VERSION}:{PATCH_TRANSLATION_LOCALE}:"
)
_LOCK_PREFIX = f"platform:patch-translation:lock:{PATCH_TRANSLATION_VERSION}:"
_PROMPT_CACHE_KEY = (
    f"oldsparky-patch-{PATCH_TRANSLATION_LOCALE}-{PATCH_TRANSLATION_VERSION}"
)
_LOCK_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""


def _iter_changes(patch: dict[str, Any]):
    """Yield only patch-note change strings; entity labels remain untouched."""

    for section_index, section in enumerate(patch.get("sections") or []):
        if not isinstance(section, dict):
            continue
        changes = section.get("changes")
        if isinstance(changes, list):
            for change_index, change in enumerate(changes):
                if (
                    isinstance(change, str)
                    and change.strip()
                    and not _IMAGE_URL_RE.fullmatch(change.strip())
                ):
                    yield (
                        f"s{section_index:03d}-c{change_index:03d}",
                        change,
                        ("sections", section_index, "changes", change_index),
                    )
        abilities = section.get("abilities")
        if not isinstance(abilities, list):
            continue
        for ability_index, ability in enumerate(abilities):
            if not isinstance(ability, dict):
                continue
            ability_changes = ability.get("changes")
            if not isinstance(ability_changes, list):
                continue
            for change_index, change in enumerate(ability_changes):
                if (
                    isinstance(change, str)
                    and change.strip()
                    and not _IMAGE_URL_RE.fullmatch(change.strip())
                ):
                    yield (
                        f"s{section_index:03d}-a{ability_index:03d}-c{change_index:03d}",
                        change,
                        (
                            "sections",
                            section_index,
                            "abilities",
                            ability_index,
                            "changes",
                            change_index,
                        ),
                    )


def extract_translation_segments(patch: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"id": segment_id, "text": text}
        for segment_id, text, _path in _iter_changes(patch)
    ]


def _segment_contexts(patch: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Return read-only labels that help interpret terse change text."""

    contexts: dict[str, dict[str, str]] = {}
    for section_index, section in enumerate(patch.get("sections") or []):
        if not isinstance(section, dict):
            continue
        section_context = {
            key: value
            for key, raw_value in (
                ("section_kind", section.get("kind")),
                ("section_title", section.get("title")),
                ("hero_name", section.get("hero_name")),
                ("item_name", section.get("item_name")),
            )
            if (value := str(raw_value or "").strip())
        }
        changes = section.get("changes")
        if isinstance(changes, list):
            for change_index, change in enumerate(changes):
                if (
                    isinstance(change, str)
                    and change.strip()
                    and not _IMAGE_URL_RE.fullmatch(change.strip())
                ):
                    contexts[f"s{section_index:03d}-c{change_index:03d}"] = dict(
                        section_context
                    )
        abilities = section.get("abilities")
        if not isinstance(abilities, list):
            continue
        for ability_index, ability in enumerate(abilities):
            if not isinstance(ability, dict):
                continue
            ability_context = dict(section_context)
            ability_name = str(ability.get("name") or "").strip()
            if ability_name:
                ability_context["ability_name"] = ability_name
            ability_changes = ability.get("changes")
            if not isinstance(ability_changes, list):
                continue
            for change_index, change in enumerate(ability_changes):
                if (
                    isinstance(change, str)
                    and change.strip()
                    and not _IMAGE_URL_RE.fullmatch(change.strip())
                ):
                    contexts[
                        f"s{section_index:03d}-a{ability_index:03d}-c{change_index:03d}"
                    ] = dict(ability_context)
    return contexts


def _source_hash(segments: list[dict[str, str]]) -> str:
    return hashlib.sha256(
        json.dumps(
            segments,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _model_token(model: str) -> str:
    token = re.sub(r"[^A-Za-z0-9._-]+", "-", model.strip()).strip("-")
    return token[:80] or "default"


def _cache_key(patch_id: str, source_hash: str, model: str) -> str:
    return f"{_CACHE_PREFIX}{_model_token(model)}:{patch_id}:{source_hash}"


def _lock_key(patch_id: str, source_hash: str, model: str) -> str:
    return f"{_LOCK_PREFIX}{_model_token(model)}:{patch_id}:{source_hash}"


def _set_path(target: dict[str, Any], path: tuple[Any, ...], value: str) -> None:
    current: Any = target
    for part in path[:-1]:
        current = current[part]
    current[path[-1]] = value


def merge_translation(
    patch: dict[str, Any],
    translated_by_id: dict[str, str],
) -> dict[str, Any]:
    merged = deepcopy(patch)
    for segment_id, _text, path in _iter_changes(patch):
        translated = translated_by_id.get(segment_id)
        if translated is not None:
            _set_path(merged, path, translated)
    return merged


def _output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "segments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "text": {"type": "string"},
                    },
                    "required": ["id", "text"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["segments"],
        "additionalProperties": False,
    }


def _response_output_text(payload: dict[str, Any]) -> str:
    chunks: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if (
                isinstance(part, dict)
                and part.get("type") == "output_text"
                and isinstance(part.get("text"), str)
            ):
                chunks.append(part["text"])
    return "".join(chunks).strip()


def _glossary_payload(glossary: Glossary) -> dict[str, list[str]]:
    """Keep the complete glossary as one compact canonical concept map."""

    return {
        source: list(targets)
        for source, targets in sorted(
            glossary.items(),
            key=lambda pair: pair[0].casefold(),
        )
        if targets
    }


def _openai_usage(payload: dict[str, Any]) -> tuple[int, int, int]:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return 0, 0, 0
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    details = usage.get("input_tokens_details")
    cached_tokens = (
        int(details.get("cached_tokens") or 0)
        if isinstance(details, dict)
        else 0
    )
    return input_tokens, cached_tokens, output_tokens


async def _request_openai(
    *,
    patch_id: str,
    segments: list[dict[str, Any]],
    glossary: dict[str, list[str]],
    settings: PlatformSettings,
) -> dict[str, str]:
    api_key = (settings.platform_openai_api_key or "").strip()
    if not api_key:
        raise RuntimeError("PLATFORM_OPENAI_API_KEY is not configured.")

    request_payload = {
        "model": settings.platform_openai_model,
        "reasoning": {"effort": "none"},
        "prompt_cache_key": _PROMPT_CACHE_KEY,
        "instructions": (
            "Translate only Valve Deadlock patch-note change descriptions from English "
            "to Russian. Return only segment.id and translated segment.text. Read segment.context "
            "only to understand terse wording; never translate or reproduce context labels unless "
            "the source text itself contains them. Hero names, item names and ability names are "
            "normally protected by ENTITY placeholders and must stay byte-for-byte unchanged. "
            "Keep every segment.id unchanged and in the same order. Do not summarize, shorten, "
            "omit, add or alter gameplay facts. Translate every source sentence and every source "
            "clause, including long prose segments and headings embedded inside a segment. A long "
            "segment must remain complete even when concise Russian wording is possible. FACT "
            "placeholders contain immutable numeric values or T1/T2/T3-style tiers and must stay "
            "byte-for-byte unchanged.\n\n"
            "GLOSSARY POLICY: the attached glossary is the complete authoritative English concept "
            "to official Russian localization map for this task. Apply it semantically, not by "
            "exact string matching. Canonical English keys are concept anchors: spelling, case, "
            "spaces, hyphens, concatenation and normal wording variants still map to the same "
            "concept. For example Move Speed also governs Movement Speed, Movespeed, move-speed "
            "and similar phrasing. If the source expresses a glossary concept, use the Russian "
            "value from that glossary entry; do not leave the English mechanic term untranslated "
            "and do not invent another Russian synonym.\n\n"
            "AMBIGUOUS ITEM/MECHANIC NAMES: entity_mechanic_collisions lists the small set of "
            "English strings that are both real Deadlock item names and gameplay mechanic names. "
            "They are intentionally NOT hidden behind ENTITY placeholders. Decide their meaning "
            "from the source sentence and segment.context. If the text refers to the named item, "
            "preserve that item name in English exactly. If it refers to a numeric/stat mechanic, "
            "translate it using the glossary. Examples of mechanic usage include '+10% Bullet "
            "Lifesteal' and '+60% Bullet, Spirit and Melee Lifesteal'; examples of item usage "
            "include references such as 'the same change for Melee Lifesteal'. Never reinterpret "
            "Lifesteal as Resist/Resistance. Coordinated Bullet/Spirit/Melee Resist wording must "
            "remain resistance terminology.\n\n"
            "STYLE: match concise Valve Russian Steam patch-note language. Prefer natural phrases "
            "such as 'время перезарядки' when describing a cooldown value and describe spirit "
            "scaling/ratio as a coefficient or scaling from 'спиритическая мощь' rather than the "
            "awkward literal phrase 'спиритическое масштабирование'. Units are not protected: "
            "render speed measurements in Valve-style м/с when the source m value represents "
            "movement/sprint/dash speed, and render distance/range/radius values in м. Seconds use "
            "с. Russian sentence structure may differ freely from English. Before returning the "
            "JSON, silently audit every segment once: all source clauses are represented, no "
            "glossary-covered mechanic remains in English unless it is truly an item name, and "
            "Lifesteal/Resist families are not confused."
        ),
        "input": json.dumps(
            {
                "glossary": glossary,
                "entity_mechanic_collisions": list(ENTITY_MECHANIC_COLLISIONS),
                "segments": segments,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "text": {
            "format": {
                "type": "json_schema",
                "name": "deadlock_patch_translation",
                "schema": _output_schema(),
                "strict": True,
            }
        },
        "metadata": {
            "purpose": "deadlock_patch_translation",
            "patch_id": patch_id[:32],
            "translation_version": PATCH_TRANSLATION_VERSION,
        },
    }
    timeout = httpx.Timeout(max(60.0, settings.platform_openai_timeout_seconds))
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            OPENAI_RESPONSES_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=request_payload,
        )
        response.raise_for_status()
        response_payload = response.json()

    input_tokens, cached_tokens, output_tokens = _openai_usage(response_payload)
    logger.info(
        "patch_translation_openai_usage patch_id=%s input_tokens=%s cached_tokens=%s output_tokens=%s cache_key=%s",
        patch_id,
        input_tokens,
        cached_tokens,
        output_tokens,
        _PROMPT_CACHE_KEY,
    )

    output_text = _response_output_text(response_payload)
    if not output_text:
        raise ValueError("OpenAI response did not contain output text.")
    decoded = json.loads(output_text)
    returned = decoded.get("segments") if isinstance(decoded, dict) else None
    if not isinstance(returned, list):
        raise ValueError("OpenAI structured output is missing segments.")

    expected_ids = [str(segment["id"]) for segment in segments]
    source_by_id = {
        str(segment["id"]): str(segment["text"])
        for segment in segments
    }
    returned_ids: list[str] = []
    translated: dict[str, str] = {}
    for segment in returned:
        if not isinstance(segment, dict):
            raise ValueError("OpenAI returned an invalid segment.")
        segment_id = segment.get("id")
        text = segment.get("text")
        if not isinstance(segment_id, str) or not isinstance(text, str):
            raise ValueError("OpenAI returned an invalid segment.")
        if segment_id in translated or segment_id not in source_by_id:
            raise ValueError("OpenAI returned duplicate or unexpected segment ids.")
        validate_prepared_translation(source_by_id[segment_id], text)
        returned_ids.append(segment_id)
        translated[segment_id] = text.strip()
    if returned_ids != expected_ids:
        raise ValueError("OpenAI changed or omitted segment ids.")
    return translated


def _prepare_segments(
    patch: dict[str, Any],
    catalog: dict[str, Any],
) -> tuple[
    list[dict[str, Any]],
    dict[str, dict[str, str]],
    dict[str, dict[str, str]],
]:
    collision_keys = {term.casefold() for term in ENTITY_MECHANIC_COLLISIONS}
    protected_terms = [
        term
        for term in catalog_protected_terms(catalog)
        if term.casefold() not in collision_keys
    ]
    contexts = _segment_contexts(patch)
    prepared: list[dict[str, Any]] = []
    entity_replacements: dict[str, dict[str, str]] = {}
    fact_replacements: dict[str, dict[str, str]] = {}
    for segment in extract_translation_segments(patch):
        fact_protected, fact_map = protect_facts(segment["text"])
        text, entity_map = protect_entities(fact_protected, protected_terms)
        prepared_segment: dict[str, Any] = {
            "id": segment["id"],
            "text": text,
        }
        context = contexts.get(segment["id"])
        if context:
            prepared_segment["context"] = context
        prepared.append(prepared_segment)
        entity_replacements[segment["id"]] = entity_map
        fact_replacements[segment["id"]] = fact_map
    return prepared, entity_replacements, fact_replacements


async def _read_json(cache: Any, key: str) -> dict[str, Any] | None:
    raw = await cache.get(key)
    if not raw:
        return None
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


async def get_cached_patch_translation(
    patch: dict[str, Any],
    *,
    settings: PlatformSettings | None = None,
) -> dict[str, str] | None:
    settings = settings or get_settings()
    segments = extract_translation_segments(patch)
    patch_id = str(patch.get("id") or "").strip()
    if not segments:
        return {}
    if not patch_id:
        return None

    cache = redis_client()
    try:
        cached = await _read_json(
            cache,
            _cache_key(
                patch_id,
                _source_hash(segments),
                settings.platform_openai_model,
            ),
        )
    except Exception:
        logger.warning(
            "patch_translation_cache_read_failed patch_id=%s",
            patch_id,
            exc_info=True,
        )
        return None
    finally:
        await cache.aclose()

    cached_segments = cached.get("segments") if cached else None
    if not isinstance(cached_segments, list):
        return None
    translated: dict[str, str] = {}
    for segment in cached_segments:
        if not isinstance(segment, dict):
            return None
        segment_id = segment.get("id")
        text = segment.get("text")
        if not isinstance(segment_id, str) or not isinstance(text, str):
            return None
        translated[segment_id] = text
    if list(translated) != [segment["id"] for segment in segments]:
        return None
    return translated


async def apply_cached_patch_translation(
    patch: dict[str, Any],
    *,
    settings: PlatformSettings | None = None,
) -> dict[str, Any]:
    translated = await get_cached_patch_translation(patch, settings=settings)
    return patch if translated is None else merge_translation(patch, translated)


async def translate_patch_to_russian(
    patch: dict[str, Any],
    catalog: dict[str, Any],
    *,
    settings: PlatformSettings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    patch_id = str(patch.get("id") or "").strip()
    segments = extract_translation_segments(patch)
    if not patch_id or not segments:
        return {
            "ok": True,
            "status": "skipped",
            "patch_id": patch_id,
            "reason": "no_translatable_segments",
        }

    source_hash = _source_hash(segments)
    cache_key = _cache_key(patch_id, source_hash, settings.platform_openai_model)
    lock_key = _lock_key(patch_id, source_hash, settings.platform_openai_model)
    cache = redis_client()
    lock_token = token_urlsafe(24)
    acquired = False
    try:
        if await _read_json(cache, cache_key) is not None:
            return {
                "ok": True,
                "status": "cached",
                "patch_id": patch_id,
                "source_hash": source_hash,
            }
        if not (settings.platform_openai_api_key or "").strip():
            return {
                "ok": False,
                "status": "skipped",
                "patch_id": patch_id,
                "reason": "openai_not_configured",
            }

        acquired = bool(
            await cache.set(
                lock_key,
                lock_token,
                nx=True,
                ex=PATCH_TRANSLATION_LOCK_TTL_SECONDS,
            )
        )
        if not acquired:
            return {
                "ok": True,
                "status": "locked",
                "patch_id": patch_id,
                "source_hash": source_hash,
            }
        if await _read_json(cache, cache_key) is not None:
            return {
                "ok": True,
                "status": "cached",
                "patch_id": patch_id,
                "source_hash": source_hash,
            }

        prepared, entity_replacements, fact_replacements = _prepare_segments(
            patch,
            catalog,
        )
        glossary = await get_translation_glossary(settings)
        full_glossary = _glossary_payload(glossary)
        translated_prepared = await _request_openai(
            patch_id=patch_id,
            segments=prepared,
            glossary=full_glossary,
            settings=settings,
        )

        translated: dict[str, str] = {}
        for segment_id, text in translated_prepared.items():
            restored = restore_placeholders(
                text,
                entity_replacements.get(segment_id, {}),
            )
            restored = restore_placeholders(
                restored,
                fact_replacements.get(segment_id, {}),
            )
            translated[segment_id] = localize_russian_notation(restored)

        original_by_id = {segment["id"]: segment["text"] for segment in segments}
        for segment_id, text in translated.items():
            if fact_fingerprint(original_by_id[segment_id]) != fact_fingerprint(text):
                raise ValueError("Translation changed a numeric value or ability tier.")

        payload = {
            "locale": PATCH_TRANSLATION_LOCALE,
            "source_hash": source_hash,
            "translation_version": PATCH_TRANSLATION_VERSION,
            "model": settings.platform_openai_model,
            "translated_at": datetime.now(UTC).isoformat(),
            "glossary_term_count": len(full_glossary),
            "segments": [
                {"id": segment["id"], "text": translated[segment["id"]]}
                for segment in segments
            ],
        }
        await cache.set(
            cache_key,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            ex=PATCH_TRANSLATION_CACHE_TTL_SECONDS,
        )
        logger.info(
            "patch_translation_completed patch_id=%s segments=%s glossary_terms=%s model=%s",
            patch_id,
            len(segments),
            len(full_glossary),
            settings.platform_openai_model,
        )
        return {
            "ok": True,
            "status": "translated",
            "patch_id": patch_id,
            "source_hash": source_hash,
            "segments": len(segments),
            "glossary_terms": len(full_glossary),
        }
    except Exception as error:
        logger.warning(
            "patch_translation_failed patch_id=%s error=%s",
            patch_id,
            type(error).__name__,
            exc_info=True,
        )
        return {
            "ok": False,
            "status": "failed",
            "patch_id": patch_id,
            "source_hash": source_hash,
            "error": type(error).__name__,
        }
    finally:
        if acquired:
            try:
                await cache.eval(
                    _LOCK_RELEASE_SCRIPT,
                    1,
                    lock_key,
                    lock_token,
                )
            except Exception:
                logger.warning(
                    "patch_translation_lock_release_failed patch_id=%s",
                    patch_id,
                    exc_info=True,
                )
        await cache.aclose()


__all__ = [
    "PATCH_TRANSLATION_TASK_NAME",
    "apply_cached_patch_translation",
    "extract_translation_segments",
    "get_cached_patch_translation",
    "merge_translation",
    "translate_patch_to_russian",
]
