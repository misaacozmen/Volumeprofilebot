from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import MetaTrader5 as mt5
import pandas as pd


ROOT = Path(r"C:\ForwardShadow")
NY = "America/New_York"
SYMBOL_HINTS = ("US100", "NASDAQ", "USTEC", "US500", "SP500", "S&P")


def public_fields(value: object, names: tuple[str, ...]) -> dict[str, object]:
    return {name: getattr(value, name, None) for name in names}


def missing_ranges(actual: pd.DatetimeIndex, start: pd.Timestamp, end: pd.Timestamp) -> list[dict[str, object]]:
    expected = pd.date_range(start, end, freq="1min", inclusive="left")
    missing = expected.difference(actual)
    if missing.empty:
        return []
    ranges: list[dict[str, object]] = []
    first = previous = missing[0]
    for current in missing[1:]:
        if current - previous != pd.Timedelta(minutes=1):
            ranges.append(
                {
                    "first": first.isoformat(),
                    "last": previous.isoformat(),
                    "minutes": int((previous - first) / pd.Timedelta(minutes=1)) + 1,
                }
            )
            first = current
        previous = current
    ranges.append(
        {
            "first": first.isoformat(),
            "last": previous.isoformat(),
            "minutes": int((previous - first) / pd.Timedelta(minutes=1)) + 1,
        }
    )
    return ranges


def main() -> None:
    terminal_path = (ROOT / "mt5-terminal.txt").read_text(encoding="ascii").strip()
    if not mt5.initialize(terminal_path, timeout=60_000):
        raise SystemExit(f"initialize failed: {mt5.last_error()}")
    try:
        account = mt5.account_info()
        terminal = mt5.terminal_info()
        symbols = []
        for info in mt5.symbols_get() or ():
            haystack = f"{info.name} {info.description} {info.path}".upper()
            if not any(hint in haystack for hint in SYMBOL_HINTS):
                continue
            symbols.append(
                public_fields(
                    info,
                    (
                        "name",
                        "description",
                        "path",
                        "visible",
                        "select",
                        "trade_mode",
                        "order_mode",
                        "filling_mode",
                        "digits",
                        "point",
                        "trade_stops_level",
                        "volume_min",
                        "volume_step",
                        "volume_max",
                        "currency_base",
                        "currency_profit",
                    ),
                )
            )

        configured = ["US100Cash", "US500Cash"]
        start = pd.Timestamp("2026-07-01T00:00:00Z")
        end = pd.Timestamp.now(tz="UTC").floor("min")
        feeds: dict[str, object] = {}
        for symbol in configured:
            mt5.symbol_select(symbol, True)
            rates = mt5.copy_rates_range(
                symbol,
                mt5.TIMEFRAME_M1,
                start.to_pydatetime(),
                end.to_pydatetime(),
            )
            if rates is None:
                feeds[symbol] = {"error": list(mt5.last_error())}
                continue
            utc = pd.to_datetime([int(row["time"]) for row in rates], unit="s", utc=True)
            ny = pd.DatetimeIndex(utc).tz_convert(NY)
            day_windows = {}
            for trade_date in ("2026-07-27", "2026-07-28", "2026-07-29"):
                window_start = pd.Timestamp(trade_date, tz=NY) - pd.Timedelta(hours=6)
                window_end = pd.Timestamp(f"{trade_date} 11:00", tz=NY)
                day_windows[trade_date] = missing_ranges(ny, window_start, window_end)

            schedule_evidence = []
            for trade_date in ("2026-07-07", "2026-07-13", "2026-07-14", "2026-07-20", "2026-07-21", "2026-07-27", "2026-07-28"):
                date_start = pd.Timestamp(trade_date, tz=NY)
                probe_start = date_start - pd.Timedelta(hours=6)
                probe_end = date_start
                schedule_evidence.append(
                    {
                        "trade_date": trade_date,
                        "prior_evening": missing_ranges(ny, probe_start, probe_end),
                    }
                )

            tick_windows = {}
            for label, tick_start, tick_end in (
                ("sunday_gap", "2026-07-26T22:00:00Z", "2026-07-27T02:00:00Z"),
                ("daily_gap", "2026-07-28T00:00:00Z", "2026-07-28T01:00:00Z"),
            ):
                ticks = mt5.copy_ticks_range(
                    symbol,
                    datetime.fromisoformat(tick_start.replace("Z", "+00:00")),
                    datetime.fromisoformat(tick_end.replace("Z", "+00:00")),
                    mt5.COPY_TICKS_ALL,
                )
                tick_windows[label] = {
                    "count": None if ticks is None else len(ticks),
                    "error": list(mt5.last_error()) if ticks is None else None,
                    "first_utc": (
                        None
                        if ticks is None or len(ticks) == 0
                        else datetime.fromtimestamp(int(ticks[0]["time"]), tz=timezone.utc).isoformat()
                    ),
                    "last_utc": (
                        None
                        if ticks is None or len(ticks) == 0
                        else datetime.fromtimestamp(int(ticks[-1]["time"]), tz=timezone.utc).isoformat()
                    ),
                }
            feeds[symbol] = {
                "rate_count": len(rates),
                "first_utc": None if len(utc) == 0 else utc.min().isoformat(),
                "last_utc": None if len(utc) == 0 else utc.max().isoformat(),
                "day_windows": day_windows,
                "schedule_evidence": schedule_evidence,
                "tick_windows": tick_windows,
            }

        report = {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "copyrates_input_timezone": "UTC",
            "strategy_timezone": NY,
            "account": public_fields(
                account,
                ("login", "server", "company", "trade_mode", "trade_allowed", "trade_expert"),
            ),
            "terminal": public_fields(
                terminal,
                ("connected", "trade_allowed", "tradeapi_disabled", "maxbars", "build"),
            ),
            "candidate_symbols": symbols,
            "feeds": feeds,
        }
        output = ROOT / "app" / "outputs" / "xm_mt5_forward" / "nq3m_spx5m" / "diagnostics"
        output.mkdir(parents=True, exist_ok=True)
        path = output / "xm_session_diagnostic_2026-07-29.json"
        path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        print(path)
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
