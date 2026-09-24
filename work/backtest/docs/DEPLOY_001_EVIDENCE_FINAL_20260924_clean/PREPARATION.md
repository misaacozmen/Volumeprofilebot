# Hazırlama ve tekrar üretim

Uygulanan ortam komutları (PowerShell):

```powershell
py -3.11 -m venv C:/Users/ISAAC/Documents/risk-clean-delivery-20260924/venv
Set-Location C:/Users/ISAAC/Documents/otobacktestprojesi-risk-clean/work/backtest
& C:/Users/ISAAC/Documents/risk-clean-delivery-20260924/venv/Scripts/python.exe -m pip install '.[test]'
git clone --no-local --single-branch --branch codex/risk-clean-delivery-20260924 C:/Users/ISAAC/Documents/otobacktestprojesi-risk-clean C:/Users/ISAAC/Documents/risk-clean-delivery-20260924/checkout
```

Pip normal wheel kurdu; editable install yapılmadı. Kaynak snapshot dependency tanımı final ref ile aynıdır. Checkout başlangıç d709b46'dan yalnız fast-forward ile c574e55'e getirildi; başka ref/tag fetch edilmedi. Test/provenance komutlarının cwd'si `.../checkout/work/backtest`; pytest pythonpath ve capture_environment import doğrulaması tüm proje modüllerini bu clone'a bağlar. CPython/dependency sürümleri `environment-final.log`, kurulum çıktısı `environment-install.log` içinde.

`manifest.json` runs dizisi uygulanan tam executable/argument/cwd/UTC/commit/exit değerlerini içerir. Her çağrı `run_gate.py` ile stdout ve stderr'i ayrı yakaladı. Final collection açık `tests`, `-p no:cacheprovider`, owner `--symlink-fixture-root` içerir; gizli PYTEST_ADDOPTS yok. `full.xml` aynı full-final çağrısının JUnit çıktısıdır. Yeni tekrar için yeni dış evidence dizini ve temiz venv/clone kullan; mevcut dosyaları ezme.

Provenance source root `C:/Users/ISAAC/Documents/otobacktestprojesi-risk-integration/work/backtest`, ayrı audit root `C:/Users/ISAAC/Documents/risk-hardening-closeout-20260924/evidence/engine-audit-source`. Prepare komutu 144 pre-2025 dosyayı hash doğrulayıp clone'a kopyaladı; ayrı 2025 girdilerini geçici stage'e hazırladı. Eski raw dosyalar ve owner link silinmedi/yeniden oluşturulmadı.

`scripts/verify_clean_delivery.py --repo <clean-repo> --evidence <this-package> --ref <exact-evidence-commit>` final paketin gerçek Git blob'larını ve tested-ref'e kaynak/config eşitliğini doğrular. Tekil mutation koşusu ayrı geçici kopyada yapılır. Exact final private scan: `scripts/scan_public_broker_identity.py --root . --candidate-ref <exact-evidence-commit> --denylist <private-local-file> --baseline security/broker_identity_history_baseline.json --report <outside-repo-private-report>`; stdout da dış private log'a yönlendirilmelidir. Public rapora yalnız PASS/count/ref özetini taşı.
