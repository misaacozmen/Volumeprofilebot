# Super1 kampanya devam tasarımı

Bu belge yalnızca mevcut kampanyanın kesinti sonrası devamı için yerel tasarımdır. Lock, rollover, init, daemon, scheduled task, MT5 veya broker işlemi yapmaz.

## Uygulama önkoşulları

Devam işlemi ancak aynı kampanya lock dosyasının doğrulanabilir snapshot’ı, hedef makineden alınmış state snapshot’ı ve yeni release kök imzası doğrulandıktan sonra değerlendirilebilir. Bu çalışmada kaynak state snapshot’ı yoktur; planner sonucu `SOURCE_SNAPSHOT_NOT_VERIFIED` ve `apply_allowed=false` üretir. Yeni sürüm/hash değerleri yalnızca önerilen yerel byte kanıtıdır, kaynak sürüm kanıtı değildir.

## Geçiş kaydı

`scripts/super1_continuation.py` içindeki `build_transition_record()` şu alanları üretir:

- schema, kampanya kökeni, oluşturulma zamanı ve önceki transition hash zinciri;
- original campaign-lock SHA256;
- old/new release, runtime, harness, contract ve calendar hash grupları;
- değişmeyecek engine, frozen candidate ve strategy-risk hash grubu;
- broker account/server/company bilgisinin yalnızca digest’i;
- state snapshot manifest durumu, hash’i ve tam inventory listesi;
- state schema sürümleri, izinli değişiklik listesi ve release-root signature durumu.

Public account number hiçbir rapor veya fixture çıktısına yazılmaz.

## Snapshot inventory ve tutarlılık

Inventory; campaign lock, minute bars/conflicts SQLite, order intents, outbox, broker execution states, order/deal logları, prefix/final/manual kayıtları, health/fatal/recovery kayıtları, sayaçlar ve son 10 terminal R broker-deal ID’sini kapsar. Gerçek snapshot uygulandığında SQLite için önce quiesce/consistent backup veya WAL dahil immutable kopya alınmalı; backup tamamlanmadan geçiş kaydı “verified” olamaz.

Fixture doğrulaması created_at, order ID’leri, UNKNOWN kayıtları, outbox sırası, deal dedup, completed result ve son 10 terminal R değerlerini birebir korur. Eksik hedef geçmişi devam blocker’ıdır; boş tarih aralığı flat kanıtı değildir.

## Rollback doğrulaması

Scheduler başlatmadan harness seviyesinde şu durumlar test edilmelidir: start timeout, watchdog failure, stop failure ve evidence-write failure. Post-start belirsizlikte otomatik restore/restart yapılmaz; state korunur ve kalıcı latch ile yeni entry engellenir. Rollback kanıtı yoksa release contract durumu review-required kalır.

## Bu koşunun kararı

`DESIGN_TESTED` yalnızca planner/validator/fixture seviyesinde bir sonuçtur. Gerçek snapshot, hedef Windows task-user kanıtı, MT5/broker preflight ve release imzası çalıştırılmadığı için bu tasarım uygulanabilir bir release veya GO kararı değildir.
