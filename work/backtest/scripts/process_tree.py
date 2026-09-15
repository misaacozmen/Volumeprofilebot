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
        limits.BasicLimitInformation.LimitFlags = 0x00002000 | 0x00000008
        limits.BasicLimitInformation.ActiveProcessLimit = 1
        if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            kernel32.CloseHandle(handle)
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
        process_handle = kernel32.OpenProcess(0x0200 | 0x0400, False, int(pid))
        if not process_handle:
            kernel32.CloseHandle(handle)
            raise OSError(ctypes.get_last_error(), "OpenProcess failed")
        try:
            if not kernel32.AssignProcessToJobObject(handle, process_handle):
                kernel32.CloseHandle(handle)
                return
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
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            os.kill(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass


def run_bounded(command: list[str], *, timeout_seconds: int) -> tuple[int, bytes, bytes]:
    if int(timeout_seconds) <= 0:
        raise ValueError("child process timeout must be positive")
    flags = 0x00000200 if os.name == "nt" else 0
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
            except OSError:
                # A host-owned outer Job Object can forbid nesting; taskkill /T
                # remains the closed process-tree cleanup fallback.
                job = None
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
