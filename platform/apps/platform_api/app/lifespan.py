from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from apps.platform_api.app.services.bracket_events import dispose_bracket_event_relays
from apps.platform_api.app.services.ready_check_events import dispose_ready_check_event_relay
from python_packages.platform_infra.db import dispose_engine, warm_up_engine
from python_packages.platform_infra.logging import configure_logging
from python_packages.platform_infra.redis import dispose_redis, warm_up_redis


@asynccontextmanager
async def platform_lifespan(_: FastAPI):
    configure_logging()
    await warm_up_engine()
    await warm_up_redis()
    try:
        yield
    finally:
        await dispose_bracket_event_relays()
        await dispose_ready_check_event_relay()
        await dispose_redis()
        await dispose_engine()
