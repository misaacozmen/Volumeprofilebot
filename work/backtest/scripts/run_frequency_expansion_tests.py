from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_corrected_engine_3month_report as base_report
import run_selected_candidates_full_data_report as selected
from backtest.data_loader import load_ohlcv
from backtest.risk import apply_pair_risk_rule
from backtest.strategy import run_backtest, trades_to_frame


REPORT_DIR = ROOT / "outputs" / "reports" / "frequency_expansion_tests"


def main() -> None:
    reset_report_dir()
    loaded = load_data()
    baselines = run_baselines(loaded)
    variants = run_variants(loaded)

    candidate_rows = []
    extra_rows = []
    pair_rows = []
    all_trades = []

    for run in [*baselines.values(), *variants]:
        candidate_rows.append(candidate_row(run))
        all_trades.append(run["tagged"])
        if run["variant"] != "baseline":
            baseline = baselines[run["leg_key"]]
            extra = extra_trades(run["trades"], baseline["trades"])
            extra_rows.append(extra_row(run, extra))
            pair_rows.extend(pair_impact_rows(run, baseline, baselines))

    candidate = pd.DataFrame(candidate_rows)
    extra = pd.DataFrame(extra_rows)
    pair = pd.DataFrame(pair_rows)
    trades = pd.concat([frame for frame in all_trades if not frame.empty], ignore_index=True)

    candidate.to_csv(REPORT_DIR / "candidate_comparison.csv", index=False)
    extra.to_csv(REPORT_DIR / "extra_trades_only.csv", index=False)
    pair.to_csv(REPORT_DIR / "pair_impact.csv", index=False)
    trades.to_csv(REPORT_DIR / "all_trades.csv", index=False)
    write_report(candidate, extra, pair)
    print(f"Wrote: {REPORT_DIR}")


def reset_report_dir() -> None:
    if REPORT_DIR.exists():
        for path in sorted(REPORT_DIR.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def load_data() -> dict[tuple[str, str], pd.DataFrame]:
    data = {}
    for candidate in selected.CANDIDATES:
        key = (candidate.symbol, candidate.timeframe)
        if key in data:
            continue
        data[key] = load_ohlcv(base_report.filter_paths(base_report.RAW_DIR, candidate.symbol, candidate.timeframe)).frame
    return data


def run_baselines(loaded: dict[tuple[str, str], pd.DataFrame]) -> dict[str, dict[str, object]]:
    return {spec["leg_key"]: execute(spec, loaded) for spec in baseline_specs()}


def run_variants(loaded: dict[tuple[str, str], pd.DataFrame]) -> list[dict[str, object]]:
    return [execute(spec, loaded) for spec in expansion_specs()]


def baseline_specs() -> list[dict[str, object]]:
    return [
        spec("funded", "funded_nq", "NQ funded", "baseline", funded_nq(), funded_nq_config()),
        spec("funded", "funded_spx", "SPX funded", "baseline", funded_spx(), funded_spx_config()),
        spec("phase", "phase_nq", "NQ phase", "baseline", phase_nq(), phase_nq_config()),
        spec("phase", "phase_spx", "SPX phase", "baseline", phase_spx(), phase_spx_config()),
    ]


def expansion_specs() -> list[dict[str, object]]:
    specs = []
    for system, leg_key, label, candidate, config in [
        ("funded", "funded_nq", "NQ funded", funded_nq(), funded_nq_config()),
        ("phase", "phase_nq", "NQ phase", phase_nq(), phase_nq_config()),
    ]:
        specs.extend(
            [
                spec(system, leg_key, label, "window_0930_1045", candidate, replace(config, trade_window_end="10:45")),
                spec(system, leg_key, label, "window_0930_1100", candidate, replace(config, trade_window_end="11:00")),
                spec(system, leg_key, label, "all_weekdays", candidate, replace(config, allowed_weekdays="Monday,Tuesday,Wednesday,Thursday,Friday")),
                spec(system, leg_key, label, "max2_per_day", candidate, replace(config, max_trades_per_day=2)),
                spec(
                    system,
                    leg_key,
                    label,
                    "all_weekdays_window_1100",
                    candidate,
                    replace(config, allowed_weekdays="Monday,Tuesday,Wednesday,Thursday,Friday", trade_window_end="11:00"),
                ),
            ]
        )
    for system, leg_key, label, candidate, config in [
        ("funded", "funded_spx", "SPX funded", funded_spx(), funded_spx_config()),
        ("phase", "phase_spx", "SPX phase", phase_spx(), phase_spx_config()),
    ]:
        specs.extend(
            [
                spec(system, leg_key, label, "latest_entry_1045", candidate, replace(config, latest_entry_time="10:45")),
                spec(system, leg_key, label, "latest_entry_1100", candidate, replace(config, latest_entry_time="11:00")),
                spec(system, leg_key, label, "all_weekdays", candidate, replace(config, allowed_weekdays="Monday,Tuesday,Wednesday,Thursday,Friday")),
                spec(system, leg_key, label, "max3_per_day", candidate, replace(config, max_trades_per_day=3)),
                spec(
                    system,
                    leg_key,
                    label,
                    "all_weekdays_latest_entry_1100",
                    candidate,
                    replace(config, allowed_weekdays="Monday,Tuesday,Wednesday,Thursday,Friday", latest_entry_time="11:00"),
                ),
            ]
        )
    return specs


def spec(system: str, leg_key: str, label: str, variant: str, candidate: selected.SelectedCandidate, config) -> dict[str, object]:
    return {"system": system, "leg_key": leg_key, "label": label, "variant": variant, "candidate": candidate, "config": config}


def funded_nq_config():
    return selected.build_config(funded_nq())


def funded_spx_config():
    return replace(selected.build_config(funded_spx()), reward_r=2.0, latest_entry_time="10:30")


def phase_nq_config():
    return replace(selected.build_config(phase_nq()), strong_swing_min_touches=3)


def phase_spx_config():
    return replace(selected.build_config(phase_spx()), reward_r=2.5, latest_entry_time="10:30")


def funded_nq() -> selected.SelectedCandidate:
    return by_label("NQ funded")


def funded_spx() -> selected.SelectedCandidate:
    return by_label("SPX funded")


def phase_nq() -> selected.SelectedCandidate:
    return by_label("NQ phase")


def phase_spx() -> selected.SelectedCandidate:
    return by_label("SPX phase")


def by_label(label: str) -> selected.SelectedCandidate:
    for candidate in selected.CANDIDATES:
        if candidate.label == label:
            return candidate
    raise RuntimeError(f"Missing candidate: {label}")


def execute(spec: dict[str, object], loaded: dict[tuple[str, str], pd.DataFrame]) -> dict[str, object]:
    candidate = spec["candidate"]
    result = run_backtest(loaded[(candidate.symbol, candidate.timeframe)].copy(), spec["config"])
    trades = trades_to_frame(result.trades)
    tagged = trades.copy()
    if not tagged.empty:
        tagged.insert(0, "variant", spec["variant"])
        tagged.insert(0, "leg_key", spec["leg_key"])
        tagged.insert(0, "label", spec["label"])
        tagged.insert(0, "system", spec["system"])
    return {**spec, "trades": trades, "tagged": tagged}


def candidate_row(run: dict[str, object]) -> dict[str, object]:
    return {
        "system": run["system"],
        "leg_key": run["leg_key"],
        "label": run["label"],
        "variant": run["variant"],
        **stats(run["trades"]),
        "avg_trades_per_month": round(len(run["trades"]) / active_month_count(run["trades"]), 2) if len(run["trades"]) else 0.0,
    }


def extra_row(run: dict[str, object], extra: pd.DataFrame) -> dict[str, object]:
    baseline_count = len(extra_trades(run["trades"], extra, reverse=True))
    return {
        "system": run["system"],
        "leg_key": run["leg_key"],
        "label": run["label"],
        "variant": run["variant"],
        "baseline_trades": baseline_count,
        "variant_trades": len(run["trades"]),
        "extra_trades": len(extra),
        "extra_avg_trades_per_month": round(len(extra) / active_month_count(run["trades"]), 2) if len(run["trades"]) else 0.0,
        **prefix_stats("extra_", stats(extra)),
    }


def extra_trades(variant: pd.DataFrame, baseline: pd.DataFrame, reverse: bool = False) -> pd.DataFrame:
    if variant.empty:
        return variant.copy()
    if baseline.empty:
        return variant.copy()
    key = ["entry_time", "direction", "symbol", "timeframe"]
    left = baseline if reverse else variant
    right = variant if reverse else baseline
    merged = left.merge(right[key].drop_duplicates(), on=key, how="left", indicator=True)
    return merged[merged["_merge"] == "left_only"].drop(columns=["_merge"])


def pair_impact_rows(run: dict[str, object], baseline: dict[str, object], baselines: dict[str, dict[str, object]]) -> list[dict[str, object]]:
    other_key = pair_other_key(run["leg_key"])
    baseline_pair = pd.concat([baseline["trades"], baselines[other_key]["trades"]], ignore_index=True)
    variant_pair = pd.concat([run["trades"], baselines[other_key]["trades"]], ignore_index=True)
    return [
        pair_row(run, "baseline_pair", apply_daily_cap(baseline_pair)),
        pair_row(run, "variant_pair", apply_daily_cap(variant_pair)),
    ]


def pair_other_key(leg_key: str) -> str:
    return {
        "funded_nq": "funded_spx",
        "funded_spx": "funded_nq",
        "phase_nq": "phase_spx",
        "phase_spx": "phase_nq",
    }[leg_key]


def apply_daily_cap(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return trades
    capped = trades.copy()
    capped["entry_time_dt"] = pd.to_datetime(capped["entry_time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
    capped["group"] = "pair"
    capped["label"] = capped["symbol"]
    return apply_pair_risk_rule(capped, -1.0)


def pair_row(run: dict[str, object], pair_case: str, trades: pd.DataFrame) -> dict[str, object]:
    result = {
        "system": run["system"],
        "changed_leg": run["leg_key"],
        "variant": run["variant"],
        "pair_case": pair_case,
        **stats(trades),
        "avg_trades_per_month": round(len(trades) / active_month_count(trades), 2) if len(trades) else 0.0,
    }
    daily = trades.groupby("date")["r_multiple"].sum() if not trades.empty else pd.Series(dtype=float)
    result["worst_day_r"] = round(float(daily.min()), 2) if len(daily) else 0.0
    return result


def stats(trades: pd.DataFrame) -> dict[str, object]:
    if trades.empty:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "net_r": 0.0,
            "max_drawdown_r": 0.0,
            "profit_factor": 0.0,
            "net_r_per_dd": "",
        }
    ordered = trades.copy()
    if "entry_time_dt" not in ordered.columns:
        ordered["entry_time_dt"] = pd.to_datetime(ordered["entry_time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
    ordered = ordered.sort_values("entry_time_dt")
    wins = int((ordered["result"] == "win").sum())
    losses = int((ordered["r_multiple"] < 0).sum())
    net_r = float(ordered["r_multiple"].sum())
    equity = ordered["r_multiple"].cumsum()
    dd = float((equity - equity.cummax()).min())
    gross_win = float(ordered.loc[ordered["r_multiple"] > 0, "r_multiple"].sum())
    gross_loss = abs(float(ordered.loc[ordered["r_multiple"] < 0, "r_multiple"].sum()))
    return {
        "trades": len(ordered),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / len(ordered) * 100, 2),
        "net_r": round(net_r, 2),
        "max_drawdown_r": round(dd, 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else 0.0,
        "net_r_per_dd": round(net_r / abs(dd), 2) if dd else "",
    }


def active_month_count(trades: pd.DataFrame) -> int:
    if trades.empty:
        return 1
    entry_dt = pd.to_datetime(trades["entry_time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
    return max(1, entry_dt.dt.tz_localize(None).dt.to_period("M").nunique())


def prefix_stats(prefix: str, data: dict[str, object]) -> dict[str, object]:
    return {f"{prefix}{key}": value for key, value in data.items()}


def write_report(candidate: pd.DataFrame, extra: pd.DataFrame, pair: pd.DataFrame) -> None:
    lines = [
        "# Frequency Expansion Tests",
        "",
        "Goal: find ways to add trades without damaging the selected funded/phase systems.",
        "",
        "## Candidate Comparison",
        "",
        base_report.markdown_table(candidate),
        "",
        "## Extra Trades Only",
        "",
        base_report.markdown_table(extra),
        "",
        "## Pair Impact With Daily -1R Cap",
        "",
        base_report.markdown_table(pair),
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
