from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
import time

import pandas as pd
from backtest.market_calendar import signed_market_dates


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import run_canonical_production_full_history as full_history
import run_engine_path_comparison_2025_feb_mar as comparison
import run_main_candidate_filter_tests as filters
from backtest.engine_pipeline import EngineLeg, run_canonical_pair_pipeline, stable_frame_hash
from backtest.manual_state import ManualStateConfig
from backtest.risk import apply_pair_risk_rule
from frozen_first30_thresholds import load_thresholds
from machine_policy_v1 import apply_exact_pair_cap


BASELINE_DIR = ROOT / "outputs" / "reports" / "canonical_production_full_history_research"
ANCHORED_DIR = ROOT / "outputs" / "reports" / "anchored_threshold_holdout_research"
REPORT_DIR = ROOT / "outputs" / "reports" / "machine_metric_mechanics_research"
DEVELOPMENT_START = pd.Timestamp("2022-01-03")
DEVELOPMENT_END = pd.Timestamp("2024-12-31")
HOLDOUT_START = pd.Timestamp("2025-01-01")
FULL_END = pd.Timestamp("2026-07-02")
DEVELOPMENT_FOLDS = {
    "train_2022_validate_2023": (
        pd.Timestamp("2022-01-01"),
        pd.Timestamp("2023-01-01"),
        pd.Timestamp("2024-01-01"),
    ),
    "train_2022_2023_validate_2024": (
        pd.Timestamp("2022-01-01"),
        pd.Timestamp("2024-01-01"),
        HOLDOUT_START,
    ),
}


POLICIES: dict[str, dict[str, object]] = {
    "baseline": {"decision_source": "baseline", "blocked": set()},
    "spx_anchored_q60": {"decision_source": "spx_anchored", "blocked": set()},
    "block_spx_first_structure": {
        "decision_source": "baseline",
        "blocked": {("spx", "FIRST_QUALIFIED_STRUCTURE")},
    },
    "block_spx_selected_liquidity": {
        "decision_source": "baseline",
        "blocked": {("spx", "SELECTED_LIQUIDITY_SWEEP")},
    },
    "block_spx_first_and_selected": {
        "decision_source": "baseline",
        "blocked": {
            ("spx", "FIRST_QUALIFIED_STRUCTURE"),
            ("spx", "SELECTED_LIQUIDITY_SWEEP"),
        },
    },
    "spx_anchored_block_first_structure": {
        "decision_source": "spx_anchored",
        "blocked": {("spx", "FIRST_QUALIFIED_STRUCTURE")},
    },
    "spx_anchored_block_selected_liquidity": {
        "decision_source": "spx_anchored",
        "blocked": {("spx", "SELECTED_LIQUIDITY_SWEEP")},
    },
    "spx_anchored_block_first_and_selected": {
        "decision_source": "spx_anchored",
        "blocked": {
            ("spx", "FIRST_QUALIFIED_STRUCTURE"),
            ("spx", "SELECTED_LIQUIDITY_SWEEP"),
        },
    },
    "risk_profile_block_nq_premarket": {
        "decision_source": "baseline",
        "blocked": {("nq", "PREMARKET_CONTEXT")},
    },
    "risk_profile_spx_anchored_block_nq_premarket": {
        "decision_source": "spx_anchored",
        "blocked": {("nq", "PREMARKET_CONTEXT")},
    },
}


def frozen_spx_threshold(*, verify_sources: bool = True) -> float:
    rows = load_thresholds(verify_sources=verify_sources)
    lookup = {(row["symbol"], row["timeframe"]): row for row in rows}
    return float(lookup[comparison.SYMBOLS["spx"]]["first30_q60"])


def run_spx_anchored_development() -> pd.DataFrame:
    path = REPORT_DIR / "spx_anchored_development_decisions.csv"
    if path.exists():
        return pd.read_csv(path)
    loaded = filters.load_data()
    configs = comparison.build_active_configs(loaded)
    spx_threshold = frozen_spx_threshold()
    configs["spx"] = replace(
        configs["spx"],
        first30_range_filter="live_safe_max",
        first30_range_max=spx_threshold,
    )
    legs = [
        EngineLeg(
            key,
            full_history.period_frame(loaded[source], DEVELOPMENT_START, DEVELOPMENT_END),
            configs[key],
        )
        for key, source in comparison.SYMBOLS.items()
    ]
    dates = signed_market_dates(DEVELOPMENT_START, DEVELOPMENT_END)
    started = time.perf_counter()
    result = run_canonical_pair_pipeline(legs, dates, state_config=ManualStateConfig())
    result.decisions.to_csv(path, index=False)
    (REPORT_DIR / "spx_anchored_development_manifest.json").write_text(
        json.dumps(
            {
                "start": str(DEVELOPMENT_START.date()),
                "end": str(DEVELOPMENT_END.date()),
                "spx_threshold": spx_threshold,
                "result_hash": stable_frame_hash(result.decisions),
                "runtime_seconds": round(time.perf_counter() - started, 3),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"SPX anchored development: {len(result.decisions)} decisions", flush=True)
    return result.decisions


def anchored_decisions(baseline: pd.DataFrame, development: pd.DataFrame) -> pd.DataFrame:
    holdout = pd.read_csv(ANCHORED_DIR / "spx_pre2025_q60_decisions.csv")
    spx = pd.concat(
        [
            development[development["leg_key"] == "spx"],
            holdout[holdout["leg_key"] == "spx"],
        ],
        ignore_index=True,
    )
    nq = baseline[baseline["leg_key"] == "nq"]
    return pd.concat([nq, spx], ignore_index=True).sort_values(
        ["date", "event_known_time", "leg_key", "order_id"], kind="mergesort"
    ).reset_index(drop=True)


def apply_machine_policy(
    decisions: pd.DataFrame,
    blocked: set[tuple[str, str]],
) -> pd.DataFrame:
    filled = decisions[decisions["order_state"] == "FILLED"].copy()
    for leg_key, context_source in blocked:
        filled = filled[
            ~(
                filled["leg_key"].astype(str).eq(leg_key)
                & filled["context_source"].astype(str).eq(context_source)
            )
        ].copy()
    filled["group"] = "NQ_SPX_PAIR"
    filled["label"] = filled["leg_key"]
    return apply_exact_pair_cap(filled, -1.0)


def metric_row(variant: str, segment: str, filled: pd.DataFrame) -> dict[str, object]:
    wins = int(filled["outcome"].eq("TP").sum()) if not filled.empty else 0
    r = pd.to_numeric(filled.get("r_multiple", pd.Series(dtype=float)), errors="coerce").fillna(0.0)
    equity = r.cumsum()
    drawdown = equity - equity.cummax().clip(lower=0.0)
    gross_win = float(r[r > 0].sum())
    gross_loss = abs(float(r[r < 0].sum()))
    return {
        "variant": variant,
        "segment": segment,
        "trades": len(filled),
        "wins": wins,
        "win_rate_pct": round(100 * wins / len(filled), 2) if len(filled) else 0.0,
        "net_r": round(float(r.sum()), 3),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
        "max_drawdown_r": round(float(drawdown.min()), 3) if not drawdown.empty else 0.0,
    }


def development_segments(filled: pd.DataFrame) -> dict[str, pd.DataFrame]:
    dates = pd.to_datetime(filled["date"])
    segments = {"development_pre_holdout": filled[dates < HOLDOUT_START]}
    for name, (start, validation_start, end) in DEVELOPMENT_FOLDS.items():
        segments[f"{name}__train"] = filled[(dates >= start) & (dates < validation_start)]
        segments[f"{name}__validation"] = filled[
            (dates >= validation_start) & (dates < end)
        ]
    return segments


def build_development_ranking(metrics: pd.DataFrame) -> pd.DataFrame:
    indexed = metrics.set_index(["variant", "segment"])
    baseline_development = indexed.loc[("baseline", "development_pre_holdout"), :]
    rows = []
    validation_segments = [f"{name}__validation" for name in DEVELOPMENT_FOLDS]
    train_segments = [f"{name}__train" for name in DEVELOPMENT_FOLDS]
    available_variants = set(metrics["variant"].astype(str))
    for variant, policy in POLICIES.items():
        if policy["decision_source"] != "baseline" or variant not in available_variants:
            continue
        development = indexed.loc[(variant, "development_pre_holdout"), :]
        fold_scores = []
        validation_gates: list[bool] = []
        retention_gates: list[bool] = []
        train_gates: list[bool] = []
        for train_segment, validation_segment in zip(train_segments, validation_segments):
            train = indexed.loc[(variant, train_segment), :]
            baseline_train = indexed.loc[("baseline", train_segment), :]
            baseline_validation = indexed.loc[("baseline", validation_segment), :]
            validation = indexed.loc[(variant, validation_segment), :]
            train_gates.append(bool(train["net_r"] >= baseline_train["net_r"]))
            validation_gates.append(
                bool(
                    validation["net_r"] >= baseline_validation["net_r"]
                    and validation["win_rate_pct"] >= baseline_validation["win_rate_pct"]
                    and validation["max_drawdown_r"] >= baseline_validation["max_drawdown_r"]
                )
            )
            retention_gates.append(
                bool(validation["trades"] >= baseline_validation["trades"] * 0.80)
            )
            fold_scores.append(
                float(validation["net_r"] - baseline_validation["net_r"])
                + 0.5 * float(validation["win_rate_pct"] - baseline_validation["win_rate_pct"])
                + 0.5
                * float(validation["max_drawdown_r"] - baseline_validation["max_drawdown_r"])
                - 0.1 * max(0.0, float(baseline_validation["trades"] - validation["trades"]))
            )
        gates = {
            "development_positive": bool(development["net_r"] > 0),
            "development_net_nonworse": bool(
                development["net_r"] >= baseline_development["net_r"]
            ),
            "all_fold_train_nonworse": all(train_gates),
            "all_fold_validation_nonworse": all(validation_gates),
            "all_fold_trade_retention": all(retention_gates),
            "holdout_used_for_selection": False,
        }
        rows.append(
            {
                "variant": variant,
                "nested_development_score": round(sum(fold_scores) / len(fold_scores), 3),
                **gates,
                "all_gates_pass": all(
                    value for key, value in gates.items() if key != "holdout_used_for_selection"
                ),
            }
        )
    return pd.DataFrame(rows).sort_values(
        ["all_gates_pass", "nested_development_score", "variant"],
        ascending=[False, False, True],
    )


def final_holdout_evaluation(
    policy_fills: dict[str, pd.DataFrame], selected: str
) -> tuple[pd.DataFrame, dict[str, bool]]:
    variants = ["baseline"] if selected == "NONE" else ["baseline", selected]
    rows = []
    for variant in dict.fromkeys(variants):
        filled = policy_fills[variant]
        dates = pd.to_datetime(filled["date"])
        rows.append(
            metric_row(
                variant,
                "final_holdout_2025_plus",
                filled[dates >= HOLDOUT_START],
            )
        )
    frame = pd.DataFrame(rows)
    if selected == "NONE":
        return frame, {"candidate_selected": False}
    indexed = frame.set_index("variant")
    baseline = indexed.loc["baseline"]
    candidate = indexed.loc[selected]
    gates = {
        "candidate_selected": True,
        "holdout_net_nonworse": bool(candidate["net_r"] >= baseline["net_r"]),
        "holdout_dd_nonworse": bool(
            candidate["max_drawdown_r"] >= baseline["max_drawdown_r"]
        ),
        "holdout_trade_retention": bool(candidate["trades"] >= baseline["trades"] * 0.80),
    }
    return frame, gates


def main() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    baseline = pd.read_csv(BASELINE_DIR / "canonical_decisions.csv")
    # Anchored variants remain available as diagnostics, but their threshold was fitted
    # on all pre-2025 data. Candidate selection therefore uses baseline-source policies
    # only and scores them on rolling-origin development folds.
    sources = {"baseline": baseline}
    metric_rows = []
    yearly_rows = []
    policy_fills: dict[str, pd.DataFrame] = {}
    for variant, policy in POLICIES.items():
        if policy["decision_source"] != "baseline":
            continue
        filled = apply_machine_policy(
            sources[str(policy["decision_source"])],
            set(policy["blocked"]),
        )
        policy_fills[variant] = filled
        dates = pd.to_datetime(filled["date"])
        segments = development_segments(filled)
        metric_rows.extend(metric_row(variant, segment, sample) for segment, sample in segments.items())
        for year, sample in filled.assign(year=dates.dt.year).groupby("year"):
            yearly_rows.append({"year": int(year), **metric_row(variant, "year", sample)})
    metrics = pd.DataFrame(metric_rows)
    yearly = pd.DataFrame(yearly_rows)
    ranking_frame = build_development_ranking(metrics)
    eligible = ranking_frame[
        ranking_frame["all_gates_pass"] & ranking_frame["variant"].ne("baseline")
    ]
    selected = str(eligible.iloc[0]["variant"]) if not eligible.empty else "NONE"
    holdout_frame, holdout_gates = final_holdout_evaluation(policy_fills, selected)
    metrics.to_csv(REPORT_DIR / "metrics_by_segment.csv", index=False)
    yearly.to_csv(REPORT_DIR / "metrics_by_year.csv", index=False)
    ranking_frame.to_csv(REPORT_DIR / "balanced_ranking.csv", index=False)
    holdout_frame.to_csv(REPORT_DIR / "final_holdout_evaluation.csv", index=False)
    if selected != "NONE":
        policy_fills[selected].to_csv(REPORT_DIR / "selected_candidate_fills.csv", index=False)
    selected_payload = {
        "selected_candidate": selected,
        "policy": None if selected == "NONE" else {
            "decision_source": POLICIES[selected]["decision_source"],
            "blocked": sorted([list(item) for item in POLICIES[selected]["blocked"]]),
            "spx_first30_range_max": (
                frozen_spx_threshold(verify_sources=False)
                if POLICIES[selected]["decision_source"] == "spx_anchored"
                else "UNCHANGED"
            ),
        },
        "selection_protocol": {
            "development_end_exclusive": str(HOLDOUT_START.date()),
            "folds": {
                name: {
                    "train_start": str(start.date()),
                    "validation_start": str(validation_start.date()),
                    "validation_end_exclusive": str(end.date()),
                }
                for name, (start, validation_start, end) in DEVELOPMENT_FOLDS.items()
            },
            "holdout_used_for_selection": False,
        },
        "final_holdout_evaluation": holdout_gates,
        "live_promoted": False,
    }
    (REPORT_DIR / "selected_candidate.json").write_text(
        json.dumps(selected_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    lines = [
        "# Machine metric mechanics research",
        "",
        "Human/manual similarity is not used. Ranking uses rolling-origin pre-2025 development folds only.",
        "The 2025+ final holdout is evaluated once after selection and is absent from the ranking score and gates.",
        "",
        f"Selected local candidate: `{selected}`. Live promoted: `False`.",
        "",
        "```text",
        ranking_frame.to_string(index=False),
        "```",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(ranking_frame.to_string(index=False), flush=True)
    print(json.dumps(selected_payload, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
