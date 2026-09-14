"""Audit all authoritative V4 invalid leg-days without publishing data."""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from hashlib import sha256
import json
from pathlib import Path
import sys
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.dukascopy_acquisition import AtomicJsonStore  # noqa: E402


INVENTORY_SHA256 = "a63406f235ded8d3daa123c0311adb678e53db3d996141b194493309f2cce075"
TARGET_COUNT = 113
SYMBOLS = {"nq": ("usatechidxusd", "DUKASCOPY_USATECHIDXUSD", "3m"), "spx": ("usa500idxusd", "DUKASCOPY_USA500IDXUSD", "5m")}
EXPECTED_COLUMNS = ("time", "open", "high", "low", "close", "volume")


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _read_frame(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    volume_name = "volume" if "volume" in frame.columns else "Volume"
    if not set((*EXPECTED_COLUMNS[:5], volume_name)).issubset(frame.columns):
        raise ValueError(f"CSV schema is incomplete: {path.name}")
    frame = frame.rename(columns={volume_name: "volume"})
    frame = frame[list(EXPECTED_COLUMNS)].copy()
    frame["time"] = pd.to_datetime(frame["time"], utc=True, format="mixed")
    for column in EXPECTED_COLUMNS[1:]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if frame.isna().any().any() or not frame["time"].is_monotonic_increasing or frame["time"].duplicated().any():
        raise ValueError(f"CSV contains NaN, duplicate or unsorted rows: {path.name}")
    if (frame["high"] < frame[["open", "close", "low"]].max(axis=1)).any() or (frame["low"] > frame[["open", "close", "high"]].min(axis=1)).any():
        raise ValueError(f"CSV OHLC invariant failed: {path.name}")
    if (frame["volume"] < 0).any():
        raise ValueError(f"CSV volume invariant failed: {path.name}")
    return frame


def semantic_hash(frame: pd.DataFrame) -> str:
    canonical = frame[list(EXPECTED_COLUMNS)].copy()
    canonical["time"] = canonical["time"].map(lambda value: pd.Timestamp(value).isoformat())
    return sha256(canonical.to_csv(index=False, lineterminator="\n").encode("utf-8")).hexdigest()


def legacy_semantic_hash(path: Path) -> str:
    raw = pd.read_csv(path)
    volume_name = "volume" if "volume" in raw.columns else "Volume"
    raw = raw.rename(columns={volume_name: "volume"})[list(EXPECTED_COLUMNS)].copy()
    raw["time"] = raw["time"].map(lambda value: pd.Timestamp(value).isoformat())
    return sha256(raw.to_csv(index=False, lineterminator="\n").encode("utf-8")).hexdigest()


def _resample(frame: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    minutes = int(timeframe.removesuffix("m"))
    return (
        frame.set_index("time")
        .resample(f"{minutes}min", label="left", closed="left")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna(subset=["open", "high", "low", "close"])
        .reset_index()
    )


def _safe_child(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("bundle path escapes provenance root") from exc
    return resolved


def verify_bundle(row: dict[str, Any], *, bundle_root: Path, require_committed: bool = False) -> dict[str, Any]:
    date_text = str(row["date"])
    leg = str(row["leg"])
    timeframe = str(row["timeframe"])
    symbol_key, symbol, expected_timeframe = SYMBOLS.get(leg, ("", "", ""))
    directory = _safe_child(bundle_root / f"{date_text}_{leg}", bundle_root)
    if not directory.is_dir():
        return {"state": "MISSING_BUNDLE", "target": {"date": date_text, "leg": leg, "timeframe": timeframe}}
    try:
        manifest_paths = sorted(directory.glob(f"{symbol}, {timeframe}_*.csv.manifest.json"))
        if len(manifest_paths) != 1 or timeframe != expected_timeframe:
            raise ValueError("bundle manifest filename/target mismatch")
        manifest_path = manifest_paths[0]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or int(manifest.get("schema_version", 0)) != 4:
            raise ValueError("bundle manifest schema is not V4")
        to_date = (date.fromisoformat(date_text) + timedelta(days=1)).isoformat()
        derived_name = f"{symbol}, {timeframe}_{date_text}_{to_date}.csv"
        derived_path = _safe_child(directory / derived_name, bundle_root)
        minute_path = _safe_child(directory / f"{symbol}, 1m_{date_text}_{to_date}.csv", bundle_root)
        if not derived_path.is_file() or not minute_path.is_file():
            raise ValueError("bundle is missing 1m or derived CSV")
        if manifest.get("output_path", "").split("/")[-1] != derived_name:
            raise ValueError("manifest output filename does not match target")
        if manifest.get("instrument") != symbol_key or manifest.get("symbol") != symbol or manifest.get("side") != "BID" or manifest.get("source_granularity") != "M1":
            raise ValueError("bundle provider identity is not exact")
        if int(manifest.get("session_context_hours", -1)) != 6:
            raise ValueError("bundle does not contain the required six-hour context")
        raw_chunks = manifest.get("raw_chunks")
        if not isinstance(raw_chunks, list) or not raw_chunks:
            raise ValueError("bundle raw CAS evidence is missing")
        provenance_root = ROOT / "data/provenance/dukascopy_v4"
        raw_hashes = []
        for chunk in raw_chunks:
            raw_path = _safe_child(ROOT / str(chunk.get("raw_path") or ""), provenance_root)
            if not raw_path.is_file() or digest(raw_path) != str(chunk.get("raw_sha256")) or raw_path.stat().st_size != int(chunk.get("raw_byte_count", -1)):
                raise ValueError("bundle raw CAS evidence is invalid")
            raw_hashes.append(digest(raw_path))
        minute = _read_frame(minute_path)
        derived = _read_frame(derived_path)
        required_start = pd.Timestamp(str(row["required_start"])).tz_convert("UTC")
        required_end = pd.Timestamp(str(row["required_end"])).tz_convert("UTC")
        expected_grid = pd.date_range(required_start, required_end, freq=f"{int(timeframe.removesuffix('m'))}min")
        observed_grid = derived.loc[(derived["time"] >= required_start) & (derived["time"] <= required_end), "time"]
        if len(observed_grid) != len(expected_grid) or list(observed_grid) != list(expected_grid):
            raise ValueError("official session bar grid is incomplete")
        regenerated = _resample(minute, timeframe)
        regenerated = regenerated[regenerated["time"].isin(derived["time"])].reset_index(drop=True)
        comparable = derived[derived["time"].isin(regenerated["time"])].reset_index(drop=True)
        if semantic_hash(regenerated) != semantic_hash(comparable):
            raise ValueError("derived timeframe is not deterministic from 1m")
        derived_semantic_sha = semantic_hash(derived)
        if manifest.get("derived_sha256") not in {digest(derived_path), derived_semantic_sha, legacy_semantic_hash(derived_path)}:
            raise ValueError("derived semantic/byte SHA mismatch")
        committed_path = directory / "COMMITTED.json"
        if require_committed and not committed_path.is_file():
            raise ValueError("V5 bundle is not atomically committed")
        if committed_path.is_file():
            committed = json.loads(committed_path.read_text(encoding="utf-8"))
            if committed.get("manifest_sha256") != digest(manifest_path) or committed.get("derived_sha256") != digest(derived_path) or committed.get("minute_sha256") != digest(minute_path):
                raise ValueError("COMMITTED.json does not attest every bundle byte")
        return {
            "state": "VERIFIED_V5_BUNDLE" if committed_path.is_file() else "VERIFIED_EXISTING_BUNDLE",
            "target": {"date": date_text, "leg": leg, "timeframe": timeframe},
            "bundle_path": directory.relative_to(ROOT).as_posix(),
            "manifest_sha256": digest(manifest_path),
            "minute_sha256": digest(minute_path),
            "derived_sha256": digest(derived_path),
            "minute_semantic_sha256": semantic_hash(minute),
            "derived_semantic_sha256": derived_semantic_sha,
            "raw_sha256": raw_hashes,
        }
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return {"state": "INVALID_BUNDLE", "target": {"date": date_text, "leg": leg, "timeframe": timeframe}, "error": str(exc)}


def audit_inventory(inventory_path: Path, bundle_root: Path, report_root: Path) -> dict[str, Any]:
    if digest(inventory_path) != INVENTORY_SHA256:
        raise ValueError("authoritative inventory hash mismatch")
    inventory = pd.read_csv(inventory_path).to_dict(orient="records")
    keys = {(str(item["date"]), str(item["leg"]), str(item["timeframe"])) for item in inventory}
    if len(inventory) != TARGET_COUNT or len(keys) != TARGET_COUNT:
        raise ValueError("authoritative inventory must contain exactly 113 unique targets")
    rows = [verify_bundle(item, bundle_root=bundle_root, require_committed=False) for item in inventory]
    evidence_path = ROOT / "data/provenance/dukascopy_v4/acquisition_v5/superseded_failure_evidence.json"
    if evidence_path.is_file():
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        superseded = {(str(item.get("canonical_target", {}).get("date")), str(item.get("canonical_target", {}).get("leg")), str(item.get("canonical_target", {}).get("timeframe"))) for item in evidence.get("entries", [])}
        for item, audit in zip(inventory, rows):
            key = (str(item["date"]), str(item["leg"]), str(item["timeframe"]))
            if audit["state"] == "MISSING_BUNDLE" and key in superseded:
                audit["state"] = "SUPERSEDED_EVIDENCE"
    residual = [item for item in rows if item["state"] not in {"VERIFIED_EXISTING_BUNDLE", "VERIFIED_V5_BUNDLE"}]
    report_root.mkdir(parents=True, exist_ok=True)
    inventory_payload = {
        "schema_version": 1,
        "inventory_path": inventory_path.relative_to(ROOT).as_posix(),
        "inventory_sha256": INVENTORY_SHA256,
        "target_count": len(inventory),
        "unique_target_count": len(keys),
        "distribution": {f"{leg}/{timeframe}": sum(1 for item in inventory if item["leg"] == leg and item["timeframe"] == timeframe) for leg, timeframe in (("nq", "3m"), ("spx", "5m"))},
        "contains_2025_04_16_nq_3m": ("2025-04-16", "nq", "3m") in keys,
    }
    (report_root / "inventory_audit.json").write_text(json.dumps(inventory_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (report_root / "bundle_audit.json").write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    pd.DataFrame([item | {"missing_intervals": item.get("missing_intervals", "")} for item in inventory if (str(item["date"]), str(item["leg"]), str(item["timeframe"])) in {(str(row["target"]["date"]), str(row["target"]["leg"]), str(row["target"]["timeframe"])) for row in residual}]).to_csv(report_root / "residual_leg_days.csv", index=False)
    result = {"inventory": inventory_payload, "bundles": rows, "residual": residual, "residual_count": len(residual)}
    if not residual:
        manifest = {
            "schema_version": 5,
            "inventory_sha256": INVENTORY_SHA256,
            "target_count": TARGET_COUNT,
            "residual_count": 0,
            "targets": rows,
        }
        output = ROOT / "data/provenance/dukascopy_v4/acquisition_v5/reacquisition_manifest_v5.json"
        AtomicJsonStore(output).write(manifest)
        result["manifest_path"] = output.relative_to(ROOT).as_posix()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, default=ROOT / "data/provenance/dukascopy_v4/frozen_invalid_leg_days_v4.csv")
    parser.add_argument("--bundle-root", type=Path, default=ROOT / "data/provenance/dukascopy_v4/reacquired_session")
    parser.add_argument("--report-root", type=Path, default=ROOT / "outputs/reports/dukascopy_reacquisition_v5")
    args = parser.parse_args()
    result = audit_inventory(args.inventory.resolve(), args.bundle_root.resolve(), args.report_root.resolve())
    print(json.dumps({"target_count": TARGET_COUNT, "residual_count": result["residual_count"]}, sort_keys=True))
    if result["residual_count"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
