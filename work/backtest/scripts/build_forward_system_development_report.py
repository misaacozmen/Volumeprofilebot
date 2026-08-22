from __future__ import annotations

from pathlib import Path
import sys
import json

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.risk import apply_pair_risk_rule
FULL = ROOT / "outputs" / "reports" / "canonical_production_full_history_research"
ABLATION = ROOT / "outputs" / "reports" / "canonical_state_ablations_research"
LEAKAGE = ROOT / "outputs" / "reports" / "selector_calibration_leakage_audit"
ANCHORED = ROOT / "outputs" / "reports" / "anchored_threshold_holdout_research"
OUTPUT = ROOT / "outputs" / "reports" / "forward_system_development_review"


def context_stability(filled: pd.DataFrame) -> pd.DataFrame:
    work = filled.copy()
    work["date"] = pd.to_datetime(work["date"])
    work["segment"] = work["date"].lt(pd.Timestamp("2025-01-01")).map(
        {True: "development_pre_2025", False: "holdout_2025_plus"}
    )
    work["win"] = work["outcome"].eq("TP")
    work["r_multiple"] = pd.to_numeric(work["r_multiple"], errors="coerce").fillna(0.0)
    return (
        work.groupby(["leg_key", "context_source", "segment"], dropna=False)
        .agg(trades=("outcome", "size"), wins=("win", "sum"), net_r=("r_multiple", "sum"))
        .reset_index()
        .assign(win_rate_pct=lambda frame: (100 * frame["wins"] / frame["trades"]).round(2))
    )


def recommendation_table(
    context: pd.DataFrame,
    ablations: pd.DataFrame,
    counterfactuals: pd.DataFrame,
) -> pd.DataFrame:
    dead_fields = ablations[
        (ablations["variant"] != "baseline") & ablations["result_identical_to_baseline"].astype(bool)
    ]["variant"].tolist()
    all_rows = counterfactuals[counterfactuals["segment"] == "all"].set_index("variant")
    baseline = all_rows.loc["baseline"]
    nq_premarket = all_rows.loc["block_nq_PREMARKET_CONTEXT"]
    spx_first_structure = all_rows.loc["block_spx_FIRST_QUALIFIED_STRUCTURE"]
    return pd.DataFrame(
        [
            {
                "priority": "P0",
                "change": "Canlı kampanyaya strateji değişikliği yapma",
                "evidence": "Selector calibration, drawdown ve holdout kapıları henüz geçilmedi",
                "status": "KEEP_LIVE_FROZEN",
            },
            {
                "priority": "P1",
                "change": "XM aynı-feed M1 makine replay kapsamı",
                "evidence": "Minimum 30 geçerli XM seansı ve 20 puanlanabilir motor kararı gerekli",
                "status": "COLLECT_MACHINE_EVIDENCE",
            },
            {
                "priority": "P1",
                "change": "PREMARKET_CONTEXT yetkisini ayrı ablation olarak sınırla",
                "evidence": (
                    f"NQ PREMARKET_CONTEXT blok karşı-olgusu: işlem {int(baseline['trades'])}->{int(nq_premarket['trades'])}, "
                    f"WR %{baseline['win_rate_pct']}->%{nq_premarket['win_rate_pct']}, "
                    f"net {baseline['net_r']}R->{nq_premarket['net_r']}R, "
                    f"DD {baseline['max_drawdown_r']}R->{nq_premarket['max_drawdown_r']}R"
                ),
                "status": "RESEARCH_ONLY",
            },
            {
                "priority": "P2",
                "change": "SPX FIRST_QUALIFIED_STRUCTURE false-positive gate",
                "evidence": (
                    f"Karşı-olgu net {baseline['net_r']}R->{spx_first_structure['net_r']}R ve yalnız "
                    f"{int(baseline['trades'] - spx_first_structure['trades'])} fill çıkarıyor; örnek ve holdout etkisi küçük"
                ),
                "status": "WATCH",
            },
            {
                "priority": "P1",
                "change": "M1 volume-profile ve emir sırası replay",
                "evidence": "3m/5m candle-volume dağıtımı fiyat aralığına uniform yayılıyor; intrabar sıra bilinmiyor",
                "status": "IMPLEMENTED_RESEARCH_LAYER",
            },
            {
                "priority": "P2",
                "change": "Etkisiz config alanlarını kaldır veya gerçek authority yoluna bağla",
                "evidence": ", ".join(dead_fields) if dead_fields else "Ablation tamamlanınca otomatik belirlenecek",
                "status": "CODE_CONTRACT_CLEANUP",
            },
            {
                "priority": "P2",
                "change": "Broker session takvimini terminal kanıtından sürümlü üret",
                "evidence": "Planlı kapanış listesi statik; tatil/erken kapanış otomatik güncellenmiyor",
                "status": "INFRA_RESEARCH",
            },
        ]
    )


def exact_context_counterfactuals(decisions: pd.DataFrame) -> pd.DataFrame:
    from machine_policy_v1 import apply_exact_pair_cap

    candidates: list[tuple[str, str | None, str | None]] = [("baseline", None, None)]
    filled = decisions[decisions["order_state"] == "FILLED"].copy()
    for (leg_key, source), _ in filled.groupby(["leg_key", "context_source"], dropna=False):
        candidates.append((f"block_{leg_key}_{source}", str(leg_key), str(source)))
    rows = []
    for name, blocked_leg, blocked_source in candidates:
        work = filled.copy()
        if blocked_leg is not None:
            work = work[
                ~((work["leg_key"] == blocked_leg) & (work["context_source"].astype(str) == blocked_source))
            ].copy()
        work["group"] = "NQ_SPX_PAIR"
        work["label"] = work["leg_key"]
        allowed = apply_exact_pair_cap(work, -1.0)
        dates = pd.to_datetime(allowed["date"])
        for segment, mask in (
            ("all", pd.Series(True, index=allowed.index)),
            ("development_pre_2025", dates < pd.Timestamp("2025-01-01")),
            ("holdout_2025_plus", dates >= pd.Timestamp("2025-01-01")),
        ):
            sample = allowed.loc[mask]
            wins = int(sample["outcome"].eq("TP").sum())
            r = pd.to_numeric(sample["r_multiple"], errors="coerce").fillna(0.0)
            equity = r.cumsum()
            drawdown = equity - equity.cummax().clip(lower=0.0)
            rows.append(
                {
                    "variant": name,
                    "blocked_leg": blocked_leg or "",
                    "blocked_context_source": blocked_source or "",
                    "segment": segment,
                    "trades": len(sample),
                    "wins": wins,
                    "win_rate_pct": round(100 * wins / len(sample), 2) if len(sample) else 0.0,
                    "net_r": round(float(r.sum()), 3),
                    "max_drawdown_r": round(float(drawdown.min()), 3) if not drawdown.empty else 0.0,
                }
            )
    return pd.DataFrame(rows)


def main() -> None:
    required = [FULL / "summary.csv", FULL / "filled_after_causal_pair_cap.csv", ABLATION / "summary.csv"]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError(f"Research runs are incomplete: {missing}")
    summary = pd.read_csv(FULL / "summary.csv").iloc[0]
    filled = pd.read_csv(FULL / "filled_after_causal_pair_cap.csv")
    decisions = pd.read_csv(FULL / "canonical_decisions.csv")
    ablations = pd.read_csv(ABLATION / "summary.csv")
    leakage = pd.read_csv(LEAKAGE / "threshold_comparison.csv")
    anchored = pd.read_csv(ANCHORED / "summary.csv")
    verification = json.loads((FULL / "verification_summary.json").read_text(encoding="utf-8"))
    context = context_stability(filled)
    counterfactuals = exact_context_counterfactuals(decisions)
    recommendations = recommendation_table(context, ablations, counterfactuals)
    baseline_holdout = anchored[anchored["variant"] == "active_frozen_baseline"].iloc[0]
    spx_anchored = anchored[anchored["variant"] == "spx_pre2025_q60"].iloc[0]
    recommendations = pd.concat(
        [
            recommendations,
            pd.DataFrame(
                [
                    {
                        "priority": "P1",
                        "change": "SPX first30 threshold anchored pre-2025 q60",
                        "evidence": (
                            f"2025+ holdout: fill {int(baseline_holdout['fills'])}->{int(spx_anchored['fills'])}, "
                            f"WR %{baseline_holdout['win_rate_pct']}->%{spx_anchored['win_rate_pct']}, "
                            f"net {baseline_holdout['net_r']}R->{spx_anchored['net_r']}R, "
                            f"DD {baseline_holdout['max_drawdown_r']}R->{spx_anchored['max_drawdown_r']}R"
                        ),
                        "status": "RESEARCH_CANDIDATE_NOT_PROMOTED",
                    }
                ]
            ),
        ],
        ignore_index=True,
    )
    OUTPUT.mkdir(parents=True, exist_ok=True)
    context.to_csv(OUTPUT / "context_stability.csv", index=False)
    recommendations.to_csv(OUTPUT / "recommendations.csv", index=False)
    ablations.to_csv(OUTPUT / "ablation_summary.csv", index=False)
    counterfactuals.to_csv(OUTPUT / "causal_context_counterfactuals.csv", index=False)
    lines = [
        "# Forward sistem A–Z geliştirme raporu",
        "",
        "## Karar",
        "",
        "**Canlı XM demo kampanyasına strateji değişikliği eklenmeyecek.** Yerel altyapı düzeltmeleri ve araştırma katmanları hazır; strateji promotion kapıları geçilmedi.",
        "",
        "## Frozen canonical baseline",
        "",
        f"- Karar: `{int(summary['canonical_decisions'])}`",
        f"- Pair-cap sonrası fill: `{int(summary['filled_post_pair_cap'])}`",
        f"- WR: `%{summary['win_rate_pct']}`",
        f"- Net: `{summary['net_r']}R`",
        f"- PF: `{summary['profit_factor']}`",
        f"- Maksimum düşüş: `{summary['max_drawdown_r']}R`",
        f"- Aylık ortalama işlem: `{summary['average_trades_per_month']}`",
        "",
        "Bu sonuç eski `strategy.py` motorunun tarihsel +88,5R sonucu değildir; canlıda kullanılan `manual_state_canonical_v1` yolunun birebir sonucudur.",
        "",
        "## Determinism ve causality doğrulaması",
        "",
        f"- Tüm yıllar deterministik: `{verification['deterministic_all_years']}`",
        f"- Örneklenmiş prefix kontrolü: `{verification['prefix_checks']}`",
        f"- Prefix/lookahead ihlali: `{verification['prefix_violations']}`",
        f"- Teknik verification gate: `{verification['passed']}`",
        "",
        "## Selector calibration sızıntısı",
        "",
        *[
            f"- {row.leg_key.upper()}: frozen `{row.frozen_threshold}`, pre-2025 q60 `{row.pre_2025_q60}`; tarihsel OOS=`{row.historical_oos_status}`, forward causality=`{row.forward_causality_status}`"
            for row in leakage.itertuples(index=False)
        ],
        "- Bu nedenle +38,5R bağımsız OOS beklenti olarak kullanılamaz. Threshold canlıda değiştirilmeyecek; offline anchored walk-forward yeniden ölçülmeli.",
        "",
        "## Anchored 2025+ threshold holdout",
        "",
        *[
            f"- {row.variant}: fill `{int(row.fills)}`, WR `%{row.win_rate_pct}`, net `{row.net_r}R`, DD `{row.max_drawdown_r}R`"
            for row in anchored.itertuples(index=False)
        ],
        "- SPX anchored q60 araştırma adayıdır; NQ anchored q60 reddedildi. Hiçbiri tam deterministik holdout promotion kapısından geçmedi.",
        "",
        "## Uygulanan yerel güvenlik düzeltmeleri",
        "",
        "- Açık M1 mum cache'e alınmıyor.",
        "- Trade-window final prefix yeni emir sayılmıyor; stale pending emir iptal ediliyor.",
        "- Outside-window ve yürütülemeyen prefix kendi magic pending emirlerini temizliyor.",
        "- Broker tick-size, stop-distance, trade-mode, limit-order permission ve stale tick kapıları eklendi.",
        "- Restart sonrası INTENT ile mevcut broker objesi eşleştiriliyor; duplicate send yasak.",
        "- Günlük submitted/cancelled sayıları artık New York işlem gününe göre sayılıyor.",
        "",
        "## Araştırma geliştirmeleri",
        "",
        "- XM aynı-feed M1 üzerinde tamamen makine tabanlı execution/profile replay katmanı.",
        "- Causal M1 volume-profile karşılaştırması.",
        "- M1 execution replay; entry/stop/target aynı dakika ise `AMBIGUOUS/WATCH`.",
        "- Liquidity, CISD, PD-array, thesis ve target-before-fill tek-faktör ablation koşusu.",
        "",
        "## Promotion kapısı",
        "",
        "Bir varyant için toplam en az 100 fill, pre-2025 development ve 2025+ holdout tarafında ayrı ayrı en az 50 fill ve pozitif net-R gerekir. Bunlara ek olarak en az 30 geçerli XM seansı, 20 puanlanabilir motor kararı, deterministik tekrar, sıfır prefix/lookahead ihlali ve M1 ambiguity raporu zorunludur. İnsan kararına benzerlik ölçülmez.",
        "",
        "Öneri sırası `recommendations.csv`, state/yıl stabilitesi `context_stability.csv`, tek-faktör state sonucu `ablation_summary.csv`, context-source karşı-olguları `causal_context_counterfactuals.csv` dosyalarındadır. Karşı-olgular pair-cap'i yeniden hesaplar fakat bağımsız holdout kapısı olmadan promotion önermez.",
    ]
    (OUTPUT / "report_tr.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote: {OUTPUT}")


if __name__ == "__main__":
    main()
