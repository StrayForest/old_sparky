"""Compatibility facade for the patch translation subsystem."""

from __future__ import annotations

import asyncio
from typing import Any

from apps.platform_api.app.services.patch_translation_config import (
    PATCH_TRANSLATION_LOCALE,
    PATCH_TRANSLATION_TASK_NAME,
    PATCH_TRANSLATION_VERSION,
)
from apps.platform_api.app.services.patch_translation_glossary import (
    CANONICAL_GLOSSARY,
    ENTITY_MECHANIC_COLLISIONS,
    get_translation_glossary,
)
from apps.platform_api.app.services.patch_translation_runtime import (
    apply_cached_patch_translation,
    ensure_patch_translation_records,
    extract_translation_segments,
    get_cached_patch_translation,
    mark_patch_translation_failed,
    merge_translation,
    translation_source_hash,
    translate_patch_to_russian as _translate_patch_to_russian,
)
from apps.platform_api.app.services.patch_translation_terms import (
    catalog_protected_terms as _catalog_protected_terms,
    fact_fingerprint as _fact_fingerprint,
    localize_russian_notation,
    protect_entities,
    protect_facts,
    restore_placeholders,
    term_pattern as _term_pattern,
    validate_prepared_translation as _validate_translation,
)
from python_packages.platform_infra.config import PlatformSettings, get_settings


_OPENAI_MIN_TIMEOUT_SECONDS = 75.0
_RETRYABLE_OPENAI_ERRORS = frozenset(
    {
        "ConnectError",
        "ConnectTimeout",
        "NetworkError",
        "PoolTimeout",
        "ReadError",
        "ReadTimeout",
        "RemoteProtocolError",
        "WriteError",
        "WriteTimeout",
    }
)


async def translate_patch_to_russian(
    patch: dict[str, Any],
    catalog: dict[str, Any],
    *,
    settings: PlatformSettings | None = None,
    expected_source_hash: str | None = None,
) -> dict[str, Any]:
    """Translate once, then retry one transient OpenAI transport failure."""

    settings = settings or get_settings()
    effective_settings = settings.model_copy(
        update={
            "platform_openai_timeout_seconds": max(
                _OPENAI_MIN_TIMEOUT_SECONDS,
                settings.platform_openai_timeout_seconds,
            )
        }
    )
    result = await _translate_patch_to_russian(
        patch,
        catalog,
        settings=effective_settings,
        expected_source_hash=expected_source_hash,
    )
    if result.get("ok") or result.get("error") not in _RETRYABLE_OPENAI_ERRORS:
        return result

    await asyncio.sleep(1.0)
    return await _translate_patch_to_russian(
        patch,
        catalog,
        settings=effective_settings,
        expected_source_hash=expected_source_hash,
        allow_failed_retry=True,
    )


__all__ = [
    "CANONICAL_GLOSSARY",
    "ENTITY_MECHANIC_COLLISIONS",
    "ensure_patch_translation_records",
    "PATCH_TRANSLATION_LOCALE",
    "PATCH_TRANSLATION_TASK_NAME",
    "PATCH_TRANSLATION_VERSION",
    "_catalog_protected_terms",
    "_fact_fingerprint",
    "_term_pattern",
    "_validate_translation",
    "apply_cached_patch_translation",
    "extract_translation_segments",
    "get_cached_patch_translation",
    "get_translation_glossary",
    "localize_russian_notation",
    "mark_patch_translation_failed",
    "merge_translation",
    "protect_entities",
    "protect_facts",
    "restore_placeholders",
    "translation_source_hash",
    "translate_patch_to_russian",
]
