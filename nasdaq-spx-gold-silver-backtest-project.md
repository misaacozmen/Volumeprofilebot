# NAS100 / SPX500 / Gold / Silver Backtest Project

Bu dosya, sonraki Codex sohbetlerinde projenin kaldigi yerden devam edebilmesi icin hazirlandi. Yeni chatte once bu dosyayi okut, sonra veri CSV dosyalarini ver.

## Amac

Volume Profile + ICT karisimi manuel intraday stratejiyi otomatik backtest sistemine cevirmek.

Ilk prototip CFD verileriyle yapilacak. Sonuclar strateji davranisini test etmek icin kullanilacak; nihai futures performansi olarak okunmayacak.

Nihai hedef:

- NASDAQ / SPX / Gold / Silver icin otomatik backtest
- 3m + 5m timeframe birlikte test
- Trade listesi ve performans raporu
- Win rate, net R, max drawdown, profit factor, ay/saat/sembol bazli rapor

## Veri Durumu

Kullanici TradingView uzerinden analiz yapiyor.

2026-07-04 guncellemesi:

- Uzun veri kaynagi olarak Dukascopy indirici eklendi: `work/backtest/scripts/download_dukascopy.py`
- NQ (`DUKASCOPY_USATECHIDXUSD`), SPX (`DUKASCOPY_USA500IDXUSD`) ve Silver (`DUKASCOPY_XAGUSD`) icin 2022-01-02 18:00 NY -> 2026-07-02 23:55/23:57 NY araligi indirildi.
- Dosyalar `work/backtest/data/raw/` altinda proje formatinda duruyor: `time,open,high,low,close,Volume`
- Indirici m1 BID veriyi indirip 3m/5m'e resample ediyor. Loader timestamp duplicate'lerini temizliyor.
- Gold (`DUKASCOPY_XAUUSD`) henuz indirilmedi.

Ilk etapta futures yerine Capital.com CFD verisi kullanilacak:

- CAPITALCOM:NAS100
- CAPITALCOM:SPX500
- Gold ve Silver icin muhtemelen Capital.com CFD sembolleri

TradingView CSV export ayarlari:

- Time format: ISO time
- Chart timezone: New York
- Volume indikatoru chartta acik olmali
- CSV kolonlari sunlara benzemeli:

```csv
time,open,high,low,close,Volume
```

Volume kolonu sart. Fixed Range Volume Profile tool'unun kendisi export edilmek zorunda degil; Python tarafinda OHLCV mumlarindan VAH/VAL hesaplanacak.

Mevcut kontrol edilen dosyalar:

- `CAPITALCOM_NAS100, 5_76e7d.csv`
  - Aralik: 2026-05-25 14:25 NY -> 2026-06-30 07:00 NY
  - Volume yok
- `CAPITALCOM_NAS100, 5_46343.csv`
  - Aralik: 2026-05-25 14:25 NY -> 2026-06-30 07:05 NY
  - Volume var
- `CAPITALCOM_NAS100, 5_6f94a.csv`
  - Aralik: 2026-05-20 06:35 NY -> 2026-05-25 14:25 NY
  - Volume var

Kullanici 6 aylik veri hazirlayacak. Dosyalar 1 aylik/parcali gelebilir. Script dosyalari birlestirmeli, duplicate mumlari temizlemeli, tarih sirasina dizmeli ve gap raporu vermeli.

## Strateji Ozeti

Strateji: Fixed Range Volume Profile + ICT.

Islem arama bolgesi:

- NY session baslangicindan lunch oncesine kadar
- 09:30 - 12:00 New York
- Lunch dahil degil
- Her sembol icin gunluk maksimum 2 islem

Timeframe:

- 3 dakika
- 5 dakika
- 3m + 5m birlikte test edilecek

Volume Profile araligi:

- Onceki gun 18:00 NY -> islem gunu 09:30 NY
- Value Area Volume: 70%
- Row count: 1000
- Cikan seviyeler: VAH, VAL

## Setup Kurallari

Short setup:

1. Fiyat VAH bolgesine gelir, VAH'a dokunur, ustune fitil atar veya body ile ustunde kapanabilir.
2. Onemli olan VAH civarindaki likidite/sweep sonrasi reversal olmasi.
3. Reversal 3m veya 5m bearish CISD ile dogrulanir.
4. CISD sonrasi 5 mum icinde bearish FVG veya bearish IFVG aranir.
5. Entry = FVG/IFVG baslangici.
6. Stop = CISD oncesi bullish mumlarin en yuksek body seviyesi.
7. TP = 3R.

Long setup:

1. Fiyat VAL bolgesine gelir, VAL'e dokunur, altina fitil atar veya body ile altinda kapanabilir.
2. Onemli olan VAL civarindaki likidite/sweep sonrasi reversal olmasi.
3. Reversal 3m veya 5m bullish CISD ile dogrulanir.
4. CISD sonrasi 5 mum icinde bullish FVG veya bullish IFVG aranir.
5. Entry = FVG/IFVG baslangici.
6. Stop = CISD oncesi bearish mumlarin en dusuk body seviyesi.
7. TP = 3R.

## CISD Tanimi

Bullish CISD:

- Bearish sweep sonrasi son bearish mumun body high seviyesi ustunde kapanis.

Bearish CISD:

- Bullish sweep sonrasi son bullish mumun body low seviyesi altinda kapanis.

CISD bekleme penceresi:

- 3m chart: sweep/reaksiyon sonrasi maksimum 10 mum
- 5m chart: sweep/reaksiyon sonrasi maksimum 6 mum
- Yaklasik 30 dakika

## FVG / IFVG Tanimi

FVG:

- Klasik 3 mum FVG.
- Bullish FVG: 1. mum high < 3. mum low.
- Bearish FVG: 1. mum low > 3. mum high.
- Entry = FVG baslangici.

IFVG:

- FVG ters yonde kapanisla kirilirsa IFVG olur.
- Fitil yeterli degil; kapanis gerekir.
- Entry = IFVG baslangici.

CISD sonrasi FVG/IFVG penceresi:

- Maksimum 5 mum.

## Likidite Seviyeleri

Session high/low seviyeleri:

- New York PM session high/low: 13:30 - 16:00 NY
- Asia session high/low: 20:00 - 00:00 NY
- London session high/low: 02:00 - 05:00 NY

Equal highs/lows:

- Son X mum icinde iki swing high/low birbirine Y tick mesafeden yakinsa equal high/low sayilir.
- Ilk versiyonda makul varsayim:
  - Swing lookback: 100 mum
  - Equal tolerance: yaklasik 4 tick

VAH/VAL yakinlik toleranslari:

- NAS100 / NQ: 5 point
- SPX500 / ES: 1.5 point
- Gold / GC: 2 point
- Silver / SI: 0.03 point

Bu toleranslar ilk prototip icin kabul edildi, sonra optimize edilebilir.

## Emir ve Risk Kurallari

- Entry limit emirdir.
- Limit emir tetiklendiyse SL veya TP olana kadar acik kalabilir.
- Entry tetiklenmeden once TP hedefindeki likidite alinirsa emir iptal edilir.
- TP sabit: 3R.
- Partial yok.
- Breakeven yok.
- Spread ve slippage rapora dahil edilmeli.
- Ilk CFD prototipinde spread/slippage tahmini kullanilabilir; sembol bazli ayarlanabilir parametre olmali.

## Backtest Motorundan Beklenenler

1. CSV dosyalarini oku.
2. Kolonlari normalize et: time/open/high/low/close/volume.
3. Timezone'u New York olarak isle.
4. Birden fazla CSV varsa birlestir:
   - duplicate mumlari temizle
   - tarih sirasina diz
   - gap/overlap raporu ver
5. Her gun icin 18:00 -> 09:30 arasi volume profile hesapla.
6. VAH/VAL uret.
7. 09:30 -> 12:00 arasi setup ara.
8. 3m ve 5m sinyalleri birlikte degerlendir.
9. Gunluk sembol basina maksimum 2 trade uygula.
10. Trade listesi ve ozet rapor uret.

Trade listesi kolonlari:

```text
symbol
timeframe
date
direction
vah
val
trigger_level
liquidity_context
sweep_time
cisd_time
fvg_time
entry_time
entry_price
stop_price
target_price
exit_time
exit_price
result
r_multiple
notes
```

Ozet rapor:

```text
symbol
timeframe
total_trades
wins
losses
win_rate
net_r
avg_r
max_drawdown_r
profit_factor
best_day
worst_day
long_stats
short_stats
month_stats
hour_stats
```

## Onemli Notlar

- Capital.com CFD verisindeki volume gercek CME futures contract volume olmayabilir. Muhtemelen broker/tick volume davranisi gosterebilir.
- Bu nedenle ilk backtest "strateji davranis testi" kabul edilecek.
- Nihai karar icin daha sonra NQ, ES, GC, SI futures verisi gerekir.
- Futures veri icin potansiyel kaynaklar:
  - Databento
  - CME DataMine
  - Baska lisansli futures intraday data saglayicilari

## Sonraki Chat Icin Talimat

Yeni chatte yapilacak ilk is:

0. Once guncel durum dosyasini oku:
   - `work/backtest/PROJECT_STATUS.md`

1. Bu dosyayi oku.
2. Kullanici tarafindan verilen yeni CSV dosyalarini incele.
3. Her dosya icin:
   - sembol
   - timeframe
   - ilk tarih
   - son tarih
   - satir sayisi
   - volume var/yok
   - duplicate var/yok
   - gap var/yok
   raporu ver.
4. Veri yeterliyse Python backtest projesini kur:
   - `work/backtest/`
   - `data/raw/`
   - `data/processed/`
   - `outputs/reports/`
5. Once sadece NAS100 5m ile minimum viable backtest calistir.
6. Sonra 3m, SPX500, Gold ve Silver ekle.

## Eksik / Sonradan Netlestirilecek Noktalar

- Gold ve Silver CFD sembol adlari.
- Spread/slippage varsayimlari:
  - NAS100
  - SPX500
  - Gold
  - Silver
- 3m ve 5m birlikte sinyal verdiginde oncelik:
  - Ilk gelen sinyal mi?
  - Daha kucuk stop veren mi?
  - 5m sinyali 3m'e gore daha guclu mu?
- Equal high/low algoritmasinin manuel orneklerle kalibre edilmesi.
- Volume profile hesaplamasinin TradingView Fixed Range Volume Profile ile ne kadar benzediginin 5-10 ornek gun uzerinden kontrol edilmesi.
