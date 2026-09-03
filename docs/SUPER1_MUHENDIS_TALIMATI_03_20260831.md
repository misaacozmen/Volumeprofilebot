**Super1 — üçüncü mühendis talimatı; run-006 denetimi — 31 Ağustos 2026**

**Karar NO_GO. run-006 için yazılımın hazır olduğu onaylanmıyor.** Hash manifestindeki 31 dosya mevcut bytes ile eşleşiyor; kayıtlı JUnit sonuçları 139 hedefli / 288 toplam, hata/başarısızlık/skip sıfır. Ancak aşağıdaki üretim yolu hataları bu testlerin dışında kalmış veya hatalı broker taklidiyle örtülmüş. Compileall için sıfır exit code kaydı var; bu mimari denetimde testler yeniden çalıştırılmadı. Kod, kampanya state'i, sunucu veya broker değiştirilmedi.

Bu belge [ikinci talimatın](C:/Users/ISAAC/Documents/otobacktestprojesi/docs/SUPER1_MUHENDIS_TALIMATI_02_20260831.md:1) açık kalan maddelerini ve run-006 düzeltmelerindeki somut hataları kapatır. Önceki talimatları, run-006'yı ve daha eski kanıtları değiştirme. Eski rapordaki PASS alanlarını geriye dönük düzeltme; bu denetimi sonraki koşunun başlangıç bulgusu olarak kaydet.

Kapanmış işleri geri alma: no-transport yolundaki `send_guard_at=None`; ortak prefix veri cutoff'u; SDK öncesi son zaman kontrolü ve hiç gönderilmemiş süresi dolan intent'in terminalleşmesi; doğrulanmış eski terminal kanıtın korunması; takvimde ham hash/provenance/coverage doğrulaması; `source_fenced=NOT_VERIFIED` ayrımı. Bunların mevcut regresyonlarını koru. Aşağıdaki kabul listesi tamamlanmadan yalnız test sayısının artmasıyla PASS verme.

1. **Sabit kararları ve bu turun yetkisini uygula.**

   Hedef Windows, SSH mevcut. Yalnız Super1 taşınacak; aynı XM MT5 demo hesabı ve mevcut kampanya devam edecek. İkinci adayla hesap, terminal, Windows kullanıcısı veya çalışma bağımlılığı kurulmayacak. Bu kararları yeniden sorma.

   Yalnız yerel, hedefli kaynak/test düzeltmeleri, yerel sentetik fixture/harness çalışmaları ve yeni kanıt/tasarım belgeleri yetkilidir. Gerçek SSH, kaynak/hedef state okuma veya snapshot alma, MT5 initialize/login, broker çağrısı/emri, scheduled task, credential, kaynak durdurma, kurulum, transfer, paket üretimi/yayımlama ve gerçek release imzalama yok. Bu belgedeki resmî MetaQuotes/Nasdaq/NYSE belgelerinin herkese açık salt okunur erişimi serbesttir; regresyonlarda dış ağ ve gerçek MT5 SDK bağlantısı kullanılmaz.

   Yeni kampanya, mevcut `campaign_lock.json` dosyasını silme/değiştirme, reset, `init` ve `rollover` bu taşımanın yöntemi değildir. Normal daemon'un kampanya kabulünü yeni continuation protokolüne henüz açma. PowerShell güvenlik düzeltmesi yalnız yalıtılmış harness ile test edilecek; gerçek deployment betiği işletilmeyecek.

   Frozen canonical pair pipeline + Super1 overlay korunur. US100Cash/NQ 3m, New York 09:30–10:30, 3R, günlük en çok 1; US500Cash/SPX 5m, 09:30–11:00, 2.5R, günlük en çok 2; 8 saniye kapanmış bar gecikmesi ve mevcut polling korunur. Baz risk %1, son 10 terminal R'ye bağlı mevcut 0.75/1.1 ölçeği, -1R pair cap ve iki frozen BLOCK kuralı değişmez. Account/server/magic/symbol, strateji payload'ı, engine ve risk formülü değiştirilemez. `proven=false` ve tarihsel parity=false kalır.

2. **E01 — Gerçek MT5 history API sözleşmesini düzelt.**

   [XM:901](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_xm_mt5_forward.py:901) giriş order deal'lerini `history_deals_get(order=...)` ile arıyor. Resmî Python API'de bu sorgu `history_deals_get(ticket=entry_order_ticket)`, pozisyon zinciri sorgusu `history_deals_get(position=position_id)` biçimindedir. `ticket` burada deal ticket'ı değildir. [MetaQuotes API](https://www.mql5.com/en/docs/python_metatrader5/mt5historydealsget_py).

   Üretim çağrısını düzelt. [Fixture:1889](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/tests/test_xm_mt5_forward.py:1889) aynı yanlış `order=` parametresini kabul ediyor; fixture'ı SDK'nın belgelenmiş çağrı biçimleriyle sınırla. Bilinmeyen keyword başarılı koleksiyon döndüremez. Tarih sorguları gerçekten aralığı, ticket sorguları entry order'ı, position sorguları aynı pozisyonun tüm bağlı deal'lerini filtrelesin. `None` okuma hatası ile boş tuple farklı kalsın.

   Kabul: gerçek istemci ve kalıcı store üzerinden pending → entry order 1001 / deal 2001 / position 3001 → exit order 1002 / deal 2002 akışı çalışsın. LONG/SHORT ve SL/TP ayrı durumlar olsun. Çağrı izi doğru keyword'leri kanıtlasın; yanlış `order=` kullanan önceki kaynak aynı fixture'da başarısız olsun. Sahiplik, position kimliği ve history zincirini mock ederek geçme.

3. **E02 — İlk emir hacmi ile kalan hacmi birbirine eşitleme.**

   [XM:820](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_xm_mt5_forward.py:820) `volume_current` değerini isteğin ilk hacmiyle karşılaştırıyor; [satır 916](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_xm_mt5_forward.py:916) aynı kontrolü history order'a uyguluyor. Tam dolan emirde initial=0.10/current=0 veya kısmi dolumda initial=0.10/current=0.06 olması normaldir. [Fixture:1832](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/tests/test_xm_mt5_forward.py:1832) history kaydından `volume_current` alanını çıkararak bu hatayı gizliyor. [MetaQuotes hacim alanları](https://www.mql5.com/en/docs/constants/tradingconstants/orderproperties).

   İstek hacmini order'ın `volume_initial` alanıyla doğrula. Kalan pending, gerçekleşmiş giriş, gerçekleşmiş çıkış, iptal edilmiş kalan ve açık pozisyon hacimlerini ayrı değerlendir; doğrulanmış ekonomik zincirde hacim dengesi kur. Broker volume_step hassasiyeti kullan; eksik alanı istek değerinden doldurma. Broker order/deal/position fixture'ları gerçek alanları ve yaşam döngüsündeki değer değişimini taşısın; aynı nesne tipinde bulunması gereken alanları test kolaylığı için kaldırma.

   Kabul: initial=0.10/current=0, entry toplamı=0.10 ve doğru SL/TP'li açık pozisyon=0.10 → `OPEN_PROTECTED`. Initial=0.10/current=0.06, entry=0.04 ve korumalı pozisyon=0.04 → partial; tam dolum değildir, tamamı/kalanı tekrar gönderilmez. Kısmi çıkış kapanmış sayılmaz; yalnız tamamlanmış giriş/çıkış dengesi ve ilgili açık pozisyon/pending yokluğu terminal kapanış sağlar. Kimliği/hacmi eksik veya uyumsuz zincir yeni girişi engeller.

4. **E03 — Pending iptalini mevcut pozisyonun kapanışıyla karıştırma.**

   [XM:2175](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_xm_mt5_forward.py:2175) iptalden sonra aynı magic'te herhangi bir pozisyon varsa `CANCEL_FILL_RACE_EXPOSURE` ve fatal exception üretiyor. Önceden bilinen korumalı kısmi pozisyon veya başka bir kendi emrine ait pozisyon, bu emrin iptal sırasında dolduğunun kanıtı değildir.

   İptal öncesi/sonrası aynı order → position zincirini uzlaştır. Pending kalanın kaldırıldığının teyidi ile ekonomik pozisyonun durumunu ayrı kaydet. Entry'nin tamamını CANCELLED/CLOSED/FLAT yapma. İptal sonrası pozisyonun varlığı tek başına fatal değildir; doğru SL/TP'yi doğrula, mevcut izleme ve yeni-emir güvenlik kapısını sürdür. Gerçek fill/cancel yarışı veya belirsiz REMOVE sonucunda belirsizliği koru; broker erişildikçe uzlaştır. Kimlik/bütünlük ihlallerinin kritik durdurma davranışını gevşetme.

   Kabul: 0.10 isteğin 0.04'ü korumalı pozisyon, 0.06'sı pending; kalan başarılı REMOVE ve geri okumayla yok olsun. Fatal yok, yeni entry=0, 0.04 exposure görünür ve izlenir; intent yanlış terminalleşmez. Aynı magic'teki ilgisiz pozisyon iptal edilen emre bağlanmaz. Ayrıca gerçek yarış, kaybolmuş geri okuma ve korunması doğrulanamayan pozisyon testleri olsun. Otomatik market close, SL genişletme veya yeni risk ekleme.

5. **E04 — UNKNOWN/PARTIAL aynı döngünün sonraki adayını da durdursun.**

   [XM:2315](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_xm_mt5_forward.py:2315) broker güvenliğini bir kez hesaplıyor. [Aday döngüsü:2428](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_xm_mt5_forward.py:2428) ilk adayın `SEND_UNKNOWN_NO_SEND` veya `SEND_PARTIAL_NO_SEND` sonucundan sonra ikinci order_id'yi göndermeye devam edebiliyor.

   Her aday sonucundan sonra gönderim yetkisini yeniden değerlendir. Belirsiz/kısmi ekonomik etki kalıcı olarak çözülmeden aynı kampanyanın başka yeni emri gönderilemez; bu kontrol yalnız aynı order_id'nin dedup kontrolü olamaz. Kalıcı intent/broker-state kanıtı sonraki turda, yeniden oluşturulan istemcide ve SDK sınırına ulaşan diğer giriş yollarında da uygulanmalı. Kontrol ile intent alma arasında başka süreçte oluşan blokaj atlanmamalı. Beklenen UNKNOWN nedeniyle izlemeyi fatal latch ile bitirme; güvenli pending cleanup ve pozisyon takibi devam etsin.

   Mevcut aday seçme koşullarını geçmiş bütün `candidates` için `active_comments` kümesini gönderim döngüsünden önce oluştur; bütün tarihsel `payload.decisions` listesini aktif sayma. Döngü güvenlik nedeniyle kesildiğinde henüz işlenmemiş fakat hâlâ geçerli adayların mevcut pending emirlerini yanlış `NO_LONGER_ACTIVE_PREFIX` gerekçesiyle iptal etme. Mevcut bağımsız DATA_INVALID/pencere/unsafe iptal kuralları yürürlükte kalır.

   Kabul: aynı gerçek prefix'te iki uygun farklı order_id. İlk SDK çağrısı emri broker fixture'ında oluşturup sırayla None/timeout/connection/DONE_PARTIAL döndürsün; ikinci adayın yeni entry çağrısı=0, toplam yeni entry=1. Sonraki tur ve client/store restart'ında belirsizlik sürdükçe sayı artmasın. Broker kanıtı geldiğinde ilk emir yeniden gönderilmeden doğru state'e uzlaşılsın; ikinci karar yalnız hâlâ güncel/uygun ve tüm kapılar açık olduğunda değerlendirilsin. Geç kalmış karar geriye dönük gönderilemez.

6. **T01/T02 — Veri ufku sabit kalsın; gerçek zamanları geriye yazma, erken final mühürleme yapma.**

   [Core:836](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_capital_forward.py:836) broker gözlem zamanını `min(known_time, chunk_end)` ile geriye çekiyor. [Satır 993](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_capital_forward.py:993) karar üretim zamanını artık döngü başlangıcı olan `now` yapıyor. Bunlar ortak veri cutoff'u için gerekli değildir; fiilî gözlem/üretim kanıtını bozar.

   Dört zamanı açık ayır: (a) iki bacağa ortak `market_data_asof=cycle_started_at` ve istenen fetch aralığı; (b) gerçek provider observation/first-known zamanı; (c) hesaplama bitince alınan gerçek `decision_produced_at/recorded_at`; (d) gerçek send/finalization audit zamanı. Market bar seçimi ve closed cutoff yalnız (a)'ya bağlı kalsın. (b)/(c)'yi cutoff'a kırpma. Broker verisi karar verilmeden bilinmiş olmalı; son send guard gerçek güncel zamanla stale/expired kontrolünü korumalı.

   [Core:1320](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_capital_forward.py:1320) eski fetch kapsamını daha ileri finalization saatiyle sonlandırıyor; [satır 1146](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_capital_forward.py:1146) kalıcı marker yazıyor. Çözüm: finalization'a gerçek saatle birlikte doğrulanmış ortak fetch kapsamını geçir. Her iki bacağın gerekli final veri cutoff'u başarılı fetch kapsamı içinde değilse `WAIT_FOR_FINAL_DATA` dön; final attempt/marker oluşturma. Gerçekten tamamlanmış kapsamda eksik/çelişkili veri varsa mevcut DATA_INVALID/critical kuralları sürsün. Bilinen veri hatasıyla henüz istenmemiş dakikayı aynı şey sayma.

   T01 kabulü: gerçek fetch → BarStore → aggregation → prefix zincirinde NQ çekimi 10:23:50, SPX çekim sonu 10:24:10 NY; aynı veri as-of korunur, first-known ve produced-at gerçek ilerlemiş zamanı gösterir. Bu fixture'ın verisi yalnız NQ'da yürütülebilir aday üretsin, SPX adayı bulunmasın: ilk tur stale NQ için yeni send=0, sonraki tam/güncel tur uygun NQ adayı için 1. NQ stale diye başka bir testteki taze SPX adayını genel olarak engelleme. Geçmiş gün verisini bugün alan fixture'ın first-known zamanı geçmiş güne çekilemez. Fetch/prefix'i mock etme.

   T02 kabulü: fetch as-of 10:58 NY; gecikme sonrası finalization 11:05:10. Henüz alınmamış SPX 10:59 nedeniyle kalıcı final/invalid marker oluşmasın. Sonraki tamamlayıcı iki bacak fetch'inden sonra aynı gün bir kez finalleşsin; tekrar ALREADY_FINALIZED olsun. Finalize/BarStore/aggregation mock edilmez. Kapsamı tamamlanmış fakat eksik/çelişkili veri ayrı negatif testte fail-closed kalır. Önceki immutable kayıtları güncelleyerek test geçme.

7. **C01 — Doğru takvim verisini koru; iki kaynak karşılaştırmasını gerçekten kanıtla.**

   Mevcut 2026 tatil/erken kapanış listesi resmî [Nasdaq](https://www.nasdaqtrader.com/Trader.aspx?id=Calendar) ve [NYSE saat/tatil tablosuyla](https://www.nyse.com/trade/hours-calendars) uyumlu: 10 tatil; 27 Kasım ve 24 Aralık 13:00 erken kapanış; 2 Temmuz normal kapanış. Bu doğruluk kazanımını geri alma. Bu, XM CFD işlem saatlerinin doğrulandığı anlamına gelmez.

   [Validator:113](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/validate_super1_rth_calendar.py:113) yalnız source ID/HTTPS kontrolünden `independent_source_records=PASS` ve sabit “transcriptions agree” metni üretiyor. Kaynak dosyanın hash doğrulaması, içeriğinin bağımsız okunup karşılaştırıldığı anlamına gelmez.

   İki ayrı kaynak çıkarım kaydı oluştur: `nasdaq-2026-extracted.json` ve `nyse-2026-extracted.json`. Her birinde kendi ham kaynak SHA256'sı, URL'si, çıkarım yöntemi, tatil/erken kapanış kayıtları ve kaynak konumları olsun; Nasdaq için 2026 tablo satırı, [NYSE PDF](https://www.nyse.com/publicdocs/nyse/ICE_NYSE_2026_Yearly_Trading_Calendar.pdf) için sayfa 1/takvim hücresi ve lejantı göster. Görsel/elle okuma kullanılıyorsa açıkça öyle kaydet; otomatik PDF parser çalışmış gibi raporlama. İki listeyi aynı EXPECTED sabitinden çoğaltma.

   Validator bu iki çıkarımı birbirleriyle ve runtime takvimiyle kayıt bazında karşılaştırsın. Kaynak hash bağları yoksa veya tek bir tarih/saat uyuşmazsa semantik karşılaştırma PASS olamaz. Çıkarımlardan birinin gününü/saatini değiştiren negatif test gerçekten FAIL üretmeli. Kaynak bytes, mevcut strict loader ve günlük coverage korunur; yeni takvim kütüphanesi/servisi ekleme.

8. **C02 — Takvim gereken gerçek Super1 filtresini SDK sınırına kadar test et.**

   [Mevcut zincir testi:378](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/tests/test_super1_xm_forward.py:378) gerçek reconcile yoluna girse de ALLOW adayı overnight dalını atlıyor. [BLOCK testi:346](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/tests/test_super1_xm_forward.py:346) filtreyi mock ediyor. [Validator:170](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/validate_super1_rth_calendar.py:170) yalnız overnight helper'ını çalıştırıyor; raporda bunun kapsamını helper testi olarak yaz.

   Gerçek `Super1XmMt5DemoOrderClient.reconcile_orders → _filter_state/_place_candidate → _pending_request → SDK sınırı` çalışmalı. Guard, filtre, risk hesaplama, request üretimi ve broker-chain algoritması mock edilmez. Saat, broker/piyasa sınırı ve geçici depolar kontrol edilir. Frozen feature kanıtına uygun, gerçekten overnight gereken aday kullan.

   Ayrı kabul durumları: frozen Monday-short BLOCK; frozen proximity+overnight-up BLOCK; proximity koşulunda kanıtlı overnight ile ALLOW; eksik önceki RTH barıyla UNRESOLVED. Sırasıyla yeni-entry sayıları 0/0/1/0; tekrar/restart duplicate üretmez. Her iki bacak/yön ve risk ölçeği, NQ bittikten sonraki 10:33/10:45 SPX ve SDK öncesi saat ilerlemesi önceki talimatın gerçek zincir matrisinde kalır. BLOCK/UNRESOLVED testi uygun olmayan prefix nedeniyle zaten NO_SEND'e düşerek geçemez; beklenen gerçek filtre nedenini de assert et.

   Takvim gereken dalda bozuk raw hash, provenance bytes/hash/URL uyuşmazlığı, duplicate JSON key/tarih, eksik coverage günü, kapsam dışı sorgu, yanlış timezone/saat ayrı negatif testlerdir. Çalışan feature hesabındaki takvim okuma/çözümleme hatası ham parser exception'ıyla izlemeyi bitirmesin; belirli NO_SEND nedeni çıkarsın. Bu kural startup'taki mühürlü hash/provenance/bütünlük doğrulamasını yutup başlatma izni vermez; mevcut kritik başlatma engeli korunur. Takvim gerekmeyen dalı yeni genel BLOCK kuralına çevirme.

   Önceki seans örnekleri: 31 Ağustos→28 Ağustos; 8 Eylül→4 Eylül; 30 Kasım→27 Kasım 12:59; 28 Aralık→24 Aralık 12:59; 2 Temmuz son RTH barı 15:59 NY. DST sınırlarında 6/9 Mart ve 30 Ekim/2 Kasım için NY saati sabit, UTC dönüşümü doğru olsun. Bu senaryoların gerçek test node ID'lerini rapora koy; kaynak/helper çıktısını uçtan uca emir testi sayma.

9. **M01 — Devam planlayıcısını gerçekten salt okunur ve kökeni doğru yap.**

   [Planlayıcı:37](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/plan_super1_campaign_continuation.py:37) snapshot yokken kaynak kampanyanın created_at değerini uyduruyor. [Satır 30](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/plan_super1_campaign_continuation.py:30) research manifest hash'ini release, tek XM script hash'ini harness diye etiketliyor. [Satır 79](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/plan_super1_campaign_continuation.py:79) çıktı yolu kaynak lock'a verilirse dosyayı ezebiliyor.

   Snapshot yokken source-created-at/root lock/eski sürüm/önceki transition/snapshot kimliği unresolved/null kalsın; planın gerçek oluşturulma zamanı ayrı alanda olsun. Yerel beklenen hesap/strateji kimliği ile kaynaktan gözlenen kimliği ayrı tut. Bilinmeyen ilk transition durumunu kanıtlanmış genesis sayma. `safe_to_apply=false`, `apply_performed=false`, `SOURCE_SNAPSHOT_NOT_VERIFIED` korunur.

   Hash adları mevcut sözleşmeyle aynı anlamı taşısın: raw file hash ham bytes; harness, Super1 için yapılandırılmış [core.harness_hash](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_capital_forward.py:231) kapsamı/sırası; frozen config kendi mevcut algoritması; runtime/contract/calendar kendi doğrulanan bytes'ı. Varsayılan Capital runtime kapsamını Super1 diye hash'leme. Henüz üretilmemiş/imzalanmamış release kimliği null kalsın; research manifest ayrı aday kanıtıdır. JSON hash algoritmalarını tek ad altında değiştirip eski hash'leri geçersizleştirme.

   Çıktı yalnız yeni koşunun kanıt dizinine, kaynağın ve tüm input dosyalarının dışına, yeni dosya olarak atomik CreateNew/exclusive-create ile yazılabilir. Çözülmüş Windows yollarında kaynak/çıktı eşitliği, kaynak altında hedef, mevcut hedef, case eşdeğerliği ve junction/symlink/reparse kaçışı yazmadan reddedilsin. `--output` verilmezse yalnız stdout. Input/output çakışmasını parent mkdir veya dosya açmadan önce denetle; planlayıcının hiçbir yolu source lock/SQLite/state yazamaz.

   Kabul: kaynak lock'u output verme, mevcut çıktı üzerine yazma ve kaynak içine yönlenen reparse örnekleri reddedilir; kaynak ağacının önce/sonra bytes ve dosya envanteri aynı kalır. Varsayılan dry-run hiçbir kaynak olgu uydurmaz. Bunlar yalnız geçici fixture dizinlerinde çalıştırılır.

10. **M02 — Geçiş doğrulayıcısı VERIFIED beyanını kanıt saymasın.**

    [Validator:76](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/super1_continuation.py:76) alan biçimini kısmen kontrol ediyor; [satır 119](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/super1_continuation.py:119) `signature.status=VERIFIED` olmasını yeterli görüyor. Kök lock/snapshot bytes, zincir, hesap ve şema doğrulanmadan DESIGN_VALIDATED denemez.

    Doğrulayıcıya kayıtla birlikte gerçek yerel kanıt bytes'ı ve güven kökü ver. Alanların tam zorunlu envanterini tanımla; gerekli hash'i null/64 karakter herhangi bir metin diye kabul etme. Root lock raw hash'i, asıl created_at, aynı campaign/account/server bağları, eski→yeni sürüm bağları, önceki transition hash'i ve benzersiz zincir sırası doğrulansın. İlk geçiş ancak root lock ile ve öncül olmadığı kanıtlanan manifestle tanımlansın; sonrakilerde kopuk/tekrar/çevrim kabul edilmez. Engine/frozen candidate/strateji-risk değişiklikleri izin listesi dışındadır. Snapshot manifesti ve içerik hash'leri gerçekten okunsun.

    İmza doğrulaması mevcut [release güven kökü/verifier sözleşmesini](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/deploy/release_integrity.ps1:35) izlesin: pinli güven kökü, manifestin tanımlı ham bytes'ı ve SHA256/RSA doğrulaması; archive/member bağları korunur. Geçiş yetkisi root lock, zincir, snapshot ve eski/yeni hash'leri kapsayan kanonik geçiş payload'ını ayrıca bağlamalı; başka bir release'in geçerli imzasını bu geçişe yetki sayma. Kaydın içine konan herhangi bir public key'i kendi kendine güvenilir kabul etme.

    Yalnız testte, mevcut kriptografik yöntemle üretilen geçici TEST ONLY anahtarı/fixture imzası kullanılabilir. Üretim özel anahtarı, certificate store veya gerçek release imzalama kullanılmaz; test güven kökü normal daemon/release kabulüne eklenmez. Sentetik validator sonucu gerçek snapshot/release doğrulaması diye sunulmaz; başarılı sentetik durumda bile uygulama kapısı kapalıdır.

    Kabul: doğru sentetik paket geçer; lock/manifest/member/transition bytes değişikliği, sahte imza, yanlış kök, eksik hash/şema, başka hesap/kampanya, risk değişikliği ve kopuk/tekrarlanan zincir ayrı ayrı reddedilir. Yalnız JSON status alanını VERIFIED yapmak bunları geçiremez. Bu kabul tamamlanana kadar continuation durumu DESIGN_INCOMPLETE'dir.

11. **M03 — Gerçek state şemasıyla kampanya/risk sürekliliğini kanıtla.**

    [Inventory:128](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/super1_continuation.py:128) yanlış dosyalar arıyor. Envanter aşağıdaki gerçek üreticilere bağlanmalı. Tablodaki yollar `--snapshot-root` göreli yollarıdır; gerçek sunucunun state kökü gözlenmiş sayılmaz.

    | State | Doğru yol / içerik |
    | --- | --- |
    | Kampanya kökü | campaign_lock.json; orijinal bytes/created_at/kimlik |
    | Bar/veri ihtilafı | cache/capital_bars.sqlite3; minute_bars ve conflicts |
    | Emir sürekliliği | orders/idempotency.sqlite3; order_intents, order_event_outbox, broker_execution_states |
    | Broker ekonomik kayıtları | orders/events.jsonl; deal/order/position kimlikleri ve risk-sizing event'leri |
    | Eski karar/kanıtlar | manual/, prefix/, sessions/, finalized/, raw/, preflight/, daily_health/ |
    | Duruş/sağlık/kurtarma | health.json, varsa fatal_latch.json, launcher_failure.json, runtime/broker_recovery_required.json, broker_recovery/ ve technical_failures/ |
    | Risk geçmişi/sayaçlar | Gerçek producer/store/event kaynağından türetilir; var olmayan counters.json veya risk.json uydurulmaz |

    Dosyalar ve SQLite şeması [BarStore](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_capital_forward.py:374), [emir store](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_xm_mt5_forward.py:399) ve [Super1 risk hesabıyla](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_super1_xm_mt5_forward.py:486) eşleştirilsin. Condition-dependent dosyanın doğrulanmış yokluğu manifestte açık kaydedilir; zorunlu verinin bilinmemesi “yok/boş” değildir. Manifestte bulunan dosyanın kaybı reddedilir.

    DB'lere gerçekte olmayan schema_version=1 atama. Mevcut campaign lock şemasını aynen oku; SQLite için gerçek PRAGMA user_version ve tablo/kolon/anahtar şeması fingerprint'i kaydedilsin. Yerel continuation adapter şema sürümü ayrı tanımlansın; tanınmayan DB şeması bloke olsun. Bu tur kaynak DB'ye version/migration yazılamaz.

    Seçilen ileriki yöntem: kaynak yazıcılar durdurulduktan sonra tutarlı SQLite backup ve dosya manifesti; bu tur yalnız sentetik depoda prova. Gerçek store şemalarıyla geçici kaynak kur, SQLite backup API ile ayrı fixture snapshot üret, snapshot'tan yeniden okuyup karşılaştır. WAL modlu fixture'da son commit yalnız WAL'da bulunsun; snapshot/readback bunu korumalı. Yalnız ana SQLite dosyasının kopyası eksik kayıtla PASS olamaz. Tutarlı backup'ın ayrıca WAL dosyası gerektirmediğini raporda açık ayır.

    [Fixture validator:147](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/super1_continuation.py:147) hazır before/after listelerini ve eksik alanlarda None==None sonucunu kabul ediyor. Yerine gerçek source/snapshot readback'ten created_at, campaign kimliği, bütün order_id/intent durumları, UNKNOWN, outbox içerik/sırası/ack durumu, broker execution kanıtları, deal dedup, tamamlanmış sonuçlar ve son 10 terminal R dizisi karşılaştırılsın. Deal before kümesi dahil; kayıp veya değiştirilmiş deal gözden kaçamaz. Eksik alan otomatik boş liste olamaz.

    Risk sırası ve R hesabı yeniden icat edilmez: mevcut Super1 hesabının broker çıkış kanıtı, time_msc/ticket sırası ve risk-sizing event'leri kullanılır. Yerel continuation önkoşulu, snapshot'ın kanıtladığı geçmişle hedefi taklit eden history cevabının eksik/kesilmişliğini yakalamalı; eksik history'yi yeni kampanyanın sıfır geçmişi sayamaz. Gerçekten hiç terminal sonucu olmayan kampanya ancak snapshot bunu kanıtlarsa boş olabilir. Bu kontrol sentetik continuation validator'da test edilir; normal daemon'un yeni protokol kabulü bu tur açılmaz.

    Kabul: 10'dan fazla terminal sonucu, gönderilmiş ve UNKNOWN intent, ack'li/ack'siz outbox içeren gerçek şemalı fixture korunur. Kayıp WAL commit'i, silinmiş/değişmiş deal, bozulmuş outbox, kesilmiş risk geçmişi, eksik zorunlu alan ve kendi içinde tutarlı başka kampanya ayrı negatiflerdir. Girdi bytes'ı değiştirilmez; eski olaylara yeni runtime hash'i yazılmaz.

12. **O01 — Önceki rollback maddesini gerçek güvenlik kapısıyla tamamla.**

    [Rollover:634](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/deploy/rollover_super1_campaign_windows.ps1:634) arşive failure JSON yazıyor; daemon'un [başlangıç latch kontrolü](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_capital_forward.py:1488) bunu tüketmiyor. [Test:524](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/tests/test_deployment_security.py:524) hâlâ metin varlığı testi. İkinci talimatın 12. maddesi tamamlanmış değildir; bu, rollover'ı mevcut kampanya taşınması için kullanma izni vermez.

    Post-start belirsizliği mevcut launcher/daemon'un gerçekten okuduğu kalıcı fatal/recovery kapısına bağla. Okunmayan arşiv kaydı yetmez. Scheduler/process/broker sınırlarını yalıtan PowerShell harness içinde start timeout, start sonrasında watchdog hatası, stop hatası ve arşiv kanıtı yazma hatası enjekte et. Eski/yeni state korunmalı; belirsizlikte otomatik restore/restart yok; gerçekten durduğu kanıtlanmayan süreç STOPPED yazılamaz.

    Her durumda yeni oluşturulmuş process'in gerçek başlangıç kapısı aynı geçici state üzerinde yeni entry'yi engellediğini kanıtlasın. Arşiv raporu yazılamaması güvenlik latch'ini atlatamaz; latch'i temizleyen otomatik recovery ekleme. Kalıcı güvenlik kaydı da yazılamayan arıza garanti edilmiş restart koruması diye PASS raporlanamaz; bu durum açık NO_GO olarak kalır. Harness hiçbir gerçek görev/terminal/credential'a erişirse test başarısızdır.

13. **V01 — Son bytes üzerinde özgün koşu kanıtı ve tam kabul eşlemesi üret.**

    Önce E01–E04 ve T01/T02 hatalarını run-006 üretim bytes'ı üzerinde yeni regresyonla göster; sonra hedefli düzelt. Çalışan ağacı eski sürüme geri yazma. Fixture'ın gözlenen bozuk davranışı, düzeltmenin etkisi ve SDK yeni-entry/REMOVE çağrı sayıları ayrı kaydedilsin. Raporu testin doğru davranışı gerçekten zorlayacağı şekilde hazırla; implementation'ın yanlış alanlarını taklit ederek PASS üreten fake kabul edilmez.

    Her E/T/C/M/O maddesi ve alt senaryosu için gereksinim → tam pytest/harness node ID → beklenen/gerçek sonuç → yeni-entry/REMOVE sayısı → kalıcı state/çıktı yolu eşlemesi teslim et. İlgili olmayan salt dosya testinde broker sayısı NOT_APPLICABLE olabilir; çalışmayan senaryoya sıfır yazılamaz. Hedefli koşu bütün bu node'ları içersin; tam suite ve compileall son kaynak/config/contract bytes'ında ayrıca çalışsın. Test silme, xfail/skip ekleyerek açık kapatma veya sayıya göre beklenti gevşetme yok.

    run-006'da ham stdout/stderr ve tam process kökeni yerine kısa command/result özetleri var; iki JUnit suite başlangıç timestamp'i de aynı. Bu, tek başına hatalı koşu kanıtı değildir; ayrı koşuların kökenini doğrulamaya yetmez. Yeni koşuda mutlak interpreter yolu/sürümü, gerçek argv, cwd, UTC başlama/bitiş, exit code, bağımlılık sürümleri, Git HEAD/diff ve test edilen input hash'leri gerçek process'ten kaydedilsin. Hassas env/credential dökümü yapma.

    Hedefli/tam pytest için ayrı ham stdout/stderr, pytest'in doğrudan ürettiği özgün JUnit ve run metadata; compileall için gerçek argv/stdout/stderr/exit code sakla. Özetler bu kayıtlardan türetilir; elde PASS metni veya JUnit yazma, bir koşunun XML'ini diğer koşu diye çoğaltma. Testler sırasında source/config bytes değişmediyse bunu önce/sonra hash ile göster; değiştiyse ilgili koşuları son bytes ile tekrar çalıştır. Kanıt üreticisinin çıktısı da manifestte yer alsın.

    Eski 268 tam / 139 artifact v16 release kapısını değiştirme. Yeni sürüm önerisinde kesin test dosyaları/node envanteri, gerçek yeni toplamlar, artifact içinden çalıştırılacak testler, continuation/planner/calendar validator ve calendar/provenance/çıkarım bağımlılıkları açık payload/hash gereksinimi olarak listelensin. Builder'ın dizin kopyalaması tek başına artifact doğrulaması değildir. Eski paket overwrite/re-sign, >= eşik, bypass veya test toplamını uydurma yok; yeni release sözleşmesi bu tur REVIEW_REQUIRED kalır.

14. **Yeni kanıtı teslim et ve operasyon aşamasına geçmeden dur.**

    [Kanıt kökü](C:/Users/ISAAC/Documents/otobacktestprojesi/outputs/super1_readiness_20260831) altında mevcut olmayan run-007 dizinini kullan; önceden varsa overwrite etmeden sıradaki boş run numarasını seç ve gerçek kimliği raporla. report/readiness/hash manifest, özgün hedefli+tam test çıktıları, compileall kayıtları, kabul matrisi, resmi kaynak çıkarım/karşılaştırma sonucu, continuation tasarımı/dry-run/fixture sonuçları, rollback harness kanıtı ve release sözleşmesi önerisini ekle. Önceki run dosyaları byte olarak aynı kalır.

    Yerel izinli kaynak/hash bağımlılıklarını son bytes'a bağla; frozen strateji/risk/aday payload'ını bu bahaneyle değiştirme. Hash manifestini en son üret; kapsanan dosyalar bundan sonra değişmez. Manifestin kendi hash'ini kendi içine döngüsel ekleme; teslimde ayrı kaydet. Çalışma ağacındaki tüm yeni kaynaklar/config/provenance/fixture yardımcıları envantere girsin; tracked diff'in untracked dosyaları göstermediğini unutma.

    | Alan | Bu turun kabul kuralı |
    | --- | --- |
    | user_decisions | Kesinleşmiş Windows+SSH / aynı demo hesap / aynı kampanya / bağımsız Super1 |
    | source_code_integrity | Son yerel bytes/hash/diff; sunucu kanıtı değildir |
    | software_status | E01–E04, T01/T02, C02 ve O01 gerçek davranışsal kabulü + özgün son koşu kanıtı varsa PASS; aksi FAIL/INCOMPLETE |
    | calendar_status | Veri/kaynak semantiği C01 ve gerçek gerekli feature zinciri C02 birlikte kanıtlıysa PASS; alt sonuçlar ayrı |
    | campaign_continuation | M01–M03 tüm olumlu/olumsuz fixture'ları geçerse yalnız DESIGN_TESTED; aksi DESIGN_INCOMPLETE |
    | source_snapshot / apply | SOURCE_SNAPSHOT_NOT_VERIFIED; safe_to_apply=false; apply_performed=false |
    | release_contract_status | REVIEW_REQUIRED; üretilmiş/onaylanmış yeni release yok |
    | source_fenced / target_environment | NOT_VERIFIED |
    | broker_preflight / pending_accept_cancel / strategy_fill_exit / task-reboot-reconnect | Gerçek ortam için NOT_RUN; fixture sonuçları ayrı |
    | overall_status | NO_GO; yerel PASS hiçbir operasyonel yetki açmaz |

    Zorunlu bir yerel kabul tamamlanamıyorsa nedenini ve eksik node/kanıtı belirt; o alanı PASS yapma. Strateji, risk, kimlik, güven kökü veya operasyon izni gerektiren yeni karar alamazsın. Böyle bir karar gerekiyorsa diğer kapsam içi işleri bitirip tek somut soruyla mimara dön; yukarıdaki kesin kullanıcı kararlarını tekrar sorma.

    Bu tur sonrasında mimari inceleme yapılacak. Onaylı kaynak snapshot/fence, hedef Windows görev kullanıcısında emirsiz doğrulama, ayrı yetkili demo pending kabul/iptal testi ve doğal Super1 fill/exit/restart kanıtı olmadan taşıma hazır sayılmaz. Pending emrin brokerca kabulü ile fiyat koşulu oluştuğunda dolması ayrı aşamalardır; her emrin kesin dolacağına veya kesintisiz çalışmaya garanti verilmez. Keyfî Super1 magic'li fill/manuel kapatma ile mevcut risk geçmişi kirletilmez.
