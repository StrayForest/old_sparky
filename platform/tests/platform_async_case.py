from __future__ import annotations

import unittest
from ipaddress import IPv4Address
from itertools import count

import httpx

from python_packages.platform_infra.config import PlatformSettings, get_settings
from python_packages.platform_infra.csrf import csrf_cookie_name
from python_packages.platform_infra.db import dispose_engine
from python_packages.platform_infra.redis import dispose_redis_clients


_TEST_ASGI_PEER_BASE = int(IPv4Address("127.1.0.0"))
_TEST_ASGI_PEER_SEQUENCE = count(1)


def next_test_asgi_peer() -> tuple[str, int]:
    """Give each test client a distinct loopback peer without proxy headers."""

    sequence = next(_TEST_ASGI_PEER_SEQUENCE)
    address = IPv4Address(_TEST_ASGI_PEER_BASE + sequence)
    if not address.is_loopback:
        raise RuntimeError("test ASGI peer sequence exhausted loopback range")
    return str(address), 49152 + (sequence % 16384)


def same_origin_request_headers(
    client: httpx.AsyncClient,
    *,
    settings: PlatformSettings | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build headers for one explicitly positive browser-style unsafe request."""

    current_settings = settings or get_settings()
    headers = {"Origin": current_settings.platform_web_origin}
    csrf_token = client.cookies.get(csrf_cookie_name(current_settings))
    if csrf_token:
        headers["X-CSRF-Token"] = str(csrf_token)
    if extra:
        headers.update(extra)
    return headers


class PlatformIsolatedAsyncioTestCase(unittest.IsolatedAsyncioTestCase):
    """Close process-level async infrastructure before each test loop exits."""

    def setUp(self) -> None:
        # IsolatedAsyncioTestCase enables asyncio debug mode and its generic
        # 100 ms callback threshold. Real PostgreSQL integration steps can
        # legitimately exceed that threshold; keep a five-second stall signal
        # while avoiding routine scheduler noise in the backend gate.
        assert self._asyncioRunner is not None
        self._asyncioRunner.get_loop().slow_callback_duration = 5.0

    def run(self, result: unittest.TestResult | None = None) -> unittest.TestResult:
        self.addAsyncCleanup(self._dispose_platform_resources)
        return super().run(result)

    @staticmethod
    async def _dispose_platform_resources() -> None:
        await dispose_redis_clients()
        await dispose_engine()
