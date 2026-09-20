"""Closed public schema for translation QA producer output.

The QA runner handles patch text privately.  Only bounded counters and text
lengths cross the workflow boundary; this helper is deliberately independent
of the translation service so adversarial inputs cannot become public fields.
"""

from __future__ import annotations

from collections.abc import Mapping


SCHEMA = 1
MAX_METRIC = 1_000_000
STATUSES = frozenset({"passed", "failed"})
ERROR_CLASSES = frozenset(
    {
        "none",
        "source_missing",
        "call_budget",
        "translation_failed",
        "translation_locked",
        "translation_missing",
        "segment_contract",
        "objective_contract",
        "fact_contract",
        "locale_contract",
        "terminology_contract",
        "unit_contract",
        "internal",
        "producer",
        "remote_or_transport",
    }
)
METRIC_FIELDS = (
    "patches_checked",
    "segments_checked",
    "watched_segments",
    "translation_calls",
    "max_source_length",
    "max_translation_length",
)


def _bounded_metric(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return min(MAX_METRIC, max(0, number))


def public_summary(
    *, status: str, error_class: str, metrics: Mapping[str, object] | None = None
) -> dict[str, object]:
    """Return the only schema permitted on the translation QA public channel."""

    if not isinstance(status, str) or status not in STATUSES:
        status = "failed"
    if not isinstance(error_class, str) or error_class not in ERROR_CLASSES:
        error_class = "internal"
    source = metrics or {}
    return {
        "schema": SCHEMA,
        "kind": "translation_qa",
        "status": status,
        "error_class": error_class,
        **{field: _bounded_metric(source.get(field, 0)) for field in METRIC_FIELDS},
    }
