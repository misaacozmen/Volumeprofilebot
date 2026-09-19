from __future__ import annotations

import json
from pathlib import Path

from scripts.scan_public_broker_identity import _history_scan, scan


def _git(repo: Path, *args: str) -> None:
    import subprocess

    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_denylist_covers_nested_tracked_untracked_ignored_and_history(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    token = "765432109"
    deny = tmp_path / "deny.json"
    deny.write_text(json.dumps({"denylist": [token]}), encoding="utf-8")
    nested = repo / "work" / "backtest" / "outputs" / "live_recovery" / "20000101" / "v16-audit" / "operator"
    nested.mkdir(parents=True)
    tracked = nested / "controller.ps1"
    tracked.write_text(f"$expected=@{{account_login={token}}}\n", encoding="utf-8")
    (repo / ".gitignore").write_text("ignored.ps1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
    (nested / "untracked.ps1").write_text(f"$account_login={token}", encoding="utf-8")
    (nested / "ignored.ps1").write_text(f"$account_login={token}", encoding="utf-8")
    hits = scan(repo, deny)
    assert {row["scope"] for row in hits if row["denylist_match_count"]} == {
        "tracked",
        "untracked",
        "ignored",
    }
    assert sum(row["denylist_match_count"] for row in hits) == 3
    tracked.write_text("$expected=@{account_login=0}\n", encoding="utf-8")
    _git(repo, "add", str(tracked))
    _git(repo, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "remove current literal")
    matches, counts = _history_scan(repo, [token.encode()])
    assert counts["blob_denylist_occurrence_count"] == 1
    assert any(row["path"].endswith("operator/controller.ps1") for row in matches)
