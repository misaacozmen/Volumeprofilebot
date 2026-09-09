from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pandas as pd

from backtest.strategy import compute_first30_range
from candidate_artifact import (
    ArtifactValidationError,
    build_provenance,
    load_artifact,
    repo_relative,
    seal_artifact,
    write_json_atomic,
)


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "research_candidates" / "calibration" / "first30_thresholds_pre2025_v2.json"
CUTOFF_EXCLUSIVE = "2025-01-01"
QUANTILES = (0.60, 0.70)


def calibration_values(frame: pd.DataFrame, cutoff_exclusive: str = CUTOFF_EXCLUSIVE) -> list[float]:
    cutoff = pd.Timestamp(cutoff_exclusive).date()
    values: list[float] = []
    for trade_date in sorted(frame["date"].unique()):
        if trade_date >= cutoff:
            continue
        value = compute_first30_range(frame, trade_date)
        if value is not None:
            values.append(float(value))
    if not values:
        raise ArtifactValidationError("No pre-cutoff first-30 samples were available")
    return values


def create_artifact(
    loaded: dict[tuple[str, str], pd.DataFrame],
    source_paths: dict[tuple[str, str], Iterable[Path]],
    *,
    destination: Path = ARTIFACT,
) -> dict:
    rows = []
    raw_inputs: dict[Path, str] = {}
    for (symbol, timeframe), frame in sorted(loaded.items()):
        cutoff = pd.Timestamp(CUTOFF_EXCLUSIVE).date()
        referenced = set(
            frame.loc[frame["date"] < cutoff, "source_file"].dropna().astype(str)
        )
        paths = sorted(
            {
                path.resolve()
                for path in source_paths[(symbol, timeframe)]
                if str(path.resolve()) in referenced
            }
        )
        if not paths:
            raise ArtifactValidationError(f"No pre-cutoff source files found for {(symbol, timeframe)}")
        values = pd.Series(calibration_values(frame), dtype=float)
        for path in paths:
            raw_inputs[path] = "market_data"
        rows.append(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "sample_days": int(len(values)),
                "first30_q60": round(float(values.quantile(0.60)), 4),
                "first30_q70": round(float(values.quantile(0.70)), 4),
                "source_paths": [repo_relative(path, ROOT) for path in paths],
            }
        )
    code_inputs = {
        Path(__file__).resolve(): "calibration_code",
        (ROOT / "scripts" / "run_main_candidate_filter_tests.py").resolve(): "calibration_entrypoint",
        (ROOT / "scripts" / "candidate_artifact.py").resolve(): "artifact_code",
        (ROOT / "backtest" / "strategy.py").resolve(): "calculation_code",
        (ROOT / "backtest" / "data_loader.py").resolve(): "loader_code",
        (ROOT / "backtest" / "data_inspector.py").resolve(): "loader_dependency",
        (ROOT / "backtest" / "integrity.py").resolve(): "loader_dependency",
    }
    payload = seal_artifact(
        {
            "artifact_type": "first30_thresholds",
            "artifact_id": "first30_thresholds_pre2025_v2",
            "calibration": {
                "cutoff_exclusive": CUTOFF_EXCLUSIVE,
                "quantiles": list(QUANTILES),
                "method": "session_first30_range",
                "timezone": "America/New_York",
            },
            "thresholds": rows,
            "provenance": build_provenance(
                ROOT,
                [(path, role) for path, role in {**raw_inputs, **code_inputs}.items()],
                dependencies=("pandas",),
            ),
        }
    )
    write_json_atomic(destination, payload)
    return payload


def load_thresholds(
    *,
    artifact_path: Path = ARTIFACT,
    verify_sources: bool = True,
) -> list[dict[str, object]]:
    payload = load_artifact(
        artifact_path,
        ROOT,
        artifact_type="first30_thresholds",
        verify_inputs=verify_sources,
    )
    calibration = payload.get("calibration", {})
    if calibration.get("cutoff_exclusive") != CUTOFF_EXCLUSIVE:
        raise ArtifactValidationError("Threshold artifact does not use the locked pre-2025 cutoff")
    if calibration.get("quantiles") != list(QUANTILES):
        raise ArtifactValidationError("Threshold artifact quantiles do not match the locked contract")
    rows = payload.get("thresholds")
    if not isinstance(rows, list) or not rows:
        raise ArtifactValidationError("Threshold artifact has no threshold rows")
    result: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        key = (str(row.get("symbol", "")), str(row.get("timeframe", "")))
        if not all(key) or key in seen:
            raise ArtifactValidationError(f"Invalid or duplicate threshold key: {key}")
        seen.add(key)
        q60 = row.get("first30_q60")
        q70 = row.get("first30_q70")
        if not isinstance(q60, (int, float)) or not isinstance(q70, (int, float)):
            raise ArtifactValidationError(f"Missing frozen quantiles for {key}")
        if float(q60) <= 0 or float(q70) < float(q60):
            raise ArtifactValidationError(f"Invalid frozen quantiles for {key}")
        result.append(
            {
                "symbol": key[0],
                "timeframe": key[1],
                "first30_q60": float(q60),
                "first30_q70": float(q70),
                "sample_days": int(row["sample_days"]),
                "cutoff_exclusive": CUTOFF_EXCLUSIVE,
                "artifact_id": payload["artifact_id"],
                "artifact_sha256": payload["artifact_sha256"],
            }
        )
    return result
