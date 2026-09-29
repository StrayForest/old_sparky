"""Write bounded command output without retaining an unbounded SSH response."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    parser.add_argument("max_bytes", type=int)
    args = parser.parse_args(argv)
    if args.max_bytes <= 0:
        return 2
    try:
        descriptor = os.open(
            args.destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_TRUNC
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError:
        return 2
    total = 0
    status = 0
    try:
        while True:
            chunk = sys.stdin.buffer.read(64 * 1024)
            if not chunk:
                break
            remaining = args.max_bytes - total
            if len(chunk) > remaining:
                if remaining > 0:
                    os.write(descriptor, chunk[:remaining])
                status = 2
                break
            os.write(descriptor, chunk)
            total += len(chunk)
        os.fchmod(descriptor, 0o600)
    except OSError:
        status = 2
    finally:
        os.close(descriptor)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
