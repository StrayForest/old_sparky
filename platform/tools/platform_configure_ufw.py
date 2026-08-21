#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from platform_update_cloudflare_ips import IPV4_URL, IPV6_URL, fetch_text, parse_ranges


DEFAULT_STATE = Path("/var/lib/oldsparky/cloudflare-ufw.json")
MANAGED_COMMENT = "oldsparky-cloudflare-origin"
WEB_PORTS = (80, 443)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Allow HTTP/S only from validated Cloudflare CIDRs while preserving SSH. "
            "Dry-run is the default."
        )
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--confirm-console-access",
        action="store_true",
        help="Required for the initial apply after out-of-band console access is verified.",
    )
    parser.add_argument(
        "--managed-update",
        action="store_true",
        help="Update an already initialized managed ruleset; refuses first-time use.",
    )
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser.parse_args()


def network_strings(
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> list[str]:
    return [str(network) for network in networks]


def load_state(path: Path) -> set[str]:
    if not path.exists():
        return set()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("format_version") != 1 or payload.get("comment") != MANAGED_COMMENT:
        raise RuntimeError("Managed UFW state has an unsupported format or owner.")
    ranges = payload.get("ranges")
    if not isinstance(ranges, list):
        raise RuntimeError("Managed UFW state does not contain a range list.")
    return {str(ipaddress.ip_network(value, strict=True)) for value in ranges}


def write_state(path: Path, ranges: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "format_version": 1,
        "comment": MANAGED_COMMENT,
        "sources": [IPV4_URL, IPV6_URL],
        "ranges": sorted(ranges),
        "ports": list(WEB_PORTS),
    }
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        temporary.chmod(0o600)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run_ufw(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["ufw", *arguments],
        check=True,
        capture_output=True,
        text=True,
    )


def validate_firewall_baseline() -> None:
    verbose = run_ufw("status", "verbose").stdout
    if "Status: active" not in verbose:
        raise RuntimeError("UFW must already be active before managed web rules are applied.")
    if "Default: deny (incoming)" not in verbose:
        raise RuntimeError("UFW incoming default policy must be deny.")
    numbered = run_ufw("status", "numbered").stdout
    broad_web_rules = [
        line.strip()
        for line in numbered.splitlines()
        if "ALLOW IN" in line
        and "Anywhere" in line
        and any(str(port) in line for port in WEB_PORTS)
    ]
    if broad_web_rules:
        raise RuntimeError(
            "Broad public HTTP/S UFW rules exist; review and remove them before Cloudflare-only apply."
        )


def add_range(network: str) -> None:
    for port in WEB_PORTS:
        run_ufw(
            "allow",
            "proto",
            "tcp",
            "from",
            network,
            "to",
            "any",
            "port",
            str(port),
            "comment",
            MANAGED_COMMENT,
        )


def remove_range(network: str) -> None:
    for port in WEB_PORTS:
        run_ufw(
            "--force",
            "delete",
            "allow",
            "proto",
            "tcp",
            "from",
            network,
            "to",
            "any",
            "port",
            str(port),
        )


def apply_ranges(state_file: Path, desired: set[str], *, managed_update: bool) -> dict[str, int]:
    if os.geteuid() != 0:
        raise PermissionError("UFW changes require root.")
    previous = load_state(state_file)
    if managed_update and not previous:
        raise RuntimeError("--managed-update requires an initialized managed state file.")
    validate_firewall_baseline()

    additions = sorted(desired - previous)
    removals = sorted(previous - desired)
    added: list[str] = []
    try:
        for network in additions:
            add_range(network)
            added.append(network)
    except Exception:
        for network in reversed(added):
            try:
                remove_range(network)
            except Exception:
                pass
        raise

    # Add-before-remove keeps the origin reachable throughout an official range change.
    for network in removals:
        remove_range(network)
    write_state(state_file, desired)
    return {"added": len(additions), "removed": len(removals)}


def main() -> int:
    args = parse_args()
    if args.timeout <= 0 or args.timeout > 60:
        raise ValueError("--timeout must be greater than zero and at most 60 seconds.")
    if args.managed_update and not args.apply:
        raise ValueError("--managed-update requires --apply.")
    if args.apply and not args.managed_update and not args.confirm_console_access:
        raise RuntimeError("Initial --apply requires --confirm-console-access.")

    ipv4 = parse_ranges(fetch_text(IPV4_URL, args.timeout), 4)
    ipv6 = parse_ranges(fetch_text(IPV6_URL, args.timeout), 6)
    desired = set(network_strings([*ipv4, *ipv6]))
    changes = {"added": 0, "removed": 0}
    if args.apply:
        changes = apply_ranges(args.state_file, desired, managed_update=args.managed_update)
    else:
        previous = load_state(args.state_file)
        changes = {
            "added": len(desired - previous),
            "removed": len(previous - desired),
        }

    result = {
        "ok": True,
        "mode": "apply" if args.apply else "dry-run",
        "ipv4_ranges": len(ipv4),
        "ipv6_ranges": len(ipv6),
        **changes,
    }
    if args.as_json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(
            f"Cloudflare-only UFW plan valid: IPv4={len(ipv4)}, IPv6={len(ipv6)}, "
            f"add={changes['added']}, remove={changes['removed']}, mode={result['mode']}."
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Cloudflare UFW update failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
