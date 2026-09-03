**Super1 — run-007 denetimi ve dördüncü mühendis talimatı — 31 Ağustos 2026**

**NO_GO doğru; genel yerel yazılım kabulü ve continuation DESIGN_TESTED henüz onaylanmıyor.** Bu talimat [üçüncü talimatın](C:/Users/ISAAC/Documents/otobacktestprojesi/docs/SUPER1_MUHENDIS_TALIMATI_03_20260831.md:1) kalan kabul maddelerini kapatır. Yeni strateji veya deployment kapsamı açmaz. Önce o belgedeki sabit kararları ve bu belgedeki somut düzeltmeleri oku.

Denetimde final manifestin 41 kanıt dosyası, metadata'daki 23 girdi hash'i ve run-006'ya ait 14 kanıt dosyası eşleşti. Özgün JUnit/ham çıktı/process saatleri 136 hedefli ve 314 tam test sonucuyla tutarlı; hata/skip sıfır. run-006'nın 288 test node'u tam suite'te korunmuş, 26 node eklenmiş. Hedefli sayının 139'dan 136'ya düşmesi tek başına test silindiği anlamına gelmiyor. Mimar testleri yeniden çalıştırmadı; kaynak/state/sunucu değiştirmedi.

Kabul edilen ilerleme: E01 doğru MT5 ticket sorgusu; E02 initial/current hacim ayrımı; E03 normal korumalı partial ve ilgisiz pozisyon ayrımı; E04 aynı döngü/istemci yeniden oluşumunda blokaj; T01 gerçek gözlem/üretim timestamp'lerinin korunması; T02'nin 10:58 erken kapsam beklemesi; C01 iki kaynak çıkarımının semantik karşılaştırması; planlayıcıda bilinmeyen kaynak zamanı/release için null ve exclusive writer; production rollover'da fatal/recovery yazımı. Bu düzeltmeleri geri alma. Kısmen kapanan başlıkların aşağıda belirtilen eksikleri ayrı duruyor.

1. **Yetki sınırı değişmedi.**

   Yalnız Super1, aynı XM MT5 demo hesabı, aynı mevcut kampanya; hedef Windows ve SSH mevcut; diğer adayla bağımlılık yok. Kullanıcıdan bunları yeniden isteme. Frozen engine/strateji/risk, magic/hesap/server/symbol kimliği ve önceki iki bacak/saat kuralları değişmeyecek. `proven=false`, `parity=false` korunacak.

   Bu tur yalnız yerel hedefli kod/test düzeltmesi, sentetik geçici depolar ve yeni kanıt üretimi yapılır. Gerçek SSH, kaynak/hedef state veya snapshot, MT5 initialize/login, broker/emir, görev/credential, kaynak durdurma, kurulum/transfer, gerçek imza/paket üretimi/yayımlama yok. Regresyonlar dış ağdan ve gerçek MT5 bağlantısından yalıtılır. Önceki resmî dokümanları salt okunur inceleme izni operasyon izni değildir.

   Mevcut lock'u silme/değiştirme, yeni kampanya/reset/init/rollover kullanma. Normal daemon'un continuation kabulünü henüz açma. PowerShell yalnız aşağıda tanımlanan yalıtılmış test sınırında çalıştırılabilir. Eski belgeler ve run-007 dahil eski kanıtlar değişmez.

2. **R01 — Üretimde son gönderim saatinin sabitlenmesini kaldır. Öncelik bir.**

   [XM:2551](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_xm_mt5_forward.py:2551) artık production reconcile içinden `_place_candidate(..., send_now=now)` çağırıyor. [Guard:1604](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_xm_mt5_forward.py:1604) bu değeri kullanınca order_check/izin/disk gecikmesi boyunca saat ilerlemiyor. Böylece önceki turda kapanmış olan SDK öncesi gerçek zaman koruması geri alınmış.

   Production reconcile çağrısından sabit `send_now` aktarımını kaldır. Production SDK öncesi guard, izin sorgularından sonra gerçek güncel saati okusun; ilk reconcile zamanı yalnız o gözlemin timestamp'i olsun. Test kolaylığı için production saatini dondurma. Testler saati yalnız `core.utc_now` sınırında ilerleyebilen kontrol edilebilir saatle yönetsin; gerçek reconcile girişinden başlayan kabul testleri `_place_candidate` içine sabit timestamp veremez.

   Tazelik kararı [ilk talimattaki iki-cutoff sözleşmesidir](C:/Users/ISAAC/Documents/otobacktestprojesi/docs/SUPER1_TASIMA_ONCESI_MUHENDIS_TALIMATI_20260831.md:43): her bacak için `min(closed_cutoff(actual_now, timeframe, 8), frozen_end)` hesapla; ortak prefix'in iki cutoff'u da bu vektörle eşleşsin. [Mevcut guard:1505](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_xm_mt5_forward.py:1505) yalnız aday bacağını kontrol ediyor; ortak vektörü denetle. Yeni prefix kayıtları ve guard aynı frozen-end sınırını kullansın; eski kayıt bytes'ı değişmez. Entry pencere kontrolü yalnız emrin ilgili bacağınadır. NQ bittikten sonra onun vektör bileşeni 10:30'da sabitlenir; ortak prefix tazeyse SPX 11:00'e kadar çalışabilir. Üçüncü talimattaki “taze SPX'i NQ stale diye engelleme” cümlesi muğlaktı; bu paragraf onun yerini alır. Yalnız SPX bileşeninin taze olması ortak eski prefix'i geçerli yapmaz.

   Üç ayrı regresyon: NQ reconcile 10:29:59 NY'da başlasın; sırasıyla order_check, izin sorgusu ve intent/outbox işlemi saati 10:30:01'e taşısın. Production reconcile → Super1 override → son guard yolu çalışsın; yeni-entry SDK çağrısı=0; kesin hiç gönderilmemiş NQ intent/outbox terminal no-send olsun. Sonraki uygun 10:35 SPX adayı iki cutoff'u da güncel ortak prefix ile 1 kez çalışabilsin. Ek tazelik testi: saat 10:24:10, prefix NQ=10:21/SPX=10:20 iken SPX için de send=0; NQ=10:24/SPX=10:20 ile yeniden üretilmiş uygun ortak prefix'te send=1. Stale→fresh senaryosunda toplam 1 gönderim sınırı korunsun.

   Bu testlerden en az biri run-007 production bytes'ında hata göstermeden R01 kapanmış sayılmaz. Sadece private helper'a saat enjekte eden eski test bu kabulün yerine geçmez. Sabit saati kaldırınca başka testler bozulursa production davranışını geri bozma; onların saat fixture'larını doğru sınıra taşı.

3. **R02 / E03 — Belirsiz REMOVE, izlemeyi fatal durdurmasın.**

   [XM:2220](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_xm_mt5_forward.py:2220) REMOVE sonucu None veya DONE dışındaysa `UnsafeOpenOrdersError` yükseltiyor. Broker iptali uygulayıp cevabı kaybettiğinde bu, daemon'un izlemeyi kesmesine yol açıyor. Başarılı REMOVE sonrasındaki history okuma hatasını test etmek, REMOVE cevabının kaybını test etmek değildir.

   İptal girişimini ilgili order/intent'e kalıcı olarak bağla. SDK sonrası None/exception/timeout/connection belirsizliği kalıcı cancel-unknown olarak saklansın; yeni entry kapalı, broker uzlaştırması ve korunma izlemesi açık kalsın. Kesin ret, kabul cevabı ve belirsiz ekonomik sonuç ayrı sınıflansın. Belirsiz cevabı CANCELLED/flat veya kesin hiç yapılmadı diye etiketleme; körlemesine ikinci REMOVE/yeni entry gönderme. Yalnız broker teyidiyle sonuç çözülür. Kimlik/bütünlük ihlallerinin kritik duruşu korunur.

   Gerçek istemci/ledger kullanan fixture önce sahipliği kesin pending emir oluştursun. REMOVE broker fixture'ında gerçekten uygulansın, fakat cevap sırasıyla None, timeout exception, timeout retcode ve connection retcode olsun. İlk geri okuma belirsiz, sonraki geri okuma iptali doğrulanmış gösterecek. Her durumda yeni-entry=0, REMOVE toplamı=1, fatal yok; istemci/store yeniden oluşturulunca belirsizlik korunacak ve teyitle çözülecek. Partial pozisyon varsa görünür/izlenir kalacak; kalan pending'in iptali bütün ekonomik intent'i terminalleştirmeyecek. Mevcut korumalı/unrelated-position testlerini koru.

4. **R03 / T02 — Finalization için fetch sonu kadar kullanılabilir closed cutoff'u da doğrula.**

   [Core:1090](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_capital_forward.py:1090) fetch_scope_end'in final pencere sonuna ulaşmasını yeterli sayıyor. Oysa [build_frames:886](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_capital_forward.py:886) 8 saniye gecikmeli closed cutoff kullanıyor. SPX shared-asof=11:00:07, finalization=11:05:10 olduğunda fetch kontrolü geçer, fakat kullanılabilir 5m cutoff 10:55'tir. Final gate'in 11:00 istemesi, yeterli barlar store'da olsa bile yanlış kalıcı DATA_INVALID yaratabilir.

   Her bacak için iki koşulu birlikte iste: başarılı fetch kapsamı gerekli final cutoff'u kapsıyor ve mevcut `closed_cutoff(shared_asof, timeframe, closed_bar_delay_seconds)` aynı cutoff'a ulaşmış. Koşullardan biri eksikse WAIT_FOR_FINAL_DATA; final attempt/marker yok. 8 saniye kuralını kaldırma, shared-asof'u gerçek finalization saatiyle değiştirme veya henüz çekilmemiş veriyi varsayma. Gerekli iki koşul tamamlandığında gerçek eksik/çelişkili veri mevcut DATA_INVALID/critical kurallarına tabidir.

   Store=object() ve erken return testi yeterli değildir. Gerçek BarStore, iki bacak fetch kapsamı, aggregation ve finalize ile aşağıdaki durumları çalıştır:

   | Shared as-of; gerçek finalization saati 11:05:10 NY | Zorunlu sonuç |
   | --- | --- |
   | 10:58 | WAIT; final attempt/marker yok |
   | 11:00:00 ve 11:00:07 | WAIT; son bucket henüz kullanılabilir değil |
   | 11:00:08; iki bacak kapsamı ve verisi tam | Bir kez gerçek final; tekrar ALREADY_FINALIZED |
   | 11:00:08 veya sonrası; kapsam tam, veri gerçekten eksik/çelişkili | Mevcut DATA_INVALID/critical; sahte VALID yok |

   Bekleyen fixture sonraki tamamlayıcı fetch ile aynı gün doğru finalleşsin. Önceki immutable kaydı silip tekrar yaratmak çözüm değildir. R03'ün sınır hatasını run-007 bytes'ında ayrı kanıtla.

5. **I01 — E04, T01 ve C02 için istenen birleşik kabul yolunu çalıştır.**

   **E04:** Aynı döngü ve restart blokajı kodda düzelmiş. Fakat [fixture:1460](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/tests/test_xm_mt5_forward.py:1460) broker emri oluşturmadan sayaç artırıp None/exception/partial döndürüyor. Mevcut dört parametreli testi genişlet: ilk SDK çağrısı emri/uygun partial deal'i broker sınırında gerçekten oluştursun, görünürlük geciksin, ikinci order_id bloke kalsın. Sonraki görünür broker kanıtıyla ilk intent yeniden gönderilmeden doğru order/position'a bağlansın. Hâlâ güncel/uygun ikinci karar ancak tüm kapılar gerçekten açıldıktan sonra değerlendirilsin. İlk order_id toplam send=1; bilinmeyen dönemde ikinci=0. Yeni ekonomik etkiyi yalnız sayaçla taklit etme.

   **T01:** [Mevcut test:336](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/tests/test_capital_forward.py:336) yalnız bir fetch→BarStore gözlem zamanını kanıtlıyor. İstenen iki bacak→aggregation→prefix→karar üretimi→gerçek reconcile akışını ekle. NQ fetch 10:23:50, SPX fetch sonu 10:24:10 NY; fixture yalnız NQ'da yürütülebilir aday üretsin. Aynı veri as-of korunurken gerçek observation/produced-at ilerlesin; eski ortak prefix'te ilk tur send=0, tam/güncel sonraki tur 1 olsun. İki-cutoff tazelik kuralı R01'de kesinleştirilmiştir; NQ'nun penceresinin kapanmasıyla ortak prefix'in eski olması farklı durumlardır. Geçmiş günün bugün öğrenilmesinde first-known bugün kalmalı. Fetch/prefix/aggregation/guard mock edilmez.

   **C02:** [Mevcut ALLOW:378](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/tests/test_super1_xm_forward.py:378) hâlâ away-from-VA adayla overnight dalını atlıyor. Ayrı mocked filtre/helper testlerini bir araya yazmak uçtan uca kanıt değildir. Gerçek sealed calendar + piyasa barları + frozen filter + risk/request + prefix/send guard + ledger + SDK sınırı aynı test akışında çalışsın; yalnız saat, piyasa/broker sınırı ve geçici depolar kontrollü olsun.

   Dört ayrı gerçek senaryo: Monday-short BLOCK; proximity+overnight-up BLOCK; proximity dalında kanıtlı overnight ALLOW; önceki doğru RTH kapanış barı eksik UNRESOLVED. Yeni-entry sayıları 0/0/1/0; gerçek filtre nedeni, request içeriği ve kalıcı state assert edilsin. Tekrar ve yeniden oluşturulan istemci duplicate üretmesin. BLOCK/UNRESOLVED başka bir eski prefix/permission hatasından send=0 olduğu için geçemez. Önceki talimattaki iki bacak/yön/risk ölçekleri ve 10:33/10:45 SPX kabulü korunur.

   Calendar negatiflerini ayrı hata katmanlarına ayır. [Mevcut hash testi:47](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/tests/test_super1_calendar.py:47) geçerlidir; ancak JSON parser'a ulaşmadan düşer. Duplicate key/tarih, coverage boşluğu/dışı, timezone/saat ve provenance testlerinde fixture'ın gerekli önceki kapıları geçmesini sağlayıp beklenen somut nedenin oluştuğunu assert et. Örneğin yalnız geçici test takviminde malformed bytes'ın beklenen ham hash'ini doğru hesapla ki parser gerçekten çalışsın; production config/trust kontrolünü kapatma. Source semantik mutasyonu tam yerel validator girişinden de FAIL üretmeli.

   31 Ağustos/8 Eylül önceki seansları, 30 Kasım/28 Aralık 12:59 erken kapanış barları, 2 Temmuz 15:59 ve Mart/Kasım DST örnekleri önceki listede zorunlu kalır. Çalışan feature okuma hatası belirli NO_SEND üretir; startup sealed-hash/bütünlük hatası ise mevcut kritik başlatma engelini korur. Takvim gerekmeyen dal yeni genel BLOCK kuralına dönüşmez.

6. **M01 — Planlayıcıdaki Super1 bağlamını ve bilinmeyen şemaları düzelt.**

   [Planlayıcı:18](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/plan_super1_campaign_continuation.py:18) core.harness_hash'i import ediyor; [satır 34](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/plan_super1_campaign_continuation.py:34) Super1 configure_core bağlamı kurulmadan çağırıyor. Varsayılan Capital runtime/script kapsamının hash'i Super1 harness değildir.

   Hesabı gerçek Super1 için yapılandırılan sıralı HARNESS_PATHS + RUNTIME_CONFIG + PARENT_BASELINE bytes/basename sözleşmesine bağla. Mevcut hash algoritmasını değiştirme; global import sırasına bağlı yanlış Capital sonucu üretme. Bağımsız beklenen Super1 dosya listesi/sırası testte doğrulansın; yanlış Capital kapsamı negatif olsun. Bu salt yerel konfigürasyondur, campaign_lock/init/client/broker oluşturma gerekçesi değildir.

   [Satır 56](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/plan_super1_campaign_continuation.py:56) gözlenmemiş kaynak DB şemalarına hâlâ 1 yazıyor. Snapshot yokken gözlenen schema/PRAGMA/fingerprint unresolved kalsın; yerel beklenen adapter şeması ayrı alan olabilir. Source-created-at/root/öncül/eski release null davranışı korunur.

   Exclusive writer korunacak; üçüncü talimattaki existing-output/source containment/case eşdeğerliği/junction/reparse ve kaynak dosya envanterinin değişmezliği testleri tamamlanacak. Testler yalnız geçici fixture'da; gerçek kaynak kökü okunmaz. Yol testinin kurulamaması o testi PASS yapmaz.

7. **M02 — İmza, ayrı dosyaları değil geçişin bütün anlamını bağlasın.**

   [Validator:264](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/super1_continuation.py:264) bazı dosya bytes/hash'lerini denetliyor ama imzalı transition içeriğini kaydın account/snapshot/old-new hash/zincir alanlarıyla karşılaştırmıyor. [Fixture:76](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/tests/test_super1_continuation.py:76) transition dosyasına yalnız TEST_ONLY etiketi yazıyor. [HMAC doğrulaması:255](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/super1_continuation.py:255) mevcut RSA release güven kökü sözleşmesinin kanıtı değildir.

   Tek kabul biçimi: gerçek şemalı kanonik transition payload'ı; orijinal root lock hash/created_at/campaign, broker hesap-server kimlik bağı, snapshot manifest hash'i, önceki transition hash'i, eski-yeni release/runtime/harness/contract/calendar bağları, state şema kimlikleri, değişmeyen engine/frozen candidate/risk ve allowed-change listesi imzalanan bytes'ın içinde olmalı. Doğrulayıcı dışarıdan verilen kayıtla bu imzalı payload'ı alan alan eşleştirsin; snapshot/member ve release manifest/archive hash'lerini aynı zincire bağlasın.

   Mevcut [release_integrity.ps1](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/deploy/release_integrity.ps1:35) RSA/SHA256 byte/padding sözleşmesi kullanılacak. Geçici TEST ONLY RSA anahtarı güvenilir test girdisi olarak harness tarafından verilebilir; payload'ın kendi key beyanı güven kökü olamaz. HMAC test yolu bu kabulün yerine kullanılamaz. Production private key/certificate store/pin değiştirilmez; test anahtarı normal runtime kabulüne eklenmez. Orijinal campaign_lock bytes'ı yeniden yazılıp “yeni kök” yapılmaz; değişmeyen lock hash'i geçiş yetkisinin içinde bağlanır.

   Olumlu fixture tam şemalı aynı kampanya zinciri olsun. Ondan türetilen ayrı negatifler: dış record'un account veya new-runtime hash'ini değiştir ama transition bytes/imza aynı kalsın; snapshot manifestini değiştir; farklı campaign/root; yanlış RSA kökü; signature/payload bytes değişikliği; eksik hash/şema; kopuk/tekrarlanan/çevrimli öncül; risk/frozen değişikliği. Her biri gerçek verifier'dan FAIL almalı. Birbirinden bağımsız geçerli imzalar ortak geçiş yetkisi sayılmaz. Önceki transition yokluğu bilinmiyorsa kanıtlanmış ilk geçiş varsayma.

   Sentetik kabul geçse de gerçek dry-run SOURCE_SNAPSHOT_NOT_VERIFIED, apply_allowed/safe_to_apply=false kalacak. Normal daemon'un yeni protokol kabulü açılmayacak.

8. **M03 — Hazır blocked=True kayıtlarını kaldır; gerçek snapshot farklarını hesapla.**

   [Test:170](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/tests/test_super1_continuation.py:170) her senaryoya `blocked=True` veriyor; [validator:389](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/super1_continuation.py:389) bunu geri okuyor. Bu negatif test değildir. [WAL testi:150](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/tests/test_super1_continuation.py:150) iki kolonlu oyuncak tablo kurup yalnız şema eşitliğine bakıyor; ekonomik state taşınmasını kanıtlamıyor.

   Üretici BarStore ve emir-store şemalarıyla geçici kaynak kur. Gerçek kolonlarıyla minute_bars/conflicts, order_intents, order_event_outbox, broker_execution_states; deal/risk event'leri; root lock/karar/sağlık kayıtları oluştur. 10'dan fazla terminal R, UNKNOWN ve gönderilmiş intent, ack'li/ack'siz outbox içersin. SQLite backup API ile ayrı snapshot al, bağımsız bağlantıdan bütün gerekli satırları/kimlikleri/sıraları oku; source/snapshot karşılaştırmasını validator hesaplasın. Fixture doğrulayıcıya beklenen blocked/PASS sonucu girdi olarak verilmez.

   Aynı sağlam snapshot'tan her negatif için ayrı kopya üret ve gerçekten boz: WAL'daki son commit kaybı, deal silme/değiştirme, outbox sıra/ack/içerik bozulması, risk geçmişini kesme, zorunlu alan kaybı, kendi içinde tutarlı farklı campaign. Doğrulayıcı gerçek veri uyuşmazlığından reddetsin. Bozulmuş snapshot kendi manifestine göre yeniden hash'lense bile kaynak/kimlik/süreklilik karşılaştırması bu kaybı yakalasın. Kontrol edilmeyen vaka boş dict üzerinden all([])=True ile PASS olamaz.

   [Inventory:338](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/super1_continuation.py:338) launcher_failure yolunu `runtime/` altında arıyor; gerçek üreticiye göre kök `launcher_failure.json` olacak. Önceki state tablosundaki sessions/preflight dahil tüm ilgili yolları gerçek üreticilerle eşleştir. Varsayılan server kökünü gözlenmiş kaynak kökü sayma.

   [Satır 352](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/super1_continuation.py:352) risk sırasını updated_at diye tarif ediyor; Super1 hesabının broker çıkışları için mevcut time_msc/ticket sırası ve R yöntemi korunacak. Kaynak/snapshot son 10 R, deal dedup ve order kimlikleri gerçek kayıtlardan karşılaştırılacak. Hedefi taklit eden eksik broker history, yeni kampanya boş geçmişi sayılmayacak. Gerçekten sıfır terminal sonuç ancak kaynak kanıtlıysa mümkün; “10'dan fazla” tüm kampanyalara üretim önkoşulu değildir, bu fixture'ın kapsama şartıdır.

9. **O01 — Harness'i gerçek production hata yolu ve başlangıç kapısına bağla.**

   [Harness:47](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_super1_rollback_harness.ps1:47) kendi switch/throw/JSON mantığını çalıştırıyor. [Satır 97](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_super1_rollback_harness.ps1:97) daemon yerine string karşılaştırıyor; process_recreation_latch_consumed satır 104'te sabit true. Production rollover'un fatal/recovery yazımı düzelmiş olsa da bu harness onu sınamıyor.

   Mevcut production post-start hata yönetimi ve kalıcı recovery yazımını yeniden kullanılabilen dar bir fonksiyon sınırına çıkar; gerçek rollover orchestration ve test aynı fonksiyon/kod yolunu kullansın. Kopya hata algoritması veya sadece test için paralel deployment yolu oluşturma. Test harness yalnız scheduler/process/broker sınırlarını ve belirli I/O hata noktalarını değiştirir; asıl catch/recovery davranışını mock etmez. Production betiğin bu ortak yolu çağırdığı test edilir; yalnız ortak helper'ı tek başına çalıştırıp çağırmayan production kodunu PASS sayma.

   Start timeout, start sonrası watchdog hatası, stop hatası ve arşiv kanıtı yazma hatasını gerçek orchestration sınırında enjekte et. Sonuçlar gerçek çağrı izinden/state'ten hesaplanacak: old/new state korunur; post-start belirsizlikte otomatik restore/restart yok; kanıtsız STOPPED yok. Archive yazımı başarısızken güvenlik latch'i yine kalır; latch de yazılamıyorsa yeniden başlatma koruması kanıtlanmış sayılmaz ve NO_GO olur.

   Ardından ayrı, yalıtılmış Python process aynı geçici state üzerinde gerçek `assert_no_fatal_latch` başlangıç kapısını çalıştırsın; beklenen latch kaynaklı engel ve broker/start çağrısı=0 kanıtlansın. Başka bir eksik env/config hatası bu kanıt yerine geçmez. Sonuç JSON'una process_recreation=true sabiti koyma. Gerçek gate kaldırılmış/bağlantısı kopmuş izole negatif kontrolde testin başarısız olduğunu göster; üretimde bypass/recovery otomatiği ekleme. Hiçbir gerçek scheduled task, terminal veya credential okunmaz/çalıştırılmaz.

10. **V02 — Kabul matrisini gerçekten çalışmış senaryolardan üret.**

    [Matris üreticisi:121](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_super1_evidence.py:121) bütün satırlara ortak process-exit sonucundan PASS yazıyor; yeni-entry/REMOVE her satırda NOT_APPLICABLE ve state açıklamaları sabit. [Final matris](C:/Users/ISAAC/Documents/otobacktestprojesi/outputs/super1_readiness_20260831/run-007/final-acceptance-matrix.json:1) bu nedenle gereksinim kabulü değil, genel koşu özeti.

    Her gereksinim ve alt senaryo için zorunlu gerçek test node'ları, parametrizasyonları ve ölçülecek sonuçlar envanterini önce tanımla. Son JUnit'de bu node'ların her birinin çalışmış ve başarılı olduğunu doğrula; eksik/skipped/xfail node veya gözlem dosyası varsa ilgili satır INCOMPLETE/FAIL. Sadece bir helper testinin geçmesi bütün entegrasyon satırını PASS yapamaz. Toplam test sayısı hedef değildir; test silme veya beklentiyi gevşetme yok.

    Broker sınırındaki çağrı izinden ölçülen yeni-entry/REMOVE sayıları; gerçek ledger/snapshot readback'i; filtre/guard/finalization nedenleri ve hata enjeksiyonu izleri, ilgili testin gözlem çıktısı olarak kaydedilsin. Matris bu özgün dosyaları hash/path ile göstersin. Expected değer testte tanımlanabilir; actual değer expected kopyası veya önceden verilen blocked=true olamaz. E/T/C emir senaryolarının sayıları NOT_APPLICABLE olamaz; yalnız gerçekten emirsiz dosya testlerinde bu etiket kullanılabilir.

    Eksik node, tek bir gözlem sayısının değişmesi ve production bağlantısının bozulması matris kabulünü düşürmeli. Bunun için genel mutation framework kurma; tanımlı negatif fixture ve kontrollü bağlantı negatifleri yeterlidir. Kapsamı karşılayamadığın satırı dar helper açıklamasıyla PASS kapatma.

11. **V03 — Kanıt kökenini koru ve eksik hash kapsamını tamamla.**

    Özgün stdout/stderr, ayrı JUnit, gerçek argv/interpreter/cwd/UTC süreç kayıtları run-007'de iyileşmiş; bunları koru. Fakat [final üretici:86](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_super1_final_evidence.py:86) girdi hash'lerini yalnız testler bittikten sonra alıyor. [Girdi listesi:93](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_super1_evidence.py:93) kendi import edilen evidence üreticisini kapsamıyor; ilgili production deploy/helper ve hedefli listede olmayan deployment testleri de listede yok.

    Test edilen değiştirilebilir bütün kaynak/config/contract/test/harness/helper/provenance bağımlılıkları için koşu öncesi ve sonrası hash envanteri al; envanterin kendisi eski/new files'i kapsasın. En az iki evidence üreticisi, production rollback ve kullanılan gerçek recovery/startup helper'ları dahil olsun. Commit+diff değişmeyen tracked dosyalar için köken sağlayabilir; untracked yeni dosyaları salt isim listesiyle bağlama. Hash değişirse o test koşusu son bytes için kabul edilmez; tekrar çalıştırılır.

    R01/R02/R03 için run-007 production bytes'ında önce başarısız regresyon, sonra düzeltmeyle başarılı sonuç kanıtı üret. Eski ağacı veya eski kanıtları overwrite etme. Yeni test eklenmesiyle değişen test dosyalarının hash'lerini production başlangıç hash'lerinden ayrı kaydet; hangi bytes'ın hangi koşuda test edildiği belirsiz olmasın.

    Kanıt üreticisi tek yeni/boş run dizinini kullansın; hardcoded run-007'ye yeni çıktı ekleme. Çıktı create-new sınırı gerçek process çıktıları/JUnit için de sağlansın; mevcut JUnit üzerine çalıştırıp sonra stdout'da FileExistsError almak append-only koruma değildir. Son report/readiness/matris bütün özgün final çıktılara açıkça bağlansın. Hash manifesti en son üretilir; kendi hash'i dışında teslimin kaynak ve rapor bağımlılıkları doğrudan veya açık hash zinciriyle kapsanır.

    Eski v16 268/139 release kapısı ve paketleri değişmez. Yeni release önerisi sadece REVIEW_REQUIRED etiketi olamaz: kesin test node/dosya envanteri, son gerçek toplamlar, artifact içi test listesi, yeni kaynak/helper/calendar/provenance payload dosyaları ve doğrulama gereksinimleri listelensin. Sayıyı >= yapma, eski paketi yeniden imzalama veya bu tur paket üretme.

12. **Yeni koşuyu teslim et; operasyon aşamasına geçme.**

    [Kanıt kökü](C:/Users/ISAAC/Documents/otobacktestprojesi/outputs/super1_readiness_20260831) altında boş run-008 kullan; varsa overwrite etmeden ilk sonraki boş run numarasını seç ve gerçek kimliği kaydet. R01/R02/R03 önce-sonra kanıtları, tüm açık kabul senaryolarının gerçek gözlemleri, hedefli/tam JUnit ve ham çıktılar, compileall, calendar, continuation, production bağlantılı rollback, öncesi/sonrası hash'ler ve yeni release önerisini teslim et.

    | Karar alanı | Kabul |
    | --- | --- |
    | software_local_acceptance | R01–R03 ve önceki E/T/C/O zorunlu gerçek davranışsal kabulü + özgün son koşu kanıtı varsa PASS; aksi FAIL/INCOMPLETE |
    | campaign_continuation | M01–M03 gerçek olumlu/olumsuz fixture'ları tamamlanırsa yalnız DESIGN_TESTED; aksi DESIGN_INCOMPLETE |
    | source_fenced | NOT_VERIFIED; public doküman erişimi veya yerel hash bu alanın sonucu değildir |
    | source_snapshot / target_environment | NOT_VERIFIED |
    | broker/preflight/pending/fill-exit/task-reboot/deployment | Gerçek ortam için NOT_RUN |
    | apply_allowed / safe_to_apply / proven / parity | false |
    | release_contract | REVIEW_REQUIRED |
    | overall | NO_GO |

    Bu liste önceki açıkları kapatır; yeni özellik, alternatif strateji, release bypass veya kendiliğinden operasyon kararı alamazsın. Zorunlu bir gerçek akışı test edemiyorsan yerine sonuç üreten taklit yazma; ilgili satırı INCOMPLETE bırak, çalıştırmayı engelleyen somut nedeni ve dosya/satırı raporla. Yapılabilir diğer yerel işleri tamamla. Kaynak/target/hesap gibi kullanıcının kesin kararlarını yeniden sorma.

    Mimar yeni kanıtı incelemeden kaynak durdurma, sunucu bağlantısı, transfer/kurulum veya demo emir aşamasına geçilmeyecek. Bu çalışma henüz yalnız yerel hazırlıktır; broker kabulü, doğal strateji dolumu/çıkışı ve hedef Windows kullanıcı bağlamında yeniden başlama ayrıca yetkilendirilip gözlenecek.
