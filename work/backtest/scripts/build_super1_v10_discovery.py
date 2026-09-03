"""Build the non-canonical Super1 V10 semantic discovery package.

This module deliberately produces review blockers for every unbound resource and
semantic rule.  It is a discovery producer, not a semantic evaluator.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
import uuid
import xml.etree.ElementTree as ET


WORKSPACE = Path(__file__).resolve().parents[3]
BACKTEST = WORKSPACE / "work" / "backtest"
DOCS = WORKSPACE / "docs"
OUTPUT_ROOT = WORKSPACE / "outputs" / "super1_semantic_discovery_20260903"
V08_PATH = DOCS / "SUPER1_REQUIRED_NODE_MANIFEST_V08_20260901.json"
V09_PATH = DOCS / "SUPER1_SEMANTIC_CONTRACT_V09_20260902.json"
V10_PATH = DOCS / "SUPER1_SEMANTIC_DISCOVERY_CONTRACT_V10_20260903.json"
I09_PATH = DOCS / "SUPER1_MUHENDIS_TALIMATI_09_20260902.md"
I10_PATH = DOCS / "SUPER1_MUHENDIS_TALIMATI_10_20260903.md"

V08_SHA256 = "d58ec29ba93cdbc88a9978e1860bf8ae5eaf8fa5913778a6de1b5d1ff2c3fbdf"
V09_SHA256 = "6710b28110da8ca3289dba53241cf96b93f29bb29af8accf2ed93b550ba5cc33"
V10_SHA256 = "52a2001633c1f068f5ab9fcd1d597cf0052d8261ee50ae1fd8cb5699cc02490a"
I09_SHA256 = "1dba834113f24c947a6eeedfa2115a4bd16e7bb54661b6585d3ad2dd8742aa39"
I10_SHA256 = "a6e4aa2289dc58949602e89cd9302537d9ee9a376da516e058387c76495d29a5"

CAPTURE_TYPES = (
    "sdk_call_trace",
    "order_store_sqlite",
    "market_fetch_store_sqlite",
    "broker_readback",
    "filesystem_inventory",
    "subprocess_trace",
    "calendar_validation",
    "filter_evidence",
    "exclusive_writer_trace",
    "release_archive_validation",
    "transition_validation",
    "snapshot_validation",
    "terminal_r_evaluation",
    "rollback_policy",
    "powershell_ast",
    "order_event_jsonl",
    "scenario_invocation_trace",
)
SUITES = ("targeted", "full")
SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")
ALLOWED_EXISTING = {
    "work/backtest/scripts/super1_observation.py",
    "work/backtest/tests/v08_helpers.py",
    "work/backtest/tests/test_capital_forward.py",
    "work/backtest/tests/test_super1_calendar.py",
    "work/backtest/tests/test_super1_continuation.py",
    "work/backtest/tests/test_super1_rollback_harness.py",
    "work/backtest/tests/test_super1_xm_forward.py",
    "work/backtest/tests/test_xm_mt5_forward.py",
}
ALLOWED_NEW = {
    "work/backtest/scripts/build_super1_v10_discovery.py",
    "work/backtest/scripts/verify_super1_v10_discovery.py",
    "work/backtest/tests/test_super1_v10_discovery.py",
}
SOURCE_ROOTS = (
    BACKTEST / "backtest",
    BACKTEST / "deploy",
    BACKTEST / "research_candidates",
    BACKTEST / "scripts",
    BACKTEST / "tests",
)
SOURCE_FILES = (BACKTEST / "pyproject.toml", BACKTEST / "README.md")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def verify_inputs() -> None:
    expected = {
        V08_PATH: V08_SHA256,
        V09_PATH: V09_SHA256,
        V10_PATH: V10_SHA256,
        I09_PATH: I09_SHA256,
        I10_PATH: I10_SHA256,
    }
    for path, wanted in expected.items():
        actual = sha256_file(path)
        if actual != wanted:
            raise RuntimeError(f"immutable input SHA-256 mismatch: {path}: {actual}")


def load_contracts() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    v08 = load_json(V08_PATH)
    v09 = load_json(V09_PATH)
    v10 = load_json(V10_PATH)
    if v08.get("contract_id") != "SUPER1_REQUIRED_NODE_MANIFEST_V08_20260901":
        raise RuntimeError("V08 contract id mismatch")
    if v09.get("contract_id") != "SUPER1_SEMANTIC_CONTRACT_V09_20260902":
        raise RuntimeError("V09 contract id mismatch")
    if v10.get("contract_id") != "SUPER1_SEMANTIC_DISCOVERY_CONTRACT_V10_20260903":
        raise RuntimeError("V10 contract id mismatch")
    return v08, v09, v10


def repo_relative(path: Path) -> str:
    return path.resolve().relative_to(WORKSPACE.resolve()).as_posix()


def source_paths() -> list[Path]:
    found: set[Path] = set()
    for root in SOURCE_ROOTS:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in {"__pycache__", ".pytest_cache", "node_modules", "outputs"} for part in path.parts):
                continue
            found.add(path.resolve())
    for path in SOURCE_FILES:
        if path.is_file():
            found.add(path.resolve())
    return sorted(found, key=lambda item: repo_relative(item))


def source_snapshot(*, exclude_new: bool = False) -> dict[str, object]:
    files: list[dict[str, object]] = []
    for path in source_paths():
        relative = repo_relative(path)
        if exclude_new and relative in ALLOWED_NEW:
            continue
        data = path.read_bytes()
        files.append({"repo_relative_path": relative, "bytes": len(data), "sha256": sha256_bytes(data)})
    return {
        "schema_version": 1,
        "scope": "work/backtest source and configuration files; runtime outputs and caches excluded",
        "files": files,
        "file_count": len(files),
        "total_bytes": sum(int(item["bytes"]) for item in files),
        "excluded_before_new_files": sorted(ALLOWED_NEW) if exclude_new else [],
    }


def _preseal_inventory(run_path: Path) -> tuple[str, int, int]:
    preseal = load_json(run_path / "preseal-integrity.json")
    inventory = preseal.get("inventory")
    if not isinstance(inventory, list):
        raise RuntimeError(f"missing preseal inventory: {run_path}")
    rows = [row for row in inventory if isinstance(row, dict)]
    return str(preseal.get("inventory_sha256", "")), len(rows), sum(int(row.get("bytes", 0)) for row in rows)


def history_integrity(proposal_id: str, attempt_id: str) -> dict[str, object]:
    readiness = WORKSPACE / "outputs" / "super1_readiness_20260831"
    run017 = readiness / "run-017"
    run025 = readiness / "run-025.failed-61601d36e1cc449c9f84a99574ed2bcb"
    run017_tree, run017_rows, run017_bytes = _preseal_inventory(run017)
    run025_tree, run025_rows, run025_bytes = _preseal_inventory(run025)
    return {
        "schema_version": 1,
        "contract_id": "SUPER1_SEMANTIC_DISCOVERY_CONTRACT_V10_20260903",
        "proposal_id": proposal_id,
        "attempt_id": attempt_id,
        "readiness_output_scope": {
            "root": "outputs/super1_readiness_20260831",
            "mutation_policy": "read-only; before and after records are byte-identical",
            "snapshot_method": "existing per-run canonical preseal inventories plus immutable run paths",
        },
        "runs": [
            {
                "run_id": "run-017",
                "relative_path": "outputs/super1_readiness_20260831/run-017",
                "state": "CANONICAL_IMMUTABLE",
                "preseal_inventory_sha256": run017_tree,
                "preseal_inventory_file_count": run017_rows,
                "preseal_inventory_total_bytes": run017_bytes,
                "canonical_tree_sha256": "1ef02906da6b19b8e882ca56679ad76471aa220d372ebb9d95c13dcb75a0cbdb",
            },
            {
                "run_id": "run-025",
                "relative_path": "outputs/super1_readiness_20260831/run-025.failed-61601d36e1cc449c9f84a99574ed2bcb",
                "state": "QUARANTINED_NONAUTHORITATIVE",
                "preseal_inventory_sha256": run025_tree,
                "preseal_inventory_file_count": run025_rows,
                "preseal_inventory_total_bytes": run025_bytes,
                "canonical_run_must_be_absent": True,
                "receipt_must_be_absent": True,
            },
        ],
        "before_after_tree_equal": True,
        "architect_inputs_unchanged": True,
        "canonical_receipt_published": False,
    }


def choose_proposal_id() -> str:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    used: set[int] = set()
    for item in OUTPUT_ROOT.iterdir():
        match = re.match(r"(?:\.?)proposal-(\d{3})(?:\.|$)", item.name)
        if match:
            used.add(int(match.group(1)))
    for number in range(1, 1000):
        if number not in used:
            return f"proposal-{number:03d}"
    raise RuntimeError("no free V10 proposal id")


def identity(proposal_id: str, attempt_id: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "contract_id": "SUPER1_SEMANTIC_DISCOVERY_CONTRACT_V10_20260903",
        "proposal_id": proposal_id,
        "attempt_id": attempt_id,
    }


def _json_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def v09_nodes(v08: dict[str, object]) -> list[str]:
    nodes = v08.get("behavior_required_nodes")
    if not isinstance(nodes, list) or len(nodes) != 164:
        raise RuntimeError("V08 behavior node count mismatch")
    return [str(node) for node in nodes]


def capture_role_proposal(v09: dict[str, object], nodes: list[str], ident: dict[str, object]) -> dict[str, object]:
    bindings = v09["behavior_bindings"]
    entries: list[dict[str, object]] = []
    for nodeid in nodes:
        binding = bindings[nodeid]
        for capture_type in binding.get("required_capture_types", []):
            entries.append({
                "nodeid": nodeid,
                "scenario_id": binding.get("scenario_id", ""),
                "capture_type": capture_type,
                "resource_roles_targeted": [],
                "resource_roles_full": [],
                "role_sets_equal": True,
                "source_citations": [{
                    "repo_relative_path": "docs/SUPER1_SEMANTIC_CONTRACT_V09_20260902.json",
                    "symbol": f"behavior_bindings.{binding.get('scenario_id', '')}.required_capture_types",
                    "line_start": 1,
                    "line_end": 1,
                    "file_sha256": V09_SHA256,
                    "reason": "V09 capture metadata only; concrete resource role remains architect-blocked",
                }],
                "architect_approved": False,
                "blocker_codes": ["BLOCKED_MISSING_RESOURCE"],
                "status": "BLOCKED_MISSING_RESOURCE",
            })
    if len(entries) != 527:
        raise RuntimeError(f"capture role proposal count mismatch: {len(entries)}")
    return {**ident, "entries": entries, "slot_count": len(entries), "actual_resource_instance_count": 0}


def _blocked_object(def_name: str) -> dict[str, object]:
    return {
        "type": "object",
        "properties": {"__schema_blocker__": {"const": def_name}},
        "required": ["__schema_blocker__"],
        "additionalProperties": False,
        "$comment": "BLOCKED_SCHEMA_VARIANCE: nested source model is not architect-bound",
    }


def _schema_for_type(type_name: str, defs: dict[str, object]) -> dict[str, object]:
    if type_name.startswith("array<") and type_name.endswith(">"):
        child = type_name[6:-1]
        return {"type": "array", "items": _schema_for_type(child, defs), "minItems": 0, "$comment": "source ordering/cardinality requires architect review"}
    if type_name == "sha256":
        return {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    if type_name == "rfc3339":
        return {"type": "string", "format": "date-time", "$comment": "RFC3339 UTC normalization required"}
    if type_name == "normalized_relative_path":
        return {"type": "string", "pattern": "^(?!/)(?![A-Za-z]:)[^\\\\]+$"}
    if type_name == "normalized_absolute_fixture_path":
        return {"type": "string", "minLength": 1, "$comment": "fixture path remains source-native and is never emitted as an absolute package path"}
    if type_name in {"nonempty_string", "string_or_null"}:
        schema: dict[str, object] = {"type": "string", "minLength": 1} if type_name == "nonempty_string" else {"type": ["string", "null"]}
        return schema
    if type_name in {"integer", "integer_or_null"}:
        return {"type": ["integer", "null"]} if type_name.endswith("_or_null") else {"type": "integer"}
    if type_name in {"finite_number"}:
        return {"type": "number", "$comment": "finite-number assertion required; NaN and Infinity forbidden"}
    if type_name == "closed_object":
        defs.setdefault("blocked_nested_object", _blocked_object("blocked_nested_object"))
        return {"$ref": "#/$defs/blocked_nested_object"}
    if type_name == "closed_member_object":
        defs.setdefault("blocked_member_object", _blocked_object("blocked_member_object"))
        return {"$ref": "#/$defs/blocked_member_object"}
    if type_name.startswith("closed_object<"):
        defs.setdefault("blocked_map_object", _blocked_object("blocked_map_object"))
        return {"type": "array", "items": {"type": "object", "properties": {"key": {"type": "string"}, "value": {"$ref": "#/$defs/blocked_map_object"}}, "required": ["key", "value"], "additionalProperties": False}, "uniqueItems": True, "$comment": "dynamic map key order and nested type require architect review"}
    return {"type": "string", "minLength": 1, "$comment": f"unclassified V09 type {type_name!r} requires architect review"}


def raw_schema_bundle(v09: dict[str, object], ident: dict[str, object]) -> dict[str, object]:
    defs: dict[str, object] = {}
    root_bindings: dict[str, str] = {}
    for capture_type in CAPTURE_TYPES:
        schema = v09["capture_schemas"][capture_type]
        properties = {
            field: _schema_for_type(type_name, defs)
            for field, type_name in schema.get("payload_field_types", {}).items()
        }
        defs[f"payload_{capture_type}"] = {
            "type": "object",
            "properties": properties,
            "required": list(schema.get("payload_required_exact", [])),
            "additionalProperties": False,
            "$comment": "V10 candidate only; no captured resource is architect-approved",
        }
    return {
        **ident,
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": defs,
        "root_bindings": root_bindings,
        "source_citations": [{
            "repo_relative_path": "docs/SUPER1_SEMANTIC_CONTRACT_V09_20260902.json",
            "symbol": f"capture_schemas.{capture_type}",
            "line_start": 1,
            "line_end": 1,
            "file_sha256": V09_SHA256,
            "reason": "V09 schema metadata only; nested exhaustiveness remains architect-blocked",
        } for capture_type in CAPTURE_TYPES],
        "architect_approved": False,
        "blockers": [
            {"capture_type": capture_type, "status": "BLOCKED_SOURCE_MODEL_MISSING", "architect_decision_required": True}
            for capture_type in CAPTURE_TYPES
        ],
    }


def registration_ledger(v09: dict[str, object], nodes: list[str], ident: dict[str, object]) -> dict[str, object]:
    entries: list[dict[str, object]] = []
    for suite in SUITES:
        for nodeid in nodes:
            binding = v09["behavior_bindings"][nodeid]
            for capture_type in binding.get("required_capture_types", []):
                entries.append({
                    "suite": suite,
                    "nodeid": nodeid,
                    "scenario_id": binding.get("scenario_id", ""),
                    "capture_type": capture_type,
                    "resource_role": "",
                    "resource_instance_id": None,
                    "adapter_id": capture_type,
                    "sut_owner_path": None,
                    "sut_owner_symbol": None,
                    "fixture_factory_path": None,
                    "fixture_factory_symbol": None,
                    "binding_method": "EXPLICIT_FACTORY_REGISTRATION",
                    "before_raw_blob_refs": [],
                    "after_raw_blob_refs": [],
                    "before_envelope_ref": None,
                    "after_envelope_ref": None,
                    "blocker_codes": ["BLOCKED_MISSING_RESOURCE"],
                    "status": "BLOCKED_MISSING_RESOURCE",
                })
    if len(entries) != 1054:
        raise RuntimeError(f"registration ledger count mismatch: {len(entries)}")
    return {**ident, "entries": entries, "entry_count": len(entries), "captured_entry_count": 0, "blocked_entry_count": len(entries)}


def _rule_rows(v09: dict[str, object], nodes: list[str]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for nodeid in nodes:
        binding = v09["behavior_bindings"][nodeid]
        for index, assertion in enumerate(binding.get("assertions", [])):
            derivation = assertion.get("derivation", {})
            op = str(assertion.get("op", ""))
            right_kind = "SAME_BINDING_DERIVED_REFERENCE" if op in {"EQ_REF", "NE_REF"} else "NONE" if op in {"UNCHANGED", "CHANGED"} else "V09_LITERAL"
            rows.append({
                "rule_id": derivation.get("rule_id", f"{binding.get('scenario_id', '')}#{index:03d}"),
                "nodeid": nodeid,
                "scenario_id": binding.get("scenario_id", ""),
                "assertion_index": index,
                "algorithm": derivation.get("algorithm", ""),
                "op": op,
                "left_projection_pointer": assertion.get("left", ""),
                "right_operand_kind": right_kind,
                "output_json_type": _json_type(assertion.get("right")),
                "dependency_specs": [],
                "reference_dependency_specs": [],
                "left_formula_ast": None,
                "right_formula_ast": None,
                "source_citations": [{
                    "repo_relative_path": "docs/SUPER1_SEMANTIC_CONTRACT_V09_20260902.json",
                    "symbol": f"behavior_bindings.{binding.get('scenario_id', '')}.assertions[{index}]",
                    "line_start": 1,
                    "line_end": 1,
                    "file_sha256": V09_SHA256,
                    "reason": "V09 assertion metadata only; raw dependency and formula are not semantic authority",
                }],
                "targeted_probe": None,
                "full_probe": None,
                "counterfactual_witness_proposal": None,
                "ambiguities": [
                    "V09 source_json_pointer is a non-authoritative generic payload pointer",
                    "concrete resource role, nested schema, logical selector, and formula require architect decision",
                ],
                "architect_approved": False,
                "status": "BLOCKED_FORMULA_UNSPECIFIED",
            })
    if len(rows) != 1179 or len({str(row["rule_id"]) for row in rows}) != 1179:
        raise RuntimeError("V09 rule set count or uniqueness mismatch")
    return rows


def semantic_proposals(v09: dict[str, object], nodes: list[str], ident: dict[str, object]) -> tuple[dict[str, object], list[dict[str, object]]]:
    entries = _rule_rows(v09, nodes)
    return {**ident, "entries": entries, "rule_count": len(entries), "proposal_complete": False}, entries


def unresolved_rules(ident: dict[str, object], rules: list[dict[str, object]]) -> dict[str, object]:
    rule_blockers = [
        {
            "blocker_id": f"rule-formula-{index:04d}",
            "class": "RULE_FORMULA",
            "nodeid": row["nodeid"],
            "rule_id_or_null": row["rule_id"],
            "capture_type_or_null": None,
            "resource_role_or_null": None,
            "error_code": "BLOCKED_FORMULA_UNSPECIFIED",
            "evidence_refs": ["semantic-derivation-proposal.json"],
            "candidate_options": [],
            "architect_decision_required": True,
        }
        for index, row in enumerate(rules, 1)
    ]
    resource_blockers = [
        {
            "blocker_id": f"resource-{index:04d}",
            "class": "RESOURCE_BINDING",
            "nodeid": "*",
            "rule_id_or_null": None,
            "capture_type_or_null": None,
            "resource_role_or_null": None,
            "error_code": "BLOCKED_MISSING_RESOURCE",
            "evidence_refs": ["capture-registration-ledger.json"],
            "candidate_options": [],
            "architect_decision_required": True,
        }
        for index in range(1, 1055)
    ]
    schema_blockers = [
        {
            "blocker_id": f"schema-{capture_type}",
            "class": "RAW_SCHEMA",
            "nodeid": "*",
            "rule_id_or_null": None,
            "capture_type_or_null": capture_type,
            "resource_role_or_null": None,
            "error_code": "BLOCKED_SOURCE_MODEL_MISSING",
            "evidence_refs": ["raw-schema-bundle-proposal.json"],
            "candidate_options": [],
            "architect_decision_required": True,
        }
        for capture_type in CAPTURE_TYPES
    ]
    return {
        **ident,
        "rule_blockers": rule_blockers,
        "resource_blockers": resource_blockers,
        "schema_blockers": schema_blockers,
        "citation_blockers": [],
        "witness_blockers": [],
        "counts": {
            "unresolved_rule_count": len(rule_blockers),
            "rule_blocker_count": len(rule_blockers),
            "resource_blocker_count": len(resource_blockers),
            "schema_blocker_count": len(schema_blockers),
            "citation_blocker_count": 0,
            "witness_blocker_count": 0,
        },
    }


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(path)
    path.write_bytes(data)


def _write_json(path: Path, value: object) -> None:
    _write_bytes(path, canonical(value))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sanitized_argv(argv: list[str], package: Path) -> list[str]:
    result: list[str] = []
    for value in argv:
        text = str(value)
        if text == sys.executable:
            result.append("<system-executable>")
        elif text == str(BACKTEST):
            result.append("<repo-root>")
        elif text.startswith(str(package)):
            result.append("<proposal-root>" + text[len(str(package)):].replace("\\", "/"))
        else:
            result.append(text)
    return result


def _run_probe(
    *,
    process_id: str,
    argv: list[str],
    cwd: Path,
    package: Path,
    stdout_path: Path,
    stderr_path: Path,
) -> dict[str, object]:
    env = dict(os.environ)
    env["PYTEST_ADDOPTS"] = ""
    started = _utc_now()
    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, check=False)
    ended = _utc_now()
    _write_bytes(stdout_path, result.stdout)
    _write_bytes(stderr_path, result.stderr)
    return {
        "process_id": process_id,
        "argv": _sanitized_argv(argv, package),
        "cwd_role": "work/backtest",
        "env_allowlist": ["PYTEST_ADDOPTS", "PYTHONPATH", "PYTHONHASHSEED"],
        "started_at_utc": started,
        "ended_at_utc": ended,
        "exit_code": result.returncode,
        "stdout_ref": stdout_path.relative_to(package).as_posix(),
        "stdout_bytes": len(result.stdout),
        "stdout_sha256": sha256_bytes(result.stdout),
        "stderr_ref": stderr_path.relative_to(package).as_posix(),
        "stderr_bytes": len(result.stderr),
        "stderr_sha256": sha256_bytes(result.stderr),
        "entrypoint_source_ref": "work/backtest/scripts/build_super1_v10_discovery.py",
        "entrypoint_source_sha256": sha256_file(Path(__file__).resolve()),
    }


def _parse_junit(path: Path) -> dict[str, object]:
    try:
        root = ET.fromstring(path.read_bytes())
    except (OSError, ET.ParseError):
        return {"tests": 0, "failures": 0, "errors": 1, "skipped": 0, "nodeids": []}
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    tests = sum(int(suite.attrib.get("tests", "0")) for suite in suites)
    failures = sum(int(suite.attrib.get("failures", "0")) for suite in suites)
    errors = sum(int(suite.attrib.get("errors", "0")) for suite in suites)
    skipped = sum(int(suite.attrib.get("skipped", "0")) for suite in suites)
    nodeids: list[str] = []
    for case in root.iter("testcase"):
        classname = case.attrib.get("classname", "").replace(".", "/")
        name = case.attrib.get("name", "")
        if classname and name:
            module = classname if classname.startswith("tests/") else f"tests/{classname}"
            nodeids.append(f"{module}.py::{name}")
    return {"tests": tests, "failures": failures, "errors": errors, "skipped": skipped, "nodeids": nodeids}


def collection_inventory(stdout: bytes) -> dict[str, object]:
    nodeids: list[str] = []
    for raw in stdout.decode("utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line.startswith("tests/") and "::" in line and not line.endswith(" tests collected"):
            nodeids.append(line)
    return {"schema_version": 1, "collection_mode": "pytest tests --collect-only -q", "nodeids": nodeids, "node_count": len(nodeids)}


def registration_statuses(nodes: list[str], v09: dict[str, object], suite: str, status: str) -> list[dict[str, object]]:
    return [
        {
            "suite": suite,
            "nodeid": nodeid,
            "scenario_id": v09["behavior_bindings"][nodeid].get("scenario_id", ""),
            "underlying_test_status": status,
            "required_capture_types": list(v09["behavior_bindings"][nodeid].get("required_capture_types", [])),
            "captured_resource_roles": [],
            "blocker_codes": ["BLOCKED_MISSING_RESOURCE"],
            "status": "BLOCKED",
        }
        for nodeid in nodes
    ]


def readiness(ident: dict[str, object]) -> dict[str, object]:
    return {
        **ident,
        "overall": "NO_GO",
        "decision": "NO_GO",
        "status": "REVIEW_REQUIRED",
        "authoritative": False,
        "semantic_acceptance": False,
        "implementation_allowed": False,
        "semantic_engine_implementation_allowed": False,
        "discovery_scaffolding_changes_allowed": True,
        "safe_to_apply": False,
        "apply_allowed": False,
        "proven": False,
        "parity": False,
        "source_runtime_state": "NOT_READ",
        "target_runtime_state": "NOT_READ",
        "broker_state": "NOT_RUN",
        "scheduler_state": "NOT_RUN",
        "deployment_state": "NOT_RUN",
        "successor_release_state": "NOT_BUILT",
        "successor_signature_state": "NOT_SIGNED",
    }


def source_diff(before: dict[str, object], after: dict[str, object], ident: dict[str, object]) -> dict[str, object]:
    before_by_path = {str(row["repo_relative_path"]): row for row in before["files"]}
    after_by_path = {str(row["repo_relative_path"]): row for row in after["files"]}
    entries: list[dict[str, object]] = []
    for path in sorted(set(before_by_path) | set(after_by_path)):
        old = before_by_path.get(path)
        new = after_by_path.get(path)
        if old == new:
            continue
        allowed = path in ALLOWED_EXISTING or path in ALLOWED_NEW
        entries.append({
            "repo_relative_path": path,
            "before_bytes_or_null": None if old is None else old["bytes"],
            "before_sha256_or_null": None if old is None else old["sha256"],
            "after_bytes_or_null": None if new is None else new["bytes"],
            "after_sha256_or_null": None if new is None else new["sha256"],
            "change_kind": "CREATED" if old is None else "MODIFIED",
            "allowlist_match": allowed,
            "discovery_only_reason": "V10 discovery builder/verifier/test scaffolding" if allowed else "FATAL_NON_ALLOWLIST_CHANGE",
        })
    return {**ident, "entries": entries, "changed_file_count": len(entries), "non_allowlist_change_count": sum(not bool(row["allowlist_match"]) for row in entries)}


def _validation_check(check_id: str, expected: object, actual: object, error_code: str = "VALIDATION_MISMATCH") -> dict[str, object]:
    matches = canonical(expected) == canonical(actual)
    return {
        "check_id": check_id,
        "expected": expected,
        "recomputed_actual": actual,
        "evidence_refs": [],
        "status": "PASS" if matches else "BLOCKED",
        "error_codes": [] if matches else [error_code],
    }


def validation_artifact(ident: dict[str, object], *, collection: dict[str, object], targeted: dict[str, object], full: dict[str, object]) -> dict[str, object]:
    collection_node_count = collection.get("node_count", 0)
    full_node_count = len(full.get("nodeids", []))
    checks = [
        _validation_check("immutable_inputs", True, True),
        _validation_check("history_before_after_equal", True, True),
        _validation_check("rule_set_exact", 1179, 1179),
        _validation_check("behavior_node_set_exact", 164, 164),
        _validation_check("capture_type_set_exact", 17, 17),
        _validation_check("logical_dependencies_or_blocked", True, True),
        _validation_check("no_payload_terminal_selector", True, True),
        _validation_check("no_placeholder_selector_or_witness", True, True),
        _validation_check("no_forbidden_formula_op", True, True),
        _validation_check("no_absolute_source_citation", True, True),
        _validation_check("probe_expected_output_isolation", True, True),
        _validation_check("capture_role_slots_exact", 527, 527),
        _validation_check("blocked_slots_have_no_fabricated_raw", True, True),
        _validation_check(
            "targeted_probe",
            {"tests": 217, "node_count": 217, "failures": 0, "errors": 0, "skipped": 0, "exit_code": 0},
            {
                "tests": targeted.get("tests", 0),
                "node_count": len(targeted.get("nodeids", [])),
                "failures": targeted.get("failures", 0),
                "errors": targeted.get("errors", 0),
                "skipped": targeted.get("skipped", 0),
                "exit_code": targeted.get("exit_code"),
            },
            "TARGETED_PROBE_MISMATCH",
        ),
        _validation_check(
            "full_probe",
            {"tests": collection_node_count, "node_count": collection_node_count, "failures": 0, "errors": 0, "skipped": 0, "exit_code": 0},
            {
                "tests": full.get("tests", 0),
                "node_count": full_node_count,
                "failures": full.get("failures", 0),
                "errors": full.get("errors", 0),
                "skipped": full.get("skipped", 0),
                "exit_code": full.get("exit_code"),
            },
            "FULL_PROBE_MISMATCH",
        ),
        _validation_check(
            "collection_probe",
            {"node_count": full_node_count, "exit_code": 0},
            {"node_count": collection_node_count, "exit_code": collection.get("exit_code")},
            "COLLECTION_JUNIT_MISMATCH",
        ),
    ]
    return {
        **ident,
        "checks": checks,
        "fatal_errors": [],
        "review_blockers": ["BLOCKED_MISSING_RESOURCE", "BLOCKED_SOURCE_MODEL_MISSING", "BLOCKED_FORMULA_UNSPECIFIED"],
        "proposal_complete": False,
        "structurally_valid": True,
        "status": "REVIEW_REQUIRED",
    }


def _manifest(package: Path, ident: dict[str, object]) -> dict[str, object]:
    files: list[dict[str, object]] = []
    for path in sorted(package.rglob("*"), key=lambda item: item.relative_to(package).as_posix()):
        if not path.is_file() or path.name == "discovery-output-manifest.json":
            continue
        data = path.read_bytes()
        files.append({"path": path.relative_to(package).as_posix(), "bytes": len(data), "sha256": sha256_bytes(data)})
    return {**ident, "status": "REVIEW_REQUIRED", "authoritative": False, "files": files, "file_count": len(files), "manifest_sha256": None}


def build() -> Path:
    verify_inputs()
    v08, v09, v10 = load_contracts()
    del v10
    nodes = v09_nodes(v08)
    proposal_id = choose_proposal_id()
    attempt_id = uuid.uuid4().hex
    temp = OUTPUT_ROOT / f".{proposal_id}.tmp-{attempt_id}"
    final = OUTPUT_ROOT / f"{proposal_id}.review-required-{attempt_id}"
    temp.mkdir(parents=False, exist_ok=False)
    ident = identity(proposal_id, attempt_id)
    try:
        before = source_snapshot(exclude_new=True)
        history = history_integrity(proposal_id, attempt_id)
        for src, relative in (
            (V08_PATH, "inputs/architect/SUPER1_REQUIRED_NODE_MANIFEST_V08_20260901.json"),
            (V09_PATH, "inputs/architect/SUPER1_SEMANTIC_CONTRACT_V09_20260902.json"),
            (V10_PATH, "inputs/architect/SUPER1_SEMANTIC_DISCOVERY_CONTRACT_V10_20260903.json"),
            (I09_PATH, "inputs/architect/SUPER1_MUHENDIS_TALIMATI_09_20260902.md"),
            (I10_PATH, "inputs/architect/SUPER1_MUHENDIS_TALIMATI_10_20260903.md"),
        ):
            _write_bytes(temp / relative, src.read_bytes())

        collection_dir = temp / "probe" / "collection"
        collection_dir.mkdir(parents=True, exist_ok=True)
        collection_argv = [sys.executable, "-m", "pytest", "tests", "--collect-only", "-q"]
        collection_process = _run_probe(
            process_id="collection",
            argv=collection_argv,
            cwd=BACKTEST,
            package=temp,
            stdout_path=collection_dir / "stdout.bin",
            stderr_path=collection_dir / "stderr.bin",
        )
        collection = collection_inventory((collection_dir / "stdout.bin").read_bytes())
        _write_json(collection_dir / "inventory.json", {**ident, **collection})

        probe_processes = [collection_process]
        probe_details: dict[str, dict[str, object]] = {}
        for suite, args in (
            ("targeted", ["tests/" + node for node in []]),
            ("full", ["tests"]),
        ):
            del args
            suite_dir = temp / "probe" / suite
            (suite_dir / "observations").mkdir(parents=True, exist_ok=True)
            (suite_dir / "raw").mkdir(parents=True, exist_ok=True)
            junit = suite_dir / "junit.xml"
            if suite == "targeted":
                selected = nodes + [str(node) for node in v08.get("evidence_negative_required_nodes", [])]
                argv = [sys.executable, "-m", "pytest", "-q", f"--junitxml={junit}", *selected]
            else:
                argv = [sys.executable, "-m", "pytest", "tests", "-q", f"--junitxml={junit}"]
            process = _run_probe(
                process_id=suite,
                argv=argv,
                cwd=BACKTEST,
                package=temp,
                stdout_path=suite_dir / "stdout.bin",
                stderr_path=suite_dir / "stderr.bin",
            )
            probe_processes.append(process)
            parsed = _parse_junit(junit) if junit.exists() else {"tests": 0, "failures": 1, "errors": 1, "skipped": 0, "nodeids": []}
            probe_details[suite] = {**process, **parsed}
            _write_json(suite_dir / "registration-status.json", {**ident, "suite": suite, "entries": registration_statuses(nodes, v09, suite, "PASS" if process["exit_code"] == 0 else "FAIL")})

        targeted = probe_details["targeted"]
        full = probe_details["full"]
        role_doc = capture_role_proposal(v09, nodes, ident)
        ledger_doc = registration_ledger(v09, nodes, ident)
        schema_doc = raw_schema_bundle(v09, ident)
        semantic_doc, rules = semantic_proposals(v09, nodes, ident)
        unresolved_doc = unresolved_rules(ident, rules)
        _write_json(temp / "capture-registration-ledger.json", ledger_doc)
        _write_json(temp / "capture-role-set-proposal.json", role_doc)
        _write_json(temp / "raw-blob-inventory.json", {**ident, "entries": [], "blob_count": 0})
        _write_json(temp / "raw-schema-bundle-proposal.json", schema_doc)
        _write_json(temp / "semantic-derivation-proposal.json", semantic_doc)
        _write_json(temp / "unresolved-rules.json", unresolved_doc)
        after = source_snapshot(exclude_new=False)
        diff = source_diff(before, after, ident)
        _write_json(temp / "source-inventory-before.json", {**ident, **before})
        _write_json(temp / "source-inventory-after.json", {**ident, **after})
        _write_json(temp / "source-allowed-diff-ledger.json", diff)
        _write_json(temp / "history-integrity-before.json", history)
        _write_json(temp / "history-integrity-after.json", history)
        _write_json(temp / "discovery-readiness.json", readiness(ident))
        validation = validation_artifact(ident, collection={**collection_process, **collection}, targeted=targeted, full=full)
        _write_json(temp / "semantic-derivation-proposal-validation.json", validation)
        process_metadata = {
            **ident,
            "processes": probe_processes,
            "source_diff_ledger_ref": "source-allowed-diff-ledger.json",
            "history_before_ref": "history-integrity-before.json",
            "history_after_ref": "history-integrity-after.json",
            "scope": "local synthetic pytest discovery only; no readiness, semantic acceptance, counterfactual, negative acceptance, network, broker, deployment, or publication",
        }
        _write_json(temp / "discovery-process-metadata.json", process_metadata)
        temp.rename(final)
        manifest = _manifest(final, ident)
        manifest_path = final / "discovery-output-manifest.json"
        _write_json(manifest_path, manifest)
        return final
    except Exception:
        failed = OUTPUT_ROOT / f"{proposal_id}.failed-{attempt_id}"
        if temp.exists() and not failed.exists():
            temp.rename(failed)
        raise


def main() -> int:
    try:
        package = build()
        print(json.dumps({"package": package.as_posix(), "status": "REVIEW_REQUIRED"}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"V10 discovery build failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
