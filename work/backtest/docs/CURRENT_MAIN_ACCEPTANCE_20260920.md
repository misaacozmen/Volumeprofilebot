# 2026-09-20 main ve dış kabul kaydı

Bu kayıt, `PROJECT_WORKSTREAM_STATUS.md` ve `DUKASCOPY_PROMOTION_STATUS.md`
belgelerindeki 19 Eylül tarihli durumların yerine güncel karar kaydıdır.
Kod, main entegrasyonu ve dış kabul birbirinden ayrı değerlendirilir.

## Kaynak ve kapsam

- Kabul edilmiş DEPLOY-001: `d1c0b07cdbf5729136cc436746f78ddcc00544ec`.
- DEPLOY-001 main merge: `779a34f266275ee3e0f0809398a367839252a0f3`.
- Checkout/harness düzeltmesi: `448df9b88cd45ba7512176c4d302302a7790b953`.
- Yenilenmiş hedef kanıt: `1d26582`; manifest tam commit/tree bağını içerir.
- Birleşik doğrulama ref'i: `af07dcbe7ec7377940445e2b932de4edd38dd767`.
- Temiz doğrulama checkout'u: `C:/Users/ISAAC/Documents/current-main-acceptance-20260920`.
- Repo dışı kanıt kökü: `C:/Users/ISAAC/Documents/all-workstreams-evidence-20260920`.

## Beş iş kalemi

| İş | Kod / test | Main entegrasyonu | Gerçek / dış kabul |
|---|---|---|---|
| 1. DEPLOY-001 | `PASS_CODE`; kabul edilmiş kaynak | `779a34f` ile merge edildi | Deployment kabulü değildir |
| 2. V08 / Super1 hash | Pinler değiştirilmeden checkout kuralları düzeltildi; `PASS_CODE` | `af07dcb` birleşik ref | Tam release kapısı ayrı; aşağıdaki engeller açık |
| 3. Gerçek signing/deployment | Gerçek DPAPI anahtarıyla challenge imzası, release public key ile doğrulandı | Release kodu main'de | `BLOCKED`: release manifest imzalanmadı, deployment denenmedi; oturum yönetici değil |
| 4. Risk-hardening 1–11 | Main'deki komşu regresyonlar geçti; 11 maddenin modül/testleri main'de yok | `NOT_PRESENT_ON_MAIN` | 11 madde için toplu kabul verilmedi |
| 5. Dukascopy/promotion 12–19 | Mevcut snapshot ve 56 dosya bağı yeniden incelendi | Promotion uygulaması ayrı/kirli worktree'de | `BLOCKED_EXTERNAL_INPUT`; provider/owner kabulü yok |

`DEPLOYMENT_READY = false`. Hedef testlerin geçmesi tam release kapısı veya
provider/owner kabulü yerine geçmez.

## Checkout ve test kanıtı

V08 manifest, candidate, signal contract ve provenance fixture mevcut LF
pinlerine; V09 sözleşmesi, runtime kaynakları ve motor mevcut CRLF pinlerine
bağlıdır. Kök `.gitattributes` bu mevcut sözleşmeleri açıkça belirtir.
`core.autocrlf=true/false` ile gerçek Git checkout testleri aynı pinleri doğrular.
Önceden açılmış worktree'lerde yalnız attribute merge etmek mevcut dosyaları
yeniden yazmaz; kabul testi yeni ve temiz checkout üzerinde çalıştırılmıştır.

PowerShell harness `write_text` üzerinden mevcut CRLF'ye fazladan CR ekliyordu.
Harness artık verilen UTF-8 baytlarını korur; LF ve CRLF continuation regresyonu
ayrı ayrı çalışır. Üretim scriptleri ve beklenen güvenlik hash'leri değişmedi.

- Genişletilmiş hedef regresyon: **219 passed**.
- Evidence verifier: **2 passed**, her artifact için tekil bozulma negatifleri dahil.
- Koleksiyon: **576 node**, import/collection hatası yok.
- Üç production PowerShell scriptinin AST parse sonucu: **0 hata**.
- Temiz checkout tam test sonucu: **571 passed, 5 failed, 0 errors, 0 skipped**
  (576 test; exit 1). Repo dışı `current-main-full.junit.xml` /
  `current-main-full.log` ve `acceptance-manifest.json` ile bağlıdır.
  `PASS_FULL_SUITE` değildir.

Tam release kapısındaki ayrı engeller:

1. İki threshold testi, `first30_thresholds_pre2025_v1.json` provenance girdilerini
   reddediyor. Dört motor dosyasının eski araştırma pinleri Git LF blob'una,
   Super1 motor pin'i ise CRLF checkout'una eşleşiyor. Aynı kaynak dosyaları için
   iki ham-bayt beklentisi bir checkout'ta birlikte sağlanamaz. Pinler değiştirilmedi;
   araştırma artifact'inin kaynak/veri zinciriyle yeniden üretilmesi gerekir.
2. Candidate finalizer testi için `selected_trades.csv` temiz checkout'ta yok.
3. Capital two-leg testi yerel `live_forward/capital_demo_config.json` dosyasına
   bağlı; temiz checkout bu dosyayı içermiyor. Özel runtime config repoya taşınmadı.
4. Forward baseline `run_manifest.json` temiz checkout'ta yok. Eski çalışma
   ağacındaki kopyanın LF ve CRLF hash'leri de beklenen pine eşleşmiyor;
   bu kopya güvenilir baseline yerine kullanılmadı.

## Risk-hardening 1–11

`risk-main-matrix.json`, her yolun Git tree'de bulunup bulunmadığını ve ayrı risk
worktree'sindeki varlığını kaydeder. Aşağıdaki modüller ve ilgili hedef testleri
main'de bulunmadığından bu maddeler için `PASS_CODE` ilan edilmez.

| Madde | Main'de bulunmayan temel kaynak |
|---:|---|
| 1 | `backtest/live/risk_guard.py`, `production_flow.py`, `execution.py` |
| 2 | `backtest/live/retry.py` |
| 3 | `backtest/live/instruments.py` |
| 4 | `backtest/evaluation_window.py` |
| 5 | `backtest/live/approval.py`, `scripts/super1_order_approval.py` |
| 6 | `backtest/optimization.py` |
| 7 | `backtest/risk_xray.py` |
| 8 | `backtest/live/strategy_health.py` |
| 9 | `schemas/super1_environment_v1.schema.json`, environment scanner |
| 10 | `backtest/numeric_contracts.py` |
| 11 | `backtest/sandbox.py`, `scripts/windows_appcontainer_launcher.py` |

Komşu regresyonun **133 testi** (risk causality, engine contracts, broker
identity env, XM forward) hem `779a34f` üzerinde hem `af07dcb` temiz checkout
tam koşusunda geçti. Bu testler 11 maddenin eksik uygulamalarının yerine geçmez.
Ayrı risk worktree'sindeki commit edilmemiş kod bu teslimata otomatik taşınmadı.

## Gerçek imzalama ve Windows kabulü

`key-access-result.json` gerçek kullanıcı DPAPI anahtarına erişildiğini ve
üretilen challenge imzasının release helper'daki public key ile doğrulandığını
gösterir. Challenge açıkça release manifest olmadığını belirtir. Gizli anahtar
rapora veya repoya yazılmadı. Release builder'ın tam test kapısı açıkken release
imzası üretilmedi. Mevcut oturum elevated değildir; gerçek upgrade, MT5/broker,
task/service veya ACL değişimi yapılmadı.

Tam kabul için tam test kapısının geçmesi, yönetici yetkili Windows hedef/oturum
bağının sağlanması ve gerçek signed release ile upgrade/rollback kanıtı gerekir.

## Promotion 12–19 dış kabul hazırlığı

Snapshot SHA-256:
`b14daa3d4dbdb35109bb07e47e9ccc5887c02f7779503969efb72059f9f032cb`.
Tarihsel dağılım: 113 hedef; 14 VERIFIED_LEGACY, 3 INVALID_LEGACY,
1 INCOMPLETE_STAGING, 95 MISSING; tarihsel residual **99**.

Yeni kontrol: 14 legacy hedefin manifest/minute/derived dosyalarına ait **42/42**
hash snapshot ile eşleşti. **14/14 acceptance attestation** hash'i eşleşmedi.
Fark LF/CRLF değildir; ortak alanlarda `auditor_sha256` ve `node_helper_sha256`
değişmiştir. Bu nedenle eski 14 VERIFIED_LEGACY etiketi güncel dış kabul olarak
taşınamaz. Yeni residual-zero/provider koşusu yapılmadı; yeni residual sayısı
uydurulmadı. Mevcut veri ve attestation dosyaları değiştirilmedi.

`promotion-snapshot-recheck.json` ve `promotion-attestation-drift.json` bu
bulguları tekil hedef/dosya bazında kaydeder. Gerekli dış girdiler
`external-acceptance-prerequisites.json` içinde ayrıdır:

- 12–14: provider rate-limit koşulları, gözlem penceresi ve normal erişim kanıtı.
- 15, 19: 113 hedefe bağlı canonical V5 manifest ve gerçek residual-zero kabulü.
- 16–17: owner public-key/trust pinleri, detached imza/attestation,
  source/tree/run nonce/event-root ve replay bağları.
- 18: gerçek DEMO/account-switch read-only gözlemi.

Demo repository publication ve signed promotion farklı kabul yollarıdır.
Fixture imzası veya yerel test anahtarı gerçek owner kabulü yerine kullanılamaz.
Tarih politikası korunur: `HISTORY_RETAINED_KNOWN_INACTIVE_IDENTIFIERS`;
history rewrite ve force-push yapılmadı.
