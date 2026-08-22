from __future__ import annotations

import json
import sys
from hashlib import sha256
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "outputs" / "reports" / "strategy_improvement_loop_v2" / "body_plus_classic_quota_day1_v1" / "selected_trades.csv"
REPORT_ROOT = ROOT / "outputs" / "reports" / "strategy_improvement_loop_v4"
SEGMENTS = {
    "learn_2016_2021": (2016, 2021),
    "validation_2022_2023": (2022, 2023),
    "validation_2024": (2024, 2024),
}


def stats(frame: pd.DataFrame) -> dict[str, float | int]:
    if frame.empty:
        return {"trades": 0, "wins": 0, "win_rate": 0.0, "net_r": 0.0, "max_drawdown_r": 0.0, "profit_factor": 0.0}
    pnl = frame["strategy_r"].astype(float)
    equity = pnl.cumsum()
    drawdown = equity - equity.cummax().clip(lower=0.0)
    gross_win = pnl[pnl > 0].sum()
    gross_loss = -pnl[pnl < 0].sum()
    return {
        "trades": int(len(frame)),
        "wins": int((frame["r_multiple"] > 0).sum()),
        "win_rate": round(float((frame["r_multiple"] > 0).mean() * 100), 2),
        "net_r": round(float(pnl.sum()), 3),
        "max_drawdown_r": round(float(abs(drawdown.min())), 3),
        "profit_factor": round(float(gross_win / gross_loss), 3) if gross_loss else 999.0,
    }


def causal_equity_overlay(frame: pd.DataFrame, threshold: float, low_scale: float, high_scale: float = 1.0) -> pd.DataFrame:
    ordered = frame.copy()
    ordered["entry_dt"] = pd.to_datetime(ordered["entry_time"], utc=True, format="mixed")
    ordered["exit_dt"] = pd.to_datetime(ordered["exit_time"], utc=True, format="mixed")
    ordered = ordered.sort_values(["entry_dt", "candidate"], kind="mergesort")
    completed: list[tuple[pd.Timestamp, float]] = []
    next_completion = 0
    equity = 0.0
    high_water = 0.0
    scales: list[float] = []
    pending = []
    for row in ordered.itertuples(index=False):
        pending.sort(key=lambda item: item[0])
        while pending and pending[0][0] <= row.entry_dt:
            _, realized = pending.pop(0)
            equity += realized
            high_water = max(high_water, equity)
        dd = high_water - equity
        scale = low_scale if dd >= threshold else high_scale
        scales.append(scale)
        pending.append((row.exit_dt, float(row.r_multiple) * scale))
    ordered["risk_scale"] = scales
    ordered["strategy_r"] = ordered["r_multiple"].astype(float) * ordered["risk_scale"]
    return ordered


def causal_period_loss_cap(frame: pd.DataFrame, cap_r: float, period: str) -> pd.DataFrame:
    ordered = frame.copy()
    ordered["entry_dt"] = pd.to_datetime(ordered["entry_time"], utc=True, format="mixed")
    ordered["exit_dt"] = pd.to_datetime(ordered["exit_time"], utc=True, format="mixed")
    ordered = ordered.sort_values(["entry_dt", "candidate"], kind="mergesort")
    pending: list[tuple[pd.Timestamp, str, float]] = []
    realized: dict[str, float] = {}
    kept = []
    for index, row in ordered.iterrows():
        entry_dt = row["entry_dt"]
        still_pending = []
        for exit_dt, key, pnl in pending:
            if exit_dt <= entry_dt:
                realized[key] = realized.get(key, 0.0) + pnl
            else:
                still_pending.append((exit_dt, key, pnl))
        pending = still_pending
        if period == "month":
            key = entry_dt.strftime("%Y-%m")
        elif period == "week":
            iso = entry_dt.isocalendar()
            key = f"{iso.year}-W{iso.week:02d}"
        else:
            key = entry_dt.strftime("%Y-%m-%d")
        if realized.get(key, 0.0) <= -cap_r:
            continue
        kept.append(index)
        pending.append((row["exit_dt"], key, float(row["r_multiple"])))
    result = ordered.loc[kept].copy()
    result["risk_scale"] = 1.0
    result["strategy_r"] = result["r_multiple"].astype(float)
    return result


def causal_loss_cooldown(frame: pd.DataFrame, skip_signals: int) -> pd.DataFrame:
    ordered = frame.copy()
    ordered["entry_dt"] = pd.to_datetime(ordered["entry_time"], utc=True, format="mixed")
    ordered["exit_dt"] = pd.to_datetime(ordered["exit_time"], utc=True, format="mixed")
    ordered = ordered.sort_values(["entry_dt", "candidate"], kind="mergesort")
    pending: list[tuple[pd.Timestamp, float]] = []
    cooldown = 0
    kept = []
    for index, row in ordered.iterrows():
        terminal_losses = 0
        still_pending = []
        for exit_dt, pnl in pending:
            if exit_dt <= row["entry_dt"]:
                terminal_losses += int(pnl < 0)
            else:
                still_pending.append((exit_dt, pnl))
        pending = still_pending
        if terminal_losses:
            cooldown = max(cooldown, skip_signals)
        if cooldown > 0:
            cooldown -= 1
            continue
        kept.append(index)
        pending.append((row["exit_dt"], float(row["r_multiple"])))
    result = ordered.loc[kept].copy()
    result["risk_scale"] = 1.0
    result["strategy_r"] = result["r_multiple"].astype(float)
    return result


def causal_loss_day_cooldown(frame: pd.DataFrame, cooldown_days: int) -> pd.DataFrame:
    ordered = frame.copy()
    ordered["entry_dt"] = pd.to_datetime(ordered["entry_time"], utc=True, format="mixed")
    ordered["exit_dt"] = pd.to_datetime(ordered["exit_time"], utc=True, format="mixed")
    ordered = ordered.sort_values(["entry_dt", "candidate"], kind="mergesort")
    pending: list[tuple[pd.Timestamp, float]] = []
    blocked_until = pd.Timestamp.min.tz_localize("UTC")
    kept = []
    for index, row in ordered.iterrows():
        still_pending = []
        for exit_dt, pnl in pending:
            if exit_dt <= row["entry_dt"] and pnl < 0:
                candidate_until = exit_dt.normalize() + pd.Timedelta(days=cooldown_days + 1)
                blocked_until = max(blocked_until, candidate_until)
            elif exit_dt > row["entry_dt"]:
                still_pending.append((exit_dt, pnl))
        pending = still_pending
        if row["entry_dt"] < blocked_until:
            continue
        kept.append(index)
        pending.append((row["exit_dt"], float(row["r_multiple"])))
    result = ordered.loc[kept].copy()
    result["risk_scale"] = 1.0
    result["strategy_r"] = result["r_multiple"].astype(float)
    return result


def causal_post_loss_scale(frame: pd.DataFrame, scaled_signals: int, loss_scale: float, healthy_scale: float = 1.0) -> pd.DataFrame:
    ordered = frame.copy()
    ordered["entry_dt"] = pd.to_datetime(ordered["entry_time"], utc=True, format="mixed")
    ordered["exit_dt"] = pd.to_datetime(ordered["exit_time"], utc=True, format="mixed")
    ordered = ordered.sort_values(["entry_dt", "candidate"], kind="mergesort")
    pending: list[tuple[pd.Timestamp, float]] = []
    remaining = 0
    scales = []
    for _, row in ordered.iterrows():
        still_pending = []
        terminal_loss = False
        for exit_dt, raw_pnl in pending:
            if exit_dt <= row["entry_dt"]:
                terminal_loss |= raw_pnl < 0
            else:
                still_pending.append((exit_dt, raw_pnl))
        pending = still_pending
        if terminal_loss:
            remaining = max(remaining, scaled_signals)
        scale = loss_scale if remaining > 0 else healthy_scale
        scales.append(scale)
        remaining = max(0, remaining - 1)
        pending.append((row["exit_dt"], float(row["r_multiple"])))
    ordered["risk_scale"] = scales
    ordered["strategy_r"] = ordered["r_multiple"].astype(float) * ordered["risk_scale"]
    return ordered


def causal_rolling_state_scale(frame: pd.DataFrame, lookback: int, negative_scale: float, nonnegative_scale: float) -> pd.DataFrame:
    ordered = frame.copy()
    ordered["entry_dt"] = pd.to_datetime(ordered["entry_time"], utc=True, format="mixed")
    ordered["exit_dt"] = pd.to_datetime(ordered["exit_time"], utc=True, format="mixed")
    ordered = ordered.sort_values(["entry_dt", "candidate"], kind="mergesort")
    pending: list[tuple[pd.Timestamp, float]] = []
    terminal: list[float] = []
    scales = []
    for _, row in ordered.iterrows():
        completed = sorted((item for item in pending if item[0] <= row["entry_dt"]), key=lambda item: item[0])
        terminal.extend(pnl for _, pnl in completed)
        pending = [item for item in pending if item[0] > row["entry_dt"]]
        state_sum = sum(terminal[-lookback:])
        scale = negative_scale if state_sum < 0 else nonnegative_scale
        scales.append(scale)
        pending.append((row["exit_dt"], float(row["r_multiple"])))
    ordered["risk_scale"] = scales
    ordered["strategy_r"] = ordered["r_multiple"].astype(float) * ordered["risk_scale"]
    return ordered


def baseline(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["risk_scale"] = 1.0
    result["strategy_r"] = result["r_multiple"].astype(float)
    return result.sort_values(["entry_time", "candidate"], kind="mergesort")


def metrics(frame: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for name, (start, end) in SEGMENTS.items():
        rows.append({"segment": name, **stats(frame[frame["entry_year"].between(start, end)])})
    rows.append({"segment": "pre_2025_all", **stats(frame[frame["entry_year"].le(2024)])})
    return pd.DataFrame(rows)


def write_variant(iteration: int, name: str, frame: pd.DataFrame, mechanism: dict) -> dict:
    safe_name = "".join(character if character.isalnum() or character in "-_." else "_" for character in name)
    directory = REPORT_ROOT / f"iteration_{iteration:03d}" / safe_name
    directory.mkdir(parents=True, exist_ok=True)
    visible = frame[frame["entry_year"].le(2024)].copy()
    table = metrics(visible)
    visible.to_csv(directory / "selected_trades_pre2025.csv", index=False)
    table.to_csv(directory / "metrics_pre2025.csv", index=False)
    result_hash = sha256(visible.to_csv(index=False).encode()).hexdigest()
    manifest = {
        "iteration": iteration,
        "variant": name,
        "mechanism": mechanism,
        "tuning_data": "2016-2024 only",
        "historical_2025_2026_hidden": True,
        "trade_evidence_source": str(SOURCE),
        "graph_provable_only": True,
        "live_enabled": False,
        "code_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "config_sha256": sha256(json.dumps(mechanism, sort_keys=True).encode()).hexdigest(),
        "result_sha256": result_hash,
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    result = {"variant": name, **table[table["segment"].eq("pre_2025_all")].iloc[0].to_dict()}
    for segment in SEGMENTS:
        segment_row = table[table["segment"].eq(segment)].iloc[0]
        result[f"{segment}_net_r"] = float(segment_row["net_r"])
        result[f"{segment}_dd_r"] = float(segment_row["max_drawdown_r"])
        result[f"{segment}_wr"] = float(segment_row["win_rate"])
    return result


def main() -> None:
    profile = sys.argv[1]
    iteration = int(sys.argv[2])
    source = pd.read_csv(SOURCE)
    source = source[source["entry_year"].le(2024)].copy()
    rows = []
    if profile == "baseline":
        rows.append(write_variant(iteration, "baseline", baseline(source), {"type": "none"}))
    elif profile == "equity_dd_scale":
        for threshold in (4.0, 6.0, 8.0, 10.0, 12.0):
            for low_scale in (0.0, 0.25, 0.5, 0.75):
                name = f"dd{threshold:g}_scale{low_scale:g}"
                rows.append(write_variant(iteration, name, causal_equity_overlay(source, threshold, low_scale), {"type": profile, "threshold": threshold, "low_scale": low_scale, "high_scale": 1.0}))
    elif profile == "equity_asymmetric":
        for threshold in (4.0, 6.0, 8.0, 10.0, 12.0):
            for low_scale in (0.25, 0.5, 0.75):
                for high_scale in (1.1, 1.2, 1.35):
                    name = f"dd{threshold:g}_low{low_scale:g}_high{high_scale:g}"
                    rows.append(write_variant(iteration, name, causal_equity_overlay(source, threshold, low_scale, high_scale), {"type": profile, "threshold": threshold, "low_scale": low_scale, "high_scale": high_scale}))
    elif profile == "october_candidate_block":
        variants = [("all", None)] + [(name, name) for name in sorted(source["candidate"].unique())]
        for label, candidate_name in variants:
            mask = source["entry_month"].eq(10)
            if candidate_name is not None:
                mask &= source["candidate"].eq(candidate_name)
            filtered = baseline(source[~mask].copy())
            rows.append(write_variant(iteration, f"block_october_{label}", filtered, {"type": profile, "month": 10, "candidate": candidate_name or "ALL"}))
        for quota_source in sorted(source["quota_source"].unique()):
            mask = source["entry_month"].eq(10) & source["quota_source"].eq(quota_source)
            filtered = baseline(source[~mask].copy())
            rows.append(write_variant(iteration, f"block_october_source_{quota_source}", filtered, {"type": profile, "month": 10, "quota_source": quota_source}))
    elif profile == "categorical_block":
        feature = sys.argv[3]
        for value in sorted(source[feature].dropna().astype(str).unique()):
            value_mask = source[feature].astype(str).eq(value)
            filtered = baseline(source[~value_mask].copy())
            rows.append(write_variant(iteration, f"block_all_{feature}_{value}", filtered, {"type": profile, "feature": feature, "value": value, "candidate": "ALL"}))
            for candidate_name in sorted(source["candidate"].unique()):
                mask = value_mask & source["candidate"].eq(candidate_name)
                filtered = baseline(source[~mask].copy())
                rows.append(write_variant(iteration, f"block_{candidate_name}_{feature}_{value}", filtered, {"type": profile, "feature": feature, "value": value, "candidate": candidate_name}))
    elif profile == "compound_quality_search":
        features = [
            "candidate", "time_bucket", "entry_weekday", "direction", "quota_source",
            "cisd_strength_regime", "fvg_size_regime", "htf_alignment", "opposing_structure",
            "liquidity_type", "month_direction", "prev_atr_regime", "prev20_vol_regime",
            "first30_ratio_regime", "gap_regime", "open_va_regime", "overnight_direction",
        ]
        candidates = []
        for left_index, left in enumerate(features):
            for right in features[left_index + 1:]:
                for values, cell in source.groupby([left, right], dropna=False):
                    segment_cells = [cell[cell["entry_year"].between(start, end)] for start, end in SEGMENTS.values()]
                    if any(len(part) < minimum for part, minimum in zip(segment_cells, (10, 5, 3))):
                        continue
                    if not all(float(part["r_multiple"].sum()) <= 0 for part in segment_cells):
                        continue
                    mask = source[left].fillna("<NA>").astype(str).eq("<NA>" if pd.isna(values[0]) else str(values[0]))
                    mask &= source[right].fillna("<NA>").astype(str).eq("<NA>" if pd.isna(values[1]) else str(values[1]))
                    filtered = baseline(source[~mask].copy())
                    overall = stats(filtered)
                    if overall["trades"] < int(len(source) * 0.70):
                        continue
                    candidates.append((overall["max_drawdown_r"], -overall["net_r"], left, right, values, filtered))
        candidates.sort(key=lambda item: (item[0], item[1]))
        for _, _, left, right, values, filtered in candidates[:40]:
            name = f"block_{left}_{values[0]}__{right}_{values[1]}"
            rows.append(write_variant(iteration, name, filtered, {"type": profile, "conditions": {left: None if pd.isna(values[0]) else str(values[0]), right: None if pd.isna(values[1]) else str(values[1])}, "minimum_samples": {"learn": 10, "validation_1": 5, "validation_2": 3}, "nonpositive_in_each_segment": True}))
    elif profile == "pair_of_compound_blocks":
        rules = []
        for manifest_path in sorted((REPORT_ROOT / "iteration_063").glob("*/manifest.json")):
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            conditions = payload["mechanism"].get("conditions")
            if conditions:
                rules.append((payload["variant"], conditions))
        for left_index, (left_name, left_rule) in enumerate(rules):
            for right_name, right_rule in rules[left_index + 1:]:
                blocked = pd.Series(False, index=source.index)
                for rule in (left_rule, right_rule):
                    mask = pd.Series(True, index=source.index)
                    for feature, value in rule.items():
                        mask &= source[feature].fillna("<NA>").astype(str).eq("<NA>" if value is None else str(value))
                    blocked |= mask
                filtered = baseline(source[~blocked].copy())
                rows.append(write_variant(iteration, f"pair_{left_name}___{right_name}", filtered, {"type": profile, "components": [left_rule, right_rule], "triple_combination": False}))
    elif profile == "locked_equity_asymmetric":
        locked_source = pd.read_csv(REPORT_ROOT / "iteration_065" / "locked_pair_v1" / "selected_trades_pre2025.csv")
        for threshold in (3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 10.0):
            for low_scale in (0.25, 0.5, 0.75):
                for high_scale in (1.05, 1.1, 1.2, 1.3):
                    name = f"locked_dd{threshold:g}_low{low_scale:g}_high{high_scale:g}"
                    overlaid = causal_equity_overlay(locked_source, threshold, low_scale, high_scale)
                    rows.append(write_variant(iteration, name, overlaid, {"type": profile, "threshold": threshold, "low_scale": low_scale, "high_scale": high_scale, "state_uses_only_terminal_results": True}))
    elif profile == "locked_monthly_loss_cap":
        locked_source = pd.read_csv(REPORT_ROOT / "iteration_065" / "locked_pair_v1" / "selected_trades_pre2025.csv")
        for cap_r in (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0):
            capped = causal_period_loss_cap(locked_source, cap_r, "month")
            rows.append(write_variant(iteration, f"monthly_loss_cap_{cap_r:g}R", capped, {"type": profile, "cap_r": cap_r, "reset": "calendar_month", "state_uses_only_terminal_results": True}))
    elif profile == "locked_monthly_loss_cap_sensitivity":
        locked_source = pd.read_csv(REPORT_ROOT / "iteration_065" / "locked_pair_v1" / "selected_trades_pre2025.csv")
        for cap_r in (7.5, 8.0, 8.5, 9.0, 9.5, 10.0, 10.5, 11.0):
            capped = causal_period_loss_cap(locked_source, cap_r, "month")
            rows.append(write_variant(iteration, f"monthly_loss_cap_{cap_r:g}R", capped, {"type": profile, "cap_r": cap_r, "reset": "calendar_month", "state_uses_only_terminal_results": True}))
    elif profile == "locked_weekly_loss_cap":
        locked_source = pd.read_csv(REPORT_ROOT / "iteration_065" / "locked_pair_v1" / "selected_trades_pre2025.csv")
        for cap_r in (1.0, 2.0, 3.0, 4.0, 5.0, 6.0):
            capped = causal_period_loss_cap(locked_source, cap_r, "week")
            rows.append(write_variant(iteration, f"weekly_loss_cap_{cap_r:g}R", capped, {"type": profile, "cap_r": cap_r, "reset": "ISO_week", "state_uses_only_terminal_results": True}))
    elif profile == "locked_weekly_loss_cap_sensitivity":
        locked_source = pd.read_csv(REPORT_ROOT / "iteration_065" / "locked_pair_v1" / "selected_trades_pre2025.csv")
        for cap_r in (4.0, 4.5, 5.0, 5.5, 6.0, 6.5, 7.0):
            capped = causal_period_loss_cap(locked_source, cap_r, "week")
            rows.append(write_variant(iteration, f"weekly_loss_cap_{cap_r:g}R", capped, {"type": profile, "cap_r": cap_r, "reset": "ISO_week", "state_uses_only_terminal_results": True}))
    elif profile == "locked_daily_loss_cap":
        locked_source = pd.read_csv(REPORT_ROOT / "iteration_065" / "locked_pair_v1" / "selected_trades_pre2025.csv")
        for cap_r in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0):
            capped = causal_period_loss_cap(locked_source, cap_r, "day")
            rows.append(write_variant(iteration, f"daily_loss_cap_{cap_r:g}R", capped, {"type": profile, "cap_r": cap_r, "reset": "trading_day", "state_uses_only_terminal_results": True}))
    elif profile == "locked_loss_cooldown":
        locked_source = pd.read_csv(REPORT_ROOT / "iteration_065" / "locked_pair_v1" / "selected_trades_pre2025.csv")
        for skip_signals in (1, 2, 3, 4, 5):
            cooled = causal_loss_cooldown(locked_source, skip_signals)
            rows.append(write_variant(iteration, f"loss_cooldown_skip_{skip_signals}", cooled, {"type": profile, "skip_next_signal_opportunities": skip_signals, "trigger": "terminal_loss", "open_trade_outcome_used": False}))
    elif profile == "locked_loss_day_cooldown":
        locked_source = pd.read_csv(REPORT_ROOT / "iteration_065" / "locked_pair_v1" / "selected_trades_pre2025.csv")
        for cooldown_days in (0, 1, 2, 3):
            cooled = causal_loss_day_cooldown(locked_source, cooldown_days)
            rows.append(write_variant(iteration, f"loss_cooldown_days_{cooldown_days}", cooled, {"type": profile, "cooldown_days_after_terminal_loss": cooldown_days, "open_trade_outcome_used": False}))
    elif profile == "locked_post_loss_scale":
        locked_source = pd.read_csv(REPORT_ROOT / "iteration_065" / "locked_pair_v1" / "selected_trades_pre2025.csv")
        for scaled_signals in (1, 2, 3, 4, 5):
            for loss_scale in (0.25, 0.5, 0.75):
                scaled = causal_post_loss_scale(locked_source, scaled_signals, loss_scale)
                rows.append(write_variant(iteration, f"post_loss_{scaled_signals}_signals_scale_{loss_scale:g}", scaled, {"type": profile, "scaled_signals": scaled_signals, "loss_scale": loss_scale, "open_trade_outcome_used": False}))
    elif profile == "locked_post_loss_asymmetric":
        locked_source = pd.read_csv(REPORT_ROOT / "iteration_065" / "locked_pair_v1" / "selected_trades_pre2025.csv")
        for scaled_signals in (1, 2, 3):
            for loss_scale in (0.5, 0.75):
                for healthy_scale in (1.1, 1.2, 1.3, 1.4):
                    scaled = causal_post_loss_scale(locked_source, scaled_signals, loss_scale, healthy_scale)
                    rows.append(write_variant(iteration, f"post_loss_{scaled_signals}_low_{loss_scale:g}_healthy_{healthy_scale:g}", scaled, {"type": profile, "scaled_signals": scaled_signals, "loss_scale": loss_scale, "healthy_scale": healthy_scale, "open_trade_outcome_used": False}))
    elif profile == "locked_post_loss_asymmetric_sensitivity":
        locked_source = pd.read_csv(REPORT_ROOT / "iteration_065" / "locked_pair_v1" / "selected_trades_pre2025.csv")
        for healthy_scale in (1.22, 1.24, 1.25, 1.26, 1.28, 1.30):
            scaled = causal_post_loss_scale(locked_source, 1, 0.75, healthy_scale)
            rows.append(write_variant(iteration, f"post_loss_1_low_0.75_healthy_{healthy_scale:g}", scaled, {"type": profile, "scaled_signals": 1, "loss_scale": 0.75, "healthy_scale": healthy_scale, "open_trade_outcome_used": False}))
    elif profile == "locked_rolling_state_scale":
        locked_source = pd.read_csv(REPORT_ROOT / "iteration_065" / "locked_pair_v1" / "selected_trades_pre2025.csv")
        for lookback in (3, 5, 8, 10, 20):
            for negative_scale in (0.5, 0.75):
                for nonnegative_scale in (1.0, 1.1, 1.2):
                    scaled = causal_rolling_state_scale(locked_source, lookback, negative_scale, nonnegative_scale)
                    rows.append(write_variant(iteration, f"rolling_{lookback}_negative_{negative_scale:g}_healthy_{nonnegative_scale:g}", scaled, {"type": profile, "lookback_terminal_trades": lookback, "negative_scale": negative_scale, "nonnegative_scale": nonnegative_scale, "open_trade_outcome_used": False}))
    else:
        raise SystemExit(f"unknown profile: {profile}")
    comparison = pd.DataFrame(rows).sort_values(["max_drawdown_r", "net_r"], ascending=[True, False])
    directory = REPORT_ROOT / f"iteration_{iteration:03d}"
    comparison.to_csv(directory / "comparison_pre2025.csv", index=False)
    print(comparison.to_string(index=False))


if __name__ == "__main__":
    main()
