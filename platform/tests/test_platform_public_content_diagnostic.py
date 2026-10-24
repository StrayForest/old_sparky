from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
import io
import json
from threading import Lock
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from tools import platform_public_content_diagnostic as diagnostic

PATCH_IDS = ("101", "102", "103", "104")
HERO_PATCH_IDS = (PATCH_IDS[0], PATCH_IDS[1], PATCH_IDS[3])


class PlatformPublicContentDiagnosticTests(unittest.TestCase):
    def test_sample_uses_four_cached_feed_items_and_emits_bounded_metrics(self) -> None:
        patch_rows = tuple(
            (patch_id, f"Public title {index}")
            for index, patch_id in enumerate(PATCH_IDS)
        )
        home = {
            "patches_available": True,
            "patches": [{"id": patch_id, "title": title} for patch_id, title in patch_rows],
        }
        abilities = [
            {
                "name": f"ability-{index}",
                "display_name": f"Умение {index}",
                "description": f"Описание умения {index}",
                "icon_url": f"https://assets.example/ability-{index}.webp",
            }
            for index in range(4)
        ]
        hero_section = {
            "kind": "hero",
            "hero_display_name": "Герой",
            "hero_image_url": "https://assets.example/hero.webp",
            "hero_description": "Описание героя",
            "hero_metadata_pending": False,
            "abilities": abilities,
        }
        partial_balance_section = {**hero_section, "abilities": abilities[:3]}
        details = {
            patch_id: {
                "id": patch_id,
                "title": title,
                "content": "PRIVATE_SOURCE_BODY",
                "sections": [partial_balance_section, partial_balance_section]
                if index == 2
                else [hero_section],
            }
            for index, (patch_id, title) in enumerate(patch_rows)
        }
        active = 0
        maximum_active = 0
        lock = Lock()
        calls: list[str] = []

        async def fake_request(_client: object, path: str) -> diagnostic.HttpRead:
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
                calls.append(path)
            await asyncio.sleep(0.005)
            payload = home if path == "home" else details[path.removeprefix("patches/")]
            cache_age = 300 if path == "home" else 60
            stale_age = 900 if path == "home" else 300
            result = diagnostic.HttpRead(
                path=path,
                status=200,
                duration_ms=5.0,
                response_bytes=64,
                cache_control=f"public, max-age={cache_age}, stale-while-revalidate={stale_age}",
                payload=payload,
            )
            with lock:
                active -= 1
            return result

        with (
            patch.object(diagnostic, "_request_json", side_effect=fake_request),
            patch.object(
                diagnostic,
                "_cache_coverage",
                new=AsyncMock(return_value=(True, 4)),
            ),
        ):
            report = asyncio.run(
                diagnostic.run_diagnostic(
                    expected_home_patch_ids=PATCH_IDS,
                    expected_hero_patch_ids=HERO_PATCH_IDS,
                )
            )

        self.assertEqual(report["status"], "passed")
        self.assertTrue(report["sample_only"])
        self.assertEqual(report["coordinated_refreshes"], 1)
        self.assertEqual(report["home"]["count"], 3)
        self.assertEqual(report["patch_details"]["count"], 12)
        self.assertEqual(report["warm_cache"]["patch_details_present"], 4)
        self.assertEqual(report["metadata"][0]["russian_abilities"], 4)
        self.assertEqual(report["metadata"][0]["ability_icons"], 4)
        self.assertEqual(report["metadata"][0]["complete_hero_sections"], 1)
        self.assertEqual(report["metadata"][2]["hero_sections"], 2)
        self.assertEqual(report["metadata"][2]["abilities"], 6)
        self.assertLessEqual(maximum_active, 4)
        self.assertEqual(calls.count("home"), 3)
        self.assertEqual(len(calls), 15)
        self.assertNotIn("PRIVATE_SOURCE_BODY", repr(report))

        details[PATCH_IDS[0]]["sections"][0]["abilities"][0][
            "icon_url"
        ] = None
        with (
            patch.object(diagnostic, "_request_json", side_effect=fake_request),
            patch.object(
                diagnostic,
                "_cache_coverage",
                new=AsyncMock(return_value=(True, 4)),
            ),
        ):
            incomplete = asyncio.run(
                diagnostic.run_diagnostic(
                    expected_home_patch_ids=PATCH_IDS,
                    expected_hero_patch_ids=HERO_PATCH_IDS,
                )
            )
        self.assertEqual(incomplete["status"], "incomplete")
        self.assertEqual(incomplete["metadata"][0]["ability_icons"], 3)

        unstable_calls = 0

        async def unstable_request(_client: object, path: str) -> diagnostic.HttpRead:
            nonlocal unstable_calls
            result = await fake_request(_client, path)
            if path == f"patches/{PATCH_IDS[0]}":
                unstable_calls += 1
                payload = dict(result.payload or {})
                sections = [dict(section) for section in payload.get("sections", [])]
                sections[0]["hero_display_name"] = f"Герой {unstable_calls}"
                payload["sections"] = sections
                return diagnostic.HttpRead(
                    path=result.path,
                    status=result.status,
                    duration_ms=result.duration_ms,
                    response_bytes=result.response_bytes,
                    cache_control=result.cache_control,
                    payload=payload,
                )
            return result

        details[PATCH_IDS[0]]["sections"][0]["abilities"][0][
            "icon_url"
        ] = "https://assets.example/ability-0.webp"
        with (
            patch.object(diagnostic, "_request_json", side_effect=unstable_request),
            patch.object(
                diagnostic,
                "_cache_coverage",
                new=AsyncMock(return_value=(True, 4)),
            ),
        ):
            unstable = asyncio.run(
                diagnostic.run_diagnostic(
                    expected_home_patch_ids=PATCH_IDS,
                    expected_hero_patch_ids=HERO_PATCH_IDS,
                )
            )
        self.assertEqual(unstable["status"], "incomplete")
        self.assertFalse(unstable["metadata"][0]["metadata_stable"])

        details[PATCH_IDS[1]]["sections"] = []
        with (
            patch.object(diagnostic, "_request_json", side_effect=fake_request),
            patch.object(
                diagnostic,
                "_cache_coverage",
                new=AsyncMock(return_value=(True, 4)),
            ),
        ):
            missing_expected_hero = asyncio.run(
                diagnostic.run_diagnostic(
                    expected_home_patch_ids=PATCH_IDS,
                    expected_hero_patch_ids=HERO_PATCH_IDS,
                )
            )
        self.assertEqual(missing_expected_hero["status"], "incomplete")
        self.assertEqual(missing_expected_hero["metadata"][1]["hero_sections"], 0)

        pass_report = {"schema": 1, "kind": "bounded_public_content_read_diagnostic", "status": "passed"}
        output = io.StringIO()
        with (
            patch.dict(
                "os.environ",
                {
                    "PLATFORM_PUBLIC_CONTENT_EXPECTED_HOME_PATCH_IDS": ",".join(PATCH_IDS),
                    "PLATFORM_PUBLIC_CONTENT_EXPECTED_HERO_PATCH_IDS": ",".join(HERO_PATCH_IDS),
                },
                clear=False,
            ),
            patch.object(diagnostic, "run_diagnostic", new=AsyncMock(return_value=pass_report)) as run_mock,
            redirect_stdout(output),
        ):
            self.assertEqual(diagnostic.main(), 0)
        run_mock.assert_awaited_once_with(
            expected_home_patch_ids=PATCH_IDS,
            expected_hero_patch_ids=HERO_PATCH_IDS,
        )
        self.assertEqual(
            json.loads(output.getvalue().removeprefix("PUBLIC_CONTENT_SAMPLE_JSON ")),
            pass_report,
        )

        incomplete_report = {**pass_report, "status": "incomplete"}
        output = io.StringIO()
        with (
            patch.dict(
                "os.environ",
                {
                    "PLATFORM_PUBLIC_CONTENT_EXPECTED_HOME_PATCH_IDS": "",
                    "PLATFORM_PUBLIC_CONTENT_EXPECTED_HERO_PATCH_IDS": "",
                },
                clear=False,
            ),
            patch.object(diagnostic, "run_diagnostic", new=AsyncMock(return_value=incomplete_report)),
            redirect_stdout(output),
        ):
            self.assertEqual(diagnostic.main(), 1)
        self.assertEqual(
            json.loads(output.getvalue().removeprefix("PUBLIC_CONTENT_SAMPLE_JSON ")),
            incomplete_report,
        )

        output = io.StringIO()
        with (
            patch.dict(
                "os.environ",
                {
                    "PLATFORM_PUBLIC_CONTENT_EXPECTED_HOME_PATCH_IDS": "invalid;manifest",
                    "PLATFORM_PUBLIC_CONTENT_EXPECTED_HERO_PATCH_IDS": ",".join(HERO_PATCH_IDS),
                },
                clear=False,
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(diagnostic.main(), 1)
        failure_report = json.loads(output.getvalue().removeprefix("PUBLIC_CONTENT_SAMPLE_JSON "))
        self.assertEqual(failure_report["status"], "failed")
        self.assertEqual(failure_report["error_class"], "target_manifest_invalid")

    def test_home_feed_rejects_duplicate_or_non_numeric_patch_ids(self) -> None:
        with self.assertRaisesRegex(ValueError, "home_patch_identity_invalid"):
            diagnostic._patch_rows(
                {
                    "patches_available": True,
                    "patches": [
                        {"id": "10", "title": "one"},
                        {"id": "10", "title": "duplicate"},
                        {"id": "12", "title": "three"},
                        {"id": "13", "title": "four"},
                    ],
                },
            )
        with self.assertRaisesRegex(ValueError, "target_patch_set_changed"):
            diagnostic._patch_rows(
                {
                    "patches_available": True,
                    "patches": [
                        {"id": "10", "title": "one"},
                        {"id": "11", "title": "two"},
                        {"id": "12", "title": "three"},
                        {"id": "13", "title": "four"},
                    ],
                },
                expected_patch_ids=("10", "11", "12", "14"),
            )
        with self.assertRaisesRegex(ValueError, "home_patch_identity_invalid"):
            diagnostic._patch_rows(
                {
                    "patches_available": True,
                    "patches": [
                        {"id": "10", "title": "one"},
                        {"id": "bad/path", "title": "two"},
                        {"id": "12", "title": "three"},
                        {"id": "13", "title": "four"},
                    ],
                }
            )

    def test_http_reader_rejects_redirects_bounds_bodies_and_total_time(self) -> None:
        self.assertTrue(
            diagnostic._cache_policy_matches(
                "public, max-age=60, stale-while-revalidate=300",
                expected_cache_age=60,
                expected_stale_age=300,
            )
        )
        for invalid_policy in (
            "public, max-age=600, stale-while-revalidate=300",
            "public, max-age=60, max-age=60, stale-while-revalidate=300",
            "public, max-age=60, stale-while-revalidate=300, no-cache",
        ):
            self.assertFalse(
                diagnostic._cache_policy_matches(
                    invalid_policy,
                    expected_cache_age=60,
                    expected_stale_age=300,
                )
            )

        async def redirect_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"Location": "https://example.invalid/"}, request=request)

        async def oversized_handler(request: httpx.Request) -> httpx.Response:
            content = b"x" * (diagnostic.MAX_RESPONSE_BYTES + 1)
            return httpx.Response(200, content=content, request=request)

        async def slow_handler(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.05)
            return httpx.Response(
                200,
                headers={"Cache-Control": "public, max-age=300"},
                json={"patches_available": True, "patches": []},
                request=request,
            )

        async def exercise() -> tuple[diagnostic.HttpRead, diagnostic.HttpRead, diagnostic.HttpRead]:
            async with httpx.AsyncClient(
                follow_redirects=False,
                transport=httpx.MockTransport(redirect_handler),
            ) as client:
                redirected = await diagnostic._request_json(client, "home")
            async with httpx.AsyncClient(
                follow_redirects=False,
                transport=httpx.MockTransport(oversized_handler),
            ) as client:
                oversized = await diagnostic._request_json(client, "home")
            with patch.object(diagnostic, "REQUEST_TIMEOUT_SECONDS", 0.001):
                async with httpx.AsyncClient(
                    follow_redirects=False,
                    transport=httpx.MockTransport(slow_handler),
                ) as client:
                    timed_out = await diagnostic._request_json(client, "home")
            return redirected, oversized, timed_out

        redirected, oversized, timed_out = asyncio.run(exercise())
        self.assertEqual((redirected.status, redirected.error_class), (302, "redirect"))
        self.assertEqual((oversized.status, oversized.error_class), (0, "response_too_large"))
        self.assertEqual((timed_out.status, timed_out.error_class), (0, "timeout"))

    def test_redis_cache_probe_has_socket_and_total_deadlines(self) -> None:
        class SlowRedis:
            closed = False

            async def get(self, _key: str) -> None:
                await asyncio.sleep(0.05)

            async def aclose(self) -> None:
                self.closed = True
                return None

        client = SlowRedis()
        with (
            patch.object(diagnostic, "from_url", return_value=client) as make_client,
            patch.object(diagnostic, "REDIS_TIMEOUT_SECONDS", 0.001),
            patch(
                "python_packages.platform_infra.config.get_settings",
                return_value=type("Settings", (), {"platform_redis_url": "redis://127.0.0.1:6379/15"})(),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "cache_probe_timeout"):
                asyncio.run(diagnostic._cache_coverage(PATCH_IDS))

        self.assertEqual(make_client.call_args.kwargs["socket_connect_timeout"], 1.0)
        self.assertEqual(make_client.call_args.kwargs["socket_timeout"], 1.0)
        self.assertTrue(client.closed)


if __name__ == "__main__":
    unittest.main()
