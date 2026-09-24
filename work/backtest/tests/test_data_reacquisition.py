from __future__ import annotations

import json

import pandas as pd
import pytest

from scripts.merge_reacquired_dukascopy import merge


def _frame(rows: list[tuple[str, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"time": time, "open": price, "high": price + 1, "low": price - 1, "close": price, "Volume": 1}
            for time, price in rows
        ]
    )


def test_reacquisition_replaces_only_requested_dates_and_records_provider(tmp_path) -> None:
    existing = tmp_path / "existing.csv"
    reacquired = tmp_path / "reacquired.csv"
    output = tmp_path / "dataset_v2.csv"
    _frame(
        [("2025-03-06T09:30:00-05:00", 10), ("2025-03-07T09:30:00-05:00", 20), ("2025-03-08T09:30:00-05:00", 30)]
    ).to_csv(existing, index=False)
    _frame([("2025-03-07T09:30:00-05:00", 200), ("2025-03-27T09:30:00-04:00", 270)]).to_csv(reacquired, index=False)

    merge(existing, reacquired, output, {"2025-03-07", "2025-03-27"})
    result = pd.read_csv(output)
    assert result.loc[result.time.str.startswith("2025-03-07"), "open"].item() == 200
    assert result.loc[result.time.str.startswith("2025-03-06"), "open"].item() == 10
    assert json.loads(output.with_suffix(".csv.manifest.json").read_text(encoding="utf-8"))["synthetic_bars"] is False


def test_reacquisition_rejects_a_missing_requested_date(tmp_path) -> None:
    existing = tmp_path / "existing.csv"
    reacquired = tmp_path / "reacquired.csv"
    _frame([("2025-03-07T09:30:00-05:00", 20)]).to_csv(existing, index=False)
    _frame([("2025-03-08T09:30:00-05:00", 30)]).to_csv(reacquired, index=False)
    with pytest.raises(ValueError, match="did not return requested dates"):
        merge(existing, reacquired, tmp_path / "dataset_v2.csv", {"2025-03-07"})
