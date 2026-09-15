# SUPER1 — 1–22 Durum ve Kabul Kanıtı

Tarih: 2026-09-15
Promotion final HEAD: `c1795e36d63f994fb59d1d2cf7381d62514b21f8`
Promotion final tree: `aa4e94fe2106ab5ce52859b80eb9966477f3fdd6`
V5 binding commit: `ea297abb2f1ea4bb4776a4978e12fc2c653c9fe8`
Bound/tested production source commit: `788904525871b625c410faef1d8edd463f8e8550`
Bound/tested production source tree: `3e4a320183e15c3fb3abb2e5d3626fecb8368940`
Risk final HEAD: `4c3ed33bdc6a8db7113f7a9ae0233babfbe46ba6`
Risk final tree: `e3968dc8393526a84ec31abcd2847ea5e3a76caa`

## Nihai kapı

`NOT PROMOTABLE / NO PUSH / NO LIVE ORDER`

Provider çağrısı, broker hesabı/parolası, private key üretimi/kullanımı, remote işlem, history rewrite veya live order yapılmadı. Kod ve test kanıtı hazırlandı; platform ve dış owner/provider kanıtları olmadan promotion PASS üretilmedi. Tek harici paket: `C:\Users\ISAAC\Documents\OWNER_ACTION_REQUIRED_SUPER1_20260915.md`.

## Test ve branch kanıtı

| Kapsam | HEAD | Sonuç | JUnit / kanıt SHA-256 |
|---|---|---:|---|
| Promotion remediation hedefli suite | `ea297ab` | 29 passed, 0 skipped | acquisition/CLI, 12–18, 19–22 ve public-identity testleri |
| Provider coordinator subprocess E2E | `ea297ab` | 7 passed, 0 skipped | `tests/test_super1_provider_coordinator_e2e.py` — 200/429/500/timeout/crash/resume/concurrent lease |
| Risk sandbox hedefli suite | `4c3ed33` | 29 passed, 0 skipped | sandbox ve risk E2E subset |
| Promotion tam pytest | `ea297ab` | 782 passed, 3 skipped, 0 failed | `outputs/reports/pytest_promotion_full_20260915_super1_final.xml` — `b401f19b60932dd965eda3d8adbaac97c02a15ef70e0e2d1c10e5580b8e585a7` |
| Risk tam pytest | `4c3ed33` | 727 passed, 0 skipped, 0 failed | `risk worktree outputs/reports/pytest_risk_full_20260915_super1.xml` — `fe3bc8acebc98fad1cac8087c2d4dcb99b1f1b13f63032f5cf3bc0c96c582c40` |
| `compileall`, PowerShell AST, Node AST, `git diff --check` | iki branch / promotion | passed; 36 PowerShell dosyası | platform static-check kanıtı |

Promotion’daki üç skip Windows AppContainer erişim/launcher engeli nedeniyle PASS sayılmadı; risk full suite’inde skip yok. Item 11 `BLOCKED_PLATFORM` olarak sınıflandırıldı.

## Uygulanan güvenlik ve V5 değişiklikleri

- Risk branch’inde whitelist key-set reconciliation, hesap-geneli IN/INOUT cooldown, gerçek account/positions/orders/history broker read set’i, monoton query sequence, sorgu sınırları ve approval sonrası ikinci fresh query uygulandı. Approval/risk reddinde `order_send=0` korunuyor; SQLite concurrency aynı transactional kapıda.
- Sandbox, doğrulanmış OS izolasyonu yoksa `BLOCKED_PLATFORM` ile fail-closed kalıyor; rlimit-only subprocess izolasyon kabulü yok.
- Acquisition coordinator fixture ve provider modlarını ayırıyor; provider yolu status/URL/body/size/provenance doğrulamasından sonra CAS/COMMITTED yazıyor, lease sonrası `REQUEST_STARTED` açıyor ve her hata yolunda terminal event bırakıyor. SQLite WAL ledger, crash/resume, gerçek subprocess lease yarışı, CAS readback ve Windows process-start token ile fail-closed recovery ölçüldü. Node child transport 200/429, body cancel, oversize body ve timeout davranışları fixture harness ile ölçüldü.
- Windows child process `CREATE_SUSPENDED` + Job Object ataması başarıyla tamamlanmadan resume edilmiyor; atama/resume başarısızlığı child kill/reap ile fail-closed. Deadline descendant terminate/reap ve sessiz `taskkill` fallback’inin reddi kod/test kapısında.
- Final reacquisition manifesti iki bağımsız audit identity, run/nonce/process/source/tree/frozen-hash bağları, raw→decoded→attestation→COMMITTED zinciri, replacement date bounds, repo dışı owner trust policy/replay ledger ve `ValidatedFinalManifest` döndürüyor. Owner imzası yoksa durum `AWAITING_OWNER_SIGNATURE`.
- Aktif Super1 runner ve Windows runtime contract V5’e bağlandı; V5 candidate yalnız `load_artifact(..., verify_inputs=True)` ve dedicated V5 validator üzerinden yükleniyor. V5 unsigned/live-disabled/fresh-forward-only; eski OOS/promotion alanları taşınmıyor. Candidate/signal/config/manifest güncel source SHA, raw file SHA, artifact SHA, source commit/tree ve karşılıklı hash bağlarıyla mühürlendi.
- Test-only RSA-PSS detached attestation için pozitif imza ve bozuk imza negatif davranışı vardır; owner key kullanılmadı. Production risk akışı account-wide whitelist, monoton read sequence, approval sonrası ikinci snapshot, ortak SQLite `SEND_ARMED`/daily-slot transaction ve process yarışlarında `order_send=0`/tek slot davranışını kapsıyor.
- Owner trust policy purpose/nonce, harici provision edilmiş root anchor, tüketilmiş imzalı replay receipt ve atomic prepare→validate→publish akışıyla bağlandı. MT5 kabulü yalnız hash-pinned credentialsiz read-only probe kullanıyor; legacy `PASS_READ_ONLY` yolu kabul kapısından çıkarıldı.
- Public identity scan tracked/ignored untracked dosyalar, quarantine ve reachable Git object’lerini tarıyor; Git object okuması başarısızsa raw pack “temiz” sayılmıyor. Pre-push local cleanup ve post-push remote rescan ayrı durumlar olarak tutuluyor.

## 1–22 ayrı durum

| # | Durum | Kabul kanıtı | Eksik dış girdi / blocker |
|---:|---|---|---|
| 1 | `PASS_CODE` | risk/approval/production-flow testleri; exact rejection ve `order_send=0` | Read-only demo acceptance owner/provider kanıtını bekliyor |
| 2 | `PASS_CODE` | retry, write-once ve broker-read negatif suite | Gerçek broker readback yetkisi yok |
| 3 | `PASS_CODE` | instrument metadata/tick/economic semantic negatif suite | Gerçek XM symbol snapshot yok |
| 4 | `PASS_CODE` | evaluation window, warmup, gap/closure suite | Tam üretim veri kapsamı residual nedeniyle yok |
| 5 | `PASS_CODE` | atomic slot, approval replay/request-hash, ortak SQLite ve iki-process concurrency suite | İmzalı lease ve broker E2E owner acceptance’ı yok |
| 6 | `PASS_CODE` | closed search, overlap, deterministic/no-valid-trial suite | Yeni gerçek forward/OOS çalışması yok |
| 7 | `PASS_CODE` | closed-terminal, null/reason, provenance risk-xray suite | Tam valid coverage yok |
| 8 | `PASS_CODE` | lifecycle/cursor/late-deal suite | Gerçek broker session/deal kanıtı yok |
| 9 | `PASS_CODE` | unknown-env ve secret-redaction suite | Gerçek deployment environment teyidi yok |
| 10 | `PASS_CODE` | denominator/nonfinite/numeric contract suite | Broker canlı quantization snapshot yok |
| 11 | `BLOCKED_PLATFORM` | sandbox negatif suite ve fail-closed code | Windows AppContainer/restricted-token ve POSIX namespace/ACL kanıtı yok |
| 12 | `FAIL_CODE` | acquisition coordinator + provider subprocess 200/429/500/timeout/crash/resume E2E; Node child transport suite | Gerçek provider erişimi ve owner/provider dış kabulü yok |
| 13 | `FAIL_CODE` | persistent lease, SQLite WAL init retry, crash/resume ve concurrent lease E2E | Gerçek owner restart/provider acceptance birlikte kapanmadı |
| 14 | `FAIL_CODE` | status-first response validation, terminal event, CAS zero-invalid-commit ve Node child tests | Gerçek provider ortamı CLI E2E’si yok |
| 15 | `FAIL_CODE` | nested `target_key`/unknown dataset negative ve 113-target apply smoke | 113 hedefin gerçek provider ile `113/113 verified`, residual=0 sonucu yok |
| 16 | `FAIL_CODE` | raw/decoded/bundle recomputation, RSA-PSS positive/negative detached-signature testleri | İmzalı final manifest, source HEAD/event root ve repo dışı pinned key yok |
| 17 | `FAIL_CODE` | evidence path/hash, typed `ValidatedFinalManifest`, identity scan code | Repo dışı security/history/rotation kanıt kökü ve gerçek artifact seti yok |
| 18 | `FAIL_CODE` | CAS/target/terminal contract, lease, crash/resume and transaction code | Provider erişimi, fixture isolation/account-switch dış kabulü yok |
| 19 | `BLOCKED_EXTERNAL_ACCEPTANCE` | `outputs/reports/dukascopy_reacquisition_v5/bundle_audit.json` SHA `b14daa3d4dbdb35109bb07e47e9ccc5887c02f7779503969efb72059f9f032cb` | 14 VERIFIED_LEGACY, 3 INVALID_LEGACY, 1 INCOMPLETE_STAGING, 95 MISSING; residual 99; provider call 0; final signed manifest yok |
| 20 | `BLOCKED_EXTERNAL_ACCEPTANCE` | `test_super1_instruction_19_22.py`; denylist olmadan açık blocked sonucu | Hesap sahibinin repo dışı denylist’i yok; public/history `UNASSESSED_MISSING_DENYLIST` |
| 21 | `BLOCKED_EXTERNAL_ACCEPTANCE` | no-push çalışma kuralı ve identity scan code | Owner-approved sanitized mirror, reachable-object re-scan ve explicit lease yok |
| 22 | `BLOCKED_EXTERNAL_ACCEPTANCE` | deployment binding yalnız doğrulama kapısı olarak kaldı | Broker owner rotation/revocation ve yeni private signed binding kanıtı yok |

## Veri, güvenlik ve yayın sınırı

- Frozen inventory SHA: `a63406f235ded8d3daa123c0311adb678e53db3d996141b194493309f2cce075`; 113 unique hedef değişmedi.
- Acquisition state SHA: `c42eecf3bc6f4ae4a000a9f4a11f7c43140d147dfa9dbb3dce940c8a9a8dbccb`; state `DEFERRED_RATE_LIMIT`, provider call `0`.
- V4 dosyaları 660933a blob’larıyla birebir korunuyor: candidate `e02bad2aa7af3c3ee1db8fb1199e3217a50d117a`, manifest `bed6c345ec9ae5b291aeec2d46b14592a2bea33e`, signal `f898ab60ad147df4f2b35ab0662a71b84a4866c9`, config `2dc60b25c7f3489c4baa12a1ecd06ae064f9da1c`.
- V5 candidate file `1794ec151dfeced2cc3532647c8b25c6e8ac1ff8b6479646d0d38784d196a8a4`, artifact `3c78f33c338e825fc36e693bc18f4ed680ea9dd0d2751e2ff2437ea792e8e440`, signal `c58958730549426426394ddce13c070c5188032f8d3309c12d1c54633adef8d4`, config `aa01fe44d71a084ff492695abb955b4fecdad3fcfc53068ef13002d6aefc71ff`, manifest `014da9e4d8daf4ff56a2e3760d03428e9987f6827a6b3b28c7baf5068613f3cf`, current engine `af84b07871c8dbca648c8f343c53b2b632d3620701743fba337c1500280eb337`.
- V5 manifest source binding: commit `788904525871b625c410faef1d8edd463f8e8550`, tree `3e4a320183e15c3fb3abb2e5d3626fecb8368940`; `validate_production_source_binding` geçti ve eski `8d1ae6e` bağı kaldırıldı.
- Korunmuş untracked girdiler değiştirilmedi ve stage edilmedi. SHA-256’ları: `7cc0518a0c5957102cd867e358316761ca8330d80ecc0662fe84ea67f94df1ec` (3m manifest), `167259c5cc4cebde76d2c5d7b70ed65aa8f21c2ace2e8a10ace48b1e7a8a4839` (5m manifest), `af37ef2ff5e3b489df7d715aa826fd746af9138d762300661b48a783e3eecca3` (takvim), ve legacy backup dört dosyasında sırasıyla `e0fbaecf29dfc9208cfdd830c36d4264972a3ecc8bce78eb0f4ddafe6c2be130`, `d6785a2b6a80ad5d5de45ea6c33a9d38d343443fe0c4aa14667c5be3ef081d9d`, `88f0b9f91913ed4b1ccc2a7f5b4cb3ece181dca2d7bd4bd1a915dcae004a7f5b`, `1f7104e6b20676406102b6c798af05c41a2c31620e011662624ba229b1a5d683`.
- Promotion final working tree yalnız bu dört protected untracked girdiyi gösteriyor; protected girdilerin hiçbiri staged olmadı. Harici owner paketi repo dışındadır ve secret içermez.
