from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_corrected_engine_3month_report as base_report
import run_frequency_expansion_tests as frequency
import run_selected_candidates_full_data_report as selected
from backtest.data_loader import load_ohlcv
from backtest.risk import apply_pair_risk_rule
from backtest.strategy import compute_first30_range, run_backtest, trades_to_frame


REPORT_DIR = ROOT / "outputs" / "reports" / "old_candidate_rule_ablation_v3"
RAW_DIR = ROOT / "data" / "raw"
TIMEZONE = "America/New_York"
HOLDOUT_START = pd.Timestamp("2025-01-01", tz=TIMEZONE)


@dataclass(frozen=True)
class Leg:
    system: str
    key: str
    label: str
    candidate: selected.SelectedCandidate
    baseline: object


@dataclass(frozen=True)
class Variant:
    name: str
    description: str
    changes: dict[str, object]


def main() -> None:
    started = time.perf_counter()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    cache_dir = REPORT_DIR / "cache"
    cache_dir.mkdir(exist_ok=True)

    legs = build_legs()
    loaded = load_data(legs)
    thresholds = development_thresholds(legs, loaded)
    runs = run_all(legs, loaded, thresholds, cache_dir)
    leg_rows = build_leg_rows(runs)
    pair_rows = build_pair_rows(legs, runs)

    leg_frame = pd.DataFrame(leg_rows)
    pair_frame = pd.DataFrame(pair_rows)
    leg_frame.to_csv(REPORT_DIR / "leg_results.csv", index=False)
    pair_frame.to_csv(REPORT_DIR / "pair_results.csv", index=False)
    pd.DataFrame(
        [
            {"leg": key, "threshold": value, "learned_from": "pre_2025_only"}
            for key, value in thresholds.items()
        ]
    ).to_csv(REPORT_DIR / "development_thresholds.csv", index=False)

    write_report(leg_frame, pair_frame, thresholds, time.perf_counter() - started)
    write_manifest(legs, loaded, thresholds, time.perf_counter() - started)
    print(f"Wrote: {REPORT_DIR}")
    print(f"Runtime seconds: {time.perf_counter() - started:.1f}")


def build_legs() -> list[Leg]:
    funded_nq = frequency.funded_nq()
    funded_spx = frequency.funded_spx()
    phase_nq = frequency.phase_nq()
    phase_spx = frequency.phase_spx()
    return [
        Leg("funded", "funded_nq", "NQ funded", funded_nq, frequency.funded_nq_config()),
        Leg("funded", "funded_spx", "SPX funded", funded_spx, frequency.funded_spx_config()),
        Leg("phase", "phase_nq", "NQ phase", phase_nq, frequency.phase_nq_config()),
        Leg("phase", "phase_spx", "SPX phase", phase_spx, frequency.phase_spx_config()),
    ]


def variants(leg: Leg, threshold: float) -> list[Variant]:
    common = [
        Variant("baseline", "Eski kural", {}),
        Variant("rejection_close", "Sweep sonrası seviyenin içine kapanış zorunlu", {"require_sweep_rejection_close": True}),
        Variant(
            "fresh_sweep",
            "Aynı yön takip setup'ı için yeni sweep zorunlu",
            {"allow_same_direction_followup_without_fresh_sweep": False},
        ),
        Variant(
            "first30_dev_q60",
            "İlk 30 dakika eşiği yalnız 2025 öncesi q60",
            {"first30_range_filter": "live_safe_max", "first30_range_max": threshold},
        ),
        Variant(
            "first30_rolling_20x1_25",
            "İlk 30 dakika eşiği önceki 20 seans medyanının 1.25 katı",
            {"first30_range_filter": "off", "first30_range_max": None},
        ),
    ]
    if leg.key == "funded_nq":
        return common + [
            Variant("entry_start", "FVG başlangıcından giriş", {"fvg_entry_mode": "start"}),
            Variant(
                "strong_swing_2",
                "Seans likiditesi yerine en az 2 temaslı güçlü swing",
                {"session_liquidity_only": False, "swing_liquidity_mode": "strong_only", "strong_swing_min_touches": 2},
            ),
            Variant("rr_2_5", "TP 3R yerine 2.5R", {"reward_r": 2.5}),
            Variant("window_1045", "İşlem penceresini 10:45'e uzat", {"trade_window_end": "10:45"}),
        ]
    if leg.key == "funded_spx":
        return common + [
            Variant("exclude_tuesday", "Salı işlemlerini kaldır", {"allowed_weekdays": "Wednesday,Friday"}),
            Variant("entry_midpoint", "FVG orta noktasından giriş", {"fvg_entry_mode": "midpoint"}),
            Variant("max_1", "Günde en fazla 1 işlem", {"max_trades_per_day": 1}),
            Variant(
                "strong_swing_2",
                "Bütün swingler yerine en az 2 temaslı güçlü swing",
                {"session_liquidity_only": False, "swing_liquidity_mode": "strong_only", "strong_swing_min_touches": 2},
            ),
            Variant("rr_2_5", "TP 2R yerine 2.5R", {"reward_r": 2.5}),
        ]
    if leg.key == "phase_nq":
        return common + [
            Variant("strong_swing_2", "Güçlü swing temasını 3'ten 2'ye indir", {"strong_swing_min_touches": 2}),
            Variant("entry_midpoint", "FVG orta noktasından giriş", {"fvg_entry_mode": "midpoint"}),
            Variant("entry_quarter_25", "FVG yüzde 25 derinlikten giriş", {"fvg_entry_mode": "quarter_25"}),
            Variant("rr_2_5", "TP 3R yerine 2.5R", {"reward_r": 2.5}),
            Variant("window_1045", "İşlem penceresini 10:45'e uzat", {"trade_window_end": "10:45"}),
        ]
    return common + [
        Variant("exclude_tuesday", "Salı işlemlerini kaldır", {"allowed_weekdays": "Wednesday,Friday"}),
        Variant("max_1", "Günde en fazla 1 işlem", {"max_trades_per_day": 1}),
        Variant(
            "strong_swing_2",
            "Bütün swingler yerine en az 2 temaslı güçlü swing",
            {"session_liquidity_only": False, "swing_liquidity_mode": "strong_only", "strong_swing_min_touches": 2},
        ),
        Variant("entry_start", "FVG başlangıcından giriş", {"fvg_entry_mode": "start"}),
        Variant("entry_quarter_25", "FVG yüzde 25 derinlikten giriş", {"fvg_entry_mode": "quarter_25"}),
        Variant("rr_2", "TP 2.5R yerine 2R", {"reward_r": 2.0}),
        Variant("rr_3", "TP 2.5R yerine 3R", {"reward_r": 3.0}),
    ]


def load_data(legs: list[Leg]) -> dict[tuple[str, str], pd.DataFrame]:
    loaded: dict[tuple[str, str], pd.DataFrame] = {}
    for leg in legs:
        key = (leg.candidate.symbol, leg.candidate.timeframe)
        if key not in loaded:
            paths = base_report.filter_paths(RAW_DIR, *key)
            loaded[key] = load_ohlcv(paths).frame
    return loaded


def development_thresholds(
    legs: list[Leg], loaded: dict[tuple[str, str], pd.DataFrame]
) -> dict[str, float]:
    output: dict[str, float] = {}
    for leg in legs:
        frame = loaded[(leg.candidate.symbol, leg.candidate.timeframe)]
        dates = pd.to_datetime(frame["date"])
        development_dates = sorted(frame.loc[dates.dt.year < 2025, "date"].unique())
        ranges = [compute_first30_range(frame, date) for date in development_dates]
        values = pd.Series([value for value in ranges if value is not None], dtype=float)
        output[leg.key] = round(float(values.quantile(0.60)), 4)
    return output


def run_all(
    legs: list[Leg],
    loaded: dict[tuple[str, str], pd.DataFrame],
    thresholds: dict[str, float],
    cache_dir: Path,
) -> dict[tuple[str, str], dict[str, object]]:
    output: dict[tuple[str, str], dict[str, object]] = {}
    for leg in legs:
        frame = loaded[(leg.candidate.symbol, leg.candidate.timeframe)]
        for variant in variants(leg, thresholds[leg.key]):
            config = replace(leg.baseline, **variant.changes)
            digest = sha256(json.dumps(asdict(config), sort_keys=True).encode()).hexdigest()[:20]
            cache_path = cache_dir / f"{leg.key}_{digest}.csv"
            print(f"{leg.key}: {variant.name}")
            if cache_path.exists():
                trades = pd.read_csv(cache_path)
            else:
                trades = trades_to_frame(run_backtest(frame.copy(), config).trades)
                trades.to_csv(cache_path, index=False)
            if variant.name == "first30_rolling_20x1_25":
                trades = apply_rolling_first30(trades, frame, ratio=1.25)
            output[(leg.key, variant.name)] = {
                "leg": leg,
                "variant": variant,
                "config": config,
                "trades": trades,
            }
    return output


def apply_rolling_first30(trades: pd.DataFrame, frame: pd.DataFrame, ratio: float) -> pd.DataFrame:
    if trades.empty:
        return trades.copy()
    days = sorted(pd.to_datetime(frame["date"]).dt.date.unique())
    values = pd.Series(
        [compute_first30_range(frame, day) for day in days],
        index=days,
        dtype=float,
    )
    thresholds = values.shift(1).rolling(20, min_periods=20).median() * ratio
    entry = pd.to_datetime(trades["entry_time"], utc=True, format="mixed").dt.tz_convert(TIMEZONE)
    first30 = entry.dt.date.map(values)
    threshold = entry.dt.date.map(thresholds)
    after_ten = (entry.dt.hour > 10) | ((entry.dt.hour == 10) & (entry.dt.minute >= 0))
    blocked = after_ten & threshold.notna() & first30.notna() & (first30 > threshold)
    return trades.loc[~blocked].copy()


def segment(trades: pd.DataFrame, name: str) -> pd.DataFrame:
    if trades.empty or name == "all":
        return trades.copy()
    entry = pd.to_datetime(trades["entry_time"], utc=True, format="mixed").dt.tz_convert(TIMEZONE)
    if name == "development_pre_2025":
        return trades[entry < HOLDOUT_START].copy()
    return trades[entry >= HOLDOUT_START].copy()


def build_leg_rows(runs: dict[tuple[str, str], dict[str, object]]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for (leg_key, variant_name), run in runs.items():
        for segment_name in ["development_pre_2025", "holdout_2025_plus", "all"]:
            rows.append(
                {
                    "system": run["leg"].system,
                    "leg": leg_key,
                    "label": run["leg"].label,
                    "variant": variant_name,
                    "description": run["variant"].description,
                    "segment": segment_name,
                    **frequency.stats(segment(run["trades"], segment_name)),
                }
            )
    return rows


def tagged(trades: pd.DataFrame, leg: Leg) -> pd.DataFrame:
    if trades.empty:
        return trades.copy()
    frame = trades.copy()
    frame["group"] = leg.system
    frame["label"] = leg.label
    return frame


def capped_pair(left: dict[str, object], right: dict[str, object]) -> pd.DataFrame:
    frames = [tagged(left["trades"], left["leg"]), tagged(right["trades"], right["leg"])]
    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        return pd.DataFrame()
    return apply_pair_risk_rule(pd.concat(frames, ignore_index=True), -1.0)


def build_pair_rows(
    legs: list[Leg], runs: dict[tuple[str, str], dict[str, object]]
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    by_system = {system: [leg for leg in legs if leg.system == system] for system in {leg.system for leg in legs}}
    for system, pair_legs in by_system.items():
        nq = next(leg for leg in pair_legs if "nq" in leg.key)
        spx = next(leg for leg in pair_legs if "spx" in leg.key)
        baseline = capped_pair(runs[(nq.key, "baseline")], runs[(spx.key, "baseline")])
        cases = [("pair", "baseline", "Eski çift", baseline)]
        for changed, other in [(nq, spx), (spx, nq)]:
            for variant in variants(changed, 0.0):
                if variant.name == "baseline":
                    continue
                pair = capped_pair(runs[(changed.key, variant.name)], runs[(other.key, "baseline")])
                cases.append((changed.key, variant.name, variant.description, pair))
        combo_specs = (
            [
                ("combo_entry_rr", "entry_start", "rr_2_5", "NQ başlangıç girişi + SPX 2.5R"),
                ("combo_entry_strong", "entry_start", "strong_swing_2", "NQ başlangıç girişi + SPX güçlü swing"),
            ]
            if system == "funded"
            else [
                ("combo_rejection_quarter", "rejection_close", "entry_quarter_25", "NQ rejection + SPX yüzde 25 giriş"),
                ("combo_rejection_rr3", "rejection_close", "rr_3", "NQ rejection + SPX 3R"),
                ("combo_holdout_probe", "strong_swing_2", "entry_start", "NQ güçlü swing + SPX başlangıç girişi"),
            ]
        )
        for name, nq_variant, spx_variant, description in combo_specs:
            pair = capped_pair(runs[(nq.key, nq_variant)], runs[(spx.key, spx_variant)])
            cases.append(("pair_combo", name, description, pair))
        for changed_leg, variant_name, description, pair in cases:
            for segment_name in ["development_pre_2025", "holdout_2025_plus", "all"]:
                current = frequency.stats(segment(pair, segment_name))
                base = frequency.stats(segment(baseline, segment_name))
                rows.append(
                    {
                        "system": system,
                        "changed_leg": changed_leg,
                        "variant": variant_name,
                        "description": description,
                        "segment": segment_name,
                        **current,
                        "delta_trades": current["trades"] - base["trades"],
                        "delta_wr": round(float(current["win_rate"]) - float(base["win_rate"]), 2),
                        "delta_net_r": round(float(current["net_r"]) - float(base["net_r"]), 2),
                        "delta_dd_r": round(float(current["max_drawdown_r"]) - float(base["max_drawdown_r"]), 2),
                    }
                )
    return rows


def write_report(
    leg: pd.DataFrame,
    pair: pd.DataFrame,
    thresholds: dict[str, float],
    runtime_seconds: float,
) -> None:
    development = pair[pair["segment"] == "development_pre_2025"].copy()
    holdout = pair[pair["segment"] == "holdout_2025_plus"].copy()
    all_rows = pair[pair["segment"] == "all"].copy()
    ranked = development[development["variant"] != "baseline"].sort_values(
        ["delta_net_r", "delta_dd_r", "delta_wr"], ascending=False
    )
    lines = [
        "# Old Candidate Rule Ablation V3",
        "",
        "Her testte yalnız bir kural değiştirilmiştir. Çalışan/live sistem değiştirilmemiştir.",
        "2025 öncesi geliştirme, 2025+ holdout olarak ayrılmıştır. First30 q60 eşikleri yalnız geliştirme döneminden öğrenilmiştir.",
        "SPX context-source blokları bu motorda config alanı olmadığı için burada tekrar edilmedi; ayrı V2 testinde doğrulandı.",
        f"Runtime: {runtime_seconds:.1f} saniye",
        "",
        "## Development-only q60 eşikleri",
        "",
        base_report.markdown_table(pd.DataFrame([{"leg": key, "q60": value} for key, value in thresholds.items()])),
        "",
        "## Geliştirme sıralaması",
        "",
        base_report.markdown_table(ranked),
        "",
        "## Holdout sonuçları",
        "",
        base_report.markdown_table(holdout),
        "",
        "## Tam dönem sonuçları",
        "",
        base_report.markdown_table(all_rows),
        "",
        "## Tekil aday sonuçları",
        "",
        base_report.markdown_table(leg),
        "",
    ]
    (REPORT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


def write_manifest(
    legs: list[Leg],
    loaded: dict[tuple[str, str], pd.DataFrame],
    thresholds: dict[str, float],
    runtime_seconds: float,
) -> None:
    payload = {
        "status": "LOCAL_RESEARCH_ONLY",
        "live_system_changed": False,
        "holdout_start": HOLDOUT_START.isoformat(),
        "threshold_learning_period": "pre_2025_only",
        "thresholds": thresholds,
        "legs": [leg.key for leg in legs],
        "data_rows": {f"{symbol}/{timeframe}": len(frame) for (symbol, timeframe), frame in loaded.items()},
        "script_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "runtime_seconds": round(runtime_seconds, 2),
    }
    (REPORT_DIR / "manifest.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
