**Super1 — ikinci mühendis talimatı, mevcut kampanyayla devam — 31 Ağustos 2026**

Karar **NO_GO**. run-005'teki 130 hedefli / 279 toplam test sonucu JUnit ve ham çıktılarla tutarlı; hash manifestindeki 32 dosya da mevcut bytes ile eşleşiyor. Buna rağmen kod denetiminde yürütmeyi etkileyen açık hatalar var. Testlerin geçtiği kabul edilir; yazılımın hazır olduğu kabul edilmez. Bu mimari denetimde test, broker veya sunucu işlemi çalıştırılmadı.

Kullanıcının kesin kararları: **hedef Windows; SSH mevcut; aynı XM MT5 demo hesabı; mevcut Super1 kampanyası kesintide kaldığı yerden devam edecek; ikinci adayla hesap/terminal/kullanıcı bağımlılığı kurulmayacak.** Bunları yeniden sorma. SSH erişilebilirliği, hedef görev kullanıcısının MT5 çalıştırabildiğinin kanıtı değildir.

Bu belge önceki talimatın devamıdır. Yeni kampanya, campaign reset, mevcut lock'un silinmesi/değiştirilmesi, `init` ve `rollover` bu taşıma için **yasaktır**. İlk talimattaki hesap, strateji, risk, iki bacak, kimlik ve imza korumaları geçerlidir. Önceki talimatı ve run-005 kanıtlarını değiştirme; bunların hash'leri geçmiş kanıt paketine dahildir.

1. **Bu turun yetkisini sınırla ve rapor anlamlarını düzelt.**

   Yalnız yerel hedefli kod/test düzeltmeleri, takvim verisi hazırlama ve kampanya devamının yerel tasarım/doğrulama araçları yetkilidir. SSH, kaynak/hedef sunucu, gerçek MT5, broker, scheduled task, credential, kurulum, transfer ve gerçek release imzalama işlemi yok. Yeni paket yayımlama veya eski v16 paketini kullanarak devam etme.

   Tek izinli ağ faaliyeti 8. maddede verilen resmî, herkese açık takvim belgelerinin salt okunur alınmasıdır. Yerel regresyonlarda ağ ve gerçek MT5 bağlantısı kapalı kalacak. Yeni takvim kütüphanesi/servisi, genel refactor veya alternatif deployment yolu ekleme.

   [run-005 readiness:6](C:/Users/ISAAC/Documents/otobacktestprojesi/outputs/super1_readiness_20260831/run-005/readiness.json:6) içindeki `source_fenced=PASS` Git/diff bilgisine dayanıyor; kaynak sunucunun kapalı olduğuna ilişkin kanıt değil. Yeni raporda bu veriyi `source_code_integrity` alanına taşı; **`source_fenced.status=NOT_VERIFIED`** yaz. Kullanıcı kararlarını `user_decisions` altında kaydet; `target_environment.status=NOT_VERIFIED` kalsın. Kullanıcı beyanıyla operasyonel doğrulamayı karıştırma.

2. **Ortak döngüde veri zamanı ile gönderim zamanını ayır.**

   [run_capital_forward.py:1271](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_capital_forward.py:1271) veri çekimini `cycle_started_at` ile sınırlıyor; [satır 1297](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_capital_forward.py:1297) prefix'i daha sonraki `evaluation_now` için üretiyor. İki bacak çekilirken yeni kapanmış bar sınırı aşılırsa henüz çekilmemiş dakika DATA_INVALID snapshot'ına girebilir; aynı cutoff'un dosyası tekrar hesaplanmadığından geçici yetişememe kalıcılaşabilir.

   Tek çözüm: döngü başındaki `cycle_started_at` aynı zamanda iki bacağın **ortak veri as-of zamanı** olsun; fetch, closed-cutoff ve `run_prefix` bu aynı zamana bağlı kalsın. Gönderim ve sonlandırma için gerçek güncel saat ayrı alınsın. Prefix tamamlanınca yeni bar sınırı aşılmışsa gönderim guard'ı bunu stale olarak ertelesin; sonraki döngü yeni veriyle yeni cutoff'u üretsin. Güncel saati kullanabilmek için çekilmemiş veriyi var sayma veya immutable snapshot üzerine yazma. Gerçek eksik/çelişkili piyasa verisini de geçerli yapma.

   [Satır 1301](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_capital_forward.py:1301) `send_guard_at` değişkenini yalnız reconcile dalında atıyor; [satır 1336](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_capital_forward.py:1336) koşulsuz kullanıyor. `reconcile_orders` olmayan client için bu değer açıkça `None` olsun, JSON'da null yazılsın; `NO_ORDER_TRANSPORT` sonucu exception vermesin. Olmayan bir gönderim kontrolü yapılmış gibi timestamp üretme.

   Kabul: gerçek BarStore/aggregation/prefix ile NQ çekimi 10:23:50, SPX çekim sonu 10:24:10 NY olacak şekilde saat ilerlesin. İlk tur yeni send=0; takip eden tam verili tur uygun aday için send=1; geçici fetch yetişmemesi yeni kesiti kalıcı DATA_INVALID yapmasın. Ayrıca reconcile metodu olmayan client ile tam `run_once` hatasız tamamlanmalı. Test edilen fetch/prefix bağlantısını mock ederek bu iki testi geçirme.

3. **SDK öncesi erteleme/sona erme durumlarını eksiksiz ve atomik kaydet.**

   [run_xm_mt5_forward.py:1719](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_xm_mt5_forward.py:1719) ikinci zaman guard'ında WINDOW_EXPIRED için yalnız event yazıyor; önceden alınmış intent `INTENT` kalabiliyor. Sonraki döngü hiç gönderilmemiş emri brokerda arayıp bütün sistemi UNKNOWN ile kilitliyor.

   SDK hiç çağrılmadan pencere kapanırsa intent, outbox ve neden kaydı aynı transaction ile **terminal no-send** durumuna geçsin. Stale prefix için `PRE_SEND_DEFERRED` ayrı kalsın; yalnız kesin hiç-gönderilmedi kanıtıyla yeniden alınabilsin. `WINDOW_NOT_OPEN` yanlış tarihli/eski adayın otomatik yeniden oynatılmasına izin vermesin. Tamamlanmış veya belirsiz gönderilmiş intent hiçbir yoldan tekrar gönderilebilir yapılmasın.

   Zaman guard'ını yalnız [satır 1705](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_xm_mt5_forward.py:1705) seviyesinde bırakma. Kalıcı intent işlemleri ve hesap/terminal izin sorguları gecikebilir. Yeni pending'in SDK çağrısına en yakın noktada, izin sorgularından sonra güncel saatle son kontrolü yap. Kontrolün ardından ilave broker sorgusu, disk/outbox işi veya bekleme koyma. Send-attempt işareti kalıcı olmalı; bu işaretten sonraki crash kesin gönderilmedi diye yorumlanamaz. Son guard kesin no-send ürettiyse yalnız o canlı çağrı akışının kanıtıyla doğru terminal/deferred kaydına geç. İptal çağrıları bu yeni-emir guard'ına takılmamalı.

   Kabul: NQ 10:29:59'da girsin, `order_check` saati 10:30:01'e taşısın: yeni SDK send=0, NQ intent terminal no-send. Sonraki 10:35 SPX geçerli adayı bundan dolayı UNKNOWN'a düşmeden 1 kez gönderilsin. Aynı sınır geçişini izin sorgusu ve intent/outbox gecikmesinde de test et. Stale→fresh aynı order_id için toplam send=1; crash/paralel süreçte de en çok 1. Test saati, guard'a sabit bir `send_now` vererek dondurulmasın.

4. **Broker yaşam döngüsünü position_id ve giriş/çıkış rolleri üzerinden düzelt.**

   [XM:879](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_xm_mt5_forward.py:879) yalnız giriş order ticket'ına ait deal'leri seçiyor; normal çıkışın farklı order ticket'ı eleniyor. [Satır 951](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_xm_mt5_forward.py:951) tüm deal yönlerini giriş yönüyle karşılaştırıyor; normal karşı yön kapanışını da reddedecek. [Satır 993](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_xm_mt5_forward.py:993) korumalı açık pozisyon görünce kısmi dolum ayrımından önce dönüyor.

   Önce sahipliği doğrulanmış giriş order/deal zincirinden `position_id` bul. Ardından aynı position_id'nin giriş ve çıkış deal'lerini getir; exit order ticket'ının entry order ticket'ıyla eşit olmasını şart koşma. Giriş/çıkış rolüne göre yön kontrolü yap. Kaynak emir sahipliğini doğruladıktan sonra bağlı SL/TP çıkışlarını salt comment veya magic farklılığı yüzünden sessizce atma; tüm kimlik/symbol/pozisyon tutarlılığını denetle. Başka pozisyonun deal'ini katma. MetaQuotes, order-ticket ve position-id sorgularını ayrı tanımlar; örneğinde aynı pozisyonun giriş ve çıkış order ticket'ları farklıdır. [history_deals_get](https://www.mql5.com/en/docs/python_metatrader5/mt5historydealsget_py), [deal özellikleri](https://www.mql5.com/en/docs/constants/tradingconstants/dealproperties).

   İstenen hacim, gerçekleşen giriş hacmi, kalan pending hacmi, toplam çıkış hacmi ve açık pozisyon hacmini ayrı tut. Kısmi dolum, SL/TP doğru olsa da tam dolum değildir. Pending varsa erken dönüp eşzamanlı dolumu/pozisyon korunmasını atlama. Hacim karşılaştırmaları broker volume_step hassasiyetine bağlı olsun; eksik/değişmiş alanlar için istek değerini varsayılan kullanıp eşleşme uydurma.

   `CLOSED_SL/CLOSED_TP` yalnız broker çıkış kanıtı + gerçekleşmiş giriş/çıkış hacim dengesi + ilgili açık pozisyon ve kalan pending yokluğu ile verilsin. Tam giriş deal'i görünüp pozisyon/çıkış kanıtı henüz görünmüyorsa korunma doğrulanmış sayılmasın; izleme ve yeni-emir blokajı sürsün. Partial/reversal/close-by veya karışık çıkış nedeni mevcut risk sözleşmesiyle açıklanamıyorsa açıkça unresolved kalsın. Risk R kuralını değiştirme; broker kapanış kaydı ile risk sonucunun kanıtı ayrı alanlardır.

   Kabul: sentetik entry order=1001, BUY entry deal=2001, position_id=3001; TP veya SL için SELL exit deal=2002, exit order=1002. LONG ve SHORT için kapanış doğru bulunmalı. İstenen hacmin yarısıyla korumalı açık pozisyon PARTIAL kalmalı; yarım çıkış CLOSED olmamalı. Başka position_id, gecikmeli exit, eksik position alanları ve pending+partial birlikteliği negatif senaryoları da çalışmalı.

5. **Tamamlanmış emirleri 35 gün sonra yeniden UNKNOWN yapma.**

   [XM:1054](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_xm_mt5_forward.py:1054) bütün intent'leri sürekli tarıyor; geçmiş sorguları [satır 863](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_xm_mt5_forward.py:863) ve 883'te yalnız 35 gün. Tamamlanmış SUBMITTED/LINKED_EXISTING eskiyince broker sonucu görünmez ve yeni işlemler bloke olabilir. Aynı kampanya devamında bu kabul edilemez.

   Doğrulanmış terminal broker kanıtını kalıcı, denetlenebilir şekilde sakla; ekonomik işlem kimliği/idempotency kaydı süresiz kalsın. Kanıtlanmış kapalı/iptal/expired/rejected intent'i aktif uzlaştırma kuyruğundan çıkar; eski kaydı silme veya yeniden gönderilebilir yapma. Her döngüde açık broker order/position envanteri ayrıca denetlensin; kayıtsız/uyuşmaz exposure eski terminal etiketiyle gizlenmesin.

   Terminal kanıtı olmayan eski intent için giriş ticket/position kimliğiyle tam ilgili history'yi sorgula; 35 günlük kesitin boşluğu flat veya kapanmış anlamına gelmez. Gerçek tarihçe bulunamıyorsa unresolved blocker kalsın. Kaynak kampanyadan taşınan doğrulanmış terminal kayıtları da aynı kuralla korunmalı.

   Kabul: tarih aralığı filtresine gerçekten uyan fake broker kullan. Kanıtlı kapanış/iptalden 36 gün sonra restart'ta terminal kayıt aynı kalsın ve yeni geçerli aday çalışsın. Kanıtsız 36 günlük intent yeni gönderimi engellesin; bunun kaydı silinerek test geçilmesin. Broker okumaları büyüyen bütün kampanya için her döngüde ayrı ayrı tam tarihçe indiren sınırsız bir iş döngüsüne dönüşmesin.

6. **Kısmi ve belirsiz SDK sonucunu kesin ret/fatal duruş sayma.**

   [XM:1760](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_xm_mt5_forward.py:1760) PLACED/DONE dışındaki her sonucu SEND_REJECTED yapıp fatal yükseltiyor; [satır 1072](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_xm_mt5_forward.py:1072) SEND_REJECTED'i uzlaştırmadan çıkarıyor. Önceki talimatın bu maddesi kapanmamıştır.

   | SDK sonucu | Zorunlu davranış |
   | --- | --- |
   | PLACED / DONE | Cevap alınmış, henüz yürütme teyitsiz; 4. maddedeki broker kanıtıyla ilerle. |
   | DONE_PARTIAL | Kısmi/henüz uzlaştırılmamış ekonomik etkiyi kaydet; tamamını veya kalanı otomatik tekrar gönderme. |
   | None, send sırasında exception, timeout/connection veya tanınmayan belirsiz sonuç | Kalıcı SEND_UNKNOWN; yeni emir yok; broker erişilebildikçe uzlaştırma sürer. |
   | Belgelenmiş kesin request reddi | Ayrı ret nedeni/retcode; o isteği tamamlanmış ret olarak kaydet, başarı sayma. Başka mevcut exposure'un izlenmesini kesme. |

   Kodları mevcut MT5 sabitleri ve resmî anlamlarıyla sınıflandır; brokerdan hiç cevap alınmamış durumu cevaplı ret gibi yazma. Beklenen gönderim belirsizliği daemon'un izleme döngüsünü fatal latch ile bitirmesin. Kimlik, imza veya veri bütünlüğü ihlallerinin mevcut kritik durdurma davranışı korunur. [MetaQuotes dönüş kodları](https://www.mql5.com/en/docs/constants/errorswarnings/enum_trade_return_codes).

   Kabul: fake broker ekonomik emri oluşturduktan sonra sırayla None/timeout/connection/DONE_PARTIAL döndürsün; history sonraki döngüde görünsün. SDK yeni-entry çağrı sayısı toplam 1; izleme devam etsin; doğru state'e uzlaşılsın. Partial testi PLACED döndürerek bu gereksinimi karşılayamaz. SDK hiç çağrılmamış order_check hatasıyla SDK sonrası belirsizliği karıştırma.

7. **Yeni emir blokajı, mevcut tehlikeli pending'in iptalini atlamasın.**

   [XM:1961](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_xm_mt5_forward.py:1961) unsafe erken dönüşü DATA_INVALID/pencere/diğer cleanup yollarından önce. [test_xm_mt5_forward.py:1685](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/tests/test_xm_mt5_forward.py:1685) yanlış SL'li pending'in açık kalmasını `assert mt5.pending` ile doğru kabul ediyor. Bu test beklentisi düzeltilmelidir.

   Döngüyü mantıksal olarak broker envanteri/uzlaştırma → güvenli pending cleanup → yeni giriş kararı sırasına bağla. Sahipliği kesin bilinen unsafe kendi pending'i, DATA_INVALID, pencere sonu veya diğer mevcut iptal gerekçelerinde iptal edilsin ve brokerdan teyit alınsın. UNKNOWN nedeniyle bilgi eksikse bilmediğin emri/pozisyonu kapatma; bilinen kendi pending için mümkün olan güvenli iptali dene, sonuç belirsizse bunu koru. Yeni-entry çağrısı engelli kalmalı.

   Kabul: yanlış SL + DATA_INVALID ve yanlış SL + pencere sonu senaryolarında yeni send=0, bilinen pending iptal/geri okuması tamamlanmış olsun. İptal ile dolum yarışında CANCELLED/flat yazılmasın; pozisyon görünür kalsın, SL/TP doğrulanıp alarm durumu kaydedilsin. Otomatik market kapatma, SL genişletme veya risk artırımı ekleme. Bu madde için yanlış eski assertion'ın değiştirilmesi yetkilidir; başka test beklentilerini sayıyı tutturmak için değiştirme.

8. **RTH takvimini gerçek kaynakla mühürle; yalnız config'e verified yazma.**

   [Super1:277](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_super1_xm_mt5_forward.py:277) mevcut parser'da tarih/start/end yeterli; kaynak/hash doğrulanmıyor, duplicate tarihler ezilebiliyor, mevcut girdide coverage kontrolü atlanıyor. `verified=true` bunu düzeltmez. Takvim eksikliği bütün adayları değil, overnight feature gereken koşullu filtre dalını bloke eder; raporu bu etki sınırıyla yaz.

   Kaynaklar: [Nasdaq resmî 2026 takvimi](https://www.nasdaqtrader.com/Trader.aspx?id=Calendar) ve [NYSE resmî saat/tatil takvimi](https://www.nyse.com/trade/hours-calendars). Normal RTH 09:30–16:00 New York'tur. 2026 tam kapalı tarihler: 01-01, 01-19, 02-16, 04-03, 05-25, 06-19, 07-03, 09-07, 11-26, 12-25. Erken kapanış: 11-27 ve 12-24, saat 13:00. **2026-07-02 normal 16:00 kapanıştır; tahmini 13:00 ekleme.** Resmî kaynaklar arasında uyuşmazlık varsa otomatik seçim yapma; açık blocker yaz. XM CFD işlem saatleri ile bu ABD nakit hisse RTH takvimi ayrı kavramlardır.

   Tek üretim biçimi kullan: `work/backtest/live_forward/calendars/us_equity_rth_2026.json`. Runtime'daki `rth_session_calendar` bunun repo-içi `path`, ham `sha256` ve `calendar_id` referansını tutsun. Takvim dosyasında `schema_version`, `calendar_id=US_EQUITY_RTH_2026`, `timezone=America/New_York`, `coverage.start/end`, kaynak kayıtları ve tarih bazlı seanslar bulunsun. Her takvim günü tam bir kez yer alsın; state yalnız OPEN/EARLY_CLOSE/CLOSED, açık günde start/end zorunlu, kapalı günde boş olsun. List/dict/sparse takvim alternatiflerini üretimde kabul etme. JSON duplicate key/date, yanlış saat/zone, coverage dışı entry ve eksik günlük kayıt reddedilsin.

   Resmî kaynakların indirilen bytes'ını ayrı provenance dosyalarında sakla; URL, UTC erişim zamanı ve SHA256 kaydet. En az 2026 takvim yılını tam kapsa; mevcut kampanya için gerçekten gereken daha eski previous-session girdisi varsa aynı şekilde resmî kanıtla ekle, varsayma. Kapsamın öncesine/sonrasına taşan sorgu kesin unresolved olsun. Kaynak URL metni veya bir verified bayrağı tek başına kanıt değildir. Çıktı takvimi iki kaynaktaki kapalı/erken kapanış kayıtlarıyla bağımsız karşılaştıran doğrulama raporu üret.

   Runtime takvimin ham hash'ini config'teki değerle doğrulasın; path repo dışına/reparse hedefe kaçamasın. Takvim referansı ve kaynak dosyalarının yeni release payload/provenance gereksinimlerini ayrıca listele. Testte kaynak JSON ve beklenen sonuç aynı fonksiyondan üretilmesin. Hatalı runtime takvimi ham parser exception'ı yerine açık NO_SEND gerekçesine dönsün; takvim feature gerekmeyen dalı yeni genel strateji filtresine dönüştürme.

   Kabul: 2026-08-31→önceki 08-28; 09-08→önceki 09-04 (09-07 tatil); 11-30→önceki 11-27 kapanış barı 12:59; 12-28→önceki 12-24 kapanış barı 12:59. 07-02 kapanış barı 15:59 olmalı. Kaynaksız/değişmiş hash, duplicate, eksik coverage ve doğrulanmamış tarih negatifleri send=0. DST öncesi/sonrası NY saati aynı kalsın, UTC karşılığı doğru değişsin. Calendar kaynak belgelerinden kanıt alınamayan senaryoyu PASS ilan etme.

9. **Gerçek Super1 giriş noktasını kullanan regresyon matrisiyle bitir.**

   [Mevcut yeni test:444](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/tests/test_super1_xm_forward.py:444) override'a ulaşıyor fakat prefix/send context vermiyor; [XM:1340](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_xm_mt5_forward.py:1340) prefix yokken ALLOW döndürüyor. Bu, üretimdeki zaman/takvim zincirinin testi değildir. Üretim strateji gönderiminde prefix eksikliği fail-closed olsun; mevcut ayrı demo smoke yolu bu kontrolü aşmak için kullanılmasın.

   Gerçek `Super1XmMt5DemoOrderClient.reconcile_orders → _place_candidate → _pending_request → SDK sınırı` çalışsın. `_filter_state`, guard, `_broker_execution_chain` ve request üretimini mock etme. Yalnız saat, piyasa/broker sınırı ve geçici depolar kontrol edilsin. İki gerçek frozen BLOCK kuralı; takvim gereken ALLOW ve UNRESOLVED; iki bacak/yön/risk ölçeği; NQ bittikten sonra 10:33 ve 10:45 SPX; SDK aşamalarında saat ilerlemesi aynı zincirde doğrulansın. ALLOW fixture'ını yalnız overnight dalını atlayan fiyatlardan seçme.

   Maddeler 2–8 için ayrı isimli regresyonlar ekle ve raporda gereksinim→test adı→broker çağrı sayısı→kalıcı state eşleştirmesi yap. Hata önce mevcut bytes üzerinde gözlenmiş olmalı; eski sürümü çalışan repo üzerine geri yazmadan izole fixture/diff uygulamasıyla kanıtla. Yerel bağımlılıklar aynı kalsın. Değişen testler, tam suite ve compileall son bytes üzerinde çalışsın; JUnit/komut/exit code/stdout/stderr saklansın. 279'u yeni hedef sayı yapma; gerçek test envanteri büyüyebilir.

10. **Mevcut kampanyanın devamı için sürümlü geçiş protokolünü hazırla.**

    [core.campaign_lock:244](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_capital_forward.py:244) runtime/harness/hesap sözleşmesini tam eşitlikle doğruluyor. Yeni runner bytes'ı ve takvim config'i eski kilitle eşleşmeyecek. Kullanıcının devam kararı, hash kontrolünü devre dışı bırakma veya eski lock'u yeni hash'lerle yeniden yazma izni değildir.

    Seçilen tasarım: **orijinal campaign_lock değişmeden kalır; aynı kampanyaya onaylı sürüm geçiş kaydı eklenir.** Bu tur yalnız yerel migration planlayıcısı, belge şeması, doğrulayıcısı ve fixture testleri hazırlanacak. Normal daemon'un kabul kapısını henüz bu yeni protokole açma; gerçek kaynak snapshot'ı ve imzalı geçiş yetkisi ayrıca incelenecek. Bu sınırlama kampanyanın sıfırlanmasına izin vermez.

    Geçiş kaydı şu alanları bağlasın: schema_version; orijinal campaign_lock SHA256; orijinal created_at/kampanya kökeni; önceki geçiş kaydının hash'i; eski/yeni release kimliği ve runtime/harness/contract/calendar hash'leri; değişmeyen engine/frozen candidate/strateji-risk parametre hash'leri; aynı broker hesap/server kimliği; kaynak state snapshot manifest hash'i; state şema sürümleri; bu belgede izin verilen değişiklik listesi; mevcut release güven köküne bağlanan imza bilgisi. Gerçek hesap kimliğini kamuya açık rapora koyma.

    Eksik/değişmiş imza veya kök lock, kopmuş/tekrarlanmış geçiş zinciri, farklı hesap, değişmiş risk/aday veya kayıp state reddedilsin. Sırf dosyaları hash'lemek geçiş yetkisi oluşturmasın. Planlayıcı saf bir yerel dry-run olmalı: kaynak bytes değişmez, broker/network çağrısı yok, task yok, default init yok. Gerçek kaynak sürümünü yerel çalışma ağacından tahmin etme. Kaynak sunucunun güncel mühürlü snapshot'ı yoksa `SOURCE_SNAPSHOT_NOT_VERIFIED` yaz; sentetik test kanıtını kaynak kanıtı sayma.

    Gelecekte runtime entegrasyonunun kabul kuralı: root lock ve orijinal created_at korunur, etkin runtime sözleşmesi yalnız doğrulanmış zincirden türetilir. Kampanya kimliği/sayaçları ve eski event/prefix/final/manual kayıtları değişmez; eski kayıtlar kendi eski code/data hash'leriyle kalır. Yeni kod dönemi mevcut kampanyanın sürüm kaydıyla ayrılır; eski günlere yeni sürüm uygulanmış gibi geçmiş kanıt yeniden etiketlenmez. `proven=false` ve tarihsel parity durumu değişmez.

11. **State ve risk sürekliliğini salt dosya kopyasına indirgeme.**

    Devam planının snapshot envanteri: campaign_lock; SQLite minute bars/conflicts; order_intents; order_event_outbox; broker_execution_states; order/deal event günlükleri; prefix/finalized/manual kararlar; sağlık/fatal/recovery kayıtları; mevcut sayaçlar ve son risk sonucunu açıklayan broker deal kimlikleri. Açık SQLite'ın yalnız ana dosyasını kopyalama; tutarlı SQLite backup/snapshot ve WAL işlemi tanımla. Gerçek uygulama/snapshot üretimi bu tur yapılmayacak.

    Yerel fixture'da aynı kampanyayı taşıyan snapshot ile created_at, karar/order_id, gönderilmiş/unknown intent'ler, outbox sırası, deal dedup kimlikleri, tamamlanmış sonuçlar ve son 10 terminal R dizisi aynı kalmalı. Hedefte MT5 history boş/kesilmiş gelirse mevcut kampanya ilk kez başlıyormuş gibi sıfır risk geçmişine geçilemez. [Super1 risk hesabı:388](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_super1_xm_mt5_forward.py:388) için bu devam önkoşulunu test et; eksik geçmiş NO_SEND/continuation blocker olsun. Kendi içinde tutarlı fakat başka kampanyaya ait snapshot da reddedilsin.

    Kaynak kapatılmadan hedefe emir yetkisi açılmayacak. Kaynak fence gerçek main/watchdog görevlerinin devre dışı olması, ilgili süreç yokluğu, diğer restart yollarının kapanması ve reboot sonrası kapalılıkla kanıtlanacak. Hedef için imzalı **prepare-only** kurulum planı mevcut state'i kabul etmeli, ikinci adayı önkoşul yapmamalı ve init/rollover/daemon başlatmamalı. SSH oturumu ile scheduled task kullanıcısını eşdeğer sayma; hedef kullanıcı/DPAPI ve MT5 oturumu hedefte ayrıca doğrulanacak. Bunlar bu tur tasarım ve fixture gereksinimleridir; gerçek işlemler yetkili değildir.

12. **Rollback korumasını davranışsal ve yeniden başlatmaya dayanıklı test et.**

    [Rollover değişikliği:539](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/deploy/rollover_super1_campaign_windows.ps1:539) broker yan etkisi sınırını işaretliyor; fakat [test_deployment_security.py:524](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/tests/test_deployment_security.py:524) yalnız metin varlığını denetliyor. İlk talimattaki hata enjeksiyonu henüz kanıtlanmış değil. Bu betik düzeltilse bile mevcut kampanya taşınmasında kullanılmayacak.

    Mevcut güvenli PowerShell harness ile, gerçek scheduler çağrısı olmadan start timeout, gönderimden sonra watchdog hatası, stop başarısızlığı ve kanıt yazma hatasını çalıştır. Eski/yeni state korunmalı; post-start belirsizlikte otomatik restore/restart olmamalı. Hata sonrasında süreç yeniden yaratılırsa da yeni emir yasağı sürmeli: mevcut launcher/daemon'un gerçekten tükettiği fatal/recovery kapısına kalıcı durum bağla. Yalnız okunmayan bir failure JSON dosyası yazmak yeterli değildir. Latch temizleme/otomatik recovery kısayolu ekleme. Durdurma kanıtı yoksa STOPPED yazma.

13. **Yeni sürüm sözleşmesini ayrı tasarla; eski v16 kapısını gevşetme.**

    İlk rapordaki hash güncellemesi yararlıdır; fakat yeni takvim, yeni yerel araçlar ve regresyonlar sonrasında sözleşme yeniden mühürlenecek. Yalnız izin verilen runtime/calendar/bütünlük alanları değişebilir; hesap, risk, sinyal payload'ı ve frozen strateji aynı kalır. Önceki dosyaların artık değişmediğini test/hash envanteriyle kanıtla.

    [Builder:72](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/deploy/build_signed_windows_release.ps1:72) ve [release_integrity:141](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/deploy/release_integrity.ps1:141) eski 268/139 sözleşmesini koruyor. Bu tur bunları yeni test sayısına körlemesine değiştirme; eski release dosyalarını yeniden imzalama veya overwrite etme. Yeni sürüm önerisinde kesin test dosyaları/test envanteri, gerçek başarılı toplamlar, artifact içinden çalıştırılacak testler, eklenecek calendar/provenance payload dosyaları ve verifier değişiklikleri birlikte listelensin. Basit `>=` test sayısı, bypass flag veya historical exception çözüm değildir. Son paket üretimi ayrı inceleme sonrasıdır.

14. **Yeni kanıt paketiyle teslim et; henüz taşıma yapma.**

    `C:\Users\ISAAC\Documents\otobacktestprojesi\outputs\super1_readiness_20260831` altında boş bir sonraki run dizinine yaz; mevcut run-005 ve daha eski çıktılar değişmez. `report.md`, `readiness.json`, test JUnit/ham çıktıları, son kod/diff/evidence SHA256 manifesti, `rth_calendar_validation.json`, `campaign_continuation_design.md` ve dry-run fixture sonuçlarını teslim et. Her açık bulgunun eski tetiklenmesi, düzeltme ve yeni davranış kanıtı ayrı gösterilsin.

    | Yeni rapor alanı | Kabul edilen anlam |
    | --- | --- |
    | `user_decisions` | Windows+SSH, aynı demo hesap, mevcut kampanya, bağımsız Super1 — kullanıcı tarafından kesinleştirildi. |
    | `source_code_integrity` | Yerel dosya/diff/hash eşleşmesi; kaynak sunucu fence'i değildir. |
    | `software_status` | Ancak maddeler 2–9 ve 12'nin zorunlu davranışsal regresyonları son bytes'ta geçerse PASS. |
    | `calendar_status` | Kaynak, hash, coverage, normal/erken/tatil ve gerçek Super1 feature yolu kanıtlıysa PASS. |
    | `campaign_continuation` | En fazla DESIGN_TESTED; gerçek kaynak snapshot/geçiş yetkisi yokken APPLIED veya VERIFIED olamaz. |
    | `release_contract_status` | Tasarım/inceleme bekliyor; eski ZIP yeni kodun paketi değildir. |
    | `source_fenced`, `target_environment` | NOT_VERIFIED. |
    | `broker_preflight`, `pending_accept_cancel`, `strategy_fill_exit` | Gerçek ortam için NOT_RUN. Fixture sonuçları ayrı alanlarda. |
    | `overall_status` | Bu tur NO_GO; yerel PASS operasyonel izin üretmez. |

    Tarihsel parity/proven bayraklarının false kalması doğru davranıştır; bunları teknik geçiş kapısını geçmek için true yapma. Bu bayraklar strateji performansının kanıt durumu olup broker altyapısının çalışmasıyla aynı şey değildir. Hedefte gerçek emirsiz doğrulama, onaylı demo pending testi, doğal Super1 yürütmesi ve reboot/reconnect kanıtı sonraki ayrı aşamadır. Mevcut Super1 magic ile keyfî fill/manuel kapatma testi yaparak risk geçmişini kirletme.

    Kapsam içindeki yerel işleri tamamla; yeni davranış kararı gereken konuyu gerekçe ve dosya/satırla raporla. Yukarıdaki kesin kullanıcı kararlarını tekrar belirsiz ilan etme. Kanıt paketi mimar tarafından incelenmeden kurulum, kaynak durdurma veya hedef başlatma aşamasına kendiliğinden geçme.
