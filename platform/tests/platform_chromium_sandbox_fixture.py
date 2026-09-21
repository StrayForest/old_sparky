"""Load the reviewed Chromium sandbox helper used by artifact fixtures.

The byte payload was copied from the reviewed Chrome for Testing
``149.0.7827.55`` Linux ``chromium-1228`` runtime and matches the production
size/hash contract.  Its upstream ``chrome-linux64/ABOUT`` identifies Google
Chrome and the Chromium open-source project; the fixture is only the small
helper, not a browser distribution.  It is kept as deterministic,
gzip-compressed base64 text so the test suite does not depend on a host
live-QA cache.  It is the ordinary (non-set-id) source payload; archive tests
apply the production ``04755`` TarInfo contract only at the archive boundary.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
import zlib


FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "chromium_sandbox_1228.gz.b64"
)
EXPECTED_SIZE = 15_232
EXPECTED_SHA256 = "4f21eddabe22d24f83b907f9404cb331135acf2d5064292aed106c7794578cb3"
EXPECTED_COMPRESSED_SHA256 = (
    "65a69829cb252ce4519c713beda3862ced82ef61e351ad2c75ca03972b0271f0"
)
MAX_ENCODED_BYTES = 64 * 1024


def read_bytes() -> bytes:
    """Decode and verify the source-controlled sandbox, failing closed."""

    try:
        encoded = b"".join(FIXTURE_PATH.read_bytes().split())
    except OSError as exc:
        raise RuntimeError("Chromium sandbox fixture is unavailable") from exc
    if not encoded or len(encoded) > MAX_ENCODED_BYTES:
        raise RuntimeError("Chromium sandbox fixture encoding is unsafe")
    try:
        compressed = base64.b64decode(encoded, validate=True)
        if hashlib.sha256(compressed).hexdigest() != EXPECTED_COMPRESSED_SHA256:
            raise ValueError("compressed payload digest mismatch")
        decoder = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
        decoded = decoder.decompress(compressed, EXPECTED_SIZE + 1)
        decoded += decoder.flush()
    except (ValueError, zlib.error) as exc:
        raise RuntimeError("Chromium sandbox fixture encoding is invalid") from exc
    if (
        not decoder.eof
        or decoder.unused_data
        or decoder.unconsumed_tail
        or len(decoded) != EXPECTED_SIZE
        or hashlib.sha256(decoded).hexdigest() != EXPECTED_SHA256
    ):
        raise RuntimeError("Chromium sandbox fixture contract is invalid")
    return decoded
