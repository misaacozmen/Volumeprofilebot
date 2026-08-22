from __future__ import annotations

import run_manual_regression_harness as harness


def main() -> None:
    harness.CASES_FILE = harness.ROOT / "calibration_examples" / "manual_feedback_regression_v3.csv"
    harness.REPORT_DIR = harness.ROOT / "outputs" / "reports" / "manual_feedback_regression_v3"
    harness.EXTRA_PROFILES = {"challenge_core_v2_htf_requalification"}
    harness.main()


if __name__ == "__main__":
    main()
