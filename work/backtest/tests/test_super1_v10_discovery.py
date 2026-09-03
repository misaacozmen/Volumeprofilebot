from __future__ import annotations

import json
from pathlib import Path

from scripts.build_super1_v10_discovery import validation_artifact
from scripts.verify_super1_v10_discovery import _validation_checks_by_id


ROOT = Path(__file__).resolve().parents[3]
V08 = ROOT / "docs" / "SUPER1_REQUIRED_NODE_MANIFEST_V08_20260901.json"
V09 = ROOT / "docs" / "SUPER1_SEMANTIC_CONTRACT_V09_20260902.json"
V10 = ROOT / "docs" / "SUPER1_SEMANTIC_DISCOVERY_CONTRACT_V10_20260903.json"


def test_v10_contract_sets_are_recomputed_from_immutable_inputs() -> None:
    v08 = json.loads(V08.read_text(encoding="utf-8"))
    v09 = json.loads(V09.read_text(encoding="utf-8"))
    v10 = json.loads(V10.read_text(encoding="utf-8"))
    nodes = v08["behavior_required_nodes"]
    slots = sum(len(v09["behavior_bindings"][node]["required_capture_types"]) for node in nodes)
    rules = sum(len(v09["behavior_bindings"][node]["assertions"]) for node in nodes)
    assert len(nodes) == 164
    assert slots == 527
    assert rules == 1179
    assert v10["contract_id"] == "SUPER1_SEMANTIC_DISCOVERY_CONTRACT_V10_20260903"
    assert v10["discovery_readiness_exact"]["status"] == "REVIEW_REQUIRED"


def test_blocked_discovery_has_no_semantic_oracle_fields() -> None:
    blocked = {
        "dependency_specs": [],
        "reference_dependency_specs": [],
        "left_formula_ast": None,
        "right_formula_ast": None,
        "targeted_probe": None,
        "full_probe": None,
        "counterfactual_witness_proposal": None,
        "ambiguities": ["architect decision required"],
        "architect_approved": False,
        "status": "BLOCKED_FORMULA_UNSPECIFIED",
    }
    assert blocked["ambiguities"]
    assert blocked["status"].startswith("BLOCKED_")
    assert blocked["architect_approved"] is False
    assert all(blocked[key] in ([], None) for key in (
        "dependency_specs", "reference_dependency_specs", "left_formula_ast",
        "right_formula_ast", "targeted_probe", "full_probe", "counterfactual_witness_proposal",
    ))


def _probe(*, tests: int, node_count: int, exit_code: int = 0) -> dict[str, object]:
    return {
        "tests": tests,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
        "nodeids": [f"tests/test_probe.py::test_{index}" for index in range(node_count)],
        "exit_code": exit_code,
    }


def test_collection_validation_is_derived_from_matching_junit_evidence() -> None:
    artifact = validation_artifact(
        {"schema_version": 1, "contract_id": "contract", "proposal_id": "proposal-001", "attempt_id": "a" * 32},
        collection={"node_count": 517, "exit_code": 0},
        targeted=_probe(tests=217, node_count=217),
        full=_probe(tests=517, node_count=517),
    )
    checks = {row["check_id"]: row for row in artifact["checks"]}
    assert checks["full_probe"]["expected"]["tests"] == 517
    assert checks["collection_probe"]["expected"] == {"node_count": 517, "exit_code": 0}
    assert checks["collection_probe"]["expected"] == checks["collection_probe"]["recomputed_actual"]
    assert checks["collection_probe"]["status"] == "PASS"


def test_collection_validation_rejects_515_expected_517_actual_marked_pass() -> None:
    artifact = validation_artifact(
        {"schema_version": 1, "contract_id": "contract", "proposal_id": "proposal-001", "attempt_id": "a" * 32},
        collection={"node_count": 517, "exit_code": 0},
        targeted=_probe(tests=217, node_count=217),
        full=_probe(tests=515, node_count=515),
    )
    collection_check = next(row for row in artifact["checks"] if row["check_id"] == "collection_probe")
    assert collection_check["expected"] == {"node_count": 515, "exit_code": 0}
    assert collection_check["recomputed_actual"] == {"node_count": 517, "exit_code": 0}
    assert collection_check["status"] == "BLOCKED"

    errors: list[str] = []
    _validation_checks_by_id([{**collection_check, "status": "PASS"}], errors)
    assert any("status contradicts expected/recomputed_actual comparison" in error for error in errors)
