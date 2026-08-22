from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import run_engine_path_comparison_2025_feb_mar as comparison
import run_main_candidate_filter_tests as filters
from backtest.manual_state import (
    cisd_events_to_frame,
    cisd_qualifications_to_frame,
    context_authority_to_frame,
    context_triggers_to_frame,
    decisions_to_frame,
    liquidity_selections_to_frame,
    premarket_contexts_to_frame,
    run_manual_state_backtest,
    thesis_episodes_to_frame,
    ManualStateConfig,
)


REPORT_DIR = ROOT / "outputs" / "reports" / "six_divergence_path_audit"
CASES = {
    "nq": ["2025-02-07", "2025-02-13", "2025-02-14", "2025-02-21", "2025-03-24"],
    "spx": ["2025-03-26"],
}


def main() -> None:
    reset_report_dir()
    loaded = filters.load_data()
    configs = comparison.build_active_configs(loaded)
    outputs: dict[str, list[pd.DataFrame]] = {
        "premarket": [],
        "liquidity": [],
        "context_triggers": [],
        "context_authority": [],
        "cisd_events": [],
        "cisd_qualifications": [],
        "thesis_episodes": [],
        "decisions": [],
    }

    for leg_key, date_texts in CASES.items():
        symbol, timeframe = comparison.SYMBOLS[leg_key]
        config = configs[leg_key]
        frame = comparison.select_window(loaded[(symbol, timeframe)])
        dates = [pd.Timestamp(value).date() for value in date_texts]
        state_config = ManualStateConfig(
            trade_window_start=config.trade_window_start,
            trade_window_end=config.trade_window_end,
            cisd_anchor_lookback=6,
            fvg_window_candles=config.fvg_window_candles,
        )
        result = run_manual_state_backtest(frame, config, dates, state_config)
        frames = {
            "premarket": premarket_contexts_to_frame(result.days),
            "liquidity": liquidity_selections_to_frame(result.days),
            "context_triggers": context_triggers_to_frame(result.days),
            "context_authority": context_authority_to_frame(result.days),
            "cisd_events": cisd_events_to_frame(result.days),
            "cisd_qualifications": cisd_qualifications_to_frame(result.days),
            "thesis_episodes": thesis_episodes_to_frame(result.days),
            "decisions": decisions_to_frame(result.days),
        }
        for name, output in frames.items():
            if output.empty:
                continue
            output.insert(0, "leg_key", leg_key)
            if "symbol" not in output.columns:
                output.insert(1, "symbol", symbol)
            outputs[name].append(output)

    for name, frames in outputs.items():
        combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        combined.to_csv(REPORT_DIR / f"{name}.csv", index=False)
    build_case_summary(outputs).to_csv(REPORT_DIR / "case_summary.csv", index=False)
    print(f"Wrote: {REPORT_DIR}")


def reset_report_dir() -> None:
    if REPORT_DIR.exists():
        for path in sorted(REPORT_DIR.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


def combine(frames: list[pd.DataFrame]) -> pd.DataFrame:
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def build_case_summary(outputs: dict[str, list[pd.DataFrame]]) -> pd.DataFrame:
    contexts = combine(outputs["context_triggers"])
    qualifications = combine(outputs["cisd_qualifications"])
    thesis = combine(outputs["thesis_episodes"])
    decisions = combine(outputs["decisions"])
    rows = []
    for leg_key, dates in CASES.items():
        symbol = comparison.SYMBOLS[leg_key][0]
        for date_text in dates:
            day_contexts = contexts[(contexts["symbol"] == symbol) & (contexts["date"] == date_text)]
            day_quals = qualifications[
                (qualifications["symbol"] == symbol) & (qualifications["date"] == date_text)
            ]
            day_thesis = thesis[(thesis["symbol"] == symbol) & (thesis["date"] == date_text)]
            day_decisions = decisions[
                (decisions["symbol"] == symbol) & (decisions["date"] == date_text)
            ]
            rows.append(
                {
                    "leg_key": leg_key,
                    "symbol": symbol,
                    "date": date_text,
                    "context_path": join_values(day_contexts, "gate", "preferred_direction", "time"),
                    "cisd_path": join_values(
                        day_quals,
                        "lifecycle_state",
                        "cisd_direction",
                        "cisd_confirm_time",
                    ),
                    "thesis_path": join_values(
                        day_thesis,
                        "status",
                        "direction",
                        "authority_time",
                    ),
                    "decision_path": join_values(
                        day_decisions,
                        "order_state",
                        "direction",
                        "terminal_reason",
                    ),
                }
            )
    return pd.DataFrame(rows)


def join_values(frame: pd.DataFrame, *columns: str) -> str:
    if frame.empty:
        return ""
    return " | ".join(
        "/".join(str(row[column]) for column in columns)
        for _, row in frame.iterrows()
    )


if __name__ == "__main__":
    main()
