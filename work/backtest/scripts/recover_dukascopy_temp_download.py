from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.download_dukascopy import filter_new_york_range, normalize_download, parse_timeframes, resample_ohlcv, write_frame


def main() -> None:
    parser = argparse.ArgumentParser(description="Recover project CSVs from partially downloaded dukascopy-node temp CSV chunks.")
    parser.add_argument("--temp-dir", type=Path, required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--from-date", required=True)
    parser.add_argument("--to-date", required=True)
    parser.add_argument("--timeframes", default="3m,5m")
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    args = parser.parse_args()

    csv_paths = sorted(path for path in args.temp_dir.glob("*.csv") if path.stat().st_size > 0)
    if not csv_paths:
        raise SystemExit(f"No non-empty CSV chunks found in {args.temp_dir}")

    frames = []
    for index, csv_path in enumerate(csv_paths, start=1):
        print(f"Recovering chunk {index}/{len(csv_paths)}: {csv_path.name}")
        frame = normalize_download(csv_path)
        if not frame.empty:
            frames.append(frame)
    if not frames:
        raise SystemExit("No usable rows recovered.")

    minute_frame = pd.concat(frames, ignore_index=True)
    minute_frame = minute_frame.sort_values("time").drop_duplicates(subset=["time"], keep="last")
    minute_frame = filter_new_york_range(minute_frame, args.from_date, args.to_date)
    if minute_frame.empty:
        raise SystemExit("Recovered rows are outside requested date range.")

    args.raw_dir.mkdir(parents=True, exist_ok=True)
    for timeframe in parse_timeframes(args.timeframes):
        output = resample_ohlcv(minute_frame, timeframe)
        output_path = write_frame(output, args.raw_dir, args.symbol, timeframe, args.from_date, args.to_date)
        print(f"Wrote {len(output):,} rows: {output_path}")


if __name__ == "__main__":
    main()
