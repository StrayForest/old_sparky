from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import hashlib
import json
import logging
import re
from secrets import token_urlsafe
from typing import Any

import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects import postgresql

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
from python_packages.platform_infra.db import session_factory
from python_packages.platform_infra.models import PatchTranslation, new_uuid
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

TRANSLATION_STATUS_PENDING = "pending"
TRANSLATION_STATUS_PROCESSING = "processing"
TRANSLATION_STATUS_COMPLETED = "completed"
TRANSLATION_STATUS_FAILED = "failed"
TRANSLATION_STATUS_SKIPPED = "skipped"
TRANSLATION_STATUS_SUPERSEDED = "superseded"
TRANSLATION_ENQUEUE_RETRY_SECONDS = 15 * 60
TRANSLATION_PROCESSING_TIMEOUT_SECONDS = 15 * 60


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


def translation_source_hash(patch: dict[str, Any]) -> str:
    return _source_hash(extract_translation_segments(patch))


def _translation_identity(
    patch_id: str,
    source_hash: str,
    settings: PlatformSettings,
) -> dict[str, str]:
    return {
        "patch_id": patch_id,
        "source_hash": source_hash,
        "locale": PATCH_TRANSLATION_LOCALE,
        "translation_version": PATCH_TRANSLATION_VERSION,
        "model": settings.platform_openai_model,
    }


async def _select_translation_record(
    db_session: Any,
    *,
    patch_id: str,
    source_hash: str,
    settings: PlatformSettings,
    for_update: bool = False,
) -> PatchTranslation | None:
    statement = select(PatchTranslation).where(
        PatchTranslation.patch_id == patch_id,
        PatchTranslation.source_hash == source_hash,
        PatchTranslation.locale == PATCH_TRANSLATION_LOCALE,
        PatchTranslation.translation_version == PATCH_TRANSLATION_VERSION,
        PatchTranslation.model == settings.platform_openai_model,
    )
    if for_update:
        statement = statement.with_for_update()
    return await db_session.scalar(statement)


def _validated_translation_segments(
    raw_segments: Any,
    segments: list[dict[str, str]],
) -> dict[str, str] | None:
    if not isinstance(raw_segments, list):
        return None
    expected_ids = [segment["id"] for segment in segments]
    if len(raw_segments) != len(expected_ids):
        return None
    translated: dict[str, str] = {}
    for segment in raw_segments:
        if not isinstance(segment, dict):
            return None
        segment_id = segment.get("id")
        text = segment.get("text")
        if not isinstance(segment_id, str) or not isinstance(text, str):
            return None
        if segment_id in translated:
            return None
        translated[segment_id] = text
    if list(translated) != expected_ids:
        return None
    return translated


def _translation_cache_payload(
    *,
    source_hash: str,
    model: str,
    segments: list[dict[str, str]],
    translated_by_id: dict[str, str],
    translated_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "locale": PATCH_TRANSLATION_LOCALE,
        "source_hash": source_hash,
        "translation_version": PATCH_TRANSLATION_VERSION,
        "model": model,
        "translated_at": (translated_at or datetime.now(UTC)).isoformat(),
        "segments": [
            {"id": segment["id"], "text": translated_by_id[segment["id"]]}
            for segment in segments
        ],
    }


async def _read_database_translation(
    *,
    patch_id: str,
    source_hash: str,
    segments: list[dict[str, str]],
    settings: PlatformSettings,
) -> tuple[dict[str, str], datetime | None] | None:
    try:
        async with session_factory()() as db_session:
            record = await _select_translation_record(
                db_session,
                patch_id=patch_id,
                source_hash=source_hash,
                settings=settings,
            )
            if record is None:
                logger.error(
                    "patch_translation_database_record_missing patch_id=%s source_hash=%s",
                    patch_id,
                    source_hash,
                )
                return None
            if record.status not in {
                TRANSLATION_STATUS_COMPLETED,
                TRANSLATION_STATUS_SKIPPED,
            }:
                logger.info(
                    "patch_translation_database_record_not_ready patch_id=%s source_hash=%s status=%s error=%s",
                    patch_id,
                    source_hash,
                    record.status,
                    record.error_code,
                )
                return None
            raw_segments = record.translated_segments
            translated = _validated_translation_segments(raw_segments, segments)
            if translated is None:
                logger.warning(
                    "patch_translation_database_payload_invalid patch_id=%s source_hash=%s",
                    patch_id,
                    source_hash,
                )
                return None
            return translated, record.translated_at
    except Exception:
        logger.warning(
            "patch_translation_database_read_failed patch_id=%s source_hash=%s",
            patch_id,
            source_hash,
            exc_info=True,
        )
        return None


async def _write_translation_cache(
    *,
    patch_id: str,
    source_hash: str,
    model: str,
    segments: list[dict[str, str]],
    translated_by_id: dict[str, str],
    translated_at: datetime | None = None,
) -> None:
    cache = redis_client()
    try:
        await cache.set(
            _cache_key(patch_id, source_hash, model),
            json.dumps(
                _translation_cache_payload(
                    source_hash=source_hash,
                    model=model,
                    segments=segments,
                    translated_by_id=translated_by_id,
                    translated_at=translated_at,
                ),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            ex=PATCH_TRANSLATION_CACHE_TTL_SECONDS,
        )
    finally:
        await cache.aclose()


async def _persist_translation_completion(
    *,
    patch_id: str,
    source_hash: str,
    segments: list[dict[str, str]],
    translated_by_id: dict[str, str],
    settings: PlatformSettings,
    status: str = TRANSLATION_STATUS_COMPLETED,
) -> None:
    now = datetime.now(UTC)
    translated_segments = [
        {"id": segment["id"], "text": translated_by_id[segment["id"]]}
        for segment in segments
    ]
    values = {
        "id": new_uuid(),
        **_translation_identity(patch_id, source_hash, settings),
        "status": status,
        "translated_segments": translated_segments,
        "error_code": None,
        "translated_at": now,
        "processing_started_at": None,
        "updated_at": now,
    }
    statement = postgresql.insert(PatchTranslation).values(values)
    statement = statement.on_conflict_do_update(
        constraint="uq_patch_translations_identity",
        set_={
            "status": status,
            "translated_segments": translated_segments,
            "error_code": None,
            "processing_started_at": None,
            "translated_at": now,
            "updated_at": now,
        },
    )
    async with session_factory()() as db_session:
        await db_session.execute(statement)
        await db_session.commit()


async def _set_translation_failure(
    *,
    patch_id: str,
    source_hash: str | None,
    error_code: str,
    status: str = TRANSLATION_STATUS_FAILED,
    settings: PlatformSettings | None = None,
) -> bool:
    if not source_hash:
        return False
    settings = settings or get_settings()
    async with session_factory()() as db_session:
        record = await _select_translation_record(
            db_session,
            patch_id=patch_id,
            source_hash=source_hash,
            settings=settings,
            for_update=True,
        )
        if record is None or record.status in {
            TRANSLATION_STATUS_COMPLETED,
            TRANSLATION_STATUS_SKIPPED,
            TRANSLATION_STATUS_SUPERSEDED,
        }:
            return False
        record.status = status
        record.error_code = error_code[:80]
        record.processing_started_at = None
        await db_session.commit()
    return True


async def mark_patch_translation_failed(
    patch_id: str,
    source_hash: str | None,
    error_code: str,
    *,
    settings: PlatformSettings | None = None,
) -> bool:
    return await _set_translation_failure(
        patch_id=patch_id,
        source_hash=source_hash,
        error_code=error_code,
        settings=settings,
    )


async def _claim_translation(
    *,
    patch_id: str,
    source_hash: str,
    settings: PlatformSettings,
    allow_failed_retry: bool = False,
) -> tuple[str, str | None]:
    now = datetime.now(UTC)
    async with session_factory()() as db_session:
        record = await _select_translation_record(
            db_session,
            patch_id=patch_id,
            source_hash=source_hash,
            settings=settings,
            for_update=True,
        )
        if record is None:
            return "not_registered", None
        if record.status in {
            TRANSLATION_STATUS_COMPLETED,
            TRANSLATION_STATUS_SKIPPED,
            TRANSLATION_STATUS_SUPERSEDED,
        }:
            return record.status, record.error_code
        if record.status == TRANSLATION_STATUS_FAILED and not allow_failed_retry:
            return record.status, record.error_code
        if record.status == TRANSLATION_STATUS_PROCESSING:
            started_at = record.processing_started_at
            if started_at is not None and now - started_at < timedelta(
                seconds=TRANSLATION_PROCESSING_TIMEOUT_SECONDS
            ):
                return TRANSLATION_STATUS_PROCESSING, None
        record.status = TRANSLATION_STATUS_PROCESSING
        record.attempts += 1
        record.error_code = None
        record.processing_started_at = now
        await db_session.commit()
    return TRANSLATION_STATUS_PROCESSING, None


async def _mark_translation_superseded(
    *,
    patch_id: str,
    source_hash: str,
    settings: PlatformSettings | None = None,
) -> bool:
    return await _set_translation_failure(
        patch_id=patch_id,
        source_hash=source_hash,
        error_code="source_changed",
        status=TRANSLATION_STATUS_SUPERSEDED,
        settings=settings,
    )


def _enqueue_translation_task(
    patch_id: str,
    source_hash: str,
    *,
    model: str,
) -> str:
    from apps.platform_worker.worker import patch_translation

    task_id = "patch-translation-" + hashlib.sha256(
        f"{PATCH_TRANSLATION_VERSION}:{PATCH_TRANSLATION_LOCALE}:{model}:{patch_id}:{source_hash}".encode(
            "utf-8"
        )
    ).hexdigest()
    task = patch_translation.apply_async(
        args=[patch_id, source_hash],
        task_id=task_id,
    )
    return str(task.id)


async def _clear_translation_enqueue_timestamp(record_id: str) -> None:
    async with session_factory()() as db_session:
        await db_session.execute(
            update(PatchTranslation)
            .where(
                PatchTranslation.id == record_id,
                PatchTranslation.status == TRANSLATION_STATUS_PENDING,
            )
            .values(last_enqueued_at=None, updated_at=datetime.now(UTC))
        )
        await db_session.commit()


async def ensure_patch_translation_records(
    patch_details: dict[str, dict[str, Any]],
) -> dict[str, int]:
    """Register newly observed patch versions and enqueue each at most once."""

    settings = get_settings()
    entries: list[tuple[str, str, list[dict[str, str]]]] = []
    for patch_id, patch in patch_details.items():
        normalized_patch_id = str(patch_id or "").strip()
        if not normalized_patch_id.isdigit() or not isinstance(patch, dict):
            continue
        segments = extract_translation_segments(patch)
        entries.append(
            (
                normalized_patch_id,
                translation_source_hash(patch),
                segments,
            )
        )
    if not entries:
        return {"registered": 0, "enqueued": 0, "enqueue_failures": 0}

    now = datetime.now(UTC)
    enqueue_candidates: list[tuple[str, str, str]] = []
    registered = 0
    async with session_factory()() as db_session:
        for patch_id, source_hash, segments in entries:
            status = (
                TRANSLATION_STATUS_PENDING
                if segments
                else TRANSLATION_STATUS_SKIPPED
            )
            statement = postgresql.insert(PatchTranslation).values(
                id=new_uuid(),
                **_translation_identity(patch_id, source_hash, settings),
                status=status,
                translated_segments=[],
                attempts=0,
            )
            result = await db_session.execute(
                statement.on_conflict_do_nothing(
                    constraint="uq_patch_translations_identity"
                )
            )
            registered += max(int(result.rowcount or 0), 0)

        for patch_id, source_hash, segments in entries:
            if not segments:
                continue
            record = await _select_translation_record(
                db_session,
                patch_id=patch_id,
                source_hash=source_hash,
                settings=settings,
                for_update=True,
            )
            if record is None:
                continue
            if record.status == TRANSLATION_STATUS_PROCESSING:
                started_at = record.processing_started_at
                if started_at is None or now - started_at >= timedelta(
                    seconds=TRANSLATION_PROCESSING_TIMEOUT_SECONDS
                ):
                    record.status = TRANSLATION_STATUS_PENDING
                    record.processing_started_at = None
            if record.status != TRANSLATION_STATUS_PENDING:
                continue
            if record.last_enqueued_at is not None and now - record.last_enqueued_at < timedelta(
                seconds=TRANSLATION_ENQUEUE_RETRY_SECONDS
            ):
                continue
            record.last_enqueued_at = now
            enqueue_candidates.append((str(record.id), patch_id, source_hash))
        await db_session.commit()

    enqueued = 0
    enqueue_failures = 0
    for record_id, patch_id, source_hash in enqueue_candidates:
        try:
            _enqueue_translation_task(
                patch_id,
                source_hash,
                model=settings.platform_openai_model,
            )
        except Exception:
            enqueue_failures += 1
            logger.exception(
                "Failed to enqueue patch translation patch_id=%s source_hash=%s.",
                patch_id,
                source_hash,
            )
            try:
                await _clear_translation_enqueue_timestamp(record_id)
            except Exception:
                logger.exception(
                    "Failed to clear enqueue marker patch_id=%s source_hash=%s.",
                    patch_id,
                    source_hash,
                )
        else:
            enqueued += 1
    return {
        "registered": registered,
        "enqueued": enqueued,
        "enqueue_failures": enqueue_failures,
    }


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
            "and do not invent another Russian synonym. Spirit Scaling is a canonical glossary "
            "concept: use the glossary wording 'коэффициент масштабирования от спиритической "
            "мощности' rather than shortening it to 'коэффициент спиритической мощности' or "
            "'коэффициент от спиритической мощности'.\n\n"
            "AMBIGUOUS ITEM/MECHANIC NAMES: entity_mechanic_collisions lists the small set of "
            "English strings that are both real Deadlock item names and gameplay mechanic names. "
            "They are intentionally NOT hidden behind ENTITY placeholders. Decide their meaning "
            "from the source sentence and segment.context. If the text refers to the named item, "
            "preserve that item name in English exactly. If it refers to a numeric/stat mechanic, "
            "translate it using the glossary. Examples of mechanic usage include '+10% Bullet "
            "Lifesteal' and '+60% Bullet, Spirit and Melee Lifesteal'; examples of item usage "
            "include references such as 'the same change for Melee Lifesteal'. That exact item "
            "reference must keep the literal English item name 'Melee Lifesteal'; it is not a "
            "numeric/stat mechanic. Never reinterpret "
            "Lifesteal as Resist/Resistance. Coordinated Bullet/Spirit/Melee Resist wording must "
            "remain resistance terminology.\n\n"
            "STYLE: match concise Valve Russian Steam patch-note language. Prefer natural phrases "
            "such as 'время перезарядки' when describing a cooldown value. Units are not protected. "
            "A bare source m attached to Move Speed, Movement Speed, Sprint Speed or Dash Speed is "
            "a speed unit and MUST be rendered as m/s before Russian unit localization. For example, "
            "'Move speed bonus reduced from +3.5m to +2m' must use '+3.5m/s' and '+2m/s' in the "
            "translated prepared text; after localization these become '+3,5 м/с' and '+2 м/с'. "
            "Distance, range and radius values remain m and localize to м. Seconds use с. Before "
            "returning the JSON, silently audit every segment once: all source clauses are "
            "represented, no glossary-covered mechanic remains in English unless it is truly an "
            "item name, Lifesteal/Resist families are not confused, Spirit Scaling uses its full "
            "canonical glossary wording, and every movement/sprint/dash speed value uses m/s rather "
            "than bare m. Russian sentence structure may differ freely from English."
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
    source_hash = _source_hash(segments)

    cache = redis_client()
    cached: dict[str, Any] | None = None
    try:
        cached = await _read_json(
            cache,
            _cache_key(
                patch_id,
                source_hash,
                settings.platform_openai_model,
            ),
        )
    except Exception:
        logger.warning(
            "patch_translation_cache_read_failed patch_id=%s",
            patch_id,
            exc_info=True,
        )
    finally:
        try:
            await cache.aclose()
        except Exception:
            logger.warning(
                "patch_translation_cache_close_failed patch_id=%s",
                patch_id,
                exc_info=True,
            )

    cached_matches_identity = bool(
        cached
        and cached.get("locale") == PATCH_TRANSLATION_LOCALE
        and cached.get("source_hash") == source_hash
        and cached.get("translation_version") == PATCH_TRANSLATION_VERSION
        and cached.get("model") == settings.platform_openai_model
    )
    translated = _validated_translation_segments(
        cached.get("segments") if cached_matches_identity else None,
        segments,
    )
    if translated is not None:
        return translated

    database_translation = await _read_database_translation(
        patch_id=patch_id,
        source_hash=source_hash,
        segments=segments,
        settings=settings,
    )
    if database_translation is None:
        return None

    translated, translated_at = database_translation
    try:
        await _write_translation_cache(
            patch_id=patch_id,
            source_hash=source_hash,
            model=settings.platform_openai_model,
            segments=segments,
            translated_by_id=translated,
            translated_at=translated_at,
        )
    except Exception:
        logger.warning(
            "patch_translation_cache_warm_failed patch_id=%s source_hash=%s",
            patch_id,
            source_hash,
            exc_info=True,
        )
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
    expected_source_hash: str | None = None,
    allow_failed_retry: bool = False,
) -> dict[str, Any]:
    settings = settings or get_settings()
    patch_id = str(patch.get("id") or "").strip()
    segments = extract_translation_segments(patch)
    source_hash = _source_hash(segments)
    if not patch_id:
        return {
            "ok": True,
            "status": "skipped",
            "patch_id": patch_id,
            "reason": "no_translatable_segments",
        }
    if expected_source_hash and expected_source_hash != source_hash:
        await _mark_translation_superseded(
            patch_id=patch_id,
            source_hash=expected_source_hash,
            settings=settings,
        )
        return {
            "ok": False,
            "status": TRANSLATION_STATUS_SUPERSEDED,
            "patch_id": patch_id,
            "source_hash": source_hash,
            "expected_source_hash": expected_source_hash,
        }
    if not segments:
        try:
            await _persist_translation_completion(
                patch_id=patch_id,
                source_hash=source_hash,
                segments=[],
                translated_by_id={},
                settings=settings,
                status=TRANSLATION_STATUS_SKIPPED,
            )
        except Exception:
            logger.warning(
                "patch_translation_skip_persist_failed patch_id=%s source_hash=%s",
                patch_id,
                source_hash,
                exc_info=True,
            )
        return {
            "ok": True,
            "status": TRANSLATION_STATUS_SKIPPED,
            "patch_id": patch_id,
            "source_hash": source_hash,
            "reason": "no_translatable_segments",
        }

    lock_key = _lock_key(patch_id, source_hash, settings.platform_openai_model)
    lock_token = token_urlsafe(24)
    acquired = False
    cache = None
    try:
        cached_translation = await get_cached_patch_translation(
            patch,
            settings=settings,
        )
        if cached_translation is not None:
            try:
                await _persist_translation_completion(
                    patch_id=patch_id,
                    source_hash=source_hash,
                    segments=segments,
                    translated_by_id=cached_translation,
                    settings=settings,
                )
            except Exception:
                logger.warning(
                    "patch_translation_cached_persist_failed patch_id=%s source_hash=%s",
                    patch_id,
                    source_hash,
                    exc_info=True,
                )
            return {
                "ok": True,
                "status": "cached",
                "patch_id": patch_id,
                "source_hash": source_hash,
            }

        cache = redis_client()
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

        cached_translation = await get_cached_patch_translation(
            patch,
            settings=settings,
        )
        if cached_translation is not None:
            await _persist_translation_completion(
                patch_id=patch_id,
                source_hash=source_hash,
                segments=segments,
                translated_by_id=cached_translation,
                settings=settings,
            )
            return {
                "ok": True,
                "status": "cached",
                "patch_id": patch_id,
                "source_hash": source_hash,
            }

        claim_status, claim_error = await _claim_translation(
            patch_id=patch_id,
            source_hash=source_hash,
            settings=settings,
            allow_failed_retry=allow_failed_retry,
        )
        if claim_status != TRANSLATION_STATUS_PROCESSING:
            if claim_status in {
                TRANSLATION_STATUS_COMPLETED,
                TRANSLATION_STATUS_SKIPPED,
            }:
                return {
                    "ok": True,
                    "status": "cached",
                    "patch_id": patch_id,
                    "source_hash": source_hash,
                }
            if claim_status == "not_registered":
                logger.error(
                    "patch_translation_task_unregistered patch_id=%s source_hash=%s",
                    patch_id,
                    source_hash,
                )
            return {
                "ok": False,
                "status": claim_status,
                "patch_id": patch_id,
                "source_hash": source_hash,
                "error": claim_error,
            }

        if not (settings.platform_openai_api_key or "").strip():
            await _set_translation_failure(
                patch_id=patch_id,
                source_hash=source_hash,
                error_code="openai_not_configured",
                settings=settings,
            )
            return {
                "ok": False,
                "status": TRANSLATION_STATUS_FAILED,
                "patch_id": patch_id,
                "source_hash": source_hash,
                "error": "openai_not_configured",
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

        translated_at = datetime.now(UTC)
        await _persist_translation_completion(
            patch_id=patch_id,
            source_hash=source_hash,
            segments=segments,
            translated_by_id=translated,
            settings=settings,
        )
        try:
            await _write_translation_cache(
                patch_id=patch_id,
                source_hash=source_hash,
                model=settings.platform_openai_model,
                segments=segments,
                translated_by_id=translated,
                translated_at=translated_at,
            )
        except Exception:
            logger.warning(
                "patch_translation_cache_write_failed patch_id=%s source_hash=%s",
                patch_id,
                source_hash,
                exc_info=True,
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
        try:
            await _set_translation_failure(
                patch_id=patch_id,
                source_hash=source_hash,
                error_code=type(error).__name__,
                settings=settings,
            )
        except Exception:
            logger.warning(
                "patch_translation_failure_persist_failed patch_id=%s source_hash=%s",
                patch_id,
                source_hash,
                exc_info=True,
            )
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
        if acquired and cache is not None:
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
        if cache is not None:
            try:
                await cache.aclose()
            except Exception:
                logger.warning(
                    "patch_translation_cache_close_failed patch_id=%s",
                    patch_id,
                    exc_info=True,
                )


__all__ = [
    "PATCH_TRANSLATION_TASK_NAME",
    "apply_cached_patch_translation",
    "ensure_patch_translation_records",
    "extract_translation_segments",
    "get_cached_patch_translation",
    "mark_patch_translation_failed",
    "merge_translation",
    "translation_source_hash",
    "translate_patch_to_russian",
]
