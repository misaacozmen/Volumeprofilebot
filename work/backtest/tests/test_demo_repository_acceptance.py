from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.demo_repository_acceptance import (
    evaluate,
    validate_owner_declaration,
    validate_push_approval,
)
from scripts.git_provenance import current_branch, current_commit_tree


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "demo_repository_acceptance.py"


def _context() -> dict[str, str]:
    source = current_commit_tree(ROOT)
    return {"repository_identity": "demo-repository-test", "branch": current_branch(ROOT), "source_commit": source["source_commit"], "source_tree_oid": source["source_tree_sha256"]}


def test_demo_acceptance_missing_external_inputs_is_blocked_without_legacy_signed_acceptance(tmp_path: Path) -> None:
    report = tmp_path / "acceptance.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(ROOT), "--repository-identity", "demo-repository-test", "--report", str(report)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["status"] == "BLOCKED_EXTERNAL_ACCEPTANCE"
    assert payload["legacy_signed_acceptance_used"] is False
    assert payload["live_risk_controls_unchanged"] is True
    assert payload["manifest_integrity_controls_unchanged"] is True


def test_demo_owner_declaration_is_account_rotation_only_and_push_approval_binds_branch_and_commit(tmp_path: Path) -> None:
    expected = _context()
    owner = tmp_path / "owner-declaration.json"
    owner.write_text(json.dumps({
        "schema_version": 1,
        "statement_type": "DEMO_BROKER_ACCOUNT_ROTATION_OWNER_DECLARATION_V1",
        "status": "DECLARED",
        "declarant_role": "OWNER",
        "purpose": "DEMO_REPOSITORY_PUBLICATION",
        "rotation_date": "2026-09-18",
        "old_credential_not_used": True,
        "credential_values_shared": False,
    }), encoding="utf-8")
    approval = tmp_path / "push-approval.json"
    approval.write_text(json.dumps({
        "schema_version": 1,
        "approval_type": "DEMO_REPOSITORY_PUSH_APPROVAL_V1",
        "status": "APPROVED",
        **expected,
        "destination_ref": f"refs/heads/{expected['branch']}",
        "approved": True,
        "approved_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "approval_scope": "DEMO_REPOSITORY_PUBLICATION",
    }), encoding="utf-8")
    assert validate_owner_declaration(owner)["status"] == "DECLARED"
    assert validate_push_approval(approval, expected=expected)["approved"] is True
    result = evaluate(ROOT, repository_identity=expected["repository_identity"], owner_declaration=owner, history_scan=None, sensitive_scan=None, push_approval=approval)
    assert result["status"] == "BLOCKED_EXTERNAL_ACCEPTANCE"
    assert result["checks"] == {"owner_declaration": True, "history_scan": False, "sensitive_scan": False, "push_approval": True, "scan_denylist_binding": False, "sensitive_inventory_binding": False}


def test_demo_policy_rejects_legacy_signed_fields(tmp_path: Path) -> None:
    expected = _context()
    owner = tmp_path / "owner-declaration.json"
    payload = {
        "schema_version": 1,
        "statement_type": "DEMO_BROKER_ACCOUNT_ROTATION_OWNER_DECLARATION_V1",
        "status": "DECLARED",
        "declarant_role": "OWNER",
        "purpose": "DEMO_REPOSITORY_PUBLICATION",
        "rotation_date": "2026-09-18",
        "old_credential_not_used": True,
        "credential_values_shared": False,
        "signature_b64": "legacy-artifact-not-used",
    }
    owner.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="closed"):
        validate_owner_declaration(owner)


def test_demo_policy_keeps_live_risk_and_manifest_guards_in_place() -> None:
    live = (ROOT / "scripts" / "run_super1_xm_mt5_forward.py").read_text(encoding="utf-8")
    manifest = (ROOT / "backtest" / "reacquisition_contract.py").read_text(encoding="utf-8")
    assert "_assert_super1_lease" in live and "order_send" in live
    assert "COMMITTED" in manifest and "source_tree" in manifest
