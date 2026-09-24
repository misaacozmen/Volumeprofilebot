from __future__ import annotations

import importlib.util
import json
from copy import deepcopy
from pathlib import Path
import sys
import os

import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


artifacts = load_script("candidate_artifact")
thresholds = load_script("frozen_first30_thresholds")
mechanics = load_script("run_machine_metric_mechanics")
evaluator = load_script("evaluate_conservative_rolling_candidate_v4")
finalizer = load_script("finalize_strategy_improvement_loop_v4")


def test_artifact_rejects_payload_tampering(tmp_path: Path) -> None:
    source = tmp_path / "input.csv"
    source.write_bytes(b"a,b\n1,2\n")
    payload = artifacts.seal_artifact(
        {
            "artifact_type": "test",
            "provenance": artifacts.build_provenance(
                tmp_path, [(source, "input")], dependencies=()
            ),
        }
    )
    payload["extra"] = "tampered"
    with pytest.raises(artifacts.ArtifactValidationError, match="payload SHA-256"):
        artifacts.validate_artifact(payload, tmp_path, artifact_type="test")


def test_artifact_rejects_changed_input(tmp_path: Path) -> None:
    source = tmp_path / "input.csv"
    source.write_bytes(b"old")
    payload = artifacts.seal_artifact(
        {
            "artifact_type": "test",
            "provenance": artifacts.build_provenance(
                tmp_path, [(source, "input")], dependencies=()
            ),
        }
    )
    source.write_bytes(b"new")
    with pytest.raises(artifacts.ArtifactValidationError, match="input changed"):
        artifacts.validate_artifact(payload, tmp_path, artifact_type="test")


def test_threshold_calibration_accepts_deterministic_synthetic_input() -> None:
    rows: list[dict[str, object]] = []
    for offset in range(3):
        day = pd.Timestamp("2024-01-02", tz="America/New_York") + pd.Timedelta(days=offset)
        for bar in range(10):
            timestamp = day + pd.Timedelta(hours=9, minutes=30 + bar * 3)
            rows.append(
                {
                    "time": timestamp,
                    "date": timestamp.date(),
                    "high": 100.0 + offset + 2.0,
                    "low": 100.0 + offset,
                    "open": 100.0 + offset + 1.0,
                    "close": 100.0 + offset + 1.0,
                    "volume": 1.0,
                }
            )
    frame = pd.DataFrame(rows)
    assert thresholds.calibration_values(frame) == [2.0, 2.0, 2.0]


def test_development_ranking_has_no_holdout_metric_columns() -> None:
    rows = []
    for variant in ("baseline", "block_spx_first_structure"):
        for segment in [
            "development_pre_holdout",
            *[
                part
                for name in mechanics.DEVELOPMENT_FOLDS
                for part in (f"{name}__train", f"{name}__validation")
            ],
        ]:
            rows.append(
                {
                    "variant": variant,
                    "segment": segment,
                    "trades": 100,
                    "win_rate_pct": 50.0,
                    "net_r": 10.0,
                    "max_drawdown_r": -2.0,
                }
            )
    ranking = mechanics.build_development_ranking(pd.DataFrame(rows))
    assert not any("holdout_" in column and column != "holdout_used_for_selection" for column in ranking)
    assert not ranking["holdout_used_for_selection"].any()


def test_evaluator_uses_risk_rule_from_payload() -> None:
    frame = pd.DataFrame(
        [
            {
                "candidate": "x",
                "entry_time": "2024-01-02T10:00:00Z",
                "exit_time": "2024-01-02T10:01:00Z",
                "r_multiple": -1.0,
                "entry_weekday": "Tuesday",
                "direction": "long",
                "liquidity_type": "x",
                "overnight_direction": "down",
            },
            {
                "candidate": "x",
                "entry_time": "2024-01-02T10:02:00Z",
                "exit_time": "2024-01-02T10:03:00Z",
                "r_multiple": 1.0,
                "entry_weekday": "Tuesday",
                "direction": "long",
                "liquidity_type": "x",
                "overnight_direction": "down",
            },
        ]
    )
    low = evaluator.apply_payload(
        frame, {"setup_rules": [], "risk_rule": {"lookback": 1, "negative_scale": 0.2, "nonnegative_scale": 1.0}}
    )
    high = evaluator.apply_payload(
        frame, {"setup_rules": [], "risk_rule": {"lookback": 1, "negative_scale": 0.8, "nonnegative_scale": 1.0}}
    )
    assert low["risk_scale"].tolist() == [1.0, 0.2]
    assert high["risk_scale"].tolist() == [1.0, 0.8]


def test_checked_in_threshold_artifact_has_current_provenance() -> None:
    payload = artifacts.load_artifact(
        thresholds.ARTIFACT,
        ROOT,
        artifact_type="first30_thresholds",
        verify_inputs=True,
    )
    assert payload["artifact_id"] == "first30_thresholds_pre2025_v1"


def test_finalizer_binds_real_development_and_holdout_metrics(monkeypatch, tmp_path: Path) -> None:
    rows: list[dict[str, object]] = []
    sequence = 0
    for year in range(2016, 2025):
        for index in range(20):
            sequence += 1
            win = index >= 12
            rows.append(
                {
                    "candidate": f"dev-{sequence:04d}",
                    "entry_time": f"{year}-01-{index + 2:02d}T10:00:00Z",
                    "exit_time": f"{year}-01-{index + 2:02d}T10:30:00Z",
                    "entry_dt": f"{year}-01-{index + 2:02d}T10:00:00Z",
                    "entry_year": year,
                    "direction": "long",
                    "sweep_time": f"{year}-01-{index + 2:02d}T09:45:00Z",
                    "cisd_time": f"{year}-01-{index + 2:02d}T09:50:00Z",
                    "liquidity_type": "custom",
                    "overnight_direction": "down",
                    "data_valid": True,
                    "r_multiple": 1.0 if win else -1.0,
                    "strategy_r": 10.0 if win else -1.0,
                    "risk_scale": 1.0,
                    "result": "WIN" if win else "LOSS",
                    "entry_price": 100.0,
                    "stop_price": 99.0,
                    "target_price": 103.0,
                    "entry_weekday": "Tuesday",
                }
            )
    for year in (2025, 2026):
        for index in range(10):
            sequence += 1
            win = index >= 8
            rows.append(
                {
                    "candidate": f"holdout-{sequence:04d}",
                    "entry_time": f"{year}-01-{index + 2:02d}T10:00:00Z",
                    "exit_time": f"{year}-01-{index + 2:02d}T10:30:00Z",
                    "entry_dt": f"{year}-01-{index + 2:02d}T10:00:00Z",
                    "entry_year": year,
                    "direction": "long",
                    "sweep_time": f"{year}-01-{index + 2:02d}T09:45:00Z",
                    "cisd_time": f"{year}-01-{index + 2:02d}T09:50:00Z",
                    "liquidity_type": "custom",
                    "overnight_direction": "down",
                    "data_valid": True,
                    "r_multiple": 1.0 if win else -1.0,
                    "strategy_r": 2.0 if win else -0.5,
                    "risk_scale": 1.0,
                    "result": "WIN" if win else "LOSS",
                    "entry_price": 100.0,
                    "stop_price": 99.0,
                    "target_price": 103.0,
                    "entry_weekday": "Tuesday",
                }
            )
    source = pd.DataFrame(rows)
    source_path = tmp_path / "selected_trades.csv"
    evidence_path = tmp_path / "trade_evidence_index.csv"
    preselected_path = tmp_path / "selected_trades_pre2025.csv"
    source.to_csv(source_path, index=False)
    source["all_event_bars_found"] = True
    source.to_csv(evidence_path, index=False)
    source[source["entry_year"].le(2024)].drop(columns=["all_event_bars_found"], errors="ignore").to_csv(
        preselected_path, index=False
    )

    monkeypatch.setattr(finalizer, "SOURCE", source_path)
    monkeypatch.setattr(finalizer, "EVIDENCE", evidence_path)
    monkeypatch.setattr(finalizer, "PRE_SELECTED", preselected_path)
    monkeypatch.setattr(finalizer, "apply_payload", lambda frame, _payload: frame.copy())
    monkeypatch.setattr(
        finalizer,
        "build_provenance",
        lambda *_args, **_kwargs: {"inputs": [], "environment": {"synthetic": True}},
    )

    payload, evaluated, _metrics, _rolling = finalizer.build_candidate()
    finalizer.validate_publication_contract(payload, evaluated)
    assert payload["checks"]["criteria_pass"] is True
    assert payload["development_result_sha256"] != payload["full_evaluation_result_sha256"]
    assert payload["selection_protocol"]["final_holdout_used_for_selection"] is False
    assert payload["selection_protocol"]["final_holdout_used_for_publication"] is False
    assert {row["segment"] for row in _metrics.to_dict("records")} >= {
        "final_holdout_2025_2026", "all_2016_2026"
    }
    metrics = _metrics.set_index("segment")
    development_metrics = finalizer.risk_stats(evaluated[evaluated["entry_year"].le(2024)])
    assert development_metrics["win_rate"] != metrics.loc["final_holdout_2025_2026", "win_rate"]
    assert development_metrics["net_r"] != metrics.loc["final_holdout_2025_2026", "net_r"]
    assert not any("holdout" in str(key).lower() for key in payload["criteria"])

    for field in ("final_holdout_used_for_selection", "final_holdout_used_for_publication"):
        mutated = deepcopy(payload)
        mutated["selection_protocol"][field] = True
        with pytest.raises(ValueError, match="holdout"):
            finalizer.validate_publication_contract(mutated, evaluated)

    mutated_hash = deepcopy(payload)
    mutated_hash["development_result_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="development result hash"):
        finalizer.validate_publication_contract(mutated_hash, evaluated)

    mutated_hash = deepcopy(payload)
    mutated_hash["full_evaluation_result_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="full evaluation result hash"):
        finalizer.validate_publication_contract(mutated_hash, evaluated)

    original_risk_stats = finalizer.risk_stats

    def use_full_metrics_for_development(frame: pd.DataFrame) -> dict[str, object]:
        if len(frame) == 180:
            return original_risk_stats(source)
        return original_risk_stats(frame)

    monkeypatch.setattr(finalizer, "risk_stats", use_full_metrics_for_development)
    mutated_payload, _evaluated, _metrics, _rolling = finalizer.build_candidate()
    assert mutated_payload["checks"]["criteria_pass"] is False

    monkeypatch.setattr(finalizer, "risk_stats", original_risk_stats)
    mutated_source = source.copy()
    holdout = mutated_source["entry_year"].gt(2024)
    mutated_source.loc[holdout, "strategy_r"] = -2.0
    mutated_source.loc[holdout, "r_multiple"] = -1.0
    mutated_source_path = tmp_path / "selected_trades_holdout_mutated.csv"
    mutated_source.to_csv(mutated_source_path, index=False)
    monkeypatch.setattr(finalizer, "SOURCE", mutated_source_path)
    mutated_payload, mutated_evaluated, mutated_metrics, _rolling = finalizer.build_candidate()
    assert mutated_payload["criteria"] == payload["criteria"]
    assert mutated_payload["development_result_sha256"] == payload["development_result_sha256"]
    assert mutated_payload["full_evaluation_result_sha256"] != payload["full_evaluation_result_sha256"]
    mutated_metrics = mutated_metrics.set_index("segment")
    assert mutated_metrics.loc["all_2016_2026", "net_r"] != metrics.loc["all_2016_2026", "net_r"]
    assert mutated_metrics.loc["final_holdout_2025_2026", "net_r"] != metrics.loc["final_holdout_2025_2026", "net_r"]
    finalizer.validate_publication_contract(mutated_payload, mutated_evaluated)


def test_multi_directory_publication_rolls_back_every_destination(tmp_path, monkeypatch) -> None:
    first_staging = tmp_path / "first.new"
    second_staging = tmp_path / "second.new"
    first_destination = tmp_path / "first"
    second_destination = tmp_path / "second"
    for path, value in (
        (first_staging, "new-first"),
        (second_staging, "new-second"),
        (first_destination, "old-first"),
        (second_destination, "old-second"),
    ):
        path.mkdir()
        (path / "value.txt").write_text(value, encoding="utf-8")
    original = os.replace

    def fail_second_promotion(source, destination):
        if Path(source) == second_staging.resolve() and Path(destination) == second_destination.resolve():
            raise OSError("injected second-promotion failure")
        return original(source, destination)

    monkeypatch.setattr(artifacts.os, "replace", fail_second_promotion)
    with pytest.raises(OSError, match="injected"):
        artifacts.promote_directories_transactional(
            ((first_staging, first_destination), (second_staging, second_destination))
        )

    assert (first_destination / "value.txt").read_text(encoding="utf-8") == "old-first"
    assert (second_destination / "value.txt").read_text(encoding="utf-8") == "old-second"


def test_finalizer_does_not_publish_without_required_research_inputs() -> None:
    with pytest.raises(FileNotFoundError):
        finalizer.build_candidate()
