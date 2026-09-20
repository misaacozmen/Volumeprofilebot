# DEPLOY-001 — Uygulama kararı

Durum: uygulama adayı hazır; bağımsız mimar kabulü ve gerçek release/deployment kapıları açık.

## Kaynak

- Başlangıç commit’i: `65b4ff10f9f01618df4af3249217a97ea9eb1e88`
- Başlangıç tree’si: `accc7a0d662691f0ee3e978bc83f3036ea93a1be`
- Worktree: `codex/deploy-001-release-contract`
- Helper blob: `9394ea1018727754fdfc30974dbab8408c56efc9`
- Helper sözleşmesi: `deploy/release_integrity_contract.json`
- Helper byte SHA-256: `bfa1fa7ddcc54bb172e1c33e399ba7d259d36b8baa69e66de41237879db0722c`

Helper sözleşmesi UTF-8, BOM’suz, LF ve 21.876 baytlık canonical payload’ı bağlar.
`.gitattributes` yalnız `deploy/release_integrity.ps1 text eol=lf` kuralını içerir;
toplu renormalizasyon yapılmamıştır.

## Uygulanan sınırlar

Super1 ve ForwardShadow için imzalı archive, manifest, signature, helper ve task
tanımları runtime kontrolüne girmeden önce doğrulanır. İlk runtime stop çağrısından
hemen önce `runtimeControlEntered` true yapılır. Preflight hatasında stop/kill/start,
task yazımı, aktif app/venv/state/config/ACL mutasyonu ve rollback çalışmaz.

Runtime kontrolüne girişten sonra mevcut stopped-state kapısı, transaction sahiplik
bayrakları ve fail-closed rollback korunur. ForwardShadow’un bağımsız erken stop
fonksiyonu kaldırılmıştır; Super1 cleanup ve rollback yolları faz bayrağıyla
koşulludur.

Builder, signing key’i çözmeden önce sözleşme metadata’sını, kaynak commit/tree/blob
kimliğini, temiz checkout byte’larını ve iki upgrader pinini doğrular. Staging ve ZIP
helper entry’si aynı byte sözleşmesine uymalıdır; hash otomatik düzeltilmez.

## Kanıt ve durum

Yerel Windows PowerShell AST parse: üç production builder/upgrader script’inde hata yok.
Hedef regresyon kümesi: CPython 3.11.9 üzerinde 92 test geçti. Ayrı artifact
kapısında `test_deployment_security.py`, `test_xm_mt5_forward.py`,
`test_check_mt5_flat.py` ve `test_v16_deployment_contract.py` birlikte 170 test
geçti. `test_super1_xm_forward.py` ise değişmemiş baseline candidate hash
uyuşmazlığı nedeniyle 50 setup hatası verdi; bu hata DEPLOY-001 kapsamına
alınmadı ve başarı sayılmadı. Tam koleksiyon 507 testte, aynı checkout'taki
önceden mevcut `V08 manifest SHA-256 mismatch` import hatası nedeniyle durdu.
Gerçek signing, deployment, MT5, broker, görev, süreç veya ACL işlemi
çalıştırılmadı.

Kanıt komutları Windows PowerShell 5.1 ve CPython 3.11.9 ile çalıştırıldı:

- `py -3.11 -m pytest -q tests/test_deploy_001_release_contract.py tests/test_deployment_security.py tests/test_forward_upgrade_transaction.py tests/test_v16_deployment_contract.py tests/test_v16_transfer_runbook.py` — exit `0`, `92 passed`.
- Artifact alt kümesi (`test_deployment_security.py`, `test_xm_mt5_forward.py`, `test_check_mt5_flat.py`, `test_v16_deployment_contract.py`) — exit `0`, `170 passed`.
- Tam koleksiyon `py -3.11 -m pytest --collect-only -q` — exit `1`, `509 collected`, V08 manifest import hatası.
- Birleşik artifact komutu — exit `1`, `170 passed`, `50 errors`; Super1 candidate hash fixture kapısı.
- AST mock harness — exit `0`; gerçek stop gövdeleri, pre-entry guard, partial-stop ve mutation duyarlılığı doğrulandı.
- Builder missing-key probe — exit `1`; sözleşme kapısından sonra beklenen DPAPI key hatasına ulaştı.

`PASS_CODE` yalnız bu yerel test kanıtını ifade eder. `MERGED_MAIN`,
`PASS_EXTERNAL_ACCEPTANCE` ve `DEPLOYMENT_READY` bu teslimle verilmez.
