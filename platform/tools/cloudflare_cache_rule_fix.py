#!/usr/bin/env python3
"""Safely repair the one stale production-session cookie in Cache Rules.

This is intentionally narrow: it refuses to modify the rule unless the
existing rule has the expected description, action, path and old cookie, then
PATCHes only that rule and verifies the returned definition.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_ROOT = "https://api.cloudflare.com/client/v4"
ZONE_NAME = "old-sparky.com"
PHASE = "http_request_cache_settings"
RULESET_ID = "e6aab946f268454f8c59fcca19711c9e"
RULE_ID = "bf95363c9da84f3daaee12324892ce0f"
OLD_COOKIE = "deadlock_platform_session="
NEW_COOKIE = "__Host-old_sparky_session="
EXPECTED_DESCRIPTION = "Bypass authenticated tournament cache"


def request_json(token: str, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "old-sparky-cloudflare-cache-remediation/1",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = Request(f"{API_ROOT}{path}", data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read()
            status = response.status
    except HTTPError as error:
        status = error.code
        raw = error.read()
    except URLError as error:
        raise RuntimeError(f"Cloudflare request failed: {error.reason.__class__.__name__}") from error
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Cloudflare returned invalid JSON ({status})") from error
    if status != 200 or not isinstance(payload, dict) or not payload.get("success"):
        errors = payload.get("errors", []) if isinstance(payload, dict) else []
        codes = [item.get("code") for item in errors if isinstance(item, dict) and "code" in item]
        raise RuntimeError(f"Cloudflare {method} failed with HTTP {status}, error_codes={codes}")
    return payload


def main() -> int:
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    if not token or not account_id:
        print("Cloudflare remediation credentials are missing", file=sys.stderr)
        return 2

    zones = request_json(token, "GET", f"/zones?name={ZONE_NAME}&status=active&per_page=20")
    zone_rows = zones.get("result")
    if not isinstance(zone_rows, list) or len(zone_rows) != 1:
        raise RuntimeError("expected exactly one active production zone")
    zone = zone_rows[0]
    if not isinstance(zone, dict) or zone.get("account", {}).get("id") != account_id:
        raise RuntimeError("production zone account mismatch")
    zone_id = zone.get("id")
    if not isinstance(zone_id, str):
        raise RuntimeError("production zone id missing")

    entrypoint = request_json(token, "GET", f"/zones/{zone_id}/rulesets/phases/{PHASE}/entrypoint")
    ruleset = entrypoint.get("result")
    if not isinstance(ruleset, dict) or ruleset.get("id") != RULESET_ID:
        raise RuntimeError("cache ruleset id did not match the reviewed production rule")
    rules = ruleset.get("rules")
    if not isinstance(rules, list):
        raise RuntimeError("cache ruleset rules are missing")
    matches = [
        rule
        for rule in rules
        if isinstance(rule, dict)
        and rule.get("id") == RULE_ID
        and rule.get("description") == EXPECTED_DESCRIPTION
        and rule.get("action") == "set_cache_settings"
        and isinstance(rule.get("action_parameters"), dict)
        and rule["action_parameters"].get("cache") is False
        and "/api/v1/tournaments" in str(rule.get("expression", ""))
        and OLD_COOKIE in str(rule.get("expression", ""))
        and NEW_COOKIE not in str(rule.get("expression", ""))
    ]
    if len(matches) != 1:
        raise RuntimeError("reviewed stale-cookie rule was not uniquely identified")
    rule = matches[0]
    new_expression = str(rule["expression"]).replace(OLD_COOKIE, NEW_COOKIE)
    patch: dict[str, Any] = {
        "action": rule["action"],
        "action_parameters": rule["action_parameters"],
        "description": rule["description"],
        "expression": new_expression,
        "enabled": rule.get("enabled", True),
    }
    if rule.get("ref") is not None:
        patch["ref"] = rule["ref"]

    updated = request_json(
        token,
        "PATCH",
        f"/zones/{zone_id}/rulesets/{RULESET_ID}/rules/{RULE_ID}",
        patch,
    )
    updated_ruleset = updated.get("result")
    updated_rules = updated_ruleset.get("rules", []) if isinstance(updated_ruleset, dict) else []
    verified = next(
        (
            item
            for item in updated_rules
            if isinstance(item, dict) and item.get("id") == RULE_ID
        ),
        None,
    )
    if not isinstance(verified, dict) or verified.get("expression") != new_expression:
        raise RuntimeError("Cloudflare response did not contain the repaired rule")
    if OLD_COOKIE in str(verified.get("expression", "")) or NEW_COOKIE not in str(verified.get("expression", "")):
        raise RuntimeError("repaired rule verification failed")
    print("Updated and verified the reviewed authenticated-cache rule")
    print("Rule id suffix: " + RULE_ID[-6:])
    print("Session cookie bypass: __Host-old_sparky_session")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
