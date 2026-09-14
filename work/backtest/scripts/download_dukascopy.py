from __future__ import annotations

import argparse
from hashlib import sha256
import inspect
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pandas as pd


PRESETS = {
    "nq": ("usatechidxusd", "DUKASCOPY_USATECHIDXUSD"),
    "spx": ("usa500idxusd", "DUKASCOPY_USA500IDXUSD"),
    "gold": ("xauusd", "DUKASCOPY_XAUUSD"),
    "silver": ("xagusd", "DUKASCOPY_XAGUSD"),
}
LOCK_ROOT = Path(__file__).resolve().parents[1] / "tools" / "dukascopy-downloader"
LOCKED_VERSION = "1.50.0"
LOCKED_INTEGRITY = "sha512-o2Co/asUD/TXFNhblJUYkRseHMt/uvFrnhzOKWezLuiFJqbl4Zn2oJGL4/W+PY1b2YsI11+9+TO40qNQBAj8/w=="


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
    parser.add_argument("--raw-dir", type=Path, default=Path("data/provenance/dukascopy_v4/derived"))
    parser.add_argument("--provenance-root", type=Path, default=Path("data/provenance/dukascopy_v4"))
    parser.add_argument("--keep-1m", action="store_true", help="Also write normalized 1m CSV.")
    parser.add_argument("--session-context-hours", type=int, default=0, help="Include this many local hours before --from-date in normalized output.")
    parser.add_argument("--batch-size", default="20")
    parser.add_argument("--batch-pause", default="1000")
    parser.add_argument("--chunk-days", type=int, default=60, help="Download range in chunks to avoid large request failures.")
    parser.add_argument(
        "--request-pause-seconds",
        type=float,
        default=0.0,
        help="Minimum pause between provider requests, including recursively split chunks.",
    )
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

        publications: list[tuple[pd.DataFrame, str, Path, dict[str, object] | None]] = []
        if args.keep_1m:
            path = output_path(args.raw_dir, symbol, "1m", args.from_date, args.to_date)
            publications.append((minute_frame, "1m", path, None))
        for timeframe in parse_timeframes(args.timeframes):
            first = resample_ohlcv(minute_frame.copy(), timeframe)
            second = resample_ohlcv(minute_frame.copy(), timeframe)
            if canonical_frame_hash(first) != canonical_frame_hash(second):
                raise SystemExit(f"locked raw bytes produced non-deterministic {timeframe} resampling")
            path = output_path(args.raw_dir, symbol, timeframe, args.from_date, args.to_date)
            publications.append((first, timeframe, path, publication_manifest(args, instrument, symbol, timeframe, minute_frame, first, path)))
        paths = [path for _, _, path, _ in publications]
        paths.extend(path.with_suffix(path.suffix + ".manifest.json") for _, _, path, manifest in publications if manifest)
        existing = [path for path in paths if path.exists()]
        if existing:
            raise SystemExit(f"no-overwrite publication refused existing path: {existing[0]}")
        for output, timeframe, path, manifest in publications:
            write_frame_to_path(output, path)
            if manifest:
                write_new_bytes(path.with_suffix(path.suffix + ".manifest.json"), canonical_bytes(manifest))
            print(f"Wrote {len(output):,} rows: {path}")


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
        download_to = (
            pd.Timestamp(args.to_date)
            if args.session_context_hours
            else pd.Timestamp(args.to_date) + pd.Timedelta(days=1)
        ).strftime("%Y-%m-%d")
        chunk_ranges = build_chunk_ranges(download_from, download_to, args.chunk_days)
        frames: list[pd.DataFrame] = []
        failed_chunks: list[dict[str, str]] = []
        sealed_paths: list[Path] = []
        raw_records: list[dict[str, object]] = []
        chunk_number = 0
        for chunk_from, chunk_to in chunk_ranges:
            chunks = download_chunk_recursive(args, instrument, tmp_dir, chunk_from, chunk_to, failed_chunks)
            for csv_path, actual_from, actual_to in chunks:
                chunk_number += 1
                sealed, record = seal_raw_chunk(args, instrument, csv_path, actual_from, actual_to)
                sealed_paths.append(sealed)
                raw_records.append(record)
                print(f"Normalizing chunk {chunk_number}: {csv_path.name}")
                frame = normalize_download(sealed)
                if not frame.empty:
                    frames.append(frame)
        if failed_chunks:
            failed_bytes = canonical_bytes({"failed_attempts": failed_chunks})
            failed_hash = sha256(failed_bytes).hexdigest()
            failed_path = args.provenance_root / "failed" / f"{instrument}_{args.from_date}_{args.to_date}_{failed_hash}.json"
            if not failed_path.exists():
                write_new_bytes(failed_path, failed_bytes)
            raise SystemExit(f"{len(failed_chunks)} Dukascopy chunks failed; derived publication aborted")
        if not frames:
            return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
        frame = pd.concat(frames, ignore_index=True)
        frame = frame.sort_values("time").drop_duplicates(subset=["time"], keep="last")
        first = filter_new_york_range(frame, args.from_date, args.to_date, args.session_context_hours)
        second = filter_new_york_range(pd.concat([normalize_download(path) for path in sealed_paths], ignore_index=True).sort_values("time").drop_duplicates("time", keep="last"), args.from_date, args.to_date, args.session_context_hours)
        if canonical_frame_hash(first) != canonical_frame_hash(second):
            raise SystemExit("locked raw bytes produced non-deterministic normalization")
        args.raw_records = raw_records
        args.normalization_hash = canonical_frame_hash(first)
        return first


def download_chunk_recursive(
    args: argparse.Namespace,
    instrument: str,
    tmp_dir: str,
    date_from: str,
    date_to: str,
    failed_chunks: list[dict[str, str]],
) -> list[tuple[Path, str, str]]:
    output_name = f"{instrument}_m1_{date_from}_{date_to}".replace("-", "")
    try:
        return [(run_dukascopy_cli(args, instrument, tmp_dir, output_name, date_from, date_to), date_from, date_to)]
    except subprocess.CalledProcessError:
        start = pd.Timestamp(date_from)
        end = pd.Timestamp(date_to)
        if (end - start).days <= 1:
            failed_chunks.append({"instrument": instrument, "from": date_from, "to": date_to})
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
    lock = json.loads((LOCK_ROOT / "package-lock.json").read_text(encoding="utf-8"))
    package = lock.get("packages", {}).get("node_modules/dukascopy-node", {})
    if package.get("version") != LOCKED_VERSION or package.get("integrity") != LOCKED_INTEGRITY:
        raise SystemExit("dukascopy-node lockfile version/integrity mismatch")
    executable = LOCK_ROOT / "node_modules" / ".bin" / ("dukascopy-node.cmd" if __import__("os").name == "nt" else "dukascopy-node")
    if not executable.is_file():
        raise SystemExit("locked downloader is not installed; run npm ci in tools/dukascopy-downloader")
    command = [
        str(executable),
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
    last_request = getattr(args, "_last_request_monotonic", None)
    if last_request is not None:
        remaining = args.request_pause_seconds - (time.monotonic() - last_request)
        if remaining > 0:
            time.sleep(remaining)
    args._last_request_monotonic = time.monotonic()
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


def filter_new_york_range(frame: pd.DataFrame, from_date: str, to_date: str, context_hours: int = 0) -> pd.DataFrame:
    if context_hours < 0 or context_hours > 24:
        raise ValueError("session context hours must be between 0 and 24")
    start = pd.Timestamp(from_date, tz="America/New_York") - pd.Timedelta(hours=context_hours)
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


def output_path(raw_dir: Path, symbol: str, timeframe: str, from_date: str, to_date: str) -> Path:
    return raw_dir / f"{symbol}, {timeframe}_{from_date}_{to_date}.csv"


def write_frame(frame: pd.DataFrame, raw_dir: Path, symbol: str, timeframe: str, from_date: str, to_date: str) -> Path:
    return write_frame_to_path(frame, output_path(raw_dir, symbol, timeframe, from_date, to_date))


def write_frame_to_path(frame: pd.DataFrame, path: Path) -> Path:
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
    path.parent.mkdir(parents=True, exist_ok=True)
    output.to_csv(path, index=False, mode="x")
    return path


def canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")


def write_new_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(value)


def canonical_frame_hash(frame: pd.DataFrame) -> str:
    output = frame.copy()
    output["time"] = output["time"].map(lambda value: pd.Timestamp(value).isoformat())
    return sha256(output.to_csv(index=False, lineterminator="\n").encode("utf-8")).hexdigest()


def seal_raw_chunk(args: argparse.Namespace, instrument: str, path: Path, date_from: str, date_to: str) -> tuple[Path, dict[str, object]]:
    raw = path.read_bytes()
    digest = sha256(raw).hexdigest()
    target = args.provenance_root / "raw" / digest[:2] / f"{digest}.csv"
    if target.exists():
        if target.read_bytes() != raw:
            raise SystemExit("content-addressed raw chunk collision")
    else:
        write_new_bytes(target, raw)
    return target, {"request_from": date_from, "request_to": date_to, "instrument": instrument, "price_type": args.price_type.upper(), "raw_path": target.as_posix(), "raw_byte_count": len(raw), "raw_sha256": digest}


def publication_manifest(args: argparse.Namespace, instrument: str, symbol: str, timeframe: str, minute: pd.DataFrame, derived: pd.DataFrame, output_path: Path) -> dict[str, object]:
    duplicate_count = int(minute["time"].duplicated().sum())
    gaps = minute["time"].sort_values().diff().dropna()
    return {
        "schema_version": 4, "provider": "Dukascopy", "instrument": instrument,
        "symbol": symbol, "side": args.price_type.upper(), "source_granularity": "M1",
        "request_range": {"start": args.from_date, "end_exclusive": args.to_date},
        "session_context_hours": args.session_context_hours,
        "timezone": "America/New_York", "downloader": {"package": "dukascopy-node", "version": LOCKED_VERSION, "integrity": LOCKED_INTEGRITY, "lockfile_sha256": sha256((LOCK_ROOT / "package-lock.json").read_bytes()).hexdigest()},
        "raw_chunks": list(args.raw_records), "raw_row_bounds": {"first": minute["time"].min().isoformat(), "last": minute["time"].max().isoformat(), "rows": len(minute)},
        "duplicates": duplicate_count, "gaps_over_one_minute": int((gaps > pd.Timedelta(minutes=1)).sum()),
        "normalization_sha256": args.normalization_hash,
        "resampling": {"algorithm": "pandas left-closed/left-labelled OHLCV v1", "source_sha256": sha256(inspect.getsource(resample_ohlcv).encode("utf-8")).hexdigest(), "timeframe": timeframe},
        "parent_normalization_sha256": args.normalization_hash, "derived_sha256": canonical_frame_hash(derived),
        "output_path": output_path.as_posix(), "failed_attempts": [],
    }


if __name__ == "__main__":
    main()
