"""Pinned source bytes must survive either Git autocrlf setting unchanged."""
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]


@pytest.mark.parametrize("autocrlf", ["true", "false"])
def test_pinned_bytes_are_independent_of_autocrlf(tmp_path, autocrlf):
    from super1_required_nodes import MANIFEST_SHA256, CONTRACT_SHA256

    signal = json.loads((ROOT / "tests/fixtures/super1_signal_contract.json").read_text())
    runtime = json.loads((ROOT / "tests/fixtures/super1_xm_mt5_demo_config.json").read_text())
    candidate = json.loads((ROOT / "tests/fixtures/super1_candidate.json").read_text())
    pins = {
        "docs/SUPER1_REQUIRED_NODE_MANIFEST_V08_20260901.json": MANIFEST_SHA256,
        "docs/SUPER1_SEMANTIC_CONTRACT_V09_20260902.json": CONTRACT_SHA256,
        "work/backtest/" + runtime["candidate_path"]: runtime["candidate_file_sha256"],
        "work/backtest/" + runtime["signal_contract_path"]: runtime["signal_contract_sha256"],
    }
    for item in candidate["provenance"]["inputs"]:
        pins["work/backtest/" + item["path"]] = item["sha256"]
    for section, path_key, hash_key in (
        ("signal_source", "config_path", "config_sha256"),
        ("signal_source", "generator_path", "generator_sha256"),
        ("signal_source", "payload_adapter_path", "payload_adapter_sha256"),
        ("overlay_candidate", "runtime_path", "runtime_sha256"),
        ("demo_order_transport", "path", "sha256"),
    ):
        pins["work/backtest/" + signal[section][path_key]] = signal[section][hash_key]
    engine_files = sorted((ROOT / "backtest").glob("*.py"))
    paths = list(pins) + [p.relative_to(REPO).as_posix() for p in engine_files]
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "core.autocrlf", autocrlf], check=True)
    for relative in (".gitattributes", "work/backtest/.gitattributes"):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((REPO / relative).read_bytes())
    for relative in paths:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(subprocess.check_output(["git", "show", "HEAD:" + relative], cwd=REPO))
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True)
    for relative in paths:
        (tmp_path / relative).unlink()
    subprocess.run(["git", "-C", str(tmp_path), "checkout-index", "-a"], check=True)
    for relative, expected in pins.items():
        assert hashlib.sha256((tmp_path / relative).read_bytes()).hexdigest() == expected, relative
    digest = hashlib.sha256()
    for source in engine_files:
        digest.update(source.name.encode())
        digest.update((tmp_path / source.relative_to(REPO)).read_bytes())
    assert digest.hexdigest() == signal["signal_source"]["engine_source_sha256"]


@pytest.mark.parametrize("autocrlf", ["true", "false"])
def test_active_v4_chain_is_independent_of_autocrlf(tmp_path, autocrlf):
    contract_path = ROOT / "research_candidates/super1/super1_signal_contract_v4.json"
    runtime_path = ROOT / "live_forward/super1_xm_mt5_demo_config_v4.json"
    manifest_path = ROOT / "research_candidates/super1/super1_manifest_v4.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "forward_shadow/baseline_lock.json").read_text(encoding="utf-8"))

    pins = {
        "work/backtest/live_forward/super1_xm_mt5_demo_config_v4.json": manifest["config_sha256"],
        "work/backtest/research_candidates/super1/super1_signal_contract_v4.json": manifest["signal_contract_sha256"],
        "work/backtest/research_candidates/super1/super1_unsigned_candidate_v4.json": manifest["candidate_file_sha256"],
        "work/backtest/research_candidates/super1/super1_manifest_v4.json": None,
        "work/backtest/forward_shadow/frozen_config.json": contract["signal_source"]["config_sha256"],
        "work/backtest/forward_shadow/baseline_lock.json": None,
        "work/backtest/forward_shadow/engine_reliability_audit_2025_feb_mar_manifest.json": lock["baseline_manifest_sha256"],
    }
    for section, path_key, hash_key in (
        ("signal_source", "generator_path", "generator_sha256"),
        ("signal_source", "payload_adapter_path", "payload_adapter_sha256"),
        ("overlay_candidate", "runtime_path", None),
        ("demo_order_transport", "path", "sha256"),
        ("rth_session_calendar", "path", "sha256"),
    ):
        path = contract[section][path_key]
        pins["work/backtest/" + path] = None if hash_key is None else contract[section][hash_key]
    candidate_runtime = "work/backtest/" + contract["overlay_candidate"]["runtime_path"]
    pins[candidate_runtime] = None

    engine_files = sorted(
        (ROOT / "backtest").rglob("*.py"),
        key=lambda path: path.relative_to(ROOT).as_posix(),
    )
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "core.autocrlf", autocrlf], check=True)
    for relative in (".gitattributes", "work/backtest/.gitattributes"):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((REPO / relative).read_bytes())
    paths = list(pins) + [path.relative_to(REPO).as_posix() for path in engine_files]
    for relative in paths:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(subprocess.check_output(["git", "show", "HEAD:" + relative], cwd=REPO))
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True)
    for relative in paths:
        (tmp_path / relative).unlink()
    subprocess.run(["git", "-C", str(tmp_path), "checkout-index", "-a"], check=True)

    for relative in pins:
        expected = hashlib.sha256(
            subprocess.check_output(["git", "show", "HEAD:" + relative], cwd=REPO)
        ).hexdigest()
        assert hashlib.sha256((tmp_path / relative).read_bytes()).hexdigest() == expected, relative
    assert hashlib.sha256((tmp_path / "work/backtest/live_forward/super1_xm_mt5_demo_config_v4.json").read_bytes()).hexdigest() == manifest["config_sha256"]
    assert hashlib.sha256((tmp_path / "work/backtest/research_candidates/super1/super1_signal_contract_v4.json").read_bytes()).hexdigest() == manifest["signal_contract_sha256"]

    digest = hashlib.sha256()
    for source in engine_files:
        relative = source.relative_to(ROOT).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update((tmp_path / source.relative_to(REPO)).read_bytes())
        digest.update(b"\0")
    assert digest.hexdigest() == contract["signal_source"]["engine_source_sha256"]
