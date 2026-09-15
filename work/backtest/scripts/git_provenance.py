"""Local Git object validation used by acquisition and promotion consumers."""

from __future__ import annotations

import re
from pathlib import Path
import subprocess
from hashlib import sha256


OID_RE = re.compile(r"^[0-9a-f]{40}$")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("required Git object is unavailable")
    return result.stdout.strip()


def validate_commit_tree(repository_root: str | Path, commit_oid: str, tree_oid: str) -> dict[str, str]:
    """Require an existing commit and its exact tree object, with no ref lookup."""
    repo = Path(repository_root).resolve()
    commit = str(commit_oid).strip()
    tree = str(tree_oid).strip()
    if not repo.is_dir() or not OID_RE.fullmatch(commit) or not OID_RE.fullmatch(tree):
        raise ValueError("Git commit/tree identity is incomplete")
    observed_commit = _git(repo, "cat-file", "-e", f"{commit}^{{commit}}")
    if observed_commit != "":
        raise ValueError("Git commit existence probe returned unexpected output")
    observed_tree = _git(repo, "rev-parse", f"{commit}^{{tree}}")
    if observed_tree != tree:
        raise ValueError("Git commit-to-tree binding differs")
    observed_tree_type = _git(repo, "cat-file", "-t", tree)
    if observed_tree_type != "tree":
        raise ValueError("Git source tree object is not a tree")
    return {"source_commit": commit, "source_tree_sha256": tree}


def current_commit_tree(repository_root: str | Path) -> dict[str, str]:
    repo = Path(repository_root).resolve()
    commit = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", f"{commit}^{{tree}}")
    return validate_commit_tree(repo, commit, tree)


def validate_production_source_binding(repository_root: str | Path, commit_oid: str, tree_oid: str) -> dict[str, str]:
    """Require the bound Git tree to contain the exact current backtest source bytes."""
    repo = Path(repository_root).resolve()
    identity = validate_commit_tree(repo, commit_oid, tree_oid)
    current = sorted(
        path.relative_to(repo).as_posix()
        for path in (repo / "backtest").rglob("*.py")
        if path.is_file() and not any(part in {"__pycache__", ".pytest_cache"} for part in path.parts)
    )
    tree_paths = _git(repo, "ls-tree", "-r", "--name-only", tree_oid).splitlines()
    bound = sorted(path for path in tree_paths if path.startswith("backtest/") and path.endswith(".py"))
    if current != bound:
        raise ValueError("Git source tree does not contain the current production source file set")
    for relative in current:
        current_bytes = (repo / relative).read_bytes()
        bound_bytes = subprocess.run(
            ["git", "-C", str(repo), "show", f"{tree_oid}:{relative}"],
            capture_output=True,
            check=False,
        )
        if bound_bytes.returncode != 0 or sha256(bound_bytes.stdout).digest() != sha256(current_bytes).digest():
            raise ValueError(f"Git source tree does not bind current production bytes: {relative}")
    return identity


def current_branch(repository_root: str | Path) -> str:
    branch = _git(Path(repository_root).resolve(), "symbolic-ref", "--quiet", "--short", "HEAD")
    if not branch:
        raise ValueError("Git source is detached and has no promotion branch")
    return branch
