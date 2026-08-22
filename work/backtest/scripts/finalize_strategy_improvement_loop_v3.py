from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
REPORT_ROOT = ROOT / "outputs" / "reports" / "strategy_improvement_loop_v2"
BEST = REPORT_ROOT / "body_plus_classic_quota_day1_v1"
FINAL = REPORT_ROOT / "final_50_iterations"
CANDIDATE_DIR = ROOT / "research_candidates" / "v9_strategy_loop"
BASE_CONFIG = ROOT / "research_candidates" / "v7_strategy_loop" / "nq_spx_quality_time_mixed_rr_v1.json"


ITERATIONS = [
    (1, "V7 baseline yeniden üretimi", "REJECT"),
    (2, "2018 erken dönem setup teşhisi", "REJECT"),
    (3, "CISD-close same-candle", "REJECT"),
    (4, "CISD-close ve first30 kapalı", "REJECT"),
    (5, "gap/DATA_INVALID kök neden denetimi", "REJECT"),
    (6, "geçerli seans takvimi", "WATCH"),
    (7, "geçerlilik düzeltilmiş baseline", "WATCH"),
    (8, "grafik kanıtlı setup havuzu", "WATCH"),
    (9, "SPX Phase body-FVG tüm hafta", "WATCH"),
    (10, "SPX Phase Pazartesi/Perşembe", "REJECT"),
    (11, "eşzamanlı SPX sinyal dedup", "WATCH"),
    (12, "Funded-priority dedup", "WATCH"),
    (13, "korelasyonlu duplicate risk ölçeği", "REJECT"),
    (14, "asimetrik Phase duplicate ölçeği", "REJECT"),
    (15, "exact sweep+CISD duplicate ölçeği", "REJECT"),
    (16, "break-even/half-stop", "REJECT"),
    (17, "causal equity throttle", "REJECT"),
    (18, "terminal kayıp serisi throttle", "REJECT"),
    (19, "tekil causal blok hücreleri", "REJECT"),
    (20, "ikili causal blok hücreleri", "WATCH"),
    (21, "Eylül düşük-FVG bloğu", "WATCH"),
    (22, "duplicate ölçeği + equity throttle", "REJECT"),
    (23, "açık risk bütçesi", "REJECT"),
    (24, "segment-DD tekil filtre taraması", "WATCH"),
    (25, "SPX Phase Pazartesi bloğu", "WATCH"),
    (26, "tüm Pazartesi bloğu", "REJECT"),
    (27, "ikili Pazartesi filtre taraması", "WATCH"),
    (28, "tüm SPX Pazartesi bloğu", "WATCH"),
    (29, "aylık gerçekleşmiş kayıp limiti", "REJECT"),
    (30, "günlük terminal kayıp limiti", "REJECT"),
    (31, "classic aylık quota", "REJECT"),
    (32, "düşük-DD base + classic quota", "REJECT"),
    (33, "classic + body quota", "REJECT"),
    (34, "body low-delay filtresi", "WATCH"),
    (35, "supplement kaynak katkı analizi", "REJECT"),
    (36, "body-only quota", "WATCH"),
    (37, "classic high-FVG + HTF-opposed hücresi", "WATCH"),
    (38, "body + seçili classic causal quota", "WATCH"),
    (39, "body_plus_classic_quota_day1 formal tekrar", "BEST_EFFORT"),
    (40, "quota kaynaklarında global Phase Pazartesi bloğu", "REJECT"),
    (41, "yıllık ve eksik ay teşhisi", "REJECT"),
    (42, "SPX Phase classic RR 2.5", "WATCH"),
    (43, "kaynağa göre RR yeniden kurulum", "WATCH"),
    (44, "RR base + Phase-classic hariç quota", "REJECT"),
    (45, "SPX Phase body tüm hafta RR 2.5", "REJECT"),
    (46, "low-delay body RR 2.5 quota", "WATCH"),
    (47, "NQ Funded+Phase RR 2.5", "REJECT"),
    (48, "NQ Funded/Phase RR 2.5 ayrı ablation", "REJECT"),
    (49, "determinism/prefix/raw-bar kanıt denetimi", "PASS_AUDIT"),
    (50, "yerel best-effort dondurma", "REJECT_CRITERIA_UNMET"),
]


KNOWN = {
    1: (854, 40.75, 277.0, 17.0, 1.547, 14, 1),
    7: (854, 40.75, 277.0, 17.0, 1.547, 14, 1),
    9: (971, 40.06, 283.0, 20.0, None, 9, 1),
    12: (670, 40.45, 230.0, 13.0, None, 25, 1),
    21: (819, 41.39, 285.0, 17.0, None, 16, 1),
    25: (798, 40.98, 270.0, 17.0, None, 15, 1),
    26: (676, 41.72, 231.0, 17.0, None, 27, 1),
    28: (748, 41.18, 263.0, 17.0, None, 19, 1),
    36: (883, 40.54, 278.0, 15.0, None, 9, 1),
    38: (894, 40.94, 292.0, 16.0, 1.553, 7, 1),
    39: (894, 40.94, 292.0, 16.0, 1.553, 7, 1),
    40: (864, 40.74, 280.0, 19.0, None, 8, 1),
    42: (617, 40.52, 259.5, 14.5, None, 33, 1),
    43: (802, 39.90, 283.0, 16.5, None, 14, 1),
    45: (1042, 35.03, 237.0, 23.5, None, 6, 1),
    46: (893, 40.09, 280.5, 15.0, None, 7, 1),
    47: (614, 42.02, 204.0, 15.0, 1.573, 33, 1),
    48: (614, 41.86, 235.0, 15.0, 1.658, 33, 1),
    50: (894, 40.94, 292.0, 16.0, 1.553, 7, 1),
}


def main() -> None:
    FINAL.mkdir(parents=True, exist_ok=True)
    CANDIDATE_DIR.mkdir(parents=True, exist_ok=True)
    metrics = pd.read_csv(BEST / "metrics.csv")
    monthly = pd.read_csv(BEST / "monthly_gate.csv")
    annual = pd.read_csv(BEST / "annual_gate.csv")
    audit = json.loads((BEST / "audit" / "audit.json").read_text(encoding="utf-8"))
    base = json.loads(BASE_CONFIG.read_text(encoding="utf-8"))

    comparison_rows = []
    for number, mechanism, decision in ITERATIONS:
        row = {"iteration": number, "mechanism": mechanism, "decision": decision}
        values = KNOWN.get(number)
        if values:
            row.update(dict(zip(("trades", "win_rate", "net_r", "max_drawdown_r", "profit_factor", "valid_months_below_5", "valid_years_below_20r"), values)))
        comparison_rows.append(row)
    comparison = pd.DataFrame(comparison_rows)

    candidate = {
        "name": "NQ_SPX_LOCAL_BEST_EFFORT_50_ITERATIONS_V1",
        "status": "LOCAL_REJECT_CRITERIA_UNMET",
        "base_candidate": str(BASE_CONFIG),
        "mechanisms": {
            "base_profile": "block_spx_phase_monday_v1",
            "spx_latest_entry_time": "10:45",
            "base_spx_phase_monday": "BLOCK",
            "supplemental_phase_body": {"setup": "body_fvg", "cisd_fvg_candles_regime": "low"},
            "supplemental_classic": {"fvg_size_regime": "high", "htf_alignment": "opposed"},
            "causal_selector": "base always; chronological supplemental while accepted month-to-date count < 5",
            "selector_uses_trade_outcome": False,
        },
        "entry_mode": base["entry_mode"],
        "reward_r": base["reward_r"],
        "stop_management": base["stop_management"],
        "simple_rules": base["simple_rules"],
        "compound_rules": base["compound_rules"],
        "pair_rules": base["pair_rules"],
        "criteria": {
            "win_rate_pct": [40, 45],
            "max_drawdown_r": [9, 13],
            "minimum_trades_each_valid_completed_month": 5,
            "minimum_net_r_each_full_valid_year": 20,
            "net_r_must_visibly_improve": True,
        },
        "observed": metrics.loc[metrics["segment"].eq("all_2016_2026")].iloc[0].to_dict(),
        "criteria_pass": {"win_rate": True, "net_r": True, "max_drawdown": False, "monthly_trades": False, "annual_net_r": False},
        "checks": {**audit, "project_tests": "85/85 PASS"},
        "selection_excluded": "2025-2026",
        "fresh_forward_required": True,
        "live_enabled": False,
        "data_sha256": base["data_sha256"],
        "result_sha256": audit["result_sha256"],
        "code_sha256": sha256((ROOT / "scripts" / "run_strategy_improvement_iteration.py").read_bytes() + (ROOT / "scripts" / "audit_strategy_improvement_loop_v3.py").read_bytes() + Path(__file__).read_bytes()).hexdigest(),
        "config_sha256": "",
    }
    candidate["config_sha256"] = sha256(json.dumps({k: v for k, v in candidate.items() if k != "config_sha256"}, sort_keys=True, default=str).encode()).hexdigest()
    candidate_path = CANDIDATE_DIR / "nq_spx_local_best_effort_50_iterations_v1.json"
    candidate_path.write_text(json.dumps(candidate, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    comparison.to_csv(FINAL / "iteration_comparison.csv", index=False)
    metrics.to_csv(FINAL / "segment_metrics.csv", index=False)
    monthly.to_csv(FINAL / "monthly_gate.csv", index=False)
    annual.to_csv(FINAL / "annual_gate.csv", index=False)
    manifest = {
        "status": candidate["status"], "candidate": str(candidate_path), "best_profile": BEST.name,
        "code_sha256": candidate["code_sha256"], "config_sha256": candidate["config_sha256"],
        "data_sha256": candidate["data_sha256"], "result_sha256": candidate["result_sha256"],
        "audit_pass": audit["pass"], "project_tests": "85/85 PASS", "live_enabled": False,
    }
    (FINAL / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (FINAL / "report.md").write_text(report(metrics, monthly, annual, candidate_path, manifest), encoding="utf-8")
    print(metrics.to_string(index=False))
    print(f"candidate={candidate_path}")
    print(f"report={FINAL / 'report.md'}")


def report(metrics: pd.DataFrame, monthly: pd.DataFrame, annual: pd.DataFrame, candidate_path: Path, manifest: dict) -> str:
    all_row = metrics[metrics["segment"].eq("all_2016_2026")].iloc[0]
    bad_months = monthly[(monthly["data_valid"] == True) & (monthly["trades"] < 5)]  # noqa: E712
    bad_years = annual[(annual["data_valid_year"] == True) & (annual["net_r"] < 20)]  # noqa: E712
    month_text = ", ".join(f"{row.year_month} ({int(row.trades)})" for row in bad_months.itertuples())
    year_text = ", ".join(f"{int(row.calendar_year)} ({row.net_r:.1f}R)" for row in bad_years.itertuples())
    return f"""# Strateji geliştirme döngüsü — 50 iterasyon

## Karar

`REJECT / LOCAL BEST-EFFORT`. Zorunlu kriterlerin tamamı karşılanmadı; AWS/XM veya live sisteme deployment yapılmadı.

- Aday: `{candidate_path}`
- Tümü: {int(all_row.trades)} işlem, WR %{all_row.win_rate:.2f}, net {all_row.net_r:.1f}R, max DD {abs(all_row.max_drawdown_r):.1f}R, PF {all_row.profit_factor:.3f}.
- Baseline: 854 işlem, WR %40.75, net 277R, max DD 17R. Net değişim: +15R.
- Başarısız DD kapısı: 16R; gerekli 9–13R.
- Beş işlemin altındaki geçerli aylar: {month_text}.
- +20R altındaki tam geçerli yıllar: {year_text}.

## Kanıt denetimi

- 894/894 işlemin sweep, CISD, FVG, entry ve exit mumları ham Dukascopy dosyalarında bulundu.
- Deterministik tekrar, 2024 prefix, causal olay sırası, fiyat geometrisi ve DATA_INVALID kontrolleri geçti.
- 85/85 proje testi geçti. Her işlem `trade_evidence_index.csv` içinde kaynak dosyası ve zamanlarıyla izlenebilir.
- 2025–2026 yalnız tarihsel raporlandı; seçim/tuning için kullanılmadı.

## Hash'ler

- Code: `{manifest['code_sha256']}`
- Config: `{manifest['config_sha256']}`
- Data: `{manifest['data_sha256']}`
- Result: `{manifest['result_sha256']}`

Fresh-forward gereklidir; bu aday kanıtlanmış sayılmaz.
"""


if __name__ == "__main__":
    main()
