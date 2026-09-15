# SUPER1 final delivery — 2026-09-16

Bu artifact iki ayrı branch’in kod teslimini, test kanıtını ve açık kabul kapılarını birlikte kaydeder. `NOT PROMOTABLE / NO PUSH / NO LIVE ORDER` korunmuştur. Provider, owner, parola, login, private key, history rewrite ve push işlemi yapılmamıştır.

## Doğrulanmış kod ref’leri

| Hat | Başlangıç HEAD | Doğrulanan implementation HEAD / tree | Durum |
|---|---|---|---|
| promotion `codex/super1-promotion-blockers-v4` | `4019d5b4d454ec23d1156b6172eb1a55c9cb0862` | `9a3d552b36f6615632503fd2e9a0bc29b65c8ca9` / `6d8b4b38887fb8e2eb27de0c99cd9d4ddb8bcd02` | kod doğrulandı; dış kabul eksik |
| risk `codex/super1-local-runtime-hardening` | `4c3ed33bdc6a8db7113f7a9ae0233babfbe46ba6` | `4a9d33a4530aeb5f3e0566672f60a7e4214f7c21` / `b70a99eea46baa29b88fe614b4b2854da0791054` | kod doğrulandı; madde 11 platform bloklu |

Implementation ref’leri rapor commit’inden önceki, test edilen kod ref’leridir; rapor eklendikten sonraki gerçek tip/HEAD ayrıca teslim mesajında verilir. Böylece rapor self-referential HEAD iddiasında bulunmaz.

## Madde–kod–test matrisi

Satır numaraları test edilen implementation ref’lerindendir. `PASS_CODE`, yalnız kod ve mevcut regresyon kapısının geçtiğini; dış provider/owner kabulünü ifade etmez.

| Madde | Kod: dosya / fonksiyon / satır | Pozitif ve negatif kanıt: test fonksiyonu / satır | Sonuç |
|---:|---|---|---|
| 1 | `backtest/dukascopy_acquisition.py` — `reserve_artifact`, `_validate_provider_response`, `AcquisitionCoordinator.run` — 693, 869, 916; `tools/dukascopy-downloader/acquire_v5.mjs` — `fetchWithLimits` | `test_provider_status_and_body_rejection`, `test_provider_timeout_and_crash_no_commit`, `test_crash_resume_and_concurrent_lease` — `tests/test_super1_provider_coordinator_e2e.py:111,127,135,151`; 200/429/500/404, boş/eksik body ve zero-invalid-COMMITTED negatifleri | `PASS_CODE` |
| 2 | `backtest/dukascopy_acquisition.py` — coordinator ledger/lease akışı — 693–916 | `test_successful_200_is_not_requested_again`, `test_persistent_rate_spacing_after_success` — `tests/test_super1_provider_coordinator_e2e.py:111,144` | `PASS_CODE` |
| 3 | `backtest/reacquisition_contract.py` — `target_key`, `validate_committed_bundle`, `validate_final_manifest`, `apply_verified_reacquisitions` — 173, 232, 352, 594 | `test_nested_target_keys_residual_zero_apply`, `test_final_manifest_rejects_missing_detached_attestation` — `tests/test_super1_instruction_12_18.py:49,74,159`; replay/tamper negatifleri | `PASS_CODE` |
| 4 | `backtest/reacquisition_contract.py` — response/bundle hash ve provenance doğrulamaları — 232–352 | `test_fixture_cli_rejects_corrupt_cas_and_provider_record`, `test_provider_fixture_is_not_network_evidence` — `tests/test_super1_final_remediation_cli.py:43,57`; raw/decoded/bundle byte hash negatifleri | `PASS_CODE` |
| 5 | `backtest/reacquisition_contract.py` — `validate_final_manifest`; `scripts/audit_dukascopy_reacquisition_v5.py` — `finalize_manifest` — 352, 250 | `test_owner_signed_finalization_contract`, `test_unsigned_finalize_cannot_complete` — `tests/test_super1_final_remediation_cli.py:78,88`; prepare → exact staging bytes sign → validate → atomic publish | `PASS_CODE` |
| 6 | `scripts/owner_trust.py` — `validate_policy` — 73; `scripts/owner_replay_ledger.py` — `validate`, `validate_receipt` — 106,139 | `test_owner_rotation_binds_run_nonce_and_opaque_bindings`, detached signature/replay/nonce negatifleri — `tests/test_super1_final_remediation_cli.py:88,106`, `tests/test_super1_instruction_12_18.py:74,87` | `PASS_CODE` |
| 7 | `backtest/candidate_validation.py` — `validate_v4_promotion_evidence`, `validate_super1_v4_candidate` — 227,429 | `test_v4_promotion_evidence_direct_positive_and_negative` — `tests/test_super1_instruction_12_18.py:180`; closed schema, expected status, external root, real-file SHA, manifest/source binding; random 64-hex kabul edilmez | `PASS_CODE` |
| 8 | `scripts/run_xm_mt5_forward.py` — `_record_broker_state`, `_broker_execution_chain`, `_reconcile_persistent_intents` — 1399,1860,2183 | `test_identity_drift_is_unknown_no_send`, `test_terminal_snapshot_account_switch_is_recorded_as_unknown_no_send`, `test_broker_execution_chain_is_read_only`, `test_sqlite_intent_race` — `tests/test_xm_mt5_forward.py:1221,1243,3006,3178` | `PASS_CODE` |
| 9 | `scripts/scan_public_broker_identity.py` — `tracked_files`, `python_concrete_paths`, `scan`, `_history_scan`, `_quarantine_scan` — 20,68,102,158,229; `scripts/discover_xm_mt5_server.py:13` | `test_scanner_finds_python_literal`, `test_scanner_finds_reachable_git_python_blob`, `test_tracked_public_tree_has_no_concrete_broker_identity` — `tests/test_public_identity_scan.py:12,18,23,35`; private denylist yokken sonuç açıkça `UNASSESSED_MISSING_DENYLIST` | `PASS_CODE` |
| 10 | `scripts/run_super1_owner_acceptance.py` — `_verify_signed_document`, `_verify_rotation`, `_verify_binding_signature`, `run` — 64,86,99,106; `scripts/mt5_read_only_acceptance.py` — `run_adapter`, `run_pinned_read_only_acceptance` — 93,97 | `test_owner_acceptance_cli_checks_run_nonce_and_old_new_bindings`, `test_read_only_acceptance_does_not_import_external_adapter` — `tests/test_super1_final_remediation_cli.py:106`, read-only probe `scripts/mt5_read_only_probe.py:13`; legacy düz SHA yolu kapı dışında | `PASS_CODE` |
| 11 | Risk: `scripts/windows_appcontainer_launcher.py` — `launch`, `CREATE_SUSPENDED`, Job assignment/resume — 36,395,473–487; `backtest/sandbox.py` — `windows_appcontainer_available`, `run_sandbox` — 25,278 | `test_real_appcontainer_isolation`, `test_network_and_child_process_denial`, `test_timeout_kills_child_and_reaps`, `test_acl_is_restored` — `tests/test_backtest_sandbox.py:64,71,89,93,154`; capability yoksa erken “passed” değil skip/block; worker-level test OS kanıtı sayılmaz | `BLOCKED_PLATFORM` |
| 12 | `backtest/dukascopy_acquisition.py:916` — shared `AcquisitionCoordinator` provider path | 200 success dedup ve 429/5xx/404/timeout/crash E2E: `tests/test_super1_provider_coordinator_e2e.py:111,127,135`; sentetik/fixture proof dışında gerçek provider kabulü yok | `FAIL_CODE` |
| 13 | `backtest/dukascopy_acquisition.py:693`; `tools/dukascopy-downloader/acquire_v5.mjs` — `fetchWithLimits` | persistent host-state spacing subprocess testi `tests/test_super1_provider_coordinator_e2e.py:144`; request-start/terminal ledger negatifleri geçiyor, ancak dış rate-limit kabulü yok | `FAIL_CODE` |
| 14 | `tools/dukascopy-downloader/acquire_v5.mjs` — fetch/body abort; `backtest/dukascopy_acquisition.py:869` | slow-header `tests/test_super1_instruction_12_18.py:334` yeterli değildir; slow-body gerçek stream abort ve no-CAS/no-COMMITTED `:345`; Node contract `:273` | `FAIL_CODE` |
| 15 | `backtest/reacquisition_contract.py:173,594` — nested target key ve residual-zero apply | `test_nested_target_keys_residual_zero_apply` — `tests/test_super1_instruction_12_18.py:49,159`; 113 target fixture bridge ve residual zero kod kanıtı var, owner manifest yok | `FAIL_CODE` |
| 16 | `backtest/reacquisition_contract.py:232,298,352`; `scripts/owner_trust.py:73`; `scripts/owner_replay_ledger.py:139` | `test_detached_attestation_rejects_tamper`, `test_owner_rotation_binds_run_nonce_and_opaque_bindings` — `tests/test_super1_instruction_12_18.py:74,87`, `tests/test_super1_final_remediation_cli.py:88,106`; source/tree, run/nonce, pinned external owner key ve detached signature negative’leri | `FAIL_CODE` |
| 17 | `backtest/candidate_validation.py:227,429` | Doğrudan pozitif/negatif V4 evidence testi `tests/test_super1_instruction_12_18.py:180`; root/real-file/status/content SHA/manifest/source binding kapalı şema ile doğrulanıyor; sentetik evidence gerçek owner kabulü değildir | `FAIL_CODE` |
| 18 | `backtest/dukascopy_acquisition.py:693,869,916`; `scripts/run_xm_mt5_forward.py:1399,2183` | acquisition crash/retry/concurrent lease: `tests/test_super1_provider_coordinator_e2e.py:127,135,151`; account-switch/fresh-read transactional snapshot: `tests/test_xm_mt5_forward.py:1221,1243,3178`; fixture network izolasyonu ve dış provider/DEMO evidence yok | `FAIL_CODE` |
| 19 | `backtest/reacquisition_contract.py:594`; `scripts/audit_dukascopy_reacquisition_v5.py:250` | `test_nested_target_keys_residual_zero_apply` — `tests/test_super1_instruction_12_18.py:159`; 113/113 gerçek provider hedefi, residual 0 ve owner-imzalı COMMITTED V5 manifest mevcut değil | `BLOCKED_EXTERNAL_ACCEPTANCE` |
| 20 | `scripts/scan_public_broker_identity.py:102,158,229` | `tests/test_public_identity_scan.py:12,18,23,35`; private denylist, remote refs, bütün reachable objects ve owner disposable sanitized mirror kanıtı yok; scanner bu nedenle clean-history iddiası üretmiyor | `BLOCKED_EXTERNAL_ACCEPTANCE` |
| 21 | `scripts/run_super1_owner_acceptance.py:64,86,99,106`; `scripts/owner_replay_ledger.py:106,139` | signed exact push lease (remote/ref/eski-yeni OID/expiry/nonce), owner rotation ve replay receipt yok; push yapılmadı | `BLOCKED_EXTERNAL_ACCEPTANCE` |
| 22 | `scripts/mt5_read_only_acceptance.py:97`; `scripts/run_xm_mt5_forward.py:1860` | `tests/test_super1_final_remediation_cli.py:106`, `tests/test_xm_mt5_forward.py:3006`; eski binding revoke + yeni DEMO binding + gerçek credentialsiz read-only probe/order_send=0 için owner-imzalı dış kanıt yok; live order yok | `BLOCKED_EXTERNAL_ACCEPTANCE` |

## Test ve artifact kanıtı

- Promotion full: `789 passed, 3 skipped`; JUnit `outputs/reports/pytest_promotion_full_20260916_final.xml`, SHA-256 `0EBB41752811AC8394876C7F96D2C54FF18C9FAAFB54BFA26C5CCC5FAAE5E28`.
- Risk full: `722 passed, 10 skipped`; JUnit `outputs/reports/pytest_risk_full_20260916_final.xml`, SHA-256 `C9F2D73250D271F3090D8008D926FD1EBD20BE4782E6CAF7FD58A1534D8A15F5`.
- Promotion hedefli provider/12–18/identity/MT5: `139 passed`; CLI 19–22 negatifleri: `8 passed`.
- Risk hedefli env/sandbox/E2E: `24 passed, 10 skipped`.
- Her iki branch: `python -m compileall -q backtest scripts tests` ve `git diff --check` geçti.
- Kod diff patch’leri: promotion `outputs/reports/super1_promotion_4019d5b4_to_final.patch`, SHA-256 `24D27D15E21A7F1C1088038C13B2AC75F141C490EE78187995320D5BF34D8B12`; risk `outputs/reports/super1_risk_4c3ed33b_to_final.patch`, SHA-256 `6381EA8CE208D69C0B73A1444310A48D0D7D9A43F35768F5ECC3D13F4A4ABE4C`.

## Korunan girdiler ve karar

Promotion’daki dört V4 sealed blob değiştirilmedi; Git blob OID’leri sırasıyla `e02bad2a7af3c3ee1db8fb1199e3217a50d117a`, `bed6c345ec9ae5b291aeec2d46b14592a2bea33e`, `f898ab60ad147df4f2b35ab0662a71b84a4866c9`, `2dc60b25c7f3489c4baa12a1ecd06ae064f9da1c` olarak korundu. Dört protected untracked girdisi stage edilmedi veya değiştirilmedi; manifest SHA’ları `7cc0518a0c5957102cd867e358316761ca8330d80ecc0662fe84ea67f94df1ec`, `167259c5cc4cebde76d2c5d7b70ed65aa8f21c2ace2e8a10ace48b1e7a8a4839`, calendar `af37ef2ff5e3b489df7d715aa826fd746af9138d762300661b48a783e3eecca3`; legacy backup dosyaları da korunmuştur.

Sonuç: kod düzeltmeleri ilgili branch’lerde ayrı commit’lerle teslim edildi; ancak madde 11 `BLOCKED_PLATFORM`, 12–18 `FAIL_CODE`, 19–22 `BLOCKED_EXTERNAL_ACCEPTANCE` kaldığı için promotion yapılamaz, push yapılamaz ve live order kapısı kapalıdır.
