"""Fail when tracked public JSON contains concrete broker account identity."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess


PUBLIC_PREFIXES = ("live_forward/", "research_candidates/", "forward_shadow/", "deploy/")
IDENTITY_KEYS = frozenset({"expected_server", "expected_company"})


def tracked_files(root: Path) -> list[Path]:
    output = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True).stdout
    return [root / item.decode("utf-8") for item in output.split(b"\0") if item]


def concrete_paths(value: object, prefix: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}"
            if key == "account_login" and isinstance(item, int) and not isinstance(item, bool) and item > 0:
                found.append(path)
            if key in IDENTITY_KEYS and isinstance(item, str) and item.strip():
                found.append(path)
            found.extend(concrete_paths(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(concrete_paths(item, f"{prefix}[{index}]"))
    return found


def scan(root: Path, denylist: Path | None = None) -> list[dict[str, object]]:
    denied: list[bytes] = []
    if denylist is not None:
        payload = json.loads(denylist.read_text(encoding="utf-8"))
        denied = [str(item).encode() for item in payload.get("denylist", []) if str(item)]
    matches: list[dict[str, object]] = []
    for path in tracked_files(root):
        relative = path.relative_to(root).as_posix()
        raw = path.read_bytes()
        locations: list[str] = []
        if relative.startswith(PUBLIC_PREFIXES) and path.suffix.lower() == ".json":
            try:
                locations.extend(concrete_paths(json.loads(raw.decode("utf-8"))))
            except (UnicodeError, json.JSONDecodeError):
                pass
        denied_count = sum(raw.count(item) for item in denied)
        if locations or denied_count:
            matches.append({
                "path": relative,
                "field_paths": locations,
                "denylist_match_count": denied_count,
                "file_sha256": sha256(raw).hexdigest(),
            })
    return matches


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--denylist", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    matches = scan(args.root.resolve(), args.denylist.resolve() if args.denylist else None)
    payload = {"schema_version": 1, "match_count": len(matches), "matches": matches}
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded, encoding="utf-8", newline="\n")
    print(encoded, end="")
    raise SystemExit(1 if matches else 0)


if __name__ == "__main__":
    main()
