"""Strict, provider-pinned Steam OpenID 2.0 helpers.

The caller persists digests of the state and browser grant before redirecting
to Steam.  This module never stores or logs either raw secret.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
import hmac
import re
import secrets
from hashlib import sha256
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from python_packages.platform_infra.config import PlatformSettings


STEAM_OPENID_ENDPOINT = "https://steamcommunity.com/openid/login"
OPENID_NS = "http://specs.openid.net/auth/2.0"
# Steam historically returned an HTTP Claimed Identifier and current responses
# may use HTTPS. The provider host/path is still pinned exactly and the complete
# assertion is verified with Steam before the identifier is trusted.
STEAM_ID_CLAIMED_ID_RE = re.compile(
    r"^https?://steamcommunity\.com/openid/id/([0-9]{17})$"
)
FLOW_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{32,512}$")
MAX_OPENID_PARAMETERS = 32
MAX_OPENID_PARAMETER_VALUE_LENGTH = 4096
MAX_PROVIDER_RESPONSE_BYTES = 8192
NONCE_FUTURE_SKEW_SECONDS = 60


class SteamOpenIDError(ValueError):
    """An untrusted Steam OpenID assertion did not meet the protocol contract."""


class SteamOpenIDVerificationError(SteamOpenIDError):
    """Steam could not be reached or did not confirm an otherwise valid assertion."""


def normalize_return_path(value: str | None, *, default: str = "/") -> str:
    """Keep post-auth navigation on this site; invalid values fall back safely."""

    if not value or len(value) > 2048 or not value.startswith("/"):
        return default
    if value.startswith("//") or "\\" in value:
        return default
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        return default
    decoded_path = _percent_decode_path(parsed.path)
    if (
        not decoded_path.startswith("/")
        or decoded_path.startswith("//")
        or "\\" in decoded_path
        or any(segment in {".", ".."} for segment in decoded_path.split("/"))
    ):
        return default
    return urlunsplit(("", "", parsed.path, parsed.query, ""))


def _percent_decode_path(path: str) -> str:
    # unquote() is intentionally local to the security check: return the
    # original escaped path so application routing keeps its normal semantics.
    from urllib.parse import unquote

    return unquote(path)


def new_flow_secret() -> str:
    """Return an opaque browser/state secret suitable for a one-time flow."""

    return secrets.token_urlsafe(32)


def digest_flow_secret(
    value: str, secret_key: str, *, purpose: str = "steam-auth-flow"
) -> str:
    """Domain-separated HMAC digest for DB persistence and constant-time matching."""

    if not value or not purpose:
        raise ValueError("Flow secret and digest purpose are required.")
    return hmac.new(
        secret_key.encode("utf-8"),
        f"{purpose}:{value}".encode("utf-8"),
        sha256,
    ).hexdigest()


def flow_secret_matches(
    stored_digest: str,
    value: str,
    secret_key: str,
    *,
    purpose: str = "steam-auth-flow",
) -> bool:
    return hmac.compare_digest(
        stored_digest, digest_flow_secret(value, secret_key, purpose=purpose)
    )


def build_steam_callback_return_to(callback_url: str, state: str) -> str:
    """Append an opaque state to the exact URL Steam must return to."""

    if not FLOW_SECRET_RE.fullmatch(state):
        raise ValueError("Steam flow state has an invalid format.")
    parsed = urlsplit(callback_url)
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("Steam callback URL contains an invalid port.") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError(
            "Steam callback URL must be an absolute HTTP(S) URL without a fragment."
        )
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if any(key == "state" for key, _ in query):
        raise ValueError("Steam callback URL must not contain a state parameter.")
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path or "/",
            urlencode([*query, ("state", state)]),
            "",
        )
    )


def build_openid_authorization_url(callback_url: str, state: str) -> str:
    """Build Steam's checkid_setup redirect using a provider-pinned endpoint."""

    return_to = build_steam_callback_return_to(callback_url, state)
    parsed_return_to = urlsplit(return_to)
    realm = urlunsplit((parsed_return_to.scheme, parsed_return_to.netloc, "/", "", ""))
    query = urlencode(
        {
            "openid.ns": OPENID_NS,
            "openid.mode": "checkid_setup",
            "openid.return_to": return_to,
            "openid.realm": realm,
            "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
            "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
        }
    )
    return f"{STEAM_OPENID_ENDPOINT}?{query}"


def _openid_parameters(
    params: Mapping[str, Any] | Iterable[tuple[str, Any]],
) -> dict[str, str]:
    multi_items = getattr(params, "multi_items", None)
    raw_items = (
        multi_items()
        if callable(multi_items)
        else (params.items() if isinstance(params, Mapping) else params)
    )
    result: dict[str, str] = {}
    for key, value in raw_items:
        if not isinstance(key, str) or not isinstance(value, str):
            raise SteamOpenIDError("OpenID assertion parameters must be strings.")
        if key in result:
            raise SteamOpenIDError("OpenID assertion contains duplicate parameters.")
        # `state` is our return_to query parameter, not an OpenID field. The
        # route checks it against the persisted one-time flow separately.
        if key == "state":
            continue
        if (
            not key.startswith("openid.")
            or len(key) > 128
            or len(value) > MAX_OPENID_PARAMETER_VALUE_LENGTH
        ):
            raise SteamOpenIDError("OpenID assertion contains an invalid parameter.")
        result[key] = value
        if len(result) > MAX_OPENID_PARAMETERS:
            raise SteamOpenIDError("OpenID assertion has too many parameters.")
    return result


def _steam_id_from_assertion(
    assertion: Mapping[str, str],
    *,
    expected_return_to: str,
    now: datetime,
    nonce_max_age_seconds: int,
) -> str:
    required = {
        "openid.ns": OPENID_NS,
        "openid.mode": "id_res",
        "openid.op_endpoint": STEAM_OPENID_ENDPOINT,
        "openid.return_to": expected_return_to,
    }
    for key, expected in required.items():
        if assertion.get(key) != expected:
            raise SteamOpenIDError(
                "Steam OpenID assertion does not match the expected callback."
            )
    claimed_id = assertion.get("openid.claimed_id")
    identity = assertion.get("openid.identity")
    if not claimed_id or claimed_id != identity:
        raise SteamOpenIDError("Steam OpenID claimed identity is invalid.")
    match = STEAM_ID_CLAIMED_ID_RE.fullmatch(claimed_id)
    if not match or not _is_valid_uint64(match.group(1)):
        raise SteamOpenIDError("Steam OpenID claimed identity is invalid.")
    if not assertion.get("openid.signed") or not assertion.get("openid.sig"):
        raise SteamOpenIDError("Steam OpenID assertion is unsigned.")
    _validate_signed_fields(assertion["openid.signed"])
    _validate_response_nonce(
        assertion.get("openid.response_nonce"), now, nonce_max_age_seconds
    )
    return match.group(1)


def _is_valid_uint64(value: str) -> bool:
    return value.isascii() and value.isdigit() and 0 < int(value) <= (2**64 - 1)


def _validate_signed_fields(signed: str) -> None:
    fields = signed.split(",")
    if (
        len(fields) != len(set(fields))
        or any(not field or not re.fullmatch(r"[a-z_]+", field) for field in fields)
    ):
        raise SteamOpenIDError("Steam OpenID signed fields are invalid.")
    required = {
        "op_endpoint",
        "claimed_id",
        "identity",
        "return_to",
        "response_nonce",
        "assoc_handle",
    }
    if not required.issubset(fields):
        raise SteamOpenIDError("Steam OpenID assertion omits required signed fields.")


def _validate_response_nonce(
    value: str | None, now: datetime, max_age_seconds: int
) -> None:
    if not value or len(value) > 256:
        raise SteamOpenIDError("Steam OpenID response nonce is invalid.")
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z).+", value)
    if not match:
        raise SteamOpenIDError("Steam OpenID response nonce is invalid.")
    try:
        issued_at = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        )
    except ValueError as exc:
        raise SteamOpenIDError("Steam OpenID response nonce is invalid.") from exc
    checked_at = now if now.tzinfo else now.replace(tzinfo=UTC)
    age_seconds = (checked_at - issued_at).total_seconds()
    if age_seconds > max_age_seconds or age_seconds < -NONCE_FUTURE_SKEW_SECONDS:
        raise SteamOpenIDError("Steam OpenID response nonce is expired.")


async def verify_openid_assertion(
    params: Mapping[str, Any] | Iterable[tuple[str, Any]],
    expected_return_to: str,
    settings: PlatformSettings,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Verify a Steam response locally and with Steam's fixed check endpoint."""

    assertion = _openid_parameters(params)
    steam_id = _steam_id_from_assertion(
        assertion,
        expected_return_to=expected_return_to,
        now=datetime.now(UTC),
        nonce_max_age_seconds=settings.platform_auth_flow_ttl_minutes * 60,
    )
    verification_payload = dict(assertion)
    verification_payload["openid.mode"] = "check_authentication"
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(follow_redirects=False)
    assert client is not None
    try:
        async with client.stream(
            "POST",
            STEAM_OPENID_ENDPOINT,
            data=verification_payload,
            follow_redirects=False,
            timeout=settings.platform_steam_openid_timeout_seconds,
        ) as response:
            if response.status_code != 200 or response.history:
                raise SteamOpenIDVerificationError("Steam OpenID verification failed.")
            response_bytes = bytearray()
            async for chunk in response.aiter_bytes():
                response_bytes.extend(chunk)
                if len(response_bytes) > MAX_PROVIDER_RESPONSE_BYTES:
                    raise SteamOpenIDVerificationError(
                        "Steam OpenID verification response is too large."
                    )
    except httpx.HTTPError as exc:
        raise SteamOpenIDVerificationError(
            "Steam OpenID verification is unavailable."
        ) from exc
    finally:
        if owns_client:
            await client.aclose()
    if b"is_valid:true" not in {
        line.strip() for line in bytes(response_bytes).splitlines()
    }:
        raise SteamOpenIDVerificationError("Steam OpenID verification was rejected.")
    return steam_id