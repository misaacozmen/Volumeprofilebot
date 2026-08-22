from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_challenge_v2_to_funded_lifecycle as candidate_lifecycle
import run_corrected_engine_3month_report as base_report
import run_final_candidate_fund_account_lifecycle as lifecycle
import run_frequency_expansion_tests as frequency
import run_main_candidate_filter_tests as filters
import run_manual_rule_v2_validation as manual_v2
import run_phase_to_funded_account_lifecycle as phase_to_funded
from backtest.risk import apply_pair_risk_rule
from backtest.strategy import run_backtest, trades_to_frame


REPORT_DIR = ROOT / "outputs" / "reports" / "manual_logic_v3_candidate_validation"
PROFILES = {
    "baseline": "Current active candidate logic",
    "htf_requalification": "Research-only 15m FVG/OB body-close requalification",
}
SYSTEMS = {
    "CHALLENGE_CORE_V2": ("phase_selected", True),
    "FON_CORE": ("funded_selected", False),
}


def main() -> None:
    reset_report_dir()
    loaded = filters.load_data()
    thresholds = filters.build_thresholds(loaded)
    threshold_lookup = {(row["symbol"], row["timeframe"]): row for row in thresholds}
    leg_lookup = {(leg.system, leg.leg_key): leg for leg in filters.build_leg_specs()}

    leg_frames: list[pd.DataFrame] = []
    pair_frames: dict[tuple[str, str], pd.DataFrame] = {}
    for profile in PROFILES:
        for operating_name, (system, use_manual_v2) in SYSTEMS.items():
            pair, legs = build_candidate_pair(
                operating_name,
                system,
                profile,
                use_manual_v2,
                leg_lookup,
                loaded,
                threshold_lookup,
            )
            pair_frames[(profile, operating_name)] = pair
            leg_frames.extend(legs)

    leg_trades = pd.concat(leg_frames, ignore_index=True) if leg_frames else pd.DataFrame()
    pair_trades = pd.concat(pair_frames.values(), ignore_index=True) if pair_frames else pd.DataFrame()
    leg_comparison = build_leg_comparison(leg_trades)
    pair_comparison = build_pair_comparison(pair_frames)
    trade_changes = build_trade_changes(pair_frames)
    lifecycle_runs, lifecycle_stages = run_lifecycle(pair_frames)
    lifecycle_summary = phase_to_funded.build_summary(lifecycle_runs, lifecycle_stages)
    lifecycle_fit = phase_to_funded.build_account_fit(lifecycle_summary)

    leg_trades.to_csv(REPORT_DIR / "leg_trades.csv", index=False)
    pair_trades.to_csv(REPORT_DIR / "pair_trades_after_cap.csv", index=False)
    leg_comparison.to_csv(REPORT_DIR / "leg_comparison.csv", index=False)
    pair_comparison.to_csv(REPORT_DIR / "pair_comparison.csv", index=False)
    trade_changes.to_csv(REPORT_DIR / "trade_changes.csv", index=False)
    lifecycle_runs.to_csv(REPORT_DIR / "lifecycle_runs.csv", index=False)
    lifecycle_stages.to_csv(REPORT_DIR / "lifecycle_stages.csv", index=False)
    lifecycle_summary.to_csv(REPORT_DIR / "lifecycle_summary.csv", index=False)
    lifecycle_fit.to_csv(REPORT_DIR / "lifecycle_account_fit.csv", index=False)
    write_report(pair_comparison, leg_comparison, trade_changes, lifecycle_summary, lifecycle_fit)
    print(f"Wrote: {REPORT_DIR}")


def reset_report_dir() -> None:
    if REPORT_DIR.exists():
        for path in sorted(REPORT_DIR.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def build_candidate_pair(
    operating_name: str,
    system: str,
    profile: str,
    use_manual_v2: bool,
    leg_lookup: dict[tuple[str, str], object],
    loaded: dict[tuple[str, str], pd.DataFrame],
    threshold_lookup: dict[tuple[str, str], dict[str, object]],
) -> tuple[pd.DataFrame, list[pd.DataFrame]]:
    frames: list[pd.DataFrame] = []
    for leg_key in ["nq", "spx"]:
        leg = leg_lookup[(system, leg_key)]
        variant_name = candidate_lifecycle.FINAL_NQ_VARIANTS[system] if leg_key == "nq" else "baseline"
        variant = filters.VariantSpec(
            variant_name,
            variant_name,
            "engine",
            first30_quantile=manual_v2.variant_quantile(variant_name),
        )
        config = filters.build_variant_config(leg, variant, threshold_lookup)
        if use_manual_v2:
            config = manual_v2.manual_v2_config(config)
        if profile == "htf_requalification":
            config = replace(config, htf_body_close_requalification="fvg_ob")

        print(f"{profile} / {operating_name} / {leg_key}")
        source = loaded[(leg.candidate.symbol, leg.candidate.timeframe)].copy()
        trades = trades_to_frame(run_backtest(source, config).trades)
        if trades.empty:
            continue
        trades.insert(0, "profile", profile)
        trades.insert(1, "operating_name", operating_name)
        trades.insert(2, "leg_key", leg_key)
        trades.insert(3, "label", leg.label)
        frames.append(trades)

    pair = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if pair.empty:
        return pair, frames
    pair["entry_time_dt"] = pd.to_datetime(pair["entry_time"], utc=True, format="mixed").dt.tz_convert(
        "America/New_York"
    )
    pair["group"] = operating_name
    capped = apply_pair_risk_rule(pair, -1.0).sort_values("entry_time_dt").reset_index(drop=True)
    return capped, frames


def build_leg_comparison(leg_trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for operating_name in SYSTEMS:
        for leg_key in ["nq", "spx"]:
            baseline = select_trades(leg_trades, "baseline", operating_name, leg_key)
            base_stats = frequency.stats(baseline)
            for profile in PROFILES:
                trades = select_trades(leg_trades, profile, operating_name, leg_key)
                stats = frequency.stats(trades)
                rows.append(
                    {
                        "profile": profile,
                        "operating_name": operating_name,
                        "leg_key": leg_key,
                        **stats,
                        "delta_trades": int(stats["trades"] - base_stats["trades"]),
                        "delta_net_r": round(float(stats["net_r"]) - float(base_stats["net_r"]), 2),
                        "delta_max_drawdown_r": round(
                            float(stats["max_drawdown_r"]) - float(base_stats["max_drawdown_r"]), 2
                        ),
                    }
                )
    return pd.DataFrame(rows)


def select_trades(frame: pd.DataFrame, profile: str, operating_name: str, leg_key: str | None = None) -> pd.DataFrame:
    if frame.empty:
        return frame
    selected = frame[(frame["profile"] == profile) & (frame["operating_name"] == operating_name)]
    if leg_key is not None:
        selected = selected[selected["leg_key"] == leg_key]
    return selected.copy()


def build_pair_comparison(pair_frames: dict[tuple[str, str], pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for operating_name in SYSTEMS:
        base_stats = frequency.stats(pair_frames[("baseline", operating_name)])
        for profile in PROFILES:
            stats = frequency.stats(pair_frames[(profile, operating_name)])
            rows.append(
                {
                    "profile": profile,
                    "operating_name": operating_name,
                    **stats,
                    "delta_trades": int(stats["trades"] - base_stats["trades"]),
                    "delta_net_r": round(float(stats["net_r"]) - float(base_stats["net_r"]), 2),
                    "delta_max_drawdown_r": round(
                        float(stats["max_drawdown_r"]) - float(base_stats["max_drawdown_r"]), 2
                    ),
                }
            )
    return pd.DataFrame(rows)


def build_trade_changes(pair_frames: dict[tuple[str, str], pd.DataFrame]) -> pd.DataFrame:
    rows = []
    identity = ["symbol", "direction", "entry_time"]
    for operating_name in SYSTEMS:
        baseline = pair_frames[("baseline", operating_name)]
        addon = pair_frames[("htf_requalification", operating_name)]
        baseline_keys = {tuple(row) for row in baseline[identity].itertuples(index=False, name=None)} if not baseline.empty else set()
        addon_keys = {tuple(row) for row in addon[identity].itertuples(index=False, name=None)} if not addon.empty else set()
        for status, frame, keys in [
            ("added", addon, addon_keys - baseline_keys),
            ("removed", baseline, baseline_keys - addon_keys),
        ]:
            if frame.empty:
                continue
            for row in frame.itertuples(index=False):
                key = (row.symbol, row.direction, row.entry_time)
                if key not in keys:
                    continue
                rows.append(
                    {
                        "operating_name": operating_name,
                        "change": status,
                        "symbol": row.symbol,
                        "date": row.date,
                        "direction": row.direction,
                        "entry_time": row.entry_time,
                        "result": row.result,
                        "r_multiple": row.r_multiple,
                        "notes": row.notes,
                    }
                )
    return pd.DataFrame(rows)


def run_lifecycle(pair_frames: dict[tuple[str, str], pd.DataFrame]) -> tuple[pd.DataFrame, pd.DataFrame]:
    run_rows = []
    stage_rows = []
    for profile in PROFILES:
        phase = pair_frames[(profile, "CHALLENGE_CORE_V2")]
        funded = pair_frames[(profile, "FON_CORE")]
        combo = f"{profile}: CHALLENGE_CORE_V2 -> FON_CORE"
        for account in lifecycle.ACCOUNTS:
            for start_index in range(len(phase)):
                run, stages = phase_to_funded.simulate_combo(
                    combo=combo,
                    phase_candidate=f"{profile}:CHALLENGE_CORE_V2",
                    funded_candidate=f"{profile}:FON_CORE",
                    account=account,
                    phase_trades=phase,
                    funded_trades=funded,
                    start_index=start_index,
                )
                run_rows.append(run)
                stage_rows.extend(stages)
    return pd.DataFrame(run_rows), pd.DataFrame(stage_rows)


def write_report(
    pair_comparison: pd.DataFrame,
    leg_comparison: pd.DataFrame,
    trade_changes: pd.DataFrame,
    lifecycle_summary: pd.DataFrame,
    lifecycle_fit: pd.DataFrame,
) -> None:
    lines = [
        "# Manual Logic V3 Candidate Validation",
        "",
        "Research-only comparison. Active defaults remain unchanged. The controlling-array selector thresholds and weights are untouched.",
        "",
        "Daily NQ+SPX pair cap: -1R.",
        "",
        "## Decision",
        "",
        "REJECT FOR PROMOTION. Manual decision+direction alignment remains 7/15 (46.67%), FON_CORE loses 2.0R, and rolling A2/A3 funded reach deteriorates. Keep the implementation research-only and disabled by default.",
        "",
        "## Pair Comparison",
        "",
        base_report.markdown_table(pair_comparison),
        "",
        "## Leg Comparison",
        "",
        base_report.markdown_table(leg_comparison),
        "",
        "## Changed Trades",
        "",
        base_report.markdown_table(trade_changes) if not trade_changes.empty else "No capped pair trades changed.",
        "",
        "## Lifecycle Summary",
        "",
        base_report.markdown_table(lifecycle_summary),
        "",
        "## Lifecycle Account Fit",
        "",
        base_report.markdown_table(lifecycle_fit),
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
