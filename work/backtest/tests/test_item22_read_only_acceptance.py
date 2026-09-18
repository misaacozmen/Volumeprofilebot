from __future__ import annotations

import builtins
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


BACKTEST_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKTEST_ROOT.parents[1]
OBSERVER_PATH = BACKTEST_ROOT / "scripts" / "item22_read_only_observer.py"
VALIDATOR_PATH = BACKTEST_ROOT / "scripts" / "validate_item22_read_only_acceptance.py"
PROMOTION_MANIFEST_SHA256 = "135545dbc8749cff942c627e38e518bda33ad496f35bac9981d2c30f40231476"
COMMIT = "a" * 40
TREE = "b" * 40
VALIDATOR_COMMIT = "e" * 40
VALIDATOR_TREE = "f" * 40
RUN_ID = "c" * 64
NONCE = "d" * 64


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


observer = _load("item22_test_observer", OBSERVER_PATH)
validator = _load("item22_test_validator", VALIDATOR_PATH)


class FakeMt5:
    ACCOUNT_TRADE_MODE_DEMO = 2

    def __init__(
        self,
        *,
        mode=2,
        demo_constant=2,
        login=123,
        server="demo-server",
        company="demo-company",
        positions=(),
        orders=(),
        history=(),
        initialize_result=True,
        initialize_exception=None,
        shutdown_result=True,
        shutdown_exception=None,
    ):
        self.mode = mode
        self.ACCOUNT_TRADE_MODE_DEMO = demo_constant
        self.login = login
        self.server = server
        self.company = company
        self.positions = positions
        self.orders = orders
        self.history = history
        self.initialize_result = initialize_result
        self.initialize_exception = initialize_exception
        self.shutdown_result = shutdown_result
        self.shutdown_exception = shutdown_exception
        self.calls: list[str] = []
        self.order_send_reached = False

    def initialize(self):
        self.calls.append("initialize")
        if self.initialize_exception:
            raise self.initialize_exception
        return self.initialize_result

    def account_info(self):
        self.calls.append("account_info")
        return SimpleNamespace(login=self.login, server=self.server, company=self.company, trade_mode=self.mode)

    def positions_get(self):
        self.calls.append("positions_get")
        return self.positions

    def orders_get(self):
        self.calls.append("orders_get")
        return self.orders

    def history_deals_get(self, *_args):
        self.calls.append("history_deals_get")
        return self.history

    def shutdown(self):
        self.calls.append("shutdown")
        if self.shutdown_exception:
            raise self.shutdown_exception
        return self.shutdown_result

    def order_send(self, *_args, **_kwargs):
        self.order_send_reached = True
        return None


def _run(monkeypatch, tmp_path: Path, fake: FakeMt5, **overrides):
    key = tmp_path / "test-owner-hmac.key"
    key.write_bytes(b"k" * 32)
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    values = {
        "run_id": RUN_ID,
        "nonce": NONCE,
        "source_commit": COMMIT,
        "source_tree_oid": TREE,
    }
    values.update(overrides)
    return observer.run(key_path=key, **values)


def test_valid_demo_fixture_passes(monkeypatch, tmp_path):
    fake = FakeMt5(positions=(), orders=(), history=())
    result = _run(monkeypatch, tmp_path, fake)
    assert result["status"] == "PASS_EXTERNAL"
    assert result["operations"] == list(observer.ALLOWED_OPERATIONS)
    assert result["shutdown_success"] is True


def test_non_demo_is_rejected_and_shutdown_runs(monkeypatch, tmp_path):
    fake = FakeMt5(mode=0)
    result = _run(monkeypatch, tmp_path, fake)
    assert result["status"] == "BLOCKED_EXTERNAL_ACCEPTANCE"
    assert result["shutdown_success"] is True
    assert fake.calls[-1] == "shutdown"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("login", None),
        ("login", True),
        ("login", 0),
        ("server", None),
        ("server", "   "),
        ("server", 123),
        ("company", None),
        ("company", "\t"),
        ("company", 123),
    ],
)
def test_missing_none_blank_and_wrong_type_identity_is_rejected(monkeypatch, tmp_path, field, value):
    kwargs = {field: value}
    result = _run(monkeypatch, tmp_path, FakeMt5(**kwargs))
    assert result["status"] == "BLOCKED_EXTERNAL_ACCEPTANCE"
    assert result["shutdown_success"] is True


@pytest.mark.parametrize(
    ("mode", "demo_constant"),
    [(True, 2), ("2", 2), (2, True), (2, "2")],
)
def test_boolean_or_invalid_trade_mode_is_rejected(monkeypatch, tmp_path, mode, demo_constant):
    result = _run(monkeypatch, tmp_path, FakeMt5(mode=mode, demo_constant=demo_constant))
    assert result["status"] == "BLOCKED_EXTERNAL_ACCEPTANCE"
    assert result["shutdown_success"] is True


def test_failed_query_is_rejected_and_shutdown_runs(monkeypatch, tmp_path):
    result = _run(monkeypatch, tmp_path, FakeMt5(positions=None))
    assert result["status"] == "BLOCKED_EXTERNAL_ACCEPTANCE"
    assert result["shutdown_success"] is True


def test_initialize_failure_and_exception_still_shutdown(monkeypatch, tmp_path):
    failed = _run(monkeypatch, tmp_path, FakeMt5(initialize_result=False))
    raised = _run(monkeypatch, tmp_path, FakeMt5(initialize_exception=RuntimeError("init")))
    assert failed["status"] == "BLOCKED_EXTERNAL_ACCEPTANCE"
    assert raised["status"] == "BLOCKED_EXTERNAL_ACCEPTANCE"
    assert failed["shutdown_attempted"] is True
    assert raised["shutdown_attempted"] is True


def test_shutdown_failure_and_exception_block_acceptance(monkeypatch, tmp_path):
    failed = _run(monkeypatch, tmp_path, FakeMt5(shutdown_result=False))
    raised = _run(monkeypatch, tmp_path, FakeMt5(shutdown_exception=RuntimeError("shutdown")))
    assert failed["status"] == "BLOCKED_EXTERNAL_ACCEPTANCE"
    assert raised["status"] == "BLOCKED_EXTERNAL_ACCEPTANCE"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "A" * 64),
        ("nonce", "short"),
        ("source_commit", "g" * 40),
        ("source_tree_oid", True),
    ],
)
def test_invalid_provenance_is_rejected_before_broker_import(monkeypatch, tmp_path, field, value):
    imported = False
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        nonlocal imported
        if name == "MetaTrader5":
            imported = True
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    values = {field: value}
    with pytest.raises(ValueError):
        _run(monkeypatch, tmp_path, FakeMt5(), **values)
    assert imported is False


def test_forbidden_api_is_blocked_before_real_module(monkeypatch):
    fake = FakeMt5()
    proxy = observer.ObservedMt5(fake)
    with pytest.raises(observer.ReadOnlyViolation):
        proxy.order_send()
    assert fake.order_send_reached is False
    assert proxy.forbidden_attempts == ["order_send"]


def _write_json(path: Path, value: dict):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _make_valid_evidence(monkeypatch, tmp_path: Path) -> tuple[Path, dict, dict]:
    evidence = tmp_path / "evidence"
    evidence.mkdir(parents=True)
    key = tmp_path / "test-owner-hmac.key"
    key.write_bytes(b"k" * 32)
    fake = FakeMt5()
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    probe = observer.run(key_path=key, run_id=RUN_ID, nonce=NONCE, source_commit=COMMIT, source_tree_oid=TREE)
    _write_json(evidence / "owner-declaration.json", {
        "evidence_type": "OWNER_CHAT_DECLARATION",
        "password_rotation": "OWNER_DECLARED",
        "rotation_time": "NOT_PROVIDED",
        "read_only_probe_authorized": True,
        "push_authorized": False,
    })
    _write_json(evidence / "run-context.json", {
        "run_id": RUN_ID,
        "nonce": NONCE,
        "source_commit": COMMIT,
        "source_tree_oid": TREE,
        "validator_commit": VALIDATOR_COMMIT,
        "validator_tree_oid": VALIDATOR_TREE,
    })
    probe_path = evidence / "mt5-read-only-observed.json"
    _write_json(probe_path, probe)
    files = []
    installed = {
        "work/backtest/scripts/mt5_read_only_probe.py": REPO_ROOT / ".." / "item20-21-acceptance-evidence-promotion-20260918-v1" / "work/backtest/scripts/mt5_read_only_probe.py",
        "work/backtest/scripts/mt5_read_only_acceptance.py": REPO_ROOT / ".." / "item20-21-acceptance-evidence-promotion-20260918-v1" / "work/backtest/scripts/mt5_read_only_acceptance.py",
        "work/backtest/scripts/run_super1_owner_acceptance.py": REPO_ROOT / ".." / "item20-21-acceptance-evidence-promotion-20260918-v1" / "work/backtest/scripts/run_super1_owner_acceptance.py",
        "work/backtest/scripts/owner_trust.py": REPO_ROOT / ".." / "item20-21-acceptance-evidence-promotion-20260918-v1" / "work/backtest/scripts/owner_trust.py",
    }
    for manifest_path, source_path in installed.items():
        digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
        files.append({"source_path": str(source_path), "manifest_path": manifest_path, "exists": True, "actual_sha256": digest, "manifest_sha256": digest, "matches": True})
    observer_digest = hashlib.sha256(OBSERVER_PATH.read_bytes()).hexdigest()
    files.append({"source_path": str(OBSERVER_PATH), "manifest_path": None, "exists": True, "actual_sha256": observer_digest, "manifest_sha256": None, "matches": None, "comparison": "SEPARATE_OBSERVER_COMMIT_BOUND_SOURCE"})
    source_record = {
        "source_commit": COMMIT,
        "source_tree_oid": TREE,
        "observer_commit": COMMIT,
        "observer_tree_oid": TREE,
        "validator_commit": VALIDATOR_COMMIT,
        "validator_tree_oid": VALIDATOR_TREE,
        "promotion_manifest_path": str(validator.TRUSTED_PROMOTION_MANIFEST_PATH),
        "promotion_manifest_sha256": PROMOTION_MANIFEST_SHA256,
        "promotion_manifest_expected_sha256": PROMOTION_MANIFEST_SHA256,
        "promotion_manifest_sha256_matches": True,
        "files": files,
        "observed_acceptance_artifact_sha256": hashlib.sha256(probe_path.read_bytes()).hexdigest(),
    }
    _write_json(evidence / "source-artifact-verification.json", source_record)
    return evidence, probe, source_record


def _stub_git(monkeypatch):
    def fake_git(_repo, *args):
        if args == ("rev-parse", "HEAD"):
            return VALIDATOR_COMMIT
        if args == ("rev-parse", "HEAD^{tree}"):
            return VALIDATOR_TREE
        if args == ("status", "--porcelain"):
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(validator, "_git", fake_git)


def test_validator_valid_fixture_produces_closed(monkeypatch, tmp_path):
    _stub_git(monkeypatch)
    evidence, _probe, _record = _make_valid_evidence(monkeypatch, tmp_path)
    result = validator.validate_evidence(evidence_dir=evidence, repo=REPO_ROOT)
    assert result["item22_scoped_status"] == "CLOSED"
    assert result["status"] == "PASS_ACCEPTANCE"
    assert result["exit_code"] == 0


def test_validator_rejects_missing_promotion_manifest(monkeypatch, tmp_path):
    _stub_git(monkeypatch)
    evidence, _probe, _record = _make_valid_evidence(monkeypatch, tmp_path)
    monkeypatch.setattr(validator, "TRUSTED_PROMOTION_MANIFEST_PATH", tmp_path / "missing-manifest.json")
    result = validator.validate_evidence(evidence_dir=evidence, repo=REPO_ROOT)
    assert result["status"] == "BLOCKED_EXTERNAL_ACCEPTANCE"
    assert result["errors"] == ["PROMOTION_MANIFEST_MISSING"]


def test_validator_rejects_modified_promotion_manifest(monkeypatch, tmp_path):
    _stub_git(monkeypatch)
    evidence, _probe, _record = _make_valid_evidence(monkeypatch, tmp_path)
    modified = tmp_path / "modified-manifest.json"
    manifest = json.loads(validator.TRUSTED_PROMOTION_MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest["source_tree_oid"] = "f" * 40
    _write_json(modified, manifest)
    monkeypatch.setattr(validator, "TRUSTED_PROMOTION_MANIFEST_PATH", modified)
    result = validator.validate_evidence(evidence_dir=evidence, repo=REPO_ROOT)
    assert result["status"] == "BLOCKED_EXTERNAL_ACCEPTANCE"
    assert result["errors"] == ["PROMOTION_MANIFEST_HASH_MISMATCH"]


def test_validator_rejects_contradictory_manifest_file_hash(monkeypatch, tmp_path):
    _stub_git(monkeypatch)
    evidence, _probe, record = _make_valid_evidence(monkeypatch, tmp_path)
    record["files"][0]["manifest_sha256"] = "0" * 64
    _write_json(evidence / "source-artifact-verification.json", record)
    result = validator.validate_evidence(evidence_dir=evidence, repo=REPO_ROOT)
    assert result["status"] == "BLOCKED_EXTERNAL_ACCEPTANCE"
    assert result["errors"] == ["MANIFEST_FILE_RECORD_HASH_CONFLICT"]


def test_validator_rejects_changed_promotion_source_file_entry(monkeypatch, tmp_path):
    _stub_git(monkeypatch)
    evidence, _probe, record = _make_valid_evidence(monkeypatch, tmp_path)
    record["files"][0]["actual_sha256"] = "0" * 64
    _write_json(evidence / "source-artifact-verification.json", record)
    result = validator.validate_evidence(evidence_dir=evidence, repo=REPO_ROOT)
    assert result["status"] == "BLOCKED_EXTERNAL_ACCEPTANCE"
    assert result["errors"] == ["SOURCE_FILE_HASH_CHANGED"]


def test_validator_wrong_commit_or_tree_binding_is_rejected(monkeypatch, tmp_path):
    _stub_git(monkeypatch)
    evidence, _probe, _record = _make_valid_evidence(monkeypatch, tmp_path)
    context_path = evidence / "run-context.json"
    context = json.loads(context_path.read_text(encoding="utf-8"))
    context["source_tree_oid"] = "e" * 40
    _write_json(context_path, context)
    result = validator.validate_evidence(evidence_dir=evidence, repo=REPO_ROOT)
    assert result["status"] == "BLOCKED_EXTERNAL_ACCEPTANCE"
    assert result["item22_scoped_status"] == "OPEN"


def test_validator_changed_observer_or_probe_artifact_is_rejected(monkeypatch, tmp_path):
    _stub_git(monkeypatch)
    evidence, _probe, record = _make_valid_evidence(monkeypatch, tmp_path)
    record["files"][-1]["actual_sha256"] = "0" * 64
    _write_json(evidence / "source-artifact-verification.json", record)
    changed_observer = validator.validate_evidence(evidence_dir=evidence, repo=REPO_ROOT)
    assert changed_observer["status"] == "BLOCKED_EXTERNAL_ACCEPTANCE"

    evidence2, _probe2, record2 = _make_valid_evidence(monkeypatch, tmp_path / "second")
    report = json.loads((evidence2 / "mt5-read-only-observed.json").read_text(encoding="utf-8"))
    report["finished_at_utc"] = "2999-01-01T00:00:00+00:00"
    _write_json(evidence2 / "mt5-read-only-observed.json", report)
    changed_probe = validator.validate_evidence(evidence_dir=evidence2, repo=REPO_ROOT)
    assert changed_probe["status"] == "BLOCKED_EXTERNAL_ACCEPTANCE"


def test_validator_event_counter_mismatch_is_rejected(monkeypatch, tmp_path):
    _stub_git(monkeypatch)
    evidence, _probe, _record = _make_valid_evidence(monkeypatch, tmp_path)
    report_path = evidence / "mt5-read-only-observed.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["order_send"] = 1
    _write_json(report_path, report)
    result = validator.validate_evidence(evidence_dir=evidence, repo=REPO_ROOT)
    assert result["status"] == "BLOCKED_EXTERNAL_ACCEPTANCE"


def test_validator_missing_owner_and_nonce_mismatch_are_rejected(monkeypatch, tmp_path):
    _stub_git(monkeypatch)
    evidence, _probe, _record = _make_valid_evidence(monkeypatch, tmp_path)
    (evidence / "owner-declaration.json").unlink()
    missing_owner = validator.validate_evidence(evidence_dir=evidence, repo=REPO_ROOT)
    assert missing_owner["status"] == "BLOCKED_EXTERNAL_ACCEPTANCE"

    evidence2, _probe2, _record2 = _make_valid_evidence(monkeypatch, tmp_path / "second")
    context_path = evidence2 / "run-context.json"
    context = json.loads(context_path.read_text(encoding="utf-8"))
    context["nonce"] = "f" * 64
    _write_json(context_path, context)
    mismatch = validator.validate_evidence(evidence_dir=evidence2, repo=REPO_ROOT)
    assert mismatch["status"] == "BLOCKED_EXTERNAL_ACCEPTANCE"


def test_validator_output_refuses_overwrite(monkeypatch, tmp_path):
    output = tmp_path / "existing.json"
    output.write_text("original\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["validator", "--evidence-dir", str(tmp_path), "--repo", str(REPO_ROOT), "--output", str(output)])
    with pytest.raises(SystemExit, match="refusing to overwrite"):
        validator.main()
    assert output.read_text(encoding="utf-8") == "original\n"
