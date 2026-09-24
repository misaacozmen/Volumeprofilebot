from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from backtest.evaluation_window import EvaluationWindow, EvaluationWindowError, classify_sessions, coverage_report


def test_warmup_is_available_to_source_but_not_evaluation() -> None:
    frame = pd.DataFrame({"time": pd.DatetimeIndex(list(pd.date_range("2025-01-31 14:00", periods=5, freq="5min", tz="UTC")) + list(pd.date_range("2025-02-03 14:00", periods=6, freq="5min", tz="UTC")))})
    window = EvaluationWindow(date(2025, 1, 31), date(2025, 2, 3), date(2025, 2, 3), 2)
    source, evaluated = window.split_frame(frame)
    assert len(source) == 8
    assert len(evaluated) == 6
    assert window.manifest_fields(evaluated_sessions=1)["warmup_bars"] == 2


def test_dates_are_inclusive_and_outside_trades_fail() -> None:
    window = EvaluationWindow(date(2025, 2, 1), date(2025, 2, 3), date(2025, 2, 4))
    assert window.includes_session(date(2025, 2, 3))
    assert window.includes_session(date(2025, 2, 4))
    with pytest.raises(EvaluationWindowError):
        window.assert_trades_in_window(pd.DataFrame({"date": ["2025-02-05"]}))


def test_coverage_has_exactly_one_session_class() -> None:
    classification = classify_sessions(
        [date(2025, 2, 3), date(2025, 2, 4), date(2025, 2, 5), date(2025, 2, 6), date(2025, 2, 7)],
        source_dates=[date(2025, 2, 3), date(2025, 2, 5)],
        planned_closed=[date(2025, 2, 4)],
        invalid_data=[date(2025, 2, 6)],
    )
    report = coverage_report(classification, evaluated=[date(2025, 2, 3)])
    assert set(classification.values()) == {"VALID", "PLANNED_CLOSED", "INVALID_DATA", "MISSING_SOURCE"}
    assert report["requested_sessions"] == 5
