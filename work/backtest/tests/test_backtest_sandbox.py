from __future__ import annotations

import json
import os
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest
import backtest.sandbox as sandbox_module

from backtest.sandbox import (
    PROFILES,
    SandboxError,
    SandboxProfile,
    posix_namespace_available,
    run_cli_sandboxed,
    run_sandbox,
    windows_appcontainer_available,
)


def _sandbox_request(tmp_path: Path, request: dict[str, object]) -> dict[str, object]:
    output_root = tmp_path / "sandbox-output"
    output_root.mkdir(exist_ok=True)
    return {"output_root": str(output_root), **request}


def _require_backend() -> None:
    """Require an independently detected backend; valid-launch errors must fail."""
    if os.name == "nt":
        if not windows_appcontainer_available():
            pytest.fail("BLOCKED_PLATFORM: Windows AppContainer capability is unavailable")
        return
    if not posix_namespace_available():
        pytest.fail("BLOCKED_PLATFORM: POSIX namespace/ACL capability is unavailable")


def test_worker_environment_excludes_broker_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _require_backend()
    monkeypatch.setenv("XM_MT5_PASSWORD", "never-visible")
    monkeypatch.setenv("CAPITAL_API_KEY", "never-visible")
    response = run_sandbox(_sandbox_request(tmp_path, {"action": "probe_env"}))
    rendered = repr(response)
    assert "never-visible" not in rendered
    assert not any(name.startswith(("XM_", "CAPITAL_", "SUPER1_")) for name in response["environment"])


def test_worker_environment_is_explicit_allowlist_and_cannot_read_host_sentinel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _require_backend()
    from backtest.live.settings import WORKER_ENV_ALLOWLIST

    monkeypatch.setenv("OTOBT_HOST_ONLY_SENTINEL", "must-not-cross")
    response = run_sandbox(_sandbox_request(tmp_path, {"action": "probe_env"}))
    # Windows environment names are case-insensitive and the child API may
    # canonicalize SystemRoot to SYSTEMROOT.
    assert {name.casefold() for name in response["environment"]}.issubset(
        {name.casefold() for name in WORKER_ENV_ALLOWLIST}
    )
    assert "OTOBT_HOST_ONLY_SENTINEL" not in response["environment"]
    assert "must-not-cross" not in repr(response)


@pytest.mark.parametrize(
    "action",
    [
        "probe_network",
        "probe_subprocess",
        "probe_live_import",
        "probe_dynamic",
        "probe_native_load",
        "probe_native_load_via_wrapper",
    ],
)
def test_worker_denies_network_and_child_processes(tmp_path: Path, action: str) -> None:
    _require_backend()
    with pytest.raises(SandboxError, match="PermissionError"):
        run_sandbox(_sandbox_request(tmp_path, {"action": action}))


def test_worker_timeout_terminates_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _require_backend()
    monkeypatch.setitem(PROFILES, "test-fast", SandboxProfile(1, 10, 1024, 1))
    started = time.monotonic()
    with pytest.raises(SandboxError, match="deadline"):
        run_sandbox(_sandbox_request(tmp_path, {"action": "loop"}), profile="test-fast")
    assert time.monotonic() - started < 10


def test_worker_memory_limit_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _require_backend()
    monkeypatch.setitem(PROFILES, "test-memory", SandboxProfile(15, 10, 1024, 1))
    # Windows may surface a hard memory stop as either worker failure or the
    # bounded launcher deadline; both are fail-closed outcomes.
    with pytest.raises(SandboxError, match="worker failed|wall deadline exceeded"):
        run_sandbox(_sandbox_request(tmp_path, {"action": "memory"}), profile="test-memory")


def test_sandbox_backend_is_available() -> None:
    _require_backend()


def _prepare_os_boundary_roots(tmp_path: Path) -> dict[str, Path]:
    input_root = tmp_path / "input"
    input_root.mkdir()
    input_file = input_root / "allowed.txt"
    input_file.write_text("input", encoding="utf-8")
    output_root = tmp_path / "output"
    output_root.mkdir()
    outside_root = tmp_path / "outside"
    outside_root.mkdir()
    outside_file = outside_root / "outside.txt"
    outside_file.write_text("outside", encoding="utf-8")
    sibling_root = tmp_path / "sibling"
    sibling_root.mkdir()
    sibling_file = sibling_root / "sibling.txt"
    sibling_file.write_text("sibling", encoding="utf-8")
    return {
        "input_root": input_root,
        "input_file": input_file,
        "output_root": output_root,
        "outside_root": outside_root,
        "outside_file": outside_file,
        "sibling_file": sibling_file,
    }


def _probe_os_boundaries(roots: dict[str, Path], probe_paths: dict[str, str]) -> dict[str, object]:
    _require_backend()
    return run_sandbox(
        {
            "action": "probe_os_boundaries",
            "output_root": str(roots["output_root"]),
            "input_roots": [str(roots["input_root"])],
            "write_path": str(roots["output_root"] / "positive.txt"),
            "probe_paths": probe_paths,
        }
    )


def _assert_positive_boundaries(results: dict[str, dict[str, object]]) -> None:
    assert results["filesystem:input"]["allowed"] is True
    assert results["filesystem:explicit_staging_write"]["allowed"] is True


def _assert_attestation(response: dict[str, object]) -> None:
    attestation = response["sandbox_attestation"]
    assert attestation["capabilities"] == []
    assert attestation["network_capabilities"] == []
    assert len(attestation["runtime_manifest_sha256"]) == 64
    assert attestation["exit_code"] == 0
    assert attestation["command"]
    base_install = Path(sys.base_prefix).resolve()
    assert Path(attestation["command"][0]).resolve() != Path(sys.executable).resolve()
    assert all(
        base_install != Path(root).resolve() and base_install not in Path(root).resolve().parents
        for root in attestation["acl_roots"]["read_only"]
    )


def test_real_cli_backtest_publishes_nonempty_attested_artifacts(tmp_path: Path) -> None:
    _require_backend()
    data = Path(__file__).resolve().parents[1] / "data" / "raw" / "nq" / "CAPITALCOM_NAS100, 5_4eef5.csv"
    staging = tmp_path / "staging"
    staging.mkdir()
    output = staging / "result"
    response = run_sandbox(
        {
            "action": "cli",
            "argv": [
                "run",
                str(data),
                "--symbol",
                "CAPITALCOM_NAS100",
                "--timeframe",
                "5m",
                "--output-dir",
                str(output),
            ],
            "output_root": str(staging),
            "input_roots": [str(data.parent)],
        }
    )
    assert "Loaded 20055 candles: CAPITALCOM_NAS100 5m" in response["stdout"]
    artifacts = {path.name: path for path in output.iterdir() if path.is_file()}
    expected = {"summary.csv", "trades.csv", "monthly_stats.csv", "weekday_stats.csv", "parameters.csv", "risk_xray.json", "risk_xray.md"}
    assert expected.issubset(artifacts)
    assert all(path.stat().st_size > 0 for path in artifacts.values())
    assert all(len(hashlib.sha256(path.read_bytes()).hexdigest()) == 64 for path in artifacts.values())
    _assert_attestation(response)


def test_runtime_packaging_excludes_host_startup_and_base_site_packages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = tmp_path / "base"
    venv = tmp_path / "venv"
    base_lib = base / "Lib"
    base_site = base_lib / "site-packages"
    venv_site = venv / "Lib" / "site-packages"
    base_dlls = base / "DLLs"
    for directory in (base_lib, base_site, venv_site, base_dlls):
        directory.mkdir(parents=True)
    (base / "python.exe").write_bytes(b"base-interpreter")
    (base / "python311.dll").write_bytes(b"python")
    (base / "evil.dll").write_bytes(b"evil")
    (base_lib / "site.py").write_text("trusted = True", encoding="utf-8")
    (base_lib / "sitecustomize.py").write_text("raise RuntimeError('host startup')", encoding="utf-8")
    (base_lib / "usercustomize.py").write_text("raise RuntimeError('user startup')", encoding="utf-8")
    (base_lib / "host.pth").write_text("import host_startup", encoding="utf-8")
    (base_site / "host_only.py").write_text("raise RuntimeError('host package')", encoding="utf-8")
    (base_site / "cffi").mkdir()
    (base_site / "cffi" / "host.py").write_text("host = True", encoding="utf-8")
    (base_dlls / "_socket.pyd").write_bytes(b"socket")
    (base_dlls / "evil.dll").write_bytes(b"evil")
    (base_dlls / "sqlite3.dll").write_bytes(b"sqlite")
    required = ("cffi", "cryptography", "dateutil", "numpy", "numpy.libs", "pandas", "pandas.libs", "pycparser", "tzdata")
    for name in required:
        package = venv_site / name
        package.mkdir()
        (package / "allowed.py").write_text(name, encoding="utf-8")
    (venv_site / "six.py").write_text("six = True", encoding="utf-8")
    (venv_site / "_cffi_backend.cp311-win_amd64.pyd").write_bytes(b"backend")
    (venv_site / "host.pth").write_text("import host_startup", encoding="utf-8")
    monkeypatch.setattr(sandbox_module.sys, "base_prefix", str(base))
    monkeypatch.setattr(sandbox_module.sys, "prefix", str(venv))

    root, runtime, _ = sandbox_module._prepare_windows_runtime({"action": "cli"})
    try:
        assert not (runtime / "python" / "Lib" / "sitecustomize.py").exists()
        assert not (runtime / "python" / "Lib" / "usercustomize.py").exists()
        assert not (runtime / "python" / "Lib" / "host.pth").exists()
        assert not (runtime / "python" / "Lib" / "site-packages" / "host_only.py").exists()
        assert (runtime / "python" / "Lib" / "site-packages" / "cffi" / "allowed.py").is_file()
        assert not (runtime / "python" / "evil.dll").exists()
        assert not (runtime / "python" / "DLLs" / "evil.dll").exists()
        manifest = json.loads((runtime / "runtime_manifest.json").read_text(encoding="utf-8"))
        assert "cffi" in {item["name"] for item in manifest["dependencies"]}
        assert "DLLs/_socket.pyd" in {item["path"] for item in manifest["dlls"]}
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def owner_symlink_fixture_roots(pytestconfig: pytest.Config) -> dict[str, Path]:
    raw_root = pytestconfig.getoption("--symlink-fixture-root")
    if not raw_root:
        pytest.fail(
            "LINK_FIXTURE_ROOT_REQUIRED: run the normal-user preparation script, have the owner create its reported symlink, then rerun with --symlink-fixture-root"
        )
    root = Path(str(raw_root))
    if not root.is_absolute():
        pytest.fail("LINK_FIXTURE_ROOT_REQUIRED: fixture root must be an absolute path")
    root = root.resolve()
    roots = {
        "root": root,
        "input_root": root / "input",
        "input_file": root / "input" / "allowed.txt",
        "output_root": root / "output",
        "outside_root": root / "outside",
        "outside_file": root / "outside" / "outside.txt",
        "symlink": root / "output" / "escape-symlink.txt",
    }
    for name in ("root", "input_root", "output_root", "outside_root", "input_file", "outside_file"):
        if not roots[name].exists():
            pytest.fail(f"LINK_FIXTURE_ROOT_INVALID: missing prepared {name}: {roots[name]}")
    symlink = roots["symlink"]
    if not symlink.is_symlink():
        pytest.fail(f"LINK_FIXTURE_REQUIRED: owner link is missing: {symlink}")
    if symlink.resolve(strict=True) != roots["outside_file"].resolve(strict=True):
        pytest.fail(f"LINK_FIXTURE_INVALID: symlink target mismatch: {symlink}")
    return roots


def test_real_appcontainer_enforces_filesystem_boundaries(tmp_path: Path) -> None:
    roots = _prepare_os_boundary_roots(tmp_path)
    home_probe = Path.home() / "NTUSER.DAT"
    assert home_probe.is_file()
    response = _probe_os_boundaries(
        roots,
        {
            "repo": str(Path(__file__).resolve().parents[1] / "pyproject.toml"),
            "home": str(home_probe),
            "sibling": str(roots["sibling_file"]),
            "outside": str(roots["outside_file"]),
            "input": str(roots["input_file"]),
        },
    )
    results = {item["label"]: item for item in response["os_boundary_results"]}
    _assert_positive_boundaries(results)
    for label in ("repo", "home", "sibling", "outside"):
        assert results[f"filesystem:{label}"]["allowed"] is False
    _assert_attestation(response)


def test_real_appcontainer_enforces_network_boundary(tmp_path: Path) -> None:
    roots = _prepare_os_boundary_roots(tmp_path)
    response = _probe_os_boundaries(roots, {"input": str(roots["input_file"])})
    results = {item["label"]: item for item in response["os_boundary_results"]}
    _assert_positive_boundaries(results)
    # Windows permits binding the local loopback interface inside an
    # AppContainer; external name resolution is the egress probe.
    assert results["network:dns"]["allowed"] is False
    _assert_attestation(response)


def test_real_appcontainer_enforces_process_boundary(tmp_path: Path) -> None:
    roots = _prepare_os_boundary_roots(tmp_path)
    response = _probe_os_boundaries(roots, {"input": str(roots["input_file"])})
    results = {item["label"]: item for item in response["os_boundary_results"]}
    _assert_positive_boundaries(results)
    assert results["process:child"]["allowed"] is False
    _assert_attestation(response)


def test_real_appcontainer_rejects_symlink_escape(owner_symlink_fixture_roots: dict[str, Path]) -> None:
    roots = owner_symlink_fixture_roots
    symlink = roots["symlink"]
    response = _probe_os_boundaries(
        roots,
        {"input": str(roots["input_file"]), "symlink": str(symlink)},
    )
    results = {item["label"]: item for item in response["os_boundary_results"]}
    _assert_positive_boundaries(results)
    assert results["filesystem:symlink"]["allowed"] is False
    _assert_attestation(response)


def test_real_appcontainer_rejects_junction_escape(tmp_path: Path) -> None:
    roots = _prepare_os_boundary_roots(tmp_path)
    junction = roots["output_root"] / "escape-junction"
    try:
        subprocess.run(
            [os.environ["COMSPEC"], "/d", "/c", "mklink", "/J", str(junction), str(roots["outside_root"])],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        pytest.fail(f"LINK_FIXTURE_REQUIRED junction creation failed; owner must prepare only this fixture: {exc}")
    assert junction.is_dir()
    assert junction.resolve(strict=True) == roots["outside_root"].resolve(strict=True)
    response = _probe_os_boundaries(
        roots,
        {"input": str(roots["input_file"]), "junction": str(junction / roots["outside_file"].name)},
    )
    results = {item["label"]: item for item in response["os_boundary_results"]}
    _assert_positive_boundaries(results)
    assert results["filesystem:junction"]["allowed"] is False
    _assert_attestation(response)


def test_real_appcontainer_rejects_hardlink_escape(tmp_path: Path) -> None:
    roots = _prepare_os_boundary_roots(tmp_path)
    hardlink = roots["output_root"] / "escape-hardlink.txt"
    try:
        os.link(roots["outside_file"], hardlink)
    except OSError as exc:
        pytest.fail(f"LINK_FIXTURE_REQUIRED hardlink creation failed: {exc}")
    assert hardlink.is_file()
    assert os.path.samefile(hardlink, roots["outside_file"])
    try:
        response = _probe_os_boundaries(
            roots,
            {"input": str(roots["input_file"]), "hardlink": str(hardlink)},
        )
    except SandboxError as exc:
        # The production policy rejects pre-existing hardlinks before launch;
        # accepting a generic launcher failure here would hide regressions.
        assert "hardlink" in str(exc).casefold()
        return
    results = {item["label"]: item for item in response["os_boundary_results"]}
    _assert_positive_boundaries(results)
    assert results["filesystem:hardlink"]["allowed"] is False
    _assert_attestation(response)


def test_crash_does_not_publish_final_artifact(tmp_path: Path) -> None:
    destination = tmp_path / "final"
    with pytest.raises(SandboxError):
        run_cli_sandboxed(["run", "missing.csv", "--output-dir", str(destination)], destination)
    assert not destination.exists()
