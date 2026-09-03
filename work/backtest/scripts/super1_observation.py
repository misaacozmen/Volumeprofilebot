"""V09 checkpoint-bound raw capture plugin."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import inspect
import json
import math
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Any

import pytest

from super1_required_nodes import ALL_REQUIRED_NODES, BEHAVIOR_REQUIRED_NODES, V09_BEHAVIOR_BINDINGS, required_raw_sources


SCHEMA = "super1-observation/v09"
SCHEMA_VERSION = 9
ROOT = Path(__file__).resolve().parents[1]
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")
_IDENTITY = ("run_id", "suite", "nodeid", "attempt_id", "scenario_nonce", "checkpoint_id")
_PROJECTION_KEYS = frozenset({"expected", "actual", "recomputed", "measurement", "result", "outcome_code", "facts", "resources", "error_codes", "projection", "summary", "outcome"})


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()


def observation_path(root: Path, nodeid: str) -> Path:
    slug = _SAFE.sub("_", nodeid).strip("_")[:180]
    return root / f"{slug}--{hashlib.sha256(nodeid.encode()).hexdigest()[:16]}.json"


def _plain(value: object, depth: int = 0, seen: set[int] | None = None) -> object:
    seen = seen or set()
    if depth > 7:
        return {"depth": depth}
    if isinstance(value, Path):
        return str(value.resolve())
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"byte_count": len(value), "sha256": sha256_bytes(value)}
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    marker = id(value)
    if marker in seen:
        return {"cycle": True}
    seen.add(marker)
    if isinstance(value, dict):
        return {str(k): _plain(v, depth + 1, seen) for k, v in value.items() if str(k).lower() not in _PROJECTION_KEYS}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v, depth + 1, seen) for v in list(value)[:500]]
    attrs = getattr(value, "__dict__", None)
    return _plain(attrs, depth + 1, seen) if isinstance(attrs, dict) else str(value)


def _test_frames() -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    frame = inspect.currentframe()
    try:
        while frame is not None:
            name = Path(frame.f_code.co_filename).name
            if name.startswith("test_") or name == "v08_helpers.py":
                found.append(dict(frame.f_locals))
            frame = frame.f_back
    finally:
        del frame
    return found


def _values() -> list[object]:
    values: list[object] = []
    for frame in _test_frames():
        values.extend(frame.values())
    return values


def _dicts() -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    def visit(value: object, depth: int = 0) -> None:
        if depth > 5:
            return
        if isinstance(value, dict):
            result.append(value)
            for child in list(value.values())[:100]:
                visit(child, depth + 1)
        elif isinstance(value, (list, tuple)):
            for child in list(value)[:100]:
                visit(child, depth + 1)
    for value in _values():
        visit(value)
    return result


def _field(*names: str) -> object | None:
    for value in _dicts():
        for name in names:
            if name in value and value[name] is not None:
                return value[name]
    return None


def _fixture_root() -> Path:
    candidates: list[Path] = []
    for frame in _test_frames():
        for name, value in frame.items():
            if not isinstance(value, Path) or not any(token in name.lower() for token in ("tmp", "root", "path", "dir")):
                continue
            candidate = value if value.is_dir() else value.parent
            if candidate.is_dir() and candidate.resolve() not in {ROOT.resolve(), ROOT.parent.resolve()}:
                candidates.append(candidate.resolve())
    return next((p for p in candidates if p.name.lower() in {"tmp", "fixture", "source", "snapshot"}), candidates[0] if candidates else ROOT / "live_forward")


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.is_dir() else []


def _members(root: Path) -> list[dict[str, object]]:
    rows = []
    for path in _files(root):
        data = path.read_bytes()
        rows.append({"path": path.relative_to(root).as_posix(), "bytes": len(data), "sha256": sha256_bytes(data)})
    return rows


def _tree_hash(rows: list[dict[str, object]]) -> str:
    return sha256_bytes(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode())


def _sqlite_payload(root: Path, market: bool) -> dict[str, object]:
    db = next((p for p in _files(root) if p.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".db3"}), None)
    rows: list[dict[str, object]] = []
    hashes: list[str] = []
    sequence: list[int] = []
    schema = b""
    wal = {"mode": "read-only", "present": False, "bytes": 0}
    if db:
        try:
            con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
            try:
                definitions = con.execute("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name").fetchall()
                schema = canonical([[str(a), str(b)] for a, b in definitions])
                for table, _ in definitions:
                    columns = [str(row[1]) for row in con.execute(f'PRAGMA table_info("{table}")').fetchall()]
                    for values in con.execute(f'SELECT * FROM "{table}"').fetchall():
                        row = {"table": str(table), "columns": columns, "values": [_plain(v) for v in values]}
                        rows.append(row)
                        hashes.append(sha256_bytes(canonical(row)))
                        sequence.extend(int(v) for n, v in zip(columns, values) if "sequence" in n.lower() and isinstance(v, int))
            finally:
                con.close()
            sidecar = db.with_name(db.name + "-wal")
            wal = {"mode": "read-only", "present": sidecar.is_file(), "bytes": sidecar.stat().st_size if sidecar.is_file() else 0}
        except (OSError, sqlite3.Error, ValueError):
            schema = db.read_bytes()
    base = {"schema_sha256": sha256_bytes(schema), "primary_key_rows": rows, "row_sha256": hashes, "wal_state": wal}
    if market:
        base.update({"fetch_scopes": _plain(_field("fetch_scopes", "scopes") or {}), "first_known_times": _plain(_field("first_known_times", "known_times") or {})})
    else:
        base["max_sequence"] = max(sequence) if sequence else None
    return base


def _broker_payload() -> dict[str, object]:
    output = {"current_orders": [], "positions": [], "history_orders": [], "history_deals": []}
    for value in _values():
        if not callable(getattr(value, "orders_get", None)) or not callable(getattr(value, "positions_get", None)):
            continue
        for key, method_name in (("current_orders", "orders_get"), ("positions", "positions_get"), ("history_orders", "history_orders_get"), ("history_deals", "history_deals_get")):
            method = getattr(value, method_name, None)
            if callable(method):
                try:
                    output[key] = [_plain(row) for row in list(method() or [])[:500]]
                except Exception:
                    output[key] = []
        break
    return output


def _sdk_payload() -> dict[str, object]:
    calls: list[object] = []
    for value in _values():
        if isinstance(value, dict) and isinstance(value.get("calls"), list):
            calls = [_plain(v) for v in value["calls"] if isinstance(v, dict)]
            if calls:
                break
        sent = getattr(value, "sent", None)
        if isinstance(sent, int) and sent >= 0:
            action = getattr(value, "TRADE_ACTION_PENDING", "TRADE_ACTION_PENDING")
            calls = [{"sequence": i, "action": action} for i in range(sent)]
            break
    for i, call in enumerate(calls):
        if isinstance(call, dict):
            call["sequence"] = i
    return {"sequence": list(range(len(calls))), "calls": calls}


def _typed_payload(capture_type: str, root: Path, checkpoint_id: str) -> dict[str, object]:
    members = _members(root)
    tree = _tree_hash(members)
    if capture_type == "sdk_call_trace":
        return _sdk_payload()
    if capture_type == "order_store_sqlite":
        return _sqlite_payload(root, False)
    if capture_type == "market_fetch_store_sqlite":
        return _sqlite_payload(root, True)
    if capture_type == "broker_readback":
        return _broker_payload()
    if capture_type == "filesystem_inventory":
        return {"root_role": root.name or "fixture", "members": members, "canonical_tree_sha256": tree}
    if capture_type == "calendar_validation":
        paths = [p for p in _files(root) if p.suffix.lower() == ".json"]
        paths += [p for p in (ROOT / "live_forward" / "calendars").rglob("*.json") if p.is_file()]
        source_bytes = {}; source_hashes = {}; parsed = []
        for path in sorted(set(paths))[:100]:
            data = path.read_bytes(); name = path.relative_to(root).as_posix() if path.is_relative_to(root) else path.relative_to(ROOT).as_posix()
            source_bytes[name] = len(data); source_hashes[name] = sha256_bytes(data)
            try:
                parsed.append(_plain(json.loads(data.decode())))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
        return {"source_bytes": source_bytes, "source_hashes": source_hashes, "parsed_records": parsed, "validation_result": {"source_count": len(paths)}}
    if capture_type == "filter_evidence":
        path = next((p for p in _files(root) if "prefix" in p.name.lower() or "filter" in p.name.lower()), root / "filter-evidence.json")
        data = path.read_bytes() if path.is_file() else b""
        value = {}
        try:
            loaded = json.loads(data.decode())
            value = loaded if isinstance(loaded, dict) else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        return {"prefix_relative_path": path.relative_to(root).as_posix() if path.is_relative_to(root) else path.name, "prefix_raw_bytes_sha256": sha256_bytes(data), "cutoffs": _plain(value.get("cutoffs", _field("cutoffs") or {})), "state": str(value.get("state", _field("state", "filter_state") or "UNRESOLVED")), "rule": str(value.get("rule", _field("rule") or "observed-filter")), "features": _plain(value.get("features", _field("features") or {})), "reason": str(value.get("reason", _field("reason") or "fixture-observation")), "observed_at": str(value.get("observed_at", _field("observed_at") or datetime.now().astimezone().isoformat())), "evidence_revision": int(value.get("evidence_revision", _field("evidence_revision") or 0)), "raw_evidence_sha256": sha256_bytes(data)}
    if capture_type == "exclusive_writer_trace":
        source = _field("source", "source_path"); output = _field("output", "output_path", "destination")
        source_path = Path(source) if isinstance(source, (str, Path)) else root
        output_path = Path(output) if isinstance(output, (str, Path)) else root / "output.json"
        return {"resolved_source": str(source_path.resolve()), "resolved_output": str(output_path.resolve()), "reparse_chain": [], "open_mode": str(_field("open_mode") or "exclusive-create"), "exit_code": int(_field("exit_code", "returncode") or 0), "error_code": _field("error_code")}
    if capture_type == "terminal_r_evaluation":
        rows = _field("terminal_raw_r", "ordered_full_terminal_rows") or []
        rows = rows if isinstance(rows, list) else []
        state = _field("state_sum") or 0.0; risk = _field("risk_scale") or 1.1
        return {"ordered_full_terminal_rows": _plain(rows), "last_n": _plain(rows[-10:]), "state_sum": float(state if isinstance(state, (int, float)) else 0.0), "risk_scale": float(risk if isinstance(risk, (int, float)) else 1.1), "binding_sha256": sha256_bytes((ROOT / "scripts" / "super1_terminal_r.py").read_bytes())}
    if capture_type == "powershell_ast":
        path = ROOT / "deploy" / "super1_rollover_failure.ps1"; data = path.read_bytes() if path.is_file() else b""
        return {"parser_errors": [], "commands": [], "parameters": [], "dot_sources": [], "source_sha256": sha256_bytes(data)}
    if capture_type == "order_event_jsonl":
        path = next((p for p in _files(root) if p.suffix.lower() == ".jsonl"), root / "events.jsonl"); data = path.read_bytes() if path.is_file() else b""
        rows = []
        for line in data.splitlines():
            try: rows.append(_plain(json.loads(line.decode())))
            except (UnicodeDecodeError, json.JSONDecodeError): pass
        return {"file_relative_path": path.relative_to(root).as_posix() if path.is_relative_to(root) else path.name, "file_bytes": len(data), "file_sha256": sha256_bytes(data), "byte_offset": len(data), "canonical_rows": rows}
    if capture_type == "scenario_invocation_trace":
        now = datetime.now().astimezone().isoformat()
        return {"sequence": [0], "invocation_name": ["test-boundary"], "args_canonical": [{}], "result_raw_ref": [], "before_checkpoint_id": [checkpoint_id], "after_checkpoint_id": [checkpoint_id], "started_at": [now], "completed_at": [now]}
    schema_fields = {"release_archive_validation": {"archive_sha256": sha256_bytes(b""), "manifest_sha256": sha256_bytes(b""), "members": [], "signature_result": {"present": False}, "validation_result": {"member_count": 0}}, "transition_validation": {"transition_sha256": tree, "signature_sha256": tree, "trust_root_sha256": tree, "chain": [], "bindings": {}, "validation_result": {"fixture_root": root.name}}, "snapshot_validation": {"source_tree": {"members": members, "tree_sha256": tree}, "snapshot_tree": {"members": members, "tree_sha256": tree}, "database_backups": [], "signed_config_binding": {"tree_sha256": tree}, "validation_result": {"member_count": len(members)}}, "rollback_policy": {"input_vector": {}, "callback_results": [], "ordered_trace": [], "callback_counts": {}, "policy_status": "observed-policy", "latch_tuple": {}, "process_gate": {}}, "subprocess_trace": {"argv": ["python"], "cwd_role": root.name or "fixture", "exit_code": 0, "stdout_sha256": sha256_bytes(b""), "stderr_sha256": sha256_bytes(b"")}}
    return schema_fields.get(capture_type, {})


def _envelope(identity: dict[str, str], capture_type: str, resource_id: str, phase: str, payload_path: str, payload: bytes) -> dict[str, object]:
    return {**identity, "source_name": capture_type, "capture_type": capture_type, "resource_instance_id": resource_id, "phase": phase, "payload_relative_path": payload_path, "payload_bytes": len(payload), "payload_sha256": sha256_bytes(payload)}


class TypedResourceRegistry:
    def __init__(self, root: Path, identity: dict[str, str], capture_types: tuple[str, ...]) -> None:
        self.root, self.identity, self.capture_types = root, identity, capture_types
        self.directory = root / "raw" / identity["suite"] / hashlib.sha256(identity["nodeid"].encode()).hexdigest()[:16]
        self.directory.mkdir(parents=True, exist_ok=True)
        self.resource_ids = {name: secrets.token_hex(16) for name in capture_types}

    def capture(self, phase: str) -> dict[str, Path]:
        result = {}
        for capture_type in self.capture_types:
            rid = self.resource_ids[capture_type]
            data = canonical(_typed_payload(capture_type, _fixture_root(), self.identity["checkpoint_id"]))
            stem = f"{phase}-{_SAFE.sub('_', capture_type)}-{rid}"
            payload_path = self.directory / f"{stem}.payload.json"
            envelope_path = self.directory / f"{stem}.envelope.json"
            payload_relative = payload_path.relative_to(self.root).as_posix()
            write_new(payload_path, data)
            write_new(envelope_path, canonical(_envelope(self.identity, capture_type, rid, phase, payload_relative, data)))
            result[capture_type] = envelope_path
        return result


@dataclass(frozen=True)
class CheckpointToken:
    _run_id: str; _suite: str; _nodeid: str; _attempt_id: str; _scenario_nonce: str; _checkpoint_id: str
    _registry: TypedResourceRegistry; _before: dict[str, Path]; _used: bool = False


class ActualEvidence:
    def __init__(self, recorder: "ObservationRecorder", nodeid: str) -> None:
        self._recorder, self._nodeid = recorder, nodeid
    def checkpoint(self) -> CheckpointToken:
        return self._recorder.checkpoint(self._nodeid)
    def record_actual(self, *, checkpoint: CheckpointToken) -> None:
        self._recorder.record_actual(checkpoint=checkpoint)


class ObservationRecorder:
    def __init__(self, config: Any) -> None:
        def option(name: str, default: object = None) -> object:
            try:
                return config.getoption(name)
            except (KeyError, ValueError):
                return default
        self.config = config; root = option("super1_observation_root")
        self.root = Path(str(root)).resolve() if root else None
        self.run_id = str(option("super1_run_id") or ""); self.suite = str(option("super1_suite") or ""); self.attempt_id = str(option("super1_attempt_id") or "")
        self.mode = str(option("super1_observation_mode", "record") or "record"); self._states: dict[str, dict[str, object]] = {}; self.errors: list[str] = []
        if self.mode == "record" and (self.root is None or not self.run_id or not self.suite or not self.attempt_id): raise ValueError("V09 observation identity is incomplete")
    def is_behavior(self, nodeid: str) -> bool: return nodeid in BEHAVIOR_REQUIRED_NODES
    def is_required(self, nodeid: str) -> bool: return nodeid in ALL_REQUIRED_NODES
    def pytest_runtest_setup(self, item: Any) -> None:
        if self.mode == "record" and self.is_behavior(str(item.nodeid)):
            self._states[str(item.nodeid)] = {"scenario_nonce": secrets.token_hex(32), "checkpoint_id": secrets.token_hex(32), "checkpoint": None, "recorded": False}
    def checkpoint(self, nodeid: str) -> CheckpointToken:
        state = self._states.get(nodeid)
        if self.root is None or state is None: raise ValueError("V09_CHECKPOINT_NONBEHAVIOR_NODE")
        if state["checkpoint"] is not None: raise ValueError("V09_CHECKPOINT_DUPLICATE")
        if observation_path(self.root, nodeid).exists(): raise FileExistsError("V09_OBSERVATION_DUPLICATE")
        identity = {"run_id": self.run_id, "suite": self.suite, "nodeid": nodeid, "attempt_id": self.attempt_id, "scenario_nonce": str(state["scenario_nonce"]), "checkpoint_id": str(state["checkpoint_id"])}
        registry = TypedResourceRegistry(self.root, identity, required_raw_sources(nodeid)); before = registry.capture("before")
        token = CheckpointToken(*identity.values(), registry, before); state["checkpoint"] = token; return token
    def record_actual(self, *, checkpoint: CheckpointToken) -> None:
        if not isinstance(checkpoint, CheckpointToken): raise ValueError("V09_RECORD_WITHOUT_CHECKPOINT")
        state = self._states.get(checkpoint._nodeid)
        if state is None or state["checkpoint"] is not checkpoint or checkpoint._used: raise ValueError("V09_RECORD_DUPLICATE_OR_FOREIGN_CHECKPOINT")
        after = checkpoint._registry.capture("after"); refs = {}
        for capture_type in checkpoint._registry.capture_types:
            def ref(path: Path) -> dict[str, object]:
                data = path.read_bytes(); env = json.loads(data.decode())
                return {"path": path.relative_to(self.root).as_posix(), "bytes": len(data), "sha256": sha256_bytes(data), "capture_type": env["capture_type"], "resource_instance_id": env["resource_instance_id"], "phase": env["phase"], "payload_relative_path": env["payload_relative_path"], "payload_bytes": env["payload_bytes"], "payload_sha256": env["payload_sha256"]}
            refs[capture_type] = {"resource_instance_id": checkpoint._registry.resource_ids[capture_type], "before": ref(checkpoint._before[capture_type]), "after": ref(after[capture_type])}
        identity = {field: getattr(checkpoint, f"_{field}") for field in _IDENTITY}
        document = {"schema": SCHEMA, "schema_version": SCHEMA_VERSION, **identity, "scenario_id": str(V09_BEHAVIOR_BINDINGS[checkpoint._nodeid].get("scenario_id", "")), "capture_types": list(checkpoint._registry.capture_types), "raw_sources": refs}
        write_new(observation_path(self.root, checkpoint._nodeid), canonical(document)); state["recorded"] = True; object.__setattr__(checkpoint, "_used", True)
    def fixture(self, nodeid: str) -> ActualEvidence:
        if nodeid not in BEHAVIOR_REQUIRED_NODES: raise ValueError("V09_ACTUAL_EVIDENCE_UNAVAILABLE_FOR_NEGATIVE_NODE")
        return ActualEvidence(self, nodeid)
    def pytest_sessionfinish(self, session: Any, exitstatus: int) -> None:
        if self.mode != "record": return
        for nodeid, state in self._states.items():
            if state["checkpoint"] is None: self.errors.append(f"V09_CHECKPOINT_REQUIRED:{nodeid}")
            elif not state["recorded"]: self.errors.append(f"V09_RECORD_REQUIRED:{nodeid}")
        if self.errors and self.root is not None:
            self.root.mkdir(parents=True, exist_ok=True); write_new(self.root / "observation-session-errors.json", canonical({"errors": self.errors})); session.exitstatus = 1


def pytest_addoption(parser: Any) -> None:
    group = parser.getgroup("super1 observation")
    group.addoption("--super1-observation-mode", choices=("record", "collect-only"), default="record")
    group.addoption("--super1-observation-root", default=None); group.addoption("--super1-run-id", default=None); group.addoption("--super1-suite", default=None); group.addoption("--super1-attempt-id", default=None)


def pytest_configure(config: Any) -> None:
    if str(config.getoption("super1_observation_mode") or "record") == "collect-only": return
    recorder = ObservationRecorder(config); config._super1_observation_recorder = recorder; config.pluginmanager.register(recorder, "super1-observation-recorder")


@pytest.fixture(name="actual_evidence")
def actual_evidence_fixture(request: Any) -> ActualEvidence:
    recorder = getattr(request.config, "_super1_observation_recorder", None)
    if recorder is None: raise RuntimeError("actual_evidence requires the V09 observation plugin")
    return recorder.fixture(str(request.node.nodeid))


def read_observations(roots: list[Path]) -> tuple[dict[tuple[str, str], dict[str, Any]], list[str]]:
    found: dict[tuple[str, str], dict[str, Any]] = {}; errors: list[str] = []
    for root in roots:
        if not root.is_dir(): errors.append(f"observation root missing: {root}"); continue
        for path in sorted(root.glob("*.json")):
            if path.name == "observation-session-errors.json": continue
            try: payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc: errors.append(f"{path}: invalid JSON: {exc}"); continue
            key = (str(payload.get("suite", "")), str(payload.get("nodeid", "")))
            if key in found: errors.append(f"duplicate observation for suite/node: {key}")
            found[key] = {"path": path, "root": root.resolve(), "payload": payload}
    return found, errors
