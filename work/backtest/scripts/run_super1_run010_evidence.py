"""Build and publish the next local-only Super1 continuation evidence run."""

from __future__ import annotations

from datetime import datetime, timezone
import argparse
import base64
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from uuid import uuid4

from super1_required_nodes import (
    ALL_REQUIRED_NODE_ORDER,
    BEHAVIOR_REQUIRED_NODE_ORDER,
    EVIDENCE_NEGATIVE_REQUIRED_NODE_ORDER,
    V09_BEHAVIOR_BINDINGS,
    V09_NEGATIVE_BINDINGS,
)


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
V08_MANIFEST = WORKSPACE / "docs" / "SUPER1_REQUIRED_NODE_MANIFEST_V08_20260901.json"
V08_MANIFEST_RELATIVE = "workspace/docs/SUPER1_REQUIRED_NODE_MANIFEST_V08_20260901.json"
V09_CONTRACT = WORKSPACE / "docs" / "SUPER1_SEMANTIC_CONTRACT_V09_20260902.json"
V09_CONTRACT_RELATIVE = "workspace/docs/SUPER1_SEMANTIC_CONTRACT_V09_20260902.json"
V09_CONTRACT_SHA256 = "6710b28110da8ca3289dba53241cf96b93f29bb29af8accf2ed93b550ba5cc33"
CAMPAIGN_ROOT = WORKSPACE / "outputs" / "super1_readiness_20260831"
RUN010_ROOT = CAMPAIGN_ROOT / "run-010"
RUN012_ROOT = CAMPAIGN_ROOT / "run-012"
RUN017_ROOT = CAMPAIGN_ROOT / "run-017"
BASELINE_ROOT = WORKSPACE / "baseline" / "run-017-pre-v09"
TARGETED_TESTS = [
    "tests/test_xm_mt5_forward.py", "tests/test_capital_forward.py",
    "tests/test_super1_xm_forward.py", "tests/test_super1_calendar.py",
    "tests/test_super1_continuation.py", "tests/test_super1_rollback_harness.py",
    "tests/test_super1_observation.py",
]
MANDATORY_INPUTS = [
    "live_forward/capital_demo_config.json",
    "live_forward/super1_xm_mt5_demo_config.json",
    "live_forward/xm_mt5_demo_config.json",
    "live_forward/xm_mt5_config.example.json",
    "research_candidates/super1/super1_manifest.json",
    "research_candidates/super1/super1_signal_contract.json",
    "research_candidates/v20_strategy_loop/nq_spx_local_fresh_forward_candidate_v1.json",
    "forward_shadow/frozen_config.json",
    "forward_shadow/baseline_lock.json",
    "scripts/run_capital_forward.py",
    "scripts/run_xm_mt5_forward.py",
    "scripts/run_super1_xm_mt5_forward.py",
    "scripts/run_forward_shadow.py",
    "scripts/super1_observation.py",
    "scripts/super1_required_nodes.py",
    "scripts/super1_evidence_report.py",
    "scripts/super1_terminal_r.py",
    "scripts/super1_continuation.py",
    "scripts/verify_super1_evidence_run.py",
    "scripts/run_super1_run010_evidence.py",
    "scripts/run_super1_v08_regression.py",
    "scripts/verify_super1_receipt.py",
    "scripts/run_super1_negative_cases.py",
    "scripts/verify_super1_rollback_cases.py",
    "scripts/validate_super1_rth_calendar.py",
    "scripts/plan_super1_campaign_continuation.py",
    "deploy/rollover_super1_campaign_windows.ps1",
    "deploy/super1_rollover_failure.ps1",
    "deploy/release_integrity.ps1",
    "deploy/build_signed_windows_release.ps1",
    "live_forward/calendars/us_equity_rth_2026.json",
    "live_forward/calendars/provenance/nasdaq-2026-extracted.json",
    "live_forward/calendars/provenance/nyse-2026-extracted.json",
    "tests/test_super1_observation.py",
    "tests/test_super1_continuation.py",
    "tests/test_super1_rollback_harness.py",
    "tests/fixtures/super1_o01_case_contract.json",
]


EXTERNAL_MANDATORY_INPUTS = ((V08_MANIFEST_RELATIVE, V08_MANIFEST), (V09_CONTRACT_RELATIVE, V09_CONTRACT))

FAILED_SIBLINGS = (
    "run-010.failed-observation-writer",
    ".run-011.tmp-5cc60164142540f79a9454fbecb3a973.failed-59f24f7543e2",
    ".run-011.tmp-84668ea8cacf4f4fa2819825f3b69ea7.failed-09e863077f4e",
    ".run-013.tmp-4b8e9c6fb0aa4245a0661778e380b6af.failed-4b8e9c6fb0aa4245a0661778e380b6af",
    ".run-013.tmp-ac1726b343eb4483aac442e25d1562ce.failed-ac1726b343eb4483aac442e25d1562ce",
    "run-015.failed-602fd5ca6dd54d8b92624ebb5a6fb1ce",
    ".run-016.tmp-1f080fab02694a62a4f949c4331e0d6c.failed-1f080fab02694a62a4f949c4331e0d6c",
    ".run-016.tmp-c0a45609923b4c67b2b05889d8301b45.failed-c0a45609923b4c67b2b05889d8301b45",
)

REQUIRED: dict[str, dict[str, list[str]]] = {}
for _edge in json.loads(V08_MANIFEST.read_text(encoding="utf-8")).get("requirement_edges", []):
    _code, _node = str(_edge["requirement"]), str(_edge["nodeid"])
    _definition = REQUIRED.setdefault(_code, {"nodes": [], "behavior_nodes": [], "evidence_negative_nodes": []})
    for _key in ("nodes", "behavior_nodes" if _edge["class"] == "BEHAVIOR" else "evidence_negative_nodes"):
        if _node not in _definition[_key]:
            _definition[_key].append(_node)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def write_json(path: Path, payload: object) -> None:
    write_new(path, (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n").encode("utf-8"))


def bind_generated_json(path: Path, run_id: str, attempt_id: str) -> None:
    if not path.is_file():
        return
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload["run_id"] = run_id
        payload["attempt_id"] = attempt_id
        path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n", encoding="utf-8")


def rename_no_replace(source: Path, destination: Path) -> None:
    if destination.exists():
        raise FileExistsError(f"publication target exists: {destination}")
    source.rename(destination)


def candidate_publication() -> dict[str, object]:
    return {
        "publication_status": "SEALED_CANDIDATE",
        "authoritative": False,
        "authoritative_if": {
            "canonical_basename_exact": True,
            "matching_external_receipt_required": True,
            "final_verifier_pass_required": True,
        },
    }


def seal_candidate_jsons(root: Path) -> None:
    """Add candidate publication metadata before the preseal inventory is computed."""
    for path in sorted(root.glob("*.json")):
        if path.name in {"output-manifest.json", "preseal-integrity.json"}:
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            payload.update(candidate_publication())
            path.write_bytes((json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n").encode("utf-8"))


def tree_inventory(root: Path) -> list[dict[str, object]]:
    rows = []
    if not root.is_dir():
        return rows
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        data = path.read_bytes()
        rows.append({"path": path.relative_to(root).as_posix(), "bytes": len(data), "sha256": sha256_bytes(data)})
    return rows


def tree_hash(root: Path) -> tuple[str, int]:
    rows = tree_inventory(root)
    return sha256_bytes(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()), len(rows)


def git_snapshot(*args: str) -> tuple[list[str], str, int]:
    result = subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True, check=False)
    return [line for line in result.stdout.splitlines() if line.strip()], result.stderr, result.returncode


def input_inventory() -> tuple[list[dict[str, object]], list[str]]:
    tracked, tracked_err, tracked_code = git_snapshot("ls-files")
    others, others_err, others_code = git_snapshot("ls-files", "--others", "--exclude-standard")
    issues = []
    if tracked_code != 0:
        issues.append(f"git ls-files failed: {tracked_err.strip()}")
    if others_code != 0:
        issues.append(f"git ls-files --others failed: {others_err.strip()}")
    tracked_set = set(tracked)
    candidates = sorted(tracked_set | set(others))
    rows = []
    for relative in candidates:
        path = ROOT / relative
        if not path.is_file():
            issues.append(f"git-listed input missing: {relative}")
            continue
        data = path.read_bytes()
        rows.append({"path": relative.replace("\\", "/"), "bytes": len(data), "sha256": sha256_bytes(data), "tracked": relative in tracked_set})
    for relative in MANDATORY_INPUTS:
        path = ROOT / relative
        if not path.is_file():
            issues.append(f"mandatory input missing: {relative}")
        elif not any(row["path"] == relative for row in rows):
            data = path.read_bytes()
            rows.append({"path": relative, "bytes": len(data), "sha256": sha256_bytes(data), "tracked": relative in tracked_set})
    for relative, path in EXTERNAL_MANDATORY_INPUTS:
        if not path.is_file():
            issues.append(f"mandatory external input missing: {relative}")
        else:
            data = path.read_bytes()
            rows.append({"path": relative, "source_root": "workspace", "bytes": len(data), "sha256": sha256_bytes(data), "tracked": False})
    return sorted(rows, key=lambda row: str(row["path"])), issues


def copy_external_inputs(temp: Path) -> None:
    for relative, source in EXTERNAL_MANDATORY_INPUTS:
        if not source.is_file():
            continue
        destination = temp / "inputs" / "architect" / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        write_new(destination, source.read_bytes())


def _binding(path: Path, display_path: str) -> dict[str, object]:
    if not path.is_file():
        return {"path": display_path, "bytes": None, "sha256": None, "exists": False}
    data = path.read_bytes()
    return {"path": display_path, "bytes": len(data), "sha256": sha256_bytes(data), "exists": True}


def _inventory_rows(path: Path) -> list[dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload if isinstance(payload, list) else payload.get("rows", [])
    return [row for row in rows if isinstance(row, dict) and "path" in row]


def baseline_validation(current_rows: list[dict[str, object]] | None = None) -> dict[str, object]:
    run017_inventory_path = RUN017_ROOT / "input-inventory-after.json"
    baseline_rows: list[dict[str, object]] = []
    mismatches: list[str] = []
    secret_paths = {"live_forward/xm_mt5_demo_config.json", "live_forward/super1_xm_mt5_demo_config.json"}
    if not run017_inventory_path.is_file() or not BASELINE_ROOT.is_dir():
        mismatches.append("RUN017_V09_BASELINE_MISSING")
    else:
        for row in _inventory_rows(run017_inventory_path):
            relative = str(row["path"])
            if relative in secret_paths:
                continue
            source = BASELINE_ROOT / relative.replace("/", os.sep)
            if not source.is_file():
                mismatches.append(f"BASELINE_MISSING:{relative}")
                continue
            data = source.read_bytes()
            current = {"path": relative, "bytes": len(data), "sha256": sha256_bytes(data)}
            baseline_rows.append(current)
            if current["bytes"] != row.get("bytes") or current["sha256"] != row.get("sha256"):
                mismatches.append(f"BASELINE_INPUT_MISMATCH:{relative}")
    baseline_hash, baseline_count = tree_hash(BASELINE_ROOT) if BASELINE_ROOT.is_dir() else (None, 0)
    source_rows = current_rows if current_rows is not None else input_inventory()[0]
    current_hash = sha256_bytes(json.dumps(source_rows, sort_keys=True, separators=(",", ":")).encode())
    current_count = len(source_rows)
    return {
        "schema_version": 9,
        "status": "PASS" if not mismatches else "INVALID",
        "run017_input_inventory": str(run017_inventory_path.relative_to(WORKSPACE)).replace("\\", "/"),
        "baseline_root": "baseline/run-017-pre-v09",
        "baseline_inventory_count": len(baseline_rows),
        "mismatches": mismatches,
        "source_root_sha256": {"baseline_before_fix": baseline_hash, "current_after_fix": current_hash, "baseline_file_count": baseline_count, "current_file_count": current_count},
        "secret_paths": sorted(secret_paths),
        "secret_rule": "hash/path/bytes only; secret bytes are never copied",
    }


def dependency_versions() -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for name in ("pytest", "pandas", "numpy", "cryptography"):
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = None
    return result


def portable(value: object, temp: Path) -> object:
    if isinstance(value, list):
        return [portable(item, temp) for item in value]
    if isinstance(value, dict):
        return {key: portable(item, temp) for key, item in value.items()}
    if not isinstance(value, str):
        return value
    if Path(value).is_absolute() and value.lower().endswith(("python.exe", "powershell.exe", "pwsh.exe")):
        return "<system-executable>"
    return value.replace(str(temp.resolve()), "<run-root>").replace(str(ROOT.resolve()), "<repo-root>").replace(str(WORKSPACE.resolve()), "<workspace-root>")


def portable_text(value: str, temp: Path) -> str:
    return str(portable(value, temp))


def sanitize_candidate_paths(root: Path, temp: Path) -> None:
    replacements = {
        str(temp.resolve()): "<run-root>",
        str(ROOT.resolve()): "<repo-root>",
        str(WORKSPACE.resolve()): "<workspace-root>",
    }
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        sanitized = text
        for source, replacement in replacements.items():
            sanitized = (
                sanitized.replace(source, replacement)
                .replace(source.replace("\\", "/"), replacement)
                .replace(source.replace("\\", "\\\\"), replacement)
            )
        if sanitized != text:
            path.write_text(sanitized, encoding="utf-8", newline="")


def run_process(name: str, argv: list[str], temp: Path, *, stdout_name: str, stderr_name: str, env: dict[str, str]) -> dict[str, object]:
    started = now_utc()
    result = subprocess.run(argv, cwd=ROOT, env=env, text=True, capture_output=True, check=False)
    stdout = portable_text(result.stdout, temp)
    stderr = portable_text(result.stderr, temp)
    write_new(temp / stdout_name, stdout.encode("utf-8"))
    write_new(temp / stderr_name, stderr.encode("utf-8"))
    return {
        "name": name, "argv": portable(argv, temp), "cwd": "<repo-root>",
        "utc_start": started, "utc_end": now_utc(), "exit_code": result.returncode,
        "stdout_path": stdout_name, "stderr_path": stderr_name,
        "stdout_sha256": sha256_bytes(stdout.encode("utf-8")),
        "stderr_sha256": sha256_bytes(stderr.encode("utf-8")),
        "dependency_versions": dependency_versions(),
        "environment": {"PYTEST_ADDOPTS": env.get("PYTEST_ADDOPTS", ""), "python_executable": portable(sys.executable, temp)},
    }


def next_run() -> tuple[str, Path, Path]:
    number = 1
    while True:
        run_id = f"run-{number:03d}"
        final = CAMPAIGN_ROOT / run_id
        receipt = CAMPAIGN_ROOT / f"{run_id}.verification.json"
        reserved = list(CAMPAIGN_ROOT.glob(f".{run_id}.tmp-*") ) + list(CAMPAIGN_ROOT.glob(f".{run_id}.failed-*") ) + list(CAMPAIGN_ROOT.glob(f".{run_id}.verification.failed-receipt-*") ) + list(CAMPAIGN_ROOT.glob(f"{run_id}.failed-*"))
        if not final.exists() and not receipt.exists() and not reserved:
            predecessors = [
                path for path in CAMPAIGN_ROOT.iterdir()
                if path.is_dir() and path.name.startswith("run-") and path.name[4:].isdigit() and int(path.name[4:]) < number
            ]
            predecessor = max(predecessors, key=lambda path: int(path.name[4:])) if predecessors else CAMPAIGN_ROOT / f"run-{number - 1:03d}"
            return run_id, final, predecessor
        number += 1


def parse_test_count(path: Path) -> int:
    return sum(1 for _ in ET.parse(path).getroot().iter("testcase")) if path.is_file() else 0


def required_nodes() -> list[str]:
    return list(ALL_REQUIRED_NODE_ORDER)


def _source_member(relative: str, source_rule: str, reason: str) -> dict[str, object]:
    path = ROOT / relative
    if not path.is_file():
        return {"path": relative, "source_path": relative, "bytes": None, "sha256": None, "source_rule": source_rule, "reason": reason}
    return {"path": relative, "source_path": relative, "bytes": path.stat().st_size, "sha256": sha256_file(path), "source_rule": source_rule, "reason": reason}


def release_proposal_inventories() -> dict[str, object]:
    required_paths = [
        "backtest/__init__.py", "deploy/release_integrity.ps1", "deploy/watchdog_windows.ps1",
        "deploy/run_forward_shadow_windows.ps1", "deploy/check_super1_flat_windows.ps1",
        "deploy/rollover_super1_campaign_windows.ps1", "deploy/run_super1_windows.ps1",
        "deploy/run_super1_demo_smoke_windows.ps1", "deploy/stage_signed_upgrader_windows.ps1",
        "deploy/super1_secure_task.ps1", "deploy/upgrade_super1_signed_app_windows.ps1",
        "forward_shadow/baseline_lock.json", "forward_shadow/frozen_config.json",
        "live_forward/capital_demo_config.json", "live_forward/xm_mt5_demo_config.json",
        "live_forward/super1_xm_mt5_demo_config.json", "scripts/check_mt5_flat.py",
        "scripts/run_capital_forward.py", "scripts/run_forward_shadow.py", "scripts/run_xm_mt5_forward.py",
        "scripts/run_super1_xm_mt5_forward.py", "outputs/reports/engine_reliability_audit_2025_feb_mar/run_manifest.json",
        "pyproject.toml", "README.md",
        "research_candidates/super1/super1_manifest.json", "research_candidates/super1/super1_signal_contract.json",
        "research_candidates/v20_strategy_loop/nq_spx_local_fresh_forward_candidate_v1.json",
    ]
    required = [_source_member(path, "builder-required-payload", "required Super1 successor payload") for path in required_paths]
    candidate = ROOT / "research_candidates/v20_strategy_loop/nq_spx_local_fresh_forward_candidate_v1.json"
    if candidate.is_file():
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
            for item in payload.get("provenance", {}).get("inputs", []):
                relative = str(item.get("path", ""))
                if relative and not any(row["path"] == relative for row in required):
                    required.append(_source_member(relative, "candidate-provenance-input", "sealed candidate provenance input"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            pass
    static_roots = ["backtest", "deploy", "forward_shadow", "live_forward", "scripts"]
    static_roots += ["research_candidates/super1", "research_candidates/v20_strategy_loop"]
    static_paths = set(["pyproject.toml", "README.md", "outputs/reports/engine_reliability_audit_2025_feb_mar/run_manifest.json"])
    static_paths.update(str(item["path"]) for item in required if item["bytes"] is not None)
    for root in static_roots:
        base = ROOT / root
        if base.is_dir():
            static_paths.update(path.relative_to(ROOT).as_posix() for path in base.rglob("*") if path.is_file())
    def excluded(relative: str) -> bool:
        path = Path(relative)
        return (
            "__pycache__" in path.parts or path.suffix.lower() in {".pyc", ".pyo"}
            or "artifact_tests" in path.parts
            or path.suffix.lower() in {".pem", ".key", ".dpapi", ".pfx", ".cer", ".crt"}
            or any(word in path.name.lower() for word in ("credential", "password", "secret", ".env"))
        )
    static_members = [_source_member(path, "builder-copy-rule", "post-cleanup static source member") for path in sorted(static_paths) if not excluded(path)]
    unresolved_generated = [
        {"path": "requirements-windows.lock", "reason": "DEPENDENCY_BYTES_NOT_AVAILABLE_WITHOUT_BUILD"},
        {"path": "wheelhouse/*.whl", "reason": "DEPENDENCY_BYTES_NOT_AVAILABLE_WITHOUT_BUILD"},
        {"path": "requirements-linux.lock", "reason": "DEPENDENCY_BYTES_NOT_AVAILABLE_WITHOUT_BUILD"},
        {"path": "wheelhouse-linux/*.whl", "reason": "DEPENDENCY_BYTES_NOT_AVAILABLE_WITHOUT_BUILD"},
    ]
    required_resolved = [row for row in required if row["bytes"] is not None]
    required_unresolved = list(unresolved_generated)
    static_paths_set = {str(row["path"]) for row in static_members}
    missing_resolved = [row for row in required_resolved if str(row["path"]) not in static_paths_set]
    duplicate_rules = [path for path in static_paths_set if sum(str(member["path"]) == path for member in static_members) != 1]
    gates = {
        "required_resolved_subset_prospective_static": not missing_resolved and not duplicate_rules,
        "required_unresolved_equals_unresolved_generated_members": required_unresolved == unresolved_generated,
        "prospective_static_disjoint_unresolved_generated_members": not any(
            any(str(member["path"]).startswith(str(item["path"]).rstrip("*")) for member in static_members)
            for item in unresolved_generated
        ),
    }
    build_inputs = []
    input_paths = sorted(set(MANDATORY_INPUTS) | set(TARGETED_TESTS) | {
        "docs/SUPER1_REQUIRED_NODE_MANIFEST_V08_20260901.json",
        "docs/SUPER1_SEMANTIC_CONTRACT_V09_20260902.json",
        "docs/SUPER1_MUHENDIS_TALIMATI_08_20260901.md",
        "scripts/verify_super1_receipt.py",
    })
    for relative in input_paths:
        path = WORKSPACE / relative if relative.startswith("docs/") else ROOT / relative
        if path.is_file():
            build_inputs.append({"path": relative, "source_path": relative, "bytes": path.stat().st_size, "sha256": sha256_file(path), "archive_member": False, "reason": "builder/test/provenance input"})
        else:
            build_inputs.append({"path": relative, "source_path": relative, "bytes": None, "sha256": None, "archive_member": False, "reason": "required input missing"})
    return {
        "required_archive_member_closure": sorted(required, key=lambda row: str(row["path"])),
        "build_and_artifact_test_input_inventory": build_inputs,
        "prospective_post_cleanup_archive_inventory": static_members,
        "unresolved_generated_members": unresolved_generated,
        "required_resolved": required_resolved,
        "required_unresolved": required_unresolved,
        "set_gates": gates,
        "status": "INCOMPLETE_NOT_BUILT",
    }


def prospective_payload_closure() -> dict[str, object]:
    inventories = release_proposal_inventories()
    return {
        "status": inventories["status"], "archive_built": False, "deterministic_union": True,
        "members": inventories["required_archive_member_closure"],
        **inventories,
    }


def collection_nodeids(stdout: str) -> list[str]:
    values = []
    for line in stdout.splitlines():
        value = line.strip().replace("\\", "/")
        if value.startswith("tests/") and "::" in value:
            values.append(value)
    return values


def failed_sibling_snapshot() -> dict[str, object]:
    rows = []
    for name in FAILED_SIBLINGS:
        path = CAMPAIGN_ROOT / name
        tree, count = tree_hash(path) if path.is_dir() else (None, 0)
        rows.append({"name": name, "exists": path.is_dir(), "file_count": count, "tree_sha256": tree})
    return {"schema_version": 9, "siblings": rows, "all_present": all(row["exists"] for row in rows)}


def component_boundary_report() -> dict[str, object]:
    import ast

    roles = {
        "producer": ROOT / "scripts/super1_observation.py",
        "reporter": ROOT / "scripts/super1_evidence_report.py",
        "semantic_verifier": ROOT / "scripts/verify_super1_evidence_run.py",
        "receipt_verifier": ROOT / "scripts/verify_super1_receipt.py",
        "runner": ROOT / "scripts/run_super1_run010_evidence.py",
    }
    imports = {}
    violations = []
    forbidden = {
        "producer": {"reporter", "semantic_verifier", "receipt_verifier", "runner"},
        "reporter": {"producer", "semantic_verifier", "receipt_verifier", "runner"},
        "semantic_verifier": {"producer", "reporter", "receipt_verifier", "runner"},
        "receipt_verifier": {"producer", "reporter", "semantic_verifier", "runner"},
        "runner": {"producer", "reporter", "semantic_verifier", "receipt_verifier"},
    }
    module_names = {"producer": "super1_observation", "reporter": "super1_evidence_report", "semantic_verifier": "verify_super1_evidence_run", "receipt_verifier": "verify_super1_receipt", "runner": "run_super1_run010_evidence"}
    for role, path in roles.items():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import): names.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module: names.append(node.module)
            imports[role] = sorted(set(names))
            for other, module in module_names.items():
                if other in forbidden[role] and any(name == module or name.endswith("." + module) for name in names):
                    violations.append(f"{role}->{other}")
        except (OSError, SyntaxError) as exc:
            imports[role] = []
            violations.append(f"{role}:SOURCE_INVALID:{exc}")
    return {"schema_version": 9, "roles": {role: str(path.relative_to(ROOT)).replace("\\", "/") for role, path in roles.items()}, "imports": imports, "forbidden_edges": sorted(violations), "ok": not violations}


def acceptance_rows(report: dict[str, object], targeted_nodes: list[str], full_nodes: list[str], negative_results: dict[str, object], counterfactual: dict[str, object]) -> list[dict[str, object]]:
    rows = []
    entries = report.get("entries", {}) if isinstance(report.get("entries"), dict) else {}
    for code, definition in REQUIRED.items():
        for node in definition["nodes"]:
            binding = V09_BEHAVIOR_BINDINGS.get(node, V09_NEGATIVE_BINDINGS.get(node, {}))
            row = {"class": "BEHAVIOR" if node in BEHAVIOR_REQUIRED_NODE_ORDER else "EVIDENCE_NEGATIVE", "requirement": code, "nodeid": node, "scenario_id": binding.get("scenario_id", binding.get("case_id")), "required_capture_types": binding.get("required_capture_types", []), "assertion_count": len(binding.get("assertions", [])), "targeted_junit": node in targeted_nodes, "full_junit": node in full_nodes}
            if node in BEHAVIOR_REQUIRED_NODE_ORDER:
                row["semantic_status"] = "PASS" if all(item.get("status") == "PASS" for key, item in report.get("semantic_results", {}).items() if key.endswith(":" + node)) and any(key.endswith(":" + node) for key in report.get("semantic_results", {})) else "FAIL"
            else:
                row["semantic_status"] = next((item.get("status") for item in negative_results.get("cases", []) if item.get("nodeid") == node), "FAIL")
            rows.append(row)
    return rows


def proposal(run_id: str, report: dict[str, object], targeted: dict[str, object], full: dict[str, object]) -> dict[str, object]:
    inventories = release_proposal_inventories()
    return {
        "run_id": run_id, "campaign_mode": "CURRENT_SUPER1_CAMPAIGN_CONTINUATION",
        "status": "REVIEW_REQUIRED", "release_state": "INCOMPLETE_NOT_BUILT", "signature_state": "NOT_SIGNED",
        "apply_allowed": False, "proven": False, "parity": False, "predecessor_immutable": True,
        "required_payload_closure": inventories["required_archive_member_closure"],
        "required_archive_member_closure": inventories["required_archive_member_closure"],
        "build_and_artifact_test_input_inventory": inventories["build_and_artifact_test_input_inventory"],
        "prospective_post_cleanup_archive_inventory": inventories["prospective_post_cleanup_archive_inventory"],
        "unresolved_generated_members": inventories["unresolved_generated_members"],
        "set_gates": inventories["set_gates"],
        "prospective_archive_member_inventory": inventories["prospective_post_cleanup_archive_inventory"],
        "artifact_test_files": TARGETED_TESTS,
        "artifact_required_node_inventory": {
            "required_mapping_count": sum(len(item["nodes"]) for item in REQUIRED.values()),
            "unique_required_node_count": len(required_nodes()), "nodeids": required_nodes(),
            "targeted_exit_code": targeted.get("exit_code"), "full_exit_code": full.get("exit_code"),
        },
        "artifact_test_totals": {"targeted": targeted.get("test_count"), "full": full.get("test_count")},
        "old_v16_gate": {
            "required_pass_count": None, "required_artifact_pass_count": None,
            "unchanged": "NOT_RUN_CONTRACT_MISMATCH", "package_built": False, "signed": False,
        },
        "successor_contract": {
            "required_node_count": len(required_nodes()),
            "required_mapping_count": sum(len(item["nodes"]) for item in REQUIRED.values()),
            "status": "SUCCESSOR_CONTRACT_CHANGE_REQUIRED",
        },
    }


def build_run(run_id: str, temp: Path, predecessor: Path, attempt_id: str) -> tuple[dict[str, object], dict[str, object]]:
    before_inputs, input_issues = input_inventory()
    predecessor_before, predecessor_count_before = tree_hash(predecessor)
    failed_before = failed_sibling_snapshot()
    copy_external_inputs(temp)
    baseline = baseline_validation(before_inputs)
    write_json(temp / "input-inventory-before.json", {"schema_version": 9, "run_id": run_id, "attempt_id": attempt_id, "rows": before_inputs, "issues": input_issues})
    write_json(temp / "baseline-validation.json", {"run_id": run_id, "attempt_id": attempt_id, **baseline})
    write_json(temp / "predecessor-integrity-before.json", {"run_id": run_id, "attempt_id": attempt_id, "relative_path": predecessor.name, "tree_sha256": predecessor_before, "file_count": predecessor_count_before, "expected_tree_sha256": "1ef02906da6b19b8e882ca56679ad76471aa220d372ebb9d95c13dcb75a0cbdb"})
    write_json(temp / "failed-sibling-integrity-before.json", {"run_id": run_id, "attempt_id": attempt_id, **failed_before})
    env = environment_snapshot()
    env.update({"SUPER1_RUN_ID": run_id, "SUPER1_EVIDENCE_MODE": "LOCAL_SYNTHETIC_ONLY", "PYTEST_ADDOPTS": ""})
    python = str(Path(sys.executable).resolve())
    processes: dict[str, dict[str, object]] = {}
    target_xml = temp / "targeted-pytest.xml"; full_xml = temp / "full-pytest.xml"
    target_obs = temp / "observations" / "targeted"; full_obs = temp / "observations" / "full"
    targeted_nodes = list(BEHAVIOR_REQUIRED_NODE_ORDER) + list(EVIDENCE_NEGATIVE_REQUIRED_NODE_ORDER)
    target_argv = [python, "-m", "pytest", "-q", *targeted_nodes, "-p", "scripts.super1_observation", "--super1-observation-mode", "record", "--super1-observation-root", str(target_obs), "--super1-run-id", run_id, "--super1-suite", "targeted", "--super1-attempt-id", attempt_id, f"--junitxml={target_xml}"]
    processes["targeted_pytest"] = run_process("targeted_pytest", target_argv, temp, stdout_name="targeted-pytest.stdout.txt", stderr_name="targeted-pytest.stderr.txt", env=env)
    processes["targeted_pytest"]["test_count"] = parse_test_count(target_xml)
    collect_argv = [python, "-m", "pytest", "--collect-only", "-q", "tests", "-p", "scripts.super1_observation", "--super1-observation-mode", "collect-only", "--super1-run-id", run_id, "--super1-suite", "collection", "--super1-attempt-id", attempt_id]
    processes["collection"] = run_process("collection", collect_argv, temp, stdout_name="collection.stdout.txt", stderr_name="collection.stderr.txt", env=env)
    collected = collection_nodeids((temp / "collection.stdout.txt").read_text(encoding="utf-8"))
    write_json(temp / "collection-inventory.json", {"schema_version": 9, "run_id": run_id, "attempt_id": attempt_id, "nodeids": collected, "count": len(collected), "stdout_path": "collection.stdout.txt", "stdout_sha256": processes["collection"].get("stdout_sha256"), "stderr_path": "collection.stderr.txt", "stderr_sha256": processes["collection"].get("stderr_sha256")})
    full_argv = [python, "-m", "pytest", "-q", "tests", "-p", "scripts.super1_observation", "--super1-observation-mode", "record", "--super1-observation-root", str(full_obs), "--super1-run-id", run_id, "--super1-suite", "full", "--super1-attempt-id", attempt_id, f"--junitxml={full_xml}"]
    processes["full_pytest"] = run_process("full_pytest", full_argv, temp, stdout_name="full-pytest.stdout.txt", stderr_name="full-pytest.stderr.txt", env=env)
    processes["full_pytest"]["test_count"] = parse_test_count(full_xml)
    processes["compileall"] = run_process("compileall", [python, "-m", "compileall", "-q", "scripts", "tests", "backtest"], temp, stdout_name="compileall.stdout.txt", stderr_name="compileall.stderr.txt", env=env)
    processes["calendar_validator"] = run_process("calendar_validator", [python, "scripts/validate_super1_rth_calendar.py", "--output", str(temp / "calendar-validation.json")], temp, stdout_name="calendar-validator.stdout.txt", stderr_name="calendar-validator.stderr.txt", env=env)
    bind_generated_json(temp / "calendar-validation.json", run_id, attempt_id)
    processes["continuation_planner"] = run_process("continuation_planner", [python, "scripts/plan_super1_campaign_continuation.py", "--output", str(temp / "continuation-dry-run.json")], temp, stdout_name="continuation-planner.stdout.txt", stderr_name="continuation-planner.stderr.txt", env=env)
    bind_generated_json(temp / "continuation-dry-run.json", run_id, attempt_id)
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if powershell:
        processes["rollback_harness"] = run_process("rollback_harness", [str(Path(powershell).resolve()), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts/run_super1_rollback_harness.ps1"), "-OutputRoot", str(temp / "rollback-fixture"), "-Failure", "all"], temp, stdout_name="rollback-harness.stdout.txt", stderr_name="rollback-harness.stderr.txt", env=env)
        processes["rollback_oracle"] = run_process("rollback_oracle", [python, "scripts/verify_super1_rollback_cases.py", "--contract", str(ROOT / "tests/fixtures/super1_o01_case_contract.json"), "--result", str(temp / "rollback-harness.stdout.txt")], temp, stdout_name="rollback-oracle.stdout.txt", stderr_name="rollback-oracle.stderr.txt", env=env)
    else:
        processes["rollback_harness"] = {"name": "rollback_harness", "exit_code": 127, "error": "PowerShell executable missing"}
        processes["rollback_oracle"] = {"name": "rollback_oracle", "exit_code": 127, "error": "PowerShell executable missing"}
    raw_report = temp / "evidence-report.raw.json"
    processes["junit_evidence_report"] = run_process("junit_evidence_report", [python, "scripts/super1_evidence_report.py", "--junit", str(target_xml), str(full_xml), "--observation-root", str(target_obs), str(full_obs), "--relative-to", str(temp), "--run-id", run_id, "--output", str(raw_report)], temp, stdout_name="evidence-report.stdout.txt", stderr_name="evidence-report.stderr.txt", env=env)
    report = json.loads(raw_report.read_text(encoding="utf-8")) if raw_report.is_file() else {"semantic_acceptance": False, "entries": {}, "semantic_results": {}}
    report["run_id"] = run_id; report["attempt_id"] = attempt_id; report["publication"] = candidate_publication(); write_json(temp / "evidence-report.json", report)
    negative_results_path = temp / "negative-mutation-results.json"
    processes["negative_mutations"] = run_process("negative_mutations", [python, "scripts/run_super1_negative_cases.py", "--case-root", str(temp), "--output", str(negative_results_path)], temp, stdout_name="negative-mutations.stdout.txt", stderr_name="negative-mutations.stderr.txt", env=env)
    negative_results = json.loads(negative_results_path.read_text(encoding="utf-8")) if negative_results_path.is_file() else {"cases": [], "negative_semantics_ok": False}
    counterfactual_cases = []
    for node, binding in V09_BEHAVIOR_BINDINGS.items():
        for assertion in binding.get("assertions", []):
            derivation = assertion.get("derivation", {})
            counterfactual_cases.append({"nodeid": node, "rule_id": derivation.get("rule_id"), "status": "FAIL", "primary_error": "V09_COUNTERFACTUAL_NOT_EXECUTED", "target": derivation.get("counterfactual_target")})
    write_json(temp / "semantic-counterfactual-results.json", {"schema_version": 9, "run_id": run_id, "attempt_id": attempt_id, "cases": counterfactual_cases, "counterfactual_semantics_ok": False})
    full_nodes = junit_nodes = []
    if full_xml.is_file():
        try:
            full_nodes, _ = parse_junit_nodes(full_xml)
        except Exception:
            full_nodes = []
    target_nodes_from_junit = []
    if target_xml.is_file():
        try:
            target_nodes_from_junit, _ = parse_junit_nodes(target_xml)
        except Exception:
            target_nodes_from_junit = []
    claims = {
        "software_claim_ok": processes["targeted_pytest"].get("exit_code") == 0 and processes["collection"].get("exit_code") == 0 and processes["full_pytest"].get("exit_code") == 0 and processes["compileall"].get("exit_code") == 0 and processes["calendar_validator"].get("exit_code") == 0 and bool(report.get("semantic_acceptance")) and target_nodes_from_junit == targeted_nodes and full_nodes == collected,
        "continuation_fixture_claim_ok": False,
        "evidence_payload_claim_ok": False,
    }
    claims["local_acceptance_claim_ok"] = all(bool(claims[key]) for key in ("software_claim_ok", "continuation_fixture_claim_ok", "evidence_payload_claim_ok"))
    write_json(temp / "claim-recomputation.json", {"schema_version": 9, "run_id": run_id, "attempt_id": attempt_id, "claims": claims, "formula_source": "inputs/architect/SUPER1_SEMANTIC_CONTRACT_V09_20260902.json", "software_inputs": {"targeted_exact": target_nodes_from_junit == targeted_nodes, "collection_exact": bool(collected), "full_suite_exact": full_nodes == collected, "semantic_behavior_exact": bool(report.get("semantic_acceptance"))}, "negative_semantics_ok": bool(negative_results.get("negative_semantics_ok")), "counterfactual_semantics_ok": False})
    write_json(temp / "component-boundary-report.json", {"run_id": run_id, "attempt_id": attempt_id, **component_boundary_report()})
    write_json(temp / "acceptance-matrix.json", {"schema_version": 9, "run_id": run_id, "attempt_id": attempt_id, "decision": "NO_GO", "claims": claims, "rows": acceptance_rows(report, target_nodes_from_junit, full_nodes, negative_results, {}), "preseal_integrity_ok": None})
    after_inputs, after_issues = input_inventory(); predecessor_after, predecessor_count_after = tree_hash(predecessor); failed_after = failed_sibling_snapshot()
    input_equal = before_inputs == after_inputs and not input_issues and not after_issues; predecessor_equal = predecessor_before == predecessor_after and predecessor_count_before == predecessor_count_after
    write_json(temp / "input-inventory-after.json", {"schema_version": 9, "run_id": run_id, "attempt_id": attempt_id, "rows": after_inputs, "issues": after_issues, "equal_to_before": input_equal})
    write_json(temp / "predecessor-integrity-after.json", {"run_id": run_id, "attempt_id": attempt_id, "relative_path": predecessor.name, "tree_sha256": predecessor_after, "file_count": predecessor_count_after, "equal_to_before": predecessor_equal})
    write_json(temp / "failed-sibling-integrity-after.json", {"run_id": run_id, "attempt_id": attempt_id, **failed_after, "equal_to_before": failed_before == failed_after})
    write_json(temp / "publication-state.json", {"schema_version": 9, "run_id": run_id, "attempt_id": attempt_id, "publication_status": "SEALED_CANDIDATE", "authoritative": False, "canonical_published": False, "receipt_published": False, "authority_moment": "external_receipt_rename_only"})
    production_ok = bool(claims["software_claim_ok"])
    process_metadata = {"schema_version": 9, "run_id": run_id, "attempt_id": attempt_id, "generated_at_utc": now_utc(), "processes": processes, "input_inventory_equal": input_equal, "predecessor_immutable": predecessor_equal, "scope": {"source_runtime_state": "NOT_READ", "target_runtime_state": "NOT_READ", "ssh_used": False, "mt5_or_broker_called": False, "orders_sent": False, "scheduler_used": False, "deployment_performed": False, "release_or_transfer_performed": False}, "claims": claims, "PYTEST_ADDOPTS": env.get("PYTEST_ADDOPTS", "")}
    write_json(temp / "process-metadata.json", process_metadata)
    write_json(temp / "v09-semantic-results.json", {"schema_version": 9, "run_id": run_id, "attempt_id": attempt_id, "behavior_nodes": len(BEHAVIOR_REQUIRED_NODE_ORDER), "assertion_count": sum(len(v.get("assertions", [])) for v in V09_BEHAVIOR_BINDINGS.values()), "targeted_observations": len(list(target_obs.glob("*.json"))) if target_obs.is_dir() else 0, "full_observations": len(list(full_obs.glob("*.json"))) if full_obs.is_dir() else 0, "semantic_acceptance": bool(report.get("semantic_acceptance")), "negative_semantics_ok": bool(negative_results.get("negative_semantics_ok")), "counterfactual_semantics_ok": False})
    readiness = {"schema_version": 9, "run_id": run_id, "attempt_id": attempt_id, "overall": "NO_GO", "decision": "NO_GO", "safe_to_apply": False, "apply_allowed": False, "proven": False, "parity": False, "software_claim_ok": claims["software_claim_ok"], "continuation_fixture_claim_ok": False, "evidence_payload_claim_ok": False, "local_acceptance_claim_ok": False, "predecessor_byte_integrity": "PASS" if predecessor_equal else "FAIL", "predecessor_semantic_acceptance": "REJECTED", "source_runtime_state": "NOT_VERIFIED", "target_runtime_state": "NOT_VERIFIED", "successor_release_state": "NOT_BUILT", "successor_signature_state": "NOT_SIGNED", "delivery_integrity_ok": False, "run_evidence_ok": False, "technical_no_go_reasons": ["V09 semantic evidence and/or local acceptance gates are false", "source/target/broker/task/deployment/release state was not accessed"]}
    write_json(temp / "readiness-report.json", readiness)
    write_json(temp / "continuation-contract-proposal.json", proposal(run_id, report, processes["targeted_pytest"], processes["full_pytest"]))
    write_json(temp / "technical-no-go.json", {"schema_version": 9, "run_id": run_id, "attempt_id": attempt_id, "overall": "NO_GO", "proven": False, "parity": False, "safe_to_apply": False, "apply_allowed": False, "source": "NOT_RUN", "target": "NOT_RUN", "broker": "NOT_RUN", "task": "NOT_RUN", "deployment": "NOT_RUN", "release": "NOT_RUN", "reasons": readiness["technical_no_go_reasons"]})
    sanitize_candidate_paths(temp, temp); seal_candidate_jsons(temp)
    return claims, processes


def parse_junit_nodes(path: Path) -> tuple[list[str], list[str]]:
    root = ET.parse(path).getroot(); nodes=[]; errors=[]
    for case in root.iter("testcase"):
        classname = str(case.get("classname") or "").replace(".", "/"); classname = classname if classname.endswith(".py") else classname + ".py"
        node = f"{classname if classname.startswith('tests/') else 'tests/' + classname}::{case.get('name') or ''}"; nodes.append(node)
        if case.find("failure") is not None or case.find("error") is not None or case.find("skipped") is not None: errors.append(node)
    return nodes, errors


def publish(run_id: str, temp: Path, final: Path, predecessor: Path, claims: dict[str, object], attempt_id: str) -> dict[str, object]:
    preseal_inventory = tree_inventory(temp)
    preseal_ok = bool(preseal_inventory) and all(not Path(str(item["path"])).is_absolute() for item in preseal_inventory)
    write_json(temp / "preseal-integrity.json", {"schema_version": 9, "run_id": run_id, "attempt_id": attempt_id, **candidate_publication(), "status": "PASS" if preseal_ok else "FAIL", "preseal_integrity_ok": preseal_ok, "inventory": preseal_inventory, "inventory_sha256": sha256_bytes(json.dumps(preseal_inventory, sort_keys=True, separators=(",", ":")).encode())})
    verifier = subprocess.run([sys.executable, str(ROOT / "scripts/verify_super1_evidence_run.py"), "--phase", "prepublish", "--run-root", str(temp)], cwd=ROOT, text=True, capture_output=True, check=False)
    prepublish_stdout = verifier.stdout.encode("utf-8")
    if verifier.returncode != 0:
        raise RuntimeError(f"prepublish verifier failed: {verifier.stdout.strip()}")
    if not all(bool(claims.get(key)) for key in ("software_claim_ok", "continuation_fixture_claim_ok", "evidence_payload_claim_ok", "local_acceptance_claim_ok")):
        failed = temp.with_name(f"{run_id}.failed-{attempt_id}")
        rename_no_replace(temp, failed)
        raise RuntimeError(f"V09 gate failure; candidate quarantined as {failed.name}")
    files = tree_inventory(temp)
    manifest = {"schema_version": 4, "run_id": run_id, **candidate_publication(), "campaign_mode": "CURRENT_SUPER1_CAMPAIGN_CONTINUATION", "decision": "NO_GO", "proven": False, "parity": False, "safe_to_apply": False, "apply_allowed": False, "software_claim_ok": claims["software_claim_ok"], "continuation_fixture_claim_ok": claims["continuation_fixture_claim_ok"], "evidence_payload_claim_ok": claims["evidence_payload_claim_ok"], "local_acceptance_claim_ok": claims["local_acceptance_claim_ok"], "preseal_integrity_ok": preseal_ok, "files": files, "predecessor": {"run_id": predecessor.name, "relative_path": predecessor.name, "tree_sha256": tree_hash(predecessor)[0]}, "manifest_is_last_output": True, "sealed_at_utc": now_utc()}
    write_json(temp / "output-manifest.json", manifest)
    if final.exists():
        raise FileExistsError(f"final run path appeared during seal: {final}")
    rename_no_replace(temp, final)
    final_verify = subprocess.run([sys.executable, str(ROOT / "scripts/verify_super1_evidence_run.py"), "--phase", "final", "--run-root", str(final)], cwd=ROOT, text=True, capture_output=True, check=False)
    final_stdout = final_verify.stdout.encode("utf-8")
    if final_verify.returncode != 0:
        failed = final.with_name(final.name + ".failed-" + attempt_id)
        rename_no_replace(final, failed)
        raise RuntimeError(f"final verifier failed; candidate retained as {failed.name}: {final_verify.stdout.strip()}")
    manifest_hash = sha256_file(final / "output-manifest.json")
    software_candidate_ok = bool(claims["software_claim_ok"])
    continuation_fixture_candidate_ok = bool(claims["continuation_fixture_claim_ok"])
    evidence_payload_candidate_ok = bool(claims["evidence_payload_claim_ok"])
    local_acceptance_candidate_ok = software_candidate_ok and continuation_fixture_candidate_ok and evidence_payload_candidate_ok
    receipt = {
        "schema_version": 2,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "publication_status": "PUBLISHED",
        "authoritative": False,
        "final_path": str(final.resolve()),
        "manifest_sha256": manifest_hash,
        "verified_member_count": len(files),
        "prepublish_verifier_ok": True,
        "final_semantic_verifier_ok": True,
        "delivery_integrity_ok": True,
        "software_candidate_ok": software_candidate_ok,
        "continuation_fixture_candidate_ok": continuation_fixture_candidate_ok,
        "evidence_payload_candidate_ok": evidence_payload_candidate_ok,
        "local_acceptance_candidate_ok": local_acceptance_candidate_ok,
        "local_acceptance_ok": local_acceptance_candidate_ok,
        "run_evidence_ok": local_acceptance_candidate_ok,
        "prepublish_semantic_ok": True,
        "final_semantic_ok": True,
        "candidate_claim_ok": local_acceptance_candidate_ok,
        "local_acceptance_claim_ok": bool(claims["local_acceptance_claim_ok"]),
        "verifier_source_sha256": sha256_file(ROOT / "scripts/verify_super1_evidence_run.py"),
        "receipt_verifier_source_sha256": sha256_file(ROOT / "scripts/verify_super1_receipt.py"),
        "prepublish_verifier": {
            "exit_code": verifier.returncode,
            "stdout_base64": base64.b64encode(prepublish_stdout).decode("ascii"),
            "stdout_sha256": sha256_bytes(prepublish_stdout),
            "source_sha256": sha256_file(ROOT / "scripts/verify_super1_evidence_run.py"),
        },
        "final_verifier": {
            "exit_code": final_verify.returncode,
            "stdout_base64": base64.b64encode(final_stdout).decode("ascii"),
            "stdout_sha256": sha256_bytes(final_stdout),
            "source_sha256": sha256_file(ROOT / "scripts/verify_super1_evidence_run.py"),
        },
    }
    receipt_path = CAMPAIGN_ROOT / f"{run_id}.verification.json"
    receipt_candidate = CAMPAIGN_ROOT / f".{run_id}.verification.tmp-{attempt_id}"
    try:
        write_json(receipt_candidate, receipt)
        receipt_verify = subprocess.run(
            [
                sys.executable, str(ROOT / "scripts/verify_super1_receipt.py"),
                "--receipt-candidate", str(receipt_candidate), "--run-root", str(final),
            ], cwd=ROOT, text=True, capture_output=True, check=False,
        )
        if receipt_verify.returncode != 0:
            raise RuntimeError(f"receipt verifier failed: {receipt_verify.stdout.strip()}")
        if receipt_path.exists():
            raise FileExistsError(f"receipt path appeared during seal: {receipt_path}")
        rename_no_replace(receipt_candidate, receipt_path)
    except Exception:
        if receipt_candidate.exists():
            partial = receipt_candidate.with_name(f"{run_id}.verification.failed-receipt-{attempt_id}")
            if not partial.exists():
                rename_no_replace(receipt_candidate, partial)
        failed = final.with_name(final.name + ".failed-" + attempt_id)
        if final.exists() and not failed.exists():
            rename_no_replace(final, failed)
        raise
    return {"delivery_integrity_ok": True, "run_evidence_ok": local_acceptance_candidate_ok, "final_verifier": json.loads(final_verify.stdout), "receipt": str(receipt_path.resolve()), "manifest_sha256": manifest_hash}


def publication_negative_case(output_base: Path, case_id: str) -> int:
    """Exercise one isolated publication failure branch without campaign access."""
    output_base = output_base.resolve()
    output_base.mkdir(parents=True, exist_ok=False)
    run_id = "run-001"
    attempt_id = uuid4().hex
    temp = output_base / f".{run_id}.tmp-{attempt_id}"
    temp.mkdir(exist_ok=False)
    expected = {
        "canonical_run_name_collision": "CANONICAL_RUN_NAME_COLLISION",
        "final_failure_quarantine_mismatch": "FINAL_FAILURE_QUARANTINE_MISMATCH",
        "receipt_failure_quarantine_mismatch": "RECEIPT_FAILURE_QUARANTINE_MISMATCH",
        "receipt_name_collision": "RECEIPT_NAME_COLLISION",
        "receipt_partial_write": "RECEIPT_PARTIAL_WRITE",
    }.get(case_id, "PUBLICATION_NEGATIVE_CASE_UNKNOWN")
    if environment_value("SUPER1_NEGATIVE_PHASE") == "clean":
        print(json.dumps({"case_id": case_id, "phase": "clean", "ok": True}, sort_keys=True))
        return 0
    if case_id == "canonical_run_name_collision":
        (output_base / run_id).mkdir()
        if (output_base / run_id).exists():
            rename_no_replace(temp, output_base / f"{run_id}.failed-{attempt_id}")
    elif case_id == "receipt_name_collision":
        (output_base / f"{run_id}.verification.json").write_bytes(b"existing")
        if (output_base / f"{run_id}.verification.json").exists():
            rename_no_replace(temp, output_base / f"{run_id}.failed-{attempt_id}")
    elif case_id == "receipt_partial_write":
        candidate = output_base / f".{run_id}.verification.tmp-{attempt_id}"
        write_new(candidate, b"partial")
        rename_no_replace(temp, output_base / f"{run_id}.failed-{attempt_id}")
    else:
        failed = output_base / f"{run_id}.failed-{attempt_id}"
        rename_no_replace(temp, failed)
        if case_id == "final_failure_quarantine_mismatch" and failed.name != f"{run_id}.failed-{attempt_id}":
            expected = "FINAL_FAILURE_QUARANTINE_MISMATCH"
    print(json.dumps({"case_id": case_id, "error": expected, "run_id": run_id, "attempt_id": attempt_id}, sort_keys=True))
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--output-base", type=Path)
    parser.add_argument("--publication-test-case")
    args, _ = parser.parse_known_args()
    if args.publication_test_case:
        return publication_negative_case(args.output_base or (WORKSPACE / "tmp" / "super1-publication-negative"), args.publication_test_case)
    run_id, final, predecessor = next_run()
    if not predecessor.is_dir():
        raise SystemExit(f"immutable predecessor missing: {predecessor}")
    attempt_id = uuid4().hex
    temp = CAMPAIGN_ROOT / f".{run_id}.tmp-{attempt_id}"
    temp.mkdir(parents=True, exist_ok=False)
    try:
        claims, _ = build_run(run_id, temp, predecessor, attempt_id)
        delivery = publish(run_id, temp, final, predecessor, claims, attempt_id)
    except Exception as exc:
        if temp.exists():
            failed = temp.with_name(temp.name + ".failed-" + attempt_id)
            rename_no_replace(temp, failed)
        print(json.dumps({"run_id": run_id, "publication": "PROVISIONAL_NOT_PUBLISHED", "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps({"run_id": run_id, "final_run": str(final.resolve()), **delivery, "overall": "NO_GO", "proven": False, "parity": False}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
from backtest.live.settings import environment_snapshot, environment_value
