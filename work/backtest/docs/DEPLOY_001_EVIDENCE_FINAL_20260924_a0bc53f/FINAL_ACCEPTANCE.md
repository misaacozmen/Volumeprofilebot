# DEPLOY-001 — 24 Eylül 2026 teslim durumu

## Kod sonucu

Code-tested ref `a0bc53f3f7e9735adc90eaea1899b7cdd1bde4d2`, tree `9d6e58445ece28f863165326075ac610f6e52c10`, branch `codex/risk-hardening-1-11`. Ref, önceki bağımsız inceleme noktası `a350263abc57a8add3827a533befd189b58bc01e` sonrasındaki 18 commit’i içerir; yeni son commit environment scanner ve V4 architecture regression düzeltmesidir.

Önceki incelemedeki source-test hatası artık bu temiz CPython 3.11.9 checkout’ta yeniden üretilemedi: ignore edilen `live_forward/super1_xm_mt5_demo_config.json` alias’ı yok; mimari testi tracked canonical V4 JSON’u açıkça yükleyip yalnız test içi kopyaya sentetik hesap uygular. Gerçek `Super1XmMt5DemoOrderClient` constructor’ı, `_strict_reconciliation`, tek live-entry ve tek fiziksel `order_send` assertion’ları korunur. Gerçek MT5 bağlantısı/order yok.

- Architecture + environment scanner + Super1 forward + candidate artifact hedef kümesi: **64 passed**.
- Tam suite: **799 passed, 0 failed, 0 error, 0 skipped**, 1 beklenen duplicate-ZIP test uyarısı; exit `0`, 452.45 saniye.
- Collection ve JUnit: **799/799 unique node**, whitespace-parametreli ID’ler dahil multiset eşit; failure/error/skip/deselection `0`.
- Eski dokuz DEPLOY-001 hedef dosyası full JUnit içinde **229 node** olarak bulundu (önceki 225’e 4 test eklenmiş); sandbox **20/20**, evidence verifier **5/5**, owner symlink node’u PASS.
- Risk matrisi için seçilen **39/39** pozitif/negatif node JUnit’de PASS.
- Ham provenance: NQ 72 + SPX 72 = **144/144** tracked hash manifestiyle eşleşti. Ayrı 2025 NQ 3m/SPX 5m girdi manifesti, hash, sembol/timeframe/window ve coverage denetiminden geçti. Audit: `core_tests_passed=true`, deterministic rerun `true`, prefix violation `0`, 48 prefix check, invalid days blocked `3`, canonical decisions `57`, `forward_shadow_ready=true`. Threshold SHA `2bffd175490af718f66dc79cb0b037e059ceea6b13a694f78d5bc673f36b4d0d`; baseline manifest SHA `266835384775e3924fee2a1c72ca66d54bdf8ad4ec87f9caec3bcfed2f2506c5` ve tracked baseline byte’ları değişmedi.
- Ayrı environment-schema scan: `CLEAN`, 0 finding; hem nested project hem repo-root `.github/workflows` tarandı. Identity structural-only scan: `PASS`, 0 structural violation; source/origin değişmedi. Private denylist scan yok/blocked; structural-only sonucu private match sonucu değildir.

Tam komutlar, exit code, interpreter/dependency listesi, node hash’leri, JUnit/log SHA-256’ları ve audit manifests bu klasördeki `manifest.json` ve bağlı artifact’larda kayıtlıdır. `full.xml` SHA-256 `7d14babb6f46ce3e4fdbba436229b92ee173f100344e2b6ef4d87bd66b310e25`; `full.log` SHA-256 `21f728fc0efdc1eaf7dce6ffc885036bce73a808e793dd39510ad9c252c24e27`; collection log SHA-256 `c555cd7c2e05106440b867a9d3f173e775f60fe3d1ea2941bb4f6050553b38b9`.

## OS, kaynak ve tarihçe

Owner fixture kökü `C:/Users/ISAAC/AppData/Local/Temp/otobt-owner-fixture-final-20260922`; `output/escape-symlink.txt` gerçek SymbolicLink ve tam hedef aynı kökün `outside/outside.txt` dosyasıdır. Fixture oluşturulmadı, silinmedi veya izinleri değiştirilmedi. Gerçek AppContainer testleri izinli input/output kontrollerini ve dış hedef reddini/attestation’ı doğruladı. Yetki/path snapshot’ı `owner-fixture-and-authority.json`’da; sonuç yalnızca bu Windows makinesi içindir.

Risk hardening kod ref’inde tracked kaynaklar temizdi. Original engineer worktree’de **144 ham raw dosya untracked** ve `docs/DEPLOY_001_EVIDENCE/full-n1.junit.xml` adlı önceki kanıt dosyası untracked olarak korundu; hiçbirini stage etmedim veya değiştirmedim. Temiz test checkout’ta da test için ayrı 144 dosya hash doğrulamasıyla stage edildi; commit edilmedi. Engine-audit provider receipt/retrieval timestamp mevcut değildi; manifest bunu açıkça sınırlıyor.

N1 post-bootstrap native-load bulgusu a350263 bağımsız raporundaki dış worker-kopya probe’uyla kapatılmış olarak korunur; bu turda yeniden probe edilmedi. Kaynak kullanıcı eki SHA-256: `7e2764bb68de54f6e18ae4f570f454b789af6302d16c8c58efb52dbb07a9d8b0`. Sandbox production/test dosyaları a350263’ten değişmedi. Yeni a0bc53f kod ref’i için bağımsız inceleme henüz yapılmadı; önceki rapor kararı `CHANGES_REQUIRED` olarak kalır.

## Zorunlu ayrı durum alanları

| Kapı | Durum | Kapsam / somut engel |
|---|---|---|
| `PASS_CODE` | **YES** | a0bc53f, bu Windows CPython 3.11.9 temiz checkout’u; 799 test PASS. Bağımsız kabul veya main iddiası değildir. |
| `PASS_OS_SANDBOX` | **YES** | Bu makinedeki gerçek AppContainer + owner fixture; sandbox 20/20. Başka host’a genellenmez. |
| `INDEPENDENT_REVIEW` | **PENDING** | a0bc53f/tree henüz bağımsız reviewer tarafından incelenmedi. |
| `MERGED_MAIN` | **NO** | Uzak main SHA `c1c769aec68b84c9c13f9add198ab130241c69ad`; push/merge yapılmadı. |
| `CI` | **NOT_RUN** | Self-hosted runner/Actions sonucu alınmadı. |
| `PASS_EXTERNAL_DEPLOYMENT_ACCEPTANCE` | **BLOCKED** | Accepted main, admin target session, trust pin/owner snapshots ve gerçek signed upgrade/rollback yok. DPAPI path’i mevcut ama key okunmadı/kullanılmadı. |
| `PASS_PROMOTION_ACCEPTANCE` | **BLOCKED** | Provider rate-limit evidence, 113-target canonical V5 owner manifest/residual-zero, owner attestation ve DEMO read-only observation yok. Güncel residual sayısı hesaplanmadı. |
| `DEPLOYMENT_READY` | **false** | Bağımsız kabul, main doğrulaması, gerçek signed deployment/rollback ve Promotion dış kabulü tamamlanmadı. |

## Dış girdiler

Owner/provider sorumluları, eksik artifact alanları ve güvenli yeniden çalıştırma adımları [`EXTERNAL_BLOCKERS.md`](EXTERNAL_BLOCKERS.md)’de; R1–R11 runtime/test eşlemesi [`RISK_MATRIX.md`](RISK_MATRIX.md)’de. Başarısız geliştirme koşuları ve nedenleri [`DEVELOPMENT_FAILURES.md`](DEVELOPMENT_FAILURES.md)’de saklıdır.
