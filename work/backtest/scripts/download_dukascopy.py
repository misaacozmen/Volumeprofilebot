from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

import pandas as pd


PRESETS = {
    "nq": ("usatechidxusd", "DUKASCOPY_USATECHIDXUSD"),
    "spx": ("usa500idxusd", "DUKASCOPY_USA500IDXUSD"),
    "gold": ("xauusd", "DUKASCOPY_XAUUSD"),
    "silver": ("xagusd", "DUKASCOPY_XAGUSD"),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download Dukascopy 1m data and export project-ready 3m/5m CSV files.")
    parser.add_argument(
        "--preset",
        choices=[*PRESETS.keys(), "all"],
        help="Known project instrument preset.",
    )
    parser.add_argument("--instrument", help="Dukascopy instrument id, e.g. usatechidxusd")
    parser.add_argument("--symbol", help="Output/backtest symbol, e.g. DUKASCOPY_USATECHIDXUSD")
    parser.add_argument("--from-date", required=True, help="Start date, YYYY-MM-DD")
    parser.add_argument("--to-date", required=True, help="End date, YYYY-MM-DD. Dukascopy treats this as an exclusive end.")
    parser.add_argument("--price-type", default="bid", choices=["bid", "ask"])
    parser.add_argument("--timeframes", default="3m,5m", help="Comma-separated output timeframes: 3m,5m")
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--keep-1m", action="store_true", help="Also write normalized 1m CSV.")
    parser.add_argument("--batch-size", default="20")
    parser.add_argument("--batch-pause", default="1000")
    parser.add_argument("--chunk-days", type=int, default=60, help="Download range in chunks to avoid large request failures.")
    parser.add_argument(
        "--chunk-timeout-seconds",
        type=int,
        default=180,
        help="Maximum runtime for one dukascopy-node chunk before its complete process tree is stopped.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    targets = resolve_targets(args)
    args.raw_dir.mkdir(parents=True, exist_ok=True)

    for instrument, symbol in targets:
        minute_frame = download_m1(args, instrument)
        if minute_frame.empty:
            raise SystemExit(f"No data returned for {instrument} {args.from_date} -> {args.to_date}")

        if args.keep_1m:
            write_frame(minute_frame, args.raw_dir, symbol, "1m", args.from_date, args.to_date)

        for timeframe in parse_timeframes(args.timeframes):
            output = resample_ohlcv(minute_frame, timeframe)
            output_path = write_frame(output, args.raw_dir, symbol, timeframe, args.from_date, args.to_date)
            print(f"Wrote {len(output):,} rows: {output_path}")


def resolve_targets(args: argparse.Namespace) -> list[tuple[str, str]]:
    if args.preset == "all":
        return list(PRESETS.values())
    if args.preset:
        return [PRESETS[args.preset]]
    if not args.instrument or not args.symbol:
        raise SystemExit("Use --preset or provide both --instrument and --symbol.")
    return [(args.instrument.lower(), args.symbol)]


def parse_timeframes(value: str) -> list[str]:
    timeframes: list[str] = []
    for raw in value.split(","):
        timeframe = raw.strip().lower()
        if not timeframe:
            continue
        if timeframe not in {"3m", "5m"}:
            raise SystemExit(f"Unsupported output timeframe: {raw}. Supported: 3m,5m")
        timeframes.append(timeframe)
    if not timeframes:
        raise SystemExit("--timeframes cannot be empty")
    return timeframes


def download_m1(args: argparse.Namespace, instrument: str) -> pd.DataFrame:
    with tempfile.TemporaryDirectory(prefix="dukascopy_") as tmp_dir:
        download_from = (pd.Timestamp(args.from_date) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        download_to = (pd.Timestamp(args.to_date) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        chunk_ranges = build_chunk_ranges(download_from, download_to, args.chunk_days)
        frames: list[pd.DataFrame] = []
        failed_chunks: list[dict[str, str]] = []
        chunk_number = 0
        for chunk_from, chunk_to in chunk_ranges:
            csv_paths = download_chunk_recursive(args, instrument, tmp_dir, chunk_from, chunk_to, failed_chunks)
            for csv_path in csv_paths:
                chunk_number += 1
                print(f"Normalizing chunk {chunk_number}: {csv_path.name}")
                frame = normalize_download(csv_path)
                if not frame.empty:
                    frames.append(frame)
        if failed_chunks:
            failed_path = args.raw_dir / f"dukascopy_failed_chunks_{instrument}_{args.from_date}_{args.to_date}.csv"
            pd.DataFrame(failed_chunks).to_csv(failed_path, index=False)
            print(f"WARNING: {len(failed_chunks)} chunks failed. Wrote {failed_path}")
        if not frames:
            return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
        frame = pd.concat(frames, ignore_index=True)
        frame = frame.sort_values("time").drop_duplicates(subset=["time"], keep="last")
        return filter_new_york_range(frame, args.from_date, args.to_date)


def download_chunk_recursive(
    args: argparse.Namespace,
    instrument: str,
    tmp_dir: str,
    date_from: str,
    date_to: str,
    failed_chunks: list[dict[str, str]],
) -> list[Path]:
    output_name = f"{instrument}_m1_{date_from}_{date_to}".replace("-", "")
    try:
        return [run_dukascopy_cli(args, instrument, tmp_dir, output_name, date_from, date_to)]
    except subprocess.CalledProcessError:
        start = pd.Timestamp(date_from)
        end = pd.Timestamp(date_to)
        if (end - start).days <= 1:
            failed_chunks.append({"instrument": instrument, "from": date_from, "to": date_to})
            print(f"WARNING: skipping failed one-day chunk {instrument} {date_from} -> {date_to}")
            return []
        middle = start + (end - start) / 2
        middle = pd.Timestamp(middle.date())
        if middle <= start:
            middle = start + pd.Timedelta(days=1)
        print(f"Chunk failed; splitting {date_from} -> {date_to} at {middle.strftime('%Y-%m-%d')}")
        return [
            *download_chunk_recursive(args, instrument, tmp_dir, date_from, middle.strftime("%Y-%m-%d"), failed_chunks),
            *download_chunk_recursive(args, instrument, tmp_dir, middle.strftime("%Y-%m-%d"), date_to, failed_chunks),
        ]


def build_chunk_ranges(from_date: str, to_date: str, chunk_days: int) -> list[tuple[str, str]]:
    if chunk_days < 1:
        raise SystemExit("--chunk-days must be at least 1")
    start = pd.Timestamp(from_date)
    end = pd.Timestamp(to_date)
    ranges: list[tuple[str, str]] = []
    current = start
    while current < end:
        next_end = min(current + pd.Timedelta(days=chunk_days), end)
        ranges.append((current.strftime("%Y-%m-%d"), next_end.strftime("%Y-%m-%d")))
        current = next_end
    return ranges


def run_dukascopy_cli(
    args: argparse.Namespace,
    instrument: str,
    tmp_dir: str,
    output_name: str,
    date_from: str,
    date_to: str,
) -> Path:
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if npx is None:
        raise SystemExit("npx not found. Install Node.js/npm or add npm to PATH.")
    command = [
        npx,
        "-y",
        "dukascopy-node",
        "-i",
        instrument,
        "-from",
        date_from,
        "-to",
        date_to,
        "-t",
        "m1",
        "-p",
        args.price_type,
        "-v",
        "-vu",
        "units",
        "-f",
        "csv",
        "-dir",
        tmp_dir,
        "-fn",
        output_name,
        "-bs",
        str(args.batch_size),
        "-bp",
        str(args.batch_pause),
        "-r",
        "3",
        "-rp",
        "2000",
    ]
    process = subprocess.Popen(command)
    try:
        return_code = process.wait(timeout=args.chunk_timeout_seconds)
    except subprocess.TimeoutExpired as error:
        terminate_process_tree(process.pid)
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=command,
            output=f"Timed out after {args.chunk_timeout_seconds} seconds",
        ) from error
    if return_code != 0:
        raise subprocess.CalledProcessError(return_code, command)
    csv_path = Path(tmp_dir) / f"{output_name}.csv"
    if not csv_path.exists():
        raise SystemExit(f"dukascopy-node did not create expected file: {csv_path}")
    return csv_path


def terminate_process_tree(process_id: int) -> None:
    if shutil.which("taskkill"):
        subprocess.run(
            ["taskkill", "/PID", str(process_id), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    subprocess.run(["kill", "-TERM", str(process_id)], check=False)


def normalize_download(path: Path) -> pd.DataFrame:
    columns = ["time", "open", "high", "low", "close", "volume"]
    if path.stat().st_size == 0:
        return pd.DataFrame(columns=columns)
    frame = pd.read_csv(path)
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = required.difference(frame.columns)
    if missing:
        raise SystemExit(f"{path} missing columns: {', '.join(sorted(missing))}")

    frame["time"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True).dt.tz_convert("America/New_York")
    for column in ["open", "high", "low", "close", "volume"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["time", "open", "high", "low", "close", "volume"])
    frame = frame.sort_values("time").drop_duplicates(subset=["time"], keep="last")
    return frame[columns].reset_index(drop=True)


def filter_new_york_range(frame: pd.DataFrame, from_date: str, to_date: str) -> pd.DataFrame:
    start = pd.Timestamp(from_date, tz="America/New_York")
    end = pd.Timestamp(to_date, tz="America/New_York")
    filtered = frame[(frame["time"] >= start) & (frame["time"] < end)].copy()
    return filtered.reset_index(drop=True)


def resample_ohlcv(frame: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    minutes = int(timeframe[:-1])
    indexed = frame.set_index("time")
    output = indexed.resample(f"{minutes}min", label="left", closed="left").agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    )
    output = output.dropna(subset=["open", "high", "low", "close"]).reset_index()
    return output


def write_frame(frame: pd.DataFrame, raw_dir: Path, symbol: str, timeframe: str, from_date: str, to_date: str) -> Path:
    output = frame.copy()
    output["time"] = output["time"].map(lambda value: value.isoformat())
    output = output.rename(
        columns={
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "volume": "Volume",
        }
    )
    output = output[["time", "open", "high", "low", "close", "Volume"]]
    path = raw_dir / f"{symbol}, {timeframe}_{from_date}_{to_date}.csv"
    output.to_csv(path, index=False)
    return path


if __name__ == "__main__":
    main()
