# Oto Backtest

Bu klasor NAS100 / SPX500 / Gold / Silver icin Volume Profile + ICT backtest prototipidir.

## Klasorler

- `data/raw/`: TradingView CSV export dosyalari
- `data/processed/`: normalize edilmis veri ciktilari
- `outputs/reports/`: veri kontrol ve backtest raporlari
- `backtest/`: Python kaynak kodu

## Ilk komut

CSV dosyalarini `data/raw/` altina koyduktan sonra:

```powershell
python -m backtest.cli inspect data/raw
```

Rapor CSV olarak da yazilabilir:

```powershell
python -m backtest.cli inspect data/raw --output outputs/reports/data_inspection.csv
```

## Backtest

5m:

```powershell
python -m backtest.cli run data/raw --timeframe 5m --output-dir outputs/reports/5m
```

3m:

```powershell
python -m backtest.cli run data/raw --timeframe 3m --output-dir outputs/reports/3m
```

Kalibrasyon notlari:

```text
outputs/reports/calibration_review.md
```

Manuel ornekleri CSV ile analiz etmek:

```powershell
python -m backtest.cli calibrate --examples calibration_examples --raw data/raw --output outputs/reports/calibration_analysis.csv
```

Beklenen CSV kolonlari:

```text
time,open,high,low,close,Volume
```

Kolon adlari buyuk/kucuk harfe ve bazi TradingView varyasyonlarina toleranslidir.

## Forward shadow

NQ 3m + SPX 5m causal, emir iletmeyen forward test akisi:

```text
forward_shadow/README_TR.md
```
