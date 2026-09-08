"""Monotonic strategy-health lifecycle driven by locked OOS baseline facts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


class StrategyHealthError(RuntimeError):
    pass


class StrategyHealthState(str):
    UNKNOWN = "UNKNOWN"
    WARMUP = "WARMUP"
    ACTIVE = "ACTIVE"
    MONITORING = "MONITORING"
    DECAYED = "DECAYED"
    DISABLED = "DISABLED"


@dataclass(frozen=True, slots=True)
class LockedOOSBaseline:
    candidate_hash: str
    closed_trades: int
    valid_sessions: int
    years: tuple[int, ...]
    rolling_net_r_p05: float
    drawdown_p95: float
    drawdown_p99: float
    seed: int
    locked: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_hash, str) or not self.candidate_hash.strip():
            raise StrategyHealthError("locked baseline must identify a candidate artifact")
        if not isinstance(self.closed_trades, int) or self.closed_trades < 0:
            raise StrategyHealthError("baseline closed_trades must be non-negative")
        if not isinstance(self.valid_sessions, int) or self.valid_sessions < 0:
            raise StrategyHealthError("baseline valid_sessions must be non-negative")
        if not isinstance(self.years, tuple) or any(not isinstance(year, int) for year in self.years):
            raise StrategyHealthError("baseline years must be a tuple of integers")
        for name in ("rolling_net_r_p05", "drawdown_p95", "drawdown_p99"):
            value = float(getattr(self, name))
            if not math.isfinite(value):
                raise StrategyHealthError(f"baseline {name} must be finite")
        if not isinstance(self.seed, int) or isinstance(self.seed, bool):
            raise StrategyHealthError("baseline seed must be an integer")
        if not isinstance(self.locked, bool):
            raise StrategyHealthError("baseline lock flag must be boolean")

    @property
    def sufficient(self) -> bool:
        return self.closed_trades >= 100 and self.valid_sessions > 0 and len(set(self.years)) >= 3


@dataclass(frozen=True, slots=True)
class HealthDecision:
    state: str
    reason: str
    insufficient_baseline: bool = False


@dataclass(frozen=True, slots=True)
class BrokerHealthMetrics:
    rolling_net_r: float
    drawdown_r: float
    fill_count: int
    session_count: int
    deal_ids: tuple[str, ...]
    session_ids: tuple[str, ...]


class StrategyHealth:
    ORDER = (StrategyHealthState.UNKNOWN, StrategyHealthState.ACTIVE, StrategyHealthState.MONITORING, StrategyHealthState.DECAYED, StrategyHealthState.DISABLED)

    def __init__(self, state: str = StrategyHealthState.UNKNOWN, *, baseline: LockedOOSBaseline | None = None, expected_candidate_hash: str | None = None, audit: Callable[[Mapping[str, Any]], Any] | None = None) -> None:
        if state not in self.ORDER:
            raise StrategyHealthError("unknown strategy health state")
        self.state = state
        self.baseline = baseline
        if expected_candidate_hash is not None and (baseline is None or baseline.candidate_hash != expected_candidate_hash):
            raise StrategyHealthError("baseline candidate hash does not match signed candidate")
        self.audit = audit
        self.closed_fills = 0
        self.broker_deal_ids: tuple[str, ...] = ()
        self.valid_sessions = 0
        self.checkpoint_fills = 0
        self.rolling_net_r = 0.0
        self.drawdown_r = 0.0
        self.session_ids: tuple[str, ...] = ()
        self.last_decision: HealthDecision | None = None

    def _record(self, decision: HealthDecision) -> HealthDecision:
        self.last_decision = decision
        if self.audit is not None:
            payload = {
                "event": "STRATEGY_HEALTH_EVALUATION",
                "state": decision.state,
                "reason": decision.reason,
                "insufficient_baseline": decision.insufficient_baseline,
            }
            if callable(self.audit):
                self.audit(payload)
            elif hasattr(self.audit, "append"):
                self.audit.append(
                    "STRATEGY_HEALTH_EVALUATION",
                    entity_type="strategy",
                    entity_id=self.baseline.candidate_hash if self.baseline is not None else "",
                    payload=payload,
                )
        return decision

    def transition(self, target: str, *, reason: str = "") -> str:
        if target not in self.ORDER:
            raise StrategyHealthError("unknown strategy health state")
        if self.ORDER.index(target) < self.ORDER.index(self.state):
            raise StrategyHealthError("strategy health is monotonic; rollback is not allowed")
        self.state = target
        return self.state

    @staticmethod
    def _timestamp(value: Any, *, deal_id: str) -> datetime:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
        elif isinstance(value, str) and value.strip():
            try:
                parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError as exc:
                raise StrategyHealthError(f"broker deal {deal_id} has an invalid terminal timestamp") from exc
        else:
            raise StrategyHealthError(f"broker deal {deal_id} has no terminal timestamp")
        if parsed.tzinfo is None:
            raise StrategyHealthError(f"broker deal {deal_id} terminal timestamp has no timezone")
        return parsed.astimezone(timezone.utc)

    @classmethod
    def metrics_from_broker_deals(cls, broker_deals: Iterable[Mapping[str, Any]]) -> BrokerHealthMetrics:
        rows: list[tuple[datetime, str, float, str]] = []
        seen: set[str] = set()
        for deal in broker_deals:
            if not isinstance(deal, Mapping):
                raise StrategyHealthError("reconciled broker deals must be mappings")
            deal_id = str(deal.get("deal_id") or "").strip()
            if not deal_id or deal_id in seen:
                raise StrategyHealthError("broker deals require unique terminal deal_id")
            seen.add(deal_id)
            raw_r = next((deal.get(key) for key in ("r_multiple", "r", "net_r") if deal.get(key) is not None), None)
            try:
                r_value = float(raw_r)
            except (TypeError, ValueError, OverflowError) as exc:
                raise StrategyHealthError(f"broker deal {deal_id} has no numeric R conversion") from exc
            if not math.isfinite(r_value):
                raise StrategyHealthError(f"broker deal {deal_id} has a non-finite R conversion")
            closed_at = cls._timestamp(
                next((deal.get(key) for key in ("terminal_known_time", "closed_at", "time") if deal.get(key) is not None), None),
                deal_id=deal_id,
            )
            session_id = str(
                next((deal.get(key) for key in ("session_id", "session_date", "trade_date") if deal.get(key) is not None), "")
            ).strip()
            if not session_id:
                session_id = closed_at.date().isoformat()
            rows.append((closed_at, deal_id, r_value, session_id))
        rows.sort(key=lambda item: (item[0], item[1]))
        values = [item[2] for item in rows]
        equity = 0.0
        peak = 0.0
        drawdown = 0.0
        for value in values:
            equity += value
            peak = max(peak, equity)
            drawdown = max(drawdown, peak - equity)
        return BrokerHealthMetrics(
            rolling_net_r=float(sum(values[-30:])),
            drawdown_r=float(drawdown),
            fill_count=len(rows),
            session_count=len({item[3] for item in rows}),
            deal_ids=tuple(item[1] for item in rows),
            session_ids=tuple(sorted({item[3] for item in rows})),
        )

    def _apply_broker_metrics(self, metrics: BrokerHealthMetrics) -> int:
        previous_ids = set(self.broker_deal_ids)
        self.rolling_net_r = metrics.rolling_net_r
        self.drawdown_r = metrics.drawdown_r
        self.closed_fills = metrics.fill_count
        self.valid_sessions = metrics.session_count
        self.broker_deal_ids = metrics.deal_ids
        self.session_ids = metrics.session_ids
        new_fills = len(set(metrics.deal_ids) - previous_ids)
        self.checkpoint_fills += new_fills
        return new_fills

    def activate(self, *, broker_deals: Iterable[Mapping[str, Any]]) -> HealthDecision:
        self._apply_broker_metrics(self.metrics_from_broker_deals(broker_deals))
        if self.baseline is None or not self.baseline.locked or not self.baseline.sufficient:
            return self._record(HealthDecision(self.state, "INSUFFICIENT_BASELINE", True))
        if self.state == StrategyHealthState.UNKNOWN and self.valid_sessions > 0:
            self.transition(StrategyHealthState.ACTIVE)
        return self._record(HealthDecision(self.state, "WARMUP_COMPLETE"))

    def evaluate(
        self,
        *,
        broker_deals: Iterable[Mapping[str, Any]],
    ) -> HealthDecision:
        metrics = self.metrics_from_broker_deals(broker_deals)
        self._apply_broker_metrics(metrics)
        if self.baseline is None or not self.baseline.locked:
            return self._record(HealthDecision(self.state, "INSUFFICIENT_BASELINE", True))
        if not self.baseline.sufficient:
            return self._record(HealthDecision(self.state, "INSUFFICIENT_BASELINE", True))
        if self.closed_fills < 30 or self.valid_sessions < 60:
            return self._record(HealthDecision(self.state, "INSUFFICIENT_LIVE_SAMPLE"))
        violation = self.rolling_net_r < self.baseline.rolling_net_r_p05 or self.drawdown_r > self.baseline.drawdown_p95
        severe = self.drawdown_r > self.baseline.drawdown_p99
        if self.state in {StrategyHealthState.ACTIVE, StrategyHealthState.MONITORING} and violation:
            if severe or (self.state == StrategyHealthState.MONITORING and self.checkpoint_fills >= 10):
                self.transition(StrategyHealthState.DECAYED)
                return self._record(HealthDecision(self.state, "DECAY_CONFIRMED"))
            self.transition(StrategyHealthState.MONITORING)
            self.checkpoint_fills = 0
            return self._record(HealthDecision(self.state, "BASELINE_P05_OR_P95_BREACH"))
        return self._record(HealthDecision(self.state, "WITHIN_BASELINE"))

    def disable(self, *, reason: str = "") -> HealthDecision:
        self.transition(StrategyHealthState.DISABLED, reason=reason)
        return self._record(HealthDecision(self.state, reason or "OPERATOR_DISABLED"))

    def allows_new_position(self) -> bool:
        return self.state in {StrategyHealthState.ACTIVE, StrategyHealthState.MONITORING}

    def health_json(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "closed_fills": self.closed_fills,
            "valid_sessions": self.valid_sessions,
            "rolling_window_closed_fills": min(self.closed_fills, 30),
            "rolling_net_r": self.rolling_net_r,
            "drawdown_r": self.drawdown_r,
            "last_decision": None if self.last_decision is None else asdict(self.last_decision),
            "baseline_candidate_hash": None if self.baseline is None else self.baseline.candidate_hash,
            "broker_deal_ids": list(self.broker_deal_ids),
            "session_ids": list(self.session_ids),
        }

    def write_health(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.health_json(), sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, target)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def persist_canonical(self, path: str | Path) -> None:
        """Persist health and its locked baseline in the canonical order DB."""
        if self.baseline is None or not self.baseline.locked:
            raise StrategyHealthError("canonical health requires a locked baseline")
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(target, timeout=30.0, isolation_level=None)
        try:
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS strategy_health_state (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    state TEXT NOT NULL,
                    baseline_candidate_hash TEXT NOT NULL,
                    baseline_json TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                )"""
            )
            metrics = {
                "closed_fills": self.closed_fills,
                "valid_sessions": self.valid_sessions,
                "rolling_net_r": self.rolling_net_r,
                "drawdown_r": self.drawdown_r,
                "broker_deal_ids": list(self.broker_deal_ids),
                "session_ids": list(self.session_ids),
            }
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO strategy_health_state(singleton,state,baseline_candidate_hash,baseline_json,metrics_json,updated_at_utc)
                   VALUES(1,?,?,?,?,?)
                   ON CONFLICT(singleton) DO UPDATE SET state=excluded.state,
                   baseline_candidate_hash=excluded.baseline_candidate_hash, baseline_json=excluded.baseline_json,
                   metrics_json=excluded.metrics_json, updated_at_utc=excluded.updated_at_utc""",
                (self.state, self.baseline.candidate_hash, json.dumps(asdict(self.baseline), sort_keys=True, separators=(",", ":")), json.dumps(metrics, sort_keys=True, separators=(",", ":")), datetime.now(timezone.utc).isoformat()),
            )
            connection.commit()
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @classmethod
    def load_canonical(
        cls,
        path: str | Path,
        *,
        baseline: LockedOOSBaseline,
        expected_candidate_hash: str,
        audit: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> "StrategyHealth":
        connection = sqlite3.connect(Path(path), timeout=30.0)
        try:
            row = connection.execute(
                "SELECT state,baseline_candidate_hash,baseline_json,metrics_json FROM strategy_health_state WHERE singleton=1"
            ).fetchone()
        except sqlite3.Error as exc:
            raise StrategyHealthError("canonical strategy-health state is missing") from exc
        finally:
            connection.close()
        if row is None:
            raise StrategyHealthError("canonical strategy-health state is missing")
        if str(row[1]) != expected_candidate_hash or str(row[1]) != baseline.candidate_hash:
            raise StrategyHealthError("canonical strategy-health candidate hash mismatch")
        try:
            baseline_json = json.loads(str(row[2]))
            metrics = json.loads(str(row[3]))
        except json.JSONDecodeError as exc:
            raise StrategyHealthError("canonical strategy-health JSON is corrupt") from exc
        canonical_baseline = json.loads(json.dumps(asdict(baseline), sort_keys=True, separators=(",", ":")))
        if baseline_json != canonical_baseline or not isinstance(metrics, dict):
            raise StrategyHealthError("canonical strategy-health baseline was mutated")
        health = cls(str(row[0]), baseline=baseline, expected_candidate_hash=expected_candidate_hash, audit=audit)
        try:
            health.closed_fills = int(metrics["closed_fills"])
            health.valid_sessions = int(metrics["valid_sessions"])
            health.rolling_net_r = float(metrics["rolling_net_r"])
            health.drawdown_r = float(metrics["drawdown_r"])
            health.broker_deal_ids = tuple(str(value) for value in metrics["broker_deal_ids"])
            health.session_ids = tuple(str(value) for value in metrics["session_ids"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise StrategyHealthError("canonical strategy-health metrics are corrupt") from exc
        if health.closed_fills != len(set(health.broker_deal_ids)) or health.valid_sessions != len(set(health.session_ids)):
            raise StrategyHealthError("canonical strategy-health counters are not derived from unique broker facts")
        if not all(math.isfinite(value) for value in (health.rolling_net_r, health.drawdown_r)):
            raise StrategyHealthError("canonical strategy-health metrics are non-finite")
        return health

    @classmethod
    def load_health(cls, path: str | Path, *, baseline: LockedOOSBaseline | None = None, expected_candidate_hash: str | None = None, audit: Callable[[Mapping[str, Any]], Any] | None = None) -> "StrategyHealth":
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            state = str(payload["state"])
            if state in {StrategyHealthState.ACTIVE, StrategyHealthState.MONITORING, StrategyHealthState.DECAYED} and not payload.get("canonical_state_hash"):
                raise StrategyHealthError("mutable strategy-health JSON cannot authorize an active state")
            health = cls(state, baseline=baseline, expected_candidate_hash=expected_candidate_hash, audit=audit)
            health.closed_fills = int(payload.get("closed_fills", 0))
            health.valid_sessions = int(payload.get("valid_sessions", 0))
            health.rolling_net_r = float(payload.get("rolling_net_r", 0.0))
            health.drawdown_r = float(payload.get("drawdown_r", 0.0))
            health.broker_deal_ids = tuple(str(item) for item in payload.get("broker_deal_ids", ()))
            health.session_ids = tuple(str(item) for item in payload.get("session_ids", ()))
            if health.closed_fills != len(set(health.broker_deal_ids)):
                raise StrategyHealthError("persisted health count is not derived from unique deal IDs")
            if health.valid_sessions != len(set(health.session_ids)):
                raise StrategyHealthError("persisted session count is not derived from broker deals")
            return health
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StrategyHealthError("strategy health state is missing or corrupt") from exc
