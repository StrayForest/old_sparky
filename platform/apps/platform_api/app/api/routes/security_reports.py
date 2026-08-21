from __future__ import annotations

import json
import logging
import math
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Request, Response, status


router = APIRouter()
logger = logging.getLogger("platform.security.csp")
MAX_REPORT_BYTES = 32 * 1024
MAX_REPORT_BATCH = 8
MAX_FIELD_LENGTH = 512
_BROWSER_EXTENSION_SCHEMES = {
    "chrome-extension",
    "moz-extension",
    "ms-browser-extension",
    "safari-extension",
    "safari-web-extension",
}
_LOG_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_CSP_DIRECTIVE_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_CSP_DISPOSITIONS = {"enforce", "report"}


def _safe_url(value: object) -> str:
    candidate = str(value or "").strip()[:MAX_FIELD_LENGTH]
    lowered = candidate.lower()
    if lowered in {"inline", "'inline'"}:
        return "inline"
    if lowered in {"eval", "'eval'"}:
        return "eval"
    if lowered == "blob" or lowered.startswith("blob:"):
        return "blob"
    if lowered == "data" or lowered.startswith("data:"):
        return "data"
    try:
        parsed = urlsplit(candidate)
        scheme = parsed.scheme.lower()
        if scheme in _BROWSER_EXTENSION_SCHEMES:
            return "browser-extension"
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return "invalid"
    if scheme not in {"http", "https"} or not hostname:
        return "invalid"
    safe_hostname = f"[{hostname}]" if ":" in hostname else hostname
    safe_netloc = safe_hostname if port is None else f"{safe_hostname}:{port}"
    return urlunsplit((scheme, safe_netloc, parsed.path[:256], "", ""))


def _safe_text(value: object, *, limit: int = 128) -> str:
    return " ".join(str(value or "").split())[:limit]


def _safe_directive(value: object) -> str:
    candidate = _safe_text(value, limit=128).lower().split(" ", 1)[0]
    return candidate if _CSP_DIRECTIVE_PATTERN.fullmatch(candidate) else "invalid"


def _safe_disposition(value: object) -> str:
    candidate = _safe_text(value, limit=32).lower()
    return candidate if candidate in _CSP_DISPOSITIONS else "invalid"


def _safe_log_identifier(value: object) -> str:
    candidate = str(value or "").strip()
    if not candidate:
        return "missing"
    if not _LOG_IDENTIFIER_PATTERN.fullmatch(candidate):
        return "invalid"
    return candidate


def _safe_status_code(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return 0
    if isinstance(value, str) and not re.fullmatch(r"[0-9]+", value.strip()):
        return 0
    try:
        candidate = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return candidate if 0 <= candidate <= 999 else 0


def _report_rows(payload: object) -> list[dict[str, Any]]:
    candidates: list[object]
    if isinstance(payload, list):
        candidates = payload[:MAX_REPORT_BATCH]
    else:
        candidates = [payload]
    batch_size = len(candidates)
    rows: list[dict[str, Any]] = []
    for batch_index, candidate in enumerate(candidates, start=1):
        if not isinstance(candidate, dict):
            continue
        if "csp-report" in candidate:
            report = candidate.get("csp-report")
            report_format = "legacy"
        elif "body" in candidate:
            report = candidate.get("body")
            report_format = "reporting-api"
        else:
            report = candidate
            report_format = "reporting-api" if isinstance(payload, list) else "legacy"
        if not isinstance(report, dict):
            continue
        report_type = _safe_text(candidate.get("type"), limit=64).lower()
        if report_format == "reporting-api" and report_type not in {"", "csp-violation"}:
            continue
        rows.append(
            {
                "report_format": report_format,
                "batch_index": batch_index,
                "batch_size": batch_size,
                "document_uri": _safe_url(report.get("document-uri") or report.get("documentURL")),
                "blocked_uri": _safe_url(report.get("blocked-uri") or report.get("blockedURL")),
                "effective_directive": _safe_directive(
                    report.get("effective-directive") or report.get("effectiveDirective")
                ),
                "violated_directive": _safe_directive(
                    report.get("violated-directive") or report.get("violatedDirective")
                ),
                "disposition": _safe_disposition(report.get("disposition")),
                "status_code": _safe_status_code(
                    report.get("status-code") or report.get("statusCode") or 0
                ),
                "source_file": _safe_url(report.get("source-file") or report.get("sourceFile")),
            }
        )
    return rows


@router.post("/csp-report", status_code=status.HTTP_204_NO_CONTENT, include_in_schema=False)
async def receive_csp_report(request: Request) -> Response:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            parsed_content_length = int(content_length)
            if parsed_content_length < 0:
                return Response(status_code=status.HTTP_400_BAD_REQUEST)
            if parsed_content_length > MAX_REPORT_BYTES:
                return Response(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        except ValueError:
            return Response(status_code=status.HTTP_400_BAD_REQUEST)

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_REPORT_BYTES:
            return Response(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
    try:
        payload = json.loads(body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    rows = _report_rows(payload)
    request_id = _safe_log_identifier(request.headers.get("x-request-id"))
    cf_ray = _safe_log_identifier(request.headers.get("cf-ray"))
    for row in rows:
        logger.warning(
            "csp_report request_id=%s cf_ray=%s report_format=%s "
            "batch_position=%s/%s row_count=%s document_uri=%s blocked_uri=%s effective_directive=%s "
            "violated_directive=%s disposition=%s status_code=%s source_file=%s",
            request_id,
            cf_ray,
            row["report_format"],
            row["batch_index"],
            row["batch_size"],
            len(rows),
            row["document_uri"],
            row["blocked_uri"],
            row["effective_directive"],
            row["violated_directive"],
            row["disposition"],
            row["status_code"],
            row["source_file"],
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
