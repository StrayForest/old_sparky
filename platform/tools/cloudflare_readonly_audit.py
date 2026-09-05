#!/usr/bin/env python3
"""Collect redacted, read-only Cloudflare evidence for the production checklist.

The tool deliberately never prints the API token or raw API responses. A
successful request is not, by itself, closure evidence for every checklist
item: checks that need intent, dashboard-only settings, or runtime smoke
coverage are reported as REVIEW with the observed API facts.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_ROOT = "https://api.cloudflare.com/client/v4"
ZONE_NAME = "old-sparky.com"
CDN_HOST = "cdn.old-sparky.com"
EXPECTED_TURNSTILE_DOMAINS = {ZONE_NAME}
R2_BUCKET_NAME = "oldsparky"


@dataclass(frozen=True)
class ApiResult:
    status: int | None
    payload: dict[str, Any] | None
    error_codes: tuple[Any, ...] = ()
    transport_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == 200 and bool(self.payload and self.payload.get("success"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default=os.environ.get("CLOUDFLARE_AUDIT_REPORT", "cloudflare-audit-report.json"),
        help="Path for the redacted JSON report.",
    )
    return parser.parse_args()


def api_get(token: str, path: str, params: dict[str, str] | None = None) -> ApiResult:
    query = f"?{urlencode(params)}" if params else ""
    request = Request(
        f"{API_ROOT}{path}{query}",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "old-sparky-cloudflare-readonly-audit/1",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=20) as response:
            status = response.status
            raw = response.read()
    except HTTPError as error:
        status = error.code
        raw = error.read()
    except URLError as error:
        return ApiResult(status=None, payload=None, transport_error=error.reason.__class__.__name__)
    except TimeoutError:
        return ApiResult(status=None, payload=None, transport_error="TimeoutError")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ApiResult(status=status, payload=None, transport_error="InvalidJSON")
    if not isinstance(payload, dict):
        return ApiResult(status=status, payload=None, transport_error="UnexpectedJSONShape")
    errors = payload.get("errors")
    error_codes: list[Any] = []
    if isinstance(errors, list):
        for error in errors:
            if isinstance(error, dict) and "code" in error:
                error_codes.append(error["code"])
    return ApiResult(status=status, payload=payload, error_codes=tuple(error_codes))


def api_evidence(result: ApiResult) -> dict[str, Any]:
    evidence: dict[str, Any] = {"http_status": result.status}
    if result.error_codes:
        evidence["error_codes"] = list(result.error_codes)
    if result.transport_error:
        evidence["transport_error"] = result.transport_error
    return evidence


def response_result(result: ApiResult) -> Any:
    if result.ok and result.payload:
        return result.payload.get("result")
    return None


def record(
    report: dict[str, Any],
    check_id: str,
    status: str,
    summary: str,
    evidence: dict[str, Any] | None = None,
) -> None:
    report["checks"].append(
        {
            "id": check_id,
            "status": status,
            "summary": summary,
            "evidence": evidence or {},
        }
    )


def add_summary(report: dict[str, Any]) -> None:
    counts: dict[str, int] = {}
    for check in report["checks"]:
        counts[check["status"]] = counts.get(check["status"], 0) + 1
    report["summary"] = counts


def short_record(record_value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: record_value[key]
        for key in ("name", "type", "content", "proxied", "ttl", "priority")
        if key in record_value
    }


def ruleset_summary(result: ApiResult) -> dict[str, Any]:
    if not result.ok:
        return api_evidence(result)
    ruleset = response_result(result) or {}
    rules = ruleset.get("rules", []) if isinstance(ruleset, dict) else []
    summarized: list[dict[str, Any]] = []
    for rule in rules if isinstance(rules, list) else []:
        if not isinstance(rule, dict):
            continue
        item = {
            key: rule[key]
            for key in ("id", "ref", "description", "expression", "action", "enabled", "version")
            if key in rule
        }
        parameters = rule.get("action_parameters")
        if isinstance(parameters, dict):
            item["action_parameters"] = {
                key: parameters[key]
                for key in (
                    "cache",
                    "cache_key",
                    "browser_ttl",
                    "edge_ttl",
                    "serve_stale",
                    "matched_data",
                )
                if key in parameters
            }
        summarized.append(item)
    return {**api_evidence(result), "ruleset_id": ruleset.get("id"), "rules": summarized}


def run_audit(token: str, account_id: str) -> dict[str, Any]:
    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "zone_name": ZONE_NAME,
        "account_id_suffix": account_id[-6:] if len(account_id) >= 6 else "redacted",
        "read_only": True,
        "checks": [],
    }

    zones = api_get(token, "/zones", {"name": ZONE_NAME, "status": "active", "per_page": "20"})
    zone_result = response_result(zones)
    zone = None
    if isinstance(zone_result, list) and len(zone_result) == 1 and isinstance(zone_result[0], dict):
        zone = zone_result[0]
    if zone is None:
        record(
            report,
            "zone-access",
            "UNAVAILABLE" if zones.status in {401, 403} else "FAIL",
            "Could not resolve exactly one active production zone.",
            api_evidence(zones),
        )
        r2_buckets = api_get(token, f"/accounts/{account_id}/r2/buckets")
        buckets_result = response_result(r2_buckets)
        buckets = buckets_result.get("buckets", []) if isinstance(buckets_result, dict) else []
        bucket_names = [
            item.get("name")
            for item in buckets
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ]
        record(
            report,
            "r2-api-access",
            "PASS" if r2_buckets.ok else ("UNAVAILABLE" if r2_buckets.status in {401, 403} else "FAIL"),
            "Zone access was unavailable; account-level R2 access was checked independently.",
            {**api_evidence(r2_buckets), "bucket_names": bucket_names},
        )
        turnstile = api_get(token, f"/accounts/{account_id}/challenges/widgets", {"per_page": "100"})
        widgets_result = response_result(turnstile)
        widget_count = len(widgets_result) if isinstance(widgets_result, list) else None
        record(
            report,
            "turnstile-api-access",
            "PASS" if turnstile.ok else ("UNAVAILABLE" if turnstile.status in {401, 403} else "FAIL"),
            "Zone access was unavailable; account-level Turnstile access was checked independently.",
            {**api_evidence(turnstile), "widget_count": widget_count},
        )
        add_summary(report)
        return report

    zone_id = str(zone.get("id", ""))
    account = zone.get("account") if isinstance(zone.get("account"), dict) else {}
    account_match = account.get("id") == account_id
    record(
        report,
        "zone-access",
        "PASS" if account_match else "FAIL",
        "Resolved the active zone and matched the configured account.",
        {"zone_id_suffix": zone_id[-6:] if len(zone_id) >= 6 else "redacted", "account_match": account_match},
    )
    if not account_match:
        return report

    dns = api_get(token, f"/zones/{zone_id}/dns_records", {"per_page": "100"})
    records = response_result(dns)
    records = records if isinstance(records, list) else []
    by_name: dict[str, list[dict[str, Any]]] = {ZONE_NAME: [], CDN_HOST: [], f"www.{ZONE_NAME}": []}
    for item in records:
        if isinstance(item, dict) and item.get("name") in by_name:
            by_name[item["name"]].append(short_record(item))
    record(
        report,
        "dns-routing",
        "PASS" if dns.ok else ("UNAVAILABLE" if dns.status in {401, 403} else "FAIL"),
        "Read apex, CDN and www DNS records without changing them.",
        {**api_evidence(dns), "records": by_name},
    )
    record(
        report,
        "www-canonical-policy",
        "PASS" if dns.ok and not by_name[f"www.{ZONE_NAME}"] else ("UNAVAILABLE" if not dns.ok else "REVIEW"),
        "Observed whether the unsupported www hostname has DNS records.",
        {**api_evidence(dns), "www_records": by_name[f"www.{ZONE_NAME}"]},
    )
    caa_records = [item for item in records if isinstance(item, dict) and item.get("type") == "CAA"]
    record(
        report,
        "caa-decision",
        "REVIEW" if dns.ok else "UNAVAILABLE",
        "CAA records are observable; policy completeness still requires an operator CA decision.",
        {**api_evidence(dns), "records": [short_record(item) for item in caa_records]},
    )

    certificate_packs = api_get(
        token,
        f"/zones/{zone_id}/ssl/certificate_packs",
        {"status": "all", "per_page": "50", "deploy": "production"},
    )
    packs = response_result(certificate_packs)
    packs = packs if isinstance(packs, list) else []
    pack_summary = []
    active_certs = 0
    non_active_packs = []
    for pack in packs:
        if not isinstance(pack, dict):
            continue
        status = pack.get("status")
        if status != "active":
            non_active_packs.append(status)
        certs = pack.get("certificates") if isinstance(pack.get("certificates"), list) else []
        active_certs += sum(1 for cert in certs if isinstance(cert, dict) and cert.get("status") == "active")
        pack_summary.append(
            {
                key: pack[key]
                for key in ("type", "status", "hosts", "certificate_authority", "validation_errors")
                if key in pack
            }
        )
    record(
        report,
        "edge-certificates",
        "PASS" if certificate_packs.ok and active_certs > 0 and not non_active_packs else ("UNAVAILABLE" if certificate_packs.status in {401, 403} else "REVIEW"),
        "Read production certificate-pack status; expiry alerting is recorded separately.",
        {**api_evidence(certificate_packs), "active_certificate_count": active_certs, "packs": pack_summary},
    )
    ct_alerting = api_get(token, f"/zones/{zone_id}/ct/alerting")
    ct_result = response_result(ct_alerting)
    ct_enabled = isinstance(ct_result, dict) and ct_result.get("enabled") is True
    record(
        report,
        "certificate-alerts",
        "PASS" if ct_alerting.ok and ct_enabled else ("UNAVAILABLE" if ct_alerting.status in {401, 403} else "REVIEW"),
        "Read Certificate Transparency alerting state without exposing alert recipients.",
        {**api_evidence(ct_alerting), "enabled": ct_result.get("enabled") if isinstance(ct_result, dict) else None},
    )

    r2_buckets = api_get(token, f"/accounts/{account_id}/r2/buckets")
    buckets_result = response_result(r2_buckets)
    buckets = buckets_result.get("buckets", []) if isinstance(buckets_result, dict) else []
    buckets = buckets if isinstance(buckets, list) else []
    target_bucket = next((item for item in buckets if isinstance(item, dict) and item.get("name") == R2_BUCKET_NAME), None)
    bucket_details = (
        api_get(token, f"/accounts/{account_id}/r2/buckets/{R2_BUCKET_NAME}")
        if isinstance(target_bucket, dict)
        else None
    )
    detailed_bucket = response_result(bucket_details) if bucket_details else None
    detailed_bucket = detailed_bucket if isinstance(detailed_bucket, dict) else target_bucket
    storage_class = detailed_bucket.get("storage_class") if isinstance(detailed_bucket, dict) else None
    record(
        report,
        "r2-public-bucket",
        "PASS" if bucket_details and bucket_details.ok and storage_class == "Standard" else ("UNAVAILABLE" if r2_buckets.status in {401, 403} or (bucket_details and bucket_details.status in {401, 403}) else "REVIEW"),
        "Read the configured media bucket storage class.",
        {
            **api_evidence(r2_buckets),
            "bucket_detail": api_evidence(bucket_details) if bucket_details else None,
            "bucket": {
                key: target_bucket[key]
                for key in ("name", "storage_class", "jurisdiction", "location")
                if isinstance(detailed_bucket, dict) and key in detailed_bucket
            },
        },
    )
    if isinstance(target_bucket, dict):
        managed_domain = api_get(token, f"/accounts/{account_id}/r2/buckets/{R2_BUCKET_NAME}/domains/managed")
        managed_result = response_result(managed_domain)
        managed_enabled = isinstance(managed_result, dict) and managed_result.get("enabled") is True
        record(
            report,
            "r2-dev-public-access",
            "FAIL" if managed_domain.ok and managed_enabled else ("PASS" if managed_domain.ok else "UNAVAILABLE"),
            "Read whether the media bucket is public through r2.dev.",
            {
                **api_evidence(managed_domain),
                "enabled": managed_result.get("enabled") if isinstance(managed_result, dict) else None,
                "domain_present": bool(managed_result.get("domain")) if isinstance(managed_result, dict) else None,
            },
        )
        cors = api_get(token, f"/accounts/{account_id}/r2/buckets/{R2_BUCKET_NAME}/cors")
        cors_result = response_result(cors)
        cors_rules = cors_result.get("rules", []) if isinstance(cors_result, dict) else None
        record(
            report,
            "r2-browser-put-cors",
            "PASS"
            if (cors.ok and cors_rules in (None, [])) or (cors.status == 404 and 10059 in cors.error_codes)
            else ("REVIEW" if cors.ok else "UNAVAILABLE"),
            "Read browser CORS policy for the media bucket; a missing policy is the expected result.",
            {**api_evidence(cors), "rule_count": len(cors_rules) if isinstance(cors_rules, list) else None},
        )
        custom_domains = api_get(token, f"/accounts/{account_id}/r2/buckets/{R2_BUCKET_NAME}/domains/custom")
        custom_result = response_result(custom_domains)
        domains = custom_result.get("domains", []) if isinstance(custom_result, dict) else []
        record(
            report,
            "r2-custom-domain",
            "PASS" if custom_domains.ok else ("UNAVAILABLE" if custom_domains.status in {401, 403} else "REVIEW"),
            "Read configured R2 custom-domain state without changing it.",
            {
                **api_evidence(custom_domains),
                "domains": [
                    {
                        key: domain[key]
                        for key in ("domain", "enabled", "status", "minTLS")
                        if isinstance(domain, dict) and key in domain
                    }
                    for domain in domains
                    if isinstance(domain, dict)
                ],
            },
        )
    record(
        report,
        "media-token-scope",
        "REVIEW",
        "The Cloudflare API does not expose the GitHub secret's intended media-token separation; verify token inventory/scope in the dashboard.",
    )

    turnstile = api_get(token, f"/accounts/{account_id}/challenges/widgets", {"per_page": "100"})
    widgets_result = response_result(turnstile)
    widgets = widgets_result if isinstance(widgets_result, list) else []
    widget_domains = [
        {
            key: widget[key]
            for key in ("sitekey", "name", "mode", "domains", "bot_fight_mode")
            if isinstance(widget, dict) and key in widget
        }
        for widget in widgets
        if isinstance(widget, dict)
    ]
    unexpected_domains = sorted(
        {
            domain
            for widget in widget_domains
            for domain in widget.get("domains", [])
            if domain not in EXPECTED_TURNSTILE_DOMAINS
        }
    )
    record(
        report,
        "turnstile-hostnames",
        "PASS" if turnstile.ok and not unexpected_domains else ("UNAVAILABLE" if turnstile.status in {401, 403} else "REVIEW"),
        "Read Turnstile widget hostnames without retrieving widget secrets.",
        {**api_evidence(turnstile), "widgets": widget_domains, "unexpected_domains": unexpected_domains},
    )

    phases = {
        "cache-rules": "http_request_cache_settings",
        "managed-and-custom-waf": "http_request_firewall_custom",
        "managed-waf-entrypoint": "http_request_main",
        "edge-rate-limits": "http_ratelimit",
    }
    for check_id, phase in phases.items():
        result = api_get(token, f"/zones/{zone_id}/rulesets/phases/{phase}/entrypoint")
        status = "PASS" if result.ok else ("UNAVAILABLE" if result.status in {401, 403, 404} else "REVIEW")
        summary = ruleset_summary(result)
        if check_id in {"cache-rules", "edge-rate-limits"} and result.ok:
            status = "REVIEW"
        record(
            report,
            check_id,
            status,
            f"Read the {phase} entrypoint ruleset; intent and runtime compatibility still need correlation.",
            summary,
        )

    bot_management = api_get(token, f"/zones/{zone_id}/bot_management")
    bot_result = response_result(bot_management)
    bot_observed = {}
    if isinstance(bot_result, dict):
        bot_observed = {
            key: bot_result[key]
            for key in (
                "fight_mode",
                "sbfm_definitely_automated",
                "sbfm_likely_automated",
                "sbfm_static_resource_protection",
                "sbfm_verified_bots",
                "stale_zone_configuration",
            )
            if key in bot_result
        }
    record(
        report,
        "bot-fight-mode",
        "REVIEW" if bot_management.ok else ("UNAVAILABLE" if bot_management.status in {401, 403} else "FAIL"),
        "Read Bot Fight Mode configuration; API state alone does not prove API/CDN/Turnstile runtime compatibility.",
        {**api_evidence(bot_management), "configuration": bot_observed},
    )

    add_summary(report)
    return report


def write_summary(report: dict[str, Any]) -> None:
    lines = [
        "## Cloudflare read-only audit",
        "",
        "No Cloudflare settings were changed. The JSON artifact contains only redacted evidence.",
        "",
        "| Check | Status | Summary |",
        "| --- | --- | --- |",
    ]
    for check in report["checks"]:
        summary = str(check["summary"]).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| `{check['id']}` | **{check['status']}** | {summary} |")
    lines.extend(["", f"Summary: `{json.dumps(report['summary'], sort_keys=True)}`"])
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    print("Cloudflare read-only audit completed")
    for check in report["checks"]:
        print(f"{check['status']:11} {check['id']}")
    print(f"Summary: {json.dumps(report['summary'], sort_keys=True)}")


def main() -> int:
    args = parse_args()
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    if not token or not account_id:
        print("CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID are required", file=sys.stderr)
        return 2
    report = run_audit(token, account_id)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
