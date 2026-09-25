import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from verify_clean_delivery import safe_path, verify_bindings


@pytest.fixture
def committed_evidence(tmp_path: Path):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], stderr=subprocess.PIPE).decode().strip()

    git("init", "-q")
    git("config", "user.name", "Evidence test")
    git("config", "user.email", "evidence@example.invalid")
    git("config", "core.autocrlf", "false")
    (tmp_path / "source.py").write_bytes(b"source\n")
    git("add", ".")
    git("commit", "-qm", "source")
    tested = git("rev-parse", "HEAD")
    prefix = "work/backtest/docs/DEPLOY_001_EVIDENCE_FINAL_fixture"
    evidence = tmp_path / prefix
    evidence.mkdir(parents=True)
    data = b"passed\n"
    (evidence / "full.log").write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    manifest = {"tested_commit": tested, "tested_tree": git("rev-parse", "HEAD^{tree}"),
                "evidence_repo_path": prefix, "artifacts": [{"path": "full.log", "bytes": len(data),
                "sha256": digest, "git_blob_sha256": digest, "git_blob_sha1": git("hash-object", str(evidence / "full.log"))}]}
    (evidence / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "evidence")
    return tmp_path, evidence, git, manifest


def test_clean_evidence_binds_real_git_bytes_and_rejects_mutation(committed_evidence):
    repo, evidence, git, manifest = committed_evidence
    assert verify_bindings(repo, evidence, "HEAD") == manifest
    artifact = evidence / "full.log"
    artifact.write_bytes(b"failed\n")
    with pytest.raises(ValueError, match="artifact hash mismatch"):
        verify_bindings(repo, evidence, "HEAD")
    # Rewriting the manifest alongside corrupt bytes cannot change committed evidence.
    manifest["artifacts"][0]["sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    (evidence / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest differs"):
        verify_bindings(repo, evidence, "HEAD")


def test_clean_evidence_rejects_config_change_after_tested_commit(committed_evidence):
    repo, evidence, git, _ = committed_evidence
    (repo / ".gitattributes").write_text("*.log -text\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "config drift")
    with pytest.raises(ValueError, match="source/config changed"):
        verify_bindings(repo, evidence, "HEAD")


def test_clean_evidence_rejects_unlisted_files_and_unsafe_paths(committed_evidence):
    repo, evidence, _, _ = committed_evidence
    (evidence / "unlisted.log").write_text("unlisted", encoding="utf-8")
    with pytest.raises(ValueError, match="inventory incomplete"):
        verify_bindings(repo, evidence, "HEAD")
    for path in ("../escape", "/absolute", "C:/absolute", "a\\b", "a//b", "a/./b", ""):
        with pytest.raises(ValueError):
            safe_path(path)
