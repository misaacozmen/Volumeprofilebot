from __future__ import annotations

from backtest.strategy import Trade, monthly_stats, summarize_trades, weekday_stats


def test_negative_open_trade_is_mark_to_market_not_realized_loss() -> None:
    trade = Trade(
        symbol="TEST",
        timeframe="5m",
        date="2025-01-02",
        direction="long",
        vah=101.0,
        val=99.0,
        trigger_level=100.0,
        liquidity_context="test",
        sweep_time="2025-01-02T09:30:00-05:00",
        cisd_time="2025-01-02T09:35:00-05:00",
        fvg_time="2025-01-02T09:40:00-05:00",
        entry_time="2025-01-02T09:45:00-05:00",
        entry_price=100.0,
        stop_price=99.0,
        target_price=102.0,
        exit_time="2025-01-02T16:00:00-05:00",
        exit_price=99.5,
        result="open_data_end",
        r_multiple=-0.5,
        notes="",
    )

    summary = summarize_trades([trade]).iloc[0]
    monthly = monthly_stats([trade]).iloc[0]
    weekday = weekday_stats([trade]).iloc[0]

    for row, loss_key in ((summary, "losses"), (monthly, "sl"), (weekday, "sl")):
        assert row[loss_key] == 0
        assert row["open_trades"] == 1
        assert row["net_r"] == 0.0
        assert row["mtm_net_r"] == -0.5
