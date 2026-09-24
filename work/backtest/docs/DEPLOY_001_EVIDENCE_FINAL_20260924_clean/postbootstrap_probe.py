import ast
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path.cwd()))
import backtest.sandbox as sandbox

root = Path(__file__).resolve().parent / "native-probe-final"
root.mkdir(exist_ok=False)
source_path = Path("scripts/backtest_sandbox_worker.py")
original = source_path.read_text(encoding="utf-8")
dispatch = "                    trusted_cli.main(_trusted=True)"
probe = '''                    import ctypes
                    print(json.dumps({"phase": NATIVE_LOAD_PHASE, "probes": [
                        _attempt("direct", lambda: ctypes.CDLL("kernel32.dll")),
                        _attempt("trusted_wrapper", lambda: ctypes.cdll.LoadLibrary("kernel32.dll")),
                    ]}))'''
assert original.count(dispatch) == 1
results = {}
for mode in ("success", "import_failure"):
    modified = original.replace(dispatch, probe)
    if mode == "import_failure":
        call = "                trusted_cli = _bootstrap_cli_runtime()"
        injection = '''                import builtins
                original_import = builtins.__import__
                def fail_cli(name, globals=None, locals=None, fromlist=(), level=0):
                    if name == "backtest" and "cli" in fromlist:
                        original_import("ctypes")
                        raise ImportError("injected bootstrap failure")
                    return original_import(name, globals, locals, fromlist, level)
                builtins.__import__ = fail_cli
                try:
                    _bootstrap_cli_runtime()
                except ImportError:
                    pass
                else:
                    raise AssertionError("bootstrap failure was not observed")
                finally:
                    builtins.__import__ = original_import'''
        assert modified.count(call) == 1
        modified = modified.replace(call, injection)
    hooks = {}
    for name in ("deny_hook", "_bootstrap_cli_runtime", "_bootstrap_dll_is_allowlisted"):
        def dump(source):
            return ast.dump(next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == name))
        assert dump(original) == dump(modified)
        hooks[name] = hashlib.sha256(dump(original).encode()).hexdigest()
    worker = root / (mode + "_worker.py")
    worker.write_text(modified, encoding="utf-8", newline="\n")
    output = root / (mode + "_output")
    output.mkdir()
    sandbox._worker_path = lambda: worker
    response = sandbox.run_sandbox({"action": "cli", "output_root": str(output), "argv": ["--output-dir", str(output)]})
    probes = json.loads(response["stdout"])
    assert probes["phase"] == "dispatch"
    assert all(not item["allowed"] and item["error_type"] == "PermissionError" for item in probes["probes"])
    assert response["sandbox_attestation"]["capabilities"] == []
    results[mode] = {"response": response, "probes": probes, "production_hook_ast_sha256": hooks,
                     "worker_sha256": hashlib.sha256(worker.read_bytes()).hexdigest()}
results["source_sha256"] = hashlib.sha256(source_path.read_bytes()).hexdigest()
(root / "result.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"status": "PASS", "bootstrap_success": "direct and wrapper denied", "bootstrap_failure": "direct and wrapper denied", "production_hooks_unchanged": True}))
