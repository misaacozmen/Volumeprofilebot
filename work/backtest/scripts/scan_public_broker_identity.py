"""Fail when tracked public JSON contains concrete broker account identity."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
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
            if key == "account_login" and ((isinstance(item, int) and not isinstance(item, bool) and item > 0) or (isinstance(item, str) and item.isascii() and item.isdigit() and int(item) > 0)):
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


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout


def _deny_values(denylist: Path | None) -> tuple[list[bytes], str | None]:
    if denylist is None:
        return [], None
    raw = denylist.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    return [str(item).encode("utf-8") for item in payload.get("denylist", []) if str(item)], sha256(raw).hexdigest()


def _history_scan(root: Path, denied: list[bytes]) -> tuple[list[dict[str, object]], dict[str, int]]:
    rows = _git(root, "rev-list", "--objects", "--all").decode("utf-8", "replace").splitlines()
    object_paths: dict[str, str] = {}
    for row in rows:
        object_id, _, path = row.partition(" ")
        object_paths.setdefault(object_id, path)
    matches: list[dict[str, object]] = []
    counts = {"commit_count": int(_git(root, "rev-list", "--all", "--count").decode().strip() or 0), "blob_count": 0, "object_count": 0, "scanned_bytes": 0, "denylist_occurrence_count": 0, "denylist_unique_object_count": 0}
    object_ids = list(object_paths)
    batch = subprocess.run(
        ["git", "-C", str(root), "cat-file", "--batch"],
        input=("\n".join(object_ids) + "\n").encode("ascii"),
        check=True,
        capture_output=True,
    ).stdout
    offset = 0
    for object_id in object_ids:
        end_header = batch.find(b"\n", offset)
        if end_header < 0:
            raise RuntimeError("git cat-file batch output is truncated")
        header = batch[offset:end_header].split()
        offset = end_header + 1
        if len(header) != 3:
            raise RuntimeError("git cat-file batch header is invalid")
        kind = header[1]
        size = int(header[2])
        blob = batch[offset:offset + size]
        offset += size
        if offset < len(batch) and batch[offset:offset + 1] == b"\n":
            offset += 1
        counts["object_count"] += 1
        if kind != b"blob":
            continue
        counts["blob_count"] += 1
        counts["scanned_bytes"] += len(blob)
        path = object_paths[object_id]
        denied_count = sum(blob.count(item) for item in denied)
        fields: list[str] = []
        if path.endswith(".json"):
            try:
                fields = concrete_paths(json.loads(blob.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
        if fields or denied_count:
            matches.append({"object": object_id, "path": path or "<unmapped>", "field_paths": fields, "denylist_match_count": denied_count, "blob_sha256": sha256(blob).hexdigest()})
        counts["denylist_occurrence_count"] += denied_count
        if denied_count:
            counts["denylist_unique_object_count"] += 1
    counts["denylist_unique_file_count"] = len({row.get("path") for row in matches if int(row.get("denylist_match_count", 0))})
    counts["denylist_match_count"] = sum(int(row.get("denylist_match_count", 0)) for row in matches)
    return matches, counts


def _ref_tips(root: Path) -> list[dict[str, str]]:
    output = _git(root, "for-each-ref", "--format=%(refname) %(objectname)").decode("utf-8", "replace")
    return [{"ref": row.split(" ", 1)[0], "sha": row.split(" ", 1)[1]} for row in output.splitlines() if " " in row]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--denylist", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    git_prefix = _git(root, "rev-parse", "--show-prefix").decode("utf-8", "replace").strip().replace("\\", "/")
    denylist = args.denylist.resolve() if args.denylist else None
    started = datetime.now(timezone.utc)
    matches = scan(root, denylist) if denylist is not None else []
    denied, denylist_sha256 = _deny_values(denylist)
    history_matches, counts = _history_scan(root, denied)
    before_head = _git(root, "rev-parse", "HEAD").decode().strip()
    before_origin = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"], capture_output=True, text=True, check=False).stdout.strip()
    after_head = _git(root, "rev-parse", "HEAD").decode().strip()
    after_origin = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"], capture_output=True, text=True, check=False).stdout.strip()
    ended = datetime.now(timezone.utc)
    scanner_hash = sha256(Path(__file__).read_bytes()).hexdigest()
    payload = {
        "schema_version": 2,
        "git_prefix": git_prefix,
        "source_head": before_head,
        "all_ref_tips": _ref_tips(root),
        **counts,
        "scanner_source_sha256": scanner_hash,
        "private_denylist_sha256": denylist_sha256,
        "status": "ASSESSED" if denylist is not None else "UNASSESSED_MISSING_DENYLIST",
        "working_tree_unique_file_count": len(matches) if denylist is not None else None,
        "working_tree_unique_object_count": len(matches) if denylist is not None else None,
        "working_tree_occurrence_count": sum(int(row.get("denylist_match_count", 0)) for row in matches) if denylist is not None else None,
        "reachable_history_unique_object_count": counts.get("denylist_unique_object_count") if denylist is not None else None,
        "reachable_history_unique_file_count": counts.get("denylist_unique_file_count") if denylist is not None else None,
        "reachable_history_occurrence_count": counts.get("denylist_occurrence_count") if denylist is not None else None,
        "working_tree_match_count": len(matches) if denylist is not None else None,
        "reachable_history_match_count": len(history_matches) if denylist is not None else None,
        "sanitized_mirror_match_count": None,
        "source_unchanged": before_head == after_head,
        "origin_unchanged": before_origin == after_origin,
        "started_at_utc": started.isoformat().replace("+00:00", "Z"),
        "finished_at_utc": ended.isoformat().replace("+00:00", "Z"),
        "match_count": len(matches),
        "matches": matches,
        "reachable_history_matches": history_matches,
    }
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded, encoding="utf-8", newline="\n")
    print(encoded, end="")
    if denylist is None:
        raise SystemExit(2)
    raise SystemExit(1 if matches or history_matches else 0)


if __name__ == "__main__":
    main()
