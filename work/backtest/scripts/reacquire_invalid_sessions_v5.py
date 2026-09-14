"""Rate-limit-safe V5 Dukascopy reacquisition coordinator."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.dukascopy_acquisition import (  # noqa: E402
    AtomicJsonStore,
    DEFAULT_HOST_ALLOWLIST,
    LEGACY_LOG_SHA256,
    MIGRATION_COOLDOWN_UTC,
    RateLimitController,
    STATE_RELATIVE,
    iso_utc,
    parse_utc,
    utc_now,
)


INVENTORY_SHA256 = "a63406f235ded8d3daa123c0311adb678e53db3d996141b194493309f2cce075"
TARGET_COUNT = 113
HOST = next(iter(DEFAULT_HOST_ALLOWLIST))


def load_authoritative_targets(path: str | Path) -> list[dict[str, object]]:
    inventory_path = Path(path)
    if sha256(inventory_path.read_bytes()).hexdigest() != INVENTORY_SHA256:
        raise ValueError("authoritative frozen inventory hash mismatch")
    rows = pd.read_csv(inventory_path).to_dict(orient="records")
    keys = {(str(row["date"]), str(row["leg"]), str(row["timeframe"])) for row in rows}
    if len(rows) != TARGET_COUNT or len(keys) != TARGET_COUNT:
        raise ValueError("authoritative frozen inventory must contain 113 unique targets")
    if ("2025-04-16", "nq", "3m") not in keys:
        raise ValueError("authoritative frozen inventory is missing 2025-04-16/nq/3m")
    return rows


def index_superseded_failure_evidence(provenance_root: Path) -> Path:
    failures = sorted((provenance_root / "failed").rglob("*.json")) if (provenance_root / "failed").is_dir() else []
    rows: list[dict[str, object]] = []
    for path in failures:
        raw = path.read_bytes()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        text = path.name
        attempts = payload.get("failed_attempts") if isinstance(payload, dict) else None
        first_attempt = attempts[0] if isinstance(attempts, list) and attempts and isinstance(attempts[0], dict) else {}
        instrument = str(payload.get("instrument") or first_attempt.get("instrument") or "")
        date = str(payload.get("date") or payload.get("from") or first_attempt.get("from") or "")[:10]
        leg = str(payload.get("leg") or {"usatechidxusd": "nq", "usa500idxusd": "spx"}.get(instrument, ""))
        timeframe = str(payload.get("timeframe") or {"nq": "3m", "spx": "5m"}.get(leg, "1m"))
        match = re.search(r"(\d{4}-\d{2}-\d{2})", text)
        if not date and match:
            date = match.group(1)
        rows.append({
            "path": path.relative_to(ROOT).as_posix(),
            "sha256": sha256(raw).hexdigest(),
            "bytes": len(raw),
            "classification": "MISNAMED_DUPLICATE",
            "canonical_target": {"date": date, "leg": leg, "timeframe": timeframe},
        })
    output = provenance_root / "acquisition_v5" / "superseded_failure_evidence.json"
    AtomicJsonStore(output).write({"schema_version": 1, "entries": rows})
    return output


def write_deferred_checkpoint(root: Path, *, next_retry_at_utc: str, reason: str = "DEFERRED_RATE_LIMIT") -> Path:
    path = root / STATE_RELATIVE
    store = AtomicJsonStore(path)
    state = store.read()
    state.update({
        "schema_version": 1,
        "run_status": reason,
        "next_retry_at_utc": next_retry_at_utc,
        "provider_call_attempted": False,
        "updated_at_utc": iso_utc(utc_now()),
    })
    state.setdefault("hosts", {}).setdefault(HOST, {}).update({
        "circuit": "OPEN", "next_retry_at_utc": next_retry_at_utc,
    })
    store.write(state)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, default=ROOT / "data/provenance/dukascopy_v4/frozen_invalid_leg_days_v4.csv")
    parser.add_argument("--output-root", type=Path, default=ROOT / "data/provenance/dukascopy_v4/reacquired_session")
    parser.add_argument("--provenance-root", type=Path, default=ROOT / "data/provenance/dukascopy_v4")
    parser.add_argument("--now", type=str, help="Injected UTC clock for offline policy tests")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_authoritative_targets(args.inventory.resolve())
    provenance_root = args.provenance_root.resolve()
    index_superseded_failure_evidence(provenance_root)
    state_path = ROOT / STATE_RELATIVE
    controller = RateLimitController(state_path)
    legacy = ROOT / "outputs/reports/reacquire_invalid_sessions_v4.log"
    if legacy.is_file() and sha256(legacy.read_bytes()).hexdigest() == LEGACY_LOG_SHA256:
        controller.migrate_legacy_log(legacy)
    else:
        write_deferred_checkpoint(ROOT, next_retry_at_utc=MIGRATION_COOLDOWN_UTC, reason="PRIVATE_LEGACY_LOG_MISSING")
        print(json.dumps({"status": "DEFERRED_RATE_LIMIT", "next_retry_at_utc": MIGRATION_COOLDOWN_UTC}, sort_keys=True))
        return
    observed = parse_utc(args.now) if args.now else utc_now()
    try:
        controller.before_request(HOST, now=observed)
    except Exception as exc:
        if getattr(exc, "next_retry_at_utc", None):
            write_deferred_checkpoint(ROOT, next_retry_at_utc=exc.next_retry_at_utc)
            print(json.dumps({"status": "DEFERRED_RATE_LIMIT", "next_retry_at_utc": exc.next_retry_at_utc}, sort_keys=True))
            return
        raise
    raise SystemExit("provider execution is intentionally not part of the offline V5 code/test commit")


if __name__ == "__main__":
    main()
