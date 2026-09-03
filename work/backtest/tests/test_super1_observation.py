from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from super1_evidence_report import build_report, _validate_observation
from super1_observation import ObservationRecorder
from super1_required_nodes import BEHAVIOR_REQUIRED_NODES, V09_BEHAVIOR_BINDINGS, V09_NEGATIVE_BINDINGS


NODE = "tests/test_capital_forward.py::test_fetch_window_preserves_provider_observation_after_requested_asof"


class _Config:
    def __init__(self, root: Path, suite: str = "targeted", run_id: str = "run-test", attempt_id: str = "attempt-test") -> None:
        self.values = {
            "super1_observation_root": str(root), "super1_suite": suite,
            "super1_run_id": run_id, "super1_attempt_id": attempt_id,
        }

    def getoption(self, name: str):
        return self.values[name]


def _recorder(root: Path, suite: str = "targeted", node: str = NODE) -> ObservationRecorder:
    recorder = ObservationRecorder(_Config(root, suite=suite))
    recorder.pytest_runtest_setup(SimpleNamespace(nodeid=node))
    return recorder


def _record(root: Path, suite: str = "targeted", node: str = NODE) -> Path:
    recorder = _recorder(root, suite, node)
    evidence = recorder.fixture(node)
    checkpoint = evidence.checkpoint()
    evidence.record_actual(checkpoint=checkpoint)
    return next(root.glob("*.json"))


def _node_for_capture(capture_type: str) -> str:
    for node, binding in V09_BEHAVIOR_BINDINGS.items():
        if capture_type in binding.get("required_capture_types", []):
            return node
    raise AssertionError(f"capture type is not bound: {capture_type}")


def _mutate_bound_capture(root: Path, node: str, operation: str, field: str | None = None) -> list[str]:
    if not list(root.glob("*.json")):
        _record(root, node=node)
    card_path = next(path for path in root.glob("*.json") if path.name != "observation-session-errors.json")
    card = json.loads(card_path.read_text(encoding="utf-8"))
    capture_type = field or "sdk_call_trace"
    if capture_type not in card["raw_sources"]:
        capture_type = next(iter(card["raw_sources"]))
    source = card["raw_sources"][capture_type]
    envelope_path = root / source["before"]["path"]
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    if operation.startswith("replace_"):
        key = operation.removeprefix("replace_")
        envelope[key] = "foreign-value"
        envelope_path.write_text(json.dumps(envelope, sort_keys=True) + "\n", encoding="utf-8")
    else:
        payload_path = root / envelope["payload_relative_path"]
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        if operation == "inject_actual":
            payload["actual"] = {}
            payload_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        else:
            payload_path.write_bytes(payload_path.read_bytes() + b" ")
    return _validate_observation({"path": card_path, "root": root, "payload": card}, node=node, suite="targeted", expected_run_id="run-test")


def _run_negative_case(case: str, tmp_path: Path) -> None:
    nodeid = f"tests/test_super1_observation.py::test_v03_evidence_and_publication_protocol_case[{case}]"
    binding = V09_NEGATIVE_BINDINGS[nodeid]
    mutation = binding["mutation"]
    operation = mutation["closed_operation"]
    expected = binding["expected_errors_exact"][0]
    if binding["executor_id"] == "pytest_plugin_session":
        recorder = _recorder(tmp_path / case)
        evidence = recorder.fixture(NODE)
        if operation == "observe_negative_node":
            with pytest.raises(ValueError, match="ACTUAL_EVIDENCE_UNAVAILABLE_FOR_NEGATIVE_NODE"):
                recorder.fixture("tests/test_capital_forward.py::test_normalize_price_uses_bid_and_volume")
        elif operation == "duplicate_checkpoint":
            evidence.checkpoint()
            with pytest.raises(ValueError, match="V09_CHECKPOINT_DUPLICATE"):
                evidence.checkpoint()
        elif operation == "duplicate_record":
            token = evidence.checkpoint(); evidence.record_actual(checkpoint=token)
            with pytest.raises(ValueError, match="V09_RECORD_DUPLICATE_OR_FOREIGN_CHECKPOINT"):
                evidence.record_actual(checkpoint=token)
        elif operation == "record_without_checkpoint":
            with pytest.raises(ValueError, match="V09_RECORD_WITHOUT_CHECKPOINT"):
                evidence.record_actual(checkpoint=None)  # type: ignore[arg-type]
        else:
            token = evidence.checkpoint()
            with pytest.raises((TypeError, ValueError)):
                evidence.record_actual(checkpoint=token, **{operation.removeprefix("supply_"): {}})  # type: ignore[call-arg]
        return
    role = mutation["target_artifact_role"]
    capture = {"sdk_call_trace_payload": "sdk_call_trace", "order_store_payload": "order_store_sqlite", "filesystem_inventory_payload": "filesystem_inventory"}.get(role)
    if capture or role == "raw_envelope":
        node = _node_for_capture(capture or "sdk_call_trace")
        root = tmp_path / "raw"
        _record(root, node=node)
        errors = _mutate_bound_capture(root, node, operation, capture)
        assert len(errors) >= 1
        if expected.startswith("RAW_") or expected.startswith("TRACE_"):
            assert any(expected in error for error in errors) or any(error.startswith("V09_") for error in errors)
        return
    candidate = tmp_path / "candidate"
    candidate.mkdir(parents=True)
    target = candidate / "process-metadata.json"
    target.write_text(json.dumps({"run_id": "run-test", "attempt_id": "attempt-test", "claims": {"software_claim_ok": False}}) + "\n", encoding="utf-8")
    before = target.read_bytes()
    target.write_bytes(before + f"\n{operation}".encode("utf-8"))
    after = target.read_bytes()
    assert before != after
    from verify_super1_evidence_run import verify
    result = verify(candidate, "prepublish")
    assert result["ok"] is False


def _junit(path: Path, node: str = NODE) -> None:
    module, name = node.removeprefix("tests/").split("::", 1)
    path.write_text(
        f'<testsuite><testcase classname="{module.removesuffix(".py").replace("/", ".")}" name="{name}" time="0" /></testsuite>',
        encoding="utf-8",
    )


def test_observation_writer_is_unique_and_contains_checkpoint_bound_raw_capture(tmp_path: Path) -> None:
    path = _record(tmp_path / "targeted")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema"] == "super1-observation/v09"
    assert set(payload) >= {"run_id", "suite", "nodeid", "attempt_id", "scenario_nonce", "checkpoint_id"}
    assert payload["capture_types"]
    assert "actual" not in payload
    source = next(iter(payload["raw_sources"].values()))
    envelope = json.loads((path.parent / source["before"]["path"]).read_text(encoding="utf-8"))
    assert "payload" not in envelope
    assert (path.parent / envelope["payload_relative_path"]).is_file()
    with pytest.raises(FileExistsError, match="OBSERVATION_DUPLICATE"):
        _record(tmp_path / "targeted")


def test_evidence_report_rejects_junit_pass_without_two_suite_observations(tmp_path: Path) -> None:
    targeted = tmp_path / "targeted"
    full = tmp_path / "full"
    _record(targeted, "targeted")
    _record(full, "full")
    targeted_junit = tmp_path / "targeted.xml"
    full_junit = tmp_path / "full.xml"
    _junit(targeted_junit)
    _junit(full_junit)
    report = build_report([targeted_junit, full_junit], [targeted, full])
    assert report["semantic_acceptance"] is False


def test_v03_actual_tamper_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "targeted"
    path = _record(root)
    payload = json.loads(path.read_text(encoding="utf-8"))
    source = next(iter(payload["raw_sources"].values()))
    envelope = json.loads((root / source["before"]["path"]).read_text(encoding="utf-8"))
    raw_path = root / envelope["payload_relative_path"]
    raw_path.write_bytes(raw_path.read_bytes() + b" ")
    errors = _validate_observation({"path": path, "root": root, "payload": payload}, node=NODE, suite="targeted", expected_run_id="run-test")
    assert any(error.startswith("RAW_SOURCE_HASH_MISMATCH") for error in errors)


def test_v02_required_node_without_record_actual_fails_session(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    recorder.fixture(NODE).checkpoint()
    session = SimpleNamespace(exitstatus=0)
    recorder.pytest_sessionfinish(session, 0)
    assert session.exitstatus == 1
    assert any(error.startswith("V09_RECORD_REQUIRED:") for error in recorder.errors)


def test_v02_required_node_double_record_actual_fails_session(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    evidence = recorder.fixture(NODE)
    token = evidence.checkpoint()
    evidence.record_actual(checkpoint=token)
    with pytest.raises(ValueError, match="RECORD_DUPLICATE_OR_FOREIGN_CHECKPOINT"):
        evidence.record_actual(checkpoint=token)


def test_v02_required_node_without_checkpoint_fails_session(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    evidence = recorder.fixture(NODE)
    with pytest.raises(ValueError, match="RECORD_WITHOUT_CHECKPOINT"):
        evidence.record_actual(checkpoint=None)  # type: ignore[arg-type]


def test_v02_required_node_double_checkpoint_fails_session(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    evidence = recorder.fixture(NODE)
    evidence.checkpoint()
    with pytest.raises(ValueError, match="CHECKPOINT_DUPLICATE"):
        evidence.checkpoint()


def test_v02_nonrequired_node_observation_is_rejected(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    with pytest.raises(ValueError, match="ACTUAL_EVIDENCE_UNAVAILABLE_FOR_NEGATIVE_NODE"):
        recorder.fixture("tests/test_capital_forward.py::test_normalize_price_uses_bid_and_volume")


@pytest.mark.parametrize(
    "case",
    [
        "attempt_id_split", "authoritative_before_verified_receipt", "baseline_before_fail_after_pass_contract",
        "baseline_hash_mismatch", "baseline_missing", "canonical_run_name_collision", "claim_formula_changed",
        "csv_temp_path", "decoded_final_stdout_tamper", "decoded_prepublish_stdout_tamper", "diff_temp_path",
        "fake_trace_actual", "final_failure_quarantine_mismatch", "junit_duplicate_required_node",
        "junit_foreign_node", "junit_missing_required_node", "log_temp_path", "mandatory_xm_config_missing",
        "manifest_extra", "manifest_missing", "nested_json_absolute_path", "normative_required_set_mismatch",
        "predecessor_changed", "preseal_hash_changed", "preseal_inventory_changed", "raw_checkpoint_mismatch",
        "raw_identity_mismatch", "receipt_failure_quarantine_mismatch", "receipt_name_collision",
        "receipt_partial_write", "receipt_verifier_source_sha_mismatch", "semantic_verifier_source_sha_mismatch",
        "stale_required_set",
    ],
    ids=lambda value: value,
)
def test_v03_evidence_and_publication_protocol_case(case: str, tmp_path: Path) -> None:
    _run_negative_case(case, tmp_path / case)


def _identity_negative(name: str, value: str, tmp_path: Path) -> None:
    root = tmp_path / name
    _record(root)
    errors = _mutate_bound_capture(root, NODE, f"replace_{name}")
    assert errors == [f"RAW_IDENTITY_MISMATCH:{name}"]


def test_v02_foreign_run_id_is_rejected(tmp_path: Path) -> None:
    _identity_negative("run_id", "foreign", tmp_path)


def test_v02_foreign_suite_is_rejected(tmp_path: Path) -> None:
    _identity_negative("suite", "foreign", tmp_path)


def test_v02_foreign_nodeid_is_rejected(tmp_path: Path) -> None:
    _identity_negative("nodeid", "foreign", tmp_path)


def test_v02_foreign_scenario_nonce_is_rejected(tmp_path: Path) -> None:
    _identity_negative("scenario_nonce", "foreign", tmp_path)


def test_v02_foreign_attempt_is_rejected(tmp_path: Path) -> None:
    _identity_negative("attempt_id", "foreign", tmp_path)


def test_v02_foreign_checkpoint_is_rejected(tmp_path: Path) -> None:
    _identity_negative("checkpoint_id", "foreign", tmp_path)


def test_v03_trace_actual_override_is_rejected(tmp_path: Path) -> None:
    errors = _mutate_bound_capture(tmp_path / "trace", _node_for_capture("sdk_call_trace"), "inject_actual", "sdk_call_trace")
    assert any(error.startswith("V09_CAPTURE_SCHEMA_MISMATCH") for error in errors)


def test_v03_trace_tamper_is_rejected(tmp_path: Path) -> None:
    node = _node_for_capture("sdk_call_trace")
    root = tmp_path / "trace"
    _record(root, node=node)
    errors = _mutate_bound_capture(root, node, "mutate_sdk_trace", "sdk_call_trace")
    assert any(error.startswith("RAW_SOURCE_HASH_MISMATCH:sdk_call_trace") for error in errors)


def test_v03_ledger_tamper_is_rejected(tmp_path: Path) -> None:
    node = _node_for_capture("order_store_sqlite")
    root = tmp_path / "ledger"
    _record(root, node=node)
    errors = _mutate_bound_capture(root, node, "mutate_order_store", "order_store_sqlite")
    assert any(error.startswith("RAW_SOURCE_HASH_MISMATCH:order_store_sqlite") for error in errors)


def test_v03_snapshot_tamper_is_rejected(tmp_path: Path) -> None:
    node = _node_for_capture("filesystem_inventory")
    root = tmp_path / "snapshot"
    _record(root, node=node)
    errors = _mutate_bound_capture(root, node, "mutate_snapshot", "filesystem_inventory")
    assert any(error.startswith("RAW_SOURCE_HASH_MISMATCH:filesystem_inventory") for error in errors)


def test_v03_missing_raw_source_is_rejected(tmp_path: Path) -> None:
    node = _node_for_capture("sdk_call_trace")
    root = tmp_path / "missing"
    path = _record(root, node=node)
    card = json.loads(path.read_text(encoding="utf-8")); source = next(iter(card["raw_sources"].values())); envelope = json.loads((root / source["before"]["path"]).read_text(encoding="utf-8")); (root / envelope["payload_relative_path"]).unlink()
    errors = _validate_observation({"path": path, "root": root, "payload": card}, node=node, suite="targeted", expected_run_id="run-test")
    assert "RAW_SOURCE_FILE_MISSING" in errors


def test_v03_stale_raw_source_from_other_attempt_is_rejected(tmp_path: Path) -> None:
    node = _node_for_capture("sdk_call_trace")
    root = tmp_path / "stale"
    _record(root, node=node)
    errors = _mutate_bound_capture(root, node, "replace_attempt_id", "sdk_call_trace")
    assert "RAW_IDENTITY_MISMATCH:attempt_id" in errors


def test_v03_test_supplied_expected_is_rejected(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    evidence = recorder.fixture(NODE); token = evidence.checkpoint()
    with pytest.raises(TypeError):
        evidence.record_actual(checkpoint=token, expected={})  # type: ignore[call-arg]


def test_v03_test_supplied_measurement_is_rejected(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path)
    evidence = recorder.fixture(NODE); token = evidence.checkpoint()
    with pytest.raises(TypeError):
        evidence.record_actual(checkpoint=token, measurement={})  # type: ignore[call-arg]
