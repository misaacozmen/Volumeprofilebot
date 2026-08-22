# Causal Forward Shadow — NQ 3m + SPX 5m

Bu katman emir iletmez ve broker bağlantısı içermez. Aktif strateji `backtest/` paketinde
değiştirilmeden çağrılır. Saat dilimi `America/New_York`, feed `DUKASCOPY`, HTF `15m` ve
yalnız kapanmış mum kullanımı sabittir.

## Dondurulmuş sözleşme

- Baseline: `outputs/reports/engine_reliability_audit_2025_feb_mar/run_manifest.json`
- Baseline dosya SHA-256:
  `dd9ccc1ccf5d98ed776008d819b92df862f019ca288422ac6e5d6f931754ad24`
- Motor code hash:
  `e42f3b38fbef997a1334f61b282edcd7390ce4273edcc2ca60a1683c79fdc98a`
- Config hash:
  `bf077c2c5be71da549622efef4436c439182564ce8a6f7affc533ad208c7b719`

Baseline, motor, config veya kampanya harness hash’i değişirse runner `CRITICAL_STOP`
üretir. Aynı kampanya devam ettirilmez; hata kayda alınır ve temiz forward dönemi gerekir.

## 11 adımlı akış

1. `init`: baseline, motor, config ve harness hash’lerini doğrular; kampanya kilidini bir kez oluşturur.
2. `manual`: motor sonucu görülmeden NQ/SPX manuel TAKE/SKIP ve yön kararını mühürler.
3. `run`: iki leg için yalnız kapanmış bar snapshot’ını alır.
4. Veri kapısı duplicate/conflicting OHLCV, eksik bar, symbol/timeframe/feed ve profile/premarket eksiklerini denetler.
5. Herhangi bir veri sorunu tüm pair seansını `DATA_INVALID` yapar; motor ve shadow işlem kararı üretilmez.
6. Geçerli veride dondurulmuş motor NQ 3m + SPX 5m ve bağımsız 15m HTF ile çalışır.
7. Her olay `bar_time`/`known_time` ile yazılır; VAH/VAL, liquidity, controlling array, CISD, FVG, thesis/order ve terminal lifecycle saklanır.
8. `-1R` pair-cap yalnız entry’den önce kesinleşmiş terminal TP/SL sonuçlarını kullanır; eşzamanlı veya intrabar belirsizlik `WATCH` olur.
9. Aynı snapshot ikinci kez çalıştırılır; result hash eşitliği zorunludur.
10. Her işlem timeframe’inin her kapanışında prefix snapshot karşılaştırılır; illegal state, duplicate order ve açıklanabilir trace kontrol edilir.
11. `status`: 30 geçerli seans ve 20 puanlanabilir motor kararı birlikte tamamlandığında tüm geçiş kapılarını değerlendirir.

TP/SL, manuel–motor FINAL veya direction puanını değiştirmez. `UNKNOWN` alanlar `null`
saklanır ve puanlanmaz. Manuel–motor farkında açıklama yoksa geçiş kapısı başarısızdır.

## Çalıştırma

Çalışma dizini `work/backtest`:

```powershell
python scripts/run_forward_shadow.py init
```

Motoru çalıştırmadan önce manuel kararı kaydet:

```powershell
python scripts/run_forward_shadow.py manual `
  --date 2026-07-27 --author ISAAC `
  --nq-final TAKE --nq-direction LONG `
  --spx-final SKIP --spx-direction SHORT
```

Belirsiz alan için `UNKNOWN` kullan. Fark için önceden bağımsız gerekçe varsa
`--nq-explanation "..."` veya `--spx-explanation "..."` ekle.

Seans verisi tamamlandıktan sonra:

```powershell
python scripts/run_forward_shadow.py run `
  --date 2026-07-27 `
  --nq "data/forward/DUKASCOPY_USATECHIDXUSD, 3m_2026-07-27.csv" `
  --spx "data/forward/DUKASCOPY_USA500IDXUSD, 5m_2026-07-27.csv" `
  --as-of "2026-07-27T16:15:00-04:00"
```

Bir leg birden çok dosyadan geliyorsa aynı `--nq` veya `--spx` bayrağını tekrarla.
Dosya adında feed, symbol ve timeframe açık olmalıdır.

Kampanya durumu:

```powershell
python scripts/run_forward_shadow.py status
```

## Günlük kontrol listesi

- [ ] Baseline/code/config/harness kilidi geçti.
- [ ] Manuel TAKE/SKIP ve yön motor öncesi mühürlendi; belirsiz alan `UNKNOWN`.
- [ ] NQ 3m ve SPX 5m aynı DUKASCOPY feed’den geldi.
- [ ] Seans sonu `--as-of` zamanı doğru; açık mumlar dışlandı.
- [ ] Veri kapısı `VALID`; değilse karar sayısı sıfır ve durum `DATA_INVALID`.
- [ ] `deterministic_rerun=true`.
- [ ] `prefix_violation_count=0`.
- [ ] `invariant_error_count=0`; duplicate order yok.
- [ ] `trace_explainable_pct=100`.
- [ ] Ambiguous intrabar olayları `AMBIGUOUS/WATCH`; pair-cap kararı açıklamalı.
- [ ] `session.json` içindeki data/config/code/harness/result hash’leri mevcut.
- [ ] `status` çıktısında FINAL ve direction uyumu ayrı incelendi.

Her deneme yeni bir timestamp dizinine yazılır; mevcut günlük kayıtlar üzerine yazılmaz.
`PASS_TO_PAPER_FORWARD` yalnız tüm minimumlar ve bütün kapılar geçince oluşur. Aksi halde
paper order yetkisi verilmez; teknik hata düzeltildikten sonra yeni output root ile temiz dönem başlatılır.
