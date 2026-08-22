from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DOWNLOADER = ROOT / "scripts" / "download_dukascopy.py"
DEFAULT_RAW_DIR = ROOT / "data" / "staging" / "seasonality_2016_2021"
DEFAULT_REPORT_DIR = ROOT / "outputs" / "reports" / "seasonality_10y_download"
TARGETS = {"nq": "3m", "spx": "5m"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Resumable monthly Dukascopy history download for seasonality research.")
    parser.add_argument("--from-date", default="2016-01-01")
    parser.add_argument("--to-date", default="2022-01-01")
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--pause-seconds", type=float, default=8.0)
    args = parser.parse_args()
    args.raw_dir.mkdir(parents=True, exist_ok=True)
    args.report_dir.mkdir(parents=True, exist_ok=True)

    periods = list(month_periods(args.from_date, args.to_date))
    state = load_state(args.report_dir / "status.json", periods)
    for start, end in periods:
        for preset, timeframe in TARGETS.items():
            key = f"{preset}:{start}:{end}"
            output = expected_output(args.raw_dir, preset, timeframe, start, end)
            if valid_output(output):
                state["items"][key] = item("COMPLETE", output=output)
                write_state(args.report_dir / "status.json", state)
                continue
            state["items"][key] = item("RUNNING")
            write_state(args.report_dir / "status.json", state)
            command = [
                sys.executable,
                str(DOWNLOADER),
                "--preset",
                preset,
                "--from-date",
                start,
                "--to-date",
                end,
                "--timeframes",
                timeframe,
                "--raw-dir",
                str(args.raw_dir),
                "--chunk-days",
                "10",
                "--chunk-timeout-seconds",
                "180",
                "--batch-size",
                "8",
                "--batch-pause",
                "1800",
            ]
            try:
                result = subprocess.run(command, cwd=ROOT, check=False, timeout=1800)
                if result.returncode == 0 and valid_output(output):
                    state["items"][key] = item("COMPLETE", output=output)
                else:
                    state["items"][key] = item("FAILED", error=f"exit={result.returncode}")
            except subprocess.TimeoutExpired:
                state["items"][key] = item("FAILED", error="monthly_timeout_1800s")
            write_state(args.report_dir / "status.json", state)
            time.sleep(args.pause_seconds)
    state["status"] = "COMPLETE" if all(
        value["status"] == "COMPLETE" for value in state["items"].values()
    ) else "INCOMPLETE"
    write_state(args.report_dir / "status.json", state)


def month_periods(start: str, end: str):
    current = pd.Timestamp(start)
    stop = pd.Timestamp(end)
    while current < stop:
        next_month = min(current + pd.offsets.MonthBegin(1), stop)
        yield current.strftime("%Y-%m-%d"), next_month.strftime("%Y-%m-%d")
        current = next_month


def expected_output(raw_dir: Path, preset: str, timeframe: str, start: str, end: str) -> Path:
    symbol = "DUKASCOPY_USATECHIDXUSD" if preset == "nq" else "DUKASCOPY_USA500IDXUSD"
    return raw_dir / f"{symbol}, {timeframe}_{start}_{end}.csv"


def valid_output(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 100


def item(status: str, *, output: Path | None = None, error: str | None = None) -> dict[str, object]:
    return {
        "status": status,
        "updated_at": datetime.now(UTC).isoformat(),
        "output": str(output) if output else None,
        "error": error,
    }


def load_state(path: Path, periods: list[tuple[str, str]]) -> dict[str, object]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    items = {
        f"{preset}:{start}:{end}": item("PENDING")
        for start, end in periods
        for preset in TARGETS
    }
    return {
        "status": "RUNNING",
        "feed": "DUKASCOPY_BID",
        "timezone": "America/New_York",
        "range": {"from": periods[0][0], "to": periods[-1][1]},
        "items": items,
    }


def write_state(path: Path, state: dict[str, object]) -> None:
    state["updated_at"] = datetime.now(UTC).isoformat()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    main()
