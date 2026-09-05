#!/usr/bin/env python3
"""One-shot Cloudflare operator remediation for the AUD-02 controls.

This tool is intentionally CI-only and is removed after the operator run. It
does not print tokens or alert destinations. The default mode is read-only;
--apply is required for changes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


API_ROOT = "https://api.cloudflare.com/client/v4"
ZONE_NAME = "old-sparky.com"
R2_BUCKET_NAME = "oldsparky"
MANAGED_WAF_RULESET_ID = "4814384a9e5d4991b9815dcfc25d2f1f"
WAF_RULESET_NAME = "OldSparky Cloudflare Managed WAF"
RATE_RULESET_NAME = "OldSparky edge rate limits"
SSL_ALERT_NAME = "OldSparky SSL certificate lifecycle alerts"
CAA_AUTHORITIES = (
    "pki.goog; cansignhttpexchanges=yes",
    "letsencrypt.org",
    "ssl.com",
    "sectigo.com",
)


class ApiFailure(RuntimeError):
    def __init__(self, method: str, path: str, status: int, codes: list[Any]) -> None:
        super().__init__(f"Cloudflare API {method} {path} failed with HTTP {status} (codes={codes})")
        self.method = method
        self.path = path
        self.status = status
        self.codes = codes


def api_request(token: str, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    request = urllib.request.Request(
        f"{API_ROOT}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "old-sparky-aud02-operator/1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = response.status
            payload = json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode() or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        errors = payload.get("errors") if isinstance(payload, dict) else None
        codes = [item.get("code") for item in errors if isinstance(item, dict)] if isinstance(errors, list) else []
        raise ApiFailure(method, path, exc.code, codes) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Cloudflare API {method} {path} transport failure: {exc.__class__.__name__}") from exc
    if not isinstance(payload, dict) or payload.get("success") is not True:
        errors = payload.get("errors") if isinstance(payload, dict) else None
        codes = [item.get("code") for item in errors if isinstance(item, dict)] if isinstance(errors, list) else []
        raise ApiFailure(method, path, status, codes)
    result = payload.get("result")
    return result if isinstance(result, dict) else {"value": result}


def list_result(token: str, path: str) -> list[dict[str, Any]]:
    result = api_request(token, "GET", path)
    value = result.get("value")
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    return []


def get_zone(token: str, account_id: str) -> dict[str, Any]:
    query = urllib.parse.urlencode({"name": ZONE_NAME, "status": "active"})
    zones = list_result(token, f"/zones?{query}")
    for zone in zones:
        if zone.get("name") == ZONE_NAME and zone.get("account", {}).get("id") == account_id:
            return zone
    raise RuntimeError(f"Active zone {ZONE_NAME} was not found for the configured account")


def phase_entrypoint(token: str, zone_id: str, phase: str) -> dict[str, Any] | None:
    try:
        return api_request(token, "GET", f"/zones/{zone_id}/rulesets/phases/{phase}/entrypoint")
    except ApiFailure as exc:
        if exc.status == 404 or 10003 in exc.codes:
            return None
        raise


def ensure_caa(token: str, zone_id: str, report: dict[str, Any], apply: bool) -> None:
    query = urllib.parse.urlencode({"type": "CAA", "name": ZONE_NAME, "per_page": 100})
    records = list_result(token, f"/zones/{zone_id}/dns_records?{query}")
    existing = {
        (str(item.get("data", {}).get("tag")), str(item.get("data", {}).get("value")))
        for item in records
        if isinstance(item.get("data"), dict)
    }
    wanted = [(tag, value) for tag in ("issue", "issuewild") for value in CAA_AUTHORITIES]
    missing = [(tag, value) for tag, value in wanted if (tag, value) not in existing]
    report["caa"] = {"existing": len(existing), "missing": len(missing), "policy": "Cloudflare partner CAs"}
    if apply:
        for tag, value in missing:
            api_request(
                token,
                "POST",
                f"/zones/{zone_id}/dns_records",
                {
                    "type": "CAA",
                    "name": ZONE_NAME,
                    "ttl": 3600,
                    "proxied": False,
                    "data": {"flags": 0, "tag": tag, "value": value},
                },
            )
        if missing:
            report["changes"].append(f"created {len(missing)} CAA records")


def ensure_ct_alerting(token: str, zone_id: str, email: str, report: dict[str, Any], apply: bool) -> None:
    current = api_request(token, "GET", f"/zones/{zone_id}/ct/alerting")
    report["certificate_transparency"] = {"enabled": bool(current.get("enabled")), "email_configured": bool(current.get("emails"))}
    if apply:
        try:
            api_request(token, "PATCH", f"/zones/{zone_id}/ct/alerting", {"enabled": True, "emails": [email]})
            report["changes"].append("enabled certificate-transparency alerting")
        except ApiFailure as exc:
            if exc.status not in (400, 403, 405) and 10090 not in exc.codes:
                raise
            api_request(token, "PATCH", f"/zones/{zone_id}/ct/alerting", {"enabled": True})
            report["changes"].append("enabled certificate-transparency alerting for SSL-permission users")


def ensure_ssl_policy(token: str, account_id: str, email: str, report: dict[str, Any], apply: bool) -> None:
    policies = list_result(token, f"/accounts/{account_id}/alerting/v3/policies")
    current = next((item for item in policies if item.get("name") == SSL_ALERT_NAME), None)
    available = list_result(token, f"/accounts/{account_id}/alerting/v3/available_alerts")
    available_types = {str(item.get("alert_type") or item.get("type")) for item in available}
    report["ssl_policy"] = {"present": current is not None, "available": "universal_ssl_event_type" in available_types}
    if not apply or "universal_ssl_event_type" not in available_types:
        return
    body = {
        "alert_type": "universal_ssl_event_type",
        "enabled": True,
        "mechanisms": {"email": [{"id": email}]},
        "name": SSL_ALERT_NAME,
        "description": "Email lifecycle alerts for Cloudflare-managed Universal SSL.",
    }
    if current:
        api_request(token, "PUT", f"/accounts/{account_id}/alerting/v3/policies/{current['id']}", body)
        report["changes"].append("updated Universal SSL lifecycle alert policy")
    else:
        api_request(token, "POST", f"/accounts/{account_id}/alerting/v3/policies", body)
        report["changes"].append("created Universal SSL lifecycle alert policy")


def managed_waf_rule() -> dict[str, Any]:
    return {
        "action": "execute",
        "action_parameters": {"id": MANAGED_WAF_RULESET_ID},
        "expression": "true",
        "description": "Execute Cloudflare Managed Ruleset",
        "enabled": True,
    }


def ensure_managed_waf(token: str, zone_id: str, report: dict[str, Any], apply: bool) -> None:
    entrypoint = phase_entrypoint(token, zone_id, "http_request_firewall_managed")
    rules = entrypoint.get("rules", []) if entrypoint else []
    present = any(item.get("action_parameters", {}).get("id") == MANAGED_WAF_RULESET_ID for item in rules)
    report["managed_waf"] = {"entrypoint": entrypoint is not None, "cloudflare_ruleset_present": present}
    if not apply or present:
        return
    body = {
        "name": WAF_RULESET_NAME,
        "description": "Execute Cloudflare managed WAF ruleset for the zone.",
        "kind": "root",
        "phase": "http_request_firewall_managed",
        "rules": [managed_waf_rule()],
    }
    if entrypoint:
        api_request(token, "POST", f"/zones/{zone_id}/rulesets/{entrypoint['id']}/rules", managed_waf_rule())
    else:
        api_request(token, "POST", f"/zones/{zone_id}/rulesets", body)
    report["changes"].append("enabled Cloudflare Managed WAF ruleset")


def rate_rule(path: str, limit: int, period: int, timeout: int, description: str) -> dict[str, Any]:
    return {
        "action": "block",
        "expression": f'(http.host eq "{ZONE_NAME}" and http.request.method eq "POST" and http.request.uri.path eq "{path}")',
        "description": description,
        "enabled": True,
        "ratelimit": {
            "characteristics": ["cf.colo.id", "ip.src"],
            "period": period,
            "requests_per_period": limit,
            "mitigation_timeout": timeout,
        },
    }


def ensure_rate_limits(token: str, zone_id: str, report: dict[str, Any], apply: bool) -> None:
    entrypoint = phase_entrypoint(token, zone_id, "http_ratelimit")
    rules = entrypoint.get("rules", []) if entrypoint else []
    wanted = [
        rate_rule("/api/v1/auth/login", 60, 60, 60, "Limit password login bursts"),
        rate_rule("/api/v1/auth/register", 30, 60, 60, "Limit registration bursts"),
        rate_rule("/api/v1/auth/password-reset/request", 10, 600, 600, "Limit password reset requests"),
    ]
    descriptions = {str(item.get("description")) for item in rules}
    missing = [item for item in wanted if item["description"] not in descriptions]
    report["rate_limits"] = {"entrypoint": entrypoint is not None, "missing": len(missing), "policy": "conservative per-IP auth limits"}
    if not apply or not missing:
        return
    if entrypoint:
        for item in missing:
            api_request(token, "POST", f"/zones/{zone_id}/rulesets/{entrypoint['id']}/rules", item)
    else:
        api_request(
            token,
            "POST",
            f"/zones/{zone_id}/rulesets",
            {
                "name": RATE_RULESET_NAME,
                "description": "Conservative per-IP limits for unauthenticated auth endpoints.",
                "kind": "root",
                "phase": "http_ratelimit",
                "rules": wanted,
            },
        )
    report["changes"].append(f"configured {len(missing)} edge auth rate limits")


def ensure_bot_fight(token: str, zone_id: str, report: dict[str, Any], apply: bool, disable: bool) -> None:
    current = api_request(token, "GET", f"/zones/{zone_id}/bot_management")
    before = bool(current.get("fight_mode"))
    report["bot_fight_mode"] = {"before": before, "requested": not disable if apply else None}
    if not apply or before == (not disable):
        return
    api_request(token, "PUT", f"/zones/{zone_id}/bot_management", {"fight_mode": not disable})
    report["changes"].append(f"{'enabled' if not disable else 'disabled'} Bot Fight Mode")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--disable-bot-fight", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    email = os.environ.get("CLOUDFLARE_ALERT_EMAIL", "")
    if not token or not account_id:
        raise RuntimeError("CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID are required")
    if args.apply and not email:
        raise RuntimeError("CLOUDFLARE_ALERT_EMAIL is required for an applied alerting run")
    zone = get_zone(token, account_id)
    zone_id = str(zone["id"])
    report: dict[str, Any] = {"zone": ZONE_NAME, "apply": args.apply, "changes": []}
    ensure_caa(token, zone_id, report, args.apply)
    ensure_ct_alerting(token, zone_id, email, report, args.apply)
    ensure_ssl_policy(token, account_id, email, report, args.apply)
    ensure_managed_waf(token, zone_id, report, args.apply)
    ensure_rate_limits(token, zone_id, report, args.apply)
    ensure_bot_fight(token, zone_id, report, args.apply, args.disable_bot_fight)
    if args.output:
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ApiFailure, RuntimeError) as exc:
        print(f"AUD-02 operator remediation failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
