from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "tools" / "platform_live_qa_mailbox_helper.py"
)
SPEC = importlib.util.spec_from_file_location(
    "platform_live_qa_mailbox_helper",
    SCRIPT_PATH,
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
CREATED_AT = NOW - timedelta(seconds=10)
MARKER = "liveqa-20260809-csp"
BASE_EMAIL = "Live.QA@auth.old-sparky.com"
RECIPIENT = "live.qa+liveqa-20260809-csp@auth.old-sparky.com"
API_KEY = "re_1234567890abcdef"
MESSAGE_ID = "a39999a6-88e3-48b1-888b-beaabcde1b33"


def bundle_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "version": 1,
        "marker": MARKER,
        "created_at": CREATED_AT.isoformat().replace("+00:00", "Z"),
        "email": BASE_EMAIL,
        "password": "PrimaryPassword9!",
        "mailbox_helper": "/opt/oldsparky/platform/shared/platform-live-qa-mailbox",
        "roster_accounts": [
            {
                "id": f"00000000-0000-4000-8000-{index:012d}",
                "email": f"roster-{index:02d}+{MARKER}@example.com",
                "password": f"RosterPassword{index:02d}!",
            }
            for index in range(13)
        ],
    }
    payload.update(updates)
    return payload


def list_payload(*items: object, has_more: bool = False) -> dict[str, object]:
    return {"object": "list", "has_more": has_more, "data": list(items)}


def summary_payload(
    *,
    message_id: str = MESSAGE_ID,
    recipient: str = RECIPIENT,
    subject: str = "Код подтверждения Old Sparky Arena",
    created_at: datetime = NOW,
) -> dict[str, object]:
    return {
        "id": message_id,
        "to": [recipient],
        "subject": subject,
        "created_at": created_at.isoformat(),
    }


def detail_payload(
    *,
    message_id: str = MESSAGE_ID,
    recipient: str = RECIPIENT,
    subject: str = "Код подтверждения Old Sparky Arena",
    created_at: datetime = NOW,
    text: object = "Код подтверждения:\n\n123456\n",
) -> dict[str, object]:
    return {
        "object": "email",
        "id": message_id,
        "to": [recipient],
        "subject": subject,
        "created_at": created_at.isoformat(),
        "text": text,
    }


class FakeClock:
    def __init__(self) -> None:
        self.elapsed = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.elapsed

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.elapsed += seconds

    def now(self) -> datetime:
        return NOW + timedelta(seconds=self.elapsed)


class FakeSentEmailClient:
    def __init__(self, listed: list[object], detail: object) -> None:
        self.listed = list(listed)
        self.detail = detail
        self.list_calls = 0
        self.after_values: list[str | None] = []
        self.retrieve_calls: list[str] = []

    def list_sent(self, *, after: str | None = None) -> object:
        self.list_calls += 1
        self.after_values.append(after)
        value = self.listed.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def retrieve_sent(self, message_id: str) -> object:
        self.retrieve_calls.append(message_id)
        return self.detail


class MailboxHelperTests(unittest.TestCase):
    def test_cli_accepts_only_exact_code_contract(self) -> None:
        self.assertEqual(
            MODULE.parse_invocation(["code", "email-verification"]),
            "email-verification",
        )
        self.assertEqual(
            MODULE.parse_invocation(["code", "password-reset"]),
            "password-reset",
        )
        for invalid in (
            [],
            ["code"],
            ["get", "email-verification", RECIPIENT],
            ["code", "other"],
            ["code", "password-reset", RECIPIENT],
        ):
            with (
                self.subTest(invalid=invalid),
                self.assertRaises(MODULE.MailboxHelperError),
            ):
                MODULE.parse_invocation(invalid)

    def test_bundle_schema_address_formula_and_recent_cutoff_are_strict(self) -> None:
        parsed = MODULE.parse_bundle(json.dumps(bundle_payload()), now=NOW)

        self.assertEqual(parsed.base_email, BASE_EMAIL)
        self.assertEqual(parsed.marker, MARKER)
        self.assertEqual(parsed.created_at, CREATED_AT)
        self.assertEqual(
            MODULE.marker_email(parsed.base_email, parsed.marker), RECIPIENT
        )

        with self.assertRaises(MODULE.MailboxHelperError):
            MODULE.parse_bundle(json.dumps(bundle_payload(extra=True)), now=NOW)
        with self.assertRaises(MODULE.MailboxHelperError):
            MODULE.parse_bundle(
                json.dumps(
                    bundle_payload(
                        created_at=(NOW - timedelta(hours=5))
                        .isoformat()
                        .replace("+00:00", "Z")
                    )
                ),
                now=NOW,
            )
        with self.assertRaises(MODULE.MailboxHelperError):
            MODULE.parse_bundle(
                json.dumps(bundle_payload(created_at=NOW.isoformat())),
                now=NOW,
            )
        invalid_roster = bundle_payload()["roster_accounts"]
        assert isinstance(invalid_roster, list)
        invalid_roster[0] = {**invalid_roster[0], "id": "NOT-A-CANONICAL-UUID"}
        with self.assertRaises(MODULE.MailboxHelperError):
            MODULE.parse_bundle(
                json.dumps(bundle_payload(roster_accounts=invalid_roster)),
                now=NOW,
            )
        with self.assertRaises(MODULE.MailboxHelperError):
            MODULE.marker_email(f"{'a' * 50}@example.com", MARKER)

    def test_recipient_is_derived_inside_helper_and_never_received_in_argv(self) -> None:
        bundle = MODULE.BundleConfig(
            base_email=BASE_EMAIL,
            marker=MARKER,
            created_at=CREATED_AT,
        )
        with (
            patch.object(MODULE.os, "geteuid", return_value=0),
            patch.object(MODULE, "load_bundle_from_environment", return_value=bundle),
            patch.object(MODULE, "load_resend_api_key", return_value=API_KEY),
            patch.object(MODULE, "ResendSentEmailClient", return_value=object()),
            patch.object(MODULE, "poll_for_code", return_value="123456") as poll,
        ):
            result = MODULE.run(
                ["code", "email-verification"],
                environ={MODULE.BUNDLE_ENV_NAME: "/root/qa.json"},
                now=lambda: NOW,
            )

        self.assertEqual(result, "123456")
        self.assertEqual(poll.call_args.kwargs["recipient"], RECIPIENT)

    def test_private_file_requires_absolute_root_only_file_and_controlled_parent(
        self,
    ) -> None:
        tests_dir = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory(dir=tests_dir) as temporary:
            directory = Path(temporary)
            path = directory / "private.json"
            path.write_text("{}", encoding="utf-8")
            path.chmod(0o600)

            self.assertEqual(MODULE.read_root_private_file(path, max_bytes=32), "{}")

            path.chmod(0o640)
            with self.assertRaises(MODULE.MailboxHelperError):
                MODULE.read_root_private_file(path, max_bytes=32)
            path.chmod(0o600)

            link = directory / "private-link.json"
            link.symlink_to(path)
            with self.assertRaises(MODULE.MailboxHelperError):
                MODULE.read_root_private_file(link, max_bytes=32)

            directory.chmod(0o770)
            with self.assertRaises(MODULE.MailboxHelperError):
                MODULE.read_root_private_file(path, max_bytes=32)
            directory.chmod(0o750)
            with self.assertRaises(MODULE.MailboxHelperError):
                MODULE.read_root_private_file(path, max_bytes=32)
            directory.chmod(0o700)

            with self.assertRaises(MODULE.MailboxHelperError):
                MODULE.read_root_private_file(Path("private.json"), max_bytes=32)

    def test_private_metadata_rejects_non_root_owner(self) -> None:
        metadata = SimpleNamespace(
            st_mode=0o100600,
            st_uid=1000,
            st_size=2,
        )
        with self.assertRaises(MODULE.MailboxHelperError):
            MODULE._validate_private_file_metadata(
                metadata,
                allowed_modes=frozenset({0o600}),
                max_bytes=32,
            )

    def test_fixed_env_permissions_reject_group_read_contour(self) -> None:
        with self.assertRaises(MODULE.MailboxHelperError):
            MODULE._validate_private_file_metadata(
                SimpleNamespace(st_mode=0o100640, st_uid=0, st_gid=0, st_size=100),
                allowed_modes=frozenset({0o600, 0o640}),
                max_bytes=1024,
            )
        for controlled_path in (
            Path("/opt/oldsparky"),
            Path("/opt/oldsparky/platform"),
        ):
            self.assertTrue(
                MODULE._directory_owner_is_allowed(controlled_path, 0)
            )
            self.assertFalse(
                MODULE._directory_owner_is_allowed(controlled_path, 1000)
            )

        for unsafe_mode in (0o100660, 0o100644):
            with (
                self.subTest(mode=oct(unsafe_mode)),
                self.assertRaises(MODULE.MailboxHelperError),
            ):
                MODULE._validate_private_file_metadata(
                    SimpleNamespace(
                        st_mode=unsafe_mode,
                        st_uid=0,
                        st_size=100,
                    ),
                    allowed_modes=frozenset({0o600, 0o640}),
                    max_bytes=1024,
                )

    def test_fixed_env_directory_chain_requires_root_owned_ancestors(
        self,
    ) -> None:
        MODULE._validate_directory_metadata(
            Path("/opt/oldsparky"),
            SimpleNamespace(st_mode=0o040755, st_uid=0),
            strict_root_parent=False,
        )
        with self.assertRaises(MODULE.MailboxHelperError):
            MODULE._validate_directory_metadata(
                Path("/opt/oldsparky"),
                SimpleNamespace(st_mode=0o040755, st_uid=1000),
                strict_root_parent=False,
            )
        with self.assertRaises(MODULE.MailboxHelperError):
            MODULE._validate_directory_metadata(
                Path("/opt/oldsparky/platform/shared"),
                SimpleNamespace(st_mode=0o040775, st_uid=0),
                strict_root_parent=False,
            )

    def test_private_file_rejects_a_symlinked_parent_component(self) -> None:
        tests_dir = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory(dir=tests_dir) as temporary:
            directory = Path(temporary)
            real_parent = directory / "real"
            real_parent.mkdir(mode=0o700)
            target = real_parent / "private.json"
            target.write_text("{}", encoding="utf-8")
            target.chmod(0o600)
            alias = directory / "alias"
            alias.symlink_to(real_parent, target_is_directory=True)

            with self.assertRaises(MODULE.MailboxHelperError):
                MODULE.read_root_private_file(alias / target.name, max_bytes=32)

    def test_live_shared_env_metadata_matches_reviewed_contour_when_present(self) -> None:
        if not MODULE.SHARED_ENV_PATH.exists():
            self.skipTest("production shared env path is absent")
        MODULE._validate_controlled_directory_chain(
            MODULE.SHARED_ENV_PATH.parent,
            strict_root_parent=False,
        )
        MODULE._validate_private_file_metadata(
            MODULE.SHARED_ENV_PATH.lstat(),
            allowed_modes=frozenset({0o600}),
            max_bytes=MODULE.MAX_ENV_BYTES,
        )

    def test_only_strict_resend_key_is_parsed_from_shared_env_content(self) -> None:
        raw = "\n".join(
            (
                "PLATFORM_DATABASE_URL=must-not-be-returned",
                f"PLATFORM_RESEND_API_KEY='{API_KEY}'",
                "PLATFORM_SECRET_KEY=must-not-be-returned",
            )
        )
        self.assertEqual(MODULE.parse_resend_api_key(raw), API_KEY)

        for invalid in (
            "PLATFORM_RESEND_API_KEY=not-a-resend-key",
            f"PLATFORM_RESEND_API_KEY={API_KEY}\nPLATFORM_RESEND_API_KEY={API_KEY}",
            f"export PLATFORM_RESEND_API_KEY={API_KEY}",
            "PLATFORM_OTHER=value",
        ):
            with (
                self.subTest(invalid=invalid),
                self.assertRaises(MODULE.MailboxHelperError),
            ):
                MODULE.parse_resend_api_key(invalid)

    def test_resend_key_loader_uses_only_fixed_shared_env_path(self) -> None:
        with patch.object(
            MODULE,
            "read_root_private_file",
            return_value=f"PLATFORM_RESEND_API_KEY={API_KEY}\n",
        ) as read_private:
            self.assertEqual(MODULE.load_resend_api_key(), API_KEY)

        read_private.assert_called_once_with(
            MODULE.SHARED_ENV_PATH,
            max_bytes=MODULE.MAX_ENV_BYTES,
            allowed_modes=frozenset({0o600, 0o640}),
            strict_root_parent=False,
        )

    def test_exact_subject_recipient_and_cutoff_sort_matching_messages(self) -> None:
        subject = MODULE.SUBJECT_BY_PURPOSE["email-verification"]
        candidates = MODULE.matching_summaries(
            list_payload(
                summary_payload(
                    message_id="old", created_at=CREATED_AT - timedelta(seconds=1)
                ),
                summary_payload(message_id="wrong-to", recipient="other@example.com"),
                summary_payload(message_id="wrong-subject", subject="Other"),
                summary_payload(
                    message_id="newer", created_at=NOW + timedelta(seconds=1)
                ),
                summary_payload(
                    message_id="newest", created_at=NOW + timedelta(seconds=2)
                ),
            ),
            recipient=RECIPIENT,
            subject=subject,
            not_before=CREATED_AT,
            now=NOW,
        )

        self.assertEqual(
            [candidate.message_id for candidate in candidates], ["newest", "newer"]
        )

    def test_code_must_be_the_only_standalone_six_digit_token(self) -> None:
        self.assertEqual(MODULE.extract_standalone_code("Код:\n123456\n"), "123456")
        for invalid in (
            "no code",
            "12345",
            "0123456",
            "abc123456def",
            "first 123456 second 654321",
            "same 123456 repeated 123456",
        ):
            with (
                self.subTest(invalid=invalid),
                self.assertRaises(MODULE.MailboxHelperError),
            ):
                MODULE.extract_standalone_code(invalid)

    def test_polling_uses_mocked_time_and_retrieves_only_matching_detail(self) -> None:
        clock = FakeClock()
        client = FakeSentEmailClient(
            [
                list_payload(),
                list_payload(summary_payload(created_at=NOW)),
            ],
            detail_payload(created_at=NOW),
        )

        code = MODULE.poll_for_code(
            client,
            recipient=RECIPIENT,
            purpose="email-verification",
            not_before=CREATED_AT,
            wall_clock=clock.now,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            timeout_seconds=10,
            interval_seconds=2,
            max_attempts=4,
        )

        self.assertEqual(code, "123456")
        self.assertEqual(client.list_calls, 2)
        self.assertEqual(client.after_values, [None, None])
        self.assertEqual(client.retrieve_calls, [MESSAGE_ID])
        self.assertEqual(clock.sleeps, [2])

    def test_polling_pages_until_cutoff_and_rejects_ambiguous_codes(self) -> None:
        older_id = "00000000-0000-4000-8000-000000000099"
        client = FakeSentEmailClient(
            [
                list_payload(summary_payload(), has_more=True),
                list_payload(
                    summary_payload(
                        message_id=older_id,
                        recipient="other@example.com",
                        created_at=CREATED_AT - timedelta(seconds=1),
                    ),
                    has_more=True,
                ),
            ],
            detail_payload(),
        )
        code = MODULE.poll_for_code(
            client,
            recipient=RECIPIENT,
            purpose="email-verification",
            not_before=CREATED_AT,
            wall_clock=lambda: NOW,
            monotonic=lambda: 0.0,
            sleep=lambda _seconds: None,
        )
        self.assertEqual(code, "123456")
        self.assertEqual(client.after_values, [None, MESSAGE_ID])

        ambiguous = FakeSentEmailClient(
            [
                list_payload(
                    summary_payload(message_id=MESSAGE_ID),
                    summary_payload(
                        message_id="00000000-0000-4000-8000-000000000002",
                        created_at=NOW - timedelta(seconds=1),
                    ),
                ),
            ],
            detail_payload(),
        )
        with self.assertRaises(MODULE.MailboxHelperError):
            MODULE.poll_for_code(
                ambiguous,
                recipient=RECIPIENT,
                purpose="email-verification",
                not_before=CREATED_AT,
                wall_clock=lambda: NOW,
                monotonic=lambda: 0.0,
                sleep=lambda _seconds: None,
            )
        self.assertEqual(ambiguous.retrieve_calls, [])

    def test_polling_is_bounded_across_transient_api_failures(self) -> None:
        clock = FakeClock()
        client = FakeSentEmailClient(
            [MODULE.ResendApiError(), MODULE.ResendApiError(), list_payload()],
            detail_payload(),
        )
        with self.assertRaises(MODULE.MailboxHelperError):
            MODULE.poll_for_code(
                client,
                recipient=RECIPIENT,
                purpose="password-reset",
                not_before=CREATED_AT,
                wall_clock=clock.now,
                monotonic=clock.monotonic,
                sleep=clock.sleep,
                timeout_seconds=3,
                interval_seconds=2,
                max_attempts=10,
            )

        self.assertEqual(client.list_calls, 3)
        self.assertEqual(clock.sleeps, [2, 1])
        self.assertEqual(clock.elapsed, 3)

    def test_http_client_is_get_only_and_pinned_to_sent_email_paths(self) -> None:
        responses = [
            list_payload(),
            list_payload(),
            detail_payload(),
        ]
        connections: list[SimpleNamespace] = []

        class Response:
            status = 200

            def __init__(self, payload: object) -> None:
                self.body = json.dumps(payload).encode("utf-8")

            def getheader(self, _name: str) -> None:
                return None

            def read(self, limit: int) -> bytes:
                return self.body[:limit]

        class Connection:
            def __init__(self, host: str, port: int, *, timeout: float) -> None:
                self.host = host
                self.port = port
                self.timeout = timeout
                self.requests: list[tuple[str, str, dict[str, str]]] = []
                self.closed = False
                self.response = Response(responses.pop(0))
                connections.append(self)

            def request(
                self, method: str, path: str, *, headers: dict[str, str]
            ) -> None:
                self.requests.append((method, path, headers))

            def getresponse(self) -> Response:
                return self.response

            def close(self) -> None:
                self.closed = True

        client = MODULE.ResendSentEmailClient(API_KEY, connection_factory=Connection)
        self.assertEqual(client.list_sent()["object"], "list")
        self.assertEqual(client.list_sent(after=MESSAGE_ID)["object"], "list")
        self.assertEqual(client.retrieve_sent(MESSAGE_ID)["object"], "email")

        self.assertEqual(len(connections), 3)
        self.assertTrue(
            all(connection.host == MODULE.RESEND_API_HOST for connection in connections)
        )
        self.assertTrue(all(connection.port == 443 for connection in connections))
        self.assertTrue(all(connection.closed for connection in connections))
        self.assertEqual(
            [
                (connection.requests[0][0], connection.requests[0][1])
                for connection in connections
            ],
            [
                ("GET", "/emails?limit=100"),
                ("GET", f"/emails?limit=100&after={MESSAGE_ID}"),
                ("GET", f"/emails/{MESSAGE_ID}"),
            ],
        )
        self.assertTrue(
            all(
                connection.requests[0][2]["Authorization"] == f"Bearer {API_KEY}"
                for connection in connections
            )
        )
        with self.assertRaises(MODULE.MailboxHelperError):
            client.retrieve_sent("../../sent-email")
        self.assertEqual(len(connections), 3)

    def test_main_outputs_only_code_or_generic_redacted_error(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(MODULE, "run", return_value="123456"),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            self.assertEqual(MODULE.main(["code", "email-verification"]), 0)
        self.assertEqual(stdout.getvalue(), "123456\n")
        self.assertEqual(stderr.getvalue(), "")

        stdout = io.StringIO()
        stderr = io.StringIO()
        sensitive = f"{RECIPIENT} {MESSAGE_ID} {API_KEY} body-content"
        with (
            patch.object(MODULE, "run", side_effect=RuntimeError(sensitive)),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            self.assertEqual(MODULE.main(["code", "email-verification"]), 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), f"{MODULE.GENERIC_ERROR}\n")
        self.assertNotIn(RECIPIENT, stderr.getvalue())
        self.assertNotIn(MESSAGE_ID, stderr.getvalue())
        self.assertNotIn(API_KEY, stderr.getvalue())

    def test_source_has_no_shell_or_database_access(self) -> None:
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertNotIn("import subprocess", source)
        self.assertNotIn("os.system", source)
        self.assertNotIn("sqlalchemy", source)
        self.assertNotIn("psycopg", source)


if __name__ == "__main__":
    unittest.main()
