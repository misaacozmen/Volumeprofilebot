from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from candidate_artifact import (  # noqa: E402
    build_provenance,
    promote_directories_transactional,
    seal_artifact,
    write_json_atomic,
)
from evaluate_conservative_rolling_candidate_v4 import apply_payload  # noqa: E402
from freeze_conservative_rolling_state_v4 import PARAMETERS  # noqa: E402
from freeze_risk_overlay_v4 import result_hash, risk_stats  # noqa: E402
from freeze_strategy_improvement_loop_v4 import IDENTITY, select  # noqa: E402


SOURCE = ROOT / "outputs" / "reports" / "strategy_improvement_loop_v2" / "body_plus_classic_quota_day1_v1" / "selected_trades.csv"
EVIDENCE = ROOT / "outputs" / "reports" / "strategy_improvement_loop_v2" / "body_plus_classic_quota_day1_v1" / "audit" / "trade_evidence_index.csv"
PRE_SELECTED = ROOT / "outputs" / "reports" / "strategy_improvement_loop_v4" / "iteration_096" / "locked_conservative_rolling_v1" / "selected_trades_pre2025.csv"
FINAL = ROOT / "outputs" / "reports" / "strategy_improvement_loop_v4" / "final_success_iteration_098"
CANDIDATE_DIR = ROOT / "research_candidates" / "v20_strategy_loop"
CANDIDATE_NAME = "nq_spx_local_fresh_forward_candidate_v1.json"
SEGMENTS = {
    "learn_2016_2021": (2016, 2021),
    "validation_2022_2023": (2022, 2023),
    "validation_2024": (2024, 2024),
    "final_holdout_2025_2026": (2025, 2026),
    "all_2016_2026": (2016, 2026),
}


def run_project_tests() -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    summary_lines = [
        line.strip()
        for line in (completed.stdout + "\n" + completed.stderr).splitlines()
        if line.strip()
    ]
    return {
        "command": f"{sys.executable} -m pytest -q",
        "exit_code": completed.returncode,
        "passed": completed.returncode == 0,
        "summary": summary_lines[-1] if summary_lines else "no output",
    }


def build_candidate() -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    source = pd.read_csv(SOURCE)
    first = apply_payload(
        source,
        {
            "setup_rules": [
                {"action": "BLOCK", "conditions": {"entry_weekday": "Monday", "direction": "short"}},
                {"action": "BLOCK", "conditions": {"liquidity_type": "vah_val_proximity", "overnight_direction": "up"}},
            ],
            "risk_rule": PARAMETERS,
        },
    )
    second = apply_payload(
        source,
        {
            "setup_rules": [
                {"action": "BLOCK", "conditions": {"entry_weekday": "Monday", "direction": "short"}},
                {"action": "BLOCK", "conditions": {"liquidity_type": "vah_val_proximity", "overnight_direction": "up"}},
            ],
            "risk_rule": PARAMETERS,
        },
    )
    deterministic = result_hash(first) == result_hash(second)
    development = first[first["entry_year"].le(2024)].copy()
    prefix_pass = result_hash(development) == result_hash(pd.read_csv(PRE_SELECTED))
    evidence = pd.read_csv(EVIDENCE)
    keys = set(first[IDENTITY].astype(str).agg("|".join, axis=1))
    evidence_subset = evidence[
        evidence[IDENTITY].astype(str).agg("|".join, axis=1).isin(keys)
    ].copy()
    graph_pass = len(evidence_subset) == len(first) and bool(
        evidence_subset["all_event_bars_found"].all()
    )
    data_invalid_excluded = bool(first["data_valid"].astype(str).str.lower().eq("true").all())
    metrics = pd.DataFrame(
        [
            {
                "segment": name,
                **risk_stats(first[first["entry_year"].between(start, end)]),
            }
            for name, (start, end) in SEGMENTS.items()
        ]
    )
    development_observed = risk_stats(development)
    development_source = source[source["entry_year"].le(2024)]
    criteria = {
        "development_win_rate_40_45": bool(40 <= development_observed["win_rate"] <= 45),
        "development_net_r_above_263_baseline": bool(development_observed["net_r"] > 263),
        "development_max_drawdown_9_13": bool(
            9 <= development_observed["max_drawdown_r"] <= 13
        ),
        "development_retention_at_least_70pct": bool(
            len(development) / len(development_source) >= 0.70
        ),
    }
    rolling = []
    for years in (3, 4, 5):
        for start in range(2016, 2025 - years + 1):
            end = start + years - 1
            window = source[source["entry_year"].between(start, end)].copy()
            rolling.append(
                {
                    "window_years": years,
                    "start_year": start,
                    "end_year": end,
                    **risk_stats(apply_payload(window, {"setup_rules": [
                        {"action": "BLOCK", "conditions": {"entry_weekday": "Monday", "direction": "short"}},
                        {"action": "BLOCK", "conditions": {"liquidity_type": "vah_val_proximity", "overnight_direction": "up"}},
                    ], "risk_rule": PARAMETERS})),
                }
            )
    rolling_frame = pd.DataFrame(rolling)
    checks = {
        "criteria_pass": all(criteria.values()),
        "deterministic": deterministic,
        "development_prefix_matches_frozen_result": prefix_pass,
        "graph_evidence_pass": graph_pass,
        "data_invalid_excluded": data_invalid_excluded,
        "rolling_windows": int(len(rolling_frame)),
        "rolling_all_positive": bool(rolling_frame["net_r"].gt(0).all()),
        "rolling_all_dd_at_most_13": bool(rolling_frame["max_drawdown_r"].le(13).all()),
    }
    payload = seal_artifact(
        {
            "artifact_type": "strategy_candidate",
            "artifact_id": "nq_spx_local_fresh_forward_candidate_v1",
            "name": "NQ_SPX_LOCAL_FRESH_FORWARD_CANDIDATE_V1",
            "status": "LOCAL_FRESH_FORWARD_CANDIDATE_UNPROVEN",
            "setup_rules": [
                {"action": "BLOCK", "conditions": {"entry_weekday": "Monday", "direction": "short"}},
                {"action": "BLOCK", "conditions": {"liquidity_type": "vah_val_proximity", "overnight_direction": "up"}},
            ],
            "risk_rule": dict(PARAMETERS),
            "selection_protocol": {
                "development_period": "2016-2024",
                "final_holdout_period": "2025-2026",
                "final_holdout_used_for_selection": False,
                "final_holdout_used_for_publication": False,
            },
            "development_result_sha256": result_hash(development),
            "full_evaluation_result_sha256": result_hash(first),
            "criteria": criteria,
            "checks": checks,
            "live_enabled": False,
            "fresh_forward_required": True,
            "proven": False,
            "provenance": build_provenance(
                ROOT,
                [
                    (SOURCE, "research_dataset"),
                    (EVIDENCE, "trade_evidence"),
                    (PRE_SELECTED, "frozen_development_result"),
                    (Path(__file__), "finalizer_code"),
                    (ROOT / "scripts" / "candidate_artifact.py", "artifact_code"),
                    (ROOT / "scripts" / "evaluate_conservative_rolling_candidate_v4.py", "evaluator_code"),
                    (ROOT / "scripts" / "run_strategy_improvement_loop_v4.py", "strategy_code"),
                    (ROOT / "scripts" / "freeze_strategy_improvement_loop_v4.py", "selector_code"),
                    (ROOT / "scripts" / "freeze_conservative_rolling_state_v4.py", "risk_config_code"),
                    (ROOT / "scripts" / "freeze_risk_overlay_v4.py", "metric_code"),
                ],
                dependencies=("pandas", "pytest"),
            ),
        }
    )
    return payload, first, metrics, rolling_frame


def validate_publication_contract(payload: dict[str, Any], evaluated: pd.DataFrame) -> None:
    protocol = payload.get("selection_protocol")
    if not isinstance(protocol, dict):
        raise ValueError("candidate selection protocol is missing")
    if protocol.get("final_holdout_used_for_selection") is not False:
        raise ValueError("final holdout was used for selection")
    if protocol.get("final_holdout_used_for_publication") is not False:
        raise ValueError("final holdout was used for publication")
    criteria = payload.get("criteria")
    if not isinstance(criteria, dict) or any("holdout" in str(key).lower() for key in criteria):
        raise ValueError("publication criteria contain a holdout metric")
    if "entry_year" not in evaluated:
        raise ValueError("candidate evaluation has no entry_year column")
    development = evaluated[evaluated["entry_year"].le(2024)]
    if development.empty or not bool(evaluated["entry_year"].gt(2024).any()):
        raise ValueError("candidate evaluation does not contain development and final holdout data")
    if payload.get("development_result_sha256") != result_hash(development):
        raise ValueError("development result hash is not bound to the published candidate")
    if payload.get("full_evaluation_result_sha256") != result_hash(evaluated):
        raise ValueError("full evaluation result hash is not bound to the published candidate")


def main() -> None:
    payload, selected, metrics, rolling = build_candidate()
    validate_publication_contract(payload, selected)
    test_result = run_project_tests()
    gates_pass = all(
        [
            bool(payload["checks"]["criteria_pass"]),
            bool(payload["checks"]["deterministic"]),
            bool(payload["checks"]["development_prefix_matches_frozen_result"]),
            bool(payload["checks"]["graph_evidence_pass"]),
            bool(payload["checks"]["data_invalid_excluded"]),
            bool(payload["checks"]["rolling_all_positive"]),
            bool(payload["checks"]["rolling_all_dd_at_most_13"]),
            bool(test_result["passed"]),
        ]
    )
    if not gates_pass:
        print(json.dumps({"published": False, "checks": payload["checks"], "project_tests": test_result}, indent=2))
        raise SystemExit(1)

    staging_root = Path(tempfile.mkdtemp(prefix=".v20-staging-", dir=CANDIDATE_DIR.parent))
    candidate_staging = staging_root / CANDIDATE_DIR.name
    report_staging = Path(tempfile.mkdtemp(prefix=".final-report-staging-", dir=FINAL.parent))
    try:
        candidate_staging.mkdir()
        write_json_atomic(candidate_staging / CANDIDATE_NAME, payload)
        selected.to_csv(report_staging / "selected_trades.csv", index=False)
        metrics.to_csv(report_staging / "segment_metrics.csv", index=False)
        rolling.to_csv(report_staging / "rolling_robustness.csv", index=False)
        manifest = {
            "candidate": f"research_candidates/v20_strategy_loop/{CANDIDATE_NAME}",
            "candidate_artifact_sha256": payload["artifact_sha256"],
            "status": payload["status"],
            "checks": payload["checks"],
            "project_tests": test_result,
            "live_enabled": False,
            "proven": False,
        }
        write_json_atomic(report_staging / "manifest.json", manifest)
        (report_staging / "report.md").write_text(
            report(metrics, payload, test_result), encoding="utf-8"
        )
        promote_directories_transactional(
            ((candidate_staging, CANDIDATE_DIR), (report_staging, FINAL))
        )
    finally:
        if staging_root.exists():
            shutil.rmtree(staging_root)
        if report_staging.exists():
            shutil.rmtree(report_staging)
    print(
        json.dumps(
            {
                "published": True,
                "candidate": f"research_candidates/v20_strategy_loop/{CANDIDATE_NAME}",
                "artifact_sha256": payload["artifact_sha256"],
                "project_tests": test_result,
            },
            indent=2,
        )
    )


def report(metrics: pd.DataFrame, payload: dict[str, Any], tests: dict[str, Any]) -> str:
    return f"""# Strategy improvement candidate

All required gates passed before atomic publication. The candidate remains local and unproven; live use is disabled.

- Selection: development data through 2024 only.
- Final holdout: 2025-2026, evaluated after selection.
- Project tests: `{tests['summary']}`.
- Artifact SHA-256: `{payload['artifact_sha256']}`.

```csv
{metrics.to_csv(index=False).strip()}
```
"""


if __name__ == "__main__":
    main()
