from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import json
from pathlib import Path

import pandas as pd

from .calibration import analyze_examples
from .config import SYMBOL_CONFIGS
from .data_inspector import infer_symbol_timeframe, inspect_paths, print_reports, write_reports_csv
from .data_loader import load_ohlcv
from .evaluation_window import EvaluationWindow, EvaluationWindowError, classify_sessions, coverage_report
from .risk_xray import build_risk_xray, write_risk_xray
from .strategy import monthly_stats, run_backtest, summarize_trades, trades_to_frame, weekday_stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="otobt")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="Inspect TradingView CSV files")
    inspect_parser.add_argument("paths", nargs="+", type=Path, help="CSV file(s) or directories")
    inspect_parser.add_argument("--output", type=Path, help="Optional CSV report output path")
    inspect_parser.add_argument(
        "--timezone",
        default="America/New_York",
        help="Timezone used for naive CSV timestamps. Default: America/New_York",
    )

    run_parser = subparsers.add_parser("run", help="Run MVP NAS100 5m backtest")
    run_parser.add_argument("paths", nargs="+", type=Path, help="CSV file(s) or directories")
    run_parser.add_argument("--output-dir", type=Path, default=Path("outputs/reports"))
    run_parser.add_argument("--timezone", default="America/New_York")
    run_parser.add_argument("--symbol", default="CAPITALCOM_NAS100")
    run_parser.add_argument("--timeframe", default="5m", choices=["3m", "5m"])
    run_parser.add_argument("--max-trades-per-day", type=int, help="Override configured daily trade limit")
    run_parser.add_argument("--reward-r", type=float, help="Override target reward multiple, e.g. 1, 2, 3, 4")
    run_parser.add_argument("--min-fvg-points", type=float, help="Minimum FVG/IFVG zone size in points")
    run_parser.add_argument(
        "--allow-entry-on-setup-candle",
        action="store_true",
        help="Allow entry fill on the same candle that forms the FVG/IFVG",
    )
    run_parser.add_argument(
        "--stop-model",
        choices=["sweep_wick", "cisd_body", "fvg_opposite_edge", "swing_based"],
        help="Initial stop model",
    )
    run_parser.add_argument(
        "--stop-management",
        choices=["none", "be_at_half_target", "half_stop_at_half_target"],
        help="Move stop after price reaches 50%% of target distance",
    )
    run_parser.add_argument(
        "--entry-mode",
        choices=["midpoint", "start", "quarter_25", "cisd_close", "body_end", "ote_62", "ote_705", "ote_79"],
        help="Entry model: FVG/IFVG midpoint, start, 25%% from start, body edge, or CISD close without waiting for FVG",
    )
    run_parser.add_argument("--direction", choices=["all", "long", "short"], help="Trade direction filter")
    run_parser.add_argument("--setup-type", choices=["all", "fvg", "ifvg", "body_fvg", "pd_array"], help="Setup type filter")
    run_parser.add_argument(
        "--allowed-weekdays",
        help="Comma-separated NY weekdays to trade, e.g. Monday,Wednesday,Friday",
    )
    run_parser.add_argument("--evaluation-start", help="Inclusive New York session date, YYYY-MM-DD")
    run_parser.add_argument("--evaluation-end", help="Inclusive New York session date, YYYY-MM-DD")
    run_parser.add_argument("--warmup-bars", type=int, default=0, help="Bars supplied for warmup before evaluation")
    run_parser.add_argument(
        "--allow-incomplete-evaluation",
        action="store_true",
        help="Research-only override; mark reports NON_PROMOTABLE when source coverage is incomplete",
    )

    calibrate_parser = subparsers.add_parser("calibrate", help="Analyze manual calibration examples")
    calibrate_parser.add_argument("--examples", type=Path, default=Path("calibration_examples"))
    calibrate_parser.add_argument("--raw", type=Path, default=Path("data/raw"))
    calibrate_parser.add_argument("--output", type=Path, default=Path("outputs/reports/calibration_analysis.csv"))

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "inspect":
        reports = inspect_paths(args.paths, timezone=args.timezone)
        print_reports(reports)
        if args.output:
            write_reports_csv(reports, args.output)
    elif args.command == "run":
        input_paths = filter_paths(args.paths, args.symbol, args.timeframe)
        market_data = load_ohlcv(input_paths, timezone=args.timezone)
        config = SYMBOL_CONFIGS.get((args.symbol, args.timeframe))
        if config is None:
            raise SystemExit(f"Desteklenmeyen symbol config: {args.symbol} {args.timeframe}")
        if args.max_trades_per_day is not None:
            if args.max_trades_per_day < 1:
                raise SystemExit("--max-trades-per-day en az 1 olmali")
            config = replace(config, max_trades_per_day=args.max_trades_per_day)
        if args.reward_r is not None:
            if args.reward_r <= 0:
                raise SystemExit("--reward-r pozitif olmali")
            config = replace(config, reward_r=args.reward_r)
        if args.min_fvg_points is not None:
            if args.min_fvg_points < 0:
                raise SystemExit("--min-fvg-points negatif olamaz")
            config = replace(config, min_fvg_points=args.min_fvg_points)
        if args.allow_entry_on_setup_candle:
            config = replace(config, allow_entry_on_setup_candle=True)
        if args.stop_model is not None:
            config = replace(config, stop_model=args.stop_model)
        if args.stop_management is not None:
            config = replace(config, stop_management=args.stop_management)
        if args.entry_mode is not None:
            config = replace(config, fvg_entry_mode=args.entry_mode)
        if args.direction is not None:
            config = replace(config, direction_filter=args.direction)
        if args.setup_type is not None:
            config = replace(config, setup_type_filter=args.setup_type)
        if args.allowed_weekdays is not None:
            weekdays = normalize_weekday_filter(args.allowed_weekdays)
            config = replace(config, allowed_weekdays=weekdays)
        if market_data.symbol != config.symbol or market_data.timeframe != config.timeframe:
            raise SystemExit(
                f"Veri/config uyumsuz: data={market_data.symbol} {market_data.timeframe}, "
                f"config={config.symbol} {config.timeframe}"
            )

        evaluation_window = None
        run_frame = market_data.frame
        if args.evaluation_start or args.evaluation_end or args.warmup_bars:
            if not args.evaluation_start or not args.evaluation_end:
                raise SystemExit("--evaluation-start ve --evaluation-end birlikte verilmeli")
            if args.warmup_bars < 0:
                raise SystemExit("--warmup-bars negatif olamaz")
            try:
                session_dates = pd.to_datetime(market_data.frame["time"], utc=True, format="mixed").dt.tz_convert(args.timezone).dt.date
                evaluation_window = EvaluationWindow(session_dates.min(), args.evaluation_start, args.evaluation_end, args.warmup_bars)
                run_frame, _ = evaluation_window.split_frame(market_data.frame)
            except (EvaluationWindowError, ValueError) as exc:
                raise SystemExit(str(exc)) from exc

        result = run_backtest(run_frame, config)
        selected_trades = result.trades
        if evaluation_window is not None:
            selected_trades = [trade for trade in result.trades if evaluation_window.includes_session(trade.date)]
            if len(selected_trades) != len(result.trades):
                # Warmup can affect indicator context but is never reportable.
                result.trades[:] = selected_trades
        selected_lifecycles = [
            lifecycle
            for lifecycle in result.lifecycles
            if evaluation_window is None or evaluation_window.includes_session(lifecycle.date)
        ]
        output_dir = args.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)

        trades = trades_to_frame(selected_trades)
        summary = summarize_trades(selected_trades)
        monthly = monthly_stats(selected_trades)
        weekdays = weekday_stats(selected_trades)
        parameters = pd.DataFrame([asdict(config)])
        skipped = pd.DataFrame({"date": sorted(str(day) for day in result.skipped_dates)})
        gaps = pd.DataFrame([event.__dict__ for event in result.gap_events])
        if not gaps.empty:
            gaps["prev_time"] = gaps["prev_time"].astype(str)
            gaps["time"] = gaps["time"].astype(str)
            gaps["skip_date"] = gaps["skip_date"].astype(str)

        trades.to_csv(output_dir / "trades.csv", index=False)
        summary.to_csv(output_dir / "summary.csv", index=False)
        monthly.to_csv(output_dir / "monthly_stats.csv", index=False)
        weekdays.to_csv(output_dir / "weekday_stats.csv", index=False)
        parameters.to_csv(output_dir / "parameters.csv", index=False)
        skipped.to_csv(output_dir / "skipped_dates.csv", index=False)
        gaps.to_csv(output_dir / "backtest_gap_events.csv", index=False)

        if evaluation_window is not None:
            source_dates = set(pd.to_datetime(run_frame["time"], utc=True, format="mixed").dt.tz_convert(args.timezone).dt.date)
            evaluated_dates = set(trades["date"]) if not trades.empty else set()
            planned_closed = {session for session in evaluation_window.evaluation_dates if session.weekday() >= 5}
            classification = classify_sessions(
                evaluation_window.evaluation_dates,
                source_dates=source_dates,
                planned_closed=planned_closed,
            )
            evaluated_dates = {
                session for session, state in classification.items() if state == "VALID"
            }
            incomplete = any(
                state in {"INVALID_DATA", "MISSING_SOURCE"}
                for state in classification.values()
            ) or not any(state == "VALID" for state in classification.values())
            coverage = coverage_report(
                classification,
                evaluated=evaluated_dates,
                non_promotable=incomplete,
            )
            if incomplete and not args.allow_incomplete_evaluation:
                raise SystemExit(
                    "Evaluation coverage incomplete (INVALID_DATA/MISSING_SOURCE); "
                    "use --allow-incomplete-evaluation only for research."
                )
            (output_dir / "run_manifest.json").write_text(
                json.dumps(
                    {
                        **evaluation_window.manifest_fields(
                            first_eligible_decision_time=min(
                                (
                                    timestamp
                                    for timestamp, session in zip(
                                        pd.to_datetime(run_frame["time"], utc=True, format="mixed"),
                                        pd.to_datetime(run_frame["time"], utc=True, format="mixed")
                                        .dt.tz_convert(args.timezone)
                                        .dt.date,
                                    )
                                    if evaluation_window.includes_session(session)
                                ),
                                default=None,
                            ),
                            evaluated_sessions=len(evaluated_dates),
                        ),
                        "coverage": coverage,
                        "promotable": not incomplete,
                        "status": "NON_PROMOTABLE" if incomplete else "PROMOTABLE",
                    },
                    sort_keys=True,
                    indent=2,
                ),
                encoding="utf-8",
            )
        closed = {"win", "loss", "loss_same_bar", "breakeven", "reduced_loss"}
        xray = build_risk_xray(
            trades[trades["result"].isin(closed)] if not trades.empty else trades,
            eligible_dates=None if evaluation_window is None else evaluation_window.evaluation_dates,
            funnel={"proposed": len(selected_lifecycles), "risk_approved": len(selected_trades), "staged": len(selected_trades), "filled": len(selected_trades), "rejected": 0, "expired": 0},
        )
        write_risk_xray(xray, output_dir)

        print(f"Loaded {len(market_data.frame)} candles: {market_data.symbol} {market_data.timeframe}")
        print(f"Skipped dates: {len(result.skipped_dates)}")
        print(f"Trades: {len(result.trades)}")
        print(summary.to_string(index=False))
    elif args.command == "calibrate":
        report = analyze_examples(args.examples, args.raw, args.output)
        print(f"Examples: {len(report)}")
        print(f"Wrote: {args.output}")
        columns = [
            "date",
            "timeframe",
            "direction",
            "vah_diff",
            "val_diff",
            "nearest_sweep_level",
            "entry_mode_guess",
            "tp_diff",
        ]
        print(report[columns].to_string(index=False))


def filter_paths(paths: list[Path], symbol: str, timeframe: str) -> list[Path]:
    filtered: list[Path] = []
    for path in paths:
        candidates = sorted(path.rglob("*.csv")) if path.is_dir() else [path]
        for candidate in candidates:
            if candidate.suffix.lower() != ".csv":
                continue
            candidate_symbol, candidate_timeframe = infer_symbol_timeframe(candidate)
            if candidate_symbol == symbol and candidate_timeframe == timeframe:
                filtered.append(candidate)
    if not filtered:
        raise SystemExit(f"CSV bulunamadi: {symbol} {timeframe}")
    return filtered


def normalize_weekday_filter(value: str) -> str:
    aliases = {
        "mon": "Monday",
        "monday": "Monday",
        "tue": "Tuesday",
        "tuesday": "Tuesday",
        "wed": "Wednesday",
        "wednesday": "Wednesday",
        "thu": "Thursday",
        "thursday": "Thursday",
        "fri": "Friday",
        "friday": "Friday",
    }
    weekdays: list[str] = []
    for raw_part in value.split(","):
        part = raw_part.strip().lower()
        if not part:
            continue
        if part not in aliases:
            raise SystemExit(f"Desteklenmeyen weekday: {raw_part}")
        weekday = aliases[part]
        if weekday not in weekdays:
            weekdays.append(weekday)
    if not weekdays:
        raise SystemExit("--allowed-weekdays bos olamaz")
    return ",".join(weekdays)


if __name__ == "__main__":
    main()
