from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile

import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_forward_shadow.py"
SPEC = importlib.util.spec_from_file_location("run_forward_shadow", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
shadow = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(shadow)


def test_frozen_baseline_matches_engine_and_config() -> None:
    with pytest.raises(shadow.CriticalShadowError, match="(?i)baseline|code hash differs"):
        shadow.verify_baseline()


def test_pair_cap_uses_only_prior_realized_terminal() -> None:
    configs, _, _ = shadow.frozen_objects()
    base = {
        "order_state": "FILLED",
        "final_decision": "TAKE",
        "intrabar_ambiguity": "",
        "outcome": "SL",
        "r_multiple": -1.0,
    }
    rows = [
        {
            **base,
            "leg_key": "nq",
            "order_id": "one",
            "entry_known_time": "2025-01-02T09:40:00-05:00",
            "terminal_known_time": "2025-01-02T09:50:00-05:00",
        },
        {
            **base,
            "leg_key": "spx",
            "order_id": "two",
            "entry_known_time": "2025-01-02T09:51:00-05:00",
            "terminal_known_time": "2025-01-02T10:00:00-05:00",
        },
    ]
    capped = shadow.causal_pair_cap(rows, configs)
    assert capped[0]["pair_cap_state"] == "ALLOWED"
    assert capped[1]["pair_cap_state"] == "SUPPRESSED_DAILY_CAP"


def test_ambiguous_prior_terminal_forces_watch() -> None:
    configs, _, _ = shadow.frozen_objects()
    rows = [
        {
            "leg_key": "nq",
            "order_id": "one",
            "order_state": "FILLED",
            "final_decision": "TAKE",
            "intrabar_ambiguity": "STOP_AND_TARGET_TOUCHED_SAME_BAR",
            "outcome": "SL",
            "r_multiple": -1.0,
            "entry_known_time": "2025-01-02T09:40:00-05:00",
            "terminal_known_time": "2025-01-02T09:50:00-05:00",
        },
        {
            "leg_key": "spx",
            "order_id": "two",
            "order_state": "FILLED",
            "final_decision": "TAKE",
            "intrabar_ambiguity": "",
            "outcome": "TP",
            "r_multiple": 2.5,
            "entry_known_time": "2025-01-02T09:51:00-05:00",
            "terminal_known_time": "2025-01-02T10:00:00-05:00",
        },
    ]
    capped = shadow.causal_pair_cap(rows, configs)
    assert capped[0]["shadow_outcome"] == "AMBIGUOUS"
    assert capped[1]["pair_cap_state"] == "WATCH_UNORDERED_OR_AMBIGUOUS_PRIOR"


def test_pair_cap_uses_exact_partial_r_and_composite_order_key() -> None:
    configs, _, _ = shadow.frozen_objects()
    rows = [
        {
            "leg_key": "nq",
            "order_id": "shared",
            "order_state": "FILLED",
            "final_decision": "TAKE",
            "intrabar_ambiguity": "",
            "outcome": "SL",
            "r_multiple": -0.5,
            "entry_known_time": "2025-01-02T09:40:00-05:00",
            "terminal_known_time": "2025-01-02T09:50:00-05:00",
        },
        {
            "leg_key": "spx",
            "order_id": "shared",
            "order_state": "FILLED",
            "final_decision": "TAKE",
            "intrabar_ambiguity": "",
            "outcome": "TP",
            "r_multiple": 2.5,
            "entry_known_time": "2025-01-02T09:51:00-05:00",
            "terminal_known_time": "2025-01-02T10:00:00-05:00",
        },
    ]

    capped = shadow.causal_pair_cap(rows, configs)

    assert [row["pair_cap_state"] for row in capped] == ["ALLOWED", "ALLOWED"]


def test_daily_gate_rejects_duplicate_bar() -> None:
    configs, _, _ = shadow.frozen_objects()
    trade_date = pd.Timestamp("2025-02-04").date()
    times = pd.date_range("2025-02-03 18:00", "2025-02-04 10:30", freq="3min", tz=shadow.TZ)
    frame = pd.DataFrame(
        {
            "time": list(times) + [times[0]],
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1.0,
        }
    )
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "DUKASCOPY_USATECHIDXUSD, 3m_test.csv"
        frame.to_csv(path, index=False)
        _, report = shadow.read_leg_data(
            [path],
            "nq",
            configs["nq"],
            trade_date,
            pd.Timestamp("2025-02-04 12:00", tz=shadow.TZ),
        )
    assert report["state"] == "DATA_INVALID"
    assert "DUPLICATE_BAR" in {item["code"] for item in report["issues"]}


def test_manual_alignment_never_uses_tp_or_sl() -> None:
    decisions = [
        {
            "leg_key": "nq",
            "final_decision": "TAKE",
            "direction": "long",
            "outcome": "SL",
        }
    ]
    manual = {
        "legs": {
            "nq": {"final": "TAKE", "direction": "LONG", "explanation": None},
            "spx": {"final": None, "direction": None, "explanation": None},
        }
    }
    rows = shadow.comparison_rows(decisions, manual)
    assert rows[0]["final_match"] is True
    assert rows[0]["direction_match"] is True
    assert rows[0]["outcome_excluded_from_scoring"] is True
