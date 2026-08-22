from __future__ import annotations

import json
import sys
from hashlib import sha256
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

import run_ordered_15_round_research as research
import run_seasonality_10y_analysis as seasonality


REPORT_ROOT = ROOT / "outputs" / "reports" / "strategy_improvement_loop_v2"
PROFILE = REPORT_ROOT / "body_plus_classic_quota_day1_v1"
AUDIT_DIR = PROFILE / "audit"
IDENTITY = ["candidate", "entry_time", "direction", "sweep_time", "cisd_time"]
EVENT_COLUMNS = ["sweep_time", "cisd_time", "fvg_time", "entry_time", "exit_time"]


def quota_selection(cutoff: str | None = None) -> pd.DataFrame:
    base = pd.read_csv(REPORT_ROOT / "block_spx_phase_monday_v1" / "selected_trades.csv")
    classic = pd.read_csv(REPORT_ROOT / "spx_1045_all_weekdays_v1" / "selected_trades.csv")
    classic = classic[classic["fvg_size_regime"].eq("high") & classic["htf_alignment"].eq("opposed")]
    body = pd.read_csv(REPORT_ROOT / "spx_1045_phase_body_fvg_all_weekdays_source_v1" / "selected_trades.csv")
    body = body[body["notes"].str.contains("body_fvg", na=False) & body["cisd_fvg_candles_regime"].eq("low")]
    frames = []
    for frame, source, priority in (
        (base, "base", 0),
        (classic, "classic_high_fvg_htf_opposed", 1),
        (body, "phase_body_low_delay", 2),
    ):
        frame = frame.copy()
        frame["quota_source"] = source
        frame["source_priority"] = priority
        if cutoff is not None:
            frame = frame[frame["entry_date"].le(cutoff)]
        frames.append(frame)
    union = pd.concat(frames, ignore_index=True)
    union["entry_time_sort"] = pd.to_datetime(union["entry_time"], utc=True, format="mixed")
    union["year_month"] = union["entry_date"].str[:7]
    union["entry_day"] = union["entry_date"].str[8:10].astype(int)
    union = union.sort_values(["entry_time_sort", "candidate", "source_priority"], kind="mergesort").drop_duplicates(
        IDENTITY, keep="first"
    )
    kept: list[int] = []
    counts: dict[str, int] = {}
    for index, row in union.sort_values(["entry_time_sort", "source_priority", "candidate"], kind="mergesort").iterrows():
        month = str(row["year_month"])
        if row["quota_source"] == "base" or counts.get(month, 0) < 5:
            kept.append(index)
            counts[month] = counts.get(month, 0) + 1
    candidate = json.loads(
        (ROOT / "research_candidates" / "v7_strategy_loop" / "nq_spx_quality_time_mixed_rr_v1.json").read_text(encoding="utf-8")
    )
    return research.apply_final_pair_cap(union.loc[kept].copy(), candidate["pair_rules"])


def identity_hashes(frame: pd.DataFrame) -> set[str]:
    return set(frame[IDENTITY].astype(str).agg("|".join, axis=1))


def canonical_result_hash(frame: pd.DataFrame) -> str:
    columns = IDENTITY + ["exit_time", "result", "r_multiple", "entry_price", "stop_price", "target_price"]
    canonical = frame[columns].copy().sort_values(IDENTITY, kind="mergesort")
    for column in ["r_multiple", "entry_price", "stop_price", "target_price"]:
        canonical[column] = pd.to_numeric(canonical[column]).map(lambda value: f"{value:.10f}")
    return sha256(canonical.to_csv(index=False, lineterminator="\n").encode()).hexdigest()


def main() -> None:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    selected = pd.read_csv(PROFILE / "selected_trades.csv")
    manifest = json.loads((PROFILE / "manifest.json").read_text(encoding="utf-8"))

    deterministic_rerun = quota_selection()
    computed_result_hash = canonical_result_hash(selected)
    deterministic = computed_result_hash == canonical_result_hash(deterministic_rerun)
    required = EVENT_COLUMNS + ["symbol", "timeframe", "direction", "entry_price", "stop_price", "target_price", "result", "r_multiple"]
    nonnull = ~selected[required].isna().any(axis=1)
    times = {column: pd.to_datetime(selected[column], utc=True, format="mixed") for column in EVENT_COLUMNS}
    causal_order = (
        (times["sweep_time"] <= times["cisd_time"])
        & (times["cisd_time"] <= times["fvg_time"])
        & (times["fvg_time"] <= times["entry_time"])
        & (times["entry_time"] <= times["exit_time"])
    )
    long_prices = selected["direction"].eq("long") & (selected["stop_price"] < selected["entry_price"]) & (selected["entry_price"] < selected["target_price"])
    short_prices = selected["direction"].eq("short") & (selected["target_price"] < selected["entry_price"]) & (selected["entry_price"] < selected["stop_price"])
    price_geometry = long_prices | short_prices
    valid_data = selected["data_valid"].astype(str).str.lower().eq("true")

    specs = seasonality.candidate_specs()
    market_data = seasonality.load_data(specs)
    raw_lookup: dict[tuple[str, str], tuple[set[int], dict[int, str]]] = {}
    for key, bars in market_data.items():
        raw_ns = pd.to_datetime(bars["time"], utc=True, format="mixed").astype("int64") * 1000
        raw_lookup[key] = (set(raw_ns.tolist()), dict(zip(raw_ns.tolist(), bars["source_file"].astype(str))))
    raw_matches = []
    source_files = []
    for row in selected.itertuples(index=False):
        timestamp_set, source_map = raw_lookup[(str(row.symbol), str(row.timeframe))]
        event_ns = [int(pd.Timestamp(getattr(row, column)).tz_convert("UTC").value) for column in EVENT_COLUMNS]
        raw_matches.append(all(value in timestamp_set for value in event_ns))
        source_files.append(source_map.get(event_ns[3], ""))
    raw_matches_series = pd.Series(raw_matches, index=selected.index)

    prefix_rerun = quota_selection("2024-12-31")
    full_prefix = selected[selected["entry_date"].le("2024-12-31")]
    prefix_pass = identity_hashes(prefix_rerun) == identity_hashes(full_prefix)

    evidence = selected[
        ["candidate", "symbol", "timeframe", "direction", *EVENT_COLUMNS, "entry_price", "stop_price", "target_price", "exit_price", "result", "r_multiple", "data_valid"]
    ].copy()
    evidence.insert(0, "evidence_id", evidence.astype(str).agg("|".join, axis=1).map(lambda value: sha256(value.encode()).hexdigest()[:20]))
    evidence["entry_source_file"] = source_files
    evidence["all_event_bars_found"] = raw_matches
    evidence.to_csv(AUDIT_DIR / "trade_evidence_index.csv", index=False)

    checks = {
        "trade_count": int(len(selected)),
        "deterministic_result_hash": bool(deterministic),
        "required_evidence_nonnull": bool(nonnull.all()),
        "causal_event_order": bool(causal_order.all()),
        "entry_stop_target_geometry": bool(price_geometry.all()),
        "data_invalid_excluded": bool(valid_data.all()),
        "all_five_event_bars_found_in_raw_data": bool(raw_matches_series.all()),
        "raw_event_match_count": int(raw_matches_series.sum()),
        "prefix_through_2024_matches_full_run": bool(prefix_pass),
        "prefix_trade_count": int(len(prefix_rerun)),
        "duplicate_signal_count": int(selected.duplicated(IDENTITY).sum()),
        "result_sha256": computed_result_hash,
        "pass": bool(
            deterministic
            and nonnull.all()
            and causal_order.all()
            and price_geometry.all()
            and valid_data.all()
            and raw_matches_series.all()
            and prefix_pass
            and not selected.duplicated(IDENTITY).any()
        ),
    }
    (AUDIT_DIR / "audit.json").write_text(json.dumps(checks, indent=2), encoding="utf-8")
    print(json.dumps(checks, indent=2))
    if not checks["pass"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
