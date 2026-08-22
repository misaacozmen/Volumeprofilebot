# XM MT5 demo forward testi

Sabit sözleşme: XM `US100Cash` 3m, `US500Cash` 5m, bağımsız HTF 15m,
`America/New_York`, yalnız kapanmış mumlar. Emirler yalnız `trade_mode=DEMO`,
`XMGlobal-MT5 7`, `XM Global Limited` ve sabit hesap kimliği birlikte doğrulanırsa gönderilir.

Planlı kapanış kanıtı `diagnostics/xm_session_diagnostic_2026-07-29.json` dosyasındadır.
Pazar günü 18:00–20:59 ET ve hafta içi 20:00–20:59 ET broker M1/tick akışında
kapalıdır. Bu aralıklar eksik mum değildir; bunların dışındaki tek bir eksik açık-seans
mumu `DATA_INVALID` üretir ve emir iletimini durdurur.

Günlük kontrol:

```powershell
$env:XM_MT5_SERVER = (Get-Content -Raw C:\ForwardShadow\xm-server.txt).Trim()
$env:XM_MT5_TERMINAL_PATH = (Get-Content -Raw C:\ForwardShadow\mt5-terminal.txt).Trim()
C:\ForwardShadow\venv311\Scripts\python.exe `
  C:\ForwardShadow\app\scripts\run_xm_mt5_forward.py status
C:\ForwardShadow\venv311\Scripts\python.exe `
  C:\ForwardShadow\app\scripts\run_xm_mt5_forward.py daily-health
Get-ScheduledTaskInfo -TaskName ForwardShadowXM
```

Demo emir yetkisi ve kontrollü minimum hacimli pending-order smoke testi:

```powershell
C:\ForwardShadow\venv311\Scripts\python.exe `
  C:\ForwardShadow\app\scripts\run_xm_mt5_forward.py doctor
C:\ForwardShadow\venv311\Scripts\python.exe `
  C:\ForwardShadow\app\scripts\run_xm_mt5_forward.py smoke-order --confirm-demo
```

`DATA_INVALID`, `WATCH/AMBIGUOUS`, hash değişikliği, bağlantı sorunu veya illegal state
durumunda yeni emir gönderilmez; sistemin kendi magic number’ına ait bekleyen emirler iptal edilir.
