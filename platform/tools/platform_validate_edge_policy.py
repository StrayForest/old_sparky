#!/usr/bin/env python3
"""Read-only proof that Cloudflare, Nginx and UFW trust ranges agree."""

from __future__ import annotations

import argparse
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import sys

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


def desired_ranges(timeout: float) -> set[str]:
    ipv4 = parse_ranges(fetch_text(IPV4_URL, timeout), 4)
    ipv6 = parse_ranges(fetch_text(IPV6_URL, timeout), 6)
    return {str(network) for network in (*ipv4, *ipv6)}


def nginx_ranges(path: Path) -> set[str]:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"Nginx Cloudflare include is missing or unsafe: {path}")
    ranges: set[str] = set()
    for raw_line in path.read_text(encoding="ascii").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        prefix = "set_real_ip_from "
        if not line.startswith(prefix) or not line.endswith(";"):
            raise RuntimeError("Nginx Cloudflare include contains an unexpected directive.")
        ranges.add(str(ipaddress.ip_network(line[len(prefix):-1].strip(), strict=True)))
    return ranges


def ufw_ranges(status: str, desired: set[str]) -> set[str]:
    ranges: set[str] = set()
    for line in status.splitlines():
        if "ALLOW IN" not in line:
            continue
        if "Anywhere" in line and any(f"{port}/tcp" in line for port in WEB_PORTS):
            raise RuntimeError("UFW contains a broad public HTTP/S rule.")
        if MANAGED_COMMENT not in line:
            continue
        for candidate in CIDR_RE.findall(line):
            try:
                normalized = str(ipaddress.ip_network(candidate, strict=True))
            except ValueError:
                continue
            if normalized not in desired:
                raise RuntimeError(f"UFW contains an unexpected managed range: {normalized}")
            ranges.add(normalized)
    for network in sorted(desired):
        for port in WEB_PORTS:
            matching = [
                line
                for line in status.splitlines()
                if "ALLOW IN" in line
                and MANAGED_COMMENT in line
                and network in line
                and f"{port}/tcp" in line
            ]
            if len(matching) != 1:
                raise RuntimeError(
                    f"UFW must contain exactly one managed {port}/tcp rule for {network}."
                )
    return ranges


def run_ufw(ufw_bin: str, *arguments: str) -> str:
    completed = subprocess.run(
        [ufw_bin, *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def validate_ufw_baseline(status: str) -> None:
    if "Status: active" not in status:
        raise RuntimeError("UFW must be active for production edge validation.")
    if "Default: deny (incoming)" not in status:
        raise RuntimeError("UFW incoming policy must default to deny.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only Cloudflare/Nginx/UFW range parity proof."
    )
    parser.add_argument("--nginx-include", type=Path, default=DEFAULT_NGINX_INCLUDE)
    parser.add_argument("--ufw-bin", default="ufw")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.timeout <= 0 or args.timeout > 60:
        raise ValueError("--timeout must be greater than zero and at most 60 seconds.")
    desired = desired_ranges(args.timeout)
    nginx = nginx_ranges(args.nginx_include)
    if nginx != desired:
        raise RuntimeError(
            f"Nginx Cloudflare range parity failed: expected={len(desired)} actual={len(nginx)}."
        )
    validate_ufw_baseline(run_ufw(args.ufw_bin, "status", "verbose"))
    ufw = ufw_ranges(run_ufw(args.ufw_bin, "status", "numbered"), desired)
    if ufw != desired:
        raise RuntimeError(
            f"UFW Cloudflare range parity failed: expected={len(desired)} actual={len(ufw)}."
        )
    result = {
        "ok": True,
        "cloudflare_ranges": len(desired),
        "nginx_ranges": len(nginx),
        "ufw_ranges": len(ufw),
        "read_only": True,
    }
    if args.as_json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(
            "Cloudflare/Nginx/UFW parity passed: "
            f"ranges={len(desired)}; read_only=true."
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Edge policy validation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
