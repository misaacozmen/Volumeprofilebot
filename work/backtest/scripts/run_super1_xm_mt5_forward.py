from __future__ import annotations

import json
import hashlib
import math
import os
from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_capital_forward as core
import run_xm_mt5_forward as xm
from candidate_artifact import ArtifactValidationError, load_artifact
from super1_terminal_r import calculate_terminal_r


RUNTIME_CONFIG = ROOT / "live_forward" / "super1_xm_mt5_demo_config.json"
SUPER1_MANIFEST = ROOT / "research_candidates" / "super1" / "super1_manifest.json"
FORWARD_SHADOW_ADAPTER = ROOT / "scripts" / "run_forward_shadow.py"
DEPLOYMENT_MODE = "FROZEN_CANONICAL_PAIR_PIPELINE_WITH_SUPER1_OVERLAY"
REQUIRED_ENV = ("XM_MT5_SERVER",)
RTH_CALENDAR_RELATIVE = "live_forward/calendars/us_equity_rth_2026.json"
RTH_CALENDAR_STATES = {"OPEN", "EARLY_CLOSE", "CLOSED"}


class Super1FeatureError(core.CriticalLiveError):
    pass


def _reject_duplicate_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Super1FeatureError(f"RTH calendar contains duplicate JSON key: {key}.")
        result[key] = value
    return result


def _safe_repo_file(relative: object, label: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise Super1FeatureError(f"{label} must be a repository-relative file path.")
    candidate = ROOT / Path(relative)
    resolved = candidate.resolve()
    try:
        resolved.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise Super1FeatureError(f"{label} escapes the repository.") from exc
    if any(part.is_symlink() for part in [candidate, *candidate.parents]):
        raise Super1FeatureError(f"{label} resolves through a reparse/symlink path.")
    if not resolved.is_file():
        raise Super1FeatureError(f"{label} is missing.")
    return resolved


def load_verified_rth_calendar(runtime: dict[str, Any]) -> dict[str, Any]:
    reference = runtime.get("rth_session_calendar")
    if not isinstance(reference, dict):
        raise Super1FeatureError("Verified RTH calendar reference is missing.")
    if (
        reference.get("path") != RTH_CALENDAR_RELATIVE
        or reference.get("calendar_id") != "US_EQUITY_RTH_2026"
        or not isinstance(reference.get("sha256"), str)
    ):
        raise Super1FeatureError("RTH calendar reference is not the sealed production format.")
    path = _safe_repo_file(reference["path"], "RTH calendar")
    raw = path.read_bytes()
    actual_hash = core.file_hash(path)
    if actual_hash != reference["sha256"]:
        raise Super1FeatureError("RTH calendar raw hash mismatch.")
    try:
        calendar = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_pairs
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Super1FeatureError("RTH calendar JSON is invalid.") from exc
    if not isinstance(calendar, dict):
        raise Super1FeatureError("RTH calendar root must be an object.")
    if (
        calendar.get("schema_version") != 1
        or calendar.get("calendar_id") != "US_EQUITY_RTH_2026"
        or calendar.get("timezone") != core.TZ
    ):
        raise Super1FeatureError("RTH calendar identity or timezone is invalid.")
    coverage = calendar.get("coverage")
    if not isinstance(coverage, dict) or set(coverage) != {"start", "end"}:
        raise Super1FeatureError("RTH calendar coverage is invalid.")
    try:
        coverage_start = pd.Timestamp(coverage["start"]).date()
        coverage_end = pd.Timestamp(coverage["end"]).date()
    except (TypeError, ValueError) as exc:
        raise Super1FeatureError("RTH calendar coverage dates are invalid.") from exc
    expected_dates = pd.date_range(coverage_start, coverage_end, freq="1D")
    session_rows = calendar.get("sessions")
    if not isinstance(session_rows, list) or len(session_rows) != len(expected_dates):
        raise Super1FeatureError("RTH calendar does not cover every date exactly once.")
    sessions: dict[str, dict[str, Any]] = {}
    for row in session_rows:
        if not isinstance(row, dict) or not isinstance(row.get("date"), str):
            raise Super1FeatureError("RTH calendar contains an invalid session row.")
        date_key = row["date"]
        if date_key in sessions:
            raise Super1FeatureError(f"RTH calendar contains duplicate date: {date_key}.")
        try:
            parsed_date = pd.Timestamp(date_key).date()
        except (TypeError, ValueError) as exc:
            raise Super1FeatureError(f"RTH calendar date is invalid: {date_key}.") from exc
        if parsed_date < coverage_start or parsed_date > coverage_end:
            raise Super1FeatureError(f"RTH calendar date is outside coverage: {date_key}.")
        state = str(row.get("state") or "")
        if state not in RTH_CALENDAR_STATES:
            raise Super1FeatureError(f"RTH calendar state is invalid: {date_key}.")
        if state == "CLOSED":
            if "start" in row or "end" in row:
                raise Super1FeatureError(f"Closed RTH row must not contain hours: {date_key}.")
        else:
            start = row.get("start")
            end = row.get("end")
            if not isinstance(start, str) or not isinstance(end, str):
                raise Super1FeatureError(f"Open RTH row has no complete hours: {date_key}.")
            try:
                start_time = pd.Timestamp(f"{date_key} {start}", tz=core.TZ)
                end_time = pd.Timestamp(f"{date_key} {end}", tz=core.TZ)
            except (TypeError, ValueError) as exc:
                raise Super1FeatureError(f"RTH hours are invalid: {date_key}.") from exc
            if start_time.strftime("%H:%M") != start or end_time.strftime("%H:%M") != end:
                raise Super1FeatureError(f"RTH hours are not canonical local times: {date_key}.")
            if start_time >= end_time or start != "09:30" or end not in {"13:00", "16:00"}:
                raise Super1FeatureError(f"RTH hours are outside the US equity core session: {date_key}.")
            if state == "EARLY_CLOSE" and end != "13:00":
                raise Super1FeatureError(f"Early-close row has the wrong end time: {date_key}.")
            if state == "OPEN" and end != "16:00":
                raise Super1FeatureError(f"Open row has the wrong end time: {date_key}.")
        sessions[date_key] = row
    if set(sessions) != {item.strftime("%Y-%m-%d") for item in expected_dates}:
        raise Super1FeatureError("RTH calendar date coverage has gaps or extra dates.")
    source_records = calendar.get("source_records")
    if not isinstance(source_records, list) or not source_records:
        raise Super1FeatureError("RTH calendar source provenance is missing.")
    source_ids = [source.get("id") for source in source_records if isinstance(source, dict)]
    if source_ids != ["NASDAQ_TRADING_CALENDAR_2026", "NYSE_TRADING_CALENDAR_2026"]:
        raise Super1FeatureError("RTH calendar source provenance must contain both official 2026 records in order.")
    expected_urls = {
        "NASDAQ_TRADING_CALENDAR_2026": "https://www.nasdaqtrader.com/Trader.aspx?id=calendar",
        "NYSE_TRADING_CALENDAR_2026": "https://www.nyse.com/publicdocs/nyse/ICE_NYSE_2026_Yearly_Trading_Calendar.pdf",
    }
    for source in source_records:
        if not isinstance(source, dict):
            raise Super1FeatureError("RTH calendar source provenance row is invalid.")
        source_path = _safe_repo_file(source.get("provenance_path"), "RTH source provenance")
        if source.get("sha256") != core.file_hash(source_path):
            raise Super1FeatureError(
                f"RTH source provenance hash mismatch: {source_path.name}."
            )
        if source.get("bytes") != source_path.stat().st_size:
            raise Super1FeatureError(
                f"RTH source provenance byte count mismatch: {source_path.name}."
            )
        if source.get("url") != expected_urls.get(source.get("id")):
            raise Super1FeatureError("RTH source provenance URL is invalid.")
    return {
        "path": str(path),
        "sha256": actual_hash,
        "calendar_id": calendar["calendar_id"],
        "coverage": coverage,
        "sessions": sessions,
        "source_records": source_records,
    }


def validate_super1_candidate(runtime: dict[str, Any]) -> dict[str, Any]:
    relative = runtime.get("candidate_path")
    if not isinstance(relative, str) or not relative:
        raise Super1FeatureError("Super1 runtime has no candidate_path.")
    candidate_path = (ROOT / relative).resolve()
    try:
        candidate_path.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise Super1FeatureError("Super1 candidate path escapes the repository.") from exc
    expected_file_hash = str(runtime.get("candidate_file_sha256") or "")
    if not candidate_path.is_file() or core.file_hash(candidate_path) != expected_file_hash:
        raise Super1FeatureError("Super1 candidate file hash mismatch.")
    try:
        candidate = load_artifact(
            candidate_path,
            ROOT,
            artifact_type="strategy_candidate",
            verify_inputs=True,
        )
    except ArtifactValidationError as exc:
        raise Super1FeatureError(f"Super1 candidate artifact is invalid: {exc}") from exc
    if candidate.get("artifact_sha256") != runtime.get("candidate_artifact_sha256"):
        raise Super1FeatureError("Super1 candidate artifact SHA mismatch.")
    if candidate.get("setup_rules") != runtime.get("setup_rules"):
        raise Super1FeatureError("Super1 setup rules differ from the sealed candidate.")
    candidate_risk = candidate.get("risk_rule")
    runtime_risk = runtime.get("risk_rule")
    if not isinstance(candidate_risk, dict) or not isinstance(runtime_risk, dict):
        raise Super1FeatureError("Super1 risk rule is missing.")
    if {key: runtime_risk.get(key) for key in candidate_risk} != candidate_risk:
        raise Super1FeatureError("Super1 risk rule differs from the sealed candidate.")
    if (
        candidate.get("live_enabled") is not False
        or candidate.get("proven") is not False
        or candidate.get("fresh_forward_required") is not True
        or not all(bool(value) for value in candidate.get("checks", {}).values())
    ):
        raise Super1FeatureError("Super1 candidate safety status is invalid.")
    protocol = candidate.get("selection_protocol", {})
    if protocol.get("final_holdout_used_for_selection") is not False or protocol.get(
        "final_holdout_used_for_publication"
    ) is not False:
        raise Super1FeatureError("Super1 candidate holdout contract is invalid.")

    contract_relative = runtime.get("signal_contract_path")
    if not isinstance(contract_relative, str) or not contract_relative:
        raise Super1FeatureError("Super1 runtime has no signal contract.")
    contract_path = (ROOT / contract_relative).resolve()
    try:
        contract_path.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise Super1FeatureError("Super1 signal contract escapes the repository.") from exc
    expected_contract_hash = str(runtime.get("signal_contract_sha256") or "")
    if not contract_path.is_file() or core.file_hash(contract_path) != expected_contract_hash:
        raise Super1FeatureError("Super1 signal contract hash mismatch.")
    contract = core.read_json(contract_path)
    signal_source = contract.get("signal_source", {})
    overlay = contract.get("overlay_candidate", {})
    order_transport = contract.get("demo_order_transport", {})
    calendar_contract = contract.get("rth_session_calendar", {})
    safety = contract.get("safety", {})
    source_config = (ROOT / str(signal_source.get("config_path") or "")).resolve()
    source_generator = (ROOT / str(signal_source.get("generator_path") or "")).resolve()
    source_adapter = (ROOT / str(signal_source.get("payload_adapter_path") or "")).resolve()
    overlay_runtime = (ROOT / str(overlay.get("runtime_path") or "")).resolve()
    order_transport_path = (ROOT / str(order_transport.get("path") or "")).resolve()
    try:
        source_config.relative_to(ROOT.resolve())
        source_generator.relative_to(ROOT.resolve())
        source_adapter.relative_to(ROOT.resolve())
        overlay_runtime.relative_to(ROOT.resolve())
        order_transport_path.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise Super1FeatureError("Super1 signal contract path escapes the repository.") from exc
    if (
        int(contract.get("schema_version", 0)) != 2
        or contract.get("name") != "SUPER1_CANONICAL_OVERLAY_FRESH_FORWARD_V1"
        or contract.get("status") != "DEMO_FRESH_FORWARD_UNPROVEN"
        or contract.get("execution_scope") != "XM_MT5_DEMO_ONLY"
        or contract.get("deployment_mode") != DEPLOYMENT_MODE
        or signal_source.get("kind") != "FROZEN_CANONICAL_PAIR_PIPELINE"
        or not source_config.is_file()
        or core.file_hash(source_config) != signal_source.get("config_sha256")
        or not source_generator.is_file()
        or core.file_hash(source_generator) != signal_source.get("generator_sha256")
        or not source_adapter.is_file()
        or source_adapter != FORWARD_SHADOW_ADAPTER.resolve()
        or core.file_hash(source_adapter) != signal_source.get("payload_adapter_sha256")
        or core.source_code_hash() != signal_source.get("engine_source_sha256")
        or overlay.get("path") != relative
        or overlay.get("file_sha256") != expected_file_hash
        or overlay.get("artifact_sha256") != candidate.get("artifact_sha256")
        or overlay.get("scope") != "SETUP_FILTERS_AND_RISK_SCALING_ONLY"
        or overlay.get("defines_base_signals") is not False
        or overlay_runtime != Path(__file__).resolve()
        or core.file_hash(overlay_runtime) != overlay.get("runtime_sha256")
        or order_transport_path != Path(xm.__file__).resolve()
        or core.file_hash(order_transport_path) != order_transport.get("sha256")
            or order_transport.get("account_identity_gate")
            != "XM_FIXED_DEMO_TRADE_MODE_SERVER_COMPANY_LOGIN"
            or calendar_contract != runtime.get("rth_session_calendar")
            or safety.get("independent_super1_signal_producer_present") is not False
        or safety.get("demo_order_execution_enabled") is not True
        or safety.get("real_money_live_enabled") is not False
        or safety.get("fresh_forward_required") is not True
        or safety.get("real_money_execution_allowed") is not False
        or safety.get("deployed_pipeline_historical_parity_proven") is not False
        or safety.get("candidate_research_results_apply_to_deployed_pipeline") is not False
        or "live_enabled" in safety
        or runtime.get("execution") != "MT5_DEMO_ORDERS"
        or runtime.get("account_mode") != "DEMO_ORDER"
        or runtime.get("deployment_mode") != DEPLOYMENT_MODE
    ):
        raise Super1FeatureError("Super1 signal contract is invalid or does not match runtime bytes.")

    manifest = core.read_json(SUPER1_MANIFEST)
    research_inputs = [
        item
        for item in candidate.get("provenance", {}).get("inputs", [])
        if item.get("role") == "research_dataset"
    ]
    if len(research_inputs) != 1:
        raise Super1FeatureError("Super1 candidate research dataset provenance is not unique.")
    deployment = manifest.get("deployment", {})
    if (
        manifest.get("name") != "Super1"
        or manifest.get("status") != "LOCAL_FRESH_FORWARD_CANDIDATE_UNPROVEN"
        or manifest.get("candidate_path") != relative
        or manifest.get("candidate_file_sha256") != expected_file_hash
        or manifest.get("candidate_artifact_sha256") != candidate.get("artifact_sha256")
        or manifest.get("signal_contract_path") != contract_relative
        or manifest.get("signal_contract_sha256") != expected_contract_hash
        or manifest.get("deployment_mode") != DEPLOYMENT_MODE
        or manifest.get("config_sha256") != core.file_hash(RUNTIME_CONFIG)
        or manifest.get("overlay_candidate_research_dataset_sha256")
        != research_inputs[0].get("sha256")
        or manifest.get("overlay_candidate_research_result_sha256")
        != candidate.get("full_evaluation_result_sha256")
        or manifest.get("deployed_pipeline_historical_parity_proven") is not False
        or "deployed_pipeline_result_sha256" not in manifest
        or manifest.get("deployed_pipeline_result_sha256") is not None
        or "data_sha256" in manifest
        or "result_sha256" in manifest
        or deployment.get("demo_order_execution_enabled") is not True
        or deployment.get("real_money_live_enabled") is not False
        or deployment.get("real_money_execution_allowed") is not False
        or "live_enabled" in deployment
        or manifest.get("fresh_forward_required") is not True
        or manifest.get("proven") is not False
    ):
        raise Super1FeatureError("Super1 manifest is not bound to runtime and candidate bytes.")
    load_verified_rth_calendar(runtime)
    return candidate


def _day_row(record: dict[str, Any], leg_key: str) -> dict[str, Any]:
    matches = [row for row in record["payload"]["days"] if str(row.get("leg_key")) == leg_key]
    if len(matches) != 1:
        raise Super1FeatureError(f"{leg_key}: current-day feature row is not unique.")
    return matches[0]


def _level_price(day: dict[str, Any], level_name: str) -> float:
    liquidity = day.get("liquidity") or {}
    if str(liquidity.get("selected_name") or "") == level_name and liquidity.get("selected_price") is not None:
        return float(liquidity["selected_price"])
    names = list(liquidity.get("candidate_names") or [])
    prices = list(liquidity.get("candidate_prices") or [])
    if len(names) != len(prices):
        raise Super1FeatureError("Liquidity candidate names/prices are inconsistent.")
    exact = [float(price) for name, price in zip(names, prices) if str(name) == level_name]
    if len(exact) == 1:
        return exact[0]
    if level_name.lower() == "vah" and day.get("vah") is not None:
        return float(day["vah"])
    if level_name.lower() == "val" and day.get("val") is not None:
        return float(day["val"])
    raise Super1FeatureError(f"Liquidity level price cannot be proven for {level_name}.")


def liquidity_type(record: dict[str, Any], decision: dict[str, Any], config: Any) -> str:
    context = str(decision.get("liquidity_context") or "").strip()
    if not context:
        raise Super1FeatureError("Decision has no liquidity context.")
    level = context.split()[0]
    if level.startswith("asia_"):
        return "asia_high_low"
    if level.startswith("london_"):
        return "london_high_low"
    if level.startswith("ny_am_"):
        return "ny_am_high_low"
    if level.startswith(("ny_pm_", "previous_day_")):
        return "session_high_low"
    if level.startswith("swing_"):
        touches = (
            record.get("graph_features", {})
            .get(str(decision["leg_key"]), {})
            .get("swing_touches", {})
            .get(level)
        )
        if touches is None:
            raise Super1FeatureError(f"Swing touch count cannot be proven for {level}.")
        if int(touches) >= 3:
            return "strong_swing"
        if int(touches) >= 2:
            return "equal_high_low"
    day = _day_row(record, str(decision["leg_key"]))
    if day.get("vah") is None or day.get("val") is None:
        raise Super1FeatureError("VAH/VAL is unavailable for the liquidity filter.")
    price = _level_price(day, level)
    tolerance = float(config.vah_val_tolerance) * 2.0
    near = min(abs(price - float(day["vah"])), abs(price - float(day["val"]))) <= tolerance
    return "vah_val_proximity" if near else "other_liquidity"


class Super1XmMt5DemoOrderClient(xm.XmMt5DemoOrderClient):
    def __init__(self, config: dict[str, Any], secrets: dict[str, str]):
        super().__init__(config, secrets)
        self._super1_record: dict[str, Any] | None = None
        self._active_risk_scale: float | None = None
        self._last_sizing: dict[str, Any] | None = None

    def _overnight_direction(self, symbol: str, trade_date: str) -> str:
        calendar = load_verified_rth_calendar(self.config)
        end = core.utc_now()
        start = end - pd.Timedelta(days=max(10, int(self.config.get("history_days", 10))))
        _, rates = self.prices(symbol, start, end)
        rows: list[tuple[str, pd.Timestamp, float, float]] = []
        for row in rates:
            try:
                timestamp = pd.Timestamp(str(row["snapshotTimeUTC"])).tz_convert(core.TZ)
                open_price = float(row["openPrice"]["bid"])
                close_price = float(row["closePrice"]["bid"])
            except (KeyError, TypeError, ValueError) as exc:
                raise Super1FeatureError(f"{symbol}: RTH adapter price row is invalid.") from exc
            rows.append(
                (
                    str(timestamp.date()),
                    timestamp,
                    open_price,
                    close_price,
                )
            )

        sessions: dict[str, list[tuple[pd.Timestamp, float, float]]] = {}
        for date_key, timestamp, open_price, close_price in rows:
            sessions.setdefault(date_key, []).append((timestamp, open_price, close_price))

        def session_spec(day: pd.Timestamp) -> tuple[pd.Timestamp, pd.Timestamp] | None:
            entry = calendar["sessions"].get(day.strftime("%Y-%m-%d"))
            if entry is None:
                raise Super1FeatureError(
                    f"{symbol}: RTH calendar has no verified session for {day.date()}"
                )
            if entry["state"] == "CLOSED":
                return None
            return (
                pd.Timestamp(f"{day.date()} {entry['start']}", tz=core.TZ),
                pd.Timestamp(f"{day.date()} {entry['end']}", tz=core.TZ),
            )

        def verified_session(
            day: pd.Timestamp,
            *,
            require_open: bool,
            require_close: bool,
        ) -> tuple[float, float] | None:
            spec = session_spec(day)
            if spec is None:
                return None
            session_start, session_end = spec
            selected = [
                item
                for item in sessions.get(str(day.date()), [])
                if session_start <= item[0] < session_end
            ]
            timestamps = [item[0] for item in selected]
            if len(timestamps) != len(set(timestamps)) or timestamps != sorted(timestamps):
                raise Super1FeatureError(
                    f"{symbol}: duplicate or out-of-order RTH bars cannot be proven."
                )
            opening = [item for item in selected if item[0] == session_start]
            closing = [item for item in selected if item[0] == session_end - pd.Timedelta(minutes=1)]
            if (require_open and len(opening) != 1) or (require_close and len(closing) != 1):
                raise Super1FeatureError(
                    f"{symbol}: causal RTH open/previous close cannot be proven for {day.date()}."
                )
            return (
                float(opening[0][1]) if opening else float("nan"),
                float(closing[0][2]) if closing else float("nan"),
            )

        current_day = pd.Timestamp(trade_date, tz=core.TZ)
        current = verified_session(current_day, require_open=True, require_close=False)
        if current is None:
            raise Super1FeatureError(f"{symbol}: current RTH opening cannot be proven.")

        previous_day = current_day - pd.Timedelta(days=1)
        previous_spec: tuple[pd.Timestamp, pd.Timestamp] | None = None
        for _ in range(370):
            previous_spec = session_spec(previous_day)
            if previous_spec is not None:
                break
            previous_day -= pd.Timedelta(days=1)
        if previous_spec is None:
            raise Super1FeatureError(f"{symbol}: previous RTH session cannot be proven.")
        previous = verified_session(previous_day, require_open=False, require_close=True)
        if previous is None:
            raise Super1FeatureError(f"{symbol}: previous RTH close cannot be proven.")
        change = current[0] - previous[1]
        return "up" if change >= 0 else "down"

    def _terminal_r_state(self) -> dict[str, Any]:
        rule = self.config["risk_rule"]
        now = core.utc_now()
        start = (now - pd.Timedelta(days=int(rule["terminal_history_days"]))).to_pydatetime()
        end = (now + pd.Timedelta(minutes=1)).to_pydatetime()
        configs, _, _ = core.live_strategy_objects()
        rewards = {
            str(self.config["legs"][key]["epic"]): float(configs[key].reward_r)
            for key in core.LEG_ORDER
        }
        terminal_rows = calculate_terminal_r(
            self._mt5_collection("history_deals_get", start, end),
            self._mt5_collection("positions_get"),
            magic=self.magic,
            reward_by_symbol=rewards,
            lookback=int(rule["lookback"]),
            entry_out=int(getattr(self.mt5, "DEAL_ENTRY_OUT", 1)),
            reason_sl=int(getattr(self.mt5, "DEAL_REASON_SL", 4)),
            reason_tp=int(getattr(self.mt5, "DEAL_REASON_TP", 5)),
        )
        terminal = [float(item["r"]) for item in terminal_rows]
        lookback = int(rule["lookback"])
        state_sum = float(sum(terminal[-lookback:]))
        scale = float(rule["negative_scale"] if state_sum < 0 else rule["nonnegative_scale"])
        return {
            "lookback": lookback,
            "terminal_count": len(terminal),
            "terminal_raw_r": terminal[-lookback:],
            "state_sum": state_sum,
            "risk_scale": scale,
            "open_trade_outcome_used": False,
        }

    @staticmethod
    def _aligned_volume(raw: float, minimum: float, maximum: float, step: float) -> float:
        if not all(math.isfinite(value) and value > 0 for value in (raw, minimum, maximum, step)):
            raise Super1FeatureError("Broker volume parameters are invalid.")
        if raw + 1e-12 < minimum:
            raise xm.CandidateNotExecutableError(
                "RISK_BELOW_MINIMUM_VOLUME",
                "Calculated risk volume is below broker minimum; order blocked.",
            )
        capped = min(raw, maximum)
        steps = math.floor((capped - minimum + 1e-12) / step)
        volume = minimum + steps * step
        precision = max(0, int(math.ceil(-math.log10(step))) + 2) if step < 1 else 2
        return round(volume, precision)

    def _risk_volume(self, symbol: str, direction: str, entry: float, stop: float, scale: float) -> dict[str, Any]:
        account = self.mt5.account_info()
        info = self.mt5.symbol_info(symbol)
        if account is None or info is None:
            raise Super1FeatureError(f"{symbol}: account/symbol unavailable for risk sizing.")
        equity = float(getattr(account, "equity", 0.0) or 0.0)
        if equity <= 0:
            raise Super1FeatureError("Account equity is unavailable for risk sizing.")
        order_type = self.mt5.ORDER_TYPE_BUY if direction == "long" else self.mt5.ORDER_TYPE_SELL
        loss = self.mt5.order_calc_profit(order_type, symbol, 1.0, float(entry), float(stop))
        loss_per_lot = abs(float(loss)) if loss is not None else 0.0
        if loss_per_lot <= 0:
            raise Super1FeatureError(f"{symbol}: stop loss cash value cannot be calculated.")
        risk_cash = equity * float(self.config["base_risk_percent"]) / 100.0 * scale
        raw_volume = risk_cash / loss_per_lot
        volume = self._aligned_volume(
            raw_volume,
            float(info.volume_min),
            float(info.volume_max),
            float(info.volume_step),
        )
        estimated_loss = loss_per_lot * volume
        if estimated_loss > risk_cash * (1.0 + 1e-8):
            raise xm.CandidateNotExecutableError(
                "RISK_BUDGET_EXCEEDED",
                "Aligned broker volume exceeds the Super1 risk budget.",
            )
        return {
            "equity": equity,
            "base_risk_percent": float(self.config["base_risk_percent"]),
            "risk_scale": scale,
            "risk_cash": risk_cash,
            "loss_per_lot": loss_per_lot,
            "raw_volume": raw_volume,
            "volume": volume,
            "estimated_stop_loss": estimated_loss,
        }

    def _pending_request(
        self,
        symbol: str,
        direction: str,
        entry: float,
        stop: float,
        target: float,
        comment: str,
    ) -> dict[str, object]:
        request = super()._pending_request(symbol, direction, entry, stop, target, comment)
        if self._active_risk_scale is not None:
            sizing = self._risk_volume(
                symbol,
                direction,
                float(request["price"]),
                float(request["sl"]),
                self._active_risk_scale,
            )
            request["volume"] = sizing["volume"]
            self._last_sizing = sizing
        return request

    def _preflight_risk_scales(self) -> tuple[float | None, ...]:
        rule = self.config["risk_rule"]
        return (float(rule["negative_scale"]), float(rule["nonnegative_scale"]))

    def _filter_state(self, decision: dict[str, Any]) -> dict[str, Any]:
        if self._super1_record is None:
            raise Super1FeatureError("Super1 prefix record is unavailable.")
        trade_date = str(decision.get("date") or self._super1_record["date"])
        weekday = pd.Timestamp(trade_date).day_name()
        features: dict[str, Any] = {
            "entry_weekday": weekday,
            "direction": str(decision["direction"]),
        }
        configs, _, _ = core.live_strategy_objects()
        leg_key = str(decision["leg_key"])
        for index, rule in enumerate(self.config["setup_rules"], 1):
            if rule.get("action") != "BLOCK" or not isinstance(rule.get("conditions"), dict):
                raise Super1FeatureError("Unsupported sealed Super1 setup rule.")
            matched = True
            for feature, expected in rule["conditions"].items():
                if feature in features:
                    value = features[feature]
                elif feature == "liquidity_type":
                    value = features[feature] = liquidity_type(
                        self._super1_record, decision, configs[leg_key]
                    )
                elif feature == "overnight_direction":
                    value = features[feature] = self._overnight_direction(
                        str(self.config["legs"][leg_key]["epic"]), trade_date
                    )
                else:
                    raise Super1FeatureError(f"Unsupported Super1 candidate feature: {feature}.")
                if value != expected:
                    matched = False
                    break
            if matched:
                return {
                    "state": "BLOCK",
                    "rule": f"CANDIDATE_RULE_{index}",
                    "conditions": rule["conditions"],
                    "features": features,
                }
        return {"state": "ALLOW", "rule": None, "features": features}

    def _filter_evidence_fields(
        self,
        decision: dict[str, Any],
        filter_state: dict[str, Any],
        reason: str,
        prefix_record: dict[str, Any] | None,
        prefix_raw: bytes | None,
    ) -> dict[str, object]:
        raw = (
            prefix_raw
            if isinstance(prefix_raw, bytes)
            else (
                core.canonical_json(prefix_record).encode("utf-8")
                if isinstance(prefix_record, dict)
                else b""
            )
        )
        cutoffs = (
            dict(prefix_record.get("cutoffs") or {})
            if isinstance(prefix_record, dict)
            else {}
        )
        observed_at = (
            str(prefix_record.get("recorded_at") or prefix_record.get("decision_produced_at"))
            if isinstance(prefix_record, dict)
            else ""
        ) or core.utc_now().isoformat()
        trade_date = str(
            (prefix_record or {}).get("date") or decision.get("date") or ""
        )
        targeted_cutoff = str(cutoffs.get(str(decision["leg_key"]), ""))
        full_cutoff = core.canonical_json(cutoffs)
        raw_sha256 = hashlib.sha256(raw).hexdigest()
        evidence = {
            "filter_state": filter_state,
            "reason": reason,
            "decision": decision,
            "cutoffs": cutoffs,
            "raw_sha256": raw_sha256,
        }
        return {
            "strategy": "Super1",
            "order_id": str(decision["order_id"]),
            "state": str(filter_state["state"]),
            "trade_date": trade_date,
            "targeted_cutoff": targeted_cutoff,
            "full_cutoff": full_cutoff,
            "full_cutoffs_json": full_cutoff,
            "observed_at": observed_at,
            "raw_byte_count": len(raw),
            "raw_sha256": raw_sha256,
            "filter_evidence_sha256": hashlib.sha256(
                core.canonical_json(evidence).encode("utf-8")
            ).hexdigest(),
        }

    def _upsert_filter_evidence(self, fields: dict[str, object], output_root: Path) -> None:
        now = core.utc_now().isoformat()
        connection = self._ready_order_connection(output_root)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO strategy_filter_evidence (
                    strategy, order_id, state, trade_date, targeted_cutoff,
                    full_cutoff, full_cutoffs_json, observed_at, raw_byte_count,
                    raw_sha256, filter_evidence_sha256, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(strategy, order_id) DO UPDATE SET
                    state=excluded.state,
                    trade_date=excluded.trade_date,
                    targeted_cutoff=excluded.targeted_cutoff,
                    full_cutoff=excluded.full_cutoff,
                    full_cutoffs_json=excluded.full_cutoffs_json,
                    observed_at=excluded.observed_at,
                    raw_byte_count=excluded.raw_byte_count,
                    raw_sha256=excluded.raw_sha256,
                    filter_evidence_sha256=excluded.filter_evidence_sha256,
                    updated_at=excluded.updated_at
                """,
                (
                    fields["strategy"],
                    fields["order_id"],
                    fields["state"],
                    fields["trade_date"],
                    fields["targeted_cutoff"],
                    fields["full_cutoff"],
                    fields["full_cutoffs_json"],
                    fields["observed_at"],
                    fields["raw_byte_count"],
                    fields["raw_sha256"],
                    fields["filter_evidence_sha256"],
                    now,
                    now,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _promote_filter_if_newer(
        self,
        output_root: Path,
        decision: dict[str, Any],
        filter_state: dict[str, Any],
        reason: str,
        prefix_record: dict[str, Any] | None,
        prefix_raw: bytes | None,
    ) -> dict[str, object]:
        fields = self._filter_evidence_fields(
            decision, filter_state, reason, prefix_record, prefix_raw
        )
        order_id = str(decision["order_id"])
        now = core.utc_now().isoformat()
        connection = self._ready_order_connection(output_root)
        try:
            connection.execute("BEGIN IMMEDIATE")
            intent = connection.execute(
                "SELECT status FROM order_intents WHERE order_id = ?", (order_id,)
            ).fetchone()
            old = connection.execute(
                "SELECT observed_at, full_cutoffs_json, full_cutoff, raw_sha256 "
                "FROM strategy_filter_evidence WHERE strategy = 'Super1' AND order_id = ?",
                (order_id,),
            ).fetchone()
            if intent is None or str(intent[0]) != "FILTER_UNRESOLVED_DEFERRED" or old is None:
                connection.commit()
                return {"promoted": False, "state": None if intent is None else str(intent[0])}
            try:
                old_observed = pd.Timestamp(str(old[0]))
                new_observed = pd.Timestamp(str(fields["observed_at"]))
                old_cutoffs = json.loads(str(old[1] or old[2] or "{}"))
                new_cutoffs = json.loads(str(fields["full_cutoffs_json"]))
                cutoff_non_regression = all(
                    pd.Timestamp(new_cutoffs[key]) >= pd.Timestamp(old_cutoffs[key])
                    for key in ("nq", "spx")
                )
                cutoff_advanced = any(
                    pd.Timestamp(new_cutoffs[key]) > pd.Timestamp(old_cutoffs[key])
                    for key in ("nq", "spx")
                )
                strictly_newer = (
                    new_observed > old_observed
                    and cutoff_non_regression
                    and cutoff_advanced
                    and str(fields["raw_sha256"]) != str(old[3])
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                strictly_newer = False
            if not strictly_newer:
                connection.commit()
                return {
                    "promoted": False,
                    "state": "FILTER_UNRESOLVED_DEFERRED",
                    "reason": "FILTER_EVIDENCE_NOT_STRICTLY_NEWER",
                }
            connection.execute(
                "UPDATE order_intents SET status = 'PRE_SEND_DEFERRED', updated_at = ? "
                "WHERE order_id = ? AND status = 'FILTER_UNRESOLVED_DEFERRED'",
                (now, order_id),
            )
            connection.execute(
                """
                UPDATE strategy_filter_evidence SET
                    state = 'PRE_SEND_DEFERRED', trade_date = ?, targeted_cutoff = ?,
                    full_cutoff = ?, full_cutoffs_json = ?, observed_at = ?,
                    raw_byte_count = ?, raw_sha256 = ?, filter_evidence_sha256 = ?,
                    updated_at = ?
                WHERE strategy = 'Super1' AND order_id = ?
                """,
                (
                    fields["trade_date"],
                    fields["targeted_cutoff"],
                    fields["full_cutoff"],
                    fields["full_cutoffs_json"],
                    fields["observed_at"],
                    fields["raw_byte_count"],
                    fields["raw_sha256"],
                    fields["filter_evidence_sha256"],
                    now,
                    order_id,
                ),
            )
            event = {
                "event": "SUPER1_FILTER_PROMOTED",
                "order_id": order_id,
                "reason": "NEWER_COMPLETE_FILTER_EVIDENCE",
                "observed_at": fields["observed_at"],
                "cutoffs": json.loads(str(fields["full_cutoffs_json"])),
                "raw_sha256": fields["raw_sha256"],
                "prefix_sha256": fields["raw_sha256"],
                "raw_byte_count": fields["raw_byte_count"],
                "filter_evidence_sha256": fields["filter_evidence_sha256"],
            }
            if not self._outbox_event_exists(connection, order_id, "SUPER1_FILTER_PROMOTED"):
                connection.execute(
                    "INSERT INTO order_event_outbox (order_id, event_json) VALUES (?, ?)",
                    (order_id, core.canonical_json({"recorded_at": now, "magic": self.magic, **event})),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        self._drain_order_outbox(output_root)
        return {"promoted": True, "state": "PRE_SEND_DEFERRED"}

    def _persist_filter_terminal(
        self,
        output_root: Path,
        decision: dict[str, Any],
        filter_state: dict[str, Any],
        state: str,
        reason: str,
        prefix_record: dict[str, Any] | None,
        prefix_raw: bytes | None = None,
    ) -> dict[str, object]:
        """Persist a filter terminal outcome atomically before any broker call."""
        order_id = str(decision["order_id"])
        comment = self._comment(str(decision["leg_key"]), order_id)
        event_name = {
            "FILTER_BLOCKED": "SUPER1_FILTER_TERMINAL",
            "FILTER_UNRESOLVED_DEFERRED": "SUPER1_FILTER_DEFERRED",
            "FILTER_EXPIRED_NO_SEND": "SUPER1_FILTER_EXPIRED",
        }.get(state, "SUPER1_FILTER_TERMINAL")
        public_state = (
            "SUPER1_FILTER_BLOCKED"
            if state == "FILTER_BLOCKED"
            else "SUPER1_FILTER_UNRESOLVED_NO_SEND"
            if state == "FILTER_UNRESOLVED_DEFERRED"
            else "FILTER_EXPIRED_NO_SEND"
        )
        fields = self._filter_evidence_fields(
            decision, filter_state, reason, prefix_record, prefix_raw
        )
        now = core.utc_now().isoformat()
        connection = self._ready_order_connection(output_root)
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT status FROM order_intents WHERE order_id = ?", (order_id,)
            ).fetchone()
            execution = connection.execute(
                "SELECT state FROM broker_execution_states WHERE order_id = ?", (order_id,)
            ).fetchone()
            if existing is not None or execution is not None:
                current = str(existing[0]) if existing is not None else str(execution[0])
                if current in {"FILTER_BLOCKED", "FILTER_UNRESOLVED_DEFERRED", "FILTER_EXPIRED_NO_SEND"}:
                    if current == "FILTER_UNRESOLVED_DEFERRED" and state == "FILTER_EXPIRED_NO_SEND":
                        connection.execute(
                            "UPDATE order_intents SET status = ?, updated_at = ? WHERE order_id = ?",
                            (state, now, order_id),
                        )
                        connection.execute(
                            """
                            UPDATE strategy_filter_evidence SET state = ?, trade_date = ?,
                                targeted_cutoff = ?, full_cutoff = ?, full_cutoffs_json = ?,
                                observed_at = ?, raw_byte_count = ?, raw_sha256 = ?,
                                filter_evidence_sha256 = ?, updated_at = ?
                            WHERE strategy = 'Super1' AND order_id = ?
                            """,
                            (
                                state,
                                fields["trade_date"],
                                fields["targeted_cutoff"],
                                fields["full_cutoff"],
                                fields["full_cutoffs_json"],
                                fields["observed_at"],
                                fields["raw_byte_count"],
                                fields["raw_sha256"],
                                fields["filter_evidence_sha256"],
                                now,
                                order_id,
                            ),
                        )
                        connection.execute(
                            "INSERT INTO order_event_outbox (order_id, event_json) VALUES (?, ?)",
                            (
                                order_id,
                                core.canonical_json(
                                    {
                                        "recorded_at": now,
                                        "magic": int(getattr(self, "magic", self.config.get("magic_number", 0))),
                                        "event": event_name,
                                        "order_id": order_id,
                                        "comment": comment,
                                        "reason": reason,
                                        "raw_sha256": fields["raw_sha256"],
                                        "prefix_sha256": fields["raw_sha256"],
                                        "raw_byte_count": fields["raw_byte_count"],
                                        "cutoffs": json.loads(str(fields["full_cutoffs_json"])),
                                        "filter_evidence_sha256": fields["filter_evidence_sha256"],
                                    }
                                ),
                            ),
                        )
                        connection.commit()
                        transitioned = True
                    else:
                        transitioned = False
                    if transitioned:
                        pass
                    else:
                        connection.commit()
                    if transitioned:
                        result = {
                            "state": public_state,
                            "persistent_state": state,
                            "order_id": order_id,
                            "idempotent": False,
                            "filter_state": filter_state,
                            "cancelled_pending": [],
                        }
                    else:
                        result = {
                            "state": public_state,
                            "persistent_state": current,
                            "order_id": order_id,
                            "idempotent": True,
                            "filter_state": filter_state,
                            "cancelled_pending": [],
                        }
                    if transitioned:
                        self._drain_order_outbox(output_root)
                    return result
                connection.commit()
                return {
                    "state": "FILTER_DEFERRED_TO_BASE_LIFECYCLE",
                    "order_id": order_id,
                    "existing_state": current,
                }
            broker_objects = self._broker_objects(comment)
            if broker_objects:
                connection.commit()
                return {
                    "state": "FILTER_DEFERRED_TO_BASE_LIFECYCLE",
                    "order_id": order_id,
                    "reason": "broker_object_exists_before_filter_terminal",
                }
            connection.execute(
                """INSERT INTO order_intents
                   (order_id, status, comment, request_json, broker_ticket, created_at, updated_at)
                   VALUES (?, ?, ?, NULL, NULL, ?, ?)""",
                (order_id, state, comment, now, now),
            )
            connection.execute(
                """
                INSERT INTO strategy_filter_evidence (
                    strategy, order_id, state, trade_date, targeted_cutoff,
                    full_cutoff, full_cutoffs_json, observed_at, raw_byte_count,
                    raw_sha256, filter_evidence_sha256, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fields["strategy"], fields["order_id"], fields["state"], fields["trade_date"],
                    fields["targeted_cutoff"], fields["full_cutoff"], fields["full_cutoffs_json"],
                    fields["observed_at"], fields["raw_byte_count"], fields["raw_sha256"],
                    fields["filter_evidence_sha256"], now, now,
                ),
            )
            connection.execute(
                "INSERT INTO order_event_outbox (order_id, event_json) VALUES (?, ?)",
                (
                    order_id,
                    core.canonical_json(
                        {
                            "recorded_at": now,
                            "magic": int(getattr(self, "magic", self.config.get("magic_number", 0))),
                            "event": event_name,
                            "order_id": order_id,
                            "comment": comment,
                            "reason": reason,
                            "raw_sha256": fields["raw_sha256"],
                            "prefix_sha256": fields["raw_sha256"],
                            "raw_byte_count": fields["raw_byte_count"],
                            "prefix_bytes": fields["raw_byte_count"],
                            "cutoffs": json.loads(str(fields["full_cutoffs_json"])),
                            "filter_evidence_sha256": fields["filter_evidence_sha256"],
                            "request_json": None,
                            "broker_ticket": None,
                        }
                    ),
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        self._drain_order_outbox(output_root)
        return {
            "state": public_state,
            "persistent_state": state,
            "order_id": order_id,
            "filter_state": filter_state,
            "cancelled_pending": [],
        }

    def _place_candidate(
        self,
        output_root: Path,
        decision: dict[str, Any],
        symbol: str,
        reward_r: float,
        *,
        prefix_record: dict[str, Any] | None = None,
        prefix_raw: bytes | None = None,
        send_now: pd.Timestamp | None = None,
    ) -> dict[str, object]:
        order_id = str(decision["order_id"])
        comment = self._comment(str(decision["leg_key"]), order_id)

        try:
            filter_state = self._filter_state(decision)
        except Super1FeatureError as exc:
            unresolved = {
                    "state": "UNRESOLVED",
                    "rule": None,
                    "features": {
                        "entry_weekday": pd.Timestamp(
                            str(decision.get("date") or "")
                        ).day_name(),
                        "direction": str(decision.get("direction") or ""),
                    },
                }
            guard = self._candidate_send_context(
                decision,
                prefix_record,
                pd.Timestamp(send_now()) if callable(send_now) else send_now,
            )
            unresolved_state = (
                "FILTER_EXPIRED_NO_SEND"
                if guard["state"] == "WINDOW_EXPIRED"
                else "FILTER_UNRESOLVED_DEFERRED"
            )
            return self._persist_filter_terminal(
                output_root,
                decision,
                unresolved,
                unresolved_state,
                str(exc),
                prefix_record,
                prefix_raw,
            )
        if filter_state["state"] == "BLOCK":
            return self._persist_filter_terminal(
                output_root,
                decision,
                filter_state,
                "FILTER_BLOCKED",
                "FILTER_RULE_BLOCK",
                prefix_record,
                prefix_raw,
            )
        prior_filter = self._intent_state(output_root, order_id)
        if prior_filter and prior_filter["status"] == "FILTER_UNRESOLVED_DEFERRED":
            guard = self._candidate_send_context(
                decision,
                prefix_record,
                pd.Timestamp(send_now()) if callable(send_now) else send_now,
            )
            if guard["state"] == "WINDOW_EXPIRED":
                return self._persist_filter_terminal(
                    output_root,
                    decision,
                    filter_state,
                    "FILTER_EXPIRED_NO_SEND",
                    "FILTER_WINDOW_EXPIRED",
                    prefix_record,
                    prefix_raw,
                )
            promotion = self._promote_filter_if_newer(
                output_root,
                decision,
                filter_state,
                "NEWER_COMPLETE_FILTER_EVIDENCE",
                prefix_record,
                prefix_raw,
            )
            if not promotion.get("promoted"):
                return {
                    "state": "SUPER1_FILTER_UNRESOLVED_NO_SEND",
                    "persistent_state": "FILTER_UNRESOLVED_DEFERRED",
                    "order_id": order_id,
                    "reason": promotion.get("reason", "FILTER_EVIDENCE_NOT_STRICTLY_NEWER"),
                    "filter_state": filter_state,
                    "cancelled_pending": [],
                }
        elif not prior_filter or prior_filter["status"] not in {
            "FILTER_BLOCKED",
            "FILTER_EXPIRED_NO_SEND",
        }:
            self._upsert_filter_evidence(
                self._filter_evidence_fields(
                    decision,
                    filter_state,
                    "FILTER_ALLOW",
                    prefix_record,
                    prefix_raw,
                ),
                output_root,
            )
        risk_state = self._terminal_r_state()
        self._active_risk_scale = float(risk_state["risk_scale"])
        self._last_sizing = None
        try:
            result = super()._place_candidate(
                output_root,
                decision,
                symbol,
                reward_r,
                prefix_record=prefix_record,
                prefix_raw=prefix_raw,
                send_now=send_now,
            )
            if result.get("state") == "SUBMITTED" and self._last_sizing is not None:
                self._append_order_event(
                    output_root,
                    {
                        "event": "SUPER1_RISK_SIZING",
                        "deployment_mode": str(self.config["deployment_mode"]),
                        "order_id": order_id,
                        "risk_state": risk_state,
                        "sizing": self._last_sizing,
                        "filter_features": filter_state["features"],
                    },
                )
            return {
                **result,
                "risk_state": risk_state,
                "filter_state": filter_state,
                "filter_features": filter_state["features"],
            }
        finally:
            self._active_risk_scale = None
            self._last_sizing = None

    def reconcile_orders(
        self,
        output_root: Path,
        prefix: dict[str, Any],
        now: pd.Timestamp,
        lock: dict[str, Any],
    ) -> dict[str, object]:
        self._super1_record = None
        self._prefix_raw_path = None
        self._prefix_raw_bytes = None
        if prefix.get("path") and Path(str(prefix["path"])).exists():
            prefix_path = Path(str(prefix["path"]))
            prefix_raw = prefix_path.read_bytes()
            parsed = json.loads(prefix_raw.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise Super1FeatureError("Super1 prefix JSON root must be an object.")
            self._super1_record = parsed
            self._prefix_raw_path = prefix_path
            self._prefix_raw_bytes = prefix_raw
        try:
            result = super().reconcile_orders(output_root, prefix, now, lock)
            result["strategy"] = "Super1"
            result["deployment_mode"] = str(self.config["deployment_mode"])
            return result
        finally:
            self._super1_record = None
            self._prefix_raw_path = None
            self._prefix_raw_bytes = None


def configure_core() -> None:
    runtime = core.read_json(RUNTIME_CONFIG)
    validate_super1_candidate(runtime)
    xm.RUNTIME_CONFIG = RUNTIME_CONFIG
    core.RUNTIME_CONFIG = RUNTIME_CONFIG
    core.SCRIPT_PATH = Path(__file__).resolve()
    core.HARNESS_PATHS = (
        Path(__file__).resolve(),
        Path(xm.__file__).resolve(),
        Path(core.__file__).resolve(),
        FORWARD_SHADOW_ADAPTER.resolve(),
        (ROOT / str(runtime["candidate_path"])).resolve(),
        (ROOT / str(runtime["signal_contract_path"])).resolve(),
        SUPER1_MANIFEST.resolve(),
    )
    core.REQUIRED_ENV = REQUIRED_ENV
    core.CapitalDemoClient = Super1XmMt5DemoOrderClient
    core.install_xm_scheduled_gap_integrity()


def main() -> None:
    configure_core()
    core.main()


if __name__ == "__main__":
    main()
