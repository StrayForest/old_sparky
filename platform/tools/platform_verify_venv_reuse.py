#!/usr/bin/env python3
"""Fail-closed proof that an existing shared venv is unchanged and reusable."""

from __future__ import annotations

import argparse
import base64
import csv
from email.parser import BytesParser
from email.policy import compat32
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import stat
import subprocess
import sys
import sysconfig
import tempfile
import types
import zipfile

PINNED_PYTHON_SHA256 = "e50d468e8b0adfb05733f5b87b3cff34829c4a8c1aea50c865aa8bdfe4bb150f"


class ReuseRefused(RuntimeError):
    pass


def _compile_source(source: bytes, filename: str) -> types.CodeType:
    # The verifier module has future annotations enabled; never let those
    # verifier-local compiler flags alter the installed source comparison.
    return compile(source, filename, "exec", dont_inherit=True)


def _regular(path: Path, *, mode: int | None = None, max_size: int = 16 * 1024 * 1024) -> bytes:
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) & 0o022 or info.st_size > max_size
                or (mode is not None and stat.S_IMODE(info.st_mode) != mode)):
            raise ReuseRefused("metadata")
        return path.read_bytes()
    except OSError as exc:
        raise ReuseRefused("missing") from exc


def _stable_private_regular(path: Path, *, mode: int, max_size: int = 4096) -> bytes:
    """Read a root-private receipt without following or racing its pathname."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    fd = -1
    try:
        fd = os.open(path, flags)
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or before.st_gid != 0
                or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != mode
                or before.st_size <= 0 or before.st_size > max_size):
            raise ReuseRefused("origin_receipt_metadata")
        raw = bytearray()
        while len(raw) <= max_size:
            chunk = os.read(fd, min(4096, max_size + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(fd)
        named = path.lstat()
        def identity(item: os.stat_result) -> tuple[int, ...]:
            return (
                item.st_dev, item.st_ino, item.st_mode, item.st_uid, item.st_gid,
                item.st_nlink, item.st_size, item.st_mtime_ns, item.st_ctime_ns,
            )
        if (len(raw) != before.st_size or identity(after) != identity(before)
                or identity(named) != identity(before)):
            raise ReuseRefused("origin_receipt_changed")
        return bytes(raw)
    except OSError as exc:
        raise ReuseRefused("origin_receipt_metadata") from exc
    finally:
        if fd >= 0:
            os.close(fd)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ReuseRefused("origin_receipt_schema")
        result[key] = value
    return result


def _release_identity(release: Path, releases: Path) -> tuple[str, str, str]:
    """Return (slug, source SHA, RELEASE.json SHA) for one direct real release."""
    try:
        info = release.lstat()
        releases_real = releases.resolve(strict=True)
        if (release.parent.resolve(strict=True) != releases_real
                or stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != 0 or info.st_gid != 0
                or stat.S_IMODE(info.st_mode) & (0o022 | 0o7000)):
            raise ReuseRefused("release_identity")
        slug = release.name
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,179}", slug) is None:
            raise ReuseRefused("release_identity")
        raw = _regular(release / "RELEASE.json", max_size=65536)
        metadata = json.loads(raw, object_pairs_hook=_unique_object)
        source = metadata.get("source_git_commit") if isinstance(metadata, dict) else None
        if (not isinstance(source, str)
                or re.fullmatch(r"[0-9a-f]{40,64}", source) is None
                or metadata.get("release_slug") != slug):
            raise ReuseRefused("release_identity")
        return slug, source, hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ReuseRefused("release_identity") from exc


def _release_rollback(release: Path) -> Path:
    rollback = release / ".rollback"
    try:
        info = rollback.lstat()
    except OSError as exc:
        raise ReuseRefused("transaction_identity") from exc
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != 0 or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ReuseRefused("transaction_identity")
    return rollback


def _activation_scripts_digest(scripts: dict[Path, tuple[bytes, int]]) -> str:
    digest = hashlib.sha256(b"oldsparky-venv-activation-v1\0")
    for path, (content, mode) in sorted(scripts.items(), key=lambda item: item[0].name):
        name = path.name.encode("ascii")
        digest.update(len(name).to_bytes(2, "big"))
        digest.update(name)
        digest.update(mode.to_bytes(2, "big"))
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


_ORIGIN_RECEIPT_FIELDS = frozenset({
    "schema", "release_slug", "release_source_sha", "release_json_sha256",
    "venv_dev", "venv_ino", "freeze_sha256", "wheelhouse_manifest_sha256",
    "origin_release_slug", "origin_source_sha", "origin_release_json_sha256",
    "activation_sha256",
})


def _read_origin_receipt(
    release: Path,
    releases: Path,
    venv: Path,
) -> dict[str, object] | None:
    path = release / ".rollback" / "venv-origin.json"
    if not path.exists() and not path.is_symlink():
        return None
    raw = _stable_private_regular(path, mode=0o600)
    try:
        payload = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReuseRefused("origin_receipt_schema") from exc
    if not isinstance(payload, dict) or set(payload) != _ORIGIN_RECEIPT_FIELDS:
        raise ReuseRefused("origin_receipt_schema")
    if type(payload.get("schema")) is not int or payload["schema"] != 1:
        raise ReuseRefused("origin_receipt_schema")
    slug, source, release_sha = _release_identity(release, releases)
    venv_info = venv.lstat()
    if (stat.S_ISLNK(venv_info.st_mode) or not stat.S_ISDIR(venv_info.st_mode)
            or venv_info.st_uid != 0 or venv_info.st_gid != 0
            or stat.S_IMODE(venv_info.st_mode) != 0o755):
        raise ReuseRefused("venv_directory")
    expected_hashes = {
        "release_source_sha": source,
        "release_json_sha256": release_sha,
        "freeze_sha256": _sha(release / "requirements-platform.freeze.txt"),
        "wheelhouse_manifest_sha256": _sha(release / "wheelhouse" / "WHEELHOUSE.sha256"),
    }
    for name, expected in expected_hashes.items():
        if payload.get(name) != expected:
            raise ReuseRefused("origin_receipt_binding")
    if (payload.get("release_slug") != slug
            or type(payload.get("venv_dev")) is not int
            or type(payload.get("venv_ino")) is not int
            or payload["venv_dev"] != venv_info.st_dev
            or payload["venv_ino"] != venv_info.st_ino):
        raise ReuseRefused("origin_receipt_binding")
    origin_slug = payload.get("origin_release_slug")
    origin_source = payload.get("origin_source_sha")
    if (not isinstance(origin_slug, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,179}", origin_slug) is None
            or not isinstance(origin_source, str)
            or re.fullmatch(r"[0-9a-f]{40,64}", origin_source) is None):
        raise ReuseRefused("origin_receipt_schema")
    for name in ("freeze_sha256", "wheelhouse_manifest_sha256",
                 "origin_release_json_sha256", "activation_sha256"):
        value = payload.get(name)
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ReuseRefused("origin_receipt_schema")
    return payload


def _derive_venv_origin(
    current: Path,
    app: Path,
    venv: Path,
    *,
    max_hops: int = 32,
) -> tuple[str, str, str, dict[str, object] | None]:
    """Resolve the generation slug from an anchor or a bounded legacy chain."""
    releases = app / "releases"
    current_slug, current_source, _current_sha = _release_identity(current, releases)
    transition = _stable_private_regular(
        _release_rollback(current) / "venv-transition", mode=0o600, max_size=32,
    )
    if transition not in (b"snapshot\n", b"unchanged\n"):
        raise ReuseRefused("transaction_identity")
    receipt = _read_origin_receipt(current, releases, venv)
    if receipt is not None:
        if transition != b"unchanged\n":
            raise ReuseRefused("origin_receipt_binding")
        return (
            str(receipt["origin_release_slug"]),
            str(receipt["origin_source_sha"]),
            str(receipt["origin_release_json_sha256"]),
            receipt,
        )

    visited: set[tuple[int, int]] = set()
    release = current
    for _ in range(max_hops):
        slug, source, release_sha = _release_identity(release, releases)
        info = release.lstat()
        identity = (info.st_dev, info.st_ino)
        if identity in visited:
            raise ReuseRefused("origin_chain_cycle")
        visited.add(identity)
        rollback = _release_rollback(release)
        transition = _stable_private_regular(
            rollback / "venv-transition", mode=0o600, max_size=32,
        )
        if transition == b"snapshot\n":
            snapshot = rollback / "shared-venv-before-install"
            try:
                snapshot_info = snapshot.lstat()
            except OSError as exc:
                raise ReuseRefused("transaction_identity") from exc
            if (stat.S_ISLNK(snapshot_info.st_mode) or not stat.S_ISDIR(snapshot_info.st_mode)
                    or snapshot_info.st_uid != 0 or snapshot_info.st_gid != 0):
                raise ReuseRefused("transaction_identity")
            return slug, source, release_sha, None
        if transition != b"unchanged\n":
            raise ReuseRefused("transaction_identity")
        snapshot = rollback / "shared-venv-before-install"
        if snapshot.exists() or snapshot.is_symlink():
            raise ReuseRefused("transaction_identity")
        freeze_receipt = _stable_private_regular(
            rollback / "shared-freeze.sha256", mode=0o600, max_size=128,
        ).decode("ascii").strip()
        if freeze_receipt != _sha(release / "requirements-platform.freeze.txt"):
            raise ReuseRefused("transaction_identity")
        previous_raw = _stable_private_regular(
            rollback / "previous-release", mode=0o600, max_size=4096,
        )
        try:
            previous_text = previous_raw.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise ReuseRefused("transaction_identity") from exc
        previous = Path(previous_text)
        if (not previous.is_absolute() or previous.parent != releases
                or previous.name == release.name):
            raise ReuseRefused("transaction_identity")
        _release_identity(previous, releases)
        release = previous
    raise ReuseRefused("origin_chain_limit")


def _sha(path: Path) -> str:
    return hashlib.sha256(_regular(path)).hexdigest()


def _same_dependency_inputs(old: Path, new: Path) -> None:
    for relative in (
        "requirements-platform.txt", "requirements-platform.lock.txt",
        "requirements-platform.freeze.txt", "wheelhouse/WHEELHOUSE.sha256",
    ):
        if _regular(old / relative) != _regular(new / relative):
            raise ReuseRefused("dependency_inputs")
    old_wheels = sorted(p.name for p in (old / "wheelhouse").glob("*.whl"))
    new_wheels = sorted(p.name for p in (new / "wheelhouse").glob("*.whl"))
    if not old_wheels or old_wheels != new_wheels:
        raise ReuseRefused("wheel_set")
    for name in old_wheels:
        if _sha(old / "wheelhouse" / name) != _sha(new / "wheelhouse" / name):
            raise ReuseRefused("wheel_hash")


def _runtime(venv: Path, expected_python: Path) -> None:
    if not expected_python.is_file() or expected_python.is_symlink():
        raise ReuseRefused("base_python")
    base = expected_python.resolve(strict=True)
    base_info = base.stat()
    if (base != expected_python or not stat.S_ISREG(base_info.st_mode)
            or base_info.st_uid != 0 or stat.S_IMODE(base_info.st_mode) != 0o755
            or stat.S_IMODE(base_info.st_mode) & 0o7000
            or hashlib.sha256(base.read_bytes()).hexdigest() != PINNED_PYTHON_SHA256
            or Path(sys.executable).resolve(strict=True) != base):
        raise ReuseRefused("base_python")
    for directory in (
        venv, venv / "bin", venv / "lib", venv / "lib" / "python3.12",
        venv / "lib" / "python3.12" / "site-packages",
    ):
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o755
                or stat.S_IMODE(info.st_mode) & 0o7000):
            raise ReuseRefused("venv_directory")
    py = venv / "bin/python"
    if not py.is_symlink() or py.resolve(strict=True) != base:
        raise ReuseRefused("venv_python")
    for alias in ("python3", "python3.12"):
        link = venv / "bin" / alias
        if not link.is_symlink() or link.resolve(strict=True) != base:
            raise ReuseRefused("venv_python")
    if (sys.version_info[:2] != (3, 12) or sys.implementation.cache_tag != "cpython-312"
            or sysconfig.get_config_var("SOABI") != "cpython-312-x86_64-linux-gnu"
            or sysconfig.get_platform() != "linux-x86_64"):
        raise ReuseRefused("runtime_abi")
    cfg = venv / "pyvenv.cfg"
    raw = _regular(cfg)
    values: dict[str, str] = {}
    for line in raw.decode("utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip().lower()] = value.strip()
    if (values.get("home") != str(base.parent) or values.get("include-system-site-packages") != "false"
            or values.get("version") != "3.12.3"):
        raise ReuseRefused("venv_config")


def _expected_console_scripts(venv: Path, site_root: Path) -> dict[Path, bytes]:
    with tempfile.TemporaryDirectory(prefix="venv-reuse-scripts-") as temporary:
        output = Path(temporary)
        code = (
            "import sys; from pip._internal.metadata import get_default_environment; "
            "from pip._internal.operations.install.wheel import get_console_script_specs,get_entrypoints; "
            "from pip._internal.operations.install.wheel import PipScriptMaker; "
            "m=PipScriptMaker(None,sys.argv[1]); m.clobber=True; m.variants={''}; m.set_mode=True; "
            "[(m.make_multiple(get_console_script_specs(get_entrypoints(d)[0]))) "
            "for d in get_default_environment().iter_installed_distributions()]"
        )
        result = subprocess.run(
            [str(venv / "bin/python"), "-I", "-B", "-c", code, str(output), str(site_root)],
            env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LANG": "C.UTF-8",
                 "LC_ALL": "C.UTF-8", "PIP_CONFIG_FILE": "/dev/null",
                 "PYTHONDONTWRITEBYTECODE": "1"},
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False,
        )
        if result.returncode != 0:
            raise ReuseRefused("script_generator")
        return {venv / "bin" / path.name: path.read_bytes() for path in output.iterdir() if path.is_file()}


def _render_activation_scripts(venv: Path, temporary_name: str) -> dict[Path, tuple[bytes, int]]:
    import venv as venv_module
    from types import SimpleNamespace

    templates = Path(venv_module.__file__).parent / "scripts"
    builder = venv_module.EnvBuilder()
    context = SimpleNamespace(
        env_dir=str(venv), env_name=temporary_name, prompt=f"({temporary_name}) ",
        bin_name="bin", env_exe=str(venv / "bin" / "python3.12"),
    )
    result: dict[Path, tuple[bytes, int]] = {}
    for source, name in (
        (templates / "common/activate", "activate"),
        (templates / "posix/activate.csh", "activate.csh"),
        (templates / "posix/activate.fish", "activate.fish"),
        (templates / "common/Activate.ps1", "Activate.ps1"),
    ):
        context.script_path = str(source)
        try:
            raw = source.read_bytes().decode("utf-8")
            expected = builder.replace_variables(raw, context).encode("utf-8")
            mode = stat.S_IMODE(source.stat().st_mode)
        except (OSError, UnicodeError, ValueError) as exc:
            raise ReuseRefused("activation_template") from exc
        result[venv / "bin" / name] = (expected, mode)
    return result


def _expected_activation_scripts(venv: Path, release_slug: str) -> dict[Path, tuple[bytes, int]]:
    activation = venv / "bin" / "activate"
    raw = _regular(activation).decode("utf-8")
    prompt_pattern = re.compile(
        r"^[ \t]*VIRTUAL_ENV_PROMPT='\((\.venv-install-"
        + re.escape(release_slug)
        + r"\.[A-Za-z0-9]{6})\) '\s*$",
        re.MULTILINE,
    )
    prompts = prompt_pattern.findall(raw)
    if len(prompts) != 1:
        raise ReuseRefused("activation_context")
    return _render_activation_scripts(venv, prompts[0])


def _wheel_record_index(wheelhouse: Path, site_root: Path, venv: Path) -> tuple[dict[Path, tuple[str, str]], dict[Path, bytes]]:
    indexed: dict[Path, tuple[str, str]] = {}
    data_scripts: dict[Path, bytes] = {}
    for wheel in wheelhouse.glob("*.whl"):
        try:
            with zipfile.ZipFile(wheel) as archive:
                record_names = [name for name in archive.namelist() if name.endswith(".dist-info/RECORD")]
                if len(record_names) != 1:
                    raise ReuseRefused("wheel_record")
                record_name = record_names[0]
                metadata_names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
                if len(metadata_names) != 1:
                    raise ReuseRefused("wheel_metadata")
                metadata = BytesParser(policy=compat32).parsebytes(archive.read(metadata_names[0]))
                distribution_name = metadata.get("Name")
                if not distribution_name:
                    raise ReuseRefused("wheel_metadata")
                header_name = re.sub(r"[-_.]+", "-", distribution_name).lower()
                for row in csv.reader(archive.read(record_name).decode("utf-8").splitlines()):
                    if len(row) != 3:
                        raise ReuseRefused("wheel_record")
                    member, digest, size = row
                    parts = PurePosixPath(member).parts
                    if not parts or PurePosixPath(member).is_absolute() or ".." in parts:
                        raise ReuseRefused("wheel_record_path")
                    if len(parts) >= 3 and parts[0].endswith(".data"):
                        scheme, *suffix = parts[1:]
                        if scheme in {"purelib", "platlib"}:
                            target = site_root.joinpath(*suffix)
                        elif scheme == "scripts":
                            target = venv / "bin" / Path(*suffix).name
                            content = archive.read(member)
                            if content.startswith(b"#!python"):
                                data_scripts[target] = (
                                    b"#!" + str(venv / "bin/python").encode()
                                    + content[len(b"#!python"):]
                                )
                        elif scheme == "data":
                            target = venv.joinpath(*suffix)
                        elif scheme == "headers":
                            target = (venv / "include" / "site" / "python3.12"
                                      / header_name / Path(*suffix))
                        else:
                            raise ReuseRefused("wheel_record_scheme")
                    else:
                        target = site_root.joinpath(*parts)
                    target = target.resolve(strict=False)
                    if target in indexed:
                        raise ReuseRefused("wheel_record_duplicate")
                    indexed[target] = (digest, size)
        except (OSError, UnicodeError, zipfile.BadZipFile, KeyError) as exc:
            raise ReuseRefused("wheel_record") from exc
    if not indexed:
        raise ReuseRefused("wheel_record")
    return indexed, data_scripts


def _safe_record_target(root: Path, relative: str, venv: Path) -> Path:
    item = PurePosixPath(relative)
    if item.is_absolute() or not item.parts:
        raise ReuseRefused("record_path")
    current = root
    for index, component in enumerate(item.parts):
        if component == ".":
            continue
        if component == "..":
            current = current.parent
        else:
            current = current / component
        try:
            current.relative_to(venv)
        except ValueError as exc:
            raise ReuseRefused("record_path") from exc
        if index < len(item.parts) - 1 or current.exists():
            try:
                info = current.lstat()
            except OSError as exc:
                raise ReuseRefused("record_path") from exc
            if stat.S_ISLNK(info.st_mode):
                raise ReuseRefused("record_path")
            if index < len(item.parts) - 1 and (
                not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) & (0o022 | 0o7000)
            ):
                raise ReuseRefused("record_path")
    return current.resolve(strict=False)


def _venv_integrity(venv: Path, wheelhouse: Path, release_slug: str) -> str:
    site_roots = sorted((venv / "lib").glob("python*/site-packages"))
    if len(site_roots) != 1:
        raise ReuseRefused("site_packages")
    root = site_roots[0]
    directory = venv
    for component in root.relative_to(venv).parts:
        directory = directory / component
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o755
                or stat.S_IMODE(info.st_mode) & 0o7000):
            raise ReuseRefused("site_packages_metadata")
    root_info = root.lstat()
    if (not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode)
            or root_info.st_uid != 0 or stat.S_IMODE(root_info.st_mode) != 0o755
            or stat.S_IMODE(root_info.st_mode) & 0o7000):
        raise ReuseRefused("site_packages_metadata")
    records = sorted(root.glob("*.dist-info/RECORD"))
    if not records:
        raise ReuseRefused("record_missing")
    wheel_records, data_scripts = _wheel_record_index(wheelhouse, root, venv)
    if len(data_scripts) != 1:
        raise ReuseRefused("data_script_set")
    listed: set[Path] = set()
    record_targets: set[Path] = set()
    generated_console_scripts: set[Path] = set()
    record_rows: dict[Path, tuple[str, str]] = {}
    for record in records:
        for row in csv.reader(_regular(record).decode("utf-8").splitlines()):
            if len(row) != 3:
                raise ReuseRefused("record_format")
            relative, digest, size = row
            target = _safe_record_target(root, relative, venv)
            canonical_relative = os.path.relpath(target, root).replace(os.sep, "/")
            if relative != canonical_relative:
                raise ReuseRefused("record_path_spelling")
            try:
                target.relative_to(venv.resolve())
            except ValueError as exc:
                raise ReuseRefused("record_path") from exc
            if target in record_targets:
                raise ReuseRefused("record_duplicate_target")
            record_targets.add(target)
            if not target.exists():
                if relative.endswith(".pyc"):
                    continue
                raise ReuseRefused("record_missing_file")
            file_info = target.lstat()
            file_mode = stat.S_IMODE(file_info.st_mode)
            if (not stat.S_ISREG(file_info.st_mode) or file_info.st_nlink != 1
                    or file_info.st_uid != 0 or file_mode not in {0o644, 0o755}
                    or file_mode & 0o7000):
                raise ReuseRefused("record_metadata")
            listed.add(target)
            record_rows[target] = (digest, size)
            content = target.read_bytes()
            trusted = wheel_records.get(target)
            if target in data_scripts:
                if (relative != canonical_relative
                        or content != data_scripts[target]
                        or file_mode != 0o755
                        or re.fullmatch(r"sha256=[A-Za-z0-9_-]{43}", digest) is None
                        or not size.isdigit() or int(size) > 1024 * 1024
                        or not os.access(target, os.X_OK)):
                    raise ReuseRefused("record_script")
                continue
            if trusted is not None:
                wheel_digest, wheel_size = trusted
                if digest == wheel_digest and size == wheel_size:
                    if digest:
                        algorithm, _, encoded = digest.partition("=")
                        if algorithm != "sha256" or not encoded:
                            raise ReuseRefused("record_hash_format")
                        actual = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
                        if actual != encoded or str(len(content)) != size:
                            raise ReuseRefused("record_content")
                    continue
                raise ReuseRefused("record_content")
            if target.suffix == ".pyc" and not digest and not size:
                continue
            if target.parent.name.endswith(".dist-info") and target.name in {"INSTALLER", "REQUESTED"}:
                expected_content = b"pip\n" if target.name == "INSTALLER" else b""
                actual = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
                if (content != expected_content or digest != f"sha256={actual}"
                        or size != str(len(content))):
                    raise ReuseRefused("generated_metadata")
                continue
            if target.parent == venv / "bin" and target.is_file() and os.access(target, os.X_OK):
                canonical_relative = os.path.relpath(target, root).replace(os.sep, "/")
                if relative != canonical_relative:
                    raise ReuseRefused("record_script_path")
                generated_console_scripts.add(target)
                continue
            raise ReuseRefused(
                f"record_untrusted_bin_{target.parent == venv / 'bin'}_exec_{os.access(target, os.X_OK)}_suffix_{target.suffix}"
            )
    if not wheel_records.keys() <= listed:
        raise ReuseRefused("record_incomplete")
    if not data_scripts.keys() <= listed:
        raise ReuseRefused("record_script")
    # Bytecode is generated after installation and is safe only when its
    # source remains RECORD-authenticated and its header is for this runtime.
    magic = __import__("importlib.util", fromlist=["MAGIC_NUMBER"]).MAGIC_NUMBER
    cache_paths = list(root.rglob("*.pyc")) + list((venv / "bin").rglob("*.pyc"))
    for cache in cache_paths:
        cache_info = cache.lstat()
        if (not stat.S_ISREG(cache_info.st_mode) or cache_info.st_nlink != 1
                or cache_info.st_uid != 0 or stat.S_IMODE(cache_info.st_mode) != 0o644):
            raise ReuseRefused("pyc_metadata")
        source = Path(importlib.util.source_from_cache(str(cache))).resolve()
        if source not in listed or not source.is_file():
            raise ReuseRefused("pyc_source")
        data = cache.read_bytes()
        if len(data) < 16 or data[:4] != magic:
            raise ReuseRefused("pyc_header")
        flags = int.from_bytes(data[4:8], "little")
        source_bytes = source.read_bytes()
        if flags == 0:
            source_info = source.stat()
            if (int.from_bytes(data[8:12], "little") != int(source_info.st_mtime)
                    or int.from_bytes(data[12:16], "little") != source_info.st_size):
                raise ReuseRefused("pyc_header")
        elif flags == 3:
            if data[8:16] != importlib.util.source_hash(source_bytes):
                raise ReuseRefused("pyc_header")
        else:
            raise ReuseRefused("pyc_header")
        try:
            expected_code = _compile_source(source_bytes, str(source))
            actual_code = __import__("marshal").loads(data[16:])
        except (OSError, ValueError, EOFError) as exc:
            raise ReuseRefused("pyc_parse") from exc
        if actual_code != expected_code:
            raise ReuseRefused("pyc_content")
    for path in root.rglob("*"):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            if (info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o755
                    or stat.S_IMODE(info.st_mode) & 0o7000):
                raise ReuseRefused("directory_metadata")
            continue
        mode = stat.S_IMODE(info.st_mode)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0
                or mode not in {0o644, 0o755} or mode & 0o7000):
            raise ReuseRefused("unrecorded_metadata")
        resolved = path.resolve()
        if resolved.suffix != ".pyc" and resolved not in listed:
            raise ReuseRefused("unrecorded_file")

    # Installed pip is executed only after every package file, .pth file,
    # script payload and bytecode cache has been authenticated against wheels.
    expected_scripts = _expected_console_scripts(venv, root)
    expected_activation = _expected_activation_scripts(venv, release_slug)
    allowed_links = {venv / "bin" / name for name in ("python", "python3", "python3.12")}
    allowed_bin_files = set(expected_scripts) | set(expected_activation) | set(data_scripts)
    expected_bin_pycs = {
        Path(importlib.util.cache_from_source(str(path)))
        for path in allowed_bin_files if path.suffix == ".py"
    }
    observed_bin_files: set[Path] = set()
    for path in (venv / "bin").iterdir():
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            if path not in allowed_links:
                raise ReuseRefused("bin_extra_link")
            continue
        if stat.S_ISDIR(info.st_mode):
            if (path != venv / "bin" / "__pycache__" or info.st_uid != 0
                    or stat.S_IMODE(info.st_mode) != 0o755):
                raise ReuseRefused("bin_extra_directory")
            cache_files = {item for item in path.iterdir()}
            if not cache_files <= expected_bin_pycs:
                raise ReuseRefused("bin_extra_cache")
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1:
            raise ReuseRefused("bin_metadata")
        observed_bin_files.add(path)
    if observed_bin_files != allowed_bin_files:
        raise ReuseRefused("bin_file_set")
    for path, (expected_content, expected_mode) in expected_activation.items():
        info = path.lstat()
        if (stat.S_IMODE(info.st_mode) != expected_mode or expected_mode != 0o644
                or path.read_bytes() != expected_content):
            raise ReuseRefused("activation_content")
    activation_digest = _activation_scripts_digest(expected_activation)
    if set(expected_scripts) != generated_console_scripts:
        raise ReuseRefused("script_set")
    for path, expected_content in expected_scripts.items():
        content = path.read_bytes()
        row = record_rows.get(path)
        if (content != expected_content or path.lstat().st_mode & 0o7777 != 0o755 or row is None
                or re.fullmatch(r"sha256=[A-Za-z0-9_-]{43}", row[0]) is None
                or not row[1].isdigit() or int(row[1]) > 1024 * 1024):
            raise ReuseRefused("record_script")
        # pip wrote this RECORD row before relocate_venv_paths rewrote the
        # temporary environment shebang; require the exact reproducible script
        # and row membership while allowing only that known transformation.
    return activation_digest


def _transaction_identity(
    state: Path, quiesce_state: Path, app: Path, current: Path,
    candidate: Path, previous_before: str,
) -> None:
    transaction_tool = Path(__file__).with_name("platform_release_transaction.py")
    state_present = state.exists() or state.is_symlink()
    quiesce_present = quiesce_state.exists() or quiesce_state.is_symlink()
    if not state_present and not quiesce_present:
        raise ReuseRefused("transaction_missing")

    def read_status(path: Path, command: str, *, expected_phases: set[str]) -> dict[str, object]:
        if path.is_symlink():
            raise ReuseRefused("transaction_state")
        result = subprocess.run(
            ["/usr/bin/python3", "-I", "-S", "-B", str(transaction_tool), command,
             "--state", str(path), "--json"],
            env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LANG": "C.UTF-8",
                 "LC_ALL": "C.UTF-8"}, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
        )
        if result.returncode != 0:
            raise ReuseRefused("transaction_state")
        try:
            record = json.loads(result.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReuseRefused("transaction_state") from exc
        if (record.get("operation") != "install"
                or record.get("phase") not in expected_phases
                or record.get("app_dir") != str(app)
                or record.get("current_before") != str(current)
                or record.get("previous_before") != (previous_before or None)
                or record.get("candidate_release") != str(candidate)):
            raise ReuseRefused("transaction_identity")
        return record

    if state_present:
        read_status(state, "status", expected_phases={"quiesce-pending", "prepared"})
    if quiesce_present:
        read_status(quiesce_state, "status-quiesce", expected_phases={"quiesce-pending"})


def prove(
    app: Path, current: Path, candidate: Path, venv: Path, python: Path,
    transaction_state: Path, quiesce_state: Path, previous_before: str,
) -> None:
    pointer = app / "current"
    if not pointer.is_symlink() or pointer.resolve(strict=True) != current.resolve(strict=True):
        raise ReuseRefused("current_pointer")
    previous_pointer = app / "previous"
    if previous_before:
        if (not previous_pointer.is_symlink()
                or previous_pointer.resolve(strict=True) != Path(previous_before).resolve(strict=True)):
            raise ReuseRefused("previous_pointer")
    elif previous_pointer.exists() or previous_pointer.is_symlink():
        raise ReuseRefused("previous_pointer")
    if not candidate.is_dir() or candidate.is_symlink() or candidate.parent != app / "releases":
        raise ReuseRefused("candidate")
    rollback = current / ".rollback"
    transition = _regular(rollback / "venv-transition", mode=0o600)
    previous = ""
    if transition in (b"snapshot\n", b"unchanged\n"):
        snapshot = rollback / "shared-venv-before-install"
        if transition == b"snapshot\n" and not snapshot.is_dir():
            raise ReuseRefused("transaction_identity")
        if transition == b"unchanged\n" and (snapshot.exists() or snapshot.is_symlink()):
            raise ReuseRefused("transaction_identity")
        previous = _stable_private_regular(
            rollback / "previous-release", mode=0o600, max_size=4096,
        ).decode().strip()
        if not previous or not Path(previous).is_dir() or Path(previous).is_symlink():
            raise ReuseRefused("transaction_identity")
        if transition == b"unchanged\n":
            receipt = _regular(rollback / "shared-freeze.sha256", mode=0o600).decode().strip()
            if receipt != _sha(current / "requirements-platform.freeze.txt"):
                raise ReuseRefused("transaction_identity")
    else:
        raise ReuseRefused("transaction_identity")
    releases_root = app / "releases"
    current_slug, current_source, _current_release_sha = _release_identity(current, releases_root)
    candidate_slug, candidate_source, candidate_release_sha = _release_identity(candidate, releases_root)
    if previous:
        _release_identity(Path(previous), releases_root)
    _transaction_identity(transaction_state, quiesce_state, app, current,
                          candidate, previous_before)
    _same_dependency_inputs(current, candidate)
    _runtime(venv, python)
    env = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LANG": "C.UTF-8",
           "LC_ALL": "C.UTF-8", "PIP_CONFIG_FILE": "/dev/null", "PIP_NO_INDEX": "1",
           "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    origin_slug, origin_source, origin_release_sha, origin_receipt = _derive_venv_origin(
        current, app, venv,
    )
    activation_digest = _venv_integrity(venv, current / "wheelhouse", origin_slug)
    if (origin_receipt is not None
            and origin_receipt.get("activation_sha256") != activation_digest):
        raise ReuseRefused("origin_receipt_binding")
    for args in (("-I", "-m", "pip", "check"), ("-I", "-m", "pip", "freeze", "--all")):
        result = subprocess.run([str(venv / "bin/python"), "-B", *args], env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, check=False)
        if result.returncode != 0:
            raise ReuseRefused("pip_state")
        if args[-2:] == ("freeze", "--all"):
            expected = _regular(current / "requirements-platform.freeze.txt", mode=0o444).splitlines()
            if sorted(result.stdout.splitlines()) != sorted(expected):
                raise ReuseRefused("freeze")
    venv_info = venv.lstat()
    if (stat.S_ISLNK(venv_info.st_mode) or not stat.S_ISDIR(venv_info.st_mode)
            or venv_info.st_uid != 0 or venv_info.st_gid != 0
            or stat.S_IMODE(venv_info.st_mode) != 0o755):
        raise ReuseRefused("venv_directory")
    return {
        "schema": 1,
        "release_slug": candidate_slug,
        "release_source_sha": candidate_source,
        "release_json_sha256": candidate_release_sha,
        "venv_dev": venv_info.st_dev,
        "venv_ino": venv_info.st_ino,
        "freeze_sha256": _sha(candidate / "requirements-platform.freeze.txt"),
        "wheelhouse_manifest_sha256": _sha(candidate / "wheelhouse" / "WHEELHOUSE.sha256"),
        "origin_release_slug": origin_slug,
        "origin_source_sha": origin_source,
        "origin_release_json_sha256": origin_release_sha,
        "activation_sha256": activation_digest,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--venv", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--transaction-state", type=Path, required=True)
    parser.add_argument("--quiesce-state", type=Path, required=True)
    parser.add_argument("--previous-before", default="")
    args = parser.parse_args()
    try:
        receipt = prove(args.app, args.current, args.candidate, args.venv, args.python,
                        args.transaction_state, args.quiesce_state, args.previous_before)
    except (ReuseRefused, OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
        return 1
    sys.stdout.write(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
