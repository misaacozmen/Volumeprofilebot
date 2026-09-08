from __future__ import annotations

import argparse
from dataclasses import asdict
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
from backtest.manual_state import ManualStateConfig, build_independent_htf_frame
from backtest.state_audit import prefix_invariance_violations


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Independent determinism and sampled-prefix verification.")
    parser.add_argument("--report-dir", type=Path, default=full_history.DEFAULT_REPORT_DIR)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report_dir = args.report_dir.resolve()
    manifest = json.loads((report_dir / "run_manifest.json").read_text(encoding="utf-8"))
    start = pd.Timestamp(manifest["start"])
    end = pd.Timestamp(manifest["end"])
    loaded = filters.load_data()
    configs = comparison.build_active_configs(loaded)
    state_config = ManualStateConfig()
    rows = []
    all_prefix_violations = []
    for year in range(start.year, end.year + 1):
        year_start = max(start, pd.Timestamp(f"{year}-01-01"))
        year_end = min(end, pd.Timestamp(f"{year}-12-31"))
        dates = signed_market_dates(year_start, year_end)
        legs = [
            EngineLeg(key, full_history.period_frame(loaded[source], year_start, year_end), configs[key])
            for key, source in comparison.SYMBOLS.items()
        ]
        expected = json.loads(
            (report_dir / "checkpoints" / str(year) / "checkpoint.json").read_text(encoding="utf-8")
        )["result_hash"]
        started = time.perf_counter()
        result = run_canonical_pair_pipeline(legs, dates, state_config=state_config)
        actual = stable_frame_hash(result.decisions)
        filled_dates = {
            (row.leg_key, pd.Timestamp(row.date).date())
            for row in result.decisions.itertuples(index=False)
            if row.order_state == "FILLED"
        }
        prefix_checks = 0
        prefix_violations = 0
        for leg in legs:
            htf = build_independent_htf_frame(
                leg.frame,
                leg.config.timeframe,
                state_config.htf_timeframe_minutes,
            )
            leg_filled_dates = sorted(date for key, date in filled_dates if key == leg.key)
            sampled = set(dates[::40]) | set(leg_filled_dates[::20])
            cutoffs = sorted({"10:00", leg.config.trade_window_end})
            for trade_date in sorted(sampled):
                prefix_checks += len(cutoffs)
                violations = prefix_invariance_violations(
                    leg.frame,
                    leg.config,
                    trade_date,
                    cutoffs,
                    state_config,
                    htf_frame=htf,
                )
                prefix_violations += len(violations)
                all_prefix_violations.extend(
                    {"year": year, "leg_key": leg.key, **asdict(item)} for item in violations
                )
        row = {
            "year": year,
            "expected_result_hash": expected,
            "actual_result_hash": actual,
            "deterministic": expected == actual,
            "prefix_checks": prefix_checks,
            "prefix_violations": prefix_violations,
            "runtime_seconds": round(time.perf_counter() - started, 3),
        }
        rows.append(row)
        (report_dir / "verification_progress.json").write_text(
            json.dumps(rows, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"{year}: deterministic={row['deterministic']} prefix={prefix_violations}/{prefix_checks} runtime={row['runtime_seconds']}s", flush=True)
    verification = pd.DataFrame(rows)
    verification.to_csv(report_dir / "determinism_prefix_verification.csv", index=False)
    pd.DataFrame(all_prefix_violations).to_csv(report_dir / "prefix_violations.csv", index=False)
    summary = {
        "deterministic_all_years": bool(verification["deterministic"].all()),
        "prefix_checks": int(verification["prefix_checks"].sum()),
        "prefix_violations": int(verification["prefix_violations"].sum()),
        "passed": bool(verification["deterministic"].all() and verification["prefix_violations"].sum() == 0),
    }
    (report_dir / "verification_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
