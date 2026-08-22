# Manual Review - 2025 Random Sample

Source test:

```text
3m, all directions, max 1 trade per day
outputs/reports/3m_max1/trades.csv
```

Purpose:

```text
Check whether the engine is taking the same trades the user would take manually.
```

Reviewed on 2026-07-02.

## Reviewed Dates

| Date | User manual review |
|---|---|
| 2025-04-25 | Would take long. Result: SL. |
| 2025-04-07 | Long order would be placed, but price reached target before entry fill. |
| 2025-05-08 | Long order would be placed, but price moved without filling entry. |
| 2025-05-26 | Would take long. Result: SL. |
| 2025-06-09 | Would take long. Result: TP. |
| 2025-06-18 | Would not take trade; no valid PD array was left for entry. |

## Early Findings

- Some engine-selected examples do not match the user's manual direction.
- Some manual-valid ideas are not filled before target, so pending-limit cancellation logic matters.
- `2025-06-18` highlights that the engine can accept setups where the user sees no tradable PD array.

