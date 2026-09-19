# Broker identity test comparison

The comparison used the same Python interpreter and the same four pre-existing
test files in clean Git worktrees:

```text
py -3 -m pytest -q \
  work/backtest/tests/test_super1_continuation.py \
  work/backtest/tests/test_super1_xm_forward.py \
  work/backtest/tests/test_xm_mt5_forward.py \
  work/backtest/tests/test_super1_calendar.py
```

| Scope | Result | Classification | Evidence |
|---|---:|---|---|
| `origin/main` `023e39b...` | collection stopped before tests | `BASELINE_FAILURE` | The two special runtime config files are absent from the tracked baseline: `xm_mt5_demo_config.json` and `super1_xm_mt5_demo_config.json`. |
| Fixed HEAD | 241 passed, 9 failed | fixture collection is fixed; failures remain isolated | Eight failures stop at the unchanged RTH source-provenance hash drift. The `PRE_SEND_DEFERRED` lifecycle case passes alone (`2 passed`) and fails only after the mixed module suite, so it is an order-sensitive baseline test issue, not a broker-identity code regression. |
| New identity/regression suite | 30 passed | `PASS` | Includes clean PowerShell worker validation, structural-only mode, comparison literals, remote branch/tag trees, and baseline reuse regressions. |

The production release manifest/contract/calendar hashes were not rewritten to
make the suite green. Their unchanged blob IDs at baseline and fixed HEAD prove
that the RTH hash drift predates this change and remains a separate release
blocker.
