# SUPER1 — 1–22 Durum ve Kabul Kanıtı

Tarih: 2026-09-15
Promotion code HEAD tested: `40bf3aa7e627fd5ab6c697242ce619618f4e482b`
Risk branch HEAD tested: `faf37e1323bd0e9f22f3031b384f45c342d7d60c`

## Nihai kapı

`NOT PROMOTABLE / NO PUSH / NO LIVE ORDER`

Kod tarafındaki 1–11 risk düzeltmeleri ayrı risk branch’inde yapıldı ve promotion branch’ine yalnızca risk commit’i taşındı. 12–18 için fail-closed acquisition, persistent state lock, body-stream timeout, target-key, raw/decoded bundle attestation ve gerçek dosya kanıtı kontrolleri eklendi. Dış yetki veya üretim verisi yokken PASS üretilmedi.

## Test ve branch kanıtı

| Kapsam | HEAD | Sonuç | JUnit / kanıt SHA-256 |
|---|---|---:|---|
| Promotion hedefli negatif suite | `40bf3aa` | 51 passed | `outputs/reports/pytest_super1_targeted_20260915.xml` — `ec60ae64c4e3cc5150f6a6fa992e230820203ba9074c26f4acf6775b398d2cff` |
| Risk hedefli suite | `faf37e1` | 74 passed | `outputs/reports/pytest_risk_targeted_20260915.xml` — `da2b24ad0962b7102924106e4e7847874f025988c9c19b917bc27ab7f753f52e` |
| Promotion tam pytest | `40bf3aa` | 742 passed, 4 failed, 0 errors | `outputs/reports/pytest_super1_full_20260915.xml` — `d2a4261ffe6569812694d76be8e0bef4ac77bde0cb197acf4f717c008265619e` (746 test) |
| Risk tam pytest | `faf37e1` | 3 collection errors | `outputs/reports/pytest_risk_full_20260915.xml` — `7746d785c791644c90b02d27fc644784822c6be287dd21f9cd7c06b6715efc03` |
| Python compile / Node AST / PowerShell AST / `git diff --check` | iki branch | passed | çalışma ağacı gate çıktısı |

Promotion tam suite hataları eski V4 sealed candidate/source hash zincirinin yeni kaynak HEAD ile uyuşmamasıdır; bu gerçek release blocker’dır. Risk tam suite ayrıca worktree’de bulunmayan `live_forward/xm_mt5_demo_config.json` ve V08 manifest SHA uyuşmazlığı nedeniyle collection’da durdu.

## 1–22 ayrı durum

| # | Durum | Kabul kanıtı | Eksik dış girdi / blocker |
|---:|---|---|---|
| 1 | PASS (hedefli) | `test_super1_instruction_1_11.py`, approval/production-flow suite; risk JUnit | Sealed V4 runtime hash zinciri release testinde eskimiş; promotion yine yayınlanamaz |
| 2 | PASS (hedefli) | retry ve write-once negatif testleri; risk JUnit | Gerçek broker readback yetkisi yok |
| 3 | PASS (hedefli) | instrument metadata/tick/economic semantic negatif testleri; risk JUnit | Gerçek XM symbol snapshot yok |
| 4 | PASS (hedefli) | evaluation window, warmup, gap/closure testleri; risk JUnit | Tam üretim veri kapsamı residual nedeniyle yok |
| 5 | PASS (hedefli) | atomic slot, approval replay/request-hash testleri; risk JUnit | İmzalı lease ve broker E2E yok |
| 6 | PASS (hedefli) | closed search, overlap, deterministic/no-valid-trial testleri; risk JUnit | Yeni gerçek forward/OOS çalışması yok |
| 7 | PASS (hedefli) | closed-terminal, null/reason, provenance risk-xray testleri; risk JUnit | Tam valid coverage yok |
| 8 | PASS (hedefli) | lifecycle/cursor/late-deal negatif testleri; risk JUnit | Gerçek broker session/deal kanıtı yok |
| 9 | PASS (hedefli) | unknown env ve secret-redaction testleri; risk JUnit | Gerçek deployment environment teyidi yok |
| 10 | PASS (hedefli) | denominator/nonfinite/numeric contract testleri; risk JUnit | Broker canlı quantization snapshot yok |
| 11 | BLOCKED | sandbox negatif suite çalıştı | Windows AppContainer/restricted-token ve POSIX network namespace için OS düzeyi kabul kanıtı yok |
| 12 | BLOCKED | `test_super1_instruction_12_18.py`; coordinator loop/checkpoint kodu | Tek URL başarılı fixture’ı provider-start/terminal-event/CAS/coordinator termination ile E2E kanıtlanmadı |
| 13 | BLOCKED | persistent lock ve `record_success` negatif testleri | Restart + iki gerçek süreç concurrency kabulü eksik |
| 14 | BLOCKED | Node `reader`/timer/cancel negatif kanıtı | Yavaş header/body ve >16 MiB fixture E2E kanıtı eksik |
| 15 | BLOCKED | nested `target_key` apply ve unknown dataset negatif testleri | Residual-zero 113 hedef gerçek apply smoke’u yok |
| 16 | BLOCKED | strict raw/decoded/bundle recomputation ve detached-signature fail-closed kodu | İmzalı final manifest, unique nonce/source HEAD/event root ve repo dışı pinned key yok |
| 17 | BLOCKED | private evidence-root/path/hash doğrulaması ve eksik denylist testleri | Repo dışı security/history/rotation kanıt kökü ve yedi gerçek artifact yok |
| 18 | BLOCKED | cache/target/CAS/terminal contract kodu ve negatif suite | Provider erişimi, fixture isolation, crash/resume ve account-switch E2E kanıtı yok |
| 19 | BLOCKED | `outputs/reports/dukascopy_reacquisition_v5/bundle_audit.json` SHA `b14daa3d4dbdb35109bb07e47e9ccc5887c02f7779503969efb72059f9f032cb` | 14 VERIFIED_LEGACY, 3 INVALID_LEGACY, 1 INCOMPLETE_STAGING, 95 MISSING; residual 99. Circuit OPEN, provider call 0, next retry `2026-09-15T15:33:39.3919524Z`; final signed manifest yok |
| 20 | BLOCKED | `test_super1_instruction_19_22.py`; readiness report SHA `7787ab0d29ded73ca20a04094ce741b79c8343be9bbd43930e7a55675818711a` | Hesap sahibinin repo dışı denylist’i yok; public/history durumları `UNASSESSED_MISSING_DENYLIST` |
| 21 | BLOCKED | readiness report ve no-push çalışma kuralı | 9 erişilebilir kimlik blob’u için owner-approved sanitized mirror, remote ref re-scan ve explicit lease yok |
| 22 | BLOCKED | readiness report; deployment binding kodu yalnızca doğrulama kapısıdır | Broker owner rotation/revocation ve yeni private signed binding kanıtı yok |

## Veri, güvenlik ve yayın sınırı

- Frozen inventory SHA: `a63406f235ded8d3daa123c0311adb678e53db3d996141b194493309f2cce075`; 113 unique hedef değişmedi.
- Acquisition state SHA: `c42eecf3bc6f4ae4a000a9f4a11f7c43140d147dfa9dbb3dce940c8a9a8dbccb`; state `DEFERRED_RATE_LIMIT`, provider call `0`.
- Dört korumalı untracked girdi değiştirilmedi ve stage edilmedi.
- Broker hesabı, parola, private key, remote history, fetch/pull/tag/push veya live order işlemi yapılmadı.
