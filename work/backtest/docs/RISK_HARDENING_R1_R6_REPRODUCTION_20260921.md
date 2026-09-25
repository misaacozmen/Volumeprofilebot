# Risk-hardening R1–R6 yeniden üretim

Bu prosedür yerel doğrulama içindir. CI sonucu veya `DEPLOYMENT_READY=true` üretmez.

## Temiz checkout hazırlığı

CPython 3.11 ve proje bağımlılıklarını kurun:

```powershell
cd work/backtest
py -3.11 -m venv .venv311
& .\.venv311\Scripts\python.exe -m pip install -e ".[test]"
```

Threshold girdileri Git geçmişine dahil değildir. Yetkili DUKASCOPY export arşivini aşağıdaki yapıda hazırlayın:

```text
<prepared-root>/data/raw/nq/DUKASCOPY_USATECHIDXUSD, 3m_YYYY-MM-01_YYYY-MM-01.csv
<prepared-root>/data/raw/spx/DUKASCOPY_USA500IDXUSD, 5m_YYYY-MM-01_YYYY-MM-01.csv
```

Beklenen 144 dosya ve SHA-256 değerleri [`first30_pre2025_inputs.sha256`](../data/provenance/first30_pre2025_inputs.sha256) içinde sabittir. Kaynak arşiv, dosya adları veya hash’ler değişirse hazırlık fail-closed olur; threshold değeri sessizce yeniden kalibre edilmez.

```powershell
& .\.venv311\Scripts\python.exe scripts/prepare_risk_provenance.py `
  --source-root C:\path\to\prepared-root `
  --engine-audit-source-root C:\path\to\2025-engine-audit-root
```

`2025-engine-audit-root` is separate from the 144 pre-2025 calibration files. It
contains exactly the hash-listed NQ 3m and SPX 5m CSV inputs plus
`engine_audit_manifest.json`; the manifest records the official Dukascopy source
reference, observation time, provenance limitations, requested window, symbols,
and each file SHA-256. The preparation command checks the manifest, actual bar
coverage, and hashes before staging. Obtain the source and its provenance note
from the owner/provider; do not label filesystem timestamps as retrieval times.
CI requires both roots and does not treat missing audit data as an empty pass.

Komut, girdileri doğrular, ignored `data/raw` ağacını hazırlar, checked-in sealed threshold artifact’in provenance’ını doğrular ve audit sonucunu tracked `forward_shadow/engine_reliability_audit_2025_feb_mar_manifest.json` dosyasına yazar. Bilinçli artifact yenilemesi gerektiğinde aynı komuta `--rebuild-threshold-artifact` eklenir; yeni artifact bytes ayrıca gözden geçirilmeden kabul edilmez. Baseline lock artık ignored `outputs` altındaki dosyaya bağlı değildir.

## Doğrulama

```powershell
& .\.venv311\Scripts\python.exe -m pytest -q
& .\.venv311\Scripts\python.exe -m pytest -q `
  tests/test_super1_risk_e2e.py::test_two_processes_same_account_same_sqlite_cannot_interleave_daily_slots
```

Windows symlink/AppContainer fixture’i ayrıca sahibi tarafından hazırlanır; fixture yolu verilmeden bu kapı başarı sayılmaz. Workflow yalnız yerel olarak doğrulanmıştır ve henüz CI PASS iddiası yoktur.
