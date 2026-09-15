"""Bounded child-process execution with Windows Job Object cleanup."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import signal
import subprocess
from typing import Any


class _WindowsJob:
    def __init__(self, pid: int) -> None:
        self.handle: Any | None = None
        if os.name != "nt":
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")

        class BasicLimit(ctypes.Structure):
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

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimit),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        limits = ExtendedLimit()
        # KILL_ON_JOB_CLOSE is the tree boundary.  Do not impose an active
        # process count of one: the downloader may legitimately create
        # descendants, which must remain inside this same Job Object.
        limits.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            kernel32.CloseHandle(handle)
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
        process_handle = kernel32.OpenProcess(0x0200 | 0x0400, False, int(pid))
        if not process_handle:
            kernel32.CloseHandle(handle)
            raise OSError(ctypes.get_last_error(), "OpenProcess failed")
        try:
            if not kernel32.AssignProcessToJobObject(handle, process_handle):
                error = ctypes.get_last_error()
                kernel32.CloseHandle(handle)
                raise OSError(error, "AssignProcessToJobObject failed")
        finally:
            kernel32.CloseHandle(process_handle)
        self.handle = (kernel32, handle)

    def terminate(self) -> None:
        if self.handle is not None:
            kernel32, handle = self.handle
            kernel32.TerminateJobObject(handle, 1)

    def close(self) -> None:
        if self.handle is not None:
            kernel32, handle = self.handle
            kernel32.CloseHandle(handle)
            self.handle = None


def terminate_process_tree(pid: int, job: _WindowsJob | None = None) -> None:
    if job is not None and os.name == "nt":
        job.terminate()
        return
    if os.name == "nt":
        raise RuntimeError("Windows process-tree cleanup requires a successfully assigned Job Object")
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            os.kill(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass


def _resume_suspended_windows_process(pid: int) -> None:
    """Resume the primary thread only after the process is inside its Job Object."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
    invalid = ctypes.c_void_p(-1).value
    if snapshot == invalid:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")

    class ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD), ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG), ("dwFlags", wintypes.DWORD),
        ]

    entry = ThreadEntry32()
    entry.dwSize = ctypes.sizeof(entry)
    found = False
    try:
        if not kernel32.Thread32First(snapshot, ctypes.byref(entry)):
            raise OSError(ctypes.get_last_error(), "Thread32First failed")
        while True:
            if int(entry.th32OwnerProcessID) == int(pid):
                thread = kernel32.OpenThread(0x0002, False, entry.th32ThreadID)  # THREAD_SUSPEND_RESUME
                if not thread:
                    raise OSError(ctypes.get_last_error(), "OpenThread failed")
                try:
                    result = kernel32.ResumeThread(thread)
                    if result == 0xFFFFFFFF:
                        raise OSError(ctypes.get_last_error(), "ResumeThread failed")
                    found = True
                finally:
                    kernel32.CloseHandle(thread)
                break
            if not kernel32.Thread32Next(snapshot, ctypes.byref(entry)):
                break
        if not found:
            raise OSError("suspended process primary thread was not found")
    finally:
        kernel32.CloseHandle(snapshot)


def run_bounded(command: list[str], *, timeout_seconds: int) -> tuple[int, bytes, bytes]:
    if int(timeout_seconds) <= 0:
        raise ValueError("child process timeout must be positive")
    # Break away from an enclosing launcher job when permitted, then put the
    # still-suspended child into our own kill-on-close job.  If the host job
    # denies breakaway, startup below fails closed rather than running loose.
    flags = (0x00000004 | 0x01000000) if os.name == "nt" else 0  # CREATE_SUSPENDED | CREATE_BREAKAWAY_FROM_JOB
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        start_new_session=(os.name != "nt"),
        creationflags=flags,
    )
    job: _WindowsJob | None = None
    try:
        if os.name == "nt":
            try:
                job = _WindowsJob(process.pid)
                _resume_suspended_windows_process(process.pid)
            except Exception as exc:
                # The child has not been resumed yet.  A direct process handle
                # termination is sufficient for this fail-closed startup path;
                # no untracked running process may continue after assignment
                # failure.
                try:
                    process.kill()
                finally:
                    process.wait(timeout=10)
                raise RuntimeError("Windows child Job Object assignment or resume failed") from exc
        try:
            stdout, stderr = process.communicate(timeout=int(timeout_seconds))
        except subprocess.TimeoutExpired as error:
            terminate_process_tree(process.pid, job)
            try:
                stdout, stderr = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=command,
                output=stdout + b"\nTimed out after " + str(timeout_seconds).encode("ascii") + b" seconds",
                stderr=stderr,
            ) from error
        return int(process.returncode), stdout, stderr
    finally:
        if process.poll() is None:
            terminate_process_tree(process.pid, job)
            process.wait(timeout=10)
        if job is not None:
            job.close()
