from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prepare_symlink_fixture.py"


def _powershell_executable() -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        raise AssertionError("PowerShell is required to validate New-Item metadata")
    return executable


def test_preparer_uses_supported_new_item_parameters_in_a_fresh_fixture(tmp_path: Path) -> None:
    fixture_root = tmp_path / "fresh-item11-fixture"
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(fixture_root)],
        check=True,
        capture_output=True,
        text=True,
    )
    lines = completed.stdout.splitlines()
    assert len(lines) == 3
    description = json.loads(lines[0])
    assert lines[1] == "OWNER_COMMAND:"
    command = lines[2]

    metadata = subprocess.run(
        [
            _powershell_executable(),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$p=(Get-Command New-Item -ErrorAction Stop).Parameters; "
            "[pscustomobject]@{has_path=$p.ContainsKey('Path'); "
            "has_value=$p.ContainsKey('Value')} | ConvertTo-Json -Compress",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    parameters = json.loads(metadata.stdout)
    assert parameters == {"has_path": True, "has_value": True}

    assert "New-Item -ItemType SymbolicLink -Path $link -Value $target -ErrorAction Stop" in command
    assert "New-Item -ItemType SymbolicLink -LiteralPath" not in command
    assert "-Target $target" not in command
    assert "Resolve-Path" not in command

    input_file = Path(description["input_file"])
    outside_file = Path(description["outside_file"])
    link = Path(description["symlink"])
    assert input_file.is_file()
    assert outside_file.is_file()
    assert not link.exists()
    assert not link.is_symlink()
