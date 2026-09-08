from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from hashlib import sha256
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
from backtest.engine_pipeline import EngineLeg, run_canonical_pair_pipeline, source_code_hash, stable_frame_hash
from backtest.manual_state import ManualStateConfig


DEFAULT_REPORT_DIR = ROOT / "outputs" / "reports" / "canonical_state_ablations_research"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="One-factor canonical state-engine research ablations.")
    parser.add_argument("--start", default="2025-02-01")
    parser.add_argument("--end", default="2025-03-31")
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    return parser.parse_args()


def variants() -> dict[str, ManualStateConfig]:
    baseline = ManualStateConfig()
    return {
        "baseline": baseline,
        "liquidity_no_va_flip": replace(baseline, enable_va_flip_gate=False),
        "cisd_raw": replace(baseline, cisd_qualification_mode="raw"),
        "pd_array_detection_off": replace(baseline, enable_htf_order_blocks=False),
        "htf_optional_authority_off": replace(baseline, enable_htf_optional_gate=False),
        "thesis_opposite_cisd_reversal": replace(baseline, allow_opposite_cisd_reversal_without_context=True),
        "target_before_fill_explicit_only": replace(baseline, target_before_fill_mode="explicit_only"),
        "ny_htf_reaction_authority": replace(baseline, allow_ny_htf_reaction_gate=True),
    }


def run_variant(
    name: str,
    state_config: ManualStateConfig,
    loaded: dict[tuple[str, str], pd.DataFrame],
    configs: dict[str, object],
    start: pd.Timestamp,
    end: pd.Timestamp,
    report_dir: Path,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, object]]:
    variant_dir = report_dir / name
    variant_dir.mkdir(parents=True, exist_ok=True)
    dates = signed_market_dates(start, end)
    legs = [
        EngineLeg(key, full_history.period_frame(loaded[source], start, end), configs[key])
        for key, source in comparison.SYMBOLS.items()
    ]
    contract_payload = {
        "name": name,
        "start": start.strftime("%Y-%m-%d"),
        "end": end.strftime("%Y-%m-%d"),
        "state": asdict(state_config),
        "configs": {key: asdict(value) for key, value in configs.items()},
        "code_hash": source_code_hash(),
        "data_hashes": {leg.key: full_history.frame_hash(leg.frame) for leg in legs},
    }
    contract = sha256(json.dumps(contract_payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    manifest_path = variant_dir / "manifest.json"
    decisions_path = variant_dir / "decisions.csv"
    filled_path = variant_dir / "filled.csv"
    if manifest_path.exists() and decisions_path.exists() and filled_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("contract_hash") == contract:
            print(f"{name}: verified checkpoint", flush=True)
            return pd.read_csv(decisions_path), pd.read_csv(filled_path), manifest
    started = time.perf_counter()
    result = run_canonical_pair_pipeline(legs, dates, state_config=state_config)
    result.decisions.to_csv(decisions_path, index=False)
    result.filled_after_pair_cap.to_csv(filled_path, index=False)
    manifest = {
        **contract_payload,
        "contract_hash": contract,
        "result_hash": stable_frame_hash(result.decisions),
        "runtime_seconds": round(time.perf_counter() - started, 3),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(f"{name}: {len(result.decisions)} decisions, {len(result.filled_after_pair_cap)} fills, {manifest['runtime_seconds']}s", flush=True)
    return result.decisions, result.filled_after_pair_cap, manifest


def variant_summary(
    name: str,
    decisions: pd.DataFrame,
    filled: pd.DataFrame,
    baseline_decisions: pd.DataFrame | None,
    baseline_filled: pd.DataFrame | None,
) -> dict[str, object]:
    summary = full_history.performance_summary(filled, decisions, pd.DataFrame())
    baseline_ids = set() if baseline_filled is None or baseline_filled.empty else set(baseline_filled["order_id"])
    current_ids = set() if filled.empty else set(filled["order_id"])
    dates = pd.to_datetime(filled["date"]) if not filled.empty else pd.Series(dtype="datetime64[ns]")
    development = filled[dates < pd.Timestamp("2025-01-01")] if not filled.empty else filled
    holdout = filled[dates >= pd.Timestamp("2025-01-01")] if not filled.empty else filled
    development_r = float(pd.to_numeric(development.get("r_multiple", pd.Series(dtype=float)), errors="coerce").sum())
    holdout_r = float(pd.to_numeric(holdout.get("r_multiple", pd.Series(dtype=float)), errors="coerce").sum())
    temporal_gate = len(development) >= 50 and len(holdout) >= 50 and development_r > 0 and holdout_r > 0
    return {
        "variant": name,
        **summary,
        "result_identical_to_baseline": (
            baseline_decisions is not None
            and stable_frame_hash(decisions) == stable_frame_hash(baseline_decisions)
        ),
        "fills_shared_with_baseline": len(current_ids & baseline_ids),
        "fills_added_vs_baseline": len(current_ids - baseline_ids),
        "fills_removed_vs_baseline": len(baseline_ids - current_ids),
        "development_fills_pre_2025": len(development),
        "development_net_r": round(development_r, 3),
        "holdout_fills_2025_plus": len(holdout),
        "holdout_net_r": round(holdout_r, 3),
        "promotion_sample_gate": len(filled) >= 100,
        "promotion_temporal_gate": temporal_gate,
        "promotion_ready": len(filled) >= 100 and temporal_gate,
    }


def main() -> None:
    args = parse_args()
    start = pd.Timestamp(args.start).normalize()
    end = pd.Timestamp(args.end).normalize()
    if end < start:
        raise ValueError("End date precedes start date.")
    report_dir = args.report_dir.resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    loaded = filters.load_data()
    configs = comparison.build_active_configs(loaded)
    runs: dict[str, tuple[pd.DataFrame, pd.DataFrame, dict[str, object]]] = {}
    for name, state_config in variants().items():
        runs[name] = run_variant(name, state_config, loaded, configs, start, end, report_dir)
    baseline_filled = runs["baseline"][1]
    baseline_decisions = runs["baseline"][0]
    summary = pd.DataFrame(
        [
            variant_summary(name, decisions, filled, baseline_decisions, baseline_filled)
            for name, (decisions, filled, _) in runs.items()
        ]
    )
    summary.to_csv(report_dir / "summary.csv", index=False)
    lines = [
        "# Canonical state-engine one-factor ablations",
        "",
        f"Period: `{start.date()}` through `{end.date()}`. Offline research only; no live promotion.",
        "",
        "Each row changes one state-engine authority/lifecycle rule. Promotion requires at least 100 post-cap fills plus at least 50 positive-net-R fills on both pre-2025 development and 2025+ holdout; deterministic XM machine-replay gates are additional and are not evaluated here.",
        "",
        "```text",
        summary.to_string(index=False),
        "```",
    ]
    (report_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(summary.to_string(index=False), flush=True)
    print(f"Wrote: {report_dir}", flush=True)


if __name__ == "__main__":
    main()
