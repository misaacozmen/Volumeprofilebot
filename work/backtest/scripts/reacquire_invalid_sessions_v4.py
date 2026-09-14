"""Reacquire only frozen invalid leg-day session windows from locked Dukascopy M1."""

from __future__ import annotations

import argparse
from hashlib import sha256
from pathlib import Path
import subprocess
import sys
import time

import pandas as pd


EXPECTED_INVENTORY_SHA256 = "a63406f235ded8d3daa123c0311adb678e53db3d996141b194493309f2cce075"
PRESETS = {"nq": "nq", "spx": "spx"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=Path("data/provenance/dukascopy_v4/reacquired_session"))
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--pause-seconds", type=float, default=5.0)
    args = parser.parse_args()
    if sha256(args.inventory.read_bytes()).hexdigest() != EXPECTED_INVENTORY_SHA256:
        raise SystemExit("frozen invalid-leg inventory hash mismatch")
    rows = pd.read_csv(args.inventory)[["date", "leg"]].drop_duplicates().sort_values(["date", "leg"])
    for index, row in enumerate(rows.itertuples(index=False), 1):
        date, leg = str(row.date), str(row.leg)
        output = args.output_root / f"{date}_{leg}"
        if list(output.glob("*.manifest.json")):
            continue
        end = (pd.Timestamp(date) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        print(f"[{index}/{len(rows)}] reacquiring {date}/{leg}", flush=True)
        command = [
                sys.executable, "scripts/download_dukascopy.py", "--preset", PRESETS[leg],
                "--from-date", date, "--to-date", end, "--timeframes", "3m" if leg == "nq" else "5m",
                "--session-context-hours", "6", "--keep-1m", "--raw-dir", str(output),
                "--chunk-days", "1", "--request-pause-seconds", "10",
                "--provenance-root", "data/provenance/dukascopy_v4",
            ]
        for attempt in range(1, args.attempts + 1):
            completed = subprocess.run(command, check=False)
            if completed.returncode == 0:
                break
            if attempt == args.attempts:
                raise SystemExit(f"strict reacquisition failed after {attempt} separate attempts: {date}/{leg}")
            time.sleep(args.pause_seconds * attempt)
        time.sleep(args.pause_seconds)


if __name__ == "__main__":
    main()
