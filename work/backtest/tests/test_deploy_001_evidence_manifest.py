import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _verify_evidence_manifest(evidence_root: Path, repo_root: Path) -> None:
    manifest = json.loads((evidence_root / "manifest.json").read_text(encoding="utf-8"))
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
        for binding_name in ("junit", "stdout_stderr"):
            if binding_name not in run:
                continue
            binding = run[binding_name]
            relative = str(binding["path"])
            checkout = (evidence_root / relative).read_bytes()
            checkout_sha256 = hashlib.sha256(checkout).hexdigest()
            assert checkout_sha256 == binding["sha256"]
            committed = subprocess.check_output(
                [
                    "git",
                    "show",
                    f"HEAD:work/backtest/docs/DEPLOY_001_EVIDENCE/{relative}",
                ],
                cwd=repo_root,
            )
            assert committed == checkout
            assert hashlib.sha256(committed).hexdigest() == binding["git_blob_sha256"]


def test_evidence_manifest_binds_checkout_and_committed_bytes() -> None:
    _verify_evidence_manifest(ROOT / "docs" / "DEPLOY_001_EVIDENCE", ROOT.parents[1])


def test_evidence_manifest_rejects_each_individually_corrupted_artifact(
    tmp_path: Path,
) -> None:
    evidence_root = ROOT / "docs" / "DEPLOY_001_EVIDENCE"
    fixture_root = tmp_path / "DEPLOY_001_EVIDENCE"
    shutil.copytree(evidence_root, fixture_root)
    manifest = json.loads((fixture_root / "manifest.json").read_text(encoding="utf-8"))
    artifacts = [
        str(run[binding_name]["path"])
        for run in manifest["runs"]
        for binding_name in ("junit", "stdout_stderr")
        if binding_name in run
    ]
    assert sorted(artifacts) == sorted(
        ["targeted.junit.xml", "targeted.log", "collection.log", "ast.log"]
    )

    _verify_evidence_manifest(fixture_root, ROOT.parents[1])
    for relative in artifacts:
        artifact = fixture_root / relative
        artifact.write_bytes(artifact.read_bytes() + b"\nCORRUPTED\n")
        with pytest.raises(AssertionError):
            _verify_evidence_manifest(fixture_root, ROOT.parents[1])
        shutil.copy2(evidence_root / relative, artifact)
