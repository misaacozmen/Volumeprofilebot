# Implementation Readiness V6 — 2026-09-14

## Final status

`REJECTED_SOFTWARE_DATA_AND_SECURITY_HISTORY_GATES`

Promotion and release readiness remain rejected. The software fail-closed gates pass, but data coverage and security/history gates remain open. No MT5, broker, order, signing, private-key, archive, push, fetch, pull, tag, or history-rewrite operation was performed.

## Repository identity and commits

- Branch: `codex/super1-promotion-blockers-v4`
- Required initial HEAD: `a21ee2c2619a154ef1701ff55cd4e45f98ea1ca6`
- Code commit: `1e69c74c98380e4495b286130c3a5f075185998a` — `fix: complete fail-closed acquisition and promotion gates`
- Documentation commit: this report's containing commit, created separately with message `docs: record V6 readiness evidence`; its full SHA is recorded by the post-commit verification in the final task result.
- `git diff --check a21ee2c2619a154ef1701ff55cd4e45f98ea1ca6..HEAD`: passed before the documentation commit.

Protected user-provided untracked entries were not staged or changed.

## Software gates

- Full test suite: **726 passed**, 0 failed, 0 errors, 0 skipped; JUnit evidence: `outputs/reports/pytest_v6.xml`.
- Python compile check on a temporary copy: passed.
- PowerShell AST parse for `deploy/build_signed_windows_release.ps1`: passed.
- `node --check tools/dukascopy-downloader/acquire_v5.mjs`: passed.
- Offline downloader plan: passed with locked package/dependency and generated URL hashes.
- `require_promotable=False`: passed.
- `require_promotable=True`: correctly blocked by `V4 promotion blocker is active`.
- Windows release `-ValidateOnly`: correctly blocked before staging/signing/archive/key access.

## Data and acquisition gate

Authoritative frozen inventory:

- SHA-256: `a63406f235ded8d3daa123c0311adb678e53db3d996141b194493309f2cce075`
- Targets: **113** unique; NQ/3m **69**, SPX/5m **44**.
- Required target `2025-04-16,nq,3m`: present.
- Exact offline classes: `VERIFIED_LEGACY=14`, `INVALID_LEGACY=3`, `INCOMPLETE_STAGING=1`, `MISSING=95`.
- Residual targets: **99**.
- New provider downloads: **0**; no data commit was created.
- Final V5 `COMMITTED` manifest: not produced.

The provider circuit is fail-closed:

- Run status: `DEFERRED_RATE_LIMIT`.
- `provider_call_count`: **0**.
- Host circuit: `OPEN`; consecutive 429 count: **5**.
- Exact next retry: `2026-09-15T15:33:39.3919524Z`.
- Canonical full-history preflight and reliability audit stopped before engine execution because residual data was 99; result/determinism hashes are therefore `N/A — blocked before engine execution`.

## Security/history gate

- Public identity scan: `UNASSESSED_MISSING_DENYLIST`.
- History sanitization rehearsal: `UNASSESSED_MISSING_DENYLIST`.
- Working-tree/reachable-history/sanitized-mirror counts: `null / null / null` because the required private denylist was unavailable; no denylist values were guessed or written.
- `source_unchanged=true`, `origin_unchanged=true`.
- Scan source HEAD: `1e69c74c98380e4495b286130c3a5f075185998a`.
- Security history, account rotation, and remediation gates remain unresolved; promotion is blocked.

## Protected-input verification

Byte count, mtime, and SHA-256 matched the initial inventory after tests, audit runs, and both commits:

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
| Software correctness and fail-closed controls | PASS | 726 tests plus compile/AST/Node/negative checks |
| V5 data completeness and promotion | FAIL | 99 residual targets; no final manifest |
| Provider cooldown safety | PASS | 0 calls; exact persisted cooldown and open circuit |
| Public identity/history sanitation | FAIL | private denylist missing; status remains unassessed |
| Account rotation/remediation | FAIL | required security-history evidence unavailable |
| Release signing/archive/broker execution | NOT RUN | promotion gate rejected before those actions |

Final disposition: **not promotable, not accepted, and not release-ready**.
