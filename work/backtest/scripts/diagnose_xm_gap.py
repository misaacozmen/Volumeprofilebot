from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path

import MetaTrader5 as mt5


def utc_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a UTC offset")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only XM MT5 gap diagnostic.")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start", type=utc_datetime, required=True)
    parser.add_argument("--end", type=utc_datetime, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.end <= args.start:
        parser.error("--end must be after --start")

    terminal_path = (args.root / "mt5-terminal.txt").read_text(encoding="ascii").strip()
    if not mt5.initialize(terminal_path, timeout=60_000):
        raise SystemExit(f"initialize failed: {mt5.last_error()}")
    try:
        rates = mt5.copy_rates_range(args.symbol, mt5.TIMEFRAME_M1, args.start, args.end)
        rate_error = list(mt5.last_error()) if rates is None else None
        ticks = mt5.copy_ticks_range(args.symbol, args.start, args.end, mt5.COPY_TICKS_ALL)
        tick_error = list(mt5.last_error()) if ticks is None else None
        payload = {
            "schema_version": 1,
            "read_only": True,
            "symbol": args.symbol,
            "start": args.start.isoformat(),
            "end": args.end.isoformat(),
            "rate_count": None if rates is None else len(rates),
            "rate_error": rate_error,
            "rate_times": [] if rates is None else [int(row["time"]) for row in rates],
            "tick_count": None if ticks is None else len(ticks),
            "tick_error": tick_error,
            "first_tick_msc": None if ticks is None or len(ticks) == 0 else int(ticks[0]["time_msc"]),
            "last_tick_msc": None if ticks is None or len(ticks) == 0 else int(ticks[-1]["time_msc"]),
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
