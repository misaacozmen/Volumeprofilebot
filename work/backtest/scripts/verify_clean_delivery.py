"""Verify clean ancestry, complete source equality, byte evidence and test multisets."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import xml.etree.ElementTree as ET


def git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def safe_path(value: str) -> str:
    require(bool(value) and "\\" not in value and ":" not in value, "unsafe evidence path")
    require(not PurePosixPath(value).is_absolute(), "absolute evidence path")
    require(all(part not in {"", ".", ".."} for part in value.split("/")), "unsafe evidence path")
    return value


def source_tree(repo: Path, ref: str, evidence_path: str) -> dict[str, str]:
    entries = {}
    for entry in git(repo, "ls-tree", "-rz", ref).split(b"\0"):
        if not entry:
            continue
        metadata, path = entry.split(b"\t", 1)
        name = path.decode("utf-8")
        if not name.startswith(evidence_path + "/"):
            entries[name] = metadata.decode("ascii")
    return entries


def verify_bindings(repo: Path, evidence: Path, ref: str) -> dict:
    manifest = json.loads((evidence / "manifest.json").read_text(encoding="utf-8"))
    prefix = safe_path(manifest["evidence_repo_path"])
    require(prefix.startswith("work/backtest/docs/DEPLOY_001_EVIDENCE_FINAL_"), "invalid evidence scope")
    tested = manifest["tested_commit"]
    require(re.fullmatch(r"[0-9a-f]{40}", tested) is not None, "tested commit must be exact")
    require(git(repo, "rev-parse", f"{tested}^{{tree}}").decode().strip() == manifest["tested_tree"], "tested tree mismatch")
    require(subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", tested, ref], capture_output=True).returncode == 0,
            "tested commit is not an ancestor of evidence ref")
    require(source_tree(repo, tested, prefix) == source_tree(repo, ref, prefix), "source/config changed after tested commit")
    require(git(repo, "show", f"{ref}:{prefix}/manifest.json") == (evidence / "manifest.json").read_bytes(), "manifest differs from committed bytes")
    artifacts = manifest["artifacts"]
    require(bool(artifacts), "empty evidence inventory")
    listed = set()
    for item in artifacts:
        relative = safe_path(item["path"])
        require(relative not in listed and relative != "manifest.json", "duplicate/self artifact")
        listed.add(relative)
        path = evidence / relative
        require(not path.is_symlink() and path.is_file(), "artifact must be a regular file")
        require(path.resolve().is_relative_to(evidence.resolve()), "artifact escapes package")
        data = path.read_bytes()
        require(len(data) == item["bytes"], f"artifact size mismatch: {relative}")
        digest = hashlib.sha256(data).hexdigest()
        require(digest == item["sha256"] == item["git_blob_sha256"], f"artifact hash mismatch: {relative}")
        committed = git(repo, "show", f"{ref}:{prefix}/{relative}")
        require(committed == data, f"artifact Git bytes mismatch: {relative}")
        oid = git(repo, "rev-parse", f"{ref}:{prefix}/{relative}").decode().strip()
        require(oid == item["git_blob_sha1"], f"artifact Git object mismatch: {relative}")
    present = {path.relative_to(evidence).as_posix() for path in evidence.rglob("*") if path.is_file() or path.is_symlink()}
    require(present == listed | {"manifest.json"}, "evidence inventory incomplete")
    return manifest


def verify(repo: Path, evidence: Path, ref: str) -> dict:
    manifest = verify_bindings(repo, evidence, ref)
    project = repo / "work/backtest"
    helper_path = project / "docs/DEPLOY_001_EVIDENCE_FINAL_20260924_a0bc53f/verify_final_evidence.py"
    spec = importlib.util.spec_from_file_location("historical_node_contract", helper_path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    collection = (evidence / "collection.log").read_text(encoding="utf-8-sig")
    full = (evidence / "full.log").read_text(encoding="utf-8-sig")
    node_pattern = r"^tests/\S+\.py::.+$"
    nodes = [line for line in collection.splitlines() if re.match(node_pattern, line)]
    baseline = [line for line in (helper_path.parent / "collection.log").read_text(encoding="utf-8-sig").splitlines() if re.match(node_pattern, line)]
    require(len(baseline) == 799 and len(nodes) == len(set(nodes)), "invalid collection inventory")
    require(not (Counter(baseline) - Counter(nodes)), "original 799 nodes were removed")
    root = ET.parse(evidence / "full.xml").getroot()
    cases = root.findall(".//testcase")
    expected = Counter(helper.junit_key(node) for node in nodes)
    actual = Counter((case.get("classname", ""), case.get("name", "")) for case in cases)
    require(expected == actual, "collection/JUnit multiset mismatch")
    require(not any(root.findall(f".//{kind}") for kind in ("failure", "error", "skipped")), "nonpassing JUnit")
    require(not re.search(r"[1-9][0-9]* deselected", collection + full), "deselected tests")
    require(all(helper.junit_key(node) in actual for group in helper.RISK_NODES.values() for node in group), "missing risk node")
    sandbox = sum(case.get("classname") == "tests.test_backtest_sandbox" for case in cases)
    require(sandbox == 20, "sandbox node inventory changed")
    for run in manifest["runs"]:
        require(run["exit_code"] == 0, "failed acceptance run")
        require(run["tested_commit"] == manifest["tested_commit"], "run tested a different commit")
    return {"status": "PASS", "tested_commit": manifest["tested_commit"],
            "evidence_commit": git(repo, "rev-parse", ref).decode().strip(),
            "artifacts": len(manifest["artifacts"]), "passed": len(nodes), "sandbox_passed": sandbox,
            "failed": 0, "errors": 0, "skipped": 0, "deselected": 0,
            "preserved_baseline_nodes": len(baseline), "added_nodes": sorted(set(nodes) - set(baseline)),
            "node_sha256_sorted_lf": hashlib.sha256("\n".join(sorted(nodes)).encode()).hexdigest()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--ref", required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.repo.resolve(), args.evidence.resolve(), args.ref), indent=2))
