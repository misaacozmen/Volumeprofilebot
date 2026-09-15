"""Fail when tracked public JSON contains concrete broker account identity."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess


PUBLIC_PREFIXES = ("live_forward/", "research_candidates/", "forward_shadow/", "deploy/")
IDENTITY_KEYS = frozenset({"expected_server", "expected_company"})


def tracked_files(root: Path) -> list[Path]:
    output = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True).stdout
    return [root / item.decode("utf-8") for item in output.split(b"\0") if item]


def untracked_files(root: Path) -> list[Path]:
    output = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout
    paths: list[Path] = []
    for item in output.split(b"\0"):
        if not item.startswith(b"?? "):
            continue
        path = root / item[3:].decode("utf-8")
        if path.is_file():
            paths.append(path)
    return paths


def ignored_files(root: Path) -> list[Path]:
    output = subprocess.run(
        ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout
    return [root / item.decode("utf-8") for item in output.split(b"\0") if item and (root / item.decode("utf-8")).is_file()]


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
    seen: set[Path] = set()
    files = (
        [(path, "tracked") for path in tracked_files(root)]
        + [(path, "untracked") for path in untracked_files(root)]
        + ([(path, "ignored") for path in ignored_files(root)] if denylist is not None else [])
    )
    for path, scope in files:
        if path in seen or not path.is_file():
            continue
        seen.add(path)
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
                "scope": scope,
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
    values = payload.get("denylist") if isinstance(payload, dict) else None
    if not isinstance(values, list) or not values or any(not isinstance(item, str) or not item for item in values):
        raise ValueError("private denylist must contain a non-empty string array")
    encoded = [item.encode("utf-8") for item in values]
    if len(encoded) != len(set(encoded)):
        raise ValueError("private denylist contains duplicates")
    return encoded, sha256(raw).hexdigest()


def _history_scan(root: Path, denied: list[bytes]) -> tuple[list[dict[str, object]], dict[str, int]]:
    rows = _git(root, "rev-list", "--objects", "--all").decode("utf-8", "replace").splitlines()
    object_paths: dict[str, str] = {}
    for row in rows:
        object_id, _, path = row.partition(" ")
        object_paths.setdefault(object_id, path)
    matches: list[dict[str, object]] = []
    counts = {"commit_count": int(_git(root, "rev-list", "--all", "--count").decode().strip() or 0), "blob_count": 0, "object_count": 0, "scanned_bytes": 0, "denylist_occurrence_count": 0, "denylist_unique_object_count": 0, "commit_message_match_count": 0, "tag_note_match_count": 0}
    commit_ids = _git(root, "rev-list", "--all").decode("ascii", "replace").split()
    ref_ids = _git(root, "for-each-ref", "--format=%(objectname)").decode("ascii", "replace").split()
    object_ids = list(dict.fromkeys([*object_paths, *commit_ids, *ref_ids]))
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
        if kind == b"blob":
            counts["blob_count"] += 1
        counts["scanned_bytes"] += len(blob)
        path = object_paths.get(object_id, "")
        denied_count = sum(blob.count(item) for item in denied)
        fields: list[str] = []
        if kind == b"blob" and path.endswith(".json"):
            try:
                fields = concrete_paths(json.loads(blob.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
        if kind == b"commit" and denied_count:
            counts["commit_message_match_count"] += denied_count
        if kind == b"tag" and denied_count:
            counts["tag_note_match_count"] += denied_count
        if fields or denied_count:
            matches.append({"object": object_id, "path": path or "<unmapped>", "field_paths": fields, "denylist_match_count": denied_count, "blob_sha256": sha256(blob).hexdigest()})
        counts["denylist_occurrence_count"] += denied_count
        if denied_count:
            counts["denylist_unique_object_count"] += 1
    counts["denylist_unique_file_count"] = len({row.get("path") for row in matches if int(row.get("denylist_match_count", 0))})
    counts["denylist_match_count"] = sum(int(row.get("denylist_match_count", 0)) for row in matches)
    return matches, counts


def _ref_name_matches(root: Path, denied: list[bytes]) -> list[dict[str, object]]:
    rows = _git(root, "for-each-ref", "--format=%(refname) %(objectname)").decode("utf-8", "replace").splitlines()
    matches: list[dict[str, object]] = []
    for row in rows:
        ref, _, object_id = row.partition(" ")
        count = sum(ref.encode("utf-8").count(value) for value in denied)
        if count:
            matches.append({"scope": "ref-name", "ref": ref, "object": object_id, "denylist_match_count": count})
    return matches


def _quarantine_scan(quarantine_root: Path | None, denied: list[bytes], *, repository_root: Path | None = None) -> int:
    if quarantine_root is None:
        return 0
    root = quarantine_root.resolve()
    if not root.is_dir():
        raise ValueError("quarantine object directory is missing")
    if repository_root is None:
        raise ValueError("quarantine scan requires a Git repository for object verification")
    git_root = repository_root.resolve()
    git_dir = (git_root / ".git").resolve()
    objects = git_dir / "objects"
    environment = {**os.environ, "GIT_OBJECT_DIRECTORY": str(root), "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(objects)}
    listed = subprocess.run(
        ["git", "-C", str(git_root), "cat-file", "--batch-all-objects", "--batch-check"],
        capture_output=True, check=False, env=environment,
    )
    if listed.returncode != 0:
        raise RuntimeError("quarantine Git object read failed; raw pack bytes are not clean evidence")
    object_ids: list[str] = []
    for line in listed.stdout.splitlines():
        fields = line.split()
        if len(fields) != 3 or len(fields[0]) != 40 or any(char not in b"0123456789abcdef" for char in fields[0]):
            raise RuntimeError("quarantine Git object listing is malformed; raw pack bytes are not clean evidence")
        object_ids.append(fields[0].decode("ascii"))
    if not object_ids:
        return 0
    read = subprocess.run(
        ["git", "-C", str(git_root), "cat-file", "--batch"],
        input=("\n".join(object_ids) + "\n").encode("ascii"), capture_output=True, check=False, env=environment,
    )
    if read.returncode != 0:
        raise RuntimeError("quarantine Git object read failed; raw pack bytes are not clean evidence")
    data = read.stdout
    offset = 0
    total = 0
    for object_id in object_ids:
        newline = data.find(b"\n", offset)
        if newline < 0:
            raise RuntimeError("quarantine Git object stream is truncated; raw pack bytes are not clean evidence")
        header = data[offset:newline].split()
        offset = newline + 1
        if len(header) != 3 or header[0].decode("ascii", "ignore") != object_id or header[1] == b"missing":
            raise RuntimeError("quarantine Git object stream is malformed; raw pack bytes are not clean evidence")
        try:
            size = int(header[2])
        except ValueError as exc:
            raise RuntimeError("quarantine Git object size is invalid; raw pack bytes are not clean evidence") from exc
        body = data[offset:offset + size]
        if len(body) != size:
            raise RuntimeError("quarantine Git object body is truncated; raw pack bytes are not clean evidence")
        offset += size
        if offset >= len(data) or data[offset:offset + 1] != b"\n":
            raise RuntimeError("quarantine Git object delimiter is missing; raw pack bytes are not clean evidence")
        offset += 1
        total += sum(body.count(value) for value in denied)
    if offset != len(data):
        raise RuntimeError("quarantine Git object stream has trailing bytes; raw pack bytes are not clean evidence")
    return total


def _ref_tips(root: Path) -> list[dict[str, str]]:
    output = _git(root, "for-each-ref", "--format=%(refname) %(objectname)").decode("utf-8", "replace")
    return [{"ref": row.split(" ", 1)[0], "sha": row.split(" ", 1)[1]} for row in output.splitlines() if " " in row]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--denylist", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--quarantine-object-dir", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    git_prefix = _git(root, "rev-parse", "--show-prefix").decode("utf-8", "replace").strip().replace("\\", "/")
    denylist = args.denylist.resolve() if args.denylist else None
    if denylist is not None:
        try:
            denylist.relative_to(root)
        except ValueError:
            pass
        else:
            raise SystemExit("private denylist must be outside the source repository")
    started = datetime.now(timezone.utc)
    matches = scan(root, denylist) if denylist is not None else []
    denied, denylist_sha256 = _deny_values(denylist)
    history_matches, counts = _history_scan(root, denied)
    ref_matches = _ref_name_matches(root, denied)
    quarantine_match_count = _quarantine_scan(args.quarantine_object_dir, denied, repository_root=root)
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
        "match_count": len(matches) + len(ref_matches) + int(quarantine_match_count),
        "matches": matches,
        "reachable_history_matches": history_matches,
        "ref_name_match_count": sum(int(row["denylist_match_count"]) for row in ref_matches),
        "ref_name_matches": ref_matches,
        "quarantine_object_match_count": quarantine_match_count,
        "quarantine_object_dir_provided": args.quarantine_object_dir is not None,
    }
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded, encoding="utf-8", newline="\n")
    print(encoded, end="")
    if denylist is None:
        raise SystemExit(2)
    raise SystemExit(1 if matches or history_matches or ref_matches or quarantine_match_count else 0)


if __name__ == "__main__":
    main()
