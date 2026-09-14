from __future__ import annotations

import ast
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd
import pytest

from backtest.config import SymbolConfig
from backtest.live_signal_protocol import (
    LiveSignalProtocolError,
    build_request,
    canonical_hash,
    evaluate_live_signal_twice,
    validate_request,
)
from backtest.manual_state import ManualStateConfig
from backtest.sandbox import PROFILES


ROOT = Path(__file__).resolve().parents[1]


def protocol_request() -> dict[str, object]:
    frozen = json.loads((ROOT / "forward_shadow" / "frozen_config.json").read_text(encoding="utf-8"))
    configs = {key: SymbolConfig(**value) for key, value in frozen["legs"].items()}
    frames = {}
    for key, minutes in (("nq", 3), ("spx", 5)):
        times = pd.date_range("2025-02-03 03:30", "2025-02-03 11:05", freq=f"{minutes}min", tz="America/New_York")
        frames[key] = pd.DataFrame({
            "time": times, "open": 100.0, "high": 101.0, "low": 99.0,
            "close": 100.25, "volume": 1.0,
            "known_time": times + pd.Timedelta(minutes=minutes),
            "source_minute_count": minutes,
        })
    digest = sha256(b"test-binding").hexdigest()
    return build_request(
        frames=frames, configs=configs, state_config=ManualStateConfig(**frozen["state"]),
        trade_date=pd.Timestamp("2025-02-03").date(),
        market_data_asof=pd.Timestamp("2025-02-03 11:10", tz="America/New_York"),
        knowledge_asof=pd.Timestamp("2025-02-03 11:10", tz="America/New_York"),
        cutoffs={"nq": pd.Timestamp("2025-02-03 11:00", tz="America/New_York"), "spx": pd.Timestamp("2025-02-03 11:00", tz="America/New_York")},
        candidate_artifact_hash=digest, candidate_file_hash=digest, calendar_hash=digest,
    )


def resign(request: dict[str, object]) -> None:
    request["request_hash"] = canonical_hash({key: value for key, value in request.items() if key != "request_hash"})


def test_live_signal_profile_is_fixed() -> None:
    profile = PROFILES["live-signal"]
    assert (profile.wall_seconds, profile.cpu_seconds, profile.memory_mb, profile.output_mb, profile.child_processes) == (20, 15, 1024, 16, 0)


def test_live_signal_worker_is_deterministic_and_schema_closed() -> None:
    result = evaluate_live_signal_twice(protocol_request())
    assert result["semantic_output_hash"]
    assert set(result) == {"schema_version", "request_hash", "decisions", "events", "lifecycle", "days", "state_snapshots", "graph_features", "semantic_output_hash"}


@pytest.mark.parametrize("mutation", ["unknown", "tamper", "duplicate", "nan"])
def test_live_signal_request_rejects_malformed_or_tampered_data(mutation: str) -> None:
    request = deepcopy(protocol_request())
    if mutation == "unknown":
        request["legs"]["nq"]["bars"][0]["path"] = "forbidden.py"
    elif mutation == "tamper":
        request["legs"]["nq"]["bars"][0]["close"] = 999.0
    elif mutation == "duplicate":
        request["legs"]["nq"]["bars"][1]["time"] = request["legs"]["nq"]["bars"][0]["time"]
        resign(request)
    else:
        request["legs"]["nq"]["bars"][0]["close"] = float("nan")
    with pytest.raises((LiveSignalProtocolError, ValueError)):
        validate_request(request)


def test_live_entrypoints_cannot_import_or_call_engine_pipeline() -> None:
    for name in ("run_capital_forward.py", "run_forward_shadow.py", "run_xm_mt5_forward.py", "run_super1_xm_mt5_forward.py"):
        path = ROOT / "scripts" / name
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module != "backtest.engine_pipeline", path
            if isinstance(node, ast.Import):
                assert all(alias.name != "backtest.engine_pipeline" for alias in node.names), path
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "run_canonical_pair_pipeline", path
