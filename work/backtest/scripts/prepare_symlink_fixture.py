"""Prepare item-11 directories/files without creating the owner-only symlink."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _powershell_literal(value: Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="new, unique absolute fixture root")
    args = parser.parse_args()
    root = Path(args.root)
    if not root.is_absolute():
        raise SystemExit("--root must be an absolute path")
    root = root.resolve()
    if root.exists() and any(root.iterdir()):
        raise SystemExit(f"fixture root must be absent or empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    input_root = root / "input"
    output_root = root / "output"
    outside_root = root / "outside"
    input_root.mkdir()
    output_root.mkdir()
    outside_root.mkdir()
    input_file = input_root / "allowed.txt"
    outside_file = outside_root / "outside.txt"
    input_file.write_text("input", encoding="utf-8")
    outside_file.write_text("outside", encoding="utf-8")
    link = output_root / "escape-symlink.txt"
    print(
        json.dumps(
            {
                "root": str(root),
                "input_file": str(input_file),
                "outside_file": str(outside_file),
                "symlink": str(link),
                "owner_action": "create exactly this file symlink; do not copy or retarget the target",
            },
            sort_keys=True,
        )
    )
    print("OWNER_COMMAND:")
    link_literal = _powershell_literal(link)
    target_literal = _powershell_literal(outside_file)
    print(
        "$ErrorActionPreference='Stop'; "
        f"$link={link_literal}; $target={target_literal}; "
        "if ($null -ne (Get-Item -LiteralPath $link -Force -ErrorAction SilentlyContinue)) "
        "{ throw 'owner link path already exists; refusing to replace it' }; "
        "if (-not (Test-Path -LiteralPath $target -PathType Leaf)) "
        "{ throw 'fixture target file is missing' }; "
        "New-Item -ItemType SymbolicLink -Path $link -Value $target -ErrorAction Stop | Out-Null"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
