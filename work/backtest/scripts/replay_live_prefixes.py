from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

import pandas as pd


SCRIPT_FILE = Path(__file__).resolve()
ROOT = SCRIPT_FILE.parents[1]
if not (ROOT / "scripts").is_dir() and (SCRIPT_FILE.parent / "app" / "scripts").is_dir():
    ROOT = SCRIPT_FILE.parent / "app"
sys.path.insert(0, str(ROOT / "scripts"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay historical live prefixes from a production bar cache.")
    parser.add_argument("--profile", choices=("forward", "super1"), required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--date", action="append", required=True)
    args = parser.parse_args()
    if args.output_root.exists():
        raise SystemExit(f"diagnostic output already exists: {args.output_root}")
    args.output_root.mkdir(parents=True)

    if args.profile == "forward":
        import run_xm_mt5_forward as harness
    else:
        import run_super1_xm_mt5_forward as harness
    harness.configure_core()
    core = harness.core
    if not environment_value("XM_MT5_SERVER"):
        raise SystemExit("XM_MT5_SERVER is required for the diagnostic campaign lock")

    store = core.BarStore(args.data_root.resolve())
    results = []
    try:
        for value in args.date:
            trade_date = pd.Timestamp(value).date()
            replay_time = pd.Timestamp(f"{trade_date} 12:00", tz=core.TZ)
            result = core.run_prefix(args.output_root.resolve(), store, replay_time, replay_time.tz_convert(core.UTC))
            record = core.read_json(Path(result["path"])) if result.get("path") else {}
            results.append(
                {
                    "date": str(trade_date),
                    "state": result.get("state"),
                    "issues": {
                        leg: record.get("data_gates", {}).get(leg, {}).get("issues", [])
                        for leg in core.LEG_ORDER
                    },
                    "decisions": [
                        {
                            "leg_key": item.get("leg_key"),
                            "final_decision": item.get("final_decision"),
                            "direction": item.get("direction"),
                            "setup_state": item.get("setup_state"),
                            "order_state": item.get("order_state"),
                            "terminal_reason": item.get("terminal_reason"),
                        }
                        for item in record.get("payload", {}).get("decisions", [])
                    ],
                }
            )
    finally:
        store.close()
    print(json.dumps({"profile": args.profile, "results": results}, indent=2, sort_keys=True))
    return 0 if all(item["state"] == "VALID" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
from backtest.live.settings import environment_value
