"""Fail-closed subprocess boundary for untrusted strategy/backtest work."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Sequence

from .live.settings import minimal_subprocess_environment


class SandboxError(RuntimeError):
    pass


def windows_appcontainer_available() -> bool:
    """Return whether the checked-in Win32 AppContainer launcher can be loaded."""
    if os.name != "nt":
        return False
    launcher = Path(__file__).resolve().parents[1] / "scripts" / "windows_appcontainer_launcher.py"
    if not launcher.is_file():
        return False
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        userenv = ctypes.WinDLL("userenv", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        required = (
            (kernel32, "CreateProcessW"),
            (kernel32, "InitializeProcThreadAttributeList"),
            (kernel32, "UpdateProcThreadAttribute"),
            (kernel32, "CreateJobObjectW"),
            (kernel32, "SetInformationJobObject"),
            (kernel32, "AssignProcessToJobObject"),
            (userenv, "CreateAppContainerProfile"),
            (userenv, "DeriveAppContainerSidFromAppContainerName"),
            (advapi32, "SetNamedSecurityInfoW"),
            (advapi32, "SetEntriesInAclW"),
        )
        return all(hasattr(dll, symbol) for dll, symbol in required)
    except (AttributeError, OSError):
        return False


def posix_namespace_available() -> bool:
    """Return whether a real namespace+ACL launcher is installed.

    Resource limits and a new session are not a filesystem/network boundary.
    Until the deployment supplies an attested launcher with user, mount,
    network, PID, and ACL namespaces, POSIX execution must also fail closed.
    """
    return False


@dataclass(frozen=True, slots=True)
class SandboxProfile:
    wall_seconds: int
    cpu_seconds: int
    memory_mb: int
    output_mb: int
    open_files: int = 256
    child_processes: int = 0


PROFILES = {
    "offline": SandboxProfile(900, 600, 4096, 1024),
    "live-signal": SandboxProfile(20, 15, 1024, 16),
}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def _worker_path() -> Path:
    path = (Path(__file__).resolve().parents[1] / "scripts" / "backtest_sandbox_worker.py").resolve()
    if not path.is_file():
        raise SandboxError("sandbox worker is missing")
    return path


def _posix_limits(profile: SandboxProfile):
    def install() -> None:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (profile.cpu_seconds, profile.cpu_seconds))
        resource.setrlimit(resource.RLIMIT_AS, (profile.memory_mb * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_FSIZE, (profile.output_mb * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_NOFILE, (profile.open_files, profile.open_files))
        if hasattr(resource, "RLIMIT_NPROC"):
            resource.setrlimit(resource.RLIMIT_NPROC, (1, 1))
    return install


def _windows_launcher_path() -> Path:
    path = (Path(__file__).resolve().parents[1] / "scripts" / "windows_appcontainer_launcher.py").resolve()
    if not path.is_file():
        raise SandboxError("Windows AppContainer launcher is missing; sandboxed backtest is BLOCKED_PLATFORM")
    return path


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _digest_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: item.relative_to(root).as_posix()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(_digest_file(path)))
    return digest.hexdigest()


def _runtime_lib_ignore(directory: str, names: list[str]) -> list[str]:
    ignored = []
    for name in names:
        lower = name.casefold()
        if (
            lower in {"site-packages", "sitecustomize.py", "usercustomize.py"}
            or lower.endswith((".pth", ".egg-link"))
            or lower == "__pycache__"
            or lower.endswith((".pyc", ".pyo"))
        ):
            ignored.append(name)
    return ignored


def _allow_runtime_binary(directory: Path, name: str) -> bool:
    lower = name.casefold()
    if directory.name.casefold() == "dlls":
        return lower.endswith(".pyd") or lower in {"sqlite3.dll"} or (
            lower.startswith("lib") and lower.endswith(".dll")
        )
    return lower.endswith(".dll") and lower.startswith(("python", "vcruntime", "msvcp", "ucrtbase"))


def _runtime_dll_ignore(directory: str, names: list[str]) -> list[str]:
    root = Path(directory)
    return [name for name in names if not _allow_runtime_binary(root, name)]


def _dependency_ignore(directory: str, names: list[str]) -> list[str]:
    return _runtime_lib_ignore(directory, names)


def _prepare_windows_runtime(request: Mapping[str, Any]) -> tuple[Path, Path, Path]:
    """Build a deterministic, allowlisted runtime without ACLs on host installs."""
    root = Path(tempfile.mkdtemp(prefix=".otobt-appcontainer-", dir=tempfile.gettempdir())).resolve()
    try:
        runtime = root / "runtime"
        (runtime / "scripts").mkdir(parents=True)
        shutil.copy2(_worker_path(), runtime / "scripts" / "backtest_sandbox_worker.py")
        shutil.copytree(
            Path(__file__).resolve().parents[1] / "backtest",
            runtime / "backtest",
            ignore=_runtime_lib_ignore,
        )
        schema_source = Path(__file__).resolve().parents[1] / "schemas" / "super1_environment_v1.schema.json"
        (runtime / "schemas").mkdir(parents=True, exist_ok=True)
        shutil.copy2(schema_source, runtime / "schemas" / schema_source.name)

        base_root = Path(sys.base_prefix).resolve()
        base_python = base_root / "python.exe"
        if not base_python.is_file():
            raise SandboxError("isolated Python runtime is missing the base interpreter")
        python_root = runtime / "python"
        python_root.mkdir()
        shutil.copy2(base_python, python_root / base_python.name)
        copied_dlls: list[dict[str, str]] = []
        for path in base_root.glob("*.dll"):
            if path.is_file() and _allow_runtime_binary(base_root, path.name):
                target = python_root / path.name
                shutil.copy2(path, target)
                copied_dlls.append({"path": f"python/{path.name}", "sha256": _digest_file(target)})
        archive = base_root / "python311.zip"
        if archive.is_file():
            target = python_root / archive.name
            shutil.copy2(archive, target)
            copied_dlls.append({"path": f"python/{archive.name}", "sha256": _digest_file(target)})
        for directory in ("DLLs", "Lib"):
            source = base_root / directory
            if source.is_dir():
                shutil.copytree(
                    source,
                    python_root / directory,
                    ignore=_runtime_dll_ignore if directory == "DLLs" else _runtime_lib_ignore,
                )
        for path in sorted((item for item in (python_root / "DLLs").rglob("*") if item.is_file()), key=lambda item: item.relative_to(python_root).as_posix()):
            copied_dlls.append({"path": path.relative_to(python_root).as_posix(), "sha256": _digest_file(path)})

        dependency_records: list[dict[str, str]] = []
        if request.get("action") == "cli":
            site_packages = Path(sys.prefix).resolve() / "Lib" / "site-packages"
            if not site_packages.is_dir():
                raise SandboxError("isolated Python runtime is missing site-packages")
            isolated_site_packages = python_root / "Lib" / "site-packages"
            isolated_site_packages.mkdir(parents=True, exist_ok=True)
            required_packages = (
                "cffi",
                "cryptography",
                "dateutil",
                "numpy",
                "numpy.libs",
                "pandas",
                "pandas.libs",
                "pycparser",
                "six.py",
                "tzdata",
            )
            for name in required_packages:
                source = site_packages / name
                if not source.exists() or source.is_symlink():
                    raise SandboxError(f"isolated Python runtime is missing dependency: {name}")
                target = isolated_site_packages / name
                if target.exists():
                    raise SandboxError(f"isolated dependency collision: {name}")
                if source.is_dir():
                    if any(item.is_symlink() for item in source.rglob("*")):
                        raise SandboxError(f"isolated dependency contains a reparse entry: {name}")
                    shutil.copytree(source, target, ignore=_dependency_ignore)
                else:
                    shutil.copy2(source, target)
                dependency_records.append({"name": name, "sha256": _digest_tree(target) if target.is_dir() else _digest_file(target)})
            for source in sorted(site_packages.glob("_cffi_backend*.pyd")):
                if source.is_symlink():
                    raise SandboxError("isolated dependency contains a reparse entry: _cffi_backend")
                target = isolated_site_packages / source.name
                if target.exists():
                    raise SandboxError(f"isolated dependency collision: {source.name}")
                shutil.copy2(source, target)
                dependency_records.append({"name": source.name, "sha256": _digest_file(target)})

        runtime_manifest = {
            "schema_version": "windows-sandbox-runtime-v1",
            "interpreter": {"path": "python/python.exe", "sha256": _digest_file(python_root / base_python.name)},
            "stdlib": {
                "source": "CPython base Lib without site-packages/startup loaders",
                "file_count": sum(1 for item in (python_root / "Lib").rglob("*") if item.is_file()),
            },
            "dependencies": dependency_records,
            "dlls": copied_dlls,
            "excluded_host_entries": ["Lib/site-packages", "sitecustomize.py", "usercustomize.py", "*.pth", "*.egg-link"],
        }
        manifest_path = runtime / "runtime_manifest.json"
        manifest_path.write_text(json.dumps(runtime_manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8", newline="\n")
        return root, runtime, python_root / base_python.name
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise


def _validated_acl_roots(request: Mapping[str, Any]) -> list[Path]:
    values = request.get("input_roots", [])
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        raise SandboxError("sandbox input_roots must be a list")
    roots: list[Path] = []
    for value in values:
        path = Path(str(value))
        if not path.is_absolute() or not path.is_dir():
            raise SandboxError("sandbox input ACL root must be an existing absolute directory")
        resolved = path.resolve()
        if resolved.parent == resolved:
            raise SandboxError("sandbox input ACL root may not be a filesystem root")
        roots.append(resolved)
    if len(set(roots)) != len(roots):
        raise SandboxError("sandbox input ACL roots must be unique")
    return roots


def _reject_existing_hardlinks(root: Path, scope: str) -> None:
    """Reject file entries whose inode/file record has more than one name."""
    try:
        entries = root.rglob("*")
        for path in entries:
            metadata = path.lstat()
            if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink > 1:
                raise SandboxError(f"sandbox {scope} contains a hardlink entry: {path}")
    except SandboxError:
        raise
    except OSError as exc:
        raise SandboxError(f"sandbox {scope} link validation failed") from exc


def _validated_staging_policy(request: Mapping[str, Any]) -> tuple[Path, list[Path]]:
    """Accept only owner-selected, already-created narrow staging directories."""
    raw_output = request.get("output_root")
    output_root = Path(str(raw_output or ""))
    if not output_root.is_absolute() or not output_root.is_dir():
        raise SandboxError("trusted sandbox output_root must be a pre-created directory")
    output_root = output_root.resolve()
    roots = _validated_acl_roots(request)
    if output_root in roots or any(output_root in root.parents or root in output_root.parents for root in roots):
        raise SandboxError("sandbox staging policy has overlapping input and output roots")
    # AppContainer ACLs are path-based; a hardlink can otherwise expose a
    # file whose security descriptor was inherited outside the allowed root.
    _reject_existing_hardlinks(output_root, "output root")
    for root in roots:
        _reject_existing_hardlinks(root, "input root")
    return output_root, roots


def _windows_launcher_result(
    payload: bytes,
    request: Mapping[str, Any],
    limits: SandboxProfile,
    *,
    environment: Mapping[str, str] | None,
) -> tuple[bytes, dict[str, Any]]:
    if not windows_appcontainer_available():
        raise SandboxError("Windows AppContainer isolation is unavailable; sandboxed backtest is BLOCKED_PLATFORM")
    launcher = _windows_launcher_path()
    output_root, input_roots = _validated_staging_policy(request)
    root, runtime, interpreter = _prepare_windows_runtime(request)
    expected_runtime_manifest_hash = _digest_file(runtime / "runtime_manifest.json")
    ro_roots = [runtime, *input_roots]
    command = [
        sys.executable, "-I", str(launcher),
        "--python", str(interpreter),
        "--worker", str(runtime / "scripts" / "backtest_sandbox_worker.py"),
        "--cwd", str(runtime),
        "--rw-root", str(output_root),
        "--cpu-seconds", str(limits.cpu_seconds),
        "--memory-mb", str(limits.memory_mb),
        "--wall-seconds", str(limits.wall_seconds),
        "--output-bytes", str(limits.output_mb * 1024 * 1024),
    ]
    for path in ro_roots:
        command.extend(("--ro-root", str(path)))
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            env=minimal_subprocess_environment(environment),
            creationflags=0x08000000,
        )
        try:
            stdout, stderr = process.communicate(payload, timeout=limits.wall_seconds + 3)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.communicate()
            raise SandboxError("sandbox wall deadline exceeded") from exc
    finally:
        shutil.rmtree(root, ignore_errors=True)
    if len(stdout) > 4 * 1024 * 1024:
        raise SandboxError("sandbox launcher response is too large")
    try:
        envelope = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        detail = stderr.decode("utf-8", "replace")[-1000:]
        raise SandboxError(f"sandbox launcher returned invalid JSON: {detail}") from exc
    if not isinstance(envelope, dict) or envelope.get("schema") != "windows-appcontainer-launcher-v1":
        raise SandboxError("sandbox launcher returned an invalid closed schema")
    status = envelope.get("status")
    if status == "BLOCKED_PLATFORM":
        stage = str(envelope.get("error_stage", "unknown"))
        code = envelope.get("error_code")
        detail = f" at {stage}" if code is None else f" at {stage} ({code})"
        raise SandboxError(f"Windows AppContainer launcher failed closed{detail}; sandboxed backtest is BLOCKED_PLATFORM")
    if status == "DEADLINE":
        raise SandboxError("sandbox wall deadline exceeded")
    if status == "OUTPUT_LIMIT":
        raise SandboxError("sandbox output limit exceeded")
    if status != "OK":
        detail = str(envelope.get("worker_stderr_tail", ""))[-1000:]
        raise SandboxError(f"sandbox worker failed ({envelope.get('exit_code')}): {detail}")
    try:
        worker_stdout = __import__("base64").b64decode(str(envelope["worker_stdout_b64"]), validate=True)
        attestation = envelope["attestation"]
    except (KeyError, ValueError, TypeError) as exc:
        raise SandboxError("sandbox launcher returned an invalid attestation schema") from exc
    if len(worker_stdout) > 2 * 1024 * 1024:
        raise SandboxError("sandbox response is too large")
    _validate_windows_attestation(attestation, worker_stdout)
    expected_launcher_hash = hashlib.sha256(launcher.read_bytes()).hexdigest()
    expected_worker_hash = hashlib.sha256(_worker_path().read_bytes()).hexdigest()
    if (
        attestation["launcher_sha256"] != expected_launcher_hash
        or attestation["worker_sha256"] != expected_worker_hash
        or attestation["runtime_manifest_sha256"] != expected_runtime_manifest_hash
    ):
        raise SandboxError("sandbox attestation code hash mismatch")
    if attestation["windows_build"] != platform.version() or attestation["command"] != [
        str(interpreter), "-I", str(runtime / "scripts" / "backtest_sandbox_worker.py")
    ]:
        raise SandboxError("sandbox attestation host or command mismatch")
    return worker_stdout, attestation


def _validate_windows_attestation(value: Any, worker_stdout: bytes) -> None:
    required = {
        "schema_version", "launcher_sha256", "worker_sha256", "runtime_manifest_sha256", "windows_build",
        "appcontainer_sid", "capabilities", "network_capabilities", "command",
        "exit_code", "test_results_sha256", "job_limits", "acl_roots",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise SandboxError("sandbox attestation is not a closed schema")
    digest = re.compile(r"^[0-9a-f]{64}$")
    for name in ("launcher_sha256", "worker_sha256", "runtime_manifest_sha256", "test_results_sha256"):
        if not isinstance(value[name], str) or not digest.fullmatch(value[name]):
            raise SandboxError("sandbox attestation contains an invalid hash")
    if value["schema_version"] != "windows-appcontainer-launcher-v1" or value["capabilities"] != [] or value["network_capabilities"] != []:
        raise SandboxError("sandbox attestation does not prove zero network capabilities")
    if not isinstance(value["windows_build"], str) or not value["windows_build"]:
        raise SandboxError("sandbox attestation is missing the Windows build")
    if not isinstance(value["appcontainer_sid"], str) or not re.fullmatch(r"S-1-15-2-(?:\d+-)*\d+", value["appcontainer_sid"]):
        raise SandboxError("sandbox attestation contains an invalid AppContainer SID")
    if not isinstance(value["command"], list) or not all(isinstance(item, str) and item for item in value["command"]):
        raise SandboxError("sandbox attestation command is invalid")
    if value["exit_code"] != 0 or value["test_results_sha256"] != hashlib.sha256(worker_stdout).hexdigest():
        raise SandboxError("sandbox attestation is not bound to the child result")
    limits = value["job_limits"]
    if not isinstance(limits, dict) or set(limits) != {"active_process_limit", "cpu_seconds", "memory_mb", "wall_seconds", "output_bytes"}:
        raise SandboxError("sandbox attestation Job Object limits are invalid")
    if limits["active_process_limit"] != 1 or not all(isinstance(limits[name], int) and limits[name] > 0 for name in ("cpu_seconds", "memory_mb", "wall_seconds", "output_bytes")):
        raise SandboxError("sandbox attestation does not prove the required process/resource limits")
    roots = value["acl_roots"]
    if not isinstance(roots, dict) or set(roots) != {"read_write", "read_only"} or not all(isinstance(item, str) and Path(item).is_absolute() for kind in roots.values() for item in kind):
        raise SandboxError("sandbox attestation ACL roots are invalid")


def run_sandbox(
    request: Mapping[str, Any], *, profile: str = "offline", environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    limits = PROFILES.get(profile)
    if limits is None:
        raise SandboxError("unknown sandbox profile")
    payload = _canonical_json({**dict(request), "limits": {"output_mb": limits.output_mb}})
    if len(payload) > 4 * 1024 * 1024:
        raise SandboxError("sandbox request is too large")
    # Validate caller-owned staging before platform capability so malformed
    # requests cannot be reported as platform blocks.
    _validated_staging_policy(request)
    if os.name == "nt":
        stdout, attestation = _windows_launcher_result(
            payload,
            request,
            limits,
            environment=environment,
        )
        try:
            response = json.loads(stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SandboxError("sandbox returned invalid JSON") from exc
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise SandboxError("sandbox returned an invalid response schema")
        claimed = response.pop("response_hash", None)
        if claimed != hashlib.sha256(_canonical_json(response)).hexdigest():
            raise SandboxError("sandbox response hash mismatch")
        response["sandbox_attestation"] = attestation
        return response
    if not posix_namespace_available():
        raise SandboxError("POSIX namespace and ACL isolation is unavailable; sandboxed backtest is BLOCKED_PLATFORM")
    command = [sys.executable, "-I", str(_worker_path())]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
        "shell": False, "env": minimal_subprocess_environment(environment),
    }
    kwargs["start_new_session"] = True
    kwargs["preexec_fn"] = _posix_limits(limits)
    try:
        process = subprocess.Popen(command, **kwargs)
    except Exception as exc:
        raise SandboxError("sandbox process could not be started") from exc
    try:
        stdout, stderr = process.communicate(payload, timeout=limits.wall_seconds)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.communicate()
        raise SandboxError("sandbox wall deadline exceeded") from exc
    if process.returncode != 0:
        detail = stderr.decode("utf-8", "replace")[-1000:]
        raise SandboxError(f"sandbox worker failed ({process.returncode}): {detail}")
    if len(stdout) > 2 * 1024 * 1024:
        raise SandboxError("sandbox response is too large")
    try:
        response = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SandboxError("sandbox returned invalid JSON") from exc
    if not isinstance(response, dict) or response.get("ok") is not True:
        raise SandboxError("sandbox returned an invalid response schema")
    claimed = response.pop("response_hash", None)
    if claimed != hashlib.sha256(_canonical_json(response)).hexdigest():
        raise SandboxError("sandbox response hash mismatch")
    return response


def _validated_files(root: Path, max_bytes: int) -> None:
    root = root.resolve()
    total = 0
    for path in root.rglob("*"):
        if path.is_symlink():
            raise SandboxError("sandbox output may not contain symlinks")
        resolved = path.resolve()
        if root != resolved and root not in resolved.parents:
            raise SandboxError("sandbox output escaped its staging directory")
        if path.is_file():
            total += path.stat().st_size
            if total > max_bytes:
                raise SandboxError("sandbox output limit exceeded")


def run_cli_sandboxed(argv: Sequence[str], output_dir: Path, *, profile: str = "offline") -> str:
    output = output_dir.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and any(output.iterdir()):
        raise SandboxError("sandbox output directory must be absent or empty")
    staging = Path(tempfile.mkdtemp(prefix=".otobt-sandbox-", dir=output.parent)).resolve()
    sandbox_output = staging / "result"
    rewritten = list(argv)
    if "--output-dir" in rewritten:
        rewritten[rewritten.index("--output-dir") + 1] = str(sandbox_output)
    else:
        rewritten.extend(("--output-dir", str(sandbox_output)))
    input_roots: list[str] = []
    for index, value in enumerate(argv):
        if index and argv[index - 1] == "--output-dir":
            continue
        candidate = Path(str(value))
        if not candidate.exists():
            continue
        resolved = candidate.resolve()
        if resolved == staging or staging in resolved.parents:
            continue
        input_roots.append(str(resolved if resolved.is_dir() else resolved.parent))
    try:
        response = run_sandbox(
            {"action": "cli", "argv": rewritten, "output_root": str(staging), "input_roots": sorted(set(input_roots))},
            profile=profile,
        )
        _validated_files(staging, PROFILES[profile].output_mb * 1024 * 1024)
        if output.exists():
            output.rmdir()
        os.replace(staging, output)
        return str(response.get("stdout", ""))
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
