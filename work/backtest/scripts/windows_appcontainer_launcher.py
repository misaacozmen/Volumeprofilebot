"""Small, fail-closed Win32 AppContainer process launcher.

This file is intentionally a standalone trusted launcher.  The Python worker is
never started with ``subprocess.Popen``: CreateProcessW receives a
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES attribute and an empty capability
list, so the child gets an AppContainer token with no network capability.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import subprocess
import sys
import threading
import time
from typing import Any

SCHEMA = "windows-appcontainer-launcher-v1"
ERROR_ALREADY_EXISTS = 183
HRESULT_ALREADY_EXISTS = (0x80000000 | (7 << 16) | ERROR_ALREADY_EXISTS)
CREATE_SUSPENDED = 0x00000004
CREATE_UNICODE_ENVIRONMENT = 0x00000400
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
INFINITE = 0xFFFFFFFF
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
HANDLE_FLAG_INHERIT = 0x00000001
SE_FILE_OBJECT = 1
DACL_SECURITY_INFORMATION = 0x00000004
OBJECT_INHERIT_ACE = 0x00000001
CONTAINER_INHERIT_ACE = 0x00000002
TRUSTEE_IS_SID = 0
TRUSTEE_IS_USER = 1
GRANT_ACCESS = 1
FILE_GENERIC_READ = 0x120089
FILE_GENERIC_WRITE = 0x120116
FILE_GENERIC_EXECUTE = 0x1200A0
DELETE = 0x00010000


class LauncherError(RuntimeError):
    def __init__(self, stage: str, winerror: int | None = None) -> None:
        self.stage = stage
        self.winerror = winerror
        suffix = "" if winerror is None else f" ({winerror})"
        super().__init__(f"{stage}{suffix}")


def _worker_environment(rw_root: Path, runtime_root: Path) -> dict[str, str]:
    schema_path = runtime_root / "schemas" / "super1_environment_v1.schema.json"
    try:
        payload = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LauncherError("worker environment schema is unavailable") from exc
    required = {
        "schema_version",
        "project_prefixes",
        "project_variables",
        "platform_variables",
        "ci_variables",
        "worker_allowed_variables",
    }
    if not isinstance(payload, dict) or set(payload) != required or payload.get("schema_version") != 1:
        raise LauncherError("worker environment schema is not closed v1")
    allowed = payload.get("worker_allowed_variables")
    if not isinstance(allowed, list) or not all(isinstance(value, str) for value in allowed):
        raise LauncherError("worker environment allowlist is invalid")
    environment = dict(os.environ)
    child_env = {name: str(value) for name in allowed if (value := environment.get(name))}
    child_env.update({
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TEMP": str(rw_root),
        "TMP": str(rw_root),
        "USERPROFILE": str(rw_root),
        "APPDATA": str(rw_root),
        "LOCALAPPDATA": str(rw_root),
        "HOMEDRIVE": rw_root.drive,
        "HOMEPATH": "\\",
    })
    return child_env


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [
        ("AppContainerSid", ctypes.c_void_p),
        ("Capabilities", ctypes.POINTER(SID_AND_ATTRIBUTES)),
        ("CapabilityCount", wintypes.DWORD),
        ("Reserved", wintypes.DWORD),
    ]


class STARTUPINFO(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class STARTUPINFOEX(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFO), ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


class TRUSTEE(ctypes.Structure):
    _fields_ = [
        ("pMultipleTrustee", ctypes.c_void_p),
        ("MultipleTrusteeOperation", wintypes.DWORD),
        ("TrusteeForm", wintypes.DWORD),
        ("TrusteeType", wintypes.DWORD),
        ("ptstrName", ctypes.c_void_p),
    ]


class EXPLICIT_ACCESS(ctypes.Structure):
    _fields_ = [
        ("grfAccessPermissions", wintypes.DWORD),
        ("grfAccessMode", wintypes.DWORD),
        ("grfInheritance", wintypes.DWORD),
        ("Trustee", TRUSTEE),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class Win32:
    def __init__(self) -> None:
        if os.name != "nt":
            raise LauncherError("Windows AppContainer requires Windows")
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.userenv = ctypes.WinDLL("userenv", use_last_error=True)
        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self._configure()

    def _configure(self) -> None:
        k = self.kernel32
        k.CloseHandle.argtypes = (wintypes.HANDLE,)
        k.CloseHandle.restype = wintypes.BOOL
        k.LocalFree.argtypes = (ctypes.c_void_p,)
        k.LocalFree.restype = ctypes.c_void_p
        k.CreatePipe.argtypes = (ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(SECURITY_ATTRIBUTES), wintypes.DWORD)
        k.CreatePipe.restype = wintypes.BOOL
        k.SetHandleInformation.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD)
        k.SetHandleInformation.restype = wintypes.BOOL
        k.InitializeProcThreadAttributeList.argtypes = (ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t))
        k.InitializeProcThreadAttributeList.restype = wintypes.BOOL
        k.UpdateProcThreadAttribute.argtypes = (ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p)
        k.UpdateProcThreadAttribute.restype = wintypes.BOOL
        k.DeleteProcThreadAttributeList.argtypes = (ctypes.c_void_p,)
        k.DeleteProcThreadAttributeList.restype = None
        k.CreateProcessW.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFOEX), ctypes.POINTER(PROCESS_INFORMATION))
        k.CreateProcessW.restype = wintypes.BOOL
        k.ResumeThread.argtypes = (wintypes.HANDLE,)
        k.ResumeThread.restype = wintypes.DWORD
        k.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k.GetExitCodeProcess.restype = wintypes.BOOL
        k.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        k.SetInformationJobObject.restype = wintypes.BOOL
        k.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        k.AssignProcessToJobObject.restype = wintypes.BOOL
        k.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        k.TerminateJobObject.restype = wintypes.BOOL
        u = self.userenv
        u.CreateAppContainerProfile.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(SID_AND_ATTRIBUTES), wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))
        u.CreateAppContainerProfile.restype = ctypes.c_long
        u.DeriveAppContainerSidFromAppContainerName.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p))
        u.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
        u.DeleteAppContainerProfile.argtypes = (wintypes.LPCWSTR,)
        u.DeleteAppContainerProfile.restype = ctypes.c_long
        a = self.advapi32
        a.FreeSid.argtypes = (ctypes.c_void_p,)
        a.FreeSid.restype = ctypes.c_void_p
        a.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))
        a.ConvertSidToStringSidW.restype = wintypes.BOOL
        a.GetNamedSecurityInfoW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p))
        a.GetNamedSecurityInfoW.restype = wintypes.DWORD
        a.SetEntriesInAclW.argtypes = (wintypes.DWORD, ctypes.POINTER(EXPLICIT_ACCESS), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
        a.SetEntriesInAclW.restype = wintypes.DWORD
        a.SetNamedSecurityInfoW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
        a.SetNamedSecurityInfoW.restype = wintypes.DWORD

    @staticmethod
    def error() -> int:
        return int(ctypes.get_last_error())

    def acl(self, path: Path, sid: ctypes.c_void_p, *, write: bool) -> tuple[Path, ctypes.c_void_p, ctypes.c_void_p]:
        sd = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        rc = self.advapi32.GetNamedSecurityInfoW(
            str(path), SE_FILE_OBJECT, DACL_SECURITY_INFORMATION,
            None, None, ctypes.byref(dacl), None, ctypes.byref(sd),
        )
        if rc:
            raise LauncherError("read ACL", int(rc))
        rights = FILE_GENERIC_READ | FILE_GENERIC_EXECUTE
        if write:
            rights |= FILE_GENERIC_WRITE | DELETE
        trustee = TRUSTEE(None, 0, TRUSTEE_IS_SID, TRUSTEE_IS_USER, ctypes.cast(sid, ctypes.c_void_p).value)
        entry = EXPLICIT_ACCESS(rights, GRANT_ACCESS, OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE if path.is_dir() else 0, trustee)
        new_dacl = ctypes.c_void_p()
        try:
            rc = self.advapi32.SetEntriesInAclW(1, ctypes.byref(entry), dacl, ctypes.byref(new_dacl))
            if rc:
                raise LauncherError("build ACL", int(rc))
            rc = self.advapi32.SetNamedSecurityInfoW(
                str(path), SE_FILE_OBJECT, DACL_SECURITY_INFORMATION,
                None, None, new_dacl, None,
            )
            if rc:
                raise LauncherError("apply ACL", int(rc))
        finally:
            if new_dacl:
                self.kernel32.LocalFree(new_dacl)
        if not sd or not dacl:
            if sd:
                self.kernel32.LocalFree(sd)
            raise LauncherError("read ACL returned no DACL")
        return path, sd, dacl

    def restore_acl(self, snapshot: tuple[Path, ctypes.c_void_p, ctypes.c_void_p]) -> None:
        path, sd, dacl = snapshot
        try:
            rc = self.advapi32.SetNamedSecurityInfoW(
                str(path), SE_FILE_OBJECT, DACL_SECURITY_INFORMATION,
                None, None, dacl, None,
            )
            if rc:
                raise LauncherError("restore ACL", int(rc))
        finally:
            self.kernel32.LocalFree(sd)

    def profile_sid(self, name: str) -> ctypes.c_void_p:
        sid = ctypes.c_void_p()
        hr = int(self.userenv.CreateAppContainerProfile(name, name, "offline backtest worker", None, 0, ctypes.byref(sid))) & 0xFFFFFFFF
        if hr not in (0, HRESULT_ALREADY_EXISTS & 0xFFFFFFFF):
            raise LauncherError("create AppContainer profile", hr)
        if not sid:
            hr = int(self.userenv.DeriveAppContainerSidFromAppContainerName(name, ctypes.byref(sid))) & 0xFFFFFFFF
            if hr != 0 or not sid:
                raise LauncherError("derive AppContainer SID", hr)
        return sid

    def sid_string(self, sid: ctypes.c_void_p) -> str:
        value = wintypes.LPWSTR()
        if not self.advapi32.ConvertSidToStringSidW(sid, ctypes.byref(value)):
            raise LauncherError("convert AppContainer SID", self.error())
        try:
            return value.value
        finally:
            self.kernel32.LocalFree(value)

    def create_job(self, profile: dict[str, int]) -> wintypes.HANDLE:
        job = self.kernel32.CreateJobObjectW(None, None)
        if not job:
            raise LauncherError("create Job Object", self.error())
        info = EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            | JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | JOB_OBJECT_LIMIT_PROCESS_TIME
        )
        info.BasicLimitInformation.ActiveProcessLimit = 1
        info.BasicLimitInformation.PerProcessUserTimeLimit = int(profile["cpu_seconds"] * 10_000_000)
        info.ProcessMemoryLimit = int(profile["memory_mb"] * 1024 * 1024)
        if not self.kernel32.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)):
            self.kernel32.CloseHandle(job)
            raise LauncherError("install Job Object limits", self.error())
        return job

    def terminate(self, job: wintypes.HANDLE) -> None:
        self.kernel32.TerminateJobObject(job, 1)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def _environment_block(values: dict[str, str]) -> ctypes.Array[ctypes.c_wchar]:
    text = "\0".join(f"{key}={value}" for key, value in sorted(values.items())) + "\0\0"
    return ctypes.create_unicode_buffer(text)


def _handle_int(handle: Any) -> int:
    value = getattr(handle, "value", handle)
    if isinstance(value, bytes):
        return int.from_bytes(value, "little")
    return int(value)


def _absolute_existing(
    value: str,
    *,
    directory: bool | None = None,
    preserve_interpreter_path: bool = False,
) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise LauncherError("launcher path must be absolute")
    resolved = path.absolute() if preserve_interpreter_path else path.resolve(strict=True)
    if not resolved.is_file() and directory is not True:
        raise LauncherError("launcher executable is not a file")
    if not resolved.exists():
        raise LauncherError("launcher path does not exist")
    if directory is True and not resolved.is_dir():
        raise LauncherError("launcher ACL root is not a directory")
    if directory is False and not resolved.is_file():
        raise LauncherError("launcher executable is not a file")
    return resolved


def _reject_hardlinks(root: Path, scope: str) -> None:
    """Recheck trusted staging immediately before ACL/process transitions."""
    try:
        for path in root.rglob("*"):
            metadata = path.lstat()
            if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink > 1:
                raise LauncherError(f"{scope} contains a hardlink entry: {path}")
    except LauncherError:
        raise
    except OSError as exc:
        raise LauncherError(f"{scope} hardlink validation failed") from exc


def _read_pipe(handle: wintypes.HANDLE, chunks: list[bytes], total: list[int], limit: int, overflow: threading.Event) -> None:
    import msvcrt

    fd = msvcrt.open_osfhandle(_handle_int(handle), os.O_RDONLY)
    try:
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                return
            total[0] += len(chunk)
            if total[0] > limit:
                overflow.set()
                return
            chunks.append(chunk)
    finally:
        os.close(fd)


def launch(args: argparse.Namespace) -> dict[str, Any]:
    api = Win32()
    worker = _absolute_existing(args.worker, directory=False)
    python_exe = _absolute_existing(
        args.python,
        directory=False,
        preserve_interpreter_path=True,
    )
    cwd = _absolute_existing(args.cwd, directory=True)
    runtime_manifest = _absolute_existing(cwd / "runtime_manifest.json", directory=False)
    runtime_manifest_sha256 = hashlib.sha256(runtime_manifest.read_bytes()).hexdigest()
    rw_root = _absolute_existing(args.rw_root, directory=True)
    ro_roots = [_absolute_existing(value, directory=True) for value in args.ro_root]
    for path in ro_roots:
        if path == rw_root or rw_root in path.parents or path in rw_root.parents:
            raise LauncherError("read-only ACL root overlaps writable root")
    _reject_hardlinks(rw_root, "writable staging")

    profile = {
        "cpu_seconds": int(args.cpu_seconds),
        "memory_mb": int(args.memory_mb),
    }
    name = f"otobt-sandbox-{os.getpid()}-{time.monotonic_ns()}"
    sid = api.profile_sid(name)
    sid_text = api.sid_string(sid)
    job = None
    process = PROCESS_INFORMATION()
    attribute_buffer = None
    input_handle = wintypes.HANDLE()
    output_handle = wintypes.HANDLE()
    error_handle = wintypes.HANDLE()
    parent_input = wintypes.HANDLE()
    parent_output = wintypes.HANDLE()
    parent_error = wintypes.HANDLE()
    worker_stdout: list[bytes] = []
    worker_stderr: list[bytes] = []
    total_output = [0]
    overflow = threading.Event()
    status = "WORKER_FAILED"
    exit_code = -1
    acl_snapshots: list[tuple[Path, ctypes.c_void_p, ctypes.c_void_p]] = []
    command = [str(python_exe), "-I", str(worker)]
    try:
        # The parent performs an early scan for useful diagnostics.  This
        # trusted launcher repeats it after process hand-off and again after
        # ACL mutation so a newly-added hardlink cannot pass the launch gate
        # merely because it appeared between the parent's scan and ACL setup.
        _reject_hardlinks(rw_root, "writable staging before ACL")
        acl_snapshots.append(api.acl(rw_root, sid, write=True))
        acl_snapshots.append(api.acl(worker, sid, write=False))
        for path in ro_roots:
            acl_snapshots.append(api.acl(path, sid, write=False))

        pipe_security = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, True)
        if not api.kernel32.CreatePipe(ctypes.byref(input_handle), ctypes.byref(parent_input), ctypes.byref(pipe_security), 0):
            raise LauncherError("create worker stdin pipe", api.error())
        if not api.kernel32.CreatePipe(ctypes.byref(parent_output), ctypes.byref(output_handle), ctypes.byref(pipe_security), 0):
            raise LauncherError("create worker stdout pipe", api.error())
        if not api.kernel32.CreatePipe(ctypes.byref(parent_error), ctypes.byref(error_handle), ctypes.byref(pipe_security), 0):
            raise LauncherError("create worker stderr pipe", api.error())
        for handle in (parent_input, parent_output, parent_error):
            if not api.kernel32.SetHandleInformation(handle, HANDLE_FLAG_INHERIT, 0):
                raise LauncherError("remove pipe handle inheritance", api.error())

        handles = (wintypes.HANDLE * 3)(input_handle, output_handle, error_handle)
        caps = SECURITY_CAPABILITIES(sid, None, 0, 0)
        required = ctypes.c_size_t()
        api.kernel32.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(required))
        if not required.value:
            raise LauncherError("size AppContainer process attributes", api.error())
        attribute_buffer = ctypes.create_string_buffer(required.value)
        if not api.kernel32.InitializeProcThreadAttributeList(attribute_buffer, 2, 0, ctypes.byref(required)):
            raise LauncherError("initialize AppContainer process attributes", api.error())
        if not api.kernel32.UpdateProcThreadAttribute(attribute_buffer, 0, PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES, ctypes.byref(caps), ctypes.sizeof(caps), None, None):
            raise LauncherError("set AppContainer security capabilities", api.error())
        if not api.kernel32.UpdateProcThreadAttribute(attribute_buffer, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, ctypes.byref(handles), ctypes.sizeof(handles), None, None):
            raise LauncherError("set AppContainer handle allowlist", api.error())

        startup = STARTUPINFOEX()
        startup.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEX)
        startup.StartupInfo.dwFlags = 0x00000100  # STARTF_USESTDHANDLES
        startup.StartupInfo.hStdInput = input_handle
        startup.StartupInfo.hStdOutput = output_handle
        startup.StartupInfo.hStdError = error_handle
        startup.lpAttributeList = ctypes.cast(attribute_buffer, ctypes.c_void_p)
        child_env = _worker_environment(rw_root, cwd)
        environment = _environment_block(child_env)
        command_buffer = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
        _reject_hardlinks(rw_root, "writable staging before process")
        if not api.kernel32.CreateProcessW(
            str(python_exe), command_buffer, None, None, True,
            CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT,
            ctypes.cast(environment, ctypes.c_void_p), str(cwd), ctypes.byref(startup), ctypes.byref(process),
        ):
            raise LauncherError("create AppContainer child", api.error())
        api.kernel32.CloseHandle(input_handle)
        input_handle = None
        api.kernel32.CloseHandle(output_handle)
        output_handle = None
        api.kernel32.CloseHandle(error_handle)
        error_handle = None
        job = api.create_job(profile)
        if not api.kernel32.AssignProcessToJobObject(job, process.hProcess):
            raise LauncherError("assign AppContainer child to Job Object", api.error())
        if api.kernel32.ResumeThread(process.hThread) == 0xFFFFFFFF:
            raise LauncherError("resume AppContainer child", api.error())
        parent_input_fd = __import__("msvcrt").open_osfhandle(_handle_int(parent_input), os.O_WRONLY)
        parent_input = None
        try:
            payload = sys.stdin.buffer.read(4 * 1024 * 1024 + 1)
            os.write(parent_input_fd, payload)
        finally:
            os.close(parent_input_fd)
        parent_input = None
        stdout_thread = threading.Thread(target=_read_pipe, args=(parent_output, worker_stdout, total_output, int(args.output_bytes), overflow), daemon=True)
        stderr_thread = threading.Thread(target=_read_pipe, args=(parent_error, worker_stderr, total_output, int(args.output_bytes), overflow), daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        deadline = time.monotonic() + int(args.wall_seconds)
        while True:
            wait = api.kernel32.WaitForSingleObject(process.hProcess, 100)
            if overflow.is_set():
                status = "OUTPUT_LIMIT"
                api.terminate(job)
                break
            if wait == WAIT_OBJECT_0:
                break
            if wait != WAIT_TIMEOUT:
                raise LauncherError("wait for AppContainer child", api.error())
            if time.monotonic() >= deadline:
                status = "DEADLINE"
                api.terminate(job)
                break
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
        code = wintypes.DWORD()
        if not api.kernel32.GetExitCodeProcess(process.hProcess, ctypes.byref(code)):
            raise LauncherError("read AppContainer exit code", api.error())
        exit_code = int(code.value)
        if status == "WORKER_FAILED" and exit_code == 0:
            status = "OK"
    finally:
        restore_error = None
        for snapshot in reversed(acl_snapshots):
            try:
                api.restore_acl(snapshot)
            except BaseException as exc:
                restore_error = exc
        if restore_error is not None:
            status = "BLOCKED_PLATFORM"
            exit_code = -1
        if attribute_buffer is not None:
            api.kernel32.DeleteProcThreadAttributeList(attribute_buffer)
        for handle in (input_handle, output_handle, error_handle, parent_input, parent_output, parent_error):
            if handle:
                api.kernel32.CloseHandle(handle)
        if process.hThread:
            api.kernel32.CloseHandle(process.hThread)
        if process.hProcess:
            api.kernel32.CloseHandle(process.hProcess)
        if job:
            api.kernel32.CloseHandle(job)
        api.userenv.DeleteAppContainerProfile(name)
        api.advapi32.FreeSid(sid)

    stdout = b"".join(worker_stdout)
    stderr = b"".join(worker_stderr)
    if status == "WORKER_FAILED" and exit_code in {0xC0000022, 0xC0000142} and not stdout and not stderr:
        status = "BLOCKED_PLATFORM"
    return {
        "schema": SCHEMA,
        "status": status,
        "worker_stdout_b64": base64.b64encode(stdout).decode("ascii"),
        "worker_stderr_tail": stderr.decode("utf-8", "replace")[-2000:],
        "exit_code": exit_code,
        "attestation": {
            "schema_version": SCHEMA,
            "launcher_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "worker_sha256": hashlib.sha256(worker.read_bytes()).hexdigest(),
            "runtime_manifest_sha256": runtime_manifest_sha256,
            "windows_build": platform.version(),
            "appcontainer_sid": sid_text,
            "capabilities": [],
            "network_capabilities": [],
            "command": command,
            "exit_code": exit_code,
            "test_results_sha256": hashlib.sha256(stdout).hexdigest(),
            "job_limits": {
                "active_process_limit": 1,
                "cpu_seconds": profile["cpu_seconds"],
                "memory_mb": profile["memory_mb"],
                "wall_seconds": int(args.wall_seconds),
                "output_bytes": int(args.output_bytes),
            },
            "acl_roots": {
                "read_write": [str(rw_root)],
                "read_only": [str(path) for path in ro_roots],
            },
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--rw-root", required=True)
    parser.add_argument("--ro-root", action="append", default=[])
    parser.add_argument("--cpu-seconds", required=True, type=int)
    parser.add_argument("--memory-mb", required=True, type=int)
    parser.add_argument("--wall-seconds", required=True, type=int)
    parser.add_argument("--output-bytes", required=True, type=int)
    try:
        result = launch(parser.parse_args())
    except BaseException as exc:
        result = {
            "schema": SCHEMA,
            "status": "BLOCKED_PLATFORM",
            "error_stage": getattr(exc, "stage", type(exc).__name__),
            "error_code": getattr(exc, "winerror", None),
        }
    sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
