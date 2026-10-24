#!/usr/bin/env python3
"""Run a small loopback-only cached-read diagnostic for current home patches."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
import re
import statistics
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from redis.asyncio import from_url

try:
    from apps.platform_api.app.services.home_content import (
        HOME_CONTENT_KEY,
        PATCH_DETAIL_KEY_PREFIX,
    )
except ModuleNotFoundError:  # Direct execution from platform/tools.
    from apps.platform_api.app.services.home_content import (  # type: ignore[no-redef]
        HOME_CONTENT_KEY,
        PATCH_DETAIL_KEY_PREFIX,
    )


API_BASE = "http://127.0.0.1:8010/api/v1/content/"
READ_ROUNDS = 3
PATCH_READ_CONCURRENCY = 4
HOME_READ_CONCURRENCY = 2
REQUEST_TIMEOUT_SECONDS = 8.0
REDIS_TIMEOUT_SECONDS = 3.0
MAX_RESPONSE_BYTES = 1_000_000
PATCH_ID_RE = re.compile(r"^[0-9]{1,32}$")
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
@dataclass(frozen=True, slots=True)
class HttpRead:
    path: str
    status: int
    duration_ms: float
    response_bytes: int
    cache_control: str
    payload: dict[str, Any] | None
    error_class: str | None = None


def _request_url(path: str) -> str:
    if path == "home":
        return API_BASE + path
    patch_match = re.fullmatch(r"patches/([0-9]{1,32})", path)
    if patch_match is None:
        raise ValueError("invalid_path")
    return API_BASE + path


def _cache_policy_matches(
    value: str,
    *,
    expected_cache_age: int,
    expected_stale_age: int,
) -> bool:
    directives: dict[str, str | None] = {}
    for raw_directive in value.split(","):
        name, separator, raw_value = raw_directive.strip().lower().partition("=")
        if not name or name in directives:
            return False
        directives[name] = raw_value.strip().strip('"') if separator else None
    return (
        set(directives) == {"public", "max-age", "stale-while-revalidate"}
        and directives["public"] is None
        and directives.get("max-age") == str(expected_cache_age)
        and directives.get("stale-while-revalidate") == str(expected_stale_age)
    )


async def _request_json(client: httpx.AsyncClient, path: str) -> HttpRead:
    started = time.perf_counter()
    url = _request_url(path)
    try:
        async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
            async with client.stream("GET", url, headers={"Accept": "application/json"}) as response:
                final_url = urlsplit(str(response.url))
                if (
                    final_url.scheme != "http"
                    or final_url.hostname != "127.0.0.1"
                    or final_url.port != 8010
                ):
                    return HttpRead(path, 0, (time.perf_counter() - started) * 1000, 0, "", None, "unexpected_origin")
                if 300 <= response.status_code < 400:
                    return HttpRead(path, response.status_code, (time.perf_counter() - started) * 1000, 0, "", None, "redirect")
                if response.status_code != 200:
                    return HttpRead(path, response.status_code, (time.perf_counter() - started) * 1000, 0, "", None, "http_status")
                content_length = response.headers.get("Content-Length")
                if content_length and content_length.isdigit() and int(content_length) > MAX_RESPONSE_BYTES:
                    return HttpRead(path, 0, (time.perf_counter() - started) * 1000, 0, "", None, "response_too_large")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                        return HttpRead(path, 0, (time.perf_counter() - started) * 1000, 0, "", None, "response_too_large")
                    raw.extend(chunk)
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                return HttpRead(path, 0, (time.perf_counter() - started) * 1000, len(raw), "", None, "invalid_response")
            return HttpRead(
                path=path,
                status=200,
                duration_ms=(time.perf_counter() - started) * 1000,
                response_bytes=len(raw),
                cache_control=response.headers.get("Cache-Control", "").lower(),
                payload=payload,
            )
    except (httpx.TimeoutException, TimeoutError):
        status = 0
        error_class = "timeout"
    except httpx.HTTPError:
        status = 0
        error_class = "transport"
    except (TypeError, ValueError, json.JSONDecodeError):
        status = 0
        error_class = "invalid_response"
    return HttpRead(
        path=path,
        status=status,
        duration_ms=(time.perf_counter() - started) * 1000,
        response_bytes=0,
        cache_control="",
        payload=None,
        error_class=error_class,
    )


async def _cache_coverage(patch_ids: tuple[str, ...]) -> tuple[bool, int]:
    from python_packages.platform_infra.config import get_settings

    cache = from_url(
        get_settings().platform_redis_url,
        decode_responses=False,
        socket_connect_timeout=1.0,
        socket_timeout=1.0,
        retry_on_timeout=False,
    )
    try:
        try:
            async with asyncio.timeout(REDIS_TIMEOUT_SECONDS):
                async with asyncio.TaskGroup() as group:
                    home_task = group.create_task(cache.get(HOME_CONTENT_KEY))
                    detail_tasks = [
                        group.create_task(cache.get(PATCH_DETAIL_KEY_PREFIX + patch_id))
                        for patch_id in patch_ids
                    ]
                home_value = home_task.result()
                detail_values = [task.result() for task in detail_tasks]
        except TimeoutError as exc:
            raise RuntimeError("cache_probe_timeout") from exc
        detail_count = sum(value is not None for value in detail_values)
        return home_value is not None, detail_count
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001 - do not expose Redis diagnostics.
        raise RuntimeError("cache_probe_failed") from exc
    finally:
        try:
            async with asyncio.timeout(1.0):
                await cache.aclose()
        except Exception:  # noqa: BLE001 - closing a timed-out client is best effort.
            pass


def _patch_rows(
    home: dict[str, Any],
    *,
    expected_patch_ids: tuple[str, ...] | None = None,
) -> tuple[tuple[str, str], ...]:
    rows = home.get("patches")
    if home.get("patches_available") is not True or not isinstance(rows, list) or len(rows) < 4:
        raise ValueError("home_patch_feed_unavailable")
    selected: list[tuple[str, str]] = []
    seen: set[str] = set()
    for row in rows[:4]:
        if not isinstance(row, dict):
            raise ValueError("home_patch_shape_invalid")
        patch_id = str(row.get("id") or "")
        title = str(row.get("title") or "").strip()
        if not PATCH_ID_RE.fullmatch(patch_id) or not title or patch_id in seen:
            raise ValueError("home_patch_identity_invalid")
        seen.add(patch_id)
        selected.append((patch_id, title))
    if expected_patch_ids is not None and tuple(patch_id for patch_id, _ in selected) != expected_patch_ids:
        raise ValueError("target_patch_set_changed")
    return tuple(selected)


def _manifest_ids(raw: str, *, expected_count: int) -> tuple[str, ...] | None:
    if not raw:
        return None
    ids = tuple(raw.split(","))
    if (
        len(ids) != expected_count
        or len(set(ids)) != expected_count
        or any(PATCH_ID_RE.fullmatch(patch_id) is None for patch_id in ids)
    ):
        raise ValueError("target_manifest_invalid")
    return ids


def _measurements(reads: list[HttpRead], *, expected_cache_age: int) -> dict[str, Any]:
    durations = sorted(read.duration_ms for read in reads)
    rank = max(0, (len(durations) * 95 + 99) // 100 - 1)
    return {
        "count": len(reads),
        "status_200": sum(read.status == 200 for read in reads),
        "status_other": sum(read.status != 200 for read in reads),
        "cache_control_expected": sum(
            _cache_policy_matches(
                read.cache_control,
                expected_cache_age=expected_cache_age,
                expected_stale_age=900 if expected_cache_age == 300 else 300,
            )
            for read in reads
        ),
        "response_bytes_total": sum(read.response_bytes for read in reads),
        "latency_ms": {
            "p50": round(statistics.median(durations), 2) if durations else None,
            "p95": round(durations[rank], 2) if durations else None,
            "max": round(durations[-1], 2) if durations else None,
        },
    }


def _metadata_summary(patch_id: str, expected_title: str, payload: dict[str, Any]) -> dict[str, Any]:
    sections = payload.get("sections")
    if not isinstance(sections, list):
        sections = []
    heroes = [
        section for section in sections
        if isinstance(section, dict) and section.get("kind") == "hero"
    ]
    abilities = [
        ability
        for hero in heroes
        for ability in (hero.get("abilities") if isinstance(hero.get("abilities"), list) else [])
        if isinstance(ability, dict)
    ]
    russian_hero_metadata = sum(
        bool(CYRILLIC_RE.search(str(hero.get("hero_display_name") or "")))
        and bool(CYRILLIC_RE.search(str(hero.get("hero_description") or "")))
        for hero in heroes
    )
    russian_abilities = sum(
        bool(CYRILLIC_RE.search(str(ability.get("display_name") or ability.get("name") or "")))
        and bool(CYRILLIC_RE.search(str(ability.get("description") or "")))
        for ability in abilities
    )
    complete_hero_sections = sum(
        bool(CYRILLIC_RE.search(str(hero.get("hero_display_name") or "")))
        and bool(hero.get("hero_image_url"))
        and bool(CYRILLIC_RE.search(str(hero.get("hero_description") or "")))
        and hero.get("hero_metadata_pending") is not True
        and isinstance(hero.get("abilities"), list)
        and len(hero["abilities"]) == 4
        and all(
            isinstance(ability, dict)
            and bool(ability.get("icon_url"))
            and bool(CYRILLIC_RE.search(str(ability.get("display_name") or ability.get("name") or "")))
            and bool(CYRILLIC_RE.search(str(ability.get("description") or "")))
            for ability in hero["abilities"]
        )
        for hero in heroes
    )
    return {
        "id": patch_id,
        "identity_matches": payload.get("id") == patch_id and payload.get("title") == expected_title,
        "hero_sections": len(heroes),
        "complete_hero_sections": complete_hero_sections,
        "hero_images": sum(bool(hero.get("hero_image_url")) for hero in heroes),
        "hero_metadata_pending": sum(hero.get("hero_metadata_pending") is True for hero in heroes),
        "russian_hero_metadata": russian_hero_metadata,
        "abilities": len(abilities),
        "ability_icons": sum(bool(ability.get("icon_url")) for ability in abilities),
        "russian_abilities": russian_abilities,
    }


def _hero_metadata_ready(row: dict[str, Any]) -> bool:
    return (
        row["hero_sections"] == 1
        and row["metadata_stable"] is True
        and row["hero_images"] == 1
        and row["hero_metadata_pending"] == 0
        and row["russian_hero_metadata"] == 1
        and row["abilities"] == row["ability_icons"] == row["russian_abilities"]
    )


async def run_diagnostic(
    *,
    expected_home_patch_ids: tuple[str, ...] | None = None,
    expected_hero_patch_ids: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    if (expected_home_patch_ids is None) != (expected_hero_patch_ids is None):
        raise ValueError("target_manifest_invalid")
    if expected_hero_patch_ids is not None and not set(expected_hero_patch_ids).issubset(
        expected_home_patch_ids or ()
    ):
        raise ValueError("target_manifest_invalid")
    timeout = httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=2.0, pool=2.0)
    limits = httpx.Limits(max_connections=PATCH_READ_CONCURRENCY, max_keepalive_connections=PATCH_READ_CONCURRENCY)
    async with httpx.AsyncClient(
        timeout=timeout,
        limits=limits,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        home_first = await _request_json(client, "home")
        if home_first.status != 200 or home_first.payload is None:
            raise RuntimeError("home_read_failed")
        patch_rows = _patch_rows(home_first.payload, expected_patch_ids=expected_home_patch_ids)
        patch_ids = tuple(row[0] for row in patch_rows)
        home_cache_present, detail_cache_count = await _cache_coverage(patch_ids)
        if not home_cache_present or detail_cache_count != 4:
            raise RuntimeError("refreshed_cache_incomplete")

        home_reads = [home_first]
        home_reads.extend(
            await asyncio.gather(*(_request_json(client, "home") for _ in range(HOME_READ_CONCURRENCY)))
        )
        expected_ids = tuple(row[0] for row in patch_rows)
        for read in home_reads:
            if read.status != 200 or read.payload is None:
                raise RuntimeError("home_cached_read_failed")
            if tuple(row[0] for row in _patch_rows(read.payload)) != expected_ids:
                raise RuntimeError("home_patch_cache_changed_during_sample")

        patch_reads_by_id: dict[str, list[HttpRead]] = {patch_id: [] for patch_id in patch_ids}
        for _ in range(READ_ROUNDS):
            reads = await asyncio.gather(
                *(_request_json(client, f"patches/{patch_id}") for patch_id in patch_ids)
            )
            for patch_id, read in zip(patch_ids, reads, strict=True):
                if read.status != 200 or read.payload is None:
                    raise RuntimeError("patch_cached_read_failed")
                patch_reads_by_id[patch_id].append(read)

    metadata: list[dict[str, Any]] = []
    for patch_id, title in patch_rows:
        reads = patch_reads_by_id[patch_id]
        if any(
            not _cache_policy_matches(
                read.cache_control,
                expected_cache_age=60,
                expected_stale_age=300,
            )
            for read in reads
        ):
            raise RuntimeError("patch_cache_headers_invalid")
        rows = [_metadata_summary(patch_id, title, read.payload or {}) for read in reads]
        row = dict(rows[0])
        row["identity_matches"] = all(sample["identity_matches"] for sample in rows)
        first_sections = (reads[0].payload or {}).get("sections")
        row["metadata_stable"] = all(
            (read.payload or {}).get("sections") == first_sections for read in reads[1:]
        )
        if not row["identity_matches"]:
            raise RuntimeError("patch_identity_mismatch")
        metadata.append(row)
    if any(
        not _cache_policy_matches(
            read.cache_control,
            expected_cache_age=300,
            expected_stale_age=900,
        )
        for read in home_reads
    ):
        raise RuntimeError("home_cache_headers_invalid")

    if expected_hero_patch_ids is not None:
        expected_hero_rows = [row for row in metadata if row["id"] in expected_hero_patch_ids]
        heroes_ready = len(expected_hero_rows) == len(expected_hero_patch_ids) and all(
            _hero_metadata_ready(row)
            and row["complete_hero_sections"] == 1
            and row["abilities"] == row["ability_icons"] == row["russian_abilities"] == 4
            for row in expected_hero_rows
        )
    else:
        hero_rows = [row for row in metadata if row["hero_sections"] > 0]
        heroes_ready = all(_hero_metadata_ready(row) for row in hero_rows)
    all_patch_reads = [read for reads in patch_reads_by_id.values() for read in reads]
    return {
        "schema": 1,
        "kind": "bounded_public_content_read_diagnostic",
        "status": "passed" if heroes_ready else "incomplete",
        "sample_only": True,
        "coordinated_refreshes": 1,
        "warm_cache": {"home_present": home_cache_present, "patch_details_present": detail_cache_count},
        "concurrency": {"home": HOME_READ_CONCURRENCY, "patch_details": PATCH_READ_CONCURRENCY},
        "rounds": READ_ROUNDS,
        "home": _measurements(home_reads, expected_cache_age=300),
        "patch_details": _measurements(all_patch_reads, expected_cache_age=60),
        "metadata": metadata,
    }


def main() -> int:
    try:
        expected_home_patch_ids = _manifest_ids(
            os.environ.get("PLATFORM_PUBLIC_CONTENT_EXPECTED_HOME_PATCH_IDS", ""),
            expected_count=4,
        )
        expected_hero_patch_ids = _manifest_ids(
            os.environ.get("PLATFORM_PUBLIC_CONTENT_EXPECTED_HERO_PATCH_IDS", ""),
            expected_count=3,
        )
        report = asyncio.run(
            run_diagnostic(
                expected_home_patch_ids=expected_home_patch_ids,
                expected_hero_patch_ids=expected_hero_patch_ids,
            )
        )
    except Exception as exc:  # noqa: BLE001 - bounded closed operational output.
        error = str(exc)
        allowed = {
            "home_read_failed", "home_patch_feed_unavailable", "home_patch_shape_invalid",
            "home_patch_identity_invalid", "target_patch_set_changed", "refreshed_cache_incomplete", "home_cached_read_failed",
            "home_patch_cache_changed_during_sample", "patch_cached_read_failed",
            "patch_cache_headers_invalid", "patch_identity_mismatch", "home_cache_headers_invalid",
            "cache_probe_timeout", "cache_probe_failed",
            "target_manifest_invalid",
        }
        print("PUBLIC_CONTENT_SAMPLE_JSON " + json.dumps({
            "schema": 1,
            "kind": "bounded_public_content_read_diagnostic",
            "status": "failed",
            "error_class": error if error in allowed else "diagnostic_error",
        }, separators=(",", ":")))
        return 1
    print("PUBLIC_CONTENT_SAMPLE_JSON " + json.dumps(report, ensure_ascii=True, separators=(",", ":")))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
