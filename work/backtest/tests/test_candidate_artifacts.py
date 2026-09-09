from __future__ import annotations

import importlib.util
import json
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


def test_frozen_threshold_loader_ignores_runtime_frames() -> None:
    filters = load_script("run_main_candidate_filter_tests")
    expected = filters.build_thresholds()
    future_only = {
        ("DUKASCOPY_USATECHIDXUSD", "3m"): pd.DataFrame(
            {"date": [pd.Timestamp("2099-01-01").date()]}
        )
    }
    assert filters.build_thresholds(future_only) == expected
    assert {row["cutoff_exclusive"] for row in expected} == {"2025-01-01"}


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


def test_checked_in_threshold_artifact_is_valid() -> None:
    payload = artifacts.load_artifact(
        thresholds.ARTIFACT,
        ROOT,
        artifact_type="first30_thresholds",
        verify_inputs=True,
    )
    assert payload["calibration"]["cutoff_exclusive"] == "2025-01-01"
    assert payload["provenance"]["environment"]["git_commit"] == "1b233f524c1f03b7eb9cf3afc076507626ad4bf8"


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


def test_finalizer_publication_gates_are_development_only() -> None:
    payload, _, _, _ = finalizer.build_candidate()

    assert payload["selection_protocol"]["final_holdout_used_for_selection"] is False
    assert payload["selection_protocol"]["final_holdout_used_for_publication"] is False
    assert all(name.startswith("development_") for name in payload["criteria"])
