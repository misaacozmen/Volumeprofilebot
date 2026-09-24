# Risk-hardening R1–R6 final kapanış kanıtı

Tarih: 22 Eylül 2026  
Dal: `codex/risk-hardening-1-11`  
Code-tested ref: `93debdd0cd79506fd077e6bf36ee87febc09ceb6`  
Evidence-bound ref: `a1d5c4dbc2ccaa57025a4f96e276985bbd5be54a`  
Final HEAD: this report's containing commit; verify with `git rev-parse HEAD`  
Karar: **DÜZELTME GEREKLİ — `DEPLOYMENT_READY=false`**

Push, merge, signed deployment ve rollback yapılmadı. Ana dal/CI sonucu iddia edilmedi.

## Bulgular

- **B1 — kapandı:** Forward runner aktif V4 config’i açıkça bağlar. Builder, `super1_xm_mt5_demo_config_v4.json` dosyasını staged legacy alias’a byte-identical olarak üretir; eksik/yanlış config fail-closed olur.
- **B2 — kapandı:** Windows sandbox trusted CPython runtime kökünü user/project kodundan ayırır. Dynamic compile/exec ve native loading yalnızca trusted runtime’dan kabul edilir. Gerçek CLI backtest geçti: `Loaded 20055 candles: CAPITALCOM_NAS100 5m`, `Trades: 43`; non-empty summary/trades/risk-xray çıktıları ve attestation üretildi.
- **N1 — kapandı:** `ctypes.dlopen` artık immediate caller stack frame’ine güvenmiyor. Native yükleme varsayılan olarak kapalı; yalnız CLI dependency bootstrap sırasında `kernel32`, `kernel32.dll`, `user32`, `user32.dll`, `tzres.dll` allowlist’i geçici olarak açık ve dispatch öncesi kapanıyor. Doğrudan `ctypes.CDLL` ve `ctypes.cdll.LoadLibrary` AppContainer probe’ları reddedildi; pozitif CLI backtest geçmeye devam etti. Commit: `84245e6`.
- **B3 — kapandı:** Runtime paketlemesi explicit allowlist kullanır; host `sitecustomize.py`, `usercustomize.py`, `.pth`, base `site-packages` ve izin verilmeyen DLL’ler dışlanır. Runtime manifest dependency/DLL hash’lerini bağlar; başarısız hazırlık geçici runtime’ı temizler.
- **B4 — kapandı:** Targeted JUnit/log, collection ve PowerShell AST çıktıları benzersiz dosyalara yazıldı; manifest checkout ve committed bytes SHA-256 ile doğrulanıyor. `225 passed`; manifest verifier `2 passed`.
- **B5 — kapandı:** Owner-created gerçek `SymbolicLink` doğrulandı; link hedefi aynı fixture kökündeki `outside\outside.txt`. Gerçek AppContainer symlink-escape testi `1 passed`; izinli okuma/yazma ve attestation kontrolleri de geçti.

## Test kanıtı

- Collection: `779 tests collected`.
- Sandbox suite: `20 passed`, gerçek symlink fixture dahil.
- Frozen forward-shadow baseline: `6 passed`; baseline code hash `e8503eb08cfbdb4bd1f90c85a4b3d3b22c2f7543f308b19b3cf4fa040632b51d`; baseline manifest SHA-256 `266835384775e3924fee2a1c72ca66d54bdf8ad4ec87f9caec3bcfed2f2506c5`.
- Tam CPython 3.11 suite: `779 passed, 0 failed, 0 errors, 0 skipped, 0 deselected, 1 warning`.
- Bağımsız temiz checkout (`a1d5c4d`, yalnızca 144/144 hash-doğrulanmış ham girdi): `779 passed`; bağımsız sandbox suite `20 passed`.
- Final evidence verifier: `4 passed`; önceki evidence paketleri korunmuştur.
- R1 structural scan: `PASS`; `candidate_tree_structural_violation_count=0`, `origin_unchanged=true`, `source_unchanged=true`.

Kanıt dosyaları:

- [DEPLOY-001 manifest](C:/Users/ISAAC/Documents/otobacktestprojesi-risk-integration/work/backtest/docs/DEPLOY_001_EVIDENCE/manifest.json)
- [Final evidence manifest](C:/Users/ISAAC/Documents/otobacktestprojesi-risk-integration/work/backtest/docs/DEPLOY_001_EVIDENCE_FINAL_20260922_a6988d9/manifest.json)
- [Final full JUnit](C:/Users/ISAAC/Documents/otobacktestprojesi-risk-integration/work/backtest/docs/DEPLOY_001_EVIDENCE_FINAL_20260922_a6988d9/full.xml)
- [Final full log](C:/Users/ISAAC/Documents/otobacktestprojesi-risk-integration/work/backtest/docs/DEPLOY_001_EVIDENCE_FINAL_20260922_a6988d9/full.log)
- [Final sandbox JUnit](C:/Users/ISAAC/Documents/otobacktestprojesi-risk-integration/work/backtest/docs/DEPLOY_001_EVIDENCE_FINAL_20260922_a6988d9/sandbox.junit.xml)
- [Final sandbox log](C:/Users/ISAAC/Documents/otobacktestprojesi-risk-integration/work/backtest/docs/DEPLOY_001_EVIDENCE_FINAL_20260922_a6988d9/sandbox.log)
- [R1 rescan](C:/Users/ISAAC/Documents/otobacktestprojesi-risk-integration/work/backtest/outputs/risk-hardening-r1-identity-rescan-final-20260922.json)

Full JUnit SHA-256: `5f2b3a76e2591d71d72b9f2b9a642f5e9d137a3b8917114eca8175bfe4cb4aef`  
Full log SHA-256: `b94e3dd8fb5986c38ebee05f7419c949b6b80500c55bb99accd8a97a5ba2ca81`  
Sandbox JUnit SHA-256: `c868df5642ce52c49f97a1408a2bac3f5057d1b95d631c6d3c48b225fdabb3f1`  
Sandbox log SHA-256: `ecd7898f53f6d6db2b0c3e91fd5970693d279bbdf928ece73740d444539af965`  

## Durum kapıları

```text
PASS_CODE                 = YES (779/779)
PASS_OS_SANDBOX           = YES (20/20, gerçek symlink fixture dahil)
INDEPENDENT_REVIEW        = YES (a1d5c4d, 144/144 pinned raw inputs)
MERGED_MAIN               = false
CI                        = NOT_RUN
PASS_EXTERNAL_DEPLOYMENT  = false
PASS_PROMOTION            = false
DEPLOYMENT_READY          = false
```

Yerel kod/OS kabulü tamamlandı. Bağımsız inceleme, main entegrasyonu, CI, gerçek signing/upgrade/rollback ve promotion/provider-owner kabulü tamamlanmadan `DEPLOYMENT_READY=true` yapılmayacak.
