# SUPER1 — 1–22 Durum ve Kabul Kanıtı

Tarih: 2026-09-15
Promotion HEAD with V5 bindings: `ea81bd76076fd4ee14ef3aafef690eef26d05e92`
Promotion tree: `aa513237932704230744af9d6e45e70562589c06`
Bound/tested production source commit: `8d1ae6e0631e20d567c40cb7788e75ab5c006d93`
Bound/tested production source tree: `fb3067044e199a9324a6397f08e347272e4ff3ae`
Risk branch commit tested: `064e1a81117576a05559c05976b609c37c5db60`
Risk branch tree: `905a5a8dbbc958b90c5fb44d3e762c7163ff4cda`

## Nihai kapı

`NOT PROMOTABLE / NO PUSH / NO LIVE ORDER`

Provider çağrısı, broker hesabı/parolası, private key üretimi/kullanımı, remote işlem, history rewrite veya live order yapılmadı. Kod ve test kanıtı hazırlandı; platform ve dış owner/provider kanıtları olmadan promotion PASS üretilmedi. Tek harici paket: `C:\Users\ISAAC\Documents\OWNER_ACTION_REQUIRED_SUPER1_20260915.md`.

## Test ve branch kanıtı

| Kapsam | HEAD | Sonuç | JUnit / kanıt SHA-256 |
|---|---|---:|---|
| Promotion hedefli suite | `ea81bd7` | 83 passed, 1 skipped | `outputs/reports/pytest_promotion_targeted_20260915_final5.xml` — `0bdb86ae8722581b0546af04dfde90b5e25d36e5f83b237237f5d527f9789c46` |
| Risk + production flow hedefli suite | `cbd442c` | 19 passed, 0 skipped | `outputs/reports/pytest_risk_promotion_targeted_20260915_final2.xml` — `531b85c90f081b6c1a6e89460c01225df961085fa12a82765bf322ff0282d529` |
| Promotion tam pytest | `ea81bd7` | 771 passed, 3 skipped, 0 failed | `outputs/reports/pytest_promotion_full_20260915_final2.xml` — `c285255b49c7b171c3f84798244cf521aff7f09a577ced2f871e49a995508033` |
| Risk tam pytest | `064e1a8` | 726 passed, 0 skipped, 0 failed | `risk worktree outputs/reports/pytest_risk_full_20260915_acceptance2.xml` — `00e1c7b8e0acc0773037c6ceb436691d301039aa2edb6c8f07fbb54ba0de5d3f` |
| `compileall`, PowerShell AST, Node AST, `git diff --check` | iki branch / promotion | passed; 36 PowerShell dosyası | platform static-check kanıtı |

Skip’ler Windows AppContainer/restricted-token veya gerçek POSIX namespace/ACL launcher yokluğu nedeniyle PASS sayılmadı; item 11 `BLOCKED_PLATFORM` olarak sınıflandırıldı.

## Uygulanan güvenlik ve V5 değişiklikleri

- Risk branch’inde whitelist key-set reconciliation, hesap-geneli IN/INOUT cooldown, gerçek account/positions/orders/history broker read set’i, monoton query sequence, sorgu sınırları ve approval sonrası ikinci fresh query uygulandı. Approval/risk reddinde `order_send=0` korunuyor; SQLite concurrency aynı transactional kapıda.
- Sandbox, doğrulanmış OS izolasyonu yoksa `BLOCKED_PLATFORM` ile fail-closed kalıyor; rlimit-only subprocess izolasyon kabulü yok.
- Acquisition coordinator yalnız zorunlu fixture root kabul ediyor; provider callback’i reddediyor, SQLite WAL ledger, CAS readback, run identity, crash/resume ve aynı artifact yarışında idempotent commit kullanıyor. SQLite ilk-açılış WAL yarışında kısa locked retry var; provider process hard deadline ve process-tree cleanup ile çalışıyor. Node child transport 200/429, body cancel, oversize body ve timeout davranışları fixture harness ile ölçüldü.
- Final reacquisition manifesti iki bağımsız audit identity, run/nonce/process/source/tree/frozen-hash bağları, raw→decoded→attestation→COMMITTED zinciri, replacement date bounds, repo dışı owner trust policy/replay ledger ve `ValidatedFinalManifest` döndürüyor. Owner imzası yoksa durum `AWAITING_OWNER_SIGNATURE`.
- Aktif Super1 runner ve Windows runtime contract V5’e bağlandı; V5 candidate yalnız `load_artifact(..., verify_inputs=True)` ve dedicated V5 validator üzerinden yükleniyor. V5 unsigned/live-disabled/fresh-forward-only; eski OOS/promotion alanları taşınmıyor. Candidate/signal/config/manifest güncel source SHA, raw file SHA, artifact SHA, source commit/tree ve karşılıklı hash bağlarıyla mühürlendi.
- Test-only RSA-PSS detached attestation için pozitif imza ve bozuk imza negatif davranışı vardır; owner key kullanılmadı. Production risk akışı account-wide whitelist, monoton read sequence, approval sonrası ikinci snapshot, ortak SQLite `SEND_ARMED`/daily-slot transaction ve process yarışlarında `order_send=0`/tek slot davranışını kapsıyor.
- Public identity scan tracked dosyalar, korunmuş untracked dosyalar ve reachable Git object’lerini tarıyor; denylist değeri çıktılanmıyor, yalnız kapsam/yol/hash/sayaç kanıtı tutuluyor.

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
| 12 | `PASS_CODE` | acquisition coordinator/timeout/CAS negative suite; Node child transport davranışı | Provider-start/terminal-event/termination dış kabul kanıtı yok |
| 13 | `PASS_CODE` | persistent lease, SQLite WAL init retry, `record_success`, crash/resume ve process race suite | Owner restart acceptance kanıtı yok |
| 14 | `PASS_CODE` | Node 200/429 reader, timeout, cancel ve >16 MiB body child tests | Gerçek provider ortamı dış kabulü yok |
| 15 | `PASS_CODE` | nested `target_key`/unknown dataset negative ve 113-target apply smoke | Gerçek residual bundle seti owner tarafından tamamlanmadı |
| 16 | `PASS_CODE` | raw/decoded/bundle recomputation, RSA-PSS positive/negative detached-signature testleri | İmzalı final manifest, source HEAD/event root ve repo dışı pinned key yok |
| 17 | `PASS_CODE` | evidence path/hash, typed `ValidatedFinalManifest`, identity scan code | Repo dışı security/history/rotation kanıt kökü ve gerçek artifact seti yok |
| 18 | `PASS_CODE` | CAS/target/terminal contract, lease, crash/resume and transaction code | Provider erişimi, fixture isolation/account-switch dış kabulü yok |
| 19 | `BLOCKED_EXTERNAL_ACCEPTANCE` | `outputs/reports/dukascopy_reacquisition_v5/bundle_audit.json` SHA `b14daa3d4dbdb35109bb07e47e9ccc5887c02f7779503969efb72059f9f032cb` | 14 VERIFIED_LEGACY, 3 INVALID_LEGACY, 1 INCOMPLETE_STAGING, 95 MISSING; residual 99; provider call 0; final signed manifest yok |
| 20 | `BLOCKED_EXTERNAL_ACCEPTANCE` | `test_super1_instruction_19_22.py`; denylist olmadan açık blocked sonucu | Hesap sahibinin repo dışı denylist’i yok; public/history `UNASSESSED_MISSING_DENYLIST` |
| 21 | `BLOCKED_EXTERNAL_ACCEPTANCE` | no-push çalışma kuralı ve identity scan code | Owner-approved sanitized mirror, reachable-object re-scan ve explicit lease yok |
| 22 | `BLOCKED_EXTERNAL_ACCEPTANCE` | deployment binding yalnız doğrulama kapısı olarak kaldı | Broker owner rotation/revocation ve yeni private signed binding kanıtı yok |

## Veri, güvenlik ve yayın sınırı

- Frozen inventory SHA: `a63406f235ded8d3daa123c0311adb678e53db3d996141b194493309f2cce075`; 113 unique hedef değişmedi.
- Acquisition state SHA: `c42eecf3bc6f4ae4a000a9f4a11f7c43140d147dfa9dbb3dce940c8a9a8dbccb`; state `DEFERRED_RATE_LIMIT`, provider call `0`.
- V4 dosyaları 660933a blob’larıyla birebir korunuyor: candidate `e02bad2aa7af3c3ee1db8fb1199e3217a50d117a`, manifest `bed6c345ec9ae5b291aeec2d46b14592a2bea33e`, signal `f898ab60ad147df4f2b35ab0662a71b84a4866c9`, config `2dc60b25c7f3489c4baa12a1ecd06ae064f9da1c`.
- V5 candidate file `1794ec151dfeced2cc3532647c8b25c6e8ac1ff8b6479646d0d38784d196a8a4`, artifact `3c78f33c338e825fc36e693bc18f4ed680ea9dd0d2751e2ff2437ea792e8e440`, signal `f8680a772498e65b47b84796ceeff56272902924637cbf5d02063921bca8e78f`, config `f838e3a7db28e832b3247425ee0c8b93d7fc20a14e11f31d2b44c550a30ca90b`, manifest `d96b512d90f0b98a709939858915b40a709c24f105103cb1987198e0469dcb22`, current engine `7e653988972692356d8967090fac0539ccec09324f2dcf267fd16e0ccad1359a`.
- V5 manifest source binding: commit `8d1ae6e0631e20d567c40cb7788e75ab5c006d93`, tree `fb3067044e199a9324a6397f08e347272e4ff3ae`.
- Korunmuş untracked girdiler değiştirilmedi ve stage edilmedi. SHA-256’ları: `7cc0518a0c5957102cd867e358316761ca8330d80ecc0662fe84ea67f94df1ec` (3m manifest), `167259c5cc4cebde76d2c5d7b70ed65aa8f21c2ace2e8a10ace48b1e7a8a4839` (5m manifest), `af37ef2ff5e3b489df7d715aa826fd746af9138d762300661b48a783e3eecca3` (takvim), ve legacy backup dört dosyasında sırasıyla `e0fbaecf29dfc9208cfdd830c36d4264972a3ecc8bce78eb0f4ddafe6c2be130`, `d6785a2b6a80ad5d5de45ea6c33a9d38d343443fe0c4aa14667c5be3ef081d9d`, `88f0b9f91913ed4b1ccc2a7f5b4cb3ece181dca2d7bd4bd1a915dcae004a7f5b`, `1f7104e6b20676406102b6c798af05c41a2c31620e011662624ba229b1a5d683`.
- Promotion final working tree yalnız bu dört protected untracked girdiyi gösteriyor; protected girdilerin hiçbiri staged olmadı. Harici owner paketi repo dışındadır ve secret içermez.
