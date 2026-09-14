"""Compatibility wrapper for the fail-closed V5 reacquisition entrypoint."""

from __future__ import annotations

import runpy
from pathlib import Path


V5_ENTRYPOINT = Path(__file__).with_name("reacquire_invalid_sessions_v5.py")


def main() -> None:
    if not V5_ENTRYPOINT.is_file():
        raise SystemExit("V5 reacquisition entrypoint is missing")
    runpy.run_path(str(V5_ENTRYPOINT), run_name="__main__")


if __name__ == "__main__":
    main()
