"""Executable, resumable Dukascopy V5 acquisition coordinator."""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time as wall_time
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.dukascopy_acquisition import (  # noqa: E402
    AcquisitionDeferred, AtomicJsonStore, DEFAULT_HOST_ALLOWLIST, EVENTS_RELATIVE,
    HttpCas, MIGRATION_COOLDOWN_UTC, ProviderProcessLock, RateLimitController,
    STATE_RELATIVE, RETRYABLE_STATUS, RETRY_DELAYS_SECONDS, iso_utc, parse_utc, retry_delay_for_attempt, sha256_bytes, utc_now, validate_clock,
)
from backtest.reacquisition_contract import INVENTORY_SHA256, TARGET_COUNT, load_inventory, target_key  # noqa: E402

HOST = next(iter(DEFAULT_HOST_ALLOWLIST))
NODE_HELPER = ROOT / "tools/dukascopy-downloader/acquire_v5.mjs"
PROVENANCE = ROOT / "data/provenance/dukascopy_v4"
ACQUISITION_ROOT = PROVENANCE / "acquisition_v5"
BUNDLE_ROOT = ACQUISITION_ROOT / "bundles"
CAS_ROOT = ACQUISITION_ROOT / "http_cas"
RUN_STATE = ACQUISITION_ROOT / "checkpoint.json"


def load_authoritative_targets(path: str | Path) -> list[dict[str, object]]:
    rows = load_inventory(Path(path))
    return sorted(rows, key=target_key)


def index_superseded_failure_evidence(provenance_root: Path) -> Path:
    failures = sorted((provenance_root / "failed").rglob("*.json")) if (provenance_root / "failed").is_dir() else []
    rows = []
    for path in failures:
        raw = path.read_bytes()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        attempts = payload.get("failed_attempts") if isinstance(payload, dict) else None
        first = attempts[0] if isinstance(attempts, list) and attempts and isinstance(attempts[0], dict) else {}
        instrument = str(payload.get("instrument") or first.get("instrument") or "")
        date_text = str(payload.get("date") or payload.get("from") or first.get("from") or "")[:10]
        leg = str(payload.get("leg") or {"usatechidxusd": "nq", "usa500idxusd": "spx"}.get(instrument, ""))
        timeframe = str(payload.get("timeframe") or {"nq": "3m", "spx": "5m"}.get(leg, "1m"))
        rows.append({"path": path.relative_to(ROOT).as_posix(), "sha256": sha256(raw).hexdigest(), "bytes": len(raw), "classification": "SUPERSEDED_EVIDENCE_ONLY", "canonical_target": {"date": date_text, "leg": leg, "timeframe": timeframe}})
    output = provenance_root / "acquisition_v5" / "superseded_failure_evidence.json"
    AtomicJsonStore(output).write({"schema_version": 2, "entries": rows})
    return output


def _envelope(date_text: str) -> tuple[datetime, datetime]:
    local_day = datetime.combine(date.fromisoformat(date_text), time.min, tzinfo=ZoneInfo("America/New_York"))
    next_day = local_day + timedelta(days=1)
    return local_day - timedelta(hours=6), next_day


def _instrument(leg: str) -> str:
    return "usatechidxusd" if leg == "nq" else "usa500idxusd"


def _preserved_deadline(value: object | None) -> str:
    """Return the exact locked deadline string unless a later deadline is persisted."""
    if value:
        parsed = parse_utc(str(value))
        minimum = parse_utc(MIGRATION_COOLDOWN_UTC)
        if parsed > minimum:
            return str(value)
    return MIGRATION_COOLDOWN_UTC


def _run_node(*args: str) -> dict[str, object]:
    result = subprocess.run(["node", str(NODE_HELPER), *args], cwd=NODE_HELPER.parent, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "dukascopy node helper failed")
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("dukascopy node helper returned no evidence")
    return json.loads(lines[-1])


def _canonical_frame(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    volume = "volume" if "volume" in frame.columns else "Volume"
    frame = frame.rename(columns={volume: "volume"})
    frame = frame.rename(columns={"timestamp": "time"})
    frame["time"] = pd.to_datetime(frame["time"], utc=True, format="mixed")
    for name in ("open", "high", "low", "close", "volume"):
        frame[name] = pd.to_numeric(frame[name], errors="raise")
    return frame[["time", "open", "high", "low", "close", "volume"]].sort_values("time", kind="mergesort").reset_index(drop=True)


def _write_csv(frame: pd.DataFrame, path: Path) -> None:
    frame = frame.copy()
    frame["time"] = frame["time"].map(lambda value: pd.Timestamp(value).isoformat())
    frame.to_csv(path, index=False, lineterminator="\n")


def _build_bundle(target: dict[str, object], plan: dict[str, object], cas_items: list[dict[str, object]], decoded_csv: Path) -> Path:
    date_text, leg, timeframe = str(target["date"]), str(target["leg"]), str(target["timeframe"])
    symbol = "DUKASCOPY_USATECHIDXUSD" if leg == "nq" else "DUKASCOPY_USA500IDXUSD"
    final = BUNDLE_ROOT / f"{date_text}_{leg}"
    if final.is_dir() and (final / "COMMITTED.json").is_file():
        return final
    staging_root = BUNDLE_ROOT / ".staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = staging_root / f"{date_text}_{leg}.{uuid4().hex}"
    staging.mkdir(parents=True)
    try:
        minute = _canonical_frame(decoded_csv)
        derived = minute.set_index("time").resample(f"{int(timeframe.removesuffix('m'))}min", label="left", closed="left").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna(subset=["open", "high", "low", "close"]).reset_index()
        minute_path = staging / f"{symbol}, 1m_{date_text}_{(date.fromisoformat(date_text) + timedelta(days=1)).isoformat()}.csv"
        derived_path = staging / f"{symbol}, {timeframe}_{date_text}_{(date.fromisoformat(date_text) + timedelta(days=1)).isoformat()}.csv"
        _write_csv(minute, minute_path)
        _write_csv(derived, derived_path)
        manifest_path = staging / f"{symbol}, {timeframe}_{date_text}_{(date.fromisoformat(date_text) + timedelta(days=1)).isoformat()}.csv.manifest.json"
        raw_chunks = [{"url": item["url"], "url_sha256": item["url_sha256"], "raw_path": str(Path(item["cas_path"]).relative_to(ROOT)).replace("\\", "/"), "raw_sha256": item["sha256"], "raw_byte_count": item["byte_count"]} for item in cas_items]
        manifest = {"schema_version": 5, "provider": "Dukascopy", "instrument": _instrument(leg), "symbol": symbol, "side": "BID", "source_granularity": "M1", "timezone": "America/New_York", "session_context_hours": 6, "request_range": {"start": date_text, "end_exclusive": (date.fromisoformat(date_text) + timedelta(days=1)).isoformat()}, "output_path": str(derived_path.relative_to(ROOT)).replace("\\", "/"), "raw_chunks": raw_chunks, "ordered_urls": [item["url"] for item in cas_items], "downloader": {"package": "dukascopy-node", "version": "1.50.0", "integrity": "sha512-o2Co/asUD/TXFNhblJUYkRseHMt/uvFrnhzOKWezLuiFJqbl4Zn2oJGL4/W+PY1b2YsI11+9+TO40qNQBAj8/w==", "lockfile_sha256": sha256((ROOT / "tools/dukascopy-downloader/package-lock.json").read_bytes()).hexdigest()}, "derived_sha256": sha256(derived_path.read_bytes()).hexdigest(), "minute_sha256": sha256(minute_path.read_bytes()).hexdigest(), "decoded_sha256": sha256(decoded_csv.read_bytes()).hexdigest(), "plan_url_sha256": [item["url_sha256"] for item in cas_items]}
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        committed = {"schema_version": 1, "manifest_name": manifest_path.name, "minute_name": minute_path.name, "derived_name": derived_path.name, "manifest_sha256": sha256(manifest_path.read_bytes()).hexdigest(), "minute_sha256": sha256(minute_path.read_bytes()).hexdigest(), "derived_sha256": sha256(derived_path.read_bytes()).hexdigest(), "target": {"date": date_text, "leg": leg, "timeframe": timeframe}}
        (staging / "COMMITTED.json").write_text(json.dumps(committed, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.exists():
            raise RuntimeError("committed bundle already exists with different bytes")
        staging.replace(final)
        return final
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def acquire_targets(args: argparse.Namespace, targets: list[dict[str, object]], controller: RateLimitController, run_id: str) -> dict[str, object]:
    state = controller._load()
    state["run_status"] = "RUNNING"
    state["inventory_sha256"] = INVENTORY_SHA256
    state["target_count"] = TARGET_COUNT
    state["provider_call_count"] = int(state.get("provider_call_count", 0))
    controller._write(state)
    cas = HttpCas(CAS_ROOT)
    checkpoint = AtomicJsonStore(RUN_STATE)
    progress = checkpoint.read()
    for target in targets:
        key = "|".join(target_key(target))
        plan_fd, plan_name = tempfile.mkstemp(prefix="dukascopy-plan-", suffix=".json")
        os.close(plan_fd)
        plan_path = Path(plan_name)
        try:
            start, end = _envelope(str(target["date"]))
            plan = _run_node("plan", "--instrument", _instrument(str(target["leg"])), "--start", start.isoformat(), "--end", end.isoformat(), "--timeframe", "m1", "--price-type", "bid", "--output", str(plan_path))
            cas_items = []
            for index, url in enumerate(plan["urls"]):
                url_hash = str(plan["url_sha256"][index])
                cached = progress.get("artifacts", {}).get(f"{key}|{index}")
                if isinstance(cached, dict) and cached.get("status") == "VERIFIED_DECODED" and cached.get("url_sha256") == url_hash:
                    try:
                        cas_path = cas.path_for(str(cached["sha256"]))
                        if cas_path.is_file() and cas_path.stat().st_size == int(cached["byte_count"]):
                            cas_items.append({"url": url, "url_sha256": url_hash, "sha256": cached["sha256"], "byte_count": cached["byte_count"], "cas_path": str(cas_path)})
                            continue
                    except (KeyError, ValueError, TypeError):
                        pass
                retry_index = 0
                while True:
                    try:
                        request = controller.before_request(HOST, run_id=run_id, artifact_id=f"{key}|{index}")
                    except AcquisitionDeferred as deferred:
                        if deferred.reason != "DEFERRED_PROVIDER_SPACING":
                            raise
                        remaining = max(0.0, parse_utc(deferred.next_retry_at_utc).timestamp() - utc_now().timestamp())
                        wall_time.sleep(min(60.0, remaining))
                        continue
                    stage_root = ACQUISITION_ROOT / "staging"
                    try:
                        evidence = _run_node("fetch-one", "--plan", str(plan_path), "--index", str(index), "--url-sha256", url_hash, "--stage-root", str(stage_root))
                        status = int(evidence.get("status", 0))
                        body_path = Path(str(evidence["staging_path"]))
                        body = body_path.read_bytes()
                        body_sha, cas_path = cas.put(body)
                        if str(evidence.get("url_sha256")) != url_hash or str(evidence.get("body_sha256")) != body_sha or str(evidence.get("buffer_sha256")) != body_sha:
                            raise RuntimeError("provider body or URL evidence binding mismatch")
                        body_path.unlink(missing_ok=True)
                        controller.finish_request(request, status=status, body_sha256=body_sha, body_byte_count=len(body), headers=evidence.get("headers") if isinstance(evidence.get("headers"), dict) else {}, endpoint=str(evidence.get("endpoint") or ""), request_url_sha256=url_hash)
                    except (subprocess.TimeoutExpired, TimeoutError) as exc:
                        controller.finish_request(request, error_code="TRANSPORT_TIMEOUT", endpoint="https://datafeed.dukascopy.com", request_url_sha256=url_hash)
                        if retry_index >= len(RETRY_DELAYS_SECONDS):
                            controller.record_terminal(HOST, "TRANSPORT_RETRY_EXHAUSTED")
                            raise RuntimeError("provider transport retry budget exhausted") from exc
                        retry_index += 1
                        wall_time.sleep(retry_delay_for_attempt(retry_index))
                        continue
                    except RuntimeError as exc:
                        controller.finish_request(request, error_code="TRANSPORT_ERROR", endpoint="https://datafeed.dukascopy.com", request_url_sha256=url_hash)
                        if retry_index >= len(RETRY_DELAYS_SECONDS) or "timeout" not in str(exc).lower():
                            controller.record_terminal(HOST, "TRANSPORT_ERROR")
                            raise
                        retry_index += 1
                        wall_time.sleep(retry_delay_for_attempt(retry_index))
                        continue
                state = controller._load()
                status_counts = dict(state.get("http_status_counts", {})); status_counts[str(status)] = int(status_counts.get(str(status), 0)) + 1; state["http_status_counts"] = status_counts
                controller._write(state)
                if status == 429:
                    controller.record_429(HOST, (evidence.get("headers") or {}).get("retry-after"))
                    raise AcquisitionDeferred(controller._load()["hosts"][HOST]["next_retry_at_utc"])
                if status in {404, 410}:
                    controller.record_terminal(HOST, "SOURCE_ARTIFACT_MISSING")
                    raise RuntimeError("SOURCE_ARTIFACT_MISSING")
                if status in RETRYABLE_STATUS:
                    if retry_index >= 4:
                        controller.record_terminal(HOST, f"HTTP_{status}_RETRY_EXHAUSTED")
                        raise RuntimeError(f"provider HTTP status {status}; retry budget exhausted")
                    retry_index += 1
                    wall_time.sleep(retry_delay_for_attempt(retry_index))
                    continue
                if status not in {200, 206}:
                    controller.record_terminal(HOST, f"HTTP_{status}")
                    raise RuntimeError(f"provider HTTP status {status}")
                if not body:
                    controller.record_terminal(HOST, "EMPTY_ARTIFACT")
                    raise RuntimeError("provider returned an empty artifact")
                controller.record_success(HOST)
                checkpoint_state = checkpoint.read(); checkpoint_state.setdefault("artifacts", {})[f"{key}|{index}"] = {"url_sha256": url_hash, "sha256": body_sha, "byte_count": len(body), "cas_path": str(cas_path), "status": "FETCHED_CAS"}; checkpoint.write(checkpoint_state)
                cas_items.append({"url": url, "url_sha256": url_hash, "sha256": body_sha, "byte_count": len(body), "cas_path": str(cas_path)})
            input_path = plan_path.with_suffix(".decode.json")
            input_path.write_text(json.dumps([{"index": index, "path": item["cas_path"], "sha256": item["sha256"]} for index, item in enumerate(cas_items)], indent=2) + "\n", encoding="utf-8")
            decoded_path = plan_path.with_suffix(".m1.csv")
            _run_node("decode", "--plan", str(plan_path), "--input", str(input_path), "--output", str(decoded_path))
            _build_bundle(target, plan, cas_items, decoded_path)
            progress = checkpoint.read()
            for index in range(len(cas_items)):
                artifact = progress.setdefault("artifacts", {}).get(f"{key}|{index}")
                if isinstance(artifact, dict):
                    artifact["status"] = "VERIFIED_DECODED"
            progress.setdefault("targets", {})[key] = "VERIFIED_V5_BUNDLE"
            checkpoint.write(progress)
        finally:
            plan_path.unlink(missing_ok=True)
            plan_path.with_suffix(".decode.json").unlink(missing_ok=True)
            plan_path.with_suffix(".m1.csv").unlink(missing_ok=True)
    state = controller._load(); state["run_status"] = "COMPLETE"; controller._write(state)
    return state


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, default=ROOT / "data/provenance/dukascopy_v4/frozen_invalid_leg_days_v4.csv")
    parser.add_argument("--now", type=str)
    parser.add_argument("--transport-fixture", action="store_true")
    parser.add_argument("--run-id", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.now and not args.transport_fixture:
        raise SystemExit("--now is permitted only with --transport-fixture")
    targets = load_authoritative_targets(args.inventory.resolve())
    index_superseded_failure_evidence(PROVENANCE)
    controller = RateLimitController(ROOT / STATE_RELATIVE, event_path=ROOT / EVENTS_RELATIVE)
    controller.start_run(args.run_id or uuid4().hex)
    state = controller._load()
    if not state["migration"].get("completed"):
        legacy = ROOT / "outputs/reports/reacquire_invalid_sessions_v4.log"
        if legacy.is_file():
            controller.migrate_legacy_log(legacy)
        else:
            state["run_status"] = "DEFERRED_RATE_LIMIT"
            state["migration_blocker"] = "PRIVATE_LEGACY_LOG_MISSING"
            state["next_retry_at_utc"] = _preserved_deadline(state.get("next_retry_at_utc"))
            state["hosts"].setdefault(HOST, {}).update({"circuit": "OPEN", "consecutive_429": max(5, int(state["hosts"].get(HOST, {}).get("consecutive_429", 0))), "next_retry_at_utc": state["next_retry_at_utc"]})
            controller._write(state)
            print(json.dumps({"status": "DEFERRED_RATE_LIMIT", "provider_call_count": state["provider_call_count"], "next_retry_at_utc": MIGRATION_COOLDOWN_UTC}, sort_keys=True))
            return
    observed = validate_clock(now=parse_utc(args.now) if args.now else None, transport_fixture=args.transport_fixture)
    state = controller._load()
    deadline_text = _preserved_deadline(state.get("hosts", {}).get(HOST, {}).get("next_retry_at_utc"))
    if deadline_text != state.get("hosts", {}).get(HOST, {}).get("next_retry_at_utc") or deadline_text != state.get("next_retry_at_utc"):
        state["hosts"].setdefault(HOST, {})["next_retry_at_utc"] = deadline_text
        state["next_retry_at_utc"] = deadline_text
        controller._write(state)
    deadline = parse_utc(deadline_text)
    if observed < deadline:
        state["run_status"] = "DEFERRED_RATE_LIMIT"; state["next_retry_at_utc"] = deadline_text; controller._write(state)
        print(json.dumps({"status": "DEFERRED_RATE_LIMIT", "provider_call_count": state["provider_call_count"], "next_retry_at_utc": deadline_text}, sort_keys=True)); return
    lock_path = ROOT / "outputs/reports/.dukascopy_acquisition_v5/provider.lock"
    with ProviderProcessLock(lock_path):
        run_id = args.run_id or uuid4().hex
        try:
            result = acquire_targets(args, targets, controller, run_id)
        except AcquisitionDeferred as exc:
            state = controller._load(); state["run_status"] = exc.reason; state["next_retry_at_utc"] = exc.next_retry_at_utc; controller._write(state)
            print(json.dumps({"status": exc.reason, "provider_call_count": state["provider_call_count"], "next_retry_at_utc": exc.next_retry_at_utc}, sort_keys=True)); return
        print(json.dumps({"status": result.get("run_status"), "provider_call_count": result.get("provider_call_count", 0)}, sort_keys=True))


if __name__ == "__main__":
    main()
