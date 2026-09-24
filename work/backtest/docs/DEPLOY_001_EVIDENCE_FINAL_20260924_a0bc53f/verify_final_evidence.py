from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from pathlib import PurePosixPath
import re
import xml.etree.ElementTree as ET


RISK_NODES = {
    "R1": [
        "tests/test_live_risk_guard.py::test_guard_approves_and_creates_order_only_after_live_facts_are_checked",
        "tests/test_live_risk_guard.py::test_guard_denies_missing_approval_health_contract_or_budget",
        "tests/test_super1_risk_e2e.py::test_production_flow_stale_snapshot_is_no_send",
    ],
    "R2": [
        "tests/test_live_retry_policy.py::test_read_retries_at_most_five_with_full_jitter_and_no_write_retry",
        "tests/test_live_retry_policy.py::test_retry_after_is_clamped_and_auth_contract_errors_are_not_retried",
        "tests/test_super1_instruction_1_11.py::test_retry_only_retries_explicit_read_transport_and_write_is_once",
    ],
    "R3": [
        "tests/test_instrument_contract.py::test_registry_requires_exact_case_sensitive_symbol_and_metadata",
        "tests/test_instrument_contract.py::test_semantic_metadata_and_economic_probe_fail_closed",
        "tests/test_super1_instruction_1_11.py::test_symbol_metadata_tick_and_economic_fingerprint_are_exact",
    ],
    "R4": [
        "tests/test_evaluation_window.py::test_warmup_is_available_to_source_but_not_evaluation",
        "tests/test_evaluation_window.py::test_dates_are_inclusive_and_outside_trades_fail",
        "tests/test_optimization_walk_forward.py::test_walk_forward_selects_on_train_and_reports_oos_only",
    ],
    "R5": [
        "tests/test_order_approval.py::test_approval_is_lease_bound_and_expires_at_proposal_or_ttl",
        "tests/test_order_approval.py::test_approval_rejects_staged_proposal_without_exact_wire_request_hash",
        "tests/test_order_approval.py::test_proposal_is_single_lifecycle_and_consumption_is_replay_safe",
        "tests/test_production_order_flow.py::test_actual_super1_production_flow_sends_once_then_reconciles_fake_mt5",
    ],
    "R6": [
        "tests/test_optimization_walk_forward.py::test_optimization_is_deterministic_and_research_only",
        "tests/test_optimization_walk_forward.py::test_walk_forward_selects_on_train_and_reports_oos_only",
        "tests/test_risk_causality.py::test_future_loss_cannot_suppress_earlier_pair_entry",
        "tests/test_risk_causality.py::test_realized_loss_suppresses_later_pair_entry",
    ],
    "R7": [
        "tests/test_risk_xray.py::test_risk_xray_is_fill_based_and_includes_zero_trade_eligible_days",
        "tests/test_risk_xray.py::test_no_trades_and_no_gross_loss_are_null_not_zero_or_nonfinite",
        "tests/test_risk_xray.py::test_risk_xray_rejects_closed_fill_without_terminal_time",
    ],
    "R8": [
        "tests/test_strategy_health.py::test_strategy_health_is_monotonic_and_decay_requires_checkpoint",
        "tests/test_strategy_health.py::test_insufficient_baseline_blocks_promotion_and_severe_dd_decays_once",
        "tests/test_strategy_health.py::test_decayed_disables_only_after_owned_exposure_is_flat",
    ],
    "R9": [
        "tests/test_runtime_settings.py::test_settings_are_typed_mode_aware_and_secret_safe",
        "tests/test_runtime_settings.py::test_demo_order_missing_required_settings_fails_closed",
        "tests/test_environment_schema_scanner.py::test_nested_scan_finds_unknown_key_in_repository_root_workflow",
        "tests/test_environment_schema_scanner.py::test_source_and_ci_reads_outside_schema_are_reported",
    ],
    "R10": [
        "tests/test_financial_numeric_contracts.py::test_financial_edge_cases_fail_closed",
        "tests/test_financial_numeric_contracts.py::test_decimal_quantization_never_increases_volume_risk",
    ],
    "R11": [
        "tests/test_backtest_sandbox.py::test_real_cli_backtest_publishes_nonempty_attested_artifacts",
        "tests/test_backtest_sandbox.py::test_real_appcontainer_enforces_filesystem_boundaries",
        "tests/test_backtest_sandbox.py::test_real_appcontainer_enforces_network_boundary",
        "tests/test_backtest_sandbox.py::test_real_appcontainer_enforces_process_boundary",
        "tests/test_backtest_sandbox.py::test_real_appcontainer_rejects_symlink_escape",
        "tests/test_backtest_sandbox.py::test_real_appcontainer_rejects_junction_escape",
        "tests/test_backtest_sandbox.py::test_real_appcontainer_rejects_hardlink_escape",
    ],
}


def junit_key(node_id: str) -> tuple[str, str]:
    file_part, _, rest = node_id.partition("::")
    parts = rest.split("::")
    classname = file_part[:-3].replace("/", ".")
    if len(parts) > 1:
        classname += "." + ".".join(parts[:-1])
    return classname, parts[-1]


def verify_artifact_bindings(evidence_root: Path) -> int:
    root = evidence_root.resolve(strict=True)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("evidence manifest must contain an artifacts list")

    listed: set[str] = set()
    for item in artifacts:
        relative = str(item.get("path", ""))
        parts = relative.split("/")
        if not relative or "\\" in relative or any(part in {"", ".", ".."} for part in parts):
            raise ValueError(f"unsafe evidence artifact path: {relative!r}")
        posix_path = PurePosixPath(relative)
        if posix_path.is_absolute():
            raise ValueError(f"absolute evidence artifact path: {relative!r}")
        if relative in listed:
            raise ValueError(f"duplicate evidence artifact path: {relative}")
        listed.add(relative)

        artifact_path = root.joinpath(*posix_path.parts)
        resolved_path = artifact_path.resolve(strict=True)
        try:
            resolved_path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"evidence artifact escapes package root: {relative}") from exc
        if artifact_path.is_symlink() or not artifact_path.is_file():
            raise ValueError(f"evidence artifact is not a regular file: {relative}")
        payload = artifact_path.read_bytes()
        if len(payload) != item.get("bytes"):
            raise ValueError(f"evidence artifact size mismatch: {relative}")
        digest = hashlib.sha256(payload).hexdigest()
        if digest != item.get("sha256") or digest != item.get("git_blob_sha256"):
            raise ValueError(f"evidence artifact hash mismatch: {relative}")

    present = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    present.discard("manifest.json")
    if present != listed:
        missing = sorted(listed - present)
        unlisted = sorted(present - listed)
        raise ValueError(f"evidence artifact inventory mismatch: missing={missing}, unlisted={unlisted}")
    return len(listed)


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare pytest collection/JUnit nodes and risk-matrix nodes.")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--collection", type=Path, required=True)
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--deploy-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    verify_artifact_bindings(args.collection.parent)
    collection_bytes = args.collection.read_bytes()
    collection_text = collection_bytes.decode("utf-8-sig")
    nodes = [line for line in collection_text.splitlines() if re.match(r"^tests/\S+\.py::.+$", line)]
    expected = Counter(junit_key(node) for node in nodes)
    root = ET.parse(args.junit).getroot()
    cases = root.findall(".//testcase")
    actual = Counter((item.get("classname", ""), item.get("name", "")) for item in cases)
    passed = Counter(
        (item.get("classname", ""), item.get("name", ""))
        for item in cases
        if item.find("failure") is None and item.find("error") is None and item.find("skipped") is None
    )

    missing_risk_nodes: list[str] = []
    for group in RISK_NODES.values():
        for node_id in group:
            if node_id not in nodes or passed[junit_key(node_id)] == 0:
                missing_risk_nodes.append(node_id)

    deploy_manifest = json.loads(args.deploy_manifest.read_text(encoding="utf-8"))
    target_run = next(run for run in deploy_manifest["runs"] if run.get("suite") == "targeted")
    target_files = re.findall(r"tests/[A-Za-z0-9_./]+\.py", target_run["command"])
    target_nodes = [node for node in nodes if any(node.startswith(path + "::") for path in target_files)]
    sandbox = [case for case in cases if case.get("classname") == "tests.test_backtest_sandbox"]
    evidence = [case for case in cases if case.get("classname") == "tests.test_deploy_001_evidence_manifest"]
    failed = len(root.findall(".//failure"))
    errors = len(root.findall(".//error"))
    skipped = len(root.findall(".//skipped"))
    deselected = sum(int(value) for value in re.findall(r"([0-9]+) deselected", collection_text))
    status = "PASS" if (
        expected == actual
        and len(nodes) == len(set(nodes))
        and not (failed or errors or skipped or deselected or missing_risk_nodes)
        and len(target_nodes) >= 225
        and len(sandbox) == 20
        and len(evidence) == 5
    ) else "FAIL"
    result = {
        "status": status,
        "collection_nodes": len(nodes),
        "collection_unique_nodes": len(set(nodes)),
        "junit_cases": len(cases),
        "junit_unique_cases": len(actual),
        "collection_junit_multiset_match": expected == actual,
        "failures": failed,
        "errors": errors,
        "skipped": skipped,
        "deselected": deselected,
        "collection_node_sha256_sorted_lf": hashlib.sha256("\n".join(sorted(nodes)).encode()).hexdigest(),
        "collection_log_sha256": hashlib.sha256(collection_bytes).hexdigest(),
        "junit_sha256": hashlib.sha256(args.junit.read_bytes()).hexdigest(),
        "legacy_deploy_target_file_count": len(target_files),
        "legacy_deploy_target_nodes_in_full_suite": len(target_nodes),
        "legacy_deploy_target_baseline_nodes": int(target_run["passed"]),
        "sandbox_nodes": len(sandbox),
        "sandbox_failed_error_skipped": sum(case.find("failure") is not None or case.find("error") is not None or case.find("skipped") is not None for case in sandbox),
        "evidence_verifier_nodes": len(evidence),
        "evidence_verifier_failed_error_skipped": sum(case.find("failure") is not None or case.find("error") is not None or case.find("skipped") is not None for case in evidence),
        "selected_risk_nodes": {key: len(value) for key, value in RISK_NODES.items()},
        "selected_risk_node_count": sum(map(len, RISK_NODES.values())),
        "missing_or_not_passed": missing_risk_nodes,
        "owner_symlink_test_present_and_passed": "tests/test_backtest_sandbox.py::test_real_appcontainer_rejects_symlink_escape" in nodes and junit_key("tests/test_backtest_sandbox.py::test_real_appcontainer_rejects_symlink_escape") in passed,
    }
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
