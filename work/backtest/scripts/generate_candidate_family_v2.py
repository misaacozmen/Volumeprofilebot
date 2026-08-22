from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from backtest.engine_pipeline import stable_frame_hash
from machine_policy_v1 import causal_fills_after_pair_cap, causal_policy_violations
import run_machine_metric_mechanics as mechanics


BASELINE = ROOT / "outputs" / "reports" / "canonical_production_full_history_research" / "canonical_decisions.csv"
CONFIG_DIR = ROOT / "research_candidates" / "v2_family"
REPORT_DIR = ROOT / "outputs" / "reports" / "machine_candidate_family_v2"


def pre_cap_r(frame: pd.DataFrame) -> pd.Series:
    exact = pd.to_numeric(frame["r_multiple"], errors="coerce")
    closed = frame["outcome"].isin(["TP", "SL", "BE"])
    if exact[closed].isna().any():
        raise ValueError("Closed candidate decisions must carry exact R multiples.")
    return exact.where(closed)


def learned_negative_buckets(
    development: pd.DataFrame,
    feature: str,
    minimum_count: int,
) -> dict[str, list[str]]:
    stats = (
        development.groupby(["leg_key", feature], dropna=False)
        .agg(count=("r_multiple", "size"), net_r=("r_multiple", "sum"))
        .reset_index()
    )
    blocked = stats[(stats["count"] >= minimum_count) & (stats["net_r"] < 0)]
    return {
        leg_key: sorted(group[feature].astype(str).tolist())
        for leg_key, group in blocked.groupby("leg_key")
    }


def config(name: str, contexts=None, weekdays=None, directions=None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "name": name,
        "selection_basis": "PRE_2025_MACHINE_METRICS_ONLY",
        "blocked_context_sources": {"nq": [], "spx": [], **(contexts or {})},
        "blocked_weekdays": {"nq": [], "spx": [], **(weekdays or {})},
        "blocked_directions": {"nq": [], "spx": [], **(directions or {})},
        "pair_cap_r": -1.0,
        "strategy_thresholds": "UNCHANGED",
        "timeframes": {"nq": "3m", "spx": "5m", "htf": "15m"},
        "timezone": "America/New_York",
        "live_enabled": False,
    }


def build_family(
    decisions: pd.DataFrame,
    *,
    training_end_exclusive: pd.Timestamp = mechanics.HOLDOUT_START,
) -> tuple[dict[str, dict[str, Any]], pd.DataFrame]:
    development = decisions[
        decisions["order_state"].eq("FILLED")
        & (pd.to_datetime(decisions["date"]) < training_end_exclusive)
    ].copy()
    development["r_multiple"] = pre_cap_r(development)
    development["weekday"] = pd.to_datetime(development["date"]).dt.day_name()
    negative_contexts = learned_negative_buckets(development, "context_source", 8)
    negative_weekdays = learned_negative_buckets(development, "weekday", 12)
    negative_directions = learned_negative_buckets(development, "direction", 20)
    rows = []
    for feature, mapping in (
        ("context_source", negative_contexts),
        ("weekday", negative_weekdays),
        ("direction", negative_directions),
    ):
        for leg_key, values in mapping.items():
            for value in values:
                sample = development[
                    development["leg_key"].eq(leg_key)
                    & development[feature].astype(str).eq(value)
                ]
                rows.append(
                    {
                        "feature": feature,
                        "leg_key": leg_key,
                        "value": value,
                        "development_trades": len(sample),
                        "development_net_r": float(sample["r_multiple"].sum()),
                    }
                )
    family = {
        "V2_BASELINE": config("V2_BASELINE"),
        "V2_CONTEXT_EDGE": config("V2_CONTEXT_EDGE", contexts=negative_contexts),
        "V2_WEEKDAY_EDGE": config("V2_WEEKDAY_EDGE", weekdays=negative_weekdays),
        "V2_DIRECTION_EDGE": config("V2_DIRECTION_EDGE", directions=negative_directions),
        "V2_CONTEXT_WEEKDAY": config(
            "V2_CONTEXT_WEEKDAY", contexts=negative_contexts, weekdays=negative_weekdays
        ),
        "V2_SPX_QUALITY": config(
            "V2_SPX_QUALITY",
            contexts={"spx": ["FIRST_QUALIFIED_STRUCTURE", "SELECTED_LIQUIDITY_SWEEP"]},
        ),
        "V2_SPX_TUESDAY_GUARD": config(
            "V2_SPX_TUESDAY_GUARD", weekdays={"spx": ["Tuesday"]}
        ),
        "V2_SPX_QUALITY_TUESDAY": config(
            "V2_SPX_QUALITY_TUESDAY",
            contexts={"spx": ["FIRST_QUALIFIED_STRUCTURE", "SELECTED_LIQUIDITY_SWEEP"]},
            weekdays={"spx": ["Tuesday"]},
        ),
    }
    return family, pd.DataFrame(rows)


def nested_development_metrics(
    decisions: pd.DataFrame,
    candidate_names: list[str],
) -> pd.DataFrame:
    dates = pd.to_datetime(decisions["date"])
    rows = []
    for fold_name, (start, validation_start, end) in mechanics.DEVELOPMENT_FOLDS.items():
        fold_family, _ = build_family(decisions, training_end_exclusive=validation_start)
        train_decisions = decisions[(dates >= start) & (dates < validation_start)]
        validation_decisions = decisions[(dates >= validation_start) & (dates < end)]
        for name in candidate_names:
            train_fills = causal_fills_after_pair_cap(train_decisions, fold_family[name])
            validation_fills = causal_fills_after_pair_cap(
                validation_decisions, fold_family[name]
            )
            rows.append(mechanics.metric_row(name, f"{fold_name}__train", train_fills))
            rows.append(
                mechanics.metric_row(name, f"{fold_name}__validation", validation_fills)
            )
    return pd.DataFrame(rows)


def build_development_ranking(metrics: pd.DataFrame) -> pd.DataFrame:
    indexed = metrics.set_index(["variant", "segment"])
    names = list(dict.fromkeys(metrics["variant"].astype(str)))
    validation_segments = [f"{name}__validation" for name in mechanics.DEVELOPMENT_FOLDS]
    train_segments = [f"{name}__train" for name in mechanics.DEVELOPMENT_FOLDS]
    baseline_development = indexed.loc[("V2_BASELINE", "development_pre_holdout"), :]
    rows = []
    for name in names:
        development = indexed.loc[(name, "development_pre_holdout"), :]
        fold_scores: list[float] = []
        train_nonworse: list[bool] = []
        validation_nonworse: list[bool] = []
        retention: list[bool] = []
        for train_segment, validation_segment in zip(train_segments, validation_segments):
            train = indexed.loc[(name, train_segment), :]
            baseline_train = indexed.loc[("V2_BASELINE", train_segment), :]
            baseline_validation = indexed.loc[("V2_BASELINE", validation_segment), :]
            validation = indexed.loc[(name, validation_segment), :]
            train_nonworse.append(bool(train["net_r"] >= baseline_train["net_r"]))
            validation_nonworse.append(
                bool(
                    validation["net_r"] >= baseline_validation["net_r"]
                    and validation["win_rate_pct"] >= baseline_validation["win_rate_pct"]
                    and validation["max_drawdown_r"] >= baseline_validation["max_drawdown_r"]
                )
            )
            retention.append(
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
            "all_fold_train_nonworse": all(train_nonworse),
            "all_fold_validation_nonworse": all(validation_nonworse),
            "all_fold_trade_retention": all(retention),
            "holdout_used_for_selection": False,
        }
        rows.append(
            {
                "candidate": name,
                "nested_development_score": round(sum(fold_scores) / len(fold_scores), 3),
                **gates,
                "all_gates_pass": all(
                    value for key, value in gates.items() if key != "holdout_used_for_selection"
                ),
            }
        )
    return pd.DataFrame(rows).sort_values(
        ["all_gates_pass", "nested_development_score", "candidate"],
        ascending=[False, False, True],
    )


def evaluate_final_holdout(
    decisions: pd.DataFrame,
    family: dict[str, dict[str, Any]],
    selected: str,
) -> tuple[pd.DataFrame, dict[str, bool]]:
    dates = pd.to_datetime(decisions["date"])
    holdout = decisions[dates >= mechanics.HOLDOUT_START]
    names = ["V2_BASELINE"] if selected == "NONE" else ["V2_BASELINE", selected]
    rows = []
    for name in dict.fromkeys(names):
        fills = causal_fills_after_pair_cap(holdout, family[name])
        rows.append(mechanics.metric_row(name, "final_holdout_2025_plus", fills))
    frame = pd.DataFrame(rows)
    if selected == "NONE":
        return frame, {"candidate_selected": False}
    indexed = frame.set_index("variant")
    baseline = indexed.loc["V2_BASELINE"]
    candidate = indexed.loc[selected]
    return frame, {
        "candidate_selected": True,
        "holdout_net_nonworse": bool(candidate["net_r"] >= baseline["net_r"]),
        "holdout_dd_nonworse": bool(
            candidate["max_drawdown_r"] >= baseline["max_drawdown_r"]
        ),
        "holdout_trade_retention": bool(candidate["trades"] >= baseline["trades"] * 0.80),
    }


def main() -> None:
    decisions = pd.read_csv(BASELINE)
    family, learned = build_family(decisions)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    metric_rows = []
    hashes = []
    development_fills_by_candidate = {}
    decision_dates = pd.to_datetime(decisions["date"])
    development_decisions = decisions[decision_dates < mechanics.HOLDOUT_START]
    for name, candidate_config in family.items():
        config_path = CONFIG_DIR / f"{name.lower()}.json"
        config_path.write_text(
            json.dumps(candidate_config, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        fills = causal_fills_after_pair_cap(development_decisions, candidate_config)
        development_fills_by_candidate[name] = fills
        violations = causal_policy_violations(development_decisions, candidate_config)
        metric_rows.append(mechanics.metric_row(name, "development_pre_holdout", fills))
        hashes.append(
            {
                "candidate": name,
                "config_sha256": sha256(config_path.read_bytes()).hexdigest(),
                "development_result_hash": stable_frame_hash(fills),
                "causal_violations": len(violations),
            }
        )
    metrics = pd.concat(
        [pd.DataFrame(metric_rows), nested_development_metrics(decisions, list(family))],
        ignore_index=True,
    )
    ranking_frame = build_development_ranking(metrics)
    eligible = ranking_frame[
        ranking_frame["all_gates_pass"] & ranking_frame["candidate"].ne("V2_BASELINE")
    ]
    selected = str(eligible.iloc[0]["candidate"]) if not eligible.empty else "NONE"
    holdout_frame, holdout_gates = evaluate_final_holdout(decisions, family, selected)
    learned.to_csv(REPORT_DIR / "learned_development_rules.csv", index=False)
    metrics.to_csv(REPORT_DIR / "metrics_by_segment.csv", index=False)
    pd.DataFrame(hashes).to_csv(REPORT_DIR / "candidate_hashes.csv", index=False)
    ranking_frame.to_csv(REPORT_DIR / "balanced_ranking.csv", index=False)
    holdout_frame.to_csv(REPORT_DIR / "final_holdout_evaluation.csv", index=False)
    if selected != "NONE":
        development_fills_by_candidate[selected].to_csv(
            REPORT_DIR / "selected_candidate_development_fills.csv", index=False
        )
    (REPORT_DIR / "selection.json").write_text(
        json.dumps(
            {
                "selected_candidate": selected,
                "config_path": (
                    None
                    if selected == "NONE"
                    else (CONFIG_DIR / f"{selected.lower()}.json").relative_to(ROOT).as_posix()
                ),
                "live_enabled": False,
                "fresh_forward_required": True,
                "selection_protocol": {
                    "development_end_exclusive": str(mechanics.HOLDOUT_START.date()),
                    "folds": {
                        name: {
                            "train_start": str(start.date()),
                            "validation_start": str(validation_start.date()),
                            "validation_end_exclusive": str(end.date()),
                        }
                        for name, (start, validation_start, end) in mechanics.DEVELOPMENT_FOLDS.items()
                    },
                    "holdout_used_for_selection": False,
                },
                "final_holdout_evaluation": holdout_gates,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (REPORT_DIR / "report.md").write_text(
        "# Machine candidate family V2\n\n"
        "Old candidates are preserved. Human/manual similarity is not used. Rules and ranking use rolling-origin pre-2025 development folds only.\n\n"
        "The 2025+ final holdout is evaluated only after selection and never contributes to ranking.\n\n"
        f"Selected: `{selected}`. Live enabled: `False`.\n\n"
        "```text\n" + ranking_frame.to_string(index=False) + "\n```\n",
        encoding="utf-8",
    )
    print(ranking_frame.to_string(index=False))
    print(f"Selected: {selected}")


if __name__ == "__main__":
    main()
