# Risk-hardening R1–R6 yeniden teslim kanıtı

Tarih: 21 Eylül 2026  
Dal: `codex/risk-hardening-1-11`  
Karar: **DÜZELTME GEREKLİ — `DEPLOYMENT_READY=false`**

Push veya merge yapılmadı. Bağımsız kabul hâlâ gereklidir.

## Bulguların durumu

- **R1:** Test ve fixture broker kimlikleri sentetik değerlere taşındı. Aday tree taraması: `identity_matches=0`, `structural_violations=0`.
- **R2:** Aktif Super1 V4 contract/runtime/manifest zinciri güncel `source_code_hash` ile yeniden mühürlendi. Pozitif validator ve stale/mutasyon negatifleri korunuyor; `tests/test_super1_xm_forward.py`: `44 passed`.
- **R3:** İki snapshot deterministik sınırlarla ayrıldı. CPython 3.11 tam e2e: `18 passed`; interprocess yarış testi: `1 passed`.
- **R4:** Clean-checkout hazırlığı ve 144 girdilik SHA-256 provenance manifesti eklendi. Hazırlık audit sonucu: `core_tests_passed=True`, `deterministic_rerun=True`, `prefix_violation_count=0`, `forward_shadow_ready=True`.
- **R5:** Finalizer yayın sözleşmesi, holdout izolasyonu ve mutation testleri geri getirildi. `tests/test_candidate_artifacts.py`: `9 passed`.
- **R6:** Workflow köke taşındı: `.github/workflows/super1-risk-gates.yml`. Windows + CPython 3.11, bağımlılık kurulumu, provenance kaynağı ve symlink fixture kökü zorunlu; CI başarı durumu veya deployment readiness üretilmiyor.

## Provenance ve artifact kanıtı

- Pinned raw inputs: `144/144` doğrulandı.
- Threshold artifact SHA-256: `847228220a84eb32319581d2b0a440ac6d81a5d6422c89a19acfccd2bf2cd044`.
- Baseline manifest SHA-256: `b1f7125370e7a495698470d9c3676e4deeb9adfbebb446319bc75d1e6683c23c`.
- Engine code hash: `43d73dbf64d9fa85855f97d98733cc08d989d31acc10373ab79ee7cded0d9077`.
- Baseline config hash: `18e1ebbe33ef494b81e97c4e3759e22d71797f9c312aa973b322688256a89cf4`.

## CPython 3.11 tam suite

Komut:

```powershell
.\.venv311\Scripts\python.exe -m pytest -q --junitxml=outputs/risk-hardening-r1-r6-py311-final.junit.xml *> outputs/risk-hardening-r1-r6-py311-final.log
```

Sonuç: `759 passed, 11 failed, 1 error, 1 warning` — `302.42s`. Skip/xfail eklenmedi.

- JUnit SHA-256: `277d1186de1f33baa25a41a9731b6864dcd8e54a123abc0c116ca22a74736ac4`
- Log SHA-256: `37ec958d3f00f99ae2db5b64c355edac41e72a3fe4b7270535e4b744db853878`

Kalan 11 failure, AppContainer worker’ın bu venv koşulunda `No pyvenv.cfg file` ile başlatılamamasından kaynaklandı; assertion kapıları gevşetilmedi. Symlink escape testi ayrıca owner-prepared fixture kökü verilmediği için `LINK_FIXTURE_ROOT_REQUIRED` error verdi. Junction testi de aynı AppContainer başlatma blocker’ına ulaştı. Bu nedenle R6 bağımsız kabul için açık blocker’dır.

## Değişen test/fixture dosyaları

`tests/test_xm_mt5_forward.py`, `tests/test_super1_xm_forward.py`, `tests/test_super1_risk_e2e.py`, `tests/test_candidate_artifacts.py`, `tests/test_environment_schema_scanner.py`, `tests/test_production_order_flow.py`, `tests/test_super1_continuation.py`, `tests/fixtures/super1_xm_mt5_demo_config.json`, `tests/fixtures/super1_xm_mt5_demo_config_v4.json`.

## Commit paketi

`e952573` R1 kimlikleri; `b7241ec` ve `c73d67a` aktif V4 zinciri; `00ddf82` R3 yarış sınırı; `7d67a3f` kalan fixture kimlikleri; `1677bb0` provenance; `a85af9d` baseline lock; `35cd998` R5 yayın/holdout kapıları; `6319ee3` ve `aff53ec` R6 workflow/evidence bağları; `032fed3` Python 3.11 sandbox bağımlılığı; `fd441f0` launcher schema-safe env taraması.

Bağımsız kabulden önce owner-prepared symlink fixture ile root workflow’un gerçek Windows CI/temiz checkout üzerinde yeniden çalıştırılması gerekir.
