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
import run_next_chat_action_plan_tests as previous
import run_selected_candidates_full_data_report as selected
from backtest.data_loader import load_ohlcv
from backtest.risk import apply_pair_risk_rule
from backtest.strategy import run_backtest, summarize_trades, trades_to_frame


REPORT_DIR = ROOT / "outputs" / "reports" / "followup_combination_tests"


def main() -> None:
    reset_report_dir()
    loaded = load_data()

    candidate_rows: list[dict[str, object]] = []
    monthly_rows: list[pd.DataFrame] = []
    trade_frames: dict[str, pd.DataFrame] = {}

    for spec in build_candidate_specs():
        frame = loaded[(spec["candidate"].symbol, spec["candidate"].timeframe)].copy()
        config = spec["config"]
        result = run_backtest(frame, config)
        trades = trades_to_frame(result.trades)
        if not trades.empty:
            tagged = trades.copy()
            tagged.insert(0, "variant", spec["variant"])
            tagged.insert(0, "test_group", spec["test_group"])
            tagged.insert(0, "label", spec["candidate"].label)
            tagged.insert(0, "group", spec["candidate"].group)
            tagged.insert(0, "candidate_slug", spec["candidate"].slug)
            trade_frames[spec["variant"]] = tagged
            tagged.to_csv(REPORT_DIR / f"trades_{spec['variant']}.csv", index=False)
        else:
            trade_frames[spec["variant"]] = trades
        candidate_rows.append(build_candidate_row(spec, result.trades))
        monthly = previous_monthly_frame(spec, result.trades)
        if not monthly.empty:
            monthly_rows.append(monthly)

    candidate_comparison = pd.DataFrame(candidate_rows)
    monthly_detail = pd.concat(monthly_rows, ignore_index=True) if monthly_rows else pd.DataFrame()
    all_trades = pd.concat([frame for frame in trade_frames.values() if not frame.empty], ignore_index=True)
    pair_comparison = build_pair_comparison(trade_frames)

    candidate_comparison.to_csv(REPORT_DIR / "candidate_comparison.csv", index=False)
    monthly_detail.to_csv(REPORT_DIR / "monthly_detail.csv", index=False)
    all_trades.to_csv(REPORT_DIR / "all_trades.csv", index=False)
    pair_comparison.to_csv(REPORT_DIR / "pair_comparison.csv", index=False)
    write_report(candidate_comparison, pair_comparison, monthly_detail)
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


def build_candidate_specs() -> list[dict[str, object]]:
    specs: list[dict[str, object]] = []
    for candidate in selected.CANDIDATES:
        base = selected.build_config(candidate)
        if candidate.label == "NQ funded":
            specs.append(spec("funded_nq_baseline", "funded_pair_anchor", candidate, base))
        elif candidate.label == "SPX funded":
            specs.extend(
                [
                    spec("spx_funded_baseline", "spx_funded_combined", candidate, base),
                    spec(
                        "spx_funded_1045",
                        "spx_funded_combined",
                        candidate,
                        replace(base, trade_window_end="10:45"),
                    ),
                    spec(
                        "spx_funded_1045_close_away",
                        "spx_funded_combined",
                        candidate,
                        replace(base, trade_window_end="10:45", require_close_away_before_entry=True),
                    ),
                    spec(
                        "spx_funded_latest_entry_1030",
                        "spx_funded_combined",
                        candidate,
                        replace(base, latest_entry_time="10:30"),
                    ),
                    spec(
                        "spx_funded_latest_entry_1030_close_away",
                        "spx_funded_combined",
                        candidate,
                        replace(base, latest_entry_time="10:30", require_close_away_before_entry=True),
                    ),
                ]
            )
        elif candidate.label == "NQ phase":
            specs.extend(
                [
                    spec("nq_phase_baseline", "nq_phase_combined", candidate, base),
                    spec(
                        "nq_phase_min_touches_3",
                        "nq_phase_combined",
                        candidate,
                        replace(base, strong_swing_min_touches=3),
                    ),
                    spec(
                        "nq_phase_directionality_0p21",
                        "nq_phase_combined",
                        candidate,
                        replace(base, swing_first30_directionality_min=0.21),
                    ),
                    spec(
                        "nq_phase_min_touches_3_directionality_0p21",
                        "nq_phase_combined",
                        candidate,
                        replace(base, strong_swing_min_touches=3, swing_first30_directionality_min=0.21),
                    ),
                ]
            )
        elif candidate.label == "SPX phase":
            specs.extend(
                [
                    spec("spx_phase_baseline", "spx_phase_combined", candidate, base),
                    spec(
                        "spx_phase_1030",
                        "spx_phase_combined",
                        candidate,
                        replace(base, trade_window_end="10:30"),
                    ),
                    spec(
                        "spx_phase_1030_close_away",
                        "spx_phase_combined",
                        candidate,
                        replace(base, trade_window_end="10:30", require_close_away_before_entry=True),
                    ),
                    spec(
                        "spx_phase_latest_entry_1030",
                        "spx_phase_combined",
                        candidate,
                        replace(base, latest_entry_time="10:30"),
                    ),
                    spec(
                        "spx_phase_latest_entry_1030_close_away",
                        "spx_phase_combined",
                        candidate,
                        replace(base, latest_entry_time="10:30", require_close_away_before_entry=True),
                    ),
                ]
            )
    return specs


def spec(variant: str, test_group: str, candidate: selected.SelectedCandidate, config) -> dict[str, object]:
    return {"variant": variant, "test_group": test_group, "candidate": candidate, "config": config}


def build_candidate_row(spec: dict[str, object], trades: list) -> dict[str, object]:
    candidate = spec["candidate"]
    summary = summarize_trades(trades).iloc[0].to_dict()
    frame = previous.enrich_trade_timing(trades_to_frame(trades))
    net_r = float(summary.get("net_r", 0) or 0)
    max_dd = float(summary.get("max_drawdown_r", 0) or 0)
    return {
        "test_group": spec["test_group"],
        "variant": spec["variant"],
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
        "fast_0_2_candle_sl": previous.fast_sl_count(frame),
        "fast_0_2_candle_sl_pct_of_losses": previous.fast_sl_pct(frame),
        "after_1030_losses": previous.after_1030_loss_count(frame),
        "swing_loss_pct": previous.swing_loss_pct(frame),
    }


def previous_monthly_frame(spec: dict[str, object], trades: list) -> pd.DataFrame:
    from backtest.strategy import monthly_stats

    candidate = spec["candidate"]
    monthly = monthly_stats(trades)
    if monthly.empty:
        return monthly
    monthly = monthly.copy()
    monthly.insert(0, "variant", spec["variant"])
    monthly.insert(0, "test_group", spec["test_group"])
    monthly.insert(0, "label", candidate.label)
    monthly.insert(0, "group", candidate.group)
    return monthly


def build_pair_comparison(trade_frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    funded_spx_variants = [
        "spx_funded_baseline",
        "spx_funded_1045",
        "spx_funded_1045_close_away",
        "spx_funded_latest_entry_1030",
        "spx_funded_latest_entry_1030_close_away",
    ]
    for spx_variant in funded_spx_variants:
        rows.extend(
            pair_rows(
                group="funded",
                pair_variant=f"nq_baseline__{spx_variant}",
                left=trade_frames["funded_nq_baseline"],
                right=trade_frames[spx_variant],
                include_cap=True,
            )
        )

    nq_variants = [
        "nq_phase_min_touches_3",
        "nq_phase_directionality_0p21",
        "nq_phase_min_touches_3_directionality_0p21",
    ]
    spx_variants = [
        "spx_phase_1030",
        "spx_phase_1030_close_away",
        "spx_phase_latest_entry_1030",
        "spx_phase_latest_entry_1030_close_away",
    ]
    for nq_variant in nq_variants:
        for spx_variant in spx_variants:
            rows.extend(
                pair_rows(
                    group="phase",
                    pair_variant=f"{nq_variant}__{spx_variant}",
                    left=trade_frames[nq_variant],
                    right=trade_frames[spx_variant],
                    include_cap=True,
                )
            )
    return pd.DataFrame(rows)


def pair_rows(group: str, pair_variant: str, left: pd.DataFrame, right: pd.DataFrame, include_cap: bool) -> list[dict[str, object]]:
    pair = pd.concat([left, right], ignore_index=True)
    pair["entry_time_dt"] = pd.to_datetime(pair["entry_time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
    rows = [build_pair_row(group, pair_variant, "no_cap", pair)]
    if include_cap:
        capped = apply_pair_risk_rule(pair, -1.0)
        rows.append(build_pair_row(group, pair_variant, "daily_loss_cap_minus_1r", capped))
    return rows


def build_pair_row(group: str, pair_variant: str, risk_rule: str, trades: pd.DataFrame) -> dict[str, object]:
    ordered = trades.sort_values("entry_time_dt")
    net_r = float(ordered["r_multiple"].sum())
    max_dd = previous.max_drawdown(ordered)
    daily = ordered.groupby("date")["r_multiple"].sum()
    return {
        "group": group,
        "pair_variant": pair_variant,
        "risk_rule": risk_rule,
        "trades": len(ordered),
        "unique_dates": int(ordered["date"].nunique()),
        "wins": int((ordered["result"] == "win").sum()),
        "losses": int((ordered["r_multiple"] < 0).sum()),
        "win_rate": round(float((ordered["result"] == "win").mean() * 100), 2) if len(ordered) else 0.0,
        "net_r": round(net_r, 2),
        "max_drawdown_r": round(max_dd, 2),
        "profit_factor": previous.profit_factor(ordered),
        "net_r_per_dd": round(net_r / abs(max_dd), 2) if max_dd else "",
        "worst_day_r": round(float(daily.min()), 2) if len(daily) else 0.0,
        "positive_months": positive_months(ordered),
        "negative_months": negative_months(ordered),
    }


def positive_months(trades: pd.DataFrame) -> int:
    monthly = trades.groupby(trades["entry_time_dt"].dt.strftime("%Y-%m"))["r_multiple"].sum()
    return int((monthly > 0).sum())


def negative_months(trades: pd.DataFrame) -> int:
    monthly = trades.groupby(trades["entry_time_dt"].dt.strftime("%Y-%m"))["r_multiple"].sum()
    return int((monthly < 0).sum())


def write_report(candidate: pd.DataFrame, pair: pd.DataFrame, monthly: pd.DataFrame) -> None:
    lines = [
        "# Follow-Up Combination Tests",
        "",
        "Scope: full available Dukascopy data. Tests combinations selected after the next-chat action plan report.",
        "",
        "## Candidate Comparison",
        "",
        base_report.markdown_table(candidate),
        "",
        "## Pair Comparison",
        "",
        base_report.markdown_table(pair),
        "",
        "## Best Rows",
        "",
        "### Funded Pair By Net R/DD",
        "",
        base_report.markdown_table(best_rows(pair[pair["group"] == "funded"], "net_r_per_dd")),
        "",
        "### Phase Pair By Net R/DD",
        "",
        base_report.markdown_table(best_rows(pair[pair["group"] == "phase"], "net_r_per_dd")),
        "",
        "### Phase Pair By Net R",
        "",
        base_report.markdown_table(best_rows(pair[pair["group"] == "phase"], "net_r")),
        "",
        "## Monthly Detail",
        "",
        base_report.markdown_table(monthly) if not monthly.empty else "No monthly rows.",
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


def best_rows(frame: pd.DataFrame, column: str, count: int = 8) -> pd.DataFrame:
    if frame.empty:
        return frame
    sortable = frame.copy()
    sortable[column] = pd.to_numeric(sortable[column], errors="coerce").fillna(-999999)
    return sortable.sort_values(column, ascending=False).head(count)


if __name__ == "__main__":
    main()
