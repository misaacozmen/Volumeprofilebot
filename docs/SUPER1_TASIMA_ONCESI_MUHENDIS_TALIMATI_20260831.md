**Super1 taşıma öncesi mühendis talimatı — 31 Ağustos 2026**

Bu talimatın mevcut kararı **NO_GO**. Amaç yalnız Super1'in mevcut stratejisini koruyarak zamanında ve güvenli emir yürütmesini kanıtlamak, ardından tek adayın sunucu geçişini hazırlamaktır. Strateji geliştirme, performans optimizasyonu veya gerçek para hesabına geçiş görevi değildir.

İncelenen repo: `C:\Users\ISAAC\Documents\otobacktestprojesi`; başlangıç commit'i `9116e69104916a62c179248f1d6080fcb4f33afb`. Mimari inceleme salt okunur yapıldı; bu talimat dışında dosya değiştirilmedi, test/sunucu bağlantısı/broker çağrısı/kurulum çalıştırılmadı. Sekiz dosyanın mevcut sözleşmedeki SHA256 bağları yerelde eşleşti; bu kontrol davranış doğruluğu veya sunucudaki sürüm için kanıt değildir.

1. **Yetki sınırını uygula.**

   Şimdi yalnız yerel kod düzeltmesi, emirsiz testler ve rapor hazırlama yetkilidir. Aşağıdaki maddeleri sırayla uygula. Testler geçene kadar gerekli hedefli düzeltmeleri tamamla; kapsam dışındaki davranış kararlarını verme.

   Kaynak/hedef sunucuya bağlanma; kurulum, transfer, AWS/RDP işlemi, görev başlatma/durdurma, broker bağlantısı, demo/canlı emir veya dışarıya bildirim yapma. `daemon`, `init`, `rollover`, `smoke-order` ve deployment betiklerini gerçek ortamda çalıştırma. Paket imzalama/yayımlama, credential üretme, kampanya sıfırlama ve fatal latch silme yetkisi yoktur. Yerel testlerde broker/işletim sistemi yan etkileri sahte sınır nesneleri ve geçici dizinlerle karşılanmalıdır; gerçek MT5 import/init ve ağ erişimine düşen test başarısız olmalıdır.

   Özellikle [stage_signed_upgrader_windows.ps1:178](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/deploy/stage_signed_upgrader_windows.ps1:178) yalnız dosya hazırlamaz, upgrader'ı da çalıştırır. İnceleme veya doğrulama komutu diye çalıştırma. İkinci adayın kodunu, görevini, hesabını, terminalini veya state'ini operasyonel olarak değiştirme. Ortak modül düzeltmeleri için mevcut diğer-aday regresyonlarını da çalıştır.

2. **Mevcut Super1 davranışını dondur.**

   Kaynaklar: [runtime yapılandırması](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/live_forward/super1_xm_mt5_demo_config.json), [sinyal sözleşmesi](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/research_candidates/super1/super1_signal_contract.json), [manifest](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/research_candidates/super1/super1_manifest.json), [temel strateji](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/forward_shadow/frozen_config.json).

   | Korunacak özellik | Mevcut değer |
   | --- | --- |
   | Aday / yürütme | Super1; XM MT5 demo; `FROZEN_CANONICAL_PAIR_PIPELINE_WITH_SUPER1_OVERLAY` |
   | NQ bacağı | `US100Cash`, 3 dakika, New York 09:30–10:30, 3R, günlük en çok 1 işlem |
   | SPX bacağı | `US500Cash`, 5 dakika, New York 09:30–11:00, 2.5R, günlük en çok 2 işlem |
   | Zaman | `America/New_York`; kapanmış bar gecikmesi 8 saniye; döngü beklemesi 30 saniye |
   | Risk | Baz %1; son 10 terminal sonucun durumuna göre mevcut 0.75 / 1.1 ölçeği; `pair_cap_r=-1.0` |
   | Filtreler | Pazartesi short BLOCK; `vah_val_proximity` ve overnight up birleşimi BLOCK |
   | Kanıt durumu | `UNPROVEN`; geçmiş araştırma sonuçları çalışan pipeline için kanıt sayılmıyor; gerçek para yasak |

   Tek aday taşınması iki bacaktan birinin silinmesi değildir. Hesap kimliği, magic, semboller, fiyat/veri kaynağı, filtreler, giriş yöntemi, risk, TP/SL, saatler ve eşikler değişmeyecek. Testleri geçirmek için tüm sinyalleri bloke etme; geçerli pozitif yol da çalışmalıdır. `proven` veya tarihsel parity bayraklarını bu görevle true yapma.

3. **Filtre eşleşmesindeki kesin çalışma hatasını düzelt.**

   [run_super1_xm_mt5_forward.py:467](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_super1_xm_mt5_forward.py:467) helper'ının `state` parametresi, [satır 479](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_super1_xm_mt5_forward.py:479) çağrısında hem positional hem `filter_state["state"]` üzerinden veriliyor. BLOCK koşulunda `TypeError` oluşur; normal filtre sonucu yerine daemon fatal duruşa gidebilir.

   `filter_state` sözlüğünü helper'a açma. Filtre ayrıntılarını ayrı `filter_state` alanında taşı; üst düzey sonuç tam olarak `SUPER1_FILTER_BLOCKED` kalsın. Mevcut kendi pending emirlerini iptal/teyit davranışını koru; başka adayın emrine veya açık pozisyona dokunma. Feature kanıtı eksikse mevcut `SUPER1_FILTER_UNRESOLVED_NO_SEND` davranışını koru.

   Regresyon: gerçek Super1 `_place_candidate` yolunda iki BLOCK kuralı, UNRESOLVED ve ALLOW çalıştırılsın. BLOCK/UNRESOLVED için yeni emir sayısı 0 ve exception/fatal latch yok; ALLOW için 1; tekrarda toplam yine 1. Mevcut pending varsa iptal broker geri okumasıyla doğrulansın; iptal teyit edilemiyorsa başarı yazılmasın. Yalnız `_filter_state` test etmek yeterli değildir.

4. **Gönderme anındaki zaman ve veri geçerliliğini kontrol et.**

   [run_capital_forward.py:1261](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_capital_forward.py:1261) döngü başında aldığı zamanı veri çekimi/hesaplama sonrasında da kullanıyor. [run_xm_mt5_forward.py:1334](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_xm_mt5_forward.py:1334) yalnız prefix cutoff'unu kontrol ediyor. Bu nedenle pencere içinde başlayan uzun döngü bitişten sonra yeni emir isteyebilir.

   Yeni pending emrin gerçek SDK gönderiminden hemen önce güncel UTC zamanı al; New York işlem tarihi ve ilgili bacağın `[başlangıç, bitiş)` aralığını yeniden doğrula. Expiration gelecekte ve mevcut mühürlü bacak bitişinde olmalı. Güncel kesit vektörünü her bacak için mevcut `run_prefix` gibi `min(closed_cutoff(now, timeframe, 8), frozen_end)` ile hesapla ve prefix'in iki cutoff'uyla karşılaştır. NQ bitişinden sonra onun kesiti 10:30'da sabit kalmalı; bu durum SPX'in 11:00'e kadar çalışmasını engellememeli. Kullanılan prefix eskiyse gönderme, yeni veriyle yeni döngüde hesapla. Strateji sinyalini güncelmiş gibi damgalama.

   Süre bitmişse `WINDOW_EXPIRED_NO_SEND`, kesit eskimişse `STALE_PREFIX_NO_SEND` kaydet; send=0 olsun ve ilgili kendi pending emirlerini güvenli iptal yolundan geçir. Beklenen pencere kapanışı fatal hata değildir. Bu yeni-emir kontrolü güvenli iptalleri engellememelidir. Terminal/broker saatiyle host saatini doğrudan eşitleyen bir varsayım ekleme; UTC/NY dönüşümünü ve mevcut quote doğrulamasını koru.

   SDK'nın hiç çağrılmadığı kesin olan stale ertelemede intent'i `CHECK_PASSED` veya gönderim-belirsiz durumunda kilitleme. Ayrı kalıcı `PRE_SEND_DEFERRED` durumu kullan; taze prefix ve yeniden alınmış atomik tek-gönderim hakkıyla aynı `order_id` yeniden değerlendirilebilsin. SDK gönderimi başlamış veya başlayıp başlamadığı belirsizse bu retryable duruma geri çevirme; broker uzlaştırması zorunlu kalsın. Regresyon: ilk döngü stale/send=0, ikinci döngü taze/send=1, sonraki tekrarlar toplam send=1; crash ve paralel süreçte de bu sınır korunsun.

   Kanıta bar kapanışı, cutoff, veri alınışı, karar üretimi, gönderim başlangıcı, cevap ve broker teyidi zamanlarını ekle. 8 saniye + 30 saniyelik bekleme dışında hesaplama/API süreleri de bulunduğunu raporla; tam saniyede gönderim vaadi verme. Piyasa açılışı tek başına sinyal değildir.

5. **Eksik önceki seans verisinden filtre kararı üretme.**

   [run_super1_xm_mt5_forward.py:272](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_super1_xm_mt5_forward.py:272) elde bulunan önceki herhangi bir seansın son barını kapanış sayıyor. Eksik 15:59 yerine 15:58 veya eksik gün yerine daha eski gün kullanılabilir.

   Bugünün 09:30 açılışının yanında önceki beklenen RTH seansı ve gerçek kapanış kesiti de kanıtlanmış olmalı. Normal seansta 15:59 barının kapanışını doğrula. Hafta sonu/tatil/erken kapanış için yalnız doğrulanmış seans takvimini kullan; mevcut XM günlük işlem arası takvimini RTH takvimi yerine kullanma. Kanıt veya takvim yoksa önceki rastgele bara düşme; `SUPER1_FILTER_UNRESOLVED_NO_SEND` üret. Bu kontrol yalnız ilgili feature gerektiğinde uygulansın; eşleşmeyen önceki filtre koşulları yüzünden gereksiz veri zorunluluğu yaratma.

   Testler: eksik kapanış barı, eksik bütün önceki seans, sıra dışı/tekrarlı barlar, normal hafta sonu, belgeli tatil/erken kapanış ve kanıtsız takvim. Mevcut tam/valid seans pozitif fixture'ı aynı yönü ve aynı ALLOW/BLOCK kararını vermeli. Takvim kaynağı mevcut değilse yeni bağımlılık veya servis seçme; veri ihtiyacını raporda açık blocker yap.

6. **Gönderim kaydı ile broker yürütme kanıtını ayır.**

   [run_xm_mt5_forward.py:1113](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_xm_mt5_forward.py:1113) PLACED/DONE cevabını doğrudan SUBMITTED kaydediyor. [Satır 902](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_xm_mt5_forward.py:902) önceki SUBMITTED kaydında broker karşılığı görünmese de erken dönüyor. Mevcut [deal günlüğü:599](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_xm_mt5_forward.py:599) ve SL/TP raporlaması, korunma ve tamamlanma denetiminin yerine geçmez.

   Mevcut SQLite intent, benzersiz emir kimliği, outbox ve mükerrer gönderim engelini koru. SUBMITTED gönderim cevabının kaydı olarak kalabilir; ayrıca kalıcı broker yürütme durumu ve dayanak ticket'ları tut. Cevap alındı fakat teyit yok, pending teyitli, kısmi dolum, tam dolum, iptal, süre dolumu, ret, pozisyon kapanışı ve bilinmeyen durum birbirine karışmasın. Bu durumları mevcut günlüğe ekle; eski kayıtları silen veya otomatik sıfırlayan şema geçişi yapma.

   Her gönderim/restart/izleme döngüsünde account kimliği → strategy order_id → broker order ticket → deal ticket → position_id zincirini orders/positions/history geri okumalarıyla uzlaştır. Bilinen ticket'ı birincil eşleştirme olarak kullan; salt comment eşitliğine veya tarihçede herhangi bir nesnenin bulunmasına dayanarak açık/başarılı işlem sayma. Tarihçe order state'ini de yorumla.

   Pending teyidinde symbol, yön/tür, hacim, tick'e hizalı entry/SL/TP ve expiration onaylı istekle eşleşmeli. Dolumda gerçekleşen hacim/fiyatı ayrı kaydet; pending fiyatıyla fill fiyatının zorunlu eşitliğini varsayma. Kısmi dolumu tam dolum sayma veya kalanı otomatik yeniden gönderme. Açık pozisyonun broker tarafındaki SL/TP'sini teyit et; eksik/uyuşmaz korunmada yeni emirleri bloke edip alarm durumunu kaydet. Broker erişilebildiği sürece mevcut exposure için geri okuma/izleme sürsün. Otomatik market emri, SL genişletme, hacim artırma, farklı fill politikası veya pozisyon kapatma ekleme.

   `None`, bağlantı/timeout, gecikmeli history ve broker koleksiyonlarının okunamaması kesin ret veya flat değildir. Belirsizlikte `UNKNOWN_NO_SEND`; kalıcı intent korunsun, tekrar gönderim yapılmasın, uzlaştırma sürdürülsün. Definitif broker ret kodlarını ayrı kaydet; broker reddini gizleme. PLACED/DONE yalnız cevap/kabul anlamında yorumlansın, dolum kanıtı sayılmasın. [MetaQuotes order_check açıklaması](https://www.mql5.com/en/docs/python_metatrader5/mt5ordercheck_py) ve [sunucu dönüş kodları](https://www.mql5.com/en/docs/constants/errorswarnings/enum_trade_return_codes) bu ayrımı destekler.

   Mevcut risk hesabı [Super1:279](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_super1_xm_mt5_forward.py:279) belirsiz/çoklu terminal sonuçlarını reddediyor. Bu korumayı kaldırma; kısmi/manuel kapanışı tam TP/SL gibi puanlama. Yeni risk kuralı tasarlama.

7. **Rollover hata yolunda eski state ile otomatik yeniden başlamayı engelle.**

   [rollover_super1_campaign_windows.ps1:584](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/deploy/rollover_super1_campaign_windows.ps1:584) son sağlık kabulünden önce daemon'u başlatıyor. Sonraki hata yolunda [satır 649](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/deploy/rollover_super1_campaign_windows.ps1:649) eski state'i geri getiriyor; [satır 667](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/deploy/rollover_super1_campaign_windows.ps1:667) görevleri yeniden başlatıyor. Arada broker yan etkisi oluşmuş olabilir.

   Yerel betiği/testini düzelt: emir üretebilen runtime başlatma girişiminden itibaren hata oluşursa broker yan etkisini mümkün kabul et. Bu durumda eski state'i aktif state üzerine geri koyma ve hiçbir görevi otomatik başlatma. Yeni/eski state, intent/journal ve hata kanıtları ayrı ve eksiksiz korunsun; durdurma teyidi alınamıyorsa STOPPED iddiası yazılmasın. Yeniden çalıştırma ancak sonraki onaylı broker uzlaştırmasıyla mümkün olsun. Broker yan etkisi sınırından önceki güvenli rollback davranışını koru.

   Offline harness: runtime başlatma girişiminden sonra watchdog kabulünü hata verdir; eski journal ile restart=0, state kaybı=0 ve ikinci gönderim=0 doğrulansın. Gerçek görev veya sunucu kullanılmasın.

8. **Mevcut testleri genişlet; sadece mock başarıları üretme.**

   Asıl entegrasyon testleri [test_super1_xm_forward.py](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/tests/test_super1_xm_forward.py) ve [test_xm_mt5_forward.py](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/tests/test_xm_mt5_forward.py) içine eklensin. Ortak döngü ve rollback değişirse ilgili mevcut Capital/deployment contract testleri genişletilsin. Yeni framework ekleme.

   | Zorunlu senaryo | Kesin kabul |
   | --- | --- |
   | Gerçek Super1 overlay → risk → request → broker sınırı | Filtre/_place_candidate/_pending_request mock edilmez; gerçek override yürür. Broker ve kontrollü saat/veri sınırı fixture olabilir. |
   | 2 bacak × long/short × 0.75/1.1 risk ölçeği | Gerçek request; step/min/max hacim, bütçeyi aşmayan nominal risk, tick-size, SL/TP ve expiration doğrulanır. |
   | BLOCK / UNRESOLVED / geçerli ALLOW | Sırasıyla 0/0/1 yeni emir; tekrar çalıştırma sayıyı artırmaz. |
   | Pencere öncesi/tam başlangıcı/tam bitişi/sonrası; hesaplama sırasında bitişin aşılması | Yasak/eskimiş yeni emir=0; geçerli güncel sinyal doğru bacakta çalışır. NQ bitmişken 10:33 ve 10:45 SPX pozitif senaryoları geçer. Runtime NY tarihi ve broker UTC expiration DST örneklerinde birlikte doğrulanır. |
   | Eksik bar/veri takvimi; frozen hash değişimi; yanlış hesap/canlı hesap/kapalı işlem yetkisi | Yeni emir=0; neden kaydı var; gerçek hesaba fallback yok. |
   | İstekten önce crash, send sonrası cevap kaybı, kayıt öncesi crash, restart öncesi fill | Tek ekonomik emir; persistent intent korunur; history gecikmesiyle mükerrer send oluşmaz. |
   | PLACED/DONE fakat teyitsiz ticket; None koleksiyon; timeout; kısmi dolum | Teyitsiz durum PASS/flat/full-fill sayılmaz; yeni gönderim yok; uzlaştırma denenir. |
   | Pending → deal/position → SL veya TP kapanışı | Kimlik zinciri tam; gerçekleşen hacim, koruma ve exit broker fixture kanıtından çıkarılır. |
   | İptal ile aynı anda dolum; iptal teyidinin kaybı; SL/TP uyuşmazlığı | Yanlış CANCELLED/flat sonucu veya otomatik ikinci emir yok; exposure görünür kalır. |
   | Aynı SQLite için iki süreç; terminal/bağlantı kesintisi; yeniden başlama | Mevcut tek-gönderim/recovery davranışı korunur; bilinmeyen sonuçlar gizlenmez. |
   | Rollover sonrası watchdog hatası | Otomatik eski-state restart yok; iki state ve broker intent kanıtı korunur. |

   Temel DST, broker minimum lot, veri boşluğu, thread/process idempotency ve iptal geri okuma testleri zaten var; bunları silme/kopyalama. Eksik olan gerçek Super1 override ve uçtan uca durum geçişlerini ekle. Önce regresyonun eski davranışta başarısız olduğunu, sonra düzeltmeyle geçtiğini izole yerel fixture üzerinden kanıtla; çalışma ağacını eski sürüme döndürme.

   Repo için uygun mevcut yerel Python ortamında, çalışma dizini `C:\Users\ISAAC\Documents\otobacktestprojesi\work\backtest` olacak şekilde önce değişen test dosyalarını, sonra tam `python -m pytest -q` süitini çalıştır. Interpreter yolunu/sürümünü ve paket sürümlerini raporla. Sessiz pip yükseltmesi/kurulum yapma; ortam yoksa kanıt üretilemediğini yaz. Broker çağrısı gerektiren test burada koşturulmaz.

9. **Kod/sözleşme zincirini tutarlı tut; yayın kapısını atlama.**

   Değişen runtime bytes için sinyal sözleşmesindeki ilgili implementation hash'lerini, ardından runtime'ın `signal_contract_sha256` alanını, en son manifest'in contract/config hash'lerini yeniden hesapla. Yalnız bütünlük alanları değişebilir; aday payload'ı, frozen strateji ve araştırma proven bayrakları değişemez. Ham dosya SHA256 ile canonical artifact hash'ini birbirine karıştırma. Değişmeyen referans hash'lerini değiştirme; doğrulayıcıları devre dışı bırakma. Testleri son dosya bytes üzerinde yeniden geçir.

   [Release builder:72](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/deploy/build_signed_windows_release.ps1:72) tam 268, [artifact kapısı:204](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/deploy/build_signed_windows_release.ps1:204) tam 139 test istiyor. [release_integrity.ps1:141](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/deploy/release_integrity.ps1:141) ve testler de bu eski v16 sözleşmesini denetliyor. Yeni testler eklenince eski paketleme kapısının geçmesini bekleme.

   Sayıyı tutturmak için test silme, skip/xfail ekleme, yanlış manifest üretme veya eşitlik kapısını gevşetme. Bu aşamada yeni signed paket üretme. Yeni test envanteri ve sayılarıyla gerekli sürüm-sözleşmesi değişikliğini ayrı inceleme maddesi olarak teslim et; yayın sözleşmesi onaylanmadan eski v16 ZIP'ine düzeltilmiş sürüm muamelesi yapma.

10. **Sunucu geçişi için yalnız tasarım çıkar; çalıştırma.**

    [Yetkili runbook](C:/Users/ISAAC/Documents/otobacktestprojesi/docs/LIVE_OPERATIONS_TR.md) korunacak; LEGACY installer/recovery yollarından kestirme yapılmayacak. Aşağıdaki tespitleri sonraki geçiş prosedürünün zorunlu şartı olarak yaz; ortam bilgisi bilinmeden yeni installer veya alternatif taşıma kanalı seçme.

    | Konu | Sonraki onaylı geçiş için zorunlu şart |
    | --- | --- |
    | Yeni sunucu | Mevcut upgrader mevcut app/state/venv/terminal/DPAPI ve task ister; temiz sunucu bootstrap'ı kanıtlanmış değildir. Yalnız Super1'i hazırlayan, ikinci adayın varlığına bağımlı olmayan imzalı kurulum tasarımı ayrıca incelenecek. |
    | Tek aktif sunucu | Yerel dosya/SQLite kilidi iki host'u engellemez. Kaynak Super1 main/watchdog ve diğer otomatik başlatma yolları kalıcı engellenip süreç yokluğu doğrulanmadan hedefe yeni emir yetkisi açılamaz. Geçiş testi kaynak reboot sonrasında da Super1'in başlamadığını göstermeli; ikinci aday etkilenmemeli. |
    | Kampanya | Rollover state koruyan taşıma değildir. [campaign_lock:239](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_capital_forward.py:239) runtime/harness/account hash'lerini bağlar; düzeltme sonrası eski lock'u elle değiştirmek yasaktır. Eski geçmiş tam saklanmalı; yeni campaign/reset veya uyumlu devam yöntemi kullanıcı/mimar kararı olmadan uygulanamaz. |
    | State aktarımı | Kaynak tek-yazıcı kapalı ve broker durumu uzlaştırılmış olmalı. Campaign, SQLite intent/outbox, journal, veri ve karar kayıtları birlikte tutarlı snapshot alınmalı; açık SQLite'ın yalnız ana dosyası kopyalanmamalı. Kaynak/hedef hash manifesti ve bütünlük kontrolü gerekli. |
    | Hesap ve flat | Flat gate bütün hesabı kapsar. İkinci aday aynı hesabı kullanıyorsa onun emir/pozisyonlarını kapatarak veya filtreden gizleyerek gate geçilemez. Transfer yalnız broker pending/position durumu bilinen ve onaylı flat penceresinde yapılacak. |
    | Kimlik | DPAPI blob veya terminal profili taşıyarak hazır sayma. Hedef makinede doğru kullanıcı bağlamında güvenli credential kurulumu gerekli. Saved-session fallback, taze DPAPI/login kanıtının yerine geçmez. Parola/token/presigned URL rapora giremez. |
    | Task/işletim sistemi | Onaylı Windows sürümü, Python/wheelhouse/MT5 hash'leri, sınırlı Password task kullanıcısı, ACL'ler, UTC saat eşzamanlılığı ve RDP olmadan reboot sonrası çalışma aynı hedef ortamda doğrulanmalı. |
    | Sağlık | Runbook heartbeat kabulü ≤120 saniye; sadece process Running yeterli değil. Son başarılı döngü, güncel barlar, preflight, broker bağlantısı, fatal/launcher failure ve watchdog durumu ayrı gösterilmeli. Watchdog'un 180 saniyelik varsayılanını 120 saniyelik kabulün kanıtı sayma. |
    | Geri dönüş | Broker yan etkisinden sonra yalnız uygulama/state dosyalarını geri almak yeterli değildir. Her iki host yeni emre kapalı kalmalı; iki taraftaki son intent ve broker durumu uzlaştırılmadan otomatik restart yasak. |

11. **Gerçek broker kabulünü ayrı ve henüz yetkisiz aşama olarak tanımla.**

    | Kapı | Geçerli kanıt | Bu tur yetki |
    | --- | --- | --- |
    | A — Yerel yazılım | Maddeler 3–9; test çıktıları, gerçek Super1 pozitif/negatif yolları, güncel bytes/hash | Var; broker/ağ yok |
    | B — Ortam ve emirsiz broker kontrolü | Onaylı aynı release, görev kullanıcısı, doğru demo kimliği/izinler, taze veri, açık piyasada gerçek order_check, bilinen flat | Yok |
    | C — Pending kabul/iptal | Onaylı demo hesabında broker minimum hacmiyle gerçek pending ticket ve tüm request alanlarının geri okunması; iptal sonrası tüm hesap exposure=0 | Yok |
    | D — Strateji yürütmesi | Değiştirilmemiş Super1'in doğal sinyali → gönderim → broker kabulü → varsa fill/deal/koruma → terminal exit zinciri; her iki bacak için ayrı sonuç | Yok |
    | E — Hedef sunucu | Kaynak engelleme, aynı imzalı bytes, doğru kullanıcı, reboot/bağlantı toparlanması ve hedefte B–D doğrulaması | Yok |

    B'de piyasa kapalı/quote yok/preflight ertelendi sonucu PASS değildir. C'deki pending açıp iptal etme testi gerçek dolumu veya SL/TP ile kapanışı kanıtlamaz. D'de doğal sinyal/fill/exit oluşmayan alt adımı `INCOMPLETE` yaz; fiyatı, lotu, stratejiyi veya sinyali değiştirip PASS üretme. Fiyat pending emre hiç ulaşmayabilir; kabul edilmiş her emrin dolması beklenmez. Stratejinin kendi iptal/expiry kararı doğruysa ilgili durum başarılı doğrulanabilir, fakat fill kanıtı eksik kalır.

    Operasyonel test emrini Super1 risk geçmişine karıştırma. Mevcut smoke dışında yeni fill/manuel kapatma testi bu talimatla yetkili değildir; ayrı test kimliği/hesabı ve sınırları önceden onaylanmalıdır. Mevcut Super1 magic ile keyfi manuel kapanış, risk geçmişinin TP/SL beklentisini bozabilir.

    Yeni sunucunun ağ/terminal/görev davranışı orada kontrollü kurulum ve test yapılmadan kanıtlanamaz. A'nın geçmesi yalnız yazılım hazırlığını gösterir. Sonraki kontrollü kurulum ile sürekli otomatik emir yetkisini ayrı onay noktaları olarak bırak. Hiçbir kapı %100 kesintisizlik, her emrin kabulü veya dolumu garantisi değildir.

12. **Teslimatını kanıtla ve burada dur.**

    Yerel çıktı dizini `C:\Users\ISAAC\Documents\otobacktestprojesi\outputs\super1_readiness_20260831` olsun. Varsa önceki çıktıları üzerine yazma; numaralı yeni run alt dizini kullan. Şunları teslim et: `report.md`, `readiness.json`, `test-results.xml`, ham test stdout/stderr ve dosya hash manifesti. Credential, terminal profili, özel anahtar ve gerçek hesap numarası çıktıya girmesin.

    Raporda her madde için bulgu → değişen dosya/satır → test → gözlenen sonuç bağlantısı olsun. Baseline/final commit veya çalışma ağacı diff hash'i, interpreter/paket sürümleri, tam komutlar, exit code, PASS/FAIL/SKIP/NOT_RUN sayıları ve eksik kanıtlar ayrı verilsin. Test çalışmadıysa sonuç dosyası uydurma; `NOT_RUN` gerekçesini yaz. Önceki raporları yeni test çıktısı gibi sunma.

    `readiness.json` en az `software_status`, `release_contract_status`, `source_fenced`, `broker_preflight`, `pending_accept_cancel`, `strategy_fill_exit`, `target_environment`, `unresolved_user_decisions` ve `overall_status` alanlarını içersin. Çalıştırılmayan dış adımlar `NOT_RUN/NOT_VERIFIED`; genel durum bu ilk aşamada **NO_GO** kalır. Yalnız tüm zorunlu yerel regresyonlar geçerse `software_status=PASS` yazılabilir. Güvenli bloke olmak, zamanında işlem yapabildiğini tek başına kanıtlamaz.

    Mevcut tarihsel kanıtı doğru aktar: [31 Ağustos attempt_status:3](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/outputs/live_recovery/20260831/v16-day-close/today_live_attempt/attempt_status.json:3) `NOT_RUN`; [yerel doğrulama:21](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/outputs/live_recovery/20260831/v16-day-close/today_operator_validation/operator_validation_manifest.json:21) sunucu hash eşleşmesini doğrulamıyor. [Gün sonu raporu:20](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/outputs/live_recovery/20260831/v16-day-close/V16_DAY_CLOSE_20260831.md:20) güncel sunucu duruşunu da bilinmiyor olarak kaydediyor. Bu nedenle şu an “sunucu durmuş”, “son sürüm kurulu” veya “broker E2E geçti” deme.

    Yerel işleri tamamladıktan sonra düzeltme ve kanıt paketini mimara ver. Sonraki aşamaya kendiliğinden geçme. Yerel çalışmayı engellemeyen bilinmeyenler yüzünden tüm işi yarıda bırakma; ilgili dış adımı kilitli bırak.

Kullanıcıdan netleştirilecek bilgiler: hedef sunucunun işletim sistemi; Super1'in aynı XM MT5 demo hesabıyla devam edip etmeyeceği; mevcut kampanyanın devamı veya yeni dönem beklentisi; ikinci adayın hesap/terminal/Windows kullanıcısını paylaşıp paylaşmadığı. Bu bilgiler bilinmeden hesap, risk, kampanya reset'i veya geçiş biçimi hakkında karar verme. Şifre veya özel anahtarı sohbetten isteme.
