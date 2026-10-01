"""Closed, translation-free production content diagnostics contract.

The production workflow owns the network/cache probe, while this helper owns
the untrusted boundary: detail validation, internal/public parity, one-line
summary output and sanitized aggregate evidence.  It intentionally uses only
the Python standard library so the trusted workflow copy can run before any
application dependency is imported.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any


SCHEMA = 1
PATCH_ID_RE = re.compile(r"^[0-9]{1,32}$")
SUMMARY_RE = re.compile(
    r"^PRODUCTION_PATCH_DISTRIBUTION schema=1 "
    r"status=(?P<status>passed|failed) "
    r"error_class=(?P<error_class>[a-z_]+) "
    r"latest_patch_id=(?P<latest_patch_id>unavailable|[0-9]{1,32}) "
    r"internal_section_count=(?P<internal_section_count>[0-9]+) "
    r"internal_api_section_count=(?P<internal_api_section_count>[0-9]+) "
    r"public_api_section_count=(?P<public_api_section_count>[0-9]+)$"
)
ERROR_CLASSES = frozenset(
    {
        "none",
        "artifact",
        "internal",
        "malformed",
        "parity",
        "producer",
        "remote_or_transport",
        "status",
    }
)
OUTCOMES = frozenset({"success", "failure", "cancelled", "skipped", "unknown"})
SECTION_KINDS = frozenset({"general", "objective", "item", "hero"})
ITEM_CATEGORIES = frozenset({"weapon", "vitality", "spirit"})
OBJECTIVE_KEYS = frozenset({"urn", "unstable_rift"})
HOME_FIELDS = frozenset(
    {"patches", "videos", "generated_at", "patches_available", "videos_available"}
)
HOME_PATCH_FIELDS = frozenset({"id", "title", "excerpt", "published_at", "url"})
DETAIL_FIELDS = frozenset(
    {"id", "title", "published_at", "url", "content", "sections"}
)
SECTION_FIELDS = frozenset(
    {
        "kind",
        "title",
        "hero_name",
        "item_name",
        "item_category",
        "item_icon_url",
        "objective_key",
        "objective_icon_url",
        "changes",
        "abilities",
    }
)
ABILITY_FIELDS = frozenset({"name", "icon_url", "changes"})
SECTION_STABLE_FIELDS = (
    "kind",
    "title",
    "hero_name",
    "item_name",
    "item_category",
    "item_icon_url",
    "objective_key",
    "objective_icon_url",
)
ABILITY_STABLE_FIELDS = ("name", "icon_url")
MAX_SECTION_COUNT = 100
EVIDENCE_FIELDS = frozenset(
    {
        "schema",
        "kind",
        "status",
        "error_class",
        "event",
        "run_id",
        "run_attempt",
        "target_sha",
        "patch_distribution_status",
        "content_status",
        "latest_patch_id",
        "internal_section_count",
        "internal_api_section_count",
        "public_api_section_count",
    }
)


class DiagnosticsFailure(ValueError):
    """A failure that can be represented without exposing producer details."""

    def __init__(self, error_class: str) -> None:
        safe_error_class = error_class if error_class in ERROR_CLASSES else "internal"
        super().__init__(safe_error_class)
        self.error_class = safe_error_class


def _fail(error_class: str) -> None:
    raise DiagnosticsFailure(error_class)


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail("producer")
    return value


def _closed_mapping(value: object, allowed: frozenset[str]) -> Mapping[str, Any]:
    """Accept only the fields owned by the diagnostic contract."""

    source = _mapping(value)
    if any(not isinstance(key, str) or key not in allowed for key in source):
        _fail("producer")
    return source


def _required_string(
    value: Mapping[str, Any], field: str, *, max_length: int, nonempty: bool = True
) -> str:
    candidate = value.get(field)
    if not isinstance(candidate, str):
        _fail("producer")
    if len(candidate) > max_length or (nonempty and not candidate.strip()):
        _fail("producer")
    return candidate


def _optional_string(
    value: Mapping[str, Any], field: str, *, max_length: int
) -> str | None:
    candidate = value.get(field)
    if candidate is None:
        return None
    if not isinstance(candidate, str) or len(candidate) > max_length:
        _fail("producer")
    return candidate


def _string_list(
    value: Mapping[str, Any], field: str, *, max_items: int, max_length: int
) -> list[str]:
    candidate = value.get(field, [])
    if not isinstance(candidate, list) or len(candidate) > max_items:
        _fail("producer")
    if any(not isinstance(item, str) or len(item) > max_length for item in candidate):
        _fail("producer")
    return list(candidate)


def _canonical_published_at(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        _fail("producer")
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC)
        return parsed.isoformat().replace("+00:00", "Z")
    return parsed.isoformat()


def _canonical_ability(value: object) -> dict[str, Any]:
    source = _closed_mapping(value, ABILITY_FIELDS)
    name = _required_string(source, "name", max_length=120)
    icon_url = _optional_string(source, "icon_url", max_length=1000)
    changes = _string_list(source, "changes", max_items=100, max_length=30_000)
    return {"name": name, "icon_url": icon_url, "changes": changes}


def _canonical_section(value: object) -> dict[str, Any]:
    source = _closed_mapping(value, SECTION_FIELDS)
    kind = source.get("kind")
    if not isinstance(kind, str) or kind not in SECTION_KINDS:
        _fail("producer")
    title = _required_string(source, "title", max_length=120)
    hero_name = _optional_string(source, "hero_name", max_length=80)
    item_name = _optional_string(source, "item_name", max_length=120)
    item_category = _optional_string(source, "item_category", max_length=32)
    item_icon_url = _optional_string(source, "item_icon_url", max_length=1000)
    objective_key = _optional_string(source, "objective_key", max_length=32)
    objective_icon_url = _optional_string(
        source, "objective_icon_url", max_length=1000
    )
    changes = _string_list(source, "changes", max_items=500, max_length=30_000)
    abilities_source = source.get("abilities", [])
    if not isinstance(abilities_source, list) or len(abilities_source) > 20:
        _fail("producer")
    abilities = [_canonical_ability(item) for item in abilities_source]

    if item_category is not None and item_category not in ITEM_CATEGORIES:
        _fail("producer")
    if objective_key is not None and objective_key not in OBJECTIVE_KEYS:
        _fail("producer")
    metadata = (
        hero_name,
        item_name,
        item_category,
        item_icon_url,
        objective_key,
        objective_icon_url,
    )
    if kind == "general":
        if any(item is not None for item in metadata) or abilities:
            _fail("producer")
    elif kind == "hero":
        if hero_name is None or any(
            item is not None
            for item in (item_name, item_category, item_icon_url, objective_key, objective_icon_url)
        ):
            _fail("producer")
    elif kind == "item":
        if item_name is None or item_category is None or abilities or any(
            item is not None for item in (hero_name, objective_key, objective_icon_url)
        ):
            _fail("producer")
    elif kind == "objective":
        if objective_key is None or abilities or any(
            item is not None
            for item in (hero_name, item_name, item_category, item_icon_url)
        ):
            _fail("producer")

    return {
        "kind": kind,
        "title": title,
        "hero_name": hero_name,
        "item_name": item_name,
        "item_category": item_category,
        "item_icon_url": item_icon_url,
        "objective_key": objective_key,
        "objective_icon_url": objective_icon_url,
        "changes": changes,
        "abilities": abilities,
    }


def canonical_section_projection(value: object) -> list[dict[str, Any]]:
    """Return the closed, ordered public projection of patch sections."""

    if not isinstance(value, list) or not value or len(value) > MAX_SECTION_COUNT:
        _fail("producer")
    return [_canonical_section(item) for item in value]


def _stable_section_projection(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project non-translated section identity shared by cache and APIs."""

    return [
        {
            **{field: section[field] for field in SECTION_STABLE_FIELDS},
            "abilities": [
                {field: ability[field] for field in ABILITY_STABLE_FIELDS}
                for ability in section["abilities"]
            ],
        }
        for section in sections
    ]


def _cache_level_projection(detail: Mapping[str, Any]) -> dict[str, Any]:
    """Project the cache contract while allowing section text translation."""

    return {
        "id": detail["id"],
        "title": detail["title"],
        "published_at": detail["published_at"],
        "url": detail["url"],
        "content": detail["content"],
        "sections": _stable_section_projection(detail["sections"]),
    }


def canonical_detail(value: object, *, expected_id: str | None = None) -> dict[str, Any]:
    """Return the exact stable public projection of a patch detail."""

    source = _closed_mapping(value, DETAIL_FIELDS)
    patch_id = _required_string(source, "id", max_length=32)
    if PATCH_ID_RE.fullmatch(patch_id) is None:
        _fail("producer")
    if expected_id is not None and patch_id != expected_id:
        _fail("parity")
    title = _required_string(source, "title", max_length=180)
    published_at = _required_string(source, "published_at", max_length=128)
    published_at = _canonical_published_at(published_at)
    url = _required_string(source, "url", max_length=1000, nonempty=False)
    content = _required_string(source, "content", max_length=30_000, nonempty=False)
    sections = canonical_section_projection(source.get("sections"))
    return {
        "id": patch_id,
        "title": title,
        "published_at": published_at,
        "url": url,
        "content": content,
        "sections": sections,
    }


def _latest_patch_id(home_payload: object) -> str:
    source = _closed_mapping(home_payload, HOME_FIELDS)
    if source.get("patches_available") is not True:
        _fail("producer")
    if "videos" in source and not isinstance(source["videos"], list):
        _fail("producer")
    if "generated_at" in source and not isinstance(source["generated_at"], str):
        _fail("producer")
    if "videos_available" in source and not isinstance(source["videos_available"], bool):
        _fail("producer")
    patches = source.get("patches")
    if not isinstance(patches, list) or not patches or len(patches) > 4:
        _fail("producer")
    for patch in patches:
        _closed_mapping(patch, HOME_PATCH_FIELDS)
    latest = _closed_mapping(patches[0], HOME_PATCH_FIELDS)
    patch_id = latest.get("id")
    if not isinstance(patch_id, str) or PATCH_ID_RE.fullmatch(patch_id) is None:
        _fail("producer")
    return patch_id


def validate_distribution(
    home_payload: object,
    *,
    internal_detail: object,
    internal_api_detail: object,
    public_api_detail: object,
) -> tuple[str, int]:
    """Validate the cache and compare every stable public field across APIs."""

    patch_id = _latest_patch_id(home_payload)
    cached_projection = canonical_detail(internal_detail, expected_id=patch_id)
    internal_projection = canonical_detail(internal_api_detail, expected_id=patch_id)
    public_projection = canonical_detail(public_api_detail, expected_id=patch_id)
    if internal_projection != public_projection:
        _fail("parity")
    if _cache_level_projection(cached_projection) != _cache_level_projection(
        internal_projection
    ):
        _fail("parity")
    section_count = len(internal_projection["sections"])
    if not 0 < section_count <= MAX_SECTION_COUNT:
        _fail("producer")
    return patch_id, section_count


def passed_summary(patch_id: str, section_count: int) -> str:
    if (
        not isinstance(patch_id, str)
        or PATCH_ID_RE.fullmatch(patch_id) is None
        or isinstance(section_count, bool)
        or not isinstance(section_count, int)
        or not 0 < section_count <= MAX_SECTION_COUNT
    ):
        _fail("status")
    return (
        "PRODUCTION_PATCH_DISTRIBUTION schema=1 status=passed error_class=none "
        f"latest_patch_id={patch_id} internal_section_count={section_count} "
        f"internal_api_section_count={section_count} public_api_section_count={section_count}"
    )


def failed_summary(error_class: str) -> str:
    if not isinstance(error_class, str):
        _fail("malformed")
    safe_error_class = error_class if error_class in ERROR_CLASSES else "internal"
    if safe_error_class == "none":
        safe_error_class = "internal"
    return (
        "PRODUCTION_PATCH_DISTRIBUTION schema=1 status=failed "
        f"error_class={safe_error_class} latest_patch_id=unavailable "
        "internal_section_count=0 internal_api_section_count=0 "
        "public_api_section_count=0"
    )


def parse_summary_line(line: str, *, require_passed: bool = False) -> dict[str, Any]:
    """Parse exactly one closed summary line and reject invalid status semantics."""

    if not isinstance(line, str) or "\n" in line or "\r" in line:
        _fail("malformed")
    match = SUMMARY_RE.fullmatch(line)
    if match is None:
        _fail("malformed")
    record = match.groupdict()
    error_class = record["error_class"]
    if error_class not in ERROR_CLASSES:
        _fail("malformed")
    counts = {
        field: int(record[field])
        for field in (
            "internal_section_count",
            "internal_api_section_count",
            "public_api_section_count",
        )
    }
    if record["status"] == "passed":
        if error_class != "none" or record["latest_patch_id"] == "unavailable" or any(
            count <= 0 or count > 100 for count in counts.values()
        ) or len(set(counts.values())) != 1:
            _fail("status")
    elif error_class == "none" or record["latest_patch_id"] != "unavailable" or any(
        count != 0 for count in counts.values()
    ):
        _fail("status")
    if require_passed and record["status"] != "passed":
        _fail("status")
    return {**record, **counts}


def parse_summary_text(
    text: str, *, require_passed: bool = False, exit_code: int = 0
) -> str:
    """Validate one output line and its process status, returning canonical text."""

    if (
        not isinstance(text, str)
        or isinstance(exit_code, bool)
        or not isinstance(exit_code, int)
        or "\r" in text
    ):
        _fail("malformed")
    if text.endswith("\n"):
        line = text[:-1]
    else:
        line = text
    if not line or "\n" in line:
        _fail("malformed")
    record = parse_summary_line(line, require_passed=require_passed)
    if exit_code != 0:
        _fail("remote_or_transport")
    return _format_record(record)


def _format_record(record: Mapping[str, Any]) -> str:
    return (
        "PRODUCTION_PATCH_DISTRIBUTION schema=1 "
        f"status={record['status']} error_class={record['error_class']} "
        f"latest_patch_id={record['latest_patch_id']} "
        f"internal_section_count={record['internal_section_count']} "
        f"internal_api_section_count={record['internal_api_section_count']} "
        f"public_api_section_count={record['public_api_section_count']}"
    )


def _safe_int(value: object) -> int:
    if not isinstance(value, str):
        _fail("malformed")
    if re.fullmatch(r"[1-9][0-9]{0,31}", value) is None:
        return 0
    return int(value)


def _required_text(value: object) -> str:
    """Reject non-text helper inputs instead of coercing them to strings."""

    if not isinstance(value, str):
        _fail("malformed")
    return value


def evidence_payload(
    *,
    target_sha: str,
    event: str,
    run_id: str,
    run_attempt: str,
    patch_outcome: str,
    patch_summary: str,
    content_outcome: str,
) -> dict[str, Any]:
    """Build the only JSON shape permitted for retained aggregate evidence."""

    target_sha = _required_text(target_sha)
    event = _required_text(event)
    run_id = _required_text(run_id)
    run_attempt = _required_text(run_attempt)
    patch_outcome = _required_text(patch_outcome)
    patch_summary = _required_text(patch_summary)
    content_outcome = _required_text(content_outcome)
    safe_sha = target_sha if re.fullmatch(r"[0-9a-f]{40}", target_sha) else "unavailable"
    safe_event = event if event in {"workflow_dispatch", "workflow_run"} else "unavailable"
    safe_run_id = _safe_int(run_id)
    safe_attempt = _safe_int(run_attempt)
    safe_patch_outcome = patch_outcome if patch_outcome in OUTCOMES else "unknown"
    safe_content_outcome = content_outcome if content_outcome in OUTCOMES else "unknown"
    try:
        canonical_summary = parse_summary_text(patch_summary)
        record = parse_summary_line(canonical_summary, require_passed=False)
    except DiagnosticsFailure:
        record = None
    passed = (
        record is not None
        and record["status"] == "passed"
        and safe_sha != "unavailable"
        and safe_event != "unavailable"
        and safe_run_id > 0
        and safe_attempt > 0
        and safe_patch_outcome == "success"
        and safe_content_outcome == "success"
    )
    return {
        "schema": SCHEMA,
        "kind": "platform_content_diagnostics",
        "status": "passed" if passed else "failed",
        "error_class": "none" if passed else "diagnostic_failed",
        "event": safe_event,
        "run_id": safe_run_id,
        "run_attempt": safe_attempt,
        "target_sha": safe_sha,
        "patch_distribution_status": safe_patch_outcome,
        "content_status": safe_content_outcome,
        "latest_patch_id": record["latest_patch_id"] if passed else "unavailable",
        "internal_section_count": record["internal_section_count"] if passed else 0,
        "internal_api_section_count": record["internal_api_section_count"] if passed else 0,
        "public_api_section_count": record["public_api_section_count"] if passed else 0,
    }


def _write_secure_json(path: Path, payload: Mapping[str, Any]) -> None:
    parent = path.parent
    try:
        parent_info = os.lstat(parent)
    except OSError:
        _fail("artifact")
    if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        _fail("artifact")
    if not isinstance(payload, Mapping) or set(payload) != EVIDENCE_FIELDS:
        _fail("artifact")
    if (
        payload.get("schema") != SCHEMA
        or payload.get("kind") != "platform_content_diagnostics"
        or payload.get("status") not in {"passed", "failed"}
        or payload.get("error_class") not in {"none", "diagnostic_failed"}
        or (payload.get("status") == "passed") != (payload.get("error_class") == "none")
        or payload.get("event") not in {"workflow_dispatch", "workflow_run", "unavailable"}
        or payload.get("patch_distribution_status") not in OUTCOMES
        or payload.get("content_status") not in OUTCOMES
        or (
            payload.get("target_sha") != "unavailable"
            and (
                not isinstance(payload.get("target_sha"), str)
                or re.fullmatch(r"[0-9a-f]{40}", payload.get("target_sha")) is None
            )
        )
        or (
            payload.get("latest_patch_id") != "unavailable"
            and (
                not isinstance(payload.get("latest_patch_id"), str)
                or PATCH_ID_RE.fullmatch(payload.get("latest_patch_id")) is None
            )
        )
    ):
        _fail("artifact")
    for field in (
        "run_id",
        "run_attempt",
        "internal_section_count",
        "internal_api_section_count",
        "public_api_section_count",
    ):
        value = payload.get(field)
        max_value = MAX_SECTION_COUNT if field.endswith("section_count") else 10**32
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value > max_value
        ):
            _fail("artifact")
    if payload["status"] == "passed":
        if (
            payload["event"] == "unavailable"
            or payload["target_sha"] == "unavailable"
            or payload["run_id"] <= 0
            or payload["run_attempt"] <= 0
            or payload["patch_distribution_status"] != "success"
            or payload["content_status"] != "success"
            or payload["latest_patch_id"] == "unavailable"
            or min(
                payload["internal_section_count"],
                payload["internal_api_section_count"],
                payload["public_api_section_count"],
            )
            <= 0
            or len(
                {
                    payload["internal_section_count"],
                    payload["internal_api_section_count"],
                    payload["public_api_section_count"],
                }
            )
            != 1
        ):
            _fail("artifact")
    elif (
        payload["latest_patch_id"] != "unavailable"
        or payload["internal_section_count"] != 0
        or payload["internal_api_section_count"] != 0
        or payload["public_api_section_count"] != 0
    ):
        _fail("artifact")
    if os.path.lexists(path):
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            _fail("artifact")
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600, follow_symlinks=False)
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            _fail("artifact")
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> object:
    try:
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            _fail("artifact")
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _fail("remote_or_transport")
    except DiagnosticsFailure:
        raise
    except (OSError, UnicodeError, ValueError):
        _fail("producer")


def _verify_files(paths: Mapping[str, Path]) -> str:
    home = _read_json(paths["home"])
    internal = _read_json(paths["internal"])
    internal_api = _read_json(paths["internal_api"])
    public_api = _read_json(paths["public_api"])
    patch_id, section_count = validate_distribution(
        home,
        internal_detail=internal,
        internal_api_detail=internal_api,
        public_api_detail=public_api,
    )
    return passed_summary(patch_id, section_count)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--home", type=Path, required=True)
    verify.add_argument("--internal", type=Path, required=True)
    verify.add_argument("--internal-api", type=Path, required=True)
    verify.add_argument("--public-api", type=Path, required=True)

    parse = subparsers.add_parser("parse-summary")
    parse.add_argument("--file", type=Path, required=True)
    parse.add_argument("--require-passed", action="store_true")
    parse.add_argument("--exit-code", type=int, default=0)

    failure = subparsers.add_parser("failure-summary")
    failure.add_argument("--error-class", default="internal")

    evidence = subparsers.add_parser("evidence")
    evidence.add_argument("--output", type=Path, required=True)
    evidence.add_argument("--target-sha", required=True)
    evidence.add_argument("--event", required=True)
    evidence.add_argument("--run-id", required=True)
    evidence.add_argument("--run-attempt", required=True)
    evidence.add_argument("--patch-outcome", required=True)
    evidence.add_argument("--patch-summary", required=True)
    evidence.add_argument("--content-outcome", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "verify":
            line = _verify_files(
                {
                    "home": args.home,
                    "internal": args.internal,
                    "internal_api": args.internal_api,
                    "public_api": args.public_api,
                }
            )
            print(line)
            return 0
        if args.command == "parse-summary":
            with args.file.open("r", encoding="utf-8", newline="") as stream:
                text = stream.read()
            print(
                parse_summary_text(
                    text,
                    require_passed=args.require_passed,
                    exit_code=args.exit_code,
                )
            )
            return 0
        if args.command == "failure-summary":
            print(failed_summary(args.error_class))
            return 0
        payload = evidence_payload(
            target_sha=args.target_sha,
            event=args.event,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
            patch_outcome=args.patch_outcome,
            patch_summary=args.patch_summary,
            content_outcome=args.content_outcome,
        )
        _write_secure_json(args.output, payload)
        return 0
    except DiagnosticsFailure as error:
        print(failed_summary(error.error_class))
        return 1
    except Exception:
        print(failed_summary("internal"))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
