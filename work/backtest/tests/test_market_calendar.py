from __future__ import annotations

from datetime import date

import pytest

from backtest.market_calendar import DEFAULT_CALENDAR_PATH, MarketCalendarError, load_signed_calendar, signed_market_dates


def test_signed_calendar_covers_full_required_range_and_early_closes() -> None:
    payload, digest = load_signed_calendar()
    assert payload["calendar_id"] == "US_EQUITY_RTH_2022_2026_V2"
    assert payload["coverage"] == {"start": "2022-01-01", "end": "2026-12-31"}
    assert digest
    dates = signed_market_dates("2022-01-01", "2026-12-31")
    assert dates
    for holiday in (
        date(2022, 1, 17),
        date(2022, 7, 4),
        date(2022, 11, 24),
        date(2023, 4, 7),
        date(2025, 1, 9),
        date(2025, 2, 17),
    ):
        assert holiday not in dates
    assert {"2023-07-03", "2025-07-03"}.issubset(
        set(payload["early_close_dates"])
    )
    assert "2026-07-02" not in payload["early_close_dates"]
    assert date(2022, 1, 3) in dates
    assert date(2022, 12, 23) in dates
    assert date(2023, 12, 22) in dates


def test_signed_calendar_matches_the_immutable_2022_2026_exchange_sets() -> None:
    payload, _ = load_signed_calendar()
    assert set(payload["closed_dates"]) == {
        "2022-01-17", "2022-02-21", "2022-04-15", "2022-05-30", "2022-06-20", "2022-07-04",
        "2022-09-05", "2022-11-24", "2022-12-26", "2023-01-02", "2023-01-16", "2023-02-20",
        "2023-04-07", "2023-05-29", "2023-06-19", "2023-07-04", "2023-09-04", "2023-11-23",
        "2023-12-25", "2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27",
        "2024-06-19", "2024-07-04", "2024-09-02", "2024-11-28", "2024-12-25", "2025-01-01",
        "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26", "2025-06-19",
        "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25", "2026-01-01", "2026-01-19",
        "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
        "2026-11-26", "2026-12-25",
    }
    assert set(payload["early_close_dates"]) == {
        "2022-11-25", "2023-07-03", "2023-11-24", "2024-07-03", "2024-11-29", "2024-12-24",
        "2025-07-03", "2025-11-28", "2025-12-24", "2026-11-27", "2026-12-24",
    }


def test_signed_calendar_rejects_out_of_coverage_requests() -> None:
    with pytest.raises(MarketCalendarError, match="outside signed coverage"):
        signed_market_dates("2021-12-31", "2022-01-03")
    with pytest.raises(MarketCalendarError, match="reversed"):
        signed_market_dates("2022-01-03", "2022-01-01")


def test_custom_calendar_requires_explicit_hash_and_tamper_is_rejected(tmp_path) -> None:
    target = tmp_path / "calendar.json"
    target.write_bytes(DEFAULT_CALENDAR_PATH.read_bytes())
    with pytest.raises(MarketCalendarError, match="require an expected raw SHA-256"):
        load_signed_calendar(artifact=target)
    target.write_text(target.read_text(encoding="utf-8").replace("16:00", "15:00", 1), encoding="utf-8")
    with pytest.raises(MarketCalendarError, match="raw SHA-256 mismatch"):
        load_signed_calendar(artifact=target, expected_sha256="0" * 64)
