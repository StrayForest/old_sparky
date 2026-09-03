"""Provider-pinned Google OAuth authorization-code helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import json
import re
from urllib.parse import urlencode

import httpx
from pydantic import EmailStr, TypeAdapter, ValidationError

from python_packages.platform_infra.config import PlatformSettings


GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_SCOPE = "openid email profile"
MAX_PROVIDER_RESPONSE_BYTES = 32 * 1024
GOOGLE_SUBJECT_RE = re.compile(r"^[\x21-\x7e]{1,255}$")


class GoogleOAuthError(ValueError):
    """A Google OAuth response did not meet the provider contract."""


class GoogleOAuthUnavailable(GoogleOAuthError):
    """Google could not be reached or returned an unusable response."""


@dataclass(frozen=True, slots=True)
class GoogleIdentity:
    subject: str
    email: str
    display_name: str


def build_google_authorization_url(
    callback_url: str,
    client_id: str,
    state: str,
) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": callback_url,
            "response_type": "code",
            "scope": GOOGLE_SCOPE,
            "state": state,
            "access_type": "online",
            "include_granted_scopes": "true",
            "prompt": "select_account",
        }
    )
    return f"{GOOGLE_AUTHORIZATION_ENDPOINT}?{query}"


async def _read_json_response(
    response: httpx.Response,
    *,
    expected_status: int = 200,
) -> Mapping[str, object]:
    if response.status_code != expected_status or response.history:
        raise GoogleOAuthUnavailable("Google OAuth provider response was rejected.")
    response_bytes = bytearray()
    async for chunk in response.aiter_bytes():
        response_bytes.extend(chunk)
        if len(response_bytes) > MAX_PROVIDER_RESPONSE_BYTES:
            raise GoogleOAuthUnavailable("Google OAuth provider response is too large.")
    try:
        payload = json.loads(bytes(response_bytes))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GoogleOAuthUnavailable("Google OAuth provider response is invalid.") from exc
    if not isinstance(payload, Mapping):
        raise GoogleOAuthUnavailable("Google OAuth provider response is invalid.")
    return payload


def _required_string(payload: Mapping[str, object], key: str, *, max_length: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise GoogleOAuthError("Google OAuth identity is invalid.")
    return value


def _display_name(payload: Mapping[str, object], email: str) -> str:
    raw_name = payload.get("name")
    name = " ".join(raw_name.split()) if isinstance(raw_name, str) else ""
    if len(name) < 2:
        name = email.split("@", 1)[0]
    name = name[:40].strip()
    return name if len(name) >= 2 else "Google user"


def _identity_from_userinfo(payload: Mapping[str, object]) -> GoogleIdentity:
    subject = _required_string(payload, "sub", max_length=255)
    if not GOOGLE_SUBJECT_RE.fullmatch(subject):
        raise GoogleOAuthError("Google OAuth identity is invalid.")
    raw_email = _required_string(payload, "email", max_length=254)
    try:
        email = str(TypeAdapter(EmailStr).validate_python(raw_email)).lower()
    except ValidationError as exc:
        raise GoogleOAuthError("Google OAuth identity is invalid.") from exc
    if payload.get("email_verified") is not True:
        raise GoogleOAuthError("Google email is not verified.")
    return GoogleIdentity(
        subject=subject,
        email=email,
        display_name=_display_name(payload, email),
    )


async def verify_google_authorization_code(
    code: str,
    *,
    callback_url: str,
    settings: PlatformSettings,
    client: httpx.AsyncClient | None = None,
) -> GoogleIdentity:
    """Exchange a one-time code and fetch the verified Google UserInfo record."""

    if not code or len(code) > 4096 or any(char.isspace() for char in code):
        raise GoogleOAuthError("Google authorization code is invalid.")
    client_id = (settings.platform_google_client_id or "").strip()
    client_secret = (settings.platform_google_client_secret or "").strip()
    if not client_id or not client_secret:
        raise GoogleOAuthUnavailable("Google authentication is not configured.")
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(follow_redirects=False)
    assert client is not None
    try:
        try:
            async with client.stream(
                "POST",
                GOOGLE_TOKEN_ENDPOINT,
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": callback_url,
                    "grant_type": "authorization_code",
                },
                follow_redirects=False,
                timeout=settings.platform_google_oauth_timeout_seconds,
            ) as token_response:
                token_payload = await _read_json_response(token_response)
        except httpx.HTTPError as exc:
            raise GoogleOAuthUnavailable("Google authentication is unavailable.") from exc

        access_token = _required_string(token_payload, "access_token", max_length=4096)
        token_type = token_payload.get("token_type")
        if not isinstance(token_type, str) or token_type.lower() != "bearer":
            raise GoogleOAuthError("Google OAuth token response is invalid.")
        try:
            async with client.stream(
                "GET",
                GOOGLE_USERINFO_ENDPOINT,
                headers={"Authorization": f"Bearer {access_token}"},
                follow_redirects=False,
                timeout=settings.platform_google_oauth_timeout_seconds,
            ) as userinfo_response:
                userinfo_payload = await _read_json_response(userinfo_response)
        except httpx.HTTPError as exc:
            raise GoogleOAuthUnavailable("Google authentication is unavailable.") from exc
        return _identity_from_userinfo(userinfo_payload)
    finally:
        if owns_client:
            await client.aclose()


def supported_google_params(
    params: Mapping[str, object] | Iterable[tuple[str, object]],
) -> dict[str, str]:
    """Normalize callback query values without accepting duplicate code/state."""

    multi_items = getattr(params, "multi_items", None)
    raw_items = (
        multi_items()
        if callable(multi_items)
        else (params.items() if isinstance(params, Mapping) else params)
    )
    result: dict[str, str] = {}
    for key, value in raw_items:
        if key not in {"code", "state", "error"}:
            continue
        if not isinstance(value, str) or key in result or len(value) > 4096:
            raise GoogleOAuthError("Google OAuth callback is invalid.")
        result[key] = value
    return result
