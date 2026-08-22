from __future__ import annotations

from pathlib import Path
import sys

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import run_engine_path_comparison_2025_feb_mar as comparison
import run_main_candidate_filter_tests as filters
from backtest.strategy import compute_first30_range


REPORT_DIR = ROOT / "outputs" / "reports" / "selector_calibration_leakage_audit"


def main() -> None:
    loaded = filters.load_data()
    configs = comparison.build_active_configs(loaded)
    frozen_rows = {
        (row["symbol"], row["timeframe"]): row for row in filters.build_thresholds()
    }
    rows = []
    for leg_key, source in comparison.SYMBOLS.items():
        frame = loaded[source]
        values = []
        for trade_date in sorted(frame["date"].unique()):
            value = compute_first30_range(frame, trade_date)
            if value is not None:
                values.append({"date": pd.Timestamp(trade_date), "value": float(value)})
        samples = pd.DataFrame(values)
        pre_2025 = samples[samples["date"] < pd.Timestamp("2025-01-01")]
        frozen = frozen_rows[source]
        pre_2025_q60 = round(float(pre_2025["value"].quantile(0.60)), 4)
        active_threshold = float(configs[leg_key].first30_range_max)
        rows.append(
            {
                "leg_key": leg_key,
                "active_threshold": active_threshold,
                "full_data_q60": round(float(samples["value"].quantile(0.60)), 4),
                "pre_2025_q60": pre_2025_q60,
                "threshold_minus_pre_2025_q60": round(
                    active_threshold - pre_2025_q60, 4
                ),
                "full_sample_days": len(samples),
                "pre_2025_days": len(pre_2025),
                "artifact_id": frozen["artifact_id"],
                "artifact_sha256": frozen["artifact_sha256"],
                "cutoff_exclusive": frozen["cutoff_exclusive"],
                "calibration_status": (
                    "PASS_PRE_HOLDOUT_ARTIFACT"
                    if active_threshold == pre_2025_q60
                    else "FAIL_ACTIVE_THRESHOLD_MISMATCH"
                ),
            }
        )
    result = pd.DataFrame(rows)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    result.to_csv(REPORT_DIR / "threshold_comparison.csv", index=False)
    lines = [
        "# Selector calibration leakage audit",
        "",
        "Active first-30 thresholds are loaded from a checksummed artifact calibrated with dates strictly before 2025-01-01.",
        "",
        "```text",
        result.to_string(index=False),
        "```",
        "",
        "The full-data q60 column is diagnostic only and is never used by the active configuration or candidate ranking.",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(result.to_string(index=False))


if __name__ == "__main__":
    main()
