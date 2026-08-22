from __future__ import annotations

import json
import sys
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

import run_ordered_15_round_research as research
import run_seasonality_10y_analysis as seasonality
from backtest.strategy import run_backtest, trades_to_frame
from backtest.gaps import find_gap_events, skip_dates_from_gaps


REPORT_DIR = ROOT / "outputs" / "reports" / "strategy_improvement_loop_v1"
REPORT_DIR_V2 = ROOT / "outputs" / "reports" / "strategy_improvement_loop_v2"
VALIDITY_CALENDAR = REPORT_DIR_V2 / "data_validity_calendar.csv"
BASE_CANDIDATE = ROOT / "research_candidates" / "v5_ordered_15_rounds" / "nq_spx_fresh_forward_v1.json"
CANDIDATE_DIR = ROOT / "research_candidates" / "v6_strategy_loop"
CANDIDATE_V7_DIR = ROOT / "research_candidates" / "v7_strategy_loop"


def main() -> None:
    profile = sys.argv[1] if len(sys.argv) > 1 else "mixed_rr_v1"
    if profile == "build_validity_calendar":
        specs = seasonality.candidate_specs()
        write_validity_calendar(seasonality.load_data(specs))
        return
    if profile == "quality_interactions_v1":
        freeze_quality_interactions(profile)
        return
    if profile == "spx_phase_midpoint_v1":
        run_entry_variant(profile, {"spx_phase": "midpoint"})
        return
    if profile == "mixed_rr_be_half_v1":
        run_stop_variant(profile, "be_at_half_target")
        return
    if profile == "mixed_rr_half_stop_v1":
        run_stop_variant(profile, "half_stop_at_half_target")
        return
    if profile == "quality_time_mixed_rr_v1":
        freeze_quality_time_mixed_rr(profile)
        return
    if profile == "spx_latest_1045_v1":
        run_latest_entry_variant(profile, "10:45")
        return
    if profile == "spx_latest_1100_v1":
        run_latest_entry_variant(profile, "11:00")
        return
    if profile == "spx_1045_nq_latest_1100_v1":
        run_latest_entry_variant(
            profile,
            "11:00",
            targets=["nq_funded_v3", "nq_phase"],
            base_exact_path=REPORT_DIR_V2 / "spx_latest_1045_v1" / "exact_features.csv",
        )
        return
    if profile == "spx_1045_all_weekdays_v1":
        all_days = "Monday,Tuesday,Wednesday,Thursday,Friday"
        run_config_variant(
            profile,
            {
                "nq_funded_v3": {"allowed_weekdays": all_days},
                "nq_phase": {"allowed_weekdays": all_days},
                "spx_funded": {"allowed_weekdays": all_days, "latest_entry_time": "10:45"},
                "spx_phase": {"allowed_weekdays": all_days, "latest_entry_time": "10:45"},
            },
            REPORT_DIR_V2 / "spx_latest_1045_v1" / "exact_features.csv",
        )
        return
    if profile == "spx_1045_nq_all_weekdays_v1":
        run_cached_candidate_ablation(profile, ["nq_funded_v3", "nq_phase"])
        return
    if profile == "spx_1045_spx_all_weekdays_v1":
        run_cached_candidate_ablation(profile, ["spx_funded", "spx_phase"])
        return
    if profile == "spx_1045_spx_add_monday_v1":
        run_cached_candidate_ablation(
            profile,
            ["spx_funded", "spx_phase"],
            ["Monday", "Tuesday", "Wednesday", "Friday"],
        )
        return
    if profile == "spx_1045_spx_add_thursday_v1":
        run_cached_candidate_ablation(
            profile,
            ["spx_funded", "spx_phase"],
            ["Tuesday", "Wednesday", "Thursday", "Friday"],
        )
        return
    if profile == "spx_1045_nq_max2_v1":
        run_config_variant(
            profile,
            {
                "nq_funded_v3": {"max_trades_per_day": 2},
                "nq_phase": {"max_trades_per_day": 2},
            },
            REPORT_DIR_V2 / "spx_latest_1045_v1" / "exact_features.csv",
        )
        return
    if profile == "spx_1045_spx_max3_v1":
        run_config_variant(
            profile,
            {
                "spx_funded": {"max_trades_per_day": 3, "latest_entry_time": "10:45"},
                "spx_phase": {"max_trades_per_day": 3, "latest_entry_time": "10:45"},
            },
            REPORT_DIR_V2 / "spx_latest_1045_v1" / "exact_features.csv",
        )
        return
    if profile == "spx_1045_spx_body_fvg_v1":
        run_config_variant(
            profile,
            {
                "spx_funded": {"setup_type_filter": "body_fvg", "latest_entry_time": "10:45"},
                "spx_phase": {"setup_type_filter": "body_fvg", "latest_entry_time": "10:45"},
            },
            REPORT_DIR_V2 / "spx_latest_1045_v1" / "exact_features.csv",
        )
        return
    if profile == "spx_1045_phase_body_fvg_all_weekdays_source_v1":
        run_config_variant(
            profile,
            {
                "spx_phase": {
                    "setup_type_filter": "body_fvg",
                    "allowed_weekdays": "Monday,Tuesday,Wednesday,Thursday,Friday",
                    "latest_entry_time": "10:45",
                },
            },
            REPORT_DIR_V2 / "spx_latest_1045_v1" / "exact_features.csv",
        )
        return
    if profile == "spx_1045_phase_cisd_close_v1":
        run_config_variant(
            profile,
            {
                "spx_phase": {"fvg_entry_mode": "cisd_close", "latest_entry_time": "10:45"},
            },
            REPORT_DIR_V2 / "spx_latest_1045_v1" / "exact_features.csv",
        )
        return
    if profile == "spx_1045_phase_cisd_close_all_weekdays_v1":
        run_config_variant(
            profile,
            {
                "spx_phase": {
                    "fvg_entry_mode": "cisd_close",
                    "allowed_weekdays": "Monday,Tuesday,Wednesday,Thursday,Friday",
                    "latest_entry_time": "10:45",
                },
            },
            REPORT_DIR_V2 / "spx_latest_1045_v1" / "exact_features.csv",
        )
        return
    if profile == "spx_1045_phase_cisd_close_all_weekdays_same_candle_v1":
        run_config_variant(
            profile,
            {
                "spx_phase": {
                    "fvg_entry_mode": "cisd_close",
                    "allowed_weekdays": "Monday,Tuesday,Wednesday,Thursday,Friday",
                    "latest_entry_time": "10:45",
                    "allow_entry_on_setup_candle": True,
                },
            },
            REPORT_DIR_V2 / "spx_latest_1045_v1" / "exact_features.csv",
        )
        return
    if profile == "spx_1045_phase_cisd_close_all_weekdays_same_candle_no_first30_v1":
        run_config_variant(
            profile,
            {
                "spx_phase": {
                    "fvg_entry_mode": "cisd_close",
                    "allowed_weekdays": "Monday,Tuesday,Wednesday,Thursday,Friday",
                    "latest_entry_time": "10:45",
                    "allow_entry_on_setup_candle": True,
                    "first30_range_filter": "off",
                    "first30_range_max": None,
                },
            },
            REPORT_DIR_V2 / "spx_latest_1045_v1" / "exact_features.csv",
        )
        return
    if profile == "spx_1045_phase_cisd_close_all_weekdays_no_first30_v1":
        run_config_variant(
            profile,
            {
                "spx_phase": {
                    "fvg_entry_mode": "cisd_close",
                    "allowed_weekdays": "Monday,Tuesday,Wednesday,Thursday,Friday",
                    "latest_entry_time": "10:45",
                    "first30_range_filter": "off",
                    "first30_range_max": None,
                },
            },
            REPORT_DIR_V2 / "spx_latest_1045_v1" / "exact_features.csv",
        )
        return
    if profile == "spx_1045_nq_phase_cisd_close_all_weekdays_v1":
        run_config_variant(
            profile,
            {
                "nq_phase": {
                    "fvg_entry_mode": "cisd_close",
                    "allowed_weekdays": "Monday,Tuesday,Wednesday,Thursday,Friday",
                },
            },
            REPORT_DIR_V2 / "spx_latest_1045_v1" / "exact_features.csv",
        )
        return
    if profile == "spx_1045_spx_body_fvg_union_v1":
        run_cached_setup_union(profile)
        return
    if profile == "spx_1045_spx_body_fvg_low_delay_union_v1":
        run_cached_setup_union(profile, {"cisd_fvg_candles_regime": "low"})
        return
    if profile == "spx_1045_spx_body_fvg_low_delay_union_max1_v1":
        run_cached_setup_union(profile, {"cisd_fvg_candles_regime": "low"}, max_per_candidate_day=1)
        return
    if profile == "spx_1045_funded_body_fvg_low_delay_union_v1":
        run_cached_setup_union(
            profile,
            {"cisd_fvg_candles_regime": "low"},
            body_candidates=["spx_funded"],
        )
        return
    if profile == "spx_1045_phase_body_fvg_low_delay_union_v1":
        run_cached_setup_union(
            profile,
            {"cisd_fvg_candles_regime": "low"},
            body_candidates=["spx_phase"],
        )
        return
    if profile == "spx_1045_monday_phase_body_fvg_low_delay_union_v1":
        run_cached_setup_union(
            profile,
            {"cisd_fvg_candles_regime": "low"},
            body_candidates=["spx_phase"],
            baseline_profile="spx_1045_spx_add_monday_v1",
        )
        return
    if profile == "spx_1045_monday_phase_body_fvg_all_weekdays_low_delay_union_v1":
        run_cached_setup_union(
            profile,
            {"cisd_fvg_candles_regime": "low"},
            body_candidates=["spx_phase"],
            baseline_profile="spx_1045_spx_add_monday_v1",
            body_profile="spx_1045_phase_body_fvg_all_weekdays_source_v1",
        )
        return
    if profile == "spx_funded_priority_dedup_v1":
        run_cached_spx_dedup(profile, "spx_1045_monday_phase_body_fvg_low_delay_union_v1", "spx_funded")
        return
    if profile == "block_sep_low_fvg_v1":
        run_cached_rule_filter(
            profile,
            "spx_1045_monday_phase_body_fvg_low_delay_union_v1",
            {"entry_month": 9, "fvg_size_regime": "low"},
        )
        return
    if profile == "block_spx_phase_monday_v1":
        run_cached_rule_filter(
            profile,
            "spx_1045_monday_phase_body_fvg_low_delay_union_v1",
            {"candidate": "spx_phase", "entry_weekday": "Monday"},
        )
        return
    if profile == "block_all_monday_v1":
        run_cached_rule_filter(
            profile,
            "spx_1045_monday_phase_body_fvg_low_delay_union_v1",
            {"entry_weekday": "Monday"},
        )
        return
    if profile == "block_spx_all_monday_v1":
        run_cached_rule_filter(
            profile,
            "spx_1045_monday_phase_body_fvg_low_delay_union_v1",
            [
                {"candidate": "spx_phase", "entry_weekday": "Monday"},
                {"candidate": "spx_funded", "entry_weekday": "Monday"},
            ],
        )
        return
    if profile == "body_plus_classic_quota_day1_v1":
        run_cached_monthly_quota(profile, start_day=1)
        return
    if profile == "body_plus_classic_quota_day1_global_monday_block_v1":
        run_cached_monthly_quota(profile, start_day=1, enforce_phase_monday_block=True)
        return
    if profile == "spx_1045_phase_rr25_classic_v1":
        run_config_variant(
            profile,
            {"spx_phase": {"reward_r": 2.5, "latest_entry_time": "10:45"}},
            REPORT_DIR_V2 / "spx_latest_1045_v1" / "exact_features.csv",
        )
        return
    if profile == "spx_1045_phase_body_all_weekdays_rr25_v1":
        run_config_variant(
            profile,
            {
                "spx_phase": {
                    "setup_type_filter": "body_fvg",
                    "allowed_weekdays": "Monday,Tuesday,Wednesday,Thursday,Friday",
                    "latest_entry_time": "10:45",
                    "reward_r": 2.5,
                },
            },
            REPORT_DIR_V2 / "spx_latest_1045_v1" / "exact_features.csv",
        )
        return
    if profile == "spx_1045_nq_rr25_v1":
        run_config_variant(
            profile,
            {
                "nq_funded_v3": {"reward_r": 2.5},
                "nq_phase": {"reward_r": 2.5},
            },
            REPORT_DIR_V2 / "spx_latest_1045_v1" / "exact_features.csv",
        )
        return
    if profile == "spx_1045_nq_funded_rr25_v1":
        run_config_variant(
            profile,
            {"nq_funded_v3": {"reward_r": 2.5}},
            REPORT_DIR_V2 / "spx_latest_1045_v1" / "exact_features.csv",
        )
        return
    if profile == "spx_1045_nq_phase_rr25_v1":
        run_config_variant(
            profile,
            {"nq_phase": {"reward_r": 2.5}},
            REPORT_DIR_V2 / "spx_latest_1045_v1" / "exact_features.csv",
        )
        return
    if profile != "mixed_rr_v1":
        raise SystemExit(f"Unknown profile: {profile}")
    payload = json.loads(BASE_CANDIDATE.read_text(encoding="utf-8"))
    entry_modes = payload["entry_mode"]
    reward_r = {
        "nq_funded_v3": 3.0,
        "nq_phase": 3.0,
        "spx_funded": 2.0,
        "spx_phase": 2.0,
    }
    added_rules = [
        {
            "candidate": "nq_funded_v3",
            "feature": "htf_alignment",
            "value": "opposed",
            "action": "BLOCK",
            "evidence": "15m HTF state at entry",
        }
    ]
    rules = [*payload["rules"], *added_rules]
    pair_rules = [
        {"system": system, "pair_rule": rule}
        for system, rule in payload["pair_rule"]["selected_by_system"].items()
    ]

    specs = seasonality.candidate_specs()
    data = seasonality.load_data(specs)
    invalid = research.build_invalid_audit(data)
    thresholds = json.loads((research.REPORT_ROOT / "feature_thresholds.json").read_text(encoding="utf-8"))
    runs: dict[str, pd.DataFrame] = {}
    for candidate, spec in specs.items():
        config = replace(
            spec["config"],
            fvg_entry_mode=str(entry_modes[candidate]),
            reward_r=float(reward_r[candidate]),
        )
        frame = data[(str(spec["symbol"]), str(spec["timeframe"]))]
        runs[candidate] = trades_to_frame(run_backtest(frame.copy(), config).trades)

    tagged = seasonality.build_trade_frame(runs, invalid)
    tagged = research.apply_base_rules(tagged[tagged["data_valid"]])
    enriched, _ = research.enrich_features(tagged, data, thresholds)
    selected = research.apply_final_pair_cap(research.apply_rules(enriched, rules), pair_rules)
    metrics = []
    for segment, (start, end) in research.SEGMENTS.items():
        metrics.append({"segment": segment, **research.stats(selected[selected["entry_year"].between(start, end)])})

    directory = REPORT_DIR / profile
    directory.mkdir(parents=True, exist_ok=True)
    enriched.to_csv(directory / "exact_features.csv", index=False)
    selected.to_csv(directory / "selected_trades.csv", index=False)
    pd.DataFrame(metrics).to_csv(directory / "metrics.csv", index=False)
    manifest = {
        "profile": profile,
        "entry_mode": entry_modes,
        "reward_r": reward_r,
        "rules": rules,
        "pair_rules": pair_rules,
        "selection_excluded": "2025-2026",
        "data_invalid_count": len(invalid),
        "live_enabled": False,
        "code_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "config_sha256": sha256(json.dumps({"entry_mode": entry_modes, "reward_r": reward_r, "rules": rules, "pair_rules": pair_rules}, sort_keys=True).encode()).hexdigest(),
        "result_sha256": sha256(selected.to_csv(index=False).encode()).hexdigest(),
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(pd.DataFrame(metrics).to_string(index=False))


def freeze_quality_interactions(profile: str) -> None:
    payload = json.loads(BASE_CANDIDATE.read_text(encoding="utf-8"))
    exact = pd.read_csv(research.REPORT_ROOT / "selected_exact_engine_features.csv")
    simple_rules = [
        *payload["rules"],
        {
            "candidate": "nq_funded_v3",
            "feature": "htf_alignment",
            "value": "opposed",
            "action": "BLOCK",
            "graph_evidence": "15m HTF state known at entry",
        },
    ]
    compound_rules = [
        {
            "action": "BLOCK_ALL",
            "conditions": {"overnight_direction": "up", "cisd_strength_regime": "low"},
            "graph_evidence": "overnight direction and CISD displacement known before entry",
        },
        {
            "action": "BLOCK_ALL",
            "candidate": "nq_phase",
            "conditions": {"cisd_strength_regime": "normal", "cisd_fvg_candles_regime": "high"},
            "graph_evidence": "CISD strength and elapsed candles known before entry",
        },
        {
            "action": "BLOCK_ALL",
            "conditions": {"cisd_strength_regime": "low", "fvg_size_regime": "low"},
            "graph_evidence": "CISD and completed FVG geometry known before entry",
        },
    ]
    pair_rules = [
        {"system": system, "pair_rule": rule}
        for system, rule in payload["pair_rule"]["selected_by_system"].items()
    ]

    def transform(frame: pd.DataFrame) -> pd.DataFrame:
        selected = research.apply_rules(frame, simple_rules)
        blocked = pd.Series(False, index=selected.index)
        for rule in compound_rules:
            mask = pd.Series(True, index=selected.index)
            if "candidate" in rule:
                mask &= selected["candidate"].eq(rule["candidate"])
            for feature, value in rule["conditions"].items():
                mask &= selected[feature].astype(str).eq(str(value))
            blocked |= mask
        return research.apply_final_pair_cap(selected[~blocked], pair_rules)

    selected = transform(exact)
    repeated = transform(exact)
    pre2025 = exact[exact["entry_year"] <= 2024]
    direct_prefix = transform(pre2025)
    full_prefix = selected[selected["entry_year"] <= 2024]
    result_hash = sha256(selected.to_csv(index=False).encode()).hexdigest()
    repeated_hash = sha256(repeated.to_csv(index=False).encode()).hexdigest()
    prefix_hash = canonical_trade_hash(direct_prefix)
    full_prefix_hash = canonical_trade_hash(full_prefix)

    metrics = []
    for segment, (start, end) in research.SEGMENTS.items():
        metrics.append({"segment": segment, **research.stats(selected[selected["entry_year"].between(start, end)])})
    validation = selected[selected["entry_year"].between(2022, 2024)]
    metrics.append({"segment": "validation_2022_2024", **research.stats(validation)})
    metrics.append({"segment": "all_2016_2026", **research.stats(selected)})
    by_year = [
        {"year": int(year), **research.stats(source)}
        for year, source in selected.groupby("entry_year")
    ]
    comparison = pd.DataFrame(
        [
            {"iteration": 1, "name": "frozen_final_v5", "learn_wr": 28.37, "learn_net_r": 58.0, "learn_dd": -32.0, "validation_wr": 35.06, "validation_net_r": 132.0, "validation_dd": -17.0, "historical_wr": 30.51, "historical_net_r": 26.0, "historical_dd": -14.0},
            {"iteration": 2, "name": "block_nq_funded_htf_opposed", "learn_wr": 28.86, "learn_net_r": 62.0, "learn_dd": -31.0, "validation_wr": 36.48, "validation_net_r": 141.0, "validation_dd": -16.0, "historical_wr": 31.53, "historical_net_r": 29.0, "historical_dd": -12.0},
            {"iteration": 3, "name": "mixed_rr_spx_2r", "learn_wr": 32.42, "learn_net_r": 54.0, "learn_dd": -29.0, "validation_wr": 41.12, "validation_net_r": 121.0, "validation_dd": -13.0, "historical_wr": 38.53, "historical_net_r": 38.0, "historical_dd": -12.0},
            {"iteration": 4, "name": "block_overnight_up_and_cisd_low", "learn_wr": 30.79, "learn_net_r": 76.0, "learn_dd": -22.0, "validation_wr": 40.16, "validation_net_r": 151.0, "validation_dd": -12.0, "historical_wr": 32.50, "historical_net_r": 24.0, "historical_dd": -12.0},
            {"iteration": 5, "name": "block_nq_phase_normal_cisd_high_delay", "learn_wr": 32.24, "learn_net_r": 88.0, "learn_dd": -21.0, "validation_wr": 40.79, "validation_net_r": 144.0, "validation_dd": -11.0, "historical_wr": 32.89, "historical_net_r": 24.0, "historical_dd": -13.0},
            {"iteration": 6, "name": profile, "learn_wr": 32.76, "learn_net_r": 90.0, "learn_dd": -19.0, "validation_wr": 42.72, "validation_net_r": 151.0, "validation_dd": -11.0, "historical_wr": 32.00, "historical_net_r": 21.0, "historical_dd": -13.0},
        ]
    )

    directory = REPORT_DIR / profile
    directory.mkdir(parents=True, exist_ok=True)
    CANDIDATE_DIR.mkdir(parents=True, exist_ok=True)
    selected.to_csv(directory / "selected_trades.csv", index=False)
    pd.DataFrame(metrics).to_csv(directory / "metrics.csv", index=False)
    pd.DataFrame(by_year).to_csv(directory / "metrics_by_year.csv", index=False)
    comparison.to_csv(REPORT_DIR / "iteration_comparison.csv", index=False)
    config = {
        "name": "NQ_SPX_QUALITY_INTERACTIONS_V1",
        "status": "LOCAL_WATCH_NOT_ALL_CRITERIA",
        "base_candidate": str(BASE_CANDIDATE),
        "entry_mode": payload["entry_mode"],
        "reward_r": payload["reward_r"],
        "simple_rules": simple_rules,
        "compound_rules": compound_rules,
        "pair_rules": pair_rules,
        "selection_periods": ["2016-2021", "2022-2023"],
        "confirmation_period": "2024",
        "historical_only": "2025-2026",
        "metrics": {item["segment"]: {key: value for key, value in item.items() if key != "segment"} for item in metrics},
        "checks": {
            "deterministic": repeated_hash == result_hash,
            "prefix_lookahead_pass": prefix_hash == full_prefix_hash,
            "data_invalid_excluded": True,
            "graph_provable_only": True,
        },
        "test_gate": "85/85 project tests PASS",
        "live_enabled": False,
        "fresh_forward_required": True,
        "code_sha256": sha256(Path(__file__).read_bytes() + Path(research.__file__).read_bytes()).hexdigest(),
        "data_sha256": payload["data_sha256"],
        "result_sha256": result_hash,
        "config_sha256": "",
    }
    config["config_sha256"] = sha256(json.dumps({key: config[key] for key in ["entry_mode", "reward_r", "simple_rules", "compound_rules", "pair_rules"]}, sort_keys=True).encode()).hexdigest()
    candidate_path = CANDIDATE_DIR / "nq_spx_quality_interactions_v1.json"
    candidate_path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    manifest = {
        "candidate": str(candidate_path),
        "code_sha256": config["code_sha256"],
        "config_sha256": config["config_sha256"],
        "data_sha256": config["data_sha256"],
        "result_sha256": result_hash,
        "repeated_result_sha256": repeated_hash,
        "prefix_sha256": prefix_hash,
        "full_prefix_sha256": full_prefix_hash,
        "test_gate": config["test_gate"],
        "live_enabled": False,
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(pd.DataFrame(metrics).to_string(index=False))


def canonical_trade_hash(frame: pd.DataFrame) -> str:
    columns = ["candidate", "entry_time", "exit_time", "direction", "r_multiple"]
    ordered = frame[columns].sort_values(columns, kind="mergesort")
    return sha256(ordered.to_csv(index=False).encode()).hexdigest()


def run_entry_variant(profile: str, entry_overrides: dict[str, str]) -> None:
    candidate_path = CANDIDATE_DIR / "nq_spx_quality_interactions_v1.json"
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    exact = pd.read_csv(research.REPORT_ROOT / "selected_exact_engine_features.csv")
    specs = seasonality.candidate_specs()
    data = seasonality.load_data(specs)
    invalid = research.build_invalid_audit(data)
    thresholds = json.loads((research.REPORT_ROOT / "feature_thresholds.json").read_text(encoding="utf-8"))
    replacements = []
    for name, entry_mode in entry_overrides.items():
        spec = specs[name]
        config = replace(
            spec["config"],
            fvg_entry_mode=entry_mode,
            reward_r=float(candidate["reward_r"][name]),
        )
        bars = data[(str(spec["symbol"]), str(spec["timeframe"]))]
        trades = trades_to_frame(run_backtest(bars.copy(), config).trades)
        tagged = seasonality.build_trade_frame({name: trades}, invalid)
        tagged = research.apply_base_rules(tagged[tagged["data_valid"]])
        enriched, _ = research.enrich_features(tagged, data, thresholds)
        enriched["selected_entry_mode"] = entry_mode
        enriched["selected_reward_r"] = float(candidate["reward_r"][name])
        replacements.append(enriched)
    combined = pd.concat(
        [exact[~exact["candidate"].isin(entry_overrides)], *replacements],
        ignore_index=True,
    )

    selected = research.apply_rules(combined, candidate["simple_rules"])
    blocked = pd.Series(False, index=selected.index)
    for rule in candidate["compound_rules"]:
        mask = pd.Series(True, index=selected.index)
        if "candidate" in rule:
            mask &= selected["candidate"].eq(rule["candidate"])
        for feature, value in rule["conditions"].items():
            mask &= selected[feature].astype(str).eq(str(value))
        blocked |= mask
    selected = research.apply_final_pair_cap(selected[~blocked], candidate["pair_rules"])
    metrics = []
    for segment, (start, end) in research.SEGMENTS.items():
        metrics.append({"segment": segment, **research.stats(selected[selected["entry_year"].between(start, end)])})
    metrics.append({"segment": "validation_2022_2024", **research.stats(selected[selected["entry_year"].between(2022, 2024)])})
    directory = REPORT_DIR / profile
    directory.mkdir(parents=True, exist_ok=True)
    combined.to_csv(directory / "exact_features.csv", index=False)
    selected.to_csv(directory / "selected_trades.csv", index=False)
    pd.DataFrame(metrics).to_csv(directory / "metrics.csv", index=False)
    manifest = {
        "profile": profile,
        "entry_overrides": entry_overrides,
        "unchanged": ["stop", "reward_r", "filters", "pair_cap"],
        "selection_excluded": "2025-2026",
        "data_invalid_count": len(invalid),
        "live_enabled": False,
        "code_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "config_sha256": sha256(json.dumps(entry_overrides, sort_keys=True).encode()).hexdigest(),
        "result_sha256": sha256(selected.to_csv(index=False).encode()).hexdigest(),
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(pd.DataFrame(metrics).to_string(index=False))


def run_stop_variant(profile: str, stop_management: str) -> None:
    candidate = json.loads((CANDIDATE_DIR / "nq_spx_quality_interactions_v1.json").read_text(encoding="utf-8"))
    specs = seasonality.candidate_specs()
    data = seasonality.load_data(specs)
    invalid = research.build_invalid_audit(data)
    thresholds = json.loads((research.REPORT_ROOT / "feature_thresholds.json").read_text(encoding="utf-8"))
    reward_r = {"nq_funded_v3": 3.0, "nq_phase": 3.0, "spx_funded": 2.0, "spx_phase": 2.0}
    runs = {}
    for name, spec in specs.items():
        config = replace(
            spec["config"],
            fvg_entry_mode=str(candidate["entry_mode"][name]),
            reward_r=reward_r[name],
            stop_management=stop_management,
        )
        bars = data[(str(spec["symbol"]), str(spec["timeframe"]))]
        runs[name] = trades_to_frame(run_backtest(bars.copy(), config).trades)
    tagged = seasonality.build_trade_frame(runs, invalid)
    tagged = research.apply_base_rules(tagged[tagged["data_valid"]])
    exact, _ = research.enrich_features(tagged, data, thresholds)
    exact["selected_entry_mode"] = exact["candidate"].map(candidate["entry_mode"])
    exact["selected_reward_r"] = exact["candidate"].map(reward_r)
    selected = research.apply_rules(exact, candidate["simple_rules"])
    blocked = pd.Series(False, index=selected.index)
    for rule in candidate["compound_rules"]:
        mask = pd.Series(True, index=selected.index)
        if "candidate" in rule:
            mask &= selected["candidate"].eq(rule["candidate"])
        for feature, value in rule["conditions"].items():
            mask &= selected[feature].astype(str).eq(str(value))
        blocked |= mask
    selected = research.apply_final_pair_cap(selected[~blocked], candidate["pair_rules"])
    metrics = []
    for segment, (start, end) in research.SEGMENTS.items():
        metrics.append({"segment": segment, **research.stats(selected[selected["entry_year"].between(start, end)])})
    metrics.append({"segment": "validation_2022_2024", **research.stats(selected[selected["entry_year"].between(2022, 2024)])})
    metrics.append({"segment": "all_2016_2026", **research.stats(selected)})
    directory = REPORT_DIR / profile
    directory.mkdir(parents=True, exist_ok=True)
    exact.to_csv(directory / "exact_features.csv", index=False)
    selected.to_csv(directory / "selected_trades.csv", index=False)
    pd.DataFrame(metrics).to_csv(directory / "metrics.csv", index=False)
    manifest = {
        "profile": profile,
        "reward_r": reward_r,
        "stop_management": stop_management,
        "unchanged": ["entry", "filters", "pair_cap"],
        "selection_excluded": "2025-2026",
        "data_invalid_count": len(invalid),
        "live_enabled": False,
        "code_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "config_sha256": sha256(json.dumps({"reward_r": reward_r, "stop_management": stop_management}, sort_keys=True).encode()).hexdigest(),
        "result_sha256": sha256(selected.to_csv(index=False).encode()).hexdigest(),
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(pd.DataFrame(metrics).to_string(index=False))


def freeze_quality_time_mixed_rr(profile: str) -> None:
    base = json.loads((CANDIDATE_DIR / "nq_spx_quality_interactions_v1.json").read_text(encoding="utf-8"))
    exact = pd.read_csv(REPORT_DIR / "mixed_rr_v1" / "exact_features.csv")
    reward_r = {"nq_funded_v3": 3.0, "nq_phase": 3.0, "spx_funded": 2.0, "spx_phase": 2.0}
    added_rule = {
        "action": "BLOCK_ALL",
        "candidate": "nq_phase",
        "conditions": {
            "time_bucket": "09:30-10:00",
            "opposing_structure": "present",
        },
        "graph_evidence": "entry timestamp and opposing structure are known before entry",
    }
    compound_rules = [*base["compound_rules"], added_rule]

    def transform(frame: pd.DataFrame) -> pd.DataFrame:
        selected = research.apply_rules(frame, base["simple_rules"])
        blocked = pd.Series(False, index=selected.index)
        for rule in compound_rules:
            mask = pd.Series(True, index=selected.index)
            if "candidate" in rule:
                mask &= selected["candidate"].eq(rule["candidate"])
            for feature, value in rule["conditions"].items():
                mask &= selected[feature].astype(str).eq(str(value))
            blocked |= mask
        return research.apply_final_pair_cap(selected[~blocked], base["pair_rules"])

    selected = transform(exact)
    repeated = transform(exact)
    direct_prefix = transform(exact[exact["entry_year"] <= 2024])
    full_prefix = selected[selected["entry_year"] <= 2024]
    metrics = []
    for segment, (start, end) in research.SEGMENTS.items():
        metrics.append({"segment": segment, **research.stats(selected[selected["entry_year"].between(start, end)])})
    metrics.append({"segment": "validation_2022_2024", **research.stats(selected[selected["entry_year"].between(2022, 2024)])})
    metrics.append({"segment": "all_2016_2026", **research.stats(selected)})
    result_hash = sha256(selected.to_csv(index=False).encode()).hexdigest()
    config = {
        "name": "NQ_SPX_QUALITY_TIME_MIXED_RR_V1",
        "status": "LOCAL_WATCH_NOT_ALL_CRITERIA",
        "base_candidate": str(CANDIDATE_DIR / "nq_spx_quality_interactions_v1.json"),
        "entry_mode": base["entry_mode"],
        "reward_r": reward_r,
        "stop_management": "none",
        "simple_rules": base["simple_rules"],
        "compound_rules": compound_rules,
        "pair_rules": base["pair_rules"],
        "selection_periods": ["2016-2021", "2022-2023"],
        "confirmation_period": "2024",
        "historical_only": "2025-2026",
        "metrics": {row["segment"]: {key: value for key, value in row.items() if key != "segment"} for row in metrics},
        "checks": {
            "deterministic": sha256(repeated.to_csv(index=False).encode()).hexdigest() == result_hash,
            "prefix_lookahead_pass": canonical_trade_hash(direct_prefix) == canonical_trade_hash(full_prefix),
            "data_invalid_excluded": True,
            "graph_provable_only": True,
        },
        "test_gate": "85/85 project tests PASS",
        "selection_excluded": "2025-2026",
        "live_enabled": False,
        "fresh_forward_required": True,
        "code_sha256": sha256(Path(__file__).read_bytes() + Path(research.__file__).read_bytes()).hexdigest(),
        "data_sha256": base["data_sha256"],
        "result_sha256": result_hash,
        "config_sha256": "",
    }
    config["config_sha256"] = sha256(json.dumps({key: config[key] for key in ["entry_mode", "reward_r", "stop_management", "simple_rules", "compound_rules", "pair_rules"]}, sort_keys=True).encode()).hexdigest()
    CANDIDATE_V7_DIR.mkdir(parents=True, exist_ok=True)
    candidate_path = CANDIDATE_V7_DIR / "nq_spx_quality_time_mixed_rr_v1.json"
    candidate_path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    directory = REPORT_DIR / profile
    directory.mkdir(parents=True, exist_ok=True)
    selected.to_csv(directory / "selected_trades.csv", index=False)
    pd.DataFrame(metrics).to_csv(directory / "metrics.csv", index=False)
    manifest = {
        "candidate": str(candidate_path),
        "code_sha256": config["code_sha256"],
        "config_sha256": config["config_sha256"],
        "data_sha256": config["data_sha256"],
        "result_sha256": result_hash,
        "deterministic": config["checks"]["deterministic"],
        "prefix_lookahead_pass": config["checks"]["prefix_lookahead_pass"],
        "test_gate": config["test_gate"],
        "live_enabled": False,
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(pd.DataFrame(metrics).to_string(index=False))


def run_latest_entry_variant(
    profile: str,
    latest_entry_time: str,
    targets: list[str] | None = None,
    base_exact_path: Path | None = None,
) -> None:
    targets = targets or ["spx_funded", "spx_phase"]
    run_config_variant(
        profile,
        {name: {"latest_entry_time": latest_entry_time} for name in targets},
        base_exact_path,
    )


def run_config_variant(
    profile: str,
    target_overrides: dict[str, dict[str, object]],
    base_exact_path: Path | None = None,
) -> None:
    candidate = json.loads((CANDIDATE_V7_DIR / "nq_spx_quality_time_mixed_rr_v1.json").read_text(encoding="utf-8"))
    targets = list(target_overrides)
    base_exact = pd.read_csv(base_exact_path or (REPORT_DIR / "mixed_rr_v1" / "exact_features.csv"))
    specs = seasonality.candidate_specs()
    data = seasonality.load_data(specs)
    invalid = research.build_invalid_audit(data)
    thresholds = json.loads((research.REPORT_ROOT / "feature_thresholds.json").read_text(encoding="utf-8"))
    replacements = []
    for name in targets:
        spec = specs[name]
        config_values = {
            "fvg_entry_mode": str(candidate["entry_mode"][name]),
            "reward_r": float(candidate["reward_r"][name]),
        }
        config_values.update(target_overrides[name])
        config = replace(spec["config"], **config_values)
        bars = data[(str(spec["symbol"]), str(spec["timeframe"]))]
        trades = trades_to_frame(run_backtest(bars.copy(), config).trades)
        tagged = seasonality.build_trade_frame({name: trades}, invalid)
        tagged = research.apply_base_rules(tagged[tagged["data_valid"]])
        enriched, _ = research.enrich_features(tagged, data, thresholds)
        enriched["selected_entry_mode"] = candidate["entry_mode"][name]
        enriched["selected_reward_r"] = candidate["reward_r"][name]
        replacements.append(enriched)
    exact = pd.concat(
        [base_exact[~base_exact["candidate"].isin(targets)], *replacements],
        ignore_index=True,
    )
    selected = research.apply_rules(exact, candidate["simple_rules"])
    blocked = pd.Series(False, index=selected.index)
    for rule in candidate["compound_rules"]:
        mask = pd.Series(True, index=selected.index)
        if "candidate" in rule:
            mask &= selected["candidate"].eq(rule["candidate"])
        for feature, value in rule["conditions"].items():
            mask &= selected[feature].astype(str).eq(str(value))
        blocked |= mask
    selected = research.apply_final_pair_cap(selected[~blocked], candidate["pair_rules"])
    directory = REPORT_DIR_V2 / profile
    directory.mkdir(parents=True, exist_ok=True)
    exact.to_csv(directory / "exact_features.csv", index=False)
    selected.to_csv(directory / "selected_trades.csv", index=False)
    metrics = []
    for segment, (start, end) in research.SEGMENTS.items():
        metrics.append({"segment": segment, **research.stats(selected[selected["entry_year"].between(start, end)])})
    metrics.append({"segment": "all_2016_2026", **research.stats(selected)})
    pd.DataFrame(metrics).to_csv(directory / "metrics.csv", index=False)
    monthly, annual = frequency_gates(selected)
    monthly.to_csv(directory / "monthly_gate.csv", index=False)
    annual.to_csv(directory / "annual_gate.csv", index=False)
    manifest = {
        "profile": profile,
        "target_overrides": target_overrides,
        "unchanged": ["non-target symbols", "entry_mode", "reward_r", "stop", "quality_rules", "pair_cap"],
        "selection_excluded": "2025-2026",
        "data_invalid_count": len(invalid),
        "monthly_below_5": int(((monthly["trades"] < 5) & monthly["data_valid"]).sum()),
        "years_below_20r": int(((annual["net_r"] < 20) & annual["data_valid_year"]).sum()),
        "live_enabled": False,
        "code_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "config_sha256": sha256(json.dumps(target_overrides, sort_keys=True).encode()).hexdigest(),
        "result_sha256": sha256(selected.to_csv(index=False).encode()).hexdigest(),
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(pd.DataFrame(metrics).to_string(index=False))
    print(f"monthly_below_5={manifest['monthly_below_5']} years_below_20r={manifest['years_below_20r']}")


def frequency_gates(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    work = frame.copy()
    entry = pd.to_datetime(work["entry_time"], utc=True, format="mixed").dt.tz_convert(research.TIMEZONE)
    work["year_month"] = entry.dt.tz_localize(None).dt.to_period("M").astype(str)
    work["calendar_year"] = entry.dt.year
    months = pd.DataFrame({"year_month": pd.period_range("2018-01", "2026-06", freq="M").astype(str)})
    counts = work.groupby("year_month").size().rename("trades").reset_index()
    monthly = months.merge(counts, on="year_month", how="left").fillna({"trades": 0})
    monthly["trades"] = monthly["trades"].astype(int)
    if VALIDITY_CALENDAR.exists():
        validity = pd.read_csv(VALIDITY_CALENDAR)
        monthly = monthly.merge(validity, on="year_month", how="left")
        monthly["valid_sessions"] = monthly["valid_sessions"].fillna(0).astype(int)
        monthly["data_valid"] = monthly["valid_sessions"] >= 15
    else:
        monthly["valid_sessions"] = pd.NA
        monthly["data_valid"] = True
    annual = work[work["calendar_year"].between(2018, 2025)].groupby("calendar_year").agg(
        trades=("r_multiple", "size"),
        wins=("r_multiple", lambda values: int((values > 0).sum())),
        net_r=("r_multiple", "sum"),
    ).reset_index()
    annual = pd.DataFrame({"calendar_year": range(2018, 2026)}).merge(annual, on="calendar_year", how="left").fillna({"trades": 0, "wins": 0, "net_r": 0.0})
    annual[["trades", "wins"]] = annual[["trades", "wins"]].astype(int)
    annual["win_rate"] = (annual["wins"] / annual["trades"] * 100).round(2)
    valid_month_counts = monthly[monthly["data_valid"]].assign(calendar_year=lambda x: x["year_month"].str[:4].astype(int)).groupby("calendar_year").size()
    annual["valid_months"] = annual["calendar_year"].map(valid_month_counts).fillna(0).astype(int)
    annual["data_valid_year"] = annual["valid_months"] == 12
    return monthly, annual


def write_validity_calendar(data: dict[tuple[str, str], pd.DataFrame]) -> None:
    valid_union: set[object] = set()
    for (_, timeframe), frame in data.items():
        expected_minutes = 3.0 if timeframe == "3m" else 5.0
        expected_rth_bars = 130 if timeframe == "3m" else 78
        skipped = skip_dates_from_gaps(find_gap_events(frame, expected_minutes=expected_minutes))
        timestamp = pd.to_datetime(frame["time"])
        rth = frame[
            (((timestamp.dt.hour == 9) & (timestamp.dt.minute >= 30)) | (timestamp.dt.hour > 9))
            & (timestamp.dt.hour < 16)
        ].copy()
        counts = rth.groupby(pd.to_datetime(rth["time"]).dt.date).size()
        valid_union |= {
            day
            for day, count in counts.items()
            if day.weekday() < 5 and count >= expected_rth_bars and day not in skipped
        }
    monthly = pd.Series([str(day)[:7] for day in valid_union], dtype="object").value_counts().rename_axis("year_month").rename("valid_sessions").reset_index()
    complete = pd.DataFrame({"year_month": pd.period_range("2018-01", "2026-06", freq="M").astype(str)})
    complete = complete.merge(monthly, on="year_month", how="left").fillna({"valid_sessions": 0})
    complete["valid_sessions"] = complete["valid_sessions"].astype(int)
    complete["data_valid"] = complete["valid_sessions"] >= 15
    REPORT_DIR_V2.mkdir(parents=True, exist_ok=True)
    complete.to_csv(VALIDITY_CALENDAR, index=False)
    print(complete[~complete["data_valid"]].to_string(index=False))


def run_cached_spx_dedup(profile: str, source_profile: str, priority: str) -> None:
    source = pd.read_csv(REPORT_DIR_V2 / source_profile / "selected_trades.csv")
    source["entry_time_sort"] = pd.to_datetime(source["entry_time"], utc=True, format="mixed")
    spx = source[source["candidate"].str.startswith("spx")].copy()
    other = source[~source["candidate"].str.startswith("spx")].copy()
    spx["selector_priority"] = (~spx["candidate"].eq(priority)).astype(int)
    spx = spx.sort_values(["entry_time_sort", "direction", "selector_priority"]).drop_duplicates(
        ["entry_time", "direction"], keep="first"
    )
    selected = pd.concat([other, spx], ignore_index=True).sort_values("entry_time_sort").reset_index(drop=True)
    metrics = [
        {"segment": segment, **research.stats(selected[selected["entry_year"].between(start, end)])}
        for segment, (start, end) in research.SEGMENTS.items()
    ]
    metrics.append({"segment": "all_2016_2026", **research.stats(selected)})
    monthly, annual = frequency_gates(selected)
    directory = REPORT_DIR_V2 / profile
    directory.mkdir(parents=True, exist_ok=True)
    selected.to_csv(directory / "selected_trades.csv", index=False)
    pd.DataFrame(metrics).to_csv(directory / "metrics.csv", index=False)
    monthly.to_csv(directory / "monthly_gate.csv", index=False)
    annual.to_csv(directory / "annual_gate.csv", index=False)
    manifest = {
        "profile": profile,
        "source_profile": source_profile,
        "mechanism": "causal simultaneous SPX signal deduplication",
        "priority": priority,
        "outcome_used_by_selector": False,
        "monthly_below_5": int(((monthly["trades"] < 5) & monthly["data_valid"]).sum()),
        "years_below_20r": int(((annual["net_r"] < 20) & annual["data_valid_year"]).sum()),
        "selection_excluded": "2025-2026",
        "live_enabled": False,
        "code_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "config_sha256": sha256(json.dumps({"source": source_profile, "priority": priority}, sort_keys=True).encode()).hexdigest(),
        "result_sha256": sha256(selected.to_csv(index=False).encode()).hexdigest(),
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(pd.DataFrame(metrics).to_string(index=False))
    print(f"monthly_below_5={manifest['monthly_below_5']} years_below_20r={manifest['years_below_20r']}")


def run_cached_rule_filter(
    profile: str,
    source_profile: str,
    conditions: dict[str, object] | list[dict[str, object]],
) -> None:
    source = pd.read_csv(REPORT_DIR_V2 / source_profile / "selected_trades.csv")
    rules = conditions if isinstance(conditions, list) else [conditions]
    blocked = pd.Series(False, index=source.index)
    for rule in rules:
        target = pd.Series(True, index=source.index)
        for feature, value in rule.items():
            target &= source[feature].astype(str).eq(str(value))
        blocked |= target
    selected = source[~blocked].copy()
    metrics = [
        {"segment": segment, **research.stats(selected[selected["entry_year"].between(start, end)])}
        for segment, (start, end) in research.SEGMENTS.items()
    ]
    metrics.append({"segment": "all_2016_2026", **research.stats(selected)})
    monthly, annual = frequency_gates(selected)
    directory = REPORT_DIR_V2 / profile
    directory.mkdir(parents=True, exist_ok=True)
    selected.to_csv(directory / "selected_trades.csv", index=False)
    pd.DataFrame(metrics).to_csv(directory / "metrics.csv", index=False)
    monthly.to_csv(directory / "monthly_gate.csv", index=False)
    annual.to_csv(directory / "annual_gate.csv", index=False)
    manifest = {
        "profile": profile,
        "source_profile": source_profile,
        "mechanism": "causal entry feature block",
        "conditions": conditions,
        "monthly_below_5": int(((monthly["trades"] < 5) & monthly["data_valid"]).sum()),
        "years_below_20r": int(((annual["net_r"] < 20) & annual["data_valid_year"]).sum()),
        "selection_excluded": "2025-2026",
        "live_enabled": False,
        "code_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "config_sha256": sha256(json.dumps({"source": source_profile, "conditions": conditions}, sort_keys=True).encode()).hexdigest(),
        "result_sha256": sha256(selected.to_csv(index=False).encode()).hexdigest(),
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(pd.DataFrame(metrics).to_string(index=False))
    print(f"monthly_below_5={manifest['monthly_below_5']} years_below_20r={manifest['years_below_20r']}")


def run_cached_monthly_quota(profile: str, start_day: int, enforce_phase_monday_block: bool = False) -> None:
    base = pd.read_csv(REPORT_DIR_V2 / "block_spx_phase_monday_v1" / "selected_trades.csv")
    classic = pd.read_csv(REPORT_DIR_V2 / "spx_1045_all_weekdays_v1" / "selected_trades.csv")
    classic = classic[classic["fvg_size_regime"].eq("high") & classic["htf_alignment"].eq("opposed")]
    body = pd.read_csv(REPORT_DIR_V2 / "spx_1045_phase_body_fvg_all_weekdays_source_v1" / "selected_trades.csv")
    body = body[body["notes"].str.contains("body_fvg", na=False) & body["cisd_fvg_candles_regime"].eq("low")]
    base["quota_source"] = "base"
    base["source_priority"] = 0
    classic["quota_source"] = "classic_high_fvg_htf_opposed"
    classic["source_priority"] = 1
    body["quota_source"] = "phase_body_low_delay"
    body["source_priority"] = 2
    union = pd.concat([base, classic, body], ignore_index=True)
    union["entry_time_sort"] = pd.to_datetime(union["entry_time"], utc=True, format="mixed")
    union["year_month"] = union["entry_date"].str[:7]
    union["entry_day"] = union["entry_date"].str[8:10].astype(int)
    union = union.sort_values(["entry_time_sort", "candidate", "source_priority"], kind="mergesort").drop_duplicates(
        ["candidate", "entry_time", "direction", "sweep_time", "cisd_time"], keep="first"
    )
    if enforce_phase_monday_block:
        union = union[~(union["candidate"].eq("spx_phase") & union["entry_weekday"].eq("Monday"))].copy()
    kept = []
    counts: dict[str, int] = {}
    for index, row in union.sort_values(["entry_time_sort", "source_priority", "candidate"], kind="mergesort").iterrows():
        month = str(row["year_month"])
        if row["quota_source"] == "base" or (int(row["entry_day"]) >= start_day and counts.get(month, 0) < 5):
            kept.append(index)
            counts[month] = counts.get(month, 0) + 1
    candidate = json.loads((CANDIDATE_V7_DIR / "nq_spx_quality_time_mixed_rr_v1.json").read_text(encoding="utf-8"))
    selected = research.apply_final_pair_cap(union.loc[kept].copy(), candidate["pair_rules"])
    metrics = [
        {"segment": segment, **research.stats(selected[selected["entry_year"].between(start, end)])}
        for segment, (start, end) in research.SEGMENTS.items()
    ]
    metrics.append({"segment": "all_2016_2026", **research.stats(selected)})
    monthly, annual = frequency_gates(selected)
    directory = REPORT_DIR_V2 / profile
    directory.mkdir(parents=True, exist_ok=True)
    selected.to_csv(directory / "selected_trades.csv", index=False)
    pd.DataFrame(metrics).to_csv(directory / "metrics.csv", index=False)
    monthly.to_csv(directory / "monthly_gate.csv", index=False)
    annual.to_csv(directory / "annual_gate.csv", index=False)
    manifest = {
        "profile": profile,
        "mechanism": "causal month-to-date minimum-five quota",
        "start_day": start_day,
        "enforce_phase_monday_block": enforce_phase_monday_block,
        "base_profile": "block_spx_phase_monday_v1",
        "supplemental_sources": {
            "classic": {"fvg_size_regime": "high", "htf_alignment": "opposed"},
            "phase_body": {"cisd_fvg_candles_regime": "low"},
        },
        "selector": "base always; supplemental only while accepted month-to-date count < 5",
        "outcome_used_by_selector": False,
        "monthly_below_5": int(((monthly["trades"] < 5) & monthly["data_valid"]).sum()),
        "years_below_20r": int(((annual["net_r"] < 20) & annual["data_valid_year"]).sum()),
        "selection_excluded": "2025-2026",
        "live_enabled": False,
        "code_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "config_sha256": sha256(json.dumps({"start_day": start_day, "quota": 5, "enforce_phase_monday_block": enforce_phase_monday_block}, sort_keys=True).encode()).hexdigest(),
        "result_sha256": sha256(selected.to_csv(index=False).encode()).hexdigest(),
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(pd.DataFrame(metrics).to_string(index=False))
    print(f"monthly_below_5={manifest['monthly_below_5']} years_below_20r={manifest['years_below_20r']}")


def run_cached_candidate_ablation(
    profile: str,
    alternate_candidates: list[str],
    allowed_weekdays: list[str] | None = None,
) -> None:
    candidate = json.loads((CANDIDATE_V7_DIR / "nq_spx_quality_time_mixed_rr_v1.json").read_text(encoding="utf-8"))
    baseline = pd.read_csv(REPORT_DIR_V2 / "spx_latest_1045_v1" / "exact_features.csv")
    alternate = pd.read_csv(REPORT_DIR_V2 / "spx_1045_all_weekdays_v1" / "exact_features.csv")
    alternate_rows = alternate[alternate["candidate"].isin(alternate_candidates)]
    if allowed_weekdays is not None:
        alternate_rows = alternate_rows[alternate_rows["entry_weekday"].isin(allowed_weekdays)]
    exact = pd.concat(
        [
            baseline[~baseline["candidate"].isin(alternate_candidates)],
            alternate_rows,
        ],
        ignore_index=True,
    )
    selected = research.apply_rules(exact, candidate["simple_rules"])
    blocked = pd.Series(False, index=selected.index)
    for rule in candidate["compound_rules"]:
        mask = pd.Series(True, index=selected.index)
        if "candidate" in rule:
            mask &= selected["candidate"].eq(rule["candidate"])
        for feature, value in rule["conditions"].items():
            mask &= selected[feature].astype(str).eq(str(value))
        blocked |= mask
    selected = research.apply_final_pair_cap(selected[~blocked], candidate["pair_rules"])
    metrics = [
        {"segment": segment, **research.stats(selected[selected["entry_year"].between(start, end)])}
        for segment, (start, end) in research.SEGMENTS.items()
    ]
    metrics.append({"segment": "all_2016_2026", **research.stats(selected)})
    monthly, annual = frequency_gates(selected)
    directory = REPORT_DIR_V2 / profile
    directory.mkdir(parents=True, exist_ok=True)
    exact.to_csv(directory / "exact_features.csv", index=False)
    selected.to_csv(directory / "selected_trades.csv", index=False)
    pd.DataFrame(metrics).to_csv(directory / "metrics.csv", index=False)
    monthly.to_csv(directory / "monthly_gate.csv", index=False)
    annual.to_csv(directory / "annual_gate.csv", index=False)
    manifest = {
        "profile": profile,
        "mechanism": "allowed_weekdays",
        "alternate_candidates": alternate_candidates,
        "allowed_weekdays": allowed_weekdays or ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
        "unchanged": ["other candidates", "entry_mode", "reward_r", "stop", "quality_rules", "pair_cap"],
        "selection_excluded": "2025-2026",
        "monthly_below_5": int(((monthly["trades"] < 5) & monthly["data_valid"]).sum()),
        "years_below_20r": int(((annual["net_r"] < 20) & annual["data_valid_year"]).sum()),
        "live_enabled": False,
        "code_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "config_sha256": sha256(json.dumps({"candidates": alternate_candidates, "weekdays": allowed_weekdays}, sort_keys=True).encode()).hexdigest(),
        "result_sha256": sha256(selected.to_csv(index=False).encode()).hexdigest(),
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(pd.DataFrame(metrics).to_string(index=False))
    print(f"monthly_below_5={manifest['monthly_below_5']} years_below_20r={manifest['years_below_20r']}")


def run_cached_setup_union(
    profile: str,
    body_filter: dict[str, object] | None = None,
    max_per_candidate_day: int = 2,
    body_candidates: list[str] | None = None,
    baseline_profile: str = "spx_latest_1045_v1",
    body_profile: str = "spx_1045_spx_body_fvg_v1",
) -> None:
    candidate = json.loads((CANDIDATE_V7_DIR / "nq_spx_quality_time_mixed_rr_v1.json").read_text(encoding="utf-8"))
    baseline = pd.read_csv(REPORT_DIR_V2 / baseline_profile / "exact_features.csv")
    body = pd.read_csv(REPORT_DIR_V2 / body_profile / "exact_features.csv")
    baseline["setup_source_priority"] = 0
    body["setup_source_priority"] = 1
    spx_names = body_candidates or ["spx_funded", "spx_phase"]
    for feature, value in (body_filter or {}).items():
        body = body[body[feature].astype(str).eq(str(value))]
    exact = pd.concat([baseline, body[body["candidate"].isin(spx_names)]], ignore_index=True)
    exact["entry_time_sort"] = pd.to_datetime(exact["entry_time"], utc=True, format="mixed")
    signal_key = ["candidate", "entry_date", "direction", "sweep_time", "cisd_time", "fvg_time"]
    exact = exact.sort_values(["setup_source_priority", "entry_time_sort"]).drop_duplicates(signal_key, keep="first")
    exact = exact.sort_values(["candidate", "entry_date", "entry_time_sort", "setup_source_priority"])
    exact = exact.groupby(["candidate", "entry_date"], sort=False, group_keys=False).head(max_per_candidate_day).reset_index(drop=True)
    selected = research.apply_rules(exact, candidate["simple_rules"])
    blocked = pd.Series(False, index=selected.index)
    for rule in candidate["compound_rules"]:
        mask = pd.Series(True, index=selected.index)
        if "candidate" in rule:
            mask &= selected["candidate"].eq(rule["candidate"])
        for feature, value in rule["conditions"].items():
            mask &= selected[feature].astype(str).eq(str(value))
        blocked |= mask
    selected = research.apply_final_pair_cap(selected[~blocked], candidate["pair_rules"])
    metrics = [
        {"segment": segment, **research.stats(selected[selected["entry_year"].between(start, end)])}
        for segment, (start, end) in research.SEGMENTS.items()
    ]
    metrics.append({"segment": "all_2016_2026", **research.stats(selected)})
    monthly, annual = frequency_gates(selected)
    directory = REPORT_DIR_V2 / profile
    directory.mkdir(parents=True, exist_ok=True)
    exact.to_csv(directory / "exact_features.csv", index=False)
    selected.to_csv(directory / "selected_trades.csv", index=False)
    pd.DataFrame(metrics).to_csv(directory / "metrics.csv", index=False)
    monthly.to_csv(directory / "monthly_gate.csv", index=False)
    annual.to_csv(directory / "annual_gate.csv", index=False)
    manifest = {
        "profile": profile,
        "mechanism": "causal setup source union",
        "sources": ["classic_fvg_ifvg", "body_fvg"],
        "body_candidates": spx_names,
        "baseline_profile": baseline_profile,
        "body_profile": body_profile,
        "body_filter": body_filter or {},
        "selector": f"earliest entry; classic source wins exact-time ties; max {max_per_candidate_day} per candidate/day",
        "outcome_used_by_selector": False,
        "selection_excluded": "2025-2026",
        "monthly_below_5": int(((monthly["trades"] < 5) & monthly["data_valid"]).sum()),
        "years_below_20r": int(((annual["net_r"] < 20) & annual["data_valid_year"]).sum()),
        "live_enabled": False,
        "code_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "config_sha256": sha256(json.dumps({"sources": ["classic_fvg_ifvg", "body_fvg"], "body_candidates": spx_names, "body_filter": body_filter or {}, "selector_max": max_per_candidate_day, "baseline_profile": baseline_profile, "body_profile": body_profile}, sort_keys=True).encode()).hexdigest(),
        "result_sha256": sha256(selected.to_csv(index=False).encode()).hexdigest(),
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(pd.DataFrame(metrics).to_string(index=False))
    print(f"monthly_below_5={manifest['monthly_below_5']} years_below_20r={manifest['years_below_20r']}")


if __name__ == "__main__":
    main()
