from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

import run_corrected_engine_3month_report as base_report
import run_frequency_expansion_tests as frequency
from backtest.data_loader import load_ohlcv
from backtest.risk import apply_pair_risk_rule
from backtest.strategy import run_backtest, trades_to_frame


REPORT_DIR = ROOT / "outputs" / "reports" / "seasonality_10y_v1"
STAGING = ROOT / "data" / "staging" / "seasonality_2016_2021"
RAW = ROOT / "data" / "raw"
TIMEZONE = "America/New_York"
SEGMENTS = {
    "train_2016_2021": (2016, 2021),
    "validation_2022_2023": (2022, 2023),
    "validation_2024": (2024, 2024),
    "holdout_2025_2026": (2025, 2026),
    "all": (2016, 2026),
}


def main() -> None:
    started = time.perf_counter()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    specs = candidate_specs()
    data = load_data(specs)
    invalid = invalid_session_dates(data)
    invalid.to_csv(REPORT_DIR / "invalid_sessions.csv", index=False)
    runs = run_candidates(specs, data)
    trades = build_trade_frame(runs, invalid)
    trades.to_csv(REPORT_DIR / "all_trades.csv", index=False)
    cells = build_cells(trades)
    cells.to_csv(REPORT_DIR / "seasonality_cells.csv", index=False)
    rules = select_rules(cells)
    rules.to_csv(REPORT_DIR / "selected_rules.csv", index=False)
    comparison = evaluate_rules(trades, rules)
    comparison.to_csv(REPORT_DIR / "rule_comparison.csv", index=False)
    pair_comparison = evaluate_pairs(trades, rules)
    pair_comparison.to_csv(REPORT_DIR / "pair_comparison.csv", index=False)
    write_report(cells, rules, comparison, pair_comparison, invalid, time.perf_counter() - started)
    write_manifest(specs, data, invalid, rules, time.perf_counter() - started)
    print(f"Wrote: {REPORT_DIR}")
    print(f"Runtime seconds: {time.perf_counter() - started:.1f}")


def candidate_specs() -> dict[str, dict[str, object]]:
    return {
        "nq_funded_v3": {
            "symbol": frequency.funded_nq().symbol,
            "timeframe": frequency.funded_nq().timeframe,
            "config": replace(frequency.funded_nq_config(), fvg_entry_mode="start"),
        },
        "spx_funded": {
            "symbol": frequency.funded_spx().symbol,
            "timeframe": frequency.funded_spx().timeframe,
            "config": frequency.funded_spx_config(),
        },
        "nq_phase": {
            "symbol": frequency.phase_nq().symbol,
            "timeframe": frequency.phase_nq().timeframe,
            "config": frequency.phase_nq_config(),
        },
        "spx_phase": {
            "symbol": frequency.phase_spx().symbol,
            "timeframe": frequency.phase_spx().timeframe,
            "config": frequency.phase_spx_config(),
        },
    }


def data_paths(symbol: str, timeframe: str) -> list[Path]:
    return sorted(
        [
            *STAGING.rglob(f"{symbol}, {timeframe}_*.csv"),
            *RAW.glob(f"{symbol}, {timeframe}_*.csv"),
        ]
    )


def load_data(specs: dict[str, dict[str, object]]) -> dict[tuple[str, str], pd.DataFrame]:
    output = {}
    for spec in specs.values():
        key = (str(spec["symbol"]), str(spec["timeframe"]))
        if key not in output:
            output[key] = load_ohlcv(data_paths(*key)).frame
    return output


def invalid_session_dates(data: dict[tuple[str, str], pd.DataFrame]) -> pd.DataFrame:
    failed_frames = []
    for path in STAGING.glob("dukascopy_failed_chunks_*.csv"):
        frame = pd.read_csv(path)
        if not frame.empty:
            failed_frames.append(frame)
    failed = pd.concat(failed_frames, ignore_index=True).drop_duplicates(["instrument", "from"])
    rows = []
    targets = {
        "usatechidxusd": ("DUKASCOPY_USATECHIDXUSD", "3m", 130),
        "usa500idxusd": ("DUKASCOPY_USA500IDXUSD", "5m", 78),
    }
    for instrument, (symbol, timeframe, expected) in targets.items():
        frame = data[(symbol, timeframe)]
        timestamp = pd.to_datetime(frame["time"])
        rth = frame[
            (((timestamp.dt.hour == 9) & (timestamp.dt.minute >= 30)) | (timestamp.dt.hour > 9))
            & (timestamp.dt.hour < 16)
        ].copy()
        counts = rth.groupby(pd.to_datetime(rth["time"]).dt.date).size().to_dict()
        available = sorted(pd.to_datetime(frame["date"]).dt.date.unique())
        for date_text in sorted(failed.loc[failed["instrument"] == instrument, "from"].unique()):
            day = pd.Timestamp(date_text).date()
            if day.weekday() >= 5 or counts.get(day, 0) >= expected:
                continue
            rows.append({"symbol": symbol, "date": day.isoformat(), "reason": "MISSING_RTH_SESSION"})
            next_dates = [candidate for candidate in available if candidate > day and candidate.weekday() < 5]
            if next_dates:
                rows.append(
                    {
                        "symbol": symbol,
                        "date": next_dates[0].isoformat(),
                        "reason": "PREVIOUS_SESSION_DATA_INVALID",
                    }
                )
    return pd.DataFrame(rows).drop_duplicates().sort_values(["symbol", "date", "reason"])


def run_candidates(
    specs: dict[str, dict[str, object]], data: dict[tuple[str, str], pd.DataFrame]
) -> dict[str, pd.DataFrame]:
    output = {}
    for name, spec in specs.items():
        cache = REPORT_DIR / f"trades_{name}.csv"
        if cache.exists():
            output[name] = pd.read_csv(cache)
            continue
        print(f"Running {name}")
        frame = data[(str(spec["symbol"]), str(spec["timeframe"]))]
        trades = trades_to_frame(run_backtest(frame.copy(), spec["config"]).trades)
        trades.to_csv(cache, index=False)
        output[name] = trades
    return output


def build_trade_frame(runs: dict[str, pd.DataFrame], invalid: pd.DataFrame) -> pd.DataFrame:
    frames = []
    for candidate, trades in runs.items():
        if trades.empty:
            continue
        frame = trades.copy()
        entry = pd.to_datetime(frame["entry_time"], utc=True, format="mixed").dt.tz_convert(TIMEZONE)
        frame.insert(0, "candidate", candidate)
        frame["entry_year"] = entry.dt.year
        frame["entry_month"] = entry.dt.month
        frame["entry_month_name"] = entry.dt.month_name()
        frame["entry_weekday"] = entry.dt.day_name()
        frame["entry_date"] = entry.dt.date.astype(str)
        invalid_dates = set(invalid.loc[invalid["symbol"] == frame["symbol"].iloc[0], "date"])
        frame["data_valid"] = ~frame["entry_date"].isin(invalid_dates)
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def segment_name(year: int) -> str:
    for name, (start, end) in SEGMENTS.items():
        if name != "all" and start <= year <= end:
            return name
    return "outside"


def build_cells(trades: pd.DataFrame) -> pd.DataFrame:
    valid = trades[trades["data_valid"]].copy()
    rows = []
    granularities = {
        "month": ["entry_month"],
        "month_weekday": ["entry_month", "entry_weekday"],
        "month_direction": ["entry_month", "direction"],
        "month_weekday_direction": ["entry_month", "entry_weekday", "direction"],
    }
    segmented = []
    for name, (start, end) in SEGMENTS.items():
        frame = valid[(valid["entry_year"] >= start) & (valid["entry_year"] <= end)].copy()
        frame["segment"] = name
        segmented.append(frame)
    source = pd.concat(segmented, ignore_index=True)
    for granularity, columns in granularities.items():
        for keys, group in source.groupby(["candidate", "segment", *columns], dropna=False):
            if not isinstance(keys, tuple):
                keys = (keys,)
            candidate, segment, *values = keys
            row = {"candidate": candidate, "segment": segment, "granularity": granularity}
            row.update(dict(zip(columns, values)))
            row.update(stats(group))
            years = group.groupby("entry_year")["r_multiple"].sum()
            row["years_present"] = len(years)
            row["positive_year_ratio"] = round(float((years > 0).mean()), 3) if len(years) else 0.0
            rows.append(row)
    result = pd.DataFrame(rows)
    result["entry_month"] = result["entry_month"].astype(int)
    for (candidate, segment), indices in result.groupby(["candidate", "segment"]).groups.items():
        base = source[(source["candidate"] == candidate) & (source["segment"] == segment)]
        prior_mean = float(base["r_multiple"].mean()) if not base.empty else 0.0
        result.loc[indices, "shrunk_avg_r"] = (
            result.loc[indices, "net_r"] + prior_mean * 20
        ) / (result.loc[indices, "trades"] + 20)
    result["shrunk_avg_r"] = result["shrunk_avg_r"].round(4)
    return result


def stats(trades: pd.DataFrame) -> dict[str, object]:
    if trades.empty:
        return {"trades": 0, "wins": 0, "win_rate": 0.0, "net_r": 0.0, "avg_r": 0.0, "max_drawdown_r": 0.0}
    ordered = trades.copy()
    ordered["_entry"] = pd.to_datetime(ordered["entry_time"], utc=True, format="mixed")
    ordered = ordered.sort_values("_entry")
    equity = ordered["r_multiple"].cumsum()
    drawdown = float((equity - equity.cummax()).min())
    wins = int((ordered["r_multiple"] > 0).sum())
    return {
        "trades": len(ordered),
        "wins": wins,
        "win_rate": round(wins / len(ordered) * 100, 2),
        "net_r": round(float(ordered["r_multiple"].sum()), 2),
        "avg_r": round(float(ordered["r_multiple"].mean()), 4),
        "max_drawdown_r": round(drawdown, 2),
    }


def select_rules(cells: pd.DataFrame) -> pd.DataFrame:
    output_columns = [
        "candidate",
        "rule_type",
        "entry_month",
        "entry_weekday",
        "direction",
        "train_trades",
        "train_net_r",
        "validation_trades",
        "validation_net_r",
    ]
    train = cells[cells["segment"] == "train_2016_2021"].copy()
    validation = cells[cells["segment"].isin(["validation_2022_2023", "validation_2024"])].copy()
    rows = []
    harmful = train[
        (train["granularity"] == "month_weekday")
        & (train["trades"] >= 12)
        & (train["years_present"] >= 4)
        & (train["positive_year_ratio"] <= 0.34)
        & (train["net_r"] < 0)
        & (train["shrunk_avg_r"] <= -0.08)
    ]
    for _, item in harmful.iterrows():
        matches = validation[
            (validation["candidate"] == item["candidate"])
            & (validation["granularity"] == "month_weekday")
            & (validation["entry_month"] == item["entry_month"])
            & (validation["entry_weekday"] == item["entry_weekday"])
        ]
        if int(matches["trades"].sum()) < 4 or float(matches["net_r"].sum()) >= 0:
            continue
        rows.append(
            {
                "candidate": item["candidate"],
                "rule_type": "BLOCK_MONTH_WEEKDAY",
                "entry_month": int(item["entry_month"]),
                "entry_weekday": item["entry_weekday"],
                "direction": "all",
                "train_trades": int(item["trades"]),
                "train_net_r": float(item["net_r"]),
                "validation_trades": int(matches["trades"].sum()),
                "validation_net_r": round(float(matches["net_r"].sum()), 2),
            }
        )
    harmful_directions = train[
        (train["granularity"] == "month_direction")
        & (train["trades"] >= 10)
        & (train["years_present"] >= 4)
        & (train["positive_year_ratio"] <= 0.25)
        & (train["net_r"] < 0)
        & (train["shrunk_avg_r"] <= -0.08)
    ]
    for _, item in harmful_directions.iterrows():
        matches = validation[
            (validation["candidate"] == item["candidate"])
            & (validation["granularity"] == "month_direction")
            & (validation["entry_month"] == item["entry_month"])
            & (validation["direction"] == item["direction"])
        ]
        if int(matches["trades"].sum()) < 3 or float(matches["net_r"].sum()) >= 0:
            continue
        rows.append(
            {
                "candidate": item["candidate"],
                "rule_type": "BLOCK_MONTH_DIRECTION",
                "entry_month": int(item["entry_month"]),
                "entry_weekday": "all",
                "direction": item["direction"],
                "train_trades": int(item["trades"]),
                "train_net_r": float(item["net_r"]),
                "validation_trades": int(matches["trades"].sum()),
                "validation_net_r": round(float(matches["net_r"].sum()), 2),
            }
        )
    directions = train[train["granularity"] == "month_direction"]
    for (candidate, month), group in directions.groupby(["candidate", "entry_month"]):
        if set(group["direction"]) != {"long", "short"} or int(group["trades"].min()) < 10:
            continue
        ranked = group.sort_values("shrunk_avg_r", ascending=False)
        best, other = ranked.iloc[0], ranked.iloc[1]
        if (
            float(best["shrunk_avg_r"]) - float(other["shrunk_avg_r"]) < 0.12
            or float(best["net_r"]) <= 0
            or int(best["years_present"]) < 4
            or float(best["positive_year_ratio"]) < 0.60
        ):
            continue
        matches = validation[
            (validation["candidate"] == candidate)
            & (validation["granularity"] == "month_direction")
            & (validation["entry_month"] == month)
        ]
        best_val = matches[matches["direction"] == best["direction"]]
        other_val = matches[matches["direction"] == other["direction"]]
        if (
            int(best_val["trades"].sum()) < 4
            or float(best_val["net_r"].sum()) <= 0
            or float(best_val["avg_r"].mean()) <= float(other_val["avg_r"].mean())
        ):
            continue
        rows.append(
            {
                "candidate": candidate,
                "rule_type": "ALLOW_MONTH_DIRECTION",
                "entry_month": int(month),
                "entry_weekday": "all",
                "direction": best["direction"],
                "train_trades": int(best["trades"]),
                "train_net_r": float(best["net_r"]),
                "validation_trades": int(best_val["trades"].sum()),
                "validation_net_r": round(float(best_val["net_r"].sum()), 2),
            }
        )
    return pd.DataFrame(rows, columns=output_columns)


def evaluate_rules(trades: pd.DataFrame, rules: pd.DataFrame) -> pd.DataFrame:
    rows = []
    valid = trades[trades["data_valid"]].copy()
    for candidate, baseline in valid.groupby("candidate"):
        candidate_rules = rules[rules["candidate"] == candidate] if not rules.empty else rules
        filtered = baseline.copy()
        for _, rule in candidate_rules.iterrows():
            if rule["rule_type"] == "BLOCK_MONTH_WEEKDAY":
                blocked = (filtered["entry_month"] == rule["entry_month"]) & (
                    filtered["entry_weekday"] == rule["entry_weekday"]
                )
            elif rule["rule_type"] == "BLOCK_MONTH_DIRECTION":
                blocked = (filtered["entry_month"] == rule["entry_month"]) & (
                    filtered["direction"] == rule["direction"]
                )
            else:
                blocked = (filtered["entry_month"] == rule["entry_month"]) & (
                    filtered["direction"] != rule["direction"]
                )
            filtered = filtered[~blocked]
        for segment, (start, end) in SEGMENTS.items():
            base_segment = baseline[(baseline["entry_year"] >= start) & (baseline["entry_year"] <= end)]
            filtered_segment = filtered[(filtered["entry_year"] >= start) & (filtered["entry_year"] <= end)]
            base_stats = stats(base_segment)
            filtered_stats = stats(filtered_segment)
            for variant, values in [("baseline", base_stats), ("seasonal_rules", filtered_stats)]:
                rows.append({"candidate": candidate, "segment": segment, "variant": variant, **values})
    return pd.DataFrame(rows)


def apply_selected_rules(trades: pd.DataFrame, rules: pd.DataFrame) -> pd.DataFrame:
    filtered = trades.copy()
    for _, rule in rules.iterrows():
        target = filtered["candidate"] == rule["candidate"]
        if rule["rule_type"] == "BLOCK_MONTH_WEEKDAY":
            target &= (filtered["entry_month"] == rule["entry_month"]) & (
                filtered["entry_weekday"] == rule["entry_weekday"]
            )
        elif rule["rule_type"] == "BLOCK_MONTH_DIRECTION":
            target &= (filtered["entry_month"] == rule["entry_month"]) & (
                filtered["direction"] == rule["direction"]
            )
        else:
            target &= (filtered["entry_month"] == rule["entry_month"]) & (
                filtered["direction"] != rule["direction"]
            )
        filtered = filtered[~target]
    return filtered


def evaluate_pairs(trades: pd.DataFrame, rules: pd.DataFrame) -> pd.DataFrame:
    valid = trades[trades["data_valid"]].copy()
    filtered = apply_selected_rules(valid, rules)
    systems = {
        "funded_pair": ["nq_funded_v3", "spx_funded"],
        "phase_pair": ["nq_phase", "spx_phase"],
    }
    rows = []
    for system, candidates in systems.items():
        for variant, source in [("baseline", valid), ("seasonal_rules", filtered)]:
            pair = source[source["candidate"].isin(candidates)].copy()
            pair["group"] = system
            pair["label"] = pair["candidate"]
            capped = apply_pair_risk_rule(pair, -1.0)
            for segment, (start, end) in SEGMENTS.items():
                piece = capped[(capped["entry_year"] >= start) & (capped["entry_year"] <= end)]
                rows.append({"system": system, "segment": segment, "variant": variant, **stats(piece)})
    return pd.DataFrame(rows)


def write_report(
    cells: pd.DataFrame,
    rules: pd.DataFrame,
    comparison: pd.DataFrame,
    pair_comparison: pd.DataFrame,
    invalid: pd.DataFrame,
    runtime: float,
) -> None:
    holdout = comparison[comparison["segment"] == "holdout_2025_2026"]
    train = comparison[comparison["segment"] == "train_2016_2021"]
    lines = [
        "# 10Y Seasonality Analysis V1",
        "",
        "Data: Dukascopy bid, America/New_York, 2016-01-04 through 2026-07-02.",
        "Rules are learned from 2016-2021, required to agree with combined 2022-2024 validation, and only then measured on 2025-2026 holdout.",
        "Month-direction blocking was added after the first report exposed a selector-mechanic gap; results are exploratory and require fresh forward confirmation.",
        "Missing RTH sessions and the immediately following session are excluded as DATA_INVALID.",
        f"Runtime: {runtime:.1f} seconds",
        "",
        "## Selected Rules",
        "",
        base_report.markdown_table(rules) if not rules.empty else "No seasonal rule passed the gates.",
        "",
        "## Train Comparison",
        "",
        base_report.markdown_table(train),
        "",
        "## Holdout Comparison",
        "",
        base_report.markdown_table(holdout),
        "",
        "## Pair Comparison With Causal Daily -1R Cap",
        "",
        base_report.markdown_table(pair_comparison),
        "",
        "## Invalid Data",
        "",
        f"Excluded session markers: {len(invalid)}",
        "",
        "## Cell Counts",
        "",
        base_report.markdown_table(cells.groupby(["granularity", "segment"]).size().reset_index(name="cells")),
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


def write_manifest(
    specs: dict[str, dict[str, object]],
    data: dict[tuple[str, str], pd.DataFrame],
    invalid: pd.DataFrame,
    rules: pd.DataFrame,
    runtime: float,
) -> None:
    files = sorted([*STAGING.rglob("DUKASCOPY_*.csv"), *RAW.glob("DUKASCOPY_*.csv")])
    fingerprint = "\n".join(f"{path}:{path.stat().st_size}:{path.stat().st_mtime_ns}" for path in files)
    payload = {
        "status": "POST_HOC_EXPLORATORY_FRESH_FORWARD_REQUIRED",
        "live_system_changed": False,
        "feed": "DUKASCOPY_BID",
        "timezone": TIMEZONE,
        "segments": SEGMENTS,
        "candidates": list(specs),
        "data_rows": {f"{symbol}/{timeframe}": len(frame) for (symbol, timeframe), frame in data.items()},
        "invalid_session_markers": len(invalid),
        "selected_rules": len(rules),
        "data_metadata_sha256": sha256(fingerprint.encode()).hexdigest(),
        "script_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "runtime_seconds": round(runtime, 2),
    }
    (REPORT_DIR / "manifest.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
