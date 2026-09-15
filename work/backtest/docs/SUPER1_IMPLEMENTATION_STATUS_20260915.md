# SUPER1 — 1–22 Durum ve Kabul Kanıtı

Tarih: 2026-09-15
Promotion implementation commit tested: `6624bf2338057520533f311d8426b87d58435a18`
Promotion implementation tree: `5afc4cd9f467b9d40af866b1d62c0a2d498d83b2`
Risk branch commit tested: `af55caa527fac638c9894f416ea69847b2021a96`
Risk branch tree: `3b072e20d3ad589d6cf066e0cf83731cc70631dd`

## Nihai kapı

`NOT PROMOTABLE / NO PUSH / NO LIVE ORDER`

Provider çağrısı, broker hesabı/parolası, private key üretimi/kullanımı, remote işlem, history rewrite veya live order yapılmadı. Kod ve test kanıtı hazırlandı; platform ve dış owner/provider kanıtları olmadan PASS üretilmedi.

## Test ve branch kanıtı

| Kapsam | HEAD | Sonuç | JUnit / kanıt SHA-256 |
|---|---|---:|---|
| Promotion hedefli suite | `6624bf2` | 69 passed, 8 skipped | `outputs/reports/pytest_promotion_targeted_20260915_final.xml` — `f1c55d301d5f2339ba766b337291778b28109c0492b2a431104cbcd2e3b17e79` |
| Risk hedefli suite | `af55caa` | 27 passed, 0 skipped | `outputs/reports/pytest_risk_targeted_20260915_final2.xml` — `9998b4182234be2b7752cb9dd0a794a40fd683e06209ed4014fb1b69998a6b5b` |
| Promotion tam pytest | `6624bf2` | 744 passed, 10 skipped, 0 failed | `outputs/reports/pytest_promotion_full_20260915_postcommit.xml` — `a29ecf70b69bcd2e07c4f2bc27e8298ac5625dd3bda441a1ba5fef3a70d250e0` |
| Risk tam pytest | `af55caa` | 707 passed, 7 skipped, 0 failed | `outputs/reports/pytest_risk_full_20260915_final2.xml` — `1f5a24d1f5bae7d218d5977c8cf6dc39f574b9e0dd8f915ddf039f80163ffda8` |
| `compileall`, PowerShell AST, Node AST, `git diff --check` | iki branch / promotion | passed; 36 PowerShell dosyası | platform static-check kanıtı |

Skip’ler Windows AppContainer/restricted-token veya gerçek POSIX namespace/ACL launcher yokluğu nedeniyle PASS sayılmadı; item 11 `BLOCKED_PLATFORM` olarak sınıflandırıldı.

## Uygulanan güvenlik ve V5 değişiklikleri

- Risk branch’inde whitelist key-set reconciliation, hesap-geneli IN/INOUT cooldown, gerçek account/positions/orders/history broker read set’i, monoton query sequence, sorgu sınırları ve approval sonrası ikinci fresh query uygulandı. Approval/risk reddinde `order_send=0` korunuyor; SQLite concurrency aynı transactional kapıda.
- Sandbox, doğrulanmış OS izolasyonu yoksa `BLOCKED_PLATFORM` ile fail-closed kalıyor; rlimit-only subprocess izolasyon kabulü yok.
- Acquisition coordinator yalnız zorunlu fixture root kabul ediyor; provider callback’i reddediyor, SQLite WAL ledger, CAS readback, run identity, crash/resume ve aynı artifact yarışında idempotent commit kullanıyor. Provider process hard deadline ve process-tree cleanup ile çalışıyor.
- Final reacquisition manifesti iki bağımsız audit identity, run/nonce/process/source/tree/frozen-hash bağları, raw→decoded→attestation→COMMITTED zinciri, replacement date bounds, repo dışı owner trust policy/replay ledger ve `ValidatedFinalManifest` döndürüyor. Owner imzası yoksa durum `AWAITING_OWNER_SIGNATURE`.
- Aktif Super1 runner ve Windows runtime contract V5’e bağlandı; V5 candidate yalnız `load_artifact(..., verify_inputs=True)` ve dedicated V5 validator üzerinden yükleniyor. V5 unsigned/live-disabled/fresh-forward-only; eski OOS/promotion alanları taşınmıyor.
- Public identity scan tracked dosyalar, korunmuş untracked dosyalar ve reachable Git object’lerini tarıyor; denylist değeri çıktılanmıyor, yalnız kapsam/yol/hash/sayaç kanıtı tutuluyor.

## 1–22 ayrı durum

| # | Durum | Kabul kanıtı | Eksik dış girdi / blocker |
|---:|---|---|---|
| 1 | `FAIL_CODE` | risk/approval/production-flow testleri; exact rejection ve `order_send=0` | Açık risk acceptance koşulları canlı broker kanıtıyla kapanmadı |
| 2 | `PASS_CODE` | retry, write-once ve broker-read negatif suite | Gerçek broker readback yetkisi yok |
| 3 | `PASS_CODE` | instrument metadata/tick/economic semantic negatif suite | Gerçek XM symbol snapshot yok |
| 4 | `PASS_CODE` | evaluation window, warmup, gap/closure suite | Tam üretim veri kapsamı residual nedeniyle yok |
| 5 | `FAIL_CODE` | atomic slot, approval replay/request-hash ve SQLite concurrency suite | İmzalı lease ve broker E2E yok |
| 6 | `PASS_CODE` | closed search, overlap, deterministic/no-valid-trial suite | Yeni gerçek forward/OOS çalışması yok |
| 7 | `PASS_CODE` | closed-terminal, null/reason, provenance risk-xray suite | Tam valid coverage yok |
| 8 | `PASS_CODE` | lifecycle/cursor/late-deal suite | Gerçek broker session/deal kanıtı yok |
| 9 | `PASS_CODE` | unknown-env ve secret-redaction suite | Gerçek deployment environment teyidi yok |
| 10 | `PASS_CODE` | denominator/nonfinite/numeric contract suite | Broker canlı quantization snapshot yok |
| 11 | `BLOCKED_PLATFORM` | sandbox negatif suite ve fail-closed code | Windows AppContainer/restricted-token ve POSIX namespace/ACL kanıtı yok |
| 12 | `FAIL_CODE` | acquisition coordinator/timeout/CAS negative suite | Provider-start/terminal-event/termination E2E fixture kanıtı yok |
| 13 | `FAIL_CODE` | persistent lock ve `record_success` negatif suite | Restart + iki gerçek süreç concurrency kabulü yok |
| 14 | `FAIL_CODE` | Node reader/timer/cancel negatif suite | Yavaş header/body ve >16 MiB fixture E2E kanıtı yok |
| 15 | `FAIL_CODE` | nested `target_key`/unknown dataset negatif suite | Residual-zero 113 hedef apply smoke’u yok |
| 16 | `FAIL_CODE` | raw/decoded/bundle recomputation ve detached-signature fail-closed code | İmzalı final manifest, source HEAD/event root ve repo dışı pinned key yok |
| 17 | `FAIL_CODE` | evidence path/hash ve identity scan code | Repo dışı security/history/rotation kanıt kökü ve gerçek artifact seti yok |
| 18 | `FAIL_CODE` | CAS/target/terminal contract ve coordinator code | Provider erişimi, fixture isolation, crash/resume/account-switch E2E kabulü yok |
| 19 | `BLOCKED_EXTERNAL` | `outputs/reports/dukascopy_reacquisition_v5/bundle_audit.json` SHA `b14daa3d4dbdb35109bb07e47e9ccc5887c02f7779503969efb72059f9f032cb` | 14 VERIFIED_LEGACY, 3 INVALID_LEGACY, 1 INCOMPLETE_STAGING, 95 MISSING; residual 99; provider call 0; final signed manifest yok |
| 20 | `BLOCKED_EXTERNAL` | `test_super1_instruction_19_22.py`; denylist olmadan açık blocked sonucu | Hesap sahibinin repo dışı denylist’i yok; public/history `UNASSESSED_MISSING_DENYLIST` |
| 21 | `BLOCKED_EXTERNAL` | no-push çalışma kuralı ve identity scan code | Owner-approved sanitized mirror, remote ref re-scan ve explicit lease yok |
| 22 | `BLOCKED_EXTERNAL` | deployment binding yalnız doğrulama kapısı olarak kaldı | Broker owner rotation/revocation ve yeni private signed binding kanıtı yok |

## Veri, güvenlik ve yayın sınırı

- Frozen inventory SHA: `a63406f235ded8d3daa123c0311adb678e53db3d996141b194493309f2cce075`; 113 unique hedef değişmedi.
- Acquisition state SHA: `c42eecf3bc6f4ae4a000a9f4a11f7c43140d147dfa9dbb3dce940c8a9a8dbccb`; state `DEFERRED_RATE_LIMIT`, provider call `0`.
- V4 dosyaları 660933a blob’larıyla birebir korunuyor: candidate `e02bad2aa7af3c3ee1db8fb1199e3217a50d117a`, manifest `bed6c345ec9ae5b291aeec2d46b14592a2bea33e`, signal `f898ab60ad147df4f2b35ab0662a71b84a4866c9`, config `2dc60b25c7f3489c4baa12a1ecd06ae064f9da1c`.
- V5 candidate file `1794ec151dfeced2cc3532647c8b25c6e8ac1ff8b6479646d0d38784d196a8a4`, artifact `3c78f33c338e825fc36e693bc18f4ed680ea9dd0d2751e2ff2437ea792e8e440`, signal `3ab042d941b5fc31411cbcd309d6d5a2935240a76ef5c570fc22dfee8703398d`, config `ef8582ce0a0d5ff4e251a21ba93e7ae912a2939a170070d0cfd3781663ae7ab5`, manifest `bb2bbc1445909703bf6c96e8c57d1c715944a820e735063eabc1a7a69559450b`, current engine `2330d9eb9466dc624fbbbefeb77704d03b2e614fd10d933f465623a74f892124`.
- Korunmuş untracked girdiler değiştirilmedi ve stage edilmedi. SHA-256’ları: `7cc0518a0c5957102cd867e358316761ca8330d80ecc0662fe84ea67f94df1ec` (3m manifest), `167259c5cc4cebde76d2c5d7b70ed65aa8f21c2ace2e8a10ace48b1e7a8a4839` (5m manifest), `af37ef2ff5e3b489df7d715aa826fd746af9138d762300661b48a783e3eecca3` (takvim), ve legacy backup dört dosyasında sırasıyla `e0fbaecf29dfc9208cfdd830c36d4264972a3ecc8bce78eb0f4ddafe6c2be130`, `d6785a2b6a80ad5d5de45ea6c33a9d38d343443fe0c4aa14667c5be3ef081d9d`, `88f0b9f91913ed4b1ccc2a7f5b4cb3ece181dca2d7bd4bd1a915dcae004a7f5b`, `1f7104e6b20676406102b6c798af05c41a2c31620e011662624ba229b1a5d683`.
- Report commit öncesi promotion working tree yalnız bu dört protected untracked girdiyi gösteriyordu; protected girdilerin hiçbiri staged olmadı.
