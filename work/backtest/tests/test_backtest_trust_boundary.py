from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "backtest"
FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__", "system"}
FORBIDDEN_MODULES = {"importlib", "subprocess", "socket", "requests", "httpx", "urllib", "websocket"}
PRODUCTION_CANONICAL_SCRIPTS = (
    "check_mt5_flat.py",
    "run_anchored_threshold_holdout.py",
    "run_canonical_production_full_history.py",
    "run_canonical_state_ablations.py",
    "run_capital_forward.py",
    "run_engine_path_comparison_2025_feb_mar.py",
    "run_engine_reliability_audit.py",
    "run_forward_shadow.py",
    "run_machine_metric_mechanics.py",
    "run_super1_xm_mt5_forward.py",
    "run_xm_mt5_forward.py",
    "super1_order_approval.py",
    "super1_runtime_guard.py",
    "super1_terminal_r.py",
    "verify_canonical_full_history.py",
)


def test_production_backtest_has_no_dynamic_code_or_network_surface() -> None:
    for path in ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in FORBIDDEN_CALLS, path
            if isinstance(node, ast.Attribute) and node.attr == "system":
                raise AssertionError(path)
            if isinstance(node, ast.Import):
                assert all(alias.name.split(".", 1)[0] not in FORBIDDEN_MODULES for alias in node.names), path
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".", 1)[0] not in FORBIDDEN_MODULES, path


def test_production_and_canonical_scripts_use_explicit_ast_allowlist() -> None:
    scripts_root = ROOT.parent / "scripts"
    for name in PRODUCTION_CANONICAL_SCRIPTS:
        path = scripts_root / name
        assert path.is_file(), path
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in FORBIDDEN_CALLS, path
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "system" and isinstance(node.func.value, ast.Name):
                    assert node.func.value.id != "os", path
