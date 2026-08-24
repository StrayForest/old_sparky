from __future__ import annotations

from typing import Any

import httpx


EXTERNAL_CONTENT_RESPONSE_MAX_BYTES = 8 * 1024 * 1024


class BoundedNoRedirectAsyncClient(httpx.AsyncClient):
    """HTTP client that refuses redirects and caps decoded response bodies."""

    def __init__(
        self,
        *args: Any,
        max_response_bytes: int = EXTERNAL_CONTENT_RESPONSE_MAX_BYTES,
        **kwargs: Any,
    ) -> None:
        kwargs["follow_redirects"] = False
        super().__init__(*args, **kwargs)
        self._max_response_bytes = max_response_bytes

    async def get(self, url: Any, **kwargs: Any) -> httpx.Response:
        kwargs["follow_redirects"] = False
        async with self.stream("GET", url, **kwargs) as response:
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > self._max_response_bytes:
                    raise ValueError("External content response exceeded the byte limit.")
                body.extend(chunk)

            headers = httpx.Headers(
                (name, value)
                for name, value in response.headers.multi_items()
                if name.lower() not in {"content-encoding", "content-length"}
            )
            return httpx.Response(
                status_code=response.status_code,
                headers=headers,
                content=bytes(body),
                request=response.request,
                extensions=response.extensions,
            )
