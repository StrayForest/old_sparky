#!/usr/bin/python3
"""Return one live-QA auth code from the fixed Resend sent-email API.

This helper deliberately has a tiny, root-only interface.  It never reads the
platform database, accepts an API endpoint, invokes a shell, or reports mailbox
metadata.  Success writes only the requested six-digit code to stdout.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import grp
import http.client
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import stat
import sys
import time
from typing import Callable, Mapping, Protocol
from uuid import UUID


BUNDLE_ENV_NAME = "PLATFORM_LIVE_CSP_QA_BUNDLE"
SHARED_ENV_PATH = Path("/opt/oldsparky/platform/shared/.env.platform")
RESEND_API_HOST = "api.resend.com"
RESEND_LIST_PATH = "/emails?limit=100"
RESEND_RETRIEVE_PREFIX = "/emails/"
RECEIVING_DOMAIN = "auth.old-sparky.com"

MAX_BUNDLE_BYTES = 64 * 1024
MAX_ENV_BYTES = 1024 * 1024
MAX_API_RESPONSE_BYTES = 1024 * 1024
MAX_MESSAGE_TEXT_BYTES = 256 * 1024
MAX_BUNDLE_AGE = timedelta(hours=4)
MAX_FUTURE_SKEW = timedelta(seconds=60)
POLL_TIMEOUT_SECONDS = 32.0
POLL_INTERVAL_SECONDS = 2.0
MAX_POLL_ATTEMPTS = 18
MAX_LIST_PAGES = 5
NETWORK_TIMEOUT_SECONDS = 4.0

SUBJECT_BY_PURPOSE = {
    "email-verification": "Код подтверждения Old Sparky Arena",
    "password-reset": "Код восстановления Old Sparky Arena",
}
BUNDLE_KEYS = frozenset(
    {
        "version",
        "marker",
        "created_at",
        "email",
        "password",
        "mailbox_helper",
        "roster_accounts",
    }
)
ROSTER_ACCOUNT_KEYS = frozenset({"id", "email", "password"})
MARKER_PATTERN = re.compile(r"^liveqa-[a-z0-9-]{6,56}$")
MESSAGE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
CODE_PATTERN = re.compile(r"(?<!\w)([0-9]{6})(?!\w)", re.UNICODE)
API_KEY_PATTERN = re.compile(r"^re_[A-Za-z0-9_-]{8,510}$")
GENERIC_ERROR = "Mailbox helper failed."


class MailboxHelperError(RuntimeError):
    """A deliberately detail-free helper failure."""

    def __init__(self) -> None:
        super().__init__(GENERIC_ERROR)


class ResendApiError(MailboxHelperError):
    """A retryable sent-email API or transport failure."""


@dataclass(frozen=True)
class BundleConfig:
    base_email: str
    marker: str
    created_at: datetime


@dataclass(frozen=True)
class SentEmailSummary:
    message_id: str
    created_at: datetime


class SentEmailClient(Protocol):
    def list_sent(self, *, after: str | None = None) -> object: ...

    def retrieve_sent(self, message_id: str) -> object: ...


def parse_invocation(argv: list[str]) -> str:
    if (
        len(argv) != 2
        or argv[0] != "code"
        or argv[1] not in SUBJECT_BY_PURPOSE
    ):
        raise MailboxHelperError()
    return argv[1]


def _validate_email(value: object) -> str:
    if not isinstance(value, str) or not value.isascii() or len(value) > 254:
        raise MailboxHelperError()
    if value.count("@") != 1 or any(character.isspace() for character in value):
        raise MailboxHelperError()
    local, domain = value.rsplit("@", 1)
    if (
        not local
        or not domain
        or len(local) > 64
        or len(domain) > 253
        or "." not in domain
        or domain.startswith(".")
        or domain.endswith(".")
        or ".." in domain
    ):
        raise MailboxHelperError()
    return value


def marker_email(base_email: str, marker: str) -> str:
    validated_email = _validate_email(base_email)
    if not isinstance(marker, str) or MARKER_PATTERN.fullmatch(marker) is None:
        raise MailboxHelperError()
    local, domain = validated_email.rsplit("@", 1)
    candidate = f"{local}+{marker}@{domain}".lower()
    candidate_local = candidate.rsplit("@", 1)[0]
    if len(candidate) > 254 or len(candidate_local) > 64:
        raise MailboxHelperError()
    return candidate


def parse_bundle_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
        value,
    ):
        raise MailboxHelperError()
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise MailboxHelperError() from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise MailboxHelperError()
    return parsed.astimezone(UTC)


def parse_api_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise MailboxHelperError()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MailboxHelperError() from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise MailboxHelperError()
    return parsed.astimezone(UTC)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MailboxHelperError()
        result[key] = value
    return result


def parse_bundle(raw: str, *, now: datetime) -> BundleConfig:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_BUNDLE_BYTES:
        raise MailboxHelperError()
    try:
        payload = json.loads(raw, object_pairs_hook=_unique_json_object)
    except (json.JSONDecodeError, UnicodeError, MailboxHelperError) as exc:
        raise MailboxHelperError() from exc
    if not isinstance(payload, dict) or frozenset(payload) != BUNDLE_KEYS:
        raise MailboxHelperError()
    if type(payload.get("version")) is not int or payload["version"] != 1:
        raise MailboxHelperError()

    marker = payload.get("marker")
    if not isinstance(marker, str) or MARKER_PATTERN.fullmatch(marker) is None:
        raise MailboxHelperError()
    base_email = _validate_email(payload.get("email"))
    if (
        marker in base_email.lower()
        or base_email.rsplit("@", 1)[1].lower() != RECEIVING_DOMAIN
    ):
        raise MailboxHelperError()
    marker_email(base_email, marker)
    created_at = parse_bundle_timestamp(payload.get("created_at"))
    normalized_now = _require_utc_now(now)
    if (
        created_at < normalized_now - MAX_BUNDLE_AGE
        or created_at > normalized_now + MAX_FUTURE_SKEW
    ):
        raise MailboxHelperError()

    password = payload.get("password")
    mailbox_helper = payload.get("mailbox_helper")
    roster_accounts = payload.get("roster_accounts")
    if (
        not isinstance(password, str)
        or not 10 <= len(password) <= 128
        or not isinstance(mailbox_helper, str)
        or not Path(mailbox_helper).is_absolute()
        or not isinstance(roster_accounts, list)
        or len(roster_accounts) != 13
    ):
        raise MailboxHelperError()
    seen_ids: set[str] = set()
    seen_emails: set[str] = set()
    for account in roster_accounts:
        if not isinstance(account, dict) or frozenset(account) != ROSTER_ACCOUNT_KEYS:
            raise MailboxHelperError()
        account_id = account.get("id")
        account_password = account.get("password")
        account_email = _validate_email(account.get("email"))
        try:
            canonical_account_id = (
                str(UUID(account_id)) if isinstance(account_id, str) else ""
            )
        except (ValueError, AttributeError):
            canonical_account_id = ""
        if (
            not isinstance(account_id, str)
            or canonical_account_id != account_id
            or not isinstance(account_password, str)
            or not 10 <= len(account_password) <= 128
            or account_id in seen_ids
            or account_email.lower() in seen_emails
            or account_email.lower().count(marker) != 1
        ):
            raise MailboxHelperError()
        seen_ids.add(account_id)
        seen_emails.add(account_email.lower())
    return BundleConfig(
        base_email=base_email,
        marker=marker,
        created_at=created_at,
    )


def _require_utc_now(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise MailboxHelperError()
    return value.astimezone(UTC)


def _existing_user_ids(*names: str) -> frozenset[int]:
    resolved: set[int] = set()
    for name in names:
        try:
            resolved.add(pwd.getpwnam(name).pw_uid)
        except KeyError:
            continue
    return frozenset(resolved)


def _existing_group_ids(*names: str) -> frozenset[int]:
    resolved: set[int] = set()
    for name in names:
        try:
            resolved.add(grp.getgrnam(name).gr_gid)
        except KeyError:
            continue
    return frozenset(resolved)


ALLOWED_SHARED_ENV_GROUP_GIDS = _existing_group_ids(
    "oldsparky-platform",
)


def _directory_owner_is_allowed(_path: Path, owner_uid: int) -> bool:
    return owner_uid == 0


def _validate_controlled_directory_chain(
    path: Path,
    *,
    strict_root_parent: bool,
) -> None:
    descriptor = _open_controlled_directory(
        path,
        strict_root_parent=strict_root_parent,
    )
    os.close(descriptor)


def _validate_directory_metadata(
    path: Path,
    metadata: os.stat_result,
    *,
    strict_root_parent: bool,
) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or not _directory_owner_is_allowed(path, metadata.st_uid)
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or (
            strict_root_parent
            and (metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o700)
        )
    ):
        raise MailboxHelperError()


def _open_controlled_directory(
    path: Path,
    *,
    strict_root_parent: bool,
) -> int:
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or any(part in {".", ".."} for part in path.parts[1:])
    ):
        raise MailboxHelperError()
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = -1
    current_path = Path("/")
    try:
        descriptor = os.open("/", flags)
        _validate_directory_metadata(
            current_path,
            os.fstat(descriptor),
            strict_root_parent=strict_root_parent and path == current_path,
        )
        for index, component in enumerate(path.parts[1:]):
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
            current_path /= component
            _validate_directory_metadata(
                current_path,
                os.fstat(descriptor),
                strict_root_parent=(
                    strict_root_parent and index == len(path.parts[1:]) - 1
                ),
            )
        return descriptor
    except (OSError, MailboxHelperError) as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise MailboxHelperError() from exc


def _validate_private_file_metadata(
    metadata: os.stat_result,
    *,
    allowed_modes: frozenset[int],
    max_bytes: int,
) -> None:
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or mode not in allowed_modes
        or (
            mode == 0o640
            and metadata.st_gid not in ALLOWED_SHARED_ENV_GROUP_GIDS
        )
        or metadata.st_size < 1
        or metadata.st_size > max_bytes
    ):
        raise MailboxHelperError()


def read_root_private_file(
    path: Path,
    *,
    max_bytes: int,
    allowed_modes: frozenset[int] = frozenset({0o600}),
    strict_root_parent: bool = True,
) -> str:
    if not isinstance(path, Path) or not path.is_absolute():
        raise MailboxHelperError()
    if path == Path("/") or path.name in {"", ".", ".."}:
        raise MailboxHelperError()
    parent_descriptor = _open_controlled_directory(
        path.parent,
        strict_root_parent=strict_root_parent,
    )
    descriptor = -1
    try:
        before = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        _validate_private_file_metadata(
            before,
            allowed_modes=allowed_modes,
            max_bytes=max_bytes,
        )
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
    except (OSError, MailboxHelperError) as exc:
        os.close(parent_descriptor)
        raise MailboxHelperError() from exc
    os.close(parent_descriptor)

    try:
        opened = os.fstat(descriptor)
        _validate_private_file_metadata(
            opened,
            allowed_modes=allowed_modes,
            max_bytes=max_bytes,
        )
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise MailboxHelperError()
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(raw) > max_bytes or (opened.st_dev, opened.st_ino, opened.st_size) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ):
            raise MailboxHelperError()
    except (OSError, MailboxHelperError) as exc:
        raise MailboxHelperError() from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MailboxHelperError() from exc


def load_bundle_from_environment(
    environ: Mapping[str, str],
    *,
    now: datetime,
) -> BundleConfig:
    configured = environ.get(BUNDLE_ENV_NAME)
    if not isinstance(configured, str) or not configured or "\x00" in configured:
        raise MailboxHelperError()
    path = Path(configured)
    raw = read_root_private_file(path, max_bytes=MAX_BUNDLE_BYTES)
    return parse_bundle(raw, now=now)


def parse_resend_api_key(raw: str) -> str:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_ENV_BYTES:
        raise MailboxHelperError()
    found: str | None = None
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, encoded_value = line.split("=", 1)
        if key.strip() != "PLATFORM_RESEND_API_KEY":
            continue
        if found is not None:
            raise MailboxHelperError()
        try:
            values = shlex.split(encoded_value.strip(), comments=False, posix=True)
        except ValueError as exc:
            raise MailboxHelperError() from exc
        if len(values) != 1 or API_KEY_PATTERN.fullmatch(values[0]) is None:
            raise MailboxHelperError()
        found = values[0]
    if found is None:
        raise MailboxHelperError()
    return found


def load_resend_api_key() -> str:
    raw = read_root_private_file(
        SHARED_ENV_PATH,
        max_bytes=MAX_ENV_BYTES,
        allowed_modes=frozenset({0o600, 0o640}),
        strict_root_parent=False,
    )
    return parse_resend_api_key(raw)


class ResendSentEmailClient:
    def __init__(
        self,
        api_key: str,
        *,
        connection_factory: Callable[..., http.client.HTTPSConnection] | None = None,
    ) -> None:
        if API_KEY_PATTERN.fullmatch(api_key) is None:
            raise MailboxHelperError()
        self._api_key = api_key
        self._connection_factory = connection_factory or http.client.HTTPSConnection

    def list_sent(self, *, after: str | None = None) -> object:
        if after is None:
            request_path = RESEND_LIST_PATH
        else:
            if MESSAGE_ID_PATTERN.fullmatch(after) is None:
                raise MailboxHelperError()
            request_path = f"{RESEND_LIST_PATH}&after={after}"
        return self._get_json(request_path)

    def retrieve_sent(self, message_id: str) -> object:
        if MESSAGE_ID_PATTERN.fullmatch(message_id) is None:
            raise MailboxHelperError()
        return self._get_json(f"{RESEND_RETRIEVE_PREFIX}{message_id}")

    def _get_json(self, request_path: str) -> object:
        connection = self._connection_factory(
            RESEND_API_HOST,
            443,
            timeout=NETWORK_TIMEOUT_SECONDS,
        )
        try:
            connection.request(
                "GET",
                request_path,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self._api_key}",
                    "User-Agent": "OldSparky-Live-QA/1.0",
                },
            )
            response = connection.getresponse()
            if response.status != 200:
                raise ResendApiError()
            content_length = response.getheader("Content-Length")
            if content_length is not None:
                try:
                    if int(content_length) > MAX_API_RESPONSE_BYTES:
                        raise ResendApiError()
                except ValueError as exc:
                    raise ResendApiError() from exc
            body = response.read(MAX_API_RESPONSE_BYTES + 1)
            if len(body) > MAX_API_RESPONSE_BYTES:
                raise ResendApiError()
        except ResendApiError:
            raise
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise ResendApiError() from exc
        finally:
            connection.close()
        try:
            return json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ResendApiError() from exc


def matching_summaries(
    payload: object,
    *,
    recipient: str,
    subject: str,
    not_before: datetime,
    now: datetime,
) -> tuple[SentEmailSummary, ...]:
    if not isinstance(payload, dict) or payload.get("object") != "list":
        raise MailboxHelperError()
    data = payload.get("data")
    if not isinstance(data, list) or len(data) > 100:
        raise MailboxHelperError()
    normalized_now = _require_utc_now(now)
    normalized_not_before = _require_utc_now(not_before)
    candidates: list[SentEmailSummary] = []
    for item in data:
        if not isinstance(item, dict):
            raise MailboxHelperError()
        if item.get("subject") != subject or item.get("to") != [recipient]:
            continue
        created_at = parse_api_timestamp(item.get("created_at"))
        if (
            created_at < normalized_not_before
            or created_at > normalized_now + MAX_FUTURE_SKEW
        ):
            continue
        message_id = item.get("id")
        if (
            not isinstance(message_id, str)
            or MESSAGE_ID_PATTERN.fullmatch(message_id) is None
        ):
            raise MailboxHelperError()
        candidates.append(SentEmailSummary(message_id=message_id, created_at=created_at))
    candidates.sort(
        key=lambda candidate: (candidate.created_at, candidate.message_id), reverse=True
    )
    return tuple(candidates)


def matching_summaries_across_recent_pages(
    client: SentEmailClient,
    *,
    recipient: str,
    subject: str,
    not_before: datetime,
    now: datetime,
) -> tuple[SentEmailSummary, ...]:
    normalized_not_before = _require_utc_now(not_before)
    normalized_now = _require_utc_now(now)
    matches: list[SentEmailSummary] = []
    after: str | None = None
    previous_oldest: datetime | None = None
    seen_cursors: set[str] = set()
    for _page_number in range(MAX_LIST_PAGES):
        payload = client.list_sent(after=after)
        if (
            not isinstance(payload, dict)
            or payload.get("object") != "list"
            or type(payload.get("has_more")) is not bool
        ):
            raise MailboxHelperError()
        data = payload.get("data")
        if not isinstance(data, list) or len(data) > 100:
            raise MailboxHelperError()
        page_timestamps: list[datetime] = []
        page_ids: list[str] = []
        for item in data:
            if not isinstance(item, dict):
                raise MailboxHelperError()
            message_id = item.get("id")
            if (
                not isinstance(message_id, str)
                or MESSAGE_ID_PATTERN.fullmatch(message_id) is None
            ):
                raise MailboxHelperError()
            page_ids.append(message_id)
            page_timestamps.append(parse_api_timestamp(item.get("created_at")))
        if any(
            newer < older
            for newer, older in zip(page_timestamps, page_timestamps[1:])
        ):
            raise MailboxHelperError()
        if (
            previous_oldest is not None
            and page_timestamps
            and page_timestamps[0] > previous_oldest
        ):
            raise MailboxHelperError()
        matches.extend(
            matching_summaries(
                payload,
                recipient=recipient,
                subject=subject,
                not_before=normalized_not_before,
                now=normalized_now,
            )
        )
        if len(matches) > 1:
            raise MailboxHelperError()
        has_more = payload["has_more"]
        if not has_more:
            return tuple(matches)
        if not page_ids or not page_timestamps:
            raise MailboxHelperError()
        previous_oldest = page_timestamps[-1]
        if previous_oldest < normalized_not_before:
            return tuple(matches)
        after = page_ids[-1]
        if after in seen_cursors:
            raise MailboxHelperError()
        seen_cursors.add(after)
    raise MailboxHelperError()


def extract_standalone_code(text: object) -> str:
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_MESSAGE_TEXT_BYTES:
        raise MailboxHelperError()
    matches = CODE_PATTERN.findall(text)
    if len(matches) != 1:
        raise MailboxHelperError()
    return matches[0]


def code_from_detail(
    payload: object,
    *,
    summary: SentEmailSummary,
    recipient: str,
    subject: str,
    not_before: datetime,
) -> str:
    if not isinstance(payload, dict) or payload.get("object") != "email":
        raise MailboxHelperError()
    if (
        payload.get("id") != summary.message_id
        or payload.get("to") != [recipient]
        or payload.get("subject") != subject
    ):
        raise MailboxHelperError()
    created_at = parse_api_timestamp(payload.get("created_at"))
    if created_at != summary.created_at or created_at < _require_utc_now(not_before):
        raise MailboxHelperError()
    return extract_standalone_code(payload.get("text"))


def poll_for_code(
    client: SentEmailClient,
    *,
    recipient: str,
    purpose: str,
    not_before: datetime,
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    timeout_seconds: float = POLL_TIMEOUT_SECONDS,
    interval_seconds: float = POLL_INTERVAL_SECONDS,
    max_attempts: int = MAX_POLL_ATTEMPTS,
) -> str:
    _validate_email(recipient)
    normalized_not_before = _require_utc_now(not_before)
    subject = SUBJECT_BY_PURPOSE.get(purpose)
    if (
        subject is None
        or timeout_seconds <= 0
        or interval_seconds <= 0
        or max_attempts < 1
    ):
        raise MailboxHelperError()
    deadline = monotonic() + timeout_seconds
    attempts = 0
    while attempts < max_attempts and monotonic() <= deadline:
        attempts += 1
        try:
            candidates = matching_summaries_across_recent_pages(
                client,
                recipient=recipient,
                subject=subject,
                not_before=normalized_not_before,
                now=wall_clock(),
            )
            if candidates:
                detail = client.retrieve_sent(candidates[0].message_id)
                return code_from_detail(
                    detail,
                    summary=candidates[0],
                    recipient=recipient,
                    subject=subject,
                    not_before=normalized_not_before,
                )
        except ResendApiError:
            pass
        remaining = deadline - monotonic()
        if attempts >= max_attempts or remaining <= 0:
            break
        sleep(min(interval_seconds, remaining))
    raise MailboxHelperError()


def run(
    argv: list[str],
    *,
    environ: Mapping[str, str],
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> str:
    if os.geteuid() != 0:
        raise MailboxHelperError()
    purpose = parse_invocation(argv)
    started_at = _require_utc_now(now())
    bundle = load_bundle_from_environment(environ, now=started_at)
    recipient = marker_email(bundle.base_email, bundle.marker)
    api_key = load_resend_api_key()
    client = ResendSentEmailClient(api_key)
    return poll_for_code(
        client,
        recipient=recipient,
        purpose=purpose,
        not_before=bundle.created_at,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        code = run(list(sys.argv[1:] if argv is None else argv), environ=os.environ)
    except Exception:
        print(GENERIC_ERROR, file=sys.stderr)
        return 1
    sys.stdout.write(f"{code}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
