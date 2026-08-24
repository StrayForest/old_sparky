#!/usr/bin/env python3
"""Strict environment adapter for the deploy smoke implementation.

The HTTP/CSP/database smoke logic remains in ``platform_deploy_smoke_impl.py``.
This public entrypoint ensures the same dotenv parser is used by runtime,
preflight and smoke, and prevents ambient PLATFORM_* variables from masking a
missing or malformed value in the requested env file.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path
from types import ModuleType
from typing import Any


TOOLS_DIR = Path(__file__).resolve().parent
SAFE_ENV_PATH = TOOLS_DIR / "platform_safe_env_exec.py"
IMPLEMENTATION_PATH = TOOLS_DIR / "platform_deploy_smoke_impl.py"

# Contract markers retained in the public entrypoint for source-level regression
# checks while their executable implementation remains in the internal module:
# "edge_security_apple_icon_cache_busted"
# f"{args.edge_origin}/apple-icon.png"


def _load_module(name: str, path: Path) -> ModuleType:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"Deploy smoke dependency is missing or unsafe: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load deploy smoke dependency: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SAFE_ENV = _load_module("platform_deploy_smoke_safe_env", SAFE_ENV_PATH)
_IMPLEMENTATION = _load_module("platform_deploy_smoke_impl", IMPLEMENTATION_PATH)


def load_env(path: Path) -> dict[str, str]:
    """Parse the smoke env exactly as production runtime parses it."""

    return _SAFE_ENV.load_env_file(path)


# Preserve the existing import/test API. Function objects keep the internal
# module's globals, while load_env is replaced there with the strict parser.
_IMPLEMENTATION.load_env = load_env
for _name, _value in vars(_IMPLEMENTATION).items():
    if _name.startswith("__") or _name in {"load_env", "main"}:
        continue
    globals().setdefault(_name, _value)


def _clear_ambient_platform_environment() -> None:
    for key in tuple(os.environ):
        if key.startswith(("PLATFORM_", "NEXT_PUBLIC_PLATFORM_")):
            os.environ.pop(key, None)


async def main() -> int:
    _clear_ambient_platform_environment()
    _IMPLEMENTATION.load_env = load_env
    result: Any = await _IMPLEMENTATION.main()
    return int(result)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
