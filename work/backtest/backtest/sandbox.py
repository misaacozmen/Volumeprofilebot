"""Fail-closed subprocess boundary for untrusted strategy/backtest work."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Sequence

from .live.settings import minimal_subprocess_environment


class SandboxError(RuntimeError):
    pass


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


def _assign_windows_job(process: subprocess.Popen[bytes], profile: SandboxProfile):
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class BASIC(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise SandboxError("unable to create Windows Job Object")
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = 0x00002000 | 0x00000008 | 0x00000100
    info.BasicLimitInformation.ActiveProcessLimit = 1
    info.ProcessMemoryLimit = profile.memory_mb * 1024 * 1024
    if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        kernel32.CloseHandle(job)
        raise SandboxError("unable to install Windows Job Object limits")
    if not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(int(process._handle))):
        kernel32.CloseHandle(job)
        raise SandboxError("unable to assign sandbox process to Windows Job Object")
    if ctypes.WinDLL("ntdll").NtResumeProcess(wintypes.HANDLE(int(process._handle))) != 0:
        kernel32.TerminateJobObject(job, 1)
        kernel32.CloseHandle(job)
        raise SandboxError("unable to resume sandbox process")
    return kernel32, job


def run_isolated_worker(
    worker_path: str | Path, request: Mapping[str, Any], *, profile: str,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    limits = PROFILES.get(profile)
    if limits is None:
        raise SandboxError("unknown sandbox profile")
    payload = _canonical_json({**dict(request), "limits": {"output_mb": limits.output_mb}})
    max_payload_bytes = limits.output_mb * 1024 * 1024
    if len(payload) > max_payload_bytes:
        raise SandboxError("sandbox request is too large")
    worker = Path(worker_path).resolve()
    if not worker.is_file():
        raise SandboxError("sandbox worker is missing")
    command = [sys.executable, "-I", str(worker)]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
        "shell": False, "env": minimal_subprocess_environment(environment),
    }
    windows = os.name == "nt"
    if windows:
        kwargs["creationflags"] = 0x00000004
    else:
        kwargs["start_new_session"] = True
        kwargs["preexec_fn"] = _posix_limits(limits)
    try:
        process = subprocess.Popen(command, **kwargs)
    except Exception as exc:
        raise SandboxError("sandbox process could not be started") from exc
    job = None
    try:
        if windows:
            job = _assign_windows_job(process, limits)
        stdout, stderr = process.communicate(payload, timeout=limits.wall_seconds)
    except subprocess.TimeoutExpired as exc:
        if job is not None:
            job[0].TerminateJobObject(job[1], 1)
        else:
            process.kill()
        process.communicate()
        raise SandboxError("sandbox wall deadline exceeded") from exc
    finally:
        if job is not None:
            job[0].CloseHandle(job[1])
    if process.returncode != 0:
        detail = stderr.decode("utf-8", "replace")[-1000:]
        raise SandboxError(f"sandbox worker failed ({process.returncode}): {detail}")
    if len(stdout) > max_payload_bytes:
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


def run_sandbox(
    request: Mapping[str, Any], *, profile: str = "offline", environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    return run_isolated_worker(_worker_path(), request, profile=profile, environment=environment)


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
    rewritten = list(argv)
    if "--output-dir" in rewritten:
        rewritten[rewritten.index("--output-dir") + 1] = str(staging)
    else:
        rewritten.extend(("--output-dir", str(staging)))
    try:
        response = run_sandbox({"action": "cli", "argv": rewritten, "output_root": str(staging)}, profile=profile)
        _validated_files(staging, PROFILES[profile].output_mb * 1024 * 1024)
        if output.exists():
            output.rmdir()
        os.replace(staging, output)
        return str(response.get("stdout", ""))
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
