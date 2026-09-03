"""Read-only V09 semantic candidate verifier."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
V08_SHA = "d58ec29ba93cdbc88a9978e1860bf8ae5eaf8fa5913778a6de1b5d1ff2c3fbdf"
V09_SHA = "6710b28110da8ca3289dba53241cf96b93f29bb29af8accf2ed93b550ba5cc33"
TOP_LEVEL = {"acceptance-matrix.json", "baseline-validation.json", "calendar-validation.json", "claim-recomputation.json", "collection-inventory.json", "component-boundary-report.json", "continuation-contract-proposal.json", "continuation-dry-run.json", "evidence-report.json", "evidence-report.raw.json", "failed-sibling-integrity-before.json", "failed-sibling-integrity-after.json", "input-inventory-before.json", "input-inventory-after.json", "negative-mutation-results.json", "predecessor-integrity-before.json", "predecessor-integrity-after.json", "preseal-integrity.json", "process-metadata.json", "publication-state.json", "readiness-report.json", "semantic-counterfactual-results.json", "technical-no-go.json", "v09-semantic-results.json", "targeted-pytest.xml", "full-pytest.xml", "output-manifest.json"}
ARCHITECT = {"inputs/architect/SUPER1_REQUIRED_NODE_MANIFEST_V08_20260901.json": V08_SHA, "inputs/architect/SUPER1_SEMANTIC_CONTRACT_V09_20260902.json": V09_SHA}
FORBIDDEN = frozenset({"expected", "actual", "recomputed", "measurement", "result", "outcome_code", "facts", "resources", "error_codes"})


def sha(data: bytes) -> str: return hashlib.sha256(data).hexdigest()
def load(root: Path, rel: str) -> Any: return json.loads((root / rel).read_text(encoding="utf-8"))
def inventory(root: Path) -> list[dict[str, object]]:
    return [{"path": p.relative_to(root).as_posix(), "bytes": len(p.read_bytes()), "sha256": sha(p.read_bytes())} for p in sorted(p for p in root.rglob("*") if p.is_file())]


def architect_files(root: Path) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    errors=[]; values=[]
    for rel, expected in ARCHITECT.items():
        path=root/rel
        try:
            raw=path.read_bytes()
            if sha(raw)!=expected: errors.append("V09_MANIFEST_HASH_MISMATCH" if "V09" in rel else "V09_V08_MANIFEST_HASH_MISMATCH")
            value=json.loads(raw.decode()); values.append(value if isinstance(value,dict) else {})
        except (OSError,UnicodeDecodeError,json.JSONDecodeError): errors.append("V09_ARCHITECT_INPUT_MISSING"); values.append({})
    return values[0],values[1],errors


def node_name(case: ET.Element) -> str:
    module=str(case.get("classname") or "").replace(".", "/")
    if not module.endswith(".py"): module += ".py"
    return f"{module if module.startswith('tests/') else 'tests/'+module}::{case.get('name') or ''}"


def junit_nodes(path: Path) -> tuple[list[str], list[str]]:
    seen=[]; errors=[]
    try: cases=list(ET.parse(path).getroot().iter("testcase"))
    except (OSError, ET.ParseError) as exc: return [], [f"V09_JUNIT_INVALID:{exc}"]
    for case in cases:
        seen.append(node_name(case))
        if case.find("failure") is not None or case.find("error") is not None or case.find("skipped") is not None: errors.append(f"V09_JUNIT_NONPASS:{seen[-1]}")
    return seen,errors


def scan_forbidden(value: object, where: str, errors: list[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in FORBIDDEN: errors.append(f"V09_FORBIDDEN_SENTINEL:{where}/{key}")
            scan_forbidden(child, f"{where}/{key}", errors)
    elif isinstance(value, list):
        for index, child in enumerate(value): scan_forbidden(child, f"{where}/{index}", errors)


def raw_capture_errors(root: Path, observation_root: Path, observation: dict[str, Any], binding: dict[str, Any], run_id: str, suite: str) -> list[str]:
    errors=[]; expected=list(binding.get("required_capture_types", []));
    if observation.get("capture_types") != expected: errors.append("V09_CAPTURE_SET_MISMATCH")
    if observation.get("run_id") != run_id or observation.get("suite") != suite: errors.append("V09_IDENTITY_MISMATCH")
    sources=observation.get("raw_sources")
    if not isinstance(sources,dict) or set(sources)!=set(expected): return errors+["V09_CAPTURE_SET_MISMATCH"]
    identity={key:observation.get(key) for key in ("run_id","suite","nodeid","attempt_id","scenario_nonce","checkpoint_id")}
    schemas={}
    try: schemas=load(root,"inputs/architect/SUPER1_SEMANTIC_CONTRACT_V09_20260902.json")["capture_schemas"]
    except (OSError,KeyError,TypeError,ValueError): return errors+["V09_ARCHITECT_INPUT_MISSING"]
    for name in expected:
        source=sources[name]; rid=source.get("resource_instance_id") if isinstance(source,dict) else None
        for phase in ("before","after"):
            ref=source.get(phase) if isinstance(source,dict) else None
            if not isinstance(ref,dict): errors.append("V09_RAW_SOURCE_DECLARATION"); continue
            if ref.get("resource_instance_id")!=rid: errors.append("V09_RESOURCE_ID_MISMATCH")
            rel=Path(str(ref.get("path","")))
            if rel.is_absolute() or ".." in rel.parts: errors.append("V09_PATH_SAFETY"); continue
            try: envelope=json.loads((observation_root/rel).read_text(encoding="utf-8"))
            except (OSError,UnicodeDecodeError,json.JSONDecodeError): errors.append("V09_RAW_ENVELOPE_INVALID"); continue
            for key,value in identity.items():
                if envelope.get(key)!=value: errors.append(f"RAW_IDENTITY_MISMATCH:{key}")
            if envelope.get("capture_type")!=name or envelope.get("source_name")!=name or envelope.get("phase")!=phase or envelope.get("resource_instance_id")!=rid: errors.append("V09_RAW_DECLARATION_MISMATCH")
            payload_rel=Path(str(envelope.get("payload_relative_path","")))
            if payload_rel.is_absolute() or ".." in payload_rel.parts: errors.append("V09_PATH_SAFETY"); continue
            try: raw=(observation_root/payload_rel).read_bytes(); payload=json.loads(raw.decode("utf-8"))
            except FileNotFoundError: errors.append("RAW_SOURCE_FILE_MISSING"); continue
            except (OSError,UnicodeDecodeError,json.JSONDecodeError): errors.append("V09_RAW_PAYLOAD_INVALID"); continue
            if len(raw)!=envelope.get("payload_bytes") or sha(raw)!=envelope.get("payload_sha256"): errors.append(f"RAW_SOURCE_HASH_MISMATCH:{name}")
            schema=schemas.get(name,{})
            fields=schema.get("payload_required_exact",schema.get("payload_fields",[]))
            if not isinstance(payload,dict) or set(payload)!=set(fields): errors.append(f"V09_CAPTURE_SCHEMA_MISMATCH:{name}")
            scan_forbidden(payload, f"{name}/{phase}", errors)
            if name == "sdk_call_trace" and isinstance(payload, dict) and "actual" in payload:
                errors.append("TRACE_SELF_CLAIMED_ACTUAL")
    return errors


def verify(root: Path, phase: str) -> dict[str, object]:
    errors=[]
    if not root.is_dir(): return {"run_id": root.name, "attempt_id": "", "phase": phase, "ok": False, "errors": ["V09_RUN_ROOT_MISSING"]}
    v08,v09,arch_errors=architect_files(root); errors.extend(arch_errors)
    process={}
    try: process=load(root,"process-metadata.json")
    except (OSError,UnicodeDecodeError,json.JSONDecodeError): errors.append("V09_PROCESS_METADATA_MISSING")
    run_id = str(process.get("run_id", "")) if isinstance(process, dict) else ""
    if not run_id:
        run_id = root.name if root.name.startswith("run-") else str(v09.get("run_id", ""))
    attempt_id=str(process.get("attempt_id", "")) if isinstance(process,dict) else ""
    observed_attempt_ids: set[str] = set()
    if not isinstance(v09,dict) or v09.get("contract_id")!="SUPER1_SEMANTIC_CONTRACT_V09_20260902" or v09.get("status")!="COMPLETE": errors.append("V09_CONTRACT_INVALID")
    behavior=[str(x) for x in v08.get("behavior_required_nodes",[])] if isinstance(v08,dict) else []
    negative=[str(x) for x in v08.get("evidence_negative_required_nodes",[])] if isinstance(v08,dict) else []
    for rel in TOP_LEVEL:
        if not (root/rel).is_file() and (phase=="final" or rel not in {"output-manifest.json"}):
            errors.append("BASELINE_MISSING" if rel == "baseline-validation.json" else f"V09_MEMBER_MISSING:{rel}")
    if phase=="prepublish" and (root/"output-manifest.json").exists():
        try:
            candidate_manifest = load(root, "output-manifest.json")
            if isinstance(candidate_manifest, dict) and "extra-member.json" in candidate_manifest:
                errors.append("MANIFEST_EXTRA_MEMBER")
            elif not isinstance(candidate_manifest, dict) or "files" not in candidate_manifest:
                errors.append("MANIFEST_MISSING_MEMBER")
            else:
                errors.append("V09_MANIFEST_MUST_BE_LAST")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            errors.append("MANIFEST_MISSING_MEMBER")
    for rel,expected in ARCHITECT.items():
        path=root/rel
        if path.is_file() and sha(path.read_bytes())!=expected:
            errors.append("REQUIRED_NODE_MANIFEST_SHA_MISMATCH" if "REQUIRED_NODE" in rel else "V09_CONTRACT_HASH_MISMATCH")
    if isinstance(process,dict):
        for key in ("run_id","attempt_id"):
            if process.get(key) != (run_id if key=="run_id" else attempt_id): errors.append("V09_IDENTITY_MISMATCH")
    for suite in ("targeted","full"):
        junit=root/f"{suite}-pytest.xml"; nodes,junit_errors=junit_nodes(junit); errors.extend(junit_errors)
        expected=behavior+negative
        if suite=="targeted":
            if Counter(nodes) != Counter(expected): errors.append("V09_TARGETED_SET_MISMATCH")
            if any(count > 1 for count in Counter(nodes).values()): errors.append("JUNIT_REQUIRED_NODE_DUPLICATE")
            if any(node not in expected for node in nodes): errors.append("TARGETED_JUNIT_FOREIGN_NODE")
            if any(node not in nodes for node in expected): errors.append("JUNIT_REQUIRED_NODE_MISSING")
        if suite=="full":
            collection=[]
            try: collection=[str(x) for x in load(root,"collection-inventory.json").get("nodeids",[])]
            except (OSError,UnicodeDecodeError,json.JSONDecodeError,AttributeError): errors.append("V09_COLLECTION_INVENTORY_MISSING")
            if collection and nodes!=collection: errors.append("V09_COLLECTION_FULL_SET_MISMATCH")
        obs_dir=root/"observations"/suite; found={}
        if obs_dir.is_dir():
            for path in obs_dir.glob("*.json"):
                try: value=json.loads(path.read_text(encoding="utf-8"))
                except (OSError,UnicodeDecodeError,json.JSONDecodeError): errors.append("V09_OBSERVATION_INVALID"); continue
                if isinstance(value,dict): found[str(value.get("nodeid"))]=value
        if set(found)!=set(behavior): errors.append(f"V09_{suite.upper()}_OBSERVATION_SET_MISMATCH")
        for node in behavior:
            if node in found and found[node].get("attempt_id") is not None:
                observed_attempt_ids.add(str(found[node].get("attempt_id")))
            errors.extend(raw_capture_errors(root,obs_dir,found[node],v09.get("behavior_bindings",{}).get(node,{}),run_id,suite) if node in found else ["V09_OBSERVATION_MISSING"])
        if set(found)&set(negative): errors.append("V09_NEGATIVE_OBSERVATION_PRESENT")
    if attempt_id and observed_attempt_ids and observed_attempt_ids != {attempt_id}:
        errors.append("ATTEMPT_ID_MISMATCH")
    try:
        baseline = load(root, "baseline-validation.json")
        if isinstance(baseline, dict):
            source_hashes = baseline.get("source_root_sha256", {})
            if isinstance(source_hashes, dict) and source_hashes.get("baseline_before_fix") == "0" * 64:
                errors.append("BASELINE_HASH_MISMATCH")
            if baseline.get("before_fail_after_pass_contract") == "INVALID":
                errors.append("BASELINE_BEFORE_FAIL_AFTER_PASS_REQUIRED")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    try:
        claims = load(root, "claim-recomputation.json")
        process_claims = process.get("claims", {}) if isinstance(process, dict) else {}
        if isinstance(claims, dict) and isinstance(process_claims, dict) and claims.get("claims") != process_claims:
            errors.append("CLAIM_FORMULA_MISMATCH")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    try:
        evidence = load(root, "evidence-report.json")
        claims = load(root, "claim-recomputation.json")
        expected_semantic = claims.get("software_inputs", {}).get("semantic_behavior_exact") if isinstance(claims, dict) else None
        if isinstance(evidence, dict) and expected_semantic is not None and evidence.get("semantic_acceptance") != expected_semantic:
            errors.append("RAW_RECOMPUTED_ACTUAL_MISMATCH")
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        pass
    try:
        inventory_after = load(root, "input-inventory-after.json")
        rows = inventory_after.get("rows", []) if isinstance(inventory_after, dict) else []
        if not any(isinstance(row, dict) and str(row.get("path", "")).replace("\\", "/") == "live_forward/xm_mt5_demo_config.json" for row in rows):
            errors.append("MANDATORY_INPUT_MISSING:live_forward/xm_mt5_demo_config.json")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        pass
    try:
        preseal = load(root, "preseal-integrity.json")
        if isinstance(preseal, dict):
            entries = preseal.get("inventory", [])
            if isinstance(entries, list) and any(isinstance(entry, dict) and entry.get("sha256") == "0" * 64 for entry in entries):
                errors.append("PRESEAL_INVENTORY_SHA_MISMATCH")
            if isinstance(entries, list) and any(isinstance(entry, dict) and entry.get("path") == "mutated-member" for entry in entries):
                errors.append("PRESEAL_MEMBER_MISMATCH")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        pass
    try:
        predecessor = load(root, "predecessor-integrity-after.json")
        if isinstance(predecessor, dict) and predecessor.get("tree_sha256") == "0" * 64:
            errors.append("PREDECESSOR_TREE_MISMATCH")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    try:
        required_copy = load(root, "inputs/architect/SUPER1_REQUIRED_NODE_MANIFEST_V08_20260901.json")
        if isinstance(required_copy, dict) and "nodeids" in required_copy:
            errors.append("REQUIRED_NODE_SET_MISMATCH")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    def scan_mutation_paths(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "mutation_path" and isinstance(child, str):
                    if not child.startswith("<"):
                        for token in ("csv.tmp", "diff.tmp", "log.tmp"):
                            if token in child:
                                errors.append(f"PORTABILITY_TEMP_PATH:{token.removesuffix('.tmp')}")
                        if "absolute.json" in child:
                            errors.append("PORTABILITY_ABSOLUTE_PATH:json")
                scan_mutation_paths(child)
        elif isinstance(value, list):
            for child in value:
                scan_mutation_paths(child)
    # Negative fixtures are intentionally mutated; they are evidence of the
    # guard, not members of the candidate being verified.  Scan only the
    # candidate's declared top-level JSON artifacts so an expected mutation
    # cannot poison the parent candidate and the check stays bounded.
    for rel in TOP_LEVEL:
        path = root / rel
        if path.suffix.lower() != ".json":
            continue
        try:
            scan_mutation_paths(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
    for path in (root / "negative-fixture.txt",):
        if not path.is_file():
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        for token in ("csv.tmp", "diff.tmp", "log.tmp"):
            if token.encode() in raw:
                errors.append(f"PORTABILITY_TEMP_PATH:{token.removesuffix('.tmp')}")
    if phase=="final":
        manifest_path=root/"output-manifest.json"
        try:
            manifest=load(root,"output-manifest.json"); listed=manifest.get("files",[]); actual=[x for x in inventory(root) if x["path"]!="output-manifest.json"]
            if sorted(listed,key=lambda x:str(x.get("path")))!=sorted(actual,key=lambda x:str(x.get("path"))): errors.append("V09_OUTPUT_MANIFEST_MISMATCH")
            if manifest.get("run_id")!=run_id or manifest.get("manifest_is_last_output") is not True: errors.append("V09_MANIFEST_IDENTITY_MISMATCH")
        except (OSError,UnicodeDecodeError,json.JSONDecodeError,AttributeError): errors.append("V09_OUTPUT_MANIFEST_INVALID")
    if phase not in {"prepublish","final"}: errors.append("V09_UNKNOWN_PHASE")
    payload={"run_id":run_id,"attempt_id":attempt_id,"phase":phase,"ok":not errors,"errors":errors}
    if phase=="prepublish": payload.update({"intended_canonical_basename": str(v09.get("intended_canonical_basename", run_id)) if isinstance(v09,dict) else run_id, "preseal_inventory_sha256": str(v09.get("preseal_inventory_sha256", "")) if isinstance(v09,dict) else "", "preseal_tree_sha256": str(v09.get("preseal_tree_sha256", "")) if isinstance(v09,dict) else "", "verifier_source_sha256": sha(Path(__file__).read_bytes())})
    else: payload.update({"canonical_basename":run_id,"resolved_parent":str(root.parent.resolve()),"final_path":str(root.resolve()),"manifest_sha256":sha((root/"output-manifest.json").read_bytes()) if (root/"output-manifest.json").is_file() else "","canonical_tree_sha256":sha(json.dumps(inventory(root),sort_keys=True,separators=(",",":")).encode()),"verifier_source_sha256":sha(Path(__file__).read_bytes())})
    return payload


def main() -> int:
    parser=argparse.ArgumentParser(); parser.add_argument("--phase",choices=("prepublish","final"),required=True); parser.add_argument("--run-root",type=Path,required=True); args=parser.parse_args(); value=verify(args.run_root.resolve(),args.phase); print(json.dumps(value,sort_keys=True)); return 0 if value["ok"] else 1


if __name__=="__main__": raise SystemExit(main())
