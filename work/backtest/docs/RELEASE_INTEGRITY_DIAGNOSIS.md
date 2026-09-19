# Release-integrity teşhis raporu

Durum: `ROOT_CAUSE_PARTIALLY_CONFIRMED`

Bu rapor yalnızca teşhis ve güvenli yeniden üretim içerir. Release hash’i,
test beklentisi, production kodu, denylist, tarihsel baseline, GitHub secretı,
deployment ve gerçek MT5 çalıştırması değiştirilmemiştir.

## Kapsam ve kaynak SHA’ları

- Güncel `origin/main`: commit
  `e205e66a57ec5c54585c40d9e5ec36d179f4f7cf`, tree
  `452d347c513ca98dfeffa32b316b13b7659e765e`.
- Karşılaştırma başlangıcı: önceki başlangıç commit’i
  `023e39b288863ac93df9e7cfa1b7f218029f6458`, tree
  `452d347c513ca98dfeffa32b316b13b7659e765e`.
- `5ae500047b2067103539b0c72ad8f8f58024cbf2` ile
  `e205e66a57ec5c54585c40d9e5ec36d179f4f7cf` aynı tree’yi gösterir; bu
  yalnız merge içeriğinin değişmediğini gösterir, identity PR’ından önceki
  kökeni tek başına kanıtlamaz.
- Gözlenen embedded pin (`upgrade_super1_signed_app_windows.ps1:230`):
  `4051f4e68b4aa575df2952a7205ac4fdbdecf6e3da4ca9e170d760e8d9d3dcfe`.
- Her iki Windows checkout’unda `release_integrity.ps1` byte hash’i:
  `011008a070c7723f285fc44370f821cfa7e58e8535d2ccdbd6e4ff360566f3ed`.
- Her iki commit’in Git blob içeriği (LF, BOM’suz) SHA-256:
  `bfa1fa7ddcc54bb172e1c33e399ba7d259d36b8baa69e66de41237879db0722c`.
- Kullanıcı tarafından verilen aynı sürüm LF ölçümü: `bfa1fa7...`.
- Kullanıcı tarafından verilen önceki Windows checkout ölçümü: `011008a0...`.

Son iki helper ölçümü de `4051f4...` pinine eşleşmediği için CRLF/LF farkı tek
başına kök neden değildir.

## Beklenen pin ve karşılaştırılan byte içeriği

`upgrade_super1_signed_app_windows.ps1` içindeki
`$ExpectedIntegrityScriptSha256` sabiti (`:230`) seçilen
`release_integrity.ps1` dosyasının `Get-FileHash -Algorithm SHA256` sonucu ile
karşılaştırılır. Super1 akışındaki seçim (`:1870–1908`) şöyledir:

1. `$PSScriptRoot\release_integrity.ps1`.
2. `$App\deploy\release_integrity.ps1`.
3. Var olan adaylardan `Select-Object -First 1` ile ilk aday.
4. İlk adayın byte hash’i pinle karşılaştırılır; read lock alınır ve hash iki kez
   daha doğrulanır: lock alınırken ve dot-source sonrasında.

İlk aday mevcut fakat hash’i yanlışsa ikinci adaya geçilmez; işlem
`Super1 release integrity verifier hash mismatch.` ile durur. Bu seçim sırası,
gerçek uygulama dizinine dokunmayan geçici mock testinde de doğrulandı.

`upgrade_forward_shadow_windows.ps1` yalnızca korumalı release dizini altındaki
`$PSScriptRoot\release_integrity.ps1` dosyasını kullanır ve aynı `4051f4...`
pinini `:268` ve `:2790–2807` arasında doğrular. Super1
`install_*`/`bootstrap_*` yolları helper’ı `$PSScriptRoot` üzerinden source eder
ve imzalı arşiv/provenance doğrulaması yapar; bu özel embedded pin kontrolünü
kendileri uygulamaz. `stage_signed_upgrader_windows.ps1` imzalı manifestteki
helper/upgrader entry hash’lerini doğrular ve staged upgrader’ı çağırır; mismatch
sonucu staged upgrader çağrısında görünür.

## Geçmiş değişiklikleri

Main ancestry’sinde helper ve pin birlikte ilerlerken ilk ayrışma aşağıdaki
ölçümlerde görünür:

| Commit | Tarih / değişiklik | Embedded pin | Helper Git-blob SHA-256 (LF) | Aynı blob’un CRLF biçimi SHA-256 |
|---|---|---|---|---|
| `c2c716333252a950ecf0afb31fe4160660b88c84` | 2026-08-25, integrity pin güncellemesi | `1218c829...` | `1218c829...` | `ecce2ec5...` |
| `d004f3d3f5e63274c0fd1c7e175c78bde523e765` | 2026-08-26, staging gap’leri | `aa28a9fa...` | `aa28a9fa...` | `5e5e5ee7...` |
| `bb8257fa19eae83c5d170ec016a6de703c4d9980` | 2026-08-26, pretransfer contract | `74349cfd...` | `74349cfd...` | `fdab991f...` |
| `eebb1ae3a0963aaad9848b32fbf67946f031b46e` | 2026-08-27, v14 contract | `43a05fb9...` | `43a05fb9...` | `800ab9e5...` |
| `6e64ee1a660985c0771cf736b1c5512a6f85f801` | 2026-08-28, v15 audit contract | `caf6b244...` | `caf6b244...` | `d944842d...` |
| `96decc74b6dcbb28bfb8b86bb838db254dcb69cd` | 2026-08-30, V16 probe-control ACL | `d9f1aac0...` | `91da97be...` | `d9f1aac0...` |
| `023e39b288863ac93df9e7cfa1b7f218029f6458` | 2026-09-03, local order/release gates; current main ancestor | `4051f4e6...` | `bfa1fa7d...` | `011008a0...` |
| `e205e66a57ec5c54585c40d9e5ec36d179f4f7cf` | 2026-09-19 merge | `4051f4e6...` | `bfa1fa7d...` | `011008a0...` |

`60c5e4ef2698d3e5c7eafbd079b18f0e79a89a29` aynı 2026-09-03 değişikliğinin
paralel commit gösterimidir; `origin/main` first-parent yolu `023e39b...`
üzerinden gelir. Main dışındaki `2f3db875...` varyantı da kendi helper/pin
çiftini değiştirmiştir; güncel main kanıtı olarak kullanılmamıştır.

`96decc...` satırında embedded `d9f1aac0...` pininin, helper’ın CRLF biçimiyle
aynı olduğu doğrudan ölçülmüştür; Git blob’un LF hash’inin `91da97be...` olması
tek başına bozuk release sınıflandırması değildir. Güncel main için embedded
`4051f4...` değerine eşleşen byte artifact’i ayrıca kanıtlanmış değildir.

Beklenen `4051f4...` değerine byte içeriğiyle eşleşen bir reachable Git blob’u
veya incelenen yerel release artifact’i bulunamadı. Tüm reachable Git
blob’ları üzerinde 3.090 object tarandı, hedef SHA-256 için eşleşme sayısı `0`.
Bu nedenle pinin hangi dış artifact’ten üretildiği kanıtlanamamıştır; tahmini
replacement hash önerilmemiştir.

## Encoding, checkout ve paketleme etkisi

- `git ls-files --eol`: index `i/lf`, çalışma dosyası `w/crlf`.
- `core.autocrlf=true`; `.gitattributes`/`eol`/`text` için dosyaya özel kural
  yok.
- Git blob: 21.876 byte, 491 LF, BOM yok, SHA-256 `bfa1fa7d...`.
- Windows checkout: 22.367 byte, 491 CRLF, BOM yok, SHA-256 `011008a0...`.
- `build_signed_windows_release.ps1` staging alanına `Copy-Item` ile dosyaları
  alır, ZIP entry byte hash’lerini ve manifesti staging sonrasında üretir
  (`:206–214`, `:323–354`, `:420–457`). Satır sonu normalizasyonu uygulayan bir
  adım yoktur.
- Bu nedenle Windows checkout’tan üretilen artifact’in helper byte’ları,
  checkout/paketleme girdisine bağlıdır. `Get-FileHash` ise dot-source’dan
  önce ham dosya byte’larını ölçer.

## Hata çağrı yolu ve güvenlik etkisi

Bilinen hata: `Super1 release integrity verifier hash mismatch.`

Çağrı yolu: SHA-adresli korumalı upgrader → aday helper seçimi → ilk byte hash
karşılaştırması → mismatch. Normal akışta bu kontrol app/venv değişimi
başlamadan önce hata verir. Ancak dış catch bloğu (`:2384–2392`) failure’ı
aldıktan hemen sonra koşulsuz `Stop-Super1RuntimeForRollback` çağırır.

Hata koşulu, seçilen helper’ın byte içeriği embedded pinle aynı olmadığında
gerçekleşir. Yan etki, Super1 signed upgrade yolunun fail-closed biçimde
ilerlememesidir. Çalışan botun mevcut durumu hakkında bu repository testinden
sonuç çıkarılamaz; gerçek upgrader ve MT5 çalıştırılmamıştır.

Catch/rollback yolu (`:2384–2516`) önce runtime durdurma girişiminde bulunur;
`Stop-Super1RuntimeForRollback` görevleri durdurur, graceful beklemeyi dener,
gerekirse `Stop-Process` ile Python/terminal/runner süreçlerini sonlandırmayı
ve tekrar görev durdurmayı deneyebilir (`:724–770`). Sonrasında değişen
watchdog action/settings’i geri almayı, candidate/temp dosyalarını temizlemeyi,
app/venv arşivlerini geri taşımayı ve task’lerin durduğunu doğrulamayı dener.
Bu rapor gerçek sistemde durdurmanın gerçekleştiğini iddia etmez; yalnızca hata
yönetiminin bu yan etkiyi denemeye yetkili olduğunu kaydeder. Stop gate
başarısızsa rollback yapılmadığı raporlanır; rollback adımlarından biri
başarısızsa `rollback incomplete` ile fail eder. Bu etki `DEPLOY-001` altında
tutulur; ayrı iş akışı açılmaz.

## Aynı ortamda güvenli yeniden üretim

Önceki başlangıç commit’i `023e39b...` ve güncel main aynı
Python/pytest/PowerShell ortamında aynı node ve aynı kurulumla çalıştırıldı.
Collection aşamasında oluşan önceki bir hata, test gövdesindeki hatanın
önceden var olduğunu tek başına kanıtlamaz; aşağıdaki iki ham çalıştırma test
gövdesinin karşılaştırmalı kanıtıdır:

```text
python -m pytest -q work/backtest/tests/test_deployment_security.py::test_super1_upgrade_pins_and_read_locks_the_release_trust_helper
```

Ortam: Python `3.14.5`, pytest `9.1.1`, Windows PowerShell `5.1.26100.9444`,
pwsh `7.6.5`. Her iki sürümde de beklenen sonuç pinin güncel helper hash’ini
içermesiydi; gerçek sonuç aynı mismatch ve exit code `1` oldu.

| Sürüm | Commit / tree | Gerçek helper hash | Embedded pin | Sonuç |
|---|---|---|---|---|
| Önceki başlangıç | `023e39b...` / `452d347...` | `011008a0...` | `4051f4e6...` | 1 failed, exit `1` |
| Güncel main | `e205e66...` / `452d347...` | `011008a0...` | `4051f4e6...` | 1 failed, exit `1` |

Ham kanıtlar repo dışındadır; denylist veya secret içermemektedir:

- `C:\Users\ISAAC\Documents\release-integrity-evidence-20260919-v2\environment.txt` — aynı bağımlılık ortamı, 389 byte, SHA-256 `6AAB4E2E4040BA5CD18FB27E0A5BAC5BAF376844B80CAEFBBFFA726E7D65D3C9`.
- `...\baseline-023e39b.pytest.txt` — node `work/backtest/tests/test_deployment_security.py::test_super1_upgrade_pins_and_read_locks_the_release_trust_helper`, 1.355 byte, SHA-256 `BF8238FE2A71F32ED0DBB6D44D71832D4C3FF71F3CBDFD6F495DB129B1599A9B`.
- `...\main-e205e66.pytest.txt` — aynı node, 1.355 byte, SHA-256 `6769D5971D3C5503103219C4E6394A4B741F15402FD9B54F13612E5FD6001C6C`.
- Her iki ham çıktı aynı beklenen/gerçek ayrımını içerir: beklenen pinli sabit yok, gerçek byte hash `011008a0...`; gerçek sonuç `1 failed`, exit `1`.
- `...\candidate-selection-mock-v2.txt` — exit `0`, 1.068 byte, SHA-256 `2345B3BF4DE72E3418799DEAA674C88848A63A478559F67434ED534BC74362AE`.
- `C:\Users\ISAAC\Documents\release-integrity-evidence-20260919\candidate-selection-mock.ps1` — düzeltilmiş harness, SHA-256 `DD4B230F916F38951978B6B0C4A6F6C736240FF41D2476B8B65F124D38D75610`.

Mock kontrolünde `$PSScriptRoot` adayı seçildi, ilk aday mismatch olduğunda
fallback yapılmadı, ilk aday yokken `$App\deploy` adayı seçildi. Ayrıca gerçek
upgrader catch marker’ı (`failure` yakalama sonrası
`Stop-Super1RuntimeForRollback`) kaynakta doğrulandı; graceful ve forced stop
çağrı sıraları tamamen mock’landı ve görev/süreç/dosya işlemleri gerçeklenmedi.
`unexpected pass` yalnız beklenen hash-mismatch hatasıyla PASS sayılır; farklı
hata veya hatasız dönüş nonzero olur. Bu harness seçim mantığı ile catch
sözleşmesini doğrular; tam upgrader çalıştırmasının kanıtı değildir. Gerçek
upgrader, scheduled task ve MT5 kullanılmadı.

## Tek düzeltme önerisi — henüz uygulanmayacak

Önce `4051f4...` pininin karşılığı olan yetkili kaynak veya imzalı release
artifact’i bulunmalıdır. Bu girdi olmadan yeni hash seçmek güvenli değildir.
Kaynak bulunduğunda önerilen sıra:

1. Helper byte’larını (encoding, BOM ve LF/CRLF dahil) kaynak artifact’te
   dondur.
2. Aynı byte’ları staging’e kopyala ve helper SHA-256’yı staging byte’larından
   üret.
3. Upgrader pinini bu doğrulanmış artifact kaynağıyla üret; source, archive,
   manifest entry ve staged target hash’lerini aynı sırada yeniden doğrula.
4. `test_super1_upgrade_pins_and_read_locks_the_release_trust_helper`, V16
   deployment contract, signed manifest/artifact gate ve iki upgrade yolunun
   static/mock kontrol akışlarını çalıştır.
5. Dış owner/release kabulü olmadan `DEPLOYMENT_READY` ilan etme.

Bu öneri uygulanmamıştır; test skip/xfail edilmemiş ve gözlenen `011008a0...`
hash’i körlemesine pine yazılmamıştır.

## Üç iş akışı için ortak durum tablosu

| İş akışı | Genel durum | Uygulama sorumlusu | Karar sahibi |
|---|---|---|---|
| Risk-hardening 1–11 | `REVALIDATION_REQUIRED` | Proje mühendisi | Proje mimarı |
| Dukascopy/promotion 12–19 | `ACTIVE_PREPARATION` | Proje mühendisi | Proje mimarı; dış girdilerde proje sahibi |
| Broker-identity güvenliği | `CLOSED_WITH_RETAINED_HISTORY` | Proje mühendisi; sürekli tarama CI | Proje mimarı |

Son doğrulama zamanı: `2026-09-19T19:05:59Z`.
