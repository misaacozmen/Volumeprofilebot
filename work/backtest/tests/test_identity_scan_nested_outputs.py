"""Denylist coverage is byte-based and must include archived PowerShell outputs."""
import json
import subprocess
from pathlib import Path
from scripts.scan_public_broker_identity import scan, _history_scan


def git(repo, *args):
    subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)


def test_nested_powershell_tracked_untracked_ignored_and_history(tmp_path: Path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-q')
    # Synthetic value only: never embed a private denylist value in source tests.
    token = '765432109'
    deny = tmp_path / 'deny.json'
    deny.write_text(json.dumps({'denylist': [token]}))
    nested = repo / 'work/backtest/outputs/live_recovery/20000101/v16-audit/operator'
    nested.mkdir(parents=True)
    tracked = nested / 'controller.ps1'
    tracked.write_text('$expected=@{account_login=' + token + '}\n')
    (repo / '.gitignore').write_text('ignored.ps1\n')
    git(repo, 'add', '.')
    git(repo, '-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')
    (nested / 'untracked.ps1').write_text('$account_login=' + token)
    (nested / 'ignored.ps1').write_text('$account_login=' + token)
    hits = scan(repo, deny)
    assert {row['scope'] for row in hits if row['denylist_match_count']} == {'tracked', 'untracked', 'ignored'}
    assert sum(row['denylist_match_count'] for row in hits) == 3
    tracked.write_text('$expected=@{account_login=0}\n')
    git(repo, 'add', str(tracked))
    git(repo, '-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'remove current literal')
    matches, counts = _history_scan(repo, [token.encode()])
    assert counts['denylist_occurrence_count'] == 1
    assert any(row['path'].endswith('operator/controller.ps1') and row['denylist_match_count'] == 1 for row in matches)
