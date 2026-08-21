#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import grp
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile


DEFAULT_HTPASSWD = Path("/etc/nginx/oldsparky.htpasswd")
DEFAULT_CREDENTIALS = Path(
    "/opt/oldsparky/platform/shared/secrets/closed-launch-basic-auth.txt"
)
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create the temporary closed-production Basic Auth credential. "
            "Dry-run is the default and secret values are never printed."
        )
    )
    parser.add_argument("--username", default="closed-launch")
    parser.add_argument("--htpasswd-file", type=Path, default=DEFAULT_HTPASSWD)
    parser.add_argument("--credentials-file", type=Path, default=DEFAULT_CREDENTIALS)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rotate", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser.parse_args()


def validate_username(value: str) -> str:
    if not USERNAME_PATTERN.fullmatch(value):
        raise ValueError("Username must be 3-32 ASCII letters, digits, dots, dashes, or underscores.")
    return value


def apache_md5_hash(password: str, *, openssl_bin: str = "openssl") -> str:
    result = subprocess.run(
        [openssl_bin, "passwd", "-apr1", "-stdin"],
        input=password + "\n",
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    digest = result.stdout.strip()
    if result.returncode != 0 or not digest.startswith("$apr1$"):
        raise RuntimeError("OpenSSL could not generate the Basic Auth password digest.")
    return digest


def atomic_write(path: Path, content: str, *, mode: int, uid: int, gid: int) -> None:
    if path.is_symlink():
        raise RuntimeError(f"Refusing to replace symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        os.fchmod(handle.fileno(), mode)
        os.fchown(handle.fileno(), uid, gid)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def ensure_secret_directory(path: Path) -> None:
    directory = path.parent
    if directory.is_symlink():
        raise RuntimeError(f"Refusing symlinked secret directory: {directory}")
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    if stat.S_IMODE(directory.stat().st_mode) & 0o077:
        directory.chmod(0o700)
    os.chown(directory, 0, 0)


def apply_credentials(
    *,
    username: str,
    htpasswd_file: Path,
    credentials_file: Path,
    rotate: bool,
) -> dict[str, object]:
    if os.geteuid() != 0:
        raise PermissionError("--apply requires root.")
    existing = [path for path in (htpasswd_file, credentials_file) if path.exists()]
    if existing and not rotate:
        raise FileExistsError("Credential files already exist; use --rotate for intentional replacement.")
    ensure_secret_directory(credentials_file)
    password = secrets.token_urlsafe(32)
    digest = apache_md5_hash(password)
    created_at = datetime.now(UTC).isoformat()
    www_data_gid = grp.getgrnam("www-data").gr_gid
    atomic_write(
        htpasswd_file,
        f"{username}:{digest}\n",
        mode=0o640,
        uid=0,
        gid=www_data_gid,
    )
    atomic_write(
        credentials_file,
        f"username={username}\npassword={password}\ncreated_at_utc={created_at}\n",
        mode=0o600,
        uid=0,
        gid=0,
    )
    return {
        "ok": True,
        "mode": "rotate" if existing else "create",
        "username": username,
        "htpasswd_file": str(htpasswd_file),
        "credentials_file": str(credentials_file),
        "secret_printed": False,
    }


def main() -> int:
    args = parse_args()
    username = validate_username(args.username)
    if args.rotate and not args.apply:
        raise ValueError("--rotate requires --apply.")
    if args.apply:
        result = apply_credentials(
            username=username,
            htpasswd_file=args.htpasswd_file,
            credentials_file=args.credentials_file,
            rotate=args.rotate,
        )
    else:
        result = {
            "ok": True,
            "mode": "dry-run",
            "username": username,
            "htpasswd_file": str(args.htpasswd_file),
            "credentials_file": str(args.credentials_file),
            "secret_printed": False,
        }
    if args.as_json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(
            "Closed-production credential prepared: "
            f"mode={result['mode']}; username={username}; secret_printed=false."
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Closed-production credential failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
