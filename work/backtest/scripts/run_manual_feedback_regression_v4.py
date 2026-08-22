from __future__ import annotations

import pandas as pd

import run_manual_regression_harness as harness


def main() -> None:
    harness.CASES_FILE = harness.ROOT / "calibration_examples" / "manual_feedback_regression_v4_first10.csv"
    harness.REPORT_DIR = harness.ROOT / "outputs" / "reports" / "manual_feedback_regression_v4_first10"
    harness.EXTRA_PROFILES = {
        "challenge_core_v2_htf_requalification",
        "challenge_core_v2_nq_5m",
        "challenge_core_v2_nq_5m_lunch_window",
        "challenge_core_v2_nq_lunch_window",
    }
    harness.main()
    write_research_conclusions()


def write_research_conclusions() -> None:
    summary = pd.read_csv(harness.REPORT_DIR / "summary.csv")
    comparison = pd.read_csv(harness.REPORT_DIR / "comparison.csv", keep_default_na=False)
    all_rows = summary[summary["bucket"] == "all"].copy()
    metrics = all_rows[
        [
            "profile",
            "setup_accuracy",
            "order_accuracy",
            "final_accuracy",
            "direction_accuracy",
            "decision_exact_rate",
            "lifecycle_exact_rate",
            "outcome_accuracy",
        ]
    ].sort_values("decision_exact_rate", ascending=False)
    baseline = comparison[comparison["profile"] == "challenge_core_v2"][
        [
            "case_id",
            "status",
            "actual_setup",
            "actual_order_state",
            "actual_final",
            "actual_direction",
            "actual_terminal_reason",
        ]
    ]

    lines = [
        "# İlk 10 NQ Manuel-Uyum Araştırma Sonucu",
        "",
        "Kapsam yalnız İşlem 01–10'dur. İşlem 11–20 değerlendirilmemiştir. Amaç kârlılık değil; SETUP, ORDER, FINAL ve DIRECTION yaşam döngüsünde manuel karara benzemektir.",
        "",
        "## Veri yorumlama düzeltmeleri",
        "",
        "- Boş `Güven derecesi` ve `Fikrimi ne değiştirirdi?` alanları koşullu olarak uygulanamaz/belirsizdir; eksik veri sayılmaz.",
        "- Normal kronoloji yapılandırılmış saatlerden türetilir. Olay tablosu yalnız istisnai episode değişimlerinde gerekir.",
        "- `ORDER=NOT_PLACED` iken İşlem 09'daki `İptal nedeni=Diğer` yok sayılır; şablonda N/A seçeneği yoktur.",
        "- `5m/3m`, 5m bağlam ve 3m execution anlamına gelir.",
        "- TradingView bağlantıları geçerli kabul edilmiştir; bu ortamın erişim sonucu kanıt kalitesini düşürmez.",
        "",
        "## Ölçüm",
        "",
        "Birincil skor `SETUP + ORDER + FINAL + DIRECTION` tam eşleşmesidir. OUTCOME ayrı raporlanır; TP/SL farkı setup doğruluğunu bozmaz. Motor açıklayamadığı bir NOT_PLACED nedenini `UNOBSERVED` yazar, neden uydurmaz.",
        "",
        harness.base_report.markdown_table(metrics),
        "",
        "## Aktif CHALLENGE_CORE_V2 vaka sonucu",
        "",
        harness.base_report.markdown_table(baseline),
        "",
        "## Kanıtın söylediği",
        "",
        "- Aktif 3m profil 10 vakanın 1'inde karar yaşam döngüsünü tam eşleştirdi.",
        "- Genel 15m body-close requalification kolu hiçbir birincil metriği değiştirmedi; aktif sisteme alınmamalı.",
        "- Saf 5m kolu 2/10'a çıktı; tek başına yeterli ve istikrarlı bir açıklama değil.",
        "- 3m gözlem penceresini 10:30'dan 12:00'ye uzatmak setup/final bileşenlerini belirgin artırdı, fakat yanlış yönlü erken episode'lar nedeniyle tam karar eşleşmesi 2/10'da kaldı.",
        "- 5m + 12:00 birleşimi en yüksek tam karar eşleşmesini verdi (3/10). İşlem 05'in geç bearish episode'unu yakaladı, fakat genel uyum hâlâ aktifleştirme için zayıf.",
        "",
        "## Yalnız güçlü ve tekrarlanan kanıta dayalı sonraki testler",
        "",
        "1. `MANUAL_STATE_V1_LUNCH`: setup arama penceresi 12:00'ye kadar açık; emir dolmazsa 12:00'de açık terminal nedeni ile iptal. İşlem 05, 07 ve 10 bunu tekrar ediyor.",
        "2. `MANUAL_STATE_V1_EPISODES`: opposite CISD eski thesis/pending episode'u kapatır; yeni yönde işlem ancak yeni CISD ve yeni FVG ile açılır. İşlem 03, 05 ve 10 bunu tekrar ediyor.",
        "3. `MANUAL_STATE_V1_LIQUIDITY_GATE`: liquidity `required` ise seçilen seviyenin sweep'i beklenir; `optional/replaced_by_va_flip` ise VA bağlamından sonra yapı ve FVG aranır. İşlem 03, 04, 08 ve 10 katkı sağlıyor. Bu testte manuel etiketi motora doğrudan verilmemeli; gereklilik nesnel bağlam durumundan türetilmeli.",
        "4. `MANUAL_STATE_V1_PENDING`: terminal durumlar ayrı tutulur: `TARGET_BEFORE_FILL`, `OPPOSITE_CISD`, `TRADE_WINDOW_END`, `FILLED`. İşlem 03 ve 07 doğrudan, İşlem 05/10 episode geçişi üzerinden destekliyor.",
        "",
        "Bu dört davranış önce tek tek ablation olarak, sonra birleşik state-machine kolu olarak test edilmelidir. Birincil kabul ölçütü kâr değil karar-tam-eşleşmesi ve yanlış pozitif setup sayısıdır.",
        "",
        "## Yalnız WATCH",
        "",
        "- Genel controlling-array threshold/weight değişikliği: bu 10 vakadan destek çıkmıyor ve özellikle optimize edilmemeli.",
        "- Genel 15m body-close sweep/requalification: test sonucu nötr; önceki lifecycle bozulmasıyla birlikte reddedilmeli.",
        "- 5m bağlam + 3m execution: İşlem 10 tek örnek; tekrar olmadan genellenmemeli.",
        "- VAH flip'in tek başına üretim kuralı olması: İşlem 04 destekliyor fakat tek başına yeterli değil.",
        "- Stop/target varyantları: bu çalışmanın amacı dışındadır; karar motoru eşleşmeden test edilmemeli.",
        "",
        "## Cross-sweep / thesis / pending-order katkısı",
        "",
        "- Cross-sweep bir trade tetikleyicisi değil, thesis episode sınırı olarak modellenmeli.",
        "- Opposite CISD eski episode'u invalid eder; sonraki aynı/karşı yön setup eski CISD/FVG'yi miras alamaz.",
        "- Geçerli setup ile dolmuş emir ayrılmalıdır; `VALID + CANCELLED + SKIP` bağımsız ve ölçülebilir bir sonuçtur.",
        "- NOT_PLACED nedenleri motorca gözlenmiyorsa `UNOBSERVED` kalmalıdır; şablon seçeneğinden veya sonuçtan geriye doğru neden üretilmemelidir.",
        "",
        "Aktif CHALLENGE_CORE_V2, FON_CORE, günlük NQ+SPX -1R pair cap ve controlling-array selector varsayılanları değiştirilmemiştir.",
        "",
    ]
    (harness.REPORT_DIR / "research_conclusions.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
