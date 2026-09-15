from __future__ import annotations

import os
from pathlib import Path
import time

import pytest

from backtest.sandbox import PROFILES, SandboxError, SandboxProfile, _validated_files, run_cli_sandboxed, run_sandbox


@pytest.mark.skipif(os.name == "nt", reason="host has no OS-enforced AppContainer launcher")
def test_worker_environment_excludes_broker_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XM_MT5_PASSWORD", "never-visible")
    monkeypatch.setenv("CAPITAL_API_KEY", "never-visible")
    response = run_sandbox({"action": "probe_env"})
    rendered = repr(response)
    assert "never-visible" not in rendered
    assert not any(name.startswith(("XM_", "CAPITAL_", "SUPER1_")) for name in response["environment"])


@pytest.mark.skipif(os.name == "nt", reason="host has no OS-enforced AppContainer launcher")
@pytest.mark.parametrize("action", ["probe_network", "probe_subprocess", "probe_live_import", "probe_dynamic"])
def test_worker_denies_network_and_child_processes(action: str) -> None:
    with pytest.raises(SandboxError, match="PermissionError"):
        run_sandbox({"action": action})


@pytest.mark.skipif(os.name == "nt", reason="host has no OS-enforced AppContainer launcher")
def test_worker_timeout_terminates_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(PROFILES, "test-fast", SandboxProfile(1, 10, 1024, 1))
    started = time.monotonic()
    with pytest.raises(SandboxError, match="deadline"):
        run_sandbox({"action": "loop"}, profile="test-fast")
    assert time.monotonic() - started < 10


@pytest.mark.skipif(os.name == "nt", reason="host has no OS-enforced AppContainer launcher")
def test_worker_memory_limit_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(PROFILES, "test-memory", SandboxProfile(15, 10, 1024, 1))
    with pytest.raises(SandboxError, match="worker failed"):
        run_sandbox({"action": "memory"}, profile="test-memory")


def test_windows_without_appcontainer_fails_closed() -> None:
    if os.name == "nt":
        with pytest.raises(SandboxError, match="AppContainer.*BLOCKED"):
            run_sandbox({"action": "probe_env"})


def test_crash_does_not_publish_final_artifact(tmp_path: Path) -> None:
    destination = tmp_path / "final"
    with pytest.raises(SandboxError):
        run_cli_sandboxed(["run", "missing.csv", "--output-dir", str(destination)], destination)
    assert not destination.exists()


def test_symlink_output_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "staging"
    root.mkdir()
    link = root / "escape.txt"
    link.write_text("simulated link", encoding="utf-8")
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda self: self == link or original(self))
    with pytest.raises(SandboxError, match="symlink"):
        _validated_files(root, 1024)
