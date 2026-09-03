"""Independent V09 raw-byte reporter.

This process reads only JUnit, capture envelopes/payload bytes, and the two
architect-owned contract files.  It never imports the observation producer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET
from typing import Any


WORKSPACE = Path(__file__).resolve().parents[3]
V08_PATH = WORKSPACE / "docs" / "SUPER1_REQUIRED_NODE_MANIFEST_V08_20260901.json"
V09_PATH = WORKSPACE / "docs" / "SUPER1_SEMANTIC_CONTRACT_V09_20260902.json"
V08_SHA256 = "d58ec29ba93cdbc88a9978e1860bf8ae5eaf8fa5913778a6de1b5d1ff2c3fbdf"
V09_SHA256 = "6710b28110da8ca3289dba53241cf96b93f29bb29af8accf2ed93b550ba5cc33"
FORBIDDEN = frozenset({"expected", "actual", "recomputed", "measurement", "result", "outcome_code", "facts", "resources", "error_codes"})
MISSING = object()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_architect_files() -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    errors: list[str] = []
    values: list[dict[str, Any]] = []
    for path, expected, label in ((V08_PATH, V08_SHA256, "V08"), (V09_PATH, V09_SHA256, "V09")):
        try:
            raw = path.read_bytes()
            if digest(raw) != expected: errors.append(f"{label}_MANIFEST_HASH_MISMATCH")
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict): raise ValueError("object required")
            values.append(value)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"{label}_SOURCE_INVALID:{exc}"); values.append({})
    return values[0], values[1], errors


V08, V09, _ARCHITECT_ERRORS = load_architect_files()
BEHAVIOR_REQUIRED_NODES = tuple(str(v) for v in V08.get("behavior_required_nodes", []))
EVIDENCE_NEGATIVE_REQUIRED_NODES = tuple(str(v) for v in V08.get("evidence_negative_required_nodes", []))
ALL_REQUIRED_NODES = frozenset(BEHAVIOR_REQUIRED_NODES + EVIDENCE_NEGATIVE_REQUIRED_NODES)
V09_BINDINGS = V09.get("behavior_bindings", {}) if isinstance(V09.get("behavior_bindings"), dict) else {}
REQUIRED = {}
for edge in V08.get("requirement_edges", []):
    if isinstance(edge, dict):
        code = str(edge.get("requirement")); node = str(edge.get("nodeid"))
        REQUIRED.setdefault(code, {"nodes": [], "behavior_nodes": [], "evidence_negative_nodes": []})["nodes"].append(node)
        target = "behavior_nodes" if edge.get("class") == "BEHAVIOR" else "evidence_negative_nodes"
        REQUIRED[code][target].append(node)
for value in REQUIRED.values():
    for key in value: value[key] = list(dict.fromkeys(value[key]))
EXPECTED_COUNTS = {"required_mapping_count": len(V08.get("requirement_edges", [])), "unique_required_node_count": len(ALL_REQUIRED_NODES), "behavior_required_node_count": len(BEHAVIOR_REQUIRED_NODES), "evidence_negative_required_node_count": len(EVIDENCE_NEGATIVE_REQUIRED_NODES), "behavior_observation_count_per_suite": len(BEHAVIOR_REQUIRED_NODES), "behavior_observation_count_two_suites": len(BEHAVIOR_REQUIRED_NODES) * 2}


def normalize_node(classname: str, name: str) -> str:
    module = classname.replace(".", "/")
    if not module.endswith(".py"): module += ".py"
    return f"{module if module.startswith('tests/') else 'tests/' + module}::{name}"


def parse_junit(paths: list[Path]) -> dict[str, dict[str, Any]]:
    observed: dict[str, dict[str, Any]] = {}
    for path in paths:
        root = ET.parse(path).getroot()
        for case in root.iter("testcase"):
            node = normalize_node(str(case.get("classname") or ""), str(case.get("name") or ""))
            status = "PASS" if case.find("failure") is None and case.find("error") is None and case.find("skipped") is None else ("FAIL" if case.find("failure") is not None else ("ERROR" if case.find("error") is not None else "SKIPPED"))
            entry = observed.setdefault(node, {"statuses": [], "sources": [], "durations_seconds": []})
            entry["statuses"].append(status); entry["sources"].append(str(path.resolve()))
            try: entry["durations_seconds"].append(float(case.get("time") or 0.0))
            except (TypeError, ValueError): entry["durations_seconds"].append(None)
    return observed


def _find(value: object, name: str) -> object:
    if isinstance(value, dict):
        if name in value: return value[name]
        for child in value.values():
            found = _find(child, name)
            if found is not MISSING: return found
    elif isinstance(value, list):
        for child in value:
            found = _find(child, name)
            if found is not MISSING: return found
    return MISSING


def _pointer(value: object, pointer: str) -> object:
    if not pointer.startswith("/"): return MISSING
    current = value
    for part in pointer[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and part in current: current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current): current = current[int(part)]
        else: return MISSING
    return current


def _capture_payload(root: Path, ref: dict[str, Any], identity: dict[str, str]) -> tuple[object, list[str]]:
    errors: list[str] = []
    envelope_rel = Path(str(ref.get("path", "")))
    if envelope_rel.is_absolute() or ".." in envelope_rel.parts: return MISSING, ["V09_PATH_SAFETY"]
    envelope_path = root / envelope_rel
    try:
        envelope_raw = envelope_path.read_bytes(); envelope = json.loads(envelope_raw.decode("utf-8"))
    except FileNotFoundError: return MISSING, ["RAW_SOURCE_FILE_MISSING"]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc: return MISSING, [f"V09_RAW_ENVELOPE_INVALID:{exc}"]
    if not isinstance(envelope, dict): return MISSING, ["V09_RAW_ENVELOPE_OBJECT_REQUIRED"]
    for field in ("run_id", "suite", "nodeid", "attempt_id", "scenario_nonce", "checkpoint_id"):
        if envelope.get(field) != identity.get(field): errors.append(f"RAW_IDENTITY_MISMATCH:{field}")
    for field, expected in (("capture_type", ref.get("capture_type")), ("resource_instance_id", ref.get("resource_instance_id")), ("phase", ref.get("phase"))):
        if expected is not None and envelope.get(field) != expected: errors.append(f"V09_RAW_DECLARATION_MISMATCH:{field}")
    payload_rel = Path(str(envelope.get("payload_relative_path", "")))
    if payload_rel.is_absolute() or ".." in payload_rel.parts: return MISSING, errors + ["V09_PATH_SAFETY"]
    try:
        payload_raw = (root / payload_rel).read_bytes(); payload = json.loads(payload_raw.decode("utf-8"))
    except FileNotFoundError: return MISSING, errors + ["RAW_SOURCE_FILE_MISSING"]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc: return MISSING, errors + [f"V09_RAW_PAYLOAD_INVALID:{exc}"]
    if envelope.get("payload_bytes") != len(payload_raw) or envelope.get("payload_sha256") != digest(payload_raw): errors.append(f"RAW_SOURCE_HASH_MISMATCH:{envelope.get('capture_type', 'unknown')}")
    capture_type = str(envelope.get("capture_type", "")); schema = V09.get("capture_schemas", {}).get(capture_type, {})
    fields = schema.get("payload_required_exact", schema.get("payload_fields", [])) if isinstance(schema, dict) else []
    if not isinstance(payload, dict) or set(payload) != set(fields): errors.append(f"V09_CAPTURE_SCHEMA_MISMATCH:{capture_type}")
    def scan(value: object) -> None:
        if isinstance(value, dict):
            errors.extend(f"V09_FORBIDDEN_SENTINEL:{k}" for k in value if str(k).lower() in FORBIDDEN)
            for child in value.values(): scan(child)
        elif isinstance(value, list):
            for child in value: scan(child)
    scan(payload)
    return payload, errors


def _observation(root: Path, node: str, suite: str, run_id: str | None) -> tuple[dict[str, Any] | None, list[str]]:
    paths = sorted(root.glob("*.json")) if root.is_dir() else []
    matches = []
    for path in paths:
        try: value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError): continue
        if isinstance(value, dict) and value.get("nodeid") == node: matches.append((path, value))
    if len(matches) != 1: return None, [f"V09_OBSERVATION_CARDINALITY:{suite}:{node}"]
    path, value = matches[0]; errors: list[str] = []
    if value.get("schema") != "super1-observation/v09" or value.get("schema_version") != 9: errors.append("V09_OBSERVATION_SCHEMA")
    if value.get("suite") != suite or (run_id is not None and value.get("run_id") != run_id): errors.append("RAW_IDENTITY_MISMATCH:observation")
    binding = V09_BINDINGS.get(node, {}); expected_types = binding.get("required_capture_types", [])
    if value.get("capture_types") != expected_types: errors.append("V09_CAPTURE_SET_MISMATCH")
    raw_sources = value.get("raw_sources")
    if not isinstance(raw_sources, dict) or set(raw_sources) != set(expected_types): return value, errors + ["V09_CAPTURE_SET_MISMATCH"]
    identity = {field: str(value.get(field) or "") for field in ("run_id", "suite", "nodeid", "attempt_id", "scenario_nonce", "checkpoint_id")}
    payloads: dict[str, tuple[object, object]] = {}
    for capture_type in expected_types:
        source = raw_sources.get(capture_type); pairs: list[object] = []
        if not isinstance(source, dict): errors.append("V09_RAW_SOURCE_DECLARATION"); continue
        if source.get("resource_instance_id") != source.get("before", {}).get("resource_instance_id") or source.get("resource_instance_id") != source.get("after", {}).get("resource_instance_id"): errors.append("V09_RESOURCE_ID_MISMATCH")
        for phase in ("before", "after"):
            ref = source.get(phase)
            if not isinstance(ref, dict): errors.append("V09_RAW_SOURCE_DECLARATION"); continue
            payload, child_errors = _capture_payload(root, ref, identity); errors.extend(child_errors); pairs.append(payload)
        if len(pairs) == 2: payloads[capture_type] = (pairs[0], pairs[1])
    return {"path": path, "payload": value, "payloads": payloads}, errors


def _derived(payloads: dict[str, tuple[object, object]], assertion: dict[str, Any]) -> object:
    derivation = assertion.get("derivation", {}); algorithm = derivation.get("algorithm")
    types = derivation.get("source_capture_types", [])
    selected = [(name, payloads[name]) for name in types if name in payloads]
    if not selected: return MISSING
    if algorithm == "COUNT_SDK_PENDING_DELTA" or algorithm == "COUNT_SDK_REMOVE_DELTA":
        before, after = next((pair for name, pair in selected if name == "sdk_call_trace"), (MISSING, MISSING))
        if not isinstance(before, dict) or not isinstance(after, dict): return MISSING
        bc = before.get("calls"); ac = after.get("calls")
        if not isinstance(bc, list) or not isinstance(ac, list): return MISSING
        needle = "PENDING" if algorithm.endswith("PENDING_DELTA") else "REMOVE"
        return sum(needle in str(row.get("action", "")).upper() for row in ac[len(bc):] if isinstance(row, dict))
    if algorithm == "COUNT_RESTART_CALLBACK_DELTA":
        before, after = selected[0][1]
        def count(value: object) -> int: return sum("STARTOLD" in str(v).upper() for v in (value if isinstance(value, list) else []))
        return count(_find(after, "ordered_trace")) - count(_find(before, "ordered_trace"))
    if assertion.get("op") in {"UNCHANGED", "CHANGED"}:
        before, after = selected[0][1]
        return before == after if assertion.get("op") == "UNCHANGED" else before != after
    key = str(assertion.get("left", "")).rsplit("/", 1)[-1]
    for _, pair in selected:
        value = _find(pair[1], key)
        if value is not MISSING: return value
    if algorithm == "COMPARE_CANONICAL_RESOURCE_SNAPSHOTS":
        return selected[0][1][0] == selected[0][1][1]
    return MISSING


def _evaluate(node: str, payloads: dict[str, tuple[object, object]]) -> tuple[list[dict[str, Any]], list[str]]:
    binding = V09_BINDINGS.get(node, {}); rows = []; errors = []
    for assertion in binding.get("assertions", []):
        derived = _derived(payloads, assertion); expected = assertion.get("right", MISSING); op = assertion.get("op")
        if op in {"EQ_LITERAL", "EQ_REF", "EQ_REF"}:
            ok = derived is not MISSING and derived == expected
        elif op in {"NE_LITERAL", "NE_REF"}: ok = derived is not MISSING and derived != expected
        elif op == "UNCHANGED": ok = derived is True
        elif op == "CHANGED": ok = derived is True
        elif op in {"COUNT_EQ", "COUNT_DELTA_EQ", "SET_EQ_LITERAL", "SEQUENCE_EQ_LITERAL", "SHA_EQ", "SHA_NE", "LT", "LE", "GT", "GE", "EXISTS", "ABSENT"}: ok = False
        else: ok = False
        row = {"rule_id": assertion.get("derivation", {}).get("rule_id"), "op": op, "left": assertion.get("left"), "expected": expected if expected is not MISSING else None, "derived": None if derived is MISSING else derived, "status": "PASS" if ok else "FAIL"}
        rows.append(row)
        if not ok: errors.append(f"V09_SEMANTIC_CHECK_FAILED:{row['rule_id']}")
    return rows, errors


def build_report(junit_paths: list[Path], observation_roots: list[Path] | None = None, relative_to: Path | None = None, expected_run_id: str | None = None) -> dict[str, Any]:
    observed = parse_junit(junit_paths); roots = list(observation_roots or []); errors = list(_ARCHITECT_ERRORS); entries = {}; semantic_results = {}
    for code, definition in REQUIRED.items():
        nodes = definition["nodes"]; behavior = definition["behavior_nodes"]; present = [node for node in nodes if node in observed]
        failed = [node for node in present if any(s != "PASS" for s in observed[node]["statuses"])]
        node_errors = []
        for node in behavior:
            for suite, root in zip(("targeted", "full"), roots[:2]):
                card, card_errors = _observation(root, node, suite, expected_run_id)
                if card is not None:
                    projections, semantic_errors = _evaluate(node, card.get("payloads", {})); semantic_results[f"{suite}:{node}"] = {"assertions": projections, "errors": semantic_errors, "status": "PASS" if not semantic_errors and not card_errors else "FAIL"}
                    errors.extend(card_errors)
                node_errors.extend(card_errors + (semantic_errors if card is not None else []))
        missing = [node for node in nodes if node not in observed]
        junit_incomplete = [node for node in nodes if node not in observed or observed[node]["statuses"] != ["PASS"] * len(junit_paths)]
        node_errors.extend(junit_incomplete)
        entries[code] = {"code": code, "required_node_ids": nodes, "behavior_required_node_ids": behavior, "evidence_negative_required_node_ids": definition["evidence_negative_nodes"], "observed_node_ids": present, "missing_node_ids": missing, "failed_node_ids": failed, "junit_incomplete_node_ids": junit_incomplete, "actual": "PASS" if not node_errors else "FAIL", "semantic_errors": node_errors}
    semantic_ok = bool(roots) and len(semantic_results) == len(BEHAVIOR_REQUIRED_NODES) * min(2, len(roots)) and all(item["status"] == "PASS" for item in semantic_results.values())
    return {"schema_version": 9, "generator": "scripts/super1_evidence_report.py", "junit_paths": [str(p.resolve()) for p in junit_paths], "observation_roots": [str(p.resolve()) for p in roots], "observed_node_count": len(observed), "observed_nodes": observed, "entries": entries, "semantic_results": semantic_results, "semantic_acceptance": semantic_ok, "required_mapping_count": EXPECTED_COUNTS["required_mapping_count"], "unique_required_node_count": EXPECTED_COUNTS["unique_required_node_count"], "behavior_required_node_count": EXPECTED_COUNTS["behavior_required_node_count"], "evidence_negative_required_node_count": EXPECTED_COUNTS["evidence_negative_required_node_count"], "behavior_observation_node_count_per_suite": EXPECTED_COUNTS["behavior_observation_count_per_suite"], "behavior_observation_node_count_two_suites": EXPECTED_COUNTS["behavior_observation_count_two_suites"], "required_node_manifest": {"path": "inputs/architect/SUPER1_REQUIRED_NODE_MANIFEST_V08_20260901.json", "sha256": V08_SHA256}, "semantic_contract": {"path": "inputs/architect/SUPER1_SEMANTIC_CONTRACT_V09_20260902.json", "sha256": V09_SHA256}, "observation_errors": errors, "all_required_nodes_observed": ALL_REQUIRED_NODES.issubset(observed) and not errors}


def _validate_observation(item: dict[str, Any] | None, *, node: str, suite: str, expected_run_id: str | None = None) -> list[str]:
    if item is None:
        return ["V09_OBSERVATION_MISSING"]
    root = Path(item.get("root") or Path(item["path"]).parent)
    _, errors = _observation(root, node, suite, expected_run_id)
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--junit", nargs="+", type=Path, required=True); parser.add_argument("--observation-root", nargs="+", type=Path, default=[]); parser.add_argument("--relative-to", type=Path); parser.add_argument("--run-id"); parser.add_argument("--output", type=Path, required=True); args = parser.parse_args()
    report = build_report(args.junit, args.observation_root, args.relative_to, args.run_id); args.output.parent.mkdir(parents=True, exist_ok=True); args.output.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"); print(json.dumps({"output": str(args.output.resolve()), "observed_node_count": report["observed_node_count"], "semantic_acceptance": report["semantic_acceptance"]})); return 0 if report["all_required_nodes_observed"] else 1


if __name__ == "__main__": raise SystemExit(main())
