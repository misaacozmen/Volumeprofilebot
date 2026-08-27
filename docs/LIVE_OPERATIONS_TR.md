# Canlı Operasyonlar ve Rollout Prosedürü (Windows / Super1) v14

> **ÖNEMLİ:** Bu belge, Windows ve Super1 üretim ortamı için **tek yetkili operasyonel runbook**'tur. Burada belirtilen sıra dışındaki hiçbir manuel hotfix, doğrudan betik kopyalama veya geçersiz prosedür kabul edilmez.

---

## 1. Temel Kurallar ve Güvenlik Sözleşmesi

* **live_enabled=false**: Demo ortamında gerçek bakiye riski alınamaz.
* **Şifreli Kimlik Yönetimi**: MT5 parolası hiçbir zaman düz metin saklanmaz veya `.env` dosyasına yazılmaz. Yalnızca DPAPI ve güvenli launcher üzerinden enjekte edilir.
* **Çevre Değişkenleri**: Runtime tarafından `.env` dosyası **otomatik yüklenmez**. Gerçek değişken isimleri şunlardır:
  - `XM_MT5_SERVER`
  - `XM_MT5_READ_ONLY_PASSWORD`
  - `XM_MT5_TERMINAL_PATH`
* **Eski Betikler (LEGACY — DO NOT USE)**:
  - `deploy/install_super1_windows.ps1` (LEGACY)
  - `deploy/finalize_super1_fresh_windows.ps1` (LEGACY)
  - `deploy/repair_super1_task_s4u_windows.ps1` (LEGACY)
  Bu betikler asla doğrudan çalıştırılmamalıdır.

---

## 2. Tek Desteklenen Rollout Sırası

Rollout işlemi yalnızca aşağıdaki fail-closed sıra ile gerçekleştirilir:

```
[1. Signed Staging] ➔ [2. Signed Upgrade] ➔ [3. Flat Check] ➔ [4. Sealed Rollover] ➔ [5. Demo Smoke]
```

### Tek Seferlik Private S3 Transferi (v14)

Normatif makine sözleşmesi: `docs/SUPER1_PRIVATE_S3_TRANSFER_V14.json`.

Transfer yalnız `eu-central-1` bölgesinde, `otobacktest-transfer-<12_DIGIT_ACCOUNT_ID>-<V14_SHA12>` adlı geçici private bucket üzerinden yapılır. BucketOwnerEnforced, ACL disabled, Block Public Access dört ayarı açık, versioning disabled ve varsayılan şifreleme SSE-S3 olmalıdır. Bucket policy, public ACL, public URL ve static website yasaktır. Bucket içinde yalnız tek v14 transfer bundle nesnesi bulunabilir.

Presigned GET ve evidence için presigned PUT süresi 900 saniyedir. GET URL bearer secret’tır; checkpoint, log veya rapora yazılmaz. Remote URL `Read-Host` ile alınır; `PSReadLine` kaldırılır, işlem bitince URL değişkeni ve clipboard temizlenir.

Remote tarafında bundle dış SHA256, tam beş üye, duplicate/traversal ve beş iç SHA doğrulanmadan açma veya deployment yapılmaz. Incoming dizini inheritance kapalı, reparse’siz, SYSTEM/Administrators FullControl ve doğrulama sonrası ReadOnly olmalıdır. Doğrulama bitince download object silinir; boş private bucket evidence dönüşü için tutulur. Redacted evidence ZIP credential, DPAPI, `.env`, token, parola veya terminal profil dosyası içeremez. Evidence yerelde doğrulandıktan sonra evidence object ve bucket silinir.

Kullanılabilecek blocker kodları: `WAITING_USER_AWS_LOGIN`, `BLOCKED_S3_TRANSFER_PERMISSION`, `BLOCKED_RDP_TEXT_CLIPBOARD`, `BLOCKED_TRANSFER_HASH_MISMATCH`, `BLOCKED_EVIDENCE_RETURN`.

Bu bucket public website olarak yapılandırılamaz. RDP drive redirection kullanılmaz.

AWS API readback doğrulanmadan upload veya presigned URL oluşturulmaz. ZIP yollarında case-insensitive duplicate, rooted, drive-qualified, backslash ve traversal reddedilir. Remote extraction CreateNew ve containment kontrolüyle yapılır; overwrite yasaktır. Windows dizin ReadOnly biti güvenlik kapısı değildir. Incoming root ve çıkarılan her dosya SYSTEM owner, yalnız SYSTEM/Administrators explicit FullControl, inheritance kapalı ve reparse’siz olmalıdır; doğrulama sonrası dosyalar ReadOnly yapılır. URL ve clipboard temizliği try/finally içindedir. Download object remote hash doğrulamasının hemen ardından silinir. Evidence PUT benzersiz ve önceden bulunmayan key kullanır ve SHA256 checksum’a bağlanır. Evidence yerelde doğrulanmadan object veya bucket temizlenmez; sonunda evidence object ve bucket yokluğu API readback ile doğrulanır.

### Adım 1: Signed Staging (`stage_signed_upgrader_windows.ps1`)
Upgrader betiği, imzalı release bütünlüğü doğrulandıktan sonra SHA-256 adresli izole konuma kopyalanır:
```powershell
& C:\Super1\incoming\<RELEASE_ID>\stage_signed_upgrader_windows.ps1 `
    -Archive C:\Super1\incoming\<RELEASE_ID>\<RELEASE_ID>.zip `
    -ExpectedPythonSha256 <PYTHON_SHA256> `
    -ExpectedTerminalSha256 <TERMINAL_SHA256> `
    -BootstrapIntegrityScript C:\Super1\incoming\<RELEASE_ID>\release_integrity.ps1 `
    -ExpectedBootstrapIntegritySha256 <INTEGRITY_SHA256>
```
* Upgrader `C:\Program Files\OtoBacktestDeploy\super1-<SHA256>\` altına alınır.
* ACL mirası kapatılır; yalnızca SYSTEM ve Administrators FullControl yetkisiyle kilitlenir.

### Adım 2: Signed Upgrade (`upgrade_super1_signed_app_windows.ps1`)
Yalnızca kilitli ve izole staged upgrader üzerinden yürütülür:
* Uygulama dosyaları atomik olarak değiştirilir.
* İmzalı manifest ve offline wheelhouse kilitleri doğrulanır.
* Görevler `UPGRADED_STOPPED` durumunda bekletilir.

### Adım 3: Flat Check (`check_super1_flat_windows.ps1`)
Uygulama başlatılmadan önce pozisyon, emir ve yetki durumu mühürlenir:
```powershell
$flatRaw = & C:\Super1\app\deploy\check_super1_flat_windows.ps1 -KeepStopped
$flat = $flatRaw | ConvertFrom-Json
```
* Yalnızca `READY_FLAT_SEALED` çıktısı alındığında sonraki adıma geçilebilir.

### Adım 4: Sealed Rollover (`rollover_super1_campaign_windows.ps1`)
Flat-readiness kanıtı sağlandıktan sonra kampanya rollover yapılır:
```powershell
& C:\Super1\app\deploy\rollover_super1_campaign_windows.ps1 `
    -ReadinessEvidence $flat.readiness_evidence `
    -ExpectedReadinessSha256 $flat.readiness_sha256
```
* Eski kampanya verisi silinmez; güvenli arşive taşınır.
* Yeni temiz kampanya başlatılır ve tasklar aktifleştirilir.

### Adım 5: Demo Smoke Order Testi
Süreçler başladıktan sonra broker emir iletimi doğrulanır:
```powershell
& C:\Super1\app\deploy\run_super1_demo_smoke_windows.ps1 -ConfirmDemo
```
* Minimum hacimli demo limit/stop emri iletilir.
* Broker readback doğrulanır ve anında iptal edilir.
* Kabul: `PASS` + `cancelled.state=CANCELLED` + bütün `after` exposure alanları 0.

---

## 3. İzleme ve Kabul Kapıları

Her güncel seans için zorunlu kontroller:
1. `Super1XM` ve `Super1Watchdog` görevleri `Running` ve `LastTaskResult = 0`.
2. Heartbeat gecikmesi $\le 120$ saniye.
3. `fatal_latch.json` ve `launcher_failure.json` bulunmamalı.
4. Günlük sağlık raporu (`daily-health`):
   ```powershell
   & C:\Super1\venv311\Scripts\python.exe C:\Super1\app\scripts\run_super1_xm_mt5_forward.py --output-root C:\Super1\state daily-health
   ```
5. Promosyon değerlendirmesi için:
   * En az 30 takvim günü
   * En az 30 geçerli seans
   * En az 20 puanlanabilir karar tamamlanmalıdır.
