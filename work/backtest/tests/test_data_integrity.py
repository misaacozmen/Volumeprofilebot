from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.integrity import conflicting_duplicate_issues, structural_ohlcv_issues
from backtest.data_loader import load_ohlcv


def test_identical_duplicate_is_not_a_conflict() -> None:
    rows = pd.DataFrame(
        [
            {"time": "2025-01-02T10:00:00Z", "open": 1, "high": 2, "low": 0, "close": 1, "volume": 5},
            {"time": "2025-01-02T10:00:00Z", "open": 1, "high": 2, "low": 0, "close": 1, "volume": 5},
        ]
    )
    rows["time"] = pd.to_datetime(rows["time"], utc=True)

    assert conflicting_duplicate_issues(rows) == []


def test_conflicting_duplicate_is_reported() -> None:
    rows = pd.DataFrame(
        [
            {"time": "2025-01-02T10:00:00Z", "open": 1, "high": 2, "low": 0, "close": 1, "volume": 5},
            {"time": "2025-01-02T10:00:00Z", "open": 1, "high": 3, "low": 0, "close": 1, "volume": 5},
        ]
    )
    rows["time"] = pd.to_datetime(rows["time"], utc=True)

    assert [issue.code for issue in conflicting_duplicate_issues(rows)] == ["CONFLICTING_DUPLICATE_BAR"]


def test_invalid_ohlc_range_is_reported() -> None:
    rows = pd.DataFrame(
        [
            {"time": "2025-01-02T10:00:00Z", "open": 4, "high": 3, "low": 0, "close": 1, "volume": 5},
        ]
    )
    rows["time"] = pd.to_datetime(rows["time"], utc=True)

    assert "INVALID_OHLC_RANGE" in {issue.code for issue in structural_ohlcv_issues(rows)}


def test_structural_contract_reports_nonfinite_negative_duplicate_and_nonmonotonic() -> None:
    rows = pd.DataFrame(
        [
            {"time": "2025-01-02T10:03:00Z", "open": 1, "high": 2, "low": 0, "close": 1, "volume": -1},
            {"time": "2025-01-02T10:00:00Z", "open": 1, "high": float("inf"), "low": 0, "close": 1, "volume": 1},
            {"time": "2025-01-02T10:00:00Z", "open": 1, "high": 2, "low": 0, "close": 1, "volume": 1},
        ]
    )
    codes = {issue.code for issue in structural_ohlcv_issues(rows)}

    assert {"NON_FINITE_OHLCV", "NEGATIVE_VOLUME", "DUPLICATE_BAR", "NON_MONOTONIC_TIME"} <= codes


def test_loader_rejects_parse_loss_instead_of_dropping_row(tmp_path: Path) -> None:
    path = tmp_path / "TEST, 3m.csv"
    pd.DataFrame(
        [
            {"time": "2025-01-02T10:00:00Z", "open": 1, "high": 2, "low": 0, "close": 1, "volume": 1},
            {"time": "bad-time", "open": 1, "high": 2, "low": 0, "close": 1, "volume": 1},
        ]
    ).to_csv(path, index=False)

    with pytest.raises(ValueError, match="INVALID_TIME"):
        load_ohlcv([path])


def test_loader_deduplicates_identical_overlap_bars_by_default(tmp_path: Path) -> None:
    path = tmp_path / "TEST, 3m.csv"
    row = {"time": "2025-01-02T10:00:00Z", "open": 1, "high": 2, "low": 0, "close": 1, "volume": 1}
    pd.DataFrame([row, row]).to_csv(path, index=False)

    loaded = load_ohlcv([path])
    assert len(loaded.frame) == 1


def test_loader_rejects_conflicting_duplicate_even_in_identical_mode(tmp_path: Path) -> None:
    path = tmp_path / "TEST, 3m.csv"
    first = {"time": "2025-01-02T10:00:00Z", "open": 1, "high": 2, "low": 0, "close": 1, "volume": 1}
    second = {**first, "high": 3}
    pd.DataFrame([first, second]).to_csv(path, index=False)

    with pytest.raises(ValueError, match="CONFLICTING_DUPLICATE_BAR|Conflicting duplicate"):
        load_ohlcv([path])
