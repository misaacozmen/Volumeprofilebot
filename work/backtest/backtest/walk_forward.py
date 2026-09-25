"""Anchored walk-forward orchestration with train-only selection and OOS-only output."""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from typing import Callable, Mapping, Sequence

import pandas as pd

from .optimization import StudyConfig, canonical_hash, optimize
from .risk_xray import build_risk_xray, write_risk_xray


def walk_forward(
    config: StudyConfig,
    sessions: Sequence[object],
    evaluator: Callable[[Mapping[str, object], Sequence[object]], tuple[pd.DataFrame, Mapping[str, object]]],
    output_dir: str | Path,
    *,
    data_hash: str | None = None,
    code_hash: str | None = None,
) -> dict[str, object]:
    ordered = sorted(dict.fromkeys(sessions))
    if len(ordered) < config.train_sessions + config.oos_sessions:
        raise ValueError("insufficient sessions for one non-overlapping OOS fold")
    root = Path(output_dir)
    selections: list[dict[str, object]] = []
    oos_frames: list[pd.DataFrame] = []
    evaluated_oos_sessions: list[object] = []
    fold = 0
    train_end = config.train_sessions
    while train_end + config.oos_sessions <= len(ordered):
        train_start = 0 if config.anchored else train_end - config.train_sessions
        train, oos = ordered[train_start:train_end], ordered[train_end:train_end + config.oos_sessions]
        inner_start = max(1, int(len(train) * 0.8))
        inner = train[inner_start:]
        study_dir = root / f"fold_{fold:03d}" / "selection"
        selection = optimize(config, lambda params: evaluator(params, inner)[1], study_dir, data_hash=data_hash, code_hash=code_hash)
        winner = selection.get("winner")
        if not isinstance(winner, dict):
            raise ValueError(f"fold {fold} has no valid train-only selection")
        parameters = dict(winner["parameters"])
        trades, _ = evaluator(parameters, oos)
        if not isinstance(trades, pd.DataFrame):
            raise ValueError("walk-forward evaluator must return a trade DataFrame")
        if not trades.empty and "date" in trades.columns:
            observed_dates = {str(value) for value in trades["date"]}
            expected_dates = {str(value) for value in oos}
            if not observed_dates.issubset(expected_dates):
                raise ValueError(f"fold {fold} produced a trade outside its OOS sessions")
        tagged = trades.copy(); tagged["fold"] = fold
        oos_frames.append(tagged)
        evaluated_oos_sessions.extend(oos)
        selections.append({"fold": fold, "train_sessions": [str(train[0]), str(train[-1])], "oos_sessions": [str(oos[0]), str(oos[-1])], "parameter_hash": winner["parameter_hash"], "parameters": parameters})
        fold += 1; train_end += config.step_sessions
    if not selections:
        raise ValueError("no OOS folds were produced")
    if len(evaluated_oos_sessions) != len(set(evaluated_oos_sessions)):
        raise ValueError("OOS sessions overlap across folds")
    root.mkdir(parents=True, exist_ok=True)
    combined = pd.concat(oos_frames, ignore_index=True) if oos_frames else pd.DataFrame()
    combined.to_csv(root / "oos_trades.csv", index=False)
    (root / "fold_selections.json").write_text(json.dumps(selections, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    xray = build_risk_xray(
        combined,
        eligible_dates=evaluated_oos_sessions,
        funnel=None,
        provenance_hashes={
            "input_hash": data_hash or "",
            "code_hash": code_hash or "",
            "coverage_hash": canonical_hash({"oos_sessions": [str(value) for value in evaluated_oos_sessions]}),
        },
    )
    write_risk_xray(xray, root, stem="risk_xray")
    summary = {"status": "RESEARCH_ONLY_NON_PROMOTABLE", "folds": len(selections), "data_hash": data_hash, "code_hash": code_hash, "seed": config.seed, "result_hash": canonical_hash({"config": asdict(config), "data_hash": data_hash, "code_hash": code_hash, "selections": selections, "oos": combined.to_dict("records")}), "risk_xray": xray}
    (root / "oos_summary.json").write_text(json.dumps(summary, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return summary
