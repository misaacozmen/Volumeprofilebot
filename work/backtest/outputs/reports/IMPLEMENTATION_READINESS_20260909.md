# Implementation Readiness — 2026-09-09

Status: **NOT READY FOR PROMOTION, SIGNING, OR PUSH**

## Commits

- Starting checkpoint: `db640bf7f5608a9c755661b68f0006c2d6bc3bb8`
- Stage-0 checkpoint: `1b233f524c1f03b7eb9cf3afc076507626ad4bf8`
- Final implementation commit: the commit containing this report; its non-self-referential SHA is recorded in the task handoff.

## Implemented surface

- Durable single physical MT5 write boundary and test-only canonical SQLite adapter.
- Frozen live-risk policy, broker-derived daily counts/realized-R facts, second-snapshot final risk binding, approval consumption, and exact wire binding.
- Read-only retry/backoff/circuit breaker; writes remain exactly-once and non-retried.
- Exact semantic instrument registry validation and BUY/SELL economic probe.
- Engine-level evaluation-date eligibility, calendar-aware coverage, and warmup exclusion.
- Transaction-local approval lifecycle outbox for STAGED/APPROVED/REJECTED/EXPIRED/CONSUMED.
- Deterministic optimize/walk-forward CLI and OOS-only aggregation.
- Risk x-ray, numeric contracts, canonical strategy-health deal/cursor state, and monotonic decay lifecycle.
- Central environment/settings boundary and minimum-secret subprocess environment.
- Isolated CLI run/optimize/walk-forward worker with canonical JSON IPC, Windows Job Object/POSIX limits, network/child/live-import denial, timeout, memory, output-size, traversal, and symlink checks.
- New unsigned v3 runtime chain and new v2 threshold artifact; historical artifacts were not overwritten.

Changed files are the staged source, tests, docs, v3 unsigned artifacts, `research_candidates/calibration/first30_thresholds_pre2025_v2.json`, `tests/fixtures/sandbox_study.json`, and this report. Protected raw manifests, `data/raw_legacy_backup_20260908/`, and `live_forward/calendars/us_equity_rth_2022_2026.json` are excluded.

## Verification

| Command | Exit | Result |
|---|---:|---|
| `python -m pytest -q --junitxml=outputs/reports/full_pytest_pre_fix.xml` | 1 | 667 collected; 606 passed, 61 failed |
| Stage-0 targeted/full pytest | 0 | 667 passed |
| `python -B -m compileall -q backtest scripts` | 0 | PASS |
| `python -m pytest -q --junitxml=outputs/reports/full_pytest_post_fix.xml` | 0 | 695 passed in 343.46s |
| `git diff --check` | 0 | PASS |
| PowerShell AST parse of `deploy/*.ps1` | 0 | PASS |
| Architecture/settings/instrument/runtime targeted tests | 0 | 15 passed |
| CLI sandbox `run` smoke | 0 | PASS |
| CLI sandbox `optimize` smoke | 0 | `44b46520a3e85190f300e45e2737c4ad909bb30b30f20bc61348c45aa803a99d` |
| CLI sandbox `walk-forward` smoke, run 1 | 0 | `9727cf999e4c401441398254683fb4084c87a02c6694f3c88d424aa0a254b05b` |
| CLI sandbox `walk-forward` smoke, run 2 | 0 | same stable hash |

No skip/xfail was added. AST/source scan finds one physical `order_send`, at `backtest/live/execution.py::Mt5WritePort._physical_send`. Direct process-environment access is confined to `backtest/live/settings.py` and the sandbox worker probe.

## Remaining blockers

- V3 runtime/calendar/candidate status is deliberately `UNSIGNED_VALIDATION_ONLY`; no signing key was used.
- The v3 calendar has structural validation but lacks complete locked raw official-source provenance suitable for promotion.
- Required production-grade multi-year market-data coverage/provenance remains incomplete.
- The public Git history still contains a real broker account identifier; history rewriting was out of scope and no push occurred.
- Live signal generation has not yet been migrated from the legacy live runner into the new subprocess protocol; only CLI run/optimize/walk-forward are sandbox-routed. This is a software promotion blocker.
- Strategy-health persistence records append-only terminal deal identities/cursor, but complete broker late-arrival/partial-close ingestion remains a release blocker pending read-only broker evidence.

No broker connection, broker write, signing operation, push, or history rewrite was performed.
