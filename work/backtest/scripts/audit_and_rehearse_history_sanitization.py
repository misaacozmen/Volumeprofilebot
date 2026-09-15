"""Audit every reachable Git blob and rehearse equal-length redaction in disposable mirrors."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import tempfile


def git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), *args], input=input_bytes, capture_output=True, check=True
    ).stdout


def _commit_shape(repo: Path) -> dict[str, object]:
    rows = git(repo, "rev-list", "--all", "--topo-order", "--reverse", "--parents").decode("ascii", "replace").splitlines()
    ordered = [row.split()[0] for row in rows if row.strip()]
    positions = {value: index for index, value in enumerate(ordered)}
    shapes: list[dict[str, object]] = []
    for row in rows:
        parts = row.split()
        if not parts:
            continue
        commit = git(repo, "cat-file", "commit", parts[0]).decode("utf-8", "replace")
        headers, _, _ = commit.partition("\n\n")
        author = next((line.removeprefix("author ") for line in headers.splitlines() if line.startswith("author ")), "")
        committer = next((line.removeprefix("committer ") for line in headers.splitlines() if line.startswith("committer ")), "")
        shapes.append({
            "parents": [positions[parent] for parent in parts[1:] if parent in positions],
            "author": author,
            "committer": committer,
        })
    refs = git(repo, "for-each-ref", "--format=%(refname)").decode("utf-8", "replace").splitlines()
    ref_tips = git(repo, "for-each-ref", "--format=%(refname) %(objectname)").decode("ascii", "replace").splitlines()
    return {
        "ref_names": sorted(refs),
        "ref_tip_positions": {row.split(" ", 1)[0]: positions.get(row.split(" ", 1)[1]) for row in ref_tips if " " in row},
        "commit_count": len(ordered),
        "commits": shapes,
    }


def secrets(path: Path) -> list[bytes]:
    value = json.loads(path.read_text(encoding="utf-8"))
    rows = value.get("denylist") if isinstance(value, dict) else None
    if not isinstance(rows, list) or not rows or any(not isinstance(item, str) or not item for item in rows):
        raise ValueError("private denylist must contain a non-empty string array")
    encoded = [item.encode("utf-8") for item in rows]
    if len(encoded) != len(set(encoded)):
        raise ValueError("private denylist contains duplicates")
    return encoded


def replacement(value: bytes) -> bytes:
    marker = b"[REDACTED]"
    return (marker + b"_" * len(value))[: len(value)]


def scan(repo: Path, deny: list[bytes]) -> list[dict[str, object]]:
    rows = git(repo, "rev-list", "--objects", "--all").decode("utf-8", "replace").splitlines()
    results: list[dict[str, object]] = []
    for row in rows:
        object_id, _, path = row.partition(" ")
        if git(repo, "cat-file", "-t", object_id).strip() != b"blob":
            continue
        blob = git(repo, "cat-file", "blob", object_id)
        counts = [blob.count(item) for item in deny]
        if not any(counts):
            continue
        redacted = blob
        for item in deny:
            redacted = redacted.replace(item, replacement(item))
        results.append({
            "object": object_id,
            "path": path or "<unmapped>",
            "match_count": sum(counts),
            "redacted_object_sha256": sha256(redacted).hexdigest(),
        })
    return results


def _metrics(repo: Path) -> dict[str, int]:
    rows = git(repo, "rev-list", "--objects", "--all").decode("utf-8", "replace").splitlines()
    seen: set[str] = set()
    result = {"object_count": 0, "commit_count": int(git(repo, "rev-list", "--all", "--count").decode().strip() or 0), "blob_count": 0, "scanned_bytes": 0}
    for row in rows:
        object_id = row.split(" ", 1)[0]
        if object_id in seen:
            continue
        seen.add(object_id)
        result["object_count"] += 1
        if git(repo, "cat-file", "-t", object_id).strip() == b"blob":
            result["blob_count"] += 1
            result["scanned_bytes"] += len(git(repo, "cat-file", "blob", object_id))
    return result


def _working_tree_match_count(repo: Path, deny: list[bytes]) -> int:
    count = 0
    for item in git(repo, "ls-files", "-z").split(b"\0"):
        if not item:
            continue
        count += sum((repo / item.decode("utf-8")).read_bytes().count(value) for value in deny)
    return count


def build_sanitized_mirror(source: Path, target: Path, deny: list[bytes]) -> list[dict[str, object]]:
    """Create a real bare, pushable sanitized mirror without changing source."""
    target = target.resolve()
    if target.exists() or target.parent.resolve() == source.resolve():
        raise ValueError("sanitized mirror target must be a new external path")
    try:
        target.relative_to(source.resolve())
    except ValueError:
        pass
    else:
        raise ValueError("sanitized mirror target must remain outside the source repository")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="super1-history-export-") as temporary:
        mirror = Path(temporary) / "source.git"
        subprocess.run(["git", "clone", "--mirror", "--no-local", str(source), str(mirror)], check=True, capture_output=True)
        exported = subprocess.run(["git", "-C", str(mirror), "fast-export", "--all"], check=True, capture_output=True).stdout
        for item in deny:
            exported = exported.replace(item, replacement(item))
        subprocess.run(["git", "init", "--bare", str(target)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(target), "fast-import", "--quiet"], input=exported, check=True, capture_output=True)
    return scan(target, deny)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--denylist", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--sanitized-mirror", type=Path)
    parser.add_argument("--phase", choices=("pre-push", "post-push"), default="pre-push")
    args = parser.parse_args()
    source = args.source.resolve()
    started = datetime.now(timezone.utc)
    if args.denylist is not None:
        try:
            args.denylist.resolve().relative_to(source)
        except ValueError:
            pass
        else:
            raise SystemExit("private denylist must be outside the source repository")
    if args.denylist is None or not args.denylist.resolve().is_file():
        finished = datetime.now(timezone.utc)
        payload = {"schema_version": 3, "status": "UNASSESSED_MISSING_DENYLIST", "source_head": git(source, "rev-parse", "HEAD").decode().strip(), "working_tree_match_count": None, "reachable_history_match_count": None, "sanitized_mirror_match_count": None, "private_denylist_sha256": None, "source_unchanged": True, "origin_unchanged": True, "started_at_utc": started.isoformat().replace("+00:00", "Z"), "finished_at_utc": finished.isoformat().replace("+00:00", "Z"), "rehearsal_only": True}
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        raise SystemExit(2)
    if args.phase == "post-push" and args.sanitized_mirror is None:
        raise SystemExit("post-push remote rescan requires --sanitized-mirror")
    deny = secrets(args.denylist.resolve())
    denylist_sha256 = sha256(args.denylist.resolve().read_bytes()).hexdigest()
    source_head = git(source, "rev-parse", "HEAD").decode().strip()
    source_origin = git(source, "remote", "get-url", "origin").decode().strip()
    ref_rows = git(source, "for-each-ref", "--format=%(refname) %(objectname)").decode("utf-8", "replace")
    ref_tips = [{"ref": row.split(" ", 1)[0], "sha": row.split(" ", 1)[1]} for row in ref_rows.splitlines() if " " in row]
    source_metrics = _metrics(source)
    working_tree_match_count = _working_tree_match_count(source, deny)
    source_matches = scan(source, deny)
    with tempfile.TemporaryDirectory(prefix="super1-history-rehearsal-") as temporary:
        root = Path(temporary)
        mirror = root / "source.git"
        sanitized = root / "sanitized.git"
        subprocess.run(["git", "clone", "--mirror", "--no-local", str(source), str(mirror)], check=True, capture_output=True)
        subprocess.run(["git", "init", "--bare", str(sanitized)], check=True, capture_output=True)
        exported = subprocess.run(["git", "-C", str(mirror), "fast-export", "--all"], check=True, capture_output=True).stdout
        for item in deny:
            exported = exported.replace(item, replacement(item))
        subprocess.run(["git", "-C", str(sanitized), "fast-import", "--quiet"], input=exported, check=True, capture_output=True)
        sanitized_matches = scan(sanitized, deny)
        sanitized_metrics = _metrics(sanitized)
        source_shape = _commit_shape(source)
        sanitized_shape = _commit_shape(sanitized)
        topology_checks = {
            "ref_name_set_preserved": source_shape["ref_names"] == sanitized_shape["ref_names"],
            "commit_count_preserved": source_shape["commit_count"] == sanitized_shape["commit_count"],
            "parent_topology_preserved": [row["parents"] for row in source_shape["commits"]] == [row["parents"] for row in sanitized_shape["commits"]],
            "author_committer_timestamp_preserved": [({"author": row["author"], "committer": row["committer"]}) for row in source_shape["commits"]] == [({"author": row["author"], "committer": row["committer"]}) for row in sanitized_shape["commits"]],
        }
    sanitized_mirror_path = None
    sanitized_mirror_matches: list[dict[str, object]] = []
    if args.sanitized_mirror is not None:
        mirror_target = args.sanitized_mirror.resolve()
        try:
            mirror_target.relative_to(source)
        except ValueError:
            pass
        else:
            raise SystemExit("sanitized mirror must be outside the source repository")
        sanitized_mirror_matches = build_sanitized_mirror(source, mirror_target, deny)
        sanitized_mirror_path = str(mirror_target)
    if git(source, "rev-parse", "HEAD").decode().strip() != source_head or git(source, "remote", "get-url", "origin").decode().strip() != source_origin:
        raise RuntimeError("source repository or origin changed during disposable rehearsal")
    finished = datetime.now(timezone.utc)
    payload = {
        "schema_version": 2,
        "status": ("POST_PUSH_REMOTE_RESCAN_CLEAN" if args.phase == "post-push" and args.sanitized_mirror is not None and not sanitized_mirror_matches and not sanitized_matches and all(topology_checks.values()) else ("PRE_PUSH_CLEAN" if args.phase == "pre-push" and args.sanitized_mirror is not None and not sanitized_mirror_matches and not sanitized_matches and all(topology_checks.values()) else ("REHEARSAL_PASSED_NOT_REMOTE_REMEDIATED" if not sanitized_matches and all(topology_checks.values()) else "REHEARSAL_FAILED"))),
        "phase": args.phase,
        "source_head": source_head,
        "all_ref_tips": ref_tips,
        **source_metrics,
        "scanner_source_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "private_denylist_sha256": denylist_sha256,
        "working_tree_match_count": working_tree_match_count,
        "reachable_history_match_count": sum(int(row["match_count"]) for row in source_matches),
        "sanitized_mirror_match_count": sum(int(row["match_count"]) for row in sanitized_matches),
        "sanitized_mirror_metrics": sanitized_metrics,
        "source_match_count": sum(int(row["match_count"]) for row in source_matches),
        "source_matches": source_matches,
        "sanitized_match_count": sum(int(row["match_count"]) for row in sanitized_matches),
        "sanitized_matches": sanitized_matches,
        "sanitized_mirror_path": sanitized_mirror_path,
        "remote_quarantine_match_count": len(sanitized_mirror_matches),
        "topology_checks": topology_checks,
        "source_ref_tip_positions": source_shape["ref_tip_positions"],
        "sanitized_ref_tip_positions": sanitized_shape["ref_tip_positions"],
        "source_unchanged": True,
        "origin_unchanged": True,
        "started_at_utc": started.isoformat().replace("+00:00", "Z"),
        "finished_at_utc": finished.isoformat().replace("+00:00", "Z"),
        "rehearsal_only": args.sanitized_mirror is None,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    if sanitized_matches:
        raise SystemExit("sanitized mirror still contains denylist occurrences")
    if not all(topology_checks.values()):
        raise SystemExit("sanitized mirror topology or metadata differs from source")


if __name__ == "__main__":
    main()
