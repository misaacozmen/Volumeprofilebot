from __future__ import annotations

from dataclasses import asdict
import argparse
import json
import os
from hashlib import sha256
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import run_engine_path_comparison_2025_feb_mar as comparison
import run_main_candidate_filter_tests as filters
from backtest.engine_pipeline import EngineLeg, run_canonical_pair_pipeline
from backtest.evaluation_window import classify_sessions, coverage_report
from backtest.market_calendar import signed_market_dates
from backtest.manual_state import ManualStateConfig, build_independent_htf_frame
from backtest.risk_xray import build_risk_xray, write_risk_xray
from backtest.reacquisition_contract import apply_verified_reacquisitions, validate_final_manifest
from backtest.state_audit import pipeline_records, prefix_invariance_violations


REPORT_DIR = ROOT / "outputs" / "reports" / "engine_reliability_audit_2025_feb_mar"
FROZEN_INVENTORY = ROOT / "data/provenance/dukascopy_v4/frozen_invalid_leg_days_v4.csv"
REACQUISITION_MANIFEST = ROOT / "data/provenance/dukascopy_v4/acquisition_v5/reacquisition_manifest_v5.json"
REACQUISITION_ROOT = ROOT / "data/provenance/dukascopy_v4/acquisition_v5/bundles"


def main(
    report_dir: Path | None = None,
    inventory_path: Path | None = None,
    reacquisition_manifest: Path | None = None,
) -> bool:
    started = time.perf_counter()
    from audit_dukascopy_reacquisition_v5 import audit_inventory
    inventory = (inventory_path or FROZEN_INVENTORY).resolve()
    manifest_path = (reacquisition_manifest or REACQUISITION_MANIFEST).resolve()
    preflight = audit_inventory(
        inventory,
        REACQUISITION_ROOT,
        ROOT / "outputs/reports/dukascopy_reacquisition_v5",
        legacy_root=ROOT / "data/provenance/dukascopy_v4/reacquired_session",
    )
    if preflight["residual_count"] != 0:
        raise RuntimeError(f"reacquisition residual is {preflight['residual_count']}; reliability audit is blocked")
    manifest = validate_final_manifest(manifest_path, provenance_root=ROOT / "data/provenance/dukascopy_v4", inventory_path=inventory)
    target_report_dir = (report_dir or REPORT_DIR).resolve()
    target_report_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix=f".{target_report_dir.name}.", dir=target_report_dir.parent))
    baseline = target_report_dir / "phase0_canonical_baseline.json"
    if baseline.is_file():
        shutil.copy2(baseline, staging_dir / baseline.name)

    loaded = filters.load_data()
    loaded = apply_verified_reacquisitions(loaded, manifest, provenance_root=ROOT / "data/provenance/dukascopy_v4", frame_loader=lambda path: filters.load_ohlcv([path]).frame)
    configs = comparison.build_active_configs(loaded)
    dates = signed_market_dates(comparison.START_DATE, comparison.END_DATE)
    legs = [
        EngineLeg(
            key,
            comparison.select_window(loaded[(symbol, timeframe)].copy()),
            configs[key],
        )
        for key, (symbol, timeframe) in comparison.SYMBOLS.items()
    ]
    state_config = ManualStateConfig()

    determinism_root_a = Path(tempfile.mkdtemp(prefix="reliability-determinism-a-", dir=staging_dir))
    determinism_root_b = Path(tempfile.mkdtemp(prefix="reliability-determinism-b-", dir=staging_dir))
    previous_stage = os.environ.get("OTOBT_RELIABILITY_STAGING_ROOT")
    os.environ["OTOBT_RELIABILITY_STAGING_ROOT"] = str(determinism_root_a)
    first = run_canonical_pair_pipeline(legs, dates, state_config=state_config)
    os.environ["OTOBT_RELIABILITY_STAGING_ROOT"] = str(determinism_root_b)
    second = run_canonical_pair_pipeline(legs, dates, state_config=state_config)
    if previous_stage is None:
        os.environ.pop("OTOBT_RELIABILITY_STAGING_ROOT", None)
    else:
        os.environ["OTOBT_RELIABILITY_STAGING_ROOT"] = previous_stage
    deterministic = first.manifest["result_hash"] == second.manifest["result_hash"]

    first.decisions.to_csv(staging_dir / "canonical_decisions.csv", index=False)
    first.filled_after_pair_cap.to_csv(staging_dir / "filled_after_causal_pair_cap.csv", index=False)
    first.suppressed_by_pair_cap.to_csv(staging_dir / "suppressed_by_causal_pair_cap.csv", index=False)
    source_sets = [
        set(
            pd.to_datetime(leg.frame["time"], utc=True, format="mixed")
            .dt.tz_convert("America/New_York")
            .dt.date
        )
        for leg in legs
    ]
    source_dates = set.intersection(*source_sets) if source_sets else set()
    valid_sets = [
        {pd.Timestamp(day.trade_date).date() for day in result.days if day.data_state == "VALID"}
        for result in first.leg_results.values()
    ]
    invalid_dates = {
        pd.Timestamp(day.trade_date).date()
        for result in first.leg_results.values()
        for day in result.days
        if day.data_state == "INVALID"
    }
    coverage = coverage_report(
        classify_sessions(dates, source_dates=source_dates, invalid_data=invalid_dates),
        evaluated=(set.intersection(*valid_sets) if valid_sets else set()) & set(dates),
    )
    write_risk_xray(
        build_risk_xray(
            first.filled_after_pair_cap,
            eligible_dates=dates,
            coverage=coverage,
            provenance_hashes=first.manifest.get("data_hashes", {}),
            funnel={
                "proposed": len(first.decisions),
                "risk_approved": None,
                "staged": None,
                "filled": len(first.filled_after_pair_cap),
                "rejected": int((first.decisions.get("final_decision", pd.Series(dtype=str)) == "SKIP").sum()),
                "expired": 0,
            },
        ),
        staging_dir,
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
    data_quality.to_csv(staging_dir / "data_quality_by_day.csv", index=False)
    pipeline.to_csv(staging_dir / "pipeline_trace.csv", index=False)
    prefix.to_csv(staging_dir / "prefix_violations.csv", index=False)

    tests = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    (staging_dir / "core_test_output.txt").write_text(
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
    forward_shadow_ready = (
        tests.returncode == 0
        and deterministic
        and prefix_violation_count == 0
        and invalid_days == 0
        and int(coverage.get("missing_sessions", 0) or 0) == 0
        and int(coverage.get("invalid_sessions", 0) or 0) == 0
        and int(coverage.get("valid_sessions", 0) or 0) == int(coverage.get("evaluated_sessions", 0) or 0)
    )
    (staging_dir / "run_manifest.json").write_text(
        json.dumps(
            {
                **first.manifest,
                "coverage": coverage,
                "forward_shadow_ready": forward_shadow_ready,
                "reacquisition_manifest_sha256": sha256(manifest_path.read_bytes()).hexdigest(),
                "reacquisition_semantic_root_sha256": manifest["semantic_root_sha256"],
                "determinism_staging_roots": [determinism_root_a.name, determinism_root_b.name],
            },
            indent=2,
            sort_keys=True,
            default=str,
        ),
        encoding="utf-8",
    )
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
                "net_r": float(first.filled_after_pair_cap["r_multiple"].sum()) if not first.filled_after_pair_cap.empty else 0.0,
                "pair_cap_suppressed": len(first.suppressed_by_pair_cap),
                "intrabar_ambiguities_labeled": ambiguity_count,
                "forward_shadow_ready": forward_shadow_ready,
            }
        ]
    )
    summary.to_csv(staging_dir / "summary.csv", index=False)
    write_report(summary.iloc[0].to_dict(), first.manifest, tests.stdout.strip(), time.perf_counter() - started, report_dir=staging_dir)
    print(summary.to_string(index=False))
    backup_dir = target_report_dir.parent / f".{target_report_dir.name}.previous"
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    try:
        if target_report_dir.exists():
            os.replace(target_report_dir, backup_dir)
        os.replace(staging_dir, target_report_dir)
    except Exception:
        if not target_report_dir.exists() and backup_dir.exists():
            os.replace(backup_dir, target_report_dir)
        raise
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
    print(f"Wrote: {target_report_dir}")
    return bool(summary["forward_shadow_ready"].iloc[0])


def write_report(
    summary: dict[str, object],
    manifest: dict[str, object],
    test_output: str,
    runtime_seconds: float,
    *,
    report_dir: Path,
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
    (report_dir / "report_tr.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the clean canonical engine reliability audit.")
    parser.add_argument(
        "--report-dir",
        type=Path,
        help="Write the fresh attestation to this directory; never reuse an existing report.",
    )
    parser.add_argument("--frozen-inventory", type=Path, default=FROZEN_INVENTORY)
    parser.add_argument("--reacquisition-manifest", type=Path, default=REACQUISITION_MANIFEST)
    arguments = parser.parse_args()
    if not main(arguments.report_dir, arguments.frozen_inventory, arguments.reacquisition_manifest):
        raise SystemExit("engine reliability audit is not ready for release")
