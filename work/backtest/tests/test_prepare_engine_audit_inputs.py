from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from prepare_engine_audit_inputs import (
    EXPECTED_DATASETS,
    EXPECTED_WINDOW,
    MANIFEST_NAME,
    prepare,
    validate_manifest,
)
from run_engine_reliability_audit import load_audit_data


def _write_input(root: Path, key: str, rows: list[str]) -> dict[str, str]:
    symbol, timeframe = EXPECTED_DATASETS[key]
    path = root / key / f"{symbol}, {timeframe}_2025-01-01_2025-05-01.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "time,open,high,low,close,volume\n" + "\n".join(rows) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _valid_source(root: Path, *, first_day: str = "2025-01-27") -> dict[str, object]:
    root.mkdir(parents=True, exist_ok=True)
    rows = [
        f"{first_day}T10:00:00-05:00,100,101,99,100.5,10",
        "2025-04-03T15:00:00-04:00,101,102,100,101.5,12",
    ]
    manifest = {
        "schema": "engine-audit-inputs-v1",
        "source": {
            "provider": "Dukascopy",
            "source_url": "https://www.dukascopy.com/api/data/get/historical-data-export",
            "observed_at_utc": "2026-09-24T00:00:00Z",
            "provenance_note": "Synthetic test input; no provider retrieval is claimed.",
        },
        "window": dict(EXPECTED_WINDOW),
        "datasets": [
            {
                "leg_key": key,
                "symbol": symbol,
                "timeframe": timeframe,
                "files": [_write_input(root, key, rows)],
            }
            for key, (symbol, timeframe) in EXPECTED_DATASETS.items()
        ],
    }
    (root / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def test_engine_audit_input_manifest_binds_exact_datasets_window_and_source(tmp_path: Path) -> None:
    manifest = _valid_source(tmp_path / "source")
    entries = validate_manifest(manifest)
    assert len(entries) == 2
    assert {entry["leg_key"] for entry in entries} == set(EXPECTED_DATASETS)


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("window", "ENGINE_AUDIT_WINDOW_INVALID"),
        ("dataset", "ENGINE_AUDIT_DATASETS_INVALID"),
        ("source_url", "ENGINE_AUDIT_MANIFEST_INVALID"),
        ("source_host", "ENGINE_AUDIT_MANIFEST_INVALID"),
        ("source_time", "ENGINE_AUDIT_MANIFEST_INVALID"),
        ("provenance", "ENGINE_AUDIT_MANIFEST_INVALID"),
    ],
)
def test_engine_audit_input_contract_rejects_wrong_window_dataset_or_source(
    tmp_path: Path,
    mutation: str,
    error: str,
) -> None:
    manifest = _valid_source(tmp_path / "source")
    if mutation == "window":
        manifest["window"]["start_inclusive"] = "2025-02-01T00:00:00-05:00"
    elif mutation == "dataset":
        manifest["datasets"].pop()
    elif mutation == "source_url":
        manifest["source"]["source_url"] = "file:///private/data"
    elif mutation == "source_host":
        manifest["source"]["source_url"] = "https://dukascopy.com.attacker.invalid/history"
    elif mutation == "source_time":
        manifest["source"]["observed_at_utc"] = "2026-09-24T00:00:00+02:00"
    else:
        manifest["source"]["provenance_note"] = ""
    with pytest.raises(RuntimeError, match=error):
        validate_manifest(manifest)


def test_engine_audit_stage_hash_verifies_before_copying(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _valid_source(source)
    manifest_path = source / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["datasets"][0]["files"][0]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    stage = tmp_path / "stage"
    with pytest.raises(RuntimeError, match="ENGINE_AUDIT_INPUT_HASH_MISMATCH"):
        prepare(source, stage)
    assert not stage.exists()


def test_engine_audit_stage_rejects_unmanifested_csv(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _valid_source(source)
    (source / "extra.csv").write_text("not an allowed input\n", encoding="utf-8")
    stage = tmp_path / "stage"
    with pytest.raises(RuntimeError, match="ENGINE_AUDIT_INPUT_SET_MISMATCH"):
        prepare(source, stage)
    assert not stage.exists()


def test_engine_audit_stage_rejects_actual_data_outside_warmup_window(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _valid_source(source, first_day="2025-02-03")
    with pytest.raises(RuntimeError, match="ENGINE_AUDIT_WINDOW_DATA_MISSING: nq"):
        prepare(source, tmp_path / "stage")
    assert not (tmp_path / "stage").exists()


def test_risk_workflow_requires_separate_audit_inputs_and_owner_fixture() -> None:
    workflow = (Path(__file__).resolve().parents[3] / ".github" / "workflows" / "super1-risk-gates.yml").read_text(
        encoding="utf-8"
    )
    assert "engine_audit_source_root:" in workflow
    assert "symlink_fixture_root:" in workflow
    assert "ENGINE_AUDIT_SOURCE_ROOT:" in workflow
    assert "SUPER1_SYMLINK_FIXTURE_ROOT:" in workflow
    assert "prepare_risk_provenance.py --source-root" in workflow
    assert "--engine-audit-source-root \"$env:ENGINE_AUDIT_SOURCE_ROOT\"" in workflow
    assert "--collect-only -q -p no:cacheprovider tests" in workflow
    assert "-q -p no:cacheprovider tests --junitxml=" in workflow
    assert workflow.count("--symlink-fixture-root \"$env:SUPER1_SYMLINK_FIXTURE_ROOT\"") >= 3


def test_engine_audit_stage_preserves_separate_hash_pinned_inputs(tmp_path: Path) -> None:
    source = tmp_path / "source"
    manifest = _valid_source(source)
    stage = tmp_path / "stage"
    result = prepare(source, stage)
    assert result["status"] == "PASS"
    assert result["input_count"] == 2
    assert (stage / MANIFEST_NAME).read_bytes() == (source / MANIFEST_NAME).read_bytes()
    for dataset in manifest["datasets"]:
        entry = dataset["files"][0]
        staged = stage / dataset["leg_key"] / Path(entry["path"]).name
        assert hashlib.sha256(staged.read_bytes()).hexdigest() == entry["sha256"]


def test_engine_audit_loader_reads_only_the_explicit_market_data_root(tmp_path: Path) -> None:
    root = tmp_path / "separate-audit-root"
    _valid_source(root)
    loaded = load_audit_data(root)
    assert set(loaded) == set(EXPECTED_DATASETS.values())
    assert all(len(frame) == 2 for frame in loaded.values())
