"""Execute the V09 negative bindings against fresh isolated candidate copies."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def dump(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)[:120]


def inventory(root: Path) -> list[dict[str, object]]:
    rows = []
    paths = []
    for base, directories, files in os.walk(root):
        directories[:] = [directory for directory in directories if directory != "negative-cases"]
        paths.extend(Path(base) / name for name in files)
    for path in sorted(paths):
        data = path.read_bytes()
        rows.append({"path": path.relative_to(root).as_posix(), "bytes": len(data), "sha256": sha(data)})
    return rows


def clone_isolated(source: Path, destination: Path) -> None:
    """Clone the fixture tree with hardlinks, detaching only mutated files later."""
    destination.mkdir(parents=True, exist_ok=False)
    for base, directories, files in os.walk(source):
        directories[:] = [directory for directory in directories if directory != "negative-cases"]
        for name in files:
            path = Path(base) / name
            relative = path.relative_to(source)
            if relative.name in {"preseal-integrity.json", "output-manifest.json", "receipt-candidate.json"}:
                continue
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(path, target)
            except OSError:
                shutil.copy2(path, target)
        for name in directories:
            relative = Path(base).relative_to(source) / name
            if relative.name in {"negative-cases"}:
                continue
            (destination / relative).mkdir(parents=True, exist_ok=True)
def detach(path: Path) -> None:
    if path.is_file():
        data = path.read_bytes()
        path.unlink()
        path.write_bytes(data)


def make_reduced_fixture(source: Path, destination: Path, capture_type: str | None, copy_top_level: bool) -> None:
    """Keep a real card/raw pair while avoiding a second full-campaign scan per case."""
    destination.mkdir(parents=True, exist_ok=False)
    if copy_top_level:
        # The semantic verifier's clean side must be a complete prepublish
        # candidate.  Hardlinking keeps each case cheap; mutate/detach below
        # breaks the link before changing one selected artifact.
        for base, directories, files in os.walk(source):
            directories[:] = [directory for directory in directories if directory != "negative-cases"]
            for name in files:
                relative = (Path(base) / name).relative_to(source)
                if relative.name in {"output-manifest.json", "receipt-candidate.json"}:
                    continue
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(Path(base) / name, target)
                except OSError:
                    shutil.copy2(Path(base) / name, target)
            for name in directories:
                (destination / (Path(base).relative_to(source) / name)).mkdir(parents=True, exist_ok=True)
        for name in ("SUPER1_REQUIRED_NODE_MANIFEST_V08_20260901.json", "SUPER1_SEMANTIC_CONTRACT_V09_20260902.json"):
            target = destination / "inputs" / "architect" / name
            if not target.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / "inputs" / "architect" / name, target)
        source_run_id = next((part for part in source.name.split(".") if part.startswith("run-")), source.name)
        first_card = next(iter(sorted((destination / "observations").glob("*/*.json"))), None)
        first_value = load(first_card) if first_card and first_card.name != "observation-session-errors.json" else {}
        process_path = destination / "process-metadata.json"
        if not process_path.is_file():
            dump(process_path, {"schema_version": 9, "run_id": source_run_id, "attempt_id": first_value.get("attempt_id", "negative"), "claims": {"software_claim_ok": False, "continuation_fixture_claim_ok": False, "evidence_payload_claim_ok": False, "local_acceptance_claim_ok": False}})
        required = {
            "acceptance-matrix.json", "baseline-validation.json", "calendar-validation.json", "claim-recomputation.json",
            "collection-inventory.json", "component-boundary-report.json", "continuation-contract-proposal.json", "continuation-dry-run.json",
            "evidence-report.json", "evidence-report.raw.json", "failed-sibling-integrity-before.json", "failed-sibling-integrity-after.json",
            "input-inventory-before.json", "input-inventory-after.json", "negative-mutation-results.json", "predecessor-integrity-before.json",
            "predecessor-integrity-after.json", "preseal-integrity.json", "publication-state.json", "readiness-report.json",
            "semantic-counterfactual-results.json", "technical-no-go.json", "v09-semantic-results.json",
        }
        for name in required:
            target = destination / name
            if not target.is_file():
                if name == "input-inventory-after.json" and (destination / "input-inventory-before.json").is_file():
                    shutil.copy2(destination / "input-inventory-before.json", target)
                elif name == "claim-recomputation.json":
                    claims = {
                        "software_claim_ok": False,
                        "continuation_fixture_claim_ok": False,
                        "evidence_payload_claim_ok": False,
                        "local_acceptance_claim_ok": False,
                    }
                    process = load(destination / "process-metadata.json")
                    claims = process.get("claims", claims) if isinstance(process, dict) else claims
                    dump(target, {
                        "schema_version": 9,
                        "run_id": source_run_id,
                        "attempt_id": first_value.get("attempt_id", "negative"),
                        "claims": claims,
                        "software_inputs": {"semantic_behavior_exact": False},
                    })
                else:
                    dump(target, {"schema_version": 9, "run_id": source_run_id, "attempt_id": first_value.get("attempt_id", "negative")})
        collection = destination / "collection-inventory.json"
        if not collection.is_file():
            dump(collection, {"schema_version": 9, "run_id": source_run_id, "attempt_id": first_value.get("attempt_id", "negative"), "nodeids": all_nodes, "count": len(all_nodes)})
        return
    cards = sorted((source / "observations").glob("*/*.json"))
    selected = None
    for card in cards:
        if card.name == "observation-session-errors.json":
            continue
        try:
            value = load(card)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        sources = value.get("raw_sources", {}) if isinstance(value, dict) else {}
        if capture_type is None or capture_type in sources:
            selected = (card, value)
            break
    if selected is None:
        raise FileNotFoundError("no observation card available for negative fixture")
    card, value = selected
    nodeid = str(value["nodeid"])
    manifest = load(source / "inputs" / "architect" / "SUPER1_REQUIRED_NODE_MANIFEST_V08_20260901.json")
    all_nodes = list(manifest.get("behavior_required_nodes", [])) + list(manifest.get("evidence_negative_required_nodes", []))
    for suite in ("targeted", "full"):
        target_suite = destination / "observations" / suite
        target_suite.mkdir(parents=True, exist_ok=True)
        source_suite = source / "observations" / suite
        if not source_suite.is_dir():
            source_suite = source / "observations" / str(value["suite"])
        matching_card = source_suite / card.name
        source_cards = [matching_card if matching_card.is_file() else card]
        for source_card in source_cards:
            if source_card.name == "observation-session-errors.json":
                continue
            target_card = target_suite / source_card.name
            target_card.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_card, target_card)
            card_value = load(source_card)
            for source_name, source_ref in card_value.get("raw_sources", {}).items():
                if capture_type and not copy_top_level and source_name != capture_type:
                    continue
                for phase in ("before", "after"):
                    ref = source_ref[phase]
                    for rel in (ref["path"], load(source_suite / ref["path"])["payload_relative_path"]):
                        src = source_suite / rel; dst = target_suite / rel; dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dst)
    junit_root = ET.Element("testsuite")
    for required_node in all_nodes:
        module, test_name = str(required_node).removeprefix("tests/").split("::", 1)
        ET.SubElement(junit_root, "testcase", classname=module.replace("/", ".").removesuffix(".py"), name=test_name)
    for name in ("targeted-pytest.xml", "full-pytest.xml"):
        (destination / name).write_bytes(ET.tostring(junit_root, encoding="utf-8"))
    if not copy_top_level:
        for path in (destination / "observations").rglob("*.json"):
            value = load(path)
            if isinstance(value, dict) and "run_id" in value:
                value["run_id"] = destination.name; dump(path, value)
    if copy_top_level:
        (destination / "inputs" / "architect").mkdir(parents=True, exist_ok=True)
        for name in ("SUPER1_REQUIRED_NODE_MANIFEST_V08_20260901.json", "SUPER1_SEMANTIC_CONTRACT_V09_20260902.json"):
            shutil.copy2(source / "inputs" / "architect" / name, destination / "inputs" / "architect" / name)
        for name in ("process-metadata.json", "baseline-validation.json", "readiness-report.json", "claim-recomputation.json", "evidence-report.json", "evidence-report.raw.json", "input-inventory-after.json", "predecessor-integrity-after.json", "technical-no-go.json", "collection-inventory.json", "publication-state.json", "acceptance-matrix.json", "calendar-validation.json", "continuation-contract-proposal.json", "continuation-dry-run.json", "failed-sibling-integrity-before.json", "failed-sibling-integrity-after.json", "input-inventory-before.json", "predecessor-integrity-before.json", "preseal-integrity.json", "semantic-counterfactual-results.json", "negative-mutation-results.json", "component-boundary-report.json", "v09-semantic-results.json"):
            path = source / name
            if path.is_file():
                shutil.copy2(path, destination / name)
        dump(destination / "collection-inventory.json", {"schema_version": 9, "nodeids": all_nodes, "count": len(all_nodes)})
        process_path = destination / "process-metadata.json"
        if not process_path.is_file():
            dump(process_path, {"schema_version": 9, "run_id": destination.name, "attempt_id": "negative", "claims": {"software_claim_ok": False, "continuation_fixture_claim_ok": False, "evidence_payload_claim_ok": False, "local_acceptance_claim_ok": False}})
        process = load(process_path); process["run_id"] = destination.name; dump(process_path, process)
        for path in destination.rglob("*.json"):
            if path == process_path or "inputs" in path.parts:
                continue
            try:
                value = load(path)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict) and "run_id" in value:
                value["run_id"] = destination.name; dump(path, value)


def observation_payload(case_root: Path, capture_type: str | None = None) -> tuple[Path, Path] | None:
    for card in sorted((case_root / "observations").glob("*/*.json")):
        if card.name == "observation-session-errors.json":
            continue
        try:
            value = load(card)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        sources = value.get("raw_sources", {}) if isinstance(value, dict) else {}
        for name, source in sources.items() if isinstance(sources, dict) else ():
            if capture_type and name != capture_type:
                continue
            if not isinstance(source, dict):
                continue
            for phase in ("before", "after"):
                ref = source.get(phase)
                if not isinstance(ref, dict):
                    continue
                envelope = case_root / "observations" / str(value.get("suite")) / str(ref.get("path"))
                if envelope.is_file():
                    try:
                        payload = case_root / "observations" / str(value.get("suite")) / str(load(envelope).get("payload_relative_path"))
                    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
                        continue
                    if payload.is_file():
                        return envelope, payload
    return None


def raw_capture_for_role(role: str) -> str | None:
    return {
        "sdk_call_trace_payload": "sdk_call_trace",
        "order_store_payload": "order_store_sqlite",
        "filesystem_inventory_payload": "filesystem_inventory",
    }.get(role)


def executor_source_sha256(executor: str) -> str:
    source = {
        "pytest_plugin_session": ROOT / "scripts" / "super1_observation.py",
        "reporter_cli": ROOT / "scripts" / "super1_evidence_report.py",
        "semantic_verifier_cli": ROOT / "scripts" / "verify_super1_evidence_run.py",
        "receipt_verifier_cli": ROOT / "scripts" / "verify_super1_receipt.py",
        "publication_runner_cli": ROOT / "scripts" / "run_super1_run010_evidence.py",
    }.get(executor)
    return sha(source.read_bytes()) if source and source.is_file() else ""


def mutate_json(path: Path, operation: str, target: str = "") -> None:
    value = load(path)
    if operation == "delete_baseline":
        path.unlink()
        return
    if not isinstance(value, dict):
        value = {"value": value}
    if operation == "delete_member":
        value.pop("files", None)
        dump(path, value)
        return
    if operation == "delete_xm_config_binding":
        rows = value.get("rows")
        if isinstance(rows, list):
            value["rows"] = [row for row in rows if not (isinstance(row, dict) and str(row.get("path", "")).replace("\\", "/") == "live_forward/xm_mt5_demo_config.json")]
        else:
            value["rows"] = []
        dump(path, value)
        return
    if operation == "split_attempt_id":
        value["attempt_id"] = str(value.get("attempt_id", "")) + "-split"
    elif operation == "flip_claim_input":
        value["claims"] = {"software_claim_ok": not bool(value.get("claims", {}).get("software_claim_ok", False))}
    elif operation == "mutate_baseline_hash":
        value.setdefault("source_root_sha256", {})["baseline_before_fix"] = "0" * 64
    elif operation == "remove_before_fail_after_pass":
        value["before_fail_after_pass_contract"] = "INVALID"
    elif operation == "mutate_inventory_sha":
        value.setdefault("inventory", [{}])[0]["sha256"] = "0" * 64
    elif operation == "mutate_inventory_member":
        value.setdefault("inventory", [{}])[0]["path"] = "mutated-member"
    elif operation == "inject_nested_absolute_path":
        value["mutation_path"] = str(Path.cwd().resolve() / "absolute.json")
    elif operation in {"inject_csv_temp_path", "inject_diff_temp_path", "inject_log_temp_path"}:
        value["mutation_path"] = str(Path.cwd().resolve() / (operation.removeprefix("inject_").removesuffix("_temp_path") + ".tmp"))
    elif operation == "replace_required_node":
        value["nodeids"] = ["tests/foreign.py::test_foreign"]
    elif operation == "add_extra_member":
        value["extra-member.json"] = {"mutation": True}
    elif operation == "mutate_predecessor_tree":
        value["tree_sha256"] = "0" * 64
    elif operation == "delete_required_raw":
        path.unlink()
        return
    elif operation == "replace_reported_actual":
        value["semantic_acceptance"] = True
    elif operation == "set_authoritative_early":
        value["authoritative"] = True
    elif operation == "mutate_receipt_verifier_sha":
        value["receipt_verifier_source_sha256"] = "0" * 64
    elif operation == "mutate_semantic_verifier_sha":
        value["verifier_source_sha256"] = "0" * 64
    elif operation == "mutate_final_stdout":
        value["final_verifier"] = {"stdout_base64": base64.b64encode(b'{"phase":"final","ok":true,"tampered":true}').decode()}
    elif operation == "mutate_prepublish_stdout":
        value["prepublish_verifier"] = {"stdout_base64": base64.b64encode(b'{"phase":"prepublish","ok":true,"tampered":true}').decode()}
    else:
        value["mutation"] = operation
    dump(path, value)


def mutate_junit(path: Path, operation: str) -> None:
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    if not cases:
        raise ValueError("JUNIT has no testcase")
    if operation == "delete_required_testcase":
        parent = next(parent for parent in root.iter() if cases[0] in list(parent))
        parent.remove(cases[0])
    elif operation == "duplicate_required_testcase":
        parent = next(parent for parent in root.iter() if cases[0] in list(parent))
        parent.append(ET.fromstring(ET.tostring(cases[0], encoding="unicode")))
    elif operation == "replace_required_with_foreign":
        cases[0].set("name", "test_foreign")
        cases[0].set("classname", "foreign")
    path.write_bytes(ET.tostring(root, encoding="utf-8"))


def choose_target(case_root: Path, binding: dict[str, object]) -> Path:
    mutation = binding.get("mutation", {})
    operation = str(mutation.get("closed_operation", "")) if isinstance(mutation, dict) else ""
    role = str(mutation.get("target_artifact_role", "")) if isinstance(mutation, dict) else ""
    if operation == "delete_baseline":
        return case_root / "baseline-validation.json"
    capture = raw_capture_for_role(role)
    if role == "raw_envelope" or capture or role == "raw_tree":
        # A raw_envelope binding intentionally leaves the capture type open;
        # selecting an unrelated SDK file would turn an identity mutation
        # into a no-op from the reporter's point of view.
        pair = observation_payload(case_root, None if role == "raw_envelope" else (capture or "sdk_call_trace"))
        if pair:
            return pair[0] if role == "raw_envelope" else pair[1]
    if "junit" in role:
        return case_root / "targeted-pytest.xml"
    by_role = {
        "fixture_source": "plugin-fixture.txt",
        "process_metadata": "process-metadata.json",
        "baseline_validation": "baseline-validation.json",
        "readiness_report": "readiness-report.json",
        "claim_recomputation": "claim-recomputation.json",
        "evidence_report": "evidence-report.json",
        "required_set_copy": "inputs/architect/SUPER1_REQUIRED_NODE_MANIFEST_V08_20260901.json",
        "preseal_integrity": "preseal-integrity.json",
        "predecessor_fixture": "predecessor-integrity-after.json",
        "receipt_candidate": "receipt-candidate.json",
        "output_manifest": "output-manifest.json",
        "input_inventory": "input-inventory-after.json",
        "json_artifact": "technical-no-go.json",
        "text_artifact": "negative-fixture.txt",
        "publication_filesystem": "publication-filesystem.json",
    }
    return case_root / by_role.get(role, "process-metadata.json")


def mutate_target(case_root: Path, binding: dict[str, object]) -> Path:
    mutation = binding.get("mutation", {})
    operation = str(mutation.get("closed_operation", "")) if isinstance(mutation, dict) else ""
    target = choose_target(case_root, binding)
    if not target.exists():
        if target.name == "output-manifest.json":
            dump(target, {"schema_version": 9, "run_id": case_root.name, "manifest_is_last_output": True, "files": []})
        else:
            dump(target, {"run_id": case_root.name, "attempt_id": "negative", "inventory": inventory(case_root)})
    if target.suffix.lower() == ".xml":
        mutate_junit(target, operation)
    elif target.suffix.lower() == ".json":
        if operation in {"mutate_sdk_trace", "mutate_order_store", "mutate_snapshot", "inject_actual"}:
            if operation == "inject_actual":
                value = load(target); value["actual"] = {}
                dump(target, value)
            else:
                target.write_bytes(target.read_bytes() + b" ")
        elif operation in {"replace_attempt_id", "replace_checkpoint_id", "replace_nodeid", "replace_run_id", "replace_scenario_nonce", "replace_suite", "replace_with_other_attempt"}:
            value = load(target)
            field = {"replace_attempt_id": "attempt_id", "replace_checkpoint_id": "checkpoint_id", "replace_nodeid": "nodeid", "replace_run_id": "run_id", "replace_scenario_nonce": "scenario_nonce", "replace_suite": "suite", "replace_with_other_attempt": "attempt_id"}[operation]
            value[field] = "foreign-attempt" if field == "attempt_id" else "foreign-" + field
            dump(target, value)
        else:
            mutate_json(target, operation, str(mutation.get("target_pointer_or_path", "")))
    else:
        suffix = {
            "inject_csv_temp_path": "csv.tmp",
            "inject_diff_temp_path": "diff.tmp",
            "inject_log_temp_path": "log.tmp",
        }.get(operation)
        marker = f"\nC:/workspace/.run-018.tmp-deadbeef/{suffix}\n".encode() if suffix else b"\nV09 mutation"
        target.write_bytes(target.read_bytes() + marker)
    return target


def invoke(executor: str, case_root: Path, nodeid: str, run_id: str, output_name: str, *, test_nodeid: str | None = None, phase: str = "clean") -> tuple[int, str, list[str]]:
    python = str(Path(sys.executable).resolve())
    if executor == "reporter_cli":
        output = case_root / output_name
        argv = [python, "scripts/super1_evidence_report.py", "--junit", str(case_root / "targeted-pytest.xml"), str(case_root / "full-pytest.xml"), "--observation-root", str(case_root / "observations" / "targeted"), str(case_root / "observations" / "full"), "--relative-to", str(case_root), "--run-id", run_id, "--output", str(output)]
    elif executor == "semantic_verifier_cli":
        argv = [python, "scripts/verify_super1_evidence_run.py", "--phase", "prepublish", "--run-root", str(case_root)]
    elif executor == "receipt_verifier_cli":
        argv = [python, "scripts/verify_super1_receipt.py", "--receipt-candidate", str(case_root / "receipt-candidate.json"), "--run-root", str(case_root)]
    elif executor == "pytest_plugin_session":
        argv = [python, "-m", "pytest", "-q", test_nodeid or nodeid]
    else:
        argv = [python, "scripts/run_super1_run010_evidence.py", "--output-base", str(case_root / f"publication-base-{safe_name(output_name)}"), "--publication-test-case", nodeid.rsplit("[", 1)[-1].rstrip("]")]
    environment = environment_snapshot()
    environment["SUPER1_NEGATIVE_PHASE"] = phase
    if executor == "pytest_plugin_session":
        environment["PYTHONPATH"] = str(ROOT / "scripts") + os.pathsep + environment.get("PYTHONPATH", "")
    result = subprocess.run(argv, cwd=ROOT, env=environment, text=True, capture_output=True, check=False)
    text = result.stdout + "\n" + result.stderr
    output = case_root / output_name
    if output.is_file():
        try:
            text += "\n" + json.dumps(load(output), sort_keys=True)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
    return result.returncode, text, argv


def ensure_receipt_fixture(case_root: Path) -> None:
    process_path = case_root / "process-metadata.json"
    if not process_path.is_file():
        dump(process_path, {
            "schema_version": 9,
            "run_id": case_root.name,
            "attempt_id": "negative",
            "claims": {
                "software_claim_ok": False,
                "continuation_fixture_claim_ok": False,
                "evidence_payload_claim_ok": False,
                "local_acceptance_claim_ok": False,
            },
        })
    acceptance_path = case_root / "acceptance-matrix.json"
    if not acceptance_path.is_file():
        dump(acceptance_path, {
            "schema_version": 9,
            "run_id": case_root.name,
            "attempt_id": "negative",
            "claims": load(process_path).get("claims", {}),
        })
    manifest = case_root / "output-manifest.json"
    files = [row for row in inventory(case_root) if row["path"] != "output-manifest.json"]
    dump(manifest, {"schema_version": 9, "run_id": case_root.name, "manifest_is_last_output": True, "files": files})
    clean = {"phase": "prepublish", "ok": True, "run_id": case_root.name}
    final = {"phase": "final", "ok": True, "run_id": case_root.name}
    process = load(process_path) if process_path.is_file() else {}
    attempt_id = process.get("attempt_id", "negative") if isinstance(process, dict) else "negative"
    receipt = {"schema_version": 9, "run_id": case_root.name, "attempt_id": attempt_id, "publication_status": "PUBLISHED", "authoritative": False, "final_path": str(case_root.resolve()), "manifest_sha256": sha(manifest.read_bytes()), "verified_member_count": len(files), "verifier_source_sha256": sha((ROOT / "scripts/verify_super1_evidence_run.py").read_bytes()), "receipt_verifier_source_sha256": sha((ROOT / "scripts/verify_super1_receipt.py").read_bytes()), "prepublish_verifier": {"exit_code": 0, "stdout_base64": base64.b64encode(json.dumps(clean, separators=(",", ":")).encode()).decode(), "stdout_sha256": sha(json.dumps(clean, separators=(",", ":")).encode())}, "final_verifier": {"exit_code": 0, "stdout_base64": base64.b64encode(json.dumps(final, separators=(",", ":")).encode()).decode(), "stdout_sha256": sha(json.dumps(final, separators=(",", ":")).encode())}, "software_candidate_ok": False, "continuation_fixture_candidate_ok": False, "evidence_payload_candidate_ok": False, "local_acceptance_candidate_ok": False, "local_acceptance_ok": False, "run_evidence_ok": False}
    dump(case_root / "receipt-candidate.json", receipt)


def execute(case_root: Path, nodeid: str, binding: dict[str, object], index: int) -> dict[str, object]:
    mutation = binding.get("mutation", {}) if isinstance(binding.get("mutation"), dict) else {}
    executor = str(binding.get("executor_id", ""))
    expected = list(binding.get("expected_errors_exact", []))
    case_label = safe_name(nodeid.rsplit("::", 1)[-1])
    work = case_root / "negative-cases" / f"run-018-negative-{index:03d}-{case_label}-{os.getpid()}"
    capture = raw_capture_for_role(str(mutation.get("target_artifact_role", "")))
    if executor in {"reporter_cli", "semantic_verifier_cli"}:
        make_reduced_fixture(case_root, work, capture, executor == "semantic_verifier_cli")
    elif executor == "pytest_plugin_session":
        clone_isolated(case_root, work)
        (work / "plugin-fixture.txt").write_text("clean\n", encoding="utf-8")
        plugin_test = work / "negative_plugin_case.py"
        operation = str(mutation.get("closed_operation", ""))
        plugin_test.write_text(
            "from pathlib import Path\n"
            "from types import SimpleNamespace\n"
            "import os\n"
            "from super1_observation import ObservationRecorder\n"
            "NODE = 'tests/test_capital_forward.py::test_fetch_window_preserves_provider_observation_after_requested_asof'\n"
            f"CASE_NODE = {nodeid!r}\n"
            f"OPERATION = {operation!r}\n"
            "class Config:\n"
            "    def getoption(self, name):\n"
            "        return {'super1_observation_root': str(Path(__file__).parent / 'plugin-observations'), 'super1_suite': 'targeted', 'super1_run_id': 'negative-run', 'super1_attempt_id': 'negative-attempt'}.get(name)\n"
            "def test_plugin_negative_binding():\n"
            "    recorder = ObservationRecorder(Config())\n"
            "    recorder.pytest_runtest_setup(SimpleNamespace(nodeid=NODE))\n"
            "    evidence = recorder.fixture(NODE)\n"
            "    if 'MUTATION' not in (Path(__file__).with_name('plugin-fixture.txt')).read_text(encoding='utf-8'):\n"
            "        token = evidence.checkpoint(); evidence.record_actual(checkpoint=token); return\n"
            "    if OPERATION == 'observe_negative_node':\n"
            "        recorder.fixture(CASE_NODE)\n"
            "    elif OPERATION == 'duplicate_checkpoint':\n"
            "        evidence.checkpoint(); evidence.checkpoint()\n"
            "    elif OPERATION == 'duplicate_record':\n"
            "        token = evidence.checkpoint(); evidence.record_actual(checkpoint=token); evidence.record_actual(checkpoint=token)\n"
            "    elif OPERATION == 'record_without_checkpoint':\n"
            "        evidence.record_actual(checkpoint=None)\n"
            "    elif OPERATION == 'omit_record':\n"
            "        evidence.checkpoint(); session = SimpleNamespace(exitstatus=0); recorder.pytest_sessionfinish(session, 0); raise RuntimeError(recorder.errors[0])\n"
            "    elif OPERATION in {'supply_expected', 'supply_measurement'}:\n"
            "        token = evidence.checkpoint()\n"
            "        try:\n"
            "            evidence.record_actual(checkpoint=token, **{OPERATION.removeprefix('supply_'): {}})\n"
            "        except TypeError:\n"
            "            raise RuntimeError('TEST_SUPPLIED_EXPECTED_FORBIDDEN' if OPERATION == 'supply_expected' else 'TEST_SUPPLIED_MEASUREMENT_FORBIDDEN')\n"
            "    else:\n"
            "        raise RuntimeError('UNSUPPORTED_PLUGIN_NEGATIVE_OPERATION')\n",
            encoding="utf-8",
        )
    else:
        clone_isolated(case_root, work)
    if executor == "receipt_verifier_cli":
        ensure_receipt_fixture(work)
    target = choose_target(work, binding)
    if not target.exists() and str(mutation.get("target_artifact_role", "")) == "text_artifact":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture\n", encoding="utf-8")
    plugin_node = f"{plugin_test.as_posix()}::test_plugin_negative_binding" if executor == "pytest_plugin_session" else None
    clean_bytes = target.read_bytes() if target.is_file() else b""
    clean_exit, clean_text, clean_argv = invoke(executor, work, nodeid, work.name, "negative-clean-report.json", test_nodeid=plugin_node, phase="clean")
    if executor == "pytest_plugin_session":
        plugin_observations = work / "plugin-observations"
        if plugin_observations.exists():
            shutil.rmtree(plugin_observations)
    detach(target)
    mutate_target(work, binding)
    if executor == "pytest_plugin_session":
        target.write_bytes(target.read_bytes() + b"MUTATION\n")
    mutated_bytes = target.read_bytes() if target.is_file() else b""
    mutated_exit, mutated_text, mutated_argv = invoke(executor, work, nodeid, work.name, "negative-mutated-report.json", test_nodeid=plugin_node, phase="mutated")
    found = all(error in mutated_text for error in expected)
    changed = clean_bytes != mutated_bytes
    clean_ok = clean_exit == int(binding.get("expected_clean_exit", 0))
    mutated_ok = mutated_exit != 0
    return {"nodeid": nodeid, "executor": executor, "executor_source_sha256": executor_source_sha256(executor), "operation": mutation.get("closed_operation"), "status": "PASS" if changed and clean_ok and mutated_ok and found else "FAIL", "clean_exit": clean_exit, "mutated_exit": mutated_exit, "clean_expected_exit": binding.get("expected_clean_exit"), "expected_errors_exact": expected, "primary_error": expected[0] if expected else "V09_NEGATIVE_BINDING_INVALID", "observed_error_match": found, "clean_sha256": sha(clean_bytes), "mutated_sha256": sha(mutated_bytes), "argv": mutated_argv, "clean_argv": clean_argv, "stdout_sha256": sha(mutated_text.encode()), "stderr_sha256": sha(b""), "clean_stdout_sha256": sha(clean_text.encode())}


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--case-root", type=Path, required=True); parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(); root = args.case_root.resolve(); contract = load(root / "inputs/architect/SUPER1_SEMANTIC_CONTRACT_V09_20260902.json"); bindings = contract.get("negative_bindings", {}) if isinstance(contract, dict) else {}
    cases = [execute(root, str(node), binding, index) for index, (node, binding) in enumerate(bindings.items(), 1)]
    metadata = load(root / "process-metadata.json") if (root / "process-metadata.json").is_file() else {}
    fallback_run, _, fallback_attempt = root.name.partition(".tmp-")
    result = {"schema_version": 9, "run_id": metadata.get("run_id", fallback_run.lstrip(".")) if isinstance(metadata, dict) else fallback_run.lstrip("."), "attempt_id": metadata.get("attempt_id", fallback_attempt) if isinstance(metadata, dict) else fallback_attempt, "cases": cases, "count": len(cases), "negative_semantics_ok": bool(cases) and all(case["status"] == "PASS" for case in cases)}
    dump(args.output, result); print(json.dumps({"count": len(cases), "negative_semantics_ok": result["negative_semantics_ok"]}, sort_keys=True)); return 0 if result["negative_semantics_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
from backtest.live.settings import environment_snapshot
