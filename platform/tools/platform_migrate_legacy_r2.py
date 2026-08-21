#!/usr/bin/env python3
"""Cut over legacy upload references into immutable R2 media variants.

The command is intentionally migration-only. Persistent media ends in R2 and public
reads go through the CDN. The local upload directory may be consulted only as a
historical migration source; it is never a runtime fallback.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import sys
import tempfile
from typing import Any, Iterator


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM_ROOT))

from python_packages.platform_infra.db import dispose_engine, session_factory
from python_packages.platform_infra.media.r2_storage import R2Storage
from tools import platform_migrate_media as migration


DEFAULT_CHECKPOINT = Path(
    "/opt/oldsparky/platform/shared/media-migration/r2-cutover-checkpoint.json"
)
DEFAULT_MAX_RECORDS = 1_000
FILE_CHUNK_BYTES = 64 * 1024
LEGACY_PREFIXES = (
    "avatars/",
    "profile-banners/",
    "tournament-covers/",
)


@dataclass(frozen=True)
class LegacySourceSelection:
    """One bounded source selected for a DB-referenced legacy upload."""

    location: str
    analysis_settings: Any
    source_path: Path
    source_bytes: int
    source_sha256: str
    r2_gets: int
    local_reads: int


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile DB-referenced legacy /api/v1/uploads objects from R2/local "
            "history into immutable public media variants. R2 is preferred when an "
            "identical local duplicate exists. No bucket listing or runtime local "
            "fallback is used."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform the bounded legacy-to-immutable-media cutover.",
    )
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    parser.add_argument(
        "--max-records",
        type=int,
        default=DEFAULT_MAX_RECORDS,
        help="Maximum legacy DB references allowed in one cutover run.",
    )
    parser.add_argument(
        "--verify-cdn",
        action="store_true",
        help="Verify every produced variant through the configured CDN.",
    )
    parser.add_argument("--cdn-timeout", type=float, default=10.0)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    if args.max_records < 1 or args.max_records > migration.MAX_INVENTORY_RECORDS:
        raise migration.MigrationError(
            "invalid_max_records",
            (
                "--max-records must be between 1 and "
                f"{migration.MAX_INVENTORY_RECORDS}."
            ),
        )
    if args.cdn_timeout <= 0 or args.cdn_timeout > 60:
        raise migration.MigrationError(
            "invalid_cdn_timeout",
            "--cdn-timeout must be greater than zero and at most 60 seconds.",
        )
    return args


def _legacy_key(key: str | None) -> str:
    if not key:
        raise migration.MigrationError(
            "legacy_r2_key_missing",
            "Legacy R2 candidate has no object key.",
        )
    path = PurePosixPath(key)
    if (
        path.is_absolute()
        or ".." in path.parts
        or "\\" in key
        or "//" in key
        or not key.startswith(LEGACY_PREFIXES)
    ):
        raise migration.MigrationError(
            "legacy_r2_key_invalid",
            "Legacy R2 object key is outside the approved upload prefixes.",
        )
    return str(path)


def _r2_error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return ""
    return str((response.get("Error") or {}).get("Code") or "")


def build_legacy_r2_client(settings):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=str(settings.platform_r2_endpoint_url).rstrip("/"),
        aws_access_key_id=settings.platform_r2_access_key_id,
        aws_secret_access_key=settings.platform_r2_secret_access_key,
        region_name=str(settings.platform_r2_region),
        config=Config(
            signature_version="s3v4",
            connect_timeout=float(settings.platform_r2_connect_timeout_seconds),
            read_timeout=float(settings.platform_r2_read_timeout_seconds),
            retries={
                "mode": "standard",
                "total_max_attempts": int(settings.platform_r2_max_attempts),
            },
            s3={"addressing_style": "path"},
        ),
    )


def stage_legacy_r2_original(
    *,
    client: object,
    bucket_name: str,
    candidate: migration.LegacyMediaCandidate,
    destination_root: Path,
    max_bytes: int,
) -> Path:
    """Stream one exact DB-referenced legacy R2 object into private temp staging."""
    if candidate.source_kind != "local_upload":
        raise migration.MigrationError(
            "legacy_r2_candidate_invalid",
            "Only legacy /api/v1/uploads references can be staged from R2.",
        )
    key = _legacy_key(candidate.source_key)
    try:
        response = client.get_object(Bucket=bucket_name, Key=key)
    except Exception as exc:
        if _r2_error_code(exc) in {"404", "NoSuchKey", "NotFound"}:
            raise migration.MigrationError(
                "legacy_r2_source_missing",
                "A DB-referenced legacy R2 original is missing.",
            ) from exc
        raise migration.MigrationError(
            "legacy_r2_read_failed",
            "Legacy R2 original could not be opened.",
        ) from exc

    body = response.get("Body")
    try:
        declared_bytes = int(response.get("ContentLength") or 0)
    except (TypeError, ValueError) as exc:
        if body is not None and hasattr(body, "close"):
            body.close()
        raise migration.MigrationError(
            "legacy_r2_size_invalid",
            "Legacy R2 original has invalid size metadata.",
        ) from exc

    if body is None or not hasattr(body, "read"):
        raise migration.MigrationError(
            "legacy_r2_body_invalid",
            "Legacy R2 original did not return a readable body.",
        )
    if declared_bytes <= 0:
        if hasattr(body, "close"):
            body.close()
        raise migration.MigrationError(
            "empty_media",
            "Legacy R2 original is empty.",
        )
    if declared_bytes > max_bytes:
        if hasattr(body, "close"):
            body.close()
        raise migration.MigrationError(
            "media_too_large",
            "Legacy R2 original exceeds the configured media input bound.",
        )

    root = destination_root.resolve()
    destination = root.joinpath(*PurePosixPath(key).parts)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination.parent, 0o700)

    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    total = 0
    try:
        descriptor = os.open(destination, flags, 0o600)
        with os.fdopen(descriptor, "wb") as target:
            descriptor = None
            while True:
                chunk = body.read(FILE_CHUNK_BYTES)
                if not chunk:
                    break
                if not isinstance(chunk, (bytes, bytearray)):
                    raise migration.MigrationError(
                        "legacy_r2_body_invalid",
                        "Legacy R2 original returned a non-byte chunk.",
                    )
                total += len(chunk)
                if total > max_bytes or total > declared_bytes:
                    raise migration.MigrationError(
                        "legacy_r2_size_changed",
                        "Legacy R2 original exceeded its bounded declared size.",
                    )
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if hasattr(body, "close"):
            body.close()

    if total != declared_bytes:
        destination.unlink(missing_ok=True)
        raise migration.MigrationError(
            "legacy_r2_size_changed",
            "Legacy R2 original length changed during the bounded read.",
        )
    return destination


def _resolve_local_original_if_present(
    candidate: migration.LegacyMediaCandidate,
    *,
    upload_root: Path,
) -> Path | None:
    try:
        return migration.resolve_local_original(candidate, upload_root=upload_root)
    except migration.MigrationError as exc:
        if exc.code == "source_missing":
            return None
        raise


@contextmanager
def materialize_legacy_source(
    *,
    client: object,
    bucket_name: str,
    candidate: migration.LegacyMediaCandidate,
    settings,
) -> Iterator[LegacySourceSelection]:
    """Resolve one legacy reference without ever treating local disk as runtime storage.

    R2 is the preferred migration source. If both R2 and local historical copies exist,
    their bounded SHA-256/size must match exactly. A mismatch is a manual conflict.
    """
    max_bytes = int(settings.platform_media_max_input_bytes)
    local_path = _resolve_local_original_if_present(
        candidate,
        upload_root=settings.platform_upload_dir,
    )
    local_bytes = 0
    local_sha256: str | None = None
    local_reads = 0
    if local_path is not None:
        local_bytes, local_sha256 = migration.hash_bounded_file(
            local_path,
            max_bytes=max_bytes,
        )
        local_reads = 1

    with tempfile.TemporaryDirectory(prefix="old-sparky-r2-cutover-") as directory:
        temporary_root = Path(directory)
        r2_path: Path | None = None
        r2_bytes = 0
        r2_sha256: str | None = None
        r2_gets = 0
        try:
            r2_path = stage_legacy_r2_original(
                client=client,
                bucket_name=bucket_name,
                candidate=candidate,
                destination_root=temporary_root,
                max_bytes=max_bytes,
            )
            r2_gets = 1
            r2_bytes, r2_sha256 = migration.hash_bounded_file(
                r2_path,
                max_bytes=max_bytes,
            )
        except migration.MigrationError as exc:
            if exc.code != "legacy_r2_source_missing":
                raise

        if r2_path is not None and local_path is not None:
            if r2_bytes != local_bytes or r2_sha256 != local_sha256:
                raise migration.MigrationError(
                    "legacy_source_conflict",
                    "Legacy R2 and local originals differ; manual reconciliation is required.",
                )
            location = "both"
            selected_path = r2_path
            source_bytes = r2_bytes
            source_sha256 = str(r2_sha256)
            analysis_settings = settings.model_copy(
                update={"platform_upload_dir": temporary_root}
            )
        elif r2_path is not None:
            location = "r2"
            selected_path = r2_path
            source_bytes = r2_bytes
            source_sha256 = str(r2_sha256)
            analysis_settings = settings.model_copy(
                update={"platform_upload_dir": temporary_root}
            )
        elif local_path is not None:
            location = "local"
            selected_path = local_path
            source_bytes = local_bytes
            source_sha256 = str(local_sha256)
            analysis_settings = settings
        else:
            raise migration.MigrationError(
                "legacy_source_missing",
                "DB-referenced legacy media is absent from both R2 and local history.",
            )

        yield LegacySourceSelection(
            location=location,
            analysis_settings=analysis_settings,
            source_path=selected_path,
            source_bytes=source_bytes,
            source_sha256=source_sha256,
            r2_gets=r2_gets,
            local_reads=local_reads,
        )


def analyze_selected_source(
    candidate: migration.LegacyMediaCandidate,
    *,
    selection: LegacySourceSelection,
    settings,
) -> migration.CandidateAnalysis:
    analysis = migration.analyze_candidate(
        candidate,
        settings=selection.analysis_settings,
        processor=migration.production_image_processor(settings),
    )
    if not analysis.ok:
        raise migration.MigrationError(
            analysis.error_code or "invalid_source",
            "Legacy source failed media analysis.",
        )
    if (
        analysis.source_path != selection.source_path
        or analysis.source_bytes != selection.source_bytes
        or analysis.source_sha256 != selection.source_sha256
    ):
        raise migration.MigrationError(
            "legacy_source_changed",
            "Legacy source changed while it was being analyzed.",
        )
    return analysis


def summarize_inventory(
    candidates: list[migration.LegacyMediaCandidate],
) -> dict[str, int]:
    return {
        "legacy_upload_references": sum(
            candidate.source_kind == "local_upload" for candidate in candidates
        ),
        "packaged_asset_references": sum(
            candidate.source_kind == "packaged_fallback" for candidate in candidates
        ),
        "manual_conflicts": sum(
            candidate.source_kind == "manual_conflict" for candidate in candidates
        ),
    }


async def load_inventory() -> list[migration.LegacyMediaCandidate]:
    async with session_factory()() as db_session:
        return await migration.load_inventory(db_session)


def _record_verification(
    record: dict[str, Any],
    verification: dict[str, Any],
) -> None:
    record["verified_at"] = migration.utc_iso()
    record["verification"] = {
        "ok": True,
        "descriptor_fingerprint": verification["descriptor_fingerprint"],
        "variants": verification["variants"],
        "head_objects": verification["head_objects"],
        "cdn_gets": verification["cdn_gets"],
    }
    record.pop("verify_error_code", None)


async def verify_cutover_checkpoint_record(
    record: dict[str, Any],
    *,
    settings,
    storage,
    verify_cdn: bool,
    cdn_timeout: float,
) -> dict[str, Any]:
    previous_source_kind = record.get("source_kind")
    record["source_kind"] = "local_upload"
    try:
        verification = await migration.verify_checkpoint_record(
            record,
            settings=settings,
            storage=storage,
            verify_cdn=verify_cdn,
            cdn_timeout=cdn_timeout,
        )
    finally:
        if previous_source_kind is None:
            record.pop("source_kind", None)
        else:
            record["source_kind"] = previous_source_kind
    _record_verification(record, verification)
    return verification


def cutover_checkpoint_records(checkpoint: dict[str, Any]) -> list[dict[str, Any]]:
    records = [
        record
        for record in checkpoint.get("records", {}).values()
        if isinstance(record, dict)
        and record.get("asset_id")
        and isinstance(record.get("legacy_url"), str)
        and str(record["legacy_url"]).startswith("/api/v1/uploads/")
    ]
    records.sort(key=lambda record: str(record.get("cursor") or ""))
    return records


def _source_report(selection: LegacySourceSelection) -> dict[str, Any]:
    return {
        "location": selection.location,
        "source_bytes": selection.source_bytes,
        "source_sha256": selection.source_sha256,
    }


def _base_operations() -> dict[str, int]:
    return {
        "r2_gets": 0,
        "local_reads": 0,
        "r2_head_objects": 0,
        "cdn_gets": 0,
        "list_objects": 0,
        "legacy_r2_deletes": 0,
        "local_deletes": 0,
    }


async def inspect_sources(
    actionable: list[migration.LegacyMediaCandidate],
    *,
    settings,
    legacy_client: object,
) -> tuple[list[dict[str, Any]], Counter[str], dict[str, int]]:
    results: list[dict[str, Any]] = []
    locations: Counter[str] = Counter()
    operations = _base_operations()
    for candidate in actionable:
        try:
            with materialize_legacy_source(
                client=legacy_client,
                bucket_name=str(settings.platform_r2_bucket_name),
                candidate=candidate,
                settings=settings,
            ) as selection:
                analyze_selected_source(candidate, selection=selection, settings=settings)
                operations["r2_gets"] += selection.r2_gets
                operations["local_reads"] += selection.local_reads
                locations[selection.location] += 1
                results.append(
                    {
                        "cursor": candidate.cursor,
                        "ok": True,
                        **_source_report(selection),
                    }
                )
        except Exception as exc:
            code = (
                exc.code
                if isinstance(exc, migration.MigrationError)
                else "source_inspection_failed"
            )
            results.append({"cursor": candidate.cursor, "ok": False, "code": code})
    return results, locations, operations


async def run_cutover(
    args: argparse.Namespace,
    *,
    settings,
    legacy_client: object | None = None,
    storage=None,
) -> tuple[dict[str, Any], int]:
    candidates = await load_inventory()
    inventory_before = summarize_inventory(candidates)
    conflicts = [
        {
            "cursor": candidate.cursor,
            "code": candidate.conflict_code or "manual_conflict",
        }
        for candidate in candidates
        if candidate.source_kind == "manual_conflict"
    ][:50]
    actionable = [
        candidate for candidate in candidates if candidate.source_kind == "local_upload"
    ]

    if conflicts:
        return (
            {
                "ok": False,
                "mode": "apply" if args.apply else "check",
                "mutated": False,
                "code": "manual_conflicts_present",
                "inventory_before": inventory_before,
                "manual_conflicts": conflicts,
                "operations": _base_operations(),
            },
            2,
        )

    if len(actionable) > args.max_records:
        return (
            {
                "ok": False,
                "mode": "apply" if args.apply else "check",
                "mutated": False,
                "code": "legacy_inventory_bound_exceeded",
                "inventory_before": inventory_before,
                "max_records": args.max_records,
                "operations": _base_operations(),
            },
            2,
        )

    if not actionable:
        return (
            {
                "ok": True,
                "mode": "apply" if args.apply else "check",
                "mutated": False,
                "code": "ready",
                "inventory_before": inventory_before,
                "source_locations": {},
                "operations": _base_operations(),
            },
            0,
        )

    resolved_client = legacy_client or build_legacy_r2_client(settings)

    if not args.apply:
        inspected, locations, operations = await inspect_sources(
            actionable,
            settings=settings,
            legacy_client=resolved_client,
        )
        failed = sum(not result["ok"] for result in inspected)
        return (
            {
                "ok": False,
                "mode": "check",
                "mutated": False,
                "code": (
                    "legacy_source_conflicts_present"
                    if failed
                    else "legacy_media_cutover_required"
                ),
                "inventory_before": inventory_before,
                "source_locations": dict(sorted(locations.items())),
                "source_results": inspected,
                "operations": operations,
            },
            2,
        )

    resolved_storage = storage or R2Storage.from_settings(settings)
    checkpoint_store = migration.CheckpointStore(args.checkpoint)
    results: list[dict[str, Any]] = []
    source_locations: Counter[str] = Counter()
    operations = _base_operations()

    with checkpoint_store:
        checkpoint = checkpoint_store.load()

        existing_records = cutover_checkpoint_records(checkpoint)
        if len(existing_records) > args.max_records:
            return (
                {
                    "ok": False,
                    "mode": "apply",
                    "mutated": False,
                    "code": "checkpoint_bound_exceeded",
                    "inventory_before": inventory_before,
                    "max_records": args.max_records,
                    "operations": operations,
                },
                2,
            )
        for record in existing_records:
            try:
                verification = await verify_cutover_checkpoint_record(
                    record,
                    settings=settings,
                    storage=resolved_storage,
                    verify_cdn=args.verify_cdn,
                    cdn_timeout=args.cdn_timeout,
                )
                checkpoint_store.save(checkpoint)
                operations["r2_head_objects"] += int(verification["head_objects"])
                operations["cdn_gets"] += int(verification["cdn_gets"])
            except Exception as exc:
                record.pop("verified_at", None)
                record["verification"] = {"ok": False}
                record["verify_error_code"] = (
                    exc.code
                    if isinstance(exc, migration.MigrationError)
                    else "verify_failed"
                )
                checkpoint_store.save(checkpoint)
                results.append(
                    {
                        "cursor": str(record.get("cursor") or ""),
                        "asset_id": record.get("asset_id"),
                        "ok": False,
                        "code": record["verify_error_code"],
                        "phase": "checkpoint_reverify",
                    }
                )
                break

        if not any(not result["ok"] for result in results):
            for candidate in actionable:
                try:
                    with materialize_legacy_source(
                        client=resolved_client,
                        bucket_name=str(settings.platform_r2_bucket_name),
                        candidate=candidate,
                        settings=settings,
                    ) as selection:
                        operations["r2_gets"] += selection.r2_gets
                        operations["local_reads"] += selection.local_reads
                        source_locations[selection.location] += 1
                        analysis = analyze_selected_source(
                            candidate,
                            selection=selection,
                            settings=settings,
                        )
                        applied = await migration.apply_candidate(
                            analysis,
                            checkpoint=checkpoint,
                            checkpoint_store=checkpoint_store,
                        )
                        if not applied["ok"]:
                            raise migration.MigrationError(
                                str(applied.get("code") or "apply_failed"),
                                "Legacy source failed immutable media processing.",
                            )
                        record = checkpoint["records"][candidate.identity]
                        record["source_location"] = selection.location
                        record["legacy_r2_original_retained"] = selection.location in {
                            "r2",
                            "both",
                        }
                        record["legacy_local_original_retained"] = selection.location in {
                            "local",
                            "both",
                        }
                        verification = await verify_cutover_checkpoint_record(
                            record,
                            settings=settings,
                            storage=resolved_storage,
                            verify_cdn=args.verify_cdn,
                            cdn_timeout=args.cdn_timeout,
                        )
                        checkpoint_store.save(checkpoint)
                        operations["r2_head_objects"] += int(verification["head_objects"])
                        operations["cdn_gets"] += int(verification["cdn_gets"])
                        results.append(
                            {
                                "cursor": candidate.cursor,
                                "asset_id": applied["asset_id"],
                                "ok": True,
                                "variants": verification["variants"],
                                **_source_report(selection),
                            }
                        )
                except Exception as exc:
                    code = (
                        exc.code
                        if isinstance(exc, migration.MigrationError)
                        else "cutover_failed"
                    )
                    record = checkpoint.get("records", {}).get(candidate.identity)
                    if isinstance(record, dict):
                        record.pop("verified_at", None)
                        record["verification"] = {"ok": False}
                        record["verify_error_code"] = code
                        checkpoint_store.save(checkpoint)
                    results.append(
                        {
                            "cursor": candidate.cursor,
                            "ok": False,
                            "code": code,
                        }
                    )
                    break

    candidates_after = await load_inventory()
    inventory_after = summarize_inventory(candidates_after)
    remaining_conflicts = inventory_after["manual_conflicts"]
    remaining_legacy = inventory_after["legacy_upload_references"]
    failed = sum(not result["ok"] for result in results)
    ok = failed == 0 and remaining_conflicts == 0 and remaining_legacy == 0

    report = {
        "ok": ok,
        "mode": "apply",
        "mutated": bool(results),
        "processed": len(results),
        "succeeded": len(results) - failed,
        "failed": failed,
        "inventory_before": inventory_before,
        "inventory_after": inventory_after,
        "source_locations": dict(sorted(source_locations.items())),
        "operations": operations,
        "results": results,
        "checkpoint": str(checkpoint_store.path),
    }
    if not ok and failed == 0:
        report["code"] = "legacy_references_remain"
    return report, 0 if ok else 2


async def async_main(
    args: argparse.Namespace,
    *,
    legacy_client: object | None = None,
    storage=None,
) -> tuple[dict[str, Any], int]:
    configured_env_file = args.env_file or (
        Path(os.environ["PLATFORM_ENV_FILE"])
        if os.environ.get("PLATFORM_ENV_FILE")
        else None
    )
    if configured_env_file is not None:
        migration.load_env_file(configured_env_file)
    migration.get_settings.cache_clear()
    settings = migration.get_settings()
    try:
        migration.validate_platform_settings(settings)
    except Exception as exc:
        raise migration.MigrationError(
            "platform_configuration_invalid",
            "Platform production configuration validation failed.",
        ) from exc
    if settings.platform_environment.strip().lower() != "production":
        raise migration.MigrationError(
            "production_required",
            "Legacy media cutover is restricted to the production environment.",
        )
    migration.validate_database_boundary(settings)
    migration.validate_r2_mutation_boundary(settings)
    return await run_cutover(
        args,
        settings=settings,
        legacy_client=legacy_client,
        storage=storage,
    )


def print_report(report: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, sort_keys=True))
        return
    print(
        "Legacy media cutover: "
        f"mode={report.get('mode')}; "
        f"ok={str(bool(report.get('ok'))).lower()}; "
        f"mutated={str(bool(report.get('mutated'))).lower()}."
    )
    print(
        json.dumps(
            report.get("inventory_after") or report.get("inventory_before"),
            sort_keys=True,
        )
    )


async def run_with_cleanup(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    try:
        return await async_main(args)
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        report, exit_code = asyncio.run(run_with_cleanup(args))
        print_report(report, as_json=args.as_json)
        return exit_code
    except migration.MigrationError as exc:
        print(f"Legacy media cutover blocked [{exc.code}]: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Legacy media cutover interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(
            f"Legacy media cutover failed safely: {type(exc).__name__}; "
            "no credentials were printed.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
