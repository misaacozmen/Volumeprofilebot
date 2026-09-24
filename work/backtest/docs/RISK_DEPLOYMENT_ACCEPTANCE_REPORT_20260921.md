# Risk-hardening 1–11 / DEPLOY-001 / Promotion kabul raporu

Tarih: 21 Eylül 2026  
Entegrasyon branch’i: `codex/risk-hardening-1-11`  
Test edilen kod/evidence commit’i: `7a4b684ab600f3d5beba3fae0a26e8854fc5df3f`; rapor commit’i bunun üzerine eklenmiştir.  
Test edilen kod tree: `cee939ae3cdfd90757f5c9c058093d5e556dd34a`  
Başlangıç origin/main: `c1c769aec68b84c9c13f9add198ab130241c69ad`

## Sonuç kodları

- `DEPLOYMENT_READY=false`
- `MERGED_MAIN=NO` — merge/push yapılmadı; gerçek signing ve hedef Windows kabulü için gerekli girdiler yok.
- `PASS_CODE=PARTIAL` — kod kapısında 769 PASS, 1 dış fixture ERROR; 90 zincirleme XM hatası giderildi.
- `PASS_EXTERNAL_ACCEPTANCE=BLOCKED`

Kaynak/commit envanteri: `docs/RISK_HARDENING_1_11_SOURCE_INVENTORY_20260921.md`.

## Risk-hardening 1–11

Kaynak ref’i: committed risk ref `4a9d33a4530aeb5f3e0566672f60a7e4214f7c21`; dirty kaynak delta ayrıca inventory’de `81b70608...` patch SHA ile kayıtlıdır.

| Madde | Runtime bağlantısı | Hedef doğrulama | Durum |
|---|---|---|---|
| 1 | `backtest/live/risk_guard.py`, `production_flow.py`, `halt.py` | `tests/test_super1_instruction_1_11.py`, `tests/test_super1_risk_e2e.py` | `PASS_CODE=PASS` |
| 2 | `backtest/live/retry.py`, durable order intent/outbox | retry/backoff ve duplicate-send testleri | `PASS_CODE=PASS` |
| 3 | `backtest/live/instruments.py`, broker metadata gate | `test_symbol_metadata_tick_and_economic_fingerprint_are_exact` | `PASS_CODE=PASS` |
| 4 | evaluation/walk-forward sınırları | `tests/test_evaluation_window.py`, `tests/test_optimization_walk_forward.py` | `PASS_CODE=PASS` |
| 5 | `backtest/live/approval.py`, approval expiry/replay gate | `tests/test_order_approval.py` | `PASS_CODE=PASS` |
| 6 | `backtest/optimization.py`, holdout ayrımı | `tests/test_optimization_walk_forward.py`, `tests/test_risk_causality.py` | `PASS_CODE=PASS` |
| 7 | `backtest/risk_xray.py` | `tests/test_risk_xray.py` | `PASS_CODE=PASS` |
| 8 | `backtest/live/strategy_health.py` ve runtime block | `tests/test_strategy_health.py`, runtime hardening testleri | `PASS_CODE=PASS` |
| 9 | `backtest/live/settings.py`, env schema/allowlist | `tests/test_runtime_settings.py` | `PASS_CODE=PASS` |
| 10 | `backtest/numeric_contracts.py`, financial numerics | `tests/test_financial_numeric_contracts.py` | `PASS_CODE=PASS` |
| 11 | `backtest/sandbox.py`, Windows AppContainer/ACL launcher | `tests/test_backtest_sandbox.py`, symlink fixture testleri | `PASS_CODE=PARTIAL` |

Madde 11’in tek kalan hatası: `LINK_FIXTURE_ROOT_REQUIRED`. Owner tarafından oluşturulmuş elevated/AppContainer symlink fixture root ve gerçek Windows owner/admin oturumu sağlanmadı. Skip/xfail uygulanmadı.

## Test ve provenance kapıları

- Core suite: `212 passed in 20.80s`, exit `0`; runner: `scripts/run_core_tests.py`.
- Full pytest: `769 passed, 1 warning, 1 error`, exit `1`; collection `770` test.
- Full JUnit: `outputs/reports/risk_full_pytest_20260921_final3.xml`, SHA-256 `408d5fb11886807a4f1af51c54e1f2a36a665c77b758bf725bd5d956bbd2bc6a`.
- Full log: `outputs/reports/risk_full_pytest_20260921_final3.log`, SHA-256 `9ad4ce33b7009c1c50dcc1062f0bb44e58bfd9765d83f4a963bc51e96b20a071`.
- DEPLOY-001 evidence manifest: `docs/DEPLOY_001_EVIDENCE/manifest.json`; manifest güncel entegre commit öncesindeki değişmez test commit’ine bağlanır ve `tests/test_deploy_001_evidence_manifest.py` `2 passed`.
- DEPLOY-001 targeted evidence: `223 passed, 1 warning`; JUnit SHA `bc04e9c16c71cadbd9ca43a39cb10469547e587be0b1cedbcd3e84b1cb490305`; log SHA `7b2eb9bf85ec71deaffb92e094b03eaccf0d1402484c02740d15911e20806934`.
- Reliability audit: `core_tests_passed=true`, `deterministic_rerun=true`, `prefix_violation_count=0`, `prefix_checks=48`, `invalid_data_days_blocked=3`, `canonical_decisions=57`, `forward_shadow_ready=true`.
- Audit manifest SHA: `d8c7f24523ab1b9cafc409b7c6bdc2000905664c8037ead322284f9d37ac102f`.
- Baseline lock SHA: `56711052ce7bfce7c9bc0d7d1d53e8fb6c2df02a496dfeffaf6b085df21a931f`.

Threshold/calibration artifactleri trusted mevcut girdilerden yeniden üretildi; LF/CRLF sözleşmesi ve baseline manifest bağları güncellendi. Tam 2016–2024 ham tarihçe için 2022–2024 trusted DUKASCOPY girdisi mevcut olmadığından bu dönem için tam tarihsel calibration iddiası yapılmıyor. Yerel raw data ve ignored audit çıktıları commit edilmedi.

## Gerçek signed deployment ve rollback

`PASS_CODE=BLOCKED`, `PASS_EXTERNAL_ACCEPTANCE=BLOCKED`.

Gerçek private signing key/certificate, signed ZIP, detached signature ve owner trust pin verilmedi. Bu nedenle release imzalanmadı; ZIP/manifest doğrulaması yalnız kod/fixture seviyesinde kaldı.

Yönetici yetkili Windows hedefi, hedef path’leri, Super1/XM demo hesabı, terminal oturumu ve rollback için owner onayı verilmedi. Scheduled task, servis, ACL, broker veya MT5 üzerinde işlem yapılmadı. Gerçek demo broker read-only gözlemi de yürütülmedi.

Gerekli dış girdiler: signing key/certificate ve key id; elevated hedef/admin session; önce/sonra task-service-ACL snapshot’ları; signed release arşivi; demo account/terminal read-only erişimi; rollback gözlem çıktısı. Bunlar sağlandığında doğrulama `build_signed_windows_release.ps1`, `release_integrity.ps1`, signed upgrade/rollback runbook’ları ve tam JUnit ile yeniden yapılmalıdır.

## Promotion 12–19

| Kalem | Durum | Eksik kabul girdisi |
|---|---|---|
| 12 | `PASS_EXTERNAL_ACCEPTANCE=BLOCKED` | Provider rate-limit koşulu ve izinli normal erişim gözlemi |
| 13 | `PASS_EXTERNAL_ACCEPTANCE=BLOCKED` | Provider gözlem penceresi ve gerçek hata üretmeden alınmış kanıt |
| 14 | `PASS_EXTERNAL_ACCEPTANCE=BLOCKED` | Provider normal erişim/limit kabul kaydı |
| 15 | `PASS_EXTERNAL_ACCEPTANCE=BLOCKED` | Değiştirilmemiş 113 hedef envanteri ve canonical V5 manifest |
| 16 | `PASS_EXTERNAL_ACCEPTANCE=BLOCKED` | Owner public key/trust pin, detached signature, source/tree/nonce/event-root bağları |
| 17 | `PASS_EXTERNAL_ACCEPTANCE=BLOCKED` | Aynı owner imza ve event-root doğrulama paketi |
| 18 | `PASS_EXTERNAL_ACCEPTANCE=BLOCKED` | Gerçek DEMO/account-switch read-only broker gözlemi |
| 19 | `PASS_EXTERNAL_ACCEPTANCE=BLOCKED` | Her hedef için doğrulanmış bağlar ve gerçek residual-zero sonucu |

14 legacy attestation hash’i değiştirilmedi; yeni owner/provider kanıtı olmadığı için geçmiş attestation güncel kabul gibi gösterilmedi. `42/42` dosya eşleşmesi owner/provider kabulü sayılmadı.

## Kapanış

Kod entegrasyonu ve iç kapılar büyük ölçüde tamamlandı; dış kabul için yukarıdaki girdiler olmadan merge/push, signed deployment, rollback veya promotion iddiası yapılamaz. Bu teslimat `DEPLOYMENT_READY=true` değildir.
