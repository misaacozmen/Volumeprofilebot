# Proje iş akışları güncel durum kaydı

Kaynak worktree: `codex/workstream-status-release-audit`.

- Başlangıç commit: `e205e66a57ec5c54585c40d9e5ec36d179f4f7cf`.
- Başlangıç tree: `452d347c513ca98dfeffa32b316b13b7659e765e`.
- Bu tur yalnızca dokümantasyon ve repo dışı kanıt üretir.
- Risk/promotion worktree’lerindeki değişiklikler bu branch’e taşınmamıştır.
- Üretim kodu, release hash’i, test beklentisi, denylist, tarihsel baseline ve
  GitHub secret değiştirilmemiştir.

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

## Kapanış ve raporlama kuralları

- `PASS_CODE`: yalnız yerel kod/test kapısı.
- `MERGED_MAIN`: ilgili kodun exact current main’de olduğu kanıtı.
- `PASS_EXTERNAL_ACCEPTANCE`: provider/owner/dış kabul kanıtı.
- `DEPLOYMENT_READY`: release-integrity dahil deployment kapılarının tamamı.

Bu statüler birbirinin yerine kullanılmaz. Eski teslim raporları yeniden
yazılmamış; tarihsel kayıt olarak bağlanmıştır. Her yeni teslim bu üç satırlık
tabloyu koruyacak ve yalnız değişen durumları/yeni kanıtları açıklayacaktır.

Son doğrulama zamanı: `2026-09-19T18:04:38Z`.
