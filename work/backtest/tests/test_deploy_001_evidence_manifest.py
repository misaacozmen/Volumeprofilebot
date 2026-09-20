import hashlib
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_evidence_manifest_binds_checkout_and_committed_bytes() -> None:
    evidence_root = ROOT / "docs" / "DEPLOY_001_EVIDENCE"
    manifest = json.loads((evidence_root / "manifest.json").read_text(encoding="utf-8"))
    repo_root = ROOT.parents[1]
    tested_commit = str(manifest["tested_commit"])
    assert subprocess.check_output(
        ["git", "rev-parse", tested_commit], cwd=repo_root, text=True
    ).strip() == tested_commit
    assert subprocess.check_output(
        ["git", "rev-parse", f"{tested_commit}^{{tree}}"],
        cwd=repo_root,
        text=True,
    ).strip() == manifest["tested_tree"]

    source_paths = [str(path) for path in manifest["production_test_paths"]]
    assert subprocess.run(
        ["git", "diff", "--quiet", tested_commit, "HEAD", "--", *source_paths],
        cwd=repo_root,
        check=False,
    ).returncode == 0

    for run in manifest["runs"]:
        binding = run.get("junit", run.get("stdout_stderr"))
        relative = str(binding["path"])
        checkout = (evidence_root / relative).read_bytes()
        checkout_sha256 = hashlib.sha256(checkout).hexdigest()
        assert checkout_sha256 == binding["sha256"]
        committed = subprocess.check_output(
            ["git", "show", f"HEAD:work/backtest/docs/DEPLOY_001_EVIDENCE/{relative}"],
            cwd=repo_root,
        )
        assert committed == checkout
        assert hashlib.sha256(committed).hexdigest() == binding["git_blob_sha256"]
