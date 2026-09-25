import ctypes
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

sys.path[:0] = [str(Path.cwd()), str(Path.cwd() / "scripts")]
import backtest
import scan_project_environment

repo = Path.cwd().parents[1]
def git(*args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return {"exit_code": result.returncode, "stdout": result.stdout.strip()}
fixture = Path("C:/Users/ISAAC/AppData/Local/Temp/otobt-owner-fixture-final-20260922")
link = fixture / "output/escape-symlink.txt"
assert link.is_symlink() and link.resolve(strict=True) == (fixture / "outside/outside.txt").resolve(strict=True)
assert not os.environ.get("PYTEST_ADDOPTS")
assert not (Path.cwd() / "live_forward/super1_xm_mt5_demo_config.json").exists()
assert Path(backtest.__file__).resolve().is_relative_to(Path.cwd())
assert Path(scan_project_environment.__file__).resolve().is_relative_to(Path.cwd())
raw = {}
for line in (Path.cwd() / "data/provenance/first30_pre2025_inputs.sha256").read_text().splitlines():
    expected, name = line.split(None, 1)
    path = Path.cwd() / name.strip().lstrip("*")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    assert actual == expected
    raw[name] = actual
assert len(raw) == 144
print(json.dumps({"python": sys.version, "executable": sys.executable, "platform": platform.platform(),
    "administrator": bool(ctypes.windll.shell32.IsUserAnAdmin()),
    "module_paths": {"backtest": backtest.__file__, "scanner": scan_project_environment.__file__},
    "dependencies": sorted(f"{item.metadata['Name']}=={item.version}" for item in importlib.metadata.distributions()),
    "head": git("rev-parse", "HEAD"), "tree": git("rev-parse", "HEAD^{tree}"),
    "core.autocrlf": git("config", "--get", "core.autocrlf"), "core.eol": git("config", "--get", "core.eol"),
    "tracked_status": git("status", "--short", "--untracked-files=no"),
    "unsafe_objects_absent": {oid: git("cat-file", "-e", oid)["exit_code"] != 0 for oid in (
        "5569132ecf0b0836f93de3df8e2b1438b9c5566f", "31ddd480d06de88f7c5265d3c157c06e182f9500", "54b3fc751bc6c9f232799c23899db8f1038d5db4")},
    "fixture": {"path": str(fixture), "symlink": True, "target": str(link.resolve(strict=True))},
    "raw_count": len(raw), "raw_hashes": raw, "PYTEST_ADDOPTS_present": False,
    "legacy_private_config_present": False}, indent=2))
