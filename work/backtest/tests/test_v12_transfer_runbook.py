import json
from pathlib import Path


DOCS_ROOT = Path(__file__).resolve().parents[3] / "docs"
DOCS = DOCS_ROOT / "LIVE_OPERATIONS_TR.md"


def test_runbook_authorizes_only_private_s3_round_trip() -> None:
    contract = json.loads((DOCS_ROOT / "SUPER1_PRIVATE_S3_TRANSFER_V12.json").read_text(encoding="utf-8"))
    assert contract["region"] == "eu-central-1"
    assert contract["bucket_template"] == "otobacktest-transfer-{account_id}-{commit_sha12}"
    assert contract["ownership"] == "BucketOwnerEnforced"
    assert contract["public_access_block"] == {
        "block_public_acls": True,
        "ignore_public_acls": True,
        "block_public_policy": True,
        "restrict_public_buckets": True,
    }
    assert contract["versioning"] == "disabled"
    assert contract["encryption"] == "AES256"
    assert contract["bucket_policy_allowed"] is False
    assert contract["website_allowed"] is False
    assert contract["object_count_before_download"] == 1
    assert contract["bundle_entries"] == 5
    assert contract["presigned_get_seconds"] == 900
    assert contract["presigned_put_seconds"] == 900
    assert contract["rdp_drive_redirection_allowed"] is False
    assert set(contract["evidence_exclusions"]) == {"credentials", "DPAPI", ".env", "tokens", "passwords", "terminal profiles"}
    assert contract["blockers"] == [
        "WAITING_USER_AWS_LOGIN",
        "BLOCKED_S3_TRANSFER_PERMISSION",
        "BLOCKED_RDP_TEXT_CLIPBOARD",
        "BLOCKED_TRANSFER_HASH_MISMATCH",
        "BLOCKED_EVIDENCE_RETURN",
    ]
    runbook = DOCS.read_text(encoding="utf-8")
    assert "SUPER1_PRIVATE_S3_TRANSFER_V12.json" in runbook
