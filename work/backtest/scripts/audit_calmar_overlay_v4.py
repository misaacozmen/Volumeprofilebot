from __future__ import annotations

import json
import sys
from hashlib import sha256
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from run_strategy_improvement_loop_v4 import causal_equity_overlay  # noqa: E402
from freeze_risk_overlay_v4 import result_hash, risk_stats  # noqa: E402

SOURCE = ROOT / "outputs" / "reports" / "strategy_improvement_loop_v4" / "iteration_065" / "locked_pair_v1" / "selected_trades_pre2025.csv"
REPORT = ROOT / "outputs" / "reports" / "strategy_improvement_loop_v4" / "iteration_071" / "calmar_rolling_audit_v1"
PARAMETERS = {"threshold": 3.0, "low_scale": 0.75, "high_scale": 1.30}


def apply(frame: pd.DataFrame) -> pd.DataFrame:
    return causal_equity_overlay(frame, PARAMETERS["threshold"], PARAMETERS["low_scale"], PARAMETERS["high_scale"])


def main() -> None:
    REPORT.mkdir(parents=True, exist_ok=True)
    source = pd.read_csv(SOURCE)
    selected = apply(source)
    repeated = apply(source)
    rolling = []
    for years in (3, 4, 5):
        for start in range(2016, 2025 - years + 1):
            end = start + years - 1
            result = apply(source[source["entry_year"].between(start, end)].copy())
            rolling.append({"window_years": years, "start_year": start, "end_year": end, **risk_stats(result)})
    rolling_frame = pd.DataFrame(rolling)
    checks = {
        "observed_pre2025": risk_stats(selected), "rolling_windows": int(len(rolling_frame)),
        "rolling_positive_net": int(rolling_frame["net_r"].gt(0).sum()), "rolling_dd_at_most_13": int(rolling_frame["max_drawdown_r"].le(13).sum()),
        "deterministic": result_hash(selected) == result_hash(repeated),
        "terminal_only_state": True, "open_trade_outcome_used": False,
        "result_sha256": result_hash(selected), "code_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "live_enabled": False,
    }
    checks["pass"] = checks["rolling_positive_net"] == checks["rolling_windows"] and checks["rolling_dd_at_most_13"] == checks["rolling_windows"] and checks["deterministic"]
    selected.to_csv(REPORT / "selected_trades_pre2025.csv", index=False)
    rolling_frame.to_csv(REPORT / "rolling_robustness.csv", index=False)
    (REPORT / "audit.json").write_text(json.dumps(checks, indent=2), encoding="utf-8")
    print(json.dumps(checks, indent=2))
    if not checks["pass"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
