from __future__ import annotations

import sys
import argparse
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
from backtest.risk import apply_pair_risk_rule as shared_apply_pair_risk_rule
from backtest.strategy import run_backtest, summarize_trades, trades_to_frame


REPORT_DIR = ROOT / "outputs" / "reports" / "next_chat_action_plan_tests"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", choices=["all", "1", "2", "3", "4", "5"], default="all")
    parser.add_argument("--variant", help="Run only one variant name for the selected test")
    args = parser.parse_args()
    if args.test == "all":
        reset_report_dir()
    else:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
    loaded = load_data()

    if args.test in {"all", "1"}:
        spx_time, spx_time_trades = run_spx_time_cutoff_test(loaded, args.variant)
        merge_csv(REPORT_DIR / "01_spx_time_cutoff.csv", spx_time, ["test", "variant", "candidate_slug"])
        write_optional_trades("01_spx_time_cutoff_trades.csv", spx_time_trades)
    if args.test in {"all", "2"}:
        spx_fast, spx_fast_trades = run_spx_fast_stop_test(loaded, args.variant)
        merge_csv(REPORT_DIR / "02_spx_fast_stop_reduction.csv", spx_fast, ["test", "variant", "candidate_slug"])
        write_optional_trades("02_spx_fast_stop_reduction_trades.csv", spx_fast_trades)
    if args.test in {"all", "3"}:
        nq_swing, nq_swing_trades = run_nq_stronger_swing_test(loaded, args.variant)
        merge_csv(REPORT_DIR / "03_nq_stronger_swing_filter.csv", nq_swing, ["test", "variant", "candidate_slug"])
        write_optional_trades("03_nq_stronger_swing_filter_trades.csv", nq_swing_trades)
    if args.test in {"all", "4"}:
        nq_directionality, nq_directionality_trades = run_nq_swing_directionality_test(loaded, args.variant)
        merge_csv(REPORT_DIR / "04_nq_swing_first30_directionality.csv", nq_directionality, ["test", "variant", "candidate_slug"])
        write_optional_trades("04_nq_swing_first30_directionality_trades.csv", nq_directionality_trades)
    if args.test in {"all", "5"}:
        overlap = run_same_day_overlap_risk_cap_test()
        overlap.to_csv(REPORT_DIR / "05_same_day_overlap_risk_cap.csv", index=False)

    write_report_from_files()
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
    data: dict[tuple[str, str], pd.DataFrame] = {}
    for candidate in selected.CANDIDATES:
        key = (candidate.symbol, candidate.timeframe)
        if key in data:
            continue
        market_data = load_ohlcv(base_report.filter_paths(base_report.RAW_DIR, candidate.symbol, candidate.timeframe))
        data[key] = market_data.frame.copy()
    return data


def run_spx_time_cutoff_test(loaded: dict[tuple[str, str], pd.DataFrame], only_variant: str | None = None) -> tuple[pd.DataFrame, list[pd.DataFrame]]:
    variants = [
        ("baseline", None, None),
        ("window_0930_1030", "10:30", None),
        ("window_0930_1045", "10:45", None),
        ("latest_entry_1030_keep_setup_window", None, "10:30"),
    ]
    rows = []
    trades = []
    for candidate in spx_candidates():
        for variant, trade_window_end, latest_entry_time in variants:
            if only_variant is not None and variant != only_variant:
                continue
            config = selected.build_config(candidate)
            config = replace(
                config,
                trade_window_end=trade_window_end or candidate.trade_window_end,
                latest_entry_time=latest_entry_time,
            )
            frame = loaded[(candidate.symbol, candidate.timeframe)].copy()
            result = run_backtest(frame, config)
            trades_df = tag_trades(trades_to_frame(result.trades), "01_spx_time_cutoff", variant, candidate)
            trades.append(trades_df)
            rows.append(build_row("01_spx_time_cutoff", variant, candidate, result.trades))
    return pd.DataFrame(rows), trades


def run_spx_fast_stop_test(loaded: dict[tuple[str, str], pd.DataFrame], only_variant: str | None = None) -> tuple[pd.DataFrame, list[pd.DataFrame]]:
    variants = [
        ("baseline", 0, False),
        ("skip_first_1_candle_after_fvg", 1, False),
        ("skip_first_2_candles_after_fvg", 2, False),
        ("require_one_close_away_before_fill", 0, True),
    ]
    rows = []
    trades = []
    for candidate in spx_candidates():
        for variant, delay, close_away in variants:
            if only_variant is not None and variant != only_variant:
                continue
            config = selected.build_config(candidate)
            config = replace(
                config,
                min_entry_delay_candles_after_fvg=delay,
                require_close_away_before_entry=close_away,
            )
            frame = loaded[(candidate.symbol, candidate.timeframe)].copy()
            result = run_backtest(frame, config)
            trades_df = tag_trades(trades_to_frame(result.trades), "02_spx_fast_stop_reduction", variant, candidate)
            trades.append(trades_df)
            rows.append(build_row("02_spx_fast_stop_reduction", variant, candidate, result.trades))
    return pd.DataFrame(rows), trades


def run_nq_stronger_swing_test(loaded: dict[tuple[str, str], pd.DataFrame], only_variant: str | None = None) -> tuple[pd.DataFrame, list[pd.DataFrame]]:
    variants = [2, 3, 4]
    rows = []
    trades = []
    candidate = nq_phase_candidate()
    for touches in variants:
        variant = f"strong_swing_min_touches_{touches}"
        if only_variant is not None and variant != only_variant:
            continue
        config = replace(selected.build_config(candidate), strong_swing_min_touches=touches)
        frame = loaded[(candidate.symbol, candidate.timeframe)].copy()
        result = run_backtest(frame, config)
        trades_df = tag_trades(trades_to_frame(result.trades), "03_nq_stronger_swing_filter", variant, candidate)
        trades.append(trades_df)
        rows.append(build_row("03_nq_stronger_swing_filter", variant, candidate, result.trades))
    return pd.DataFrame(rows), trades


def run_nq_swing_directionality_test(loaded: dict[tuple[str, str], pd.DataFrame], only_variant: str | None = None) -> tuple[pd.DataFrame, list[pd.DataFrame]]:
    variants = [
        ("baseline_no_directionality_filter", None),
        ("swing_first30_directionality_gte_0p21", 0.21),
        ("swing_first30_directionality_gte_0p30", 0.30),
        ("swing_first30_directionality_gte_0p40", 0.40),
    ]
    rows = []
    trades = []
    candidate = nq_phase_candidate()
    for variant, threshold in variants:
        if only_variant is not None and variant != only_variant:
            continue
        config = replace(selected.build_config(candidate), swing_first30_directionality_min=threshold)
        frame = loaded[(candidate.symbol, candidate.timeframe)].copy()
        result = run_backtest(frame, config)
        trades_df = tag_trades(trades_to_frame(result.trades), "04_nq_swing_first30_directionality", variant, candidate)
        trades.append(trades_df)
        rows.append(build_row("04_nq_swing_first30_directionality", variant, candidate, result.trades))
    return pd.DataFrame(rows), trades


def run_same_day_overlap_risk_cap_test() -> pd.DataFrame:
    source = ROOT / "outputs" / "reports" / "selected_candidates_full_data" / "all_trades.csv"
    if not source.exists():
        raise SystemExit(f"Missing source report. Run selected candidates report first: {source}")
    trades = pd.read_csv(source)
    trades["entry_time_dt"] = pd.to_datetime(trades["entry_time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
    variants = [
        ("baseline", None),
        ("skip_second_symbol_if_first_symbol_loses", "skip_second_after_first_loss"),
        ("max_daily_loss_cap_minus_1r", -1.0),
        ("max_daily_loss_cap_minus_1p5r", -1.5),
        ("max_daily_loss_cap_minus_2r", -2.0),
    ]
    rows = []
    for variant, rule in variants:
        filtered = apply_pair_risk_rule(trades, rule)
        for group, group_trades in filtered.groupby("group"):
            rows.append(build_pair_row("05_same_day_overlap_risk_cap", variant, group, group_trades))
    return pd.DataFrame(rows)


def apply_pair_risk_rule(trades: pd.DataFrame, rule: object) -> pd.DataFrame:
    return shared_apply_pair_risk_rule(trades, rule)


def spx_candidates() -> list[selected.SelectedCandidate]:
    return [candidate for candidate in selected.CANDIDATES if candidate.label.startswith("SPX")]


def nq_phase_candidate() -> selected.SelectedCandidate:
    for candidate in selected.CANDIDATES:
        if candidate.label == "NQ phase":
            return candidate
    raise RuntimeError("NQ phase candidate not found")


def tag_trades(frame: pd.DataFrame, test: str, variant: str, candidate: selected.SelectedCandidate) -> pd.DataFrame:
    if frame.empty:
        return frame
    tagged = frame.copy()
    tagged.insert(0, "variant", variant)
    tagged.insert(0, "test", test)
    tagged.insert(0, "label", candidate.label)
    tagged.insert(0, "group", candidate.group)
    tagged.insert(0, "candidate_slug", candidate.slug)
    return tagged


def write_optional_trades(name: str, frames: list[pd.DataFrame]) -> None:
    non_empty = [frame for frame in frames if not frame.empty]
    if non_empty:
        merge_csv(REPORT_DIR / name, pd.concat(non_empty, ignore_index=True), ["test", "variant", "candidate_slug", "entry_time"])


def merge_csv(path: Path, frame: pd.DataFrame, keys: list[str]) -> None:
    if frame.empty:
        return
    if path.exists():
        existing = pd.read_csv(path)
        combined = pd.concat([existing, frame], ignore_index=True)
        available_keys = [key for key in keys if key in combined.columns]
        if available_keys:
            combined = combined.drop_duplicates(subset=available_keys, keep="last")
    else:
        combined = frame
    combined.to_csv(path, index=False)


def build_row(test: str, variant: str, candidate: selected.SelectedCandidate, trades: list) -> dict[str, object]:
    summary = summarize_trades(trades).iloc[0].to_dict()
    frame = trades_to_frame(trades)
    enriched = enrich_trade_timing(frame)
    net_r = float(summary.get("net_r", 0) or 0)
    max_dd = float(summary.get("max_drawdown_r", 0) or 0)
    return {
        "test": test,
        "variant": variant,
        "group": candidate.group,
        "label": candidate.label,
        "candidate_slug": candidate.slug,
        "trades": int(summary.get("total_trades", 0) or 0),
        "wins": int(summary.get("wins", 0) or 0),
        "losses": int(summary.get("losses", 0) or 0),
        "win_rate": float(summary.get("win_rate", 0) or 0),
        "net_r": net_r,
        "max_drawdown_r": max_dd,
        "profit_factor": float(summary.get("profit_factor", 0) or 0),
        "net_r_per_dd": round(net_r / abs(max_dd), 2) if max_dd else "",
        "fast_0_2_candle_sl": fast_sl_count(enriched),
        "fast_0_2_candle_sl_pct_of_losses": fast_sl_pct(enriched),
        "after_1030_losses": after_1030_loss_count(enriched),
        "swing_loss_pct": swing_loss_pct(enriched),
    }


def build_pair_row(test: str, variant: str, group: str, trades: pd.DataFrame) -> dict[str, object]:
    ordered = trades.sort_values("entry_time_dt")
    net_r = float(ordered["r_multiple"].sum())
    max_dd = max_drawdown(ordered)
    return {
        "test": test,
        "variant": variant,
        "group": group,
        "trades": len(ordered),
        "unique_dates": int(ordered["date"].nunique()),
        "wins": int((ordered["result"] == "win").sum()),
        "losses": int((ordered["r_multiple"] < 0).sum()),
        "win_rate": round(float((ordered["result"] == "win").mean() * 100), 2) if len(ordered) else 0.0,
        "net_r": round(net_r, 2),
        "max_drawdown_r": round(max_dd, 2),
        "profit_factor": profit_factor(ordered),
        "net_r_per_dd": round(net_r / abs(max_dd), 2) if max_dd else "",
        "worst_day_r": round(float(ordered.groupby("date")["r_multiple"].sum().min()), 2) if len(ordered) else 0.0,
    }


def enrich_trade_timing(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    enriched = frame.copy()
    for column in ["entry_time", "exit_time"]:
        enriched[f"{column}_dt"] = pd.to_datetime(enriched[column], utc=True, format="mixed").dt.tz_convert("America/New_York")
    enriched["entry_minutes"] = enriched["entry_time_dt"].dt.hour * 60 + enriched["entry_time_dt"].dt.minute
    enriched["timeframe_minutes"] = enriched["timeframe"].map(lambda value: int(str(value).rstrip("m")))
    enriched["exit_candles_after_entry"] = (
        enriched["exit_time_dt"] - enriched["entry_time_dt"]
    ).dt.total_seconds() / 60 / enriched["timeframe_minutes"]
    return enriched


def fast_sl_count(frame: pd.DataFrame) -> int:
    if frame.empty:
        return 0
    losses = frame[frame["r_multiple"] < 0]
    return int((losses["exit_candles_after_entry"] <= 2).sum())


def fast_sl_pct(frame: pd.DataFrame) -> float:
    if frame.empty:
        return 0.0
    losses = frame[frame["r_multiple"] < 0]
    if losses.empty:
        return 0.0
    return round(float((losses["exit_candles_after_entry"] <= 2).mean() * 100), 2)


def after_1030_loss_count(frame: pd.DataFrame) -> int:
    if frame.empty:
        return 0
    losses = frame[frame["r_multiple"] < 0]
    return int((losses["entry_minutes"] >= 10 * 60 + 30).sum())


def swing_loss_pct(frame: pd.DataFrame) -> float:
    if frame.empty:
        return 0.0
    losses = frame[frame["r_multiple"] < 0]
    if losses.empty:
        return 0.0
    return round(float(losses["liquidity_context"].astype(str).str.startswith("swing_").mean() * 100), 2)


def max_drawdown(frame: pd.DataFrame) -> float:
    equity = frame["r_multiple"].cumsum()
    drawdown = equity - equity.cummax()
    return float(drawdown.min()) if len(drawdown) else 0.0


def profit_factor(frame: pd.DataFrame) -> float:
    gross_win = float(frame.loc[frame["r_multiple"] > 0, "r_multiple"].sum())
    gross_loss = abs(float(frame.loc[frame["r_multiple"] < 0, "r_multiple"].sum()))
    return round(gross_win / gross_loss, 2) if gross_loss else 0.0


def write_report(
    spx_time: pd.DataFrame,
    spx_fast: pd.DataFrame,
    nq_swing: pd.DataFrame,
    nq_directionality: pd.DataFrame,
    overlap: pd.DataFrame,
) -> None:
    lines = [
        "# Next-Chat Action Plan Tests",
        "",
        "Scope: full available Dukascopy data for the four currently selected candidates.",
        "",
        "## 1. SPX Time Cutoff Test",
        "",
        base_report.markdown_table(spx_time),
        "",
        "## 2. SPX Fast-Stop Reduction Test",
        "",
        base_report.markdown_table(spx_fast),
        "",
        "## 3. NQ Stronger Swing Filter Test",
        "",
        base_report.markdown_table(nq_swing),
        "",
        "## 4. NQ Swing + First30 Directionality Test",
        "",
        base_report.markdown_table(nq_directionality),
        "",
        "## 5. Same-Day Overlap Risk Cap Test",
        "",
        base_report.markdown_table(overlap),
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


def write_report_from_files() -> None:
    frames = {
        "spx_time": read_report_csv("01_spx_time_cutoff.csv"),
        "spx_fast": read_report_csv("02_spx_fast_stop_reduction.csv"),
        "nq_swing": read_report_csv("03_nq_stronger_swing_filter.csv"),
        "nq_directionality": read_report_csv("04_nq_swing_first30_directionality.csv"),
        "overlap": read_report_csv("05_same_day_overlap_risk_cap.csv"),
    }
    write_report(
        frames["spx_time"],
        frames["spx_fast"],
        frames["nq_swing"],
        frames["nq_directionality"],
        frames["overlap"],
    )


def read_report_csv(name: str) -> pd.DataFrame:
    path = REPORT_DIR / name
    return pd.read_csv(path) if path.exists() else pd.DataFrame()


if __name__ == "__main__":
    main()
