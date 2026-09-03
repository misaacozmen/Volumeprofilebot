# Super1 — run-017 semantik ret kararı ve dokuzuncu mühendis talimatı — 2 Eylül 2026

**Karar: `NO_GO`.** Run-017'nin byte mührü geçerlidir; semantik, yazılım ve yerel kabul iddiaları geçersizdir. Mimari değerlendirmede yalnız `delivery_byte_integrity_ok=true` kabul edilir. `software_claim_ok=false`, `continuation_fixture_claim_ok=false`, `evidence_payload_claim_ok=false`, `local_acceptance_ok=false`, `run_evidence_ok=false` ve `authoritative=false` uygulanacaktır.

Run-017 manifest SHA-256 `af50cc22961c66bba025025250bcaa6f07a85372b16de3067a39f8b2ddecd8f5`, canonical tree SHA-256 `1ef02906da6b19b8e882ca56679ad76471aa220d372ebb9d95c13dcb75a0cbdb`, dış receipt SHA-256 `66d246001b3cc0be8920f03d55aff848ad4527198375f1307f84c7c12f42ede1` ve attempt kimliği `5809626af7704b46aef311affc3bd4ad` olur. Manifest dışındaki 2.920 üye path/byte/SHA bakımından tutarlıdır; final ağaç 2.921 dosyadır. Run-016 ağaç SHA-256 `abb4d47aa0e5ec30d0622581ac1009ec2cddcc236e8ac560e119379661f57d30` ile değişmemiştir.

Run-017'de raporlanan `515 pytest` kabul edilmez. Mühürlü targeted ve full süreçleri aynı explicit 217 node'u çalıştırmış, iki stdout da `217 passed` yazmış ve iki JUnit aynı node setini taşımıştır. `515 passed` metni yalnız run-013 artefaktında bulunur. V08'in `B=164`, `N=53`, unique `217`, mapping `225` ve iki suite toplam `328` observation sayıları biçimsel olarak doğrudur; davranış kanıtı değildir.

## 1. Yetki, değişmezlik ve çalışma sınırı

Run-017, dış receipt'i, run-016 ve bütün tarihsel canonical/failed sibling'lar byte-exact immutable kalacaktır. Run-017 veya receipt geriye dönük düzeltilmeyecek, silinmeyecek ya da yeniden mühürlenmeyecektir. Yeni rapor exact `predecessor_byte_integrity=PASS` ve `predecessor_semantic_acceptance=REJECTED` yazacaktır.

[Talimat-08](C:/Users/ISAAC/Documents/otobacktestprojesi/docs/SUPER1_MUHENDIS_TALIMATI_08_20260901.md:1) yürürlüktedir. Bu belge denetimde görülen yeni açıkları daraltır; çelişkide Talimat-09'un daha kesin hükmü uygulanır. Architect-owned dosyalar değiştirilemez:

- [V08 required-node manifesti](C:/Users/ISAAC/Documents/otobacktestprojesi/docs/SUPER1_REQUIRED_NODE_MANIFEST_V08_20260901.json:1), SHA-256 `d58ec29ba93cdbc88a9978e1860bf8ae5eaf8fa5913778a6de1b5d1ff2c3fbdf`;
- [V09 semantic contract](C:/Users/ISAAC/Documents/otobacktestprojesi/docs/SUPER1_SEMANTIC_CONTRACT_V09_20260902.json:1), SHA-256 `6710b28110da8ca3289dba53241cf96b93f29bb29af8accf2ed93b550ba5cc33`.

V09 `status=COMPLETE` olup V08'deki 164 davranış node'unun ve 53 evidence-negative node'un tamamını closed-world bağlar: 1.179 exact assertion, 17 typed raw capture türü ve 53 negatif binding vardır. Mühendis node, binding, assertion, capture schema, expected literal, hata kodu, mutation, komut veya gate formülü seçemez; silemez, yeniden adlandıramaz, gevşetemez. Contract uygulanamıyorsa ilgili gate `false` kalır ve attempt nonzero biter. Architect dosyasında byte farkı exact `V09_MANIFEST_HASH_MISMATCH` olur; mühendis yeni sürüm üretemez.

Yalnız yerel kaynak, sentetik fixture, parser, test, runner, reporter ve verifier üzerinde çalış. SSH, kaynak/hedef runtime okuma, MT5 initialize/login, broker bağlantısı veya emir, scheduled task sorgu/değişikliği, servis, transfer, kurulum, gerçek release build, production ZIP, dependency download veya production-key imzası çalıştırma. Yalnız M02 testinin pytest temp root'ta ephemeral TEST key ile ürettiği `TEST_ONLY_SYNTHETIC_FIXTURE` R0/R1 arşivleri serbesttir; successor release veya imza sayılmaz. Aynı mevcut Super1 kampanyası ve aynı XM demo hesabı korunur; init/reset/rollover yoktur. Diğer adaya kod, state, görev, hesap, klasör veya dependency bağlantısı ekleme.

İlk değişiklikten önce run-017'nin mandatory-input ve source input envanterini actual current bytes ile doğrula; source snapshot'ını ayrı read-only baseline olarak al. Tek mismatch varsa kod değiştirmeden nonzero dur. V09 dosyasını yeni mandatory input olarak ekle; run-017'de bulunmadığı için predecessor'a aitmiş gibi gösterme.

## 2. Run-017 semantik kanıtını reddet

Şu olgular yeni regression gate'lerinde exact yakalanacaktır:

- 328/328 observation `entry/remove/restart=0/0/0` ve aynı sentinel state çekirdeğini taşır.
- 1.204/1.204 raw before/after payload çifti semantik olarak aynıdır.
- SDK gerektiren 102/102 observation'da iki faz da `sdk_calls=[]` olur.
- [TypedAdapter](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/super1_observation.py:112) sabit boş payload üretir; writer expected değerlerini actual'dan türetir.
- Reporter ve semantic verifier aynı fallback/default ölçümünü tekrarlar; bağımsız oracle oluşturmaz.
- 53 negatif node'un 47'si gerçek mutation ve sorumlu subprocess çalıştırmaz.
- R02-D testleri gerçek `REMOVE=1` beklerken sealed observation `REMOVE=0`; T01 ve C02 ALLOW gerçek `PENDING=1` beklerken sealed observation sıfır yazar.

Bu kusurlardan biri veya V09'daki forbidden literal/alanlardan biri kalırsa `evidence_payload_claim_ok=false` ve `local_acceptance_ok=false` olur. Sayı, hash ve JUnit PASS bu sonucu değiştiremez.

## 3. Targeted, collection ve gerçek full suite

Çalışma dizini exact `work/backtest` olacaktır. Runner `PYTEST_ADDOPTS` değerini kaldırıp boş olduğunu kaydeder. Üç süreç aynı Python executable, source bytes, dependency environment ve aynı attempt altında ayrı raw stdout/stderr/exit kanıtı üretir.

Targeted argv, V08 manifestindeki `B ∪ N` listesini manifest sırasıyla 217 explicit node olarak açar:

```text
python -m pytest -q <V08_B_UNION_N_EXACT_ORDER> -p scripts.super1_observation --super1-observation-mode record --super1-observation-root <targeted-root> --super1-run-id <run-id> --super1-suite targeted --super1-attempt-id <attempt-id> --junitxml=<targeted-pytest.xml>
```

Full collection argv exact şudur:

```text
python -m pytest --collect-only -q tests -p scripts.super1_observation --super1-observation-mode collect-only --super1-run-id <run-id> --super1-suite collection --super1-attempt-id <attempt-id>
```

Full argv exact şudur:

```text
python -m pytest -q tests -p scripts.super1_observation --super1-observation-mode record --super1-observation-root <full-root> --super1-run-id <run-id> --super1-suite full --super1-attempt-id <attempt-id> --junitxml=<full-pytest.xml>
```

Full argv'de explicit nodeid, `-k`, `-m`, `--ignore`, `--ignore-glob`, `--deselect` veya eşdeğer collection filtresi bulunamaz. Pytest plugin/hook'u node deselect edemez. Collection stdout'undan nodeid multiset bağımsız çıkarılır; full JUnit multiset buna exact eşit, her node exact bir PASS, skip/error/failure/deselected sıfır olur. Targeted JUnit multiset exact `B ∪ N`; full JUnit'in required kesişimi exact `B ∪ N` olur. Targeted ve full observation multisetlerinin her biri exact `B`; `N` observation sayısı sıfır; iki suite toplamı exact 328 olur. Toplam collected/targeted/full test sayıları yalnız bu attempt'ın raw süreç ve JUnit bytes'ından hesaplanır; `515`, `359` veya başka tarihsel sayı sabitlenmez.

Targeted ve full argv, collection/JUnit multiset veya environment farkında V09'daki exact primary kodla fail et. İki farklı dosya adına aynı pytest seçimini yazmak full suite değildir.

## 4. Observation, expected ve bağımsız oracle

[super1_observation.py](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/super1_observation.py:1) içindeki `TypedAdapter._payload` ve bütün boş/sentinel fallback'leri sil. Producer yalnız identity ile gerçek typed raw capture path/bytes/hash referanslarını yazar; `expected`, `actual`, `recomputed`, `measurement` veya `result` yazamaz. Behavior payload'ında yalnız `PASS`, `*_NOT_APPLICABLE`, wildcard, `N/A`, prose contract, default ya da actual kopyası yasaktır.

Exact test API şudur:

```text
checkpoint = actual_evidence.checkpoint()
actual_evidence.record_actual(checkpoint=checkpoint)
```

Fixture seed tamamlandıktan ve test edilen production çağrısından hemen önce checkpoint exact bir kez; bütün domain assertion'larından sonra record exact bir kez çağrılır. Test expected, actual, sayaç, state, raw dict/path, adapter/source, nonce veya checkpoint kimliği veremez. Node çalışmadan önce SUT'nin gerçekten kullandığı concrete resource handle'ları plugin-owned typed registry'ye bağlanır. Missing veya extra handle exact `V09_CAPTURE_SET_MISMATCH` olur; fallback observation üretilemez.

V09'daki node binding hangi capture'ları istiyorsa onların tamamını gerçek fixture kaynağından al:

- execution: instrumented SDK sequence, intent/outbox/execution SQLite primary-key rows+hash+WAL, order-event JSONL ve broker current-order/position/history-order/history-deal readback;
- finalization: gerçek iki-leg fetch/store satırları, first-known metadata ve recursive final/session/marker envanteri;
- filter: prefix relative path, ham file bytes/SHA, cutoff vector, full filter state/rule/features/reason, evidence `observed_at/revision/raw SHA` ve base lifecycle kaynakları;
- M01–M03: gerçek exclusive writer, R0/R1 archive/manifest/signature, transition chain, signed config binding, SQLite backup/WAL ve terminal-R sonuçları;
- O01: gerçek PowerShell process, production policy callback trace/count/result, latch tuple, process gate ve before/after app/state/taskXML/economic envanteri.

Her raw envelope exact `run_id`, `suite`, `nodeid`, `attempt_id`, `scenario_nonce`, `checkpoint_id`, `source_name`, `capture_type`, `resource_instance_id`, `phase`, payload relative path/bytes/SHA taşır. Checkpoint ve record gerçek before/after snapshot alır. Her payload V09'daki exact required alan ve türlerle, `additionalProperties=false` olarak doğrulanır. Sıfır SDK delta yalnız binding exact sıfır istiyor ve başka gerçek state capture'ı mevcutsa geçerlidir. Geçiş bekleyen binding'de zorunlu kaynak ilişkisi değişir; no-op raw kabul edilmez. Her assertion için reporter ve verifier'ın okuduğu scalar pointer kümesi eşit ve dolu olacak; V09'un deterministik ilk-pointer mutation'ı o assertion'ı exact rule-id ile düşürmeden assertion PASS sayılamaz.

Expected yalnız immutable V09 literal/assertion'larından gelir. Runtime actual/recomputed sonucu, test assertion'ı veya producer payload'ı expected oluşturamaz. `expected.update(actual)`, actual hash'ine “independent” etiketi verme, `actual == recomputed` ile tek başına PASS ve yalnız state'in dolu olduğunu kontrol etme yasaktır.

Reporter raw bytes'ı kendi parser'ıyla hesaplar; semantic verifier aynı raw bytes'ı ayrı parser ve ayrı projection ile yeniden hesaplar. Reporter producer'ı; semantic verifier producer/reporter/runner'ı; receipt verifier producer/reporter/semantic-verifier/runner'ı import edemez. Ortak project semantic helper, parser veya constant allowlist'i boştur. AST import graph ile runtime import trace birlikte sealed kanıt olur; dinamik import da yakalanır. External crypto/SQLite kütüphanesi kullanılabilir, fakat project semantic uygulaması paylaşılamaz.

## 5. Zorunlu davranış düzeltmeleri

V09'daki 164 binding'in tamamı targeted ve full suite'te ayrı ayrı geçmeden hiçbir davranış claim'i true olamaz. Aşağıdaki mevcut kısa yollar ayrıca kaldırılacaktır.

**R02:** D-SL ve D-TP akışları checkpoint sonrasında gerçek SDK `REMOVE=1`, `ENTRY=0`, kalıcı `CLOSED_SL/CLOSED_TP` ve cancel kontrolünün economic exit'ten önce oluştuğunu raw kaynaklardan gösterecek. Diğer R02 restart/lost-readback/legacy vakaları V09'daki exact state ve sayaçlara uyacak; R02 production davranışı bu gereklilik dışında değiştirilmez.

**T01:** [Testteki](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/tests/test_super1_xm_forward.py:1188) `fresh_prefix = stale_prefix` silinir ve zaman geri sarılmaz. İlk store yalnız NQ tamamlayıcı segmentiyle eski prefix'i üretir. `market_data_asof=10:25:00`, `knowledge_asof=10:25:10`; prefix VALID ve tek NQ candidate taşır. `10:29:00` guard exact `STALE_PREFIX_NO_SEND`, PENDING=0, REMOVE=0 ve `PRE_SEND_DEFERRED` üretir. `10:29:05` sonrasında NQ+SPX tamamlayıcı fetch aynı store'a ingest edilir; yeni `market_data_asof=10:29:10`, `knowledge_asof=10:29:20`, guard `10:29:30`, cutoff `{nq:10:27,spx:10:25}` olur. Yeni prefix path ve raw SHA eskisinden farklı, order_id aynı; exact bir PENDING, SUBMITTED intent/outbox/readback; same-state yeni client tekrarında toplam PENDING yine bir olur. Bütün zamanlar 5 Ağustos 2026 `America/New_York` bağlamındadır.

**R03/T02:** [Progressive fixture](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/tests/test_capital_forward.py:422) başlangıçta gerçekten eksik iki-leg store kullanır. `10:58`, `11:00:00`, `11:00:07` çağrıları actual scope eksikliğiyle `WAIT_FOR_FINAL_DATA` ve sıfır final/session/marker üretir. Sonra aynı store'a tamamlayıcı iki-leg fetch ingest edilir: requested `market_data_asof=11:00:08`, provider observation `11:00:08.500`, `knowledge_asof=11:00:09`, finalization/decision `11:05:10`; sonuç VALID olur. Repeat ALREADY_FINALIZED ve recursive final tree, session, marker path/bytes/SHA exact değişmez. Future-known ayrı gerçek bar satırı `first_known_time=11:00:10`, knowledge `11:00:09` ile exact `WAIT_FOR_KNOWLEDGE`, excluded=1, doğru max/leg dağılımı ve sıfır artefakt verir. Missing provider/data-known metadata ayrı gerçek fixture ile `WAIT_FOR_FINAL_DATA` verir; `store=object()` kabul değildir.

**C02:** [Production persist/promotion yolu](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/run_super1_xm_mt5_forward.py:640) prefix hash'ini parsed JSON'dan değil ham prefix bytes'ından alır. UNRESOLVED promotion yalnız revision daha büyük, observed-at daha geç, cutoff componentwise nondecreasing ve raw SHA farklıysa strictly newer olur. Dördü birlikte sağlanmadan state/event/SDK değişmez. Before-cutoff, tek `BEGIN IMMEDIATE` transaction içinde CAS `FILTER_UNRESOLVED_DEFERRED -> PRE_SEND_DEFERRED`, create-once event ve transaction sonrası bütün cutoff/risk/global-entry kapılarını yeniden okuyan base lifecycle ile toplam tek PENDING üretir. After-cutoff kalıcı `FILTER_EXPIRED_NO_SEND` ve create-once expiry event üretir; sonradan send'e dönmez. BLOCK kalıcıdır.

Yedi clean-gate node'u `_broker_objects` tek stub'ına indirgenemez. `order_intents`, `broker_execution_states`, ekonomik outbox, current order, position, history order ve history deal kaynakları ayrı fresh root'ta exact bir kayıtla gerçekten seed edilir; diğer altı kaynak sıfırdır. Her vaka exact `FILTER_DEFERRED_TO_BASE_LIFECYCLE`, base lifecycle çağrı sayısı bir, yeni filter intent/event/economic-outbox ve SDK sayıları sıfır, seeded kaynak after-count bir verir. Generic broker helper history deal kaynağını da okur. Persistence node'ları exact `client_1,client_1,client_2` sırasını çalıştırır; BLOCK event toplamı bir, newer-before/after event toplamı iki, same-client repeat yeni event sayısı sıfırdır. Promotion ve strictly-newer node'ları V09'daki exact revision, observed-at, cutoff, raw-SHA, gate sırası ve state sequence değerlerini aynı persistent root üzerinde gerçekten çalıştırır; vaka etiketi silinemez veya aynı gövdeye düşürülemez.

**M01:** [Altı parametrik vaka](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/tests/test_super1_continuation.py:458) ayrı fresh root ve gerçek writer çağrısı kullanır; `del fixture_case` yasaktır. Existing empty/nonempty output üzerinde `write_exclusive_json` gerçekten çağrılır. Test output'u sonradan kendisi değiştirmez. Windows junction gerçekten oluşturulur; oluşturulamaması skip değil INCOMPLETE/nonzero'dur. Her writer çağrısının source ve existing-output before/after relative path+bytes+SHA envanteri exact değişmez ve V09'daki exact kod döner.

**M02:** El yazımı campaign lock, tekrar-karakter hash, `test-python`, tek release ve 1/1 artifact fixture'ını sil. Gerçek campaign-lock producer ve production builder/validator API'leriyle yalnız pytest temp root'ta ephemeral TEST key altında synthetic genesis → R0 → R1 zinciri üret. Synthetic genesis yalnız üçlü gate ile geçer: explicit test flag, approved pytest temp root ve ephemeral public-key binding. R0/R1 ayrı ZIP/manifest/signature bytes taşır; recursive trust, release bağları ve ZIP↔manifest üyeleri 1:1 doğrulanır. Her V09 mutation kendi fresh zincirinde yalnız adındaki gerçek alan/bytes'ı bozar, gerektiği yerde yeniden imzalanır ve exact tek hata kodu verir. Farklı adları aynı server/hash mutation'ına bağlama ve yalnız `errors` dolu kontrolü yasaktır.

**M03:** [terminal-R fonksiyonu](C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest/scripts/super1_terminal_r.py:14) exact `{ordered_full_terminal_rows,last_n,state_sum,risk_scale}` döndürür; runtime ve validator sonucu doğrudan kullanır, tekrar hesaplamaz. External signed campaign-root binding exact account/server/magic, raw Super1 config relative path/bytes/SHA, bundan türeyen epic↔symbol↔reward map, lookback=10, negative_scale=0.75, nonnegative_scale=1.1, terminal_history_days=365 ve entry/OUT/SL/TP sabitlerini verir. Raw campaign lock yalnız gerçek producer schema'sındadır; test-only alan eklenmez. V09 mutation node'u yalnız adındaki gerçek target'ı değiştirir; hepsini `engine_code_hash`, `terminal_history_days`, delivered-at veya generic status mutation'ına indirgemek yasaktır. Bar ve order DB ayrı `wal_autocheckpoint=0`, açık writer ve checkpoint edilmemiş committed son kayıtla test edilir; snapshot her iki son kaydı taşır, WAL'sız raw copy V09'daki ayrı exact continuity koduyla reddedilir.

**O01:** Harness production'daki tek `Invoke-Super1RolloverCatchPolicy` fonksiyonunu çağırır; expected case tablosunu okuyamaz. Independent Python oracle actual'dan expected türetmez ve harness parser/kodunu paylaşmaz. V09'daki 16 vaka için actual input vector, her callback'in `{sequence,name,status,error_code}` sonucu, exact numeric policy/harness/process-gate exit, exact ordered trace, listed callback count=1, absent callback count=0, latch tuple ve before/after inventory doğrulanır. Main-start ile post-start-timeout; watchdog-start ile post-watchdog failure ayrı callback-result/error semantiği taşır. Evidence writer vakası gerçek erişilemez hedef kullanır. Bütün vakalarda `automatic_restart=false`; stopped-safe vakasında iki start callback'i başarılıdır. `both_latch_writes_fail` exact `UNSAFE_UNLATCHED`, iki latch false, policy exit=1 ve process-recreation gate çalıştırılmamış olur.

PowerShell testleri string/substr sayımı yapmaz; `System.Management.Automation.Language.Parser` AST'sini kullanır. Policy çağrısı kaldırılmış ayrı source mutation'ı exact `O01_POLICY_CALL_MISSING` verir. Startup gate broker/MT5 init'ten önce actual state root'ta fatal veya recovery latch'in her birini bağımsız reddeder; fatal-only ve recovery-only ayrı yeni process/root kullanır ve diğer latch absent kalır. Production writer preexisting latch'i overwrite etmeye çalışmaz; structured `latch_preexisting/recovery_preexisting` sonucunu başarı olarak yorumlar.

Calendar, E01–E04, R01 ve diğer V08 behavior node'ları V09 literal/capture binding'leriyle aynen korunur. Bir node yalnız test assertion'ı geçtiği için değil, V09 raw capture ve bağımsız projection assertion'ları exact geçtiğinde kabul edilir.

## 6. Evidence-negative ve anti-no-op kapıları

V09'daki 53 `negative_bindings` üyesinin her biri exact şu işlemleri yapar:

1. known-good fixture'ın ayrı fresh kopyasını üretir ve clean public CLI exit=0 kanıtlar;
2. binding'deki tek `closed_operation` ile tek invariantı bozar; clean ve mutated SHA farklı olur;
3. binding'deki sorumlu public CLI'ı gerçek subprocess olarak çağırır;
4. mutated exit nonzero ve `errors` exact tek `expected_errors_exact` değeri olur;
5. argv, stdout/stderr bytes+SHA ve exit code'u sealed kanıta yazar;
6. final verifier temiz bytes ile manifestteki mutation işlemini yeniden uygulayıp mutated SHA'yı bağımsız doğrular.

`assert case`, truthy string/dict, `any(errors)`, yalnız `status=FAIL`, doğrudan internal helper sonucu veya farklı case adları altında aynı mutation kabul edilmez. Mevcut 47 no-op node gerçek mutation/subprocess node'una çevrilir; nodeid değişmez. Producer/reporter/verifier kaynaklarının AST'sini denetleyen regression gate sabit payload, actual-derived expected, forbidden sentinel, rol ihlali ve targeted/full argv eşitliğini ayrı mutationlarla yakalar. Birden çok kusur varsa V09 `validation_precedence` sırası uygulanır ve yalnız ilk sınıfın tek primary kodu döner.

## 7. Attempt, verifier ve publication state machine

Tek `attempt_id` temp dizin oluşturulmadan önce üretilir. Aynı değer temp/failed adlarında, V09 `generated_top_level_json_require_run_attempt` listesindeki bütün JSON'larda, observation/raw envelope'larda, prepublish/final stdout JSON'da ve receipt candidate/final receipt'te zorunludur. Byte-exact V08/V09 architect kopyalarına generated identity eklenmez. Prepublish stdout henüz bulunmayan manifest/final path'i iddia etmez; exact intended basename, preseal inventory/tree SHA ve verifier source SHA taşır. Final stdout exact canonical basename, resolved parent/final path, manifest/tree SHA taşır. Receipt-verifier stdout aynı kimlik ve hashleri dışarı verir fakat receipt'in içine gömülmez. `scenario_nonce` ve `checkpoint_id` 328 parent observation genelinde ayrı ayrı attempt-global unique; raw child'lar kendi parent kimliklerine exact eşittir.

Semantic ve receipt verifier source dosyası yoksa fail-closed `*_SOURCE_MISSING`; varsa process metadata, gömülü stdout ve receipt'teki bound SHA ile exact eşleşir. `is_file()==false` durumunda source-SHA kontrolünü atlama. Receipt verifier nested prepublish/final stdout'unu decode edip bu source SHA'ları kendi okuduğu bytes ile doğrular. Final verifier canonical basename ile birlikte resolved parent ve exact final path'i kontrol eder.

İlk boş run kimliği canonical, temp, receipt, failed-run ve failed-receipt sibling adlarının tamamı yoksa boştur. Şu an bu koşulu sağlayan ilk ad `run-018`'dir; çalıştırma anında algoritma tekrar hesaplar. Kimlik seçiminden publication sonuna kadar exclusive reservation tut; aynı volume no-overwrite atomic rename kullan; mevcut hedefi değiştiren `os.replace` yasaktır.

Şu sekiz failed sibling için koşudan önce ve sonra relative üye sayısı+canonical tree SHA kaydet ve exact değişmezlik ara:

- `run-010.failed-observation-writer`;
- `.run-011.tmp-5cc60164142540f79a9454fbecb3a973.failed-59f24f7543e2`;
- `.run-011.tmp-84668ea8cacf4f4fa2819825f3b69ea7.failed-09e863077f4e`;
- `.run-013.tmp-4b8e9c6fb0aa4245a0661778e380b6af.failed-4b8e9c6fb0aa4245a0661778e380b6af`;
- `.run-013.tmp-ac1726b343eb4483aac442e25d1562ce.failed-ac1726b343eb4483aac442e25d1562ce`;
- `run-015.failed-602fd5ca6dd54d8b92624ebb5a6fb1ce`;
- `.run-016.tmp-1f080fab02694a62a4f949c4331e0d6c.failed-1f080fab02694a62a4f949c4331e0d6c`;
- `.run-016.tmp-c0a45609923b4c67b2b05889d8301b45.failed-c0a45609923b4c67b2b05889d8301b45`.

Publication sırası exact şöyledir: exclusive reservation ve temp create-new → candidate üyelerini yaz/fsync → kendisi ve manifest hariç preseal'i yaz/fsync → prepublish PASS → manifesti son üye olarak create-new/fsync → temp directory fsync → same-volume no-overwrite canonical rename ve parent fsync → read-only final verifier PASS → receipt candidate create-new/fsync → read-only receipt verifier PASS → no-overwrite final receipt rename ve parent fsync. Herhangi bir local, semantic, final veya receipt gate FAIL ise authoritative canonical teslim/receipt bırakılamaz; aynı attempt kimlikli failed alan korunur ve süreç nonzero çıkar.

## 8. Claim formülleri ve teslim

Exact formüller V09 `gate_formula` alanından okunur; runner kendi formülünü tanımlayamaz. Özet zorunluluklar:

```text
software_claim_ok = Talimat-08 production gates
                    AND true_full_suite_ok
                    AND ilgili V09 behavior bindings

continuation_fixture_claim_ok = bütün M01 AND M02 AND M03 V09 bindings

evidence_payload_claim_ok = V08/V09 exact
                            AND execution contract
                            AND 164 behavior bindings x targeted/full
                            AND 53 gerçek negative mutation
                            AND identity/component independence
                            AND mandatory inputs/baseline/predecessor/failed siblings
                            AND portability/preseal/truthful reports

local_acceptance_claim_ok = software_claim_ok
                            AND continuation_fixture_claim_ok
                            AND evidence_payload_claim_ok

run_evidence_ok = local_acceptance_candidate_ok
                  AND delivery_integrity_ok

authoritative = run_evidence_ok
                AND post-rename receipt_verifier_ok
                AND publication_contract_ok
```

Acceptance matrix her requirement/node için class, V09 scenario, required capture'lar, manifest expected assertions, reporter projection, verifier projection, targeted/full JUnit ve observation sonucu, raw refs, exact error ve gate sonucu taşır. Yalnız count/boolean özeti kabul değildir. Sealed teslim V08 ve V09 exact kopyalarını ve hash doğrulamasını mandatory input olarak içerir. Canonical JSON ve receipt candidate yayın öncesinde `authoritative=false` kalır; final receipt no-overwrite rename+fsync sonrasında authority dışarıdan yeniden hesaplanır, hiçbir sealed dosya geriye dönük değiştirilmez ve receipt kendi verifier stdout'unu içermez.

Release proposal'daki üç envanter ayrımı korunur. Successor `INCOMPLETE_NOT_BUILT`, `NOT_SIGNED`, `SUCCESSOR_CONTRACT_CHANGE_REQUIRED` kalır; M02 TEST fixture bunu değiştiremez. Gerçek source/target/broker/task/deployment/release işlemleri çalıştırılmadığı için yerel kapılar geçse bile final readiness exact `overall=NO_GO`, `decision=NO_GO`, `safe_to_apply=false`, `apply_allowed=false`, `proven=false`, `parity=false`; runtime/fence/snapshot/environment `NOT_VERIFIED`; broker/pending/fill-exit/task/deployment/release `NOT_RUN`; successor release `NOT_BUILT`, signature `NOT_SIGNED` olacaktır. `proven=false/parity=false` bilgi alanıdır; teknik NO_GO gerçek ortam aşamalarının çalıştırılmamış olmasıdır.

Yeni mimar talimatı gelmeden SSH, MT5/broker preflight, transfer, kurulum, scheduler, release, imza veya deployment aşamasına geçme.
