from __future__ import annotations

import json
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


RUNTIME_CONFIG = ROOT / "live_forward" / "super1_xm_mt5_demo_config.json"
SUPER1_MANIFEST = ROOT / "research_candidates" / "super1" / "super1_manifest.json"
FORWARD_SHADOW_ADAPTER = ROOT / "scripts" / "run_forward_shadow.py"
DEPLOYMENT_MODE = "FROZEN_CANONICAL_PAIR_PIPELINE_WITH_SUPER1_OVERLAY"
REQUIRED_ENV = ("XM_MT5_SERVER",)


class Super1FeatureError(core.CriticalLiveError):
    pass


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
        end = core.utc_now()
        start = end - pd.Timedelta(days=max(10, int(self.config.get("history_days", 10))))
        _, rates = self.prices(symbol, start, end)
        rows = []
        for row in rates:
            timestamp = pd.Timestamp(str(row["snapshotTimeUTC"])).tz_convert(core.TZ)
            minute = timestamp.hour * 60 + timestamp.minute
            if 570 <= minute <= 959:
                rows.append(
                    (
                        str(timestamp.date()),
                        minute,
                        float(row["openPrice"]["bid"]),
                        float(row["closePrice"]["bid"]),
                    )
                )
        sessions: dict[str, list[tuple[int, float, float]]] = {}
        for date_key, minute, open_price, close_price in rows:
            sessions.setdefault(date_key, []).append((minute, open_price, close_price))
        current = sorted(sessions.get(trade_date) or [])
        previous_dates = sorted(date_key for date_key in sessions if date_key < trade_date)
        if not current or current[0][0] != 570 or not previous_dates:
            raise Super1FeatureError(f"{symbol}: causal RTH open/previous close cannot be proven.")
        previous = sorted(sessions[previous_dates[-1]])
        change = current[0][1] - previous[-1][2]
        return "up" if change >= 0 else "down"

    def _terminal_r_state(self) -> dict[str, Any]:
        rule = self.config["risk_rule"]
        now = core.utc_now()
        start = (now - pd.Timedelta(days=int(rule["terminal_history_days"]))).to_pydatetime()
        end = (now + pd.Timedelta(minutes=1)).to_pydatetime()
        open_positions = {
            int(getattr(item, "ticket", 0) or getattr(item, "identifier", 0))
            for item in self._mt5_collection("positions_get")
            if int(getattr(item, "magic", -1)) == self.magic
        }
        terminal_by_position: dict[int, tuple[int, int, float]] = {}
        configs, _, _ = core.live_strategy_objects()
        rewards = {
            str(self.config["legs"][key]["epic"]): float(configs[key].reward_r)
            for key in core.LEG_ORDER
        }
        for deal in self._mt5_collection("history_deals_get", start, end):
            if int(getattr(deal, "magic", -1)) != self.magic:
                continue
            if int(getattr(deal, "entry", -1)) != int(getattr(self.mt5, "DEAL_ENTRY_OUT", 1)):
                continue
            position = int(getattr(deal, "position_id", 0))
            if not position or position in open_positions:
                continue
            reason = int(getattr(deal, "reason", -1))
            if reason == int(getattr(self.mt5, "DEAL_REASON_SL", 4)):
                raw_r = -1.0
            elif reason == int(getattr(self.mt5, "DEAL_REASON_TP", 5)):
                symbol = str(getattr(deal, "symbol", ""))
                if symbol not in rewards:
                    raise Super1FeatureError(f"Unknown Super1 terminal symbol: {symbol}.")
                raw_r = rewards[symbol]
            else:
                raise Super1FeatureError(
                    f"Super1 terminal reason is not an unambiguous TP/SL: {reason}."
                )
            if position in terminal_by_position:
                raise Super1FeatureError(
                    f"Super1 position {position} has multiple terminal deals; exact R is unresolved."
                )
            terminal_by_position[position] = (
                int(getattr(deal, "time_msc", 0)),
                int(getattr(deal, "ticket", 0)),
                raw_r,
            )
        ordered = sorted(terminal_by_position.values())
        terminal = [item[2] for item in ordered]
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

    def _place_candidate(
        self,
        output_root: Path,
        decision: dict[str, Any],
        symbol: str,
        reward_r: float,
    ) -> dict[str, object]:
        order_id = str(decision["order_id"])
        comment = self._comment(str(decision["leg_key"]), order_id)

        def blocked_result(state: str, **fields: Any) -> dict[str, object]:
            cancelled = []
            for kind, item in self._broker_objects(comment):
                if kind == "ORDER":
                    cancelled.append(self._remove_order(output_root, item, state, order_id))
            return {"state": state, "order_id": order_id, "cancelled_pending": cancelled, **fields}

        try:
            filter_state = self._filter_state(decision)
        except Super1FeatureError as exc:
            return blocked_result("SUPER1_FILTER_UNRESOLVED_NO_SEND", reason=str(exc))
        if filter_state["state"] == "BLOCK":
            return blocked_result("SUPER1_FILTER_BLOCKED", **filter_state)
        risk_state = self._terminal_r_state()
        self._active_risk_scale = float(risk_state["risk_scale"])
        self._last_sizing = None
        try:
            result = super()._place_candidate(output_root, decision, symbol, reward_r)
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
            return {**result, "risk_state": risk_state, "filter_features": filter_state["features"]}
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
        if prefix.get("path") and Path(str(prefix["path"])).exists():
            self._super1_record = core.read_json(Path(str(prefix["path"])))
        try:
            result = super().reconcile_orders(output_root, prefix, now, lock)
            result["strategy"] = "Super1"
            result["deployment_mode"] = str(self.config["deployment_mode"])
            return result
        finally:
            self._super1_record = None


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
