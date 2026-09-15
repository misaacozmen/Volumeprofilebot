from __future__ import annotations

from pathlib import Path
import subprocess

from scripts.scan_public_broker_identity import _history_scan, concrete_paths, python_concrete_paths, scan


ROOT = Path(__file__).resolve().parents[1]


def test_concrete_account_identity_is_detected() -> None:
    assert concrete_paths({"account_login": 12345678, "nested": {"expected_server": "fixture"}}) == [
        "$.account_login", "$.nested.expected_server"
    ]


def test_python_identity_literal_is_detected_without_evaluation() -> None:
    assert python_concrete_paths(b"ACCOUNT_LOGIN = 123456\n") == ["$.ACCOUNT_LOGIN"]
    assert python_concrete_paths(b"login = int(environment_value('XM_MT5_ACCOUNT_LOGIN'))\n") == []


def test_reachable_python_blob_identity_is_detected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "scripts").mkdir()
    (repo / "scripts" / "fixture.py").write_text("ACCOUNT_LOGIN = 123456\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "scripts/fixture.py"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture"], cwd=repo, check=True)
    matches, _ = _history_scan(repo, [])
    assert any(row["path"] == "scripts/fixture.py" and "$.ACCOUNT_LOGIN" in row["field_paths"] for row in matches)


def test_tracked_public_tree_has_no_concrete_broker_identity() -> None:
    assert scan(ROOT) == []
