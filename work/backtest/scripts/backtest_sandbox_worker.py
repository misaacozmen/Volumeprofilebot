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
sys.path.insert(0, str(PROJECT_ROOT))

# Import trusted strategy dependencies before installing the deny hook. The hook
# then governs user-triggered execution and any lazy broker/network imports.
from backtest import cli as trusted_cli


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def deny_hook(event, args):
    if event == "import" and str(args[0]).startswith(("backtest.live", "MetaTrader5")):
        raise PermissionError("live/broker imports are forbidden in strategy sandbox")
    if event.startswith("socket.") or event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.spawn", "ctypes.dlopen"}:
        raise PermissionError("network, native loading, and child processes are forbidden")
    if ((event == "compile" and len(args) > 1 and str(args[1]) == "<string>") or
            (event == "exec" and getattr(args[0], "co_filename", "") == "<string>")):
        caller = Path(sys._getframe(1).f_code.co_filename).resolve()
        if caller == Path(__file__).resolve() or PROJECT_ROOT == caller or PROJECT_ROOT in caller.parents:
            raise PermissionError("dynamic code execution is forbidden")


def main() -> int:
    try:
        request = json.loads(sys.stdin.buffer.read(4 * 1024 * 1024 + 1))
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        action = request.get("action")
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
            if "--output-dir" not in argv or Path(str(argv[argv.index("--output-dir") + 1])).resolve() != output_root:
                raise ValueError("CLI output path is not contained by the staging root")
            sys.argv = ["otobt", *[str(item) for item in argv]]
            capture = io.StringIO()
            with contextlib.redirect_stdout(capture):
                trusted_cli.main(_trusted=True)
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
