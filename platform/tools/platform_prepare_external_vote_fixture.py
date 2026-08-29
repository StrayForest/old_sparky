#!/usr/bin/env python3
"""Prepare a retained Ready Check vote fixture for an external HTTP runner.

Only deterministic fixture setup runs on the origin.  The returned manifest is
temporary credential material and must be copied only to the external runner's
private temporary directory; it is never part of a report or an artifact.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from platform_production_qa import ProductionQa, QaFailure, dispose_engine, load_env_file


PUBLIC_ORIGIN = "https://old-sparky.com"
LOCAL_API_ORIGIN = "http://127.0.0.1:8010"
MIN_USERS_PER_TOURNAMENT = 14
MAX_USERS_PER_TOURNAMENT = 500
MAX_TOURNAMENTS = 40
MAX_USERS = MAX_USERS_PER_TOURNAMENT * MAX_TOURNAMENTS


def atomic_write_json(path: Path, payload: dict[str, Any], *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(payload, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, mode)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare one exact retained Ready Check fixture for an external runner."
    )
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--origin", default=PUBLIC_ORIGIN)
    parser.add_argument("--local-origin", default=LOCAL_API_ORIGIN)
    parser.add_argument("--report-path", type=Path, required=True)
    parser.add_argument("--manifest-path", type=Path, required=True)
    parser.add_argument("--tournament-count", type=int, default=1)
    parser.add_argument("--users-per-tournament", type=int, default=500)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--http-timeout", type=float, default=30.0)
    return parser.parse_args()


async def prepare(args: argparse.Namespace) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise QaFailure("external production fixture preparation must run as root")
    if args.origin.rstrip("/") != PUBLIC_ORIGIN:
        raise QaFailure("external fixture preparation requires the canonical public origin")
    if args.local_origin.rstrip("/") != LOCAL_API_ORIGIN:
        raise QaFailure("external fixture preparation requires the loopback API origin")
    if not 1 <= args.tournament_count <= MAX_TOURNAMENTS:
        raise QaFailure("tournament-count is outside the supported bound")
    if not MIN_USERS_PER_TOURNAMENT <= args.users_per_tournament <= MAX_USERS_PER_TOURNAMENT:
        raise QaFailure("users-per-tournament is outside the supported bound")
    if not 1 <= args.concurrency <= 256:
        raise QaFailure("concurrency is outside the supported bound")
    total_users = args.tournament_count * args.users_per_tournament
    if total_users > MAX_USERS:
        raise QaFailure("external fixture exceeds the supported user bound")

    load_env_file(args.env_file)
    qa = ProductionQa(
        origin=args.origin,
        request_origin=args.origin,
        report_path=args.report_path,
        keep_data=True,
        http_timeout=args.http_timeout,
        mode="write-burst",
        scale_users=total_users,
        concurrency=args.concurrency,
        http_max_connections=max(100, args.concurrency),
        collect_performance=False,
        write_burst_profile="single-ready",
        write_burst_users_per_tournament=args.users_per_tournament,
    )
    # The write-burst constructor normally sizes one single-ready fixture.
    # This preparation mode deliberately creates a multi-tournament cohort.
    qa.scale_users = total_users
    qa.report["requested_users"] = total_users
    qa.report["external_vote"] = {
        "tournament_count": args.tournament_count,
        "users_per_tournament": args.users_per_tournament,
        "preparation_origin": args.local_origin,
        "measurement_origin": args.origin,
    }
    api_client = await qa.new_client(args.local_origin)
    try:
        await qa.record_preprod_run(status="running", requested_users=total_users)
        users = await qa.bulk_register_scale_users()
        qa.preseed_csrf_tokens(users)
        qa.scenario(
            "external_vote_fixture_users_created",
            len(users) == total_users,
            {"users": len(users), "expected": total_users},
        )

        chunks = [
            users[index : index + args.users_per_tournament]
            for index in range(0, total_users, args.users_per_tournament)
        ]
        await qa.grant_tournament_permissions(
            [chunk[0] for chunk in chunks],
            [],
        )
        tournament_entries: list[dict[str, Any]] = []
        manifest_users: list[dict[str, str]] = []
        for index, chunk in enumerate(chunks, start=1):
            with qa.phase("external_vote_fixture_setup"):
                tournament = await qa.prepare_ready_burst_tournament(
                    api_client,
                    organizer=chunk[0],
                    participants=chunk,
                    label="external_ready",
                    index=index,
                )
            slug = str(tournament["slug"])
            tournament_entries.append(
                {
                    "id": str(tournament["id"]),
                    "slug": slug,
                    "user_count": len(chunk),
                }
            )
            for user in chunk:
                user_id = str(user["id"])
                manifest_users.append(
                    {
                        "user_id": user_id,
                        "tournament_slug": slug,
                        "session_token": qa.session_tokens_by_user_id[user_id],
                        "csrf_token": qa.csrf_tokens_by_user_id[user_id],
                    }
                )

        qa.report["tournament_ids"] = list(qa.tournament_ids)
        qa.report["tournament_slugs"] = list(qa.tournament_slugs)
        qa.report["external_vote"]["manifest_user_count"] = len(manifest_users)
        qa.scenario(
            "external_vote_fixture_ready_rounds_created",
            len(tournament_entries) == args.tournament_count
            and len(manifest_users) == total_users,
            {
                "tournaments": len(tournament_entries),
                "users": len(manifest_users),
            },
        )

        atomic_write_json(
            args.manifest_path,
            {
                "schema": 1,
                "purpose": "external_ready_vote",
                "origin": PUBLIC_ORIGIN,
                "session_cookie_name": qa.session_cookie_name,
                "csrf_cookie_name": qa.csrf_cookie_name,
                "marker": qa.marker,
                "created_at": datetime.now(UTC).isoformat(),
                "tournaments": tournament_entries,
                "users": manifest_users,
            },
            mode=0o600,
        )
        qa.report["finished_at"] = datetime.now(UTC).isoformat()
        qa.report["passed"] = True
        qa.report["report_path"] = str(args.report_path)
        args.report_path.parent.mkdir(parents=True, exist_ok=True)
        args.report_path.write_text(
            json.dumps(qa.report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        await qa.record_preprod_run(
            status="passed",
            requested_users=total_users,
            created_users=len(users),
            tournaments_created=len(tournament_entries),
            finished_at=datetime.now(UTC),
        )
        return qa.report
    except Exception:
        qa.report["finished_at"] = datetime.now(UTC).isoformat()
        qa.report["passed"] = False
        qa.report["report_path"] = str(args.report_path)
        args.report_path.parent.mkdir(parents=True, exist_ok=True)
        args.report_path.write_text(
            json.dumps(qa.report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        try:
            await qa.record_preprod_run(status="failed", finished_at=datetime.now(UTC))
        except Exception:
            pass
        raise
    finally:
        await api_client.aclose()


async def async_main() -> int:
    args = parse_args()
    report = await prepare(args)
    print(
        json.dumps(
            {
                "mode": "external-vote-prepare",
                "passed": report.get("passed"),
                "marker": report.get("marker"),
                "users": report.get("created_users"),
                "tournaments": len(report.get("tournament_ids") or []),
            },
            ensure_ascii=False,
        )
    )
    return 0


async def main() -> int:
    try:
        return await async_main()
    finally:
        await dispose_engine()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
