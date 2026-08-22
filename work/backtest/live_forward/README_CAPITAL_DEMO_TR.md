# Capital Demo canlı forward altyapısı

Bu kampanya mevcut motoru değiştirmez. Onaylı Dukascopy audit manifestini ebeveyn baseline
olarak doğrular; Capital feed ve sabit `US100`/`US500` selector’ları için yeni, temiz bir
kampanya kilidi üretir. Strateji threshold, timeframe ve kuralları aynıdır.

İlk aşama fiziksel olarak emir endpoint’i içermez. Her döngüde Capital demo REST feed’den
1 dakikalık bid OHLCV alınır; NQ 3m ve SPX 5m yalnız eksiksiz, kapanmış dakika kümelerinden
oluşturulur. Eksik veya sonradan çelişen mum tüm pair’i `DATA_INVALID` yapar.

Sunucu durumu:

```bash
sudo systemctl status forward-shadow
sudo journalctl -u forward-shadow -n 100 --no-pager
sudo -u forwardshadow /opt/forward-shadow/.venv/bin/python \
  /opt/forward-shadow/scripts/run_capital_forward.py \
  --output-root /var/lib/forward-shadow status
```

Kimlik bilgileri yalnız sunucuda `/etc/forward-shadow/capital.env` içine yazılır:

```text
CAPITAL_IDENTIFIER=Capital giriş e-postası
CAPITAL_API_KEY=API integrations ekranındaki anahtar
CAPITAL_API_PASSWORD=Anahtar oluştururken belirlenen özel parola
```

Dosya `root:forwardshadow`, mod `0640` kalmalıdır. Ardından:

```bash
sudo systemctl restart forward-shadow
sudo bash -c 'set -a; source /etc/forward-shadow/capital.env; set +a; \
  exec sudo -E -u forwardshadow /opt/forward-shadow/.venv/bin/python \
  /opt/forward-shadow/scripts/run_capital_forward.py \
  --output-root /var/lib/forward-shadow doctor'
```

Her işlem gününde motor sonucu görülmeden, 09:30 New York öncesi:

```bash
sudo -u forwardshadow /opt/forward-shadow/.venv/bin/python \
  /opt/forward-shadow/scripts/run_capital_forward.py \
  --output-root /var/lib/forward-shadow manual \
  --date 2026-07-27 --author ISAAC \
  --nq-final UNKNOWN --nq-direction UNKNOWN \
  --spx-final UNKNOWN --spx-direction UNKNOWN
```

Gerçek manuel karar varsa `UNKNOWN` yerine TAKE/SKIP ve LONG/SHORT yazılır. Kayıt append-only
olduğu için ikinci kez değiştirilemez.

`/var/lib/forward-shadow` altında ham fetch logları, ilk bilinen OHLCV cache’i, prefix
snapshot’ları, günlük trace/lifecycle/decision kayıtları ve hash’ler tutulur. Servis yeniden
başlatıldığında kaldığı yerden devam eder. Bilgisayarın kapalı olması sunucuyu etkilemez.

Audit geçişi tamamlanmadan paper-order transport eklenmez veya açılmaz. Geçişten sonra ayrı
kod/config hash’iyle temiz paper kampanyası başlatılır.
