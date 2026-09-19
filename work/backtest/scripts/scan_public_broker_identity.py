"""Fail closed when broker account identity appears in source or Git history."""

from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Iterable


ACCOUNT_KEYS = frozenset({"account_login", "account_number", "expected_login", "login"})
FIXTURE_PREFIXES = ("tests/", "test/", "work/backtest/tests/")
ASCII_DECIMAL = re.compile(r"^[0-9]+$")
POWERSHELL_ACCOUNT_LITERAL = re.compile(
    r"(?ix)(?:\$?account_login|\$?account_number|\$?expected_login|\$?login)"
    r"\s*(?:=|:)\s*['\"]?([1-9][0-9]*)['\"]?"
)
POWERSHELL_LOGIN_PARAMETER = re.compile(r"(?ix)(?:-login|-accountlogin)\s+['\"]?([1-9][0-9]*)['\"]?")


def _run(root: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        input=input_bytes,
        check=True,
        capture_output=True,
    ).stdout


def git_root(path: Path) -> Path:
    """Resolve a repository subdirectory to its Git worktree root."""

    resolved = path.resolve()
    output = subprocess.run(
        ["git", "-C", str(resolved), "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8").strip()
    return Path(output).resolve()


def _relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root).as_posix()


def tracked_files(root: Path) -> list[Path]:
    return [
        root / item.decode("utf-8", "surrogateescape")
        for item in _run(root, "ls-files", "-z").split(b"\0")
        if item
    ]


def untracked_files(root: Path) -> list[Path]:
    output = _run(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    result: list[Path] = []
    for item in output.split(b"\0"):
        if item.startswith(b"?? "):
            path = root / item[3:].decode("utf-8", "surrogateescape")
            if path.is_file():
                result.append(path)
    return result


def ignored_files(root: Path) -> list[Path]:
    output = _run(root, "ls-files", "--others", "--ignored", "--exclude-standard", "-z")
    result: list[Path] = []
    for item in output.split(b"\0"):
        if not item:
            continue
        path = root / item.decode("utf-8", "surrogateescape")
        if path.is_file():
            result.append(path)
    return result


def _is_fixture_path(relative: str) -> bool:
    return relative.startswith(FIXTURE_PREFIXES)


def _positive_ascii_login(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return 0 < value <= 2**63 - 1
    return (
        isinstance(value, str)
        and value.isascii()
        and ASCII_DECIMAL.fullmatch(value.strip()) is not None
        and 0 < int(value.strip()) <= 2**63 - 1
    )


def concrete_paths(value: object, prefix: str = "$") -> list[str]:
    """Return JSON paths containing concrete account-login values."""

    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}"
            if str(key).casefold() in ACCOUNT_KEYS and _positive_ascii_login(item):
                found.append(path)
            found.extend(concrete_paths(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(concrete_paths(item, f"{prefix}[{index}]"))
    return found


def _ast_literal(value: ast.AST | None) -> bool:
    return isinstance(value, ast.Constant) and _positive_ascii_login(value.value)


def _is_account_environment_call(value: ast.AST) -> bool:
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and value.func.attr in {"get", "getenv"}
        and bool(value.args)
        and isinstance(value.args[0], ast.Constant)
        and value.args[0].value == "XM_MT5_ACCOUNT_LOGIN"
    )


def python_concrete_paths(raw: bytes) -> list[str]:
    """Find concrete account-login literals in Python without evaluating code."""

    try:
        tree = ast.parse(raw.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError):
        return []
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id.casefold() in ACCOUNT_KEYS:
                    if _ast_literal(node.value):
                        found.add(f"$.{target.id}")
                    if _is_account_environment_call(node.value) and any(
                        _ast_literal(argument) for argument in node.value.args[1:]
                    ):
                        found.add(f"$.{target.id}.default")
        elif isinstance(node, ast.keyword) and node.arg and node.arg.casefold() in ACCOUNT_KEYS:
            if _ast_literal(node.value):
                found.add(f"$.{node.arg}")
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    if key.value.casefold() in ACCOUNT_KEYS and _ast_literal(value):
                        found.add(f"$.{key.value}")
        elif isinstance(node, ast.Call):
            function = node.func.attr if isinstance(node.func, ast.Attribute) else ""
            if _is_account_environment_call(node) and any(
                _ast_literal(argument) for argument in node.args[1:]
            ):
                found.add("$.environment_default_login")
    return sorted(found)


def powershell_concrete_paths(raw: bytes) -> list[str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return []
    found: list[str] = []
    for match in POWERSHELL_ACCOUNT_LITERAL.finditer(text):
        found.append("$.powershell_account_literal")
    for match in POWERSHELL_LOGIN_PARAMETER.finditer(text):
        found.append("$.powershell_login_parameter")
    return sorted(set(found))


def _structural_paths(relative: str, raw: bytes) -> list[str]:
    if _is_fixture_path(relative):
        return []
    suffix = Path(relative).suffix.casefold()
    if suffix == ".py":
        return python_concrete_paths(raw)
    if suffix == ".ps1":
        return powershell_concrete_paths(raw)
    if suffix == ".json":
        try:
            return concrete_paths(json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return []
    return []


def _rule_id(value: bytes) -> str:
    return f"denylist-{sha256(value).hexdigest()[:16]}"


def _rules(denied: Iterable[bytes]) -> list[tuple[str, bytes]]:
    return [(_rule_id(value), value) for value in denied]


def _match_rules(raw: bytes, rules: list[tuple[str, bytes]]) -> tuple[int, list[dict[str, object]]]:
    matches = [
        {"rule_id": rule_id, "match_count": raw.count(value)}
        for rule_id, value in rules
        if raw.count(value)
    ]
    return sum(int(item["match_count"]) for item in matches), matches


def scan(root: Path, denylist: Path | None = None) -> list[dict[str, object]]:
    """Scan tracked, untracked, and ignored files in the normalized Git root."""

    root = git_root(root)
    denied = _deny_values(denylist)[0] if denylist is not None else []
    rules = _rules(denied)
    candidates = (
        [(path, "tracked") for path in tracked_files(root)]
        + [(path, "untracked") for path in untracked_files(root)]
        + ([(path, "ignored") for path in ignored_files(root)] if denylist is not None else [])
    )
    matches: list[dict[str, object]] = []
    seen: set[Path] = set()
    for path, scope in candidates:
        path = path.resolve()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        relative = _relative(root, path)
        raw = path.read_bytes()
        field_paths = _structural_paths(relative, raw)
        deny_count, deny_rules = _match_rules(raw, rules)
        if field_paths or deny_count:
            matches.append(
                {
                    "path": relative,
                    "scope": scope,
                    "field_paths": field_paths,
                    "structural_violation": bool(field_paths),
                    "denylist_match_count": deny_count,
                    "denylist_rules": deny_rules,
                    "file_sha256": sha256(raw).hexdigest(),
                }
            )
    return matches


def _deny_values(denylist: Path | None) -> tuple[list[bytes], str | None]:
    if denylist is None:
        return [], None
    payload_raw = denylist.read_bytes()
    payload = json.loads(payload_raw.decode("utf-8"))
    values = payload.get("denylist") if isinstance(payload, dict) else None
    if (
        not isinstance(values, list)
        or not values
        or any(not isinstance(item, str) or not item for item in values)
        or len(values) != len(set(values))
    ):
        raise ValueError("private denylist must contain a non-empty unique string array")
    return [item.encode("utf-8") for item in values], sha256(payload_raw).hexdigest()


def _default_history_refs(root: Path) -> list[str]:
    current = subprocess.run(
        ["git", "-C", str(root), "symbolic-ref", "-q", "HEAD"],
        check=False,
        capture_output=True,
    ).stdout.decode("utf-8", "replace").strip()
    remote_and_tags = _run(
        root,
        "for-each-ref",
        "--format=%(refname)",
        "refs/remotes/origin",
        "refs/tags",
    ).decode("utf-8", "replace").splitlines()
    return list(dict.fromkeys([current or "HEAD", *remote_and_tags]))


def _ref_names(root: Path, refs: list[str] | None = None) -> list[tuple[str, str]]:
    if refs:
        result = []
        for ref in refs:
            sha_value = _run(root, "rev-parse", "--verify", ref).decode("ascii").strip()
            result.append((ref, sha_value))
        return result
    return _ref_names(root, _default_history_refs(root))


def _revision_args(refs: list[str] | None) -> list[str]:
    return refs if refs else ["--all"]


def _history_object_ids(root: Path, refs: list[str] | None) -> tuple[dict[str, str], list[str], list[tuple[str, str]]]:
    revisions = _revision_args(refs)
    object_paths: dict[str, str] = {}
    for line in _run(root, "rev-list", "--objects", *revisions).decode(
        "utf-8", "surrogateescape"
    ).splitlines():
        object_id, _, path = line.partition(" ")
        object_paths.setdefault(object_id, path)
    commits = _run(root, "rev-list", *revisions).decode("ascii").split()
    ref_tips = _ref_names(root, refs)
    object_ids = list(dict.fromkeys([*object_paths, *commits, *(sha_value for _, sha_value in ref_tips)]))
    return object_paths, object_ids, ref_tips


def _read_objects(root: Path, object_ids: list[str]) -> list[tuple[str, bytes, bytes]]:
    if not object_ids:
        return []
    data = subprocess.run(
        ["git", "-C", str(root), "cat-file", "--batch"],
        input=("\n".join(object_ids) + "\n").encode("ascii"),
        check=True,
        capture_output=True,
    ).stdout
    result: list[tuple[str, bytes, bytes]] = []
    offset = 0
    for object_id in object_ids:
        header_end = data.find(b"\n", offset)
        if header_end < 0:
            raise RuntimeError("Git object stream is truncated")
        header = data[offset:header_end].split()
        offset = header_end + 1
        if len(header) != 3 or header[0].decode("ascii", "replace") != object_id:
            raise RuntimeError("Git object stream header is invalid")
        kind = header[1]
        size = int(header[2])
        body = data[offset : offset + size]
        if len(body) != size:
            raise RuntimeError("Git object stream body is truncated")
        offset += size
        if offset >= len(data) or data[offset : offset + 1] != b"\n":
            raise RuntimeError("Git object stream delimiter is missing")
        offset += 1
        result.append((object_id, kind, body))
    if offset != len(data):
        raise RuntimeError("Git object stream contains trailing bytes")
    return result


def _tree_path_matches(
    root: Path, refs: list[tuple[str, str]], rules: list[tuple[str, bytes]]
) -> list[dict[str, object]]:
    matches: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    for ref, _ in refs:
        output = _run(root, "ls-tree", "-r", "-t", "--full-name", ref)
        for line in output.splitlines():
            try:
                metadata, path_bytes = line.split(b"\t", 1)
                fields = metadata.split()
                object_id, object_type = fields[2].decode("ascii"), fields[1].decode("ascii")
            except (IndexError, ValueError, UnicodeDecodeError) as exc:
                raise RuntimeError("Git tree path listing is malformed") from exc
            count, rule_matches = _match_rules(path_bytes, rules)
            if not count:
                continue
            for item in rule_matches:
                key = (object_id, object_type, str(item["rule_id"]))
                if key in seen:
                    continue
                seen.add(key)
                matches.append(
                    {
                        "object": object_id,
                        "object_type": "tree_path",
                        "path": path_bytes.decode("utf-8", "replace"),
                        "ref": ref,
                        "denylist_match_count": int(item["match_count"]),
                        "denylist_rules": [item],
                    }
                )
    return matches


def _ref_name_matches(refs: list[tuple[str, str]], rules: list[tuple[str, bytes]]) -> list[dict[str, object]]:
    matches: list[dict[str, object]] = []
    for ref, object_id in refs:
        count, rule_matches = _match_rules(ref.encode("utf-8"), rules)
        if count:
            matches.append(
                {
                    "object": object_id,
                    "object_type": "ref_name",
                    "ref": ref,
                    "denylist_match_count": count,
                    "denylist_rules": rule_matches,
                }
            )
    return matches


def _history_scan(
    root: Path, denied: list[bytes], refs: list[str] | None = None
) -> tuple[list[dict[str, object]], dict[str, int | bool]]:
    root = git_root(root)
    refs = refs if refs is not None else _default_history_refs(root)
    rules = _rules(denied)
    object_paths, object_ids, ref_tips = _history_object_ids(root, refs)
    object_rows: list[dict[str, object]] = []
    counts: dict[str, int | bool] = {
        "history_complete": not _run(root, "rev-parse", "--is-shallow-repository").decode().strip() == "true",
        "commit_count": len(_run(root, "rev-list", *(_revision_args(refs)), "--count").decode().strip()),
        "object_count": len(object_ids),
        "blob_count": 0,
        "blob_denylist_unique_object_count": 0,
        "blob_denylist_occurrence_count": 0,
        "commit_message_match_count": 0,
        "tag_note_match_count": 0,
        "tree_path_match_count": 0,
        "ref_name_match_count": 0,
        "scanned_bytes": 0,
    }
    # rev-list --count returns a decimal string; retain the numeric value in the report.
    counts["commit_count"] = int(_run(root, "rev-list", *(_revision_args(refs)), "--count").decode().strip() or 0)
    for object_id, kind, body in _read_objects(root, object_ids):
        counts["scanned_bytes"] = int(counts["scanned_bytes"]) + len(body)
        deny_count, deny_rules = _match_rules(body, rules)
        object_type = kind.decode("ascii", "replace")
        path = object_paths.get(object_id, "")
        if kind == b"blob":
            counts["blob_count"] = int(counts["blob_count"]) + 1
            counts["blob_denylist_occurrence_count"] = int(counts["blob_denylist_occurrence_count"]) + deny_count
            if deny_count:
                counts["blob_denylist_unique_object_count"] = int(counts["blob_denylist_unique_object_count"]) + 1
        if kind == b"commit":
            counts["commit_message_match_count"] = int(counts["commit_message_match_count"]) + deny_count
        if kind == b"tag":
            counts["tag_note_match_count"] = int(counts["tag_note_match_count"]) + deny_count
        field_paths = _structural_paths(path, body) if kind == b"blob" else []
        if field_paths or deny_count:
            object_rows.append(
                {
                    "object": object_id,
                    "object_type": object_type,
                    "path": path or f"<{object_type}>",
                    "field_paths": field_paths,
                    "structural_violation": bool(field_paths),
                    "denylist_match_count": deny_count,
                    "denylist_rules": deny_rules,
                    "blob_sha256": sha256(body).hexdigest(),
                }
            )
    tree_rows = _tree_path_matches(root, ref_tips, rules)
    ref_rows = _ref_name_matches(ref_tips, rules)
    counts["tree_path_match_count"] = sum(int(row["denylist_match_count"]) for row in tree_rows)
    counts["ref_name_match_count"] = sum(int(row["denylist_match_count"]) for row in ref_rows)
    history_rows = [*object_rows, *tree_rows, *ref_rows]
    counts["denylist_occurrence_count"] = sum(int(row["denylist_match_count"]) for row in history_rows)
    counts["denylist_unique_object_count"] = len(
        {
            str(row["object"])
            for row in history_rows
            if int(row["denylist_match_count"])
        }
    )
    counts["denylist_unique_file_count"] = len(
        {
            str(row.get("path", ""))
            for row in history_rows
            if int(row["denylist_match_count"])
        }
    )
    counts["denylist_match_count"] = int(counts["denylist_occurrence_count"])
    return history_rows, counts


def _baseline_entries(rows: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for row in rows:
        for rule in row.get("denylist_rules", []):
            entries.append(
                {
                    "object": row["object"],
                    "object_type": row["object_type"],
                    "rule_id": rule["rule_id"],
                    "match_count": int(rule["match_count"]),
                }
            )
    return sorted(entries, key=lambda item: (str(item["object"]), str(item["object_type"]), str(item["rule_id"])))


def _baseline_check(rows: list[dict[str, object]], baseline: Path) -> dict[str, object]:
    payload = json.loads(baseline.read_text(encoding="utf-8"))
    expected = payload.get("findings") if isinstance(payload, dict) else None
    if not isinstance(expected, list):
        raise ValueError("history baseline must contain a findings array")
    actual = _baseline_entries(rows)
    expected_keys = {
        (str(item["object"]), str(item["object_type"]), str(item["rule_id"]), int(item["match_count"]))
        for item in expected
    }
    actual_keys = {
        (str(item["object"]), str(item["object_type"]), str(item["rule_id"]), int(item["match_count"]))
        for item in actual
    }
    return {
        "baseline_entry_count": len(expected),
        "new_historical_findings": [
            item for item in actual if (
                str(item["object"]), str(item["object_type"]), str(item["rule_id"]), int(item["match_count"])
            ) not in expected_keys
        ],
        "missing_baseline_findings": [
            item for item in expected if (
                str(item["object"]), str(item["object_type"]), str(item["rule_id"]), int(item["match_count"])
            ) not in actual_keys
        ],
    }


def _git_origin(root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()


def _write_baseline(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": 1, "findings": _baseline_entries(rows)}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--denylist", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--history-ref", action="append", dest="history_refs")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    root = git_root(args.root)
    before_head = _run(root, "rev-parse", "HEAD").decode("ascii").strip()
    before_origin = _git_origin(root)
    started = datetime.now(timezone.utc)
    report: dict[str, object]
    try:
        denied, denylist_sha256 = _deny_values(args.denylist)
        working_matches = scan(root, args.denylist)
        history_matches, counts = _history_scan(root, denied, args.history_refs)
        baseline_result: dict[str, object] = {
            "baseline_entry_count": None,
            "new_historical_findings": [],
            "missing_baseline_findings": [],
        }
        if args.write_baseline:
            if args.baseline is None:
                raise ValueError("--write-baseline requires --baseline")
            _write_baseline(args.baseline, history_matches)
        elif args.baseline is not None:
            baseline_result = _baseline_check(history_matches, args.baseline)
        after_head = _run(root, "rev-parse", "HEAD").decode("ascii").strip()
        after_origin = _git_origin(root)
        structural_working = [row for row in working_matches if row["structural_violation"]]
        working_deny_count = sum(int(row["denylist_match_count"]) for row in working_matches)
        history_deny_count = int(counts["denylist_match_count"])
        baseline_violation = bool(
            baseline_result["new_historical_findings"]
        )
        if args.denylist is None:
            status = "BLOCKED_MISSING_DENYLIST"
        elif not bool(counts["history_complete"]):
            status = "BLOCKED_INCOMPLETE_HISTORY"
        elif structural_working or working_deny_count or baseline_violation:
            status = "VIOLATION"
        elif args.baseline is None and not args.write_baseline:
            status = "BLOCKED_MISSING_BASELINE"
        else:
            status = "PASS"
        report = {
            "schema_version": 3,
            "status": status,
            "history_scope": args.history_refs or _default_history_refs(root),
            "source_head": before_head,
            "all_ref_tips": [{"ref": ref, "sha": sha_value} for ref, sha_value in _ref_names(root)],
            "scanner_source_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
            "private_denylist_sha256": denylist_sha256,
            "working_tree_match_count": len(working_matches),
            "working_tree_structural_violation_count": len(structural_working),
            "working_tree_denylist_match_count": working_deny_count,
            "reachable_history_match_count": len(history_matches),
            "reachable_history_denylist_match_count": history_deny_count,
            "source_unchanged": before_head == after_head,
            "origin_unchanged": before_origin == after_origin,
            "started_at_utc": started.isoformat().replace("+00:00", "Z"),
            "finished_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            **counts,
            **baseline_result,
            "matches": working_matches,
            "reachable_history_matches": history_matches,
        }
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2, sort_keys=True))
        if status in {"BLOCKED_MISSING_DENYLIST", "BLOCKED_INCOMPLETE_HISTORY", "BLOCKED_MISSING_BASELINE"}:
            raise SystemExit(2)
        raise SystemExit(1 if status == "VIOLATION" else 0)
    except SystemExit:
        raise
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        report = {
            "schema_version": 3,
            "status": "BLOCKED_INCOMPLETE_SCAN",
            "error": f"{type(exc).__name__}: {exc}",
            "source_head": before_head,
            "origin_unchanged": before_origin == _git_origin(root),
        }
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2, sort_keys=True))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
