"""Build a source-complete, non-production demo repository ZIP with secret scanning."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
import tempfile
import zipfile
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_CONTENT = re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")
HIGH_CONFIDENCE_SECRET = re.compile(rb"(?:AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{20,})")
EXCLUDED_PARTS = frozenset({".pytest_cache", "__pycache__", "node_modules", "tmp"})
EXCLUDED_PREFIXES = ("outputs/deploy/", "outputs/releases/", "outputs/live_recovery/")
EXCLUDED_SUFFIXES = frozenset({".zip", ".exe", ".dll", ".pdb", ".pyc", ".sqlite", ".sqlite3", ".db", ".p12", ".pfx", ".key"})


def _git(repo: Path, *args: str) -> list[str]:
    raw = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True).stdout
    return [item.decode("utf-8") for item in raw.split(b"\0") if item]


def _relative(raw: str, package_root: Path, repo: Path) -> Path:
    path = (repo / raw).resolve()
    path.relative_to(package_root.resolve())
    return path


def _excluded(relative: str, path: Path, output: Path, report: Path) -> str | None:
    normalized = relative.replace("\\", "/")
    parts = set(Path(normalized).parts)
    if parts & EXCLUDED_PARTS:
        return "generated-or-runtime-directory"
    if any(normalized.startswith(prefix) for prefix in EXCLUDED_PREFIXES):
        return "generated-release-or-runtime-output"
    if path.resolve() in {output.resolve(), report.resolve()}:
        return "delivery-artifact"
    if path.suffix.lower() in EXCLUDED_SUFFIXES:
        return "binary-or-local-state-file"
    if path.name.lower() == ".env":
        return "credential-environment-file"
    lowered = path.name.casefold()
    if any(fragment in lowered for fragment in ("private-key", "private_key", "credentials", "credential-store")):
        return "credential-or-private-key-named-file"
    return None


def collect(repo: Path, package_root: Path, *, output: Path, report: Path) -> tuple[list[Path], list[dict[str, str]]]:
    raw_paths = _git(repo, "ls-files", "-z", "--", "work/backtest") + _git(repo, "ls-files", "--others", "--exclude-standard", "-z", "--", "work/backtest")
    selected: list[Path] = []
    excluded: list[dict[str, str]] = []
    seen: set[Path] = set()
    for raw in sorted(set(raw_paths)):
        path = _relative(raw, package_root, repo)
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        relative = path.relative_to(package_root).as_posix()
        reason = _excluded(relative, path, output, report)
        if reason:
            excluded.append({"path": relative, "reason": reason})
        else:
            selected.append(path)
    return selected, excluded


def _scan_bytes(raw: bytes) -> list[str]:
    findings: list[str] = []
    if PRIVATE_CONTENT.search(raw):
        findings.append("private-key-material")
    if HIGH_CONFIDENCE_SECRET.search(raw):
        findings.append("high-confidence-token-pattern")
    return findings


def _inventory(files: Iterable[Path], package_root: Path) -> tuple[list[dict[str, object]], str, list[dict[str, object]]]:
    rows: list[dict[str, object]] = []
    findings: list[dict[str, object]] = []
    for path in sorted(files):
        raw = path.read_bytes()
        relative = path.relative_to(package_root).as_posix()
        rows.append({"path": relative, "bytes": len(raw), "sha256": sha256(raw).hexdigest()})
        for finding in _scan_bytes(raw):
            findings.append({"path": relative, "finding": finding})
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return rows, sha256(canonical).hexdigest(), findings


def build(repo: Path, output: Path, report: Path) -> dict[str, object]:
    repo = repo.resolve()
    package_root = (repo / "work" / "backtest").resolve()
    output = output.resolve()
    report = report.resolve()
    files, excluded = collect(repo, package_root, output=output, report=report)
    rows, inventory_sha256, findings = _inventory(files, package_root)
    if findings:
        raise ValueError("generic sensitive scan found high-confidence material")
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    tree = subprocess.run(["git", "-C", str(repo), "rev-parse", f"{head}^{{tree}}"], check=True, capture_output=True, text=True).stdout.strip()
    branch = subprocess.run(["git", "-C", str(repo), "branch", "--show-current"], check=True, capture_output=True, text=True).stdout.strip()
    manifest = {
        "schema_version": 1,
        "package_type": "DEMO_REPOSITORY_SOURCE_HANDOFF_V1",
        "publication_status": "BLOCKED_EXTERNAL_ACCEPTANCE",
        "production_ready": False,
        "branch": branch,
        "source_commit": head,
        "source_tree_oid": tree,
        "working_tree_inventory_sha256": inventory_sha256,
        "file_count": len(rows),
        "files": rows,
        "excluded_count": len(excluded),
        "excluded_paths": excluded,
        "generic_sensitive_scan": {"status": "CLEAN", "finding_count": 0},
        "private_denylist_scan": {"status": "UNASSESSED_MISSING_EXTERNAL_DENYLIST"},
        "legacy_signed_acceptance_used": False,
        "owner_declaration_present": False,
        "history_sanitization_present": False,
        "explicit_push_approval_present": False,
        "live_risk_controls_unchanged": True,
        "manifest_integrity_controls_unchanged": True,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    report.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="demo-repository-zip-") as staging:
        staging_root = Path(staging) / "demo-repository"
        for path in files:
            target = staging_root / path.relative_to(package_root)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
        (staging_root / "DEMO_PACKAGE_MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sorted(staging_root.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(staging_root).as_posix())
    payload = {
        "schema_version": 1,
        "archive": str(output),
        "archive_sha256": sha256(output.read_bytes()).hexdigest(),
        "archive_bytes": output.stat().st_size,
        "manifest_sha256": sha256(json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n").hexdigest(),
        **manifest,
    }
    report.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=ROOT.parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        payload = build(args.repo, args.output, args.report)
    except Exception as exc:
        print(json.dumps({"status": "FAILED_SENSITIVE_SCAN", "reason_code": type(exc).__name__}, sort_keys=True))
        return 2
    print(json.dumps({"status": payload["publication_status"], "archive_sha256": payload["archive_sha256"], "file_count": payload["file_count"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
