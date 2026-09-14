from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_super1_v4_allowlist_staged_tree_imports_and_dry_starts_no_send(tmp_path) -> None:
    allowlist = json.loads((ROOT / "deploy/release_payload_allowlist.json").read_text(encoding="utf-8"))["profiles"]["super1"]["files"]
    forbidden = ("super1_unsigned_candidate_v2", "super1_signal_contract_v2", "instrument_registry_v2", "us_equity_rth_2022_2026_v2")
    assert not any(any(marker in path.lower() for marker in forbidden) for path in allowlist)
    for relative in allowlist:
        source = ROOT / relative
        if relative == "requirements-windows.lock":
            continue
        assert source.is_file(), relative
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    code = f"""
import sys
sys.path.insert(0, {str(tmp_path)!r})
import scripts.run_super1_xm_mt5_forward as runner
runner.validate_super1_candidate(runner.core.read_json(runner.RUNTIME_CONFIG))
runner.configure_core()
try:
    runner.core.runtime_config()
except Exception as exc:
    assert 'no-send' in str(exc).lower()
else:
    raise AssertionError('private binding absence did not fail closed')
assert 'MetaTrader5' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-I", "-E", "-B", "-c", code],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
