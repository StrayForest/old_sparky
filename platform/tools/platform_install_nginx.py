#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = PLATFORM_ROOT / "deploy/nginx/deadlock-platform.conf"
DEFAULT_SNIPPET_SOURCE = (
    PLATFORM_ROOT / "deploy/nginx/snippets/deadlock-platform-security-headers.conf"
)
DEFAULT_AVAILABLE = Path("/etc/nginx/sites-available/deadlock-platform")
DEFAULT_ENABLED = Path("/etc/nginx/sites-enabled/deadlock-platform")
DEFAULT_SNIPPET_DESTINATION = Path(
    "/etc/nginx/snippets/deadlock-platform-security-headers.conf"
)
DEFAULT_OLD_ENABLED = Path("/etc/nginx/sites-enabled/default")
# Debian's nginx.conf includes every path under sites-enabled via a wildcard,
# regardless of the filename suffix.  Keep the rollback link outside that
# directory so the old default server is truly disabled during validation.
DEFAULT_OLD_DISABLED = Path("/etc/nginx/sites-available/default.pre-domain")
REQUIRED_FILES = (
    Path("/etc/nginx/cloudflare-real-ip.conf"),
    Path("/opt/oldsparky/platform/shared/tls/old-sparky.com-origin.pem"),
    Path("/opt/oldsparky/platform/shared/tls/old-sparky.com-origin.key"),
)

EXPECTED_SECURITY_HEADER_DIRECTIVES = (
    'add_header X-Content-Type-Options "nosniff" always;',
    'add_header Referrer-Policy "strict-origin-when-cross-origin" always;',
    'add_header X-Frame-Options "DENY" always;',
    'add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=(), usb=(), clipboard-write=(self)" always;',
    'add_header Cross-Origin-Opener-Policy "same-origin" always;',
)
EXPECTED_REAL_IP_DIRECTIVES = (
    "include /etc/nginx/cloudflare-real-ip.conf;",
    "real_ip_header CF-Connecting-IP;",
    "real_ip_recursive on;",
)
EXPECTED_CSP_LIMIT_ZONE = (
    "limit_req_zone $binary_remote_addr zone=platform_csp_reports:1m rate=60r/m;"
)
EXPECTED_CSP_BODY_LIMIT = "client_max_body_size 32k;"
EXPECTED_CSP_LIMIT = "limit_req zone=platform_csp_reports burst=30 nodelay;"
EXPECTED_CSP_LIMIT_STATUS = "limit_req_status 429;"
EXPECTED_CSP_REPORT_METADATA_HEADERS = (
    "proxy_set_header X-Request-ID $request_id;",
    "proxy_set_header CF-Ray $http_cf_ray;",
)
EXPECTED_MEDIA_LIMIT_DIRECTIVES = (
    "limit_req_zone $binary_remote_addr zone=platform_media_uploads:1m rate=10r/m;",
    "limit_conn_zone $binary_remote_addr zone=platform_media_connections:1m;",
    "limit_req zone=platform_media_uploads burst=10 nodelay;",
    "limit_conn platform_media_connections 2;",
)
EXPECTED_TLS_DIRECTIVES = (
    "ssl_protocols TLSv1.2 TLSv1.3;",
    'ssl_ciphers "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-RSA-CHACHA20-POLY1305:ECDHE-RSA-AES256-GCM-SHA384";',
    "ssl_prefer_server_ciphers off;",
    "ssl_session_tickets off;",
)


@dataclass(frozen=True)
class PathSnapshot:
    kind: str
    data: bytes | None = None
    target: str | None = None
    mode: int | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the production Nginx candidate. Dry-run is the default; "
            "--apply installs it atomically but reloads only with --reload."
        )
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--snippet-source", type=Path, default=DEFAULT_SNIPPET_SOURCE)
    parser.add_argument("--available", type=Path, default=DEFAULT_AVAILABLE)
    parser.add_argument("--enabled", type=Path, default=DEFAULT_ENABLED)
    parser.add_argument(
        "--snippet-destination",
        type=Path,
        default=DEFAULT_SNIPPET_DESTINATION,
    )
    parser.add_argument("--nginx-bin", default="nginx")
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser.parse_args()


def run_checked(command: list[str]) -> None:
    completed = subprocess.run(command, text=True, capture_output=True)
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(message or f"Command failed: {' '.join(command)}")


def run_captured(command: list[str]) -> bytes:
    completed = subprocess.run(command, capture_output=True, check=False, timeout=15)
    if completed.returncode != 0:
        raise RuntimeError(f"Command failed safely: {command[0]} {command[1]}")
    return completed.stdout


def validate_cloudflare_ranges(path: Path) -> int:
    count = 0
    for raw_line in path.read_text(encoding="ascii").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        prefix = "set_real_ip_from "
        if not line.startswith(prefix) or not line.endswith(";"):
            raise ValueError("Cloudflare real-IP file contains an unexpected directive.")
        ipaddress.ip_network(line[len(prefix):-1].strip(), strict=True)
        count += 1
    if count < 20:
        raise ValueError("Cloudflare real-IP file is unexpectedly incomplete.")
    return count


def validate_certificate_pair(certificate: Path, private_key: Path) -> None:
    certificate_public_key = run_captured(
        ["openssl", "x509", "-in", str(certificate), "-pubkey", "-noout"]
    )
    private_public_key = run_captured(
        ["openssl", "pkey", "-in", str(private_key), "-pubout"]
    )
    if certificate_public_key != private_public_key:
        raise ValueError("Origin certificate and private key do not match.")
    run_checked(
        ["openssl", "x509", "-in", str(certificate), "-noout", "-checkhost", "old-sparky.com"]
    )
    run_checked(
        [
            "openssl",
            "x509",
            "-in",
            str(certificate),
            "-noout",
            "-checkhost",
            "media.old-sparky.com",
        ]
    )
    run_checked(
        ["openssl", "x509", "-in", str(certificate), "-noout", "-checkend", str(30 * 86_400)]
    )


def check_prerequisites() -> dict[str, str]:
    states: dict[str, str] = {}
    missing = [path for path in REQUIRED_FILES if not path.is_file()]
    if missing:
        raise FileNotFoundError("Missing required files: " + ", ".join(str(path) for path in missing))
    for path in REQUIRED_FILES:
        if path.is_symlink():
            raise PermissionError(f"Required runtime file must not be a symlink: {path}.")
        mode = stat.S_IMODE(path.stat().st_mode)
        states[str(path)] = f"{mode:04o}"
    key = REQUIRED_FILES[-1]
    if stat.S_IMODE(key.stat().st_mode) & 0o077:
        raise PermissionError(f"TLS private key permissions are too broad: {key}.")
    if key.stat().st_uid != 0 or key.stat().st_gid != 0:
        raise PermissionError("TLS private key must be owned by root:root.")
    states["cloudflare_ranges"] = str(validate_cloudflare_ranges(REQUIRED_FILES[0]))
    validate_certificate_pair(REQUIRED_FILES[1], key)
    states["origin_certificate"] = "matching-hosts-valid-30d"
    return states


def _validate_header_include_scopes(vhost_text: str, include_directive: str) -> None:
    scopes: list[dict[str, object]] = []
    tls_server_count = 0
    for line_number, raw_line in enumerate(vhost_text.splitlines(), start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.endswith("{"):
            scopes.append(
                {
                    "opener": line[:-1].strip(),
                    "include_count": 0,
                    "add_header": False,
                    "tls": False,
                    "line": line_number,
                }
            )
            continue
        if line == "}":
            if not scopes:
                raise ValueError(f"Unexpected closing brace at vhost line {line_number}.")
            scope = scopes.pop()
            opener = str(scope["opener"])
            if (
                opener.startswith("location ")
                and scope["add_header"]
                and scope["include_count"] != 1
            ):
                raise ValueError(
                    "Nginx location with a local add_header must include the shared "
                    "security headers exactly once "
                    f"(line {scope['line']}: {opener})."
                )
            if opener == "server" and scope["tls"]:
                tls_server_count += 1
                if scope["include_count"] != 1:
                    raise ValueError(
                        "TLS server must include the shared security headers exactly once."
                    )
            continue
        if not scopes:
            continue
        current = scopes[-1]
        if line == include_directive:
            current["include_count"] = int(current["include_count"]) + 1
        elif line.startswith("add_header "):
            current["add_header"] = True
        elif line.startswith("listen ") and re.match(
            r"^listen\s+(?:\[[^]]+\]:)?443(?:\s|;)", line
        ):
            current["tls"] = True

    if scopes:
        raise ValueError("Nginx vhost contains an unclosed block.")
    if tls_server_count != 1:
        raise ValueError(f"Expected exactly one TLS server, found {tls_server_count}.")


def _extract_exact_blocks(vhost_text: str, opener: str) -> list[tuple[str, ...]]:
    blocks: list[tuple[str, ...]] = []
    active_lines: list[str] | None = None
    depth = 0
    expected_opener = f"{opener} {{"
    for raw_line in vhost_text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if active_lines is None:
            if line == expected_opener:
                active_lines = []
                depth = 1
            continue
        opening_count = line.count("{")
        closing_count = line.count("}")
        depth += opening_count - closing_count
        if depth == 0:
            blocks.append(tuple(active_lines))
            active_lines = None
            continue
        if line:
            active_lines.append(line)
    if active_lines is not None:
        raise ValueError(f"Nginx block is unclosed: {opener}.")
    return blocks


def _validate_runtime_contract(vhost_text: str, vhost_lines: tuple[str, ...]) -> None:
    for directive in EXPECTED_REAL_IP_DIRECTIVES:
        if vhost_lines.count(directive) != 1:
            raise ValueError(
                f"Nginx Cloudflare real-IP policy requires exactly one `{directive}` directive."
            )
    real_ip_directives = [
        line
        for line in vhost_lines
        if line.startswith("real_ip_header ")
        or line.startswith("real_ip_recursive ")
        or line == EXPECTED_REAL_IP_DIRECTIVES[0]
    ]
    if sorted(real_ip_directives) != sorted(EXPECTED_REAL_IP_DIRECTIVES):
        raise ValueError("Nginx Cloudflare real-IP policy contains an unexpected directive.")
    if any(line.startswith("set_real_ip_from ") for line in vhost_lines):
        raise ValueError("Cloudflare IP ranges must remain owned by the validated include file.")

    if vhost_lines.count(EXPECTED_CSP_LIMIT_ZONE) != 1:
        raise ValueError("Nginx CSP report limit zone must be exactly 60 requests per minute.")
    csp_zone_references = [
        line for line in vhost_lines if "zone=platform_csp_reports" in line
    ]
    if sorted(csp_zone_references) != sorted((EXPECTED_CSP_LIMIT_ZONE, EXPECTED_CSP_LIMIT)):
        raise ValueError("Nginx CSP report limiter contains an unexpected directive.")

    report_blocks = _extract_exact_blocks(
        vhost_text,
        "location = /api/v1/security/csp-report",
    )
    if len(report_blocks) != 1:
        raise ValueError("Nginx must define exactly one exact-match CSP report location.")
    report_block = report_blocks[0]
    if report_block.count(EXPECTED_CSP_BODY_LIMIT) != 1:
        raise ValueError("Nginx CSP report bodies must remain limited to 32 KiB.")
    if report_block.count(EXPECTED_CSP_LIMIT) != 1:
        raise ValueError("Nginx CSP report location must use burst=30 with nodelay.")
    if report_block.count(EXPECTED_CSP_LIMIT_STATUS) != 1:
        raise ValueError("Nginx CSP report rate limiting must return HTTP 429.")
    if vhost_lines.count(EXPECTED_CSP_LIMIT_STATUS) != 1:
        raise ValueError("HTTP 429 rate-limit status must remain scoped to CSP reports.")
    for directive in EXPECTED_CSP_REPORT_METADATA_HEADERS:
        if report_block.count(directive) != 1:
            raise ValueError(
                "Nginx CSP report forwarding must preserve request correlation metadata."
            )

    for directive in EXPECTED_MEDIA_LIMIT_DIRECTIVES:
        if vhost_lines.count(directive) != 1:
            raise ValueError(
                f"Nginx media rate policy requires exactly one `{directive}` directive."
            )
    media_limit_references = [
        line
        for line in vhost_lines
        if "platform_media_uploads" in line or "platform_media_connections" in line
    ]
    if sorted(media_limit_references) != sorted(EXPECTED_MEDIA_LIMIT_DIRECTIVES):
        raise ValueError("Nginx media rate policy contains an unexpected directive.")


def validate_security_header_contract(
    source: Path,
    snippet_source: Path,
    snippet_destination: Path,
) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"Nginx candidate is missing: {source}.")
    if not snippet_source.is_file():
        raise FileNotFoundError(f"Nginx security-header snippet is missing: {snippet_source}.")
    if not snippet_destination.is_absolute():
        raise ValueError("Nginx snippet destination must be an absolute path.")

    snippet_lines = tuple(
        line.strip()
        for line in snippet_source.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    if snippet_lines != EXPECTED_SECURITY_HEADER_DIRECTIVES:
        raise ValueError("Nginx security-header snippet does not match the approved policy.")

    vhost_text = source.read_text(encoding="utf-8")
    vhost_lines = tuple(
        line.strip()
        for line in vhost_text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    for directive in EXPECTED_TLS_DIRECTIVES:
        if vhost_lines.count(directive) != 1:
            raise ValueError(
                f"Nginx TLS policy requires exactly one `{directive}` directive."
            )
    if sum(line.startswith("ssl_ciphers ") for line in vhost_lines) != 1:
        raise ValueError("Nginx TLS policy must have exactly one TLS 1.2 cipher allowlist.")
    combined_text = f"{vhost_text}\n{snippet_source.read_text(encoding='utf-8')}"
    if "'unsafe-" in combined_text:
        raise ValueError("Nginx security policy must not contain unsafe CSP sources.")
    if re.search(
        r"(?im)^\s*add_header\s+Content-Security-Policy(?:-Report-Only)?\s+",
        combined_text,
    ):
        raise ValueError("Nginx must not own enforced or Report-Only CSP headers.")
    if re.search(r"(?im)^\s*add_header\s+Strict-Transport-Security\s+", combined_text):
        raise ValueError("HSTS is not approved for this release stage.")
    for directive in EXPECTED_SECURITY_HEADER_DIRECTIVES:
        header_name = directive.split(maxsplit=2)[1]
        if re.search(
            rf"(?im)^\s*add_header\s+{re.escape(header_name)}\s+",
            vhost_text,
        ):
            raise ValueError(
                f"Security header {header_name} must be owned by the shared snippet."
            )

    include_directive = f"include {snippet_destination};"
    _validate_header_include_scopes(vhost_text, include_directive)
    _validate_runtime_contract(vhost_text, vhost_lines)


def validate_candidate(
    source: Path,
    nginx_bin: str,
    *,
    snippet_source: Path = DEFAULT_SNIPPET_SOURCE,
    snippet_destination: Path = DEFAULT_SNIPPET_DESTINATION,
) -> None:
    validate_security_header_contract(source, snippet_source, snippet_destination)
    with tempfile.TemporaryDirectory(prefix="oldsparky-nginx-check-") as directory:
        staged_snippet = Path(directory) / "candidate-security-headers.conf"
        shutil.copyfile(snippet_source, staged_snippet)
        include_directive = f"include {snippet_destination};"
        staged_source = Path(directory) / "candidate-vhost.conf"
        staged_vhost = source.read_text(encoding="utf-8").replace(
            include_directive,
            f"include {staged_snippet};",
        )
        staged_source.write_text(staged_vhost, encoding="utf-8")
        main_config = Path(directory) / "nginx.conf"
        main_config.write_text(
            "worker_processes 1;\n"
            f"pid {directory}/nginx.pid;\n"
            "error_log stderr notice;\n"
            "events { worker_connections 128; }\n"
            "http {\n"
            "  include /etc/nginx/mime.types;\n"
            "  default_type application/octet-stream;\n"
            f"  include {staged_source};\n"
            "}\n",
            encoding="utf-8",
        )
        run_checked([nginx_bin, "-t", "-c", str(main_config)])


def atomic_copy(source: Path, destination: Path) -> None:
    atomic_write(source.read_bytes(), destination, mode=0o644)


def atomic_write(data: bytes, destination: Path, *, mode: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(data)
    try:
        temporary.chmod(mode)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_symlink(target: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    temporary.unlink()
    try:
        temporary.symlink_to(target)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def snapshot_path(path: Path) -> PathSnapshot:
    if path.is_symlink():
        return PathSnapshot(kind="symlink", target=os.readlink(path))
    if not path.exists():
        return PathSnapshot(kind="missing")
    if not path.is_file():
        raise ValueError(f"Nginx install path must be a file or symlink: {path}.")
    return PathSnapshot(
        kind="file",
        data=path.read_bytes(),
        mode=stat.S_IMODE(path.stat().st_mode),
    )


def restore_path(path: Path, snapshot: PathSnapshot) -> None:
    if path.exists() and path.is_dir() and not path.is_symlink():
        raise ValueError(f"Cannot restore over directory: {path}.")
    path.unlink(missing_ok=True)
    if snapshot.kind == "missing":
        return
    if snapshot.kind == "symlink":
        assert snapshot.target is not None
        atomic_symlink(snapshot.target, path)
        return
    if snapshot.kind == "file":
        assert snapshot.data is not None and snapshot.mode is not None
        atomic_write(snapshot.data, path, mode=snapshot.mode)
        return
    raise ValueError(f"Unknown path snapshot kind: {snapshot.kind}.")


def install(
    source: Path,
    available: Path,
    enabled: Path,
    nginx_bin: str,
    reload_nginx: bool,
    *,
    snippet_source: Path = DEFAULT_SNIPPET_SOURCE,
    snippet_destination: Path = DEFAULT_SNIPPET_DESTINATION,
) -> bool:
    if os.geteuid() != 0:
        raise PermissionError("--apply requires root.")
    destination_paths = (available, enabled, snippet_destination)
    if len(set(destination_paths)) != len(destination_paths):
        raise ValueError("Nginx vhost, enabled link and snippet destinations must be distinct.")

    previous_available = snapshot_path(available)
    previous_enabled = snapshot_path(enabled)
    previous_snippet = snapshot_path(snippet_destination)
    moved_default = False
    old_default_enabled = DEFAULT_OLD_ENABLED.exists() or DEFAULT_OLD_ENABLED.is_symlink()
    changed = (
        previous_available.kind != "file"
        or previous_available.data != source.read_bytes()
        or previous_available.mode != 0o644
        or previous_enabled.kind != "symlink"
        or previous_enabled.target != str(available)
        or previous_snippet.kind != "file"
        or previous_snippet.data != snippet_source.read_bytes()
        or previous_snippet.mode != 0o644
        or old_default_enabled
    )
    if not changed:
        if reload_nginx:
            run_checked(["systemctl", "reload", "nginx.service"])
        return False
    if old_default_enabled and (
        DEFAULT_OLD_DISABLED.exists() or DEFAULT_OLD_DISABLED.is_symlink()
    ):
        raise FileExistsError(f"Rollback path already exists: {DEFAULT_OLD_DISABLED}.")

    try:
        if old_default_enabled:
            os.replace(DEFAULT_OLD_ENABLED, DEFAULT_OLD_DISABLED)
            moved_default = True
        atomic_copy(source, available)
        atomic_copy(snippet_source, snippet_destination)
        atomic_symlink(str(available), enabled)
        run_checked([nginx_bin, "-t"])
        if reload_nginx:
            run_checked(["systemctl", "reload", "nginx.service"])
        return True
    except Exception as install_error:
        rollback_errors: list[str] = []
        for path, snapshot in (
            (enabled, previous_enabled),
            (available, previous_available),
            (snippet_destination, previous_snippet),
        ):
            try:
                restore_path(path, snapshot)
            except Exception as exc:  # pragma: no cover - catastrophic filesystem failure
                rollback_errors.append(f"{path}: {exc}")
        try:
            if moved_default and (
                DEFAULT_OLD_DISABLED.exists() or DEFAULT_OLD_DISABLED.is_symlink()
            ):
                os.replace(DEFAULT_OLD_DISABLED, DEFAULT_OLD_ENABLED)
        except Exception as exc:  # pragma: no cover - catastrophic filesystem failure
            rollback_errors.append(f"default vhost: {exc}")
        if rollback_errors:
            raise RuntimeError(
                f"Nginx install failed ({install_error}); rollback also failed: "
                + "; ".join(rollback_errors)
            ) from install_error
        raise


def main() -> int:
    args = parse_args()
    if args.reload and not args.apply:
        raise ValueError("--reload requires --apply.")
    prerequisites = check_prerequisites()
    validate_candidate(
        args.source,
        args.nginx_bin,
        snippet_source=args.snippet_source,
        snippet_destination=args.snippet_destination,
    )
    changed = False
    if args.apply:
        changed = install(
            args.source,
            args.available,
            args.enabled,
            args.nginx_bin,
            args.reload,
            snippet_source=args.snippet_source,
            snippet_destination=args.snippet_destination,
        )
    result = {
        "ok": True,
        "mode": "apply" if args.apply else "dry-run",
        "candidate": str(args.source),
        "snippet_candidate": str(args.snippet_source),
        "available": str(args.available),
        "enabled": str(args.enabled),
        "snippet_destination": str(args.snippet_destination),
        "changed": changed,
        "reload_requested": bool(args.reload),
        "prerequisites": prerequisites,
    }
    if args.as_json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"Nginx candidate valid: mode={result['mode']}; changed={changed}.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Nginx installation check failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
