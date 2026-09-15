# SUPER1 — 1–22 Durum ve Kabul Kanıtı

Tarih: 2026-09-15
Promotion code HEAD tested: `a24a0ab950ee2b9b9e5bfe7d1cad2701bff99050`
Risk branch HEAD tested: `cd8c02b8d3914eaea6ff71dac6cbf796f83206ed`

## Nihai kapı

`NOT PROMOTABLE / NO PUSH / NO LIVE ORDER`

Kod tarafındaki 1–11 risk düzeltmeleri ayrı risk branch’inde yapıldı ve promotion branch’ine yalnızca risk commit’i taşındı. 12–18 için fail-closed acquisition, persistent state lock, body-stream timeout, target-key, raw/decoded bundle attestation ve gerçek dosya kanıtı kontrolleri eklendi. Dış yetki veya üretim verisi yokken PASS üretilmedi.

## Test ve branch kanıtı

| Kapsam | HEAD | Sonuç | JUnit / kanıt SHA-256 |
|---|---|---:|---|
| Promotion hedefli suite | `a24a0ab` | 298 passed, 1 skipped | `outputs/reports/pytest_promotion_full_targeted_final.xml` — `00254f1f569c3017cf1f3188fbf658a455868f399822d840bf835f24dac847e2` |
| Risk hedefli suite | `cd8c02b` | 32 passed, 7 skipped | `outputs/reports/pytest_risk_targeted_20260915_postcommit.xml` — `0a2755a257a00ccdb7f25c61e162c42299afd062632318321ef61e4d782dd3c1` |
| Promotion tam pytest | `a24a0ab` | 744 passed, 10 skipped, 0 failed | `outputs/reports/pytest_promotion_full_20260915_final.xml` — `5de6e326ecc9d7ffce4fe1292c46743073bf92020a98a14a4bd4012c2df0bd5d` (754 test) |
| Risk tam pytest | `cd8c02b` | 706 passed, 7 skipped, 0 failed | `outputs/reports/pytest_risk_full_20260915_final.xml` — `c997d91981798c8773861b60c1c5fae9c809ebcfff8380e06b250639ee5867ca` (713 test) |
| Python compile / Node AST / PowerShell AST / `git diff --check` | iki branch | passed | current test and static-check evidence |

Tam testlerdeki skip’ler Windows AppContainer/restricted-token launcher yokluğu nedeniyle sandbox’ın bilinçli `BLOCKED` davranışını doğrulayan platform sınırlarıdır; başarısız test yoktur. Bu durum 11 ve 12–22 için dış kabul kanıtı eksikliğini kaldırmaz.

## 1–22 ayrı durum

| # | Durum | Kabul kanıtı | Eksik dış girdi / blocker |
|---:|---|---|---|
| 1 | PASS (hedefli) | `test_super1_instruction_1_11.py`, approval/production-flow suite; promotion/risk JUnit | 11 ve 12–22 dış kabul kanıtları nedeniyle yayın kapısı kapalı |
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
- Current V4 runtime chain doğrulandı: candidate artifact `e0aa5d771befad1b8017d61b78a0f4732b7e7c4f9f40b7995da0de0509bd1b8c`, engine `0de20902eade693fb0a4688dd5fda505fe97e2497994293fde5f3fd20bdef194`, deal schema `109369101e71b9fe29be181257976efe9bdd3ec590ab001ac4463a55674b202d`, config `f861ac48c09dff3c3faf688f586c66cb9404d3c47bcdf85565f0d056390f7934`, signal contract `ae5e3c3d6ea67685384c2b36e779502969927f2b57721a848707b49278d3ee94`.
