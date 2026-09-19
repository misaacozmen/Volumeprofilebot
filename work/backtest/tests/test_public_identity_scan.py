from __future__ import annotations

import json
from pathlib import Path
import subprocess

from scripts.scan_public_broker_identity import (
    _baseline_check,
    _history_scan,
    _rules,
    _scan_ref_trees,
    _write_baseline,
    concrete_paths,
    git_root,
    python_concrete_paths,
    scan,
)


ROOT = Path(__file__).resolve().parents[1]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_concrete_account_identity_is_detected() -> None:
    assert concrete_paths({"account_login": 12345678, "nested": {"login": "87654321"}}) == [
        "$.account_login",
        "$.nested.login",
    ]


def test_python_identity_literal_is_detected_without_evaluation() -> None:
    assert python_concrete_paths(b"EXPECTED_LOGIN = 123456\n") == ["$.EXPECTED_LOGIN"]
    assert python_concrete_paths(b"login = read_account_login()\n") == []
    assert python_concrete_paths(b"login = os.getenv('XM_MT5_ACCOUNT_LOGIN', '123456')\n") == [
        "$.environment_default_login",
        "$.login.default",
    ]


def test_powershell_account_comparison_literal_is_detected() -> None:
    from scripts.scan_public_broker_identity import powershell_concrete_paths

    raw = b'if ([long]$runtime.account_login -ne "765432109") { throw "wrong" }\n'
    assert powershell_concrete_paths(raw) == ["$.powershell_account_comparison"]


def test_root_normalization_and_current_history_split(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    nested = repo / "work" / "backtest"
    nested.mkdir(parents=True)
    (repo / "scripts").mkdir()
    tracked = repo / "scripts" / "fixture.py"
    token = "765432109"
    tracked.write_text(f"ACCOUNT_LOGIN = {token}\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
    deny = tmp_path / "deny.json"
    deny.write_text(json.dumps({"denylist": [token]}), encoding="utf-8")

    assert git_root(nested) == repo.resolve()
    assert scan(nested, deny)[0]["denylist_match_count"] == 1
    (tracked).write_text("ACCOUNT_LOGIN = read_account_login()\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "clean")
    assert scan(nested, deny) == []
    history, counts = _history_scan(nested, [token.encode()])
    assert counts["blob_denylist_occurrence_count"] == 1
    assert any(row["path"] == "scripts/fixture.py" for row in history)


def test_structural_violation_is_reported_without_denylist_hit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "work" / "backtest" / "deploy").mkdir(parents=True)
    (repo / "work" / "backtest" / "deploy" / "fixture.ps1").write_text(
        "$expected_login = 123456\n", encoding="utf-8"
    )
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
    matches = scan(repo)
    assert matches and matches[0]["structural_violation"] is True


def test_explicit_test_fixture_path_is_the_only_structural_exemption(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    fixture = repo / "work" / "backtest" / "tests"
    fixture.mkdir(parents=True)
    (fixture / "test_fixture.json").write_text('{"account_login": 123456}', encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
    assert scan(repo) == []


def test_second_branch_and_annotated_tag_are_in_history_scope(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    token = "765432109"
    (repo / "clean.txt").write_text("clean\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "main")
    _git(repo, "branch", "-M", "main")
    _git(repo, "branch", "side")
    _git(repo, "switch", "side")
    (repo / "history-only.txt").write_text(token, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "side")
    _git(
        repo,
        "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
        "tag", "-a", "fixture-tag", "-m", "annotated fixture",
    )
    matches, counts = _history_scan(
        repo,
        [token.encode()],
        ["refs/heads/main", "refs/heads/side", "refs/tags/fixture-tag"],
    )
    assert counts["blob_denylist_occurrence_count"] == 1
    assert any(row["path"] == "history-only.txt" for row in matches)


def test_candidate_acceptance_ignores_dirty_main_but_repo_audit_finds_remote_tree_and_tag(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "remote", "add", "origin", "https://example.invalid/repo.git")
    token = "765432109"

    (repo / "leaked.txt").write_text(token, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "known leak")
    known_bad = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    (repo / "leaked.txt").write_text("clean\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "clean main")
    main_sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    _git(repo, "branch", "-M", "main")
    _git(repo, "switch", "-c", "side")
    (repo / "moved.txt").write_text(token, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "moved leak")
    side_sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    _git(repo, "update-ref", "refs/remotes/origin/main", main_sha)
    _git(repo, "update-ref", "refs/remotes/origin/side", side_sha)
    _git(repo, "update-ref", "refs/remotes/origin/HEAD", main_sha)
    _git(
        repo,
        "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
        "tag", "-a", "new-dirty-tag", known_bad, "-m", "reuses known dirty blob",
    )

    deny = tmp_path / "deny.json"
    deny.write_text(json.dumps({"denylist": [token]}), encoding="utf-8")
    history, _ = _history_scan(repo, [token.encode()], ["refs/remotes/origin/main", "refs/remotes/origin/side"])
    baseline = tmp_path / "baseline.json"
    _write_baseline(baseline, history)
    scanner = ROOT / "scripts" / "scan_public_broker_identity.py"

    candidate = subprocess.run(
        [
            "py", "-3", str(scanner), "--root", str(repo), "--denylist", str(deny),
            "--baseline", str(baseline), "--candidate-ref", "refs/remotes/origin/main",
        ],
        capture_output=True,
        text=True,
    )
    assert candidate.returncode == 0, candidate.stdout
    assert '"scan_scope": "candidate_acceptance"' in candidate.stdout

    tree_scans = _scan_ref_trees(
        repo,
        [("refs/remotes/origin/main", main_sha), ("refs/remotes/origin/side", side_sha), ("refs/tags/new-dirty-tag", known_bad)],
        _rules([token.encode()]),
    )
    assert any(row["ref"] == "refs/remotes/origin/side" and row["denylist_match_count"] for row in tree_scans)
    assert any(row["ref"] == "refs/tags/new-dirty-tag" and row["denylist_match_count"] for row in tree_scans)

    audit = subprocess.run(
        [
            "py", "-3", str(scanner), "--root", str(repo), "--denylist", str(deny),
            "--baseline", str(baseline), "--repo-audit",
        ],
        capture_output=True,
        text=True,
    )
    assert audit.returncode == 1, audit.stdout
    audit_report = json.loads(audit.stdout)
    assert audit_report["repo_tree_violation"] is True
    assert "refs/remotes/origin/HEAD" not in {
        row["ref"] for row in audit_report["repo_tree_scans"]
    }


def test_new_history_finding_is_outside_fixed_baseline(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    token = "765432109"
    (repo / "history.txt").write_text(token, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "known")
    history, _ = _history_scan(repo, [token.encode()])
    baseline = tmp_path / "baseline.json"
    _write_baseline(baseline, history)
    (repo / "new-history.txt").write_text(token + " second", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "new")
    current, _ = _history_scan(repo, [token.encode()])
    result = _baseline_check(current, baseline)
    assert result["new_historical_findings"]


def test_cli_missing_denylist_is_blocked_and_structural_violation_fails(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "deploy.ps1").write_text("$login = 123456\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
    scanner = ROOT / "scripts" / "scan_public_broker_identity.py"
    structural = subprocess.run(
        ["py", "-3", str(scanner), "--root", str(repo), "--structural-only"],
        capture_output=True,
        text=True,
    )
    assert structural.returncode == 1
    assert '"scan_scope": "candidate_structure"' in structural.stdout
    missing = subprocess.run(
        ["py", "-3", str(scanner), "--root", str(repo)],
        capture_output=True,
        text=True,
    )
    assert missing.returncode == 2
    deny = tmp_path / "deny.json"
    deny.write_text(json.dumps({"denylist": ["765432109"]}), encoding="utf-8")
    history, _ = _history_scan(repo, [b"765432109"])
    baseline = tmp_path / "baseline.json"
    _write_baseline(baseline, history)
    failed = subprocess.run(
        [
            "py", "-3", str(scanner), "--root", str(repo), "--denylist", str(deny),
            "--baseline", str(baseline),
        ],
        capture_output=True,
        text=True,
    )
    assert failed.returncode == 1
    assert '"status": "VIOLATION"' in failed.stdout


def test_tracked_public_tree_has_no_structural_identity() -> None:
    assert scan(ROOT) == []
