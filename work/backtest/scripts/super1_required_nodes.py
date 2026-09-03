"""Architect-owned V08/V09 contract loader.

Node membership comes only from the immutable V08 manifest.  Capture binding
and assertion metadata comes only from the immutable V09 contract; no runner
or test is allowed to redefine either set.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


WORKSPACE = Path(__file__).resolve().parents[3]
MANIFEST_PATH = WORKSPACE / "docs" / "SUPER1_REQUIRED_NODE_MANIFEST_V08_20260901.json"
MANIFEST_RELATIVE_PATH = "workspace/docs/SUPER1_REQUIRED_NODE_MANIFEST_V08_20260901.json"
MANIFEST_SHA256 = "d58ec29ba93cdbc88a9978e1860bf8ae5eaf8fa5913778a6de1b5d1ff2c3fbdf"
CONTRACT_PATH = WORKSPACE / "docs" / "SUPER1_SEMANTIC_CONTRACT_V09_20260902.json"
CONTRACT_SHA256 = "6710b28110da8ca3289dba53241cf96b93f29bb29af8accf2ed93b550ba5cc33"


def _load_manifest() -> dict[str, Any]:
    raw = MANIFEST_PATH.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != MANIFEST_SHA256:
        raise RuntimeError(f"V08 manifest SHA-256 mismatch: {actual}")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("V08 manifest must be a JSON object")
    return payload


MANIFEST = _load_manifest()
BEHAVIOR_REQUIRED_NODE_ORDER = tuple(str(node) for node in MANIFEST["behavior_required_nodes"])
EVIDENCE_NEGATIVE_REQUIRED_NODE_ORDER = tuple(str(node) for node in MANIFEST["evidence_negative_required_nodes"])
ALL_REQUIRED_NODE_ORDER = BEHAVIOR_REQUIRED_NODE_ORDER + EVIDENCE_NEGATIVE_REQUIRED_NODE_ORDER
BEHAVIOR_REQUIRED_NODES = frozenset(BEHAVIOR_REQUIRED_NODE_ORDER)
EVIDENCE_NEGATIVE_REQUIRED_NODES = frozenset(EVIDENCE_NEGATIVE_REQUIRED_NODE_ORDER)
ALL_REQUIRED_NODES = frozenset(BEHAVIOR_REQUIRED_NODES | EVIDENCE_NEGATIVE_REQUIRED_NODES)

if BEHAVIOR_REQUIRED_NODES & EVIDENCE_NEGATIVE_REQUIRED_NODES:
    raise RuntimeError("V08 behavior/evidence-negative sets overlap")


def _load_contract() -> dict[str, Any]:
    raw = CONTRACT_PATH.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != CONTRACT_SHA256:
        raise RuntimeError(f"V09 contract SHA-256 mismatch: {actual}")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("status") != "COMPLETE":
        raise RuntimeError("V09 contract is not a complete object")
    if payload.get("contract_id") != "SUPER1_SEMANTIC_CONTRACT_V09_20260902":
        raise RuntimeError("V09 contract id mismatch")
    bindings = payload.get("behavior_bindings")
    negatives = payload.get("negative_bindings")
    if not isinstance(bindings, dict) or not isinstance(negatives, dict):
        raise RuntimeError("V09 binding registries are missing")
    if tuple(bindings) != BEHAVIOR_REQUIRED_NODE_ORDER or tuple(negatives) != EVIDENCE_NEGATIVE_REQUIRED_NODE_ORDER:
        raise RuntimeError("V09 binding registries do not match the V08 manifest order")
    if sum(len(item.get("assertions", [])) for item in bindings.values()) != 1179:
        raise RuntimeError("V09 assertion registry count mismatch")
    return payload


CONTRACT = _load_contract()
V09_BEHAVIOR_BINDINGS: dict[str, dict[str, Any]] = CONTRACT["behavior_bindings"]
V09_NEGATIVE_BINDINGS: dict[str, dict[str, Any]] = CONTRACT["negative_bindings"]

REQUIRED: dict[str, dict[str, Any]] = {}
for edge in MANIFEST["requirement_edges"]:
    code = str(edge["requirement"])
    node = str(edge["nodeid"])
    definition = REQUIRED.setdefault(
        code,
        {
            "expected": f"architect-owned {code} behavior/evidence contract",
            "persistent_state": f"{code}_PERSISTENT_STATE_BOUND",
            "new_entry": f"{code}_NEW_ENTRY_CONTRACT_BOUND",
            "remove": f"{code}_REMOVE_CONTRACT_BOUND",
            "nodes": [],
            "behavior_nodes": [],
            "evidence_negative_nodes": [],
        },
    )
    if node not in definition["nodes"]:
        definition["nodes"].append(node)
    if edge["class"] == "BEHAVIOR":
        if node not in definition["behavior_nodes"]:
            definition["behavior_nodes"].append(node)
    elif edge["class"] == "EVIDENCE_NEGATIVE":
        if node not in definition["evidence_negative_nodes"]:
            definition["evidence_negative_nodes"].append(node)
    else:
        raise RuntimeError(f"unknown V08 edge class: {edge['class']}")

for definition in REQUIRED.values():
    definition["nodes"] = sorted(definition["nodes"])
    definition["behavior_nodes"] = sorted(definition["behavior_nodes"])
    definition["evidence_negative_nodes"] = sorted(definition["evidence_negative_nodes"])

REQUIRED_MAPPING_COUNT = len(MANIFEST["requirement_edges"])
BEHAVIOR_NODE_COUNT = len(BEHAVIOR_REQUIRED_NODES)
EVIDENCE_NEGATIVE_NODE_COUNT = len(EVIDENCE_NEGATIVE_REQUIRED_NODES)
UNIQUE_REQUIRED_NODE_COUNT = len(ALL_REQUIRED_NODES)

EXPECTED_COUNTS = {
    "required_mapping_count": REQUIRED_MAPPING_COUNT,
    "behavior_required_node_count": BEHAVIOR_NODE_COUNT,
    "evidence_negative_required_node_count": EVIDENCE_NEGATIVE_NODE_COUNT,
    "unique_required_node_count": UNIQUE_REQUIRED_NODE_COUNT,
    "behavior_observation_count_per_suite": BEHAVIOR_NODE_COUNT,
    "behavior_observation_count_two_suites": BEHAVIOR_NODE_COUNT * 2,
}


def node_class(nodeid: str) -> str | None:
    if nodeid in BEHAVIOR_REQUIRED_NODES:
        return "BEHAVIOR"
    if nodeid in EVIDENCE_NEGATIVE_REQUIRED_NODES:
        return "EVIDENCE_NEGATIVE"
    return None


def requirement_for(nodeid: str) -> str | None:
    for edge in MANIFEST["requirement_edges"]:
        if edge["nodeid"] == nodeid:
            return str(edge["requirement"])
    return None


def required_raw_sources(nodeid: str) -> tuple[str, ...]:
    """Exact V09 capture set for a behavior node; negatives have no parent capture."""
    binding = V09_BEHAVIOR_BINDINGS.get(nodeid)
    if binding is None:
        return ()
    values = binding.get("required_capture_types")
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise RuntimeError(f"V09 capture binding invalid for {nodeid}")
    return tuple(values)
