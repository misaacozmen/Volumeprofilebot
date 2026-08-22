from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SymbolConfig:
    symbol: str
    timeframe: str
    vah_val_tolerance: float
    spread_points: float
    slippage_points: float
    tick_size_points: float | None = None
    max_trades_per_day: int = 2
    value_area_pct: float = 0.70
    volume_profile_rows: int = 1000
    reward_r: float = 3.0
    fvg_entry_mode: str = "start"
    stop_model: str = "sweep_wick"
    stop_management: str = "none"
    direction_filter: str = "all"
    setup_type_filter: str = "all"
    allowed_weekdays: str = "all"
    swing_lookback_candles: int = 100
    equal_swing_tolerance: float = 5.0
    min_fvg_points: float = 0.0
    allow_entry_on_setup_candle: bool = False
    require_post_sweep_cisd_reference: bool = True
    invalidate_cisd_before_fvg: bool = True
    allow_same_direction_followup_without_fresh_sweep: bool = True
    session_liquidity_only: bool = False
    swing_liquidity_mode: str = "all"
    strong_swing_min_touches: int = 2
    cisd_lookahead_candles: int = 6
    fvg_window_candles: int = 5
    trade_window_start: str = "09:30"
    trade_window_end: str = "12:00"
    latest_entry_time: str | None = None
    min_entry_delay_candles_after_fvg: int = 0
    require_close_away_before_entry: bool = False
    first30_range_filter: str = "off"
    first30_range_max: float | None = None
    swing_first30_directionality_min: float | None = None
    opening_premarket_sweep_mode: str = "off"
    opening_premarket_sweep_start: str = "09:15"
    require_swing_near_value_area: bool = False
    cancel_pending_on_opposite_cisd: bool = False
    require_sweep_rejection_close: bool = False
    opening_premarket_direction_mode: str = "off"
    active_trade_block_mode: str = "exit"
    htf_body_close_requalification: str = "off"


def _configs_for_timeframes(
    symbol: str,
    vah_val_tolerance: float,
    spread_points: float,
    slippage_points: float,
    equal_swing_tolerance: float,
    min_fvg_points: float,
) -> dict[tuple[str, str], SymbolConfig]:
    return {
        (symbol, timeframe): SymbolConfig(
            symbol=symbol,
            timeframe=timeframe,
            vah_val_tolerance=vah_val_tolerance,
            spread_points=spread_points,
            slippage_points=slippage_points,
            equal_swing_tolerance=equal_swing_tolerance,
            min_fvg_points=min_fvg_points,
        )
        for timeframe in ["3m", "5m"]
    }


SYMBOL_CONFIGS: dict[tuple[str, str], SymbolConfig] = {
    **_configs_for_timeframes(
        symbol="CAPITALCOM_NAS100",
        vah_val_tolerance=5.0,
        spread_points=1.0,
        slippage_points=0.5,
        equal_swing_tolerance=5.0,
        min_fvg_points=1.5,
    ),
    **_configs_for_timeframes(
        symbol="CAPITALCOM_SPX500",
        vah_val_tolerance=1.5,
        spread_points=0.4,
        slippage_points=0.1,
        equal_swing_tolerance=1.5,
        min_fvg_points=0.5,
    ),
    **_configs_for_timeframes(
        symbol="CAPITALCOM_XAUUSD",
        vah_val_tolerance=2.0,
        spread_points=0.3,
        slippage_points=0.1,
        equal_swing_tolerance=2.0,
        min_fvg_points=0.4,
    ),
    **_configs_for_timeframes(
        symbol="CAPITALCOM_XAGUSD",
        vah_val_tolerance=0.03,
        spread_points=0.02,
        slippage_points=0.005,
        equal_swing_tolerance=0.03,
        min_fvg_points=0.025,
    ),
    **_configs_for_timeframes(
        symbol="DUKASCOPY_USATECHIDXUSD",
        vah_val_tolerance=5.0,
        spread_points=1.0,
        slippage_points=0.5,
        equal_swing_tolerance=5.0,
        min_fvg_points=1.5,
    ),
    **_configs_for_timeframes(
        symbol="DUKASCOPY_USA500IDXUSD",
        vah_val_tolerance=1.5,
        spread_points=0.4,
        slippage_points=0.1,
        equal_swing_tolerance=1.5,
        min_fvg_points=0.5,
    ),
    **_configs_for_timeframes(
        symbol="DUKASCOPY_XAUUSD",
        vah_val_tolerance=2.0,
        spread_points=0.3,
        slippage_points=0.1,
        equal_swing_tolerance=2.0,
        min_fvg_points=0.4,
    ),
    **_configs_for_timeframes(
        symbol="DUKASCOPY_XAGUSD",
        vah_val_tolerance=0.03,
        spread_points=0.02,
        slippage_points=0.005,
        equal_swing_tolerance=0.03,
        min_fvg_points=0.025,
    ),
    **_configs_for_timeframes(
        symbol="DUKASCOPY_EURUSD",
        vah_val_tolerance=0.0005,
        spread_points=0.00002,
        slippage_points=0.00001,
        equal_swing_tolerance=0.0003,
        min_fvg_points=0.00003,
    ),
    **_configs_for_timeframes(
        symbol="DUKASCOPY_GBPUSD",
        vah_val_tolerance=0.0007,
        spread_points=0.00003,
        slippage_points=0.000015,
        equal_swing_tolerance=0.0004,
        min_fvg_points=0.00004,
    ),
}
