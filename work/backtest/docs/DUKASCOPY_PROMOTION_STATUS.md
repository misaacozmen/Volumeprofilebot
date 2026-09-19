# Dukascopy 12–19 promotion durumu

Genel durum: `ACTIVE_PREPARATION`

Bu hat askıya alınmış değildir. Dış girdiye bağlı maddeler ayrıca
`BLOCKED_EXTERNAL_INPUT` olarak tutulur. Bu tur envanter ve yerel kanıt
doğrulamasıyla sınırlıdır; toplu veri indirme, promotion, gerçek hesap
bağlantısı, imza üretimi ve gerçek MT5 çalıştırılmamıştır.

## Kaynak ayrımı

Güncel `origin/main` commit’i `e205e66...` içinde promotion worktree’sindeki
12–19 implementation dosyaları bulunmuyor. Yerel kod/test kanıtı aşağıdaki
ayrı ve kirli promotion worktree’sine aittir; yeni dokümantasyon branch’ine
taşınmamıştır:

- branch: `codex/super1-promotion-blockers-v4`
- committed HEAD: `1e911d96caa0c73e3cf477981ee699c13e719f47`
- committed tree: `c80784d2fe38d1d0c18824300fdc7ceb9db19824`
- çalışma ağacı: commit edilmemiş delta ve untracked protected girdiler içeriyor.

Uncommitted delta SHA’ları:

- `work/backtest/tools/dukascopy-downloader/acquire_v5.mjs`:
  `F8156C9512AD160B6875E4880976F62A3312E9157FD298208B9C60AEF2E134D1`.
- `work/backtest/tests/test_super1_real_stream_provider_e2e.py`:
  `C7CB4BF7696556AB443EFAAA1385D9CD1FEE471C4E4335C96ED72CAF99E1E139`.

Committed promotion source SHA’ları:

- `backtest/dukascopy_acquisition.py`:
  `BC58FA5EF38152B5A35990F50BFB0DFA877DD454C33B1C32C8BABBF1F886642D`.
- `backtest/reacquisition_contract.py`:
  `74EB4748CE45FC3ACF815EE4559BC9F1ECC04C130269253E9758976F8339E881`.
- `scripts/audit_dukascopy_reacquisition_v5.py`:
  `15BCF11BE6E123B8520C2819FD1FB012E89621225FC103060D4616D09AB4348F`.

## Envanter, CAS/manifest ve residual durumu

- Frozen hedef envanteri: 113 CSV satırı; repo dışı kanıt kopyası
  `C:\Users\ISAAC\Documents\workstream-status-evidence-20260919\frozen_invalid_leg_days_v4.csv`;
  SHA-256
  `a63406f235ded8d3daa123c0311adb678e53db3d996141b194493309f2cce075`.
- `bundle_audit.json` repo dışı kanıt kopyası
  `C:\Users\ISAAC\Documents\workstream-status-evidence-20260919\bundle_audit.json`;
  113 hedef satırı; SHA-256
  `b14daa3d4dbdb35109bb07e47e9ccc5887c02f7779503969efb72059f9f032cb`.
- Bundle audit durumları: `14 VERIFIED_LEGACY`, `3 INVALID_LEGACY`,
  `1 INCOMPLETE_STAGING`, `95 MISSING`; unresolved residual `99`.
- V4 data manifest repo dışı kanıt kopyası
  `C:\Users\ISAAC\Documents\workstream-status-evidence-20260919\data_manifest_v4.json`;
  SHA-256
  `97c7f0f8d3fce09dab54ea2f6ba3234b58ce14bb4533962862355c0f1c0fc9c1`.
- Her bundle audit satırında CAS/bundle, manifest, minute/derived path ve raw
  hash bağları bulunan legacy kayıtlar vardır; bu, 113/113 güncel provider
  kabulü değildir.
- Canonical final acquisition ledger ve owner-imzalı `COMMITTED` V5
  manifest bu çalışma ağacında mevcut değildir.
- Residual-zero doğrulaması gerçek provider koşusunda yoktur; fixture apply
  testi residual-zero kod davranışını gösterir ama gerçek release kabulü
  değildir.
- Owner/provider imzalı manifest, rotation/attestation ve gerçek provider
  kabulü yoktur.

## Madde bazlı plan

| Madde | Gereksinim ve mevcut kod/test durumu | Branch/commit veya delta SHA | Eksik dış kanıt / sağlayacak kişi | Sonraki işlem | Tamamlanma ölçütü |
|---:|---|---|---|---|---|
| 12 | `AcquisitionCoordinator` provider yolu; dedup ve 429/5xx/404/timeout/crash yerel E2E’leri `PASS_CODE`. | Promotion `1e911d96...`; `backtest/dukascopy_acquisition.py` SHA yukarıda. | Gerçek Dukascopy provider erişimi ve owner kabulü — proje sahibi/provider sahibi. `BLOCKED_EXTERNAL_INPUT`. | Aynı 113 hedef manifestiyle kontrollü gerçek provider koşusu için owner girdisini bekle. | Gerçek provider response/ledger kanıtı, signed run bağları ve 200-success dedup kabulü. |
| 13 | Persistent host-state spacing, request-start/terminal ledger negatifleri yerel olarak geçti; dış rate-limit kabulü yok: `PASS_CODE`. | Promotion `1e911d96...`; provider coordinator kaynak SHA’sı yukarıda. | Gerçek dış rate-limit penceresi ve provider gözlemi — provider sahibi. `BLOCKED_EXTERNAL_INPUT`. | Provider’ın yazılı rate-limit koşulunu ve gözlem penceresini al. | Gerçek 429/5xx/timeout altında spacing, retry ve terminal ledger eşleşmesi. |
| 14 | Locked Node `fetchWithLimits`; gerçek TLS/socket mock server E2E’si fragmented success, delayed deadline, cancel, half-body, drop ve oversized-body vakalarını geçti: `PASS_CODE`. | Uncommitted `acquire_v5.mjs` ve real-stream test SHA’ları yukarıda; JUnit SHA `81682EBF...`. | Gerçek provider HTTPS cevabı ve owner/provider kabulü — provider sahibi/proje sahibi. `BLOCKED_EXTERNAL_INPUT`. | Gerçek provider olmadan production download başlatma; kabul girdisini bekle. | Gerçek provider response limit/cancel davranışı, no-CAS/no-COMMITTED negatifleri ve owner imzalı bağ. |
| 15 | Nested target key ve 113-target apply fixture testi geçti: `PASS_CODE`; bu sentetik bridge’dir. | `reacquisition_contract.py` SHA yukarıda; inventory SHA `a63406f...`. | 113/113 gerçek provider verified sonucu ve residual-zero owner manifesti — proje sahibi. `BLOCKED_EXTERNAL_INPUT`. | Frozen inventory’yi değiştirmeden gerçek provider çıktısını canonical manifest’e bağla. | 113/113 verified, residual `0`, every target CAS/manifest hash bound. |
| 16 | Raw/decoded/bundle recomputation ve RSA-PSS positive/negative yerel testleri geçti: `PASS_CODE`. | Promotion committed source `1e911d96...`; local 12–22 JUnit SHA `60AF5E7A...`. | Final signed manifest, owner key/attestation ve source HEAD/event root — proje sahibi. `BLOCKED_EXTERNAL_INPUT`. | Owner’ın imzalı manifest paketini alıp detached verification ile doğrula. | Signature, source/tree, inventory, bundle and run nonce bindings all verify. |
| 17 | Evidence path/hash, typed final manifest ve identity-scan kod kapıları yerel olarak mevcut: `PASS_CODE`. | Promotion `1e911d96...`; external acceptance artifact’leri current-source delta’ya bağlı. | Repo dışı security/history/rotation evidence root — proje sahibi. `BLOCKED_EXTERNAL_INPUT`. | Evidence root’u branch/commit/tree’ye immutable biçimde bağla. | Evidence paths, hashes and source/ref identity match without synthetic substitution. |
| 18 | Acquisition crash/retry/concurrent lease ve account-switch/fresh-read transaction testleri yerel olarak geçti: `PASS_CODE`. | Promotion `1e911d96...`; uncommitted real-stream delta SHA’ları yukarıda. | Provider/DEMO dış kanıtı ve gerçek account-switch read-only evidence — proje sahibi. `BLOCKED_EXTERNAL_INPUT`. | Broker write olmadan owner-approved read-only observation al. | Lease/CAS/transaction invariants hold on real provider/DEMO observation. |
| 19 | Reacquisition audit/apply fixture testi geçti: `PASS_CODE`; mevcut bundle audit residual `99`. | `scripts/audit_dukascopy_reacquisition_v5.py` SHA yukarıda; audit SHA `b14daa3d...`. | Final owner-signed V5 manifest ve residual-zero real provider audit — proje sahibi. `BLOCKED_EXTERNAL_INPUT`. | Audit’i frozen 113 inventory ve signed manifest ile yeniden çalıştır. | Audit status clean, 113/113 committed, residual `0`, owner signature valid. |

Yerel JUnit kanıtları:

- `C:\Users\ISAAC\Documents\workstream-status-evidence-20260919\promotion-12-22-local-20260917-v1.xml` — 32 passed, SHA-256 `60AF5E7A5B48A0818B054CDE457F297FE4F4EF1A14C0CFF603D66E38F147EA20`.
- `C:\Users\ISAAC\Documents\workstream-status-evidence-20260919\promotion-12-22-forward-local-20260917-v1.xml` — 9 passed, SHA-256 `CD47DA7F2703FFBC64183E5B89B9C856EDB5985F991C17DBFD58B6D111D8F457`.
- `C:\Users\ISAAC\Documents\workstream-status-evidence-20260919\promotion-12-22-current-source-20260917-v2.xml` — 46 passed, SHA-256 `81682EBFFE8A4FEFB3EB7A4C56BAE2D6D3F83CE90C7791B46490241BFE3FA035`.

Bu sonuçlar `PASS_CODE`’dur; `PASS_EXTERNAL_ACCEPTANCE` veya
`DEPLOYMENT_READY` değildir. Sentetik/fixture sonucu gerçek provider kabulü
olarak sunulmamıştır.

## Üç iş akışı için ortak durum tablosu

| İş akışı | Genel durum | Uygulama sorumlusu | Karar sahibi |
|---|---|---|---|
| Risk-hardening 1–11 | `REVALIDATION_REQUIRED` | Proje mühendisi | Proje mimarı |
| Dukascopy/promotion 12–19 | `ACTIVE_PREPARATION` | Proje mühendisi | Proje mimarı; dış girdilerde proje sahibi |
| Broker-identity güvenliği | `CLOSED_WITH_RETAINED_HISTORY` | Proje mühendisi; sürekli tarama CI | Proje mimarı |

Son doğrulama zamanı: `2026-09-19T18:04:38Z`.
