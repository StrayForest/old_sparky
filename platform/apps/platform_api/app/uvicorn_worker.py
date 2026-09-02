"""Gunicorn worker with explicit, operator-selectable Uvicorn transports."""

from __future__ import annotations

import os

try:
    from uvicorn_worker import UvicornWorker as _BaseUvicornWorker
except ImportError:  # Local fallback while an older virtualenv is bootstrapped.
    from uvicorn.workers import UvicornWorker as _BaseUvicornWorker


class PlatformUvicornWorker(_BaseUvicornWorker):
    """Use auto-detected uvloop/httptools by default, classic stack for A/B."""

    CONFIG_KWARGS = {
        "loop": os.environ.get("PLATFORM_UVICORN_LOOP", "auto"),
        "http": os.environ.get("PLATFORM_UVICORN_HTTP", "auto"),
    }
