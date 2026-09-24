"""Isolated worker entry point. It accepts and emits canonical JSON only."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
import os
import sys
import traceback

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRUSTED_RUNTIME_ROOT = Path(sys.base_prefix).resolve()
sys.path.insert(0, str(PROJECT_ROOT))

# Native loads are closed by default.  The only temporary exception is the
# explicit import bootstrap below, where the pinned CLI dependency graph may
# request these known Windows runtime DLLs.  No user/strategy dispatch runs in
# that phase, and the phase is closed before the CLI entry point is called.
NATIVE_LOAD_PHASE = "dispatch"
BOOTSTRAP_DLL_NAMES = frozenset({"kernel32", "kernel32.dll", "user32", "user32.dll", "tzres.dll"})

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def _bootstrap_dll_is_allowlisted(args) -> bool:
    if len(args) != 1 or not isinstance(args[0], str):
        return False
    return Path(args[0]).name.casefold() in BOOTSTRAP_DLL_NAMES


def deny_hook(event, args):
    if event == "import" and str(args[0]).startswith(("backtest.live", "MetaTrader5")):
        raise PermissionError("live/broker imports are forbidden in strategy sandbox")
    if event == "ctypes.dlopen":
        if NATIVE_LOAD_PHASE == "bootstrap" and _bootstrap_dll_is_allowlisted(args):
            return
        raise PermissionError("native loading is forbidden after the trusted bootstrap")
    if event.startswith("socket.") or event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.spawn"}:
        raise PermissionError("network, native loading, and child processes are forbidden")
    if ((event == "compile" and len(args) > 1 and str(args[1]) == "<string>") or
            (event == "exec" and getattr(args[0], "co_filename", "") == "<string>")):
        caller = Path(sys._getframe(1).f_code.co_filename).resolve()
        trusted_runtime_code = TRUSTED_RUNTIME_ROOT == caller or TRUSTED_RUNTIME_ROOT in caller.parents
        if not trusted_runtime_code:
            raise PermissionError("dynamic code execution is forbidden")


def _attempt(label, operation):
    try:
        operation()
    except BaseException as exc:
        return {
            "label": label,
            "allowed": False,
            "error_type": type(exc).__name__,
            "winerror": getattr(exc, "winerror", None),
            "errno": getattr(exc, "errno", None),
        }
    return {"label": label, "allowed": True, "error_type": None, "winerror": None, "errno": None}


def _probe_os_boundaries(request):
    """Run OS-boundary probes without the Python audit-hook safety net.

    This action is trusted acceptance code.  Its purpose is to prove that the
    AppContainer/ACL boundary, rather than only the worker audit hook, rejects
    the escape attempts.
    """
    import socket
    import subprocess

    paths = request.get("probe_paths")
    if not isinstance(paths, dict) or not paths:
        raise ValueError("probe_paths must be a non-empty object")
    results = []
    for label, value in sorted(paths.items()):
        path = Path(str(value))
        if not path.is_absolute():
            raise ValueError("probe path must be absolute")
        results.append(_attempt(f"filesystem:{label}", lambda path=path: path.read_bytes()))

    write_path = Path(str(request.get("write_path", "")))
    if not write_path.is_absolute():
        raise ValueError("write_path must be absolute")
    results.append(_attempt("filesystem:explicit_staging_write", lambda: write_path.write_bytes(b"sandbox-positive")))

    def bind_ipv4():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))

    def bind_ipv6():
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            sock.bind(("::1", 0))

    results.append(_attempt("network:ipv4_loopback_bind", bind_ipv4))
    results.append(_attempt("network:ipv6_loopback_bind", bind_ipv6))
    results.append(_attempt("network:dns", lambda: socket.getaddrinfo("example.com", 80)))
    results.append(_attempt("process:child", lambda: subprocess.run([sys.executable, "-c", "pass"], check=True)))
    return {"os_boundary_results": results}


def _bootstrap_cli_runtime():
    global NATIVE_LOAD_PHASE
    NATIVE_LOAD_PHASE = "bootstrap"
    try:
        from backtest import cli
    finally:
        NATIVE_LOAD_PHASE = "dispatch"
    return cli


def main() -> int:
    try:
        request = json.loads(sys.stdin.buffer.read(4 * 1024 * 1024 + 1))
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        action = request.get("action")
        if action != "probe_os_boundaries":
            sys.addaudithook(deny_hook)
        if action == "probe_env":
            result = {"environment": dict(os.environ)}
        elif action == "probe_network":
            import socket
            socket.create_connection(("127.0.0.1", 1), timeout=0.1)
            result = {}
        elif action == "probe_subprocess":
            import subprocess
            subprocess.run([sys.executable, "-c", "pass"], check=True)
            result = {}
        elif action == "probe_live_import":
            import backtest.live
            result = {}
        elif action == "probe_dynamic":
            eval("1 + 1")
            result = {}
        elif action == "probe_native_load":
            import ctypes
            ctypes.CDLL("kernel32.dll")
            result = {}
        elif action == "probe_native_load_via_wrapper":
            import ctypes
            ctypes.cdll.LoadLibrary("kernel32.dll")
            result = {}
        elif action == "probe_os_boundaries":
            result = _probe_os_boundaries(request)
        elif action == "loop":
            while True:
                pass
        elif action == "memory":
            chunks = []
            while True:
                chunks.append(bytearray(16 * 1024 * 1024))
        elif action == "cli":
            argv = request.get("argv")
            output_root = Path(str(request.get("output_root", ""))).resolve()
            if not isinstance(argv, list) or not output_root.is_absolute():
                raise ValueError("invalid CLI request")
            if "--output-dir" not in argv:
                raise ValueError("CLI output path is not contained by the staging root")
            cli_output = Path(str(argv[argv.index("--output-dir") + 1])).resolve()
            try:
                cli_output.relative_to(output_root)
            except ValueError as exc:
                raise ValueError("CLI output path is not contained by the staging root") from exc
            sys.argv = ["otobt", *[str(item) for item in argv]]
            capture = io.StringIO()
            with contextlib.redirect_stdout(capture):
                trusted_cli = _bootstrap_cli_runtime()

                try:
                    trusted_cli.main(_trusted=True)
                except SystemExit as exc:
                    if exc.code not in (None, 0):
                        raise
            result = {"stdout": capture.getvalue()}
        else:
            raise ValueError("unknown sandbox action")
        response = {"ok": True, **result}
        response["response_hash"] = hashlib.sha256(canonical(response)).hexdigest()
        sys.stdout.buffer.write(canonical(response))
        return 0
    except BaseException as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
