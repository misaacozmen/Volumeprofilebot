from __future__ import annotations

from pathlib import Path

import run_corrected_engine_3month_report as report


ROOT = Path(__file__).resolve().parents[1]

report.REPORT_DIR = ROOT / "outputs" / "reports" / "corrected_engine_6month"
report.SEED = 20260705
report.SELECTED_MONTH_COUNT = 6


if __name__ == "__main__":
    report.main()
