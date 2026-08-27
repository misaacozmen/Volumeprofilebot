import json
from pathlib import Path


DOCS_ROOT = Path(__file__).resolve().parents[3] / "docs"
DOCS = DOCS_ROOT / "LIVE_OPERATIONS_TR.md"


def test_v13_transfer_contract_is_complete_and_version_exact() -> None:
    contract = json.loads((DOCS_ROOT / "SUPER1_PRIVATE_S3_TRANSFER_V13.json").read_text(encoding="utf-8"))
    assert set(contract) == {
        "schema_version", "contract_id", "release_generation", "region", "identity", "bucket", "upload", "bundle",
        "presigned_get", "remote_receive", "evidence_return", "cleanup", "blockers",
    }
    assert contract["schema_version"] == 1
    assert contract["contract_id"] == "SUPER1_PRIVATE_S3_TRANSFER"
    assert contract["release_generation"] == "v13"
    assert contract["region"] == "eu-central-1"
    assert contract["identity"] == {"account_id_pattern": "^[0-9]{12}$", "commit_sha12_pattern": "^[0-9a-f]{12}$"}
    assert contract["bucket"] == {
        "template": "otobacktest-transfer-{account_id}-{commit_sha12}",
        "ownership": "BucketOwnerEnforced", "versioning": {"status": "disabled", "MFADelete": "absent"},
        "default_encryption": "AES256", "bucket_policy": "absent", "website_configuration": "absent",
        "public_access_block": {"block_public_acls": True, "ignore_public_acls": True, "block_public_policy": True, "restrict_public_buckets": True},
    }
    assert contract["upload"] == {
        "put_object_count": 1, "multipart_allowed": False, "object_key": "{release_id}.transfer.zip", "acl_header_allowed": False,
        "server_side_encryption": "AES256", "checksum_algorithm": "SHA256", "checksum_source": "bundle_sha256_base64",
        "content_type": "application/zip", "head_object_readback": ["key", "content_length", "checksum_sha256", "server_side_encryption"],
        "list_objects_v2_exact_count": 1, "multipart_upload_exact_count": 0,
    }
    assert contract["bundle"] == {
        "entries": 5, "directory_entries": False, "outer_sha_before_open": True, "inner_sha_before_extract": True,
        "extraction": "CreateNew_contained_no_overwrite",
        "members": {
            "{release_id}.zip": "archive_sha256", "{release_id}.manifest.json": "manifest_sha256",
            "{release_id}.manifest.sig": "signature_sha256", "stage_signed_upgrader_windows.ps1": "stage_helper_sha256",
            "release_integrity.ps1": "bootstrap_integrity_sha256", "manifest.upgrader_sha256": "upgrader_sha256",
        },
        "unsafe_paths": ["case_insensitive_duplicate", "directory", "backslash", "rooted", "UNC", "drive_qualified", "empty_segment", "dot_segment", "dotdot_segment", "ADS_colon", "trailing_dot_space", "reserved_device"],
    }
    assert contract["presigned_get"] == {"expiry_seconds": 900, "url_storage": False, "read_method": "Read-Host", "psreadline_removed": True, "clipboard_cleared_in_finally": True}
    assert contract["remote_receive"] == {"rdp_drive_redirection_allowed": False, "incoming_acl": "SYSTEM_only_SYSTEM_Administrators_explicit_FullControl_inheritance_disabled_reparse_free", "readonly_after_validation": True, "directory_readonly_is_security_gate": False, "aws_api_readback_before_upload_or_presign": True, "download_object_delete_after_remote_validation": True}
    assert contract["evidence_return"] == {
        "presigned_put": {"expiry_seconds": 900, "method": "PUT", "unique_new_key": True, "signed_headers": ["Content-Type", "ServerSideEncryption", "ChecksumSHA256"]},
        "inventory_required": True, "inventory_fields": ["path", "bytes", "sha256", "source_command", "redacted"], "redacted_required": True,
        "content_exclusions": ["credential", "credentials", "password", "passwd", "secret", "token", "api-key", "authorization", "cookie", "presigned URL", "DPAPI", ".env", "terminal profile"],
        "validation_before_cleanup": ["evidence_head_object", "local_download_sha256", "safe_zip", "evidence_inventory"],
    }
    assert contract["cleanup"] == {"download_object_delete_after_remote_validation": True, "evidence_object_delete_after_local_validation": True, "bucket_delete_after_local_validation": True, "final_api_readback": ["download_404", "evidence_404", "bucket_absent"]}
    assert {item["code"] for item in contract["blockers"]} == {"WAITING_USER_AWS_LOGIN", "BLOCKED_S3_TRANSFER_PERMISSION", "BLOCKED_RDP_TEXT_CLIPBOARD", "BLOCKED_TRANSFER_HASH_MISMATCH", "BLOCKED_EVIDENCE_RETURN"}
    assert all(set(item) == {"code", "exact_trigger", "required_cleanup", "allowed_next_action"} for item in contract["blockers"])
    runbook = DOCS.read_text(encoding="utf-8")
    assert "SUPER1_PRIVATE_S3_TRANSFER_V13.json" in runbook
