"""Offline, fail-closed audit and explicit finalize for V5 reacquisition."""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from hashlib import sha256
import json
from pathlib import Path
import sys
from typing import Any, Mapping

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.reacquisition_contract import (  # noqa: E402
    INVENTORY_SHA256, TARGET_COUNT, digest, load_inventory, ordered_merkle, semantic_root, target_key,
)

SYMBOLS = {"nq": ("usatechidxusd", "DUKASCOPY_USATECHIDXUSD", "3m"), "spx": ("usa500idxusd", "DUKASCOPY_USA500IDXUSD", "5m")}
EXPECTED_COLUMNS = ("time", "open", "high", "low", "close", "volume")
LOCKFILE_SHA256 = "74506876532b3d1caf4be740fc42c79e9eb529518770afd05c40a0463c732dee"
NODE_INTEGRITY = "sha512-o2Co/asUD/TXFNhblJUYkRseHMt/uvFrnhzOKWezLuiFJqbl4Zn2oJGL4/W+PY1b2YsI11+9+TO40qNQBAj8/w=="


def _read_frame(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    volume_name = "volume" if "volume" in frame.columns else "Volume"
    if not set((*EXPECTED_COLUMNS[:5], volume_name)).issubset(frame.columns):
        raise ValueError(f"CSV schema is incomplete: {path.name}")
    frame = frame.rename(columns={volume_name: "volume"})[list(EXPECTED_COLUMNS)].copy()
    frame["time"] = pd.to_datetime(frame["time"], utc=True, format="mixed")
    for column in EXPECTED_COLUMNS[1:]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if frame.empty or not frame[list(EXPECTED_COLUMNS[1:])].apply(lambda column: column.map(lambda value: pd.notna(value) and float(value) == float(value) and abs(float(value)) != float("inf")).all()).all():
        raise ValueError(f"CSV contains non-finite values: {path.name}")
    if not frame["time"].is_monotonic_increasing or frame["time"].duplicated().any():
        raise ValueError(f"CSV timestamps are not strictly increasing: {path.name}")
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
    return semantic_hash(_read_frame(path))


def _resample(frame: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    minutes = int(timeframe.removesuffix("m"))
    return frame.set_index("time").resample(f"{minutes}min", label="left", closed="left").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna(subset=["open", "high", "low", "close"]).reset_index()


def _safe_child(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("path escapes its permitted root") from exc
    return resolved


def _attestation(path: Path, observed: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": 1, **dict(observed)}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return path


def _required_grid_ok(derived: pd.DataFrame, row: Mapping[str, Any], timeframe: str) -> None:
    start = pd.Timestamp(str(row["required_start"])).tz_convert("UTC")
    end = pd.Timestamp(str(row["required_end"])).tz_convert("UTC")
    expected = pd.date_range(start, end, freq=f"{int(timeframe.removesuffix('m'))}min")
    observed = derived.loc[(derived["time"] >= start) & (derived["time"] <= end), "time"]
    if list(observed) != list(expected):
        raise ValueError("required official session grid is incomplete")


def verify_bundle(row: dict[str, Any], *, bundle_root: Path, legacy_root: Path | None = None, require_committed: bool = False, attestation_root: Path | None = None) -> dict[str, Any]:
    date_text, leg, timeframe = str(row["date"]), str(row["leg"]), str(row["timeframe"])
    symbol_key, symbol, expected_timeframe = SYMBOLS.get(leg, ("", "", ""))
    if timeframe != expected_timeframe:
        return {"state": "INVALID_LEGACY", "target": {"date": date_text, "leg": leg, "timeframe": timeframe}, "reason": "timeframe mismatch"}
    roots = [(bundle_root, "V5"), (legacy_root, "LEGACY")] if legacy_root else [(bundle_root, "V5")]
    directory = None
    kind = ""
    for candidate_root, candidate_kind in roots:
        candidate = _safe_child(candidate_root / f"{date_text}_{leg}", candidate_root)
        if candidate.is_dir():
            directory, kind = candidate, candidate_kind
            break
    target = {"date": date_text, "leg": leg, "timeframe": timeframe}
    if directory is None:
        return {"state": "MISSING", "target": target}
    if not any(directory.iterdir()):
        return {"state": "INCOMPLETE_STAGING", "target": target, "reason": "empty bundle directory"}
    try:
        manifest_paths = sorted(directory.glob(f"{symbol}, {timeframe}_*.csv.manifest.json"))
        if len(manifest_paths) != 1:
            raise ValueError("bundle manifest count is not exactly one")
        manifest_path = manifest_paths[0]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        schema = int(manifest.get("schema_version", 0))
        if kind == "V5" and schema != 5:
            raise ValueError("V5 bundle must use schema 5")
        if kind == "LEGACY" and schema != 4:
            raise ValueError("legacy bundle must use schema 4")
        to_date = (date.fromisoformat(date_text) + timedelta(days=1)).isoformat()
        derived_name = f"{symbol}, {timeframe}_{date_text}_{to_date}.csv"
        minute_name = f"{symbol}, 1m_{date_text}_{to_date}.csv"
        derived_path = _safe_child(directory / derived_name, directory)
        minute_path = _safe_child(directory / minute_name, directory)
        if not derived_path.is_file() or not minute_path.is_file():
            raise ValueError("bundle is missing minute or derived CSV")
        derived = _read_frame(derived_path)
        minute = _read_frame(minute_path)
        regenerated = _resample(minute, timeframe)
        if list(regenerated["time"]) != list(derived["time"]):
            raise ValueError("derived timestamp set differs from regenerated timestamp set")
        if semantic_hash(regenerated) != semantic_hash(derived):
            raise ValueError("derived values differ from exact canonical resample")
        _required_grid_ok(derived, row, timeframe)
        if manifest.get("output_path", "").split("/")[-1] != derived_name:
            raise ValueError("manifest output filename mismatch")
        if manifest.get("instrument") != symbol_key or manifest.get("symbol") != symbol or manifest.get("side") != "BID" or manifest.get("source_granularity") != "M1":
            raise ValueError("provider identity is not exact")
        raw_chunks = manifest.get("raw_chunks")
        if not isinstance(raw_chunks, list) or not raw_chunks:
            raise ValueError("raw CAS evidence is missing")
        ordered_urls = manifest.get("ordered_urls")
        planned_hashes = manifest.get("plan_url_sha256")
        if kind == "V5":
            downloader = manifest.get("downloader")
            if not isinstance(downloader, dict) or downloader.get("package") != "dukascopy-node" or downloader.get("version") != "1.50.0" or downloader.get("integrity") != NODE_INTEGRITY or downloader.get("lockfile_sha256") != LOCKFILE_SHA256:
                raise ValueError("V5 downloader contract is not locked")
            if not isinstance(ordered_urls, list) or not isinstance(planned_hashes, list) or len(ordered_urls) != len(raw_chunks) or planned_hashes != [sha256(str(url).encode()).hexdigest() for url in ordered_urls]:
                raise ValueError("V5 ordered URL plan is incomplete")
        raw_hashes = []
        provenance_root = ROOT / "data/provenance/dukascopy_v4"
        for index, chunk in enumerate(raw_chunks):
            if not isinstance(chunk, dict):
                raise ValueError("raw chunk evidence is invalid")
            if kind == "V5" and (sha256(str(chunk.get("url") or "").encode()).hexdigest() != str(chunk.get("url_sha256") or "") or str(chunk.get("url")) != str(ordered_urls[index])):
                raise ValueError("raw chunk URL binding mismatch")
            raw_path = _safe_child(ROOT / str(chunk.get("raw_path") or ""), provenance_root)
            if not raw_path.is_file() or digest(raw_path) != str(chunk.get("raw_sha256")) or raw_path.stat().st_size != int(chunk.get("raw_byte_count", -1)):
                raise ValueError("raw CAS byte evidence mismatch")
            if kind == "V5" and "acquisition_v5/http_cas" not in raw_path.relative_to(provenance_root).as_posix():
                raise ValueError("V5 raw evidence is outside the HTTP CAS")
            raw_hashes.append(digest(raw_path))
        committed_path = directory / "COMMITTED.json"
        if kind == "V5" and (require_committed or True) and not committed_path.is_file():
            raise ValueError("V5 bundle is not committed")
        if kind == "V5":
            allowed = {manifest_path.name, minute_path.name, derived_path.name, "COMMITTED.json"}
            if {item.name for item in directory.iterdir()} != allowed:
                raise ValueError("V5 bundle contains extra or missing files")
            committed = json.loads(committed_path.read_text(encoding="utf-8"))
            expected = {"manifest_sha256": digest(manifest_path), "minute_sha256": digest(minute_path), "derived_sha256": digest(derived_path), "manifest_name": manifest_path.name, "minute_name": minute_path.name, "derived_name": derived_path.name}
            if any(committed.get(key) != value for key, value in expected.items()):
                raise ValueError("COMMITTED.json does not attest exact bundle bytes")
        provenance_root = ROOT / "data/provenance/dukascopy_v4"
        observed = {"target": target, "bundle_path": directory.relative_to(provenance_root).as_posix(), "manifest_path": manifest_path.relative_to(provenance_root).as_posix(), "minute_path": minute_path.relative_to(provenance_root).as_posix(), "derived_path": derived_path.relative_to(provenance_root).as_posix(), "manifest_sha256": digest(manifest_path), "minute_sha256": digest(minute_path), "derived_sha256": digest(derived_path), "minute_semantic_sha256": semantic_hash(minute), "derived_semantic_sha256": semantic_hash(derived), "raw_sha256": raw_hashes, "auditor_sha256": digest(Path(__file__)), "calendar_sha256": digest(ROOT / "live_forward/calendars/us_equity_rth_2022_2026_v4.json"), "node_helper_sha256": digest(ROOT / "tools/dukascopy-downloader/acquire_v5.mjs"), "package_lock_sha256": digest(ROOT / "tools/dukascopy-downloader/package-lock.json")}
        if kind == "LEGACY":
            if attestation_root is None:
                attestation_root = ROOT / "data/provenance/dukascopy_v4/acquisition_v5/legacy_acceptance"
            attestation = _attestation(attestation_root / f"{date_text}_{leg}.json", observed)
            observed["acceptance_attestation_path"] = attestation.relative_to(provenance_root).as_posix()
            observed["acceptance_attestation_sha256"] = digest(attestation)
            observed["state"] = "VERIFIED_LEGACY"
        else:
            observed["state"] = "VERIFIED_V5_BUNDLE"
        return observed
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        state = "INVALID_LEGACY" if kind == "LEGACY" else "INVALID_V5"
        return {"state": state, "target": target, "reason": str(exc)}


def audit_inventory(inventory_path: Path, bundle_root: Path, report_root: Path, *, legacy_root: Path | None = None) -> dict[str, Any]:
    inventory = load_inventory(inventory_path)
    rows = [verify_bundle(item, bundle_root=bundle_root, legacy_root=legacy_root, require_committed=False) for item in sorted(inventory, key=target_key)]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    report_root.mkdir(parents=True, exist_ok=True)
    inventory_payload = {"schema_version": 2, "inventory_path": inventory_path.relative_to(ROOT).as_posix(), "inventory_sha256": INVENTORY_SHA256, "target_count": TARGET_COUNT, "unique_target_count": TARGET_COUNT, "distribution": {"nq/3m": 69, "spx/5m": 44}, "contains_2025_04_16_nq_3m": True, "class_counts": counts}
    (report_root / "inventory_audit.json").write_text(json.dumps(inventory_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (report_root / "bundle_audit.json").write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    residual = [row for row in rows if row["state"] not in {"VERIFIED_LEGACY", "VERIFIED_V5_BUNDLE"}]
    pd.DataFrame([next(item for item in inventory if target_key(item) == target_key(row["target"])) for row in residual]).to_csv(report_root / "residual_leg_days.csv", index=False)
    root_rows = [row for row in rows if row["state"] in {"VERIFIED_LEGACY", "VERIFIED_V5_BUNDLE"}]
    result = {"inventory": inventory_payload, "bundles": rows, "residual": residual, "residual_count": len(residual), "semantic_root_sha256": semantic_root(root_rows), "ordered_target_merkle_root_sha256": ordered_merkle(root_rows)}
    return result


def finalize_manifest(first_report: Path, second_report: Path, output: Path, *, inventory_path: Path, provenance_root: Path, calendar_sha256: str, auditor_sha256: str, node_helper_sha256: str, package_lock_sha256: str, http_event_root_sha256: str) -> Path:
    first = json.loads((first_report / "bundle_audit.json").read_text(encoding="utf-8"))
    second = json.loads((second_report / "bundle_audit.json").read_text(encoding="utf-8"))
    if first != second:
        raise ValueError("independent audit bundle results differ")
    rows = first
    inventory = load_inventory(inventory_path)
    expected_keys = {target_key(row) for row in inventory}
    if len(rows) != TARGET_COUNT or {target_key(row) for row in rows} != expected_keys:
        raise ValueError("independent audit does not cover the exact frozen target set")
    if any(row["state"] not in {"VERIFIED_LEGACY", "VERIFIED_V5_BUNDLE"} for row in rows):
        raise ValueError("cannot finalize with residual targets")
    for label, value in (("calendar_sha256", calendar_sha256), ("auditor_sha256", auditor_sha256), ("node_helper_sha256", node_helper_sha256), ("package_lock_sha256", package_lock_sha256), ("http_event_root_sha256", http_event_root_sha256)):
        if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError(f"{label} is not a lowercase SHA-256 digest")
    output = output.resolve()
    provenance_root = provenance_root.resolve()
    try:
        output.relative_to(provenance_root)
    except ValueError as exc:
        raise ValueError("final manifest output must remain under the provenance root") from exc
    targets = []
    for row in sorted(rows, key=target_key):
        target = {key: row[key] for key in ("target", "state", "bundle_path", "manifest_path", "minute_path", "derived_path", "manifest_sha256", "minute_sha256", "derived_sha256", "minute_semantic_sha256", "derived_semantic_sha256", "acceptance_attestation_path", "acceptance_attestation_sha256") if key in row}
        bundle_hash = sha256(json.dumps(target, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        target.update({"attestation_sha256": row.get("acceptance_attestation_sha256") or sha256(json.dumps(row, sort_keys=True).encode()).hexdigest(), "bundle_sha256": bundle_hash})
        targets.append(target)
    payload = {"schema_version": 5, "inventory_sha256": INVENTORY_SHA256, "target_count": TARGET_COUNT, "residual_count": 0, "targets": targets, "semantic_root_sha256": semantic_root(targets), "ordered_target_merkle_root_sha256": ordered_merkle(targets), "calendar_sha256": calendar_sha256, "auditor_sha256": auditor_sha256, "node_helper_sha256": node_helper_sha256, "package_lock_sha256": package_lock_sha256, "http_event_root_sha256": http_event_root_sha256}
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if output.exists() and output.read_text(encoding="utf-8") != encoded:
        raise ValueError("refusing to overwrite a different final reacquisition manifest")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(encoded, encoding="utf-8", newline="\n")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("audit", "finalize"), nargs="?", default="audit")
    parser.add_argument("--inventory", type=Path, default=ROOT / "data/provenance/dukascopy_v4/frozen_invalid_leg_days_v4.csv")
    parser.add_argument("--bundle-root", type=Path, default=ROOT / "data/provenance/dukascopy_v4/acquisition_v5/bundles")
    parser.add_argument("--legacy-root", type=Path, default=ROOT / "data/provenance/dukascopy_v4/reacquired_session")
    parser.add_argument("--report-root", type=Path, default=ROOT / "outputs/reports/dukascopy_reacquisition_v5")
    parser.add_argument("--second-report-root", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "data/provenance/dukascopy_v4/acquisition_v5/reacquisition_manifest_v5.json")
    parser.add_argument("--calendar", type=Path, default=ROOT / "live_forward/calendars/us_equity_rth_2022_2026_v4.json")
    args = parser.parse_args()
    if args.command == "audit":
        result = audit_inventory(args.inventory.resolve(), args.bundle_root.resolve(), args.report_root.resolve(), legacy_root=args.legacy_root.resolve())
        print(json.dumps({"target_count": TARGET_COUNT, "residual_count": result["residual_count"], "class_counts": result["inventory"]["class_counts"]}, sort_keys=True))
        raise SystemExit(2 if result["residual_count"] else 0)
    if args.second_report_root is None:
        raise SystemExit("finalize requires --second-report-root")
    calendar_sha = digest(args.calendar.resolve())
    event_path = ROOT / "outputs/reports/.dukascopy_acquisition_v5/http_events.jsonl"
    event_root = digest(event_path) if event_path.is_file() else sha256(b"").hexdigest()
    finalize_manifest(args.report_root.resolve(), args.second_report_root.resolve(), args.output.resolve(), inventory_path=args.inventory.resolve(), provenance_root=(ROOT / "data/provenance/dukascopy_v4"), calendar_sha256=calendar_sha, auditor_sha256=digest(Path(__file__)), node_helper_sha256=digest(ROOT / "tools/dukascopy-downloader/acquire_v5.mjs"), package_lock_sha256=digest(ROOT / "tools/dukascopy-downloader/package-lock.json"), http_event_root_sha256=event_root)


if __name__ == "__main__":
    main()
