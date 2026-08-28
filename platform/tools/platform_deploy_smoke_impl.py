#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
from collections.abc import Mapping
from html.parser import HTMLParser
import ipaddress
import json
import os
import pathlib
import re
import secrets
import subprocess
import urllib.parse
from typing import Any

import asyncpg
import httpx
from sqlalchemy.engine import make_url


DEFAULT_APP_DIR = pathlib.Path("/opt/oldsparky/platform")
DEFAULT_EDGE_ORIGIN = "http://127.0.0.1"
DEFAULT_API_ORIGIN = "http://127.0.0.1:8010"
DEFAULT_WEB_ORIGIN = "http://127.0.0.1:3000"
DEFAULT_SERVICES = ("deadlock-api", "deadlock-worker", "deadlock-web", "nginx")

EXPECTED_CSP_POLICY_TEMPLATE = (
    "default-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; "
    "object-src 'none'; script-src 'self' 'nonce-{nonce}' https://challenges.cloudflare.com "
    "https://static.cloudflareinsights.com; script-src-attr 'none'; "
    "style-src 'self' 'nonce-{nonce}'; style-src-attr 'none'; "
    "img-src 'self' blob: https://cdn.old-sparky.com "
    "https://steamstore-a.akamaihd.net "
    "https://clan.fastly.steamstatic.com https://deadlock.io "
    "https://assets-bucket.deadlock-api.com https://i2.ytimg.com https://i3.ytimg.com; "
    "connect-src 'self'; "
    "frame-src https://challenges.cloudflare.com; font-src 'self'; manifest-src 'self'; "
    "media-src 'none'; worker-src 'self'; report-uri /api/v1/security/csp-report; "
    "report-to csp-endpoint"
)
EXPECTED_REPORTING_ENDPOINTS = 'csp-endpoint="/api/v1/security/csp-report"'
EXPECTED_COMMON_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
        "clipboard-write=(self)"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
}
CSP_HEADER_BY_MODE = {
    "report-only": "Content-Security-Policy-Report-Only",
    "enforce": "Content-Security-Policy",
}
CSP_HEADER_NAMES = tuple(CSP_HEADER_BY_MODE.values())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run deploy smoke checks for Old Sparky Arena.")
    parser.add_argument("--app-dir", default=str(DEFAULT_APP_DIR))
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--edge-origin", default=DEFAULT_EDGE_ORIGIN)
    parser.add_argument(
        "--edge-host",
        default=None,
        help="Optional Host header for a local virtual-host edge check.",
    )
    parser.add_argument(
        "--edge-insecure-loopback",
        action="store_true",
        help="Skip TLS verification only when --edge-origin resolves literally to loopback.",
    )
    parser.add_argument("--api-origin", default=DEFAULT_API_ORIGIN)
    parser.add_argument("--web-origin", default=DEFAULT_WEB_ORIGIN)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument(
        "--expected-csp-mode",
        choices=tuple(CSP_HEADER_BY_MODE),
        required=True,
        help="Fail closed unless HTML serves exactly the expected CSP header mode.",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser.parse_args(argv)


def validate_edge_options(args: argparse.Namespace) -> None:
    parsed = urllib.parse.urlsplit(args.edge_origin)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("--edge-origin must be an absolute HTTP(S) origin.")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("--edge-origin must not include a path, query or fragment.")
    if args.edge_host is not None:
        edge_host = args.edge_host.strip().lower().rstrip(".")
        if (
            not edge_host
            or len(edge_host) > 253
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", edge_host)
        ):
            raise ValueError("--edge-host must be a valid ASCII hostname.")
        args.edge_host = edge_host
    if args.edge_insecure_loopback:
        if parsed.scheme != "https":
            raise ValueError("--edge-insecure-loopback requires an HTTPS edge origin.")
        try:
            is_loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            is_loopback = parsed.hostname == "localhost"
        if not is_loopback:
            raise ValueError("TLS verification may be skipped only for a literal loopback origin.")


def edge_origin_is_loopback(origin: str) -> bool:
    hostname = urllib.parse.urlsplit(origin).hostname
    if hostname is None:
        return False
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return hostname.lower() == "localhost"


def load_env(path: pathlib.Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip("'").strip('"')
    return env


def check_service_active(service: str) -> dict[str, object]:
    result = subprocess.run(
        ["systemctl", "is-active", service],
        capture_output=True,
        text=True,
        check=False,
    )
    status = result.stdout.strip() or result.stderr.strip() or "unknown"
    return {"name": f"service:{service}", "ok": result.returncode == 0 and status == "active", "detail": status}


def check_release_layout(app_dir: pathlib.Path) -> list[dict[str, object]]:
    current = app_dir / "current"
    previous = app_dir / "previous"
    shared_env = app_dir / "shared/.env.platform"
    current_target = current.resolve() if current.exists() else None
    previous_target = previous.resolve() if previous.exists() else None

    results: list[dict[str, object]] = [
        {
            "name": "release_current",
            "ok": current.exists() and current_target is not None and current_target.exists(),
            "detail": str(current_target) if current.exists() else "missing",
        },
        {
            "name": "release_previous",
            "ok": previous.exists() and previous_target is not None and previous_target.exists(),
            "detail": str(previous_target) if previous.exists() else "missing",
        },
        {
            "name": "shared_env",
            "ok": shared_env.exists(),
            "detail": str(shared_env),
        },
    ]
    if current_target is not None:
        results.extend(
            [
                {
                    "name": "release_metadata",
                    "ok": (current_target / "RELEASE.json").exists(),
                    "detail": str(current_target / "RELEASE.json"),
                },
                {
                    "name": "release_standalone_server",
                    "ok": (current_target / "apps/platform_web/.next/standalone/server.js").exists(),
                    "detail": str(current_target / "apps/platform_web/.next/standalone/server.js"),
                },
                {
                    "name": "release_standalone_static",
                    "ok": (current_target / "apps/platform_web/.next/standalone/.next/static").is_dir(),
                    "detail": str(current_target / "apps/platform_web/.next/standalone/.next/static"),
                },
            ]
        )
    return results


async def check_db(database_url: str) -> dict[str, object]:
    connection = None
    try:
        url = make_url(database_url)
        connection = await asyncpg.connect(
            host=url.host,
            port=url.port or 5432,
            user=url.username,
            password=url.password,
            database=(url.database or "").lstrip("/"),
            ssl=False,
            statement_cache_size=0,
        )
        value = await connection.fetchval("SELECT 1")
        return {"name": "database_select_1", "ok": int(value) == 1, "detail": "SELECT 1"}
    except Exception as exc:  # pragma: no cover - exercised on live runtime
        return {"name": "database_select_1", "ok": False, "detail": str(exc)}
    finally:
        if connection is not None:
            await connection.close()


async def check_http_json(
    client: httpx.AsyncClient,
    name: str,
    url: str,
    *,
    expected_status: int = 200,
    expected_service: str | None = None,
    require_security_headers: bool = False,
) -> dict[str, object]:
    try:
        response = await client.get(url)
        payload: Any = None
        try:
            payload = response.json()
        except Exception:
            payload = response.text
        ok = response.status_code == expected_status
        if expected_service is not None:
            ok = ok and isinstance(payload, dict) and payload.get("service") == expected_service
        header_errors = []
        if require_security_headers:
            header_errors.extend(common_security_header_errors(response.headers))
            header_errors.extend(non_document_csp_header_errors(response.headers))
        ok = ok and not header_errors
        return {
            "name": name,
            "ok": ok,
            "detail": {
                "status_code": response.status_code,
                "body": payload,
                "security_header_errors": header_errors,
            },
        }
    except Exception as exc:  # pragma: no cover - exercised on live runtime
        return {"name": name, "ok": False, "detail": str(exc)}


async def check_next_css_asset(
    client: httpx.AsyncClient,
    name: str,
    origin: str,
    *,
    expected_status: int = 200,
    require_security_headers: bool = False,
) -> dict[str, object]:
    try:
        root_response = await client.get(f"{origin.rstrip('/')}/")
        match = re.search(r'href="(?P<href>/_next/static/(?:css|chunks)/[^"]+\.css)"', root_response.text)
        if match is None:
            return {
                "name": name,
                "ok": False,
                "detail": {"status_code": root_response.status_code, "error": "missing css href"},
            }

        css_url = urllib.parse.urljoin(origin.rstrip("/") + "/", match.group("href"))
        css_response = await client.get(css_url)
        header_errors = []
        if require_security_headers:
            header_errors.extend(common_security_header_errors(css_response.headers))
            header_errors.extend(non_document_csp_header_errors(css_response.headers))
        return {
            "name": name,
            "ok": (
                css_response.status_code == expected_status
                and ":root" in css_response.text
                and not header_errors
            ),
            "detail": {
                "status_code": css_response.status_code,
                "url": css_url,
                "security_header_errors": header_errors,
            },
        }
    except Exception as exc:  # pragma: no cover - exercised on live runtime
        return {"name": name, "ok": False, "detail": str(exc)}


def _header_values(headers: Mapping[str, str], name: str) -> list[str]:
    get_list = getattr(headers, "get_list", None)
    if callable(get_list):
        return [str(value) for value in get_list(name)]
    return [
        str(value)
        for header_name, value in headers.items()
        if header_name.lower() == name.lower()
    ]


def common_security_header_errors(headers: Mapping[str, str]) -> list[str]:
    errors: list[str] = []
    for name, expected_value in EXPECTED_COMMON_SECURITY_HEADERS.items():
        values = _header_values(headers, name)
        if not values:
            errors.append(f"missing {name}")
        elif len(values) != 1:
            errors.append(f"duplicate {name}")
        elif values[0] != expected_value:
            errors.append(f"unexpected {name}")
    return errors


def non_document_csp_header_errors(headers: Mapping[str, str]) -> list[str]:
    errors: list[str] = []
    for name in (*CSP_HEADER_NAMES, "Reporting-Endpoints"):
        if _header_values(headers, name):
            errors.append(f"unexpected {name} on non-document response")
    return errors


class _InlinePolicyParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.inline_script_nonces: list[str | None] = []
        self.style_nonces: list[str | None] = []
        self.style_attribute_count = 0
        self.event_handler_count = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized = {name.lower(): value for name, value in attrs}
        if tag.lower() == "script" and "src" not in normalized:
            self.inline_script_nonces.append(normalized.get("nonce"))
        elif tag.lower() == "style":
            self.style_nonces.append(normalized.get("nonce"))
        if "style" in normalized:
            self.style_attribute_count += 1
        self.event_handler_count += sum(
            1 for name in normalized if name.startswith("on")
        )


def _decoded_nonce_length(nonce: str) -> int | None:
    try:
        encoded = nonce.encode("ascii")
        padding = b"=" * (-len(encoded) % 4)
        return len(base64.b64decode(encoded + padding, altchars=b"-_", validate=True))
    except (UnicodeEncodeError, ValueError, binascii.Error):
        return None


def document_csp_header_errors(
    headers: Mapping[str, str],
    html: str,
    expected_mode: str,
) -> list[str]:
    expected_header = CSP_HEADER_BY_MODE[expected_mode]
    other_header = next(
        name for mode, name in CSP_HEADER_BY_MODE.items() if mode != expected_mode
    )
    errors: list[str] = []
    values = _header_values(headers, expected_header)
    if not values:
        errors.append(f"missing {expected_header}")
    elif len(values) != 1:
        errors.append(f"duplicate {expected_header}")
    if _header_values(headers, other_header):
        errors.append(f"unexpected {other_header}")

    reporting_values = _header_values(headers, "Reporting-Endpoints")
    if not reporting_values:
        errors.append("missing Reporting-Endpoints")
    elif len(reporting_values) != 1:
        errors.append("duplicate Reporting-Endpoints")
    elif reporting_values[0] != EXPECTED_REPORTING_ENDPOINTS:
        errors.append("unexpected Reporting-Endpoints")

    if len(values) != 1:
        return errors

    nonce_matches = re.findall(r"'nonce-([^']+)'", values[0])
    nonce = nonce_matches[0] if nonce_matches else ""
    if not nonce or any(value != nonce for value in nonce_matches):
        errors.append("CSP nonce is missing or inconsistent")
        return errors
    decoded_length = _decoded_nonce_length(nonce)
    if decoded_length is None or decoded_length < 16:
        errors.append("CSP nonce has less than 128 bits of encoded entropy")
    if values[0] != EXPECTED_CSP_POLICY_TEMPLATE.format(nonce=nonce):
        errors.append("unexpected CSP policy")

    parser = _InlinePolicyParser()
    parser.feed(html)
    invalid_inline_scripts = sum(
        1 for tag_nonce in parser.inline_script_nonces if tag_nonce != nonce
    )
    invalid_style_tags = sum(1 for tag_nonce in parser.style_nonces if tag_nonce != nonce)
    if invalid_inline_scripts:
        errors.append(f"inline scripts without document nonce: {invalid_inline_scripts}")
    if invalid_style_tags:
        errors.append(f"style tags without document nonce: {invalid_style_tags}")
    if parser.style_attribute_count:
        errors.append(f"style attributes present: {parser.style_attribute_count}")
    if parser.event_handler_count:
        errors.append(f"inline event handlers present: {parser.event_handler_count}")
    return errors


async def check_http_document(
    client: httpx.AsyncClient,
    name: str,
    url: str,
    *,
    expected_status: int,
    expected_csp_mode: str,
    contains: str | None = None,
    require_common_security_headers: bool = False,
) -> dict[str, object]:
    try:
        response = await client.get(url)
        errors: list[str] = []
        if response.status_code != expected_status:
            errors.append(f"expected HTTP {expected_status}")
        if not response.headers.get("Content-Type", "").lower().startswith("text/html"):
            errors.append("expected an HTML document response")
        if contains is not None and contains not in response.text:
            errors.append(f"missing body text: {contains}")
        if require_common_security_headers:
            errors.extend(common_security_header_errors(response.headers))
        errors.extend(
            document_csp_header_errors(
                response.headers,
                response.text,
                expected_csp_mode,
            )
        )
        return {
            "name": name,
            "ok": not errors,
            "detail": {
                "status_code": response.status_code,
                "url": str(response.url),
                "errors": errors,
            },
        }
    except Exception as exc:  # pragma: no cover - exercised on live runtime
        return {"name": name, "ok": False, "detail": str(exc)}


async def check_csp_report_endpoint(
    client: httpx.AsyncClient,
    name: str,
    origin: str,
    *,
    expected_csp_mode: str,
) -> dict[str, object]:
    url = f"{origin.rstrip('/')}/api/v1/security/csp-report"
    payload = {
        "csp-report": {
            "document-uri": f"{origin.rstrip('/')}/__deploy-smoke-csp-report__",
            "effective-directive": "img-src",
            "violated-directive": "img-src",
            "blocked-uri": "https://csp-smoke.invalid/probe.png",
            "original-policy": "default-src 'none'",
            "disposition": "report" if expected_csp_mode == "report-only" else "enforce",
            "status-code": 200,
        }
    }
    try:
        response = await client.post(
            url,
            content=json.dumps(payload, separators=(",", ":")),
            headers={"Content-Type": "application/csp-report"},
        )
        return {
            "name": name,
            "ok": response.status_code == 204,
            "detail": {"status_code": response.status_code, "url": str(response.url)},
        }
    except Exception as exc:  # pragma: no cover - exercised on live runtime
        return {"name": name, "ok": False, "detail": str(exc)}


async def check_csp_nonce_rotation(
    client: httpx.AsyncClient,
    name: str,
    url: str,
    *,
    expected_csp_mode: str,
) -> dict[str, object]:
    header_name = CSP_HEADER_BY_MODE[expected_csp_mode]
    try:
        first, second = await asyncio.gather(client.get(url), client.get(url))
        errors: list[str] = []
        for response in (first, second):
            if response.status_code != 200:
                errors.append("nonce probe expected HTTP 200")
            errors.extend(
                document_csp_header_errors(
                    response.headers,
                    response.text,
                    expected_csp_mode,
                )
            )
        nonces: list[str] = []
        for response in (first, second):
            values = _header_values(response.headers, header_name)
            matches = re.findall(r"'nonce-([^']+)'", values[0]) if len(values) == 1 else []
            nonces.append(matches[0] if matches else "")
        if not all(nonces) or nonces[0] == nonces[1]:
            errors.append("CSP nonce did not rotate between independent GET requests")
        return {
            "name": name,
            "ok": not errors,
            "detail": {"url": url, "errors": errors},
        }
    except Exception as exc:  # pragma: no cover - exercised on live runtime
        return {"name": name, "ok": False, "detail": str(exc)}


async def check_http_security_headers(
    client: httpx.AsyncClient,
    name: str,
    url: str,
    *,
    expected_status: int,
) -> dict[str, object]:
    try:
        response = await client.get(url)
        errors = common_security_header_errors(response.headers)
        errors.extend(non_document_csp_header_errors(response.headers))
        if response.status_code != expected_status:
            errors.append(f"expected HTTP {expected_status}")
        return {
            "name": name,
            "ok": not errors,
            "detail": {
                "status_code": response.status_code,
                "url": str(response.url),
                "errors": errors,
            },
        }
    except Exception as exc:  # pragma: no cover - exercised on live runtime
        return {"name": name, "ok": False, "detail": str(exc)}


async def main() -> int:
    args = parse_args()
    validate_edge_options(args)
    app_dir = pathlib.Path(args.app_dir)
    env_file = pathlib.Path(args.env_file) if args.env_file else app_dir / "shared/.env.platform"

    env = load_env(env_file)
    merged_env = dict(os.environ)
    merged_env.update(env)

    results: list[dict[str, object]] = []
    results.extend(check_release_layout(app_dir))
    results.extend(check_service_active(service) for service in DEFAULT_SERVICES)

    required_env_keys = (
        "PLATFORM_DATABASE_URL",
        "PLATFORM_WEB_ORIGIN",
        "PLATFORM_REDIS_URL",
        "PLATFORM_CELERY_BROKER_URL",
        "PLATFORM_CELERY_RESULT_BACKEND",
    )
    for key in required_env_keys:
        results.append(
            {
                "name": f"env:{key}",
                "ok": bool(merged_env.get(key)),
                "detail": "present" if merged_env.get(key) else "missing",
            }
        )

    if merged_env.get("PLATFORM_DATABASE_URL"):
        results.append(await check_db(merged_env["PLATFORM_DATABASE_URL"]))

    timeout = httpx.Timeout(args.timeout)
    edge_headers = {"Host": args.edge_host} if args.edge_host else None
    edge_health_is_local = edge_origin_is_loopback(args.edge_origin)
    async with (
        httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client,
        httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers=edge_headers,
            verify=not args.edge_insecure_loopback,
        ) as edge_client,
    ):
        results.extend(
            await asyncio.gather(
                check_http_json(
                    client,
                    "direct_api_live",
                    f"{args.api_origin}/api/v1/health/live",
                    expected_service="deadlock-platform-api",
                ),
                check_http_json(
                    client,
                    "direct_api_ready",
                    f"{args.api_origin}/api/v1/health/ready",
                    expected_service="deadlock-platform-api",
                ),
                check_http_document(
                    client,
                    "direct_web_root",
                    f"{args.web_origin}/",
                    expected_status=200,
                    expected_csp_mode=args.expected_csp_mode,
                    contains="Old Sparky Arena",
                ),
                check_next_css_asset(
                    client,
                    "direct_web_css",
                    args.web_origin,
                ),
                check_http_json(
                    client,
                    "direct_web_api_ready",
                    f"{args.web_origin}/api/v1/health/ready",
                    expected_service="deadlock-platform-api",
                ),
                check_http_json(
                    edge_client,
                    "edge_api_ready",
                    f"{args.edge_origin}/api/v1/health/ready",
                    expected_status=200 if edge_health_is_local else 403,
                    expected_service=(
                        "deadlock-platform-api" if edge_health_is_local else None
                    ),
                    require_security_headers=True,
                ),
                check_http_document(
                    edge_client,
                    "edge_web_root",
                    f"{args.edge_origin}/",
                    expected_status=200,
                    expected_csp_mode=args.expected_csp_mode,
                    contains="Old Sparky Arena",
                    require_common_security_headers=True,
                ),
                check_http_document(
                    edge_client,
                    "edge_web_auth_login",
                    f"{args.edge_origin}/auth/login",
                    expected_status=200,
                    expected_csp_mode=args.expected_csp_mode,
                    contains="Профиль, регистрации и управление турнирами.",
                    require_common_security_headers=True,
                ),
                check_http_document(
                    edge_client,
                    "edge_web_auth_register",
                    f"{args.edge_origin}/auth/register",
                    expected_status=200,
                    expected_csp_mode=args.expected_csp_mode,
                    contains="Зарегистрируйтесь в web-платформе",
                    require_common_security_headers=True,
                ),
                check_next_css_asset(
                    edge_client,
                    "edge_web_css",
                    args.edge_origin,
                    require_security_headers=True,
                ),
                check_http_security_headers(
                    edge_client,
                    "edge_security_auth",
                    f"{args.edge_origin}/api/v1/auth/session",
                    expected_status=401,
                ),
                check_http_security_headers(
                    edge_client,
                    "edge_security_asset_cache_busted",
                    (
                        f"{args.edge_origin}/assets/main_logo/"
                        "old-sparky-arena-logo-v3.webp"
                        f"?csp-smoke={secrets.token_hex(8)}"
                    ),
                    expected_status=200,
                ),
                check_http_security_headers(
                    edge_client,
                    "edge_security_apple_icon_cache_busted",
                    (
                        f"{args.edge_origin}/apple-icon.png"
                        f"?csp-smoke={secrets.token_hex(8)}"
                    ),
                    expected_status=200,
                ),
                check_http_document(
                    edge_client,
                    "edge_security_404",
                    f"{args.edge_origin}/__platform_deploy_smoke_missing__",
                    expected_status=404,
                    expected_csp_mode=args.expected_csp_mode,
                    require_common_security_headers=True,
                ),
                check_csp_nonce_rotation(
                    edge_client,
                    "edge_csp_nonce_rotation",
                    f"{args.edge_origin}/",
                    expected_csp_mode=args.expected_csp_mode,
                ),
                check_csp_report_endpoint(
                    edge_client,
                    "edge_csp_report_endpoint",
                    args.edge_origin,
                    expected_csp_mode=args.expected_csp_mode,
                ),
            )
        )

    payload = {"ok": all(item["ok"] for item in results), "results": results}

    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for item in results:
            status = "OK" if item["ok"] else "FAIL"
            print(f"[{status}] {item['name']}: {item['detail']}")

    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
