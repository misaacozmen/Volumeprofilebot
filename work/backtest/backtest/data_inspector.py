from __future__ import annotations

import csv
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd


CANONICAL_COLUMNS = {
    "time": {"time", "date", "datetime", "timestamp"},
    "open": {"open", "o"},
    "high": {"high", "h"},
    "low": {"low", "l"},
    "close": {"close", "c"},
    "volume": {"volume", "vol", "volume ma", "Volume"},
}


@dataclass(frozen=True)
class CsvInspectionReport:
    file: str
    symbol: str
    timeframe: str
    rows: int
    first_time: str
    last_time: str
    has_volume: bool
    duplicate_rows: int
    gap_count: int
    largest_gap_minutes: float | None
    expected_minutes: float | None
    status: str
    notes: str


def inspect_paths(paths: Iterable[Path], timezone: str) -> list[CsvInspectionReport]:
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(path.rglob("*.csv")))
        elif path.suffix.lower() == ".csv":
            files.append(path)

    return [inspect_csv(path, timezone=timezone) for path in sorted(set(files))]


def inspect_csv(path: Path, timezone: str) -> CsvInspectionReport:
    symbol, timeframe = infer_symbol_timeframe(path)
    notes: list[str] = []

    try:
        df = pd.read_csv(path)
    except Exception as exc:
        return CsvInspectionReport(
            file=str(path),
            symbol=symbol,
            timeframe=timeframe,
            rows=0,
            first_time="",
            last_time="",
            has_volume=False,
            duplicate_rows=0,
            gap_count=0,
            largest_gap_minutes=None,
            expected_minutes=parse_timeframe_minutes(timeframe),
            status="error",
            notes=f"CSV okunamadi: {exc}",
        )

    original_columns = list(df.columns)
    df = normalize_columns(df)
    missing = [col for col in ["time", "open", "high", "low", "close"] if col not in df.columns]
    has_volume = "volume" in df.columns
    if not has_volume:
        notes.append("volume kolonu yok")

    if missing:
        return CsvInspectionReport(
            file=str(path),
            symbol=symbol,
            timeframe=timeframe,
            rows=len(df),
            first_time="",
            last_time="",
            has_volume=has_volume,
            duplicate_rows=0,
            gap_count=0,
            largest_gap_minutes=None,
            expected_minutes=parse_timeframe_minutes(timeframe),
            status="error",
            notes=f"eksik kolonlar: {', '.join(missing)}; bulunan kolonlar: {', '.join(original_columns)}",
        )

    times = parse_times(df["time"], timezone)
    valid_times = times.dropna().sort_values()
    if valid_times.empty:
        return CsvInspectionReport(
            file=str(path),
            symbol=symbol,
            timeframe=timeframe,
            rows=len(df),
            first_time="",
            last_time="",
            has_volume=has_volume,
            duplicate_rows=0,
            gap_count=0,
            largest_gap_minutes=None,
            expected_minutes=parse_timeframe_minutes(timeframe),
            status="error",
            notes="time kolonu parse edilemedi",
        )

    duplicate_rows = int(times.duplicated(keep=False).sum())
    if duplicate_rows:
        notes.append(f"{duplicate_rows} duplicate time satiri")

    expected_minutes = parse_timeframe_minutes(timeframe)
    gap_count = 0
    largest_gap_minutes: float | None = None
    if expected_minutes:
        deltas = valid_times.diff().dropna().dt.total_seconds() / 60
        gaps = deltas[deltas > expected_minutes * 1.5]
        gap_count = int(gaps.count())
        if gap_count:
            largest_gap_minutes = float(gaps.max())
            notes.append(f"{gap_count} gap bulundu")
    else:
        notes.append("timeframe dosya adindan anlasilamadi; gap kontrolu sinirli")

    status = "ok" if has_volume and not duplicate_rows and not gap_count else "check"

    return CsvInspectionReport(
        file=str(path),
        symbol=symbol,
        timeframe=timeframe,
        rows=len(df),
        first_time=valid_times.iloc[0].isoformat(),
        last_time=valid_times.iloc[-1].isoformat(),
        has_volume=has_volume,
        duplicate_rows=duplicate_rows,
        gap_count=gap_count,
        largest_gap_minutes=largest_gap_minutes,
        expected_minutes=expected_minutes,
        status=status,
        notes="; ".join(notes),
    )


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    normalized: dict[str, str] = {}
    for column in df.columns:
        key = str(column).strip().lower()
        key = re.sub(r"\s+", " ", key)
        for canonical, aliases in CANONICAL_COLUMNS.items():
            if key in {alias.lower() for alias in aliases}:
                normalized[column] = canonical
                break
    return df.rename(columns=normalized)


def parse_times(series: pd.Series, timezone: str) -> pd.Series:
    times = pd.to_datetime(series, errors="coerce", utc=True, format="mixed")
    return times.dt.tz_convert(timezone)


def infer_symbol_timeframe(path: Path) -> tuple[str, str]:
    stem = path.stem
    match = re.search(r"(?P<symbol>[^,]+),\s*(?P<tf>\d+[A-Za-z]?)", stem)
    if match:
        return match.group("symbol").strip(), normalize_timeframe(match.group("tf"))

    parts = re.split(r"[_\-\s]+", stem)
    timeframe = "unknown"
    for part in parts:
        if re.fullmatch(r"\d+[mMhH]?", part):
            timeframe = normalize_timeframe(part)
            break
    return parts[0] if parts else stem, timeframe


def normalize_timeframe(value: str) -> str:
    value = value.strip().lower()
    if value.isdigit():
        return f"{value}m"
    return value


def parse_timeframe_minutes(timeframe: str) -> float | None:
    match = re.fullmatch(r"(\d+)([mh])", timeframe.lower())
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    return float(amount * 60 if unit == "h" else amount)


def print_reports(reports: list[CsvInspectionReport]) -> None:
    if not reports:
        print("CSV dosyasi bulunamadi.")
        return

    rows = [asdict(report) for report in reports]
    frame = pd.DataFrame(rows)
    print(frame.to_string(index=False))


def write_reports_csv(reports: list[CsvInspectionReport], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(CsvInspectionReport.__dataclass_fields__.keys())
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for report in reports:
            writer.writerow(asdict(report))
