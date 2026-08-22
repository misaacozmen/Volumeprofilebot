from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

from .config import SYMBOL_CONFIGS
from .data_loader import load_ohlcv
from .volume_profile import compute_volume_profile


@dataclass(frozen=True)
class ManualExample:
    file: str
    date: str
    symbol: str
    timeframe: str
    direction: str
    tv_vah: float | None
    tv_val: float | None
    sweep_time: str
    cisd_time: str
    fvg_time: str
    fvg_kind: str
    entry: float
    stop: float
    tp: float
    result: str
    tradingview_link: str


def parse_examples(path: Path) -> list[ManualExample]:
    files = sorted(path.glob("*.md")) if path.is_dir() else [path]
    return [parse_example(file_path) for file_path in files if file_path.name not in {"README.md", "example_template.md"}]


def parse_example(path: Path) -> ManualExample:
    text = path.read_text(encoding="utf-8")

    def field(name: str, default: str = "") -> str:
        match = re.search(rf"^{re.escape(name)}\s*:\s*(.+)$", text, flags=re.IGNORECASE | re.MULTILINE)
        return match.group(1).strip() if match else default

    fvg_raw = field("FVG/IFVG saati")
    fvg_parts = fvg_raw.split()
    return ManualExample(
        file=str(path),
        date=field("Tarih"),
        symbol=field("Sembol", "CAPITALCOM_NAS100"),
        timeframe=normalize_timeframe(field("Timeframe")),
        direction=field("Yon").lower(),
        tv_vah=parse_decimal(field("TV VAH")),
        tv_val=parse_decimal(field("TV VAL")),
        sweep_time=normalize_clock(field("Sweep saati")),
        cisd_time=normalize_clock(field("CISD saati")),
        fvg_time=normalize_clock(fvg_parts[0] if fvg_parts else ""),
        fvg_kind=fvg_parts[1].lower() if len(fvg_parts) > 1 else "",
        entry=parse_decimal(field("Entry")) or 0.0,
        stop=parse_decimal(field("Stop")) or 0.0,
        tp=parse_decimal(field("TP")) or 0.0,
        result=field("Sonuc").lower(),
        tradingview_link=field("TradingView link"),
    )


def analyze_examples(examples_path: Path, raw_path: Path, output_path: Path) -> pd.DataFrame:
    examples = parse_examples(examples_path)
    rows: list[dict[str, object]] = []
    by_timeframe: dict[str, pd.DataFrame] = {}

    for example in examples:
        if example.timeframe not in by_timeframe:
            paths = sorted(raw_path.glob(f"{example.symbol}, {example.timeframe.removesuffix('m')}_*.csv"))
            market_data = load_ohlcv(paths)
            by_timeframe[example.timeframe] = market_data.frame
        frame = by_timeframe[example.timeframe]
        rows.append(analyze_example(example, frame))

    report = pd.DataFrame(rows)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report.to_csv(output_path, index=False)
    return report


def analyze_example(example: ManualExample, frame: pd.DataFrame) -> dict[str, object]:
    trade_date = pd.Timestamp(example.date).date()
    profile_start = pd.Timestamp(trade_date).tz_localize("America/New_York") - pd.Timedelta(hours=6)
    profile_end = pd.Timestamp(trade_date).tz_localize("America/New_York") + pd.Timedelta(hours=9, minutes=30)
    profile_frame = frame[(frame["time"] >= profile_start) & (frame["time"] < profile_end)]
    profile = compute_volume_profile(profile_frame)

    sweep_candle = candle_at(frame, example.date, example.sweep_time)
    cisd_candle = candle_at(frame, example.date, example.cisd_time)
    fvg_candle = candle_at(frame, example.date, example.fvg_time)

    manual_risk = abs(example.entry - example.stop)
    expected_tp = example.entry - 3 * manual_risk if example.direction == "short" else example.entry + 3 * manual_risk
    tp_diff = example.tp - expected_tp
    level_context = nearest_levels(
        example,
        frame,
        profile.vah if profile else None,
        profile.val if profile else None,
        sweep_candle,
    )

    row = asdict(example)
    row.update(
        {
            "code_vah": round(profile.vah, 2) if profile else None,
            "code_val": round(profile.val, 2) if profile else None,
            "vah_diff": round((profile.vah - example.tv_vah), 2) if profile and example.tv_vah else None,
            "val_diff": round((profile.val - example.tv_val), 2) if profile and example.tv_val else None,
            "sweep_ohlc": format_candle(sweep_candle),
            "cisd_ohlc": format_candle(cisd_candle),
            "fvg_ohlc": format_candle(fvg_candle),
            "manual_risk": round(manual_risk, 2),
            "expected_3r_tp": round(expected_tp, 2),
            "tp_diff": round(tp_diff, 2),
            "entry_mode_guess": guess_entry_mode(example, frame),
            "nearest_sweep_level": level_context,
        }
    )
    return row


def candle_at(frame: pd.DataFrame, date: str, clock: str):
    timestamp = pd.Timestamp(f"{date} {clock}").tz_localize("America/New_York")
    rows = frame[frame["time"] == timestamp]
    if rows.empty:
        return None
    return rows.iloc[0]


def nearest_levels(example: ManualExample, frame: pd.DataFrame, vah: float | None, val: float | None, sweep_candle) -> str:
    if sweep_candle is None:
        return ""
    trade_date = pd.Timestamp(example.date).date()
    levels: list[tuple[str, float]] = []
    if vah is not None:
        levels.append(("code_vah", vah))
    if val is not None:
        levels.append(("code_val", val))
    if example.tv_vah:
        levels.append(("tv_vah", example.tv_vah))
    if example.tv_val:
        levels.append(("tv_val", example.tv_val))
    levels.extend(session_levels(frame, trade_date))

    sweep_price = float(sweep_candle.high if example.direction == "short" else sweep_candle.low)
    if not levels:
        return ""
    name, price = min(levels, key=lambda item: abs(item[1] - sweep_price))
    return f"{name}:{price:.2f} diff={sweep_price - price:.2f}"


def session_levels(frame: pd.DataFrame, trade_date) -> list[tuple[str, float]]:
    day = pd.Timestamp(trade_date).tz_localize("America/New_York")
    sessions = {
        "asia": (day - pd.Timedelta(hours=4), day),
        "london": (day + pd.Timedelta(hours=2), day + pd.Timedelta(hours=5)),
        "pm": (day - pd.Timedelta(days=1) + pd.Timedelta(hours=13, minutes=30), day - pd.Timedelta(days=1) + pd.Timedelta(hours=16)),
    }
    levels: list[tuple[str, float]] = []
    for name, (start, end) in sessions.items():
        data = frame[(frame["time"] >= start) & (frame["time"] < end)]
        if not data.empty:
            levels.append((f"{name}_high", float(data["high"].max())))
            levels.append((f"{name}_low", float(data["low"].min())))
    return levels


def guess_entry_mode(example: ManualExample, frame: pd.DataFrame) -> str:
    fvg_timestamp = pd.Timestamp(f"{example.date} {example.fvg_time}").tz_localize("America/New_York")
    idx = frame.index[frame["time"] == fvg_timestamp]
    if len(idx) == 0 or idx[0] < 2:
        return ""
    third_index = int(idx[0])
    first = frame.loc[third_index - 2]
    third = frame.loc[third_index]
    if example.direction == "long" and first.high < third.low:
        start = float(first.high)
        midpoint = (float(first.high) + float(third.low)) / 2
    elif example.direction == "short" and first.low > third.high:
        start = float(first.low)
        midpoint = (float(first.low) + float(third.high)) / 2
    else:
        return "not_classic_fvg_at_manual_time"
    if abs(example.entry - midpoint) < abs(example.entry - start):
        return f"midpoint start={start:.2f} midpoint={midpoint:.2f}"
    return f"start start={start:.2f} midpoint={midpoint:.2f}"


def format_candle(candle) -> str:
    if candle is None:
        return ""
    return f"O={candle.open:.1f} H={candle.high:.1f} L={candle.low:.1f} C={candle.close:.1f}"


def parse_decimal(value: str) -> float | None:
    value = value.strip()
    if not value:
        return None
    value = value.replace(".", "").replace(",", ".") if "," in value else value
    try:
        return float(value)
    except ValueError:
        return None


def normalize_clock(value: str) -> str:
    value = value.strip().replace(".", ":")
    parts = value.split(":")
    if len(parts) == 1:
        return f"{int(parts[0]):02d}:00"
    return f"{int(parts[0]):02d}:{int(parts[1]):02d}"


def normalize_timeframe(value: str) -> str:
    value = value.strip().lower()
    return f"{value}m" if value.isdigit() else value
