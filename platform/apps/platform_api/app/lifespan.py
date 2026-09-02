from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from python_packages.platform_infra.db import dispose_engine, warm_up_engine
from python_packages.platform_infra.cpu_profile import ReadyVoteCpuProfiler
from python_packages.platform_infra.logging import configure_logging
from python_packages.platform_infra.ready_vote_admission import (
    start_ready_vote_admission_controller,
    stop_ready_vote_admission_controller,
)
from python_packages.platform_infra.redis import dispose_redis_clients, warm_up_redis


@asynccontextmanager
async def platform_lifespan(_: FastAPI):
    configure_logging()
    await warm_up_engine()
    await warm_up_redis()
    start_ready_vote_admission_controller()
    cpu_profiler = ReadyVoteCpuProfiler.from_environment()
    if cpu_profiler is not None:
        await cpu_profiler.start()
    try:
        yield
    finally:
        if cpu_profiler is not None:
            await cpu_profiler.stop()
        await stop_ready_vote_admission_controller()
        await dispose_redis_clients()
        await dispose_engine()
