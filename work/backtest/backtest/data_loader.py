from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .data_inspector import infer_symbol_timeframe, normalize_columns
from .integrity import conflicting_duplicate_issues, structural_ohlcv_issues


@dataclass(frozen=True)
class MarketData:
    symbol: str
    timeframe: str
    frame: pd.DataFrame


def load_ohlcv(
    paths: list[Path],
    timezone: str = "America/New_York",
    *,
    duplicate_conflict_mode: str = "identical",
) -> MarketData:
    if duplicate_conflict_mode not in {"error", "identical"}:
        raise ValueError("duplicate_conflict_mode must be 'error' or 'identical'.")
    frames: list[pd.DataFrame] = []
    symbols: set[str] = set()
    timeframes: set[str] = set()

    for path in paths:
        if path.is_dir():
            csv_paths = sorted(path.rglob("*.csv"))
        else:
            csv_paths = [path]

        for csv_path in csv_paths:
            if csv_path.suffix.lower() != ".csv":
                continue
            symbol, timeframe = infer_symbol_timeframe(csv_path)
            symbols.add(symbol)
            timeframes.add(timeframe)
            df = pd.read_csv(csv_path)
            df = normalize_columns(df)
            require_columns(df, csv_path)
            df = df[["time", "open", "high", "low", "close", "volume"]].copy()
            df["time"] = pd.to_datetime(df["time"], errors="coerce", utc=True, format="mixed").dt.tz_convert(timezone)
            df["source_file"] = str(csv_path.resolve())
            for column in ["open", "high", "low", "close", "volume"]:
                df[column] = pd.to_numeric(df[column], errors="coerce")
            source_issues = structural_ohlcv_issues(df)
            if duplicate_conflict_mode == "identical":
                source_issues = [
                    issue
                    for issue in source_issues
                    if issue.code != "DUPLICATE_BAR"
                ]
            if source_issues:
                sample = "; ".join(f"{issue.code}@{issue.time or 'frame'}" for issue in source_issues[:3])
                raise ValueError(f"Invalid OHLCV data in {csv_path}: {sample}")
            frames.append(df)

    if not frames:
        raise ValueError("CSV dosyasi bulunamadi.")
    if len(symbols) != 1 or len(timeframes) != 1:
        raise ValueError(f"Bu MVP tek sembol/timeframe bekliyor. Bulunan: symbols={symbols}, timeframes={timeframes}")

    frame = pd.concat(frames, ignore_index=True)
    conflicts = conflicting_duplicate_issues(frame)
    duplicates = frame[frame.duplicated("time", keep=False)]
    if conflicts:
        sample = "; ".join(f"{issue.time}: {', '.join(issue.source_files)}" for issue in conflicts[:3])
        raise ValueError(f"Conflicting duplicate OHLCV bars detected: {sample}")
    if not duplicates.empty and duplicate_conflict_mode == "error":
        sample_times = ", ".join(pd.Timestamp(value).isoformat() for value in duplicates["time"].drop_duplicates().head(3))
        raise ValueError(f"Duplicate OHLCV bars detected (identical values): {sample_times}")
    frame = frame.sort_values(["time", "source_file"], kind="mergesort")
    frame = frame.drop_duplicates(subset=["time"], keep="last")
    frame = frame.reset_index(drop=True)
    structural = [issue for issue in structural_ohlcv_issues(frame) if issue.code != "CONFLICTING_DUPLICATE_BAR"]
    if structural:
        sample = "; ".join(f"{issue.code}@{issue.time or 'frame'}" for issue in structural[:3])
        raise ValueError(f"Invalid OHLCV data: {sample}")
    frame["date"] = frame["time"].dt.date

    return MarketData(symbol=next(iter(symbols)), timeframe=next(iter(timeframes)), frame=frame)


def require_columns(df: pd.DataFrame, path: Path) -> None:
    missing = [column for column in ["time", "open", "high", "low", "close", "volume"] if column not in df.columns]
    if missing:
        raise ValueError(f"{path} eksik kolonlar: {', '.join(missing)}")
