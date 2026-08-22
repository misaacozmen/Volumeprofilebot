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
from backtest.strategy import run_backtest, summarize_trades, trades_to_frame


REPORT_DIR = ROOT / "outputs" / "reports" / "selected_spx_rr_tests"
RR_VALUES = [2.0, 2.5, 3.0]


def main() -> None:
    reset_report_dir()
    loaded = load_data()
    rows = []
    pair_rows = []
    all_trades = []

    nq_funded = run_candidate("funded_nq_fixed", "funded", "NQ funded fixed", funded_nq(), selected.build_config(funded_nq()), loaded)
    nq_phase = run_candidate(
        "phase_nq_fixed",
        "phase",
        "NQ phase fixed",
        phase_nq(),
        replace(selected.build_config(phase_nq()), strong_swing_min_touches=3),
        loaded,
    )

    for rr in RR_VALUES:
        funded_spx_config = replace(selected.build_config(funded_spx_candidate()), reward_r=rr, latest_entry_time="10:30")
        funded_spx_run = run_candidate(rr_slug("funded_spx", rr), "funded", f"SPX funded {rr:g}R", funded_spx_candidate(), funded_spx_config, loaded)
        rows.append(build_candidate_row(funded_spx_run["label"], funded_spx_run["trades"]))
        pair_rows.extend(build_pair_rows("funded_selected", f"spx_rr_{rr:g}", nq_funded["trades"], funded_spx_run["trades"]))
        all_trades.append(funded_spx_run["tagged"])

        phase_spx_config = replace(selected.build_config(phase_spx_candidate()), reward_r=rr, latest_entry_time="10:30")
        phase_spx_run = run_candidate(rr_slug("phase_spx", rr), "phase", f"SPX phase {rr:g}R", phase_spx_candidate(), phase_spx_config, loaded)
        rows.append(build_candidate_row(phase_spx_run["label"], phase_spx_run["trades"]))
        pair_rows.extend(build_pair_rows("phase_selected", f"spx_rr_{rr:g}", nq_phase["trades"], phase_spx_run["trades"]))
        all_trades.append(phase_spx_run["tagged"])

    rows.append(build_candidate_row(nq_funded["label"], nq_funded["trades"]))
    rows.append(build_candidate_row(nq_phase["label"], nq_phase["trades"]))
    all_trades.extend([nq_funded["tagged"], nq_phase["tagged"]])

    candidate = pd.DataFrame(rows)
    pair = pd.DataFrame(pair_rows)
    trades = pd.concat([frame for frame in all_trades if not frame.empty], ignore_index=True)

    candidate.to_csv(REPORT_DIR / "candidate_comparison.csv", index=False)
    pair.to_csv(REPORT_DIR / "pair_comparison.csv", index=False)
    trades.to_csv(REPORT_DIR / "all_trades.csv", index=False)
    write_report(candidate, pair)
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
    for candidate in [funded_nq(), funded_spx_candidate(), phase_nq(), phase_spx_candidate()]:
        key = (candidate.symbol, candidate.timeframe)
        if key in data:
            continue
        data[key] = load_ohlcv(base_report.filter_paths(base_report.RAW_DIR, candidate.symbol, candidate.timeframe)).frame
    return data


def funded_nq() -> selected.SelectedCandidate:
    return candidate_by_label("NQ funded")


def funded_spx_candidate() -> selected.SelectedCandidate:
    return candidate_by_label("SPX funded")


def phase_nq() -> selected.SelectedCandidate:
    return candidate_by_label("NQ phase")


def phase_spx_candidate() -> selected.SelectedCandidate:
    return candidate_by_label("SPX phase")


def candidate_by_label(label: str) -> selected.SelectedCandidate:
    for candidate in selected.CANDIDATES:
        if candidate.label == label:
            return candidate
    raise RuntimeError(f"Missing candidate: {label}")


def rr_slug(prefix: str, rr: float) -> str:
    return f"{prefix}_{str(rr).rstrip('0').rstrip('.').replace('.', 'p')}r"


def run_candidate(slug: str, group: str, label: str, candidate: selected.SelectedCandidate, config, loaded: dict[tuple[str, str], pd.DataFrame]) -> dict[str, object]:
    result = run_backtest(loaded[(candidate.symbol, candidate.timeframe)].copy(), config)
    trades = trades_to_frame(result.trades)
    tagged = trades.copy()
    if not tagged.empty:
        tagged.insert(0, "rr_test_slug", slug)
        tagged.insert(0, "label", label)
        tagged.insert(0, "group", group)
    return {"slug": slug, "group": group, "label": label, "trades": trades, "tagged": tagged}


def build_candidate_row(label: str, trades: pd.DataFrame) -> dict[str, object]:
    summary = summarize_trades([] if trades.empty else [None]).iloc[0].to_dict() if False else summarize_frame(trades)
    return {"label": label, **summary}


def summarize_frame(trades: pd.DataFrame) -> dict[str, object]:
    if trades.empty:
        return empty_stats()
    wins = int((trades["result"] == "win").sum())
    losses = int((trades["r_multiple"] < 0).sum())
    net_r = float(trades["r_multiple"].sum())
    dd = max_drawdown(trades)
    return {
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / len(trades) * 100, 2),
        "net_r": round(net_r, 2),
        "max_drawdown_r": round(dd, 2),
        "profit_factor": profit_factor(trades),
        "net_r_per_dd": round(net_r / abs(dd), 2) if dd else "",
    }


def empty_stats() -> dict[str, object]:
    return {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "net_r": 0.0, "max_drawdown_r": 0.0, "profit_factor": 0.0, "net_r_per_dd": ""}


def build_pair_rows(system: str, variant: str, nq: pd.DataFrame, spx: pd.DataFrame) -> list[dict[str, object]]:
    pair = pd.concat([nq, spx], ignore_index=True)
    pair["entry_time_dt"] = pd.to_datetime(pair["entry_time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
    no_cap = pair.copy()
    capped = pair.copy()
    capped["group"] = system
    capped["label"] = capped["symbol"]
    capped = apply_pair_risk_rule(capped, -1.0)
    return [
        build_pair_row(system, variant, "no_cap", no_cap),
        build_pair_row(system, variant, "daily_loss_cap_minus_1r", capped),
    ]


def build_pair_row(system: str, variant: str, risk_rule: str, trades: pd.DataFrame) -> dict[str, object]:
    ordered = trades.sort_values("entry_time_dt")
    stats = summarize_frame(ordered)
    daily = ordered.groupby("date")["r_multiple"].sum()
    return {
        "system": system,
        "variant": variant,
        "risk_rule": risk_rule,
        **stats,
        "unique_dates": int(ordered["date"].nunique()),
        "worst_day_r": round(float(daily.min()), 2) if len(daily) else 0.0,
    }


def max_drawdown(trades: pd.DataFrame) -> float:
    ordered = trades.copy()
    if "entry_time_dt" not in ordered.columns:
        ordered["entry_time_dt"] = pd.to_datetime(ordered["entry_time"], utc=True, format="mixed").dt.tz_convert("America/New_York")
    equity = ordered.sort_values("entry_time_dt")["r_multiple"].cumsum()
    dd = equity - equity.cummax()
    return float(dd.min()) if len(dd) else 0.0


def profit_factor(trades: pd.DataFrame) -> float:
    gross_win = float(trades.loc[trades["r_multiple"] > 0, "r_multiple"].sum())
    gross_loss = abs(float(trades.loc[trades["r_multiple"] < 0, "r_multiple"].sum()))
    return round(gross_win / gross_loss, 2) if gross_loss else 0.0


def write_report(candidate: pd.DataFrame, pair: pd.DataFrame) -> None:
    lines = [
        "# Selected SPX RR Tests",
        "",
        "NQ legs are fixed. SPX legs are tested at 2R, 2.5R, and 3R using the selected latest_entry_time=10:30 rule.",
        "",
        "## Candidate Comparison",
        "",
        base_report.markdown_table(candidate),
        "",
        "## Pair Comparison",
        "",
        base_report.markdown_table(pair),
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
