"""Independent read-only verifier for a sealed Super1 verification receipt."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import re


ABSOLUTE_PATH = re.compile(rb"(?:[A-Za-z]:[\\/]|/)")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def inventory(root: Path) -> list[dict[str, object]]:
    rows = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        rows.append({
            "path": path.relative_to(root).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": sha256(path.read_bytes()),
        })
    return rows


def load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def verify(receipt_path: Path, run_root: Path) -> dict[str, object]:
    errors: list[str] = []
    try:
        receipt = load_json(receipt_path)
        if not isinstance(receipt, dict):
            raise ValueError("receipt must be an object")
        manifest = load_json(run_root / "output-manifest.json")
        process = load_json(run_root / "process-metadata.json")
        acceptance = load_json(run_root / "acceptance-matrix.json")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return {"ok": False, "errors": [f"receipt input invalid: {exc}"]}

    if not isinstance(manifest, dict) or not isinstance(process, dict) or not isinstance(acceptance, dict):
        errors.append("sealed gate input is not an object")
        return {"ok": False, "errors": errors}
    if receipt.get("run_id") != run_root.name or receipt.get("publication_status") != "PUBLISHED":
        errors.append("receipt run/publication binding failed")
    if receipt.get("authoritative") is not False:
        errors.append("AUTHORITY_BEFORE_VERIFIED_RECEIPT")
    try:
        readiness = load_json(run_root / "readiness-report.json")
        if isinstance(readiness, dict) and readiness.get("authoritative") is True:
            errors.append("AUTHORITY_BEFORE_VERIFIED_RECEIPT")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        pass
    if receipt.get("final_path") != str(run_root.resolve()):
        errors.append("receipt canonical final_path mismatch")
    if receipt.get("manifest_sha256") != sha256((run_root / "output-manifest.json").read_bytes()):
        errors.append("receipt manifest SHA mismatch")
    expected_members = manifest.get("files") if isinstance(manifest, dict) else None
    actual_members = inventory(run_root)
    actual_without_manifest = [item for item in actual_members if item["path"] not in {"output-manifest.json", "receipt-candidate.json"}]
    if expected_members != actual_without_manifest:
        errors.append("receipt final tree does not match manifest")
    if receipt.get("verified_member_count") != len(actual_without_manifest):
        errors.append("receipt verified member count mismatch")

    attempt_id = process.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id or receipt.get("attempt_id") != attempt_id:
        errors.append("receipt attempt_id mismatch")

    for label, phase in (("prepublish_verifier", "prepublish"), ("final_verifier", "final")):
        binding = receipt.get(label)
        if not isinstance(binding, dict):
            errors.append(f"{label} binding missing")
            continue
        try:
            decoded = base64.b64decode(str(binding["stdout_base64"]).encode("ascii"), validate=True)
            result = json.loads(decoded.decode("utf-8"))
        except (KeyError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(f"{label} stdout decode failed: {exc}")
            continue
        if sha256(decoded) != binding.get("stdout_sha256"):
            errors.append("PREPUBLISH_VERIFIER_STDOUT_BINDING_MISMATCH" if phase == "prepublish" else "FINAL_VERIFIER_STDOUT_BINDING_MISMATCH")
        if binding.get("exit_code") != 0 or not isinstance(result, dict) or result.get("phase") != phase or result.get("ok") is not True:
            errors.append(f"{label} did not prove PASS")
        if ABSOLUTE_PATH.search(decoded):
            errors.append(f"{label} stdout is not portable")

    verifier_source = Path(__file__).with_name("verify_super1_evidence_run.py")
    if not verifier_source.is_file() or receipt.get("verifier_source_sha256") != sha256(verifier_source.read_bytes()):
        errors.append("SEMANTIC_VERIFIER_SOURCE_SHA_MISMATCH")
    receipt_verifier_source = Path(__file__).resolve()
    if receipt.get("receipt_verifier_source_sha256") != sha256(receipt_verifier_source.read_bytes()):
        errors.append("RECEIPT_VERIFIER_SOURCE_SHA_MISMATCH")

    claims = process.get("claims")
    acceptance_claims = acceptance.get("claims")
    if not isinstance(claims, dict) or not isinstance(acceptance_claims, dict) or claims != acceptance_claims:
        errors.append("sealed claim sources disagree")
    final_ok = not any(error.startswith("final_verifier") for error in errors)
    prepublish_ok = not any(error.startswith("prepublish_verifier") for error in errors)
    if isinstance(claims, dict):
        software_candidate = bool(claims.get("software_claim_ok")) and final_ok
        continuation_candidate = bool(claims.get("continuation_fixture_claim_ok")) and final_ok
        evidence_candidate = bool(claims.get("evidence_payload_claim_ok")) and prepublish_ok and final_ok
        local_candidate = software_candidate and continuation_candidate and evidence_candidate
        if receipt.get("software_candidate_ok") is not software_candidate:
            errors.append("software candidate formula mismatch")
        if receipt.get("continuation_fixture_candidate_ok") is not continuation_candidate:
            errors.append("continuation candidate formula mismatch")
        if receipt.get("evidence_payload_candidate_ok") is not evidence_candidate:
            errors.append("evidence candidate formula mismatch")
        if receipt.get("local_acceptance_candidate_ok") is not local_candidate:
            errors.append("local candidate formula mismatch")
        if receipt.get("local_acceptance_ok") is not local_candidate:
            errors.append("local acceptance formula mismatch")
        if receipt.get("run_evidence_ok") is not (local_candidate and receipt.get("delivery_integrity_ok") is True):
            errors.append("run evidence formula mismatch")
    return {"ok": not errors, "errors": errors, "run_id": run_root.name}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt-candidate", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.receipt_candidate.resolve(), args.run_root.resolve())
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
