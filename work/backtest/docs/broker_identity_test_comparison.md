# Broker identity test comparison

The baseline and fixed runs used the same Python interpreter, the same
synthetic fixture files, and the same test setup. The baseline worktree was
`origin/main` at `023e39b288863ac93df9e7cfa1b7f218029f6458`; only the test
harness and committed synthetic fixtures were overlaid so that the code under
test remained the baseline revision.

Acceptance command:

```text
py -3 -m pytest -q \
  work/backtest/tests/test_super1_continuation.py \
  work/backtest/tests/test_super1_xm_forward.py \
  work/backtest/tests/test_xm_mt5_forward.py \
  work/backtest/tests/test_super1_calendar.py \
  work/backtest/tests/test_deployment_security.py \
  work/backtest/tests/test_v16_deployment_contract.py
```

| Code under test | Result | Evidence classification |
|---|---:|---|
| `origin/main` `023e39b...` | 302 passed, 1 failed | `PRE_EXISTING_CONFIRMED` for the release-integrity mismatch below |
| Fixed candidate | 302 passed, 1 failed | Same release-integrity mismatch; no new functional failure |
| Fixed candidate, four lifecycle/calendar modules, listed order | 250 passed | `PASS` |
| Fixed candidate, same four modules, reverse order | 250 passed | `PASS` |
| Fixed candidate lifecycle node alone | 1 passed | `PASS` |
| Fixed candidate lifecycle node with related modules, both orders | 51 passed each | `PASS` |
| New identity/regression suite | 30 passed | `PASS` |

The only remaining acceptance failure is recorded with its node ID and both
versions' observed result:

| Node ID | Expected | `origin/main` actual | Fixed candidate actual |
|---|---|---|---|
| `work/backtest/tests/test_deployment_security.py::test_super1_upgrade_pins_and_read_locks_the_release_trust_helper` | `upgrade_super1_signed_app_windows.ps1` contains the current `release_integrity.ps1` SHA-256 | Current file: `011008a070c7723f285fc44370f821cfa7e58e8535d2ccdbd6e4ff360566f3ed`; embedded pin: `4051f4e68b4aa575df2952a7205ac4fdbdecf6e3da4ca9e170d760e8d9d3dcfe` | Exactly the same expected/actual mismatch |

This is `PRE_EXISTING_CONFIRMED`, not a broker-identity regression. Release,
calendar, and provenance hashes were not changed. Synthetic calendar fixtures
are test isolation inputs only and are not release-acceptance evidence.

The pre-PR identity modules and scanner did not exist in `origin/main`, so the
identity-only baseline collection result is `UNPROVEN_ORIGIN`; it is not used
as evidence that an identity test body passed or failed before the PR.
