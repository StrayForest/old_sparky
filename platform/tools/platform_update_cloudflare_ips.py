#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.request


IPV4_URL = "https://www.cloudflare.com/ips-v4"
IPV6_URL = "https://www.cloudflare.com/ips-v6"
DEFAULT_OUTPUT = Path("/etc/nginx/cloudflare-real-ip.conf")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch and validate Cloudflare origin CIDRs, then render the Nginx "
            "set_real_ip_from include. Dry-run is the default."
        )
    )
    parser.add_argument("--apply", action="store_true", help="Atomically install the candidate.")
    parser.add_argument("--reload", action="store_true", help="Reload Nginx after a successful apply.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--nginx-bin", default="nginx")
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser.parse_args()


def fetch_text(url: str, timeout: float) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "OldSparky-Cloudflare-IP-Updater/1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed HTTPS URLs
        if response.status != 200:
            raise RuntimeError(f"Cloudflare range endpoint returned HTTP {response.status}.")
        return response.read(128 * 1024).decode("ascii", errors="strict")


def parse_ranges(raw: str, expected_version: int) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for line in raw.splitlines():
        value = line.strip()
        if not value:
            continue
        network = ipaddress.ip_network(value, strict=True)
        if network.version != expected_version:
            raise ValueError(f"Unexpected IPv{network.version} range in IPv{expected_version} source.")
        networks.append(network)
    minimum = 10 if expected_version == 4 else 5
    if len(networks) < minimum:
        raise ValueError(
            f"Cloudflare IPv{expected_version} source returned only {len(networks)} ranges; "
            f"expected at least {minimum}."
        )
    if len(set(networks)) != len(networks):
        raise ValueError(f"Cloudflare IPv{expected_version} source contains duplicate ranges.")
    return networks


def render_config(
    ipv4: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
    ipv6: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> str:
    lines = [
        "# Managed by platform_update_cloudflare_ips.py; do not edit manually.",
        f"# Sources: {IPV4_URL} and {IPV6_URL}",
    ]
    lines.extend(f"set_real_ip_from {network};" for network in [*ipv4, *ipv6])
    return "\n".join(lines) + "\n"


def run_checked(command: list[str]) -> None:
    subprocess.run(command, check=True, text=True, capture_output=True)


def _atomic_write(output: Path, content: str) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output.parent,
            prefix=f".{output.name}.",
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary_path = Path(handle.name)
        temporary_path.chmod(0o644)
        os.replace(temporary_path, output)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def install_candidate(output: Path, content: str, nginx_bin: str, reload_nginx: bool) -> bool:
    output.parent.mkdir(parents=True, exist_ok=True)
    previous = output.read_text(encoding="utf-8") if output.exists() else None
    if previous == content:
        if reload_nginx:
            run_checked([nginx_bin, "-t"])
            run_checked(["systemctl", "reload", "nginx.service"])
        return False

    if output.is_absolute() and not str(output).startswith("/tmp/") and os.geteuid() != 0:
        raise PermissionError("--apply to the system Nginx include requires root.")

    backup = output.with_suffix(output.suffix + ".previous")
    if output.exists():
        shutil.copy2(output, backup)
    _atomic_write(output, content)
    try:
        run_checked([nginx_bin, "-t"])
        if reload_nginx:
            run_checked(["systemctl", "reload", "nginx.service"])
        return True
    except Exception as install_error:
        rollback_errors: list[str] = []
        try:
            if previous is None:
                output.unlink(missing_ok=True)
            else:
                _atomic_write(output, previous)
        except Exception as exc:  # pragma: no cover - catastrophic filesystem failure
            rollback_errors.append(f"restore include: {exc}")
        try:
            run_checked([nginx_bin, "-t"])
        except Exception as exc:  # pragma: no cover - live rollback failure
            rollback_errors.append(f"validate restored nginx: {exc}")
        if reload_nginx:
            try:
                run_checked(["systemctl", "reload", "nginx.service"])
            except Exception as exc:  # pragma: no cover - live rollback failure
                rollback_errors.append(f"reload restored nginx: {exc}")
        if rollback_errors:
            raise RuntimeError(
                f"Cloudflare include update failed ({install_error}); rollback also failed: "
                + "; ".join(rollback_errors)
            ) from install_error
        raise


def main() -> int:
    args = parse_args()
    if args.reload and not args.apply:
        raise ValueError("--reload requires --apply.")
    if args.timeout <= 0 or args.timeout > 60:
        raise ValueError("--timeout must be greater than zero and at most 60 seconds.")

    ipv4 = parse_ranges(fetch_text(IPV4_URL, args.timeout), 4)
    ipv6 = parse_ranges(fetch_text(IPV6_URL, args.timeout), 6)
    content = render_config(ipv4, ipv6)
    changed = False
    if args.apply:
        changed = install_candidate(args.output, content, args.nginx_bin, args.reload)

    result = {
        "ok": True,
        "mode": "apply" if args.apply else "dry-run",
        "output": str(args.output),
        "ipv4_ranges": len(ipv4),
        "ipv6_ranges": len(ipv6),
        "changed": changed,
        "reload_requested": bool(args.reload),
    }
    if args.as_json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(
            f"Cloudflare ranges valid: IPv4={len(ipv4)}, IPv6={len(ipv6)}; "
            f"mode={result['mode']}; changed={changed}."
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Cloudflare IP update failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
