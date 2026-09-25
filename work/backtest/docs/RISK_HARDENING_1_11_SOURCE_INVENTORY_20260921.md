# Risk-hardening 1–11 source inventory

## Ref and delta provenance

- Integration base: `origin/main` = `c1c769aec68b84c9c13f9add198ab130241c69ad`; tree = `2b06170074b94c34d79dbd0f018bf168bc651de1`.
- Source checkout HEAD: `4810a7323ae2ab2216a4e44c6366e744c75f853d`; tree = `9fc0259b66c51f617229de765c8df34d6e5c3c3d`.
- Committed risk source used for transfer: `4a9d33a4530aeb5f3e0566672f60a7e4214f7c21`; tree = `b70a99eea46baa29b88fe614b4b2854da0791054`.
- Delivery-only source commits after the selected risk source (`223a927`, `c0b0e81`, `4810a73`) were not transferred.
- Committed source delta `origin/main...4a9d33a` contains 158 paths; binary patch SHA-256 = `21d27eac8952e6bdcba3b7864d801441dd58b5fd918b5e7678c3f292905a125f`.

## Dirty source delta

The source worktree was not cleaned or rewritten. The five tracked risk files below were transferred as a separate delta; its binary patch SHA-256 is `81b70608d932b74affe2892335aacb36f5368ff03c3e335ea13bb12d077af04d`.

| Path | Source checkout SHA-256 |
|---|---|
| `work/backtest/backtest/live/halt.py` | `f944f83959b87d8f3e5c7be28692a2b7d60702657eb9d085cc1ff6f3863fca45` |
| `work/backtest/backtest/sandbox.py` | `6421ee2a28bd8d80331a4e0502ac89ce3fd5f92a8300b6dea10826ac0a404430` |
| `work/backtest/scripts/windows_appcontainer_launcher.py` | `5048774c49f9ce7de36f4d1a9d74f335e9ab17ea328fdb56c1b8a94d363abe11` |
| `work/backtest/tests/test_backtest_sandbox.py` | `98ce937d7d0e3bd1cbdb1dc2afbd33af79132c9b6f2c85f2977f72a64c2c2f39` |
| `work/backtest/tests/test_halt_sweep.py` | `6f5e42fce3cc29a0405a346818674d72a409441d8bbfb3aeea3864bf39a727e9` |

The four untracked test-support files transferred separately were `halt_claim_race_probe.py`, `prepare_symlink_fixture.py`, `tests/conftest.py`, and `tests/test_prepare_symlink_fixture.py`. Their source checkout SHA-256 values are respectively `09e030e09d66f3e9e16588f0e2df04df34c0d9f4d4797e810ec4e26e9b3ad4f6`, `603906368b87e6df45fb22b2724ecfd5d7f31fb05c10bfaf418c956089333685`, `5d06f66706c12c13e04fa404d1eb25b19455272317893250d27c74f0bf49e073`, and `0f26473d49e3c6294338266424ae0511b12b2b115cf5277e904678cea1fd29fc`.

The source's 30 untracked `work/backtest/artifacts/` files were not transferred. Their canonical path/hash inventory SHA-256 is `24284bddc73bbbd33eabc8ffbb4f7dc65a4d03e4225a6d3df4599c11554fd39c`.

## Transferred scope

- Risk/execution core: `backtest/candidate_validation.py`, evaluation-window, optimization, walk-forward, numeric, risk-xray, strategy/engine, and all `backtest/live/` modules.
- Runtime connections: the capital/Super1/XM forward runners, runtime guard, approval CLI, read-only acceptance, environment scanner, and sandbox worker/launcher.
- Schema and fixtures: `schemas/super1_environment_v1.schema.json`, deterministic runtime config, and risk contract documentation.
- Tests: the 1–11 contract/e2e suites, execution/approval/retry/instrument/health/math/evaluation/optimization/risk-xray/sandbox suites, and the race/symlink fixture support.
- DEPLOY-001’in kabul edilmiş release/upgrade sözleşme dosyaları (`build_signed_windows_release.ps1`, `release_integrity.ps1`, `upgrade_forward_shadow_windows.ps1`, `upgrade_super1_signed_app_windows.ps1`) `origin/main` bytes olarak korundu; broker identity/recovery ve rollover yardımcılarında yalnız risk entegrasyonunun gerektirdiği fail-closed kimlik bağları güncellendi.
