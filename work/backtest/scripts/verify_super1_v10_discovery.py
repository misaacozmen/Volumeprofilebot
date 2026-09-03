"""Independent structural verifier for a Super1 V10 discovery package.

The verifier intentionally does not import the discovery producer or the V09
semantic reporter/verifier.  A valid result means only that the review package
is structurally honest; it never means semantic acceptance.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET


WORKSPACE = Path(__file__).resolve().parents[3]
BACKTEST = WORKSPACE / "work" / "backtest"
DOCS = WORKSPACE / "docs"
V08_PATH = DOCS / "SUPER1_REQUIRED_NODE_MANIFEST_V08_20260901.json"
V09_PATH = DOCS / "SUPER1_SEMANTIC_CONTRACT_V09_20260902.json"
V10_PATH = DOCS / "SUPER1_SEMANTIC_DISCOVERY_CONTRACT_V10_20260903.json"
I09_PATH = DOCS / "SUPER1_MUHENDIS_TALIMATI_09_20260902.md"
I10_PATH = DOCS / "SUPER1_MUHENDIS_TALIMATI_10_20260903.md"
EXPECTED_DOCS = {
    "SUPER1_REQUIRED_NODE_MANIFEST_V08_20260901.json": "d58ec29ba93cdbc88a9978e1860bf8ae5eaf8fa5913778a6de1b5d1ff2c3fbdf",
    "SUPER1_SEMANTIC_CONTRACT_V09_20260902.json": "6710b28110da8ca3289dba53241cf96b93f29bb29af8accf2ed93b550ba5cc33",
    "SUPER1_SEMANTIC_DISCOVERY_CONTRACT_V10_20260903.json": "52a2001633c1f068f5ab9fcd1d597cf0052d8261ee50ae1fd8cb5699cc02490a",
    "SUPER1_MUHENDIS_TALIMATI_09_20260902.md": "1dba834113f24c947a6eeedfa2115a4bd16e7bb54661b6585d3ad2dd8742aa39",
    "SUPER1_MUHENDIS_TALIMATI_10_20260903.md": "a6e4aa2289dc58949602e89cd9302537d9ee9a376da516e058387c76495d29a5",
}
CAPTURE_TYPES = (
    "sdk_call_trace", "order_store_sqlite", "market_fetch_store_sqlite", "broker_readback",
    "filesystem_inventory", "subprocess_trace", "calendar_validation", "filter_evidence",
    "exclusive_writer_trace", "release_archive_validation", "transition_validation",
    "snapshot_validation", "terminal_r_evaluation", "rollback_policy", "powershell_ast",
    "order_event_jsonl", "scenario_invocation_trace",
)
SUITES = ("targeted", "full")
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
    BACKTEST / "backtest", BACKTEST / "deploy", BACKTEST / "research_candidates",
    BACKTEST / "scripts", BACKTEST / "tests",
)
SOURCE_FILES = (BACKTEST / "pyproject.toml", BACKTEST / "README.md")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def repo_relative(path: Path) -> str:
    return path.resolve().relative_to(WORKSPACE.resolve()).as_posix()


def source_paths() -> list[Path]:
    found: set[Path] = set()
    for root in SOURCE_ROOTS:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or any(part in {"__pycache__", ".pytest_cache", "node_modules", "outputs"} for part in path.parts):
                continue
            found.add(path.resolve())
    for path in SOURCE_FILES:
        if path.is_file():
            found.add(path.resolve())
    return sorted(found, key=repo_relative)


def source_snapshot(*, exclude_new: bool = False) -> dict[str, object]:
    files = []
    for path in source_paths():
        relative = repo_relative(path)
        if exclude_new and relative in ALLOWED_NEW:
            continue
        data = path.read_bytes()
        files.append({"repo_relative_path": relative, "bytes": len(data), "sha256": sha256_bytes(data)})
    return {"files": files, "file_count": len(files), "total_bytes": sum(int(item["bytes"]) for item in files)}


def _error(errors: list[str], message: str) -> None:
    errors.append(message)


def _required_keys(value: object, expected: set[str], label: str, errors: list[str]) -> None:
    if not isinstance(value, dict):
        _error(errors, f"{label}: not an object")
        return
    actual = set(value)
    missing = expected - actual
    extra = actual - expected
    if missing:
        _error(errors, f"{label}: missing keys {sorted(missing)}")
    if extra:
        _error(errors, f"{label}: extra keys {sorted(extra)}")


def _identity(value: object, ident: dict[str, object], label: str, errors: list[str]) -> None:
    if not isinstance(value, dict):
        return
    for key in ("schema_version", "contract_id", "proposal_id", "attempt_id"):
        if value.get(key) != ident[key]:
            _error(errors, f"{label}: identity mismatch {key}")


def _junit(path: Path) -> tuple[int, int, int, int, list[str]]:
    try:
        root = ET.fromstring(path.read_bytes())
    except (OSError, ET.ParseError):
        return 0, 1, 1, 0, []
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
    return tests, failures, errors, skipped, nodeids


def _nodeids_from_collection(path: Path) -> list[str]:
    value = load_json(path)
    return [str(item) for item in value.get("nodeids", [])] if isinstance(value, dict) else []


def _nodeids_from_collection_stdout(path: Path) -> list[str]:
    nodeids: list[str] = []
    for raw in path.read_bytes().decode("utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line.startswith("tests/") and "::" in line and not line.endswith(" tests collected"):
            nodeids.append(line)
    return nodeids


def _json_equal(left: object, right: object) -> bool:
    return json.dumps(left, ensure_ascii=False, sort_keys=True, separators=(",", ":")) == json.dumps(
        right, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _validation_checks_by_id(value: object, errors: list[str]) -> dict[str, dict[str, object]]:
    if not isinstance(value, list):
        _error(errors, "proposal validation checks are not an array")
        return {}
    required = {"check_id", "expected", "recomputed_actual", "evidence_refs", "status", "error_codes"}
    by_id: dict[str, dict[str, object]] = {}
    for index, row in enumerate(value):
        label = f"proposal validation check[{index}]"
        _required_keys(row, required, label, errors)
        if not isinstance(row, dict):
            continue
        check_id = str(row.get("check_id", ""))
        if not check_id or check_id in by_id:
            _error(errors, f"{label}: empty or duplicate check_id")
            continue
        by_id[check_id] = row
        matches = _json_equal(row.get("expected"), row.get("recomputed_actual"))
        required_status = "PASS" if matches else "BLOCKED"
        if row.get("status") != required_status:
            _error(errors, f"{label}: {row.get('status')} status contradicts expected/recomputed_actual comparison")
        error_codes = row.get("error_codes")
        if not isinstance(error_codes, list) or (matches and error_codes) or (not matches and not error_codes):
            _error(errors, f"{label}: error_codes contradict expected/recomputed_actual comparison")
    return by_id


def _require_validation_evidence(
    checks: dict[str, dict[str, object]],
    check_id: str,
    expected: object,
    actual: object,
    errors: list[str],
) -> None:
    row = checks.get(check_id)
    if row is None:
        _error(errors, f"proposal validation check missing: {check_id}")
        return
    if not _json_equal(row.get("expected"), expected):
        _error(errors, f"proposal validation expected is not evidence-derived: {check_id}")
    if not _json_equal(row.get("recomputed_actual"), actual):
        _error(errors, f"proposal validation actual is not recomputed from evidence: {check_id}")


def _scan_forbidden(value: object, terms: tuple[str, ...]) -> int:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return sum(text.count(term) for term in terms)


def verify(package: Path) -> dict[str, object]:
    errors: list[str] = []
    blockers: list[str] = []
    package = package.resolve()
    match = re.fullmatch(r"proposal-(\d{3})\.review-required-([0-9a-f]{32})", package.name)
    if not match:
        _error(errors, "package name is not a V10 review-required name")
        return {"ok": False, "structurally_valid": False, "errors": errors, "review_blockers": blockers}
    proposal_id = f"proposal-{match.group(1)}"
    attempt_id = match.group(2)
    ident = {"schema_version": 1, "contract_id": "SUPER1_SEMANTIC_DISCOVERY_CONTRACT_V10_20260903", "proposal_id": proposal_id, "attempt_id": attempt_id}
    if not package.is_dir():
        _error(errors, "package directory missing")
        return {"ok": False, "structurally_valid": False, "errors": errors, "review_blockers": blockers}

    for name, wanted in EXPECTED_DOCS.items():
        path = DOCS / name
        if not path.is_file():
            _error(errors, f"immutable architect file missing: {name}")
        elif sha256_bytes(path.read_bytes()) != wanted:
            _error(errors, f"immutable architect hash mismatch: {name}")
    v08 = load_json(V08_PATH)
    v09 = load_json(V09_PATH)
    v10 = load_json(V10_PATH)
    if not isinstance(v08, dict) or not isinstance(v09, dict) or not isinstance(v10, dict):
        _error(errors, "architect input is not object-shaped")
        return {"ok": False, "structurally_valid": False, "errors": errors, "review_blockers": blockers}
    nodes = [str(item) for item in v08.get("behavior_required_nodes", [])]
    negative_nodes = [str(item) for item in v08.get("evidence_negative_required_nodes", [])]
    bindings = v09.get("behavior_bindings", {})
    expected_rules: dict[str, tuple[str, int, dict[str, object], dict[str, object]]] = {}
    for nodeid in nodes:
        binding = bindings.get(nodeid, {})
        for index, assertion in enumerate(binding.get("assertions", [])):
            derivation = assertion.get("derivation", {})
            rule_id = str(derivation.get("rule_id", ""))
            expected_rules[rule_id] = (nodeid, index, assertion, binding)

    required_top = {
        "discovery-readiness.json", "source-inventory-before.json", "source-inventory-after.json",
        "source-allowed-diff-ledger.json", "history-integrity-before.json", "history-integrity-after.json",
        "capture-registration-ledger.json", "capture-role-set-proposal.json", "raw-blob-inventory.json",
        "raw-schema-bundle-proposal.json", "semantic-derivation-proposal.json",
        "semantic-derivation-proposal-validation.json", "unresolved-rules.json",
        "discovery-process-metadata.json", "discovery-output-manifest.json",
    }
    top_files = {path.name for path in package.iterdir() if path.is_file()}
    if top_files != required_top:
        _error(errors, f"top-level package files mismatch: extra={sorted(top_files - required_top)} missing={sorted(required_top - top_files)}")
    architect_rel = {
        "inputs/architect/SUPER1_REQUIRED_NODE_MANIFEST_V08_20260901.json",
        "inputs/architect/SUPER1_SEMANTIC_CONTRACT_V09_20260902.json",
        "inputs/architect/SUPER1_SEMANTIC_DISCOVERY_CONTRACT_V10_20260903.json",
        "inputs/architect/SUPER1_MUHENDIS_TALIMATI_09_20260902.md",
        "inputs/architect/SUPER1_MUHENDIS_TALIMATI_10_20260903.md",
    }
    actual_architect = {path.relative_to(package).as_posix() for path in (package / "inputs" / "architect").glob("*")} if (package / "inputs" / "architect").is_dir() else set()
    if actual_architect != architect_rel:
        _error(errors, "architect input member set mismatch")
    for relative in architect_rel:
        source = DOCS / Path(relative).name
        target = package / relative
        if not target.is_file() or target.read_bytes() != source.read_bytes():
            _error(errors, f"architect input bytes mismatch: {relative}")
    for suite in SUITES:
        suite_root = package / "probe" / suite
        for required in ("junit.xml", "stdout.bin", "stderr.bin", "registration-status.json"):
            if not (suite_root / required).is_file():
                _error(errors, f"missing probe file: {suite}/{required}")
        for directory in ("observations", "raw"):
            if not (suite_root / directory).is_dir():
                _error(errors, f"missing probe subtree: {suite}/{directory}")
    collection_root = package / "probe" / "collection"
    for required in ("inventory.json", "stdout.bin", "stderr.bin"):
        if not (collection_root / required).is_file():
            _error(errors, f"missing collection file: {required}")

    def read_top(name: str) -> object:
        path = package / name
        try:
            return load_json(path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            _error(errors, f"invalid JSON: {name}")
            return {}

    readiness = read_top("discovery-readiness.json")
    _identity(readiness, ident, "readiness", errors)
    readiness_expected = {
        "overall": "NO_GO", "decision": "NO_GO", "status": "REVIEW_REQUIRED", "authoritative": False,
        "semantic_acceptance": False, "implementation_allowed": False,
        "semantic_engine_implementation_allowed": False, "discovery_scaffolding_changes_allowed": True,
        "safe_to_apply": False, "apply_allowed": False, "proven": False, "parity": False,
        "source_runtime_state": "NOT_READ", "target_runtime_state": "NOT_READ", "broker_state": "NOT_RUN",
        "scheduler_state": "NOT_RUN", "deployment_state": "NOT_RUN", "successor_release_state": "NOT_BUILT",
        "successor_signature_state": "NOT_SIGNED",
    }
    if isinstance(readiness, dict):
        for key, expected in readiness_expected.items():
            if readiness.get(key) != expected:
                _error(errors, f"readiness mismatch: {key}")

    roles = read_top("capture-role-set-proposal.json")
    role_entries = roles.get("entries", []) if isinstance(roles, dict) else []
    role_keys = {(str(row.get("nodeid")), str(row.get("capture_type"))) for row in role_entries if isinstance(row, dict)}
    expected_role_keys = {(nodeid, str(ct)) for nodeid in nodes for ct in bindings[nodeid].get("required_capture_types", [])}
    if len(role_entries) != 527 or role_keys != expected_role_keys:
        _error(errors, f"capture role slot mismatch: {len(role_entries)}")
    for row in role_entries:
        if not isinstance(row, dict):
            _error(errors, "capture role row is not object")
            continue
        if set(row) != {"nodeid", "scenario_id", "capture_type", "resource_roles_targeted", "resource_roles_full", "role_sets_equal", "source_citations", "architect_approved", "blocker_codes", "status"}:
            _error(errors, "capture role row shape mismatch")
        if row.get("architect_approved") is not False or row.get("status") not in {"PROPOSED_SINGLE_CANDIDATE", "BLOCKED_AMBIGUOUS_ROLE_SET", "BLOCKED_MISSING_RESOURCE"}:
            _error(errors, "capture role approval/status mismatch")
        if row.get("status", "").startswith("BLOCKED") and (row.get("resource_roles_targeted") or row.get("resource_roles_full")):
            _error(errors, "blocked role row has fabricated role")
    if isinstance(roles, dict) and roles.get("actual_resource_instance_count") != 0:
        _error(errors, "role instance count is not recomputed as zero for blocked proposal")

    ledger = read_top("capture-registration-ledger.json")
    ledger_entries = ledger.get("entries", []) if isinstance(ledger, dict) else []
    if len(ledger_entries) != 1054:
        _error(errors, f"registration ledger count mismatch: {len(ledger_entries)}")
    ledger_keys: set[tuple[str, str, str, str]] = set()
    for row in ledger_entries:
        if not isinstance(row, dict):
            _error(errors, "registration ledger row is not object")
            continue
        key = (str(row.get("suite")), str(row.get("nodeid")), str(row.get("capture_type")), str(row.get("resource_role")))
        if key in ledger_keys:
            _error(errors, f"duplicate registration ledger key: {key}")
        ledger_keys.add(key)
        if row.get("status") not in {"CONCRETE_BOUND", "BLOCKED_AMBIGUOUS_RESOURCE", "BLOCKED_MISSING_RESOURCE"}:
            _error(errors, "registration status enum mismatch")
        if row.get("status", "").startswith("BLOCKED"):
            if row.get("before_raw_blob_refs") != [] or row.get("after_raw_blob_refs") != [] or row.get("before_envelope_ref") is not None or row.get("after_envelope_ref") is not None:
                _error(errors, "blocked registration row has fabricated raw ref")
            if not row.get("blocker_codes"):
                _error(errors, "blocked registration row has no blocker")
        if row.get("binding_method") != "EXPLICIT_FACTORY_REGISTRATION":
            _error(errors, "registration binding method mismatch")

    raw_blobs = read_top("raw-blob-inventory.json")
    if isinstance(raw_blobs, dict) and raw_blobs.get("entries") != []:
        _error(errors, "blocked discovery unexpectedly contains raw source blobs")
    schema_bundle = read_top("raw-schema-bundle-proposal.json")
    if not isinstance(schema_bundle, dict) or schema_bundle.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        _error(errors, "schema bundle dialect mismatch")
    if isinstance(schema_bundle, dict):
        if schema_bundle.get("architect_approved") is not False or set(schema_bundle.get("root_bindings", {})):
            _error(errors, "schema bundle approval/root binding mismatch")
        schema_blockers = schema_bundle.get("blockers", [])
        if len(schema_blockers) != 17 or {row.get("capture_type") for row in schema_blockers if isinstance(row, dict)} != set(CAPTURE_TYPES):
            _error(errors, "schema blocker coverage mismatch")
        defs = schema_bundle.get("$defs", {})
        if not isinstance(defs, dict) or len(defs) < 17:
            _error(errors, "schema definitions missing")
        if isinstance(defs, dict):
            for name, schema in defs.items():
                if not isinstance(schema, dict):
                    _error(errors, f"schema definition not object: {name}")
                if schema.get("type") == "object" and ("required" not in schema or schema.get("additionalProperties") is not False):
                    _error(errors, f"open object schema: {name}")

    semantic = read_top("semantic-derivation-proposal.json")
    semantic_entries = semantic.get("entries", []) if isinstance(semantic, dict) else []
    actual_rules = {str(row.get("rule_id")): row for row in semantic_entries if isinstance(row, dict)}
    if len(semantic_entries) != 1179 or set(actual_rules) != set(expected_rules):
        _error(errors, f"semantic rule set mismatch: {len(semantic_entries)}")
    required_rule_keys = {"rule_id", "nodeid", "scenario_id", "assertion_index", "algorithm", "op", "left_projection_pointer", "right_operand_kind", "output_json_type", "dependency_specs", "reference_dependency_specs", "left_formula_ast", "right_formula_ast", "source_citations", "targeted_probe", "full_probe", "counterfactual_witness_proposal", "ambiguities", "architect_approved", "status"}
    blocked_rule_ids: set[str] = set()
    for rule_id, row in actual_rules.items():
        if set(row) != required_rule_keys:
            _error(errors, f"rule shape mismatch: {rule_id}")
        if rule_id not in expected_rules:
            continue
        nodeid, index, assertion, binding = expected_rules[rule_id]
        derivation = assertion.get("derivation", {})
        for key, expected in (("nodeid", nodeid), ("scenario_id", binding.get("scenario_id", "")), ("assertion_index", index), ("algorithm", derivation.get("algorithm", "")), ("op", assertion.get("op", "")), ("left_projection_pointer", assertion.get("left", ""))):
            if row.get(key) != expected:
                _error(errors, f"V09 rule binding mismatch {rule_id}:{key}")
        op = assertion.get("op")
        expected_kind = "SAME_BINDING_DERIVED_REFERENCE" if op in {"EQ_REF", "NE_REF"} else "NONE" if op in {"UNCHANGED", "CHANGED"} else "V09_LITERAL"
        if row.get("right_operand_kind") != expected_kind:
            _error(errors, f"right operand kind mismatch {rule_id}")
        if row.get("architect_approved") is not False:
            _error(errors, f"rule architect approval is not false: {rule_id}")
        if str(row.get("status", "")).startswith("BLOCKED"):
            blocked_rule_ids.add(rule_id)
            if not row.get("ambiguities") or row.get("dependency_specs") != [] or row.get("reference_dependency_specs") != [] or row.get("left_formula_ast") is not None or row.get("right_formula_ast") is not None or row.get("targeted_probe") is not None or row.get("full_probe") is not None or row.get("counterfactual_witness_proposal") is not None:
                _error(errors, f"blocked rule contains selected semantic data: {rule_id}")
        else:
            _error(errors, f"unexpected non-blocked rule in engineer proposal: {rule_id}")
    semantic_scan = _scan_forbidden(semantic_entries, ("${", "/payload", "first-match", "FIRST_MATCH", "wildcard", "recursive key", "expected-value search"))
    if semantic_scan:
        _error(errors, f"forbidden semantic proposal token count: {semantic_scan}")

    unresolved = read_top("unresolved-rules.json")
    if not isinstance(unresolved, dict):
        _error(errors, "unresolved rules not object")
        unresolved = {}
    unresolved_rule_rows = unresolved.get("rule_blockers", [])
    unresolved_ids = {str(row.get("rule_id_or_null")) for row in unresolved_rule_rows if isinstance(row, dict)}
    if len(unresolved_rule_rows) != 1179 or unresolved_ids != blocked_rule_ids:
        _error(errors, "unresolved rule blocker coverage mismatch")
    for row in unresolved_rule_rows:
        if not isinstance(row, dict) or row.get("architect_decision_required") is not True or row.get("error_code") != "BLOCKED_FORMULA_UNSPECIFIED":
            _error(errors, "unresolved rule blocker shape mismatch")

    for suite in SUITES:
        registration = read_top(f"probe/{suite}/registration-status.json")
        entries = registration.get("entries", []) if isinstance(registration, dict) else []
        if len(entries) != 164 or {str(row.get("nodeid")) for row in entries if isinstance(row, dict)} != set(nodes):
            _error(errors, f"{suite} registration status node set mismatch")
        for row in entries:
            if isinstance(row, dict) and (row.get("status") != "BLOCKED" or not row.get("blocker_codes") or row.get("captured_resource_roles") != []):
                _error(errors, f"{suite} blocked registration status mismatch")

    collection = read_top("probe/collection/inventory.json")
    collection_inventory_nodes = _nodeids_from_collection(package / "probe/collection/inventory.json")
    collection_nodes = _nodeids_from_collection_stdout(package / "probe/collection/stdout.bin")
    target_tests, target_failures, target_errors, target_skipped, target_nodes = _junit(package / "probe/targeted/junit.xml")
    full_tests, full_failures, full_errors, full_skipped, full_nodes = _junit(package / "probe/full/junit.xml")
    if collection_inventory_nodes != collection_nodes:
        _error(errors, "collection inventory nodeids do not match collection stdout")
    expected_target_nodes = nodes + negative_nodes
    if target_tests != 217 or len(target_nodes) != 217 or target_failures or target_errors or target_skipped:
        _error(errors, "targeted JUnit is not exactly 217 clean PASS cases")
    if Counter(target_nodes) != Counter(expected_target_nodes):
        _error(errors, "targeted JUnit multiset differs from the V08 targeted multiset")
    if full_tests != len(full_nodes) or full_failures or full_errors or full_skipped:
        _error(errors, "full JUnit is not a clean PASS multiset")
    if Counter(full_nodes) != Counter(collection_nodes):
        _error(errors, "full JUnit and collection node multisets differ")
    if isinstance(collection, dict) and collection.get("node_count") != len(collection_nodes):
        _error(errors, "collection node count is not recomputed")

    before = read_top("source-inventory-before.json")
    after = read_top("source-inventory-after.json")
    ledger_diff = read_top("source-allowed-diff-ledger.json")
    actual_before = source_snapshot(exclude_new=True)
    actual_after = source_snapshot(exclude_new=False)
    for actual, stored, label in ((actual_before, before, "before"), (actual_after, after, "after")):
        if not isinstance(stored, dict) or stored.get("files") != actual["files"]:
            _error(errors, f"source inventory {label} mismatch")
    if isinstance(ledger_diff, dict):
        entries = ledger_diff.get("entries", [])
        if ledger_diff.get("non_allowlist_change_count") != 0 or any(not row.get("allowlist_match") for row in entries if isinstance(row, dict)):
            _error(errors, "non-allowlisted source change")
        changed_paths = {str(row.get("repo_relative_path")) for row in entries if isinstance(row, dict)}
        if changed_paths != ALLOWED_NEW:
            _error(errors, "source diff ledger does not contain exactly the discovery files")
        for row in entries:
            if isinstance(row, dict) and row.get("change_kind") != "CREATED":
                _error(errors, "unexpected modified source in discovery ledger")

    history_before = read_top("history-integrity-before.json")
    history_after = read_top("history-integrity-after.json")
    if history_before != history_after:
        _error(errors, "history before/after differs")
    if isinstance(history_before, dict):
        paths = {str(row.get("relative_path")): row for row in history_before.get("runs", []) if isinstance(row, dict)}
        if "outputs/super1_readiness_20260831/run-017" not in paths or "outputs/super1_readiness_20260831/run-025.failed-61601d36e1cc449c9f84a99574ed2bcb" not in paths:
            _error(errors, "immutable history coverage missing")
        if not (WORKSPACE / "outputs/super1_readiness_20260831/run-017").is_dir() or not (WORKSPACE / "outputs/super1_readiness_20260831/run-025.failed-61601d36e1cc449c9f84a99574ed2bcb").is_dir():
            _error(errors, "immutable history path missing")
        if (WORKSPACE / "outputs/super1_readiness_20260831/run-025").exists():
            _error(errors, "canonical run-025 unexpectedly exists")

    process = read_top("discovery-process-metadata.json")
    processes = process.get("processes", []) if isinstance(process, dict) else []
    process_by_id: dict[str, dict[str, object]] = {}
    if len(processes) != 3:
        _error(errors, "process metadata must contain collection, targeted, full")
    for row in processes:
        if not isinstance(row, dict):
            _error(errors, "process metadata row is not object")
            continue
        process_id = str(row.get("process_id", ""))
        if not process_id or process_id in process_by_id:
            _error(errors, "process metadata contains empty or duplicate process_id")
        else:
            process_by_id[process_id] = row
        if any(Path(str(arg)).is_absolute() for arg in row.get("argv", [])):
            _error(errors, "absolute process argv")
        for key in ("stdout_ref", "stderr_ref"):
            ref = str(row.get(key, ""))
            if ref.startswith("/") or "\\" in ref or ".." in Path(ref).parts or not (package / ref).is_file():
                _error(errors, f"invalid process ref: {ref}")
        for ref_key, bytes_key, hash_key in (("stdout_ref", "stdout_bytes", "stdout_sha256"), ("stderr_ref", "stderr_bytes", "stderr_sha256")):
            data = (package / str(row.get(ref_key))).read_bytes()
            if len(data) != row.get(bytes_key) or sha256_bytes(data) != row.get(hash_key):
                _error(errors, f"process output hash mismatch: {ref_key}")
    if set(process_by_id) != {"collection", "targeted", "full"}:
        _error(errors, "process metadata ids must be exactly collection, targeted, full")
    for process_id in ("collection", "targeted", "full"):
        if process_by_id.get(process_id, {}).get("exit_code") != 0:
            _error(errors, f"{process_id} probe process did not exit 0")

    manifest_path = package / "discovery-output-manifest.json"
    try:
        manifest = load_json(manifest_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        manifest = {}
        _error(errors, "manifest invalid")
    if isinstance(manifest, dict):
        manifest_rows = manifest.get("files", [])
        actual_rows = []
        for path in sorted(package.rglob("*"), key=lambda item: item.relative_to(package).as_posix()):
            if path.is_file() and path.name != "discovery-output-manifest.json":
                data = path.read_bytes()
                relative = path.relative_to(package).as_posix()
                if relative.startswith("/") or "\\" in relative or ".." in Path(relative).parts:
                    _error(errors, f"unsafe manifest path: {relative}")
                actual_rows.append({"path": relative, "bytes": len(data), "sha256": sha256_bytes(data)})
        if manifest_rows != actual_rows:
            _error(errors, "manifest member/hash mismatch")
        if manifest.get("status") != "REVIEW_REQUIRED" or manifest.get("authoritative") is not False:
            _error(errors, "manifest status is not truthful")

    absolute_tokens = (str(WORKSPACE).replace("\\", "/"), "C:\\Users\\", "\\tmp\\", "/tmp/", "<temp>")
    package_text = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in package.rglob("*.json") if path.name != "discovery-output-manifest.json")
    for token in absolute_tokens:
        if token in package_text:
            _error(errors, f"absolute/temp path leaked into package: {token}")

    validation = read_top("semantic-derivation-proposal-validation.json")
    _identity(validation, ident, "proposal validation", errors)
    _required_keys(
        validation,
        {"schema_version", "contract_id", "proposal_id", "attempt_id", "checks", "fatal_errors", "review_blockers", "proposal_complete", "structurally_valid", "status"},
        "proposal validation",
        errors,
    )
    validation_checks = _validation_checks_by_id(validation.get("checks", []) if isinstance(validation, dict) else None, errors)
    collection_count = len(collection_nodes)
    targeted_actual = {
        "tests": target_tests,
        "node_count": len(target_nodes),
        "failures": target_failures,
        "errors": target_errors,
        "skipped": target_skipped,
        "exit_code": process_by_id.get("targeted", {}).get("exit_code"),
    }
    full_actual = {
        "tests": full_tests,
        "node_count": len(full_nodes),
        "failures": full_failures,
        "errors": full_errors,
        "skipped": full_skipped,
        "exit_code": process_by_id.get("full", {}).get("exit_code"),
    }
    _require_validation_evidence(
        validation_checks,
        "targeted_probe",
        {"tests": 217, "node_count": 217, "failures": 0, "errors": 0, "skipped": 0, "exit_code": 0},
        targeted_actual,
        errors,
    )
    _require_validation_evidence(
        validation_checks,
        "full_probe",
        {"tests": collection_count, "node_count": collection_count, "failures": 0, "errors": 0, "skipped": 0, "exit_code": 0},
        full_actual,
        errors,
    )
    _require_validation_evidence(
        validation_checks,
        "collection_probe",
        {"node_count": len(full_nodes), "exit_code": 0},
        {"node_count": collection_count, "exit_code": process_by_id.get("collection", {}).get("exit_code")},
        errors,
    )
    if isinstance(validation, dict) and (validation.get("status") != "REVIEW_REQUIRED" or validation.get("structurally_valid") is not True or validation.get("proposal_complete") is not False or validation.get("fatal_errors") != []):
        _error(errors, "proposal validation status is untruthful")
    if errors:
        return {"ok": False, "structurally_valid": False, "errors": errors, "review_blockers": blockers, "proposal_id": proposal_id, "attempt_id": attempt_id}
    return {
        "ok": True,
        "structurally_valid": True,
        "errors": [],
        "review_blockers": sorted(set(blockers)) + ["BLOCKED_MISSING_RESOURCE", "BLOCKED_SOURCE_MODEL_MISSING", "BLOCKED_FORMULA_UNSPECIFIED"],
        "proposal_id": proposal_id,
        "attempt_id": attempt_id,
        "targeted_tests": target_tests,
        "full_tests": full_tests,
        "collection_nodes": len(collection_nodes),
        "targeted_nodes": len(target_nodes),
        "full_nodes": len(full_nodes),
        "capture_role_slots": len(role_entries),
        "registration_entries": len(ledger_entries),
        "rules": len(semantic_entries),
        "blocked_rules": len(blocked_rule_ids),
        "schema_defs": len(schema_bundle.get("$defs", {})) if isinstance(schema_bundle, dict) else 0,
        "schema_root_bindings": len(schema_bundle.get("root_bindings", {})) if isinstance(schema_bundle, dict) else 0,
        "manifest_sha256": sha256_bytes(manifest_path.read_bytes()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("package", type=Path)
    args = parser.parse_args()
    result = verify(args.package)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
