from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
STAGING = ROOT / "data" / "staging" / "seasonality_2016_2021"
RAW = ROOT / "data" / "raw"
REPORT_DIR = ROOT / "outputs" / "reports" / "seasonality_10y_download"
DOWNLOADER = ROOT / "scripts" / "download_dukascopy.py"
TARGETS = {
    "usatechidxusd": ("nq", "DUKASCOPY_USATECHIDXUSD", "3m", 130),
    "usa500idxusd": ("spx", "DUKASCOPY_USA500IDXUSD", "5m", 78),
}


def main() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    recovery_dir = STAGING / "recovered_sessions"
    recovery_dir.mkdir(parents=True, exist_ok=True)
    targets = missing_weekday_sessions()
    state = {"status": "RUNNING", "targets": targets, "updated_at": now()}
    write_state(state)
    for target in targets:
        if target["status"] == "COMPLETE":
            continue
        for attempt in range(1, 4):
            target["attempts"] = attempt
            target["status"] = "RUNNING"
            write_state(state)
            start = pd.Timestamp(target["date"])
            end = start + pd.Timedelta(days=1)
            command = [
                sys.executable,
                str(DOWNLOADER),
                "--preset",
                target["preset"],
                "--from-date",
                start.strftime("%Y-%m-%d"),
                "--to-date",
                end.strftime("%Y-%m-%d"),
                "--timeframes",
                target["timeframe"],
                "--raw-dir",
                str(recovery_dir),
                "--chunk-days",
                "1",
                "--chunk-timeout-seconds",
                "180",
                "--batch-size",
                "5",
                "--batch-pause",
                "2500",
            ]
            result = subprocess.run(command, cwd=ROOT, check=False, timeout=600)
            if result.returncode == 0 and recovered_output_exists(target, recovery_dir):
                target["status"] = "COMPLETE"
                target["error"] = None
                break
            target["status"] = "RETRY"
            target["error"] = f"exit={result.returncode}"
            write_state(state)
            time.sleep(20 * attempt)
        if target["status"] != "COMPLETE":
            target["status"] = "FAILED"
        write_state(state)
        time.sleep(8)
    state["status"] = "COMPLETE" if all(item["status"] == "COMPLETE" for item in targets) else "INCOMPLETE"
    write_state(state)


def missing_weekday_sessions() -> list[dict[str, object]]:
    failed_frames = []
    for path in STAGING.glob("dukascopy_failed_chunks_*.csv"):
        frame = pd.read_csv(path)
        if not frame.empty:
            failed_frames.append(frame)
    failed = pd.concat(failed_frames, ignore_index=True).drop_duplicates(["instrument", "from"])
    output = []
    for instrument, (preset, symbol, timeframe, expected) in TARGETS.items():
        paths = [*STAGING.rglob(f"{symbol}, {timeframe}_*.csv"), *RAW.glob(f"{symbol}, {timeframe}_*.csv")]
        dates = session_counts(paths)
        for date_text in sorted(failed.loc[failed["instrument"] == instrument, "from"].unique()):
            day = pd.Timestamp(date_text)
            if day.weekday() >= 5:
                continue
            count = int(dates.get(day.date(), 0))
            if count >= expected:
                continue
            output.append(
                {
                    "preset": preset,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "date": date_text,
                    "bars_before": count,
                    "expected": expected,
                    "status": "PENDING",
                    "attempts": 0,
                    "error": None,
                }
            )
    return output


def session_counts(paths: list[Path]) -> dict[object, int]:
    frames = []
    for path in paths:
        frame = pd.read_csv(path, usecols=["time"])
        frames.append(frame)
    combined = pd.concat(frames, ignore_index=True)
    time_column = pd.to_datetime(combined["time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
    combined = pd.DataFrame({"time": time_column}).drop_duplicates("time")
    rth = combined[
        (((combined["time"].dt.hour == 9) & (combined["time"].dt.minute >= 30)) | (combined["time"].dt.hour > 9))
        & (combined["time"].dt.hour < 16)
    ]
    return rth.groupby(rth["time"].dt.date).size().to_dict()


def recovered_output_exists(target: dict[str, object], recovery_dir: Path) -> bool:
    start = target["date"]
    end = (pd.Timestamp(start) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    path = recovery_dir / f"{target['symbol']}, {target['timeframe']}_{start}_{end}.csv"
    return path.exists() and path.stat().st_size > 100


def write_state(state: dict[str, object]) -> None:
    state["updated_at"] = now()
    path = REPORT_DIR / "recovery_status.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temporary.replace(path)


def now() -> str:
    return datetime.now(UTC).isoformat()


if __name__ == "__main__":
    main()
