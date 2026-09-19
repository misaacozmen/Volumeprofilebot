# Proje iş akışları güncel durum kaydı

Kaynak worktree: `codex/workstream-status-release-audit`.

- Başlangıç commit: `e205e66a57ec5c54585c40d9e5ec36d179f4f7cf`.
- Başlangıç tree: `452d347c513ca98dfeffa32b316b13b7659e765e`.
- Bu tur yalnızca dokümantasyon ve repo dışı kanıt üretir.
- Risk/promotion worktree’lerindeki değişiklikler bu branch’e taşınmamıştır.
- Üretim kodu, release hash’i, test beklentisi, denylist, tarihsel baseline ve
  GitHub secret değiştirilmemiştir.

Git doğrulaması (`git rev-parse <commit>^{tree}`): current main
`e205e66...` → `452d347c513ca98dfeffa32b316b13b7659e765e`; önceki başlangıç
`023e39b...` → `72fca2ff95fbf0ad07ad536bab14abd690ae577d`; promotion
`1e911d96...` → `c80784d2fe38d1d0c18824300fdc7ceb9db19824`; risk
`4810a732...` → `9fc0259b66c51f617229de765c8df34d6e5c3c3d`.

## Üç iş akışının tek güncel tablosu

| İş akışı | Genel durum | Uygulama sorumlusu | Karar sahibi |
|---|---|---|---|
| Risk-hardening 1–11 | `REVALIDATION_REQUIRED` | Proje mühendisi | Proje mimarı |
| Dukascopy/promotion 12–19 | `ACTIVE_PREPARATION` | Proje mühendisi | Proje mimarı; dış girdilerde proje sahibi |
| Broker-identity güvenliği | `CLOSED_WITH_RETAINED_HISTORY` | Proje mühendisi; sürekli tarama CI | Proje mimarı |

## Risk-hardening 1–11

Önceki risk worktree’sinde yerel test ve OS/AppContainer kanıtları bulunur;
ancak bu kayda otomatik kapanış taşınmamıştır. Risk branch’i güncel `main`
değildir ve uncommitted delta içerdiği için bu belge onu `MERGED_MAIN` veya
`DEPLOYMENT_READY` olarak sunmaz. Maddeler tek tek yeniden doğrulanmadan hat
`REVALIDATION_REQUIRED` kalır. Önceki teslim kanıtı tarihsel referanstır:

`C:\Users\ISAAC\Documents\otobacktestprojesi\work\backtest\docs\SUPER1_FINAL_DELIVERY_20260916.md`

- Kod/test durumu: risk worktree’sinde 1–11 için yerel kanıt mevcut;
  current-main entegrasyonu bu turda yapılmadı.
- Main entegrasyonu: `MERGED_MAIN` değil; güncel `main` yalnız bu doküman
  branch’inin başlangıç noktasıdır.
- Dış kabul: maddeler ayrı kanıt ister; otomatik kapatma yok.
- Deployment uygunluğu: `DEPLOYMENT_READY` değil; ayrıca DEPLOY-001 açık.
- Sonraki işlem: 1–11 maddelerini current main’e taşımadan, her birini aynı
  ref üzerinde yeniden çalıştırmak ve sonuçları ayrı kaydetmek.

Madde bazlı eksik durum tablosu:

| Madde | Kod ref’i | Mevcut kanıt | Main entegrasyonu | Dış kabul | Sonraki işlem |
|---:|---|---|---|---|---|
| 1 | `backtest/live/risk_guard.py`, `production_flow.py`, `execution.py`, `scripts/run_xm_mt5_forward.py` | Risk worktree’sindeki stale/non-finite/partial-close/daily-loss ve SQLite serialization testleri: `PASS_CODE` | `UNVERIFIED` — current main’e taşınmadı | `UNVERIFIED` — broker/owner kabulü yok | Current main’de aynı fixture ve lifecycle testlerini yeniden çalıştır |
| 2 | `backtest/live/retry.py` | Retry/backoff/terminal failure testleri: `PASS_CODE` | `UNVERIFIED` | `NOT_APPLICABLE` — yerel transport sözleşmesi; dış provider kabulü değil | Current main’de retry negatiflerini doğrula |
| 3 | Instrument registry/metadata doğrulama kodu | Exact-case symbol ve semantic/economic probe fail-closed testleri: `PASS_CODE` | `UNVERIFIED` | `BLOCKED_EXTERNAL_INPUT` — XM snapshot/provider girdisi yok | Main’e bağlanmış metadata ve provider snapshot kanıtını üret |
| 4 | `evaluation_window`, CLI ve walk-forward akışı | Evaluation boundary/walk-forward testleri: `PASS_CODE` | `UNVERIFIED` | `NOT_APPLICABLE` — deterministik yerel kabul | Current main’de boundary regresyonunu çalıştır |
| 5 | Approval/production-flow ve read-only MT5 acceptance kodu | Approval, fake-MT5 ve no-send testleri: `PASS_CODE` | `UNVERIFIED` | `BLOCKED_EXTERNAL_INPUT` — broker/owner kabulü yok | Credentialsiz owner-signed read-only kanıtı al |
| 6 | CLI/walk-forward optimization akışı | Optimization contract testleri: `PASS_CODE` | `UNVERIFIED` | `NOT_APPLICABLE` — araştırma/yerel deterministik kapı | Main’de aynı optimization fixture’ını doğrula |
| 7 | `risk_xray` ve CLI | Risk-Xray pozitif/negatif testleri: `PASS_CODE` | `UNVERIFIED` | `NOT_APPLICABLE` — yerel deterministik rapor | Main’de kod ref’i ve çıktıyı bağla |
| 8 | `strategy_health`, `scripts/run_xm_mt5_forward.py` | Health/drift/no-send testleri: `PASS_CODE` | `UNVERIFIED` | `BLOCKED_EXTERNAL_INPUT` — owner/broker gözlemi yok | Main’de health çıktısını ve dış gözlemi ayrı doğrula |
| 9 | `schemas/super1_environment_v1.schema.json`, environment/identity scanner’ları | Schema allowlist ve scanner testleri: `PASS_CODE`; release denylist bu maddeden ayrıdır | `UNVERIFIED` | `NOT_APPLICABLE` — yerel schema/scanner kapısı | Current main’de schema ve workflow ref’lerini yeniden çalıştır |
| 10 | Numeric contracts ve risk guard | Financial numeric/rounding/finite-value testleri: `PASS_CODE` | `UNVERIFIED` | `NOT_APPLICABLE` — yerel deterministik kapı | Main’de aynı numeric fixture’larını doğrula |
| 11 | `scripts/windows_appcontainer_launcher.py`, `backtest/sandbox.py` | Risk worktree full JUnit: 739 passed; item-11 symlink JUnit: 1 passed; OS kanıtı risk ref’ine ait | `UNVERIFIED` — risk worktree `4810a732...` / tree `9fc0259b...`, main’e entegre değil | `UNVERIFIED` — platform/owner bağlamı current main için yok | Current main’de AppContainer/ACL/process lifecycle ve owner fixture’ını yeniden çalıştır |

## Dukascopy/promotion 12–19

Genel durum `ACTIVE_PREPARATION`. Yerel implementation/test kanıtı promotion
worktree’sine aittir; gerçek provider, owner ve residual-zero kabulü yoktur.
Madde bazlı tablo ve eksik girdiler:

[DUKASCOPY_PROMOTION_STATUS.md](DUKASCOPY_PROMOTION_STATUS.md)

- Kod/test durumu: yerel hedef testleri geçti (`PASS_CODE`).
- Main entegrasyonu: promotion implementation commit’i `main` içinde değildir;
  uncommitted delta taşınmamıştır.
- Dış kabul: `BLOCKED_EXTERNAL_INPUT`.
- Deployment/promotion: uygun değil; 113 hedefte mevcut audit residual `99`.
- Kanıt: promotion HEAD `1e911d96...` / tree `c80784d...`; source/JUnit
  SHA’ları ayrıntılı belgede.
- Sonraki işlem: provider/owner girdileri gelmeden indirme veya promotion
  başlatmamak.

## Broker-identity güvenliği

Durum `CLOSED_WITH_RETAINED_HISTORY` anlamına gelir: forward fix main’e merge
edilmiş, güncel tree temizdir; tarihsel bulgular korunmuştur.

- Kod/test durumu: PR #1 merge commit’i
  `e205e66a57ec5c54585c40d9e5ec36d179f4f7cf`.
- Main entegrasyonu: `MERGED_MAIN`.
- Dış kabul/CI: main `identity-full` ve `repository-audit` başarılı.
- Güncel main tree: denylist `0`, structural `0`, repo-tree violation `False`.
- Tarihsel kapsam: birleşik reachable history’de `170` eşleşme; geçmiş temiz
  ilan edilmez.
- Kanıt: [main current full](C:/Users/ISAAC/Documents/broker-identity-evidence-20260919-post-merge-e205e66/main-current-full.json),
  [main structure](C:/Users/ISAAC/Documents/broker-identity-evidence-20260919-post-merge-e205e66/main-structure.json),
  [repository audit](C:/Users/ISAAC/Documents/broker-identity-evidence-20260919-post-merge-e205e66/repository-audit.json).
- Sonraki işlem: sürekli CI taramasını koru; yeni aktif kimlik veya kapsam dışı
  hassas veri çıkarsa history politikasını yeniden değerlendir.

## Ortak deployment blokajı: DEPLOY-001

Release-integrity helper pin uyuşmazlığı ayrı bir deployment bağımlılığıdır;
dördüncü iş akışı değildir ve kapanmış broker-identity güvenlik hattını yeniden
açmaz. Güncel main’de:

- embedded pin: `4051f4e68b4aa575df2952a7205ac4fdbdecf6e3da4ca9e170d760e8d9d3dcfe`;
- Windows checkout helper: `011008a070c7723f285fc44370f821cfa7e58e8535d2ccdbd6e4ff360566f3ed`;
- Git LF blob helper: `bfa1fa7ddcc54bb172e1c33e399ba7d259d36b8baa69e66de41237879db0722c`;
- baseline ve güncel main aynı deployment test node’unda exit `1` verdi.

Teşhis: [RELEASE_INTEGRITY_DIAGNOSIS.md](RELEASE_INTEGRITY_DIAGNOSIS.md).
Kaynağı kanıtlanmamış hash için düzeltme yapılmayacak; release artifact kaynağı
bulunana kadar `DEPLOYMENT_READY` ilan edilmeyecek.

`DEPLOY-001` etkisi: hash mismatch normal akışta app/venv mutasyonu başlamadan
önce oluşur; fakat upgrader catch’i failure’ı aldıktan sonra koşulsuz
`Stop-Super1RuntimeForRollback` çağırır. Bu çağrı görev durdurmayı ve gerekirse
Python/terminal/runner süreçleri için `Stop-Process` denemesini içerebilir.
Gerçek sistemde durdurmanın gerçekleştiği iddia edilmez; etki yalnız hata
yönetiminin deneme yoludur ve ayrı iş akışı değildir.

## Kapanış ve raporlama kuralları

- `PASS_CODE`: yalnız yerel kod/test kapısı.
- `MERGED_MAIN`: ilgili kodun exact current main’de olduğu kanıtı.
- `PASS_EXTERNAL_ACCEPTANCE`: provider/owner/dış kabul kanıtı.
- `DEPLOYMENT_READY`: release-integrity dahil deployment kapılarının tamamı.

Bu statüler birbirinin yerine kullanılmaz. Eski teslim raporları yeniden
yazılmamış; tarihsel kayıt olarak bağlanmıştır. Her yeni teslim bu üç satırlık
tabloyu koruyacak ve yalnız değişen durumları/yeni kanıtları açıklayacaktır.

Son doğrulama zamanı: `2026-09-19T19:58:24Z`.
