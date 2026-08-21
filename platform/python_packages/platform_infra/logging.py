from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
import re

from python_packages.platform_infra.config import get_settings


SENSITIVE_VALUE_RE = re.compile(
    r"(?i)\b(password|authorization|cookie|csrf(?:[_-]?token)?|turnstile(?:[_-]?token)?|"
    r"session(?:[_-]?token)?|reset(?:[_-]?token)?|access[_-]?key|secret(?:[_-]?key)?)"
    r"\s*[=:]\s*([^\s,;]+)"
)


def redact_log_text(value: str) -> str:
    return SENSITIVE_VALUE_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)


class JsonUtcFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat().replace(
                "+00:00", "Z"
            ),
            "service": "deadlock-platform",
            "level": record.levelname,
            "logger": record.name,
            "message": redact_log_text(record.getMessage()),
        }
        if record.exc_info:
            payload["exception"] = redact_log_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging() -> None:
    settings = get_settings()
    level = getattr(logging, settings.platform_log_level.upper(), logging.INFO)
    if settings.platform_environment.strip().lower() == "production":
        handler = logging.StreamHandler()
        handler.setFormatter(JsonUtcFormatter())
        logging.basicConfig(level=level, handlers=[handler], force=True)
    else:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
            force=True,
        )
