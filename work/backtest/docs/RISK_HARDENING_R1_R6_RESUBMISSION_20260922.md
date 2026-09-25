# Risk-hardening R1–R6 yeniden teslim kanıtı

Tarih: 22 Eylül 2026  
Dal: `codex/risk-hardening-1-11`  
Kod doğrulama HEAD’i: `50461fdd4dec225dfbbcef7cb456bda9f61c9541`; R1 taraması HEAD’i: `b6a32126a3089764c053c117ec50f7765ae7241c`.
Karar: **DÜZELTME GEREKLİ — `DEPLOYMENT_READY=false`**

Push veya merge yapılmadı. Bağımsız kabul hâlâ gereklidir.

## Bulguların durumu

- **R1:** Final HEAD üzerinde yapısal broker-identity taraması PASS: `candidate_tree_match_count=0`, `candidate_tree_structural_violation_count=0`, `origin_unchanged=true`, `source_unchanged=true`.
- **R2 / F1:** Aktif Super1 V4 zinciri checkout byte’larıyla yeniden mühürlendi. `tests/test_pinned_checkout_bytes.py`: `4 passed`; aktif source hash `87146db750866fcdccae6bc0b0d57fb5057187e0eaa20c5e4825cdcedc1fed20`, contract hash `4ac2cf7ed20c7b053688528d396f4566ef84cb4dcee373b3084130100e42ab74`.
- **R3 / F2:** Finalizer gerçek development/holdout verisi, gerçek risk istatistikleri ve result hash’leriyle bağlandı. Holdout-only mutation ve development/full mutation negatifleri korunuyor; birleşik Super1/forward-shadow/candidate suite: `63 passed`.
- **R4:** 144/144 pinned raw input doğrulandı; provenance hazırlığı `core_tests_passed=True`, `deterministic_rerun=True`, `prefix_violation_count=0`, `forward_shadow_ready=True`. Threshold artifact SHA-256 `62db244cc4126942121a96fa0a0304c42adea258415355c84350be1dbc57ac9b`; baseline manifest SHA-256 `7756a5ab9639afdd59b09640d35ff37c29e3851a693e45eef9001118dea05fd4`.
- **R5:** Windows sandbox runtime artık bağımsız kopyalanmış CPython runtime ve daraltılmış paket setiyle çalışıyor; AppContainer ACL kökleri sistem Python/venv dizinlerini kapsamıyor. Pozitif worker/import ve OS sınır testleri geçti.
- **R6 / F4:** Workflow generic hosted runner yerine owner-managed `[self-hosted, Windows, risk-hardened]` runner ister; pinned provenance kaynağı ve owner-created symlink fixture kökü zorunludur.

## Kanıt sonuçları

DEPLOY-001 kanıt paketi güncel checkout’a bağlandı: tested commit `4ad03c49e833fc9f7eb974573a06a1da330b7095`, tested tree `1ca59f749fa589d68dcb7cacd337f4269b9d9b83`. Kanıt bağlama testi `2 passed`.

Targeted DEPLOY-001 suite: `225 passed`, `1 warning`. Collection: `773 tests collected`.

CPython 3.11 tam suite: `772 passed, 1 warning, 1 error` (`616.05s`). Tek hata, owner’ın normal kullanıcı bağlamında oluşturacağı symlink fixture kökü komuta verilmediği için `LINK_FIXTURE_ROOT_REQUIRED`; bu kapı gevşetilmedi. Gerçek AppContainer symlink-escape kabulü, owner-prepared fixture ile Windows CI’da yeniden çalıştırılmalıdır.

Kanıt dosyaları:

- [Tam-suite JUnit](C:/Users/ISAAC/Documents/otobacktestprojesi-risk-integration/work/backtest/outputs/risk-hardening-py311-20260922-final.junit.xml)
- [Tam-suite log](C:/Users/ISAAC/Documents/otobacktestprojesi-risk-integration/work/backtest/outputs/risk-hardening-py311-20260922-final.log)
- [DEPLOY-001 manifest](C:/Users/ISAAC/Documents/otobacktestprojesi-risk-integration/work/backtest/docs/DEPLOY_001_EVIDENCE/manifest.json)
- [R1 final tarama](C:/Users/ISAAC/Documents/otobacktestprojesi-risk-integration/work/backtest/outputs/risk-hardening-r1-identity-rescan-final.json)

Tam-suite JUnit SHA-256: `3ffb1dabed0ede3b0aa5b102fd11841feb3e2b96c43c4571fd4b9ba0810dea95`  
Tam-suite log SHA-256: `e27c5e2362991e5e2906a8628a5315678cee92e6a639d029e82c95e416a817eb`

## Sonraki kabul koşulu

Owner-prepared symlink fixture oluşturulup self-hosted Windows runner’da workflow şu fixture köküyle yeniden çalıştırılmadan `DEPLOYMENT_READY=true` yapılmayacak ve canlı signed deployment/rollback etkinleştirilmeyecek.
