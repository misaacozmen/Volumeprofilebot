from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
import time

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import run_engine_path_comparison_2025_feb_mar as comparison
import run_main_candidate_filter_tests as filters
from backtest.engine_pipeline import EngineLeg, run_canonical_pair_pipeline
from backtest.manual_state import ManualStateConfig, build_independent_htf_frame
from backtest.state_audit import pipeline_records, prefix_invariance_violations


REPORT_DIR = ROOT / "outputs" / "reports" / "engine_reliability_audit_2025_feb_mar"


def main() -> None:
    started = time.perf_counter()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    for path in REPORT_DIR.iterdir():
        if path.is_file():
            path.unlink()

    loaded = filters.load_data()
    configs = comparison.build_active_configs(loaded)
    dates = [item.date() for item in pd.bdate_range(comparison.START_DATE, comparison.END_DATE)]
    legs = [
        EngineLeg(
            key,
            comparison.select_window(loaded[(symbol, timeframe)].copy()),
            configs[key],
        )
        for key, (symbol, timeframe) in comparison.SYMBOLS.items()
    ]
    state_config = ManualStateConfig()

    first = run_canonical_pair_pipeline(legs, dates, state_config=state_config)
    second = run_canonical_pair_pipeline(legs, dates, state_config=state_config)
    deterministic = first.manifest["result_hash"] == second.manifest["result_hash"]

    first.decisions.to_csv(REPORT_DIR / "canonical_decisions.csv", index=False)
    first.filled_after_pair_cap.to_csv(REPORT_DIR / "filled_after_causal_pair_cap.csv", index=False)
    first.suppressed_by_pair_cap.to_csv(REPORT_DIR / "suppressed_by_causal_pair_cap.csv", index=False)
    (REPORT_DIR / "run_manifest.json").write_text(
        json.dumps(first.manifest, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )

    data_rows: list[dict[str, object]] = []
    pipeline_rows: list[dict[str, object]] = []
    prefix_rows: list[dict[str, object]] = []
    prefix_checks = 0
    for leg in legs:
        result = first.leg_results[leg.key]
        filled_dates = {
            pd.Timestamp(item.date).date()
            for item in first.decisions.itertuples(index=False)
            if item.leg_key == leg.key and item.order_state == "FILLED"
        }
        focused_dates = {
            pd.Timestamp(value).date()
            for value in {
                "2025-02-07",
                "2025-02-13",
                "2025-02-14",
                "2025-02-21",
                "2025-03-24",
                "2025-03-26",
            }
        }
        sampled_dates = sorted(
            set(dates[::10])
            | filled_dates
            | focused_dates
        )
        htf_frame = build_independent_htf_frame(
            leg.frame,
            leg.config.timeframe,
            state_config.htf_timeframe_minutes,
        )
        for day in result.days:
            data_rows.append(
                {
                    "leg_key": leg.key,
                    "date": day.trade_date,
                    "data_state": day.data_state,
                    "data_reasons": "|".join(day.data_reasons),
                    "decision_count": len(day.decisions),
                    "fill_count": sum(item.order_state == "FILLED" for item in day.decisions),
                }
            )
            pipeline_rows.extend(
                {"leg_key": leg.key, **row}
                for row in pipeline_records(day)
            )
        for trade_date in sampled_dates:
            prefix_checks += len({"10:00", leg.config.trade_window_end})
            violations = prefix_invariance_violations(
                leg.frame,
                leg.config,
                trade_date,
                sorted({"10:00", leg.config.trade_window_end}),
                state_config,
                htf_frame=htf_frame,
            )
            prefix_rows.extend({"leg_key": leg.key, **asdict(item)} for item in violations)

    data_quality = pd.DataFrame(data_rows)
    pipeline = pd.DataFrame(pipeline_rows)
    prefix = pd.DataFrame(prefix_rows)
    data_quality.to_csv(REPORT_DIR / "data_quality_by_day.csv", index=False)
    pipeline.to_csv(REPORT_DIR / "pipeline_trace.csv", index=False)
    prefix.to_csv(REPORT_DIR / "prefix_violations.csv", index=False)

    tests = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_core_tests.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    (REPORT_DIR / "core_test_output.txt").write_text(
        tests.stdout + tests.stderr,
        encoding="utf-8",
    )

    ambiguity_count = int(
        first.decisions["intrabar_ambiguity"].fillna("").astype(str).ne("").sum()
        if not first.decisions.empty and "intrabar_ambiguity" in first.decisions
        else 0
    )
    invalid_days = int((data_quality["data_state"] == "INVALID").sum())
    prefix_violation_count = len(prefix)
    forward_shadow_ready = tests.returncode == 0 and deterministic and prefix_violation_count == 0
    summary = pd.DataFrame(
        [
            {
                "core_tests_passed": tests.returncode == 0,
                "deterministic_rerun": deterministic,
                "prefix_violation_count": prefix_violation_count,
                "prefix_checks": prefix_checks,
                "invalid_data_days_blocked": invalid_days,
                "canonical_decisions": len(first.decisions),
                "filled_pre_pair_cap": int((first.decisions["order_state"] == "FILLED").sum()),
                "filled_post_pair_cap": len(first.filled_after_pair_cap),
                "pair_cap_suppressed": len(first.suppressed_by_pair_cap),
                "intrabar_ambiguities_labeled": ambiguity_count,
                "forward_shadow_ready": forward_shadow_ready,
            }
        ]
    )
    summary.to_csv(REPORT_DIR / "summary.csv", index=False)
    write_report(summary.iloc[0].to_dict(), first.manifest, tests.stdout.strip(), time.perf_counter() - started)
    print(summary.to_string(index=False))
    print(f"Wrote: {REPORT_DIR}")


def write_report(
    summary: dict[str, object],
    manifest: dict[str, object],
    test_output: str,
    runtime_seconds: float,
) -> None:
    ready = bool(summary["forward_shadow_ready"])
    lines = [
        "# Motor Güvenilirliği Denetimi — Şubat–Mart 2025",
        "",
        "## Sonuç",
        "",
        (
            "**MEKANİK FORWARD SHADOW TESTE HAZIR.**"
            if ready
            else "**HENÜZ FORWARD SHADOW TESTE HAZIR DEĞİL.**"
        ),
        "",
        "Bu karar kârlılık, win-rate veya işlem sıklığına göre verilmedi. Yalnız nedensellik, "
        "determinism, veri bütünlüğü, state geçişleri ve açıklanabilirlik kapıları kullanıldı.",
        "",
        "## Güvenlik kapıları",
        "",
        f"- Çekirdek test: `{test_output}`",
        f"- Deterministik tekrar: `{summary['deterministic_rerun']}`",
        f"- Prefix/lookahead ihlali: `{summary['prefix_violation_count']}`",
        f"- Prefix kontrol noktası: `{summary['prefix_checks']}`",
        f"- Engellenen bozuk veri günü: `{summary['invalid_data_days_blocked']}`",
        f"- Etiketlenen intrabar belirsizliği: `{summary['intrabar_ambiguities_labeled']}`",
        f"- Causal pair-cap öncesi/sonrası fill: `{summary['filled_pre_pair_cap']}` / "
        f"`{summary['filled_post_pair_cap']}`",
        "",
        "## Teknik anlamı",
        "",
        "- Pair-cap yalnız entry anından önce gerçekleşmiş terminal sonuçlarını kullanır.",
        "- Eksik/çelişkili OHLCV günü NO_SETUP değil DATA_INVALID olur.",
        "- Her authority, thesis ve order deterministik kimliğe ve kronolojik trace'e sahiptir.",
        "- Config envelope motorun içinde uygulanır; dış post-filter zorunluluğu yoktur.",
        "- Aynı mum entry/target veya stop/target belirsizliği saklanmaz, etiketlenir.",
        "- Illegal state, duplicate order terminali ve geriye giden zaman çalışma anında hata verir.",
        "",
        "## Manifest",
        "",
        f"- Code hash: `{manifest['code_hash']}`",
        f"- Config hash: `{manifest['config_hash']}`",
        f"- Result hash: `{manifest['result_hash']}`",
        f"- Runtime: `{runtime_seconds:.1f}s`",
        "",
        "Forward shadow testte kurallar dondurulmalı; yalnız veri/karar trace'i toplanmalı. "
        "TP/SL sonuçları motor mantığını geriye dönük değiştirmek için kullanılmamalıdır.",
    ]
    (REPORT_DIR / "report_tr.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
