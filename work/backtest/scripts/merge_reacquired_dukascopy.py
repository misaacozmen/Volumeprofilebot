"""Replace only verified missing dates with fresh, same-provider Dukascopy bars."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path

import pandas as pd


PROVIDER = "DUKASCOPY_BID"
REQUIRED_COLUMNS = ("time", "open", "high", "low", "close", "Volume")


def file_sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def parse_dates(value: str) -> set[str]:
    dates = {item.strip() for item in value.split(",") if item.strip()}
    if not dates:
        raise SystemExit("--dates must contain at least one ISO date")
    parsed = pd.to_datetime(sorted(dates), format="%Y-%m-%d", errors="coerce")
    if parsed.isna().any():
        raise SystemExit("--dates must contain only ISO dates")
    return dates


def merge(existing_path: Path, reacquired_path: Path, output_path: Path, dates: set[str]) -> Path:
    existing = pd.read_csv(existing_path)
    reacquired = pd.read_csv(reacquired_path)
    for name, frame in (("existing", existing), ("reacquired", reacquired)):
        missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
        if missing:
            raise ValueError(f"{name} dataset is missing columns: {missing}")
    existing_times = pd.to_datetime(existing["time"], utc=True, format="mixed", errors="coerce")
    reacquired_times = pd.to_datetime(reacquired["time"], utc=True, format="mixed", errors="coerce")
    if existing_times.isna().any() or reacquired_times.isna().any():
        raise ValueError("dataset contains an invalid timestamp")
    existing_dates = existing_times.dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")
    reacquired_dates = reacquired_times.dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")
    absent = sorted(date for date in dates if not (reacquired_dates == date).any())
    if absent:
        raise ValueError(f"same-provider reacquisition did not return requested dates: {absent}")
    replacement = reacquired.loc[reacquired_dates.isin(dates), list(REQUIRED_COLUMNS)].copy()
    retained = existing.loc[~existing_dates.isin(dates), list(REQUIRED_COLUMNS)].copy()
    merged = pd.concat([retained, replacement], ignore_index=True)
    merged_times = pd.to_datetime(merged["time"], utc=True, format="mixed", errors="coerce")
    if merged_times.isna().any() or merged_times.duplicated().any():
        raise ValueError("merged dataset has invalid or duplicate timestamps")
    merged = merged.assign(_sort_time=merged_times).sort_values("_sort_time", kind="mergesort").drop(columns="_sort_time")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(output_path, index=False)
    metadata = {
        "schema_version": 1,
        "provider": PROVIDER,
        "synthetic_bars": False,
        "replaced_local_dates": sorted(dates),
        "existing_source_sha256": file_sha256(existing_path),
        "reacquired_source_sha256": file_sha256(reacquired_path),
        "output_sha256": file_sha256(output_path),
        "row_count": len(merged),
    }
    output_path.with_suffix(output_path.suffix + ".manifest.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--existing", type=Path, required=True)
    parser.add_argument("--reacquired", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dates", required=True, help="comma-separated local New York dates")
    args = parser.parse_args()
    print(merge(args.existing, args.reacquired, args.output, parse_dates(args.dates)))


if __name__ == "__main__":
    main()
