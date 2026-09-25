from __future__ import annotations

import argparse
from datetime import date, datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
from urllib.parse import urlparse

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.data_inspector import infer_symbol_timeframe
from backtest.data_loader import load_ohlcv


MANIFEST_NAME = "engine_audit_manifest.json"
EXPECTED_DATASETS = {
    "nq": ("DUKASCOPY_USATECHIDXUSD", "3m"),
    "spx": ("DUKASCOPY_USA500IDXUSD", "5m"),
}
EXPECTED_WINDOW = {
    "timezone": "America/New_York",
    "start_inclusive": "2025-01-25T00:00:00-05:00",
    "end_exclusive": "2025-04-04T00:00:00-04:00",
}
FIRST_REQUIRED_MARKET_DATE = date(2025, 1, 27)
LAST_REQUIRED_MARKET_DATE = date(2025, 4, 3)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_manifest(document: object) -> list[dict[str, object]]:
    if not isinstance(document, dict) or document.get("schema") != "engine-audit-inputs-v1":
        raise RuntimeError("ENGINE_AUDIT_MANIFEST_INVALID: schema must be engine-audit-inputs-v1")
    source = document.get("source")
    if not isinstance(source, dict):
        raise RuntimeError("ENGINE_AUDIT_MANIFEST_INVALID: source metadata is required")
    if str(source.get("provider", "")).strip().casefold() != "dukascopy":
        raise RuntimeError("ENGINE_AUDIT_MANIFEST_INVALID: provider must identify Dukascopy")
    parsed_url = urlparse(str(source.get("source_url", "")))
    if (
        parsed_url.scheme != "https"
        or not parsed_url.hostname
        or not (
            parsed_url.hostname.casefold() == "dukascopy.com"
            or parsed_url.hostname.casefold().endswith(".dukascopy.com")
        )
    ):
        raise RuntimeError("ENGINE_AUDIT_MANIFEST_INVALID: source_url must reference the official Dukascopy domain")
    try:
        observed_at = datetime.fromisoformat(str(source.get("observed_at_utc", "")).replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("ENGINE_AUDIT_MANIFEST_INVALID: observed_at_utc must be an ISO-8601 timestamp") from exc
    if observed_at.tzinfo is None or observed_at.utcoffset() is None or observed_at.utcoffset().total_seconds() != 0:
        raise RuntimeError("ENGINE_AUDIT_MANIFEST_INVALID: observed_at_utc must be UTC")
    if not str(source.get("provenance_note", "")).strip():
        raise RuntimeError("ENGINE_AUDIT_MANIFEST_INVALID: provenance_note must disclose source-record limits")

    if document.get("window") != EXPECTED_WINDOW:
        raise RuntimeError("ENGINE_AUDIT_WINDOW_INVALID: expected the declared Feb–Mar 2025 window with audit warmup")

    datasets = document.get("datasets")
    if not isinstance(datasets, list):
        raise RuntimeError("ENGINE_AUDIT_MANIFEST_INVALID: datasets must be a list")
    by_key = {str(item.get("leg_key")): item for item in datasets if isinstance(item, dict)}
    if set(by_key) != set(EXPECTED_DATASETS) or len(by_key) != len(datasets):
        raise RuntimeError("ENGINE_AUDIT_DATASETS_INVALID: manifest must bind exactly the NQ 3m and SPX 5m datasets")

    files: list[dict[str, object]] = []
    seen_paths: set[str] = set()
    seen_stage_paths: set[str] = set()
    for key, (expected_symbol, expected_timeframe) in EXPECTED_DATASETS.items():
        dataset = by_key[key]
        if dataset.get("symbol") != expected_symbol or dataset.get("timeframe") != expected_timeframe:
            raise RuntimeError(f"ENGINE_AUDIT_DATASET_INVALID: {key} symbol/timeframe mismatch")
        entries = dataset.get("files")
        if not isinstance(entries, list) or not entries:
            raise RuntimeError(f"ENGINE_AUDIT_INPUTS_MISSING: manifest has no files for {key}")
        for entry in entries:
            if not isinstance(entry, dict):
                raise RuntimeError(f"ENGINE_AUDIT_MANIFEST_INVALID: malformed file entry for {key}")
            relative = str(entry.get("path", ""))
            posix = PurePosixPath(relative)
            if (
                not relative
                or posix.is_absolute()
                or ".." in posix.parts
                or "\\" in relative
                or ":" in relative
                or posix.suffix.casefold() != ".csv"
            ):
                raise RuntimeError(f"ENGINE_AUDIT_MANIFEST_INVALID: unsafe CSV path for {key}")
            if relative in seen_paths:
                raise RuntimeError("ENGINE_AUDIT_MANIFEST_INVALID: duplicate input path")
            seen_paths.add(relative)
            expected_hash = str(entry.get("sha256", "")).lower()
            if not SHA256_RE.fullmatch(expected_hash):
                raise RuntimeError(f"ENGINE_AUDIT_MANIFEST_INVALID: invalid SHA-256 for {key} input")
            filename_symbol, filename_timeframe = infer_symbol_timeframe(Path(posix.name))
            if (filename_symbol, filename_timeframe) != (expected_symbol, expected_timeframe):
                raise RuntimeError(f"ENGINE_AUDIT_DATASET_INVALID: filename identity mismatch for {key}")
            stage_relative = f"{key}/{posix.name}"
            if stage_relative in seen_stage_paths:
                raise RuntimeError("ENGINE_AUDIT_MANIFEST_INVALID: duplicate staged filename")
            seen_stage_paths.add(stage_relative)
            files.append({"leg_key": key, "path": relative, "sha256": expected_hash})
    return files


def prepare(source_root: Path, stage_root: Path) -> dict[str, object]:
    source_root = source_root.resolve(strict=True)
    if not source_root.is_dir():
        raise RuntimeError("ENGINE_AUDIT_INPUTS_MISSING: source root is not a directory")
    manifest_path = source_root / MANIFEST_NAME
    if not manifest_path.is_file():
        raise RuntimeError(f"ENGINE_AUDIT_INPUT_MANIFEST_REQUIRED: missing {MANIFEST_NAME}")
    manifest_bytes = manifest_path.read_bytes()
    try:
        document = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("ENGINE_AUDIT_MANIFEST_INVALID: manifest must be UTF-8 JSON") from exc

    entries = validate_manifest(document)
    listed = {str(item["path"]) for item in entries}
    actual_csv = {
        path.relative_to(source_root).as_posix()
        for path in source_root.rglob("*.csv")
        if path.is_file()
    }
    if actual_csv != listed:
        raise RuntimeError("ENGINE_AUDIT_INPUT_SET_MISMATCH: source CSV set differs from the hash manifest")

    grouped: dict[str, list[Path]] = {key: [] for key in EXPECTED_DATASETS}
    sources: dict[str, Path] = {}
    for entry in entries:
        relative = str(entry["path"])
        source = (source_root / Path(*PurePosixPath(relative).parts)).resolve(strict=True)
        if not source.is_file() or source_root not in source.parents:
            raise RuntimeError("ENGINE_AUDIT_INPUT_PATH_INVALID: listed input must remain inside source root")
        if sha256(source) != entry["sha256"]:
            raise RuntimeError(f"ENGINE_AUDIT_INPUT_HASH_MISMATCH: {relative}")
        grouped[str(entry["leg_key"])].append(source)
        sources[relative] = source

    # Validate actual bars after all input hashes, identities, and declared window bounds pass.
    for key, (symbol, timeframe) in EXPECTED_DATASETS.items():
        market = load_ohlcv(grouped[key])
        if (market.symbol, market.timeframe) != (symbol, timeframe):
            raise RuntimeError(f"ENGINE_AUDIT_DATASET_INVALID: parsed identity mismatch for {key}")
        first_day = pd.Timestamp(market.frame["time"].min()).date()
        last_day = pd.Timestamp(market.frame["time"].max()).date()
        if first_day > FIRST_REQUIRED_MARKET_DATE or last_day < LAST_REQUIRED_MARKET_DATE:
            raise RuntimeError(
                f"ENGINE_AUDIT_WINDOW_DATA_MISSING: {key} actual bars do not cover the audit warmup and tail"
            )

    stage_root = stage_root.resolve()
    if stage_root == source_root or source_root in stage_root.parents or stage_root in source_root.parents:
        raise RuntimeError("ENGINE_AUDIT_STAGE_INVALID: stage root must be separate from source root")
    if stage_root.exists() and any(stage_root.iterdir()):
        raise RuntimeError("ENGINE_AUDIT_STAGE_INVALID: stage root must be empty")
    stage_root.mkdir(parents=True, exist_ok=True)
    staged_hashes: dict[str, str] = {}
    for entry in entries:
        relative = str(entry["path"])
        key = str(entry["leg_key"])
        target = stage_root / key / Path(PurePosixPath(relative).name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(sources[relative], target)
        staged_hashes[target.relative_to(stage_root).as_posix()] = sha256(target)
    (stage_root / MANIFEST_NAME).write_bytes(manifest_bytes)
    return {
        "status": "PASS",
        "schema": "engine-audit-inputs-v1",
        "source_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "source_provider": document["source"]["provider"],
        "source_url": document["source"]["source_url"],
        "source_observed_at_utc": document["source"]["observed_at_utc"],
        "source_provenance_note": document["source"]["provenance_note"],
        "window": EXPECTED_WINDOW,
        "input_count": len(entries),
        "staged_input_sha256": dict(sorted(staged_hashes.items())),
        "stage_root": str(stage_root),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify and stage distinct 2025 engine-audit inputs.")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--stage-root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.source_root, args.stage_root), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
