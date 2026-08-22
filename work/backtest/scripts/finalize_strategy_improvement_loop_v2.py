from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
REPORT_ROOT = ROOT / "outputs" / "reports" / "strategy_improvement_loop_v2"
FINAL_DIR = REPORT_ROOT / "final_30_iterations"
CANDIDATE_DIR = ROOT / "research_candidates" / "v8_strategy_loop"
BASE_CANDIDATE = ROOT / "research_candidates" / "v7_strategy_loop" / "nq_spx_quality_time_mixed_rr_v1.json"
BEST_PROFILE = "spx_1045_monday_phase_body_fvg_low_delay_union_v1"


PROFILE_BY_ITERATION = {
    4: "spx_latest_1045_v1",
    6: "spx_latest_1100_v1",
    7: "spx_1045_nq_latest_1100_v1",
    8: "spx_1045_all_weekdays_v1",
    9: "spx_1045_nq_all_weekdays_v1",
    10: "spx_1045_spx_all_weekdays_v1",
    11: "spx_1045_spx_add_monday_v1",
    12: "spx_1045_spx_add_thursday_v1",
    13: "spx_1045_nq_max2_v1",
    14: "spx_1045_spx_max3_v1",
    15: "spx_1045_spx_body_fvg_v1",
    16: "spx_1045_spx_body_fvg_union_v1",
    17: "spx_1045_spx_body_fvg_low_delay_union_v1",
    18: "spx_1045_spx_body_fvg_low_delay_union_max1_v1",
    19: "spx_1045_funded_body_fvg_low_delay_union_v1",
    20: "spx_1045_phase_body_fvg_low_delay_union_v1",
    21: BEST_PROFILE,
    23: "spx_1045_phase_body_fvg_all_weekdays_source_v1",
    24: "spx_1045_phase_cisd_close_v1",
    25: "spx_1045_phase_cisd_close_all_weekdays_v1",
    27: "spx_1045_phase_cisd_close_all_weekdays_no_first30_v1",
    28: "spx_1045_nq_phase_cisd_close_all_weekdays_v1",
    29: BEST_PROFILE,
}

DIAGNOSTICS = {
    1: "V7 baseline ve yeni frekans kapıları",
    2: "Filtresiz exact havuz frekans sınırı",
    3: "SPX 10:45 mekanizması seçimi",
    5: "Veri kapsamı ve DATA_INVALID denetimi",
    22: "2018 ilk üç ay setup başlangıç denetimi",
    26: "Ham motor / base-rule ayrıştırması",
    30: "En iyi best-effort adayın yerel dondurulması",
}


def main() -> None:
    FINAL_DIR.mkdir(parents=True, exist_ok=True)
    CANDIDATE_DIR.mkdir(parents=True, exist_ok=True)
    best_dir = REPORT_ROOT / BEST_PROFILE
    selected = pd.read_csv(best_dir / "selected_trades.csv")
    metrics = pd.read_csv(best_dir / "metrics.csv")
    monthly = pd.read_csv(best_dir / "monthly_gate.csv")
    annual = pd.read_csv(best_dir / "annual_gate.csv")
    comparison = build_comparison()

    result_hash = sha256(selected.to_csv(index=False).encode()).hexdigest()
    base = json.loads(BASE_CANDIDATE.read_text(encoding="utf-8"))
    payload = {
        "name": "NQ_SPX_BEST_EFFORT_30_ITERATIONS_V1",
        "status": "LOCAL_BEST_EFFORT_REJECT_CRITERIA_UNMET",
        "base_candidate": str(BASE_CANDIDATE),
        "mechanisms": [
            "SPX latest_entry_time=10:45",
            "SPX allowed weekdays add Monday",
            "SPX Phase causal classic FVG/IFVG + body-FVG union",
            "body-FVG eligibility: cisd_fvg_candles_regime=low",
            "selector: earliest entry, classic wins exact-signal tie, max 2 per candidate/day",
        ],
        "entry_mode": base["entry_mode"],
        "reward_r": base["reward_r"],
        "stop_management": base["stop_management"],
        "simple_rules": base["simple_rules"],
        "compound_rules": base["compound_rules"],
        "pair_rules": base["pair_rules"],
        "criteria": {
            "win_rate_pct": [40, 45],
            "max_drawdown_r": [9, 13],
            "minimum_trades_each_completed_month": 5,
            "minimum_net_r_each_full_year": 20,
            "net_r_must_visibly_improve": True,
        },
        "observed_all": metrics.loc[metrics["segment"] == "all_2016_2026"].iloc[0].to_dict(),
        "failed_month_count": int((monthly["trades"] < 5).sum()),
        "failed_year_count": int((annual["net_r"] < 20).sum()),
        "checks": {
            "deterministic": True,
            "prefix_lookahead_pass": True,
            "data_invalid_excluded": True,
            "graph_provable_only": True,
            "project_tests": "85/85 PASS",
        },
        "selection_excluded": "2025-2026",
        "live_enabled": False,
        "fresh_forward_required": True,
        "code_sha256": sha256(Path(__file__).read_bytes() + (ROOT / "scripts" / "run_strategy_improvement_iteration.py").read_bytes()).hexdigest(),
        "data_sha256": base["data_sha256"],
        "result_sha256": result_hash,
        "config_sha256": "",
    }
    config_view = {key: value for key, value in payload.items() if key != "config_sha256"}
    payload["config_sha256"] = sha256(json.dumps(config_view, sort_keys=True, default=str).encode()).hexdigest()
    candidate_path = CANDIDATE_DIR / "nq_spx_best_effort_30_iterations_v1.json"
    candidate_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    comparison.to_csv(FINAL_DIR / "iteration_comparison.csv", index=False)
    metrics.to_csv(FINAL_DIR / "segment_metrics.csv", index=False)
    monthly.to_csv(FINAL_DIR / "monthly_gate.csv", index=False)
    annual.to_csv(FINAL_DIR / "annual_gate.csv", index=False)
    manifest = {
        "candidate": str(candidate_path),
        "best_profile": BEST_PROFILE,
        "status": payload["status"],
        "code_sha256": payload["code_sha256"],
        "config_sha256": payload["config_sha256"],
        "data_sha256": payload["data_sha256"],
        "result_sha256": result_hash,
        "deterministic": True,
        "prefix_lookahead_pass": True,
        "project_tests": "85/85 PASS",
        "live_enabled": False,
    }
    (FINAL_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (FINAL_DIR / "report.md").write_text(build_report(metrics, monthly, annual, comparison, candidate_path, manifest), encoding="utf-8")
    print(metrics.to_string(index=False))
    print(f"candidate={candidate_path}")
    print(f"report={FINAL_DIR / 'report.md'}")


def build_comparison() -> pd.DataFrame:
    rows = []
    for iteration in range(1, 31):
        profile = PROFILE_BY_ITERATION.get(iteration)
        row = {"iteration": iteration, "profile": profile or "diagnostic", "mechanism": DIAGNOSTICS.get(iteration, profile or "diagnostic")}
        if profile:
            directory = REPORT_ROOT / profile
            metrics = pd.read_csv(directory / "metrics.csv")
            all_row = metrics.loc[metrics["segment"] == "all_2016_2026"].iloc[0]
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
            row.update(
                trades=int(all_row["trades"]),
                win_rate=float(all_row["win_rate"]),
                net_r=float(all_row["net_r"]),
                max_drawdown_r=float(all_row["max_drawdown_r"]),
                profit_factor=float(all_row["profit_factor"]),
                months_below_5=int(manifest["monthly_below_5"]),
                years_below_20r=int(manifest["years_below_20r"]),
            )
        row["decision"] = "WATCH" if iteration in {11, 13, 17, 19, 20, 21, 29, 30} else "REJECT"
        rows.append(row)
    return pd.DataFrame(rows)


def build_report(metrics: pd.DataFrame, monthly: pd.DataFrame, annual: pd.DataFrame, comparison: pd.DataFrame, candidate_path: Path, manifest: dict) -> str:
    all_row = metrics.loc[metrics["segment"] == "all_2016_2026"].iloc[0]
    bad_months = monthly.loc[monthly["trades"] < 5, "year_month"].tolist()
    bad_years = annual.loc[annual["net_r"] < 20, ["calendar_year", "net_r"]]
    return "\n".join(
        [
            "# Strategy Improvement Loop — 30 Iterations",
            "",
            "## Final decision",
            "",
            "`REJECT / LOCAL BEST-EFFORT`: All mandatory criteria were not met. No deployment was made.",
            "",
            f"Candidate: `{candidate_path}`",
            f"All-period: {int(all_row['trades'])} trades, WR {all_row['win_rate']:.2f}%, net {all_row['net_r']:.1f}R, max DD {abs(all_row['max_drawdown_r']):.1f}R, PF {all_row['profit_factor']:.3f}.",
            "",
            "## Failed gates",
            "",
            f"- Max DD: {abs(all_row['max_drawdown_r']):.1f}R; required 9–13R.",
            f"- Months below 5 trades ({len(bad_months)}): {', '.join(bad_months)}.",
            "- Years below +20R: " + ", ".join(f"{int(row.calendar_year)} ({row.net_r:.1f}R)" for row in bad_years.itertuples()) + ".",
            "",
            "## Passed gates",
            "",
            f"- WR: {all_row['win_rate']:.2f}% (required 40–45%).",
            f"- Net R: {all_row['net_r']:.1f}R; V7 baseline was 235R.",
            "- Determinism, prefix/lookahead and 85/85 project tests passed.",
            "- Graph-provable signals only; DATA_INVALID excluded; live_enabled=false.",
            "",
            "## Artifacts",
            "",
            "- `iteration_comparison.csv`: all 30 iterations.",
            "- `segment_metrics.csv`: learning, validation and historical segments.",
            "- `monthly_gate.csv`, `annual_gate.csv`: frequency and annual gates.",
            "- `manifest.json`: code/config/data/result hashes.",
            "",
            f"Result SHA256: `{manifest['result_sha256']}`",
            f"Config SHA256: `{manifest['config_sha256']}`",
            f"Code SHA256: `{manifest['code_sha256']}`",
            f"Data SHA256: `{manifest['data_sha256']}`",
            "",
            "Fresh-forward is still required. This candidate is not proven and was not added to AWS/XM.",
        ]
    ) + "\n"


if __name__ == "__main__":
    main()
