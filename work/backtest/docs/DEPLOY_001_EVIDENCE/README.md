# DEPLOY-001 kanıt paketi

Bu klasör yalnız `DEPLOY_001_ARCHITECTURE_DECISION.md` içindeki düzeltme
adayının exact commit’i için üretilen ham koşu çıktısını içerir. `targeted`
JUnit/stdout ve `collection` stdout ayrı koşulardır; biri diğerinin yerine
kullanılmaz. `manifest.json` dosya SHA-256’larını ve koşu commit/tree kimliğini
bağlar.

Gerçek signing, deployment, MT5/broker bağlantısı, scheduled task, servis,
transfer veya ACL işlemi bu koşulda çalıştırılmamıştır.
