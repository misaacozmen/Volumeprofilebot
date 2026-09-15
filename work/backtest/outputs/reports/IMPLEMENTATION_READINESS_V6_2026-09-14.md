# Implementation Readiness V6 — 2026-09-14

## Final status

`REJECTED_SOFTWARE_DATA_AND_SECURITY_HISTORY_GATES`

Promotion and release readiness remain rejected. A post-commit architecture audit on 2026-09-15 overturned the earlier software `PASS`: release-blocking control-flow, persistence, timeout, manifest-validation, evidence-binding, and terminal-reconciliation defects remain. The recorded test run is green but does not exercise these critical paths. No MT5, broker, order, signing, private-key, archive, push, fetch, pull, tag, or history-rewrite operation was performed.

## Repository identity and commits

- Branch: `codex/super1-promotion-blockers-v4`
- Required initial HEAD: `a21ee2c2619a154ef1701ff55cd4e45f98ea1ca6`
- Code commit: `1e69c74c98380e4495b286130c3a5f075185998a` — `fix: complete fail-closed acquisition and promotion gates`
- Audited report commit: `7b0a2564233b225c550cfed1c88ff88033984ad1` — `docs: record V6 readiness evidence`.
- The report correction is intentionally isolated in a later local documentation commit; its SHA is recorded in the task result.

Protected user-provided untracked entries were not staged or changed.

## Software gates — corrected assessment

- Recorded test run: **726 passed**, 0 failed, 0 errors, 0 skipped. The JUnit file `outputs/reports/pytest_v6.xml` is ignored and not committed or cryptographically bound to the tested HEAD, so it is not durable promotion evidence.
- Python compile check on a temporary copy: passed.
- PowerShell AST parse for `deploy/build_signed_windows_release.ps1`: passed.
- `node --check tools/dukascopy-downloader/acquire_v5.mjs`: passed.
- Offline downloader plan: passed with locked package/dependency and generated URL hashes.
- `require_promotable=False`: passed.
- `require_promotable=True`: correctly blocked by `V4 promotion blocker is active`.
- Windows release `-ValidateOnly`: correctly blocked before staging/signing/archive/key access.

Release-blocking audit findings:

1. `scripts/reacquire_invalid_sessions_v5.py:176-237`: successful request status handling is outside the `while True` loop and unreachable; the same URL can be downloaded repeatedly without termination.
2. `backtest/dukascopy_acquisition.py:423-430`: `record_success` replaces the host state and drops `last_provider_start_at_utc`, defeating persisted minimum-spacing enforcement after a success.
3. `tools/dukascopy-downloader/acquire_v5.mjs:96-124`: the abort timer is cleared after response headers, before the response body is consumed; the documented total-request timeout is not enforced on the body stream.
4. `backtest/reacquisition_contract.py:172-176,227-231`: finalization writes `timeframe` and `leg` under `target`, while manifest application reads them at the row root; a residual-zero run reaches a `KeyError` instead of completing.
5. Final-manifest validation format-checks claimed hashes without recomputing and binding all referenced artifacts, fresh audit output, and unique target identity. A forged or replayed attestation can therefore satisfy the current shape checks.
6. `backtest/candidate_validation.py:273-300`: promotion evidence fields are accepted as arbitrary 64-hex strings without proving the evidence files, their status, or their content hashes; the candidate is not bound to the complete blocker/evidence set.
7. Acquisition resume/fixture coverage and terminal-deal reconciliation remain insufficiently fail-closed: verified targets are needlessly reacquired, cached outputs are not content-hash validated, fixture mode does not guarantee network isolation, and terminal snapshots are not transactionally revalidated and account-scoped.

The acquisition suite contains only shallow primitive coverage for these new paths. No end-to-end tests demonstrate coordinator termination, Node body timeout, raw-to-decoded provenance, residual-zero finalization, crash/retry/resume behavior, concurrent state safety, manifest anti-forgery, or transactional terminal reconciliation. Software status is therefore `FAIL`, regardless of the aggregate test count.

## Data and acquisition gate

Authoritative frozen inventory:

- SHA-256: `a63406f235ded8d3daa123c0311adb678e53db3d996141b194493309f2cce075`
- Targets: **113** unique; NQ/3m **69**, SPX/5m **44**.
- Required target `2025-04-16,nq,3m`: present.
- Exact offline classes: `VERIFIED_LEGACY=14`, `INVALID_LEGACY=3`, `INCOMPLETE_STAGING=1`, `MISSING=95`.
- Residual targets: **99**.
- New provider downloads: **0**; no provider bundle or CAS data commit was created. Contrary to the earlier wording, commit `7b0a2564233b225c550cfed1c88ff88033984ad1` also modified 14 legacy-acceptance attestation JSON files, so it was not documentation-only.
- Final V5 `COMMITTED` manifest: not produced.

The provider circuit is fail-closed:

- Run status: `DEFERRED_RATE_LIMIT`.
- `provider_call_count`: **0**.
- Host circuit: `OPEN`; consecutive 429 count: **5**.
- Exact next retry: `2026-09-15T15:33:39.3919524Z`.
- State observed at `2026-09-15T06:51:11.0831544Z`; state SHA-256: `570287e46c94aafd7a303a3b168b705106795007cb01e346805ab2cef24320a7`.
- The state file is ignored and mutable, and no HTTP-event artifact was present. These values are a point-in-time observation, not committed promotion evidence. The spacing-persistence and body-timeout defects above prevent this gate from passing as an implementation guarantee.
- Canonical full-history preflight and reliability audit stopped before engine execution because residual data was 99; result/determinism hashes are therefore `N/A — blocked before engine execution`.

## Security/history gate

- Public identity scan: `UNASSESSED_MISSING_DENYLIST`.
- History sanitization rehearsal: `UNASSESSED_MISSING_DENYLIST`.
- Working-tree/reachable-history/sanitized-mirror counts: `null / null / null` because the required private denylist was unavailable; no denylist values were guessed or written.
- `source_unchanged=true`, `origin_unchanged=true`.
- Scan source HEAD: `1e69c74c98380e4495b286130c3a5f075185998a`.
- A separate read-only semantic history audit found identity-bearing `account_login` material in 9 blobs reachable from the branch; 8 were absent from the existing local `origin/main` tracking ref. Values are deliberately omitted from this report. A push would expose additional sensitive history to the public origin.
- Security history, account rotation, and remediation gates remain unresolved; promotion is blocked.

Public push disposition: **BLOCKED; no push performed**. Do not publish this branch until the private denylist scan, history sanitation, remote re-verification, and broker-account rotation evidence all pass.

## Protected-input verification

Byte count and SHA-256 matched the recorded inventory after the audit. The earlier report did not record baseline mtime values, so its mtime assertion is not independently reproducible. None of these paths was staged by the correction:

```text
data/raw/DUKASCOPY_USATECHIDXUSD, 3m_2025-03-01_2025-06-01_v2.csv.manifest.json  468      7cc0518a0c5957102cd867e358316761ca8330d80ecc0662fe84ea67f94df1ec
data/raw/DUKASCOPY_USATECHIDXUSD, 5m_2025-03-01_2025-06-01_v2.csv.manifest.json  468      167259c5cc4cebde76d2c5d7b70ed65aa8f21c2ace2e8a10ace48b1e7a8a4839
data/raw_legacy_backup_20260908/DUKASCOPY_USATECHIDXUSD, 3m_2025-03-01_2025-06-01.csv  2405159  e0fbaecf29dfc9208cfdd830c36d4264972a3ecc8bce78eb0f4ddafe6c2be130
data/raw_legacy_backup_20260908/DUKASCOPY_USATECHIDXUSD, 3m_2025-03-06_2025-03-29.csv  522826   d6785a2b6a80ad5d5de45ea6c33a9d38d343443fe0c4aa14667c5be3ef081d9d
data/raw_legacy_backup_20260908/DUKASCOPY_USATECHIDXUSD, 5m_2025-03-01_2025-06-01.csv  1443768  88f0b9f91913ed4b1ccc2a7f5b4cb3ece181dca2d7bd4bd1a915dcae004a7f5b
data/raw_legacy_backup_20260908/DUKASCOPY_USATECHIDXUSD, 5m_2025-03-06_2025-03-29.csv  314876   1f7104e6b20676406102b6c798af05c41a2c31620e011662624ba229b1a5d683
live_forward/calendars/us_equity_rth_2022_2026.json  2709       af37ef2ff5e3b489df7d715aa826fd746af9138d762300661b48a783e3eecca3
```

## Gate disposition

| Gate | Result | Evidence |
|---|---|---|
| Software correctness and fail-closed controls | FAIL | Critical coordinator, spacing, timeout, finalization, evidence-binding, and reconciliation defects remain |
| V5 data completeness and promotion | FAIL | 99 residual targets; no final manifest |
| Provider cooldown and execution safety | FAIL | Snapshot showed 0 calls/open circuit, but durable spacing and total-request timeout controls are defective |
| Promotion evidence integrity | FAIL | JUnit is uncommitted; manifest/candidate evidence is not fully recomputed and hash-bound |
| Public identity/history sanitation | FAIL | private denylist missing; status remains unassessed |
| Account rotation/remediation | FAIL | required security-history evidence unavailable |
| Release signing/archive/broker execution | NOT RUN | promotion gate rejected before those actions |

Final disposition: **not promotable, not accepted, and not release-ready**.
