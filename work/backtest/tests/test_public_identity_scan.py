from __future__ import annotations

from pathlib import Path

from scripts.scan_public_broker_identity import concrete_paths, scan


ROOT = Path(__file__).resolve().parents[1]


def test_concrete_account_identity_is_detected() -> None:
    assert concrete_paths({"account_login": 12345678, "nested": {"expected_server": "fixture"}}) == [
        "$.account_login", "$.nested.expected_server"
    ]


def test_tracked_public_tree_has_no_concrete_broker_identity() -> None:
    assert scan(ROOT) == []
