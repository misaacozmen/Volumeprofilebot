import json
import os
from pathlib import Path


REPO_ROOT = Path(os.environ.get("CONTRACT_REPO_ROOT", str(Path(__file__).resolve().parents[3]))).resolve()
DOCS_ROOT = REPO_ROOT / "docs"


def test_v14_transfer_contract_is_exact_and_stateful() -> None:
    contract = json.loads((DOCS_ROOT / "SUPER1_PRIVATE_S3_TRANSFER_V14.json").read_text(encoding="utf-8"))
    expected = {
        "schema_version": 1,
        "contract_id": "SUPER1_PRIVATE_S3_TRANSFER",
        "release_generation": "v14",
        "region": "eu-central-1",
        "identity": {
            "account_id_source": "STS.GetCallerIdentity.Account",
            "account_id_pattern": "^[0-9]{12}$",
            "commit_sha12_source": "checkpoint.git_sha[0:12]",
            "commit_sha12_pattern": "^[0-9a-f]{12}$",
            "bucket_template": "otobacktest-transfer-{account_id}-{commit_sha12}",
        },
        "bucket": {
            "create_bucket": {"location_constraint": "eu-central-1", "object_ownership": "BucketOwnerEnforced", "http_status": 200},
            "public_access_block": {"block_public_acls": True, "ignore_public_acls": True, "block_public_policy": True, "restrict_public_buckets": True},
            "encryption": {"rule_count": 1, "algorithm": "AES256"},
            "readback_before_upload_or_presign": {
                "GetBucketLocation": {"LocationConstraint": "eu-central-1"},
                "GetBucketOwnershipControls": {"rule_count": 1, "object_ownership": "BucketOwnerEnforced"},
                "GetPublicAccessBlock": {"block_public_acls": True, "ignore_public_acls": True, "block_public_policy": True, "restrict_public_buckets": True},
                "GetBucketVersioning": {"Status": "absent", "MFADelete": "absent"},
                "GetBucketEncryption": {"rule_count": 1, "algorithm": "AES256"},
                "GetBucketPolicy": {"http_status": 404, "error": "NoSuchBucketPolicy"},
                "GetBucketWebsite": {"http_status": 404, "error": "NoSuchWebsiteConfiguration"},
                "ListObjectsV2": {"count": 0},
                "ListMultipartUploads": {"count": 0},
            },
            "expected_bucket_owner": "{account_id}",
        },
        "upload": {
            "put_object_count": 1,
            "key": "{release_id}.transfer.zip",
            "body": "checkpoint.bundle_path",
            "content_length": "checkpoint.bundle_bytes",
            "content_type": "application/zip",
            "server_side_encryption": "AES256",
            "checksum_algorithm": "SHA256",
            "checksum_sha256": "base64(raw_sha256(checkpoint.bundle_bytes))",
            "sha256_precondition": "lowercase_hex(raw_sha256(bundle)) == checkpoint.bundle_sha256",
            "expected_bucket_owner": "{account_id}",
            "if_none_match": "*",
            "acl_header": "absent",
            "multipart": "forbidden",
            "post_upload_head_object": {"ChecksumMode": "ENABLED", "ContentLength": "checkpoint.bundle_bytes", "ContentType": "application/zip", "ChecksumSHA256": "calculated_base64", "ServerSideEncryption": "AES256"},
            "post_upload_list_objects_v2": {"exact_count": 1, "key": "{release_id}.transfer.zip", "size": "checkpoint.bundle_bytes"},
            "post_upload_list_multipart_uploads": {"exact_count": 0},
        },
        "bundle": {
            "root_member_count": 5,
            "root_members": {
                "{release_id}.zip": {"bytes": "checkpoint.bundle_members.archive_bytes", "sha256": "checkpoint.archive_sha256"},
                "{release_id}.manifest.json": {"bytes": "checkpoint.bundle_members.manifest_bytes", "sha256": "checkpoint.manifest_sha256"},
                "{release_id}.manifest.sig": {"bytes": "checkpoint.bundle_members.signature_bytes", "sha256": "checkpoint.signature_sha256"},
                "stage_signed_upgrader_windows.ps1": {"bytes": "checkpoint.bundle_members.stage_helper_bytes", "sha256": "checkpoint.stage_helper_sha256"},
                "release_integrity.ps1": {"bytes": "checkpoint.bundle_members.integrity_bytes", "sha256": "checkpoint.bootstrap_integrity_sha256"},
            },
            "signed_release_assertions": {"manifest.upgrader_sha256": "checkpoint.upgrader_sha256"},
            "zip_validation": {
                "comparison": "OrdinalIgnoreCase", "separator": "/", "directory_entries": False, "case_insensitive_duplicate": False,
                "rooted": False, "UNC": False, "drive_qualified": False, "empty_segment": False, "dot_segment": False, "dotdot_segment": False,
                "colon_ads": False, "control_character": False, "trailing_dot_space": False,
                "reserved_device_regex": r"(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$",
                "outer_sha_before_open": True, "inner_sha_and_bytes_before_extract": True,
                "containment": "GetFullPath + root separator + OrdinalIgnoreCase", "FileMode": "CreateNew", "overwrite": False,
            },
            "extraction": "CreateNew_contained_no_overwrite",
        },
        "presigned_get": {
            "signature": "SigV4", "method": "GET", "expiry_seconds": 900, "bearer_secret": True,
            "url_storage": ["argument", "environment", "disk", "history", "log", "checkpoint", "report"],
            "url_variables_null_in_finally": True, "clipboard_cleared_in_finally": True, "remote_input": "Read-Host", "psreadline_removed_before_read": True,
        },
        "remote_receive": {
            "rdp_drive_redirection": False,
            "incoming_acl": {"owner_sid": "S-1-5-18", "dacl_protected": True, "ace_count": 2, "sids": ["S-1-5-18", "S-1-5-32-544"], "type": "Allow", "rights": "FullControl", "inherited": False, "propagation": "None", "directory_inheritance": "ContainerInherit|ObjectInherit", "file_inheritance": "None"},
            "paths_reparse": False, "readonly_after_validation": True, "directory_readonly_security_gate": False,
            "download_object_delete_after_remote_validation": True,
        },
        "evidence_return": {
            "key": "evidence/{release_id}-{32_lower_hex_nonce}.evidence.zip",
            "presign_before_head": {"http_status": 404, "error": "NotFound"},
            "presigned_put": {"signature": "SigV4", "expiry_seconds": 900, "if_none_match": "*", "signed_headers": {"Content-Type": "application/zip", "x-amz-server-side-encryption": "AES256", "x-amz-checksum-sha256": "base64(raw_evidence_sha256)", "x-amz-expected-bucket-owner": "{account_id}"}},
            "hash_source": "remote_file_bytes",
            "post_upload_head_object": {"ChecksumMode": "ENABLED", "validate": ["bytes", "type", "SSE", "checksum"]},
            "validation_before_cleanup": ["local_GetObject", "local_sha256", "safe_zip", "evidence_inventory"],
            "inventory": {"required": True, "fields": ["path", "bytes", "sha256", "source_command", "redacted"], "redacted": True},
            "content_exclusions": ["credential", "credentials", "password", "passwd", "secret", "token", "api-key", "authorization", "cookie", "presigned URL", "DPAPI", ".env", "terminal profile"],
            "get_and_put_url_cleanup": "same_finally_bearer_secret_rules",
        },
        "cleanup": {
            "phases": ["NO_BUCKET", "BUCKET_EMPTY", "TRANSFER_PRESENT", "TRANSFER_CONSUMED_BUCKET_EMPTY", "EVIDENCE_PRESENT_UNVALIDATED", "EVIDENCE_VALIDATED"],
            "state_machine": {
                "NO_BUCKET": [],
                "BUCKET_EMPTY": ["DeleteBucket", "HeadBucket-404"],
                "TRANSFER_PRESENT": ["DeleteObject", "HeadObject-404", "ListObjectsV2-zero", "DeleteBucket", "HeadBucket-404"],
                "TRANSFER_CONSUMED_BUCKET_EMPTY": ["ListObjectsV2-zero", "DeleteBucket", "HeadBucket-404"],
                "EVIDENCE_PRESENT_UNVALIDATED": ["preserve evidence object and bucket", "cleanup URLs"],
                "EVIDENCE_VALIDATED": ["DeleteObject", "HeadObject-404", "ListObjectsV2-zero", "DeleteBucket", "HeadBucket-404"],
            },
            "final_readback": ["download_404", "evidence_404", "bucket_absent"],
        },
        "blockers": [
            {"code": "WAITING_USER_AWS_LOGIN", "exact_trigger": "AWS session is not authenticated", "required_cleanup": "no AWS resource exists; wait only", "allowed_next_action": "wait for login"},
            {"code": "BLOCKED_S3_TRANSFER_PERMISSION", "exact_trigger": "required S3 API permission or readback is denied", "required_cleanup": "attempt only phase-allowed cleanup and report residual bucket/key", "allowed_next_action": "stop without retry"},
            {"code": "BLOCKED_RDP_TEXT_CLIPBOARD", "exact_trigger": "URL or clipboard channel is unsafe", "required_cleanup": "clear URL and clipboard; delete transfer object and empty bucket with readback", "allowed_next_action": "stop"},
            {"code": "BLOCKED_TRANSFER_HASH_MISMATCH", "exact_trigger": "remote or inner SHA256 differs from checkpoint", "required_cleanup": "no extraction/deployment; quarantine remote file and clean transfer object/bucket", "allowed_next_action": "stop"},
            {"code": "BLOCKED_EVIDENCE_RETURN", "exact_trigger": "evidence cannot be safely validated and read back", "required_cleanup": "if not uploaded delete empty bucket; otherwise preserve unvalidated evidence and bucket", "allowed_next_action": "validation retry only"},
        ],
    }
    assert contract == expected
    runbook = (DOCS_ROOT / "LIVE_OPERATIONS_TR.md").read_text(encoding="utf-8")
    assert "SUPER1_PRIVATE_S3_TRANSFER_V14.json" in runbook
    forbidden_versions = tuple(f"{prefix}{number}" for prefix in ("v", "V") for number in range(11, 14))
    assert not any(token in runbook for token in forbidden_versions)
