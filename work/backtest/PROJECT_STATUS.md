# Project Status

Last updated: 2026-07-08

## Project Goal

Build an automatic backtest and later forward-test workflow for a manual intraday NAS100 / SPX500 / Gold / Silver strategy using:

- Fixed Range Volume Profile context
- VAH / VAL
- liquidity sweeps
- CISD
- FVG / IFVG entries
- fixed 3R target

Current implementation supports Capital.com exports and Dukascopy-derived 3m/5m files.
Cross-symbol configs include `CAPITALCOM_SPX500`, `CAPITALCOM_XAUUSD`, `CAPITALCOM_XAGUSD`,
`DUKASCOPY_USATECHIDXUSD`, `DUKASCOPY_USA500IDXUSD`, `DUKASCOPY_XAUUSD`, `DUKASCOPY_XAGUSD`,
`DUKASCOPY_EURUSD`, and `DUKASCOPY_GBPUSD`.

## Current Data

Raw CSV folder:

```text
work/backtest/data/raw/
```

Dukascopy download automation:

```text
work/backtest/scripts/download_dukascopy.py
```

Known Dukascopy presets:

```text
nq     -> usatechidxusd -> DUKASCOPY_USATECHIDXUSD
spx    -> usa500idxusd  -> DUKASCOPY_USA500IDXUSD
gold   -> xauusd        -> DUKASCOPY_XAUUSD
silver -> xagusd        -> DUKASCOPY_XAGUSD
```

Notes:

```text
dukascopy-node supports m1 and m5, but not m3 directly.
The project script downloads m1 BID data with volume, converts timestamps to America/New_York,
then writes project-ready 3m and 5m CSV files with columns:
time,open,high,low,close,Volume
```

Current Dukascopy data downloaded on 2026-07-04:

```text
DUKASCOPY_USATECHIDXUSD 3m:
2022-01-02 18:00 NY -> 2026-07-02 23:57 NY
511,937 unique timestamps, no zero volume rows

DUKASCOPY_USATECHIDXUSD 5m:
2022-01-02 18:00 NY -> 2026-07-02 23:55 NY
307,245 unique timestamps, no zero volume rows

DUKASCOPY_USA500IDXUSD 3m:
2022-01-02 18:00 NY -> 2026-07-02 23:57 NY
512,954 unique timestamps after retry fill, no zero volume rows

DUKASCOPY_USA500IDXUSD 5m:
2022-01-02 18:00 NY -> 2026-07-02 23:55 NY
307,996 unique timestamps after retry fill, no zero volume rows

DUKASCOPY_XAGUSD 3m:
2022-01-02 18:00 NY -> 2026-07-02 23:57 NY
531,491 unique timestamps after retry fill, no zero volume rows

DUKASCOPY_XAGUSD 5m:
2022-01-02 18:00 NY -> 2026-07-02 23:55 NY
318,954 unique timestamps after retry fill, no zero volume rows
```

Some old one-day Dukascopy failure logs remain for audit, but the SPX and Silver failed days
were successfully retried into separate CSV files. Loader de-duplicates timestamps.

```text
work/backtest/data/raw/dukascopy_failed_chunks_usatechidxusd_*.csv
```

Current NAS100 5m files:

```text
CAPITALCOM_NAS100, 5_a8a12.csv
2025-11-02 18:00 NY -> 2026-02-13 16:55 NY

CAPITALCOM_NAS100, 5_4eef5.csv
2026-02-15 18:00 NY -> 2026-05-25 14:25 NY
```

Current NAS100 3m files:

```text
CAPITALCOM_NAS100, 3_aedd5.csv
2025-11-16 18:00 NY -> 2026-01-16 16:57 NY

CAPITALCOM_NAS100, 3_aec17.csv
2026-01-18 18:00 NY -> 2026-03-22 18:12 NY

CAPITALCOM_NAS100, 3_c9279.csv
2026-03-22 18:03 NY -> 2026-05-25 14:27 NY
```

Practical shared 3m + 5m range:

```text
2025-11-16 18:00 NY -> 2026-05-25 14:25/14:27 NY
```

## Current Commands

Run data inspection:

```powershell
cd C:\Users\ISAAC\Documents\otobacktestprojesi\work\backtest
python -m backtest.cli inspect data/raw --output outputs/reports/data_inspection.csv
```

Run 5m backtest:

```powershell
python -m backtest.cli run data/raw --timeframe 5m --output-dir outputs/reports/5m
```

Run 3m backtest:

```powershell
python -m backtest.cli run data/raw --timeframe 3m --output-dir outputs/reports/3m
```

Optional daily trade limit override:

```powershell
python -m backtest.cli run data/raw --timeframe 5m --max-trades-per-day 1 --output-dir outputs/reports/5m_test
```

Optional direction, setup type, and weekday filters:

```powershell
python -m backtest.cli run data/raw --symbol CAPITALCOM_SPX500 --timeframe 5m --max-trades-per-day 2 --setup-type fvg --allowed-weekdays Tuesday,Wednesday,Friday --output-dir outputs/reports/filtered/spx_5m_max2_fvg_tue_wed_fri
```

Analyze manual calibration examples:

```powershell
python -m backtest.cli calibrate --examples calibration_examples --raw data/raw --output outputs/reports/calibration_analysis.csv
```

## Current Strategy Engine

Implemented in:

```text
work/backtest/backtest/strategy.py
```

Current deterministic logic:

1. Merge raw CSV files for selected symbol/timeframe.
2. Drop duplicate timestamps.
3. Skip dates with serious profile/trade-window gaps.
4. For each trade date, calculate volume profile from:

```text
previous day 18:00 NY -> trade day 09:30 NY
```

5. Calculate VAH/VAL with:

```text
value area = 70%
rows = 1000
```

6. During 09:30-12:00 NY, setup candidate requires:

- candle near VAH or VAL
- sweep of an untaken liquidity level

7. Liquidity levels currently include:

- Asia high/low
- London high/low
- prior NY PM high/low
- previous day high/low
- recent swing high/low liquidity

8. Sweep direction does not directly determine trade direction.
9. After sweep, whichever direction gets CISD + FVG/IFVG first determines trade direction:

```text
bullish CISD + bullish FVG/IFVG -> long
bearish CISD + bearish FVG/IFVG -> short
```

10. Entry:

```text
FVG/IFVG start boundary
```

Pending limit cancellation:

```text
If price reaches the 3R target before the entry limit is filled, the setup is cancelled.
Later return to the entry price must not count as a filled trade.
```

11. Stop:

```text
long stop = sweep wick low
short stop = sweep wick high
```

12. Target:

```text
3R
```

13. Max trades:

```text
default config: 2 trades per day
CLI can override with --max-trades-per-day
```

## Current Parameters

Written by each run to:

```text
outputs/reports/5m/parameters.csv
outputs/reports/3m/parameters.csv
```

Current values:

```text
symbol: CAPITALCOM_NAS100
vah_val_tolerance: 5.0
spread_points: 1.0
slippage_points: 0.5
max_trades_per_day: 2
value_area_pct: 0.70
volume_profile_rows: 1000
reward_r: 3.0
fvg_entry_mode: start
direction_filter: all
setup_type_filter: all
allowed_weekdays: all
swing_lookback_candles: 100
equal_swing_tolerance: 5.0
```

## Backtest Results

Old generated reports were cleared on 2026-07-03 before starting cross-symbol tests.

Current report folder:

```text
outputs/reports/
```

The folder now contains generated reports and partial long-data Dukascopy candidate tests.

Dukascopy full-data candidate report work started on 2026-07-04:

```text
Script:
work/backtest/scripts/run_dukascopy_candidate_report.py

Report folder:
work/backtest/outputs/reports/dukascopy_candidate_full_report/

Data range:
2022-01-02 18:00 NY -> 2026-07-02 23:55/23:57 NY

Strategy:
entry_mode=start
stop_management=none
RR grid=1R,2R,3R,4R
Gold excluded because it failed earlier candidate tests.
```

Candidates in this report:

```text
NQ:
DUKASCOPY_USATECHIDXUSD, 3m, max 1 trade/day, Monday/Wednesday/Thursday/Friday

SPX:
DUKASCOPY_USA500IDXUSD, 5m, max 2 trades/day, Tuesday/Wednesday/Friday, setup_type=fvg

Silver:
DUKASCOPY_XAGUSD, 5m, max 1 trade/day, Tuesday/Wednesday/Thursday
```

The first full report run was interrupted because it took too long, but 11 of 12 runs completed.
On 2026-07-04, the missing Silver 4R run was completed and the final report files were generated:

```text
work/backtest/outputs/reports/dukascopy_candidate_full_report/report.html
work/backtest/outputs/reports/dukascopy_candidate_full_report/report.md
work/backtest/outputs/reports/dukascopy_candidate_full_report/comparison.csv
work/backtest/outputs/reports/dukascopy_candidate_full_report/all_trades.csv
```

Completed partial long-data results before interruption:

```text
NQ 1R:     543 trades, 64.09% WR, 153.0R, -8.0R DD
NQ 2R:     543 trades, 44.20% WR, 177.0R, -12.0R DD
NQ 3R:     543 trades, 32.60% WR, 165.0R, -18.0R DD
NQ 4R:     543 trades, 27.44% WR, 202.0R, -19.0R DD

SPX 1R:    580 trades, 70.52% WR, 238.0R, -6.0R DD
SPX 2R:    542 trades, 50.00% WR, 271.0R, -8.0R DD
SPX 3R:    526 trades, 38.97% WR, 294.0R, -10.0R DD
SPX 4R:    524 trades, 29.96% WR, 261.0R, -16.0R DD

Silver 1R: 388 trades, 59.28% WR, 72.0R, -11.0R DD
Silver 2R: 388 trades, 39.69% WR, 74.0R, -19.0R DD
Silver 3R: 388 trades, 29.38% WR, 68.0R, -22.0R DD
Silver 4R: 388 trades, 22.94% WR, 57.0R, -38.0R DD
```

Dukascopy stop/entry model tests generated on 2026-07-04:

```text
Script:
work/backtest/scripts/run_dukascopy_stop_entry_model_report.py

Report folder:
work/backtest/outputs/reports/dukascopy_stop_entry_model_tests/

Files:
report.html
report.md
comparison.csv
monthly_detail.csv
all_trades.csv

Scope:
Same 3 Dukascopy candidates as the full candidate report.
RR grid excludes 1R and 4R; only 2R and 3R were tested.
Stop management = none.
```

Tested models:

```text
Sweep wick stop:
entry_mode=start, stop_model=sweep_wick

CISD body stop:
entry_mode=start, stop_model=cisd_body

FVG opposite edge stop:
entry_mode=start, stop_model=fvg_opposite_edge

Swing based stop:
entry_mode=start, stop_model=swing_based

FVG body-end entry + sweep wick stop:
entry_mode=body_end, stop_model=sweep_wick
```

Report-specific implementation note:

```text
backtest/config.py now has stop_model.
backtest/cli.py supports --stop-model and entry-mode body_end.
backtest/strategy.py supports sweep_wick, cisd_body, fvg_opposite_edge, and swing_based initial stops.
monthly_detail.csv includes candidate/model/RR/month plus total_trades, tp, sl, open_trades, win_rate, net_r, mtm_net_r.
```

Stop/entry model highlights:

```text
NQ best balance:
FVG opposite edge stop 3R: 547 trades, 63.80% WR, 849.0R, -6.0R DD, PF 5.29

SPX best balance:
FVG opposite edge stop 3R: 674 trades, 71.96% WR, 1266.0R, -4.0R DD, PF 7.70

Silver best balance:
FVG opposite edge stop 2R: 388 trades, 59.28% WR, 302.0R, -7.0R DD, PF 2.91
```

Manual NQ review warning added on 2026-07-05:

```text
User manually reviewed the first 15 NQ FVG opposite edge 3R sample trades.
Result: FAIL 9, LEVEL_ISSUE 4, PASS 1, PASS_WITH_CONTEXT_ISSUE 1.

Main issues:
- Already-taken London/Asia/session liquidity was still eligible as a NY sweep trigger.
- Generic swing high/low was often selected instead of named session liquidity.
- Previous NY AM high/low was missing.
- CISD timestamps often did not match manual chart reading.
- Some fills/TPs were counted around the FVG formation candle where manual review says price did not truly revisit entry.
- Very small FVGs need a minimum-size filter.

Archived feedback:
work/backtest/outputs/reports/manual_review_feedback_archive/nq_first15/
```

Manual SPX review warning added on 2026-07-05:

```text
User manually reviewed the first 10 SPX corrected-engine 3-month sample trades.
The review CSV still has blank review_status values, so classification is based on
C:\Users\ISAAC\Desktop\SPX 15 islem kontrol.txt.

Result for first 10 reviewed rows:
PASS 3, FAIL 6, LEVEL_ISSUE 1.

Main issues:
- CISD timestamps are often wrong even when the final trade is manually acceptable.
- Some CISDs appear invalid, reused from before sweep, or hallucinated.
- Sweep validation is still too loose, especially generic swing sweeps.
- Same-session second-trade logic needs a rule:
  same-direction follow-up may not require a fresh sweep, opposite direction should.
- CISD invalidation is missing when price breaks structure after CISD but before valid entry setup.
- FVG selection can be too late; engine should select the earliest valid post-CISD FVG/IFVG.
- Target-before-entry-fill cancellation needs stricter verification.

Archived feedback:
work/backtest/outputs/reports/manual_review_feedback_archive/spx_first10/spx_first10_feedback_analysis.md
```

Manual regression harness added on 2026-07-08:

```text
Cases:
work/backtest/calibration_examples/manual_regression_cases.csv

Script:
work/backtest/scripts/run_manual_regression_harness.py

Report folder:
work/backtest/outputs/reports/manual_regression_harness/

Purpose:
This is not a profitability test. It checks whether the engine takes/skips the same
scenarios as the manual SPX first-10 review.
```

Manual regression result on 2026-07-08:

```text
Baseline CHALLENGE_CORE_V2:
6/9 scored cases passed, 1 WATCH not scored, pass rate 66.67%.

Best current mini-v3:
challenge_core_v3_spx_session_opening_reversal
7/9 scored cases passed, 1 WATCH not scored, pass rate 77.78%.

What improved:
- Fixed the 2023-10-31 invalid trade case by combining session-only liquidity with
  opening premarket reversal direction logic.

Rejected / not promoted:
- strong_swing alone did not improve the SPX first-10 manual set.
- rejection-close alone did not improve the SPX first-10 manual set.
- active_trade_block_mode=entry did not produce the expected 2023-10-11 second short.
- Extending SPX follow-up scan to 11:30 did not produce the expected second short and
  reintroduced a bad 2023-10-17 trade, so it is not a default candidate.

Current machine gaps from manual regression:
1. 2023-10-04 expected TAKE is still missed. The gap is CISD detection/timing, not
   broad liquidity filtering.
2. 2023-10-11 expected second same-direction short is still missed. This is not only
   active-position blocking; the later manual structure is outside/unsupported by the
   current CISD/FVG selection rules.
3. FVG timing can still be late versus manual reading, for example 2023-10-11 where
   the engine accepts the direction but selects a later FVG than the manual note.

Implementation note:
backtest/config.py now has active_trade_block_mode with default "exit". This preserves
current production behavior. Manual harness variants can test "entry", "fvg", or "none"
without changing CHALLENGE_CORE_V2 defaults.
```

Manual CISD diagnostic pass on 2026-07-09:

```text
Script:
work/backtest/scripts/run_manual_cisd_diagnostics.py

Report folder:
work/backtest/outputs/reports/manual_cisd_diagnostics/

Focused cases:
- SPX-MAN-001, 2023-10-04 expected TAKE around 10:00 CISD.
- SPX-MAN-002, 2023-10-11 first short.
- SPX-MAN-003, 2023-10-11 second same-direction short around 11:15 CISD.

Findings:
1. 2023-10-04 is not actually missing CISD anymore in the best v3 diagnostic profile.
   The engine finds short CISD at 10:00, matching the manual note.
   The trade is still skipped because the standard 3-candle bearish FVG is only detected
   at 10:50, outside the normal fvg_window=5. When fvg_window is extended to 12, the
   FVG appears, but price does not revisit the midpoint entry after that FVG is formed.
   Earlier touches are correctly ignored because the FVG did not exist yet.

2. 2023-10-11 second trade is not solved by active_trade_block_mode=entry or by extending
   SPX scan to 11:30. Diagnostic sees a possible continuation short around 10:45-11:00
   with CISD 11:10 and FVG 11:20, but the midpoint entry is not filled. At 11:15 the
   engine also sees a fresh asia_low sweep, which conflicts with the manual reading that
   treats the move as part of the short continuation structure.

3. fvg_window_candles=12 and latest_entry_time=None were tested as harness variants.
   They did not improve the scored manual pass rate and reintroduced a bad 2023-10-17
   trade, so they are not promoted.

Conclusion:
Do not add a broad speed/frequency rule here. The remaining mismatch is more specific:
manual PD-array/entry-zone interpretation is broader than the engine's current standard
wick-based 3-candle FVG midpoint model. Next technical step should test explicit PD-array
variants in the manual harness, especially body-FVG, IFVG conversion, and OTE/discount-
premium entry zones, before changing lifecycle candidates.
```

Manual PD-array variant pass on 2026-07-09:

```text
Engine additions:
backtest/strategy.py now supports setup_type_filter="body_fvg" and "pd_array".
backtest/strategy.py and backtest/cli.py now support entry modes:
- ote_62
- ote_705
- ote_79

These are not active CHALLENGE_CORE_V2 defaults. They were added for manual-regression
testing.

Harness profiles added:
- challenge_core_v3_spx_body_fvg_midpoint
- challenge_core_v3_spx_body_fvg_ote705
- challenge_core_v3_spx_body_fvg_window12
- challenge_core_v3_spx_body_fvg_start_followup

Manual regression result:
Best clean body-FVG variants:
- challenge_core_v3_spx_body_fvg_midpoint: 8/9 scored, 1 WATCH, 88.89%.
- challenge_core_v3_spx_body_fvg_ote705: 8/9 scored, 1 WATCH, 88.89%.
- challenge_core_v3_spx_body_fvg_window12: 8/9 scored, 1 WATCH, 88.89%.

Improvement versus previous best:
- Previous best session/opening-reversal: 7/9 scored, 77.78%.
- Body-FVG fixes SPX-MAN-001 2023-10-04.
- The engine now finds:
  CISD 10:00, body-FVG 10:25, entry 10:30.
  This matches the manual idea better than standard wick-FVG, which only appeared at 10:50.

Remaining fail:
- SPX-MAN-003 2023-10-11 second same-direction short.
  Clean body-FVG variants still produce only one trade that day.

Rejected:
- challenge_core_v3_spx_body_fvg_start_followup catches the 2023-10-11 second trade, but
  also wrongly takes SPX-MAN-004 2023-10-13 and SPX-MAN-005 2023-10-17. It drops back to
  7/9 and should not be promoted.

Current conclusion:
Body-FVG is the first meaningful manual-alignment improvement after the corrected engine.
It should be the next candidate for broader validation, but not yet promoted to the active
operating plan until NQ/SPX full backtest and lifecycle impact are measured.
```

Body-FVG 6-month validation generated on 2026-07-09:

```text
Script:
work/backtest/scripts/run_body_fvg_6month_validation.py

Report folder:
work/backtest/outputs/reports/body_fvg_6month_validation/

Scope:
NQ + SPX active phase/funded legs.
Selected months:
2022-12, 2023-06, 2023-09, 2023-10, 2025-02, 2025-03

Pair-level daily cap:
-1R

Tested variants:
- baseline
- SPX body-FVG midpoint only
- SPX body-FVG OTE 70.5 only
- NQ + SPX body-FVG midpoint
```

6-month pair results:

```text
phase_selected baseline:
36 trades, 41.67% WR, +22.0R, -6.0R DD, PF 2.05

phase_selected SPX body-FVG midpoint:
48 trades, 37.50% WR, +20.5R, -9.0R DD, PF 1.68

phase_selected SPX body-FVG OTE 70.5:
47 trades, 38.30% WR, +21.5R, -9.0R DD, PF 1.74

phase_selected both NQ+SPX body-FVG midpoint:
65 trades, 36.92% WR, +27.5R, -17.0R DD, PF 1.67

funded_selected baseline:
24 trades, 58.33% WR, +23.0R, -5.0R DD, PF 3.30

funded_selected SPX body-FVG midpoint:
34 trades, 41.18% WR, +13.0R, -5.0R DD, PF 1.65

funded_selected SPX body-FVG OTE 70.5:
33 trades, 39.39% WR, +11.0R, -5.0R DD, PF 1.55

funded_selected both NQ+SPX body-FVG midpoint:
36 trades, 44.44% WR, +19.0R, -5.0R DD, PF 1.95
```

Extra-trade diagnostic:

```text
SPX phase body-FVG midpoint extra trades:
17 extra trades, 23.53% WR, -3.0R.

SPX phase body-FVG OTE 70.5 extra trades:
17 extra trades, 23.53% WR, -3.0R.

SPX funded body-FVG midpoint extra trades:
18 extra trades, 27.78% WR, -3.0R.

SPX funded body-FVG OTE 70.5 extra trades:
18 extra trades, 22.22% WR, -6.0R.
```

Decision:

```text
Do not promote body-FVG as a broad active operating-plan rule.

It improves the small manual SPX first-10 regression set from 7/9 to 8/9, but broad
6-month validation shows the unfiltered extra body-FVG trades are negative and reduce
SPX/funded quality.

Best interpretation:
body-FVG is probably a valid manual PD-array type, but it needs an additional quality/context
gate before use. Candidate filters to test next:
- body-FVG only after a cleaner named-session sweep, not generic continuation everywhere.
- body-FVG only when aligned with VAH/VAL side and first30 regime.
- body-FVG only as replacement for missed standard FVG cases, not as additive frequency.
- require body-FVG size/range threshold or displacement candle quality.
```

Body-FVG replacement/filter tests generated on 2026-07-09:

```text
Script:
work/backtest/scripts/run_body_fvg_replacement_filters.py

Report folder:
work/backtest/outputs/reports/body_fvg_replacement_filters/

Source:
work/backtest/outputs/reports/body_fvg_6month_validation/all_trades.csv

Goal:
Test body-FVG as a fallback/replacement only, not broad additive frequency.

Filters tested:
- body-FVG only on SPX days where baseline SPX has no trade.
- named-session body-FVG only, excluding generic swing liquidity.
- first named body-FVG only.
- compact named body-FVG only, body-FVG size <= 2.0.
- strong displacement named body-FVG only, FVG candle body/range >= 0.7.

Pair cap:
-1R daily pair cap.
```

Replacement/filter results:

```text
phase_selected baseline:
36 trades, 41.67% WR, +22.0R, -6.0R DD, PF 2.05

phase_selected SPX body-FVG no-baseline-day:
48 trades, 35.42% WR, +17.0R, -9.0R DD, PF 1.55

phase_selected SPX named body-FVG no-baseline-day:
42 trades, 38.10% WR, +19.5R, -8.0R DD, PF 1.75

phase_selected SPX named compact body-FVG size <= 2.0:
39 trades, 41.03% WR, +22.5R, -7.0R DD, PF 1.98

phase_selected SPX named strong-displacement body-FVG ratio >= 0.7:
36 trades, 41.67% WR, +22.0R, -6.0R DD, PF 2.05
No added trades versus baseline.

funded_selected baseline:
24 trades, 58.33% WR, +23.0R, -5.0R DD, PF 3.30

funded_selected SPX named body-FVG no-baseline-day:
25 trades, 56.00% WR, +22.0R, -5.0R DD, PF 3.00

funded_selected SPX named compact body-FVG size <= 2.0:
25 trades, 56.00% WR, +22.0R, -5.0R DD, PF 3.00

funded_selected SPX named strong-displacement body-FVG ratio >= 0.7:
24 trades, 58.33% WR, +23.0R, -5.0R DD, PF 3.30
No added trades versus baseline.
```

Decision:

```text
Do not promote body-FVG replacement to lifecycle yet.

Best filtered result is phase_selected named compact body-FVG size <= 2.0:
+0.5R net versus baseline, but DD worsens from -6R to -7R and PF drops from 2.05 to 1.98.
Funded result is worse than baseline by -1R.

Interpretation:
body-FVG is useful diagnostically because it explains manual SPX-MAN-001, but current
mechanical filters are not strong enough for production. It should remain a manual-regression
research branch, not a challenge/funded operating-plan change.

Next better path:
Add more manually reviewed examples before further optimizing body-FVG. The current 10 SPX
cases are enough to identify the missing PD-array type, but not enough to safely learn the
quality gate. Need more examples specifically labelled:
- body-FVG accepted manually
- body-FVG rejected manually
- second same-direction continuation accepted/rejected
```

Manual feedback package V1 generated on 2026-07-09:

```text
Script:
work/backtest/scripts/build_manual_feedback_package.py

Folder:
work/backtest/outputs/reports/manual_feedback_package_v1/

Review file:
work/backtest/outputs/reports/manual_feedback_package_v1/manual_review_cases_blind.csv

Excel review file:
work/backtest/outputs/reports/manual_feedback_package_v1/manual_feedback_package_v1.xlsx

Answer key, do not review before manual labels:
work/backtest/outputs/reports/manual_feedback_package_v1/answer_key_do_not_review_first.csv

Instructions:
work/backtest/outputs/reports/manual_feedback_package_v1/README.md

Case count:
54 total

Case mix:
- 27 body_fvg_quality
- 12 body_fvg_entry_model
- 12 standard_fvg_control
- 3 same_day_or_continuation

Manual columns to fill:
- manual_decision: TAKE, SKIP, WATCH
- manual_direction: long, short, blank if SKIP
- manual_liquidity_ok: yes, no, unsure
- manual_cisd_ok: yes, no, unsure
- manual_pd_array_ok: yes, no, unsure
- manual_entry_ok: yes, no, unsure
- manual_context_notes
- manual_reason

Purpose:
Learn a real body-FVG quality gate from manual labels, not from backtest optimization.
```

Manual feedback regression V2 generated on 2026-07-12:

```text
Source feedback:
C:\Users\ISAAC\Desktop\işlem kontrol bitir.txt

Structured cases:
work/backtest/calibration_examples/manual_feedback_regression_v2.csv

Runner:
work/backtest/scripts/run_manual_feedback_regression_v2.py

Report folder:
work/backtest/outputs/reports/manual_feedback_regression_v2/

Scope:
21 completed manual SPX dates.
The 6 unfinished dates remain outside the set and can be used as a later holdout review.

Labels added beyond TAKE/SKIP and direction:
- liquidity requirement: required, optional, or replaced by HTF transition
- 15m/30m PD-array context and body-close state
- CISD lifecycle/invalidation
- entry model and pending-order lifecycle
- contextual stop reference
- generalization strength
```

Manual feedback V2 regression result:

```text
CHALLENGE_CORE_V2 baseline:
5/21 passed, 23.81% manual decision/direction alignment.

Best existing research profile:
challenge_core_v3_spx_body_fvg_start_followup:
10/21 passed, 47.62% alignment.

Important interpretation:
- The earlier small 8/9 body-FVG result does not generalize to the broader 21-case manual set.
- No existing profile reaches acceptable manual alignment.
- Existing body-FVG profiles model an LTF entry PD array; they do not model the newly identified
  15m/30m opposing-PD-array body-close state gate.
- Baseline scored 0% in the new CISD invalidation, entry lifecycle, entry model,
  liquidity-context, and multi-timeframe-entry buckets.
- Do not promote any tested profile or alter CHALLENGE_CORE_V2 / FON_CORE.

Next narrow technical step:
Add diagnostic 15m/30m PD-array state extraction first, without changing trade selection.
Use the 21 cases to verify respected/body-broken state labels before testing an HTF blocker gate.
Keep the target-before-fill exception as WATCH because it currently has only one clear example.
Same-day continuation still lacks enough accepted/rejected examples for a general rule.
```

Read-only HTF PD-array diagnostic generated on 2026-07-12:

```text
Script:
work/backtest/scripts/run_manual_htf_pd_array_diagnostics.py

Report folder:
work/backtest/outputs/reports/manual_htf_pd_array_diagnostics/

Files:
- report.md
- htf_zones.csv
- case_summary.csv
- manual_state_comparison.csv

Method:
- Resample existing SPX 5m data into complete 15m and 30m candles.
- Detect standard wick-based 3-candle FVG zones.
- Track each relevant zone as untouched, wick_touched_respected,
  body_closed_inside, or body_closed_through.
- Drop old zones already body-closed-through before 09:30 NY.
- Keep the diagnostic read-only; it does not block or create trades.

Result:
- 21 manual cases processed.
- 9 cases have explicit manual HTF body-state labels.
- A heuristic HTF direction/state candidate was found for 8/9 labelled cases.
- 2023-06-20 did not produce a matching bullish respected FVG under the current
  standard wick-FVG definition and relevance thresholds.
- 327 non-stale relevant 15m/30m candidate zones were retained across the 21 dates.

Important limitation:
8/9 is candidate coverage, not proven manual agreement. Manual HTF zone price coordinates
are not labelled, so multiple nearby machine zones can satisfy the same qualitative state.
Do not promote an HTF blocker gate from this result alone.

Next narrow step:
Use the chart comments/screenshots to identify the exact manual HTF zone per labelled case,
then test whether the diagnostic selected the same zone and transition. Only after exact-zone
validation should an HTF blocker be allowed to affect manual-regression trade decisions.
```

Exact manual HTF zone validation completed on 2026-07-12:

```text
Manual zone labels:
work/backtest/calibration_examples/manual_htf_zone_labels_v1.csv

Updated diagnostic output:
work/backtest/outputs/reports/manual_htf_pd_array_diagnostics/exact_zone_comparison.csv

Method:
- Opened the 9 linked TradingView snapshots from the manual feedback.
- Read the explicitly drawn 15m/30m FVG boxes against the chart price axis.
- Stored timeframe, direction, approximate lower/upper bounds, expected body state,
  source URL, and confidence.
- Compared those boxes with the resampled Dukascopy HTF zones.
- Allowed 1.0 point tolerance for Capital.com versus Dukascopy feed differences and
  chart-axis reading precision.
- When nested zones overlap, selected the machine zone with the closest center to the
  manually drawn box instead of the widest raw overlap.

Result:
- Exact price-zone match: 9/9.
- Expected respected/body-close state match: 9/9.
- All selected machine/manual zone center differences were below 1.0 SPX point.
- 2023-06-20 is a bearish 30m resistance FVG, not bullish. The prior 8/9 heuristic
  miss was caused by direction inference from prose, not by FVG detection.

Interpretation:
The resampled 15m/30m standard wick-FVG detector can reproduce the exact manual HTF FVG
zones in all currently labelled cases. This validates the diagnostic representation,
but not yet the trade blocker rule itself.

Active plan remains unchanged:
- Challenge: CHALLENGE_CORE_V2
- Funded: FON_CORE
- NQ + SPX daily pair cap: -1R

Next narrow step:
Add an HTF blocker-gate research profile only in the manual regression harness:
- opposing respected HTF FVG blocks the trade;
- body_closed_through unlocks the trade direction;
- aligned respected HTF FVG provides context but does not create a trade by itself.
Do not apply this gate to production/backtest defaults until the 21-case regression result
and false-positive behavior are reviewed.
```

Manual HTF blocker-gate research generated on 2026-07-12:

```text
Script:
work/backtest/scripts/run_manual_htf_blocker_gate_research.py

Report folder:
work/backtest/outputs/reports/manual_htf_blocker_gate_research/

Files:
- report.md
- summary.csv
- comparison.csv
- blocked_trades.csv
- filtered_trades.csv

Scope:
Manual-regression-only post-filter. No strategy/config/default changes.

Tested source profiles:
- CHALLENGE_CORE_V2 baseline
- challenge_core_v3_spx_body_fvg_start_followup

Tested blocker variants:
- opposing active/touched HTF FVG within 10 points
- opposing active/touched HTF FVG within 15 points
- opposing active/touched HTF FVG within 25 points
- dominant/newest active touched HTF FVG within 15 points

Results:
CHALLENGE_CORE_V2 no gate: 5/21, 23.81%.
CHALLENGE_CORE_V2 best gate rows: 10/21, 47.62%.

Body-FVG start/follow-up no gate: 10/21, 47.62%.
Body-FVG start/follow-up best gate rows: 12/21, 57.14%.

Positive effect:
The gate correctly removes several manual SKIP trades, including examples where an opposing
respected HTF FVG should block the LTF setup.

False-block findings:
- 2023-06-16 manual TAKE can still be blocked because the manual unlock is a 15m order-block
  body close. The current HTF diagnostic models FVGs but not order blocks.
- 2023-09-29 manual TAKE can be blocked by older nearby bullish HTF FVGs that the manual review
  did not treat as the controlling array. Nearest/newest FVG alone is not a sufficient relevance rule.
- A broad opposing-FVG rule can also block valid trades such as 2023-10-11 unless the controlling
  aligned versus opposing array is selected correctly.

Decision:
Do not promote the HTF blocker gate.
57.14% is an improvement but still too low, and the remaining false blocks expose missing HTF
order-block detection and controlling-array selection.

Active plan remains unchanged:
- Challenge: CHALLENGE_CORE_V2
- Funded: FON_CORE
- NQ + SPX daily pair cap: -1R

Next narrow step:
Add read-only 15m order-block candidates and a controlling-array diagnostic. Validate which
single HTF array the manual review treats as controlling before rerunning the blocker gate.
```

Read-only 15m order-block and controlling-array research completed on 2026-07-12:

```text
Manual OB label:
work/backtest/calibration_examples/manual_htf_orderblock_labels_v1.csv

OB diagnostic script:
work/backtest/scripts/run_manual_htf_orderblock_diagnostics.py

OB diagnostic report:
work/backtest/outputs/reports/manual_htf_orderblock_diagnostics/report.md

OB detector definition:
- 15m opposite-color origin candle.
- Next 15m displacement candle closes beyond the origin wick.
- Displacement body/range >= 0.55.
- OB zone uses the origin candle body.
- body_closed_inside and body_closed_through are tracked separately.

2023-06-16 exact OB result:
- Manual bullish 15m OB: approximately 4427.80-4429.60.
- Machine OB: 4427.86-4429.56.
- Center difference: 0.01 SPX point.
- Origin candle: 07:45 NY.
- Confirmed after 08:00 displacement, available at 08:15 NY.
- Machine detects a body close back inside the OB before the manual short entry.
- Exact zone/state match: 1/1.

Controlling-array research variant:
controlling_fvg_ob_15pt

Rule:
- Combine touched 15m/30m FVGs and confirmed 15m OBs within 15 points of entry.
- Use the newest relevant candidate as controlling array.
- Opposing active controlling array blocks.
- FVG body_closed_through unlocks.
- OB body_closed_inside or body_closed_through unlocks.
- Aligned arrays never create a trade alone.

Results:
CHALLENGE_CORE_V2 baseline: 5/21.
CHALLENGE_CORE_V2 controlling FVG+OB: 9/21.

Body-FVG start/follow-up baseline: 10/21.
Body-FVG start/follow-up controlling FVG+OB: 12/21, 57.14%.

Important improvement:
The controlling FVG+OB variant improves the Body-FVG profile by 2 cases without turning any
previous PASS into FAIL. It fixes 2023-06-27 direction/permission behavior and 2023-09-13 SKIP.

Remaining limitation:
The selector is safer but incomplete. It does not yet fix every explicitly labelled HTF blocker,
including 2023-06-13, because a mechanically newer nearby candidate can outrank the manually
controlling zone. More exact controlling-array labels are needed before changing selection logic.

Decision:
Do not promote. Keep as manual-regression research only.
Active CHALLENGE_CORE_V2 / FON_CORE / daily -1R pair cap remain unchanged.
```

Controlling-array selector diagnostics generated on 2026-07-12:

```text
Consolidated exact labels:
work/backtest/calibration_examples/manual_controlling_array_labels_v1.csv

Script:
work/backtest/scripts/run_manual_controlling_array_diagnostics.py

Report folder:
work/backtest/outputs/reports/manual_controlling_array_diagnostics/

Files:
- report.md
- selector_summary.csv
- selector_comparison.csv

Scope:
9 exact manually labelled controlling arrays:
- 8 HTF FVG selections
- 1 HTF order-block selection

Candidate coverage:
The correct manual array exists in the machine candidate pool for 9/9 cases.
The oracle_exact_zone column confirms coverage using the manual price box and is not a live selector.

Non-oracle selector results:
- newest: 5/9, 55.56%
- opening newest: 5/9, 55.56%
- opening 30m newest: 5/9, 55.56%
- closest to open: 2/9, 22.22%
- closest to VAH/VAL: 2/9, 22.22%
- opening closest to open: 2/9, 22.22%
- opening closest to VAH/VAL: 2/9, 22.22%
- largest: 1/9, 11.11%

Opening-touch finding:
8/9 exact manual arrays first interact at 09:30 NY; 1/9 first interacts at 10:00 NY.
This is a useful relevance feature, but opening-touch plus newest/closest ranking still selects
the correct controlling array only 5/9 times.

Gate follow-up:
opening_controlling_fvg_ob_15pt was added to the manual gate research.
It remains 12/21 on the Body-FVG source profile and creates no improvement over the existing
controlling_fvg_ob_15pt result.

Conclusion:
No single mechanical selector is accurate enough to promote.
The remaining missing feature is semantic: why price reacted/reversed at one array while nearby
arrays were ignored. Proximity, recency, size, timeframe, and opening touch alone do not encode it.

Decision:
Do not tune more selector weights on 9 cases; that would overfit.
Do not change CHALLENGE_CORE_V2 / FON_CORE / daily -1R cap.
The next useful manual-data addition should label the controlling-array reason, for example:
- caused opening reaction
- aligned with VA side
- support-to-resistance or resistance-to-support conversion
- protected/unprotected liquidity behind the array
- displacement away from the array
- competing array explicitly ignored and why
```

Manual controlling-array reasons and reaction diagnostics generated on 2026-07-12:

```text
Structured reason labels:
work/backtest/calibration_examples/manual_controlling_array_reasons_v1.csv

Reason analysis script:
work/backtest/scripts/analyze_manual_controlling_array_reasons.py

Reason report folder:
work/backtest/outputs/reports/manual_controlling_array_reason_analysis/

Reason roles across 9 exact cases:
- unlock: 3
- support/aligned confirmation: 3
- block: 2
- directional bias: 1

Array relationship:
- opposing: 5
- aligned: 4

Reason coverage:
- opening reaction: 9/9
- VA context: 9/9
- HTF body-state role: 9/9
- structure/liquidity/displacement context: 9/9 from explicit comments
- competing-array ignored and reason: 6/9
- unknown competing-array reason remains for 2022-12-28, 2023-06-06, and 2023-09-01

Machine measurability conclusion:
- Array type/timeframe/direction/body state are directly measurable.
- Opening reaction, VA distance, and displacement are mechanically measurable.
- Structure alignment and optional-versus-required liquidity are only partially measurable.
- Why one competing array is ignored remains semantic/manual-only with the current labels.
```

```text
Reaction diagnostic script:
work/backtest/scripts/run_manual_array_reaction_diagnostics.py

Reaction report folder:
work/backtest/outputs/reports/manual_array_reaction_diagnostics/

Test:
For opening-touch candidates, rank arrays by favorable excursion after first touch,
normalized by the prior-hour median 5m candle range.

Results:
- 15-minute reaction selector: 4/9, 44.44%
- 30-minute reaction selector: 5/9, 55.56%
- 45-minute reaction selector: 4/9, 44.44%

Interpretation:
Reaction displacement does not improve on newest/opening-newest selection.
The strongest mechanical post-touch reaction is not reliably the manually controlling array.

Decision:
Stop selector-weight tuning on the 9-case set. Proximity, VA distance, recency, size,
opening touch, and reaction displacement have now all failed to exceed 5/9 non-oracle accuracy.
Do not promote the HTF gate and do not change the active operating plan.

Best current research result remains:
Body-FVG start/follow-up + controlling FVG/OB gate = 12/21 manual regression,
but the selector is not yet reliable enough for production.
```

Manual feedback V2 post-controlling-array FAIL analysis and CISD lifecycle test completed on 2026-07-12:

```text
Baseline CHALLENGE_CORE_V2 FAIL distribution across the 21 cases:
- HTF_PD_ARRAY: 2/6 passed (4 FAIL); controlling-array research is intentionally paused
  because the best non-oracle selector remains only 5/9 exact-array matches.
- CISD_INVALIDATION: 0/2 passed.
- LIQUIDITY_CONTEXT: 0/2 passed.
- ENTRY_LIFECYCLE: 0/2 passed.
- ENTRY_MODEL, MULTITIMEFRAME_ENTRY, and PENDING_EXCEPTION: each has only one case;
  do not generalize from them. A manual TAKE that subsequently hits SL remains a valid TAKE.

Selected narrow research rule:
- For one sweep, if the first directional CISD is invalidated before an opposite-direction
  PD array confirms, do not reuse that sweep for the opposite direction.
- This was tested only as
  challenge_core_v3_spx_body_fvg_initial_cisd_expiry, derived from the current best
  body-FVG start/follow-up research profile. It was never enabled in production defaults.

Result:
- 10/21, 47.62%, identical to the source profile; no prior PASS changed.
- It did not fix SPX-FB2-018 (2023-09-27). The engine's later short is attached to a
  separate 09:35 Asia-high sweep, while the failed bullish CISD belonged to the earlier
  09:30 sequence. Therefore same-sweep expiry is too narrow for the manual lifecycle rule.

Decision:
- Reject and remove the experimental rule/profile; do not promote it and do not run a
  second small-grid variation.
- Active plan remains unchanged: CHALLENGE_CORE_V2, FON_CORE, NQ + SPX pair cap -1R.
- The next evidence-gathering need is a manually labelled cross-sweep lifecycle boundary:
  when an invalidated opening intent should cancel the whole NY directional thesis versus
  when a later named-liquidity sweep legitimately starts a fresh thesis.
```

Entry/no-fill lifecycle diagnostic completed on 2026-07-12:

```text
Focus: the two ENTRY_LIFECYCLE cases, without changing engine behavior.

- SPX-FB2-010 (2023-06-14) is already corrected by the current body-FVG start/follow-up
  research profile: target-before-fill cancellation produces SKIP as the manual review does.
- SPX-FB2-005 (2022-12-28) remains a manual no-fill. On the available 5m Dukascopy candle,
  the first post-FVG bar spans both sides of the short entry boundary. The bar data therefore
  permits a retrace/fill, while the manual chart decision says the order was never filled.

Decision:
- Do not add an intrabar fill-order assumption from this single feed-resolution conflict.
  A rule that rejects a bar merely because it spans the entry level would have unmeasured,
  broad false-positive risk.
- Keep target-before-entry-fill cancellation unchanged and retain the manual no-fill as a
  labelled mismatch until 1m/tick evidence or additional manually labelled no-fill examples
  establish a repeatable execution-order rule.
- No active or research strategy defaults were changed by this diagnostic.
```

Entry/no-fill 3m confirmation and M1 retrieval attempt completed on 2026-07-12:

```text
- The 2022-12-28 3m Dukascopy series also supports a fill: the 10:00 NY candle opened above
  the short entry boundary and traded through it. The 5m no-fill mismatch is therefore not
  resolved by the existing 3m data.
- A one-day Dukascopy M1 download was attempted into the isolated research folder
  outputs/reports/manual_entry_lifecycle_m1/. The downloader did not return within 184 seconds,
  wrote no output file, and its exact child processes were stopped. No main raw-data files changed.

Decision:
- M1/tick evidence remains unavailable in this workspace. Do not manufacture an execution-order
  rule from the conflicting manual and OHLC evidence.
- Continue manual-alignment work only on fields with at least two consistent labels or an
  independently verifiable state transition; the unresolved 2022-12-28 no-fill remains a WATCH
  for execution-data collection rather than a strategy-rule candidate.
```

Dukascopy M1 downloader hardening completed on 2026-07-12:

```text
Cause of the earlier M1 attempt:
- dukascopy-node printed its request header but did not return data or exit; the outer command
  timeout left its npx/node child processes alive.

Fix:
- scripts/download_dukascopy.py now has --chunk-timeout-seconds (default 180).
- Each chunk runs as a managed subprocess. On timeout, the downloader terminates the complete
  process tree with taskkill /T /F on Windows, raises a normal chunk failure, and continues the
  existing split/failed-chunk audit path. This prevents orphaned Node processes.

Verification:
- Retried the isolated 2022-12-28 M1 request with a 45-second per-chunk timeout.
- The required 2022-12-27 -> 2022-12-30 source chunks all timed out without producing CSV data.
- The controlled failure audit was written to:
  outputs/reports/manual_entry_lifecycle_m1/dukascopy_failed_chunks_usa500idxusd_2022-12-28_2022-12-29.csv
- No orphan process and no main raw-data change remained.

Conclusion:
- The hang is fixed in project tooling, but this historical M1 provider response is currently
  unavailable. It cannot settle the 2022-12-28 manual no-fill disagreement.
```

Cross-sweep CISD lifecycle label seed added on 2026-07-12:

```text
File:
work/backtest/calibration_examples/manual_cross_sweep_lifecycle_labels_v1.csv

Purpose:
- Separate two superficially similar cases that the previous same-sweep expiry rule could not
  distinguish:
  - SPX-FB2-018 (2023-09-27): invalidated opening long thesis; later engine sweep is not a new
    manual thesis, so final decision is SKIP.
  - SPX-FB2-020 (2023-10-04): invalidated premarket short thesis; fresh London-high sweep plus
    a new bearish CISD/PD-array is a new manual thesis, so final decision is TAKE.

Decision:
- This is an evidence schema and two labelled seed cases, not a live rule and not a new selector.
- Add further manually reviewed contrasting cases to this file before attempting another
  cross-sweep lifecycle rule. The current two cases establish the required distinction but are
  insufficient to generalize safely.
```

Corrected engine changes on 2026-07-05:

```text
backtest/config.py:
- Added min_fvg_points.
- Added allow_entry_on_setup_candle, default false.
- Added require_post_sweep_cisd_reference, default true.
- Added invalidate_cisd_before_fvg, default true.
- Added allow_same_direction_followup_without_fresh_sweep, default true.
- Added session_liquidity_only.
- Added swing_liquidity_mode:
  all or strong_only. strong_only keeps only clustered/equal swing liquidity with at least 2 touches.
- Added trade_window_start and trade_window_end.

backtest/cli.py:
- Added --min-fvg-points.
- Added --allow-entry-on-setup-candle.

backtest/strategy.py:
- Filters out session levels already swept before 09:30 NY.
- Adds previous NY AM high/low liquidity.
- Prioritizes named session liquidity over generic swing levels when multiple levels are swept.
- Disallows entry fill on the FVG/IFVG formation candle by default.
- Applies symbol-specific minimum FVG size.
- CISD now requires a post-sweep reaction/reference candle by default.
- CISD can be invalidated before FVG/IFVG confirmation if structure breaks after CISD.
- FVG/IFVG selection now chooses the earliest valid candidate across enabled setup types.
- Same-direction follow-up trades can continue without a fresh sweep; opposite direction still requires a fresh sweep.
- Trade search window is now configurable instead of hardcoded 09:30-12:00.
- Swing liquidity can now be filtered to strong/equal-swing clusters only.
```

Corrected engine 6-random-month test generated on 2026-07-05:

```text
Script:
work/backtest/scripts/run_corrected_engine_6month_report.py

Report folder:
work/backtest/outputs/reports/corrected_engine_6month/

Random seed:
20260705

Selected months:
2022-12, 2023-06, 2023-09, 2023-10, 2025-02, 2025-03

Outputs:
report.html
report.md
comparison.csv
monthly_detail.csv
all_trades.csv
```

Corrected 6-month results after stricter SPX/NQ feedback actions:

```text
NQ FVG opposite edge 3R:
48 trades, 18.75% WR, -12.0R, -20.0R DD, PF 0.69

SPX FVG opposite edge 3R:
31 trades, 25.81% WR, +1.0R, -11.0R DD, PF 1.04

Silver FVG opposite edge 2R:
12 trades, 16.67% WR, -6.0R, -5.0R DD, PF 0.40
```

WR improvement grid generated on 2026-07-05:

```text
Script:
work/backtest/scripts/run_wr_improvement_grid_6month.py

Report folder:
work/backtest/outputs/reports/wr_improvement_grid_6month/

Selected months:
2022-12, 2023-06, 2023-09, 2023-10, 2025-02, 2025-03

Grid runs:
108

Tested:
- entry_mode: start, quarter_25, midpoint
- stop_model: fvg_opposite_edge, cisd_body, sweep_wick
- liquidity: all liquidity vs session-only liquidity
- RR: NQ/SPX 2R and 3R; Silver 1R and 2R

Important script fix:
run_corrected_engine_3month_report.py now selects each random month window separately
instead of scanning the full min-month to max-month range. This reduced the 6-month
candidate report runtime from about 5 minutes to about 71 seconds.
```

WR grid baseline rows:

```text
NQ baseline 3R start/fvg_opposite_edge/all-liquidity:
48 trades, 18.75% WR, -12.0R, -20.0R DD, PF 0.69

SPX baseline 3R start/fvg_opposite_edge/all-liquidity:
31 trades, 25.81% WR, +1.0R, -11.0R DD, PF 1.04

Silver baseline 2R start/fvg_opposite_edge/all-liquidity:
12 trades, 16.67% WR, -6.0R, -5.0R DD, PF 0.40
```

WR grid highlights:

```text
NQ best net R:
3R start/cisd_body/all-liquidity:
58 trades, 31.03% WR, +14.0R, -13.0R DD, PF 1.35

NQ best balanced:
3R midpoint/cisd_body/session-only:
15 trades, 40.00% WR, +9.0R, -4.0R DD, PF 2.00

NQ best WR with positive net R and >=8 trades:
2R start/sweep_wick/session-only:
16 trades, 43.75% WR, +5.0R, -5.0R DD, PF 1.56

SPX best WR with positive net R:
2R start/cisd_body/all-liquidity:
38 trades, 44.74% WR, +13.0R, -5.0R DD, PF 1.62

SPX best net R:
3R quarter_25/cisd_body/all-liquidity:
34 trades, 35.29% WR, +14.0R, -7.0R DD, PF 1.64

Silver:
No robust positive Silver configuration was found in this grid.
Only 1-trade session-only variants were positive, which is not useful.
Best all-liquidity WR with >=8 trades was 1R quarter_25/cisd_body:
12 trades, 25.00% WR, -6.0R, -5.0R DD, PF 0.33
```

Index-focused WR/RR tests generated on 2026-07-05:

```text
The first attempted full index grid was too large:
2 candidates x 3 RR x 3 entries x 2 stops x 3 liquidity modes x 3 time windows = 324 planned variants.
It exceeded the 30-minute command timeout.

The focused phase was reduced to:
2 candidates x 3 RR x 3 entries x 1 stop x 3 liquidity modes x 1 time window = 54 variants.

Scripts:
work/backtest/scripts/run_index_focus_grid_6month.py
work/backtest/scripts/run_index_time_window_followup_6month.py

Report folders:
work/backtest/outputs/reports/index_focus_grid_6month/
work/backtest/outputs/reports/index_time_window_followup_6month/

Selected months:
2022-12, 2023-06, 2023-09, 2023-10, 2025-02, 2025-03
```

Index focus phase-1 grid:

```text
Fixed:
stop_model = cisd_body
trade_window = 09:30-11:00

Tested:
NQ and SPX only
RR = 2R, 2.5R, 3R
entry_mode = start, quarter_25, midpoint
liquidity_mode = all, session_only, strong_swing
```

Index focus phase-1 highlights:

```text
NQ best WR with positive net R and >=8 trades:
2.5R midpoint/cisd_body/session-only/09:30-11:00:
13 trades, 46.15% WR, +8.0R, -3.0R DD, PF 2.14

NQ best net R:
3R start/cisd_body/strong-swing/09:30-11:00:
40 trades, 35.00% WR, +16.0R, -7.0R DD, PF 1.62

NQ liquidity-mode averages:
all:          39.56 trades avg, 32.61% WR, +5.56R avg, -8.72R DD avg
session_only:14.44 trades avg, 41.13% WR, +6.06R avg, -3.67R DD avg
strong_swing:36.56 trades avg, 35.30% WR, +8.56R avg, -7.17R DD avg

SPX best WR with positive net R and >=8 trades:
2R midpoint/cisd_body/all-liquidity/09:30-11:00:
23 trades, 52.17% WR, +13.0R, -2.0R DD, PF 2.18

SPX best net R:
2.5R start/cisd_body/all-liquidity/09:30-11:00:
28 trades, 46.43% WR, +17.5R, -4.0R DD, PF 2.17

SPX liquidity-mode averages:
all:          25.00 trades avg, 43.59% WR, +12.56R avg, -4.33R DD avg
session_only: 3.67 trades avg, 52.78% WR, +2.89R avg, -0.78R DD avg
strong_swing:24.00 trades avg, 41.23% WR, +10.06R avg, -4.67R DD avg
```

Index time-window follow-up:

```text
Tested the best NQ/SPX configs across:
09:30-10:30
09:30-11:00
10:00-11:30

NQ:
3R midpoint/cisd_body/session-only/09:30-10:30:
7 trades, 71.43% WR, +13.0R, -2.0R DD, PF 7.50

NQ more robust trade-count pick:
3R start/cisd_body/strong-swing/09:30-10:30:
32 trades, 40.62% WR, +20.0R, -6.0R DD, PF 2.05

SPX:
2.5R start/cisd_body/all-liquidity/09:30-11:00:
28 trades, 46.43% WR, +17.5R, -4.0R DD, PF 2.17

SPX WR pick:
2R midpoint/cisd_body/all-liquidity/09:30-11:00:
23 trades, 52.17% WR, +13.0R, -2.0R DD, PF 2.18

SPX high-R alternative:
3R quarter_25/cisd_body/all-liquidity/09:30-11:00:
24 trades, 41.67% WR, +16.0R, -5.0R DD, PF 2.14

NQ 10:00-11:30 performed poorly in follow-up and should not be preferred.
SPX 09:30-11:00 was the best balanced window.
```

Candidate list update on 2026-07-05:

```text
The shared candidate list in:
work/backtest/scripts/run_corrected_engine_3month_report.py

now includes the original 3 candidates plus 3 index-focused candidates selected from
the index focus/time-window tests.

These 3 index-focused candidates are marked conceptually as FUNDED ACCOUNT candidates:
- lower frequency
- better drawdown control
- to be developed for funded account stability

They are not meant to be the final phase-passing/high-frequency candidates.

Selected new candidates:

1. nq_strong_swing_cisd_body_3r_0930_1030
   NQ - 3m - strong swing - CISD body - 3R - 09:30-10:30
   Reason: better trade-count candidate than low-sample NQ session-only WR pick.
   Test result:
   32 trades, 40.62% WR, +20.0R, -6.0R DD, PF 2.05

2. spx_all_liquidity_cisd_body_2p5r_0930_1100
   SPX - 5m - all liquidity - CISD body - 2.5R - 09:30-11:00
   Reason: best SPX net-R balance.
   Test result:
   28 trades, 46.43% WR, +17.5R, -4.0R DD, PF 2.17

3. spx_all_liquidity_cisd_body_2r_midpoint_0930_1100
   SPX - 5m - all liquidity - CISD body - 2R midpoint - 09:30-11:00
   Reason: best SPX WR balance with enough trades.
   Test result:
   23 trades, 52.17% WR, +13.0R, -2.0R DD, PF 2.18

Not selected:
NQ 3R midpoint/cisd_body/session-only/09:30-10:30 had 71.43% WR and +13.0R,
but only 7 trades, so it remains a watchlist/validation candidate rather than a main candidate.
```

Phase candidate grid generated on 2026-07-05:

```text
Goal:
Find higher-frequency NQ/SPX candidates for phase passing, separate from the lower-frequency
funded-account candidates.

Script:
work/backtest/scripts/run_phase_candidate_grid_6month.py

Report folder:
work/backtest/outputs/reports/phase_candidate_grid_6month/

Selected months:
2022-12, 2023-06, 2023-09, 2023-10, 2025-02, 2025-03

Runs:
72

Tested:
- NQ and SPX only
- RR: 2R, 2.5R, 3R
- entry_mode: start, quarter_25
- stop_model: cisd_body
- liquidity_mode: all, strong_swing
- trade_window: 09:30-11:00, 09:30-12:00
- max_trades_per_day: NQ 1 and 2, SPX 2
```

Phase candidate grid highlights:

```text
NQ best phase-score:
3R start/cisd_body/strong-swing/09:30-11:00/max1:
40 trades, 6.67 trades/month, 35.00% WR, +16.0R, -7.0R DD, PF 1.62

NQ higher-frequency phase candidate:
3R start/cisd_body/strong-swing/09:30-12:00/max1:
54 trades, 9.00 trades/month, 33.33% WR, +18.0R, -12.0R DD, PF 1.50

NQ all-liquidity higher-frequency alternative:
3R start/cisd_body/all-liquidity/09:30-12:00/max1:
58 trades, 9.67 trades/month, 31.03% WR, +14.0R, -13.0R DD, PF 1.35

NQ max2/day warning:
Increasing NQ to max2/day increased trade count but hurt drawdown/quality.
Example:
3R start/cisd_body/strong-swing/09:30-12:00/max2:
59 trades, 9.83 trades/month, 30.51% WR, +13.0R, -16.0R DD, PF 1.32

SPX best phase-score:
2R start/cisd_body/all-liquidity/09:30-12:00/max2:
38 trades, 6.33 trades/month, 44.74% WR, +13.0R, -5.0R DD, PF 1.62

SPX best net-R phase:
2.5R start/cisd_body/all-liquidity/09:30-12:00/max2:
38 trades, 6.33 trades/month, 39.47% WR, +14.5R, -4.5R DD, PF 1.63

SPX did not reach 8-9 trades/month in this grid while staying positive.
```

Phase-candidate interpretation:

```text
NQ:
Use 3R start/cisd_body/strong-swing/09:30-12:00/max1 as the higher-frequency phase candidate
if monthly trade count is prioritized.
Use 3R start/cisd_body/strong-swing/09:30-11:00/max1 if drawdown control is prioritized.

SPX:
Use 2R start/cisd_body/all-liquidity/09:30-12:00/max2 for higher WR/stability.
Use 2.5R start/cisd_body/all-liquidity/09:30-12:00/max2 for better net-R.
```

Selected two-symbol phase pair on 2026-07-06:

```text
After deciding NQ and SPX will be traded together for phase passing, trade count was
de-prioritized versus WR/DD balance.

Selected NQ:
3R start/cisd_body/strong-swing/09:30-10:30/max1
32 trades, 40.62% WR, +20.0R, -6.0R DD, PF 2.05

Selected SPX:
2R midpoint/cisd_body/all-liquidity/09:30-11:00/max2
23 trades, 52.17% WR, +13.0R, -2.0R DD, PF 2.18

Combined selected-pair sample:
55 trades across 48 unique dates, +33.0R total.

Same-day overlap:
7 dates had both NQ and SPX trades.
Overlap dates:
2022-12-07, 2022-12-28, 2023-06-14, 2023-09-13, 2023-09-27, 2023-10-18, 2025-02-26

Overlap net R:
0.0R total across the 7 overlapping dates.

Overlap breakdown:
- Both lost: 4 dates
- Both won: 1 date
- NQ win / SPX loss: 1 date
- NQ loss / SPX win: 1 date

Generated files:
work/backtest/outputs/reports/selected_phase_pair_6month/nq_phase_trades.csv
work/backtest/outputs/reports/selected_phase_pair_6month/spx_phase_trades.csv
work/backtest/outputs/reports/selected_phase_pair_6month/combined_trades.csv
work/backtest/outputs/reports/selected_phase_pair_6month/overlap_dates.csv
```

Selected candidates full-data report generated on 2026-07-06:

```text
Goal:
Validate the 4 current candidates on all available Dukascopy data before moving toward forward test.

Script:
work/backtest/scripts/run_selected_candidates_full_data_report.py

Report folder:
work/backtest/outputs/reports/selected_candidates_full_data/

Generated files:
comparison.csv
monthly_detail.csv
all_trades.csv
pair_daily.csv
same_day_overlap.csv
report.md
```

Full-data candidate results:

```text
Funded NQ:
3R midpoint/cisd_body/session-only/09:30-10:30/max1
54 trades, 29.63% WR, +10.0R, -5.0R DD, PF 1.26

Funded SPX:
2.5R start/cisd_body/all-liquidity/09:30-11:00/max2
245 trades, 33.47% WR, +42.0R, -23.5R DD, PF 1.26

Phase NQ:
3R start/cisd_body/strong-swing/09:30-10:30/max1
296 trades, 27.03% WR, +24.0R, -27.0R DD, PF 1.11

Phase SPX:
2R midpoint/cisd_body/all-liquidity/09:30-11:00/max2
206 trades, 38.35% WR, +31.0R, -16.0R DD, PF 1.24
```

Full-data pair results:

```text
Funded pair:
290 unique trade dates, +52.0R total.
Worst day: -2.0R
Best day: +3.0R
Same-day overlap: 7 days, overlap net -2.0R, both lost 4 days, both won 0 days.

Phase pair:
443 unique trade dates, +55.0R total.
Worst day: -2.0R
Best day: +5.0R
Same-day overlap: 56 days, overlap net +20.0R, both lost 26 days, both won 8 days.
```

Important full-data interpretation:

```text
The 6-month selected candidates did not fully generalize to the complete 2022-2026 dataset.
They remain positive, but WR and drawdown weakened materially.

Main concerns before forward test:
- Funded SPX has high full-data drawdown (-23.5R) despite positive net.
- Phase NQ has weak full-data WR (27.03%) and large drawdown (-27.0R).
- Phase pair is positive, but both symbols lost on 26 same-day overlap dates.

Next recommended step:
Analyze monthly/yearly degradation and same-day overlap risk before any forward-test handoff.
Consider filters for bad months/regimes, same-day risk cap, and possibly year-specific stability checks.
```

Bad regime diagnostic report generated on 2026-07-06:

```text
Script:
work/backtest/scripts/run_bad_regime_diagnostics.py

Report folder:
work/backtest/outputs/reports/bad_regime_diagnostics/

Key diagnostic metrics added per trade:
- profile_range
- first30_range
- first30_directionality
- va_width
- profile_range_to_va_width
- entry_after_1030
- sweep_at_open
- liquidity_type
```

Diagnostic findings:

```text
SPX funded and SPX phase both degrade when first30_range is in the high quartile.

SPX funded:
first30_range_low:  62 trades, 40.32% WR, +25.5R
first30_range_mid:  122 trades, 32.79% WR, +18.0R
first30_range_high: 61 trades, 27.87% WR, -1.5R

SPX phase:
first30_range_low:  52 trades, 46.15% WR, +20.0R
first30_range_mid:  102 trades, 37.25% WR, +12.0R
first30_range_high: 52 trades, 32.69% WR, -1.0R

NQ phase also weakens on high first30_range and low first30_directionality:
first30_range_high: -2.0R
first30_directionality_low: -2.0R
first30_directionality_high: +22.0R
```

Regime filter tests generated on 2026-07-06:

```text
Script:
work/backtest/scripts/run_regime_filter_tests.py

Report folder:
work/backtest/outputs/reports/regime_filter_tests/

Filters tested with candidate-specific thresholds:
baseline
no_first30_range_high
no_first30_directionality_low
no_high_range_or_low_directionality
```

Filter thresholds:

```text
NQ funded:
first30_range_q75 = 161.45
first30_directionality_q25 = 0.25

SPX funded:
first30_range_q75 = 29.69
first30_directionality_q25 = 0.20

NQ phase:
first30_range_q75 = 162.00
first30_directionality_q25 = 0.21

SPX phase:
first30_range_q75 = 30.23
first30_directionality_q25 = 0.20
```

Filter test results:

```text
Funded pair baseline:
299 trades, 32.78% WR, +52.0R, -23.5R DD, PF 1.26, R/DD 2.21

Funded pair no_first30_range_high:
224 trades, 34.38% WR, +51.5R, -17.0R DD, PF 1.35, R/DD 3.03

Funded pair no_first30_directionality_low:
224 trades, 33.48% WR, +45.0R, -18.5R DD, PF 1.30, R/DD 2.43

Funded pair no_high_range_or_low_directionality:
160 trades, 36.25% WR, +47.5R, -15.5R DD, PF 1.47, R/DD 3.06

Phase pair baseline:
502 trades, 31.67% WR, +55.0R, -19.0R DD, PF 1.16, R/DD 2.89

Phase pair no_first30_range_high:
376 trades, 32.98% WR, +58.0R, -13.0R DD, PF 1.23, R/DD 4.46

Phase pair no_first30_directionality_low:
376 trades, 32.45% WR, +52.0R, -14.0R DD, PF 1.20, R/DD 3.71

Phase pair no_high_range_or_low_directionality:
265 trades, 33.96% WR, +49.0R, -10.0R DD, PF 1.28, R/DD 4.90
```

Filter interpretation:

```text
Best general-purpose filter:
no_first30_range_high.

It improves both funded and phase pairs while preserving more trade count and net R than
the combined range+directionality filter.

Funded:
Keeps almost the same net R (+52.0R -> +51.5R) while reducing DD (-23.5R -> -17.0R).

Phase:
Improves net R (+55.0R -> +58.0R) and reduces DD (-19.0R -> -13.0R).

The combined no_high_range_or_low_directionality filter reduces DD further but cuts too much
trade count and net R, so it should be treated as a conservative variant, not the first default.
```

Filter risk profile generated on 2026-07-06:

```text
Script:
work/backtest/scripts/analyze_filter_risk_profile.py

Report folder:
work/backtest/outputs/reports/filter_risk_profile/

Generated files:
equity_summary.csv
risk_percent_summary.csv
monthly_drawdown.csv
live_safety_summary.csv
report.md
```

DD interpretation:

```text
Baseline funded:
299 trades, +52.0R, -23.5R DD, 10 max consecutive losses, 10 max consecutive losing days

no_first30_range_high funded:
224 trades, +51.5R, -17.0R DD, 9 max consecutive losses, 9 max consecutive losing days

Baseline phase:
502 trades, +55.0R, -19.0R DD, 17 max consecutive losses, 14 max consecutive losing days

no_first30_range_high phase:
376 trades, +58.0R, -13.0R DD, 11 max consecutive losses, 10 max consecutive losing days

Conservative no_high_range_or_low_directionality phase:
265 trades, +49.0R, -10.0R DD, 9 max consecutive losses, 9 max consecutive losing days
```

Risk percentage translation for no_first30_range_high:

```text
Funded:
0.25% risk/trade -> +12.88%, -4.25% max DD
0.50% risk/trade -> +25.75%, -8.50% max DD
1.00% risk/trade -> +51.50%, -17.00% max DD

Phase:
0.25% risk/trade -> +14.50%, -3.25% max DD
0.50% risk/trade -> +29.00%, -6.50% max DD
1.00% risk/trade -> +58.00%, -13.00% max DD
```

Important live-safety caveat:

```text
The first30_range filter is known only after 10:00 NY.
Using it as a day-level pre-filter for trades entered before 10:00 would introduce lookahead bias.

Full-data selected trades entered before 10:00:
baseline funded: 17 trades, +17.0R
baseline phase: 93 trades, +15.0R
no_first30_range_high funded: 14 trades, +12.0R
no_first30_range_high phase: 70 trades, +14.0R

Therefore, the next implementation should either:
1. Apply first30_range filtering only to trades entered at/after 10:00, or
2. Replace it with a pre-09:30 proxy such as profile_range/VA width, or
3. Treat 09:30-10:00 trades as unfiltered and filter only later trades.
```

Live-safe filter tests generated on 2026-07-06:

```text
Script:
work/backtest/scripts/run_live_safe_filter_tests.py

Report folder:
work/backtest/outputs/reports/live_safe_filter_tests/

Filters tested:
baseline
live_safe_no_first30_range_high
no_profile_range_high
no_va_width_high
no_profile_or_va_high
```

Live-safe filter rule:

```text
Before 10:00 NY:
Do not apply first30_range filter.

At/after 10:00 NY:
Skip trades if first30_range is above the candidate-specific 75th percentile.
```

Live-safe results:

```text
Funded baseline:
299 trades, 32.78% WR, +52.0R, -23.5R DD, PF 1.26, R/DD 2.21

Funded live_safe_no_first30_range_high:
227 trades, 34.80% WR, +56.5R, -15.5R DD, PF 1.38, R/DD 3.65

Phase baseline:
502 trades, 31.67% WR, +55.0R, -19.0R DD, PF 1.16, R/DD 2.89

Phase live_safe_no_first30_range_high:
399 trades, 32.58% WR, +59.0R, -17.0R DD, PF 1.22, R/DD 3.47
```

Candidate-level live-safe results:

```text
NQ funded:
baseline 54 trades, 29.63% WR, +10.0R, -5.0R DD
live-safe 43 trades, 32.56% WR, +13.0R, -4.0R DD

SPX funded:
baseline 245 trades, 33.47% WR, +42.0R, -23.5R DD
live-safe 184 trades, 35.33% WR, +43.5R, -15.0R DD

NQ phase:
baseline 296 trades, 27.03% WR, +24.0R, -27.0R DD
live-safe 245 trades, 27.76% WR, +27.0R, -23.0R DD

SPX phase:
baseline 206 trades, 38.35% WR, +31.0R, -16.0R DD
live-safe 154 trades, 40.26% WR, +32.0R, -13.0R DD
```

Pre-market proxy filter interpretation:

```text
profile_range/VA width filters are live-safe before 09:30, but they cut too much funded net R
and do not beat live_safe_no_first30_range_high overall.

Best practical next filter:
live_safe_no_first30_range_high.
```

Motor implementation update on 2026-07-06:

```text
backtest/config.py:
- Added first30_range_filter.
- Added first30_range_max.

backtest/strategy.py:
- Computes 09:30-10:00 NY first30_range for each trade date.
- Implements first30_range_filter="live_safe_max".
- Filter is applied after entry fill is found:
  entries before 10:00 are never blocked by this filter;
  entries at/after 10:00 are blocked when first30_range > first30_range_max.

This avoids lookahead bias from using 09:30-10:00 range before 10:00.
```

Risk assumptions selected on 2026-07-06:

```text
Funded candidates:
risk_per_trade = 0.25%

Phase candidates:
risk_per_trade = 0.50%
```

Selected candidate full-data report rerun with motor-level live-safe filter on 2026-07-06:

```text
Script:
work/backtest/scripts/run_selected_candidates_full_data_report.py

Report folder:
work/backtest/outputs/reports/selected_candidates_full_data/
```

Current motor-level live-safe selected candidate results:

```text
NQ funded:
43 trades, 32.56% WR, +13.0R, -4.0R DD
Risk 0.25% -> +3.25%, -1.00% DD

SPX funded:
184 trades, 35.33% WR, +43.5R, -15.0R DD
Risk 0.25% -> +10.88%, -3.75% DD

Funded pair:
227 trades, 221 unique dates, +56.5R
Risk 0.25% -> +14.12%
Worst day: -2.0R = -0.50%
Same-day overlap: 5 days, overlap net -2.0R, both lost 3 days

NQ phase:
245 trades, 27.76% WR, +27.0R, -23.0R DD
Risk 0.50% -> +13.50%, -11.50% DD

SPX phase:
154 trades, 40.26% WR, +32.0R, -13.0R DD
Risk 0.50% -> +16.00%, -6.50% DD

Phase pair:
399 trades, 361 unique dates, +59.0R
Risk 0.50% -> +29.50%
Worst day: -2.0R = -1.00%
Same-day overlap: 36 days, overlap net +16.0R, both lost 18 days, both won 7 days
```

Selected candidate SL analysis generated on 2026-07-06:

```text
Script:
work/backtest/scripts/analyze_selected_candidate_losses.py

Report folder:
work/backtest/outputs/reports/selected_candidate_loss_analysis/

Generated files:
enriched_trades.csv
sl_trades.csv
loss_timing_summary.csv
loss_timing_buckets.csv
win_loss_factor_compare.csv
loss_reason_summary.csv
overlap_loss_summary.csv
report.md
```

Current selected candidate timeframes:

```text
NQ funded:
DUKASCOPY_USATECHIDXUSD 3m only

NQ phase:
DUKASCOPY_USATECHIDXUSD 3m only

SPX funded:
DUKASCOPY_USA500IDXUSD 5m only

SPX phase:
DUKASCOPY_USA500IDXUSD 5m only

There is currently no combined 3m+5m signal arbitration.
The engine supports either timeframe per run/candidate, but these selected candidates do not check both together.
```

SL timing summary:

```text
NQ funded:
29 SL trades
avg time to SL: 198.83 minutes
median time to SL: 18 minutes
avg candles to SL: 66.28 3m candles
median candles to SL: 6 3m candles
fast 0-2 candle SL: 7 trades, 24.14%

SPX funded:
119 SL trades
avg time to SL: 259.12 minutes
median time to SL: 35 minutes
avg candles to SL: 51.82 5m candles
median candles to SL: 7 5m candles
fast 0-2 candle SL: 31 trades, 26.05%

NQ phase:
177 SL trades
avg time to SL: 222.69 minutes
median time to SL: 21 minutes
avg candles to SL: 74.23 3m candles
median candles to SL: 7 3m candles
fast 0-2 candle SL: 44 trades, 24.86%

SPX phase:
92 SL trades
avg time to SL: 183.48 minutes
median time to SL: 20 minutes
avg candles to SL: 36.70 5m candles
median candles to SL: 4 5m candles
fast 0-2 candle SL: 34 trades, 36.96%
```

SL reason diagnostics:

```text
Main timing finding:
Median SL happens quickly, usually within 4-7 candles depending on candidate.
Mean SL time is much larger because some trades stay open for hours before stopping.

SPX phase has the clearest immediate-entry-quality problem:
36.96% of SL trades stop within 0-2 candles.

NQ phase has the clearest liquidity-source problem:
90.40% of SL trades are from swing liquidity.
Winning NQ phase trades also mostly use swing liquidity, but at a lower rate: 82.35%.
This suggests NQ phase needs stronger swing qualification, not complete swing removal.

SPX funded and SPX phase both have most SL trades after 10:00:
SPX funded: 97.48% of SLs after 10:00, 46.22% after 10:30.
SPX phase: 96.74% of SLs after 10:00, 48.91% after 10:30.
This suggests testing a stricter SPX entry cutoff or post-10:30 quality filter.

NQ funded losses have lower first30_directionality than wins:
wins avg 0.5095, losses avg 0.3578.

SPX phase losses have slightly higher first30_range and lower first30_directionality than wins:
wins first30_range 16.49, losses 17.87
wins directionality 0.4399, losses 0.4149
```

Potential next filters based on SL analysis:

```text
1. Test SPX phase/funded fast-stop reduction:
   - avoid entries after 10:30, or
   - require stronger post-CISD displacement before midpoint/start entry, or
   - delay/confirm entry for SPX when FVG fill happens within 0-2 candles.

2. Test NQ phase stronger swing filter:
   - require swing cluster count >= 3 instead of >= 2, or
   - require swing sweep to be near named session liquidity, or
   - require first30_directionality above threshold for NQ swing-liquidity trades.

3. Test same-day overlap risk:
   - phase pair has 36 overlap days and both lost on 18 of them, but overlap net is +16R.
   - This is not clearly bad overall, but same-day risk cap may still help DD.
```

Next-chat action plan added on 2026-07-06:

```text
User wants to continue these tests in a new chat.

Start next chat by reading:
1. work/backtest/PROJECT_STATUS.md
2. nasdaq-spx-gold-silver-backtest-project.md

Current active selected candidates:

Funded:
- NQ funded:
  3m, 3R, midpoint entry, cisd_body stop, session-only liquidity,
  09:30-10:30, max1/day, first30_range live-safe filter, risk 0.25%.

- SPX funded:
  5m, 2.5R, start entry, cisd_body stop, all liquidity,
  09:30-11:00, max2/day, first30_range live-safe filter, risk 0.25%.

Phase:
- NQ phase:
  3m, 3R, start entry, cisd_body stop, strong_swing liquidity,
  09:30-10:30, max1/day, first30_range live-safe filter, risk 0.50%.

- SPX phase:
  5m, 2R, midpoint entry, cisd_body stop, all liquidity,
  09:30-11:00, max2/day, first30_range live-safe filter, risk 0.50%.
```

Next-chat tests to run:

```text
1. SPX time cutoff test:
   Test SPX funded and SPX phase with stricter entry windows:
   - current baseline
   - 09:30-10:30
   - 09:30-10:45
   - no entries after 10:30 but keep existing setup logic
   Goal: reduce SPX fast SL and post-10:30 losses.

2. SPX fast-stop reduction test:
   Test additional entry confirmation for SPX when entry fill happens very quickly after FVG.
   Ideas:
   - skip fills on first 1 candle after FVG
   - skip fills within first 2 candles after FVG
   - require one close away from FVG before fill is valid
   Goal: reduce SPX phase 0-2 candle SL rate (currently 36.96%).

3. NQ stronger swing filter test:
   Current strong_swing means clustered/equal swing with count >= 2.
   Test:
   - strong_swing_min_touches = 2 baseline
   - strong_swing_min_touches = 3
   - strong_swing_min_touches = 4
   Goal: reduce NQ phase swing-liquidity SLs (currently 90.40% of SL trades).

4. NQ swing + first30 directionality test:
   For NQ phase, test minimum first30_directionality only on swing-liquidity trades.
   Use candidate-specific thresholds:
   - no directionality filter baseline
   - first30_directionality >= 0.21
   - first30_directionality >= 0.30
   - first30_directionality >= 0.40
   Goal: reduce low-quality NQ swing setups while preserving good directional open days.

5. Same-day overlap risk cap test:
   Especially for phase pair:
   - baseline
   - if first symbol loses, skip second symbol same day
   - max daily loss cap = -1R
   - max daily loss cap = -1.5R
   - max daily loss cap = -2R
   Goal: reduce DD from both-lost overlap days without destroying positive overlap expectancy.
```

Implementation notes for next chat:

```text
Existing useful scripts/reports:
- work/backtest/scripts/run_selected_candidates_full_data_report.py
- work/backtest/scripts/analyze_selected_candidate_losses.py
- work/backtest/scripts/run_live_safe_filter_tests.py
- work/backtest/outputs/reports/selected_candidates_full_data/
- work/backtest/outputs/reports/selected_candidate_loss_analysis/
- work/backtest/outputs/reports/live_safe_filter_tests/

Existing motor support:
- first30_range_filter="live_safe_max"
- first30_range_max
- swing_liquidity_mode="all" or "strong_only"
- trade_window_start / trade_window_end

Likely motor additions needed next:
- strong_swing_min_touches
- min_entry_delay_candles_after_fvg or equivalent SPX fast-fill filter
- optional same-day pair risk simulation script
```

Selected candidates period analysis generated on 2026-07-06:

```text
Goal:
Rank best/worst months and years for the 4 selected candidates and funded/phase pairs.

Script:
work/backtest/scripts/analyze_selected_candidates_periods.py

Report folder:
work/backtest/outputs/reports/selected_candidates_period_analysis/

Generated files:
candidate_monthly.csv
candidate_yearly.csv
pair_monthly.csv
pair_yearly.csv
rankings.csv
report.md
```

Pair yearly results:

```text
Funded pair:
2022: 73 trades, 35.62% WR, +21.0R, PF 1.45
2023: 65 trades, 35.38% WR, +17.0R, PF 1.40
2024: 57 trades, 31.58% WR, +7.0R, PF 1.18
2025: 59 trades, 30.51% WR, +5.0R, PF 1.12
2026: 45 trades, 28.89% WR, +2.0R, PF 1.06

Phase pair:
2022: 143 trades, 29.37% WR, +5.0R, PF 1.05
2023: 114 trades, 32.46% WR, +13.0R, PF 1.17
2024: 75 trades, 33.33% WR, +13.0R, PF 1.26
2025: 91 trades, 35.16% WR, +23.0R, PF 1.39
2026: 79 trades, 29.11% WR, +1.0R, PF 1.02
```

Candidate yearly results:

```text
NQ funded:
2022 +5.0R, 2023 0.0R, 2024 +1.0R, 2025 +1.0R, 2026 +3.0R

SPX funded:
2022 +16.0R, 2023 +17.0R, 2024 +6.0R, 2025 +4.0R, 2026 -1.0R

NQ phase:
2022 -6.0R, 2023 -8.0R, 2024 +16.0R, 2025 +24.0R, 2026 -2.0R

SPX phase:
2022 +11.0R, 2023 +21.0R, 2024 -3.0R, 2025 -1.0R, 2026 +3.0R
```

Pair best/worst months:

```text
Funded pair best months:
2025-03 +11.5R
2022-04 +9.5R
2023-06 +8.5R
2022-12 +7.0R
2022-06 +6.5R

Funded pair worst months:
2026-01 -5.5R
2022-01 -5.0R
2023-03 -5.0R
2025-12 -4.5R
2024-02 -4.0R

Phase pair best months:
2023-06 +12.0R
2022-04 +10.0R
2025-10 +9.0R
2024-11 +8.0R
2025-12 +8.0R

Phase pair worst months:
2022-07 -11.0R
2026-06 -8.0R
2022-01 -7.0R
2023-07 -5.0R
2024-08 -4.0R
```

Period analysis interpretation:

```text
Funded pair shows monotonic degradation from 2022 to 2026.
Most funded pair profit came from 2022-2023.
SPX funded is the main funded driver but also weakens into 2026.

Phase pair improves into 2024-2025 but weakens sharply in 2026.
NQ phase is negative in 2022, 2023, and 2026.
SPX phase is strong in 2022-2023 but negative in 2024-2025.

This means the current selected candidates are not stable enough for forward-test handoff yet.
Next work should focus on filtering bad regimes/months or selecting candidates by year-stability,
not just best total net R.
```

Next-chat action plan tests completed on 2026-07-06:

```text
Script:
work/backtest/scripts/run_next_chat_action_plan_tests.py

Report folder:
work/backtest/outputs/reports/next_chat_action_plan_tests/

Generated files:
report.md
01_spx_time_cutoff.csv
02_spx_fast_stop_reduction.csv
03_nq_stronger_swing_filter.csv
04_nq_swing_first30_directionality.csv
05_same_day_overlap_risk_cap.csv
plus per-test trade CSV files where applicable.

Motor additions:
- latest_entry_time
- min_entry_delay_candles_after_fvg
- require_close_away_before_entry
- strong_swing_min_touches
- swing_first30_directionality_min
```

SPX time cutoff test:

```text
SPX funded baseline:
184 trades, 35.33% WR, +43.5R, -15.0R DD, fast SL 26.05%, after-10:30 losses 55

SPX funded 09:30-10:30:
106 trades, 39.62% WR, +41.0R, -6.0R DD, fast SL 34.38%, after-10:30 losses 0

SPX funded 09:30-10:45:
145 trades, 37.24% WR, +44.0R, -7.5R DD, fast SL 31.87%, after-10:30 losses 27

SPX funded keep setup window but latest entry 10:30:
123 trades, 38.21% WR, +41.5R, -7.0R DD, fast SL 34.21%, after-10:30 losses 12

SPX phase baseline:
154 trades, 40.26% WR, +32.0R, -13.0R DD, fast SL 36.96%, after-10:30 losses 45

SPX phase 09:30-10:30:
87 trades, 45.98% WR, +33.0R, -9.0R DD, fast SL 46.81%, after-10:30 losses 0

SPX phase 09:30-10:45:
119 trades, 42.02% WR, +31.0R, -9.0R DD, fast SL 44.93%, after-10:30 losses 22

SPX phase keep setup window but latest entry 10:30:
100 trades, 45.00% WR, +35.0R, -10.0R DD, fast SL 43.64%, after-10:30 losses 8

Interpretation:
Time cutoff improves SPX drawdown materially while preserving most or all net R.
It reduces post-10:30 loss count, but does not reduce fast-SL percentage because the remaining loss set is smaller and earlier.
Best funded balance: 09:30-10:45 or latest-entry-10:30.
Best phase balance: latest-entry-10:30, with 09:30-10:30 as simpler lower-frequency alternative.
```

SPX fast-stop reduction test:

```text
SPX funded:
baseline: 184 trades, +43.5R, -15.0R DD, fast SL 26.05%
skip first 1 candle: 157 trades, +28.5R, -18.0R DD, fast SL 24.04%
skip first 2 candles: 134 trades, +30.5R, -15.0R DD, fast SL 21.84%
require one close away: 140 trades, +31.5R, -12.0R DD, fast SL 17.58%

SPX phase:
baseline: 154 trades, +32.0R, -13.0R DD, fast SL 36.96%
skip first 1 candle: 142 trades, +23.0R, -17.0R DD, fast SL 39.08%
skip first 2 candles: 115 trades, +23.0R, -10.0R DD, fast SL 31.88%
require one close away: 105 trades, +24.0R, -8.0R DD, fast SL 24.19%

Interpretation:
Close-away confirmation is the cleanest fast-SL reducer, especially for phase, but it cuts too much net R.
Do not adopt as default yet. If used, combine with SPX time cutoff in a follow-up grid.
```

NQ stronger swing filter test:

```text
NQ phase strong_swing_min_touches=2 baseline:
245 trades, 27.76% WR, +27.0R, -23.0R DD, swing-loss pct 90.40%

min_touches=3:
213 trades, 28.17% WR, +27.0R, -16.0R DD, swing-loss pct 85.62%

min_touches=4:
166 trades, 29.52% WR, +30.0R, -17.0R DD, swing-loss pct 80.34%

Interpretation:
Raising strong swing minimum touches is useful.
min_touches=3 gives same net R with materially lower DD and better trade count than 4.
min_touches=4 improves net R slightly but removes more frequency and has slightly worse DD than 3.
Best balance: min_touches=3.
```

NQ swing + first30 directionality test:

```text
NQ phase baseline:
245 trades, 27.76% WR, +27.0R, -23.0R DD, swing-loss pct 90.40%

swing first30_directionality >= 0.21:
186 trades, 28.49% WR, +26.0R, -14.0R DD, swing-loss pct 84.21%

swing first30_directionality >= 0.30:
166 trades, 27.71% WR, +18.0R, -16.0R DD, swing-loss pct 80.00%

swing first30_directionality >= 0.40:
149 trades, 28.86% WR, +23.0R, -17.0R DD, swing-loss pct 75.47%

Interpretation:
0.21 is the best balance: nearly preserves net R while reducing DD from -23R to -14R.
Higher thresholds overfilter and reduce expectancy.
```

Same-day overlap risk cap test:

```text
Funded pair baseline:
227 trades, 221 dates, 34.80% WR, +56.5R, -15.5R DD, worst day -2.0R

Funded if first symbol loses skip second:
223 trades, +56.5R, -15.5R DD

Funded max daily loss cap -1R:
222 trades, +57.5R, -15.5R DD, worst day -1.0R

Funded -1.5R / -2R caps:
No practical change versus baseline.

Phase pair baseline:
399 trades, 361 dates, 32.58% WR, +59.0R, -17.0R DD, worst day -2.0R

Phase if first symbol loses skip second:
376 trades, 33.24% WR, +67.0R, -16.0R DD

Phase max daily loss cap -1R:
375 trades, 33.33% WR, +68.0R, -16.0R DD, worst day -1.0R

Phase -1.5R / -2R caps:
No practical change versus baseline because daily losses are mostly one or two -1R events.

Interpretation:
Daily -1R cap is useful for phase and slightly useful for funded.
Skip-second-after-first-loss is also useful for phase, but daily -1R cap is cleaner and easier to enforce.
```

Current follow-up recommendation after these 5 tests:

```text
1. SPX: test combined time cutoff + close-away confirmation:
   - funded: 09:30-10:45 and latest-entry-10:30
   - phase: latest-entry-10:30 and 09:30-10:30
   - with/without require_close_away_before_entry

2. NQ phase: promote strong_swing_min_touches=3 into the next candidate test.

3. NQ phase: compare min_touches=3 against swing_first30_directionality_min=0.21,
   and test the combination min_touches=3 + directionality >= 0.21.

4. Pair risk: use max daily loss cap -1R in phase simulations.
```

Backtest runtime optimization completed on 2026-07-06:

```text
Files changed:
work/backtest/backtest/strategy.py

Problem:
The full-data next-chat action plan report took about 4 hours.

Main bottlenecks fixed:
1. Per-day profile/trade/execution/session filters used full-data boolean masks.
   Replaced with time-sorted searchsorted slices.

2. For every trade day, execution_frame was converted to a list from trade_start
   to the end of the entire dataset, even if no entry happened.
   Changed so execution rows are only consumed after a valid entry is found.

3. After entry, simulate_exit no longer builds a list of all future rows.
   It streams rows and stops as soon as SL/TP/data-end is reached.

Validation:
python -m compileall backtest scripts/run_next_chat_action_plan_tests.py passed.

Spot checks:
NQ phase strong_swing_min_touches=3 stayed:
213 trades, 28.17% WR, +27.0R, -16.0R DD

SPX baseline stayed:
SPX funded 184 trades, 35.33% WR, +43.5R, -15.0R DD
SPX phase 154 trades, 40.26% WR, +32.0R, -13.0R DD

Runtime after optimization:
NQ full-data single variant:
~14.5 minutes before first optimization
~8.0 minutes after searchsorted slicing
~46.7 seconds after streaming exit

SPX full-data baseline variant for both funded+phase:
~20 minutes before optimization
~68.8 seconds after optimization

Full next-chat action plan test suite:
~585.8 seconds / 9 minutes 46 seconds after optimization

Practical result:
The same 5-test full report is now about 24x faster than the previous ~4 hour run.
Next combined-filter grids should be run through the optimized motor.
```

Follow-up combination tests completed on 2026-07-06:

```text
Script:
work/backtest/scripts/run_followup_combination_tests.py

Report folder:
work/backtest/outputs/reports/followup_combination_tests/

Generated files:
report.md
candidate_comparison.csv
pair_comparison.csv
monthly_detail.csv
all_trades.csv
trades_*.csv per tested candidate variant

Runtime:
~349.5 seconds / 5 minutes 50 seconds with optimized motor.
```

Candidate-level follow-up results:

```text
SPX funded:
baseline:
184 trades, 35.33% WR, +43.5R, -15.0R DD, PF 1.37, R/DD 2.90

09:30-10:45:
145 trades, 37.24% WR, +44.0R, -7.5R DD, PF 1.48, R/DD 5.87

09:30-10:45 + close-away:
96 trades, 38.54% WR, +33.5R, -7.0R DD, PF 1.57, R/DD 4.79

latest entry 10:30:
123 trades, 38.21% WR, +41.5R, -7.0R DD, PF 1.55, R/DD 5.93

latest entry 10:30 + close-away:
76 trades, 40.79% WR, +32.5R, -5.0R DD, PF 1.72, R/DD 6.50

SPX funded interpretation:
latest_entry_10:30 is the best balance.
close-away improves WR/DD but cuts too much net R and frequency.
```

```text
SPX phase:
baseline:
154 trades, 40.26% WR, +32.0R, -13.0R DD, PF 1.35, R/DD 2.46

09:30-10:30:
87 trades, 45.98% WR, +33.0R, -9.0R DD, PF 1.70, R/DD 3.67

09:30-10:30 + close-away:
47 trades, 51.06% WR, +25.0R, -5.0R DD, PF 2.09, R/DD 5.00

latest entry 10:30:
100 trades, 45.00% WR, +35.0R, -10.0R DD, PF 1.64, R/DD 3.50

latest entry 10:30 + close-away:
56 trades, 51.79% WR, +31.0R, -5.0R DD, PF 2.15, R/DD 6.20

SPX phase interpretation:
latest_entry_10:30 has best net R.
latest_entry_10:30 + close-away has best quality/DD but much lower frequency.
```

```text
NQ phase:
baseline:
245 trades, 27.76% WR, +27.0R, -23.0R DD, PF 1.15, R/DD 1.17

strong_swing_min_touches=3:
213 trades, 28.17% WR, +27.0R, -16.0R DD, PF 1.18, R/DD 1.69

swing first30_directionality >= 0.21:
186 trades, 28.49% WR, +26.0R, -14.0R DD, PF 1.20, R/DD 1.86

strong_swing_min_touches=3 + swing first30_directionality >= 0.21:
163 trades, 29.45% WR, +29.0R, -10.0R DD, PF 1.25, R/DD 2.90

NQ phase interpretation:
The combined min_touches=3 + directionality>=0.21 filter is clearly best.
It improves net R and cuts DD from -23R to -10R.
```

Pair-level follow-up results:

```text
Funded best balance:
NQ funded baseline + SPX funded latest_entry_10:30
No cap:
166 trades, 36.75% WR, +54.5R, -7.5R DD, R/DD 7.27, worst day -2R

With daily -1R cap:
162 trades, 37.04% WR, +54.5R, -7.5R DD, R/DD 7.27, worst day -1R

Funded best net R:
NQ funded baseline + SPX funded 09:30-10:45
188 trades, 36.17% WR, +57.0R, -9.5R DD, R/DD 6.00

Funded interpretation:
For stability, prefer SPX funded latest_entry_10:30 with daily -1R cap.
For slightly higher net R, SPX funded 09:30-10:45 is acceptable but DD rises.
```

```text
Phase best net R:
NQ phase min_touches=3 + SPX phase latest_entry_10:30 + daily -1R cap
298 trades, 34.90% WR, +74.0R, -12.0R DD, R/DD 6.17, worst day -1R

Phase best balance / R-DD:
NQ phase min_touches=3 + directionality>=0.21 + SPX phase latest_entry_10:30
No cap:
263 trades, 35.36% WR, +64.0R, -9.0R DD, R/DD 7.11, worst day -2R

Same NQ combo + SPX latest_entry_10:30 + close-away + daily -1R cap:
212 trades, 35.85% WR, +64.0R, -9.0R DD, R/DD 7.11, worst day -1R

Phase interpretation:
For phase-passing aggression, best current row is:
NQ min_touches=3 + SPX latest_entry_10:30 + daily -1R cap.

For lower-DD stability, best current row is:
NQ min_touches=3 + directionality>=0.21 + SPX latest_entry_10:30,
optionally with SPX close-away and daily -1R cap to reduce worst-day risk.
```

Current candidate recommendation after follow-up combinations:

```text
Funded candidate update:
Keep NQ funded unchanged.
Change SPX funded from baseline 09:30-11:00 to latest_entry_time=10:30.
Use daily -1R cap at pair level if enforcing combined funded risk.

Phase aggressive candidate:
NQ phase strong_swing_min_touches=3.
SPX phase latest_entry_time=10:30.
Pair daily loss cap -1R.
Result: 298 trades, +74.0R, -12.0R DD.

Phase stability candidate:
NQ phase strong_swing_min_touches=3 + swing_first30_directionality_min=0.21.
SPX phase latest_entry_time=10:30.
Optional SPX close-away + daily -1R cap if lower frequency is acceptable.
Best stability result: 212-263 trades, +64.0R, -9.0R DD depending close-away/cap choice.
```

Final pre-forward-test candidate split on 2026-07-06:

```text
User asked to split the selected setups into two candidates before forward-test prep.

Candidate 1: funded_selected
Purpose: lower-DD funded-account style system

NQ funded leg:
- unchanged from selected candidate
- 3m, 3R, midpoint entry, cisd_body stop, session-only liquidity
- 09:30-10:30, max1/day, first30_range live-safe filter, risk 0.25%

SPX funded leg:
- updated from selected candidate
- 5m, 2.5R, start entry, cisd_body stop, all liquidity
- setup/search window unchanged, but latest_entry_time=10:30
- max2/day, first30_range live-safe filter, risk 0.25%

Pair risk:
- daily loss cap -1R

Candidate 2: phase_selected
Purpose: phase-passing style system with better net R

NQ phase leg:
- updated from selected candidate
- 3m, 3R, start entry, cisd_body stop, strong_swing liquidity
- strong_swing_min_touches=3
- 09:30-10:30, max1/day, first30_range live-safe filter, risk 0.50%

SPX phase leg:
- updated from selected candidate
- 5m, 2R, midpoint entry, cisd_body stop, all liquidity
- setup/search window unchanged, but latest_entry_time=10:30
- max2/day, first30_range live-safe filter, risk 0.50%

Pair risk:
- daily loss cap -1R
```

Final candidate period analysis generated on 2026-07-06:

```text
Script:
work/backtest/scripts/analyze_final_candidate_periods.py

Report folder:
work/backtest/outputs/reports/final_candidate_period_analysis/

Generated files:
report.md
all_trades_raw.csv
all_trades_after_daily_cap.csv
candidate_monthly.csv
candidate_yearly.csv
pair_monthly_after_daily_cap.csv
pair_yearly_after_daily_cap.csv
rankings.csv
drawdown_by_system.csv
```

System summary after daily -1R cap:

```text
funded_selected:
162 trades, 161 unique dates, +54.5R, -7.5R DD
worst day -1.0R, best day +3.0R
33 positive months, 17 negative months
4 positive years, 1 negative year

phase_selected:
298 trades, 290 unique dates, +74.0R, -12.0R DD
worst day -1.0R, best day +5.0R
34 positive months, 16 negative months
5 positive years, 0 negative years
```

Pair yearly after daily -1R cap:

```text
funded_selected:
2022: 38 trades, 42.11% WR, +20.5R, PF 1.93
2023: 44 trades, 38.64% WR, +17.0R, PF 1.63
2024: 36 trades, 36.11% WR, +10.5R, PF 1.46
2025: 28 trades, 35.71% WR, +8.0R, PF 1.44
2026: 16 trades, 25.00% WR, -1.5R, PF 0.88

Interpretation:
funded_selected degrades steadily over time and turns negative in 2026.
The weak leg is mainly SPX funded in 2026:
SPX funded 2026 = 13 trades, 23.08% WR, -2.5R, PF 0.75.

phase_selected:
2022: 83 trades, 31.33% WR, +11.0R, PF 1.19
2023: 88 trades, 34.09% WR, +17.0R, PF 1.29
2024: 51 trades, 39.22% WR, +20.0R, PF 1.65
2025: 47 trades, 36.17% WR, +17.0R, PF 1.57
2026: 29 trades, 37.93% WR, +9.0R, PF 1.50

Interpretation:
phase_selected is year-stable: no negative years.
It still has bad individual months, mostly driven by NQ phase.
```

Worst pair months after daily -1R cap:

```text
funded_selected worst months:
2022-07: 4 trades, 0.00% WR, -4.0R
2024-02: 4 trades, 0.00% WR, -4.0R
2023-07: 3 trades, 0.00% WR, -3.0R
2024-08: 3 trades, 0.00% WR, -3.0R
2026-05: 3 trades, 0.00% WR, -3.0R
2026-01: 6 trades, 16.67% WR, -2.5R

funded_selected negative months:
2022-01, 2022-07, 2023-03, 2023-04, 2023-07, 2023-09,
2024-02, 2024-06, 2024-08, 2024-09,
2025-04, 2025-06, 2025-07, 2025-12,
2026-01, 2026-03, 2026-05

phase_selected worst months:
2022-01: 8 trades, 0.00% WR, -8.0R
2022-07: 8 trades, 0.00% WR, -8.0R
2023-07: 8 trades, 12.50% WR, -4.0R
2023-05: 4 trades, 0.00% WR, -4.0R
2024-08: 4 trades, 0.00% WR, -4.0R
2026-06: 3 trades, 0.00% WR, -3.0R

phase_selected negative months:
2022-01, 2022-05, 2022-07,
2023-03, 2023-04, 2023-05, 2023-07, 2023-09,
2024-02, 2024-08,
2025-04, 2025-06, 2025-08, 2025-09,
2026-03, 2026-06
```

Candidate leg yearly notes:

```text
funded_selected:
NQ funded is small and mostly stable, but low frequency.
SPX funded is the main driver and the main 2026 weakness.

SPX funded yearly:
2022 +15.5R
2023 +14.0R
2024 +8.5R
2025 +6.0R
2026 -2.5R

phase_selected:
NQ phase is weak in early years:
2022 0.0R
2023 -4.0R
2024 +11.0R
2025 +18.0R
2026 +2.0R

SPX phase is strong in 2022-2024 and 2026 but weak in 2025:
2022 +9.0R
2023 +16.0R
2024 +8.0R
2025 -3.0R
2026 +5.0R
```

Interpretation before next forward-test prep test:

```text
funded_selected:
Good DD profile, but monotonic yearly degradation remains.
Forward-test caution: if 2026-like conditions continue, funded_selected may underperform.
The next filter/regime test should focus on SPX funded 2026 weakness.

phase_selected:
Better year stability and stronger total net R.
Main risk is concentrated bad months, especially 2022-01 and 2022-07 from NQ phase.
The daily -1R cap controls worst day, but not multi-day bad-month clusters.

Both systems:
Bad months overlap in July and August-like summer periods:
2022-07, 2023-07, 2024-08 appear repeatedly.
Potential second pre-forward-test idea: test month/season filter or volatility/regime filter,
but user will specify the second test.
```

Selected SPX RR tests completed on 2026-07-06:

```text
User asked to test whether SPX 2.5R / 3R is better than 2R for the two selected candidates.

Script:
work/backtest/scripts/run_selected_spx_rr_tests.py

Report folder:
work/backtest/outputs/reports/selected_spx_rr_tests/

Generated files:
report.md
candidate_comparison.csv
pair_comparison.csv
all_trades.csv

Test setup:
NQ legs fixed.
SPX legs tested with latest_entry_time=10:30 and RR values:
2R, 2.5R, 3R.
Pair rows tested both no cap and daily loss cap -1R.

Runtime:
~174.9 seconds / 2 minutes 55 seconds.
```

SPX leg-only results:

```text
SPX funded:
2R:   122 trades, 45.90% WR, +46.0R, -8.0R DD, PF 1.70, R/DD 5.75
2.5R: 123 trades, 38.21% WR, +41.5R, -7.0R DD, PF 1.55, R/DD 5.93
3R:   124 trades, 33.87% WR, +44.0R, -9.0R DD, PF 1.54, R/DD 4.89

SPX phase:
2R:   100 trades, 45.00% WR, +35.0R, -10.0R DD, PF 1.64, R/DD 3.50
2.5R: 102 trades, 41.18% WR, +45.0R, -9.0R DD, PF 1.75, R/DD 5.00
3R:   104 trades, 35.58% WR, +44.0R, -11.0R DD, PF 1.66, R/DD 4.00
```

Pair results with daily -1R cap:

```text
funded_selected, NQ fixed + SPX 2R:
161 trades, 42.86% WR, +59.0R, -7.0R DD, PF 1.64, R/DD 8.43, worst day -1R

funded_selected, NQ fixed + SPX 2.5R:
162 trades, 37.04% WR, +54.5R, -7.5R DD, PF 1.53, R/DD 7.27, worst day -1R

funded_selected, NQ fixed + SPX 3R:
163 trades, 33.74% WR, +57.0R, -7.0R DD, PF 1.53, R/DD 8.14, worst day -1R

phase_selected, NQ fixed + SPX 2R:
298 trades, 34.90% WR, +74.0R, -12.0R DD, PF 1.38, R/DD 6.17, worst day -1R

phase_selected, NQ fixed + SPX 2.5R:
299 trades, 33.78% WR, +84.5R, -11.5R DD, PF 1.43, R/DD 7.35, worst day -1R

phase_selected, NQ fixed + SPX 3R:
301 trades, 31.89% WR, +83.0R, -13.0R DD, PF 1.40, R/DD 6.38, worst day -1R
```

Interpretation:

```text
funded_selected:
SPX 2R is best overall.
It gives the highest pair net R (+59.0R), lowest/tied-lowest DD (-7.0R),
best PF (1.64), and best R/DD (8.43).
SPX 3R is close on net R/R-DD but lower WR and PF.
SPX 2.5R is not better for funded after this test.

phase_selected:
SPX 2.5R is best overall.
It improves pair net R from +74.0R to +84.5R,
slightly lowers DD from -12.0R to -11.5R,
and improves R/DD from 6.17 to 7.35.
SPX 3R is close in net R (+83.0R) but has worse DD (-13.0R) and lower WR.

Updated recommendation:
funded_selected should use SPX 2R, not 2.5R.
phase_selected should use SPX 2.5R, not 2R.
NQ stays 3R for both.
```

Frequency expansion tests completed on 2026-07-06:

```text
Script:
work/backtest/scripts/run_frequency_expansion_tests.py

Report folder:
work/backtest/outputs/reports/frequency_expansion_tests/

Generated files:
report.md
candidate_comparison.csv
extra_trades_only.csv
pair_impact.csv
all_trades.csv

Goal:
Increase monthly trade count without damaging the two selected candidates.

Tested expansions:
- NQ trade window 09:30-10:45
- NQ trade window 09:30-11:00
- NQ all weekdays
- NQ max2/day
- NQ all weekdays + 09:30-11:00
- SPX latest_entry 10:45
- SPX latest_entry 11:00
- SPX all weekdays
- SPX max3/day
- SPX all weekdays + latest_entry 11:00

Core baseline used:
funded_selected:
NQ 3R fixed, SPX 2R latest_entry=10:30, daily -1R cap

phase_selected:
NQ 3R strong_swing_min_touches=3, SPX 2.5R latest_entry=10:30, daily -1R cap

Runtime:
~564.7 seconds / 9 minutes 25 seconds.
```

Extra-trades-only highlights:

```text
Positive extra trade groups:

phase NQ all weekdays:
55 extra trades, about +1.15 trades/month, +9.0R
But pair DD worsened materially, so not preferred.

phase SPX latest_entry=10:45:
35 extra trades, about +0.73 trades/month, +7.0R
Cleanest phase frequency increase.

funded SPX latest_entry=10:45:
43 extra trades, about +0.86 trades/month, +5.0R
Cleanest funded frequency increase.

phase SPX latest_entry=11:00:
58 extra trades, about +1.14 trades/month, +1.5R
Too weak versus 10:45.

Negative or poor quality extra trade groups:

funded SPX latest_entry=11:00:
64 extra trades, -1.0R

funded SPX all weekdays:
81 extra trades, -9.0R

funded SPX all weekdays + latest_entry=11:00:
183 extra trades, -12.0R

phase SPX all weekdays:
70 extra trades, -10.5R

phase SPX all weekdays + latest_entry=11:00:
160 extra trades, -6.0R

NQ window extensions generally did not help:
funded NQ 10:45 extra -4.0R
funded NQ 11:00 extra -2.0R
phase NQ 10:45 extra -11.0R
phase NQ 11:00 extra -3.0R
```

Pair impact with daily -1R cap:

```text
funded_selected baseline:
161 trades, about 3.22 trades/month, +59.0R, -7.0R DD

funded_selected with SPX latest_entry=10:45:
201 trades, about 3.87 trades/month, +67.0R, -8.0R DD
Interpretation: best funded frequency expansion.
Adds frequency and net R, DD worsens only mildly.

funded_selected with SPX latest_entry=11:00:
221 trades, about 4.09 trades/month, +62.0R, -11.0R DD
Interpretation: too much DD for too little extra edge.

funded_selected with SPX all weekdays:
240 trades, about 4.53 trades/month, +52.0R, -10.0R DD
Interpretation: rejected; more trades but worse system.

phase_selected baseline:
299 trades, about 5.64 trades/month, +84.5R, -11.5R DD

phase_selected with SPX latest_entry=10:45:
329 trades, about 6.21 trades/month, +93.0R, -13.5R DD
Interpretation: best phase frequency expansion.
Adds net R and moderate frequency, but DD increases by 2R.

phase_selected with SPX latest_entry=11:00:
348 trades, about 6.44 trades/month, +88.0R, -13.5R DD
Interpretation: worse than 10:45.

phase_selected with SPX all weekdays + latest_entry=11:00:
428 trades, about 7.93 trades/month, +95.0R, -15.5R DD
Interpretation: trade count improves most, but extra trades are negative and DD worsens.
Not preferred unless frequency is valued over quality.

phase_selected with NQ all weekdays:
346 trades, about 6.53 trades/month, +87.5R, -19.0R DD
Interpretation: rejected because DD worsens too much.
```

Frequency expansion interpretation:

```text
Cleanest way to add trades:
SPX latest_entry_time from 10:30 to 10:45.

Do not expand SPX to 11:00 yet.
Do not add all weekdays yet.
Do not expand NQ window yet.
Do not raise max trades/day; it added almost no trades.

Updated optional frequency variants:

funded_selected_frequency:
NQ unchanged.
SPX RR 2R, latest_entry_time=10:45.
Daily cap -1R.
Result: 201 trades, ~3.87/month, +67.0R, -8.0R DD.

phase_selected_frequency:
NQ unchanged from phase_selected.
SPX RR 2.5R, latest_entry_time=10:45.
Daily cap -1R.
Result: 329 trades, ~6.21/month, +93.0R, -13.5R DD.

Trade count target status:
These expansions improve count but still do not reach 10-12 trades/month.
To reach 10-12/month without trashing quality, next likely path is not more loosening;
it is adding a separate independent signal source, probably 3m+5m combined arbitration
or a third symbol after stronger validation.
```

Final active candidate set updated on 2026-07-06:

```text
User approved adding the two frequency variants.
Older candidates/variants remain historical and should only be revived if explicitly needed.

Script:
work/backtest/scripts/run_final_candidate_set_report.py

Report folder:
work/backtest/outputs/reports/final_candidate_set/

Generated files:
report.md
final_candidates.csv
```

Active candidates:

```text
1. funded_selected
Core funded candidate.
NQ: 3R, unchanged funded NQ rules.
SPX: 2R, latest_entry_time=10:30.
Pair risk: daily -1R cap.
Result: 161 trades, ~3.22/month, 42.86% WR, +59.0R, -7.0R DD, PF 1.64, R/DD 8.43.

2. phase_selected
Core phase candidate.
NQ: 3R, strong_swing_min_touches=3.
SPX: 2.5R, latest_entry_time=10:30.
Pair risk: daily -1R cap.
Result: 299 trades, ~5.64/month, 33.78% WR, +84.5R, -11.5R DD, PF 1.43, R/DD 7.35.

3. funded_selected_frequency
Optional funded frequency candidate.
NQ: same as funded_selected.
SPX: 2R, latest_entry_time=10:45.
Pair risk: daily -1R cap.
Result: 201 trades, ~3.87/month, 42.29% WR, +67.0R, -8.0R DD, PF 1.58, R/DD 8.38.

4. phase_selected_frequency
Optional phase frequency candidate.
NQ: same as phase_selected.
SPX: 2.5R, latest_entry_time=10:45.
Pair risk: daily -1R cap.
Result: 329 trades, ~6.21/month, 34.04% WR, +93.0R, -13.5R DD, PF 1.43, R/DD 6.89.
```

Main candidate filter tests generated on 2026-07-07:

```text
Script:
work/backtest/scripts/run_main_candidate_filter_tests.py

Report folder:
work/backtest/outputs/reports/main_candidate_filter_tests/

Files:
report.md
pair_comparison.csv
leg_comparison.csv
monthly_detail.csv
first30_thresholds.csv

Scope:
Active NQ/SPX systems.
One leg is changed at a time, the other leg remains baseline, then pair daily -1R cap is applied.

Systems tested:
- funded_selected
- phase_selected
- funded_selected_frequency
- phase_selected_frequency

Filters tested:
- direction long only
- direction short only
- tighter live-safe first30 range q60/q70
- remove one allowed weekday from one leg

First30 thresholds:
DUKASCOPY_USATECHIDXUSD 3m:
q60=120.94, q70=139.86

DUKASCOPY_USA500IDXUSD 5m:
q60=22.51, q70=26.39

Runtime:
~1095.4 seconds / 18 minutes 15 seconds.
The script now has a _leg_cache folder so reruns can reuse leg-level results.

Clean improvements:

funded_selected baseline:
161 trades, 3.22/month, 42.86% WR, +59R, -7R DD, PF 1.64, R/DD 8.43

funded_selected best clean filter:
NQ first30_q70:
154 trades, 3.08/month, 44.16% WR, +62R, -7R DD, PF 1.72, R/DD 8.86
Interpretation: small but clean improvement; loses 7 trades, gains +3R, same DD.

funded_selected_frequency baseline:
201 trades, 3.87/month, 42.29% WR, +67R, -8R DD, PF 1.58, R/DD 8.38

funded_selected_frequency best clean filters:
NQ first30_q60:
185 trades, 3.63/month, 44.32% WR, +71R, -8R DD, PF 1.69, R/DD 8.88

NQ first30_q70:
194 trades, 3.73/month, 43.30% WR, +70R, -8R DD, PF 1.64, R/DD 8.75

Interpretation:
Both improve funded frequency.
q70 is less aggressive and keeps more trades; q60 has slightly better edge but loses more frequency.

phase_selected baseline:
299 trades, 5.64/month, 33.78% WR, +84.5R, -11.5R DD, PF 1.43, R/DD 7.35

phase_selected best clean filters:
NQ first30_q60:
267 trades, 5.04/month, 35.21% WR, +88.5R, -10R DD, PF 1.51, R/DD 8.85

NQ first30_q70:
283 trades, 5.34/month, 34.28% WR, +85R, -10R DD, PF 1.46, R/DD 8.50

Interpretation:
This is a meaningful quality improvement.
q60 gives better edge and DD; q70 keeps more trades.

phase_selected_frequency baseline in this report:
328 trades, 6.19/month, 33.84% WR, +90.5R, -13.5R DD, PF 1.42, R/DD 6.70

Note:
This report was generated before the shared pair-cap helper was finalized.
Use main_candidate_first30_validation and rebuilt final_candidate_set values for final decisions.

phase_selected_frequency best clean filters:
NQ first30_q60:
298 trades, 5.62/month, 35.23% WR, +96R, -10.5R DD, PF 1.50, R/DD 9.14

NQ first30_q70:
314 trades, 5.92/month, 34.39% WR, +92.5R, -10R DD, PF 1.45, R/DD 9.25

NQ exclude Thursday:
287 trades, 5.42/month, 35.19% WR, +91R, -12.5R DD, PF 1.49, R/DD 7.28

Interpretation:
NQ first30_q60/q70 are clearly better than the current phase frequency profile.
Exclude Thursday helps but is weaker than first30 filters and is more likely to be overfit.

Rejected / weaker filter families:
- SPX filters generally reduced trade count without enough edge improvement.
- short-only was weak.
- weekday removals sometimes improved DD but usually cost too much net R or are more overfit-prone.

Current recommendation:
Do not edit the final active candidate set yet.
Next validation has been completed in main_candidate_first30_validation using the shared pair-cap helper.
```

Main candidate first30 validation generated on 2026-07-07:

```text
Script:
work/backtest/scripts/run_main_candidate_first30_validation.py

Report folder:
work/backtest/outputs/reports/main_candidate_first30_validation/

Files:
report.md
summary.csv
summary_enriched.csv
deltas_vs_baseline.csv
monthly.csv
yearly.csv
drawdown.csv
worst_months.csv
all_trades_after_normalized_cap.csv

Scope:
Validates only NQ first30_q60/q70 across the 4 active NQ/SPX systems.
SPX leg stays baseline.
Pair cap uses the shared deterministic helper in:
work/backtest/backtest/risk.py

Sort order:
group, date, entry_time, symbol if present else label, source order.
Stop after daily cumulative R <= -1R.

Runtime:
~12 seconds because leg-level cache from main_candidate_filter_tests was reused.

Pair-cap helper standardization completed:
- Added work/backtest/backtest/risk.py
- run_next_chat_action_plan_tests.py now wraps the shared helper
- run_selected_spx_rr_tests.py uses the shared helper
- run_frequency_expansion_tests.py uses the shared helper
- run_followup_combination_tests.py uses the shared helper
- analyze_final_candidate_periods.py uses the shared helper
- run_main_candidate_filter_tests.py uses the shared helper
- run_main_candidate_first30_validation.py uses the shared helper

After rebuilding selected_spx_rr_tests, frequency_expansion_tests, and final_candidate_set,
the validation baselines align with final_candidate_set again.

Validation summary:

funded_selected:
baseline:
161 trades, 3.22/month, +59R, -7R DD, PF 1.64, R/DD 8.43

NQ first30_q60:
145 trades, 2.96/month, +63R, -8R DD, PF 1.80, R/DD 7.88

NQ first30_q70:
154 trades, 3.08/month, +62R, -7R DD, PF 1.72, R/DD 8.86

Decision:
q70 is preferred for funded core.
It keeps DD unchanged and improves PF/R-DD while cutting only 7 trades.
q60 gives more net/PF but worsens DD by 1R and cuts 16 trades.

funded_selected_frequency:
baseline:
201 trades, 3.87/month, +67R, -8R DD, PF 1.58, R/DD 8.38

NQ first30_q60:
185 trades, 3.63/month, +71R, -8R DD, PF 1.69, R/DD 8.88

NQ first30_q70:
194 trades, 3.73/month, +70R, -8R DD, PF 1.64, R/DD 8.75

Decision:
q70 is the balanced funded-frequency pick.
q60 is stronger but cuts more frequency.

phase_selected:
baseline:
299 trades, 5.64/month, +84.5R, -11.5R DD, PF 1.43, R/DD 7.35

NQ first30_q60:
267 trades, 5.04/month, +88.5R, -10R DD, PF 1.51, R/DD 8.85

NQ first30_q70:
284 trades, 5.36/month, +87.5R, -10R DD, PF 1.47, R/DD 8.75

Decision:
q60 is preferred for phase core if quality is priority.
q70 is acceptable if trade count is more important.

phase_selected_frequency:
baseline:
329 trades, 6.21/month, +93R, -13.5R DD, PF 1.43, R/DD 6.89

NQ first30_q60:
298 trades, 5.62/month, +96R, -10.5R DD, PF 1.50, R/DD 9.14

NQ first30_q70:
315 trades, 5.94/month, +95R, -10R DD, PF 1.46, R/DD 9.50

Decision:
q70 has the best DD and R/DD.
q60 has better net/PF but worse DD and fewer trades.
For phase frequency, q70 is probably the better balanced choice.

Yearly validation:
All systems and q60/q70 variants stay positive in every tested year.
Weakest year remains 2024 for funded systems and 2022 for phase systems.

Worst month behavior:
first30 filters reduce some worst months but do not eliminate the 2022-01 / 2022-07 losing clusters.
For phase systems, q60 improves worst month from -8R to -7R; q70 keeps worst month around -8R.

Current recommendation:
Pair-cap helper is now standardized. Final candidates can now be rebuilt with these likely choices:
- funded_selected: NQ first30_q70
- funded_selected_frequency: NQ first30_q70
- phase_selected: NQ first30_q60 if quality priority, q70 if trade count priority
- phase_selected_frequency: NQ first30_q70 as balanced default
```

Final active candidate set rebuilt on 2026-07-07:

```text
User approved moving to the next step after pair-cap standardization.
The final candidate set now uses the selected NQ first30 filters.
Older baseline candidates remain historical and can be revived later if needed.

Script:
work/backtest/scripts/run_final_candidate_set_report.py

Report folder:
work/backtest/outputs/reports/final_candidate_set/

Generated files:
report.md
final_candidates.csv
baseline_comparison.csv

Active candidates:

1. funded_selected
NQ: 3R first30_q70.
SPX: 2R, latest_entry_time=10:30.
Pair risk: daily -1R cap.
Result: 154 trades, 3.08/month, 44.16% WR, +62.0R, -7.0R DD, PF 1.72, R/DD 8.86.
Versus previous baseline: -7 trades, +3.0R, same DD, +0.08 PF.

2. phase_selected
NQ: 3R first30_q60.
SPX: 2.5R, latest_entry_time=10:30.
Pair risk: daily -1R cap.
Result: 267 trades, 5.04/month, 35.21% WR, +88.5R, -10.0R DD, PF 1.51, R/DD 8.85.
Versus previous baseline: -32 trades, +4.0R, DD improves by 1.5R, +0.08 PF.

3. funded_selected_frequency
NQ: 3R first30_q70.
SPX: 2R, latest_entry_time=10:45.
Pair risk: daily -1R cap.
Result: 194 trades, 3.73/month, 43.30% WR, +70.0R, -8.0R DD, PF 1.64, R/DD 8.75.
Versus previous baseline: -7 trades, +3.0R, same DD, +0.06 PF.

4. phase_selected_frequency
NQ: 3R first30_q70.
SPX: 2.5R, latest_entry_time=10:45.
Pair risk: daily -1R cap.
Result: 315 trades, 5.94/month, 34.60% WR, +95.0R, -10.0R DD, PF 1.46, R/DD 9.50.
Versus previous baseline: -14 trades, +2.0R, DD improves by 3.5R, +0.03 PF.

Decision:
These four are now the active final candidates before the next validation step.
The main trade-off is intentional: slightly fewer trades in exchange for better PF/R-DD and cleaner drawdown.
```

Final candidate pre-forward temporal validation generated on 2026-07-07:

```text
Script:
work/backtest/scripts/run_final_candidate_pre_forward_validation.py

Report folder:
work/backtest/outputs/reports/final_candidate_pre_forward_validation/

Generated files:
report.md
summary.csv
yearly.csv
half_year.csv
period_splits.csv
rolling_windows.csv
stress_summary.csv
forward_readiness.csv
all_final_trades_after_daily_cap.csv

Scope:
Frozen final NQ/SPX combined candidates after daily -1R cap.
This is temporal robustness validation, not true out-of-sample validation, because first30 thresholds
were selected from historical data.

Forward readiness result:
All four active final candidates pass the rule-based temporal checks:
- no negative full years
- recent 12 calendar months positive
- worst rolling 6-month net R above -8R
- overall PF >= 1.45
- recent 12-month PF >= 1.15

Readiness table:
funded_selected:
pass, +62.0R overall, PF 1.72, recent 12m +7.0R, recent 12m PF 1.33, worst 6m -3.0R, worst month -4.0R.

funded_selected_frequency:
pass, +70.0R overall, PF 1.64, recent 12m +8.0R, recent 12m PF 1.31, worst 6m -6.0R, worst month -4.0R.

phase_selected:
pass, +88.5R overall, PF 1.51, recent 12m +27.0R, recent 12m PF 1.77, worst 6m -7.5R, worst month -7.0R.

phase_selected_frequency:
pass, +95.0R overall, PF 1.46, recent 12m +32.5R, recent 12m PF 1.81, worst 6m -5.0R, worst month -8.0R.

Important caution:
The funded systems pass, but 2024 and late-2025 were weak:
- funded_selected 2024: +7.0R, PF 1.35, DD -7.0R.
- funded_selected 2025-H2: 0.0R, PF 1.00.
- funded_selected_frequency 2024: +6.0R, PF 1.21, DD -7.0R.
- funded_selected_frequency 2025-H2: -1.0R, PF 0.94.
- funded_selected_frequency has a worst rolling 12-month window of -2.0R.

Interpretation:
The final set is acceptable for the next pre-forward step, but not a blind production handoff.
Phase systems are temporally cleaner in the recent period.
Funded systems need explicit forward monitoring rules for low-PF/flat recent behavior.
Next recommended validation before Pine/live handoff:
run a manual chart audit sample from the final candidate trades, prioritizing 2024 and late-2025 funded trades
plus the worst phase months.
```

Final candidate fund-account lifecycle simulation generated on 2026-07-07:

```text
Script:
work/backtest/scripts/run_final_candidate_fund_account_lifecycle.py

Report folder:
work/backtest/outputs/reports/final_candidate_fund_lifecycle/

Generated files:
report.md
account_fit.csv
summary.csv
failure_breakdown.csv
initial_start.csv
funded_failures.csv
lifecycle_runs.csv
stage_details.csv

Scope:
Rolling-start lifecycle simulation for the 4 final combined NQ/SPX candidates, using trade lists
after the existing daily -1R pair cap.

Account models:
A1 5k 7%+5% DD10:
- phase 1 target 7%
- phase 2 target 5%
- static 10% max DD
- 1% risk per R

A2 5k 6%+5% DD8:
- phase 1 target 6%
- phase 2 target 5%
- static 8% max DD
- 1% risk per R

A3 25k 6% trail4:
- one phase target 6%
- trailing 4% max DD
- 0.5% risk per R

After the last challenge phase passes, the simulation enters funded stage with no profit target
and continues until the same DD rule fails or data ends.
Late rolling starts that do not have enough remaining data to reach funded are marked incomplete_before_funded,
not counted as hard failures.

Account fit:
funded_selected:
- A1: clean. 74.03% reached funded, 0 funded failures, no phase failures.
- A2: clean. 75.32% reached funded, 0 funded failures, no phase failures.
- A3: clean. 74.03% reached funded, 0 funded failures, no phase failures.

funded_selected_frequency:
- A1: clean. 73.71% reached funded, 0 funded failures, no phase failures.
- A2: usable_watch. 74.23% reached funded, 0.69% funded failure of reached-funded starts,
  1 phase-1 failure and 2 phase-2 failures.
- A3: not_preferred. 72.68% reached funded, but 100% of reached-funded starts later fail in funded.
  Also has 9 phase-1 failures.

phase_selected:
- A1: usable_watch. 90.26% reached funded, 2.49% funded failure of reached-funded starts,
  1 phase-1 failure and 7 phase-2 failures.
- A2: aggressive_watch. 85.39% reached funded, 5.70% funded failure of reached-funded starts,
  10 phase-1 failures and 11 phase-2 failures.
- A3: not_preferred. 69.29% reached funded, 82.70% funded failure of reached-funded starts,
  64 phase-1 failures.

phase_selected_frequency:
- A1: aggressive_watch. 88.57% reached funded, 10.04% funded failure of reached-funded starts,
  2 phase-1 failures and 13 phase-2 failures.
- A2: aggressive_watch. 78.41% reached funded, 24.70% funded failure of reached-funded starts,
  18 phase-1 failures and 29 phase-2 failures.
- A3: not_preferred. 64.76% reached funded, 43.14% funded failure of reached-funded starts,
  90 phase-1 failures.

Initial-start examples:
- funded_selected passes A1/A2/A3 and remains funded alive to data end.
- funded_selected_frequency passes A1/A2 and remains funded alive to data end; A3 reaches funded but later fails trailing DD.
- phase_selected passes A1/A2 and remains funded alive to data end; A3 reaches funded but later fails trailing DD.
- phase_selected_frequency passes A1 and remains funded alive to data end; A2 fails phase 1 on the first start;
  A3 also fails phase 1 on the first start.

Interpretation:
Best fit before forward-test prep:
1. funded_selected for A1/A2/A3.
2. funded_selected_frequency for A1, and cautiously for A2.
3. phase_selected for A1, cautiously for A2.

Avoid or heavily de-risk:
- A3 trailing 4% with funded_selected_frequency, phase_selected, and phase_selected_frequency.
- phase_selected_frequency on A2 unless accepting a high funded-stage failure rate.

Next recommended validation:
Manual chart audit on final candidate trades before live handoff, especially:
- funded_selected_frequency A2 phase/funded failure windows
- phase_selected A1/A2 phase-2 and funded failure windows
- phase_selected_frequency A1/A2 funded failure windows
- all A3 trailing failures if A3 is still considered.
```

Fund account playbook and simplified candidate names saved on 2026-07-07:

```text
Script:
work/backtest/scripts/write_fund_account_playbook.py

Report folder:
work/backtest/outputs/reports/fund_account_playbook/

Generated files:
report.md
candidate_name_map.csv
account_recommendations.csv
combo_decisions.csv

New operating names:
CHALLENGE_CORE:
old name phase_selected.
Use for phase/challenge passing. This is the primary challenge system.

CHALLENGE_FAST:
old name phase_selected_frequency.
Use only as higher-frequency challenge alternative. Not the default because phase failures increase.

FON_CORE:
old name funded_selected.
Use after funded status is reached. This is the primary funded-account system.

FON_FAST:
old name funded_selected_frequency.
Use after funded status is reached only when higher funded frequency is wanted.
Avoid on A3 trailing 4%.

Recommended operating plans by account:

A1 5k 7%+5% DD10:
Primary: CHALLENGE_CORE -> FON_CORE.
Optional: CHALLENGE_CORE -> FON_FAST.
Reason: both plans reached funded 90.26% of rolling starts with 0% funded-stage failure.

A2 5k 6%+5% DD8:
Primary: CHALLENGE_CORE -> FON_CORE.
Optional: CHALLENGE_CORE -> FON_FAST, but monitor more tightly because A2 has tighter DD.
Reason: both plans reached funded 85.39% of rolling starts with 0% funded-stage failure.

A3 25k 6% trail4:
No clean phase-to-funded plan yet.
Do not prioritize A3 until a dedicated trailing-DD challenge plan is validated.
FON_CORE is acceptable only if the account is already funded and used with caution.
Avoid FON_FAST on A3.

Default operational rule:
Use CHALLENGE_* systems only for phase/challenge passing.
After the final challenge phase passes, switch to FON_* systems.
Default A1/A2 path is CHALLENGE_CORE -> FON_CORE.
```

Manual audit package generated on 2026-07-07:

```text
Script:
work/backtest/scripts/build_manual_audit_package.py

Report folder:
work/backtest/outputs/reports/manual_audit_package/

Generated files:
report.md
manual_audit_trades.csv

Purpose:
Final manual chart review before forward-test handoff.

Primary operating plan under review:
Challenge: CHALLENGE_CORE
Funded: FON_CORE

Sample size:
37 trades total.

Buckets:
- challenge_failure_window / CHALLENGE_CORE: 6 trades.
- fon_core_weak_2024 / FON_CORE: 12 trades.
- fon_core_flat_2025_h2 / FON_CORE: 10 trades.
- fon_core_worst_month_context / FON_CORE: 9 trades.

CSV review fields intentionally left blank:
- review_status: PASS / FAIL / WATCH
- manual_decision: TAKE / SKIP
- issue_type: LEVEL / CISD / FVG / ENTRY / EXIT / CONTEXT / OTHER
- chart_notes

User should manually review these in TradingView before live/forward handoff.
```

Manual feedback v2 engine first-pass validation generated on 2026-07-08:

```text
Context:
User manually reviewed 10 final audit examples and clarified that pre-market liquidity matters only
when the liquidity is taken close to the 09:30 NY open, e.g. 09:15-09:25.
An 08:30 London high/low take should not automatically remove the need for a later NY sweep.

Code changes:
backtest/config.py:
- Added opening_premarket_sweep_mode, default off.
- Added opening_premarket_sweep_start, default 09:15.
- Added require_swing_near_value_area, default false.
- Added cancel_pending_on_opposite_cisd, default false.

backtest/strategy.py:
- Added opening premarket sweep context when opening_premarket_sweep_mode="recent_as_setup".
- Only recent premarket takes from opening_premarket_sweep_start to 09:30 can seed a no-fresh-sweep setup.
- Earlier premarket takes, such as 08:30, no longer automatically count as the same kind of opening sweep in v2 mode.
- Added optional swing liquidity gate requiring swing levels to be near the matching value-area side.
- Added optional pending entry cancellation when CISD invalidation level is broken before fill.

Cache safety:
run_main_candidate_filter_tests.py config cache key now includes the new v2 config fields.

Validation script:
work/backtest/scripts/run_manual_rule_v2_validation.py

Report folder:
work/backtest/outputs/reports/manual_rule_v2_validation/

Tested v2 settings:
- opening_premarket_sweep_mode=recent_as_setup
- opening_premarket_sweep_start=09:15
- require_swing_near_value_area=True
- cancel_pending_on_opposite_cisd=True

Result versus current final candidates:

funded_selected:
current: 154 trades, 44.16% WR, +62.0R, -7.0R DD, PF 1.72.
manual_v2: 314 trades, 35.35% WR, +76.0R, -11.0R DD, PF 1.37.
Interpretation: not acceptable as a funded replacement; trade count jumps too much and quality drops.
Cause: NQ funded leg increases from 36 to 207 trades.

funded_selected_frequency:
current: 194 trades, 43.30% WR, +70.0R, -8.0R DD, PF 1.64.
manual_v2: 350 trades, 35.71% WR, +82.0R, -12.0R DD, PF 1.36.
Interpretation: not acceptable as a funded replacement for the same reason.

phase_selected:
current: 267 trades, 35.21% WR, +88.5R, -10.0R DD, PF 1.51.
manual_v2: 303 trades, 35.64% WR, +108.5R, -9.5R DD, PF 1.56.
Interpretation: promising for challenge logic. NQ phase improves from 176 trades / +36R to 214 trades / +54R.

phase_selected_frequency:
current: 315 trades, 34.60% WR, +95.0R, -10.0R DD, PF 1.46.
manual_v2: 360 trades, 34.17% WR, +106.5R, -15.0R DD, PF 1.45.
Interpretation: mixed. Adds net R but worsens DD materially.

Decision:
Do not adopt manual_v2 globally.
Keep FON_CORE and FON_FAST on current rules for now.
Next likely path:
- Apply v2-style context rules only to CHALLENGE_CORE first.
- Keep funded-stage systems on FON_CORE/FON_FAST current rules.
- Re-run phase-to-funded lifecycle for CHALLENGE_CORE_v2 -> FON_CORE and CHALLENGE_CORE_v2 -> FON_FAST.
```

Challenge V2 to funded lifecycle generated on 2026-07-08:

```text
Script:
work/backtest/scripts/run_challenge_v2_to_funded_lifecycle.py

Report folder:
work/backtest/outputs/reports/challenge_v2_to_funded_lifecycle/

Generated files:
report.md
account_fit.csv
summary.csv
outcome_breakdown.csv
initial_start.csv
funded_failures.csv
lifecycle_runs.csv
stage_details.csv

Scope:
Challenge phases use manual-rule v2 challenge systems.
Funded stage switches to current funded systems.

Tested combinations:
- CHALLENGE_CORE_V2 -> FON_CORE
- CHALLENGE_CORE_V2 -> FON_FAST
- CHALLENGE_FAST_V2 -> FON_CORE
- CHALLENGE_FAST_V2 -> FON_FAST

Best result:
CHALLENGE_CORE_V2 -> FON_CORE.

A1 5k 7%+5% DD10:
clean.
303 starts, 280 reached funded, 92.41% funded reach, 0 phase-1 failures, 0 phase-2 failures,
0 funded-stage failures.

A2 5k 6%+5% DD8:
usable_watch.
303 starts, 259 reached funded, 85.48% funded reach, 9 phase-1 failures, 12 phase-2 failures,
0 funded-stage failures.

A3 25k 6% trail4:
usable_watch with FON_CORE only.
303 starts, 223 reached funded, 73.60% funded reach, 58 phase-1 failures,
0 funded-stage failures.
Do not use FON_FAST on A3; CHALLENGE_CORE_V2 -> FON_FAST has 75.34% funded failure
of reached-funded starts on A3.

CHALLENGE_FAST_V2:
Not preferred as default.
It has lower funded reach than CHALLENGE_CORE_V2 and more phase failures.

Updated operating plan:
Default all-account plan:
CHALLENGE_CORE_V2 -> FON_CORE.

A1 optional:
CHALLENGE_CORE_V2 -> FON_FAST if higher funded frequency is desired.

A2 optional:
CHALLENGE_CORE_V2 -> FON_FAST only with tighter monitoring.

A3:
Use only CHALLENGE_CORE_V2 -> FON_CORE if trading A3.
Avoid FON_FAST on A3.

Playbook updated:
work/backtest/outputs/reports/fund_account_playbook/report.md
```

A3 static drawdown sensitivity generated on 2026-07-08:

```text
Script:
work/backtest/scripts/run_a3_static_dd_sensitivity.py

Report folder:
work/backtest/outputs/reports/a3_static_dd_sensitivity/

Scope:
Same operating plan: CHALLENGE_CORE_V2 -> FON_CORE.
Only A3 drawdown model changes.

Compared:
- A3 current 6% target, trailing 4% DD, 0.5% risk/R.
- A3 alt 6% target, static 4% DD, 0.5% risk/R.
- A3 alt 6% target, static 5% DD, 0.5% risk/R.
- A3 alt 6% target, static 6% DD, 0.5% risk/R.

Results:
A3 current trailing 4%:
303 starts, 223 reached funded, 73.60% funded reach, 58 phase-1 failures, 0 funded failures.

A3 static 4%:
303 starts, 272 reached funded, 89.77% funded reach, 9 phase-1 failures, 0 funded failures.

A3 static 5%:
303 starts, 281 reached funded, 92.74% funded reach, 0 phase-1 failures, 0 funded failures.

A3 static 6%:
303 starts, 281 reached funded, 92.74% funded reach, 0 phase-1 failures, 0 funded failures.

Interpretation:
The A3 weakness is primarily the trailing drawdown structure, not the strategy edge.
If A3-style account had static 5%-6% DD, CHALLENGE_CORE_V2 -> FON_CORE would be clean.
For actual trailing 4% A3, keep it usable_watch at best and avoid FON_FAST.
```

Challenge speed sensitivity generated on 2026-07-08:

```text
Script:
work/backtest/scripts/run_challenge_speed_sensitivity.py

Report folder:
work/backtest/outputs/reports/challenge_speed_sensitivity/

Purpose:
Test whether challenge passing time can be reduced without materially worsening phase failure risk.
Funded stage is intentionally excluded.

Tested systems:
- CHALLENGE_CORE_V2
- CHALLENGE_FAST_V2

Tested A1/A2 risk per R:
- 1.00%
- 1.25%
- 1.50%

Key results:

CHALLENGE_CORE_V2 / A1 risk 1.00:
92.41% pass rate, 0 phase failures, avg 32.28 trade days, median 27 trade days,
avg 157.58 calendar days, median 135 calendar days.

CHALLENGE_CORE_V2 / A1 risk 1.25:
86.80% pass rate, 21 phase failures, avg 25.41 trade days, median 21 trade days,
avg 122.73 calendar days, median 106 calendar days.
Interpretation: only acceptable speed-up candidate for A1 if faster passing is worth some added phase-fail risk.

CHALLENGE_CORE_V2 / A1 risk 1.50:
76.57% pass rate, 55 phase failures, avg 20.33 trade days, median 16 trade days.
Rejected: too much pass-rate damage.

CHALLENGE_CORE_V2 / A2 risk 1.00:
85.48% pass rate, 21 phase failures, avg 27.88 trade days, median 24 trade days,
avg 134.88 calendar days, median 122 calendar days.

CHALLENGE_CORE_V2 / A2 risk 1.25:
72.61% pass rate, 67 phase failures, avg 18.40 trade days, median 16 trade days.
Rejected: A2 DD8 is too tight for 1.25% risk.

CHALLENGE_FAST_V2:
Rejected as default for both A1 and A2.
It is not meaningfully better on duration at risk 1.00 and has worse pass rates / more phase failures.

Current recommendation:
- A1 normal/stable: CHALLENGE_CORE_V2 at 1.00% risk.
- A1 faster/aggressive: CHALLENGE_CORE_V2 at 1.25% risk.
- A2: keep CHALLENGE_CORE_V2 at 1.00% risk.
- Do not use 1.50% risk for A1/A2.
- Do not use CHALLENGE_FAST_V2 as the default speed solution.

Open issue:
Calendar duration remains long because trade frequency is low. Risk increase reduces trade count,
but does not fully solve calendar time without increasing phase-failure risk.
Further speed improvement likely requires a separate, quality-preserving frequency add-on, not just higher risk.
```

Challenge forex add-on sensitivity generated on 2026-07-08:

```text
Script:
work/backtest/scripts/run_challenge_forex_addon_sensitivity.py

Report folder:
work/backtest/outputs/reports/challenge_forex_addon_sensitivity/

Purpose:
Test whether the parked forex research line can act as a CHALLENGE_CORE_V2 frequency add-on.

Add-on tested:
EURUSD 5m, 3R, midpoint, 09:30-10:30, exclude Monday.

Important caveat:
This EURUSD line is research-only and was selected in-sample from forex filter tests.

Risk rule:
NQ, SPX, and EURUSD were merged and then pair-level daily -1R cap remained active.

Trade count after daily cap:
CHALLENGE_CORE_V2 baseline:
303 trades: NQ 210, SPX 93, EURUSD 0.

CHALLENGE_CORE_V2 + EURUSD exclude-Monday:
422 trades: NQ 201, SPX 87, EURUSD 134.

Key results:
A1 risk 1.00:
Baseline: 92.41% pass rate, 0 phase failures, avg 157.58 calendar days, median 135.
With EURUSD: 88.63% pass rate, 18 phase failures, avg 127.52 calendar days, median 121.5.

A2 risk 1.00:
Baseline: 85.48% pass rate, 21 phase failures, avg 134.88 calendar days, median 122.
With EURUSD: 78.20% pass rate, 63 phase failures, avg 105.18 calendar days, median 97.5.

Decision:
Do not add EURUSD as the current challenge frequency solution.
It does reduce calendar duration, but the pass-rate and phase-failure damage is too high,
especially for A2.

Implication:
The next frequency add-on should not come from the current forex parked line.
Look for a cleaner NQ/SPX add-on or a stricter forex variant before retesting lifecycle speed.
```

Challenge NQ/SPX add-on sensitivity generated on 2026-07-08:

```text
Script:
work/backtest/scripts/run_challenge_nq_spx_addon_sensitivity.py

Report folder:
work/backtest/outputs/reports/challenge_nq_spx_addon_sensitivity/

Purpose:
Find a CHALLENGE_CORE_V2 frequency add-on using only NQ/SPX, without materially damaging
A1/A2 pass rate or phase-failure behavior.

Baseline:
manual-rule v2 CHALLENGE_CORE_V2
NQ first30_q60
SPX baseline
pair-level daily -1R cap

Tested add-ons:
- SPX latest_entry_time 10:45
- SPX latest_entry_time 11:00
- NQ trade_window_end 11:00
- NQ all weekdays
- NQ trade_window_end 11:00 + SPX latest_entry_time 10:45

Trade count after daily cap:
Baseline: 303 trades, NQ 210, SPX 93.
SPX latest 10:45: 334 trades, NQ 210, SPX 124.
SPX latest 11:00: 355 trades, NQ 210, SPX 145.
NQ window 11:00: 359 trades, NQ 266, SPX 93.
NQ all weekdays: 359 trades, NQ 273, SPX 86.
NQ 11:00 + SPX 10:45: 387 trades, NQ 266, SPX 121.

Key A1 risk 1.00 results:
Baseline: 92.41% pass, 0 phase failures, avg 32.28 trade days, avg 157.58 calendar days.
SPX 10:45: 89.22% pass, 11 phase failures, avg 30.29 trade days, avg 139.33 calendar days.
SPX 11:00: 88.73% pass, 15 phase failures, avg 31.55 trade days, avg 141.25 calendar days.
NQ 11:00: 93.04% pass, 0 phase failures, avg 36.44 trade days, avg 153.65 calendar days.
NQ all weekdays: 79.67% pass, 58 phase failures, avg 26.43 trade days, avg 118.37 calendar days.
NQ 11:00 + SPX 10:45: 92.76% pass, 0 phase failures, avg 37.29 trade days, avg 148.86 calendar days.

Key A2 risk 1.00 results:
Baseline: 85.48% pass, 21 phase failures, avg 27.88 trade days, avg 134.88 calendar days.
SPX 10:45: 80.24% pass, 41 phase failures, avg 25.29 trade days, avg 116.85 calendar days.
SPX 11:00: 75.21% pass, 63 phase failures, avg 25.91 trade days, avg 109.88 calendar days.
NQ 11:00: 89.42% pass, 13 phase failures, avg 32.72 trade days, avg 140.33 calendar days.
NQ all weekdays: 71.31% pass, 90 phase failures, avg 20.59 trade days, avg 94.62 calendar days.
NQ 11:00 + SPX 10:45: 84.50% pass, 33 phase failures, avg 32.58 trade days, avg 126.11 calendar days.

Decision:
No clean default speed add-on found yet.

Rejected:
- NQ all weekdays: speeds up but destroys pass rate and phase-failure profile.
- SPX 11:00: too much A2 degradation.
- SPX 10:45: useful speed effect, but A2 phase failures rise from 21 to 41.

Watchlist:
- NQ 11:00 is quality-positive, especially for A2, but not a speed solution because trade days
  and A2 calendar days increase.
- NQ 11:00 + SPX 10:45 preserves A1 very well and improves A2 calendar days, but A2 pass rate
  slips below baseline and phase failures rise from 21 to 33.

Next likely test:
Refine the SPX 10:45 add-on instead of accepting it directly.
Try SPX 10:45 with quality filters, for example first30 q70/q60, weekday exclusions, or
post-10:30-only filters, to keep most of the calendar speed benefit while reducing A2 phase failures.
```

Symbol expansion note:

```text
Metals are not prioritized for the next frequency expansion path because the user does not want Asia-session trades,
and metals often make important moves during Asia. Adding metals could force a materially different strategy profile.

Forex research branch is acceptable:
- EURUSD
- GBPUSD

Compatible 3m/5m forex data and initial symbol configs are now available.
Likely starting point:
- NY-only or London/NY overlap only
- avoid Asia session
- test whether the same VAH/VAL + sweep + CISD/FVG logic transfers
- treat forex as separate research branch, not as a direct addition to current NQ/SPX candidates until validated.
```

Forex research data download started/completed on 2026-07-07:

```text
User asked to test the same strategy separately on EURUSD and GBPUSD later.
These are not added to the active candidate set yet.

Downloader:
work/backtest/scripts/download_dukascopy.py

Recovery helper added because the first EURUSD download was interrupted:
work/backtest/scripts/recover_dukascopy_temp_download.py

Requested date range:
2022-01-01 -> 2026-07-03 Dukascopy CLI date range

Practical loaded NY range:
2022-01-02 17:00/17:03 NY -> 2026-07-02 23:55/23:57 NY

Symbols:
DUKASCOPY_EURUSD
DUKASCOPY_GBPUSD

Files:
work/backtest/data/raw/DUKASCOPY_EURUSD, 3m_2022-01-01_2025-02-13.csv
work/backtest/data/raw/DUKASCOPY_EURUSD, 5m_2022-01-01_2025-02-13.csv
work/backtest/data/raw/DUKASCOPY_EURUSD, 3m_2025-01-03_2025-02-13.csv
work/backtest/data/raw/DUKASCOPY_EURUSD, 5m_2025-01-03_2025-02-13.csv
work/backtest/data/raw/DUKASCOPY_EURUSD, 3m_2025-02-13_2026-07-03.csv
work/backtest/data/raw/DUKASCOPY_EURUSD, 5m_2025-02-13_2026-07-03.csv
work/backtest/data/raw/DUKASCOPY_EURUSD, 3m_2025-02-17_2025-02-18.csv
work/backtest/data/raw/DUKASCOPY_EURUSD, 5m_2025-02-17_2025-02-18.csv
work/backtest/data/raw/DUKASCOPY_GBPUSD, 3m_2022-01-01_2026-07-03.csv
work/backtest/data/raw/DUKASCOPY_GBPUSD, 5m_2022-01-01_2026-07-03.csv

EURUSD note:
The first interrupted download left usable temp chunks through early 2025.
Recovered them with recover_dukascopy_temp_download.py.
Then downloaded missing 2025-01-03 -> 2025-02-13 and 2025-02-13 -> 2026-07-03 ranges.
The one failed 2025-02-17 day was retried successfully into a separate small file.
An old failed-chunks audit CSV may remain, but the failed day was filled.
Loader de-duplicates timestamps.

Merged loader validation:
DUKASCOPY_EURUSD 3m:
4 files, 560,764 rows, 560,764 unique timestamps, no duplicates, no zero volume rows
2022-01-02 17:03 NY -> 2026-07-02 23:57 NY

DUKASCOPY_EURUSD 5m:
4 files, 336,622 rows, 336,622 unique timestamps, no duplicates, no zero volume rows
2022-01-02 17:00 NY -> 2026-07-02 23:55 NY

DUKASCOPY_GBPUSD 3m:
1 file, 560,723 rows, 560,723 unique timestamps, no duplicates, no zero volume rows
2022-01-02 17:00 NY -> 2026-07-02 23:57 NY

DUKASCOPY_GBPUSD 5m:
1 file, 336,621 rows, 336,621 unique timestamps, no duplicates, no zero volume rows
2022-01-02 17:00 NY -> 2026-07-02 23:55 NY

Next forex work:
Keep forex as separate research branch:
- no Asia session trading
- likely test NY-only or London/NY-overlap variants
- verify pip/tolerance/min-FVG assumptions before trusting results
- run the first forex strategy grid after deciding the initial NY/London-NY test windows
```

Forex symbol configs added on 2026-07-07:

```text
Config file:
work/backtest/backtest/config.py

DUKASCOPY_EURUSD 3m/5m:
vah_val_tolerance=0.0005
spread_points=0.00002
slippage_points=0.00001
equal_swing_tolerance=0.0003
min_fvg_points=0.00003

DUKASCOPY_GBPUSD 3m/5m:
vah_val_tolerance=0.0007
spread_points=0.00003
slippage_points=0.000015
equal_swing_tolerance=0.0004
min_fvg_points=0.00004

Validation:
python -m compileall backtest passed.
Config import check found all four keys:
- DUKASCOPY_EURUSD 3m
- DUKASCOPY_EURUSD 5m
- DUKASCOPY_GBPUSD 3m
- DUKASCOPY_GBPUSD 5m

Smoke run:
5m data, 2025-02 only, 09:30-11:00 NY, 2R, CISD body stop, max 1 trade/day,
all weekdays.

DUKASCOPY_EURUSD:
4 source files, 5,700 candles, 4 skipped dates, 8 trades, +1.0R, 37.50% WR

DUKASCOPY_GBPUSD:
1 source file, 5,760 candles, 3 skipped dates, 7 trades, -4.0R, 14.29% WR

Interpretation:
This was only an integration smoke test, not a candidate-quality result.
The configs, loaded data, and strategy engine are ready for proper forex research tests.
```

Forex research tests generated on 2026-07-07:

```text
First-pass grid script:
work/backtest/scripts/run_forex_research_grid.py

First-pass grid report:
work/backtest/outputs/reports/forex_research_grid/report.md
work/backtest/outputs/reports/forex_research_grid/comparison.csv
work/backtest/outputs/reports/forex_research_grid/monthly_detail.csv
work/backtest/outputs/reports/forex_research_grid/all_trades.csv

Scope:
6 selected months:
2022-12, 2023-06, 2023-09, 2023-10, 2025-02, 2025-03

Grid:
EURUSD and GBPUSD
3m and 5m
RR 2R, 2.5R, 3R
entry start and midpoint
CISD body stop
NY-only windows:
09:30-10:30
09:30-11:00
09:30-12:00
10:00-12:00
max 1 trade/day
Monday-Friday

Runtime:
~560.6 seconds / 9 minutes 21 seconds

First-pass result:
3m failed on both pairs.
EURUSD 3m: 0/24 positive runs, best net -6R
GBPUSD 3m: 0/24 positive runs, best net -7R

5m produced some positive runs:
Best GBPUSD:
5m, 2.5R, start entry, 10:00-12:00:
52 trades, 8.67/month, 34.62% WR, +11R, -7.5R DD, PF 1.32

Best EURUSD:
5m, 3R, midpoint entry, 09:30-11:00:
44 trades, 7.33/month, 29.55% WR, +8R, -7R DD, PF 1.26
```

Forex full-data validation generated on 2026-07-07:

```text
Full validation script:
work/backtest/scripts/run_forex_full_validation.py

Full validation report:
work/backtest/outputs/reports/forex_full_validation/report.md
work/backtest/outputs/reports/forex_full_validation/comparison.csv
work/backtest/outputs/reports/forex_full_validation/monthly_detail.csv
work/backtest/outputs/reports/forex_full_validation/all_trades.csv

Scope:
Best 7 first-pass 5m variants on full available Dukascopy forex data.

Runtime:
~347.0 seconds / 5 minutes 47 seconds

Full-data highlights:
GBPUSD 5m 3R start 10:00-12:00:
474 trades, 8.78/month, 26.16% WR, +22R, -31R DD, PF 1.06, R/DD 0.71

EURUSD 5m 3R midpoint 09:30-11:00:
379 trades, 6.89/month, 26.39% WR, +21R, -28R DD, PF 1.08, R/DD 0.75

EURUSD 5m 3R midpoint 09:30-10:30:
192 trades, 3.56/month, 27.08% WR, +16R, -12R DD, PF 1.11, R/DD 1.33

Interpretation:
The forex transfer is not dead, but it is not candidate-quality yet.
The positive full-data results are too weak versus current NQ/SPX systems:
PF is only around 1.06-1.11 and drawdowns are too high for the added edge.

Current forex decision:
Do not add EURUSD/GBPUSD to the active candidate set yet.
Keep them as research-only.

Best next forex follow-up if needed:
Use only 5m.
Drop 3m.
Focus on:
- EURUSD 5m 3R midpoint 09:30-10:30 or 09:30-11:00
- GBPUSD 5m 3R start 10:00-12:00
Then test filters to reduce bad months/DD before considering them as frequency add-ons.
Worst months to inspect include:
- EURUSD: 2022-07, 2023-10, 2024-06, 2025-04
- GBPUSD: 2022-02, 2022-03, 2025-05, 2025-11, 2026-02
```

Forex filter tests generated on 2026-07-07:

```text
Script:
work/backtest/scripts/run_forex_filter_tests.py

Report folder:
work/backtest/outputs/reports/forex_filter_tests/

Files:
report.md
comparison.csv
monthly_detail.csv
all_trades.csv
first30_thresholds.csv

Scope:
Filter diagnostics on the 3 strongest forex 5m research runs:
- EURUSD 5m 3R midpoint 09:30-10:30
- EURUSD 5m 3R midpoint 09:30-11:00
- GBPUSD 5m 3R start 10:00-12:00

Tested filters:
- FVG only
- IFVG only
- long only
- short only
- live-safe first30_range q60/q70 cap
- exclude each weekday as a post-trade diagnostic

Runtime:
~989.0 seconds / 16 minutes 29 seconds

Important caveat:
Weekday exclusions and first30 thresholds are diagnostic because they are selected from available history.
They need out-of-sample validation before being trusted.

first30 thresholds:
EURUSD q60=0.00169, q70=0.00193
GBPUSD q60=0.00212, q70=0.002369

Filter results:

EURUSD 5m 3R midpoint 09:30-10:30 baseline:
192 trades, 3.56/month, 27.08% WR, +16R, -12R DD, PF 1.11, R/DD 1.33

Best EURUSD 09:30-10:30 filter:
Exclude Monday:
146 trades, 2.70/month, 30.82% WR, +34R, -9R DD, PF 1.34, R/DD 3.78
This is the cleanest forex improvement found.

EURUSD 5m 3R midpoint 09:30-11:00 baseline:
379 trades, 6.89/month, 26.39% WR, +21R, -28R DD, PF 1.08, R/DD 0.75

Best EURUSD 09:30-11:00 filter:
Exclude Monday:
300 trades, 5.45/month, 28.00% WR, +36R, -21R DD, PF 1.17, R/DD 1.71
Improves materially, but still not strong enough versus NQ/SPX.

GBPUSD 5m 3R start 10:00-12:00 baseline:
474 trades, 8.78/month, 26.16% WR, +22R, -31R DD, PF 1.06, R/DD 0.71

Best GBPUSD filters:
Exclude Friday:
378 trades, 7.00/month, 27.25% WR, +34R, -28R DD, PF 1.12, R/DD 1.21

Long only:
269 trades, 4.98/month, 27.51% WR, +27R, -20R DD, PF 1.14, R/DD 1.35

Rejected filters:
- first30_range q60/q70 did not help; it usually reduced trades and failed to improve PF/DD enough.
- IFVG-only was too low frequency and negative/weak.
- Short-only was poor on GBPUSD and weak on EURUSD.

Current forex decision after filters:
Still do not add forex to the active candidate set.
Only one forex line is worth parking for possible later validation:
EURUSD 5m 3R midpoint 09:30-10:30, exclude Monday.
It has good R/DD in-sample, but only 2.70 trades/month and needs out-of-sample/month split validation.
```

Corrected engine 3-random-month test generated on 2026-07-05:

```text
Script:
work/backtest/scripts/run_corrected_engine_3month_report.py

Report folder:
work/backtest/outputs/reports/corrected_engine_3month/

Old large stop/entry report folder removed to avoid confusion:
work/backtest/outputs/reports/dukascopy_stop_entry_model_tests/

Random seed:
20260705

Selected months:
2023-10, 2025-02, 2025-03
```

Corrected 3-month results:

```text
NQ FVG opposite edge 3R:
23 trades, 17.39% WR, -7.0R, -11.0R DD, PF 0.63

SPX FVG opposite edge 3R:
13 trades, 46.15% WR, +11.0R, -3.0R DD, PF 2.57

Silver FVG opposite edge 2R:
8 trades, 25.00% WR, -2.0R, -2.0R DD, PF 0.67
```

Cross-symbol reports generated on 2026-07-03:

```text
outputs/reports/cross_symbol_summary.csv
outputs/reports/cross_symbol_weekday_stats.csv
outputs/reports/filtered_comparison.csv
outputs/reports/nq/
outputs/reports/spx/
outputs/reports/gold/
outputs/reports/silver/
outputs/reports/filtered/
```

Entry model comparison reports generated on 2026-07-03:

```text
outputs/reports/entry_model_tests/entry_model_comparison.csv
outputs/reports/entry_model_tests/nq_3m_max1_no_tuesday/
outputs/reports/entry_model_tests/spx_5m_max2_tue_wed_fri_fvg/
outputs/reports/entry_model_tests/silver_5m_max1_tue_wed_thu/
```

Tested entry modes:

```text
start       = first FVG/IFVG boundary touched by retracement
quarter_25  = 25% into the FVG/IFVG from the start boundary
cisd_close  = enter at CISD candle close without waiting for FVG/IFVG
```

Entry model comparison highlights:

```text
NAS100 no Tue start: 164 trades, 45.73% WR, 136.0R, -5.0R DD, PF 2.53
NAS100 no Tue quarter_25: 154 trades, 36.60% WR, 71.0R, -10.0R DD, PF 1.73
NAS100 no Tue cisd_close: 183 trades, 16.39% WR, -63.0R, -78.0R DD, PF 0.59

SPX500 Tue/Wed/Fri FVG start: 146 trades, 41.10% WR, 94.0R, -11.0R DD, PF 2.09
SPX500 Tue/Wed/Fri FVG quarter_25: 124 trades, 31.45% WR, 32.0R, -12.0R DD, PF 1.38
SPX500 Tue/Wed/Fri FVG cisd_close: 241 trades, 21.16% WR, -37.0R, -44.0R DD, PF 0.81

Silver Tue/Wed/Thu start: 116 trades, 42.24% WR, 80.0R, -12.0R DD, PF 2.19
Silver Tue/Wed/Thu quarter_25: 89 trades, 32.58% WR, 27.0R, -11.0R DD, PF 1.45
Silver Tue/Wed/Thu cisd_close: 137 trades, 16.79% WR, -45.0R, -47.0R DD, PF 0.61
```

Reward/RR comparison reports generated on 2026-07-03 using FVG/IFVG start entry:

```text
outputs/reports/reward_r_tests/reward_r_comparison.csv
outputs/reports/reward_r_tests/nq_3m_max1_no_tuesday/
outputs/reports/reward_r_tests/spx_5m_max2_tue_wed_fri_fvg/
outputs/reports/reward_r_tests/silver_5m_max1_tue_wed_thu/
```

Important implementation note:

```text
On 2026-07-03, reward_r testing exposed and fixed a bug where target prices used reward_r
but winning trades were still recorded as fixed 3.0R. simulate_exit now records wins as config.reward_r.
```

Reward/RR comparison highlights:

```text
NAS100 no Tue 1R: 164 trades, 75.00% WR, 82.0R, -3.0R DD, PF 3.00
NAS100 no Tue 2R: 164 trades, 52.44% WR, 94.0R, -4.0R DD, PF 2.21
NAS100 no Tue 4R: 164 trades, 32.32% WR, 101.0R, -7.0R DD, PF 1.91

SPX500 Tue/Wed/Fri FVG 1R: 163 trades, 71.17% WR, 69.0R, -4.0R DD, PF 2.47
SPX500 Tue/Wed/Fri FVG 2R: 149 trades, 51.01% WR, 79.0R, -7.0R DD, PF 2.08
SPX500 Tue/Wed/Fri FVG 4R: 146 trades, 33.56% WR, 99.0R, -10.0R DD, PF 2.02

Silver Tue/Wed/Thu 1R: 116 trades, 75.00% WR, 58.0R, -3.0R DD, PF 3.00
Silver Tue/Wed/Thu 2R: 116 trades, 52.59% WR, 67.0R, -6.0R DD, PF 2.22
Silver Tue/Wed/Thu 4R: 116 trades, 32.76% WR, 74.0R, -15.0R DD, PF 1.95
```

Fund account RR simulation reports generated on 2026-07-03 using FVG/IFVG start entry:

```text
outputs/reports/fund_rr_tests/fund_rr_summary.md
outputs/reports/fund_rr_tests/fund_rr_summary.csv
outputs/reports/fund_rr_tests/fund_rr_pass_rate_matrix.csv
outputs/reports/fund_rr_tests/fund_rr_fail_rate_matrix.csv
outputs/reports/fund_rr_tests/fund_rr_rolling.csv
outputs/reports/fund_rr_tests/fund_rr_initial_start.csv
```

Fund simulation assumptions:

```text
A1 5k 7%+5% DD10: 2 phases, 1% risk per trade, static 10% max DD
A2 5k 6%+5% DD8: 2 phases, 1% risk per trade, static 8% max DD
A3 25k 6% trail4: 1 phase, 0.5% risk per trade, trailing 4% max DD
```

Fund RR pass-rate highlights:

```text
NAS100 no Tue:
A1 pass rates 1R/2R/3R/4R = 85.37% / 86.59% / 87.80% / 84.15%
A2 pass rates 1R/2R/3R/4R = 86.59% / 87.80% / 89.02% / 86.59%
A3 pass rates 1R/2R/3R/4R = 85.98% / 87.20% / 88.41% / 87.80%

SPX500 Tue/Wed/Fri FVG:
A1 pass rates 1R/2R/3R/4R = 88.64% / 94.70% / 92.42% / 93.18%
A2 pass rates 1R/2R/3R/4R = 90.15% / 95.45% / 88.64% / 89.39%
A3 pass rates 1R/2R/3R/4R = 89.39% / 94.70% / 80.30% / 81.06%

Silver Tue/Wed/Thu:
A1 pass rates 1R/2R/3R/4R = 84.48% / 83.62% / 81.90% / 81.03%
A2 pass rates 1R/2R/3R/4R = 87.93% / 87.07% / 80.17% / 75.00%
A3 pass rates 1R/2R/3R/4R = 86.21% / 85.34% / 80.17% / 68.97%
```

Stop management test reports generated on 2026-07-03:

```text
outputs/reports/stop_management_tests/stop_management_summary.md
outputs/reports/stop_management_tests/stop_management_comparison.csv
outputs/reports/stop_management_tests/stop_management_fund_summary.md
outputs/reports/stop_management_tests/stop_management_fund_summary.csv
outputs/reports/stop_management_tests/stop_management_fund_rolling.csv
```

Stop management modes tested:

```text
none                       = original fixed stop
be_at_half_target          = when price reaches 50% of TP distance, move stop to entry
half_stop_at_half_target   = when price reaches 50% of TP distance, move stop halfway toward entry
```

Stop management backtest highlights:

```text
NAS100 no Tue 3R:
Baseline: 164 trades, 45.73% WR, 136.0R, -5.0R DD, PF 2.53, R/DD 27.20
BE at 50%: 164 trades, 36.59% WR, 117.0R, -6.0R DD, PF 2.86, R/DD 19.50
Half SL at 50%: 164 trades, 41.46% WR, 124.5R, -6.0R DD, PF 2.57, R/DD 20.75

SPX500 Tue/Wed/Fri FVG 2R:
Baseline: 149 trades, 51.01% WR, 79.0R, -7.0R DD, PF 2.08, R/DD 11.29
BE at 50%: 156 trades, 37.82% WR, 72.0R, -4.0R DD, PF 2.57, R/DD 18.00
Half SL at 50%: 151 trades, 44.37% WR, 69.5R, -5.0R DD, PF 2.08, R/DD 13.90

Silver Tue/Wed/Thu 1R:
Baseline: 116 trades, 75.00% WR, 58.0R, -3.0R DD, PF 3.00, R/DD 19.33
BE at 50%: 116 trades, 50.00% WR, 48.0R, -3.0R DD, PF 5.80, R/DD 16.00
Half SL at 50%: 116 trades, 62.07% WR, 45.0R, -3.5R DD, PF 2.67, R/DD 12.86
```

Stop management fund pass-rate highlights:

```text
NAS100 no Tue 3R:
A1 baseline/BE/halfSL = 87.80% / 88.41% / 88.41%
A2 baseline/BE/halfSL = 89.02% / 90.24% / 89.02%
A3 baseline/BE/halfSL = 88.41% / 90.24% / 89.02%

SPX500 Tue/Wed/Fri FVG 2R:
A1 baseline/BE/halfSL = 94.70% / 94.70% / 94.70%
A2 baseline/BE/halfSL = 95.45% / 95.45% / 95.45%
A3 baseline/BE/halfSL = 94.70% / 95.45% / 94.70%

Silver Tue/Wed/Thu 1R:
A1 baseline/BE/halfSL = 84.48% / 81.03% / 82.76%
A2 baseline/BE/halfSL = 87.93% / 84.48% / 84.48%
A3 baseline/BE/halfSL = 86.21% / 83.62% / 83.62%
```

Decision:

```text
Do not use BE-at-50% or half-stop-at-50% as current strategy defaults.
Both reduce overall backtest quality versus the fixed-stop baseline.
Keep stop_management = none unless explicitly testing management variants.
```

## Monthly Stats

Monthly report columns:

```text
month
total_trades
tp
sl
open_trades
win_rate
net_r
mtm_net_r
```

No current monthly stats exist yet after cleanup.

## Manual Calibration Examples

Manual examples live here:

```text
work/backtest/calibration_examples/
```

Current examples:

```text
2026-03-05 short NAS100.md
2026-03-09 Long NAS100.md
2026-03-13 Long NAS100.md
2026-03-19 Short NAS100.md
```

Old calibration report outputs were cleared. Source examples are kept for future tests.

Manual random-sample review started:

```text
work/backtest/calibration_examples/manual_review_2025_random_sample.md
```

## Known Gaps / Next Work

Highest priority:

1. Entry model default is now FVG/IFVG start.
   - CLI now supports midpoint, start, quarter_25, and cisd_close.
   - Initial candidate tests strongly favored FVG/IFVG start.
   - Manual chart review is still useful before treating final metrics as trusted.
   - Some manual trades use midpoint.
   - Some are manually between 25%-50%.

2. Remaining entry mode work:

```text
manual/test-only
```

3. Improve IFVG detection.
   - Current IFVG logic is still approximate.

4. Add true day open as context.
   - Do not make it a primary sweep trigger yet.

5. Add equal high/low detection.
   - Current swing clustering is a first approximation.

6. Build combined 3m + 5m test on shared period.
   - Use common date range only.
   - Decide how to resolve duplicate/competing 3m vs 5m signals.

7. Add more manual examples before trusting final metrics.
   - Especially examples where user would not take the trade.
   - Add explicit fields:

```text
Sweep edilen liquidity:
Sweep saati:
İşleme girdiğim PD array türü:
PD array yönü:
PD array oluşum saati:
PD array aralığı:
Entry modeli:
Entry:
```

## Forward Test Idea

Discussed but postponed.

Potential path:

1. Build TradingView Pine indicator for live chart signals.
2. Pine indicator can draw levels and trigger alerts.
3. TradingView alert webhook can send JSON to a local/cloud Python logger.
4. Python logs forward-test signals and later evaluates TP/SL.

Important limitation:

- Pine cannot directly call the Python backtest engine and receive arbitrary results inside the chart.
- TradingView Fixed Range Volume Profile values cannot be directly read from Pine.

Manual feedback regression V3 added and run on 2026-07-16:

```text
Source:
C:\Users\ISAAC\Desktop\20 işlem kontrol.txt

Structured cases:
work/backtest/calibration_examples/manual_feedback_regression_v3.csv

Manual stop policy:
work/backtest/calibration_examples/manual_stop_policy_v1.csv

Cross-sweep/thesis lifecycle evidence:
work/backtest/calibration_examples/manual_cross_sweep_lifecycle_labels_v2.csv

Runner:
work/backtest/scripts/run_manual_feedback_regression_v3.py

Report:
work/backtest/outputs/reports/manual_feedback_regression_v3/

Scope:
- 15 completed manual dates: 10 NQ, 5 SPX.
- Manual charts are 5m; active CHALLENGE_CORE_V2 engine legs are NQ 3m and SPX 5m.
- 8 TAKE (4 TP, 4 SL), 7 SKIP (3 no-fill, 4 no-trade).

CHALLENGE_CORE_V2 result:
- Decision + direction: 7/15, 46.67%.
- Decision-only: 8/15, 53.33%.
- Strong labels: 6/11, 54.55%.
- Correct-direction TAKE recall: 1/8.
- SKIP alignment: 6/7.

Interpretation:
- The result is not a frequency-optimization target. The main mismatch is failure to recognize
  manually valid TAKE contexts after VA or HTF requalification.
- Strongest next diagnostic evidence is the five-case 15m body-close contrast set.
- Three no-fill terminals are now explicit: session end, lunch end, target before fill.
- VAH resistance-to-support flip is evidence for requalification without a fresh sweep, not a
  production rule.
- IFVG/accumulation and suspected-news context cases remain WATCH.

Decision:
- No active CHALLENGE_CORE_V2 or FON_CORE default changed.
- NQ + SPX daily -1R pair cap unchanged.
- No controlling-array selector threshold or weight tuning.
```

Manual logic V3 HTF body-close research candidate tested on 2026-07-16:

```text
Implementation:
- Added a default-off `htf_body_close_requalification` research switch.
- Research mode builds complete 15m candles from the active NQ 3m / SPX 5m feeds.
- Detects FVG and a minimal order-block model near VAH/VAL, then emits a directional
  requalification only after a 15m body closes through the array.
- No controlling-array selector threshold or weight was changed.

Manual regression:
- Baseline: 7/15 decision+direction, 46.67%.
- HTF requalification: 7/15, 46.67%.
- Therefore the implementation did not resolve SPX-FB3-012 or SPX-FB3-014 and did not
  improve manual alignment.

Full pair results after the daily -1R cap:
- CHALLENGE_CORE_V2: 303 -> 306 trades, 108.5R -> 110.0R, DD unchanged at -9.5R.
- FON_CORE: 154 -> 164 trades, 62.0R -> 60.0R, DD unchanged at -7.0R.
- Funded NQ leg was the main regression: +11 trades, -3.0R, DD -4.0R -> -7.0R.

Rolling CHALLENGE_CORE_V2 -> FON_CORE funded reach:
- A1: 92.41% -> 92.48% (effectively flat).
- A2: 85.48% -> 78.10%; phase-2 failures 12 -> 34.
- A3: 73.60% -> 61.44%; phase-1 failures 58 -> 96; fit becomes not_preferred.

Decision:
- REJECT FOR PROMOTION.
- Keep research implementation disabled by default for future labeled-case diagnostics only.
- Active CHALLENGE_CORE_V2, FON_CORE, daily -1R pair cap, and all selector defaults remain unchanged.

Runner:
work/backtest/scripts/run_manual_logic_v3_candidate_validation.py

Report:
work/backtest/outputs/reports/manual_logic_v3_candidate_validation/
```

MANUAL_STATE_V1 research checkpoint added on 2026-07-21:

```text
Scope:
- Research-only explainable state engine for the first 10 NQ manual cases.
- Active CHALLENGE_CORE_V2, FON_CORE, daily NQ+SPX -1R cap, and selector defaults unchanged.

Implemented:
- Explainable CISD anchor/body/confirm/displacement/invalidation events.
- Premarket-only HTF controlling-array reaction lock and explicit array lifecycle.
- Conservative BLOCK_ONLY default for reacted HTF arrays; arrays do not automatically create direction.
- One selected liquidity context, VA flip replacement, thesis episodes, and pending-order terminals.
- Same-direction CISD no longer cancels an already resting order.
- Post-CISD FVG may use the CISD anchor as candle one, provided the FVG completes at/after confirm.
- Same-feed WATCH audit separated from the DukasCopy ten-case comparison.
- Known final-CISD detection separated from final-CISD route selection.

Key finding:
- On CAPITALCOM_NAS100 3m, NQ-FB4-010's manual 11:45 bullish CISD and 11:48 bullish FVG
  are both detected exactly.
- The engine still selects and fills an earlier short route. The leading problem is now context authority
  plus qualified/final CISD selection, not inability to detect the manual structures.
- Local Capital.com coverage starts 2025-04-03, so only case 10 of the first ten has same-feed evidence.

Next research order:
1. CISD_QUALIFICATION_V1.
2. CONTEXT_AUTHORITY_V1.
3. LIQUIDITY_SELECTOR_V1.
4. THESIS_REQUALIFICATION_V1 cross-sweep contrast set.
5. Preserve ORDER_LIFECYCLE_V1 regression behavior.

Promotion remains blocked until cases 06/09 stop producing false orders, cases 05/10 select the
correct final CISD route, cases 03/07 preserve correct no-fill terminals, and the four-case cross-sweep
contrast set passes without threshold/weight optimization.

Runner:
work/backtest/scripts/run_manual_state_v1_regression.py

Report:
work/backtest/outputs/reports/manual_state_v1_first10/report.md

Roadmap:
work/backtest/outputs/reports/manual_state_v1_first10/research_roadmap.md
```

CISD_QUALIFICATION_V1 research result on 2026-07-22:

```text
Implementation:
- Added a raw-CISD structure lifecycle: START, REQUALIFIED, INTERNAL_OPPOSITE, REVERSAL.
- Opposite CISD changes direction only when its confirmation close crosses the active CISD's
  protected origin body. This is a structural body-close rule, not a tuned displacement threshold.
- Same-direction CISD updates the protected body.
- Added a chronological context rule: an HTF invalidation and opposing VA flip on the same
  execution candle cannot retroactively bypass the premarket HTF blocker.
- Raw baseline, protected-body variant, same-feed audit, and cross-sweep contrast are reported separately.

Strong result:
- Same-feed CAPITALCOM_NAS100 3m NQ-FB4-010 changed from raw 10:36 short / SL to the exact
  manual route: 11:45 bullish CISD -> 11:48 bullish FVG -> 11:51 fill -> TP.
- The 11:03 bullish and 11:51 bearish mathematical CISDs remain INTERNAL_OPPOSITE because they
  do not close through the active protected body.
- SPX-FB2-018 changed to INVALID / NOT_PLACED / SKIP because the same-candle HTF invalidation
  no longer grants the VA flip a new short thesis. This matches the cross-sweep manual label.

Limits:
- DukasCopy first-ten aggregate did not improve; cases 06 and 09 still fail the false-order gate.
- Cross-sweep contrast is 1/4 exact. SPX-FB2-020 remains an entry-lifecycle mismatch; NQ-FB3-003
  and NQ-FB3-006 remain affected by manual-feed/timeframe context mismatch.
- Therefore CISD_QUALIFICATION_V1 is retained research-only and is not promoted.

Validation:
- 24 targeted manual-state/lifecycle tests passed.
- Python compilation passed.
- Active CHALLENGE_CORE_V2, FON_CORE, daily -1R pair cap, and selector defaults unchanged.

Next:
- CONTEXT_AUTHORITY_V1, followed by LIQUIDITY_SELECTOR_V1.
```

Manual-alignment development package completed on 2026-07-22:

```text
Completed research components:
- CISD_QUALIFICATION_V1
- CONTEXT_AUTHORITY_V1
- LIQUIDITY_SELECTOR_V1
- THESIS_REQUALIFICATION_V1
- ORDER_LIFECYCLE_V1 variant audit

Strong positive evidence:
- Same-feed NQ-FB4-010 is exact: 11:45 bullish CISD -> 11:48 FVG -> 11:51 fill -> TP.
- SPX-FB2-018 is exact INVALID / NOT_PLACED / SKIP after the same-candle HTF invalidation
  can no longer retroactively authorize an opposing VA flip.

Aggregate evidence:
- DukasCopy first10 raw -> package:
  setup 70% -> 60%, order 30% -> 20%, final 40% -> 30%, direction 10% -> 20%,
  outcome 10% -> 0%, decision exact 0% -> 0%.
- Cross-sweep contrast: 1/4 exact.
- Liquidity requirement: 5/10; observable named-reference match: 2/8.
- Explicit-only target-before-fill did not improve results and was rejected.

Decision:
- DO NOT PROMOTE the combined package.
- Keep protected-body CISD and explicit context chronology as research assets.
- Keep liquidity selector WATCH.
- Active CHALLENGE_CORE_V2, FON_CORE, NQ+SPX daily -1R cap and selector defaults unchanged.
- Active manual-feedback V4 summary.csv and comparison.csv hashes were identical before/after rerun.

Validation:
- 29 targeted tests passed.
- Python compilation passed.

Final report:
work/backtest/outputs/reports/manual_state_v1_first10/development_package_final_report.md
```
