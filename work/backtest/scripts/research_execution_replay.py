from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd

from backtest.volume_profile import VolumeProfile, compute_volume_profile


@dataclass(frozen=True)
class M1ExecutionReplay:
    order_id: str
    state: str
    outcome: str
    entry_time: str
    terminal_time: str
    ambiguity: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def compute_causal_m1_volume_profile(
    frame: pd.DataFrame,
    start: pd.Timestamp,
    cutoff: pd.Timestamp,
    rows: int = 1000,
    value_area_pct: float = 0.70,
) -> VolumeProfile | None:
    """Build a research profile only from M1 candles known closed by the cutoff."""
    if frame.empty:
        return None
    selected = frame.copy()
    selected["time"] = pd.to_datetime(selected["time"], utc=True, format="mixed").dt.tz_convert(start.tz)
    if "known_time" in selected:
        known = pd.to_datetime(selected["known_time"], utc=True, format="mixed").dt.tz_convert(start.tz)
    else:
        known = selected["time"] + pd.Timedelta(minutes=1)
    mask = (selected["time"] >= start) & (selected["time"] < cutoff) & (known <= cutoff)
    return compute_volume_profile(selected.loc[mask], rows=rows, value_area_pct=value_area_pct)


def _known_close(row) -> pd.Timestamp:
    value = getattr(row, "known_time", None)
    return pd.Timestamp(value) if value is not None and not pd.isna(value) else pd.Timestamp(row.time) + pd.Timedelta(minutes=1)


def replay_limit_order_m1(decision: dict[str, object] | pd.Series, minute_frame: pd.DataFrame) -> M1ExecutionReplay:
    """Replay one canonical limit order without inventing an unknown intraminute path."""
    order_id = str(decision["order_id"])
    direction = str(decision["direction"])
    entry = float(decision["entry_price"])
    stop = float(decision["stop_price"])
    target = float(decision["target_price"])
    active_at = pd.Timestamp(decision["fvg_known_time"])
    terminal_limit = pd.Timestamp(decision["terminal_known_time"])
    frame = minute_frame.copy()
    frame["time"] = pd.to_datetime(frame["time"], utc=True, format="mixed").dt.tz_convert(active_at.tz)
    frame = frame.sort_values("time", kind="mergesort")
    frame = frame[(frame["time"] >= active_at) & (frame["time"] < terminal_limit)]
    filled = False
    entry_time = ""
    last_known = active_at
    for candle in frame.itertuples(index=False):
        known_time = _known_close(candle)
        if known_time > terminal_limit:
            break
        last_known = known_time
        entry_hit = float(candle.low) <= entry <= float(candle.high)
        stop_hit = float(candle.low) <= stop if direction == "long" else float(candle.high) >= stop
        target_hit = float(candle.high) >= target if direction == "long" else float(candle.low) <= target
        if not filled:
            if target_hit and not entry_hit:
                return M1ExecutionReplay(order_id, "CANCELLED", "NO_FILL", "", known_time.isoformat(), "")
            if not entry_hit:
                continue
            entry_time = pd.Timestamp(candle.time).isoformat()
            conflicts = []
            if stop_hit:
                conflicts.append("ENTRY_AND_STOP_TOUCHED_SAME_M1")
            if target_hit:
                conflicts.append("ENTRY_AND_TARGET_TOUCHED_SAME_M1")
            if conflicts:
                return M1ExecutionReplay(order_id, "AMBIGUOUS", "WATCH", entry_time, known_time.isoformat(), "|".join(conflicts))
            filled = True
            continue
        if stop_hit and target_hit:
            return M1ExecutionReplay(order_id, "AMBIGUOUS", "WATCH", entry_time, known_time.isoformat(), "STOP_AND_TARGET_TOUCHED_SAME_M1")
        if stop_hit:
            return M1ExecutionReplay(order_id, "FILLED", "SL", entry_time, known_time.isoformat(), "")
        if target_hit:
            return M1ExecutionReplay(order_id, "FILLED", "TP", entry_time, known_time.isoformat(), "")
    return M1ExecutionReplay(order_id, "OPEN" if filled else "CANCELLED", "OPEN" if filled else "NO_FILL", entry_time, last_known.isoformat(), "")


def replay_filled_decisions_m1(decisions: pd.DataFrame, minute_frame: pd.DataFrame) -> pd.DataFrame:
    columns = list(M1ExecutionReplay.__dataclass_fields__)
    if decisions.empty:
        return pd.DataFrame(columns=columns)
    required = decisions[decisions["order_state"] == "FILLED"]
    return pd.DataFrame([replay_limit_order_m1(row, minute_frame).as_dict() for _, row in required.iterrows()], columns=columns)
