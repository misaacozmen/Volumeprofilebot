# Super1 canlı operasyon prosedürü

Bu release derhal `REJECTED_SOFTWARE_SAFETY_GATES` durumundadır. Aşağıdaki güvenlik düzeltmeleri ve dış imzalı release doğrulaması tamamlanmadan release üretilemez; gerçek broker-write veya signing key bu çalışma alanında kullanılmaz.

## Approval

Proposal yalnızca uygulamanın SQLite `STAGED` kaydından ID ile yüklenir. Operatör lease’i elle vermez; runtime `load_lease(..., for_order=True)` ile doğrular ve Windows process token SID’ini kullanır. Approval campaign, account, candidate, release, proposal hash, lease nonce, SID ve expiry’ye bağlıdır. `CONSUMED` kaydı tekrar kullanılamaz; replay veya farklı bağlama `NO_SEND` üretir.

## Health ve heartbeat no-send

`UNKNOWN`, eksik veya `DECAYED` health yeni pozisyonu açmaz. Şu beş heartbeat gerçek cycle olaylarıyla yazılır: market data, signal, risk, reconciliation ve audit anchor. Watchdog herhangi biri missing/stale olduğunda `runtime/no_send.sentinel.json` dosyasını atomik yazar; final-send gate bu dosya varken durur. Sentinel otomatik silinmez: broker readback ve operatör incelemesinden sonra kontrollü olarak temizlenir.

## Outbox, corruption ve replay

Telegram outbox biçimi `{ "schema_version": 3, "entries": [...] }`’dir. V2 veya eksik alanlı kayıtlar migrate edilmez; korunur ve no-send/corruption latch üretir. POST öncesi attempt, başarılı cevap sonrası pozitif `message_id` ACK atomik kaydedilir. Bozuk outbox korunur, corruption latch/dead-letter kaydı oluşturulur ve yerel watchdog/Event Log akışı devam eder. Alarm occurrence’ları ilk enqueue’da persist edilen `alert_episode_id` ve stabil `event_id` ile ayrıdır; recovery sonrasında aynı alarm yeniden bildirilir.

## Signed validation

Release doğrulaması archive, manifest, signature, source hash, canonical artifact-test listesi, locked test envanteri, candidate/config hash’leri ve lease binding’lerini birlikte kontrol eder. Mevcut signed candidate kaynak kod hash’i değiştiği için yeni imzalı release olmadan reddedilir. Bu çalışma sonunda durum `REJECTED_SOFTWARE_SAFETY_GATES` olarak kalır.

## Uygulanması zorunlu düzeltmeler

Tek yetkili emir yolu `order_mutex → signed lease → account binding → Super1 filter/risk/prefix/order_check/idempotency → _order_send_checked → broker reconciliation` olmalıdır. `ProductionOrderFlow` ve ayrı `Mt5ExecutionAdapter` canlı giriş yoluna bağlanamaz.

Proposal, approval, order state, broker evidence, audit ve health aynı mutable `C:\Super1\state\orders\idempotency.sqlite3` veritabanında tutulur. `C:\Super1\app` salt okunurdur.

Telegram sözleşmesi at-least-once ve stabil `event_id` kullanır. Telegram idempotency desteği sağlamadığından kesinlikle duplicate olmayacağı iddia edilmez. Bozuk queue yerel no-send/HALT akışını durduramaz.
