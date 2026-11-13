#!/usr/bin/env python3
"""Run the isolated browser-auth E2E API with local-only fixtures.

This module is test support, never an application entrypoint. It refuses to
start unless the complete environment names the CI-only loopback database,
Redis databases, web/API origins, Turnstile fixture and SMTP capture.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from email import policy
from email.parser import BytesParser
import hmac
import logging
import os
from pathlib import Path
import re
import secrets
import stat
import sys
from typing import Any
from urllib.parse import parse_qs, urlsplit

PLATFORM_ROOT = Path(__file__).resolve().parents[2]
if str(PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(PLATFORM_ROOT))

FIXTURE_EMAIL = "auth-e2e@example.test"
FIXTURE_DISPLAY_NAME = "AuthE2E"
FIXTURE_REGISTRATION_PASSWORD = "AuthE2E-Registration-Password-1!"
FIXTURE_RESET_PASSWORD = "AuthE2E-Reset-Password-2!"
SITE_KEY = "auth-e2e-public-site-key"
SITE_SECRET = "auth-e2e-not-a-provider-secret"
CONTROL_ENV = "PLATFORM_AUTH_E2E_CONTROL_FILE"
CONTROL_HEADER = "x-auth-e2e-control"
MAIL_SUBJECTS = {
    "verification": "Код подтверждения Old Sparky Arena",
    "reset": "Код восстановления Old Sparky Arena",
}
CODE_PATTERN = re.compile(r"(?<![0-9])([0-9]{6})(?![0-9])")
TEST_TOKENS = frozenset(
    {
        "valid",
        "wrong-action",
        "wrong-hostname",
        "rejected",
        "provider-error",
        "missing-action",
    }
)
STARTUP_STAGE = "startup"
STARTUP_STAGES = frozenset(
    {
        "environment",
        "smtp-capture",
        "application-import",
        "settings-validation",
        "service-isolation",
        "app-creation",
        "route-registration",
        "control-file",
        "uvicorn-startup",
        "preflight",
        "startup",
    }
)


def _startup_stage(stage: str) -> None:
    global STARTUP_STAGE
    if stage not in STARTUP_STAGES:
        raise RuntimeError("unknown auth E2E startup stage")
    STARTUP_STAGE = stage
    print(f"AUTH_E2E_API_PROGRESS stage={stage}", file=sys.stderr, flush=True)


def _startup_failure_category(error: Exception) -> str:
    """Return a closed error class; never expose exception text or data."""

    if isinstance(error, ModuleNotFoundError):
        return "module-missing"
    if isinstance(error, ImportError):
        return "module-import"
    if isinstance(error, PermissionError):
        return "permission"
    if isinstance(error, FileNotFoundError):
        return "path-missing"
    if isinstance(error, OSError):
        return "os-error"
    if isinstance(error, ValueError):
        return "value-error"
    if isinstance(error, TypeError):
        return "type-error"
    if isinstance(error, NameError):
        return "name-error"
    if isinstance(error, RuntimeError):
        return "guard-or-runtime"
    return "unexpected"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _parsed_service_url(raw: str, *, schemes: set[str]):
    parsed = urlsplit(raw)
    host = (parsed.hostname or "").lower()
    _require(parsed.scheme in schemes, "auth E2E service URL scheme is unsafe")
    _require(host in {"127.0.0.1", "localhost", "::1"}, "auth E2E service host is unsafe")
    _require(not parsed.query and not parsed.fragment, "auth E2E service URL options are unsafe")
    return parsed


def validate_environment(*, require_control_file: bool = True) -> None:
    """Fail before importing application modules or opening service sockets."""

    _require(os.environ.get("PLATFORM_AUTH_E2E") == "1", "auth E2E mode is not enabled")
    _require(
        os.environ.get("CI") == "true" and os.environ.get("GITHUB_ACTIONS") == "true",
        "auth E2E is owned by the hosted CI contour",
    )
    _require(os.environ.get("PLATFORM_ENVIRONMENT") == "test", "auth E2E requires test environment")
    _require(os.environ.get("PLATFORM_ENV_FILE") == "/dev/null", "auth E2E env file is not isolated")
    _require(
        os.environ.get("PLATFORM_WEB_ORIGIN") == "http://127.0.0.1:3100",
        "auth E2E web origin is unsafe",
    )
    _require(os.environ.get("PLATFORM_API_HOST") == "127.0.0.1", "auth E2E API bind is unsafe")
    _require(os.environ.get("PLATFORM_API_PORT") == "3199", "auth E2E API port is unsafe")
    _require(os.environ.get("PLATFORM_DB_SCHEMA") == "platform", "auth E2E schema is unsafe")
    database_url = _parsed_service_url(
        os.environ.get("PLATFORM_DATABASE_URL", ""),
        schemes={"postgresql+asyncpg"},
    )
    _require(
        database_url.path.lstrip("/") == "platformdb_test"
        and database_url.port == 5432
        and database_url.username == "platform_user"
        and database_url.password == "platform_password",
        "auth E2E database target is unsafe",
    )
    for name, expected_database in (
        ("PLATFORM_REDIS_URL", "15"),
        ("PLATFORM_CELERY_BROKER_URL", "13"),
        ("PLATFORM_CELERY_RESULT_BACKEND", "14"),
    ):
        redis_url = _parsed_service_url(
            os.environ.get(name, ""), schemes={"redis", "rediss"}
        )
        _require(
            redis_url.scheme == "redis"
            and redis_url.port == 6379
            and redis_url.path.lstrip("/") == expected_database
            and redis_url.username is None
            and redis_url.password is None,
            f"auth E2E {name} target is unsafe",
        )
    _require(
        os.environ.get("PLATFORM_TURNSTILE_MODE") == "always",
        "auth E2E must exercise required verification",
    )
    _require(
        os.environ.get("PLATFORM_TURNSTILE_SITE_KEY") == SITE_KEY,
        "auth E2E site key is not the local fixture",
    )
    _require(
        os.environ.get("PLATFORM_TURNSTILE_SECRET_KEY") == SITE_SECRET,
        "auth E2E secret is not the local fixture",
    )
    _require(
        os.environ.get("PLATFORM_TURNSTILE_EXPECTED_HOSTNAME") == "127.0.0.1",
        "auth E2E hostname is unsafe",
    )
    _require(
        os.environ.get("PLATFORM_PUBLIC_REGISTRATION_ENABLED", "").lower() == "true",
        "auth E2E registration must be enabled",
    )
    _require(
        os.environ.get("PLATFORM_EMAIL_VERIFICATION_REQUIRED", "").lower() == "true",
        "auth E2E email verification must be enabled",
    )
    _require(
        os.environ.get("PLATFORM_SECRET_KEY")
        == "ci-only-auth-e2e-secret-key-with-more-than-32-chars",
        "auth E2E application key is not the CI fixture",
    )
    for name in (
        "PLATFORM_RESEND_API_KEY",
        "PLATFORM_SUPPORT_SMTP_USERNAME",
        "PLATFORM_SUPPORT_SMTP_PASSWORD",
        "PLATFORM_SUPPORT_SMTP_HOST",
        "PLATFORM_EMAIL_SENDER_EMAIL",
        "PLATFORM_GOOGLE_CLIENT_SECRET",
    ):
        _require(not os.environ.get(name, "").strip(), f"auth E2E rejects provider secret {name}")
    if require_control_file:
        control_path = Path(os.environ.get(CONTROL_ENV, ""))
        _require(control_path.is_absolute(), "auth E2E control path must be absolute")
        parent = control_path.parent
        parent_metadata = parent.lstat()
        _require(
            stat.S_ISDIR(parent_metadata.st_mode)
            and not stat.S_ISLNK(parent_metadata.st_mode)
            and parent_metadata.st_uid == os.geteuid()
            and stat.S_IMODE(parent_metadata.st_mode) == 0o700,
            "auth E2E control directory is unsafe",
        )
        _require(
            not control_path.exists() and not control_path.is_symlink(),
            "auth E2E control file already exists",
        )


async def assert_empty_services() -> None:
    """Prove the exact test-only services start empty; never flush or reset."""

    from redis.asyncio import Redis
    from sqlalchemy import func, select

    from python_packages.platform_infra.db import dispose_engine, session_factory
    from python_packages.platform_infra.models import User
    from python_packages.platform_infra.config import get_settings

    settings = get_settings()
    for redis_url in (
        settings.platform_redis_url,
        settings.platform_celery_broker_url,
        settings.platform_celery_result_backend,
    ):
        redis_client = Redis.from_url(redis_url, decode_responses=False)
        try:
            _require(
                await redis_client.dbsize() == 0,
                "auth E2E Redis database is not empty",
            )
        finally:
            await redis_client.aclose()
    async with session_factory()() as preflight_session:
        user_count = await preflight_session.scalar(
            select(func.count()).select_from(User)
        )
    await dispose_engine()
    _require(int(user_count or 0) == 0, "auth E2E database already contains users")


class LocalSmtpCapture:
    """Minimal loopback-only SMTP sink that retains message bytes in memory."""

    def __init__(self) -> None:
        self.server: asyncio.AbstractServer | None = None
        self.port = 0
        self.messages: list[tuple[str, str, bytes]] = []

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = int(self.server.sockets[0].getsockname()[1])

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        if not isinstance(peer, tuple) or peer[0] not in {"127.0.0.1", "::1"}:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            return
        sender = ""
        recipients: list[str] = []
        writer.write(b"220 auth-e2e local capture\r\n")
        await writer.drain()
        try:
            while line := await asyncio.wait_for(reader.readline(), timeout=8):
                command = line.decode("ascii", errors="ignore").strip()
                upper = command.upper()
                if upper.startswith(("EHLO ", "HELO ")):
                    writer.write(b"250 auth-e2e\r\n")
                elif upper.startswith("MAIL FROM:"):
                    sender = command[10:].strip(" <>")
                    writer.write(b"250 sender accepted\r\n")
                elif upper.startswith("RCPT TO:"):
                    recipient = command[8:].strip(" <>").lower()
                    if recipient != FIXTURE_EMAIL:
                        writer.write(b"550 recipient rejected\r\n")
                    else:
                        recipients.append(recipient)
                        writer.write(b"250 recipient accepted\r\n")
                elif upper == "DATA":
                    if not sender or recipients != [FIXTURE_EMAIL]:
                        writer.write(b"550 message scope rejected\r\n")
                    else:
                        writer.write(b"354 end with dot\r\n")
                        await writer.drain()
                        body: list[bytes] = []
                        while True:
                            data_line = await asyncio.wait_for(reader.readline(), timeout=8)
                            if not data_line:
                                raise ConnectionError("SMTP message ended early")
                            if data_line in {b".\r\n", b".\n"}:
                                break
                            if data_line.startswith(b".."):
                                data_line = data_line[1:]
                            body.append(data_line)
                        self.messages.append((sender, recipients[0], b"".join(body)))
                        writer.write(b"250 message captured\r\n")
                    sender = ""
                    recipients.clear()
                elif upper == "RSET":
                    sender = ""
                    recipients.clear()
                    writer.write(b"250 reset\r\n")
                elif upper == "NOOP":
                    writer.write(b"250 ok\r\n")
                elif upper == "QUIT":
                    writer.write(b"221 bye\r\n")
                    await writer.drain()
                    break
                else:
                    writer.write(b"502 command unsupported\r\n")
                await writer.drain()
        except (asyncio.TimeoutError, ConnectionError, OSError):
            pass
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    def code_for(self, purpose: str) -> str | None:
        subject = MAIL_SUBJECTS.get(purpose)
        if subject is None:
            return None
        matches: list[str] = []
        for _sender, recipient, raw in self.messages:
            if recipient != FIXTURE_EMAIL:
                continue
            message = BytesParser(policy=policy.default).parsebytes(raw)
            if str(message.get("Subject") or "") != subject:
                continue
            body = message.get_body(preferencelist=("plain",))
            if body is None:
                continue
            matches.extend(CODE_PATTERN.findall(body.get_content()))
        return matches[-1] if matches else None


def _control_secret_file(path: Path) -> str:
    token = secrets.token_urlsafe(32)
    raw = (token + "\n").encode("ascii")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        metadata = os.fstat(descriptor)
        _require(
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_uid == os.geteuid()
            and metadata.st_nlink == 1
            and stat.S_IMODE(metadata.st_mode) == 0o600,
            "auth E2E control file metadata is unsafe",
        )
        os.write(descriptor, raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return token


async def serve() -> None:
    _startup_stage("environment")
    validate_environment()
    _startup_stage("smtp-capture")
    smtp = LocalSmtpCapture()
    await smtp.start()
    os.environ["PLATFORM_SUPPORT_SMTP_HOST"] = "127.0.0.1"
    os.environ["PLATFORM_SUPPORT_SMTP_PORT"] = str(smtp.port)
    os.environ["PLATFORM_SUPPORT_SMTP_STARTTLS"] = "false"
    os.environ["PLATFORM_SUPPORT_SMTP_SSL"] = "false"
    os.environ["PLATFORM_EMAIL_SENDER_EMAIL"] = "Auth E2E <noreply@example.test>"
    os.environ["PLATFORM_SUPPORT_SMTP_SENDER_EMAIL"] = ""

    _startup_stage("application-import")
    from fastapi import HTTPException, Request
    from fastapi.responses import JSONResponse
    import httpx
    from sqlalchemy import delete, func, or_, select
    import uvicorn

    from apps.platform_api.app.api.routes import auth as auth_routes
    from apps.platform_api.app.main import create_app
    from apps.platform_api.app.services import auth_mail
    from python_packages.platform_infra.db import session_factory
    from python_packages.platform_infra.models import (
        AuditLog,
        EmailVerificationToken,
        PasswordResetToken,
        User,
        UserSession,
    )
    from python_packages.platform_infra.turnstile import (
        TURNSTILE_SITEVERIFY_URL,
        verify_turnstile_token,
    )

    _startup_stage("settings-validation")
    settings = __import__(
        "python_packages.platform_infra.config", fromlist=["get_settings"]
    ).get_settings()
    # The full environment guard ran before installing the local SMTP fixture.
    # Re-running it here would mistake the fixture sender address for a
    # configured provider sender.
    _require(settings.platform_environment == "test", "auth E2E settings mode changed")
    _require(settings.platform_turnstile_mode == "always", "auth E2E Turnstile mode changed")
    _require(settings.platform_turnstile_secret_key == SITE_SECRET, "auth E2E secret changed")
    _require(settings.platform_turnstile_expected_hostname == "127.0.0.1", "auth E2E host changed")
    _require(not (settings.platform_resend_api_key or "").strip(), "auth E2E Resend must stay disabled")
    _require(settings.platform_support_smtp_host == "127.0.0.1", "auth E2E SMTP host is unsafe")
    _require(settings.platform_support_smtp_port == smtp.port, "auth E2E SMTP port changed")

    # The checked test service must begin empty. Never flush shared data to make
    # the fixture fit; fail before exposing the API if ownership is ambiguous.
    _startup_stage("service-isolation")
    await assert_empty_services()

    state: dict[str, int] = {
        "siteverify_calls": 0,
        "accepted": 0,
        "rejected": 0,
        "wrong_action": 0,
        "wrong_hostname": 0,
        "missing_action": 0,
        "provider_error": 0,
    }

    async def siteverify_fixture(request: httpx.Request) -> httpx.Response:
        if str(request.url) != TURNSTILE_SITEVERIFY_URL:
            return httpx.Response(502, json={"success": False})
        form = parse_qs(request.content.decode("ascii", errors="ignore"))
        tokens = form.get("response", [])
        secrets_sent = form.get("secret", [])
        if secrets_sent != [SITE_SECRET] or len(tokens) != 1:
            return httpx.Response(200, json={"success": False})
        parts = tokens[0].split(":")
        if len(parts) != 3 or parts[0] != "auth-e2e" or parts[1] not in TEST_TOKENS:
            return httpx.Response(200, json={"success": False})
        mode, token_action = parts[1], parts[2]
        if token_action not in {"login", "register", "reset_request", "verification_resend"}:
            return httpx.Response(200, json={"success": False})
        state["siteverify_calls"] += 1
        if mode == "provider-error":
            state["provider_error"] += 1
            return httpx.Response(503, json={"success": False})
        if mode == "rejected":
            state["rejected"] += 1
            return httpx.Response(200, json={"success": False, "error-codes": ["invalid-input-response"]})
        result: dict[str, Any] = {
            "success": True,
            "hostname": "wrong.invalid" if mode == "wrong-hostname" else "127.0.0.1",
            "action": "login" if mode == "wrong-action" else token_action,
        }
        if mode == "wrong-hostname":
            state["wrong_hostname"] += 1
        elif mode == "wrong-action":
            state["wrong_action"] += 1
        elif mode == "missing-action":
            state["missing_action"] += 1
            result.pop("action", None)
        else:
            state["accepted"] += 1
        return httpx.Response(200, json=result)

    async def fixture_verify(
        token: str | None,
        *,
        expected_action: str,
        remote_ip: str | None,
        adaptive_required: bool = False,
        settings: Any = None,
    ) -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(siteverify_fixture)) as client:
            await verify_turnstile_token(
                token,
                expected_action=expected_action,
                remote_ip=remote_ip,
                adaptive_required=adaptive_required,
                settings=settings,
                client=client,
            )

    async def capture_email(settings_arg: Any, message: Any) -> None:
        """Send real app-generated MIME through the loopback SMTP sink."""
        from apps.platform_api.app.services.support_mail import _send_email_message_sync

        await asyncio.to_thread(_send_email_message_sync, settings_arg, message)

    _startup_stage("app-creation")
    auth_routes.verify_turnstile_token = fixture_verify
    auth_mail.send_email_message = capture_email
    app = create_app()
    _startup_stage("control-file")
    control_token = _control_secret_file(Path(os.environ[CONTROL_ENV]))

    def require_control(request: Request) -> None:
        remote = request.client.host if request.client else ""
        supplied = request.headers.get(CONTROL_HEADER, "")
        if remote not in {"127.0.0.1", "::1"} or not hmac.compare_digest(supplied, control_token):
            raise HTTPException(status_code=404, detail="Not found.")

    async def mailbox(purpose: str, request: Request) -> JSONResponse:
        require_control(request)
        code = smtp.code_for(purpose)
        if code is None:
            return JSONResponse(status_code=404, content={"detail": "Message unavailable."})
        return JSONResponse(content={"code": code})

    async def fixture_stats(request: Request) -> JSONResponse:
        require_control(request)
        return JSONResponse(content=dict(state))

    async def cleanup_fixture(request: Request) -> JSONResponse:
        require_control(request)
        factory = session_factory()
        async with factory() as db_session:
            user = await db_session.scalar(
                select(User).where(User.email == FIXTURE_EMAIL).with_for_update()
            )
            if user is None:
                return JSONResponse(content={"cleaned": True, "audit_actions": [], "sessions": 0})
            user_id = user.id
            actions = list(
                (
                    await db_session.scalars(
                        select(AuditLog.action)
                        .where(
                            or_(
                                AuditLog.actor_user_id == user_id,
                                (AuditLog.subject_type == "user")
                                & (AuditLog.subject_id == user_id),
                            )
                        )
                        .order_by(AuditLog.id)
                    )
                ).all()
            )
            session_count = await db_session.scalar(
                select(func.count()).select_from(UserSession).where(UserSession.user_id == user_id)
            )
            await db_session.execute(
                delete(AuditLog).where(
                    or_(
                        AuditLog.actor_user_id == user_id,
                        (AuditLog.subject_type == "user")
                        & (AuditLog.subject_id == user_id),
                    )
                )
            )
            await db_session.delete(user)
            await db_session.commit()
            residue = (
                await db_session.scalar(
                    select(func.count()).select_from(User).where(User.email == FIXTURE_EMAIL)
                )
            ) or 0
            token_residue = (
                await db_session.scalar(
                    select(func.count()).select_from(EmailVerificationToken).where(
                        EmailVerificationToken.user_id == user_id
                    )
                )
            ) or 0
            reset_residue = (
                await db_session.scalar(
                    select(func.count()).select_from(PasswordResetToken).where(
                        PasswordResetToken.user_id == user_id
                    )
                )
            ) or 0
            session_residue = (
                await db_session.scalar(
                    select(func.count()).select_from(UserSession).where(
                        UserSession.user_id == user_id
                    )
                )
            ) or 0
            audit_residue = (
                await db_session.scalar(
                    select(func.count()).select_from(AuditLog).where(
                        or_(
                            AuditLog.actor_user_id == user_id,
                            (AuditLog.subject_type == "user")
                            & (AuditLog.subject_id == user_id),
                        )
                    )
                )
            ) or 0
            if residue or token_residue or reset_residue or session_residue or audit_residue:
                await db_session.rollback()
                return JSONResponse(status_code=500, content={"detail": "Fixture cleanup failed."})
        return JSONResponse(
            content={
                "cleaned": True,
                "audit_actions": [str(action) for action in actions],
                "sessions": int(session_count or 0),
            }
        )

    # These handlers are nested so they can close over the fixture state.
    # With postponed annotations, FastAPI resolves their annotations against
    # module globals; Request and JSONResponse were imported only inside
    # serve(), so bind the concrete types before route registration.
    for endpoint in (mailbox, fixture_stats, cleanup_fixture):
        endpoint.__annotations__["request"] = Request
        endpoint.__annotations__["return"] = JSONResponse

    _startup_stage("route-registration")
    app.add_api_route("/__auth_e2e__/mail/{purpose}", mailbox, methods=["GET"], include_in_schema=False)
    app.add_api_route("/__auth_e2e__/stats", fixture_stats, methods=["GET"], include_in_schema=False)
    app.add_api_route("/__auth_e2e__/cleanup", cleanup_fixture, methods=["POST"], include_in_schema=False)

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=3199,
        log_config=None,
        access_log=False,
        log_level="critical",
        server_header=False,
    )
    server = uvicorn.Server(config)
    try:
        _startup_stage("uvicorn-startup")
        await server.serve()
        if not server.started:
            raise RuntimeError("auth E2E API server did not complete startup")
    finally:
        await smtp.close()


def main() -> int:
    logging.disable(logging.CRITICAL)
    try:
        if sys.argv[1:] == ["--preflight"]:
            _startup_stage("preflight")
            validate_environment(require_control_file=False)
            asyncio.run(assert_empty_services())
        elif not sys.argv[1:]:
            asyncio.run(serve())
        else:
            print("AUTH_E2E_API status=invalid-args", file=sys.stderr)
            return 2
    except Exception as error:
        category = _startup_failure_category(error)
        marker = (
            f"AUTH_E2E_API_STARTUP status=failed stage={STARTUP_STAGE} "
            f"category={category}"
        )
        print(marker, file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
