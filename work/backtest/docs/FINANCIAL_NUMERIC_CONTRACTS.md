# Financial numeric contracts

| Public function | Input contract | Output contract |
|---|---|---|
| `finite_float` | Real, excluding bool, NaN and infinity | finite `float` |
| `positive_float` | finite value greater than zero | positive `float` |
| `nonnegative_float` | finite value at least zero | non-negative `float` |
| `strict_ratio` | finite value in `(0, 1]` | ratio `float` |
| `validate_trade_geometry` | positive long/short entry, stop and target | validated tuple |
| `quantize_price` | positive price and tick size | nearest tick, half-even |
| `quantize_volume_down` | positive volume/step/min/max | step-aligned volume rounded down; never increases risk |
| `safe_divide` | finite numerator/denominator | `(null, reason)` for zero denominator; otherwise finite quotient |
| `finite_vector` | iterable of finite real values | validated `list[float]` |

Invalid type, bool, nonfinite value, invalid geometry, overflow, zero risk distance, or out-of-range broker volume raises `FinancialMathError`. Undefined performance is represented by JSON `null` plus an explicit reason; it is never represented as zero, NaN, or infinity.
