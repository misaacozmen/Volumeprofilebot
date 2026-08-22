from __future__ import annotations

import argparse
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import sys
import time

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import run_engine_path_comparison_2025_feb_mar as comparison
import run_main_candidate_filter_tests as filters
from backtest.engine_pipeline import (
    EngineLeg,
    run_canonical_pair_pipeline,
    source_code_hash,
    stable_frame_hash,
)
from backtest.manual_state import ManualStateConfig


DEFAULT_REPORT_DIR = ROOT / "outputs" / "reports" / "canonical_production_full_history_research"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the frozen production canonical engine over history.")
    parser.add_argument("--start", default="2022-01-03")
    parser.add_argument("--end", default=None)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    return parser.parse_args()


def frame_hash(frame: pd.DataFrame) -> str:
    columns = [name for name in ("time", "open", "high", "low", "close", "volume") if name in frame]
    return stable_frame_hash(frame[columns])


def contract_hash(
    loaded: dict[tuple[str, str], pd.DataFrame],
    configs: dict[str, object],
    state_config: ManualStateConfig,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> tuple[str, dict[str, str], str]:
    data_hashes = {
        key: frame_hash(loaded[value])
        for key, value in comparison.SYMBOLS.items()
    }
    payload = {
        "start": start.strftime("%Y-%m-%d"),
        "end": end.strftime("%Y-%m-%d"),
        "state": asdict(state_config),
        "legs": {key: asdict(value) for key, value in configs.items()},
        "data_hashes": data_hashes,
        "code_hash": source_code_hash(),
    }
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return sha256(encoded).hexdigest(), data_hashes, payload["code_hash"]


def period_frame(frame: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    local_start = (start - pd.Timedelta(days=21)).tz_localize("America/New_York")
    local_end = (end + pd.Timedelta(days=4)).tz_localize("America/New_York")
    return frame[(frame["time"] >= local_start) & (frame["time"] < local_end)].copy()


def checkpoint_paths(report_dir: Path, year: int) -> dict[str, Path]:
    checkpoint = report_dir / "checkpoints" / str(year)
    checkpoint.mkdir(parents=True, exist_ok=True)
    return {
        "decisions": checkpoint / "decisions.csv",
        "filled": checkpoint / "filled.csv",
        "suppressed": checkpoint / "suppressed.csv",
        "quality": checkpoint / "data_quality.csv",
        "meta": checkpoint / "checkpoint.json",
    }


def read_or_run_year(
    year: int,
    start: pd.Timestamp,
    end: pd.Timestamp,
    report_dir: Path,
    contract: str,
    loaded: dict[tuple[str, str], pd.DataFrame],
    configs: dict[str, object],
    state_config: ManualStateConfig,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    paths = checkpoint_paths(report_dir, year)
    if paths["meta"].exists():
        meta = json.loads(paths["meta"].read_text(encoding="utf-8"))
        if meta.get("contract_hash") == contract and all(paths[key].exists() for key in ("decisions", "filled", "suppressed", "quality")):
            print(f"{year}: verified checkpoint", flush=True)
            return tuple(pd.read_csv(paths[key]) for key in ("decisions", "filled", "suppressed", "quality"))

    year_start = max(start, pd.Timestamp(f"{year}-01-01"))
    year_end = min(end, pd.Timestamp(f"{year}-12-31"))
    dates = [value.date() for value in pd.bdate_range(year_start, year_end)]
    legs = [
        EngineLeg(key, period_frame(loaded[source], year_start, year_end), configs[key])
        for key, source in comparison.SYMBOLS.items()
    ]
    started = time.perf_counter()
    result = run_canonical_pair_pipeline(legs, dates, state_config=state_config)
    quality_rows = []
    for key, leg_result in result.leg_results.items():
        quality_rows.extend(
            {
                "leg_key": key,
                "date": day.trade_date,
                "data_state": day.data_state,
                "data_reasons": "|".join(day.data_reasons),
                "decision_count": len(day.decisions),
            }
            for day in leg_result.days
        )
    quality = pd.DataFrame(quality_rows)
    frames = (result.decisions, result.filled_after_pair_cap, result.suppressed_by_pair_cap, quality)
    for key, frame in zip(("decisions", "filled", "suppressed", "quality"), frames):
        frame.to_csv(paths[key], index=False)
    meta = {
        "year": year,
        "start": year_start.strftime("%Y-%m-%d"),
        "end": year_end.strftime("%Y-%m-%d"),
        "contract_hash": contract,
        "result_hash": stable_frame_hash(result.decisions),
        "runtime_seconds": round(time.perf_counter() - started, 3),
    }
    paths["meta"].write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{year}: {len(result.decisions)} decisions, {len(result.filled_after_pair_cap)} fills, {meta['runtime_seconds']}s", flush=True)
    return frames


def concat(frames: list[pd.DataFrame]) -> pd.DataFrame:
    usable = [frame for frame in frames if not frame.empty]
    return pd.concat(usable, ignore_index=True) if usable else pd.DataFrame()


def performance_summary(filled: pd.DataFrame, decisions: pd.DataFrame, quality: pd.DataFrame) -> dict[str, object]:
    if filled.empty:
        wins = losses = 0
        r_values = pd.Series(dtype=float)
    else:
        wins = int((filled["outcome"] == "TP").sum())
        losses = int((filled["outcome"] == "SL").sum())
        r_values = pd.to_numeric(filled["r_multiple"], errors="coerce").fillna(0.0)
    equity = r_values.cumsum()
    drawdown = equity - equity.cummax().clip(lower=0.0)
    gross_win = float(r_values[r_values > 0].sum())
    gross_loss = abs(float(r_values[r_values < 0].sum()))
    month_count = max(1, pd.to_datetime(filled.get("date", pd.Series(dtype=str))).dt.to_period("M").nunique())
    return {
        "canonical_decisions": len(decisions),
        "filled_post_pair_cap": len(filled),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(100.0 * wins / len(filled), 2) if len(filled) else 0.0,
        "net_r": round(float(r_values.sum()), 3),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
        "max_drawdown_r": round(float(drawdown.min()), 3) if not drawdown.empty else 0.0,
        "average_trades_per_month": round(len(filled) / month_count, 3),
        "pair_cap_suppressed": int((decisions.get("pair_risk_state", pd.Series(dtype=str)) == "SUPPRESSED_DAILY_CAP").sum()),
        "invalid_leg_days": int((quality.get("data_state", pd.Series(dtype=str)) == "INVALID").sum()),
        "ambiguous_decisions": int(decisions.get("intrabar_ambiguity", pd.Series(dtype=str)).fillna("").astype(str).ne("").sum()),
    }


def grouped_performance(filled: pd.DataFrame, group_columns: list[str]) -> pd.DataFrame:
    if filled.empty:
        return pd.DataFrame(columns=group_columns + ["trades", "wins", "losses", "win_rate_pct", "net_r"])
    work = filled.copy()
    work["r_multiple"] = pd.to_numeric(work["r_multiple"], errors="coerce").fillna(0.0)
    work["win"] = work["outcome"].eq("TP")
    grouped = work.groupby(group_columns, dropna=False)
    result = grouped.agg(trades=("outcome", "size"), wins=("win", "sum"), net_r=("r_multiple", "sum")).reset_index()
    result["losses"] = result["trades"] - result["wins"]
    result["win_rate_pct"] = (100.0 * result["wins"] / result["trades"]).round(2)
    result["net_r"] = result["net_r"].round(3)
    return result[group_columns + ["trades", "wins", "losses", "win_rate_pct", "net_r"]]


def write_report(report_dir: Path, summary: dict[str, object], manifest: dict[str, object]) -> None:
    lines = [
        "# Frozen production canonical engine — full-history research baseline",
        "",
        "This is an offline research run. It does not modify or promote the live campaign.",
        "",
        f"- Period: `{manifest['start']}` through `{manifest['end']}`",
        f"- Decisions: `{summary['canonical_decisions']}`",
        f"- Post-cap fills: `{summary['filled_post_pair_cap']}`",
        f"- Win rate: `{summary['win_rate_pct']}%`",
        f"- Net R: `{summary['net_r']}`",
        f"- Profit factor: `{summary['profit_factor']}`",
        f"- Max drawdown: `{summary['max_drawdown_r']}R`",
        f"- Average trades/month: `{summary['average_trades_per_month']}`",
        f"- Invalid leg-days blocked: `{summary['invalid_leg_days']}`",
        f"- Ambiguous decisions labeled: `{summary['ambiguous_decisions']}`",
        "",
        f"- Code hash: `{manifest['code_hash']}`",
        f"- Config hash: `{manifest['config_hash']}`",
        f"- Result hash: `{manifest['result_hash']}`",
        f"- Contract hash: `{manifest['contract_hash']}`",
    ]
    (report_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    report_dir = args.report_dir.resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    loaded = filters.load_data()
    configs = comparison.build_active_configs(loaded)
    state_config = ManualStateConfig()
    available_end = min(pd.Timestamp(frame["time"].max()).tz_localize(None).normalize() for frame in loaded.values())
    start = pd.Timestamp(args.start).normalize()
    end = min(pd.Timestamp(args.end).normalize(), available_end) if args.end else available_end
    if end < start:
        raise ValueError(f"Invalid period: {start.date()} through {end.date()}.")
    contract, data_hashes, code_hash = contract_hash(loaded, configs, state_config, start, end)
    yearly = [
        read_or_run_year(year, start, end, report_dir, contract, loaded, configs, state_config)
        for year in range(start.year, end.year + 1)
    ]
    decisions = concat([item[0] for item in yearly])
    filled = concat([item[1] for item in yearly])
    suppressed = concat([item[2] for item in yearly])
    quality = concat([item[3] for item in yearly])
    decisions.to_csv(report_dir / "canonical_decisions.csv", index=False)
    filled.to_csv(report_dir / "filled_after_causal_pair_cap.csv", index=False)
    suppressed.to_csv(report_dir / "suppressed_by_causal_pair_cap.csv", index=False)
    quality.to_csv(report_dir / "data_quality_by_day.csv", index=False)
    summary = performance_summary(filled, decisions, quality)
    pd.DataFrame([summary]).to_csv(report_dir / "summary.csv", index=False)
    if not filled.empty:
        filled["year"] = pd.to_datetime(filled["date"]).dt.year
        filled["month"] = pd.to_datetime(filled["date"]).dt.to_period("M").astype(str)
        grouped_performance(filled, ["leg_key"]).to_csv(report_dir / "performance_by_leg.csv", index=False)
        grouped_performance(filled, ["direction"]).to_csv(report_dir / "performance_by_direction.csv", index=False)
        grouped_performance(filled, ["year"]).to_csv(report_dir / "performance_by_year.csv", index=False)
        grouped_performance(filled, ["month"]).to_csv(report_dir / "performance_by_month.csv", index=False)
    config_payload = {"state": asdict(state_config), "legs": {key: asdict(value) for key, value in configs.items()}}
    config_hash = sha256(json.dumps(config_payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    manifest = {
        "engine": "manual_state_canonical_v1",
        "purpose": "offline_research_no_live_promotion",
        "start": start.strftime("%Y-%m-%d"),
        "end": end.strftime("%Y-%m-%d"),
        "timezone": "America/New_York",
        "feeds": ["DUKASCOPY"],
        "data_hashes": data_hashes,
        "config_hash": config_hash,
        "code_hash": code_hash,
        "result_hash": stable_frame_hash(decisions),
        "contract_hash": contract,
    }
    (report_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_report(report_dir, summary, manifest)
    print(json.dumps(summary, sort_keys=True), flush=True)
    print(f"Wrote: {report_dir}", flush=True)


if __name__ == "__main__":
    main()
