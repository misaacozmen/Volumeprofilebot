from __future__ import annotations

import argparse
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
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
from backtest.evaluation_window import classify_sessions, coverage_report
from backtest.market_calendar import signed_calendar_contract, signed_market_dates
from backtest.numeric_contracts import finite_float
from backtest.risk_xray import build_risk_xray, write_risk_xray


DEFAULT_REPORT_DIR = ROOT / "outputs" / "reports" / "canonical_production_full_history_research"
_ACTIVE_STAGING: Path | None = None


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
        "calendar": signed_calendar_contract(),
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
    dates = signed_market_dates(year_start, year_end)
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
        return {
            "canonical_decisions": len(decisions),
            "filled_post_pair_cap": 0,
            "wins": 0,
            "losses": 0,
            "win_rate_pct": None,
            "net_r": None,
            "profit_factor": None,
            "profit_factor_reason": "NO_CLOSED_TRADES",
            "max_drawdown_r": None,
            "average_trades_per_month": None,
            "pair_cap_suppressed": int((decisions.get("pair_risk_state", pd.Series(dtype=str)) == "SUPPRESSED_DAILY_CAP").sum()),
            "invalid_leg_days": int((quality.get("data_state", pd.Series(dtype=str)) == "INVALID").sum()),
            "ambiguous_decisions": int(decisions.get("intrabar_ambiguity", pd.Series(dtype=str)).fillna("").astype(str).ne("").sum()),
        }
    else:
        wins = int((filled["outcome"] == "TP").sum())
        losses = int((filled["outcome"] == "SL").sum())
        r_values = pd.Series(
            [finite_float(value, "r_multiple") for value in filled["r_multiple"]],
            index=filled.index,
            dtype=float,
        )
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
    work["r_multiple"] = [finite_float(value, "r_multiple") for value in work["r_multiple"]]
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
        f"- Calendar: `{manifest['calendar_id']}` / `{manifest['calendar_sha256']}`",
        f"- Coverage: `{manifest['coverage']}`",
        f"- Promotable: `{manifest['promotable']}`",
    ]
    (report_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def first_eligible_decision_time(decisions: pd.DataFrame) -> str | None:
    """Use the first persisted decision event, never the first source candle."""
    for column in (
        "event_known_time",
        "decision_produced_at",
        "cisd_known_time",
        "fvg_known_time",
        "entry_known_time",
        "terminal_known_time",
    ):
        if column not in decisions or decisions.empty:
            continue
        values = [
            value
            for value in decisions[column].tolist()
            if value is not None and str(value).strip() and str(value).lower() != "nan"
        ]
        if values:
            parsed = pd.to_datetime(values, utc=True, errors="coerce")
            parsed = parsed[~parsed.isna()]
            if len(parsed):
                return parsed.min().isoformat()
    return None


def invalid_leg_inventory(
    quality: pd.DataFrame,
    loaded: dict[tuple[str, str], pd.DataFrame],
    configs: dict[str, object],
    data_hashes: dict[str, str],
) -> list[dict[str, object]]:
    inventory: list[dict[str, object]] = []
    source_paths = filters.threshold_source_paths()
    invalid = quality.loc[quality["data_state"] == "INVALID"] if not quality.empty else quality
    for row in invalid.itertuples(index=False):
        leg = str(row.leg_key)
        symbol, timeframe = comparison.SYMBOLS[leg]
        config = configs[leg]
        minutes = int(str(timeframe).removesuffix("m"))
        day = pd.Timestamp(str(row.date)).date()
        required_start = pd.Timestamp(day).tz_localize("America/New_York") + pd.Timedelta(hours=3, minutes=30)
        required_end = pd.Timestamp(day).tz_localize("America/New_York") + pd.Timedelta(
            hours=int(str(config.trade_window_end).split(":")[0]),
            minutes=int(str(config.trade_window_end).split(":")[1]),
        )
        expected = pd.date_range(required_start, required_end, freq=f"{minutes}min")
        frame = loaded[(symbol, timeframe)]
        times = pd.to_datetime(frame["time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
        actual = pd.DatetimeIndex(times[(times >= required_start) & (times <= required_end)]).drop_duplicates().sort_values()
        missing = expected.difference(actual)
        intervals: list[dict[str, str]] = []
        if len(missing):
            start_missing = previous = missing[0]
            for timestamp in missing[1:]:
                if timestamp - previous != pd.Timedelta(minutes=minutes):
                    intervals.append({"start": start_missing.isoformat(), "end": previous.isoformat()})
                    start_missing = timestamp
                previous = timestamp
            intervals.append({"start": start_missing.isoformat(), "end": previous.isoformat()})
        paths = source_paths.get((symbol, timeframe), [])
        inventory.append({
            "date": str(day), "leg": leg, "timeframe": timeframe,
            "required_start": required_start.isoformat(), "required_end": required_end.isoformat(),
            "expected_bar_count": len(expected), "actual_bar_count": len(actual),
            "missing_intervals": intervals,
            "source_paths": [path.relative_to(ROOT).as_posix() for path in paths],
            "raw_hash": data_hashes[leg],
            "reason_code": str(row.data_reasons),
        })
    return inventory


def _main_impl() -> None:
    global _ACTIVE_STAGING
    args = parse_args()
    report_dir = args.report_dir.resolve()
    report_parent = report_dir.parent
    report_parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix=f".{report_dir.name}.", dir=report_parent))
    _ACTIVE_STAGING = staging_dir
    loaded = filters.load_data()
    configs = comparison.build_active_configs(loaded)
    state_config = ManualStateConfig()
    available_end = min(pd.Timestamp(frame["time"].max()).tz_localize(None).normalize() for frame in loaded.values())
    start = pd.Timestamp(args.start).normalize()
    end = min(pd.Timestamp(args.end).normalize(), available_end) if args.end else available_end
    if end < start:
        raise ValueError(f"Invalid period: {start.date()} through {end.date()}.")
    contract, data_hashes, code_hash = contract_hash(loaded, configs, state_config, start, end)
    reference_manifest_path = report_dir / "run_manifest.json"
    if reference_manifest_path.is_file():
        reference = json.loads(reference_manifest_path.read_text(encoding="utf-8"))
        reference_hashes = reference.get("data_hashes", {})
        if isinstance(reference_hashes, dict) and reference_hashes and dict(reference_hashes) != data_hashes:
            raise ValueError("source data hash differs from the signed/reference full-history baseline")
    yearly = [
        read_or_run_year(year, start, end, staging_dir, contract, loaded, configs, state_config)
        for year in range(start.year, end.year + 1)
    ]
    decisions = concat([item[0] for item in yearly])
    filled = concat([item[1] for item in yearly])
    suppressed = concat([item[2] for item in yearly])
    quality = concat([item[3] for item in yearly])
    summary = performance_summary(filled, decisions, quality)
    requested_dates = signed_market_dates(start, end)
    source_sets = [
        set(
            pd.to_datetime(frame["time"], utc=True, format="mixed")
            .dt.tz_convert("America/New_York")
            .dt.date
        )
        for frame in loaded.values()
    ]
    source_dates = set.intersection(*source_sets) if source_sets else set()
    valid_sets = [
        set(pd.to_datetime(quality.loc[(quality["leg_key"] == key) & (quality["data_state"] == "VALID"), "date"], errors="coerce").dt.date.dropna())
        for key in comparison.SYMBOLS
    ] if not quality.empty else []
    invalid_dates = set(pd.to_datetime(quality.loc[quality["data_state"] == "INVALID", "date"], errors="coerce").dt.date.dropna()) if not quality.empty else set()
    coverage = coverage_report(
        classify_sessions(requested_dates, source_dates=source_dates, invalid_data=invalid_dates),
        evaluated=(set.intersection(*valid_sets) if valid_sets else set()) & set(requested_dates),
    )
    coverage_failures = [
        key for key in ("missing_sessions", "invalid_sessions")
        if int(coverage.get(key, 0) or 0) != 0
    ]
    quality.to_csv(staging_dir / "data_quality_by_day.csv", index=False)
    (staging_dir / "coverage.json").write_text(
        json.dumps(coverage, sort_keys=True, indent=2, default=str), encoding="utf-8"
    )
    invalid_inventory = invalid_leg_inventory(quality, loaded, configs, data_hashes)
    (staging_dir / "invalid_leg_days.json").write_text(
        json.dumps(invalid_inventory, sort_keys=True, indent=2, default=str), encoding="utf-8"
    )
    pd.DataFrame([
        {**item, "missing_intervals": json.dumps(item["missing_intervals"], sort_keys=True), "source_paths": json.dumps(item["source_paths"])}
        for item in invalid_inventory
    ]).to_csv(staging_dir / "invalid_leg_days.csv", index=False)
    if coverage_failures:
        raise ValueError(
            "full-history coverage is incomplete; no trades, summary, or risk-xray output may be promoted: "
            + ",".join(coverage_failures)
        )
    decisions.to_csv(staging_dir / "canonical_decisions.csv", index=False)
    filled.to_csv(staging_dir / "filled_after_causal_pair_cap.csv", index=False)
    suppressed.to_csv(staging_dir / "suppressed_by_causal_pair_cap.csv", index=False)
    pd.DataFrame([summary]).to_csv(staging_dir / "summary.csv", index=False)
    write_risk_xray(
        build_risk_xray(
            filled,
            eligible_dates=requested_dates,
            coverage=coverage,
            provenance_hashes=data_hashes,
            funnel={
                "proposed": len(decisions),
                "risk_approved": None,
                "staged": None,
                "filled": len(filled),
                "rejected": int((decisions.get("final_decision", pd.Series(dtype=str)) == "SKIP").sum()),
                "expired": 0,
            },
        ),
        staging_dir,
    )
    if not filled.empty:
        filled["year"] = pd.to_datetime(filled["date"]).dt.year
        filled["month"] = pd.to_datetime(filled["date"]).dt.to_period("M").astype(str)
        grouped_performance(filled, ["leg_key"]).to_csv(staging_dir / "performance_by_leg.csv", index=False)
        grouped_performance(filled, ["direction"]).to_csv(staging_dir / "performance_by_direction.csv", index=False)
        grouped_performance(filled, ["year"]).to_csv(staging_dir / "performance_by_year.csv", index=False)
        grouped_performance(filled, ["month"]).to_csv(staging_dir / "performance_by_month.csv", index=False)
    config_payload = {"state": asdict(state_config), "legs": {key: asdict(value) for key, value in configs.items()}}
    config_hash = sha256(json.dumps(config_payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    calendar_contract = signed_calendar_contract()
    manifest = {
        "engine": "manual_state_canonical_v1",
        "purpose": "offline_research_no_live_promotion",
        "start": start.strftime("%Y-%m-%d"),
        "end": end.strftime("%Y-%m-%d"),
        "timezone": "America/New_York",
        "calendar_id": calendar_contract["calendar_id"],
        "calendar_sha256": calendar_contract["calendar_sha256"],
        "calendar_coverage": calendar_contract["coverage"],
        "calendar_provenance": calendar_contract["provenance"],
        "feeds": ["DUKASCOPY"],
        "data_hashes": data_hashes,
        "config_hash": config_hash,
        "code_hash": code_hash,
        "result_hash": stable_frame_hash(decisions),
        "contract_hash": contract,
        "coverage": coverage,
        "first_eligible_decision_time": first_eligible_decision_time(decisions),
        "evaluated_sessions": int(coverage["evaluated_sessions"]),
        "promotable": not coverage_failures,
    }
    (staging_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_report(staging_dir, summary, manifest)
    backup_dir = report_parent / f".{report_dir.name}.previous"
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    try:
        if report_dir.exists():
            os.replace(report_dir, backup_dir)
        os.replace(staging_dir, report_dir)
    except Exception:
        if not report_dir.exists() and backup_dir.exists():
            os.replace(backup_dir, report_dir)
        raise
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        _ACTIVE_STAGING = None
    print(json.dumps(summary, sort_keys=True), flush=True)
    print(f"Wrote: {report_dir}", flush=True)


def main() -> None:
    try:
        _main_impl()
    except Exception as exc:
        staging_dir = _ACTIVE_STAGING
        if staging_dir is not None:
            failure = {
                "promotable": False,
                "status": "REJECTED_DATA_COVERAGE_GATES",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "staging_dir": str(staging_dir),
            }
            try:
                (staging_dir / "promotable.json").write_text(json.dumps(failure, sort_keys=True) + "\n", encoding="utf-8")
                sidecar = staging_dir.parent / f".{staging_dir.name}.failed.json"
                sidecar.write_text(json.dumps(failure, sort_keys=True) + "\n", encoding="utf-8")
            except OSError:
                pass
        raise


if __name__ == "__main__":
    main()
