"""Fetch one exact GitHub Actions artifact metadata object with bounded retries."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import socket
import sys
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_RESPONSE_BYTES = 524_288
MAX_ATTEMPTS = 4
TOTAL_DEADLINE_SECONDS = 50.0
REQUEST_TIMEOUT_SECONDS = 10.0
RETRY_DELAYS_SECONDS = (1.0, 2.0, 3.0)
TRANSIENT_HTTP_STATUSES = frozenset({404, 408, 425, 429, 500, 502, 503, 504})
API_VERSION = "2022-11-28"


class MetadataFetchError(RuntimeError):
    def __init__(self, failure_class: str, status: int, attempts: int) -> None:
        super().__init__(failure_class)
        self.failure_class = failure_class
        self.status = status
        self.attempts = attempts


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request: Request, response: Any, code: int, message: str, headers: Any, new_url: str) -> None:
        return None


def _validate_inputs(api_url: str, repository: str, artifact_id: str, token: str) -> str:
    try:
        parsed = urlsplit(api_url)
        port = parsed.port
    except ValueError:
        raise MetadataFetchError("invalid_input", 0, 0) from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.github.com"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or repository != "StrayForest/old_sparky"
        or re.fullmatch(r"[1-9][0-9]{0,31}", artifact_id) is None
        or not token
        or "\n" in token
        or "\r" in token
    ):
        raise MetadataFetchError("invalid_input", 0, 0)
    return f"https://api.github.com/repos/{repository}/actions/artifacts/{artifact_id}"


def fetch_metadata(
    *,
    api_url: str,
    repository: str,
    artifact_id: str,
    token: str,
    output_path: Path,
    opener: Callable[..., Any] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[int, int]:
    """Fetch metadata; return attempts and byte count, with closed failures only."""
    url = _validate_inputs(api_url, repository, artifact_id, token)
    open_request = opener or build_opener(_NoRedirect()).open
    started = monotonic()
    deadline = started + TOTAL_DEADLINE_SECONDS
    last_status = 0
    last_class = "network_error"

    for attempt in range(1, MAX_ATTEMPTS + 1):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise MetadataFetchError("deadline_exhausted", last_status, attempt - 1)
        request = Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
            },
        )
        try:
            with open_request(request, timeout=min(REQUEST_TIMEOUT_SECONDS, remaining)) as response:
                response_url = response.geturl()
                status = response.getcode()
                if response_url != url or status != 200:
                    raise MetadataFetchError("response_rejected", status, attempt)
                payload = response.read(MAX_RESPONSE_BYTES + 1)
                if len(payload) > MAX_RESPONSE_BYTES:
                    raise MetadataFetchError("response_oversize", status, attempt)
        except MetadataFetchError:
            raise
        except HTTPError as error:
            last_status = int(error.code)
            last_class = "transient_http" if last_status in TRANSIENT_HTTP_STATUSES else "http_rejected"
            error.close()
            if last_status not in TRANSIENT_HTTP_STATUSES:
                raise MetadataFetchError(last_class, last_status, attempt) from None
        except (URLError, TimeoutError, socket.timeout, ConnectionError, OSError):
            last_status = 0
            last_class = "network_error"
        else:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(output_path, flags, 0o600)
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("short metadata write")
                    view = view[written:]
                os.fsync(fd)
            except Exception:
                os.close(fd)
                try:
                    output_path.unlink()
                except FileNotFoundError:
                    pass
                raise
            else:
                os.close(fd)
            return attempt, len(payload)
        if attempt == MAX_ATTEMPTS:
            raise MetadataFetchError(
                "transient_http_exhausted" if last_status else "network_exhausted",
                last_status,
                attempt,
            )
        delay = RETRY_DELAYS_SECONDS[attempt - 1]
        remaining = deadline - monotonic()
        if remaining <= delay:
            raise MetadataFetchError("deadline_exhausted", last_status, attempt)
        sleep(delay)
    raise MetadataFetchError(last_class, last_status, MAX_ATTEMPTS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--artifact-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        attempts, size = fetch_metadata(
            api_url=args.api_url,
            repository=args.repository,
            artifact_id=args.artifact_id,
            token=os.environ.get("GH_TOKEN", ""),
            output_path=args.output,
        )
    except MetadataFetchError as error:
        print(
            "HOST_TOOLS_METADATA schema=1 status=failed"
            f" class={error.failure_class} http_status={error.status} attempts={error.attempts}",
            file=sys.stderr,
        )
        return 1
    except OSError:
        print(
            "HOST_TOOLS_METADATA schema=1 status=failed class=output_error http_status=0 attempts=0",
            file=sys.stderr,
        )
        return 1
    print(f"HOST_TOOLS_METADATA schema=1 status=ok attempts={attempts} bytes={size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
