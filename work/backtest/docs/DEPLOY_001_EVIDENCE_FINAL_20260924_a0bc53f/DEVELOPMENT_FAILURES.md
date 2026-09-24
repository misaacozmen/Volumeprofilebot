# Saklanan geliştirme koşuları

Bu kayıtlar final başarılı koşudan ayrı tutulur; hiçbir başarısız koşu silinip başarı gibi sunulmamıştır.

1. **Temiz checkout, ham girdi staging öncesi:** architecture + Super1 forward + candidate-artifact testleri; `57 passed, 1 failed`, exit `1`. Tek hata `test_checked_in_threshold_artifact_has_current_provenance`; fixture eksikliğinden değil, temiz checkout’ta 144 pinned `data/raw/nq` girdisi henüz stage edilmediğinden `Missing provenance input`. Ham girdiler manifest SHA’ları ile stage edildikten sonra aynı hedef küme (environment scanner testleriyle birlikte) `64 passed`, exit `0`. Tam traceback `prestage-failure.log`, komut `prestage-failure-command.txt`.
2. **Environment scanner regresyon iterasyonları:** ilk testte beklenen dış-root gösterim biçimi Windows çalışma alanındaki yerel `.github/workflows` görünümüyle uyuşmadı (`9 passed, 1 failed`); ardından repo-root fallback eklendiğinde sentetik unknown YAML testinin `CLEAN` dönmesi ortaya çıktı, çünkü directory walker yalnız `*.py` dosyaları topluyordu (`10 passed, 1 failed`). Walker `.py/.yml/.yaml` tarayacak şekilde düzeltildi; final hedef küme `11 passed`. Bu iki ara stdout araç çağrısında kalmış, ayrı ham dosya olarak saklanmamıştı; hata assertion’ları ve nedenleri burada açıkça kaydedildi. Final suite’te `test_nested_scan_finds_unknown_key_in_repository_root_workflow` ve gerçek workflow scan’i geçti.

İlk staging öncesi başarısız pytest’in ham çıkışı ayrıdır; 144 ham kaynağın hiçbirisi bu kayıt üretimi için değiştirilmedi.
