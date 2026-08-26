from pathlib import Path


DOCS = Path(__file__).resolve().parents[3] / "docs" / "LIVE_OPERATIONS_TR.md"


def test_runbook_has_single_v11_private_s3_transfer_contract() -> None:
    text = DOCS.read_text(encoding="utf-8")
    assert "v11" in text.lower()
    assert "Private S3" in text
    assert "eu-central-1" in text
    assert "presigned get" in text.lower()
    assert "900" in text
    assert "BucketOwnerEnforced" in text
    assert "SSE-S3" in text
    assert "Block Public Access" in text
    assert "public website" in text.lower()
    assert "RDP drive redirection" in text
    assert "BLOCKED_S3_TRANSFER_PERMISSION" in text
