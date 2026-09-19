# Broker-identity Git geçmişi politikası

Durum: `HISTORY_RETAINED_KNOWN_INACTIVE_IDENTIFIERS`

Bu belge mimari politika kararıdır. Bilinen iki hesap kimliğinin inaktif olduğu
kullanıcı beyanı esas alınarak Git geçmişi korunur. Bu karar ayrıca verilmemiş
bir kullanıcı onayı veya tarih üretmez.

## Bağlayıcı kararlar

- Git history rewrite yapılmayacak.
- Force-push yapılmayacak.
- Branch veya tag silinmeyecek.
- Tarihsel eşleşmeler silinmeyecek.
- Otomatik baseline genişletme yapılmayacak.
- Yeni güncel-tree bulguları veya yeni tarihsel bulgular baseline tarafından
  muaf tutulmayacak.
- Yeni aktif kimlik bilgisi, kapsam dışı hassas veri veya proje sahibinin açık
  politika değişikliği talebi ortaya çıkarsa karar yeniden değerlendirilecek;
  mühendis kendiliğinden rewrite başlatamayacak.

## Güncel kanıt

Merge sonrası `main`:

- commit: `e205e66a57ec5c54585c40d9e5ec36d179f4f7cf`
- tree: `452d347c513ca98dfeffa32b316b13b7659e765e`
- güncel aday tree denylist eşleşmesi: `0`
- güncel yapısal ihlal: `0`
- repository-audit repo-tree ihlali: `False`
- birleşik reachable history denylist eşleşmesi: `170`

`170`, birleşik geçmiş kapsamının sayısıdır; yalnızca `main` sayısı değildir.
`origin/HEAD`, aynı `main` commit’ini gösteren sembolik takma addır ve ayrı bir
sızıntı kaynağı olarak sayılmaz.

Güncel dış raporlar:

- [main current full](C:/Users/ISAAC/Documents/broker-identity-evidence-20260919-post-merge-e205e66/main-current-full.json)
- [main structure](C:/Users/ISAAC/Documents/broker-identity-evidence-20260919-post-merge-e205e66/main-structure.json)
- [repository audit](C:/Users/ISAAC/Documents/broker-identity-evidence-20260919-post-merge-e205e66/repository-audit.json)

## Baseline kapsamı

Baseline yalnızca kayıtlı tarihsel Git nesnelerini ve onların ölçülen
bulgularını kapsar. Güncel branch tree’lerini, yeni object’leri veya yeni
denylist eşleşmelerini muaf tutmaz. Güncel tree temizliği ile tarihsel
koruma farklı kapılardır:

- Güncel `main` temizliği tamamlandı.
- Tarihsel eşleşmeler silinmedi.
- Sürekli scan CI yürürlüktedir.
- Tarihsel bulgular raporlanmaya devam eder; geçmiş temiz ilan edilmez.

Broker-identity PR’ı normal merge ile `main`’e alındı; bu politika merge sonrası
current-tree sonucunu tarihsel koruma kararıyla karıştırmaz.

## Üç iş akışı için ortak durum tablosu

| İş akışı | Genel durum | Uygulama sorumlusu | Karar sahibi |
|---|---|---|---|
| Risk-hardening 1–11 | `REVALIDATION_REQUIRED` | Proje mühendisi | Proje mimarı |
| Dukascopy/promotion 12–19 | `ACTIVE_PREPARATION` | Proje mühendisi | Proje mimarı; dış girdilerde proje sahibi |
| Broker-identity güvenliği | `CLOSED_WITH_RETAINED_HISTORY` | Proje mühendisi; sürekli tarama CI | Proje mimarı |

`PASS_CODE`, `MERGED_MAIN`, `PASS_EXTERNAL_ACCEPTANCE` ve
`DEPLOYMENT_READY` birbirinin yerine kullanılmaz. Bu belge `MERGED_MAIN` ve
current-tree scan kanıtını kaydeder; history rewrite veya dış kabul tamamlandı
iddiası taşımaz.

Son doğrulama zamanı: `2026-09-19T19:05:59Z`.
