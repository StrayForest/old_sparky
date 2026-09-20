#!/usr/bin/env python3
"""Read-only proof that Cloudflare, Nginx and UFW trust ranges agree."""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
from pathlib import Path
import re
import subprocess
import sys
from collections.abc import Mapping

try:
    from .platform_update_cloudflare_ips import (
        IPV4_URL,
        IPV6_URL,
        fetch_text,
        parse_ranges,
    )
except ImportError:  # pragma: no cover - direct script execution on a host
    from platform_update_cloudflare_ips import IPV4_URL, IPV6_URL, fetch_text, parse_ranges


DEFAULT_NGINX_INCLUDE = Path("/etc/nginx/cloudflare-real-ip.conf")
WEB_PORTS = (80, 443)
MANAGED_COMMENT = "oldsparky-cloudflare-origin"
CIDR_RE = re.compile(r"(?<![A-Za-z0-9:])(?:[0-9a-fA-F:.]+)/(?:[0-9]{1,3})(?![A-Za-z0-9])")
WEB_PROFILE_NAMES = frozenset(
    {
        "Apache",
        "Apache Full",
        "Apache Secure",
        "Nginx Full",
        "Nginx HTTP",
        "Nginx HTTPS",
        "WWW",
        "WWW Full",
        "WWW Secure",
    }
)

PUBLIC_ERROR_CLASSES = frozenset(
    {
        "none",
        "argument",
        "cloudflare_fetch",
        "nginx_input",
        "ufw_command",
        "baseline",
        "policy",
        "internal",
    }
)


class EdgePolicyError(RuntimeError):
    """Validation failure with a closed, public error classification."""

    def __init__(self, message: str, *, error_class: str) -> None:
        super().__init__(message)
        self.error_class = (
            error_class
            if isinstance(error_class, str) and error_class in PUBLIC_ERROR_CLASSES
            else "internal"
        )


class _SafeArgumentParser(argparse.ArgumentParser):
    """Reject malformed operator input without echoing the input value."""

    def error(self, _message: str) -> None:
        raise EdgePolicyError(
            "Edge policy arguments are invalid.",
            error_class="argument",
        )


def desired_ranges(timeout: float) -> set[str]:
    try:
        ipv4 = parse_ranges(fetch_text(IPV4_URL, timeout), 4)
        ipv6 = parse_ranges(fetch_text(IPV6_URL, timeout), 6)
    except Exception:
        raise EdgePolicyError(
            "Cloudflare range data is unavailable or invalid.",
            error_class="cloudflare_fetch",
        ) from None
    return {str(network) for network in (*ipv4, *ipv6)}


def nginx_ranges(path: Path) -> set[str]:
    if not path.is_file() or path.is_symlink():
        raise EdgePolicyError(
            "Nginx Cloudflare include is missing or unsafe.",
            error_class="nginx_input",
        )
    ranges: set[str] = set()
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError):
        raise EdgePolicyError(
            "Nginx Cloudflare include cannot be read.",
            error_class="nginx_input",
        ) from None
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        prefix = "set_real_ip_from "
        if not line.startswith(prefix) or not line.endswith(";"):
            raise EdgePolicyError(
                "Nginx Cloudflare include contains an unexpected directive.",
                error_class="nginx_input",
            )
        try:
            network = ipaddress.ip_network(line[len(prefix) : -1].strip(), strict=True)
        except ValueError:
            raise EdgePolicyError(
                "Nginx Cloudflare include contains an invalid range.",
                error_class="nginx_input",
            ) from None
        ranges.add(str(network))
    return ranges


def _rule_name(line: str) -> str:
    rule = line.split(" ALLOW IN", 1)[0].strip()
    rule = re.sub(r"^\[\s*\d+\s*\]\s*", "", rule).strip()
    return re.sub(r"\s+\(v6\)$", "", rule, flags=re.IGNORECASE)


def _rule_networks(line: str) -> set[str]:
    networks: set[str] = set()
    for candidate in CIDR_RE.findall(line):
        try:
            networks.add(str(ipaddress.ip_network(candidate, strict=True)))
        except ValueError:
            continue
    return networks


def _explicit_web_ports(line: str) -> set[int]:
    return {
        int(value)
        for value in re.findall(r"(?<![0-9])(80|443)(?=(?:,|/|\s|$))", line)
    }


def _web_ports_for_rule(
    line: str,
    profile_ports: Mapping[str, set[int]],
) -> set[int]:
    ports = _explicit_web_ports(line)
    rule_name = _rule_name(line)
    if not ports:
        ports.update(profile_ports.get(rule_name, set()))
        if rule_name in WEB_PROFILE_NAMES:
            ports.update(WEB_PORTS)
    return ports & set(WEB_PORTS)


def ufw_ranges(
    status: str,
    desired: set[str],
    *,
    profile_ports: Mapping[str, set[int]] | None = None,
) -> set[str]:
    profile_ports = profile_ports or {}
    ranges: set[str] = set()
    web_rules: dict[tuple[str, int], list[str]] = {}
    for line in status.splitlines():
        if "ALLOW IN" not in line:
            continue
        web_ports = _web_ports_for_rule(line, profile_ports)
        if not web_ports:
            continue
        if "Anywhere" in line:
            raise EdgePolicyError(
                "UFW contains a broad public HTTP/S rule.",
                error_class="policy",
            )
        if MANAGED_COMMENT not in line:
            raise EdgePolicyError(
                "UFW contains an unmanaged inbound HTTP/S rule.",
                error_class="policy",
            )
        networks = _rule_networks(line)
        if len(networks) != 1:
            raise EdgePolicyError(
                "Managed UFW HTTP/S rules must name exactly one source network.",
                error_class="policy",
            )
        network = next(iter(networks))
        if network not in desired:
            raise EdgePolicyError(
                "UFW contains an unexpected managed range.",
                error_class="policy",
            )
        ranges.add(network)
        for port in web_ports:
            web_rules.setdefault((network, port), []).append(line)
    for network in sorted(desired):
        for port in WEB_PORTS:
            matching = web_rules.get((network, port), [])
            if len(matching) != 1:
                raise EdgePolicyError(
                    f"UFW must contain exactly one managed {port}/tcp rule.",
                    error_class="policy",
                )
    return ranges


def run_ufw(ufw_bin: str, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            [ufw_bin, *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        raise EdgePolicyError(
            "UFW command failed.",
            error_class="ufw_command",
        ) from None
    return completed.stdout


def parse_ufw_profile_ports(profile_info: str) -> set[int]:
    return {
        int(value)
        for value in re.findall(
            r"(?<![0-9])(80|443)(?=(?:,|/|\s|$))", profile_info
        )
    }


def ufw_profile_ports(ufw_bin: str) -> dict[str, set[int]]:
    profiles: dict[str, set[int]] = {}
    listed = run_ufw(ufw_bin, "app", "list")
    for raw_line in listed.splitlines():
        profile = raw_line.strip()
        if not profile or profile.lower().startswith("available applications"):
            continue
        ports = parse_ufw_profile_ports(run_ufw(ufw_bin, "app", "info", profile))
        if ports:
            profiles[profile] = ports
    return profiles


def validate_ufw_baseline(status: str) -> None:
    if "Status: active" not in status:
        raise EdgePolicyError(
            "UFW must be active for production edge validation.",
            error_class="baseline",
        )
    if "Default: deny (incoming)" not in status:
        raise EdgePolicyError(
            "UFW incoming policy must default to deny.",
            error_class="baseline",
        )


def parse_args() -> argparse.Namespace:
    parser = _SafeArgumentParser(
        description="Read-only Cloudflare/Nginx/UFW range parity proof."
    )
    parser.add_argument("--nginx-include", type=Path, default=DEFAULT_NGINX_INCLUDE)
    parser.add_argument("--ufw-bin", default="ufw")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser.parse_args()


def main() -> int:
    desired_count = 0
    nginx_count = 0
    ufw_count = 0
    args = argparse.Namespace(as_json="--json" in sys.argv[1:])
    try:
        args = parse_args()
        if (
            not math.isfinite(args.timeout)
            or args.timeout <= 0
            or args.timeout > 60
        ):
            raise EdgePolicyError(
                "Edge policy timeout is outside the allowed range.",
                error_class="argument",
            )
        desired = desired_ranges(args.timeout)
        desired_count = len(desired)
        nginx = nginx_ranges(args.nginx_include)
        nginx_count = len(nginx)
        if nginx != desired:
            raise EdgePolicyError(
                "Nginx Cloudflare range parity failed.",
                error_class="policy",
            )
        validate_ufw_baseline(run_ufw(args.ufw_bin, "status", "verbose"))
        ufw = ufw_ranges(
            run_ufw(args.ufw_bin, "status", "numbered"),
            desired,
            profile_ports=ufw_profile_ports(args.ufw_bin),
        )
        ufw_count = len(ufw)
        if ufw != desired:
            raise EdgePolicyError(
                "UFW Cloudflare range parity failed.",
                error_class="policy",
            )
    except EdgePolicyError as exc:
        error_class = exc.error_class
        result = {
            "schema": 1,
            "ok": False,
            "status": "failed",
            "error_class": error_class,
            "cloudflare_ranges": desired_count,
            "nginx_ranges": nginx_count,
            "ufw_ranges": ufw_count,
            "read_only": True,
        }
        if args.as_json:
            print(json.dumps(result, sort_keys=True))
        print(
            "EDGE_POLICY status=failed "
            f"error_class={error_class} cloudflare_ranges={desired_count} "
            f"nginx_ranges={nginx_count} ufw_ranges={ufw_count}",
            file=sys.stderr,
        )
        return 1
    except Exception:
        result = {
            "schema": 1,
            "ok": False,
            "status": "failed",
            "error_class": "internal",
            "cloudflare_ranges": desired_count,
            "nginx_ranges": nginx_count,
            "ufw_ranges": ufw_count,
            "read_only": True,
        }
        if args.as_json:
            print(json.dumps(result, sort_keys=True))
        print(
            "EDGE_POLICY status=failed "
            f"error_class=internal cloudflare_ranges={desired_count} "
            f"nginx_ranges={nginx_count} ufw_ranges={ufw_count}",
            file=sys.stderr,
        )
        return 1

    result = {
        "schema": 1,
        "ok": True,
        "status": "passed",
        "error_class": "none",
        "cloudflare_ranges": desired_count,
        "nginx_ranges": nginx_count,
        "ufw_ranges": ufw_count,
        "read_only": True,
    }
    if args.as_json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(
            "EDGE_POLICY status=passed error_class=none "
            f"cloudflare_ranges={desired_count} nginx_ranges={nginx_count} "
            f"ufw_ranges={ufw_count}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
