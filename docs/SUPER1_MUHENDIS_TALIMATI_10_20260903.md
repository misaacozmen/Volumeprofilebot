# Super1 — run-025 ret kararı ve onuncu mühendis talimatı — 3 Eylül 2026

**Hüküm:** `run-025.failed-61601d36e1cc449c9f84a99574ed2bcb` doğru biçimde canonical dışı kalmıştır; `NO_GO` devam eder. `53/53 PASS` negatif özeti kabul kanıtı değildir. Bu talimat bir readiness/GO koşusu yaptırmaz. Önce V09'da eksik bırakılmış exact nested raw şemaları ile 1.179 kuralın raw bağımlılık/formül haritasını incelemeye hazırlar; mühendis bu seçimleri kendi başına kesinleştiremez.

## 1. Yetki sırası ve değişmez girdiler

İlk dosya değişikliğinden önce aşağıdaki actual bytes SHA-256 değerlerini doğrula. Tek uyuşmazlıkta hiçbir dosyayı değiştirmeden nonzero dur:

- `docs/SUPER1_REQUIRED_NODE_MANIFEST_V08_20260901.json` → `d58ec29ba93cdbc88a9978e1860bf8ae5eaf8fa5913778a6de1b5d1ff2c3fbdf`;
- `docs/SUPER1_SEMANTIC_CONTRACT_V09_20260902.json` → `6710b28110da8ca3289dba53241cf96b93f29bb29af8accf2ed93b550ba5cc33`;
- `docs/SUPER1_MUHENDIS_TALIMATI_09_20260902.md` → `1dba834113f24c947a6eeedfa2115a4bd16e7bb54661b6585d3ad2dd8742aa39`;
- `docs/SUPER1_SEMANTIC_DISCOVERY_CONTRACT_V10_20260903.json` → `52a2001633c1f068f5ab9fcd1d597cf0052d8261ee50ae1fd8cb5699cc02490a`.

V08 node üyeliği, V09 literal değerleri, davranış gereksinimleri, gate formülleri, güvenlik sınırı ve publication sözleşmesi değişmez. V09 bytes immutable olmakla birlikte tek başına executable semantic spec değildir. V10 discovery sözleşmesi yalnız eksikleri ve çelişkileri incelemeye hazırlar; hiçbir proposal pointerı, formülü, role seti, şeması veya mutation recipe'si architect-approved sayılmaz. V08, V09 ve Talimat 09'u düzenleme, yeniden üretme veya kimlik alanı ekleyerek kopyalama.

Şunların tamamını byte-exact immutable tut:

- `outputs/super1_readiness_20260831/run-017` ve receipt'i;
- `outputs/super1_readiness_20260831/run-025.failed-61601d36e1cc449c9f84a99574ed2bcb`;
- bütün mevcut canonical run'lar, receipt'ler, `.tmp-*.failed-*`, `run-*.failed-*`, failed-receipt sibling'lar ve baseline;
- bütün architect dosyaları.

Koşudan önce readiness output dizinindeki bütün mevcut üyelerin normalized relative path, byte count ve SHA-256 tabanlı canonical tree envanterini al. Koşudan sonra aynı kapsamı yeniden hesapla. Tek fark varsa discovery paketi de geçersizdir. `run-025` içini geriye dönük düzeltme; içindeki `SEALED_CANDIDATE` etiketi yanlış olsa da bytes korunacaktır.

`run-025` input inventory'sinde readiness runner SHA'sı `1831eaab561ea480937209ae1e8a9f40dcc7e1cecd163b81fd3a98a062caeea0`, current source SHA'sı `a731aae223a329a7bd3bc2fca6fdd23a9d13350bcac1debb9caf128e2af61f28`'dir. Current runner'ı run-025'i üretmiş source diye raporlama. Diğer dört semantik bileşenin current SHA'sı run-025 input inventory ile eşleşmektedir; yine de yeni source baseline'ını actual current bytes ile bağımsız al.

Run-025'in 9.459 satırlık input inventory'si attempt içinde before/after eşittir. Sonradan yalnız runner değişmiştir: 54.013 byte'tan 54.059 byte'a `+46` byte; bir changed, sıfır missing input. Bu post-attempt drift'tir. Discovery boyunca current runner allowlist dışıdır ve SHA'sı `a731aae223a329a7bd3bc2fca6fdd23a9d13350bcac1debb9caf128e2af61f28` olarak değişmez kalacaktır.

## 2. run-025 için kabul edilen ve reddedilen kanıt

Exact kabul edilen gözlem şudur:

- targeted JUnit `217/217`, full JUnit `515/515` PASS;
- suite başına 164, toplam 328 behavior parent observation;
- suite başına 527 capture pair;
- 1.179 unique kural, iki suite toplam 2.358 temiz assertion değerlendirmesi;
- 192 assertion PASS, 2.166 FAIL, 2.018 `derived=null`;
- 328/328 semantic node FAIL;
- 1.179/1.179 counterfactual `V09_COUNTERFACTUAL_NOT_EXECUTED`;
- `semantic_acceptance=false`, `counterfactual_semantics_ok=false`, bütün readiness/apply/proof claim'leri false;
- canonical run-025 ve `run-025.verification.json` yoktur.

Karantina 227.081 dosya ve 1.103.393.378 byte'tır. Bunun 222.346 dosyası 53 `negative-cases/run-018-negative-*-22320` dizinindedir; negative JSON'da 106 mutlak Python executable path geçer. Gelecekte negative scratch tree sealed pakete klonlanmayacak; yalnız minimal detached fixture, gerçek stdout/stderr bytes ve mutation receipt tutulacaktır.

Aşağıdakileri PASS kanıtı olarak kullanma:

1. `negative-mutation-results.json` içindeki 53 PASS. Runner expected hatayı structured exact array olarak çözmek yerine birleşik metinde substring arıyor, beklenen exact exit yerine yalnız nonzero kabul ediyor, stderr SHA'sını boş bytes olarak sabitliyor, clean receipt/verifier artefaktlarını kendi yazıyor ve final verifier mutation'ları replay etmiyor. Altı reporter clean fixture'ı suite başına yalnız bir behavior card taşıdığı halde exit 0 almıştır. Beş publication case'i requested case id'den expected metni seçip invariantı doğrulamadan aynı metni basmaktadır; sonuçtaki `primary_error` observed outputtan değil expected[0]'dan yazılmaktadır.
2. Evidence reporter exit `0`. Reporter `semantic_acceptance=false` iken `all_required_nodes_observed` üzerinden success dönüyor.
3. Mevcut raw payload'lar. Producer test stack frame/local değerlerini tarıyor, fixture root tahmin ediyor ve gerçek SUT handle bulunmadığında varsayılan payload yazıyor.
4. Mevcut semantic verifier. 1.179 assertion'ı bağımsız türetmiyor; rapor ile claim'in aynı false değerini karşılaştırmakla yetiniyor.
5. `v09-semantic-results.json` içindeki `negative_semantics_ok=false` ile `negative-mutation-results.json`/`claim-recomputation.json` içindeki true değerlerinin herhangi biri. Aynı attempt içindeki bu çelişki nedeniyle üst claim daima false kalır.

Somut fail-open ve fallback kaynaklarını kaldırmadan yeni kanıt üretme:

- `scripts/super1_observation.py`: frame/local taraması, `_field`, `_fixture_root`, generic `_typed_payload`, `fixture-observation`, `observed-filter`, `test-boundary`, `observed-policy`, boş release/rollback/AST ve default timestamp/risk üretimi;
- `scripts/super1_evidence_report.py`: recursive leaf-name `_find`, eksik algoritmalar, `EQ_REF/NE_REF` sağ pointerını string literal gibi kullanma, koşulsuz false DSL branch'leri ve semantic başarısızlıkta exit 0;
- `scripts/verify_super1_evidence_run.py`: assertion evaluator bulunmaması, missing artefaktlarda `except: pass`, false==false claim eşleştirmesi ve negative/counterfactual replay yokluğu;
- `scripts/run_super1_negative_cases.py`: hardcoded `run-018-negative-*`, fabricated clean fixtures, substring error matching, any-nonzero kabulü ve gerçek stderr kaydı yokluğu;
- `scripts/run_super1_run010_evidence.py`: 1.179 placeholder FAIL satırı yazma, counterfactual/continuation/evidence sonuçlarını sabitleme ve V09 `gate_formula` yerine el yazımı claim mantığı.

Parent card refindeki envelope `bytes/sha256`, envelope exact schema/type, payload field type/canonical serialization, nested closure ve multiplicity mevcut reporter/verifier tarafından tam doğrulanmıyor. Bu eksikler discovery schema/ledger verifier'ında kapatılmadan raw structural PASS yazma.

Gelecek readiness çalışmasında ayrıca şu publication yolları zorunlu düzeltilecektir; bu discovery aşamasında runner'ı değiştirme: semantic gate'lerden önce bütün JSON'lara `SEALED_CANDIDATE` yazıp failed ada aynı statusle taşıma; final receipt rename+parent fsync sonrasında external receipt verifier çalıştırmadan authority iddia etme; mutlak `final_path/resolved_parent` sızdırma. Sealed run ve receipt bytes'ındaki `authoritative=false` V09'a göre doğrudur; authority yalnız post-rename external recomputation fact'idir.

## 3. Bu aşamanın sınırı

Bu çalışma yalnız `SUPER1_SEMANTIC_DISCOVERY_CONTRACT_V10_20260903` discovery paketini üretir. Şunları yapma:

- semantic reporter'ı tamamlandı sayma veya 1.179 kuralı uygulama;
- semantic verifier'ı tamamlandı sayma;
- 1.179 counterfactual'ı acceptance amacıyla çalıştırma;
- 53 negative gate'i yeniden kabul etme;
- readiness runner çalıştırma, readiness run kimliği ayırma veya canonical/receipt yayımlama;
- production davranışı, Super1 strateji parametreleri, kampanya, config, hesap, server, magic, state veya zamanlama değiştirme.

Yalnız aşağıdaki dosyalarda discovery amacıyla değişiklik yapabilirsin:

- `work/backtest/scripts/super1_observation.py`;
- `work/backtest/tests/v08_helpers.py`;
- V08 behavior node'larını taşıyan altı test dosyasında yalnız fixture/factory oluşturma ve concrete resource registration kodu: `test_capital_forward.py`, `test_super1_calendar.py`, `test_super1_continuation.py`, `test_super1_rollback_harness.py`, `test_super1_xm_forward.py`, `test_xm_mt5_forward.py`;
- yeni `work/backtest/scripts/build_super1_v10_discovery.py`;
- yeni `work/backtest/scripts/verify_super1_v10_discovery.py`;
- yalnız bu iki discovery scripti ve registration altyapısını sınayan yeni test dosyası.

Bu allowlist dışındaki current source bytes değişmeyecektir. Bir concrete SUT kaynağını bağlamak production değişikliği gerektiriyorsa production'a dokunma; ilgili capture/rule'u exact `BLOCKED_*` olarak raporla.

SSH, kaynak/hedef sunucu, MT5 initialize/login, broker ağı veya emir, scheduled task sorgu/değişikliği, service, transfer, kurulum, deployment, rollover, gerçek release build, dependency download ve production-key imzası yasaktır. Yalnız pytest temp root'taki mevcut sentetik fixture'lar kullanılabilir. Aynı mevcut Super1 kampanyası ve XM demo hesabı korunur; init/reset/rollover yoktur; diğer adayla hiçbir bağ kurulmaz.

## 4. Concrete resource registration'ı düzelt

Producer test gövdesinden expected/actual alamaz ve stack/local tarayamaz. Plugin-owned typed registry kullan. Davranış testinde görünen API exact şu iki çağrı olarak kalır:

```text
checkpoint = actual_evidence.checkpoint()
actual_evidence.record_actual(checkpoint=checkpoint)
```

Test gövdesi raw bytes/path, adapter, expected, actual, measurement, projection, state summary, counter, nonce veya checkpoint kimliği veremez. Her SUT resource, onu oluşturan pytest fixture veya helper factory içinde, test gövdesine dönmeden önce registry'ye kaydolur. Inline stub/resource kurulumlarını gerekirse davranışı değiştirmeden named fixture/factory'ye taşı; factory gerçek test nodeid'ini `request.node.nodeid` üzerinden alır.

V09 behavior binding'lerinden suite başına 527 `(nodeid,capture_type)` slotu çıkar. **527 resource-instance sayısı değildir.** Her slot, public SUT yolunun gerçekten kullandığı bütün concrete instance'ları kaydeder; her instance'ın filename/array indexinden bağımsız, nonempty ve stable bir `resource_role` değeri olur. Progressive finalization gibi bir node aynı capture type altında `valid`, `incomplete` ve `conflict` SQLite kaynaklarını ayrı role/instance olarak korur; bunları ilk bulunan DB'ye veya aggregate bir handle'a indirme.

Handle, public SUT çağrısının gerçekten kullandığı object/path/database/raw blob/subprocess record/broker stub olmalıdır. Adapter id capture type ile birebir eşleşir; test-specific adapter veya callback kabul edilmez. `resource_instance_id` plugin tarafından registration anında üretilir, attempt içinde global unique olur ve before/after boyunca aynı kalır. Role seti discovery'de yalnız `architect_approved=false` proposal'dır. Missing veya ambiguous resource için tahmin/fallback üretme; underlying sentetik test akışını çalıştır, ilgili slotu `BLOCKED_*` kaydet, fakat o slot için parent observation/raw payload yazma.

Şunları kaynak kodundan tamamen kaldır ve regression ile yasakla:

- `inspect.currentframe` ve frame/local/global taraması;
- ilk ada uyan dict/attribute seçimi;
- arbitrary `Path` local'ından fixture root tahmini;
- repository walk ile SUT kaynağı tahmini;
- capture exception'ını yutup boş array/object yazma;
- actual invocation yerine current wall clock kullanma;
- `fixture-observation`, `observed-filter`, `test-boundary`, `observed-policy`, yalnız `argv=["python"]`, empty-release SHA, boş rollback trace, boş AST veya default `risk_scale=1.1` üretme;
- `sent` sayısından sahte SDK çağrı listesi oluşturma.
- `_plain` depth/item limitiyle raw veriyi kesme veya semantic summary'ye dönüştürme.

Producer source-native bytes'ı blob olarak saklar; semantic parse yapmaz. Capture'a göre actual SQLite backup+WAL, filesystem member, stdout/stderr, calendar source, archive/manifest/signature/trust root, PowerShell source, JSONL, invocation args/response, SDK request/response ve broker readback blob ref'leri `raw-blob-inventory.json` ile bağlanır. DB row, filesystem tree, validator state veya callback sonucu producer tarafından PASS/fact projection'a çevrilmez; iki future engine bu bytes'ı ayrı parse edecektir.

Cardinality şu biçimde raporlanır:

- package genelinde exact 527 capture-role-set proposal satırı; her satır targeted ve full role setini ayrı taşır;
- her suite için 164 node registration-status satırı; status `CAPTURED` veya `BLOCKED`;
- complete proposal'da her 527 slot `PROPOSED_SINGLE_CANDIDATE`, targeted/full role setleri exact eşit ve captured instance sayısı bu role cardinality toplamına exact eşittir;
- instance sayısı en az 527 olabilir, fakat önceden 527'ye sabitlenemez;
- raw envelope ve blob sayısı actual captured instance/phase/blob-role sayısından yeniden hesaplanır; 1.054 gibi hardcoded toplam kullanılmaz;
- yalnız `CAPTURED` node parent observation üretir; complete durumda 164/suite ve iki suite genelinde 328 parent olur;
- üretilen bütün parent `scenario_nonce/checkpoint_id` değerleri attempt-global unique; raw child identity, resource role ve instance id parent/ledger ile exact bağlıdır.

Identical before/after yalnız binding exact `UNCHANGED` veya sıfır delta bekliyorsa ve aynı node'da başka concrete state capture'ı gerçek çalışmayı ispatlıyorsa kabul edilir. Transition beklenen capture no-op ise proposal `BLOCKED_SOURCE_BEHAVIOR_MISMATCH` olur.

SDK row en az V10 sözleşmesindeki closed schema'yı taşır. `action_raw` integer değeri actual request'ten; `trade_action_pending_raw=5` ve `trade_action_remove_raw=8` actual synthetic MT5 stub constant'ından capture edilir. Sayaç string contains ile değil integer exact equality ile hesaplanmak üzere haritalanır. V09'da 102 SDK delta assertion vardır: 69 right=0, 33 nonzero; nonzero dağılımı 22 PENDING ve 11 REMOVE'dır. İki probe suite'te 138 zero ve 66 nonzero değerlendirmeyi türetecek gerçek raw çağrı bulunmadan mapping complete yazma.

## 5. V09 mimari boşluğunu proposal olarak doldur

V09'da 1.179 assertion'ın tamamında `source_json_pointer=/payload`, tamamında counterfactual pointer placeholder'dır. 764 generic named-fact rule 173 fact pathine dağılır. 230 `(left,capture-set,op)` grubunun 61'inde birden fazla right literal vardır; isim/capture template'iyle authoritative formül üretilemez. Ayrıca 39 `closed_object*` ve iki `closed_member_object` field'ın nested key/type şeması yoktur. Bu nedenle engineer-generated hiçbir mapping satırı `UNAMBIGUOUS`, `APPROVED` veya executable contract sayılamaz.

### 5.1 Capture role ve raw schema proposal

`capture-role-set-proposal.json` exact 527 `(nodeid,capture_type)` satırı taşır. Her satır targeted/full `resource_role` setlerini, source citations, blocker'ları, `architect_approved=false` ve yalnız `PROPOSED_SINGLE_CANDIDATE` ya da exact `BLOCKED_*` statusunu içerir. Actual captured instance cardinality bu role setlerinden hesaplanır.

`raw-schema-bundle-proposal.json` gerçek JSON Schema Draft 2020-12 bundle'dır. Root binding unique key'i `(nodeid,capture_type,resource_role,phase)`, value'su exact `schema_id` refidir. Bütün object'ler `required` ve `additionalProperties=false`; array'ler item schema ve deterministic order/cardinality; varyantlar closed discriminator+`oneOf/const`; dinamik map'ler unique/deterministically sorted closed `{key,value}` row array'i; SHA-256, RFC3339, normalized path, finite number, duplicate JSON key ve RFC8785 kuralları explicit olur. Her captured resource phase'i exact bir root schema binding taşır. Observed örnekler tek başına schema authority değildir; production source model, SQLite DDL, public serializer veya public harness serializer exhaustiveness citation zorunludur.

`raw-blob-inventory.json` actual source bytes'ın suite/node/capture/resource_role/instance/phase/blob_role/path/bytes/SHA/media type bağını taşır. Summary-only payload, truncation, depth limiti veya semantic projection yasaktır.

V09 forbidden raw key listesi ile required capture alanları arasındaki şu altı konum bir architect-contract çelişkisidir:

- `snapshot_validation:/source_tree`;
- `terminal_r_evaluation:/last_n`;
- `terminal_r_evaluation:/ordered_full_terminal_rows`;
- `terminal_r_evaluation:/risk_scale`;
- `terminal_r_evaluation:/state_sum`;
- `rollback_policy:/input_vector`.

Discovery bunları yalnız source-native **candidate** olarak envantere alır ve conflict blocker yazar; semantic input olarak onaylamaz ve V09'a istisna uygulamaz. Daha sonraki architect-owned execution contract exact source locationı onaylamalı veya raw field'i yeniden adlandırmalıdır. Aynı key'ler test summary'sinde, parent projection'da veya reporter/verifier arası actual aktarımında yasaktır.

### 5.2 Per-rule semantic derivation proposal

`semantic-derivation-proposal.json` exact 1.179 unique `rule_id` satırı taşıyacaktır. Key seti V09 behavior assertion rule setiyle birebir aynı; eksik/extra/duplicate yasaktır. Her satır V10 `semantic_derivation_proposal_entry.required_exact` alanlarının tamamını içerir.

Her kural için:

1. nodeid, scenario_id, assertion index, algorithm, op ve left pointer V09 ile byte-semantik exact eşleşir;
2. `dependency_specs`, raw konumu incidental array indexiyle değil exact capture type, resource role, phase, schema id, dependency kind ve typed logical selector AST ile tanımlar; desteklenen kind'lar scalar, array membership/order, object presence, raw blob ve SQLite relation'dır;
3. targeted/full `probe_result` ayrı raw bytes'ta selectorın çözdüğü concrete JSON pointer veya SQL `{table,primary-key,column,type}` erişimlerini, kaynak blob SHA'sını ve derived value type/SHA'sını taşır; filter/sort/discriminator sırasında okunan bütün scalar'lar access setine dahildir;
4. `/payload`, `${...}`, wildcard, recursive key-name arama, first-match veya raw expected-value search dependency değildir;
5. EQ_REF/NE_REF için nonempty ayrı `reference_dependency_specs` ve `right_formula_ast` zorunludur; diğer op'larda right formula null olur. UNCHANGED/CHANGED right kind `NONE`, reference ops `SAME_BINDING_DERIVED_REFERENCE`, diğer kullanılan ops `V09_LITERAL` olur;
6. `left_formula_ast` ve gerektiğinde right formula yalnız V10 proposal DSL'ini kullanır. Bu DSL henüz architect-approved formal semantics değildir; operator arity/type/cardinality/collation/missing rules sonraki execution contractta mühürlenecektir;
7. aynı assertion'ın right/expected/projection subtree'sine `V09_CONST_REF` yasaktır. Formula root constant olamaz; bütün declared dependency'ler kullanılır. IF/CASE raw predicate okur, exhaustive ve type-equal olur; default expected literal branch yasaktır;
8. `DERIVE_DOMAIN_OUTCOME` exact V09 output literalini yalnız nontrivial raw branch predicate'leri sağlandıktan sonra terminal sonuç olarak önerebilir; outcome literalini predicate/default/PASS sentinel olarak kullanamaz;
9. source citations içinde en az bir `SEMANTIC_OWNER` public production/public harness kaynağı ve ayrı bir `ADAPTER_OR_SOURCE_MODEL` kaynağı bulunur. Davranış testi veya assertion tek semantic authority olamaz;
10. targeted/full probe candidate formula ile ayrı raw bytes'tan aynı V09 expected sonucu önerir; test assertion, JUnit failure text, producer summary, reporter output veya verifier output input değildir;
11. her satır exact bir rule-specific, format-valid `counterfactual_witness_proposal` veya null+BLOCKED taşır;
12. `architect_approved=false` her satırda zorunludur; complete proposal bile engineer self-approval oluşturmaz.

`PROPOSED_SINGLE_CANDIDATE` satırında dependency/formula/probe/witness alanları doludur. Raw alan, role, schema, selector, formula, citation veya witness belirsizse seçim yapma: ilgili alan null/empty, nonempty `ambiguities`, exact `BLOCKED_*` ve birebir `unresolved-rules.json` kaydı üret. Expected'e en yakın raw alanı seçmek, expected literal/hash aramak, gerçek branch yerine hardcoded mapping yazmak veya aynı formula'yı farklı case adları altında kopyalamak yasaktır.

## 6. Counterfactual tasarımını sabitle; henüz çalıştırma

Bu discovery aşamasında counterfactual acceptance çalıştırılmaz. Proposal'da source policy exact şöyledir:

- targeted ve full temiz değerlendirme zorunlu olacaktır: engine başına 2.358 assertion;
- unique counterfactual corpus exact 1.179 case'tir ve source suite `targeted` olacaktır;
- global first-capture, always-after, lexicographic scalar veya primitive-type-only mutation yoktur. Her rule kendi architect-approved witness recipe'sini gerektirir;
- proposal recipe exact dependency id, capture type, resource role, phase, schema id, logical target locator, mutation op/operand source, schema-validity expectation, concrete expected error ve allowed diff rollerini taşır;
- bool/int/finite number yanında RFC3339 `+1 second`, SHA first-nibble flip, format dışı olmayan relative-path rename, architect-candidate enum alternate, array insert/delete/swap, optional-object-key add/delete, copy-before-to-after, schema-valid after mutation, typed SQLite table/PK/column update-insert-delete ve rebuilt raw-blob recipe sınıfları kullanılır;
- plain string `+#cf` SHA/RFC3339/path/enum'a uygulanmaz. Required key silip schema PASS beklemek yasaktır. SQLite raw bytes rastgele flip edilmez; detached DB typed operationla yeniden üretilir;
- future execution'da aynı detached targeted raw kopya hem reporter hem bağımsız semantic verifier public CLI'ına subprocess olarak verilecek;
- clean iki engine PASS ve logical/resolved access setleri eşit/dolu olacaktır. Mutated iki engine exit exact `1` ve decoded ordered error array exact `["V09_SEMANTIC_CHECK_FAILED:<concrete-rule-id>"]` vermeden case PASS olmayacaktır; schema/identity/hash/başka semantic hata kabul edilmez;
- post-mutation raw Draft 2020-12 schema PASS kalır. Yalnız selected raw blob; blob bytes/SHA; payload-ref bytes/SHA; envelope `payload_bytes/payload_sha256` ile envelope bytes/SHA; parent `raw_sources` phase `bytes/sha256/payload_bytes/payload_sha256`; case-local manifest/tree hashleri değişebilir. Hardlink/shared mutable copy yasaktır;
- final verifier 1.179 case'i aynı mutation algoritmasıyla bağımsız replay edecektir.

V09'un körlemesine `after` bağladığı dört `/state_before` kuralı ve zero-count empty-array kuralları scalar mutationla çözülemez; bunları uygun phase/dependency-kind recipe ile proposal'a yaz veya BLOCKED bırak. Bu aşamada `semantic-counterfactual-results.json` üretme ve placeholder FAIL'leri yeni kanıt gibi kopyalama. Yalnız architect-approved olmayan witness proposal yaz.

## 7. Discovery probe'ları

Current source baseline alındıktan ve registration refactor tamamlandıktan sonra aynı local environment içinde şu üç process classını ayrı çalıştır:

1. V09 exact targeted multiset: 164 behavior ardından 53 negative node, her biri exact bir kez. V08 node seti immutable olduğu için targeted JUnit exact 217 PASS olmalıdır.
2. `tests` üzerinde `--collect-only -q`, hiçbir selector/deselection olmadan full collection inventory.
3. `tests` üzerinde full pytest, hiçbir selector/deselection olmadan; full JUnit multiset collection ile exact aynı olmalıdır. `515` yalnız current informational baseline'dır; sonucu JUnit ve collection bytes'tan hesapla, sabitleme.

Targeted/full argv'nin Python executable, cwd, env allowlist, pytest config/plugin SHA'ları aynı olacaktır. `PYTEST_ADDOPTS` temizdir. Probe root readiness output namespace'i değildir. JUnit/stdout/stderr/registration-status ve varsa observation/raw bytes yalnız V10 discovery package `probe/targeted` ve `probe/full` altında; collection inventory/stdout/stderr `probe/collection` altında tutulur.

Bu probe'lar semantic PASS değildir. Underlying local synthetic test processleri exit 0 ve JUnit node'ları PASS olmalıdır. Her behavior node için discovery status exact `CAPTURED` veya `BLOCKED` yazılır. BLOCKED slot parent observation/raw üretmez ve `proposal_complete=false` yapar; fakat structurally valid review package üretimini engellemez. Test davranışını PASS yapmak için expected/assertion/SUT input değişikliği yasaktır. Mevcut test semantiği registration refactor nedeniyle bozulursa düzeltme yalnız evidence scaffolding içinde yapılır; production davranışı değiştirme.

## 8. Discovery package ve verifier

Paket root'u `outputs/super1_semantic_discovery_20260903` olacaktır. İlk boş proposal kimliğini V10 algoritmasıyla seç; tek `attempt_id` temp create-new işleminden önce üret. Temp ad exact `.proposal-NNN.tmp-<attempt_id>`, review final adı `proposal-NNN.review-required-<attempt_id>`, structural failure adı `proposal-NNN.failed-<attempt_id>`; canonical veya receipt adı yoktur.

V10'daki bütün required top-level member ve subtree'leri üret. Özellikle:

- `capture-registration-ledger.json`: captured her `(suite,nodeid,capture_type,resource_role)` için concrete SUT/factory binding ve before/after raw blob/envelope refs; BLOCKED slot için raw refs boş ve blocker dolu;
- `capture-role-set-proposal.json`: exact 527 node/capture slotu, targeted/full proposed role setleri ve `architect_approved=false`;
- `raw-blob-inventory.json`: source-native blob path/bytes/SHA/media type kayıtları;
- `raw-schema-bundle-proposal.json`: Draft 2020-12 `$defs` ve exact root bindings;
- `semantic-derivation-proposal.json`: 1.179 exact rule satırı;
- `unresolved-rules.json`: BLOCKED rule/schema/resource listeleri, boşsa exact empty arrays;
- `semantic-derivation-proposal-validation.json`: V10 validation maddelerinin her biri için recomputed actual, expected, status ve error;
- `discovery-process-metadata.json`: exact argv, cwd role, sanitized env, start/end, exit, stdout/stderr relative path+bytes+SHA ve source SHA;
- `discovery-readiness.json`: V10 `discovery_readiness_exact` alanları;
- `discovery-output-manifest.json`: kendisi hariç bütün üyelerin 1:1 normalized relative path, bytes ve SHA-256 kaydı; create-new yazılan son dosya.

`inputs/architect` exact V08, V09, V10 discovery contract, Talimat 09 ve Talimat 10 kopyalarından oluşur; extra/missing üye yasaktır. Source before/after farklılığı yalnız V10 allowlist dosyalarında ve `source-allowed-diff-ledger.json` içinde before/after bytes+SHA, change kind ve discovery-only reason ile kabul edilir. Allowlist dışındaki her source exact eşit kalır.

Paket içindeki path'ler repo/package-relative olacaktır. Secret config yalnız path/bytes/SHA ile kaydedilir; içerik kopyalanmaz. Mutlak workspace, temp, kullanıcı adı, Python install yolu veya başka makineye özel path sızıntısı yasaktır. Process argv executable'ı `<system-executable>`, repo/package root'u `<repo-root>`/`<proposal-root>` olarak sanitize edilir; raw stdout/stderr ayrı dosya olarak tutulur ve SHA gerçek bytes üzerinden hesaplanır.

`verify_super1_v10_discovery.py` producer/proposal generator import etmez. V08/V09/V10 bytes'ını ayrı okur ve şunları bağımsız doğrular:

- rule key seti 1.179 ve node seti 164 exact;
- V09 algorithm/op/left/right-source bağları exact;
- exact 527 capture-role-set satırı; targeted/full proposed role setleri ve ledger instance toplamları tutarlı;
- 17 capture type ve V09'daki ilk 41 open nested field dahil bütün recursively reached object/array şemaları Draft 2020-12 bundle'da çözülmüş ya da BLOCKED;
- `PROPOSED_SINGLE_CANDIDATE` rule'larda logical dependency/formula/probe/witness dolu; BLOCKED rule'larda null/empty alanlar ile nonempty ambiguity ve birebir unresolved kaydı;
- selector terminalinde `/payload`, placeholder, first-match, expected search veya forbidden formula op yok;
- targeted/full resolved pointer/SQL/blob access gözlemleri actual parent/envelope/blob refs+hashlerle doğrulanmış;
- parent ref→envelope bytes/SHA→blob bytes/SHA, exact envelope/ref schema/type, identity, phase, resource role/instance, canonical serialization ve multiplicity bağları exact;
- source/history before-after bütünlüğü;
- required member/subtree ve manifest 1:1;
- bütün readiness/proof/apply alanları false ve status exact `REVIEW_REQUIRED`.

Discovery command yalnız package yapısal olarak geçerliyse exit 0 verebilir. BLOCKED resource/role/schema/selector/formula/citation/type/witness review blocker'dır; package `proposal_complete=false` ile exit 0 olabilir. Immutable/history mismatch, allowlist dışı source farkı, missing/duplicate/extra V09 rule veya capture slotu, BLOCKED slot için fabricated raw, corrupt member/hash/identity/path/manifest ya da truthful-status ihlali fataldir; nonzero ve exact failed-name retention üretir. Exit 0 hiçbir şekilde semantic PASS, implementation approval veya readiness değildir.

## 9. Teslim ve zorunlu duruş

Son rapor yalnız şu factual alanları verir:

- package absolute linki ve manifest SHA-256;
- proposal id/attempt id;
- targeted/full/collection gerçek sayıları;
- targeted/full node status, parent observation, 527 capture-slot, proposed role, captured resource-instance, raw envelope ve blob sayıları;
- exact rule total `1179`, `PROPOSED_SINGLE_CANDIDATE` ve her `BLOCKED_*` sayısı;
- schema root binding/$defs total, resolved ve blocked sayısı;
- placeholder, `/payload`, forbidden formula, missing source citation ve absolute/temp path sayılarının exact sıfır olup olmadığı;
- immutable history before/after tree equality;
- `overall=NO_GO`, `status=REVIEW_REQUIRED`, `semantic_acceptance=false`, `semantic_engine_implementation_allowed=false`, `discovery_scaffolding_changes_allowed=true`, `implementation_allowed=false`, `safe_to_apply=false`, `apply_allowed=false`, `proven=false`, `parity=false`;
- source/target/broker/task/deployment/release/signature işlemlerinin `NOT_RUN`/`NOT_BUILT`/`NOT_SIGNED` durumu.

Paket hazır olunca dur ve mimara gönder. Yeni mimar talimatı ile immutable per-rule mapping SHA-256 yayımlanmadan semantic reporter/verifier implementasyonu, counterfactual execution, 53 negative acceptance, readiness run seçimi, canonical publication, receipt, SSH, MT5/broker, transfer, kurulum, scheduler, release, imza veya deployment aşamasına geçme.
