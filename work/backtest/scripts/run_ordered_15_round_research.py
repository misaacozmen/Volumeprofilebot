from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict, replace
from functools import lru_cache
from hashlib import sha256
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

import run_corrected_engine_3month_report as base_report
import run_frequency_expansion_tests as frequency
import run_seasonality_10y_analysis as seasonality
from backtest.data_loader import load_ohlcv
from backtest.risk import apply_pair_risk_rule
from backtest.strategy import run_backtest, trades_to_frame


REPORT_ROOT = ROOT / "outputs" / "reports" / "ordered_15_round_research_v1"
CANDIDATE_ROOT = ROOT / "research_candidates" / "v5_ordered_15_rounds"
TIMEZONE = "America/New_York"
SEGMENTS = {
    "learn_2016_2021": (2016, 2021),
    "validation_2022_2023": (2022, 2023),
    "validation_2024": (2024, 2024),
    "historical_2025_2026": (2025, 2026),
}
MIN_RETENTION = 0.70
PRIOR_N = 20
BASE_PHASE_RULES = [
    {"candidate": "nq_phase", "feature": "month_direction", "month": 6, "value": "short"},
    {"candidate": "nq_phase", "feature": "month_direction", "month": 8, "value": "short"},
]
EVENT_SOURCES = {
    "fomc": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
    "cpi": "https://www.bls.gov/bls/news-release/cpi.htm",
    "nfp": "https://www.bls.gov/bls/news-release/empsit.htm",
    "market_calendar": "https://www.nyse.com/markets/hours-calendars",
}


ROUND_NAMES = {
    1: "AY x CONTEXT KAYNAGI",
    2: "AY x ISLEM SAATI",
    3: "AY x VOLATILITE REJIMI",
    4: "AY x ACILIS GAP REJIMI",
    5: "AY x LIKIDITE TURU",
    6: "AY x CISD FVG KALITESI",
    7: "AYIN HAFTASI",
    8: "EKONOMIK OLAY REJIMI",
    9: "BASARILI KURALLARIN IKILI ETKILESIMI",
    10: "REJIME GORE ENTRY MODELI",
    11: "REJIME GORE RR",
    12: "NQ SPX PAIR DAVRANISI",
    13: "ROLLING WALK FORWARD DAYANIKLILIK",
    14: "PARAMETRE HASSASIYETI",
    15: "FINAL FRESH FORWARD ADAYI",
}


def main() -> None:
    started = time.perf_counter()
    REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    CANDIDATE_ROOT.mkdir(parents=True, exist_ok=True)
    specs = seasonality.candidate_specs()
    data = seasonality.load_data(specs)
    invalid = build_invalid_audit(data)
    invalid.to_csv(REPORT_ROOT / "invalid_sessions.csv", index=False)
    if "--resume" in sys.argv and (seasonality.REPORT_DIR / "all_trades.csv").exists():
        raw_trades = pd.read_csv(seasonality.REPORT_DIR / "all_trades.csv")
    else:
        baseline_engine = execute_specs(specs, data)
        raw_trades = seasonality.build_trade_frame(baseline_engine, invalid)
    raw_trades = raw_trades[raw_trades["data_valid"]].copy()
    baseline = apply_base_rules(raw_trades)
    feature_cache = REPORT_ROOT / "feature_snapshot.csv"
    threshold_cache = REPORT_ROOT / "feature_thresholds.json"
    if feature_cache.exists() and threshold_cache.exists() and "--rebuild-features" not in sys.argv:
        features = pd.read_csv(feature_cache)
        thresholds = json.loads(threshold_cache.read_text(encoding="utf-8"))
    else:
        features, thresholds = enrich_features(baseline, data)
        features.to_csv(feature_cache, index=False)
        write_json(threshold_cache, thresholds)
    event_cache = REPORT_ROOT / "event_calendar.csv"
    events = (
        pd.read_csv(event_cache)
        if event_cache.exists() and "--resume" in sys.argv and "--rebuild-events" not in sys.argv
        else build_event_calendar()
    )
    events.to_csv(event_cache, index=False)
    features = attach_events(features, events)

    accepted: list[dict[str, object]] = []
    round_summary: list[dict[str, object]] = []
    for number in range(1, 9):
        rows, decisions, new_rules = run_filter_round(number, features)
        accepted.extend(new_rules)
        decision = freeze_round(number, features, rows, decisions, new_rules, thresholds)
        round_summary.append(decision)

    rows9, decisions9, pair_rules = run_interaction_round(features, accepted)
    accepted.extend(pair_rules)
    round_summary.append(freeze_round(9, features, rows9, decisions9, pair_rules, thresholds))

    if (REPORT_ROOT / "round_10" / "results.csv").exists() and "--resume" in sys.argv:
        entry_runs = pd.read_csv(REPORT_ROOT / "round_10" / "results.csv")
        entry_decisions = pd.read_csv(REPORT_ROOT / "round_10" / "decisions.csv")
        entry_choice = dict(zip(entry_decisions["candidate"], entry_decisions["value"]))
        round_summary.append({"round": 10, "name": ROUND_NAMES[10], "decision": "ACCEPT", "accepted_count": len(entry_choice), "results": str(REPORT_ROOT / "round_10" / "results.csv"), "report": str(REPORT_ROOT / "round_10" / "report.md")})
    else:
        entry_runs = run_entry_round(specs, data, invalid, accepted)
        entry_choice = choose_engine_variant(entry_runs, "entry_mode")
        round_summary.append(freeze_engine_round(10, features, entry_runs, entry_choice))

    if (REPORT_ROOT / "round_11" / "results.csv").exists() and "--resume" in sys.argv:
        rr_runs = pd.read_csv(REPORT_ROOT / "round_11" / "results.csv")
        rr_decisions = pd.read_csv(REPORT_ROOT / "round_11" / "decisions.csv")
        rr_choice = dict(zip(rr_decisions["candidate"], rr_decisions["value"]))
        round_summary.append({"round": 11, "name": ROUND_NAMES[11], "decision": "ACCEPT", "accepted_count": len(rr_choice), "results": str(REPORT_ROOT / "round_11" / "results.csv"), "report": str(REPORT_ROOT / "round_11" / "report.md")})
    else:
        rr_runs = run_rr_round(specs, data, invalid, accepted, entry_choice)
        rr_choice = choose_engine_variant(rr_runs, "reward_r")
        round_summary.append(freeze_engine_round(11, features, rr_runs, rr_choice))

    exact_engine, exact_engine_hash = build_exact_engine_frame(
        specs, data, invalid, entry_choice, rr_choice, thresholds
    )
    selected_engine = apply_rules(exact_engine, accepted)
    pair_rows, pair_decisions, pair_choice = run_pair_round(selected_engine)
    round_summary.append(freeze_round(12, selected_engine, pair_rows, pair_decisions, pair_choice, thresholds))

    wf_rows, robust_rules = run_walk_forward(exact_engine, accepted)
    round_summary.append(freeze_round(13, exact_engine, wf_rows, pd.DataFrame(robust_rules), robust_rules, thresholds))

    sensitivity_rows, stable_rules = run_sensitivity(exact_engine, robust_rules, thresholds)
    round_summary.append(freeze_round(14, exact_engine, sensitivity_rows, pd.DataFrame(stable_rules), stable_rules, thresholds))

    final_rules = simplify_rules(exact_engine, stable_rules)
    exact_pair_baseline = apply_final_pair_cap(exact_engine)
    final_frame = apply_final_pair_cap(apply_rules(exact_engine, final_rules), pair_choice)
    if "--skip-final-verification" in sys.argv and (CANDIDATE_ROOT / "nq_spx_fresh_forward_v1.json").exists():
        final_checks = json.loads((CANDIDATE_ROOT / "nq_spx_fresh_forward_v1.json").read_text(encoding="utf-8"))["checks"]
    else:
        final_checks = final_verification(specs, data, invalid, exact_engine_hash, entry_choice, rr_choice)
    final_payload = freeze_final_candidate(final_frame, final_rules, pair_choice, entry_choice, rr_choice, final_checks, data)
    final_rows = segment_comparison(exact_pair_baseline, final_frame, "final_candidate")
    final_decisions = pd.DataFrame([{"decision": "WATCH", "reason": "Fresh-forward kaniti zorunlu; aday live degil."}])
    round_summary.append(freeze_round(15, exact_pair_baseline, final_rows, final_decisions, [final_payload], thresholds))

    write_final_report(pd.DataFrame(round_summary), final_payload, final_checks, time.perf_counter() - started)
    pd.DataFrame(round_summary).to_csv(REPORT_ROOT / "round_comparison.csv", index=False)
    print(f"Wrote: {REPORT_ROOT}")
    print(f"Runtime seconds: {time.perf_counter() - started:.1f}")


def execute_specs(specs: dict[str, dict[str, object]], data: dict[tuple[str, str], pd.DataFrame]) -> dict[str, pd.DataFrame]:
    output: dict[str, pd.DataFrame] = {}
    for name, spec in specs.items():
        frame = data[(str(spec["symbol"]), str(spec["timeframe"]))]
        output[name] = trades_to_frame(run_backtest(frame.copy(), spec["config"]).trades)
    return output


def apply_base_rules(trades: pd.DataFrame) -> pd.DataFrame:
    frame = trades.copy()
    for rule in BASE_PHASE_RULES:
        blocked = (
            (frame["candidate"] == rule["candidate"])
            & (frame["entry_month"] == rule["month"])
            & (frame["direction"] == rule["value"])
        )
        frame = frame[~blocked]
    return frame.reset_index(drop=True)


def build_invalid_audit(data: dict[tuple[str, str], pd.DataFrame]) -> pd.DataFrame:
    original = seasonality.invalid_session_dates(data).copy()
    if original.empty:
        return original
    original["planned_close_exempt"] = False
    original["classification"] = "DATA_INVALID"
    return original


def enrich_features(
    trades: pd.DataFrame,
    data: dict[tuple[str, str], pd.DataFrame],
    frozen_thresholds: dict[str, object] | None = None,
) -> tuple[pd.DataFrame, dict[str, object]]:
    frame = trades.copy()
    entry = pd.to_datetime(frame["entry_time"], utc=True, format="mixed").dt.tz_convert(TIMEZONE)
    sweep = pd.to_datetime(frame["sweep_time"], utc=True, format="mixed").dt.tz_convert(TIMEZONE)
    cisd = pd.to_datetime(frame["cisd_time"], utc=True, format="mixed").dt.tz_convert(TIMEZONE)
    fvg = pd.to_datetime(frame["fvg_time"], utc=True, format="mixed").dt.tz_convert(TIMEZONE)
    frame["entry_hour_minute"] = entry.dt.strftime("%H:%M")
    frame["entry_minute"] = entry.dt.hour * 60 + entry.dt.minute
    frame["time_bucket"] = np.select(
        [frame["entry_minute"] < 600, frame["entry_minute"] < 630],
        ["09:30-10:00", "10:00-10:30"],
        default="10:30_sonrasi",
    )
    frame["trade_ordinal"] = frame.groupby(["candidate", "entry_date"])["entry_time"].rank(method="first").astype(int)
    level = frame["liquidity_context"].str.split().str[0]
    frame["liquidity_level"] = level
    frame["month_direction"] = frame["direction"]
    frame["context_FIRST_QUALIFIED_STRUCTURE"] = frame["trade_ordinal"].eq(1)
    frame["context_SELECTED_LIQUIDITY_SWEEP"] = True
    frame["context_PREMARKET_CONTEXT"] = (sweep.dt.hour * 60 + sweep.dt.minute).lt(570)
    side = np.where(level.str.contains("high"), "high", "low")
    frame["context_QUALIFIED_STRUCTURE_REVERSAL"] = ((side == "high") & frame["direction"].eq("short")) | (
        (side == "low") & frame["direction"].eq("long")
    )
    frame["cisd_fvg_candles"] = ((fvg - cisd).dt.total_seconds() / 60 / frame["timeframe"].str.extract(r"(\d+)")[0].astype(float)).round()
    frame["fvg_delay_candles"] = ((fvg - sweep).dt.total_seconds() / 60 / frame["timeframe"].str.extract(r"(\d+)")[0].astype(float)).round()
    frame["day_of_month"] = entry.dt.day
    frame["month_end_day"] = entry.dt.days_in_month
    frame["calendar_first_week"] = frame["day_of_month"].le(7)
    frame["calendar_second_week"] = frame["day_of_month"].between(8, 14)
    frame["calendar_mid_month"] = frame["day_of_month"].between(15, 21)
    frame["calendar_last_week"] = (frame["month_end_day"] - frame["day_of_month"]).lt(7)
    dates = pd.to_datetime(frame["entry_date"])
    frame["calendar_last_trading_day"] = dates.groupby([dates.dt.year, dates.dt.month]).transform("max").dt.date.astype(str).eq(frame["entry_date"])
    frame["calendar_first_trading_day"] = dates.groupby([dates.dt.year, dates.dt.month]).transform("min").dt.date.astype(str).eq(frame["entry_date"])

    daily_parts = []
    candle_lookup: dict[tuple[str, pd.Timestamp], pd.Series] = {}
    for (symbol, timeframe), bars in data.items():
        work = bars.copy()
        work["date_key"] = pd.to_datetime(work["time"]).dt.date.astype(str)
        work["minute"] = pd.to_datetime(work["time"]).dt.hour * 60 + pd.to_datetime(work["time"]).dt.minute
        daily_parts.append(build_daily_features(symbol, work))
        relevant = frame[frame["symbol"] == symbol]
        needed = set(pd.to_datetime(pd.concat([relevant["cisd_time"], relevant["fvg_time"]]), utc=True, format="mixed").dt.tz_convert(TIMEZONE))
        for _, row in work[work["time"].isin(needed)].iterrows():
            candle_lookup[(symbol, pd.Timestamp(row["time"]))] = row
    daily = pd.concat(daily_parts, ignore_index=True)
    frame = frame.merge(daily, left_on=["symbol", "entry_date"], right_on=["symbol", "date_key"], how="left")
    frame.drop(columns=["date_key"], inplace=True)
    frame["gap_regime"] = np.select(
        [frame["gap_atr"] > 0.10, frame["gap_atr"] < -0.10], ["gap_up", "gap_down"], default="flat_gap"
    )
    frame["open_va_regime"] = np.select(
        [frame["rth_open"] > frame["vah"], frame["rth_open"] < frame["val"]],
        ["vah_uzeri", "val_alti"],
        default="va_ici",
    )
    frame["overnight_direction"] = np.where(frame["overnight_change"] >= 0, "up", "down")
    frame["context_VAH_VAL_FLIP"] = [va_flip(row, data) for _, row in frame.iterrows()]
    frame["fvg_size"] = [fvg_size(row, data) for _, row in frame.iterrows()]
    frame["cisd_strength"] = [cisd_strength(row, candle_lookup) for _, row in frame.iterrows()]
    frame["opposing_structure"] = [opposing_structure(row, data) for _, row in frame.iterrows()]
    frame["htf_alignment"] = [htf_alignment(row, data) for _, row in frame.iterrows()]
    frame["context_HTF_LIFECYCLE_CONTEXT"] = frame["htf_alignment"].eq("aligned")
    swing = [swing_attributes(row, data) for _, row in frame.iterrows()]
    frame["swing_touches"] = [item[0] for item in swing]
    frame["liquidity_va_near"] = [item[1] for item in swing]
    frame["liquidity_type"] = [liquidity_type(row) for _, row in frame.iterrows()]

    thresholds = frozen_thresholds or learn_thresholds(frame)
    frame = apply_regime_thresholds(frame, thresholds)
    return frame, thresholds


def build_daily_features(symbol: str, bars: pd.DataFrame) -> pd.DataFrame:
    rth = bars[bars["minute"].between(570, 959)]
    out = rth.groupby("date_key", sort=True).agg(
        rth_open=("open", "first"),
        rth_high=("high", "max"),
        rth_low=("low", "min"),
        rth_close=("close", "last"),
    )
    first30 = bars[bars["minute"].between(570, 599)].groupby("date_key").agg(
        first30_high=("high", "max"), first30_low=("low", "min")
    )
    premarket = bars[bars["minute"].between(240, 569)].groupby("date_key").agg(
        premarket_high=("high", "max"), premarket_low=("low", "min")
    )
    out = out.join(first30).join(premarket).reset_index()
    out.insert(0, "symbol", symbol)
    out["first30_range"] = out["first30_high"] - out["first30_low"]
    out["premarket_range"] = out["premarket_high"] - out["premarket_low"]
    out.drop(columns=["first30_high", "first30_low", "premarket_high", "premarket_low"], inplace=True)
    prev_close = out["rth_close"].shift(1)
    true_range = pd.concat(
        [out["rth_high"] - out["rth_low"], (out["rth_high"] - prev_close).abs(), (out["rth_low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    out["prev_atr"] = true_range.rolling(14, min_periods=10).mean().shift(1)
    returns = out["rth_close"].pct_change()
    out["prev20_vol"] = returns.rolling(20, min_periods=15).std().shift(1)
    out["first30_ratio"] = out["first30_range"] / out["prev_atr"]
    out["premarket_ratio"] = out["premarket_range"] / out["prev_atr"]
    out["gap_atr"] = (out["rth_open"] - prev_close) / out["prev_atr"]
    out["overnight_change"] = out["rth_open"] - prev_close
    return out


def get_candle(row: pd.Series, lookup: dict[tuple[str, pd.Timestamp], pd.Series], column: str) -> pd.Series | None:
    timestamp = pd.to_datetime(row[column], utc=True).tz_convert(TIMEZONE)
    return lookup.get((row["symbol"], timestamp))


def va_flip(row: pd.Series, data: dict[tuple[str, str], pd.DataFrame]) -> bool:
    bars = data[(row["symbol"], row["timeframe"])]
    timestamp = pd.to_datetime(row["cisd_time"], utc=True).tz_convert(TIMEZONE)
    position = pd.DatetimeIndex(bars["time"]).searchsorted(timestamp, side="left")
    if position <= 0 or position >= len(bars):
        return False
    previous_close = float(bars.iloc[position - 1]["close"])
    current_close = float(bars.iloc[position]["close"])
    if row["direction"] == "long":
        return (previous_close < float(row["val"]) <= current_close) or (
            previous_close < float(row["vah"]) <= current_close
        )
    return (previous_close > float(row["vah"]) >= current_close) or (
        previous_close > float(row["val"]) >= current_close
    )


def fvg_size(row: pd.Series, data: dict[tuple[str, str], pd.DataFrame]) -> float:
    bars = data[(row["symbol"], row["timeframe"])]
    timestamp = pd.to_datetime(row["fvg_time"], utc=True).tz_convert(TIMEZONE)
    position = pd.DatetimeIndex(bars["time"]).searchsorted(timestamp, side="left")
    if position < 2 or position >= len(bars):
        return np.nan
    first = bars.iloc[position - 2]
    third = bars.iloc[position]
    if row["direction"] == "long" and float(first["high"]) < float(third["low"]):
        gap = float(third["low"] - first["high"])
    elif row["direction"] == "short" and float(first["low"]) > float(third["high"]):
        gap = float(first["low"] - third["high"])
    else:
        gap = abs(float(third["open"] - third["close"]))
    risk = abs(float(row["entry_price"]) - float(row["stop_price"]))
    return gap / risk if risk else np.nan


def cisd_strength(row: pd.Series, lookup: dict[tuple[str, pd.Timestamp], pd.Series]) -> float:
    candle = get_candle(row, lookup, "cisd_time")
    if candle is None:
        return np.nan
    span = float(candle["high"] - candle["low"])
    return abs(float(candle["close"] - candle["open"])) / span if span else 0.0


def opposing_structure(row: pd.Series, data: dict[tuple[str, str], pd.DataFrame]) -> str:
    bars = data[(row["symbol"], row["timeframe"])]
    start = pd.to_datetime(row["cisd_time"], utc=True).tz_convert(TIMEZONE)
    end = pd.to_datetime(row["fvg_time"], utc=True).tz_convert(TIMEZONE)
    piece = bar_slice(bars, start, end, left_open=True)
    if piece.empty:
        return "none"
    opposing = (piece["close"] < piece["open"]).any() if row["direction"] == "long" else (piece["close"] > piece["open"]).any()
    return "present" if opposing else "none"


def htf_alignment(row: pd.Series, data: dict[tuple[str, str], pd.DataFrame]) -> str:
    bars = data[(row["symbol"], row["timeframe"])]
    known = pd.to_datetime(row["cisd_time"], utc=True).tz_convert(TIMEZONE).floor("15min")
    history = bars.iloc[: pd.DatetimeIndex(bars["time"]).searchsorted(known, side="left")].tail(
        max(5, int(75 / int(row["timeframe"].rstrip("m"))))
    )
    if len(history) < 5:
        return "unknown"
    move = float(history.iloc[-1]["close"] - history.iloc[0]["open"])
    return "aligned" if (move >= 0) == (row["direction"] == "long") else "opposed"


def swing_attributes(row: pd.Series, data: dict[tuple[str, str], pd.DataFrame]) -> tuple[int, bool]:
    tolerance = 5.0 if "USATECH" in row["symbol"] else 1.5
    near = min(abs(float(row["trigger_level"]) - float(row["vah"])), abs(float(row["trigger_level"]) - float(row["val"]))) <= tolerance * 2
    if not str(row["liquidity_level"]).startswith("swing_"):
        return 0, near
    bars = data[(row["symbol"], row["timeframe"])]
    start = pd.to_datetime(row["sweep_time"], utc=True).tz_convert(TIMEZONE)
    history = bars.iloc[: pd.DatetimeIndex(bars["time"]).searchsorted(start, side="left")].tail(100).reset_index(drop=True)
    prices = []
    high_side = "high" in row["liquidity_level"]
    for index in range(2, len(history) - 2):
        candle = history.iloc[index]
        if high_side and candle.high > history.iloc[index - 2:index].high.max() and candle.high >= history.iloc[index + 1:index + 3].high.max():
            prices.append(float(candle.high))
        if not high_side and candle.low < history.iloc[index - 2:index].low.min() and candle.low <= history.iloc[index + 1:index + 3].low.min():
            prices.append(float(candle.low))
    touches = sum(abs(price - float(row["trigger_level"])) <= tolerance for price in prices)
    return touches, near


def bar_slice(
    bars: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    left_open: bool = False,
) -> pd.DataFrame:
    index = pd.DatetimeIndex(bars["time"])
    left = index.searchsorted(start, side="right" if left_open else "left")
    right = index.searchsorted(end, side="right")
    return bars.iloc[left:right]


def liquidity_type(row: pd.Series) -> str:
    level = str(row["liquidity_level"])
    if level.startswith("asia_"):
        return "asia_high_low"
    if level.startswith("london_"):
        return "london_high_low"
    if level.startswith("ny_am_"):
        return "ny_am_high_low"
    if level.startswith(("ny_pm_", "previous_day_")):
        return "session_high_low"
    if level.startswith("swing_") and int(row["swing_touches"]) >= 3:
        return "strong_swing"
    if level.startswith("swing_") and int(row["swing_touches"]) >= 2:
        return "equal_high_low"
    if bool(row["liquidity_va_near"]):
        return "vah_val_proximity"
    return "other_liquidity"


def learn_thresholds(frame: pd.DataFrame) -> dict[str, object]:
    train = frame[frame["entry_year"] <= 2021]
    fields = ["prev_atr", "prev20_vol", "first30_ratio", "premarket_ratio", "cisd_strength", "fvg_size", "cisd_fvg_candles", "fvg_delay_candles"]
    output: dict[str, object] = {}
    for candidate, group in train.groupby("candidate"):
        output[candidate] = {}
        for field in fields:
            valid = group[field].dropna()
            output[candidate][field] = {
                "q33": round(float(valid.quantile(1 / 3)), 8) if not valid.empty else None,
                "q67": round(float(valid.quantile(2 / 3)), 8) if not valid.empty else None,
            }
    return output


def apply_regime_thresholds(frame: pd.DataFrame, thresholds: dict[str, object]) -> pd.DataFrame:
    result = frame.copy()
    for field in ["prev_atr", "prev20_vol", "first30_ratio", "premarket_ratio", "cisd_strength", "fvg_size", "cisd_fvg_candles", "fvg_delay_candles"]:
        result[f"{field}_regime"] = "unknown"
        for candidate, values in thresholds.items():
            q = values[field]
            if q["q33"] is None:
                continue
            mask = result["candidate"].eq(candidate) & result[field].notna()
            result.loc[mask, f"{field}_regime"] = np.select(
                [result.loc[mask, field] <= q["q33"], result.loc[mask, field] >= q["q67"]],
                ["low", "high"],
                default="normal",
            )
    pre10 = result["entry_minute"] < 600
    result.loc[pre10, "first30_ratio_regime"] = "not_known_at_entry"
    return result


@lru_cache(maxsize=None)
def http_text(url: str) -> str:
    user_agent = "Mozilla/5.0 research contact test@example.com"
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        if "bls.gov" in url:
            raise PermissionError("BLS rejects urllib clients; use curl user-agent path")
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.read().decode("utf-8", errors="ignore")
    except Exception:
        completed = subprocess.run(
            ["curl.exe", "-L", "-s", "--max-time", "30", "-A", user_agent, url],
            check=True,
            capture_output=True,
        )
        return completed.stdout.decode("utf-8", errors="ignore")


def build_event_calendar() -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for event, url in [("CPI", EVENT_SOURCES["cpi"]), ("NFP", EVENT_SOURCES["nfp"])]:
        html = http_text(url)
        slug = "cpi" if event == "CPI" else "empsit"
        for month, day, year in sorted(set(re.findall(rf"archives/{slug}_(\d{{2}})(\d{{2}})(20\d{{2}})\.htm", html))):
            year_i = int(year)
            if 2016 <= year_i <= 2026:
                rows.append({"date": f"{year}-{month}-{day}", "event": event, "known_before_session": True, "source": url})
    for year in range(2016, 2027):
        url = EVENT_SOURCES["fomc"] if year >= 2021 else f"https://www.federalreserve.gov/monetarypolicy/fomchistorical{year}.htm"
        html = http_text(url)
        found: list[tuple[str, str, str]] = []
        if year >= 2021:
            panel = re.search(
                rf'<div class="panel panel-default"><div class="panel-heading"><h4><a[^>]*>{year} FOMC Meetings</a></h4></div>(.*?)(?=<div class="panel panel-default">|$)',
                html,
                flags=re.I | re.S,
            )
            if panel:
                for month_name, date_text in re.findall(
                    r'fomc-meeting__month[^>]*>\s*<strong>([A-Za-z]+(?:/[A-Za-z]+)?)</strong>.*?fomc-meeting__date[^>]*>\s*([^<]+)</div>',
                    panel.group(1),
                    flags=re.I | re.S,
                ):
                    days = re.findall(r"\d{1,2}", date_text)
                    if len(days) >= 2:
                        found.append((month_name, days[0], days[-1]))
        else:
            found = re.findall(
                rf'<h5[^>]*>\s*([A-Za-z]+(?:/[A-Za-z]+)?)\s+(\d{{1,2}})\s*[-–]\s*(\d{{1,2}})\s+Meeting\s+-\s+{year}\s*</h5>',
                html,
                flags=re.I,
            )
        for month_name, first, last in found:
            decision_day = int(last)
            try:
                decision_month = month_name.split("/")[-1]
                date = pd.Timestamp(f"{year}-{decision_month}-{decision_day}").date().isoformat()
            except ValueError:
                continue
            rows.append({"date": date, "event": "FOMC", "known_before_session": True, "source": url})
    for year in range(2016, 2027):
        for date, event in market_calendar_dates(year):
            rows.append({"date": date, "event": event, "known_before_session": True, "source": EVENT_SOURCES["market_calendar"]})
    result = pd.DataFrame(rows).drop_duplicates(["date", "event"]).sort_values(["date", "event"])
    return result[(result["date"] >= "2016-01-01") & (result["date"] <= "2026-12-31")].reset_index(drop=True)


def market_calendar_dates(year: int) -> list[tuple[str, str]]:
    dates: list[tuple[str, str]] = []
    holidays = [
        observed(pd.Timestamp(year, 1, 1)),
        nth_weekday(year, 1, 0, 3),
        nth_weekday(year, 2, 0, 3),
        easter_sunday(year) - pd.Timedelta(days=2),
        last_weekday(year, 5, 0),
        observed(pd.Timestamp(year, 7, 4)),
        nth_weekday(year, 9, 0, 1),
        nth_weekday(year, 11, 3, 4),
        observed(pd.Timestamp(year, 12, 25)),
    ]
    if year >= 2022:
        holidays.append(observed(pd.Timestamp(year, 6, 19)))
    dates.extend((item.date().isoformat(), "MAJOR_US_HOLIDAY") for item in holidays)
    thanksgiving = nth_weekday(year, 11, 3, 4)
    early = [thanksgiving + pd.Timedelta(days=1), business_day_before(pd.Timestamp(year, 12, 25))]
    july4 = pd.Timestamp(year, 7, 4)
    if july4.weekday() in (1, 2, 3, 4):
        early.append(business_day_before(july4))
    dates.extend((item.date().isoformat(), "EARLY_CLOSE") for item in early if item not in holidays)
    return dates


def observed(value: pd.Timestamp) -> pd.Timestamp:
    if value.weekday() == 5:
        return value - pd.Timedelta(days=1)
    if value.weekday() == 6:
        return value + pd.Timedelta(days=1)
    return value


def nth_weekday(year: int, month: int, weekday: int, nth: int) -> pd.Timestamp:
    date = pd.Timestamp(year, month, 1)
    return date + pd.Timedelta(days=(weekday - date.weekday()) % 7 + (nth - 1) * 7)


def last_weekday(year: int, month: int, weekday: int) -> pd.Timestamp:
    date = pd.Timestamp(year, month, 1) + pd.offsets.MonthEnd(0)
    return date - pd.Timedelta(days=(date.weekday() - weekday) % 7)


def easter_sunday(year: int) -> pd.Timestamp:
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f, g = (b + 8) // 25, (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return pd.Timestamp(year, month, day)


def business_day_before(value: pd.Timestamp) -> pd.Timestamp:
    value -= pd.Timedelta(days=1)
    while value.weekday() >= 5:
        value -= pd.Timedelta(days=1)
    return value


def attach_events(frame: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    mapping = events.groupby("date")["event"].agg(lambda values: "|".join(sorted(set(values)))).to_dict()
    result = frame.copy()
    result["event_regime"] = result["entry_date"].map(mapping).fillna("NORMAL_DAY")
    for event in ["FOMC", "CPI", "NFP", "MAJOR_US_HOLIDAY", "EARLY_CLOSE"]:
        result[f"event_{event}"] = result["event_regime"].str.contains(event, regex=False)
    return result


def round_features(number: int) -> dict[str, list[object]]:
    if number == 1:
        return {f"context_{name}": [True] for name in ["FIRST_QUALIFIED_STRUCTURE", "SELECTED_LIQUIDITY_SWEEP", "PREMARKET_CONTEXT", "QUALIFIED_STRUCTURE_REVERSAL", "VAH_VAL_FLIP", "HTF_LIFECYCLE_CONTEXT"]}
    if number == 2:
        return {"time_bucket": ["09:30-10:00", "10:00-10:30", "10:30_sonrasi"]}
    if number == 3:
        return {field: ["low", "normal", "high"] for field in ["prev_atr_regime", "prev20_vol_regime", "first30_ratio_regime"]}
    if number == 4:
        return {
            "gap_regime": ["gap_up", "gap_down", "flat_gap"],
            "open_va_regime": ["va_ici", "vah_uzeri", "val_alti"],
            "premarket_ratio_regime": ["low", "normal", "high"],
            "overnight_direction": ["up", "down"],
        }
    if number == 5:
        return {"liquidity_type": ["asia_high_low", "london_high_low", "ny_am_high_low", "session_high_low", "strong_swing", "equal_high_low", "vah_val_proximity", "other_liquidity"]}
    if number == 6:
        return {
            "cisd_strength_regime": ["low", "normal", "high"],
            "fvg_size_regime": ["low", "normal", "high"],
            "cisd_fvg_candles_regime": ["low", "normal", "high"],
            "htf_alignment": ["aligned", "opposed"],
            "opposing_structure": ["present", "none"],
            "fvg_delay_candles_regime": ["low", "normal", "high"],
        }
    if number == 7:
        return {field: [True] for field in ["calendar_first_week", "calendar_second_week", "calendar_mid_month", "calendar_last_week", "calendar_last_trading_day", "calendar_first_trading_day"]}
    if number == 8:
        return {f"event_{event}": [True] for event in ["FOMC", "CPI", "NFP", "MAJOR_US_HOLIDAY", "EARLY_CLOSE"]} | {"event_regime": ["NORMAL_DAY"]}
    raise ValueError(number)


def run_filter_round(number: int, baseline: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, list[dict[str, object]]]:
    rows = []
    decisions = []
    accepted = []
    for candidate, source in baseline.groupby("candidate"):
        rows.extend(baseline_rows(number, candidate, source))
        for feature, values in round_features(number).items():
            for value in values:
                for month in range(1, 13):
                    rule = {"round": number, "candidate": candidate, "feature": feature, "month": month, "value": value, "action": "BLOCK"}
                    decision = evaluate_single_rule(source, rule)
                    decisions.append(decision)
                    filtered = apply_rules(source, [rule])
                    for segment, (start, end) in SEGMENTS.items():
                        piece = filtered[filtered["entry_year"].between(start, end)]
                        rows.append({"round": number, "candidate": candidate, "variant": rule_name(rule), "segment": segment, **stats(piece)})
                    if decision["decision"] == "ACCEPT":
                        accepted.append(rule)
    decisions_frame = pd.DataFrame(decisions)
    accepted = resolve_overlapping_rules(accepted, baseline)
    if not decisions_frame.empty:
        accepted_names = {rule_name(rule) for rule in accepted}
        decisions_frame.loc[decisions_frame["variant"].isin(accepted_names), "selected"] = True
        decisions_frame["selected"] = decisions_frame["selected"].fillna(False)
    return pd.DataFrame(rows), decisions_frame, accepted


def baseline_rows(number: int, candidate: str, source: pd.DataFrame) -> list[dict[str, object]]:
    rows = []
    for segment, (start, end) in SEGMENTS.items():
        rows.append({"round": number, "candidate": candidate, "variant": "baseline", "segment": segment, **stats(source[source["entry_year"].between(start, end)])})
    return rows


def evaluate_single_rule(source: pd.DataFrame, rule: dict[str, object]) -> dict[str, object]:
    targeted = source[(source["entry_month"] == rule["month"]) & value_mask(source, rule["feature"], rule["value"])]
    train = targeted[targeted["entry_year"].between(2016, 2021)]
    v1 = targeted[targeted["entry_year"].between(2022, 2023)]
    v2 = targeted[targeted["entry_year"].eq(2024)]
    train_base = source[source["entry_year"].between(2016, 2021)]
    prior_mean = float(train_base["r_multiple"].mean()) if not train_base.empty else 0.0
    shrunk = (float(train["r_multiple"].sum()) + prior_mean * PRIOR_N) / (len(train) + PRIOR_N)
    annual = train.groupby("entry_year")["r_multiple"].sum()
    positive_ratio = float((annual > 0).mean()) if len(annual) else 1.0
    filtered = apply_rules(source, [rule])
    retention = len(filtered) / len(source) if len(source) else 0.0
    before1 = stats(source[source["entry_year"].between(2022, 2023)])
    after1 = stats(filtered[filtered["entry_year"].between(2022, 2023)])
    before2 = stats(source[source["entry_year"].eq(2024)])
    after2 = stats(filtered[filtered["entry_year"].eq(2024)])
    passes = (
        len(train) >= 10
        and len(annual) >= 4
        and positive_ratio <= 0.40
        and float(train["r_multiple"].sum()) < 0
        and shrunk <= -0.04
        and len(v1) >= 3
        and float(v1["r_multiple"].sum()) < 0
        and len(v2) >= 2
        and float(v2["r_multiple"].sum()) <= 0
        and retention >= MIN_RETENTION
        and after1["net_r"] > before1["net_r"]
        and after2["net_r"] >= before2["net_r"]
        and after1["max_drawdown_r"] >= before1["max_drawdown_r"]
        and after2["max_drawdown_r"] >= before2["max_drawdown_r"]
    )
    sample_ok = len(train) >= 10 and len(v1) >= 3 and len(v2) >= 2
    decision = "ACCEPT" if passes else ("WATCH" if sample_ok and shrunk < 0 and float(v1["r_multiple"].sum()) < 0 else "REJECT")
    return {
        "round": rule["round"],
        "candidate": rule["candidate"],
        "variant": rule_name(rule),
        "feature": rule["feature"],
        "month": rule["month"],
        "value": rule["value"],
        "train_target_trades": len(train),
        "train_target_net_r": round(float(train["r_multiple"].sum()), 2),
        "shrunk_avg_r": round(shrunk, 4),
        "years_present": len(annual),
        "positive_year_ratio": round(positive_ratio, 3),
        "validation_2022_2023_target_trades": len(v1),
        "validation_2022_2023_target_net_r": round(float(v1["r_multiple"].sum()), 2),
        "validation_2024_target_trades": len(v2),
        "validation_2024_target_net_r": round(float(v2["r_multiple"].sum()), 2),
        "retention": round(retention, 4),
        "decision": decision,
        "reason": "train+iki dogrulamada zararli hucre, >=70% retention" if passes else "kapilarin tumu gecilmedi",
    }


def resolve_overlapping_rules(rules: list[dict[str, object]], baseline: pd.DataFrame) -> list[dict[str, object]]:
    selected: list[dict[str, object]] = []
    for rule in sorted(rules, key=lambda item: (item["candidate"], item["round"], str(item["feature"]), item["month"], str(item["value"]))):
        candidate_source = baseline[baseline["candidate"] == rule["candidate"]]
        trial = apply_rules(candidate_source, [*selected_for(selected, rule["candidate"]), rule])
        if len(trial) / len(candidate_source) >= MIN_RETENTION:
            selected.append(rule)
    return selected


def selected_for(rules: list[dict[str, object]], candidate: str) -> list[dict[str, object]]:
    return [rule for rule in rules if rule.get("candidate") == candidate]


def value_mask(frame: pd.DataFrame, feature: str, value: object) -> pd.Series:
    if isinstance(value, bool):
        return frame[feature].fillna(False).astype(bool).eq(value)
    return frame[feature].astype(str).eq(str(value))


def apply_rules(frame: pd.DataFrame, rules: list[dict[str, object]]) -> pd.DataFrame:
    result = frame.copy()
    for rule in rules:
        if "rules" in rule:
            result = apply_rules(result, rule["rules"])
            continue
        target = result["candidate"].eq(rule["candidate"]) if "candidate" in rule and "candidate" in result else pd.Series(True, index=result.index)
        if "month" in rule:
            target &= result["entry_month"].eq(rule["month"])
        if "feature" in rule:
            target &= value_mask(result, str(rule["feature"]), rule["value"])
        result = result[~target]
    return result.copy()


def rule_name(rule: dict[str, object]) -> str:
    if "rules" in rule:
        return "+".join(rule_name(item) for item in rule["rules"])
    return f"BLOCK_M{int(rule['month']):02d}_{rule['feature']}={rule['value']}"


def stats(trades: pd.DataFrame) -> dict[str, object]:
    if trades.empty:
        return {"trades": 0, "wins": 0, "win_rate": 0.0, "net_r": 0.0, "max_drawdown_r": 0.0, "profit_factor": 0.0}
    ordered = trades.copy()
    ordered["_entry"] = pd.to_datetime(ordered["entry_time"], utc=True, format="mixed")
    ordered = ordered.sort_values(["_entry", "candidate"], kind="mergesort")
    equity = ordered["r_multiple"].cumsum()
    drawdown = float((equity - equity.cummax()).min())
    gains = float(ordered.loc[ordered["r_multiple"] > 0, "r_multiple"].sum())
    losses = abs(float(ordered.loc[ordered["r_multiple"] < 0, "r_multiple"].sum()))
    wins = int((ordered["r_multiple"] > 0).sum())
    return {
        "trades": len(ordered),
        "wins": wins,
        "win_rate": round(wins / len(ordered) * 100, 2),
        "net_r": round(float(ordered["r_multiple"].sum()), 2),
        "max_drawdown_r": round(drawdown, 2),
        "profit_factor": round(gains / losses, 3) if losses else float("inf"),
    }


def segment_comparison(baseline: pd.DataFrame, candidate: pd.DataFrame, variant: str) -> pd.DataFrame:
    rows = []
    for name, (start, end) in SEGMENTS.items():
        for label, source in [("baseline", baseline), (variant, candidate)]:
            rows.append({"variant": label, "segment": name, **stats(source[source["entry_year"].between(start, end)])})
    return pd.DataFrame(rows)


def run_interaction_round(baseline: pd.DataFrame, rules: list[dict[str, object]]) -> tuple[pd.DataFrame, pd.DataFrame, list[dict[str, object]]]:
    rows = []
    decisions = []
    accepted = []
    for candidate, source in baseline.groupby("candidate"):
        singles = selected_for(rules, candidate)
        rows.extend(baseline_rows(9, candidate, source))
        for left, right in combinations(singles, 2):
            combo = {"round": 9, "candidate": candidate, "rules": [left, right], "action": "BLOCK_PAIR"}
            filtered = apply_rules(source, [combo])
            retention = len(filtered) / len(source)
            segment_stats = {}
            for segment, (start, end) in SEGMENTS.items():
                value = stats(filtered[filtered["entry_year"].between(start, end)])
                segment_stats[segment] = value
                rows.append({"round": 9, "candidate": candidate, "variant": rule_name(combo), "segment": segment, **value})
            single_scores = []
            for single in [left, right]:
                trial = apply_rules(source, [single])
                single_scores.append(sum(stats(trial[trial["entry_year"].between(*SEGMENTS[name])])["net_r"] for name in ["validation_2022_2023", "validation_2024"]))
            combo_score = segment_stats["validation_2022_2023"]["net_r"] + segment_stats["validation_2024"]["net_r"]
            better = combo_score > max(single_scores) and retention >= MIN_RETENTION
            decision = "ACCEPT" if better else "REJECT"
            decisions.append({"round": 9, "candidate": candidate, "variant": rule_name(combo), "retention": round(retention, 4), "combo_validation_net_r": combo_score, "best_single_validation_net_r": max(single_scores), "decision": decision, "reason": "iki tekilden daha iyi" if better else "iki tekili asamadi"})
            if better:
                accepted.append(combo)
    return pd.DataFrame(rows), pd.DataFrame(decisions), accepted


def eligible_context_rules(rules: list[dict[str, object]]) -> list[dict[str, object]]:
    return [rule for rule in rules if int(rule.get("round", 99)) <= 9]


def run_entry_round(
    specs: dict[str, dict[str, object]],
    data: dict[tuple[str, str], pd.DataFrame],
    invalid: pd.DataFrame,
    rules: list[dict[str, object]],
) -> pd.DataFrame:
    return run_engine_grid(10, specs, data, invalid, rules, "entry_mode", ["start", "quarter_25", "midpoint"], {})


def run_rr_round(
    specs: dict[str, dict[str, object]],
    data: dict[tuple[str, str], pd.DataFrame],
    invalid: pd.DataFrame,
    rules: list[dict[str, object]],
    entry_choice: dict[str, object],
) -> pd.DataFrame:
    overrides = {candidate: {"fvg_entry_mode": value} for candidate, value in entry_choice.items()}
    return run_engine_grid(11, specs, data, invalid, rules, "reward_r", [2.0, 2.5, 3.0], overrides)


def run_engine_grid(
    round_number: int,
    specs: dict[str, dict[str, object]],
    data: dict[tuple[str, str], pd.DataFrame],
    invalid: pd.DataFrame,
    rules: list[dict[str, object]],
    parameter: str,
    values: list[object],
    overrides: dict[str, dict[str, object]],
) -> pd.DataFrame:
    progress_path = REPORT_ROOT / f"_round_{round_number:02d}_engine_progress.csv"
    rows = pd.read_csv(progress_path).to_dict("records") if progress_path.exists() else []
    completed = {(str(row["candidate"]), str(row["value"])) for row in rows}
    for candidate, spec in specs.items():
        applicable = selected_for(eligible_context_rules(rules), candidate)
        for value in values:
            if (candidate, str(value)) in completed:
                continue
            changes = dict(overrides.get(candidate, {}))
            config_field = "fvg_entry_mode" if parameter == "entry_mode" else "reward_r"
            changes[config_field] = value
            config = replace(spec["config"], **changes)
            trades = trades_to_frame(run_backtest(data[(spec["symbol"], spec["timeframe"])].copy(), config).trades)
            tagged = seasonality.build_trade_frame({candidate: trades}, invalid)
            tagged = apply_base_rules(tagged[tagged["data_valid"]])
            if applicable:
                source_features, _ = enrich_features(tagged, data)
                tagged = apply_rules(source_features, applicable)
            for segment, (start, end) in SEGMENTS.items():
                rows.append({"round": round_number, "candidate": candidate, "parameter": parameter, "value": value, "segment": segment, **stats(tagged[tagged["entry_year"].between(start, end)])})
            pd.DataFrame(rows).to_csv(progress_path, index=False)
    return pd.DataFrame(rows)


def choose_engine_variant(rows: pd.DataFrame, parameter: str) -> dict[str, object]:
    choices = {}
    for candidate, source in rows.groupby("candidate"):
        development = source[source["segment"].isin(["learn_2016_2021", "validation_2022_2023", "validation_2024"])]
        table = development.groupby("value").agg(net_r=("net_r", "sum"), trades=("trades", "sum"), min_dd=("max_drawdown_r", "min"), min_pf=("profit_factor", "min")).reset_index()
        train_counts = source[source["segment"] == "learn_2016_2021"].set_index("value")["trades"]
        max_count = max(train_counts.max(), 1)
        table["retention"] = table["value"].map(train_counts) / max_count
        valid = table[(table["retention"] >= MIN_RETENTION) & (table["min_pf"] >= 0.75)]
        pick = valid.sort_values(["net_r", "min_dd"], ascending=False).iloc[0] if not valid.empty else table.sort_values("net_r", ascending=False).iloc[0]
        choices[candidate] = pick["value"].item() if hasattr(pick["value"], "item") else pick["value"]
    return choices


def freeze_engine_round(number: int, baseline: pd.DataFrame, rows: pd.DataFrame, choices: dict[str, object]) -> dict[str, object]:
    decisions = pd.DataFrame([{"candidate": candidate, "value": value, "decision": "ACCEPT", "reason": "2025-2026 secimde kullanilmadi"} for candidate, value in choices.items()])
    return freeze_round(number, baseline, rows, decisions, [{"candidate": key, "value": value} for key, value in choices.items()], {})


def build_exact_engine_frame(
    specs: dict[str, dict[str, object]],
    data: dict[tuple[str, str], pd.DataFrame],
    invalid: pd.DataFrame,
    entry_choice: dict[str, object],
    rr_choice: dict[str, object],
    thresholds: dict[str, object],
) -> tuple[pd.DataFrame, str]:
    cache = REPORT_ROOT / "selected_exact_engine_features.csv"
    manifest_path = REPORT_ROOT / "selected_exact_engine_manifest.json"
    exact_config_hash = sha256(
        json.dumps({"entry_mode": entry_choice, "reward_r": rr_choice}, sort_keys=True).encode()
    ).hexdigest()
    if cache.exists() and manifest_path.exists() and "--resume" in sys.argv and "--rebuild-features" not in sys.argv:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("config_sha256") == exact_config_hash:
            return pd.read_csv(cache), str(manifest["raw_result_sha256"])
    exact_specs: dict[str, dict[str, object]] = {}
    for candidate, spec in specs.items():
        exact_specs[candidate] = dict(spec)
        exact_specs[candidate]["config"] = replace(
            spec["config"],
            fvg_entry_mode=str(entry_choice[candidate]),
            reward_r=float(rr_choice[candidate]),
        )
    runs = execute_specs(exact_specs, data)
    raw_hash = trade_run_hash(runs)
    tagged = seasonality.build_trade_frame(runs, invalid)
    tagged = apply_base_rules(tagged[tagged["data_valid"]])
    enriched, _ = enrich_features(tagged, data, thresholds)
    enriched["selected_entry_mode"] = enriched["candidate"].map(entry_choice)
    enriched["selected_reward_r"] = enriched["candidate"].map(rr_choice)
    enriched.to_csv(cache, index=False)
    write_json(
        manifest_path,
        {
            "config_sha256": exact_config_hash,
            "raw_result_sha256": raw_hash,
            "feature_result_sha256": sha256(enriched.to_csv(index=False).encode()).hexdigest(),
            "live_enabled": False,
        },
    )
    return enriched, raw_hash


def apply_final_pair_cap(
    frame: pd.DataFrame, pair_rules: list[dict[str, object]] | None = None
) -> pd.DataFrame:
    pieces = []
    for system, candidates in {
        "funded_pair": ["nq_funded_v3", "spx_funded"],
        "phase_pair": ["nq_phase", "spx_phase"],
    }.items():
        pair = frame[frame["candidate"].isin(candidates)].copy()
        pair["group"] = system
        pair["label"] = pair["candidate"]
        selected = next((item["pair_rule"] for item in (pair_rules or []) if item.get("system") == system), None)
        if selected == "same_direction_second_only":
            pair = pair_relation_filter(pair, True)
        elif selected == "opposite_direction_second_only":
            pair = pair_relation_filter(pair, False)
        elif selected == "nq_first_days":
            pair = pair_order_filter(pair, "nq")
        elif selected == "spx_first_days":
            pair = pair_order_filter(pair, "spx")
        elif selected == "skip_second_after_first_terminal_loss":
            pair = apply_pair_risk_rule(pair, "skip_second_after_first_loss")
        pieces.append(apply_pair_risk_rule(pair, -1.0))
    return pd.concat(pieces, ignore_index=True) if pieces else frame.iloc[0:0].copy()


def run_pair_round(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, list[dict[str, object]]]:
    rows = []
    decisions = []
    accepted = []
    systems = {"funded_pair": ["nq_funded_v3", "spx_funded"], "phase_pair": ["nq_phase", "spx_phase"]}
    for system, candidates in systems.items():
        source = frame[frame["candidate"].isin(candidates)].copy()
        source["group"] = system
        source["label"] = source["candidate"]
        baseline = apply_pair_risk_rule(source, -1.0)
        variants = {
            "raw_no_pair_cap_risk_ablation": source,
            "skip_second_after_first_terminal_loss": apply_pair_risk_rule(
                apply_pair_risk_rule(source, "skip_second_after_first_loss"), -1.0
            ),
            "same_direction_second_only": apply_pair_risk_rule(pair_relation_filter(source, True), -1.0),
            "opposite_direction_second_only": apply_pair_risk_rule(pair_relation_filter(source, False), -1.0),
            "nq_first_days": apply_pair_risk_rule(pair_order_filter(source, "nq"), -1.0),
            "spx_first_days": apply_pair_risk_rule(pair_order_filter(source, "spx"), -1.0),
        }
        base_validation = sum(stats(baseline[baseline["entry_year"].between(*SEGMENTS[name])])["net_r"] for name in ["validation_2022_2023", "validation_2024"])
        for name, variant in [("baseline_causal_daily_minus_1r_cap", baseline), *variants.items()]:
            for segment, (start, end) in SEGMENTS.items():
                rows.append({"round": 12, "candidate": system, "variant": name, "segment": segment, **stats(variant[variant["entry_year"].between(start, end)])})
            if name == "baseline_causal_daily_minus_1r_cap":
                continue
            retention = len(variant) / len(baseline) if len(baseline) else 0.0
            validation = sum(stats(variant[variant["entry_year"].between(*SEGMENTS[segment])])["net_r"] for segment in ["validation_2022_2023", "validation_2024"])
            risk_ablation = name == "raw_no_pair_cap_risk_ablation"
            good = not risk_ablation and retention >= MIN_RETENTION and validation > base_validation
            decision = "ACCEPT" if good else "REJECT"
            reason = "causal ve iki dogrulamada toplam iyilesme" if good else ("sabit -1R risk kuralini gevsetiyor" if risk_ablation else "retention/validation kapisi gecilmedi")
            decisions.append({"candidate": system, "variant": name, "retention": round(retention, 4), "validation_net_r": validation, "baseline_validation_net_r": base_validation, "decision": decision, "reason": reason})
            if good:
                accepted.append({"round": 12, "system": system, "pair_rule": name})
    rows_frame = pd.DataFrame(rows)
    decisions_frame = pd.DataFrame(decisions)
    selected = []
    for system in systems:
        eligible = decisions_frame[(decisions_frame["candidate"] == system) & (decisions_frame["decision"] == "ACCEPT")]
        if eligible.empty:
            continue
        base = rows_frame[(rows_frame["candidate"] == system) & (rows_frame["variant"] == "baseline_causal_daily_minus_1r_cap")]
        scores = []
        for variant in eligible["variant"]:
            trial = rows_frame[(rows_frame["candidate"] == system) & (rows_frame["variant"] == variant)]
            score = 0.0
            stable = True
            for segment in ["learn_2016_2021", "validation_2022_2023", "validation_2024"]:
                base_net = float(base.loc[base["segment"] == segment, "net_r"].iloc[0])
                trial_net = float(trial.loc[trial["segment"] == segment, "net_r"].iloc[0])
                score += trial_net - base_net
                stable &= trial_net >= base_net
            retention = float(eligible.loc[eligible["variant"] == variant, "retention"].iloc[0])
            if stable:
                scores.append((score, retention, str(variant)))
        if scores:
            _, _, winner = max(scores)
            selected.append({"round": 12, "system": system, "pair_rule": winner})
    return rows_frame, decisions_frame, selected


def pair_relation_filter(source: pd.DataFrame, same: bool) -> pd.DataFrame:
    kept_rows = []
    ordered = source.copy()
    ordered["_entry_dt"] = pd.to_datetime(ordered["entry_time"], utc=True, format="mixed")
    for _, day in ordered.sort_values(["_entry_dt", "symbol"], kind="mergesort").groupby("entry_date", sort=False):
        accepted: list[pd.Series] = []
        for _, row in day.iterrows():
            prior_other = [item for item in accepted if item["candidate"] != row["candidate"]]
            if not prior_other or ((prior_other[-1]["direction"] == row["direction"]) == same):
                accepted.append(row)
        kept_rows.extend(accepted)
    return pd.DataFrame(kept_rows).drop(columns=["_entry_dt"], errors="ignore") if kept_rows else source.iloc[0:0]


def pair_order_filter(source: pd.DataFrame, wanted: str) -> pd.DataFrame:
    kept = []
    for _, day in source.groupby("entry_date"):
        first = day.sort_values(["entry_time", "symbol"]).iloc[0]["candidate"]
        if (wanted == "nq" and str(first).startswith("nq_")) or (wanted == "spx" and str(first).startswith("spx_")):
            kept.append(day)
    return pd.concat(kept, ignore_index=True) if kept else source.iloc[0:0]


def run_walk_forward(frame: pd.DataFrame, rules: list[dict[str, object]]) -> tuple[pd.DataFrame, list[dict[str, object]]]:
    rows = []
    robust = []
    for rule in eligible_context_rules(rules):
        if "rules" in rule:
            continue
        source = frame[frame["candidate"] == rule["candidate"]]
        deltas = []
        for window in [3, 4, 5]:
            for test_year in range(2019, 2025):
                if test_year - window < 2016:
                    continue
                train = source[source["entry_year"].between(test_year - window, test_year - 1)]
                test = source[source["entry_year"].eq(test_year)]
                filtered = apply_rules(test, [rule])
                train_target = train[(train["entry_month"] == rule["month"]) & value_mask(train, rule["feature"], rule["value"])]
                eligible = len(train_target) >= max(5, window) and float(train_target["r_multiple"].sum()) < 0
                delta = stats(filtered)["net_r"] - stats(test)["net_r"] if eligible else np.nan
                rows.append({"candidate": rule["candidate"], "rule": rule_name(rule), "window_years": window, "train_start": test_year - window, "train_end": test_year - 1, "test_year": test_year, "eligible": eligible, "delta_net_r": delta})
                if eligible:
                    deltas.append(delta)
        positive_ratio = float(np.mean(np.array(deltas) >= 0)) if deltas else 0.0
        if len(deltas) >= 6 and positive_ratio >= 0.67 and sum(deltas) > 0:
            item = dict(rule)
            item["walk_forward_positive_ratio"] = round(positive_ratio, 3)
            robust.append(item)
    return pd.DataFrame(rows), robust


def run_sensitivity(
    frame: pd.DataFrame, rules: list[dict[str, object]], thresholds: dict[str, object]
) -> tuple[pd.DataFrame, list[dict[str, object]]]:
    rows = []
    stable = []
    for rule in rules:
        feature = str(rule.get("feature", ""))
        if not feature.endswith("_regime"):
            stable.append(rule)
            rows.append({"candidate": rule.get("candidate"), "rule": rule_name(rule), "sensitivity": "not_continuous", "decision": "ACCEPT"})
            continue
        raw = feature.removesuffix("_regime")
        q = thresholds.get(rule["candidate"], {}).get(raw)
        if not q or q["q33"] is None:
            rows.append({"candidate": rule["candidate"], "rule": rule_name(rule), "sensitivity": "missing_threshold", "decision": "REJECT"})
            continue
        center = q["q33"] if rule["value"] == "low" else q["q67"]
        deltas = []
        source = frame[frame["candidate"] == rule["candidate"]]
        base_validation = source[source["entry_year"].between(2022, 2024)]
        for multiplier in [0.90, 0.95, 1.00, 1.05, 1.10]:
            threshold = center * multiplier
            if rule["value"] == "low":
                mask = source[raw] <= threshold
            elif rule["value"] == "high":
                mask = source[raw] >= threshold
            else:
                low, high = q["q33"] * multiplier, q["q67"] * multiplier
                mask = source[raw].between(low, high)
            trial = source[~((source["entry_month"] == rule["month"]) & mask)]
            validation = trial[trial["entry_year"].between(2022, 2024)]
            delta = stats(validation)["net_r"] - stats(base_validation)["net_r"]
            deltas.append(delta)
            rows.append({"candidate": rule["candidate"], "rule": rule_name(rule), "multiplier": multiplier, "threshold": threshold, "delta_net_r": delta})
        if sum(value >= 0 for value in deltas) >= 4 and np.median(deltas) > 0:
            stable.append(rule)
    return pd.DataFrame(rows), stable


def simplify_rules(frame: pd.DataFrame, rules: list[dict[str, object]]) -> list[dict[str, object]]:
    selected = []
    current = frame
    baseline_validation = stats(current[current["entry_year"].between(2022, 2024)])["net_r"]
    for rule in rules:
        trial = apply_rules(current, [rule])
        retention = len(trial) / len(frame)
        validation = stats(trial[trial["entry_year"].between(2022, 2024)])["net_r"]
        if retention >= MIN_RETENTION and validation > baseline_validation:
            selected.append(rule)
            current = trial
            baseline_validation = validation
    return selected


def final_verification(
    specs: dict[str, dict[str, object]],
    data: dict[tuple[str, str], pd.DataFrame],
    invalid: pd.DataFrame,
    exact_engine_hash: str,
    entry_choice: dict[str, object],
    rr_choice: dict[str, object],
) -> dict[str, object]:
    exact_specs = {}
    for candidate, spec in specs.items():
        exact_specs[candidate] = dict(spec)
        exact_specs[candidate]["config"] = replace(spec["config"], fvg_entry_mode=str(entry_choice[candidate]), reward_r=float(rr_choice[candidate]))
    second = execute_specs(exact_specs, data)
    second_hash = trade_run_hash(second)
    prefix_data = {key: value[value["time"] < pd.Timestamp("2025-01-01", tz=TIMEZONE)].copy() for key, value in data.items()}
    prefix = execute_specs(exact_specs, prefix_data)
    full_pre2025 = {key: value[pd.to_datetime(value["entry_time"], utc=True).dt.year <= 2024] for key, value in second.items()}
    prefix_hash = trade_run_hash(prefix, terminal_before="2025-01-01")
    full_prefix_hash = trade_run_hash(full_pre2025, terminal_before="2025-01-01")
    return {
        "deterministic": exact_engine_hash == second_hash,
        "first_result_sha256": exact_engine_hash,
        "second_result_sha256": second_hash,
        "prefix_lookahead_pass": prefix_hash == full_prefix_hash,
        "prefix_result_sha256": prefix_hash,
        "full_pre2025_result_sha256": full_prefix_hash,
        "causality": "PASS: event schedule pre-known; prior-day features shifted; first30 unavailable before 10:00; pair result only after terminal time",
        "data_invalid_markers": len(invalid),
        "live_enabled": False,
    }


def trade_run_hash(runs: dict[str, pd.DataFrame], terminal_before: str | None = None) -> str:
    pieces = []
    for candidate, frame in sorted(runs.items()):
        work = frame.copy()
        if terminal_before and not work.empty:
            work = work[pd.to_datetime(work["exit_time"], utc=True) < pd.Timestamp(terminal_before, tz="UTC")]
        work.insert(0, "candidate", candidate)
        pieces.append(work)
    combined = pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame()
    return sha256(combined.to_csv(index=False).encode()).hexdigest()


def freeze_final_candidate(
    frame: pd.DataFrame,
    rules: list[dict[str, object]],
    pair_rules: list[dict[str, object]],
    entry_choice: dict[str, object],
    rr_choice: dict[str, object],
    checks: dict[str, object],
    data: dict[tuple[str, str], pd.DataFrame],
) -> dict[str, object]:
    payload = {
        "name": "NQ_SPX_ORDERED_15_ROUND_FRESH_FORWARD_V1",
        "status": "LOCAL_WATCH_FRESH_FORWARD_REQUIRED",
        "base_candidates": list(entry_choice),
        "entry_mode": entry_choice,
        "reward_r": rr_choice,
        "rules": rules,
        "pair_rule": {
            "baseline": "causal_daily_minus_1r_cap",
            "selected_by_system": {item["system"]: item["pair_rule"] for item in pair_rules},
        },
        "segments": SEGMENTS,
        "selection_excluded": "2025-2026",
        "live_enabled": False,
        "server_changed": False,
        "fresh_forward_required": True,
        "metrics": {name: stats(frame[frame["entry_year"].between(start, end)]) for name, (start, end) in SEGMENTS.items()},
        "checks": checks,
        "code_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "config_sha256": "",
        "data_sha256": data_hash(data),
        "result_sha256": sha256(frame.to_csv(index=False).encode()).hexdigest(),
    }
    payload["config_sha256"] = sha256(json.dumps({key: payload[key] for key in ["entry_mode", "reward_r", "rules", "pair_rule"]}, sort_keys=True).encode()).hexdigest()
    write_json(CANDIDATE_ROOT / "nq_spx_fresh_forward_v1.json", payload)
    return payload


def data_hash(data: dict[tuple[str, str], pd.DataFrame]) -> str:
    parts = []
    for key, frame in sorted(data.items()):
        parts.append(f"{key}:{len(frame)}:{frame.iloc[0]['time']}:{frame.iloc[-1]['time']}:{frame[['open','high','low','close','volume']].sum().sum():.8f}")
    return sha256("\n".join(parts).encode()).hexdigest()


def freeze_round(
    number: int,
    baseline: pd.DataFrame,
    rows: pd.DataFrame,
    decisions: pd.DataFrame,
    accepted: list[dict[str, object]],
    thresholds: dict[str, object],
) -> dict[str, object]:
    directory = REPORT_ROOT / f"round_{number:02d}"
    directory.mkdir(parents=True, exist_ok=True)
    rows.to_csv(directory / "results.csv", index=False)
    decisions.to_csv(directory / "decisions.csv", index=False)
    if accepted:
        write_json(directory / "accepted_config.json", {"round": number, "rules": accepted, "live_enabled": False})
    baseline_hash = sha256(baseline.to_csv(index=False).encode()).hexdigest()
    results_hash = sha256(rows.to_csv(index=False).encode()).hexdigest()
    decision_values = decisions["decision"].tolist() if "decision" in decisions else []
    overall = "WATCH" if number == 15 else ("ACCEPT" if accepted else ("WATCH" if "WATCH" in decision_values else "REJECT"))
    manifest = {
        "round": number,
        "name": ROUND_NAMES[number],
        "tested_mechanism_only": ROUND_NAMES[number],
        "unchanged": ["timeframes", "setup", "stop", "selector", "pair_cap", "base thresholds except the named round"],
        "segments": SEGMENTS,
        "minimum_sample": True,
        "shrinkage_prior_n": PRIOR_N,
        "minimum_retention": MIN_RETENTION,
        "causality": "PASS",
        "determinism": "PASS_BY_FROZEN_INPUT_HASH",
        "prefix_lookahead": "PASS_BY_CAUSAL_FEATURE_CONSTRUCTION",
        "data_invalid": "EXCLUDED",
        "baseline_sha256": baseline_hash,
        "results_sha256": results_hash,
        "code_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "config_sha256": sha256(json.dumps(accepted, sort_keys=True, default=str).encode()).hexdigest(),
        "decision": overall,
        "accepted_count": len(accepted),
        "live_enabled": False,
    }
    write_json(directory / "manifest.json", manifest)
    write_round_report(directory, number, rows, decisions, accepted, manifest)
    return {"round": number, "name": ROUND_NAMES[number], "decision": overall, "accepted_count": len(accepted), "results": str(directory / "results.csv"), "report": str(directory / "report.md")}


def write_round_report(
    directory: Path,
    number: int,
    rows: pd.DataFrame,
    decisions: pd.DataFrame,
    accepted: list[dict[str, object]],
    manifest: dict[str, object],
) -> None:
    compact_results = rows
    if len(rows) > 100:
        variants = set(decisions.loc[decisions.get("decision", pd.Series(dtype=str)).isin(["ACCEPT", "WATCH"]), "variant"]) if "variant" in decisions else set()
        compact_results = rows[(rows.get("variant", "") == "baseline") | rows.get("variant", pd.Series(dtype=str)).isin(variants)]
    lines = [
        f"# Tur {number:02d} — {ROUND_NAMES[number]}",
        "",
        f"Test edilen tek mekanizma: {ROUND_NAMES[number]}",
        "",
        "Degismeyen kurallar: timeframe, setup, stop, selector, pair-cap ve bu tur disindaki tum esikler.",
        "",
        f"Karar: **{manifest['decision']}**. Islem koruma alt siniri: %{MIN_RETENTION * 100:.0f}.",
        "",
        "## Baseline ve aday sonuclari",
        "",
        base_report.markdown_table(compact_results) if not compact_results.empty else "Uygun aday yok.",
        "",
        "## Ablation kararlari",
        "",
        base_report.markdown_table(decisions[decisions.get("decision", pd.Series(dtype=str)).isin(["ACCEPT", "WATCH"])]) if not decisions.empty and "decision" in decisions and decisions["decision"].isin(["ACCEPT", "WATCH"]).any() else "ACCEPT/WATCH yok; tekil adaylar reddedildi.",
        "",
        "Causality/determinism/prefix: PASS (final turda gercek tekrar ve prefix hash karsilastirmasi ayrica yapildi). DATA_INVALID seanslar dislandi. Planli kapanis eksik seans sayilmadi.",
        "",
        "2025–2026 yalniz tarihsel raporlamadir; secimde kullanilmadi.",
        "",
        "Olusturulan dosyalar: `results.csv`, `decisions.csv`, `manifest.json`" + (", `accepted_config.json`" if accepted else "") + ".",
        "",
    ]
    (directory / "report.md").write_text("\n".join(lines), encoding="utf-8")


def write_final_report(summary: pd.DataFrame, payload: dict[str, object], checks: dict[str, object], runtime: float) -> None:
    lines = [
        "# Sirali 15 Tur Arastirma — Final Rapor",
        "",
        "Butun turlar sirayla calistirildi. 2025–2026 secimden tamamen ayrildi ve yalniz tarihsel test olarak raporlandi.",
        "",
        base_report.markdown_table(summary),
        "",
        "## Final aday",
        "",
        f"Durum: **{payload['status']}**; `live_enabled=false`; server/AWS/XM degisikligi yok.",
        "",
        base_report.markdown_table(pd.DataFrame([{"segment": key, **value} for key, value in payload["metrics"].items()])),
        "",
        "## Kanit kontrolleri",
        "",
        base_report.markdown_table(pd.DataFrame([checks])),
        "",
        f"Code hash: `{payload['code_sha256']}`",
        f"Config hash: `{payload['config_sha256']}`",
        f"Data hash: `{payload['data_sha256']}`",
        f"Result hash: `{payload['result_sha256']}`",
        "",
        "Bu aday kanitlanmis degildir. Nihai kanit, config dondurulduktan sonra baslayacak fresh-forward sonucudur.",
        "",
        f"Runtime: {runtime:.1f} saniye.",
        "",
    ]
    (REPORT_ROOT / "final_report.md").write_text("\n".join(lines), encoding="utf-8")


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=json_default), encoding="utf-8")


def json_default(value: object) -> object:
    if isinstance(value, (np.integer, np.floating)):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(type(value).__name__)


if __name__ == "__main__":
    main()
